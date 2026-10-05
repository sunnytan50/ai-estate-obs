import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiobs_collector.core import run_lanes
from aiobs_collector.__main__ import _build_lanes, _lane_for_sample
from aiobs_collector.lane_codex_speed import (
    TOKENS, ALLOWANCE, CREDITS, CodexSpeedLane, normalize_rollouts, read_modes,
)
from aiobs_collector.monotonic import apply_monotonic

NOW = int(datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
WHEN = "2026-09-29T12:00:00Z"
TS = datetime.fromisoformat(WHEN.replace("Z", "+00:00")).timestamp()


def context(turn="turn", model="gpt-6-astra"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model}}


def usage(input=100, cached=40, output=10, timestamp=WHEN):
    return {"type": "event_msg", "timestamp": timestamp, "payload": {
        "type": "token_count", "info": {"total_token_usage": {
            "input_tokens": input, "cached_input_tokens": cached,
            "output_tokens": output, "reasoning_output_tokens": output,
        }, "last_token_usage": {"input_tokens": input, "output_tokens": output}}}}


class CodexSpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def rollout(self, records, name="a.jsonl", thread="thread"):
        p = self.root / name
        p.write_text("\n".join(json.dumps(r) for r in [
            {"type": "session_meta", "payload": {"id": thread}}, *records]) + "\n")
        return p

    def modes(self, speed="ultrafast", billing="subscription", model="gpt-6-astra"):
        return {"thread/turn": [[TS - 1, model, speed, billing]]}

    def latest(self, samples, metric, **labels):
        return sum(s.value for s in samples if s.ts_ms == NOW and s.metric == metric
                   and all(s.labels.get(k) == v for k, v in labels.items()))

    def test_ultrafast_weights_uncached_cache_and_output_once(self):
        p = self.rollout([context(), usage()])
        samples = normalize_rollouts([p], self.modes(), NOW)
        base = (60 * 250 + 40 * 25 + 10 * 1250) / 1e6
        self.assertAlmostEqual(self.latest(samples, ALLOWANCE), base * 8)
        self.assertAlmostEqual(self.latest(samples, CREDITS), base * 6)
        self.assertEqual(self.latest(samples, TOKENS), 110)
        self.assertEqual(self.latest(samples, TOKENS, kind="input"), 60)
        # Reasoning is a subset of output, never added to it a second time.
        self.assertEqual(self.latest(samples, TOKENS, kind="output"), 10)

    def test_standard_and_fast_multipliers(self):
        p = self.rollout([context(), usage()])
        for speed, included, paid in [("standard", 1, 1), ("fast", 2.5, 2)]:
            with self.subTest(speed=speed):
                s = normalize_rollouts([p], self.modes(speed), NOW)
                self.assertAlmostEqual(self.latest(s, ALLOWANCE), .0285 * included)
                self.assertAlmostEqual(self.latest(s, CREDITS), .0285 * paid)

    def test_duplicate_and_cumulative_notifications_do_not_multiply_usage(self):
        p = self.rollout([context(), usage(), usage(), usage(200, 80, 20)])
        s = normalize_rollouts([p], self.modes(), NOW)
        self.assertEqual(self.latest(s, TOKENS), 220)

    def test_null_usage_does_not_reset_baseline(self):
        p = self.rollout([context(), usage(), {"type": "event_msg", "payload": {
            "type": "token_count", "info": None}}, usage()])
        self.assertEqual(self.latest(normalize_rollouts([p], self.modes(), NOW), TOKENS), 110)

    def test_counter_reset_counts_new_context(self):
        p = self.rollout([context(), usage(), usage(10, 4, 1)])
        self.assertEqual(self.latest(normalize_rollouts([p], self.modes(), NOW), TOKENS), 121)

    def test_mode_transition_within_turn_uses_request_timestamp(self):
        p = self.rollout([context(), usage(), usage(200, 80, 20, "2026-09-29T12:00:02Z")])
        modes = self.modes("standard")
        modes["thread/turn"].append([TS + 1, "gpt-6-astra", "ultrafast", "subscription"])
        s = normalize_rollouts([p], modes, NOW)
        self.assertEqual(self.latest(s, TOKENS, speed="standard"), 110)
        self.assertEqual(self.latest(s, TOKENS, speed="ultrafast"), 110)

    def test_missing_future_or_mismatched_metadata_is_unknown(self):
        p = self.rollout([context(), usage()])
        future = self.modes(); future["thread/turn"][0][0] = TS + 1
        for modes in [{}, future, self.modes(model="gpt-6-sol")]:
            s = normalize_rollouts([p], modes, NOW)
            self.assertEqual(self.latest(s, TOKENS, speed="unknown"), 110)
            self.assertEqual(self.latest(s, ALLOWANCE), 0)

    def test_api_and_unknown_auth_never_consume_subscription_estimate(self):
        p = self.rollout([context(), usage()])
        for billing in ["api", "unknown"]:
            s = normalize_rollouts([p], self.modes(billing=billing), NOW)
            self.assertEqual(self.latest(s, TOKENS), 110)
            self.assertEqual(self.latest(s, ALLOWANCE), 0)
            self.assertEqual(self.latest(s, CREDITS), 0)

    def test_inherited_history_is_deduplicated(self):
        a = self.rollout([context(), usage()])
        b = self.rollout([context(), usage()], "b.jsonl", thread="fork")
        self.assertEqual(self.latest(normalize_rollouts([a, b], self.modes(), NOW), TOKENS), 110)

    def test_other_models_are_excluded_and_baseline_still_advances(self):
        p = self.rollout([context(model="gpt-6-sol"), usage(), context(), usage(200, 80, 20)])
        self.assertEqual(self.latest(normalize_rollouts([p], self.modes(), NOW), TOKENS), 110)

    def test_partial_line_and_invalid_counts_do_not_export_content(self):
        p = self.rollout([context(), usage(input=-1), usage(), {"type": "response_item",
            "payload": {"secret": "PRIVATE PROMPT"}}])
        with p.open("a") as f: f.write('{"type":"event_msg","payload":{"type":"token_count"')
        s = normalize_rollouts([p], self.modes(), NOW)
        self.assertEqual(self.latest(s, TOKENS), 110)
        self.assertNotIn("PRIVATE", repr(s))
        self.assertFalse(any("thread" in s.labels or "turn" in s.labels for s in s))

    def test_late_mode_metadata_does_not_reclassify_emitted_tokens(self):
        p = self.rollout([context(), usage()])
        attributions = {}
        first = normalize_rollouts([p], {}, NOW, attributions=attributions)
        _, state = apply_monotonic(first, {})
        second = normalize_rollouts([p], self.modes(), NOW, attributions=attributions)
        shaped, _ = apply_monotonic(second, state)
        self.assertEqual(self.latest(shaped, TOKENS), 110)
        self.assertEqual(self.latest(shaped, TOKENS, speed="unknown"), 110)
        self.assertEqual(self.latest(shaped, ALLOWANCE), 0)

    def test_recent_unknown_event_waits_for_metadata_before_counting(self):
        p = self.rollout([context(), usage()])
        attributions = {}
        first = normalize_rollouts([p], {}, int((TS + 60) * 1000),
                                   attributions=attributions)
        self.assertFalse(any(x.metric == TOKENS for x in first))
        self.assertEqual(attributions, {})
        second = normalize_rollouts([p], self.modes(), NOW, attributions=attributions)
        self.assertEqual(self.latest(second, TOKENS, speed="ultrafast"), 110)

    def test_malformed_log_metadata_is_skipped_or_kept_unknown(self):
        valid = ('run_sampling_request{turn_id=turn model=gpt-6-astra}: '
                 'auth_mode=Some(Chatgpt) tags_json={"service_tier": []}')
        p = self.database([(1, int(TS), 0, "thread", None),
                           (2, None, 0, "thread", valid),
                           (3, int(TS), 0, "thread", valid)])
        modes = read_modes(p, {"broken": None})
        self.assertEqual(modes["thread/turn"][0][2:], ["unknown", "subscription"])

    def database(self, rows):
        p = self.root / "logs_2.sqlite"
        with sqlite3.connect(p) as c:
            c.execute("CREATE TABLE logs(id INTEGER, ts INTEGER, ts_nanos INTEGER, "
                      "thread_id TEXT, target TEXT, module_path TEXT, feedback_log_body TEXT)")
            c.executemany("INSERT INTO logs VALUES(?,?,?,?,'feedback_tags',"
                          "'codex_core::session::turn',?)", rows)
        return p

    def test_log_aliases_auth_and_retention_cache(self):
        rows = []
        for i, (tier, auth) in enumerate([("priority", "Some(Chatgpt)"),
                                        ("default", "Some(ApiKey)"),
                                        ("ultrafast", "Some(Chatgpt)"),
                                        ("unset", "Some(Chatgpt)"),
                                        ("future-tier", "None")]):
            body = (f'run_sampling_request{{turn_id=turn{i} model=gpt-6-astra}}: '
                    f'auth_mode={auth} tags_json=' + json.dumps({"service_tier": tier}))
            rows.append((i, int(TS - 1), 500000000, "thread", body))
        p = self.database(rows)
        original = {"old/turn": [[TS - 100, "gpt-6-astra", "ultrafast", "subscription"]]}
        modes = read_modes(p, original)
        self.assertEqual(modes["thread/turn0"][0][2:], ["fast", "subscription"])
        self.assertEqual(modes["thread/turn1"][0][2:], ["standard", "api"])
        self.assertEqual(modes["thread/turn4"][0][2:], ["unknown", "unknown"])
        self.assertEqual(modes["old/turn"], original["old/turn"])
        self.assertEqual(read_modes(p, modes), modes)
        self.assertEqual(len(original), 1)

    def test_v1_cache_migration_keeps_previously_unknown_usage_unknown(self):
        p = self.rollout([context(), usage()])
        sessions = self.root / "sessions"
        sessions.mkdir()
        p.rename(sessions / "a.jsonl")
        body = ('run_sampling_request{turn_id=turn model=gpt-6-astra}: '
                'auth_mode=Some(Chatgpt) tags_json={"service_tier":"ultrafast"}')
        self.database([(1, int(TS - 1), 0, "thread", body)])
        state = {"lane:codex-speed:data": {"modes": {}},
                 "lane:codex-speed:last_success_ms": NOW - 1000}
        lane = CodexSpeedLane()
        with patch("aiobs_collector.lane_codex_speed.time.time", return_value=NOW / 1000):
            samples = lane.collect({"AIOBS_CODEX_HOME": str(self.root)}, state)
        self.assertEqual(self.latest(samples, TOKENS, speed="unknown"), 110)
        self.assertEqual(self.latest(samples, ALLOWANCE), 0)
        self.assertTrue(lane.state_data["attributions"])
        self.assertNotIn("attributions", state["lane:codex-speed:data"])

    def test_corrupt_lane_cache_does_not_prevent_collection(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        self.rollout([context(), usage()]).rename(sessions / "a.jsonl")
        self.database([])
        with patch("aiobs_collector.lane_codex_speed.time.time", return_value=NOW / 1000):
            samples = CodexSpeedLane().collect(
                {"AIOBS_CODEX_HOME": str(self.root)}, {"lane:codex-speed:data": ["broken"]})
        self.assertEqual(self.latest(samples, TOKENS, speed="unknown"), 110)

    def test_lane_state_persists_only_on_success(self):
        class Success:
            name = "test"
            state_data = {"modes": {"old": []}}
            def collect(self, cfg, state): return []
        state = {"unchanged": 1}
        _, new = run_lanes([Success()], {}, state, NOW)
        self.assertEqual(new["lane:test:data"], Success.state_data)
        self.assertEqual(state, {"unchanged": 1})

    def test_new_lane_attribution_and_monotonic_after_transcript_loss(self):
        self.assertIsInstance(_build_lanes({"AIOBS_LANES": "codex-speed"})[0], CodexSpeedLane)
        p = self.rollout([context(), usage()])
        s = normalize_rollouts([p], self.modes(), NOW)
        self.assertTrue(all(_lane_for_sample(x) == "codex-speed" for x in s))
        _, state = apply_monotonic(s, {})
        smaller = self.rollout([context(), usage(10, 4, 1)])
        shaped, _ = apply_monotonic(normalize_rollouts([smaller], self.modes(), NOW), state)
        self.assertAlmostEqual(self.latest(shaped, ALLOWANCE), self.latest(s, ALLOWANCE))

    def test_a_line_cut_mid_character_does_not_fail_the_lane(self):
        p = self.rollout([context(), usage()])
        with open(p, "ab") as handle:
            handle.write('{"type": "event_msg", "payload": {"type": "agent_message", "message": "caf'.encode() + "é".encode()[:1])
        self.assertEqual(self.latest(normalize_rollouts([p], self.modes(), NOW), TOKENS), 110)


if __name__ == "__main__":
    unittest.main()
