import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aiobs_collector.codex_usage import speed_tokens

WHEN = "2026-10-02T12:00:00Z"
TS = datetime.fromisoformat(WHEN.replace("Z", "+00:00")).timestamp()
DAY = datetime.fromtimestamp(TS).date().isoformat()


def context(turn="turn", model="gpt-6-astra"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model}}


def usage(input=100, cached=40, output=10, when=WHEN):
    return {"type": "event_msg", "timestamp": when, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": input, "cached_input_tokens": cached, "output_tokens": output}}}}


class CodexSpeedSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def rollout(self, records, name="a.jsonl", thread="thread"):
        path = self.root / name
        path.write_text("\n".join(json.dumps(r) for r in [
            {"type": "session_meta", "payload": {"id": thread}}, *records]) + "\n")
        return path

    def test_every_model_gets_its_recorded_speed(self):
        a = self.rollout([context("t1", "gpt-6-astra"), usage()], name="a.jsonl")
        b = self.rollout([context("t2", "gpt-6.1-sol"), usage(50, 0, 5)], name="b.jsonl", thread="other")
        modes = {"thread/t1": [[TS - 1, "gpt-6-astra", "fast", "subscription"]],
                 "other/t2": [[TS - 1, "gpt-6.1-sol", "standard", "subscription"]]}
        split = speed_tokens([a, b], modes)
        self.assertEqual(split[(DAY, "gpt-6-astra", "input", "fast")], 60)
        self.assertEqual(split[(DAY, "gpt-6-astra", "cache_read", "fast")], 40)
        self.assertEqual(split[(DAY, "gpt-6-astra", "output", "fast")], 10)
        self.assertEqual(split[(DAY, "gpt-6.1-sol", "input", "standard")], 50)

    def test_missing_metadata_is_unknown(self):
        split = speed_tokens([self.rollout([context(), usage()])], {})
        self.assertEqual(split[(DAY, "gpt-6-astra", "input", "unknown")], 60)

    def test_an_older_tier_is_not_reused_after_a_model_switch(self):
        modes = {"thread/turn": [[TS - 1, "gpt-6.1-sol", "fast", "subscription"]]}
        split = speed_tokens([self.rollout([context(), usage()])], modes)
        self.assertNotIn((DAY, "gpt-6-astra", "input", "fast"), split)
        self.assertEqual(split[(DAY, "gpt-6-astra", "input", "unknown")], 60)

    def test_repeated_and_cumulative_notifications_count_once(self):
        path = self.rollout([context(), usage(), usage(), usage(200, 80, 20)])
        self.assertEqual(sum(speed_tokens([path], {}).values()), 220)

    def test_inherited_fork_history_counts_once(self):
        records = [context(), usage()]
        a = self.rollout(records, name="a.jsonl")
        b = self.rollout(records, name="b.jsonl")
        self.assertEqual(sum(speed_tokens([a, b], {}).values()), 110)

    def test_since_date_drops_earlier_days(self):
        path = self.rollout([context(), usage(when="2026-09-20T12:00:00Z")])
        self.assertEqual(speed_tokens([path], {}, since_date="2026-10-01"), {})


if __name__ == "__main__":
    unittest.main()
