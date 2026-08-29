"""TDD test suite for aiobs_collector.lane_tokscale (Task 9: tokscale lane).

Runnable with no environment variables, from either location:
    python3 -m unittest discover -s workstation/tests -v   # from repo root
    python3 -m unittest discover -s tests -v                # from workstation/
"""

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# workstation/ (this file's grandparent) holds the aiobs_collector package.
_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

from aiobs_collector.core import Sample  # noqa: E402
from aiobs_collector.lane_tokscale import TokscaleLane, normalize_tokscale  # noqa: E402

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tokscale_sample.json"


def _load_fixture() -> dict:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _local_ms(*args) -> int:
    """Build an epoch-ms timestamp from local-time datetime() args, the same
    way the implementation is expected to (naive datetime -> local tz via
    .timestamp()). Kept independent of lane_tokscale's own private helpers so
    the test is a black-box check, not a tautology against the same code.
    """
    return int(datetime(*args).timestamp() * 1000)


def _end_of_day_ms(date_str: str) -> int:
    y, m, d = (int(p) for p in date_str.split("-"))
    return _local_ms(y, m, d, 23, 59, 59, 999000)


def _find(samples, metric, **labels):
    return [
        s
        for s in samples
        if s.metric == metric and all(s.labels.get(k) == v for k, v in labels.items())
    ]


class NormalizeTokscaleCumulativeTests(unittest.TestCase):
    """Exercises normalize_tokscale against the redacted real-shape fixture
    (two days, two real clients: claude -> claude-code, codex -> codex)."""

    def setUp(self):
        self.doc = _load_fixture()
        # A "now" safely after both fixture days: both days are history, so
        # every sample should land on its own end-of-day timestamp.
        self.now_history = _local_ms(2026, 8, 25, 10, 0, 0)

    def test_provider_label_maps_claude_to_claude_code(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        self.assertEqual(_find(samples, "aiobs_tokens_total", provider="claude"), [])
        claude_code = _find(samples, "aiobs_tokens_total", provider="claude-code")
        self.assertGreater(len(claude_code), 0)

    def test_provider_label_codex_passes_through_unchanged(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        codex = _find(samples, "aiobs_tokens_total", provider="codex")
        self.assertGreater(len(codex), 0)

    def test_every_token_sample_has_origin_client(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        token_samples = [s for s in samples if s.metric == "aiobs_tokens_total"]
        self.assertGreater(len(token_samples), 0)
        for s in token_samples:
            self.assertEqual(s.labels["origin"], "client")

    def test_every_cost_sample_has_origin_client(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        cost_samples = [s for s in samples if s.metric == "aiobs_cost_usd_total"]
        self.assertGreater(len(cost_samples), 0)
        for s in cost_samples:
            self.assertEqual(s.labels["origin"], "client")

    def test_cumulative_input_tokens_sum_across_days_ascending(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="claude-code",
            model="claude-sonnet-5",
            kind="input",
        )
        # day1: 8000, day2: 8000 + 5000 = 13000 -- one Sample per active day,
        # each carrying the running total as of that day, oldest first.
        self.assertEqual([s.value for s in rows], [8000.0, 13000.0])
        self.assertEqual(rows[0].ts_ms, _end_of_day_ms("2026-08-20"))
        self.assertEqual(rows[1].ts_ms, _end_of_day_ms("2026-08-21"))

    def test_cumulative_output_tokens_second_provider(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="codex",
            model="gpt-5.6-sol",
            kind="output",
        )
        # day1: 1500, day2: 1500 + 1400 = 2900
        self.assertEqual([s.value for s in rows], [1500.0, 2900.0])

    def test_cumulative_cache_read_both_providers(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        claude_rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="claude-code",
            model="claude-sonnet-5",
            kind="cache_read",
        )
        self.assertEqual([s.value for s in claude_rows], [780000.0, 1300000.0])
        codex_rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="codex",
            model="gpt-5.6-sol",
            kind="cache_read",
        )
        self.assertEqual([s.value for s in codex_rows], [400000.0, 760000.0])

    def test_cache_write_present_one_day_only_stays_at_last_known_value(self):
        # claude-sonnet-5 has cacheWrite=20000 on day1, 0 on day2: no new
        # increment on day2, so no second sample -- the series simply has
        # one point, dated day1, valued 20000 (not zero, not duplicated).
        samples = normalize_tokscale(self.doc, self.now_history)
        rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="claude-code",
            model="claude-sonnet-5",
            kind="cache_write",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].value, 20000.0)
        self.assertEqual(rows[0].ts_ms, _end_of_day_ms("2026-08-20"))

    def test_cache_write_absent_kind_is_never_invented(self):
        # gpt-5.6-sol has cacheWrite=0 on *every* fixture day -- the kind
        # never appears in the source data, so no sample for it must exist.
        # Never invent a kind the source data never reported.
        samples = normalize_tokscale(self.doc, self.now_history)
        rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="codex",
            model="gpt-5.6-sol",
            kind="cache_write",
        )
        self.assertEqual(rows, [])

    def test_reasoning_field_never_produces_a_sample_kind(self):
        # tokscale's tokenBreakdown/tokens carry a "reasoning" field, but the
        # frozen kind enum is input|output|cache_read|cache_write only.
        # Reasoning tokens must be dropped, never mapped onto any kind.
        samples = normalize_tokscale(self.doc, self.now_history)
        kinds_seen = {
            s.labels["kind"] for s in samples if s.metric == "aiobs_tokens_total"
        }
        self.assertEqual(kinds_seen, {"input", "output", "cache_read", "cache_write"})

    def test_cost_cumulative_sum_per_provider_model(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        claude_cost = _find(
            samples, "aiobs_cost_usd_total", provider="claude-code", model="claude-sonnet-5"
        )
        self.assertEqual([round(s.value, 2) for s in claude_cost], [2.75, 4.65])
        codex_cost = _find(
            samples, "aiobs_cost_usd_total", provider="codex", model="gpt-5.6-sol"
        )
        self.assertEqual([round(s.value, 2) for s in codex_cost], [1.60, 2.80])

    def test_cost_sample_labels_have_no_kind_key(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        cost_samples = [s for s in samples if s.metric == "aiobs_cost_usd_total"]
        self.assertGreater(len(cost_samples), 0)
        for s in cost_samples:
            self.assertNotIn("kind", s.labels)
            self.assertEqual(set(s.labels.keys()), {"provider", "model", "origin"})

    def test_token_sample_labels_exact_key_set(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        token_samples = [s for s in samples if s.metric == "aiobs_tokens_total"]
        for s in token_samples:
            self.assertEqual(set(s.labels.keys()), {"provider", "model", "kind", "origin"})

    def test_droid_absent_from_fixture_yields_no_series_and_no_exception(self):
        # Factory/droid never appears in this two-client fixture -- the
        # normalizer must simply produce zero droid samples, not raise.
        samples = normalize_tokscale(self.doc, self.now_history)  # must not raise
        self.assertEqual(_find(samples, "aiobs_tokens_total", provider="droid"), [])
        self.assertEqual(_find(samples, "aiobs_cost_usd_total", provider="droid"), [])

    def test_returns_sample_instances(self):
        samples = normalize_tokscale(self.doc, self.now_history)
        self.assertGreater(len(samples), 0)
        for s in samples:
            self.assertIsInstance(s, Sample)


class NormalizeTokscaleTimestampTests(unittest.TestCase):
    def setUp(self):
        self.doc = _load_fixture()

    def test_all_history_days_use_end_of_day_local(self):
        # "now" is well after both fixture days -- every sample, across
        # every provider/model/kind, must land on one of the two end-of-day
        # timestamps; none may carry the raw "now" value.
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_tokscale(self.doc, now_ms)
        self.assertGreater(len(samples), 0)
        ts_values = {s.ts_ms for s in samples}
        self.assertEqual(
            ts_values, {_end_of_day_ms("2026-08-20"), _end_of_day_ms("2026-08-21")}
        )

    def test_last_fixture_day_as_today_uses_now_ms_not_end_of_day(self):
        # "now" falls on 2026-08-21 (the fixture's last day) at an arbitrary
        # clock time -- that day's samples must carry now_ms exactly, while
        # 2026-08-20 (still history) keeps its end-of-day timestamp.
        now_ms = _local_ms(2026, 8, 21, 14, 30, 0)
        samples = normalize_tokscale(self.doc, now_ms)

        day2_rows = _find(
            samples,
            "aiobs_tokens_total",
            provider="claude-code",
            model="claude-sonnet-5",
            kind="input",
        )
        self.assertEqual(len(day2_rows), 2)
        self.assertEqual(day2_rows[0].ts_ms, _end_of_day_ms("2026-08-20"))
        self.assertEqual(day2_rows[1].ts_ms, now_ms)
        self.assertNotEqual(now_ms, _end_of_day_ms("2026-08-21"))  # sanity: scenario is meaningful

    def test_cost_sample_on_today_also_uses_now_ms(self):
        now_ms = _local_ms(2026, 8, 21, 14, 30, 0)
        samples = normalize_tokscale(self.doc, now_ms)
        codex_cost = _find(samples, "aiobs_cost_usd_total", provider="codex", model="gpt-5.6-sol")
        self.assertEqual(codex_cost[-1].ts_ms, now_ms)


class NormalizeTokscaleOrderingRobustnessTests(unittest.TestCase):
    def test_cumulative_sum_is_correct_even_if_contributions_are_out_of_order(self):
        doc = _load_fixture()
        # Reverse the day order in the source doc -- the normalizer must
        # sort by date ascending itself rather than trusting input order.
        reversed_doc = {**doc, "contributions": list(reversed(doc["contributions"]))}
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)

        in_order = normalize_tokscale(doc, now_ms)
        out_of_order = normalize_tokscale(reversed_doc, now_ms)

        in_order_rows = _find(
            in_order, "aiobs_tokens_total", provider="claude-code", model="claude-sonnet-5", kind="input"
        )
        out_of_order_rows = _find(
            out_of_order, "aiobs_tokens_total", provider="claude-code", model="claude-sonnet-5", kind="input"
        )
        self.assertEqual([s.value for s in in_order_rows], [8000.0, 13000.0])
        self.assertEqual(
            [(s.value, s.ts_ms) for s in in_order_rows],
            [(s.value, s.ts_ms) for s in out_of_order_rows],
        )


class NormalizeTokscaleUnknownClientTests(unittest.TestCase):
    def test_unknown_client_sanitized_lowercase_non_alnum_to_dash(self):
        # A client name tokscale might add in the future that isn't one of
        # our five canonical labels -- must pass through sanitized, not be
        # dropped or raise.
        doc = {
            "contributions": [
                {
                    "date": "2026-08-20",
                    "clients": [
                        {
                            "client": "Weird.Client!",
                            "modelId": "some-model",
                            "providerId": "custom",
                            "tokens": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                            "cost": 0.02,
                            "messages": 1,
                        }
                    ],
                }
            ]
        }
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_tokscale(doc, now_ms)
        rows = _find(samples, "aiobs_tokens_total", provider="weird-client-", model="some-model", kind="input")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].value, 100.0)

    def test_known_canonical_clients_map_exactly(self):
        # cursor and hermes are canonical passthroughs too (droid is covered
        # by its absence test above); pin the full mapping table here.
        doc = {
            "contributions": [
                {
                    "date": "2026-08-20",
                    "clients": [
                        {
                            "client": "cursor",
                            "modelId": "m1",
                            "tokens": {"input": 1, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                            "cost": 0,
                        },
                        {
                            "client": "hermes",
                            "modelId": "m2",
                            "tokens": {"input": 2, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                            "cost": 0,
                        },
                        {
                            "client": "droid",
                            "modelId": "m3",
                            "tokens": {"input": 3, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                            "cost": 0,
                        },
                    ],
                }
            ]
        }
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_tokscale(doc, now_ms)
        self.assertEqual(len(_find(samples, "aiobs_tokens_total", provider="cursor", model="m1")), 1)
        self.assertEqual(len(_find(samples, "aiobs_tokens_total", provider="hermes", model="m2")), 1)
        self.assertEqual(len(_find(samples, "aiobs_tokens_total", provider="droid", model="m3")), 1)


class NormalizeTokscaleZeroCostTests(unittest.TestCase):
    def test_zero_cost_entry_emits_no_cost_sample(self):
        # cost=0 means nothing new to report for the cumulative cost
        # counter that day -- consistent with the zero-token-kind rule.
        doc = {
            "contributions": [
                {
                    "date": "2026-08-20",
                    "clients": [
                        {
                            "client": "hermes",
                            "modelId": "local-model",
                            "tokens": {"input": 10, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                            "cost": 0,
                        }
                    ],
                }
            ]
        }
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_tokscale(doc, now_ms)
        self.assertEqual(_find(samples, "aiobs_cost_usd_total", provider="hermes", model="local-model"), [])
        self.assertEqual(len(_find(samples, "aiobs_tokens_total", provider="hermes", model="local-model")), 1)


class NormalizeTokscaleMalformedDocTests(unittest.TestCase):
    NOW = _local_ms(2026, 8, 25, 10, 0, 0)

    def test_empty_doc_returns_empty_list(self):
        self.assertEqual(normalize_tokscale({}, self.NOW), [])

    def test_contributions_none_returns_empty_list(self):
        self.assertEqual(normalize_tokscale({"contributions": None}, self.NOW), [])

    def test_contributions_empty_list_returns_empty_list(self):
        self.assertEqual(normalize_tokscale({"contributions": []}, self.NOW), [])

    def test_day_with_no_clients_key_is_skipped_without_error(self):
        doc = {"contributions": [{"date": "2026-08-20"}]}
        self.assertEqual(normalize_tokscale(doc, self.NOW), [])

    def test_client_entry_missing_model_id_is_skipped_without_error(self):
        doc = {
            "contributions": [
                {
                    "date": "2026-08-20",
                    "clients": [
                        {"client": "codex", "tokens": {"input": 5}, "cost": 1}
                    ],
                }
            ]
        }
        self.assertEqual(normalize_tokscale(doc, self.NOW), [])


class TokscaleLaneTests(unittest.TestCase):
    def test_name_is_tokscale(self):
        self.assertEqual(TokscaleLane().name, "tokscale")

    def test_missing_version_raises_clear_error_without_shelling_out(self):
        lane = TokscaleLane()
        with patch("aiobs_collector.lane_tokscale.subprocess.run") as mock_run:
            with self.assertRaisesRegex(RuntimeError, "AIOBS_TOKSCALE_VERSION"):
                lane.collect(cfg={}, state={})
            mock_run.assert_not_called()

    def test_empty_string_version_raises_clear_error(self):
        lane = TokscaleLane()
        with patch("aiobs_collector.lane_tokscale.subprocess.run") as mock_run:
            with self.assertRaisesRegex(RuntimeError, "AIOBS_TOKSCALE_VERSION"):
                lane.collect(cfg={"AIOBS_TOKSCALE_VERSION": "   "}, state={})
            mock_run.assert_not_called()

    def test_collect_shells_out_to_pinned_npx_tokscale_graph(self):
        lane = TokscaleLane()
        fixture_text = json.dumps(_load_fixture())
        completed = subprocess.CompletedProcess(
            args=["npx"], returncode=0, stdout=fixture_text, stderr=""
        )
        with patch("aiobs_collector.lane_tokscale.subprocess.run", return_value=completed) as mock_run:
            lane.collect(cfg={"AIOBS_TOKSCALE_VERSION": "4.14.0"}, state={})

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["npx", "-y", "tokscale@4.14.0", "graph", "--no-spinner"])
        self.assertEqual(kwargs.get("timeout"), 300)
        self.assertTrue(kwargs.get("capture_output") or kwargs.get("stdout") is not None)
        self.assertTrue(kwargs.get("text", kwargs.get("universal_newlines", False)))
        self.assertTrue(kwargs.get("check", False))

    def test_collect_returns_normalized_samples_matching_pure_function(self):
        lane = TokscaleLane()
        doc = _load_fixture()
        fixture_text = json.dumps(doc)
        completed = subprocess.CompletedProcess(
            args=["npx"], returncode=0, stdout=fixture_text, stderr=""
        )
        fixed_now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        with patch("aiobs_collector.lane_tokscale.subprocess.run", return_value=completed):
            with patch("aiobs_collector.lane_tokscale.time.time", return_value=fixed_now_ms / 1000.0):
                samples = lane.collect(cfg={"AIOBS_TOKSCALE_VERSION": "4.14.0"}, state={})

        expected = normalize_tokscale(doc, fixed_now_ms)
        self.assertEqual(samples, expected)

    def test_collect_propagates_subprocess_failure(self):
        # run_lanes() is what isolates lane failures -- collect() itself
        # must let a bad exit code surface as an exception, not swallow it.
        lane = TokscaleLane()
        with patch(
            "aiobs_collector.lane_tokscale.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["npx"]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                lane.collect(cfg={"AIOBS_TOKSCALE_VERSION": "4.14.0"}, state={})

    def test_collect_propagates_timeout(self):
        lane = TokscaleLane()
        with patch(
            "aiobs_collector.lane_tokscale.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["npx"], timeout=300),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                lane.collect(cfg={"AIOBS_TOKSCALE_VERSION": "4.14.0"}, state={})

    def test_collect_propagates_bad_json(self):
        lane = TokscaleLane()
        completed = subprocess.CompletedProcess(args=["npx"], returncode=0, stdout="not json", stderr="")
        with patch("aiobs_collector.lane_tokscale.subprocess.run", return_value=completed):
            with self.assertRaises(json.JSONDecodeError):
                lane.collect(cfg={"AIOBS_TOKSCALE_VERSION": "4.14.0"}, state={})


if __name__ == "__main__":
    unittest.main()
