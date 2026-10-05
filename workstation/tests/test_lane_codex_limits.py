import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiobs_collector.lane_codex_limits import (
    CREDITS, OBSERVED, RESETS, USED, CodexLimitsLane, limit_samples, read_limit_events, window_label,
)

NOW_MS = int(datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp() * 1000)


def limits(used=22.0, resets_ms=NOW_MS + 86_400_000, balance="1234.5", window=10080, unlimited=False):
    return {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": window,
                                             "resets_at": resets_ms // 1000},
            "secondary": None, "credits": {"has_credits": True, "unlimited": unlimited, "balance": balance},
            "plan_type": "pro"}


def event(ts_ms, rate_limits):
    stamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"timestamp": stamp, "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {}}, "rate_limits": rate_limits}}


def current(samples, metric, **labels):
    return [s.value for s in samples if s.metric == metric and s.ts_ms == NOW_MS and s.labels == labels]


class LimitSampleTests(unittest.TestCase):
    def test_latest_event_gives_the_current_gauges(self):
        samples = limit_samples([], (NOW_MS - 60_000, limits()), NOW_MS)
        self.assertEqual(current(samples, USED, window="weekly"), [0.22])
        self.assertEqual(current(samples, RESETS, window="weekly"), [float((NOW_MS + 86_400_000) // 1000)])
        self.assertEqual(current(samples, CREDITS), [1234.5])
        self.assertEqual(current(samples, OBSERVED), [(NOW_MS - 60_000) / 1000])

    def test_a_passed_reset_reads_zero_and_drops_the_reset_time(self):
        samples = limit_samples([], (NOW_MS - 3_600_000, limits(used=80.0, resets_ms=NOW_MS - 1000)), NOW_MS)
        self.assertEqual(current(samples, USED, window="weekly"), [0.0])
        self.assertEqual(current(samples, RESETS, window="weekly"), [])

    def test_history_keeps_the_last_event_per_ten_minutes(self):
        base = NOW_MS - NOW_MS % 600_000 - 3_600_000
        events = [(base + 60_000, limits(used=10.0)), (base + 120_000, limits(used=11.0)),
                  (base + 660_000, limits(used=12.0))]
        history = [s for s in limit_samples(events, None, NOW_MS) if s.metric == USED]
        self.assertEqual([(s.ts_ms, s.value) for s in history], [(base + 120_000, 0.11), (base + 660_000, 0.12)])

    def test_unlimited_or_unreadable_credits_are_omitted(self):
        for broken in (limits(unlimited=True), limits(balance="n/a")):
            self.assertEqual(current(limit_samples([], (NOW_MS, broken), NOW_MS), CREDITS), [])

    def test_window_labels(self):
        self.assertEqual([window_label(m) for m in (10080, 300, 1440, 90)], ["weekly", "5h", "daily", "90m"])


class LimitLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sessions" / "2026" / "10" / "05").mkdir(parents=True)

    def test_reads_recent_sessions_and_remembers_the_latest_event(self):
        path = self.root / "sessions" / "2026" / "10" / "05" / "rollout.jsonl"
        lines = [event(NOW_MS - 7_200_000, limits(used=20.0)), event(NOW_MS - 60_000, limits(used=22.0)),
                 {"type": "event_msg", "payload": {"type": "agent_message", "message": "rate_limits token_count"}}]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        self.assertEqual(len(read_limit_events([path], 0)), 2)
        lane = CodexLimitsLane()
        with patch("aiobs_collector.lane_codex_limits.time.time", return_value=NOW_MS / 1000):
            samples = lane.collect({"AIOBS_CODEX_HOME": str(self.root)}, {})
        self.assertEqual(current(samples, USED, window="weekly"), [0.22])
        self.assertTrue(lane.state_data["backfilled"])
        self.assertEqual(lane.state_data["last"][0], NOW_MS - 60_000)

        # No new events on the next run: the remembered latest event still drives the gauges.
        path.unlink()
        again = CodexLimitsLane()
        with patch("aiobs_collector.lane_codex_limits.time.time", return_value=NOW_MS / 1000):
            samples = again.collect({"AIOBS_CODEX_HOME": str(self.root)}, {"lane:codex-limits:data": lane.state_data})
        self.assertEqual(current(samples, USED, window="weekly"), [0.22])


if __name__ == "__main__":
    unittest.main()
