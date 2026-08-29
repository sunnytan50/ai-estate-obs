"""OpenRouter lane: direct-from-OpenRouter token/cost usage via the official
activity API.

Normalizes `GET /api/v1/activity` responses into `Sample`s per the frozen
metric contract (spec section 5), day-granular, matching lane_tokscale's
cumulative-counter + local-end-of-day-timestamp pattern:

    aiobs_tokens_total{provider="openrouter",model,kind,origin="client"}   (cumulative)
    aiobs_cost_usd_total{provider="openrouter",model,origin="client"}      (cumulative)

`kind` is input | output only here -- OpenRouter's activity rows also carry
a `reasoning_tokens` field, but (same rule as lane_tokscale's `reasoning`)
that has no home in the frozen kind enum, so it is dropped, never mapped
onto `output` or invented as a new kind.

DESIGNED-BLOCKED STATE (as of 2026-08-29): `/api/v1/activity` requires an
OpenRouter *Management API Key* -- a categorically separate credential from
the regular completion keys Hermes uses day to day. OpenRouter's own docs:
"Management keys cannot be used to make API calls to OpenRouter's
completion endpoints ... they are exclusively for administrative
operations" (openrouter.ai/docs/guides/overview/auth/management-api-keys),
and conversely the activity endpoint's documented 403 is "Only management
keys can perform this operation". Every completion key present in
`~/.hermes/.env` at implementation time (`OPENROUTER_API_KEY` and its
per-model siblings `_BACKUP`/`_GEMINI_NANO`/`_GLM51`/`_PRIMARY`) was tried
live against this endpoint and each returned 403 or 401 -- none is a
management key. `AIOBS_OPENROUTER_KEY_NAME` therefore now defaults to
`OPENROUTER_MANAGEMENT_KEY`, a var that does not yet exist in the owner's
env file. Until the owner creates one at
`openrouter.ai/settings/management-keys` and adds it under that name, this
lane is EXPECTED to raise `LaneConfigError` (missing var) or, once some key
is configured under that name but it's still the wrong class,
`LaneAuthError` (401/403) on every run. Task 8's `run_lanes` harness
catches that like any other lane exception, reports
`aiobs_lane_up{lane="openrouter"} 0`, and every other lane keeps running.
This is by design, not a bug.

Real-shape note: the fixture (`tests/fixtures/openrouter_activity.json`) is
therefore built from OpenRouter's *documented* response shape (Mintlify
docs page "Get user activity grouped by endpoint", captured 2026-08-29 via
Firecrawl), not a live capture -- a live capture is exactly the one thing
this account cannot produce (see above); the fixture's own "_provenance"
field says so too. Field mapping: `prompt_tokens` -> `input`,
`completion_tokens` -> `output`, `usage` (already USD) -> cost,
`reasoning_tokens` -> dropped. `model` (OpenRouter's own routing slug, e.g.
"deepseek/deepseek-v4-flash") is used as the `model` label --
`model_permaslug` (a more specific pinned identifier) and `provider_name`
(the *upstream* inference host, e.g. "DeepSeek" -- not our `provider`
label, which is always the constant `"openrouter"`) are both read nowhere:
the same "don't confuse the upstream host with our own label" split
lane_tokscale drew between its `client` and its (ignored) `providerId`.

By default (no `group_by` query param, which this lane never sends),
OpenRouter aggregates activity "by date, model, and endpoint" -- meaning
two rows can legitimately share the same (date, model) if they hit
different `endpoint_id`s. The normalizer sums same-day-same-model rows
before computing the running total, so exactly one input, one output, and
one cost Sample is emitted per (day, model) that actually moved, never one
set per endpoint row.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

from aiobs_collector.core import Sample, load_config

_ACTIVITY_URL = "https://openrouter.ai/api/v1/activity"


class LaneConfigError(Exception):
    """Raised when the OpenRouter lane cannot find a usable key.

    Covers: unset cfg keys, an unreadable/missing key file, or a key file
    that doesn't contain the configured var. The message names the PATH
    and VAR NAME only -- never the secret value (there usually isn't one
    to leak in these cases anyway).
    """


class LaneAuthError(LaneConfigError):
    """Raised when OpenRouter itself rejects the configured key (401/403).

    As of 2026-08-29 this is the DESIGNED, expected outcome until the
    account owner provisions a genuine OpenRouter Management API Key --
    see the module docstring. The message names the PATH, VAR NAME, and
    HTTP status only -- the key value itself is never read into it.
    """


def _end_of_day_local_ms(date_str: str) -> int:
    """23:59:59.999 LOCAL time for a `YYYY-MM-DD` string, as epoch ms.

    Same approach as lane_tokscale's helper of the same name: a naive
    datetime's `.timestamp()` is interpreted by Python as local time,
    which is exactly the semantics wanted here.
    """
    year, month, day = (int(part) for part in date_str.split("-"))
    dt = datetime(year, month, day, 23, 59, 59, 999000)
    return int(dt.timestamp() * 1000)


def _local_date_str(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000).date().isoformat()


def normalize_openrouter(doc: dict, now_ms: int, state: Optional[dict] = None) -> list[Sample]:
    """Pure normalizer: an `/api/v1/activity` document -> frozen-contract Samples.

    First aggregates `doc["data"]` rows into a per (date, model) daily
    delta (summing rows that share a (date, model) but differ only by
    `endpoint_id` -- OpenRouter's default grouping can emit more than one
    row per (date, model), one per endpoint hit that day). Then walks those
    deltas in ascending date order (ties broken by model, for determinism),
    accumulating a running total per (model, kind) and per model for cost.
    A Sample is emitted only on a day that actually moves a given counter
    (non-zero delta that day) -- carrying the *cumulative* total through
    that day, not the day's own delta. `provider` is always the constant
    `"openrouter"`.

    Timestamps: a day's Samples land at 23:59:59.999 local time for that
    calendar day, except when that day *is* the caller's local "today" (per
    `now_ms`), in which case they carry `now_ms` itself.

    Malformed/missing input (no "data", wrong type, a row with no "date" or
    "model", non-dict rows, ...) degrades to fewer Samples, never an
    exception -- same "empty lane reports zero, never fails" contract as
    lane_tokscale.

    `state` (fix-wave finding I3 -- the 30-day-window cumulative-corruption
    fix): OpenRouter's `/api/v1/activity` only ever reports a rolling ~30-day
    window. Recomputing "cumulative to date" from `doc` alone, from scratch,
    every run -- which is exactly what happens when `state` is None/empty,
    preserving this function's original behavior for a caller that has none
    -- silently REGRESSES the reported running total once a day with real
    usage ages out of that window: a genuine Prometheus-counter monotonicity
    violation, not merely a cosmetic wobble. Fix: when given, `state` carries
    forward, from the previous successful run, `openrouter:cum:<model>:<kind>`
    (token running totals; `<kind>` is `input`/`output`) and
    `openrouter:cum:<model>:cost` (cost, using the fixed literal `"cost"` as
    a pseudo-kind for this state key's namespace only -- never written onto
    a Sample's own labels, which stay exactly as before), plus a single
    `openrouter:last_date` watermark: the newest calendar date whose
    contribution has already been folded permanently into those totals.
    Every `daily` bucket dated `<= last_date` is skipped outright here --
    already accounted for, whether or not it is still inside the API's
    current window -- and the running-total accumulators below are SEEDED
    from `state` (lazily, the first time each (model, kind)/model key is
    touched this run) rather than starting at zero.

    Today is never skipped by this filter: `state["openrouter:last_date"]`
    is written by the collector's `__main__.compute_openrouter_state` only
    from PAST-day samples, so it can never advance to include a day that has
    not closed yet -- today's date is therefore always `> last_date`, and
    gets walked (and re-emitted) on every run, same as before. That is what
    keeps today's semantics exactly as they always were: every run re-derives
    "baseline through yesterday (frozen, from `state`) + today's freshly
    refetched full-day total", never a stale partial-day snapshot, and this
    function itself needs no special-casing for "today" beyond the existing
    timestamp rule above -- see `__main__.compute_openrouter_state`'s own
    docstring for the other half of this mechanism (why persistence, not
    this function, is where "today" is excluded, and why that seam was
    chosen over having this lane write into `state` directly).

    Whichever days DO get walked -- typically one historical catch-up day
    (yesterday, now closed) plus today, but possibly more after a gap in
    collector uptime, or none beyond today on a same-day rerun -- behave
    exactly as the paragraphs above describe: ascending order, one Sample
    per (day, model) counter that actually moved, cumulative value carried
    through, end-of-day-local timestamp (or `now_ms` for today).
    """
    rows = doc.get("data")
    if not isinstance(rows, list):
        return []

    state = state or {}
    last_date = state.get("openrouter:last_date") or ""

    daily: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_str = row.get("date")
        model = row.get("model")
        if not date_str or not model:
            continue
        # The live API dates rows as "YYYY-MM-DD HH:MM:SS" (the docs showed
        # bare dates); canonicalize here so grouping keys, the persisted
        # last_date, and end-of-day timestamps all agree on one format.
        date_str = str(date_str).split(" ")[0].split("T")[0]
        bucket = daily.setdefault((date_str, model), {"input": 0.0, "output": 0.0, "cost": 0.0})
        bucket["input"] += float(row.get("prompt_tokens") or 0)
        bucket["output"] += float(row.get("completion_tokens") or 0)
        bucket["cost"] += float(row.get("usage") or 0)
        # reasoning_tokens is deliberately never read here: no home in the
        # frozen kind enum (input|output only) -- dropped, never invented.

    today_str = _local_date_str(now_ms)
    token_running: dict[tuple[str, str], float] = {}
    cost_running: dict[str, float] = {}
    samples: list[Sample] = []

    for date_str, model in sorted(daily.keys()):
        if date_str <= last_date:
            continue  # already folded into state's persisted baseline -- see docstring
        deltas = daily[(date_str, model)]
        ts_ms = now_ms if date_str == today_str else _end_of_day_local_ms(date_str)

        for kind, field in (("input", "input"), ("output", "output")):
            delta = deltas[field]
            if not delta:
                continue  # zero/missing that day -- nothing new to report
            key = (model, kind)
            if key not in token_running:
                # first touch this run -- seed from the persisted baseline,
                # not zero (empty/absent state -> 0.0, same as before)
                token_running[key] = state.get(f"openrouter:cum:{model}:{kind}", 0.0)
            token_running[key] += delta
            samples.append(
                Sample(
                    metric="aiobs_tokens_total",
                    labels={
                        "provider": "openrouter",
                        "model": model,
                        "kind": kind,
                        "origin": "client",
                    },
                    value=token_running[key],
                    ts_ms=ts_ms,
                )
            )

        cost_delta = deltas["cost"]
        if cost_delta:
            if model not in cost_running:
                cost_running[model] = state.get(f"openrouter:cum:{model}:cost", 0.0)
            cost_running[model] += cost_delta
            samples.append(
                Sample(
                    metric="aiobs_cost_usd_total",
                    labels={"provider": "openrouter", "model": model, "origin": "client"},
                    value=cost_running[model],
                    ts_ms=ts_ms,
                )
            )

    return samples


def _fetch_activity(key: str, path: str, var_name: str) -> dict:
    """GET the activity endpoint; raise LaneAuthError on 401/403.

    `path`/`var_name` identify *which configured key* was rejected, for the
    error message -- `key` itself is used solely as the bearer token value
    and is never placed into any exception message or log.
    """
    req = urllib.request.Request(_ACTIVITY_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (401, 403):
            raise LaneAuthError(
                f"OpenRouter rejected key '{var_name}' (from {path}): HTTP {status}. "
                "This endpoint requires an OpenRouter Management API Key, not a "
                "regular completion key -- see the lane_openrouter module docstring."
            ) from None
        raise RuntimeError(f"OpenRouter activity request failed: HTTP {status}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenRouter activity request failed: {exc.reason}") from None


class OpenRouterLane:
    """Lane: direct OpenRouter token/cost usage, via the official activity API.

    `state` (the same dict `run_lanes` hands every lane) is read to seed
    `normalize_openrouter`'s running totals and last-processed-date watermark
    -- fixing the 30-day-window cumulative-corruption bug (I3). This lane
    never writes to `state` itself: a lane-side mutation would not survive
    `run_lanes`/`main()` anyway (see `__main__.compute_openrouter_state`'s
    docstring for why), so persistence happens there instead, derived from
    this lane's returned Samples.
    """

    name = "openrouter"

    def collect(self, cfg: dict, state: dict) -> list[Sample]:
        env_file = (cfg.get("AIOBS_OPENROUTER_ENV_FILE") or "").strip()
        var_name = (cfg.get("AIOBS_OPENROUTER_KEY_NAME") or "").strip()
        if not env_file or not var_name:
            raise LaneConfigError(
                "AIOBS_OPENROUTER_ENV_FILE and AIOBS_OPENROUTER_KEY_NAME must both be "
                "set in config/estate.env"
            )

        path = os.path.expanduser(env_file)
        try:
            key_cfg = load_config(path)
        except OSError:
            raise LaneConfigError(f"OpenRouter key file not found or unreadable: {path}") from None

        key = key_cfg.get(var_name)
        if not key:
            raise LaneConfigError(f"OpenRouter key var '{var_name}' not set in {path}") from None

        try:
            doc = _fetch_activity(key, path, var_name)
        finally:
            key = None  # drop the local reference to the secret promptly

        now_ms = int(time.time() * 1000)
        return normalize_openrouter(doc, now_ms, state)
