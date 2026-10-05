import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiobs_collector.lane_tokscale import TokscaleLane, _end_of_day_local_ms, clear_tokscale_graph_cache
from aiobs_collector.lane_usage import (
    FALLBACK, LEDGER_VERSION, TOKENS, VALUE, UsageLane, _modified_since, cumulative_samples, day_entries,
    dump_ledger, load_ledger,
)

DAY = "2026-10-02"


def row(client, model, input=0, output=0, reasoning=0, cache_read=0, cache_write=0, cost=0.0):
    return {"client": client, "modelId": model, "cost": cost, "tokens": {
        "input": input, "output": output, "reasoning": reasoning, "cacheRead": cache_read, "cacheWrite": cache_write}}


def doc(*days):
    return {"contributions": [{"date": day, "clients": list(rows)} for day, rows in days]}


def claude_bucket(input=0, output=0, cache_read=0, w5m=0, w1h=0):
    return {"input": input, "output": output, "cache_read": cache_read, "cache_write_5m": w5m, "cache_write_1h": w1h}


class DayEntriesTests(unittest.TestCase):
    def test_claude_code_is_valued_from_transcripts_with_one_hour_writes(self):
        claude = {(DAY, "claude-opus-5-5", "standard"): claude_bucket(2, 100, 1000, 100, 200)}
        entry = day_entries(doc(), claude, {})[DAY]
        # 2x$4 + 100x$20 + 1000x$0.20 + 100x$5 (5m) + 200x$8 (1h) per million
        self.assertAlmostEqual(entry[(VALUE, "claude-code", "claude-opus-5-5", "")], 4308 / 1e6)
        self.assertEqual(entry[(TOKENS, "claude-code", "claude-opus-5-5", "cache_write")], 300)
        self.assertEqual(entry[(TOKENS, "claude-code", "claude-opus-5-5", "output")], 100)

    def test_tokscale_claude_rows_only_price_models_the_table_lacks(self):
        claude = {(DAY, "claude-opus-5-5", "standard"): claude_bucket(output=1_000_000),
                  (DAY, "claude-legacy-9", "standard"): claude_bucket(output=10)}
        tokscale = doc((DAY, [row("claude", "claude-opus-5-5", output=1_000_000, cost=999.0),
                              row("claude", "claude-legacy-9", output=10, cost=3.5)]))
        entry = day_entries(tokscale, claude, {})[DAY]
        self.assertAlmostEqual(entry[(VALUE, "claude-code", "claude-opus-5-5", "")], 20.0)  # table, not 999
        self.assertEqual(entry[(VALUE, "claude-code", "claude-legacy-9", "")], 3.5)
        self.assertEqual(entry[(FALLBACK, "claude-code", "claude-legacy-9", "")], 3.5)
        self.assertEqual(entry[(TOKENS, "claude-code", "claude-opus-5-5", "output")], 1_000_000)  # not doubled

    def test_codex_output_includes_reasoning_and_speed_shares_apply(self):
        tokscale = doc((DAY, [row("codex", "gpt-6-astra", input=1_000_000, output=100_000,
                                  reasoning=50_000, cache_read=10_000_000)]))
        split = {}
        for kind in ("input", "cache_read", "output"):
            split[(DAY, "gpt-6-astra", kind, "fast")] = 3
            split[(DAY, "gpt-6-astra", kind, "standard")] = 1
        entry = day_entries(tokscale, {}, split)[DAY]
        self.assertEqual(entry[(TOKENS, "codex", "gpt-6-astra", "output")], 150_000)
        # Standard: 1M x $10 + 150K x $50 + 10M x $1 = $27.50; 3/4 at Fast (2x) + 1/4 at Standard
        self.assertAlmostEqual(entry[(VALUE, "codex", "gpt-6-astra", "")], 27.5 * 1.75)
        self.assertAlmostEqual(day_entries(tokscale, {}, {})[DAY][(VALUE, "codex", "gpt-6-astra", "")], 27.5)

    def test_models_without_an_official_rate_use_tokscale_cost_and_are_flagged(self):
        entry = day_entries(doc((DAY, [row("droid", "glm-5-2", input=10, cost=1.23)])), {}, {})[DAY]
        self.assertEqual(entry[(VALUE, "droid", "glm-5-2", "")], 1.23)
        self.assertEqual(entry[(FALLBACK, "droid", "glm-5-2", "")], 1.23)

    def test_droid_fast_suffix_and_hyphenated_gpt_ids(self):
        tokscale = doc((DAY, [row("droid", "claude-opus-5-5-fast", input=1_000_000, cost=0.1),
                              row("droid", "gpt-5-6-sol", output=1_000_000, cost=0.1)]))
        entry = day_entries(tokscale, {}, {})[DAY]
        self.assertAlmostEqual(entry[(VALUE, "droid", "claude-opus-5-5-fast", "")], 8.0)
        self.assertAlmostEqual(entry[(VALUE, "droid", "gpt-5-6-sol", "")], 20.0)
        self.assertNotIn((FALLBACK, "droid", "gpt-5-6-sol", ""), entry)

    def test_hermes_openrouter_and_malformed_rows_are_skipped(self):
        broken = {"client": "droid", "modelId": "glm-5-2"}  # no tokens, no cost
        tokscale = doc((DAY, [row("hermes", "qwen38-nvfp4", input=5, cost=1.0), broken,
                              {"client": "codex"}, "not-a-row"]))
        self.assertEqual(day_entries(tokscale, {}, {}), {})

    def test_since_date_drops_earlier_days(self):
        tokscale = doc(("2026-09-30", [row("droid", "glm-5-2", input=1, cost=1.0)]),
                       (DAY, [row("droid", "glm-5-2", input=1, cost=1.0)]))
        self.assertEqual(set(day_entries(tokscale, {}, {}, since_date="2026-10-01")), {DAY})


class LedgerTests(unittest.TestCase):
    def test_round_trip(self):
        days = {DAY: {(VALUE, "codex", "gpt-6-astra", ""): 1.5}}
        self.assertEqual(load_ledger(dump_ledger("2026-10-03", days)),
                         {"frozen_through": "2026-10-03", "days": days})

    def test_other_version_or_corrupt_ledger_starts_over(self):
        empty = {"frozen_through": None, "days": {}}
        good = dump_ledger("2026-10-03", {DAY: {(VALUE, "codex", "m", ""): 1.0}})
        for broken in (None, "x", {**good, "version": LEDGER_VERSION + 1},
                       {**good, "days": {DAY: [["only", "three", "items"]]}},
                       {**good, "frozen_through": 5}):
            self.assertEqual(load_ledger(broken), empty, broken)


class CumulativeSamplesTests(unittest.TestCase):
    def test_running_totals_with_day_end_and_today_stamps(self):
        now_ms = int(datetime(2026, 10, 3, 9, 30).timestamp() * 1000)
        key = (VALUE, "codex", "gpt-6-astra", "")
        samples = cumulative_samples({"2026-10-01": {key: 1.0}, "2026-10-03": {key: 2.0}}, now_ms)
        self.assertEqual([(s.ts_ms, s.value) for s in samples],
                         [(_end_of_day_local_ms("2026-10-01"), 1.0), (now_ms, 3.0)])
        self.assertEqual(samples[0].labels, {"provider": "codex", "model": "gpt-6-astra", "origin": "client"})


class UsageLaneTests(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 12, 0).timestamp()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "claude").mkdir()
        (root / "codex" / "sessions").mkdir(parents=True)
        self.root = root
        self.cfg = {"AIOBS_TOKSCALE_VERSION": "4.14.0", "AIOBS_CLAUDE_PROJECTS": str(root / "claude"),
                    "AIOBS_CODEX_HOME": str(root / "codex")}

    def collect(self, tokscale, state):
        lane = UsageLane()
        completed = SimpleNamespace(stdout=json.dumps(tokscale))
        clear_tokscale_graph_cache()
        with patch("aiobs_collector.lane_usage.time.time", return_value=self.NOW), \
                patch("aiobs_collector.lane_tokscale.subprocess.run", return_value=completed):
            samples = lane.collect(self.cfg, state)
        return lane, samples

    @staticmethod
    def value_at(samples, day):
        stamp = _end_of_day_local_ms(day)
        return [s.value for s in samples if s.metric == VALUE and s.ts_ms == stamp]

    def test_days_two_or_more_days_old_are_frozen_and_recent_days_recomputed(self):
        first = doc(("2026-10-01", [row("droid", "glm-5-2", input=1, cost=1.0)]),
                    ("2026-10-04", [row("droid", "glm-5-2", input=1, cost=2.0)]))
        lane, samples = self.collect(first, {})
        self.assertEqual(lane.state_data["frozen_through"], "2026-10-03")
        self.assertIn("2026-10-01", lane.state_data["days"])
        self.assertNotIn("2026-10-04", lane.state_data["days"])
        self.assertEqual(self.value_at(samples, "2026-10-01"), [1.0])

        # The sources change: the frozen day must not move, the live day must.
        second = doc(("2026-10-01", [row("droid", "glm-5-2", input=1, cost=50.0)]),
                     ("2026-10-04", [row("droid", "glm-5-2", input=1, cost=5.0)]))
        _lane, samples = self.collect(second, {"lane:usage:data": lane.state_data})
        self.assertEqual(self.value_at(samples, "2026-10-01"), [1.0])
        self.assertEqual(self.value_at(samples, "2026-10-04"), [6.0])

    def test_requires_a_pinned_tokscale_version(self):
        with self.assertRaises(RuntimeError):
            UsageLane().collect({}, {})

    def test_a_missing_transcript_folder_fails_instead_of_freezing(self):
        # A frozen day is never recomputed, so a wrong path must fail the run, not freeze empty days.
        self.cfg["AIOBS_CLAUDE_PROJECTS"] = str(self.root / "nowhere")
        with self.assertRaisesRegex(RuntimeError, "Claude Code transcripts"):
            self.collect(doc(), {})

    def test_missing_codex_session_logs_fail_the_run(self):
        self.cfg["AIOBS_CODEX_HOME"] = str(self.root / "nowhere")
        with self.assertRaisesRegex(RuntimeError, "Codex session logs"):
            self.collect(doc(), {})

    def test_a_day_tokscale_saw_but_transcripts_lack_is_not_frozen(self):
        tokscale = doc(("2026-10-01", [row("claude", "claude-opus-5-5", output=1_000, cost=1.0)]))
        lane = UsageLane()
        clear_tokscale_graph_cache()
        with self.assertRaisesRegex(RuntimeError, "2026-10-01"):
            with patch("aiobs_collector.lane_usage.time.time", return_value=self.NOW), \
                    patch("aiobs_collector.lane_tokscale.subprocess.run",
                          return_value=SimpleNamespace(stdout=json.dumps(tokscale))):
                lane.collect(self.cfg, {})
        self.assertIsNone(getattr(lane, "state_data", None))


class SharedTokscaleRunTests(unittest.TestCase):
    def test_the_tokscale_and_usage_lanes_share_one_tokscale_run(self):
        clear_tokscale_graph_cache()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "claude").mkdir()
            (Path(tmp) / "codex" / "sessions").mkdir(parents=True)
            cfg = {"AIOBS_TOKSCALE_VERSION": "4.14.0", "AIOBS_CLAUDE_PROJECTS": str(Path(tmp) / "claude"),
                   "AIOBS_CODEX_HOME": str(Path(tmp) / "codex")}
            completed = SimpleNamespace(stdout=json.dumps(doc((DAY, [row("droid", "glm-5-2", input=1, cost=1.0)]))))
            with patch("subprocess.run", return_value=completed) as run:
                TokscaleLane().collect(cfg, {})
                UsageLane().collect(cfg, {})
        self.assertEqual(run.call_count, 1)


class ModifiedSinceTests(unittest.TestCase):
    def test_keeps_rollouts_written_at_or_after_the_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sessions" / "2026" / "10"
            folder.mkdir(parents=True)
            old, new = folder / "old.jsonl", folder / "new.jsonl"
            for path in (old, new, folder / "notes.txt"):
                path.write_text("{}\n")
            cutoff = datetime(2026, 10, 1).timestamp()
            os.utime(old, (cutoff - 60, cutoff - 60))
            os.utime(new, (cutoff, cutoff))
            self.assertEqual([p.name for p in _modified_since(Path(tmp) / "sessions", cutoff)], ["new.jsonl"])
            self.assertEqual(sorted(p.name for p in _modified_since(Path(tmp) / "sessions", None)),
                             ["new.jsonl", "old.jsonl"])
            self.assertEqual(_modified_since(Path(tmp) / "missing", cutoff), [])

if __name__ == "__main__":
    unittest.main()
