"""Usage lane: API list-price value of every client's usage (spec sections 4.2-4.3).

Tokens: Claude Code from its own transcripts (cache writes split 5m / 1h,
fast mode kept apart); every other client from the pinned tokscale `graph`,
with output = output + reasoning (OpenAI bills reasoning as output). Codex
speed tiers come from the session logs joined to the diagnostic feedback
tags. Prices come from prices.py; a model with no official rate is valued at
tokscale's own cost for that day and counted again under FALLBACK, so the
dashboard can show how much of a period is estimated. hermes (mixed local
and cloud-routed traffic) and openrouter (its own lane, real spend) are
excluded.

Days at least FREEZE_AFTER_DAYS old are frozen in a ledger kept in lane
state (saved only after a successful push, like every lane's state), so
repricing, transcript clean-up, archived sessions or diagnostic-log rotation
can never move them again. Yesterday and today are recomputed every run.
"""

import json
import subprocess
import time
from collections import defaultdict
from datetime import date as Date, datetime, timedelta
from pathlib import Path

from aiobs_collector.claude_usage import parse_claude_usage, transcript_paths
from aiobs_collector.codex_usage import speed_tokens
from aiobs_collector.core import Sample
from aiobs_collector.lane_codex_speed import _cached_modes, read_modes
from aiobs_collector.lane_tokscale import _end_of_day_local_ms, _local_date_str, _map_provider
from aiobs_collector.prices import normalize_model, rate_for, value_usd

TOKENS = "aiobs_usage_tokens_total"
VALUE = "aiobs_list_value_usd_total"
FALLBACK = "aiobs_list_value_fallback_usd_total"
USAGE_METRICS = frozenset({TOKENS, VALUE, FALLBACK})
LEDGER_VERSION = 1
FREEZE_AFTER_DAYS = 2
EXCLUDED_PROVIDERS = frozenset({"hermes", "openrouter"})
STATE_KEY = "lane:usage:data"


def index_split(split: dict) -> dict:
    """speed_tokens() output -> {(date, model): {kind: {speed: tokens}}}."""
    index = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for (day, model, kind, speed), tokens in split.items():
        index[(day, model)][kind][speed] += tokens
    return index


def speed_shares(index: dict, day: str, model: str, kind: str) -> dict:
    """{speed: fraction} of `kind` tokens for (day, model); falls back to the
    model's all-kind split that day, then to 100% unknown (valued at Standard)."""
    by_kind = index.get((day, model)) or {}
    merged = defaultdict(float)
    for counts in by_kind.values():
        for speed, tokens in counts.items():
            merged[speed] += tokens
    for counts in (by_kind.get(kind) or {}, merged):
        total = sum(counts.values())
        if total > 0:
            return {speed: tokens / total for speed, tokens in counts.items()}
    return {"unknown": 1.0}


def _tokens(row: dict) -> dict:
    raw = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}

    def count(field):
        value = raw.get(field)
        return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0

    return {"input": count("input"), "output": count("output") + count("reasoning"),
            "cache_read": count("cacheRead"), "cache_write": count("cacheWrite")}


def day_entries(doc: dict, claude: dict, codex_split: dict, since_date=None) -> dict:
    """{local date: {(metric, provider, model, kind): value}} for dates >= since_date.

    `doc` is a tokscale `graph` document, `claude` parse_claude_usage() output
    and `codex_split` speed_tokens() output. kind is "" on the value metrics.
    """
    days = defaultdict(lambda: defaultdict(float))
    claude_fallback = set()
    for (day, model, speed), tokens in claude.items():
        if since_date is not None and day < since_date:
            continue
        entry = days[day]
        for kind, count in (("input", tokens["input"]), ("output", tokens["output"]),
                            ("cache_read", tokens["cache_read"]),
                            ("cache_write", tokens["cache_write_5m"] + tokens["cache_write_1h"])):
            entry[(TOKENS, "claude-code", model, kind)] += count
        rate = rate_for(model, day)
        if rate is None:
            claude_fallback.add((day, model))
        else:
            entry[(VALUE, "claude-code", model, "")] += value_usd(rate, tokens, speed)

    shares = index_split(codex_split)
    contributions = doc.get("contributions") if isinstance(doc, dict) else None
    for day_doc in contributions if isinstance(contributions, list) else []:
        day = day_doc.get("date") if isinstance(day_doc, dict) else None
        if not isinstance(day, str) or (since_date is not None and day < since_date):
            continue
        rows = day_doc.get("clients")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("client") or not row.get("modelId"):
                continue
            provider, model = _map_provider(row["client"]), row["modelId"]
            if provider in EXCLUDED_PROVIDERS:
                continue
            cost = row.get("cost")
            cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else 0.0
            entry = days[day]
            if provider == "claude-code":
                # Tokens come from the transcripts; tokscale only prices what the table cannot.
                if (day, model) in claude_fallback and cost:
                    entry[(VALUE, provider, model, "")] += cost
                    entry[(FALLBACK, provider, model, "")] += cost
                continue
            kinds = _tokens(row)
            for kind, count in kinds.items():
                entry[(TOKENS, provider, model, kind)] += count
            rate = rate_for(model, day)
            if rate is None:
                if cost:
                    entry[(VALUE, provider, model, "")] += cost
                    entry[(FALLBACK, provider, model, "")] += cost
                continue
            priced = {"input": kinds["input"], "output": kinds["output"],
                      "cache_read": kinds["cache_read"], "cache_write_5m": kinds["cache_write"]}
            if provider == "codex":
                value = 0.0
                for kind, count in priced.items():
                    split_kind = "input" if kind == "cache_write_5m" else kind
                    for speed, share in speed_shares(shares, day, model, split_kind).items():
                        value += value_usd(rate, {kind: count * share}, speed)
            else:
                value = value_usd(rate, priced, normalize_model(model)[1])
            entry[(VALUE, provider, model, "")] += value

    result = {}
    for day, entry in days.items():
        kept = {key: value for key, value in entry.items() if value}
        if kept:
            result[day] = kept
    return result


def load_ledger(data) -> dict:
    """The frozen days from lane state. Anything unreadable, or another
    LEDGER_VERSION, starts over (a rebuild from the sources)."""
    empty = {"frozen_through": None, "days": {}}
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return empty
    through, raw_days = data.get("frozen_through"), data.get("days")
    if not isinstance(through, str) or not isinstance(raw_days, dict):
        return empty
    days = {}
    for day, rows in raw_days.items():
        if not isinstance(day, str) or not isinstance(rows, list):
            return empty
        entry = {}
        for row in rows:
            if (not isinstance(row, list) or len(row) != 5 or not all(isinstance(x, str) for x in row[:4])
                    or isinstance(row[4], bool) or not isinstance(row[4], (int, float))):
                return empty
            entry[tuple(row[:4])] = float(row[4])
        days[day] = entry
    return {"frozen_through": through, "days": days}


def dump_ledger(frozen_through, days: dict) -> dict:
    return {"version": LEDGER_VERSION, "frozen_through": frozen_through,
            "days": {day: [[*key, value] for key, value in sorted(entry.items())]
                     for day, entry in sorted(days.items())}}


def cumulative_samples(days: dict, now_ms: int) -> list:
    """Sparse cumulative Samples in the tokscale lane's shape: a series gets a
    point only on days it moves, stamped 23:59:59.999 local (today: now_ms)."""
    today = _local_date_str(now_ms)
    running = defaultdict(float)
    samples = []
    for day in sorted(days):
        if day > today:
            continue
        ts_ms = now_ms if day == today else _end_of_day_local_ms(day)
        for key, value in sorted(days[day].items()):
            if not value:
                continue
            running[key] += value
            metric, provider, model, kind = key
            labels = {"provider": provider, "model": model, "origin": "client"}
            if kind:
                labels["kind"] = kind
            samples.append(Sample(metric, labels, running[key], ts_ms))
    return samples


def _modified_since(folder: Path, since_ts):
    if not folder.is_dir():
        return []
    paths = []
    for path in folder.rglob("*.jsonl"):
        try:
            if since_ts is None or path.stat().st_mtime >= since_ts:
                paths.append(path)
        except OSError:
            continue
    return paths


class UsageLane:
    """Lane: tokens and API list-price value per (provider, model), frozen by day."""

    name = "usage"

    def collect(self, cfg: dict, state: dict) -> list:
        version = (cfg.get("AIOBS_TOKSCALE_VERSION") or "").strip()
        if not version:
            raise RuntimeError("AIOBS_TOKSCALE_VERSION is not set -- pin it in config/estate.env")
        now_ms = int(time.time() * 1000)
        today = _local_date_str(now_ms)
        cutoff = (Date.fromisoformat(today) - timedelta(days=FREEZE_AFTER_DAYS)).isoformat()
        ledger = load_ledger(state.get(STATE_KEY))
        through = ledger["frozen_through"]
        since_date = None if through is None else (Date.fromisoformat(through) + timedelta(days=1)).isoformat()
        since_ts = None if since_date is None else datetime.fromisoformat(since_date).timestamp()

        result = subprocess.run(
            ["npx", "-y", f"tokscale@{version}", "graph", "--no-spinner"],
            capture_output=True, text=True, timeout=300, check=True,
        )
        doc = json.loads(result.stdout)
        claude_root = cfg.get("AIOBS_CLAUDE_PROJECTS") or "~/.claude/projects"
        claude = parse_claude_usage(transcript_paths(claude_root, since_ts), since_date)
        codex_root = Path(cfg.get("AIOBS_CODEX_HOME") or "~/.codex").expanduser()
        speed_state = state.get("lane:codex-speed:data")
        cached = speed_state.get("modes") if isinstance(speed_state, dict) else None
        database = codex_root / "logs_2.sqlite"
        modes = read_modes(database, cached) if database.exists() else _cached_modes(cached)
        rollouts = (_modified_since(codex_root / "sessions", since_ts)
                    + _modified_since(codex_root / "archived_sessions", since_ts))
        split = speed_tokens(rollouts, modes, since_date)

        fresh = day_entries(doc, claude, split, since_date)
        frozen = dict(ledger["days"])
        frozen.update({day: entry for day, entry in fresh.items() if day <= cutoff})
        live = {day: entry for day, entry in fresh.items() if day > cutoff}
        new_through = cutoff if through is None or cutoff > through else through
        self.state_data = dump_ledger(new_through, frozen)
        return cumulative_samples({**frozen, **live}, now_ms)
