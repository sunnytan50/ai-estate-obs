"""TDD suite for aiobs_collector.monotonic (shrink-tolerant cumulative counters)
and its wiring through the real main().

Background (verified live 2026-09-19): the lanes rebuild every cumulative
Sample from the SOURCE history on each run, and that history is not
append-only (Claude Code's 30-day transcript cleanup, vanishing Codex
sessions, transient parses). A shrink makes a series' recomputed running
total drop below what was already pushed; every dashboard panel's
`max_over_time(x[400d]) - max_over_time(x[400d] offset D)` then holds the old
peak on its baseline side and under-counts new usage until the loss is
regained (claude-opus-5's September read 36.6M against 539.1M in tokscale).

The fix keeps, per series, the latest raw value seen and an `offset` that
absorbs every drop, and emits `raw + offset`. Runnable from either location:
    python3 -m unittest discover -s workstation/tests -v   # from repo root
    python3 -m unittest discover -s tests -v                # from workstation/
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

import aiobs_collector.__main__ as main_mod  # noqa: E402
from aiobs_collector.core import Sample, load_state, save_state  # noqa: E402
from aiobs_collector.monotonic import (  # noqa: E402
    CUMULATIVE_METRICS,
    STATE_KEY,
    apply_monotonic,
    fetch_peaks,
    latest_raw_by_series,
    seed_offsets_from_peaks,
    series_key,
)


def _local_ms(*args) -> int:
    return int(datetime(*args).timestamp() * 1000)


DAY1 = _local_ms(2026, 8, 20, 23, 59, 59) + 999
DAY2 = _local_ms(2026, 8, 21, 23, 59, 59) + 999
DAY3 = _local_ms(2026, 8, 22, 23, 59, 59) + 999


def _tok(model, kind, value, ts, provider="codex"):
    return Sample(
        metric="aiobs_tokens_total",
        labels={"provider": provider, "model": model, "kind": kind, "origin": "client"},
        value=value,
        ts_ms=ts,
    )


def _cost(model, value, ts, provider="codex"):
    return Sample(
        metric="aiobs_cost_usd_total",
        labels={"provider": provider, "model": model, "origin": "client"},
        value=value,
        ts_ms=ts,
    )


def _gauge(lane, value, ts):
    return Sample(metric="aiobs_lane_up", labels={"lane": lane}, value=value, ts_ms=ts)


ASTRA_IN = series_key("aiobs_tokens_total", {"provider": "codex", "model": "gpt-6-astra", "kind": "input", "origin": "client"})


class SeriesKeyTests(unittest.TestCase):
    def test_labels_sorted_and_quoted(self):
        self.assertEqual(series_key("m", {"b": "2", "a": "1"}), 'm{a="1",b="2"}')

    def test_label_insertion_order_is_irrelevant(self):
        self.assertEqual(series_key("m", {"a": "1", "b": "2"}), series_key("m", {"b": "2", "a": "1"}))

    def test_cost_and_tokens_are_distinct_series(self):
        self.assertNotEqual(
            series_key("aiobs_tokens_total", {"model": "x"}), series_key("aiobs_cost_usd_total", {"model": "x"})
        )


class LatestRawBySeriesTests(unittest.TestCase):
    def test_picks_the_newest_timestamp_per_series(self):
        latest = latest_raw_by_series([_tok("gpt-6-astra", "input", 100.0, DAY1), _tok("gpt-6-astra", "input", 130.0, DAY2)])
        self.assertEqual(latest, {ASTRA_IN: 130.0})

    def test_list_order_does_not_matter(self):
        latest = latest_raw_by_series([_tok("gpt-6-astra", "input", 130.0, DAY2), _tok("gpt-6-astra", "input", 100.0, DAY1)])
        self.assertEqual(latest[ASTRA_IN], 130.0)

    def test_equal_timestamps_later_in_list_wins(self):
        latest = latest_raw_by_series([_tok("gpt-6-astra", "input", 1.0, DAY1), _tok("gpt-6-astra", "input", 2.0, DAY1)])
        self.assertEqual(latest[ASTRA_IN], 2.0)

    def test_gauges_are_ignored(self):
        self.assertEqual(latest_raw_by_series([_gauge("tokscale", 1.0, DAY1)]), {})

    def test_cumulative_metric_set_is_exactly_tokens_and_cost(self):
        self.assertEqual(set(CUMULATIVE_METRICS), {"aiobs_tokens_total", "aiobs_cost_usd_total"})


class ApplyMonotonicTests(unittest.TestCase):
    def test_first_sight_passes_values_through_and_records_raw(self):
        samples = [_tok("gpt-6-astra", "input", 100.0, DAY1), _cost("gpt-6-astra", 5.5, DAY1)]
        shaped, state = apply_monotonic(samples, {})
        self.assertEqual([s.value for s in shaped], [100.0, 5.5])
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 100.0, "offset": 0.0})
        self.assertEqual(len(state[STATE_KEY]), 2)

    def test_input_state_is_never_mutated(self):
        prior = {"other": 1, STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 0.0}}}
        snapshot = json.dumps(prior, sort_keys=True)
        apply_monotonic([_tok("gpt-6-astra", "input", 40.0, DAY2)], prior)
        self.assertEqual(json.dumps(prior, sort_keys=True), snapshot)

    def test_a_drop_is_banked_into_the_offset(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 0.0}}}
        shaped, state = apply_monotonic([_tok("gpt-6-astra", "input", 70.0, DAY2)], prior)
        self.assertEqual(shaped[0].value, 100.0)  # 70 raw + 30 banked
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 70.0, "offset": 30.0})

    def test_growth_after_a_drop_counts_one_to_one(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 70.0, "offset": 30.0}}}
        shaped, state = apply_monotonic([_tok("gpt-6-astra", "input", 80.0, DAY3)], prior)
        self.assertEqual(shaped[0].value, 110.0)
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 80.0, "offset": 30.0})

    def test_equal_or_larger_raw_leaves_offset_alone(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 12.0}}}
        _, same = apply_monotonic([_tok("gpt-6-astra", "input", 100.0, DAY2)], prior)
        _, bigger = apply_monotonic([_tok("gpt-6-astra", "input", 250.0, DAY2)], prior)
        self.assertEqual(same[STATE_KEY][ASTRA_IN]["offset"], 12.0)
        self.assertEqual(bigger[STATE_KEY][ASTRA_IN], {"raw": 250.0, "offset": 12.0})

    def test_offset_applies_to_every_sample_of_the_series_this_run(self):
        # yesterday's day-end sample AND today's live sample both carry the bank
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 0.0}}}
        shaped, _ = apply_monotonic(
            [_tok("gpt-6-astra", "input", 60.0, DAY2), _tok("gpt-6-astra", "input", 70.0, DAY3)], prior
        )
        self.assertEqual([s.value for s in shaped], [90.0, 100.0])

    def test_drop_is_measured_against_the_latest_sample_not_the_earliest(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 0.0}}}
        _, state = apply_monotonic(
            [_tok("gpt-6-astra", "input", 60.0, DAY2), _tok("gpt-6-astra", "input", 120.0, DAY3)], prior
        )
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 120.0, "offset": 0.0})

    def test_series_are_independent(self):
        luna = series_key("aiobs_tokens_total", {"provider": "codex", "model": "gpt-5.6-luna", "kind": "input", "origin": "client"})
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 0.0}, luna: {"raw": 50.0, "offset": 0.0}}}
        shaped, state = apply_monotonic(
            [_tok("gpt-6-astra", "input", 40.0, DAY2), _tok("gpt-5.6-luna", "input", 55.0, DAY2)], prior
        )
        self.assertEqual([s.value for s in shaped], [100.0, 55.0])
        self.assertEqual(state[STATE_KEY][luna], {"raw": 55.0, "offset": 0.0})

    def test_series_absent_this_run_keeps_its_entry(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 30.0}}}
        _, state = apply_monotonic([_tok("gpt-5.6-luna", "input", 5.0, DAY2)], prior)
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 100.0, "offset": 30.0})

    def test_gauges_and_other_state_keys_pass_through(self):
        prior = {"push:tokscale:max_ts_ms": 123, "openrouter:last_date": "2026-08-20"}
        shaped, state = apply_monotonic([_gauge("tokscale", 1.0, DAY1)], prior)
        self.assertEqual(shaped[0].value, 1.0)
        self.assertEqual(state["push:tokscale:max_ts_ms"], 123)
        self.assertEqual(state["openrouter:last_date"], "2026-08-20")
        self.assertEqual(state[STATE_KEY], {})

    def test_corrupt_monotonic_state_is_treated_as_empty(self):
        for bad in ("nope", 7, {ASTRA_IN: "garbage"}, {ASTRA_IN: {"raw": "x"}}):
            shaped, state = apply_monotonic([_tok("gpt-6-astra", "input", 9.0, DAY1)], {STATE_KEY: bad})
            self.assertEqual(shaped[0].value, 9.0)
            self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 9.0, "offset": 0.0})

    def test_emitted_values_never_decrease_across_runs(self):
        state = {}
        emitted = []
        for run, raw in enumerate([100.0, 40.0, 45.0, 30.0, 90.0, 200.0]):
            shaped, state = apply_monotonic([_tok("gpt-6-astra", "input", raw, DAY1 + run)], state)
            emitted.append(shaped[0].value)
        self.assertEqual(emitted, [100.0, 100.0, 105.0, 105.0, 165.0, 275.0])
        self.assertEqual(emitted, sorted(emitted))

    def test_shaped_samples_keep_metric_labels_and_timestamp(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 100.0, "offset": 0.0}}}
        original = _tok("gpt-6-astra", "input", 1.0, DAY2)
        shaped, _ = apply_monotonic([original], prior)
        self.assertEqual((shaped[0].metric, shaped[0].labels, shaped[0].ts_ms), (original.metric, original.labels, DAY2))


class SeedOffsetsFromPeaksTests(unittest.TestCase):
    def test_peak_above_raw_sets_offset_to_the_gap(self):
        state, seeded = seed_offsets_from_peaks([_tok("gpt-6-astra", "input", 1000.0, DAY1)], {}, {ASTRA_IN: 5000.0})
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 1000.0, "offset": 4000.0})
        self.assertEqual(seeded, {ASTRA_IN: 4000.0})

    def test_peak_at_or_below_raw_seeds_nothing(self):
        for peak in (1000.0, 900.0):
            state, seeded = seed_offsets_from_peaks([_tok("gpt-6-astra", "input", 1000.0, DAY1)], {}, {ASTRA_IN: peak})
            self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 1000.0, "offset": 0.0})
            self.assertEqual(seeded, {})

    def test_existing_offset_already_covering_the_peak_is_kept(self):
        prior = {STATE_KEY: {ASTRA_IN: {"raw": 1000.0, "offset": 4500.0}}}
        state, seeded = seed_offsets_from_peaks([_tok("gpt-6-astra", "input", 1000.0, DAY1)], prior, {ASTRA_IN: 5000.0})
        self.assertEqual(state[STATE_KEY][ASTRA_IN]["offset"], 4500.0)
        self.assertEqual(seeded, {})

    def test_series_without_a_peak_is_recorded_with_zero_offset(self):
        state, seeded = seed_offsets_from_peaks([_tok("gpt-6-astra", "input", 1000.0, DAY1)], {}, {})
        self.assertEqual(state[STATE_KEY][ASTRA_IN], {"raw": 1000.0, "offset": 0.0})
        self.assertEqual(seeded, {})

    def test_peaks_for_series_not_produced_this_run_are_ignored(self):
        state, _ = seed_offsets_from_peaks([], {}, {ASTRA_IN: 5000.0})
        self.assertEqual(state[STATE_KEY], {})


class FetchPeaksTests(unittest.TestCase):
    def _opener(self, doc, seen):
        def opener(url, timeout=None):
            seen.append(url)
            return io.BytesIO(json.dumps(doc).encode("utf-8"))

        return opener

    def test_parses_vm_instant_query_into_series_keys(self):
        doc = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"__name__": "aiobs_tokens_total", "provider": "codex", "model": "gpt-6-astra", "kind": "input", "origin": "client"}, "value": [1.7e9, "5000"]},
                    {"metric": {"__name__": "aiobs_cost_usd_total", "provider": "codex", "model": "gpt-6-astra", "origin": "client"}, "value": [1.7e9, "12.5"]},
                    {"metric": {"__name__": "aiobs_lane_up", "lane": "tokscale"}, "value": [1.7e9, "1"]},
                ]
            },
        }
        seen = []
        peaks = fetch_peaks("http://hub:8428/", opener=self._opener(doc, seen))
        self.assertEqual(peaks[ASTRA_IN], 5000.0)
        self.assertEqual(peaks[series_key("aiobs_cost_usd_total", {"provider": "codex", "model": "gpt-6-astra", "origin": "client"})], 12.5)
        self.assertEqual(len(peaks), 2)
        self.assertTrue(seen[0].startswith("http://hub:8428/api/v1/query?"))
        self.assertIn("max_over_time", seen[0])

    def test_non_success_status_raises(self):
        with self.assertRaises(RuntimeError):
            fetch_peaks("http://hub:8428", opener=self._opener({"status": "error", "error": "boom"}, []))

    def test_malformed_rows_are_skipped(self):
        doc = {"status": "success", "data": {"result": [{"metric": {"__name__": "aiobs_tokens_total"}, "value": "bad"}]}}
        self.assertEqual(fetch_peaks("http://hub:8428", opener=self._opener(doc, [])), {})


def _write_config(path: str, **overrides) -> str:
    values = {
        "AIOBS_LANES": "tokscale",
        "AIOBS_STATE_DIR": os.path.join(os.path.dirname(path), "state"),
        "AIOBS_HUB_TAILNET_IP": "127.0.0.1",
        "AIOBS_VM_PORT": "8428",
    }
    values.update(overrides)
    with open(path, "w", encoding="utf-8") as handle:
        for key, val in values.items():
            handle.write(f"{key}={val}\n")
    return path


def _lane(name, samples):
    class _Lane:
        def __init__(self):
            self.name = name

        def collect(self, cfg, state):
            return list(samples)

    return _Lane


def _pushed(mock_push):
    return mock_push.call_args[0][1]


class MainWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmp, "state")
        self.config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_STATE_DIR=self.state_dir)

    def _run(self, lane_cls, argv_extra=(), push_side_effect=None):
        with patch.dict("aiobs_collector.__main__._KNOWN_LANES", {"tokscale": lane_cls}, clear=True):
            with patch("aiobs_collector.__main__.push_samples", side_effect=push_side_effect) as mock_push:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = main_mod.main(["--config", self.config, *argv_extra])
        return code, mock_push

    def test_shrink_between_runs_is_banked_and_pushed_values_never_drop(self):
        code1, push1 = self._run(_lane("tokscale", [_tok("gpt-6-astra", "input", 1000.0, DAY1)]))
        self.assertEqual(code1, 0)
        self.assertEqual([s.value for s in _pushed(push1) if s.metric == "aiobs_tokens_total"], [1000.0])

        code2, push2 = self._run(_lane("tokscale", [_tok("gpt-6-astra", "input", 600.0, DAY2)]))
        self.assertEqual(code2, 0)
        self.assertEqual([s.value for s in _pushed(push2) if s.metric == "aiobs_tokens_total"], [1000.0])
        self.assertEqual(load_state(self.state_dir)[STATE_KEY][ASTRA_IN], {"raw": 600.0, "offset": 400.0})

        code3, push3 = self._run(_lane("tokscale", [_tok("gpt-6-astra", "input", 650.0, DAY3)]))
        self.assertEqual(code3, 0)
        self.assertEqual([s.value for s in _pushed(push3) if s.metric == "aiobs_tokens_total"], [1050.0])

    def test_openrouter_baseline_is_persisted_from_raw_values_not_shaped_ones(self):
        key = series_key("aiobs_tokens_total", {"provider": "openrouter", "model": "m", "kind": "input", "origin": "client"})
        save_state(self.state_dir, {STATE_KEY: {key: {"raw": 1000.0, "offset": 400.0}}})
        sample = Sample(
            metric="aiobs_tokens_total",
            labels={"provider": "openrouter", "model": "m", "kind": "input", "origin": "client"},
            value=1000.0,
            ts_ms=DAY1,
        )
        code, push = self._run(_lane("openrouter", [sample]))
        self.assertEqual(code, 0)
        self.assertEqual([s.value for s in _pushed(push) if s.metric == "aiobs_tokens_total"], [1400.0])
        persisted = load_state(self.state_dir)
        self.assertEqual(persisted["openrouter:cum:m:input"], 1000.0)  # raw, never raw + offset
        self.assertEqual(persisted[STATE_KEY][key], {"raw": 1000.0, "offset": 400.0})

    def test_failed_push_leaves_monotonic_state_unwritten(self):
        code, _ = self._run(_lane("tokscale", [_tok("gpt-6-astra", "input", 1000.0, DAY1)]), push_side_effect=RuntimeError("down"))
        self.assertEqual(code, 1)
        self.assertNotIn(STATE_KEY, load_state(self.state_dir))

    def test_dry_run_prints_shaped_values_without_saving_state(self):
        save_state(self.state_dir, {STATE_KEY: {ASTRA_IN: {"raw": 1000.0, "offset": 400.0}}})
        with patch.dict("aiobs_collector.__main__._KNOWN_LANES", {"tokscale": _lane("tokscale", [_tok("gpt-6-astra", "input", 1000.0, DAY1)])}, clear=True):
            with patch("aiobs_collector.__main__.push_samples") as mock_push:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main_mod.main(["--config", self.config, "--dry-run"])
        self.assertEqual(code, 0)
        mock_push.assert_not_called()
        self.assertIn("1400", out.getvalue())
        self.assertEqual(load_state(self.state_dir)[STATE_KEY][ASTRA_IN], {"raw": 1000.0, "offset": 400.0})

    def test_seed_flag_banks_vm_peaks_without_pushing_then_normal_run_emits_the_peak(self):
        lane = _lane("tokscale", [_tok("gpt-6-astra", "input", 1000.0, DAY1)])
        with patch("aiobs_collector.__main__.fetch_peaks", return_value={ASTRA_IN: 5000.0}) as mock_fetch:
            code, push = self._run(lane, argv_extra=("--seed-offsets-from-vm",))
        self.assertEqual(code, 0)
        push.assert_not_called()
        mock_fetch.assert_called_once_with("http://127.0.0.1:8428")
        persisted = load_state(self.state_dir)
        self.assertEqual(persisted[STATE_KEY][ASTRA_IN], {"raw": 1000.0, "offset": 4000.0})
        self.assertNotIn("push:tokscale:max_ts_ms", persisted)  # nothing was pushed, no high-water mark

        code2, push2 = self._run(lane)
        self.assertEqual(code2, 0)
        self.assertEqual([s.value for s in _pushed(push2) if s.metric == "aiobs_tokens_total"], [5000.0])

    def test_seed_flag_with_dry_run_reports_but_saves_nothing(self):
        lane = _lane("tokscale", [_tok("gpt-6-astra", "input", 1000.0, DAY1)])
        with patch("aiobs_collector.__main__.fetch_peaks", return_value={ASTRA_IN: 5000.0}):
            code, push = self._run(lane, argv_extra=("--seed-offsets-from-vm", "--dry-run"))
        self.assertEqual(code, 0)
        push.assert_not_called()
        self.assertEqual(load_state(self.state_dir), {})

    def test_seed_flag_peak_fetch_failure_exits_1_and_saves_nothing(self):
        lane = _lane("tokscale", [_tok("gpt-6-astra", "input", 1000.0, DAY1)])
        with patch("aiobs_collector.__main__.fetch_peaks", side_effect=RuntimeError("hub down")):
            code, push = self._run(lane, argv_extra=("--seed-offsets-from-vm",))
        self.assertEqual(code, 1)
        push.assert_not_called()
        self.assertEqual(load_state(self.state_dir), {})


if __name__ == "__main__":
    unittest.main()
