"""TDD test suite for aiobs_collector.lane_openrouter (Task 10: OpenRouter lane).

Runnable with no environment variables, from either location:
    python3 -m unittest discover -s workstation/tests -v   # from repo root
    python3 -m unittest discover -s tests -v                # from workstation/

NOTE on the fixture: tests/fixtures/openrouter_activity.json is built from
OpenRouter's DOCUMENTED /api/v1/activity response shape, not a live
capture -- see that file's "_provenance" field and lane_openrouter.py's
module docstring for why (every completion key in ~/.hermes/.env 403/401'd
live; the endpoint requires a separate Management API Key that does not
exist yet). No secret values appear anywhere in this file: HTTP-layer
tests use throwaway literal strings like "fake-test-key-not-real", never a
real key.
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# workstation/ (this file's grandparent) holds the aiobs_collector package.
_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

from aiobs_collector.core import Sample  # noqa: E402
from aiobs_collector.lane_openrouter import (  # noqa: E402
    LaneAuthError,
    LaneConfigError,
    OpenRouterLane,
    normalize_openrouter,
)

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "openrouter_activity.json"


def _load_fixture() -> dict:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _local_ms(*args) -> int:
    """Build an epoch-ms timestamp from local-time datetime() args, the same
    way the implementation is expected to (naive datetime -> local tz via
    .timestamp()). Kept independent of lane_openrouter's own private
    helpers so the test is a black-box check, not a tautology.
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


def _mock_response(doc: dict) -> MagicMock:
    """A MagicMock standing in for what `urllib.request.urlopen` returns,
    usable as `with urlopen(...) as resp: resp.read()`.
    """
    resp = MagicMock()
    resp.read.return_value = json.dumps(doc).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class NormalizeOpenRouterCumulativeTests(unittest.TestCase):
    """Exercises normalize_openrouter against the docs-shape fixture (two
    days, two models: deepseek/deepseek-v4-flash appears both days,
    z-ai/glm-5.1 appears only on day 1)."""

    def setUp(self):
        self.doc = _load_fixture()
        # A "now" safely after both fixture days: both days are history.
        self.now_history = _local_ms(2026, 8, 30, 10, 0, 0)

    def test_provider_label_is_always_constant_openrouter(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        self.assertGreater(len(samples), 0)
        for s in samples:
            self.assertEqual(s.labels["provider"], "openrouter")

    def test_every_token_sample_has_origin_client(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        token_samples = [s for s in samples if s.metric == "aiobs_tokens_total"]
        self.assertGreater(len(token_samples), 0)
        for s in token_samples:
            self.assertEqual(s.labels["origin"], "client")

    def test_every_cost_sample_has_origin_client(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        cost_samples = [s for s in samples if s.metric == "aiobs_cost_usd_total"]
        self.assertGreater(len(cost_samples), 0)
        for s in cost_samples:
            self.assertEqual(s.labels["origin"], "client")

    def test_cumulative_input_tokens_sum_across_days_ascending(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        rows = _find(
            samples, "aiobs_tokens_total",
            model="deepseek/deepseek-v4-flash", kind="input",
        )
        # day1: 12000, day2: 12000 + 9000 = 21000
        self.assertEqual([s.value for s in rows], [12000.0, 21000.0])
        self.assertEqual(rows[0].ts_ms, _end_of_day_ms("2026-08-27"))
        self.assertEqual(rows[1].ts_ms, _end_of_day_ms("2026-08-28"))

    def test_cumulative_output_tokens_sum_across_days_ascending(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        rows = _find(
            samples, "aiobs_tokens_total",
            model="deepseek/deepseek-v4-flash", kind="output",
        )
        # day1: 3200, day2: 3200 + 2600 = 5800
        self.assertEqual([s.value for s in rows], [3200.0, 5800.0])

    def test_cumulative_cost_sum_across_days_ascending(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        rows = _find(samples, "aiobs_cost_usd_total", model="deepseek/deepseek-v4-flash")
        # day1: 0.42, day2: 0.42 + 0.31 = 0.73
        self.assertEqual([round(s.value, 2) for s in rows], [0.42, 0.73])

    def test_model_present_one_day_only_stays_at_last_known_value(self):
        # z-ai/glm-5.1 only appears on day1 -- one point, no flat-lined
        # duplicate on day2; value is exactly that day's total (not zero).
        samples = normalize_openrouter(self.doc, self.now_history)
        input_rows = _find(samples, "aiobs_tokens_total", model="z-ai/glm-5.1", kind="input")
        self.assertEqual(len(input_rows), 1)
        self.assertEqual(input_rows[0].value, 5000.0)
        self.assertEqual(input_rows[0].ts_ms, _end_of_day_ms("2026-08-27"))
        cost_rows = _find(samples, "aiobs_cost_usd_total", model="z-ai/glm-5.1")
        self.assertEqual(len(cost_rows), 1)
        self.assertEqual(round(cost_rows[0].value, 2), 0.18)

    def test_reasoning_tokens_never_produces_a_sample_kind(self):
        # z-ai/glm-5.1's row carries reasoning_tokens=900 -- the frozen kind
        # enum here is input|output only. Must be dropped, never invented.
        samples = normalize_openrouter(self.doc, self.now_history)
        kinds_seen = {
            s.labels["kind"] for s in samples if s.metric == "aiobs_tokens_total"
        }
        self.assertEqual(kinds_seen, {"input", "output"})

    def test_cost_sample_labels_have_no_kind_key(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        cost_samples = [s for s in samples if s.metric == "aiobs_cost_usd_total"]
        self.assertGreater(len(cost_samples), 0)
        for s in cost_samples:
            self.assertNotIn("kind", s.labels)
            self.assertEqual(set(s.labels.keys()), {"provider", "model", "origin"})

    def test_token_sample_labels_exact_key_set(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        token_samples = [s for s in samples if s.metric == "aiobs_tokens_total"]
        self.assertGreater(len(token_samples), 0)
        for s in token_samples:
            self.assertEqual(set(s.labels.keys()), {"provider", "model", "kind", "origin"})

    def test_returns_sample_instances(self):
        samples = normalize_openrouter(self.doc, self.now_history)
        self.assertGreater(len(samples), 0)
        for s in samples:
            self.assertIsInstance(s, Sample)

    def test_provider_name_field_never_used_as_provider_label(self):
        # provider_name ("DeepSeek", "Z.AI") is the *upstream* inference
        # host, not our provider dimension -- our provider is always the
        # constant "openrouter", never derived from this field.
        samples = normalize_openrouter(self.doc, self.now_history)
        providers_seen = {s.labels["provider"] for s in samples}
        self.assertEqual(providers_seen, {"openrouter"})


class NormalizeOpenRouterTimestampTests(unittest.TestCase):
    def setUp(self):
        self.doc = _load_fixture()

    def test_all_history_days_use_end_of_day_local(self):
        now_ms = _local_ms(2026, 8, 30, 10, 0, 0)
        samples = normalize_openrouter(self.doc, now_ms)
        self.assertGreater(len(samples), 0)
        ts_values = {s.ts_ms for s in samples}
        self.assertEqual(
            ts_values, {_end_of_day_ms("2026-08-27"), _end_of_day_ms("2026-08-28")}
        )

    def test_last_fixture_day_as_today_uses_now_ms_not_end_of_day(self):
        now_ms = _local_ms(2026, 8, 28, 14, 30, 0)
        samples = normalize_openrouter(self.doc, now_ms)

        day2_rows = _find(
            samples, "aiobs_tokens_total",
            model="deepseek/deepseek-v4-flash", kind="input",
        )
        self.assertEqual(len(day2_rows), 2)
        self.assertEqual(day2_rows[0].ts_ms, _end_of_day_ms("2026-08-27"))
        self.assertEqual(day2_rows[1].ts_ms, now_ms)
        self.assertNotEqual(now_ms, _end_of_day_ms("2026-08-28"))  # sanity: scenario meaningful

    def test_cost_sample_on_today_also_uses_now_ms(self):
        now_ms = _local_ms(2026, 8, 28, 14, 30, 0)
        samples = normalize_openrouter(self.doc, now_ms)
        rows = _find(samples, "aiobs_cost_usd_total", model="deepseek/deepseek-v4-flash")
        self.assertEqual(rows[-1].ts_ms, now_ms)


class NormalizeOpenRouterOrderingRobustnessTests(unittest.TestCase):
    def test_cumulative_sum_is_correct_even_if_rows_are_out_of_order(self):
        doc = _load_fixture()
        reversed_doc = {**doc, "data": list(reversed(doc["data"]))}
        now_ms = _local_ms(2026, 8, 30, 10, 0, 0)

        in_order = normalize_openrouter(doc, now_ms)
        out_of_order = normalize_openrouter(reversed_doc, now_ms)

        in_order_rows = _find(
            in_order, "aiobs_tokens_total",
            model="deepseek/deepseek-v4-flash", kind="input",
        )
        out_of_order_rows = _find(
            out_of_order, "aiobs_tokens_total",
            model="deepseek/deepseek-v4-flash", kind="input",
        )
        self.assertEqual([s.value for s in in_order_rows], [12000.0, 21000.0])
        self.assertEqual(
            [(s.value, s.ts_ms) for s in in_order_rows],
            [(s.value, s.ts_ms) for s in out_of_order_rows],
        )


class NormalizeOpenRouterSameDayMultiEndpointTests(unittest.TestCase):
    def test_same_date_same_model_different_endpoints_combine_into_one_sample(self):
        # OpenRouter's default (no group_by) response aggregates "by date,
        # model, and endpoint" -- two rows can share (date, model) if they
        # hit different endpoint_ids. The normalizer must sum them into
        # exactly one input + one output + one cost Sample for that
        # (day, model), never one pair per endpoint row.
        doc = {
            "data": [
                {
                    "date": "2026-08-20",
                    "model": "openai/gpt-4.1",
                    "endpoint_id": "endpoint-a",
                    "prompt_tokens": 50,
                    "completion_tokens": 20,
                    "reasoning_tokens": 0,
                    "usage": 0.10,
                },
                {
                    "date": "2026-08-20",
                    "model": "openai/gpt-4.1",
                    "endpoint_id": "endpoint-b",
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "reasoning_tokens": 0,
                    "usage": 0.05,
                },
            ]
        }
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_openrouter(doc, now_ms)

        input_rows = _find(samples, "aiobs_tokens_total", model="openai/gpt-4.1", kind="input")
        output_rows = _find(samples, "aiobs_tokens_total", model="openai/gpt-4.1", kind="output")
        cost_rows = _find(samples, "aiobs_cost_usd_total", model="openai/gpt-4.1")

        self.assertEqual(len(input_rows), 1)
        self.assertEqual(input_rows[0].value, 80.0)
        self.assertEqual(len(output_rows), 1)
        self.assertEqual(output_rows[0].value, 30.0)
        self.assertEqual(len(cost_rows), 1)
        self.assertEqual(round(cost_rows[0].value, 2), 0.15)


class NormalizeOpenRouterZeroValueTests(unittest.TestCase):
    def test_zero_prompt_tokens_emits_no_input_sample(self):
        doc = {
            "data": [
                {
                    "date": "2026-08-20",
                    "model": "some/model",
                    "prompt_tokens": 0,
                    "completion_tokens": 40,
                    "usage": 0.01,
                }
            ]
        }
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_openrouter(doc, now_ms)
        self.assertEqual(_find(samples, "aiobs_tokens_total", model="some/model", kind="input"), [])
        self.assertEqual(len(_find(samples, "aiobs_tokens_total", model="some/model", kind="output")), 1)

    def test_zero_usage_emits_no_cost_sample(self):
        doc = {
            "data": [
                {
                    "date": "2026-08-20",
                    "model": "some/model",
                    "prompt_tokens": 10,
                    "completion_tokens": 0,
                    "usage": 0,
                }
            ]
        }
        now_ms = _local_ms(2026, 8, 25, 10, 0, 0)
        samples = normalize_openrouter(doc, now_ms)
        self.assertEqual(_find(samples, "aiobs_cost_usd_total", model="some/model"), [])
        self.assertEqual(len(_find(samples, "aiobs_tokens_total", model="some/model", kind="input")), 1)


class NormalizeOpenRouterMalformedDocTests(unittest.TestCase):
    NOW = _local_ms(2026, 8, 25, 10, 0, 0)

    def test_empty_doc_returns_empty_list(self):
        self.assertEqual(normalize_openrouter({}, self.NOW), [])

    def test_data_none_returns_empty_list(self):
        self.assertEqual(normalize_openrouter({"data": None}, self.NOW), [])

    def test_data_empty_list_returns_empty_list(self):
        self.assertEqual(normalize_openrouter({"data": []}, self.NOW), [])

    def test_row_missing_date_is_skipped_without_error(self):
        doc = {"data": [{"model": "some/model", "prompt_tokens": 5, "usage": 0.1}]}
        self.assertEqual(normalize_openrouter(doc, self.NOW), [])

    def test_row_missing_model_is_skipped_without_error(self):
        doc = {"data": [{"date": "2026-08-20", "prompt_tokens": 5, "usage": 0.1}]}
        self.assertEqual(normalize_openrouter(doc, self.NOW), [])

    def test_non_dict_row_is_skipped_without_error(self):
        doc = {"data": ["not-a-dict", 123, None]}
        self.assertEqual(normalize_openrouter(doc, self.NOW), [])


class OpenRouterLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_key_file(self, content: str) -> str:
        path = os.path.join(self.tmpdir.name, "fake.env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_name_is_openrouter(self):
        self.assertEqual(OpenRouterLane().name, "openrouter")

    def test_missing_cfg_keys_raise_lane_config_error_without_network_call(self):
        lane = OpenRouterLane()
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(LaneConfigError):
                lane.collect(cfg={}, state={})
            mock_urlopen.assert_not_called()

    def test_missing_key_file_raises_lane_config_error_naming_path(self):
        lane = OpenRouterLane()
        missing_path = os.path.join(self.tmpdir.name, "does-not-exist.env")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": missing_path, "AIOBS_OPENROUTER_KEY_NAME": "SOME_KEY"}
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaisesRegex(LaneConfigError, "does-not-exist.env"):
                lane.collect(cfg=cfg, state={})
            mock_urlopen.assert_not_called()

    def test_key_file_missing_var_raises_lane_config_error_naming_var(self):
        lane = OpenRouterLane()
        path = self._write_key_file("SOME_OTHER_VAR=fake-test-value\n")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaisesRegex(LaneConfigError, "OPENROUTER_MANAGEMENT_KEY"):
                lane.collect(cfg=cfg, state={})
            mock_urlopen.assert_not_called()

    def test_success_returns_normalized_samples_matching_pure_function(self):
        lane = OpenRouterLane()
        path = self._write_key_file("OPENROUTER_MANAGEMENT_KEY=fake-test-key-not-real\n")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        doc = _load_fixture()
        fixed_now_ms = _local_ms(2026, 8, 30, 10, 0, 0)

        with patch(
            "aiobs_collector.lane_openrouter.urllib.request.urlopen",
            return_value=_mock_response(doc),
        ):
            with patch("aiobs_collector.lane_openrouter.time.time", return_value=fixed_now_ms / 1000.0):
                samples = lane.collect(cfg=cfg, state={})

        expected = normalize_openrouter(doc, fixed_now_ms)
        self.assertEqual(samples, expected)

    def test_collect_sends_bearer_header_with_key_from_configured_var(self):
        lane = OpenRouterLane()
        path = self._write_key_file(
            "WRONG_VAR=not-this-one\nOPENROUTER_MANAGEMENT_KEY=fake-test-key-not-real\n"
        )
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        with patch(
            "aiobs_collector.lane_openrouter.urllib.request.urlopen",
            return_value=_mock_response({"data": []}),
        ) as mock_urlopen:
            lane.collect(cfg=cfg, state={})

        mock_urlopen.assert_called_once()
        request_obj = mock_urlopen.call_args[0][0]
        self.assertEqual(request_obj.full_url, "https://openrouter.ai/api/v1/activity")
        self.assertEqual(request_obj.get_header("Authorization"), "Bearer fake-test-key-not-real")
        self.assertEqual(mock_urlopen.call_args.kwargs.get("timeout"), 30)

    def test_403_raises_lane_auth_error_and_message_never_contains_key_value(self):
        lane = OpenRouterLane()
        secret_value = "fake-test-key-not-real-403"
        path = self._write_key_file(f"OPENROUTER_MANAGEMENT_KEY={secret_value}\n")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/activity", code=403, msg="Forbidden", hdrs=None, fp=None
        )
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(LaneAuthError) as ctx:
                lane.collect(cfg=cfg, state={})

        self.assertIsInstance(ctx.exception, LaneConfigError)  # subclass relationship
        message = str(ctx.exception)
        self.assertIn("403", message)
        self.assertIn("OPENROUTER_MANAGEMENT_KEY", message)
        self.assertNotIn(secret_value, message)
        for arg in ctx.exception.args:
            self.assertNotIn(secret_value, str(arg))

    def test_401_raises_lane_auth_error(self):
        lane = OpenRouterLane()
        path = self._write_key_file("OPENROUTER_MANAGEMENT_KEY=fake-test-key-not-real-401\n")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/activity", code=401, msg="Unauthorized", hdrs=None, fp=None
        )
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(LaneAuthError):
                lane.collect(cfg=cfg, state={})

    def test_other_http_error_raises_runtime_error_not_lane_config_error(self):
        # A 500 is a transient/server problem, not the designed auth gap --
        # must not be conflated with LaneConfigError/LaneAuthError.
        lane = OpenRouterLane()
        path = self._write_key_file("OPENROUTER_MANAGEMENT_KEY=fake-test-key-not-real-500\n")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/activity", code=500, msg="Internal Server Error", hdrs=None, fp=None
        )
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(RuntimeError) as ctx:
                lane.collect(cfg=cfg, state={})
        self.assertNotIsInstance(ctx.exception, LaneConfigError)

    def test_collect_propagates_bad_json(self):
        lane = OpenRouterLane()
        path = self._write_key_file("OPENROUTER_MANAGEMENT_KEY=fake-test-key-not-real\n")
        cfg = {"AIOBS_OPENROUTER_ENV_FILE": path, "AIOBS_OPENROUTER_KEY_NAME": "OPENROUTER_MANAGEMENT_KEY"}
        bad_resp = MagicMock()
        bad_resp.read.return_value = b"not json"
        bad_resp.__enter__.return_value = bad_resp
        bad_resp.__exit__.return_value = False
        with patch("aiobs_collector.lane_openrouter.urllib.request.urlopen", return_value=bad_resp):
            with self.assertRaises(json.JSONDecodeError):
                lane.collect(cfg=cfg, state={})


if __name__ == "__main__":
    unittest.main()
