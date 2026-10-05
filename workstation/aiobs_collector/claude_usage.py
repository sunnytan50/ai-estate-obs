"""Claude Code usage read straight from its transcripts (spec section 4.2).

Each API response is logged on several lines (one per content block), each
repeating the same `message.usage`, so requests are deduplicated by
(message.id, requestId) -- the line's uuid stands in for a missing message
id. Only ids, the timestamp, the model and the usage block are used;
nothing from message content is kept or exported.
"""

import json
import os
import re
from datetime import datetime

KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
_DATE_SUFFIX = re.compile(r"-\d{8}$")  # "claude-sonnet-4-5-20250929" -> tokscale's "claude-sonnet-4-5"


def transcript_paths(root, since_ts=None):
    """Every *.jsonl under `root` (subagent transcripts included), optionally
    only files modified at/after `since_ts` (epoch seconds): a file last
    written before a day began cannot hold that day's requests."""
    for dirpath, _dirs, names in os.walk(os.path.expanduser(str(root))):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            if since_ts is not None:
                try:
                    if os.path.getmtime(path) < since_ts:
                        continue
                except OSError:
                    continue
            yield path


def _count(value) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _local_date(stamp: str) -> str:
    instant = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    return datetime.fromtimestamp(instant).date().isoformat()


def parse_claude_usage(paths, since_date=None) -> dict:
    """-> {(local date, model, speed): {kind: tokens}} for kinds in KINDS.

    speed is "fast" when usage.speed == "fast", else "standard". Cache writes
    are split by TTL from usage.cache_creation; a record without the split
    counts its writes as 5-minute ones. Dated model ids lose their date
    suffix, matching tokscale's names.
    """
    requests = {}
    for path in paths:
        try:
            handle = open(path, "rb")
        except OSError:
            continue
        with handle:
            for raw in handle:
                if b'"usage"' not in raw or b'"assistant"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue  # an actively appended final line can be partial
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage, model, stamp = message.get("usage"), message.get("model"), record.get("timestamp")
                if (not isinstance(usage, dict) or not isinstance(model, str) or model == "<synthetic>"
                        or not isinstance(stamp, str)):
                    continue
                key = (message.get("id") or record.get("uuid"), record.get("requestId"))
                if key == (None, None):
                    continue
                requests[key] = (stamp, model, usage)

    totals = {}
    for stamp, model, usage in requests.values():
        try:
            date = _local_date(stamp)
        except ValueError:
            continue
        if since_date is not None and date < since_date:
            continue
        written = _count(usage.get("cache_creation_input_tokens"))
        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        one_hour = min(_count(split.get("ephemeral_1h_input_tokens")), written)
        speed = "fast" if usage.get("speed") == "fast" else "standard"
        bucket = totals.setdefault((date, _DATE_SUFFIX.sub("", model), speed), dict.fromkeys(KINDS, 0))
        bucket["input"] += _count(usage.get("input_tokens"))
        bucket["output"] += _count(usage.get("output_tokens"))
        bucket["cache_read"] += _count(usage.get("cache_read_input_tokens"))
        bucket["cache_write_5m"] += written - one_hour
        bucket["cache_write_1h"] += one_hour
    return totals
