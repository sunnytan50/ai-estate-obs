"""Collector core: sample schema, Prometheus exposition, config/state I/O, lane harness.

Stdlib only. These are the frozen interfaces Tasks 9-11 build on:
Sample, render_exposition, load_config, load_state, save_state, Lane, run_lanes.
"""

import json
import os
import sys
import traceback
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Sample:
    """One timestamped metric point."""

    metric: str
    labels: dict[str, str]
    value: float
    ts_ms: int


def _escape_label_value(value: str) -> str:
    """Escape a label value per Prometheus text-exposition rules.

    Order matters: backslashes must be escaped first, or the backslashes
    introduced by the quote/newline escapes below would be re-escaped.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("\n", "\\n")
    return value


def _format_value(value: float) -> str:
    """Render a metric value: whole numbers print without a decimal point."""
    if value.is_integer():
        return str(int(value))
    return repr(value)


def render_exposition(samples: list[Sample]) -> str:
    """Render samples as Prometheus text exposition.

    One line per sample: `metric{k="v",...} value ts_ms`, with labels sorted
    alphabetically by key and label values escaped. A sample with no labels
    is rendered without braces. Lines are joined with "\\n"; there is no
    trailing newline.
    """
    lines = []
    for sample in samples:
        label_str = ",".join(
            f'{key}="{_escape_label_value(val)}"'
            for key, val in sorted(sample.labels.items())
        )
        name_part = f"{sample.metric}{{{label_str}}}" if label_str else sample.metric
        lines.append(f"{name_part} {_format_value(sample.value)} {sample.ts_ms}")
    return "\n".join(lines)


def _strip_inline_comment(value: str) -> str:
    """Strip a dotenv-style inline comment from a raw (unstripped) value.

    Unquoted: a `#` preceded by at least one whitespace character starts a
    comment -- everything from there (including that whitespace) is
    dropped. A `#` with no preceding whitespace (e.g. `abc#def`) is part of
    the value, not a comment.

    Quoted (single or double): everything inside the quotes is kept
    verbatim, `#` included; whatever follows the closing quote -- comment
    or not -- is discarded. Leading whitespace before the opening quote is
    preserved here and removed later by the caller's own `.strip()`.

    This runs on the raw partition() value (before whitespace-stripping),
    so a comment immediately after `=` (e.g. `KEY=   # note`) is still
    recognized -- the space right after `=` counts as the "preceding
    whitespace" the comment needs.
    """
    stripped_start = value.lstrip()
    leading_ws_len = len(value) - len(stripped_start)
    if stripped_start[:1] in ("'", '"'):
        quote_char = stripped_start[0]
        closing = stripped_start.find(quote_char, 1)
        if closing != -1:
            # Keep the leading whitespace + the quoted segment (with its
            # quotes); drop everything after the closing quote outright.
            return value[: leading_ws_len + closing + 1]
        # No closing quote -- malformed; fall through and treat as unquoted.
    for i in range(1, len(value)):
        if value[i] == "#" and value[i - 1].isspace():
            return value[:i].rstrip()
    return value


def load_config(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file (estate.env).

    Blank lines and lines starting with `#` are ignored. A dotenv-style
    inline comment is stripped next (see `_strip_inline_comment`), *before*
    quote-stripping or `~`-expansion. Leading/trailing whitespace is
    stripped from both key and value. Surrounding quotes (single or
    double) are stripped from the value. A value with a leading `~` is
    expanded to the user's home directory. When a key appears more than
    once, the later occurrence wins.
    """
    result: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = _strip_inline_comment(value)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value.startswith("~"):
                value = os.path.expanduser(value)
            result[key] = value
    return result


_STATE_FILENAME = "collector-state.json"


def _state_path(state_dir: str) -> str:
    return os.path.join(state_dir, _STATE_FILENAME)


def load_state(state_dir: str) -> dict:
    """Load JSON state from `<state_dir>/collector-state.json`.

    Returns {} when the file is missing, unreadable, not valid JSON, or its
    top-level value isn't an object -- corrupt state must never take down
    the collector.
    """
    try:
        with open(_state_path(state_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(state_dir: str, state: dict) -> None:
    """Persist state as JSON in `<state_dir>/collector-state.json`.

    Creates the state dir (with parents) if needed and ensures it is mode
    0700, regardless of umask or pre-existing permissions.
    """
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    os.chmod(state_dir, 0o700)
    with open(_state_path(state_dir), "w", encoding="utf-8") as handle:
        json.dump(state, handle)


class Lane(Protocol):
    """A collector lane: a name plus a function producing samples."""

    name: str

    def collect(self, cfg: dict, state: dict) -> list[Sample]:
        ...


def run_lanes(
    lanes: list[Lane], cfg: dict, state: dict, now_ms: int
) -> tuple[list[Sample], dict]:
    """Run each lane's collect(cfg, state), isolating failures.

    Every lane receives the same original `state` (lanes never see each
    other's writes within one run). On success, the lane's samples are
    appended and state[f"lane:{name}:last_success_ms"] is set to now_ms. On
    failure -- any Exception, not BaseException, so Ctrl-C still works --
    the prior last_success (if any) is kept and the lane contributes no
    samples of its own. The failure is never silent: one line naming the
    lane and the exception type/message, plus the full traceback, is
    printed to stderr (never stdout -- stdout is reserved for `--dry-run`'s
    exposition and must stay parseable) so it reaches whatever log the
    launchd/cron/systemd caller redirects stderr to. No Sample, no state
    key, and no other return value carries this -- it is a side effect
    only, so this function's signature and return shape stay exactly as
    documented below.

    Every lane always gets an `aiobs_lane_up{lane=}` sample (1.0 on success,
    0.0 on failure). When a last_success exists (fresh or carried over from
    `state`), an `aiobs_lane_last_success_timestamp{lane=}` sample is also
    appended, valued in epoch seconds (last_success_ms / 1000.0).

    Returns (samples, new_state); the input `state` dict is never mutated.
    """
    new_state = dict(state)
    samples: list[Sample] = []

    for lane in lanes:
        key = f"lane:{lane.name}:last_success_ms"
        try:
            lane_samples = lane.collect(cfg, state)
            samples.extend(lane_samples)
            new_state[key] = now_ms
            up_value = 1.0
        except Exception as exc:
            print(
                f"aiobs_collector: lane {lane.name} failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc()
            up_value = 0.0

        samples.append(
            Sample(metric="aiobs_lane_up", labels={"lane": lane.name}, value=up_value, ts_ms=now_ms)
        )

        last_success_ms = new_state.get(key)
        if last_success_ms is not None:
            samples.append(
                Sample(
                    metric="aiobs_lane_last_success_timestamp",
                    labels={"lane": lane.name},
                    value=last_success_ms / 1000.0,
                    ts_ms=now_ms,
                )
            )

    return samples, new_state
