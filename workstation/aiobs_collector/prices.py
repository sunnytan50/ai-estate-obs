"""Owned API list-price table for the usage lane.

Spec: docs/superpowers/specs/2026-10-05-aiobs-list-value-readable-dashboard-design.md (section 4.1).

Rates are USD per million tokens, copied from the vendors' own pricing pages:

    Anthropic  https://platform.claude.com/docs/en/about-claude/pricing  (fetched 2026-10-05)
    OpenAI     https://developers.openai.com/api/docs/pricing            (fetched 2026-10-05)

A model with no entry here has no official rate: the caller values it some
other way (tokscale's own estimate) and reports that share separately.

Change rule: never edit an existing entry to correct the past. A future
price change is a NEW entry with a later `since` (days before it keep the
old price); correcting a past price is a deliberate rebuild under a new
metric version.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    """$/MTok for one model from `since` (a local YYYY-MM-DD date) onward."""

    input: float
    output: float  # reasoning tokens are billed as output
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    speed_multipliers: tuple = ()  # e.g. (("fast", 2.0), ("ultrafast", 6.0))
    since: str = "2000-01-01"

    def multiplier(self, speed: str) -> float:
        """Price multiplier for a speed tier; standard, unknown or unlisted -> 1.0."""
        return dict(self.speed_multipliers).get(speed, 1.0)


def _anthropic(inp, out, read=0.1, fast=None):
    # Cache writes: 1.25x input (5-minute TTL), 2x input (1-hour TTL).
    speeds = (("fast", fast),) if fast else ()
    return Rate(inp, out, inp * read, inp * 1.25, inp * 2.0, speeds)


def _openai(inp, cached, write, out, fast=None, ultrafast=None):
    speeds = tuple((name, m) for name, m in (("fast", fast), ("ultrafast", ultrafast)) if m)
    return Rate(inp, out, cached, write, write, speeds)


TABLE = {
    # Anthropic. Cache read 0.1x input, except Fable 5.1 / Mythos 5.1 (0.025x)
    # and Opus 5.5 (0.05x). Fast mode: Opus 5.5 $8/$40 and Opus 5 $10/$50,
    # i.e. 2x, with the cache multipliers on top. Claude 4.6+ models have no
    # long-context surcharge; no Sonnet 4.5 request here exceeded 200K input
    # (checked 2026-10-05), so its long-context rate is not modelled.
    "claude-fable-5-1": (_anthropic(10, 50, read=0.025),),
    "claude-mythos-5-1": (_anthropic(10, 50, read=0.025),),
    "claude-opus-5-5": (_anthropic(4, 20, read=0.05, fast=2.0),),
    "claude-sonnet-5-5": (_anthropic(2, 10),),
    "claude-haiku-4-5": (_anthropic(1, 5),),
    "claude-fable-5": (_anthropic(10, 50),),
    "claude-opus-5": (_anthropic(5, 25, fast=2.0),),
    "claude-sonnet-5": (_anthropic(2, 10),),
    "claude-sonnet-4-5": (_anthropic(3, 15),),
    "claude-haiku-3-5": (_anthropic(0.8, 4),),
    # OpenAI Standard tier: input / cached input / cache writes / output.
    # Fast = 2x every category; Ultrafast (Astra only) = 6x. Every Codex model
    # here runs with a 258,400-token window, so the >272K tier never applies.
    "gpt-6-astra": (_openai(10, 1, 12.5, 50, fast=2.0, ultrafast=6.0),),
    "gpt-6.1-sol": (_openai(2, 0.10, 2.5, 10, fast=2.0),),
    "gpt-6-luna": (_openai(0.10, 0.01, 0.125, 0.50, fast=2.0),),
    "gpt-5.6-sol": (_openai(4, 0.40, 5, 20),),
    "gpt-5.3-codex": (_openai(1.75, 0.175, 1.75, 14, fast=2.0),),
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_HYPHENATED_GPT = re.compile(r"^gpt-(\d+)-(\d+)(-[a-z].*)?$")


def normalize_model(model: str) -> tuple:
    """A client's model id -> (table key, speed implied by the id).

    'anthropic/' and 'openai/' prefixes and Anthropic date suffixes are
    dropped; Droid's hyphenated GPT ids become dotted ('gpt-5-6-sol' ->
    'gpt-5.6-sol'); a '-fast' suffix means fast mode on the base model.
    """
    key = model.strip().lower()
    for prefix in ("anthropic/", "openai/"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    speed = "standard"
    if key.endswith("-fast"):
        key, speed = key[: -len("-fast")], "fast"
    key = _DATE_SUFFIX.sub("", key)
    match = _HYPHENATED_GPT.match(key)
    if match:
        key = f"gpt-{match.group(1)}.{match.group(2)}{match.group(3) or ''}"
    return key, speed


def rate_for(model: str, day: str):
    """The Rate in force on local `day` (YYYY-MM-DD) for `model`, else None."""
    key, _speed = normalize_model(model)
    current = None
    for entry in TABLE.get(key, ()):
        if entry.since <= day:
            current = entry
    return current


def value_usd(rate: Rate, tokens: dict, speed: str = "standard") -> float:
    """List-price USD for token counts keyed input / output / cache_read /
    cache_write_5m / cache_write_1h (missing keys count as zero)."""
    per_million = (
        tokens.get("input", 0) * rate.input
        + tokens.get("output", 0) * rate.output
        + tokens.get("cache_read", 0) * rate.cache_read
        + tokens.get("cache_write_5m", 0) * rate.cache_write_5m
        + tokens.get("cache_write_1h", 0) * rate.cache_write_1h
    )
    return per_million * rate.multiplier(speed) / 1e6
