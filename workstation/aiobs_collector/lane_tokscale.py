"""tokscale lane: client-side AI coding-assistant token/cost usage.

Shells out to the pinned `tokscale` CLI (via `npx`) for its day-granular
contribution-graph export, then normalizes that into `Sample`s per the
frozen metric contract (spec section 5):

    aiobs_tokens_total{provider,model,kind,origin="client"}   (cumulative)
    aiobs_cost_usd_total{provider,model,origin="client"}      (cumulative, where cost exists)

`kind` is one of input | output | cache_read | cache_write only -- tokscale
also reports a `reasoning` token count, but that has no home in the frozen
kind enum, so it is dropped, never mapped onto one of the four kinds.

Real-shape note (differs from the plan brief's assumption): the brief's
Step 1 called for `tokscale --json` as the raw capture. That top-level
`--json` report (verified live, 2026-08-29 against tokscale 4.14.0) is an
all-time grand total grouped by client+model with **no date dimension at
all** -- unusable for the day-granular history this lane must produce.
The `tokscale graph` subcommand ("Export contribution graph data as JSON")
is the actual day-bucketed source: its top-level `contributions` array has
one entry per calendar day, each carrying a `clients` array of per
(client, modelId) token/cost rows for that day. This lane reads
`graph --no-spinner` (JSON on stdout; the `--output <file>` form the brief
sketched is unnecessary since stdout is already clean JSON with no banner
text once `--no-spinner` suppresses the progress UI).
"""

import json
import re
import subprocess
import time
from datetime import datetime

from aiobs_collector.core import Sample

# tokscale client name -> canonical provider label (spec: providers mapped
# to claude-code, codex, cursor, droid, hermes). Any other client tokscale
# ever adds passes through sanitized rather than being dropped or raising.
_CLIENT_TO_PROVIDER = {
    "claude": "claude-code",
    "codex": "codex",
    "cursor": "cursor",
    "droid": "droid",
    "hermes": "hermes",
}

# tokscale's per-entry `tokens` field name -> our frozen kind label.
# `reasoning` is deliberately absent: not part of the frozen kind enum, and
# the contract says skip absent/foreign kinds, never invent one.
_KIND_FIELDS = (
    ("input", "input"),
    ("output", "output"),
    ("cacheRead", "cache_read"),
    ("cacheWrite", "cache_write"),
)

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _sanitize_provider(client: str) -> str:
    """lowercase, then every non-alphanumeric character -> '-'."""
    return _NON_ALNUM.sub("-", client.lower())


def _map_provider(client: str) -> str:
    return _CLIENT_TO_PROVIDER.get(client, _sanitize_provider(client))


def _end_of_day_local_ms(date_str: str) -> int:
    """23:59:59.999 LOCAL time for a `YYYY-MM-DD` string, as epoch ms.

    A naive datetime's `.timestamp()` is interpreted by Python as local
    time, which is exactly the semantics wanted here (the collector runs
    on the workstation whose local calendar day this is).
    """
    year, month, day = (int(part) for part in date_str.split("-"))
    dt = datetime(year, month, day, 23, 59, 59, 999000)
    return int(dt.timestamp() * 1000)


def _local_date_str(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000).date().isoformat()


def normalize_tokscale(doc: dict, now_ms: int) -> list[Sample]:
    """Pure normalizer: a `tokscale graph` document -> frozen-contract Samples.

    Walks `doc["contributions"]` (one entry per calendar day) in ascending
    date order, accumulating a running total per (provider, model, kind)
    and per (provider, model) for cost. A Sample is emitted only on a day
    that actually moves a given counter (its token/cost value that day is
    non-zero) -- carrying the *cumulative* total through that day, not the
    day's own delta. A counter that never has a non-zero day (e.g. a model
    that never used cache-write) never gets a Sample at all: absent kinds
    are skipped, never invented.

    Timestamps: a day's Samples land at 23:59:59.999 local time for that
    calendar day, except when that day *is* the caller's local "today" (per
    `now_ms`), in which case they carry `now_ms` itself.

    Malformed/missing input (no "contributions", wrong type, a day with no
    "clients", a client row missing its model id, ...) degrades to fewer
    Samples, never an exception -- consistent with the collector's "empty
    lane reports zero, never fails" contract.
    """
    contributions = doc.get("contributions")
    if not isinstance(contributions, list):
        return []

    contributions = sorted(
        (day for day in contributions if isinstance(day, dict) and day.get("date")),
        key=lambda day: day["date"],
    )

    today_str = _local_date_str(now_ms)

    token_running: dict[tuple[str, str, str], float] = {}
    cost_running: dict[tuple[str, str], float] = {}
    samples: list[Sample] = []

    for day in contributions:
        date_str = day["date"]
        ts_ms = now_ms if date_str == today_str else _end_of_day_local_ms(date_str)

        clients = day.get("clients")
        if not isinstance(clients, list):
            continue

        for entry in clients:
            if not isinstance(entry, dict):
                continue
            client = entry.get("client")
            model = entry.get("modelId")
            if not client or not model:
                continue
            provider = _map_provider(client)

            tokens = entry.get("tokens")
            if isinstance(tokens, dict):
                for src_field, kind in _KIND_FIELDS:
                    day_value = tokens.get(src_field)
                    if not day_value:
                        continue  # zero/missing -- no increment, nothing new to report
                    key = (provider, model, kind)
                    token_running[key] = token_running.get(key, 0.0) + float(day_value)
                    samples.append(
                        Sample(
                            metric="aiobs_tokens_total",
                            labels={
                                "provider": provider,
                                "model": model,
                                "kind": kind,
                                "origin": "client",
                            },
                            value=token_running[key],
                            ts_ms=ts_ms,
                        )
                    )

            cost = entry.get("cost")
            if cost:  # zero/missing -- no new cost that day, nothing to report
                cost_key = (provider, model)
                cost_running[cost_key] = cost_running.get(cost_key, 0.0) + float(cost)
                samples.append(
                    Sample(
                        metric="aiobs_cost_usd_total",
                        labels={"provider": provider, "model": model, "origin": "client"},
                        value=cost_running[cost_key],
                        ts_ms=ts_ms,
                    )
                )

    return samples


class TokscaleLane:
    """Lane: client-side token/cost usage across AI coding assistants, via tokscale."""

    name = "tokscale"

    def collect(self, cfg: dict, state: dict) -> list[Sample]:
        version = (cfg.get("AIOBS_TOKSCALE_VERSION") or "").strip()
        if not version:
            raise RuntimeError(
                "AIOBS_TOKSCALE_VERSION is not set -- run `npm view tokscale version` "
                "and pin it in config/estate.env"
            )

        # 300s (not the plan's original 120s): a cold tokscale cache build
        # across a large real transcript history is slow on first run.
        result = subprocess.run(
            ["npx", "-y", f"tokscale@{version}", "graph", "--no-spinner"],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        doc = json.loads(result.stdout)
        now_ms = int(time.time() * 1000)
        return normalize_tokscale(doc, now_ms)
