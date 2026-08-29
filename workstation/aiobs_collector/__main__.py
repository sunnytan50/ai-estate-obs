"""Collector entrypoint: `python3 -m aiobs_collector --config PATH [--backfill] [--dry-run]`.

Loads config + state, builds the enabled lanes named in `AIOBS_LANES`, runs
them through the frozen `run_lanes` harness (Task 8), filters what to push
(or takes everything, under `--backfill`), then either prints the exposition
(`--dry-run`) or POSTs it to VictoriaMetrics via `push.push_samples` (Task 11).

State (`AIOBS_STATE_DIR`) is only written after a successful push: a failed
push leaves state untouched, so the next cycle simply retries with the same
baseline. Re-pushing is always safe -- VictoriaMetrics's import endpoint is
idempotent on identical series+timestamp -- so retrying costs nothing but a
little wasted bandwidth, never correctness.

Exit codes: 0 = every enabled lane pushed, OR some lane(s) failed to collect
but the push of whatever data did exist still succeeded (a lane failure is
DATA -- the `aiobs_lane_up{lane=}` sample carries it into the dashboard, it
is not this process's failure). 1 = the push itself failed (network/hub
problem). 2 = a configuration problem (bad --config path, unknown lane name
in AIOBS_LANES, a required cfg key missing) -- caught before any network
call is attempted.
"""

import argparse
import sys
import time
from datetime import datetime

from aiobs_collector.core import (
    load_config,
    load_state,
    render_exposition,
    run_lanes,
    save_state,
)
from aiobs_collector.lane_openrouter import OpenRouterLane
from aiobs_collector.lane_tokscale import TokscaleLane
from aiobs_collector.push import push_samples

# name (as it appears in AIOBS_LANES) -> zero-arg-constructible Lane class.
# Both TokscaleLane and OpenRouterLane take no constructor args -- cfg is
# passed later, to .collect(cfg, state), not to __init__.
_KNOWN_LANES = {"tokscale": TokscaleLane, "openrouter": OpenRouterLane}

# These two are the run_lanes harness's own self-health samples (Task 8):
# always timestamped `now_ms` on every run, never part of a lane's day-
# granular history, so they are always pushed and never subject to the
# past-day dedupe filter below.
_ALWAYS_PUSH_METRICS = {"aiobs_lane_up", "aiobs_lane_last_success_timestamp"}


class ConfigError(Exception):
    """A user-facing configuration problem. Caught by main(); exits 2."""


def _build_lanes(cfg: dict) -> list:
    """`AIOBS_LANES` (comma list) -> instantiated Lane objects, in order.

    Blank entries (leading/trailing/doubled commas) are ignored. An unknown
    name raises `ConfigError` naming the bad entry and the known set -- per
    contract, this is a hard error, not a skip-and-continue.
    """
    raw = cfg.get("AIOBS_LANES", "") or ""
    names = [part.strip() for part in raw.split(",") if part.strip()]
    lanes = []
    for name in names:
        cls = _KNOWN_LANES.get(name)
        if cls is None:
            known = ", ".join(sorted(_KNOWN_LANES))
            raise ConfigError(f"unknown lane '{name}' in AIOBS_LANES (known lanes: {known})")
        lanes.append(cls())
    return lanes


def _local_midnight_ms(now_ms: int) -> int:
    """Local midnight (00:00:00.000) of the calendar day `now_ms` falls on,
    as epoch ms. Same naive-datetime-is-local-time trick the lanes'
    `_end_of_day_local_ms` helpers use, at the other end of the day.
    """
    dt = datetime.fromtimestamp(now_ms / 1000)
    midnight = datetime(dt.year, dt.month, dt.day)
    return int(midnight.timestamp() * 1000)


def _lane_for_sample(sample) -> str | None:
    """Attribute a data Sample back to the lane that produced it, for
    per-lane push-dedupe state.

    Only two lanes exist today. `openrouter` always labels its own samples
    `provider="openrouter"` -- a lane-specific constant never emitted by
    tokscale's client-name mapping/sanitizer (see lane_tokscale.py's
    `_map_provider`/`_sanitize_provider`: no real coding-assistant client is
    named "openrouter"). Everything else carrying a `provider` label is
    therefore tokscale's. Returns None for samples with no `provider` label
    at all (the harness self-health metrics, or any future metric shape
    this mapping doesn't anticipate) -- callers must treat None as "cannot
    attribute, do not silently drop."
    """
    provider = sample.labels.get("provider")
    if provider is None:
        return None
    return "openrouter" if provider == "openrouter" else "tokscale"


def _past_day_tallies(samples, today_start_ms):
    """This run's past-day (ts_ms < today_start_ms) lane-data samples,
    tallied per (lane, ts_ms) -> count and per lane -> max ts_ms seen.

    Shared by `filter_for_push` (compares against the *prior* run's stored
    high-water mark) and `compute_push_state` (produces the *next* stored
    high-water mark) so the two can never disagree about what "this run's
    past-day data" means.
    """
    counts: dict[tuple[str, int], int] = {}
    max_ts: dict[str, int] = {}
    for sample in samples:
        if sample.metric in _ALWAYS_PUSH_METRICS or sample.ts_ms >= today_start_ms:
            continue
        lane = _lane_for_sample(sample)
        if lane is None:
            continue
        key = (lane, sample.ts_ms)
        counts[key] = counts.get(key, 0) + 1
        if sample.ts_ms > max_ts.get(lane, -1):
            max_ts[lane] = sample.ts_ms
    return counts, max_ts


def filter_for_push(samples: list, state: dict, now_ms: int, backfill: bool) -> list:
    """Select which of this run's samples to actually push.

    `--backfill` pushes everything, unconditionally -- the escape hatch for
    "just send it all," always safe because VM's import is idempotent.

    Otherwise: today's samples (`ts_ms >= local midnight`) and the harness's
    own lane-health samples always go, matching the contract's "always
    re-push today." A past-day, per-lane data sample is skipped only when
    it is strictly older than that lane's previously-recorded high-water
    mark, or exactly *at* that mark with no more same-timestamp samples this
    run than were seen when the mark was recorded (the "+a count" half of
    the design: catches a boundary day that grew a new row since it was
    last considered fully pushed). A sample this module cannot attribute to
    a lane (no `provider` label, and not one of the always-push metrics) is
    included defensively rather than silently dropped -- keeping the state
    small is a volume optimization, never an excuse to lose data.
    """
    if backfill:
        return list(samples)

    today_start_ms = _local_midnight_ms(now_ms)
    counts, _max_ts = _past_day_tallies(samples, today_start_ms)

    selected = []
    for sample in samples:
        if sample.metric in _ALWAYS_PUSH_METRICS or sample.ts_ms >= today_start_ms:
            selected.append(sample)
            continue
        lane = _lane_for_sample(sample)
        if lane is None:
            selected.append(sample)
            continue
        high_water_ts = state.get(f"push:{lane}:max_ts_ms")
        if high_water_ts is None or sample.ts_ms > high_water_ts:
            selected.append(sample)
        elif sample.ts_ms == high_water_ts:
            high_water_count = state.get(f"push:{lane}:max_ts_count", 0)
            if counts.get((lane, sample.ts_ms), 0) > high_water_count:
                selected.append(sample)
        # else sample.ts_ms < high_water_ts: strictly older than the
        # recorded mark -- already fully pushed in a prior run, skip.
    return selected


def compute_push_state(samples: list, prior_state: dict, now_ms: int) -> dict:
    """The state to persist after a successful push: `prior_state` (which
    already carries run_lanes' own `lane:<name>:last_success_ms` bookkeeping)
    layered with each lane's new high-water mark, computed from THIS run's
    full (unfiltered) sample set -- so a `--backfill` run's success still
    establishes a correct baseline for the next normal-cadence run.

    A lane that contributed zero past-day samples this run (collection
    failed, or it simply has no history yet) gets no key written here --
    its previous high-water mark, if any, is left exactly as `prior_state`
    already has it, in the copy this function returns.
    """
    today_start_ms = _local_midnight_ms(now_ms)
    counts, max_ts = _past_day_tallies(samples, today_start_ms)

    new_state = dict(prior_state)
    for lane, ts in max_ts.items():
        new_state[f"push:{lane}:max_ts_ms"] = ts
        new_state[f"push:{lane}:max_ts_count"] = counts[(lane, ts)]
    return new_state


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="aiobs_collector", description="Push AI-estate token/cost samples to VictoriaMetrics."
    )
    parser.add_argument("--config", required=True, help="path to estate.env")
    parser.add_argument(
        "--backfill", action="store_true", help="push all history, ignoring the push-dedupe state"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the exposition instead of pushing; no state write"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    try:
        cfg = load_config(args.config)
    except OSError as exc:
        print(f"aiobs_collector: cannot read config {args.config}: {exc}", file=sys.stderr)
        return 2

    try:
        lanes = _build_lanes(cfg)
    except ConfigError as exc:
        print(f"aiobs_collector: {exc}", file=sys.stderr)
        return 2

    state_dir = cfg.get("AIOBS_STATE_DIR") or ""
    if not state_dir:
        print("aiobs_collector: AIOBS_STATE_DIR is not set in config", file=sys.stderr)
        return 2

    state = load_state(state_dir)
    now_ms = int(time.time() * 1000)
    samples, new_state = run_lanes(lanes, cfg, state, now_ms)
    to_push = filter_for_push(samples, state, now_ms, backfill=args.backfill)

    if args.dry_run:
        exposition = render_exposition(to_push)
        if exposition:
            print(exposition)
        return 0

    hub_ip = cfg.get("AIOBS_HUB_TAILNET_IP") or ""
    vm_port = cfg.get("AIOBS_VM_PORT") or ""
    if not hub_ip or not vm_port:
        print(
            "aiobs_collector: AIOBS_HUB_TAILNET_IP and AIOBS_VM_PORT must both be set in config",
            file=sys.stderr,
        )
        return 2
    vm_base_url = f"http://{hub_ip}:{vm_port}"

    try:
        push_samples(vm_base_url, to_push)
    except Exception as exc:
        print(f"aiobs_collector: push failed: {exc}", file=sys.stderr)
        return 1

    lane_names = ", ".join(sorted(lane.name for lane in lanes)) or "none"
    print(f"aiobs_collector: pushed {len(to_push)} samples to {vm_base_url} (lanes: {lane_names})")

    final_state = compute_push_state(samples, new_state, now_ms)
    save_state(state_dir, final_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
