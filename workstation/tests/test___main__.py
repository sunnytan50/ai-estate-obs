"""TDD test suite for aiobs_collector.__main__ (Task 11: push+backfill+launchd
entrypoint).

Not in the brief's literal Create list (only test_push.py was named there),
but the dispatch's "Your Job" section says "TDD push.py -> __main__ ->
templates/installer" -- and the dedupe/filtering logic that lives in
__main__.py is exactly the kind of boundary-condition-heavy code this
project's TDD convention (test_core.py, test_lane_tokscale.py,
test_lane_openrouter.py) exists to pin down. Added as a self-directed,
in-scope extension: pure-additive, touches no frozen file, no owner-boundary
file.

Runnable with no environment variables, from either location:
    python3 -m unittest discover -s workstation/tests -v   # from repo root
    python3 -m unittest discover -s tests -v                # from workstation/

`push_samples` is mocked throughout (patching `aiobs_collector.__main__.push_samples`,
the name as bound in that module's namespace) -- push.py's own HTTP behavior
already has full dedicated coverage in test_push.py; these tests are about
__main__'s OWN logic: arg parsing, config/state wiring, lane construction,
the dedupe filter, and exit codes. Zero real network calls anywhere here.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# workstation/ (this file's grandparent) holds the aiobs_collector package.
_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

import aiobs_collector.__main__ as main_mod  # noqa: E402
from aiobs_collector.core import Sample, load_state, save_state  # noqa: E402
from aiobs_collector.lane_openrouter import OpenRouterLane  # noqa: E402
from aiobs_collector.lane_tokscale import TokscaleLane  # noqa: E402


def _local_ms(*args) -> int:
    """Same construction lane_openrouter's/tokscale's own tests use: a naive
    datetime -> local-time epoch ms, independent of the module's own helpers.
    """
    return int(datetime(*args).timestamp() * 1000)


def _write_config(path: str, **overrides) -> str:
    values = {
        "AIOBS_LANES": "",
        "AIOBS_STATE_DIR": os.path.join(os.path.dirname(path), "state"),
        "AIOBS_HUB_TAILNET_IP": "127.0.0.1",
        "AIOBS_VM_PORT": "8428",
    }
    values.update(overrides)
    with open(path, "w", encoding="utf-8") as handle:
        for key, val in values.items():
            handle.write(f"{key}={val}\n")
    return path


def _fake_lane_class(name, samples):
    """A zero-arg-constructible Lane double, matching TokscaleLane/
    OpenRouterLane's own shape (class attr `name`, `collect(cfg, state)`).
    """

    class _Fake:
        def __init__(self):
            self.name = name

        def collect(self, cfg, state):
            return list(samples)

    return _Fake


class BuildLanesTests(unittest.TestCase):
    def test_empty_lanes_string_returns_empty_list(self):
        self.assertEqual(main_mod._build_lanes({"AIOBS_LANES": ""}), [])

    def test_unset_lanes_key_returns_empty_list(self):
        self.assertEqual(main_mod._build_lanes({}), [])

    def test_single_known_lane_tokscale(self):
        lanes = main_mod._build_lanes({"AIOBS_LANES": "tokscale"})
        self.assertEqual(len(lanes), 1)
        self.assertIsInstance(lanes[0], TokscaleLane)

    def test_single_known_lane_openrouter(self):
        lanes = main_mod._build_lanes({"AIOBS_LANES": "openrouter"})
        self.assertEqual(len(lanes), 1)
        self.assertIsInstance(lanes[0], OpenRouterLane)

    def test_both_known_lanes_comma_separated_in_order(self):
        lanes = main_mod._build_lanes({"AIOBS_LANES": "tokscale,openrouter"})
        self.assertEqual([type(lane) for lane in lanes], [TokscaleLane, OpenRouterLane])

    def test_whitespace_around_names_is_stripped(self):
        lanes = main_mod._build_lanes({"AIOBS_LANES": " tokscale , openrouter "})
        self.assertEqual(len(lanes), 2)

    def test_blank_entries_between_commas_are_ignored(self):
        lanes = main_mod._build_lanes({"AIOBS_LANES": "tokscale,,openrouter,"})
        self.assertEqual(len(lanes), 2)

    def test_unknown_lane_name_raises_config_error(self):
        with self.assertRaisesRegex(main_mod.ConfigError, "bogus"):
            main_mod._build_lanes({"AIOBS_LANES": "tokscale,bogus"})

    def test_unknown_lane_error_names_known_lanes(self):
        with self.assertRaisesRegex(main_mod.ConfigError, "openrouter"):
            main_mod._build_lanes({"AIOBS_LANES": "bogus"})


class LocalMidnightMsTests(unittest.TestCase):
    def test_returns_midnight_of_the_same_local_day(self):
        now_ms = _local_ms(2026, 8, 29, 14, 30, 45)
        self.assertEqual(main_mod._local_midnight_ms(now_ms), _local_ms(2026, 8, 29, 0, 0, 0))

    def test_already_at_midnight_returns_same_ms(self):
        now_ms = _local_ms(2026, 8, 29, 0, 0, 0)
        self.assertEqual(main_mod._local_midnight_ms(now_ms), now_ms)

    def test_one_ms_before_midnight_belongs_to_the_prior_day(self):
        midnight = _local_ms(2026, 8, 29, 0, 0, 0)
        self.assertEqual(main_mod._local_midnight_ms(midnight - 1), _local_ms(2026, 8, 28, 0, 0, 0))


class LaneForSampleTests(unittest.TestCase):
    def test_provider_openrouter_maps_to_openrouter_lane(self):
        s = Sample(metric="aiobs_tokens_total", labels={"provider": "openrouter"}, value=1.0, ts_ms=1)
        self.assertEqual(main_mod._lane_for_sample(s), "openrouter")

    def test_other_providers_map_to_tokscale_lane(self):
        for provider in ("claude-code", "codex", "cursor", "droid", "hermes", "some-new-client"):
            s = Sample(metric="aiobs_tokens_total", labels={"provider": provider}, value=1.0, ts_ms=1)
            self.assertEqual(main_mod._lane_for_sample(s), "tokscale", msg=provider)

    def test_no_provider_label_returns_none(self):
        s = Sample(metric="aiobs_lane_up", labels={"lane": "tokscale"}, value=1.0, ts_ms=1)
        self.assertIsNone(main_mod._lane_for_sample(s))


class FilterForPushTests(unittest.TestCase):
    TODAY_START = _local_ms(2026, 8, 29, 0, 0, 0)
    NOW = _local_ms(2026, 8, 29, 10, 0, 0)
    YESTERDAY_END = _local_ms(2026, 8, 28, 23, 59, 59) + 999  # matches _end_of_day_local_ms shape
    TWO_DAYS_AGO_END = _local_ms(2026, 8, 27, 23, 59, 59) + 999

    def _token_sample(self, provider, ts_ms, model="m"):
        return Sample(
            metric="aiobs_tokens_total",
            labels={"provider": provider, "model": model, "kind": "input", "origin": "client"},
            value=1.0,
            ts_ms=ts_ms,
        )

    def test_backfill_returns_everything_unfiltered_regardless_of_state(self):
        samples = [
            self._token_sample("claude-code", self.TWO_DAYS_AGO_END),
            self._token_sample("claude-code", self.NOW),
        ]
        state = {"push:tokscale:max_ts_ms": self.NOW * 2, "push:tokscale:max_ts_count": 99}
        result = main_mod.filter_for_push(samples, state, self.NOW, backfill=True)
        self.assertEqual(result, samples)

    def test_today_sample_always_included_even_past_a_higher_water_mark(self):
        today_sample = self._token_sample("claude-code", self.NOW)
        # Pathological state: high-water mark is already beyond "now" -- today
        # must still be included, since the today/past-day check runs first.
        state = {"push:tokscale:max_ts_ms": self.NOW + 999999, "push:tokscale:max_ts_count": 1}
        result = main_mod.filter_for_push([today_sample], state, self.NOW, backfill=False)
        self.assertEqual(result, [today_sample])

    def test_lane_up_and_last_success_always_included(self):
        up = Sample(metric="aiobs_lane_up", labels={"lane": "tokscale"}, value=0.0, ts_ms=self.NOW)
        last_success = Sample(
            metric="aiobs_lane_last_success_timestamp", labels={"lane": "tokscale"}, value=1.0, ts_ms=self.NOW
        )
        result = main_mod.filter_for_push([up, last_success], {}, self.NOW, backfill=False)
        self.assertEqual(result, [up, last_success])

    def test_past_day_sample_with_no_prior_state_is_included(self):
        sample = self._token_sample("claude-code", self.YESTERDAY_END)
        result = main_mod.filter_for_push([sample], {}, self.NOW, backfill=False)
        self.assertEqual(result, [sample])

    def test_past_day_sample_newer_than_high_water_mark_is_included(self):
        sample = self._token_sample("claude-code", self.YESTERDAY_END)
        state = {"push:tokscale:max_ts_ms": self.TWO_DAYS_AGO_END, "push:tokscale:max_ts_count": 1}
        result = main_mod.filter_for_push([sample], state, self.NOW, backfill=False)
        self.assertEqual(result, [sample])

    def test_past_day_sample_older_than_high_water_mark_is_excluded(self):
        sample = self._token_sample("claude-code", self.TWO_DAYS_AGO_END)
        state = {"push:tokscale:max_ts_ms": self.YESTERDAY_END, "push:tokscale:max_ts_count": 1}
        result = main_mod.filter_for_push([sample], state, self.NOW, backfill=False)
        self.assertEqual(result, [])

    def test_past_day_sample_at_high_water_mark_excluded_when_count_unchanged(self):
        # Same single sample as the run that set the high-water mark: count
        # this run (1) is not > stored count (1) -> already fully pushed.
        sample = self._token_sample("claude-code", self.YESTERDAY_END)
        state = {"push:tokscale:max_ts_ms": self.YESTERDAY_END, "push:tokscale:max_ts_count": 1}
        result = main_mod.filter_for_push([sample], state, self.NOW, backfill=False)
        self.assertEqual(result, [])

    def test_past_day_sample_at_high_water_mark_included_when_count_increased(self):
        # Boundary day now has 2 samples where only 1 was recorded pushed --
        # e.g. a second model's row appeared for a day already at the mark.
        samples = [
            self._token_sample("claude-code", self.YESTERDAY_END, model="a"),
            self._token_sample("claude-code", self.YESTERDAY_END, model="b"),
        ]
        state = {"push:tokscale:max_ts_ms": self.YESTERDAY_END, "push:tokscale:max_ts_count": 1}
        result = main_mod.filter_for_push(samples, state, self.NOW, backfill=False)
        self.assertEqual(result, samples)

    def test_two_lanes_tracked_independently_one_stale_state_does_not_block_the_other(self):
        # tokscale already has a high-water mark covering "yesterday"; openrouter
        # has NO state yet (e.g. it failed on every prior run) but produces a
        # sample dated the same calendar day. It must NOT be silently dropped
        # just because tokscale's independent high-water mark already covers
        # that timestamp -- this is the exact cross-lane bug this design guards.
        tokscale_sample = self._token_sample("claude-code", self.YESTERDAY_END)
        openrouter_sample = self._token_sample("openrouter", self.YESTERDAY_END, model="x")
        state = {"push:tokscale:max_ts_ms": self.YESTERDAY_END, "push:tokscale:max_ts_count": 1}
        result = main_mod.filter_for_push(
            [tokscale_sample, openrouter_sample], state, self.NOW, backfill=False
        )
        self.assertIn(openrouter_sample, result)
        self.assertNotIn(tokscale_sample, result)  # tokscale's own mark correctly excludes it

    def test_sample_without_provider_label_defensively_included(self):
        # Hypothetical: some future metric with neither a provider label nor
        # membership in the always-push set. Must never be silently dropped.
        mystery = Sample(metric="aiobs_something_else", labels={"x": "y"}, value=1.0, ts_ms=self.YESTERDAY_END)
        state = {"push:tokscale:max_ts_ms": self.NOW, "push:tokscale:max_ts_count": 999}
        result = main_mod.filter_for_push([mystery], state, self.NOW, backfill=False)
        self.assertEqual(result, [mystery])


class ComputePushStateTests(unittest.TestCase):
    TODAY_START = _local_ms(2026, 8, 29, 0, 0, 0)
    NOW = _local_ms(2026, 8, 29, 10, 0, 0)
    YESTERDAY_END = _local_ms(2026, 8, 28, 23, 59, 59) + 999
    TWO_DAYS_AGO_END = _local_ms(2026, 8, 27, 23, 59, 59) + 999

    def _token_sample(self, provider, ts_ms, model="m"):
        return Sample(
            metric="aiobs_tokens_total",
            labels={"provider": provider, "model": model, "kind": "input", "origin": "client"},
            value=1.0,
            ts_ms=ts_ms,
        )

    def test_today_and_lane_up_samples_produce_no_push_keys(self):
        today = self._token_sample("claude-code", self.NOW)
        up = Sample(metric="aiobs_lane_up", labels={"lane": "tokscale"}, value=1.0, ts_ms=self.NOW)
        new_state = main_mod.compute_push_state([today, up], {}, self.NOW)
        self.assertEqual(new_state, {})

    def test_max_ts_and_count_correct_for_single_lane_two_days(self):
        samples = [
            self._token_sample("claude-code", self.TWO_DAYS_AGO_END),
            self._token_sample("claude-code", self.YESTERDAY_END, model="a"),
            self._token_sample("claude-code", self.YESTERDAY_END, model="b"),
        ]
        new_state = main_mod.compute_push_state(samples, {}, self.NOW)
        self.assertEqual(new_state["push:tokscale:max_ts_ms"], self.YESTERDAY_END)
        self.assertEqual(new_state["push:tokscale:max_ts_count"], 2)

    def test_two_lanes_produce_independent_keys(self):
        samples = [
            self._token_sample("claude-code", self.YESTERDAY_END),
            self._token_sample("openrouter", self.TWO_DAYS_AGO_END),
        ]
        new_state = main_mod.compute_push_state(samples, {}, self.NOW)
        self.assertEqual(new_state["push:tokscale:max_ts_ms"], self.YESTERDAY_END)
        self.assertEqual(new_state["push:openrouter:max_ts_ms"], self.TWO_DAYS_AGO_END)

    def test_carries_forward_prior_state_keys_untouched(self):
        prior = {"lane:tokscale:last_success_ms": 123456}
        samples = [self._token_sample("claude-code", self.YESTERDAY_END)]
        new_state = main_mod.compute_push_state(samples, prior, self.NOW)
        self.assertEqual(new_state["lane:tokscale:last_success_ms"], 123456)
        self.assertEqual(new_state["push:tokscale:max_ts_ms"], self.YESTERDAY_END)

    def test_no_past_day_samples_for_a_lane_leaves_its_keys_absent(self):
        samples = [self._token_sample("claude-code", self.NOW)]  # today only
        new_state = main_mod.compute_push_state(samples, {}, self.NOW)
        self.assertNotIn("push:tokscale:max_ts_ms", new_state)


class MainEntrypointTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp = tmp_dir.name

    def test_missing_config_file_exits_2(self):
        missing = os.path.join(self.tmp, "does-not-exist.env")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_mod.main(["--config", missing])
        self.assertEqual(code, 2)

    def test_unknown_lane_in_config_exits_2_and_never_calls_push(self):
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="bogus")
        with patch("aiobs_collector.__main__.push_samples") as mock_push:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 2)
        mock_push.assert_not_called()
        self.assertIn("bogus", stderr.getvalue())

    def test_dry_run_prints_exposition_and_never_calls_push(self):
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="")
        with patch("aiobs_collector.__main__.push_samples") as mock_push:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main_mod.main(["--config", config, "--dry-run"])
        self.assertEqual(code, 0)
        mock_push.assert_not_called()
        # Empty lane list -> zero data samples, but the loop still ran cleanly.
        self.assertEqual(stdout.getvalue(), "")

    def test_dry_run_does_not_save_state(self):
        state_dir = os.path.join(self.tmp, "state")
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="", AIOBS_STATE_DIR=state_dir)
        with patch("aiobs_collector.__main__.push_samples"):
            with contextlib.redirect_stdout(io.StringIO()):
                main_mod.main(["--config", config, "--dry-run"])
        self.assertFalse(os.path.isdir(state_dir))

    def test_successful_push_exits_0_and_calls_push_with_cfg_derived_base_url(self):
        config = _write_config(
            os.path.join(self.tmp, "estate.env"),
            AIOBS_LANES="",
            AIOBS_HUB_TAILNET_IP="203.0.113.7",
            AIOBS_VM_PORT="9999",
        )
        with patch("aiobs_collector.__main__.push_samples") as mock_push:
            with contextlib.redirect_stdout(io.StringIO()):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 0)
        mock_push.assert_called_once()
        base_url_arg = mock_push.call_args[0][0]
        self.assertEqual(base_url_arg, "http://203.0.113.7:9999")

    def test_successful_push_saves_state(self):
        state_dir = os.path.join(self.tmp, "state")
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="", AIOBS_STATE_DIR=state_dir)
        with patch("aiobs_collector.__main__.push_samples"):
            with contextlib.redirect_stdout(io.StringIO()):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(state_dir))

    def test_failed_push_exits_1_and_does_not_save_state(self):
        state_dir = os.path.join(self.tmp, "state")
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="", AIOBS_STATE_DIR=state_dir)
        with patch("aiobs_collector.__main__.push_samples", side_effect=RuntimeError("hub unreachable")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.isdir(state_dir))
        self.assertIn("hub unreachable", stderr.getvalue())

    def test_missing_hub_ip_exits_2(self):
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="", AIOBS_HUB_TAILNET_IP="")
        with patch("aiobs_collector.__main__.push_samples") as mock_push:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 2)
        mock_push.assert_not_called()

    def test_missing_vm_port_exits_2(self):
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="", AIOBS_VM_PORT="")
        with patch("aiobs_collector.__main__.push_samples") as mock_push:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 2)
        mock_push.assert_not_called()

    def test_missing_state_dir_config_exits_2(self):
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="", AIOBS_STATE_DIR="")
        with patch("aiobs_collector.__main__.push_samples") as mock_push:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main_mod.main(["--config", config])
        self.assertEqual(code, 2)
        mock_push.assert_not_called()

    def test_backfill_pushes_a_sample_that_default_mode_state_would_have_excluded(self):
        state_dir = os.path.join(self.tmp, "state")
        old_ts = _local_ms(2020, 1, 1, 23, 59, 59)
        fake_samples = [
            Sample(
                metric="aiobs_tokens_total",
                labels={"provider": "claude-code", "model": "m", "kind": "input", "origin": "client"},
                value=1.0,
                ts_ms=old_ts,
            )
        ]
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="tokscale", AIOBS_STATE_DIR=state_dir)
        # Pre-seed state as if this exact old day was already fully pushed.
        save_state(state_dir, {"push:tokscale:max_ts_ms": old_ts, "push:tokscale:max_ts_count": 1})

        fake_cls = _fake_lane_class("tokscale", fake_samples)
        with patch.dict("aiobs_collector.__main__._KNOWN_LANES", {"tokscale": fake_cls}, clear=True):
            with patch("aiobs_collector.__main__.push_samples") as mock_push:
                with contextlib.redirect_stdout(io.StringIO()):
                    code_default = main_mod.main(["--config", config])
                    mock_push.reset_mock()
                    code_backfill = main_mod.main(["--config", config, "--backfill"])

        self.assertEqual(code_default, 0)
        self.assertEqual(code_backfill, 0)
        backfill_samples = mock_push.call_args[0][1]
        self.assertIn(fake_samples[0], backfill_samples)

    def test_second_run_does_not_repush_a_day_already_pushed_by_the_first(self):
        # tokscale-style lane: re-emits its FULL history every call (no
        # incremental fetch), the way the real TokscaleLane does. Simulates
        # two consecutive collector cycles and asserts the second cycle's
        # push omits the already-pushed old day.
        state_dir = os.path.join(self.tmp, "state")
        config = _write_config(os.path.join(self.tmp, "estate.env"), AIOBS_LANES="tokscale", AIOBS_STATE_DIR=state_dir)
        old_day_ts = _local_ms(2026, 8, 20, 23, 59, 59) + 999
        old_sample = Sample(
            metric="aiobs_tokens_total",
            labels={"provider": "claude-code", "model": "m", "kind": "input", "origin": "client"},
            value=1.0,
            ts_ms=old_day_ts,
        )
        fake_cls = _fake_lane_class("tokscale", [old_sample])

        with patch.dict("aiobs_collector.__main__._KNOWN_LANES", {"tokscale": fake_cls}, clear=True):
            with patch("aiobs_collector.__main__.push_samples") as mock_push:
                with contextlib.redirect_stdout(io.StringIO()):
                    main_mod.main(["--config", config])
                    first_call_samples = mock_push.call_args[0][1]
                    mock_push.reset_mock()
                    main_mod.main(["--config", config])  # unchanged lane output, second cycle
                    second_call_samples = mock_push.call_args[0][1]

        self.assertIn(old_sample, first_call_samples)
        self.assertNotIn(old_sample, second_call_samples)


if __name__ == "__main__":
    unittest.main()
