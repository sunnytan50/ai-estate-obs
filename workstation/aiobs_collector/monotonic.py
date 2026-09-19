"""Shrink-tolerant cumulative counters.

The lanes rebuild their cumulative Samples from the SOURCE history on every
run: tokscale re-reads whatever transcript/session files exist right now,
OpenRouter re-derives today on top of a persisted baseline. That history is
not append-only. Claude Code's `cleanupPeriodDays` (default 30) purges old
transcripts, Codex sessions vanish, a transient parse can over- then
under-count. Whenever the source shrinks, the recomputed running total for a
series DROPS below what was already pushed -- a counter decrease -- and every
dashboard panel built on

    max_over_time(x[400d]) - max_over_time(x[400d] offset D)

then holds the old peak on its baseline side and under-counts new usage until
the loss is regained. Verified live on 2026-09-19: 18 series had last < max;
claude-opus-5's September read 36.6M tokens against 539.1M in tokscale.

This module makes the pushed counters monotonic without touching the lanes.
Per series (metric + full label set, see `series_key`) it keeps two numbers
under the single state key `monotonic`:

    raw    -- the latest raw value the lanes produced for the series last run
    offset -- everything the source has ever "forgotten" for the series

On each run (`apply_monotonic`): if this run's latest raw value is below
`raw`, the drop is added to `offset`; then every Sample of that series
carries `value + offset`. Emitted values are therefore non-decreasing across
runs whatever the source does (max(raw_new, raw_old) + offset >= raw_old +
offset), and growth after a shrink counts one-to-one. Only the two
cumulative metrics are shaped; gauges pass through untouched.

`--backfill` re-emits history with the offset applied too. VictoriaMetrics
already holds those timestamps, so the past stays as it was recorded and
only what is new takes the shape. Persistence of `monotonic` follows the
rest of the state: written only after a successful push. A failed push
leaves the previous `raw` as the baseline, which is still safe -- a drop
measured against an older baseline banks at least as much, never less.

`seed_offsets_from_peaks` + `fetch_peaks` are the one-off repair for series
that had already shrunk before this module existed: the offset is raised so
that `raw + offset` reaches the peak VictoriaMetrics already stores, and the
counter resumes from where the dashboard's baselines already are. Exposed as
`python3 -m aiobs_collector --config ... --seed-offsets-from-vm`.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from aiobs_collector.core import Sample

CUMULATIVE_METRICS = frozenset({"aiobs_tokens_total", "aiobs_cost_usd_total"})
STATE_KEY = "monotonic"
_PEAKS_QUERY = 'max_over_time({__name__=~"aiobs_tokens_total|aiobs_cost_usd_total"}[400d])'


def series_key(metric: str, labels: dict) -> str:
    """Stable identity of one series: `metric{a="1",b="2"}`, labels sorted."""
    body = ",".join(f'{name}="{labels[name]}"' for name in sorted(labels))
    return f"{metric}{{{body}}}"


def latest_raw_by_series(samples) -> dict:
    """series_key -> value of the newest-timestamped cumulative Sample.

    Ties on timestamp go to the later Sample in list order. Gauges (anything
    outside CUMULATIVE_METRICS) are ignored.
    """
    latest: dict = {}
    for sample in samples:
        if sample.metric not in CUMULATIVE_METRICS:
            continue
        key = series_key(sample.metric, sample.labels)
        current = latest.get(key)
        if current is None or sample.ts_ms >= current[0]:
            latest[key] = (sample.ts_ms, float(sample.value))
    return {key: value for key, (_, value) in latest.items()}


def _table(state: dict) -> dict:
    """A validated, copied `monotonic` table out of `state` (corrupt -> empty)."""
    prior = state.get(STATE_KEY)
    if not isinstance(prior, dict):
        return {}
    table = {}
    for key, entry in prior.items():
        if not isinstance(entry, dict):
            continue
        try:
            table[key] = {"raw": float(entry.get("raw", 0.0)), "offset": float(entry.get("offset", 0.0))}
        except (TypeError, ValueError):
            continue
    return table


def _with_table(state: dict, table: dict) -> dict:
    new_state = dict(state)
    new_state[STATE_KEY] = table
    return new_state


def apply_monotonic(samples, state: dict):
    """Shape this run's cumulative Samples so they never go down across runs.

    Returns `(shaped_samples, new_state)`; the input `state` is never mutated.
    """
    table = _table(state)
    for key, raw_now in latest_raw_by_series(samples).items():
        entry = table.get(key)
        if entry is None:
            table[key] = {"raw": raw_now, "offset": 0.0}
            continue
        offset = entry["offset"]
        if raw_now < entry["raw"]:
            offset += entry["raw"] - raw_now
        table[key] = {"raw": raw_now, "offset": offset}

    shaped = []
    for sample in samples:
        if sample.metric in CUMULATIVE_METRICS:
            offset = table[series_key(sample.metric, sample.labels)]["offset"]
            if offset:
                sample = Sample(
                    metric=sample.metric,
                    labels=dict(sample.labels),
                    value=sample.value + offset,
                    ts_ms=sample.ts_ms,
                )
        shaped.append(sample)
    return shaped, _with_table(state, table)


def seed_offsets_from_peaks(samples, state: dict, peaks: dict):
    """One-off: raise each series' offset so `raw + offset` reaches the peak
    VictoriaMetrics already stores for it (`peaks`: series_key -> value).

    Only series produced by this run are touched; an offset is never lowered.
    Returns `(new_state, seeded)` where `seeded` maps series_key -> the new
    offset for every entry that changed.
    """
    table = _table(state)
    seeded = {}
    for key, raw_now in latest_raw_by_series(samples).items():
        entry = table.get(key) or {"raw": raw_now, "offset": 0.0}
        offset = entry["offset"]
        peak = peaks.get(key)
        if peak is not None and float(peak) > raw_now + offset:
            offset = float(peak) - raw_now
            seeded[key] = offset
        table[key] = {"raw": raw_now, "offset": offset}
    return _with_table(state, table), seeded


def fetch_peaks(vm_base_url: str, opener=urllib.request.urlopen) -> dict:
    """series_key -> max value ever stored, for both cumulative metrics.

    One instant query against VictoriaMetrics' Prometheus-compatible API.
    Raises RuntimeError on transport failure or a non-success status.
    """
    url = vm_base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": _PEAKS_QUERY})
    try:
        with opener(url, timeout=120) as resp:
            doc = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"peak query to {vm_base_url} failed: {exc.reason}") from None
    if not isinstance(doc, dict) or doc.get("status") != "success":
        raise RuntimeError(f"peak query to {vm_base_url} failed: {str(doc)[:200]}")

    peaks = {}
    for row in doc.get("data", {}).get("result", []):
        if not isinstance(row, dict):
            continue
        metric = dict(row.get("metric") or {})
        name = metric.pop("__name__", None)
        if name not in CUMULATIVE_METRICS:
            continue
        try:
            peaks[series_key(name, metric)] = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return peaks
