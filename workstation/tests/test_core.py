"""TDD test suite for aiobs_collector.core (Task 8: collector core).

Runnable with no environment variables, from either location:
    python3 -m unittest discover -s workstation/tests -v   # from repo root
    python3 -m unittest discover -s tests -v                # from workstation/
"""

import dataclasses
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# workstation/ (this file's grandparent) holds the aiobs_collector package.
# Put it on sys.path explicitly so `import aiobs_collector` works regardless
# of the current working directory or PYTHONPATH.
_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

from aiobs_collector.core import (  # noqa: E402
    Lane,
    Sample,
    load_config,
    load_state,
    render_exposition,
    run_lanes,
    save_state,
)


class SampleTests(unittest.TestCase):
    def test_is_frozen(self):
        sample = Sample(metric="m", labels={}, value=1.0, ts_ms=1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sample.value = 2.0  # type: ignore[misc]


class LaneProtocolTests(unittest.TestCase):
    def test_declares_name_and_collect(self):
        # Lane is a typing.Protocol (structural) -- assert the declared shape
        # without touching Protocol's runtime-checkable isinstance machinery.
        self.assertIn("name", getattr(Lane, "__annotations__", {}))
        self.assertTrue(callable(getattr(Lane, "collect", None)))


class RenderExpositionTests(unittest.TestCase):
    def test_brief_example_line_sorted_labels(self):
        sample = Sample(
            metric="aiobs_tokens_total",
            labels={
                "provider": "claude-code",
                "kind": "input",
                "model": "opus",
                "origin": "client",
            },
            value=123.0,
            ts_ms=1756400000000,
        )
        expected = (
            'aiobs_tokens_total{kind="input",model="opus",'
            'origin="client",provider="claude-code"} 123 1756400000000'
        )
        self.assertEqual(render_exposition([sample]), expected)

    def test_escapes_backslash_in_label_value(self):
        sample = Sample(metric="m", labels={"k": "a\\b"}, value=1.0, ts_ms=1)
        self.assertEqual(render_exposition([sample]), 'm{k="a\\\\b"} 1 1')

    def test_escapes_double_quote_in_label_value(self):
        sample = Sample(metric="m", labels={"k": 'a"b'}, value=1.0, ts_ms=1)
        self.assertEqual(render_exposition([sample]), 'm{k="a\\"b"} 1 1')

    def test_escapes_newline_in_label_value(self):
        sample = Sample(metric="m", labels={"k": "a\nb"}, value=1.0, ts_ms=1)
        self.assertEqual(render_exposition([sample]), 'm{k="a\\nb"} 1 1')

    def test_escape_order_is_backslash_first_combined_regressions(self):
        # These two values regress the ORDER of the .replace() chain in
        # _escape_label_value, not just that each escape fires in
        # isolation. Backslash-escaping must run before quote- and
        # newline-escaping: if it didn't, the backslash those two
        # introduce would itself get doubled by a later backslash pass,
        # producing one extra backslash pair in the output. Built via
        # concatenation (rather than one dense literal) so each piece is
        # independently obvious -- less room for a hand-escaping mistake
        # in the test itself.

        # backslash immediately followed by a quote: a \ " b
        backslash_then_quote = "a" + "\\" + '"' + "b"
        sample_a = Sample(metric="m", labels={"k": backslash_then_quote}, value=1.0, ts_ms=1)
        expected_a = 'm{k="a' + "\\" * 3 + '"b"} 1 1'
        self.assertEqual(render_exposition([sample_a]), expected_a)

        # backslash immediately followed by a newline: a \ <newline> b
        backslash_then_newline = "a" + "\\" + "\n" + "b"
        sample_b = Sample(metric="m", labels={"k": backslash_then_newline}, value=1.0, ts_ms=1)
        expected_b = 'm{k="a' + "\\" * 3 + 'nb"} 1 1'
        self.assertEqual(render_exposition([sample_b]), expected_b)

    def test_multiple_samples_join_with_newline_no_trailing_newline(self):
        samples = [
            Sample(metric="a", labels={}, value=1.0, ts_ms=1),
            Sample(metric="b", labels={}, value=2.0, ts_ms=2),
        ]
        result = render_exposition(samples)
        self.assertEqual(result, "a 1 1\nb 2 2")
        self.assertFalse(result.endswith("\n"))

    def test_empty_samples_list_returns_empty_string(self):
        self.assertEqual(render_exposition([]), "")

    def test_whole_number_float_prints_without_decimal_point(self):
        sample = Sample(metric="m", labels={}, value=100.0, ts_ms=1)
        self.assertEqual(render_exposition([sample]), "m 100 1")

    def test_fractional_float_prints_shortest_representation(self):
        sample = Sample(metric="m", labels={}, value=12.5, ts_ms=1)
        self.assertEqual(render_exposition([sample]), "m 12.5 1")


class LoadConfigTests(unittest.TestCase):
    def _write(self, text):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        path = os.path.join(tmp_dir.name, "estate.env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_ignores_blank_lines_and_comment_lines(self):
        path = self._write(
            "\n"
            "# a full-line comment\n"
            "AIOBS_VM_PORT=8428\n"
            "\n"
            "   \n"
            "# another comment\n"
            "AIOBS_GRAFANA_PORT=3000\n"
        )
        cfg = load_config(path)
        self.assertEqual(cfg, {"AIOBS_VM_PORT": "8428", "AIOBS_GRAFANA_PORT": "3000"})

    def test_expands_leading_tilde_in_value(self):
        path = self._write("AIOBS_STATE_DIR=~/.local/state/aiobs\n")
        cfg = load_config(path)
        expected = os.path.expanduser("~/.local/state/aiobs")
        self.assertEqual(cfg["AIOBS_STATE_DIR"], expected)
        self.assertNotIn("~", cfg["AIOBS_STATE_DIR"])

    def test_strips_surrounding_double_and_single_quotes(self):
        path = self._write(
            'AIOBS_A="double quoted"\n'
            "AIOBS_B='single quoted'\n"
            "AIOBS_C=unquoted\n"
        )
        cfg = load_config(path)
        self.assertEqual(cfg["AIOBS_A"], "double quoted")
        self.assertEqual(cfg["AIOBS_B"], "single quoted")
        self.assertEqual(cfg["AIOBS_C"], "unquoted")

    def test_later_keys_override_earlier(self):
        path = self._write("AIOBS_LANES=tokscale\nAIOBS_LANES=tokscale,openrouter\n")
        cfg = load_config(path)
        self.assertEqual(cfg["AIOBS_LANES"], "tokscale,openrouter")

    def test_strips_surrounding_whitespace_around_key_and_value(self):
        path = self._write("  AIOBS_X   =   value-here   \n")
        cfg = load_config(path)
        self.assertEqual(cfg["AIOBS_X"], "value-here")

    def test_returns_plain_str_keyed_dict(self):
        path = self._write("A=1\nB=2\n")
        cfg = load_config(path)
        self.assertIsInstance(cfg, dict)
        for key, value in cfg.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, str)

    # --- fix round 1: dotenv-style inline-comment stripping ---

    def test_inline_comment_stripped_from_unquoted_value(self):
        path = self._write("KEY=a,b  # note\n")
        cfg = load_config(path)
        self.assertEqual(cfg["KEY"], "a,b")

    def test_hash_inside_double_quotes_is_kept_trailing_comment_stripped(self):
        path = self._write('KEY="a # b" # note\n')
        cfg = load_config(path)
        self.assertEqual(cfg["KEY"], "a # b")

    def test_hash_with_no_preceding_whitespace_is_part_of_value(self):
        path = self._write("KEY=abc#def\n")
        cfg = load_config(path)
        self.assertEqual(cfg["KEY"], "abc#def")

    def test_hash_inside_single_quotes_is_kept_trailing_comment_stripped(self):
        path = self._write("KEY='a # b' # note\n")
        cfg = load_config(path)
        self.assertEqual(cfg["KEY"], "a # b")

    def test_value_that_is_entirely_a_comment_yields_empty_string(self):
        # Mirrors the real estate.env pattern: AIOBS_TOKSCALE_VERSION=<spaces># pin: ...
        path = self._write("KEY=                   # pin: filled in later\n")
        cfg = load_config(path)
        self.assertEqual(cfg["KEY"], "")

    def test_inline_comment_stripped_before_tilde_expansion(self):
        # Mirrors the real estate.env pattern: AIOBS_OPENROUTER_ENV_FILE=~/.hermes/.env  # ...
        path = self._write("KEY=~/.hermes/.env  # file containing the key\n")
        cfg = load_config(path)
        self.assertEqual(cfg["KEY"], os.path.expanduser("~/.hermes/.env"))


class StateTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp = tmp_dir.name

    def test_load_state_missing_dir_returns_empty_dict(self):
        missing = os.path.join(self.tmp, "does", "not", "exist")
        self.assertEqual(load_state(missing), {})

    def test_load_state_missing_file_in_existing_dir_returns_empty_dict(self):
        self.assertEqual(load_state(self.tmp), {})

    def test_load_state_corrupt_json_returns_empty_dict(self):
        path = os.path.join(self.tmp, "collector-state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json::")
        self.assertEqual(load_state(self.tmp), {})

    def test_load_state_non_dict_json_returns_empty_dict(self):
        path = os.path.join(self.tmp, "collector-state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]")
        self.assertEqual(load_state(self.tmp), {})

    def test_save_then_load_round_trip(self):
        state = {"lane:tokscale:last_success_ms": 1756400000000, "n": 3}
        save_state(self.tmp, state)
        self.assertEqual(load_state(self.tmp), state)

    def test_save_state_creates_dir_with_parents(self):
        nested = os.path.join(self.tmp, "a", "b", "c")
        save_state(nested, {"x": 1})
        self.assertTrue(os.path.isdir(nested))
        self.assertEqual(load_state(nested), {"x": 1})

    def test_save_state_dir_mode_0700(self):
        target = os.path.join(self.tmp, "state-dir")
        save_state(target, {})
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode, 0o700)

    def test_save_state_tightens_pre_existing_dir_to_0700(self):
        # test_save_state_dir_mode_0700 only covers a *freshly created*
        # dir, where os.makedirs' own mode= argument is what sets 0700 --
        # the explicit os.chmod afterward is unobservable there since the
        # dir is already correct. Here the dir pre-exists at a looser
        # mode (0755, as a plain `mkdir` or an earlier run might leave
        # it); os.makedirs(..., exist_ok=True) ignores mode= for a dir
        # that already exists, so only the explicit os.chmod call in
        # save_state can be what tightens this one to 0700.
        target = os.path.join(self.tmp, "pre-existing-state-dir")
        os.mkdir(target)
        os.chmod(target, 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)

        save_state(target, {})

        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode, 0o700)


class _GoodLane:
    name = "good"

    def collect(self, cfg, state):
        return [
            Sample(metric="aiobs_tokens_total", labels={"provider": "x"}, value=5.0, ts_ms=1)
        ]


class _BadLane:
    name = "bad"

    def collect(self, cfg, state):
        raise RuntimeError("boom")


class _RecordingLane:
    def __init__(self, name):
        self.name = name
        self.seen_state = "UNSET"

    def collect(self, cfg, state):
        self.seen_state = state
        return []


class _EvilLane:
    name = "evil"

    def collect(self, cfg, state):
        raise KeyboardInterrupt()


def _find(samples, metric, **labels):
    return [
        s
        for s in samples
        if s.metric == metric and all(s.labels.get(k) == v for k, v in labels.items())
    ]


class RunLanesTests(unittest.TestCase):
    NOW_MS = 1756400600000

    def test_good_lane_samples_survive_bad_lane_failure(self):
        samples, _ = run_lanes([_GoodLane(), _BadLane()], cfg={}, state={}, now_ms=self.NOW_MS)
        good_samples = _find(samples, "aiobs_tokens_total", provider="x")
        self.assertEqual(len(good_samples), 1)
        self.assertEqual(good_samples[0].value, 5.0)

    def test_lane_up_is_1_for_good_and_0_for_bad(self):
        samples, _ = run_lanes([_GoodLane(), _BadLane()], cfg={}, state={}, now_ms=self.NOW_MS)

        up_good = _find(samples, "aiobs_lane_up", lane="good")
        self.assertEqual(len(up_good), 1)
        self.assertEqual(up_good[0].value, 1.0)
        self.assertEqual(up_good[0].ts_ms, self.NOW_MS)

        up_bad = _find(samples, "aiobs_lane_up", lane="bad")
        self.assertEqual(len(up_bad), 1)
        self.assertEqual(up_bad[0].value, 0.0)
        self.assertEqual(up_bad[0].ts_ms, self.NOW_MS)

    def test_good_lane_records_last_success_in_state_and_sample(self):
        samples, new_state = run_lanes([_GoodLane()], cfg={}, state={}, now_ms=self.NOW_MS)

        self.assertEqual(new_state["lane:good:last_success_ms"], self.NOW_MS)
        ts_samples = _find(samples, "aiobs_lane_last_success_timestamp", lane="good")
        self.assertEqual(len(ts_samples), 1)
        self.assertEqual(ts_samples[0].value, self.NOW_MS / 1000.0)
        self.assertEqual(ts_samples[0].ts_ms, self.NOW_MS)

    def test_bad_lane_with_no_prior_success_has_no_timestamp_sample(self):
        samples, new_state = run_lanes([_BadLane()], cfg={}, state={}, now_ms=self.NOW_MS)

        self.assertNotIn("lane:bad:last_success_ms", new_state)
        self.assertEqual(_find(samples, "aiobs_lane_last_success_timestamp", lane="bad"), [])

    def test_bad_lane_keeps_prior_last_success_from_state(self):
        prior_ms = self.NOW_MS - 3_600_000
        input_state = {"lane:bad:last_success_ms": prior_ms}
        samples, new_state = run_lanes(
            [_BadLane()], cfg={}, state=input_state, now_ms=self.NOW_MS
        )

        self.assertEqual(new_state["lane:bad:last_success_ms"], prior_ms)
        ts_samples = _find(samples, "aiobs_lane_last_success_timestamp", lane="bad")
        self.assertEqual(len(ts_samples), 1)
        self.assertEqual(ts_samples[0].value, prior_ms / 1000.0)
        self.assertEqual(ts_samples[0].ts_ms, self.NOW_MS)
        self.assertEqual(_find(samples, "aiobs_lane_up", lane="bad")[0].value, 0.0)

    def test_input_state_dict_is_not_mutated(self):
        input_state = {"lane:good:last_success_ms": 1}
        original_copy = dict(input_state)
        _, new_state = run_lanes([_GoodLane()], cfg={}, state=input_state, now_ms=self.NOW_MS)

        self.assertEqual(input_state, original_copy)
        self.assertIsNot(new_state, input_state)

    def test_each_lane_receives_the_original_state_not_an_evolving_one(self):
        lane_a = _RecordingLane("a")
        lane_b = _RecordingLane("b")
        original_state = {"lane:a:last_success_ms": 1}
        run_lanes([lane_a, lane_b], cfg={}, state=original_state, now_ms=self.NOW_MS)

        self.assertEqual(lane_a.seen_state, original_state)
        self.assertEqual(lane_b.seen_state, original_state)
        self.assertIs(lane_a.seen_state, lane_b.seen_state)

    def test_catches_exception_not_base_exception(self):
        with self.assertRaises(KeyboardInterrupt):
            run_lanes([_EvilLane()], cfg={}, state={}, now_ms=self.NOW_MS)

    def test_empty_lanes_list(self):
        samples, new_state = run_lanes([], cfg={}, state={"k": "v"}, now_ms=self.NOW_MS)
        self.assertEqual(samples, [])
        self.assertEqual(new_state, {"k": "v"})


if __name__ == "__main__":
    unittest.main()
