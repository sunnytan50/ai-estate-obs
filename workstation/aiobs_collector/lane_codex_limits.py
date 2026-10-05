"""Codex plan limits from the session logs (spec section 4.4) -- metadata only.

Every Codex `token_count` event carries OpenAI's own `rate_limits`: how much
of each window (weekly, 5-hour, ...) is used, when it resets, and the
purchased-credit balance. The newest event is the current state; earlier
events, one per 10 minutes, give the dashboard its history. Only these
fields are read; nothing from prompts or responses is decoded or kept.
"""

import json
import time
from pathlib import Path

from aiobs_collector.core import Sample
from aiobs_collector.lane_codex_speed import _timestamp

USED = "aiobs_codex_limit_used_ratio"
RESETS = "aiobs_codex_limit_resets_at_seconds"
CREDITS = "aiobs_codex_credits_balance"
OBSERVED = "aiobs_codex_limit_observed_at_seconds"
LIMIT_METRICS = frozenset({USED, RESETS, CREDITS, OBSERVED})
BACKFILL_DAYS = 7
RECENT_HOURS = 24
BUCKET_MS = 600_000
STATE_KEY = "lane:codex-limits:data"


def window_label(minutes: int) -> str:
    return {10080: "weekly", 1440: "daily", 300: "5h"}.get(minutes, f"{minutes}m")


def read_limit_events(paths, since_ms: int) -> list:
    """[(ts_ms, rate_limits)] from token_count events at/after since_ms, oldest first."""
    events = []
    for path in paths:
        try:
            # errors="replace": a final line still being written can end mid-character.
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"rate_limits"' not in line or '"token_count"' not in line:
                    continue
                try:
                    record = json.loads(line)
                    payload = record["payload"]
                    limits = payload["rate_limits"]
                    ts_ms = int(_timestamp(record["timestamp"]) * 1000)
                except (ValueError, KeyError, TypeError):
                    continue
                if payload.get("type") == "token_count" and isinstance(limits, dict) and ts_ms >= since_ms:
                    events.append((ts_ms, limits))
    events.sort(key=lambda item: item[0])
    return events


def _windows(limits: dict):
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if (isinstance(window, dict) and isinstance(window.get("used_percent"), (int, float))
                and isinstance(window.get("window_minutes"), int)):
            yield window_label(window["window_minutes"]), window


def _balance(limits: dict):
    credits = limits.get("credits")
    if not isinstance(credits, dict) or not credits.get("has_credits") or credits.get("unlimited"):
        return None
    try:
        value = float(credits.get("balance"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None  # NaN fails the comparison too


def limit_samples(events: list, latest, now_ms: int) -> list:
    """History (the last event per 10 minutes) plus the current gauges from
    `latest` ((ts_ms, rate_limits) or None). A window whose reset time has
    passed reads 0 and has no reset time until Codex is used again."""
    samples = []
    buckets = {}
    for ts_ms, limits in events:
        buckets[ts_ms // BUCKET_MS] = (ts_ms, limits)
    for ts_ms, limits in sorted(buckets.values(), key=lambda item: item[0]):
        for label, window in _windows(limits):
            samples.append(Sample(USED, {"window": label}, window["used_percent"] / 100.0, ts_ms))
        balance = _balance(limits)
        if balance is not None:
            samples.append(Sample(CREDITS, {}, balance, ts_ms))
    if latest is None:
        return samples
    ts_ms, limits = latest
    for label, window in _windows(limits):
        resets = window.get("resets_at")
        rolled = isinstance(resets, (int, float)) and resets * 1000 <= now_ms
        samples.append(Sample(USED, {"window": label}, 0.0 if rolled else window["used_percent"] / 100.0, now_ms))
        if isinstance(resets, (int, float)) and not rolled:
            samples.append(Sample(RESETS, {"window": label}, float(resets), now_ms))
    balance = _balance(limits)
    if balance is not None:
        samples.append(Sample(CREDITS, {}, balance, now_ms))
    samples.append(Sample(OBSERVED, {}, ts_ms / 1000.0, now_ms))
    return samples


class CodexLimitsLane:
    """Lane: OpenAI's own Codex limit and credit numbers, live plus history."""

    name = "codex-limits"

    def collect(self, cfg: dict, state: dict) -> list:
        root = Path(cfg.get("AIOBS_CODEX_HOME") or "~/.codex").expanduser()
        now_ms = int(time.time() * 1000)
        prior = state.get(STATE_KEY)
        prior = prior if isinstance(prior, dict) else {}
        window_ms = (RECENT_HOURS * 3600 if prior.get("backfilled") else BACKFILL_DAYS * 86400) * 1000
        since_ms = now_ms - window_ms
        paths = []
        for folder in ("sessions", "archived_sessions"):
            if not (root / folder).is_dir():
                continue
            for path in (root / folder).rglob("*.jsonl"):
                try:
                    if path.stat().st_mtime * 1000 >= since_ms:
                        paths.append(path)
                except OSError:
                    continue
        events = read_limit_events(paths, since_ms)
        latest = events[-1] if events else None
        last = prior.get("last")
        if (isinstance(last, list) and len(last) == 2 and isinstance(last[0], int)
                and isinstance(last[1], dict) and (latest is None or last[0] > latest[0])):
            latest = (last[0], last[1])
        self.state_data = {"backfilled": True, "last": list(latest) if latest else None}
        return limit_samples(events, latest, now_ms)
