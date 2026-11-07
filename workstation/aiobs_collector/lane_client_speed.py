"""Claude Code and Droid speed breakdown, kept separate from estate totals.

Claude speed comes from response usage; Droid speed comes from its model ID.
The dashboard scales these weights to the frozen usage-lane totals. Missing
history is shown as no tag, never silently relabelled as Standard. Values
are API equivalents, not Factory credits or subscription charges.
"""
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from aiobs_collector.claude_usage import KINDS, parse_claude_usage, transcript_paths
from aiobs_collector.core import Sample
from aiobs_collector.lane_tokscale import _end_of_day_local_ms, _local_date_str, tokscale_graph
from aiobs_collector.lane_usage import (
    FREEZE_AFTER_DAYS, VALUE as USAGE_VALUE, TOKENS as USAGE_TOKENS,
    check_claude_coverage, day_entries as usage_entries, dump_ledger, load_ledger,
)
from aiobs_collector.prices import normalize_model, rate_for, value_usd

VALUE = 'aiobs_client_speed_value_usd_total'
TOKENS = 'aiobs_client_speed_tokens_total'
METRICS = frozenset({VALUE, TOKENS})
STATE_KEY = 'lane:client-speed:data'


def day_entries(doc, claude, since_date=None):
    days = defaultdict(lambda: defaultdict(float))
    # Reuse the authoritative valuation for Droid, including fallback and
    # long-context estimates. Keep the original model ID for exact joins.
    totals = usage_entries(doc, {}, {}, since_date)
    for day, entries in totals.items():
        for (metric, provider, model, _kind), value in entries.items():
            if provider == 'droid' and metric in (USAGE_VALUE, USAGE_TOKENS):
                speed = normalize_model(model)[1]
                target = VALUE if metric == USAGE_VALUE else TOKENS
                days[day][(target, provider, model, speed)] += value

    unpriced = defaultdict(dict)
    for (day, model, speed), tokens in claude.items():
        if since_date is not None and day < since_date:
            continue
        count = sum(tokens.get(k, 0) for k in KINDS)
        days[day][(TOKENS, 'claude-code', model, speed)] += count
        rate = rate_for(model, day)
        if rate is not None:
            days[day][(VALUE, 'claude-code', model, speed)] += value_usd(rate, tokens, speed)
        else:
            unpriced[(day, model)][speed] = count
    # Unknown-model cost is only available as a daily aggregate. Use token
    # weights for its visual split; its Models pricing remains an estimate.
    for day_doc in doc.get('contributions', []):
        day = day_doc.get('date')
        for row in day_doc.get('clients', []):
            weights = unpriced.get((day, row.get('modelId')), {})
            cost = row.get('cost')
            if (row.get('client') == 'claude' and sum(weights.values()) > 0
                    and isinstance(cost, (int, float)) and cost > 0):
                for speed, count in weights.items():
                    days[day][(VALUE, 'claude-code', row['modelId'], speed)] += cost * count / sum(weights.values())
    return {day: {k:v for k,v in entries.items() if v} for day,entries in days.items()}


def cumulative_samples(days, now_ms):
    running = defaultdict(float)
    samples = []
    today = _local_date_str(now_ms)
    for day, entries in sorted(days.items()):
        if day > today:
            continue
        for key, value in sorted(entries.items()):
            running[key] += value
            metric, provider, model, speed = key
            samples.append(Sample(metric, dict(provider=provider, model=model, speed=speed, origin='client'),
                                  running[key], now_ms if day == today else _end_of_day_local_ms(day)))
    for (metric, provider, model, speed), value in running.items():
        samples.append(Sample(metric, dict(provider=provider, model=model, speed=speed, origin='client'), value, now_ms))
    return samples


class ClientSpeedLane:
    name = 'client-speed'

    def collect(self, cfg, state):
        now_ms = int(time.time() * 1000)
        cutoff = (date.fromisoformat(_local_date_str(now_ms)) - timedelta(days=FREEZE_AFTER_DAYS)).isoformat()
        ledger = load_ledger(state.get(STATE_KEY))
        through = ledger['frozen_through']
        since = None if through is None else (date.fromisoformat(through) + timedelta(days=1)).isoformat()
        since_ts = None if since is None else datetime.fromisoformat(since).timestamp()
        roots = [Path(p.strip()).expanduser() for p in
                 cfg.get('AIOBS_CLAUDE_PROJECTS', '~/.claude/projects').split(',') if p.strip()]
        if not roots or any(not p.is_dir() for p in roots):
            raise RuntimeError('Claude Code transcripts unavailable for client-speed')
        doc = tokscale_graph(cfg['AIOBS_TOKSCALE_VERSION'])
        claude = parse_claude_usage([p for root in roots for p in transcript_paths(root, since_ts)], since)
        check_claude_coverage(doc, claude, since, cutoff)
        fresh = day_entries(doc, claude, since)
        frozen = dict(ledger['days'])
        frozen.update({d:e for d,e in fresh.items() if d <= cutoff})
        live = {d:e for d,e in fresh.items() if d > cutoff}
        # Same four-field ledger row schema as usage; its final field is
        # speed here instead of token kind, under this lane's own state key.
        self.state_data = dump_ledger(max(cutoff, through or cutoff), frozen)
        return cumulative_samples({**frozen, **live}, now_ms)
