"""Per-day Codex speed split for every model (spec section 4.2) -- metadata only.

The same session-log walk as lane_codex_speed.normalize_rollouts (cumulative
total_token_usage deltas, context resets, fork-history fingerprints), joined
to the diagnostic feedback tags that lane_codex_speed.read_modes returns, but
kept for every model and returned as token counts per (local date, model,
kind, speed). The usage lane turns them into shares; unknown stays unknown.
"""

import json
from collections import defaultdict
from datetime import datetime

from aiobs_collector.lane_codex_speed import _timestamp

_MARKERS = ('"session_meta"', '"turn_context"', '"token_count"')


def speed_tokens(paths, modes: dict, since_date=None) -> dict:
    """-> {(local date, model, kind, speed): tokens}; kind is input (uncached),
    cache_read or output (reasoning included); speed is standard, fast,
    ultrafast or unknown."""
    split = defaultdict(float)
    seen = set()
    for path in sorted(paths):
        thread = turn = model = None
        previous = None
        try:
            # errors="replace": Codex appends while we read; a final line can end mid-character.
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not any(marker in line for marker in _MARKERS):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # an actively appended final line can be partial
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                typ = record.get("type")
                if typ == "session_meta":
                    thread = payload.get("id")
                elif typ == "turn_context":
                    turn, model = payload.get("turn_id"), payload.get("model")
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(total, dict):
                        continue
                    try:
                        current = tuple(int(total.get(k) or 0) for k in
                                        ("input_tokens", "cached_input_tokens", "output_tokens"))
                        timestamp = _timestamp(record["timestamp"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if min(current) < 0:
                        continue
                    # A context reset restarts the cumulative tally.
                    reset = previous is None or current[0] + current[2] < previous[0] + previous[2]
                    delta = current if reset else tuple(max(0, a - b) for a, b in zip(current, previous))
                    previous = current
                    fingerprint = (turn, record["timestamp"], current)
                    if not any(delta) or fingerprint in seen:
                        continue
                    seen.add(fingerprint)  # inherited fork history counts once
                    if not model:
                        continue
                    date = datetime.fromtimestamp(timestamp).date().isoformat()
                    if since_date is not None and date < since_date:
                        continue
                    speed = "unknown"
                    for boundary in modes.get(f"{thread}/{turn}", []):
                        if boundary[0] > timestamp:
                            break
                        # Never reuse an older tier after the model changes.
                        speed = boundary[2] if boundary[1] == model else "unknown"
                    cached = min(delta[0], delta[1])  # cached input is a subset of input
                    for kind, value in (("input", delta[0] - cached), ("cache_read", cached), ("output", delta[2])):
                        if value:
                            split[(date, model, kind, speed)] += value
    return dict(split)
