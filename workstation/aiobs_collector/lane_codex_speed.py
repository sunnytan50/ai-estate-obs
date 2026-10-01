"""Codex speed telemetry from local metadata, never conversation content.

The contribution graph loses service tier. Join the local diagnostic log's
per-request feedback tags to rollout turn IDs and timestamps instead. Count
changes in cumulative usage, not repeated last_token_usage notifications.
Unknown mode/authentication stays unknown; it never receives a multiplier.

Allowance units are an estimate weighted by Standard credit token rates,
not OpenAI's actual quota percentage or a USD invoice. Rates verified against
https://learn.chatgpt.com/docs/pricing and /docs/agent-configuration/speed on
2026-10-01. Astra: 250 / 25 / 1250 credits per million uncached input / cached
input / output. Included allowance: Standard 1, Fast 2.5, Ultrafast 8;
purchased credits: 1, 2, 6. API-key requests are excluded from both estimates.
"""

import json
import hashlib
import math
from contextlib import closing
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from aiobs_collector.core import Sample
from aiobs_collector.lane_tokscale import _end_of_day_local_ms, _local_date_str

TOKENS = "aiobs_codex_speed_tokens_total"
ALLOWANCE = "aiobs_codex_allowance_estimate_total"
CREDITS = "aiobs_codex_purchased_credits_estimate_total"
INCLUDED_MULTIPLIERS = {"standard": 1.0, "fast": 2.5, "ultrafast": 8.0}
CREDIT_MULTIPLIERS = {"standard": 1.0, "fast": 2.0, "ultrafast": 6.0}
ASTRA_RATES = {"input": 250e-6, "cache_read": 25e-6, "output": 1250e-6}
METADATA_GRACE_MS = 120_000
RETENTION_MS = 400 * 86400 * 1000
_REQUEST = re.compile(r"run_sampling_request\{turn_id=([^\s}]+) model=([^\s}]+)")
_AUTH = re.compile(r"auth_mode=([^ ]+)")
_SPEED = {"default": "standard", "unset": "standard", "priority": "fast",
          "fast": "fast", "ultrafast": "ultrafast"}


def _cached_modes(cached) -> dict:
    """Ignore malformed cache entries without losing valid metadata."""
    modes = {}
    for key, rows in (cached.items() if isinstance(cached, dict) else []):
        if not isinstance(key, str) or not isinstance(rows, list):
            continue
        valid = [list(row) for row in rows
                 if isinstance(row, (list, tuple)) and len(row) == 4
                 and isinstance(row[0], (int, float)) and math.isfinite(row[0])
                 and all(isinstance(x, str) for x in row[1:])]
        if valid:
            modes[key] = valid
    return modes


def read_modes(db_path: Path, cached: dict) -> dict:
    """Retain tiers after log rotation; never cache/export diagnostic bodies."""
    modes = _cached_modes(cached)
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT ts, ts_nanos, thread_id, feedback_log_body FROM logs "
            "WHERE target='feedback_tags' AND module_path='codex_core::session::turn' "
            "ORDER BY ts, ts_nanos, id"
        )
        for ts, nanos, thread, body in rows:
            if not isinstance(body, str):
                continue
            request = _REQUEST.search(body)
            pos = body.find("tags_json=")
            if not request or not thread or pos < 0:
                continue
            try:
                tags = json.JSONDecoder().raw_decode(body[pos + len("tags_json="):])[0]
            except ValueError:
                continue
            if not isinstance(tags, dict):
                continue
            auth = _AUTH.search(body)
            billing = {"Some(Chatgpt)": "subscription", "Some(ApiKey)": "api"}.get(
                auth[1] if auth else "", "unknown"
            )
            try:
                timestamp = float(ts) + float(nanos or 0) / 1e9
            except (TypeError, ValueError):
                continue
            if not math.isfinite(timestamp):
                continue
            tier = tags.get("service_tier")
            speed = _SPEED.get(tier, "unknown") if isinstance(tier, str) else "unknown"
            row = [timestamp, request[2], speed, billing]
            modes.setdefault(f"{thread}/{request[1]}", []).append(row)

    # Repeated sampling requests under one configuration need one boundary.
    result = {}
    for key, rows in modes.items():
        compact = []
        for row in sorted({tuple(row) for row in rows}):
            if not compact or tuple(compact[-1][1:]) != row[1:]:
                compact.append(list(row))
        result[key] = compact
    return result


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def normalize_rollouts(paths, modes: dict, now_ms: int, *, attributions=None,
                       historical_modes=None, historical_cutoff_ms=0) -> list[Sample]:
    """Freeze emitted classifications so late diagnostics cannot move counters.

    The local attribution cache is saved only after a successful push. Recent
    unknown events wait for diagnostic flushing; late evidence never upgrades
    already emitted unknown usage retroactively.
    """
    if attributions is None:
        attributions = {}
    daily = defaultdict(float)
    seen = set()
    now_s = now_ms / 1000
    for path in sorted(paths):
        thread = turn = model = None
        previous = None
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                # Skip prompt/response records before decoding them.
                if not any(marker in line for marker in
                           ('"session_meta"', '"turn_context"', '"token_count"')):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # an actively appended final line can be partial
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                typ = record.get("type")
                if typ == "session_meta":
                    thread = payload.get("id")
                elif typ == "turn_context":
                    turn, model = payload.get("turn_id"), payload.get("model")
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(total, dict):
                        continue
                    try:
                        current = tuple(int(total.get(k) or 0) for k in
                                        ("input_tokens", "cached_input_tokens", "output_tokens"))
                        timestamp = _timestamp(record["timestamp"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if min(current) < 0 or timestamp > now_s:
                        continue
                    # A context reset restarts the cumulative tally. Cached
                    # input is a subset of input, and reasoning is in output.
                    reset = previous is None or current[0] + current[2] < previous[0] + previous[2]
                    delta = current if reset else tuple(max(0, a - b) for a, b in zip(current, previous))
                    previous = current
                    fingerprint = (turn, record["timestamp"], current)
                    if not any(delta) or fingerprint in seen:
                        continue
                    seen.add(fingerprint)  # inherited fork history counts once
                    if model != "gpt-6-astra" or timestamp * 1000 < now_ms - RETENTION_MS:
                        continue
                    speed = billing = "unknown"
                    # Migrate v1 using its old metadata snapshot, preserving
                    # the labels previously emitted before diagnostic backfill.
                    selected_modes = (historical_modes if historical_modes is not None
                                      and timestamp * 1000 <= historical_cutoff_ms else modes)
                    candidates = selected_modes.get(f"{thread}/{turn}", [])
                    for boundary in candidates:
                        if boundary[0] > timestamp:
                            break
                        # Don't reuse an older tier when the model changes.
                        speed, billing = (boundary[2], boundary[3]) if boundary[1] == model else ("unknown", "unknown")
                    event_key = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
                    frozen = attributions.get(event_key)
                    if frozen is not None:
                        speed, billing = frozen[1:]
                    else:
                        if ((speed == "unknown" or billing == "unknown")
                                and timestamp * 1000 > now_ms - METADATA_GRACE_MS):
                            continue
                        attributions[event_key] = [timestamp, speed, billing]
                    cached_input = min(delta[0], delta[1])
                    values = {"input": delta[0] - cached_input,
                              "cache_read": cached_input, "output": delta[2]}
                    date = datetime.fromtimestamp(timestamp).date().isoformat()
                    for kind, value in values.items():
                        if value:
                            daily[(date, model, speed, billing, kind)] += value

    # Convert daily increments into the same sparse cumulative shape as the
    # other lanes. Emit live carries too, so retention/drop shaping has a
    # current endpoint even if a model has not been used today.
    increments = defaultdict(float)
    for (date, model, speed, billing, kind), value in daily.items():
        labels = {"provider": "codex", "model": model, "origin": "client",
                  "speed": speed, "billing": billing}
        increments[(date, TOKENS, tuple(sorted({**labels, "kind": kind}.items())))] += value
        if billing == "subscription" and speed in INCLUDED_MULTIPLIERS:
            base = value * ASTRA_RATES[kind]
            increments[(date, ALLOWANCE, tuple(sorted(labels.items())))] += base * INCLUDED_MULTIPLIERS[speed]
            increments[(date, CREDITS, tuple(sorted(labels.items())))] += base * CREDIT_MULTIPLIERS[speed]
    running = defaultdict(float)
    samples = []
    today = _local_date_str(now_ms)
    for (date, metric, labels), value in sorted(increments.items()):
        running[(metric, labels)] += value
        samples.append(Sample(metric, dict(labels), running[(metric, labels)],
                              now_ms if date == today else _end_of_day_local_ms(date)))
    for (metric, labels), value in running.items():
        samples.append(Sample(metric, dict(labels), value, now_ms))
    return samples


class CodexSpeedLane:
    name = "codex-speed"

    def collect(self, cfg: dict, state: dict) -> list[Sample]:
        root = Path(cfg.get("AIOBS_CODEX_HOME") or "~/.codex").expanduser()
        cutoff_ms = int(time.time() * 1000)
        prior = state.get("lane:codex-speed:data")
        prior = prior if isinstance(prior, dict) else {}
        old_modes = prior.get("modes")
        modes = read_modes(root / "logs_2.sqlite", old_modes)
        raw_attributions = prior.get("attributions")
        attributions = {}
        for key, row in (raw_attributions.items() if isinstance(raw_attributions, dict) else []):
            if (isinstance(key, str) and len(key) == 64
                    and isinstance(row, list) and len(row) == 3
                    and isinstance(row[0], (int, float)) and math.isfinite(row[0])
                    and row[0] * 1000 >= cutoff_ms - RETENTION_MS
                    and isinstance(row[1], str) and isinstance(row[2], str)
                    and row[1] in {"unknown", "standard", "fast", "ultrafast"}
                    and row[2] in {"unknown", "subscription", "api"}):
                attributions[key] = list(row)
        if not (root / "sessions").is_dir():
            raise RuntimeError("Codex session metadata directory is unavailable")
        samples = normalize_rollouts(
            (root / "sessions").rglob("*.jsonl"), modes, cutoff_ms,
            attributions=attributions,
            historical_modes=_cached_modes(old_modes) if "attributions" not in prior else None,
            historical_cutoff_ms=state.get("lane:codex-speed:last_success_ms", 0),
        )
        # Preserve model-switch boundaries in Astra turns; omit unrelated turns.
        retained_modes = {key: rows for key, rows in modes.items()
                          if any(r[1] == "gpt-6-astra" for r in rows)
                          and rows[-1][0] * 1000 >= cutoff_ms - RETENTION_MS}
        self.state_data = {"modes": retained_modes, "attributions": attributions}
        return samples
