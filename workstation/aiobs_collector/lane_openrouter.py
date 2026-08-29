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


def normalize_openrouter(doc: dict, now_ms: int) -> list[Sample]:
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
    """
    rows = doc.get("data")
    if not isinstance(rows, list):
        return []

    daily: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_str = row.get("date")
        model = row.get("model")
        if not date_str or not model:
            continue
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
        deltas = daily[(date_str, model)]
        ts_ms = now_ms if date_str == today_str else _end_of_day_local_ms(date_str)

        for kind, field in (("input", "input"), ("output", "output")):
            delta = deltas[field]
            if not delta:
                continue  # zero/missing that day -- nothing new to report
            key = (model, kind)
            token_running[key] = token_running.get(key, 0.0) + delta
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
            cost_running[model] = cost_running.get(model, 0.0) + cost_delta
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
    """Lane: direct OpenRouter token/cost usage, via the official activity API."""

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
        return normalize_openrouter(doc, now_ms)
