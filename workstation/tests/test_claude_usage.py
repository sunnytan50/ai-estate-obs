import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aiobs_collector.claude_usage import parse_claude_usage, transcript_paths

USAGE = {
    "input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 300,
    "cache_creation": {"ephemeral_1h_input_tokens": 200, "ephemeral_5m_input_tokens": 100},
    "speed": "standard",
}
NOON = "2026-10-02T12:00:00Z"


def assistant(msg_id="msg_1", req="req_1", model="claude-opus-5-5", when=NOON, usage=None, uuid="u-1"):
    return {"type": "assistant", "uuid": uuid, "requestId": req, "timestamp": when,
            "message": {"id": msg_id, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "hi"}], "usage": dict(usage or USAGE)}}


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, records, name="session.jsonl", raw_lines=()):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records] + list(raw_lines)
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def parse(self, **kw):
        return parse_claude_usage(transcript_paths(self.root), **kw)

    def test_streamed_lines_of_one_request_count_once(self):
        self.write([assistant(), assistant(), assistant()])
        self.assertEqual(self.parse()[("2026-10-02", "claude-opus-5-5", "standard")],
                         {"input": 2, "output": 100, "cache_read": 1000, "cache_write_5m": 100, "cache_write_1h": 200})

    def test_record_without_ttl_split_counts_writes_as_five_minute(self):
        usage = {k: v for k, v in USAGE.items() if k != "cache_creation"}
        self.write([assistant(usage=usage)])
        bucket = self.parse()[("2026-10-02", "claude-opus-5-5", "standard")]
        self.assertEqual((bucket["cache_write_5m"], bucket["cache_write_1h"]), (300, 0))

    def test_fast_mode_is_kept_apart(self):
        self.write([assistant(), assistant(msg_id="msg_2", req="req_2", usage={**USAGE, "speed": "fast"})])
        totals = self.parse()
        self.assertIn(("2026-10-02", "claude-opus-5-5", "fast"), totals)
        self.assertIn(("2026-10-02", "claude-opus-5-5", "standard"), totals)

    def test_subagent_transcripts_are_read(self):
        self.write([assistant(msg_id="msg_9", req="req_9")], name="proj/session-id/subagents/agent-1.jsonl")
        self.write([assistant()], name="proj/session-id.jsonl")
        self.assertEqual(self.parse()[("2026-10-02", "claude-opus-5-5", "standard")]["output"], 200)

    def test_skips_synthetic_non_assistant_and_partial_lines(self):
        user = {"type": "user", "message": {"usage": USAGE}, "timestamp": NOON}
        self.write([assistant(model="<synthetic>"), user], raw_lines=['{"type": "assistant", "message": {"usage"'])
        self.assertEqual(self.parse(), {})

    def test_falls_back_to_the_line_uuid_when_message_id_is_missing(self):
        first, second = assistant(uuid="u-1", req=None), assistant(uuid="u-2", req=None)
        for record in (first, second):
            del record["message"]["id"]
        self.write([first, second])
        self.assertEqual(self.parse()[("2026-10-02", "claude-opus-5-5", "standard")]["output"], 200)

    def test_requests_land_on_their_local_day(self):
        late = datetime(2026, 10, 1, 23, 59, 59).astimezone().isoformat()
        early = datetime(2026, 10, 2, 0, 0, 1).astimezone().isoformat()
        self.write([assistant(msg_id="a", req="a", when=late), assistant(msg_id="b", req="b", when=early)])
        self.assertEqual({key[0] for key in self.parse()}, {"2026-10-01", "2026-10-02"})

    def test_dated_model_ids_use_the_undated_name(self):
        # Transcripts log "claude-sonnet-4-5-20250929"; tokscale and the dashboard use "claude-sonnet-4-5".
        self.write([assistant(model="claude-sonnet-4-5-20250929"),
                    assistant(msg_id="msg_2", req="req_2", model="claude-sonnet-4-5")])
        self.assertEqual(set(self.parse()), {("2026-10-02", "claude-sonnet-4-5", "standard")})

    def test_since_date_and_file_mtime_filters(self):
        old = self.write([assistant(msg_id="old", req="old", when="2026-09-20T12:00:00Z")], name="old.jsonl")
        self.write([assistant()], name="new.jsonl")
        stamp = datetime(2026, 9, 21).timestamp()
        os.utime(old, (stamp, stamp))
        since_ts = datetime(2026, 10, 1).timestamp()
        self.assertEqual([os.path.basename(p) for p in transcript_paths(self.root, since_ts)], ["new.jsonl"])
        self.assertEqual(set(parse_claude_usage(transcript_paths(self.root), since_date="2026-10-01")),
                         {("2026-10-02", "claude-opus-5-5", "standard")})


if __name__ == "__main__":
    unittest.main()
