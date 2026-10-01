"""Runtime freshness and transport receipt for the AI Estate collector.

The receipt is deliberately separate from ``collector-state.json``.  Lane
state is a private implementation detail used to resume collection, whereas
this small JSON document is an operational hand-off that can be inspected by
governance checks without understanding the collector's counters.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECEIPT_FILENAME = "collector-receipt.json"
SCHEMA_VERSION = 1
STATES = frozenset({"HEALTHY", "STALE", "BLOCKED", "PAUSED", "RETIRED", "UNKNOWN"})


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _parse_iso(value: Any, field: str, *, allow_none: bool = True) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"receipt {field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"receipt {field} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"receipt {field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _state(value: Any, field: str) -> str:
    if not isinstance(value, str) or value.upper() not in STATES:
        raise ValueError(f"receipt {field} must be one of {sorted(STATES)}")
    return value.upper()


def _optional_ms(state: dict[str, Any], key: str) -> int | None:
    value = state.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def lane_outcomes(samples: list[Any], enabled_lanes: list[str]) -> tuple[list[str], list[str]]:
    """Return successful and failed lane names from run_lanes health samples."""
    outcomes: dict[str, bool] = {}
    for sample in samples:
        if getattr(sample, "metric", None) != "aiobs_lane_up":
            continue
        lane = getattr(sample, "labels", {}).get("lane")
        if lane in enabled_lanes:
            outcomes[lane] = bool(getattr(sample, "value", 0.0))
    successful = sorted(lane for lane in enabled_lanes if outcomes.get(lane) is True)
    failed = sorted(lane for lane in enabled_lanes if outcomes.get(lane) is not True)
    return successful, failed


def complete_lane_success(
    enabled_lanes: list[str],
    successful_lanes: list[str],
    requested_state: str | None = None,
) -> bool:
    """Whether every enabled lane completed in this collection cycle.

    A partial run is useful operational evidence, but it must not advance the
    receipt's collection ``last_success_at`` as if the complete data set had
    been refreshed.
    """
    enabled = set(enabled_lanes)
    successful = set(successful_lanes)
    requested = requested_state.upper() if isinstance(requested_state, str) else None
    return bool(enabled) and requested is None and successful == enabled


def _data_state(
    enabled_lanes: list[str],
    successful_lanes: list[str],
    requested_state: str | None,
) -> str:
    if requested_state in {"PAUSED", "RETIRED"}:
        return requested_state
    if not enabled_lanes:
        return "PAUSED"
    if len(successful_lanes) == len(enabled_lanes):
        return "HEALTHY"
    return "STALE"


def build_receipt(
    *,
    observed_ms: int,
    enabled_lanes: list[str],
    successful_lanes: list[str],
    failed_lanes: list[str],
    collected_count: int,
    pushed_count: int,
    transport_state: str,
    prior_state: dict[str, Any] | None = None,
    requested_state: str | None = None,
    transport_error: BaseException | str | None = None,
    backfill: bool = False,
) -> dict[str, Any]:
    """Build one receipt without exposing configuration or credential values."""
    prior = prior_state or {}
    transport = _state(transport_state, "transport.state")
    requested = requested_state.upper() if isinstance(requested_state, str) else None
    if requested not in {None, "PAUSED", "RETIRED"}:
        raise ValueError("requested collector state must be PAUSED or RETIRED")

    enabled = sorted(set(enabled_lanes))
    successful = sorted(set(successful_lanes) & set(enabled))
    failed = sorted((set(failed_lanes) | (set(enabled) - set(successful))) & set(enabled))
    data = _data_state(enabled, successful, requested)
    overall = data if transport != "BLOCKED" else "BLOCKED"
    observed_at = _iso_from_ms(observed_ms)
    complete_success = complete_lane_success(enabled, successful, requested)
    data_success_ms = (
        observed_ms if complete_success else _optional_ms(prior, "collector:last_success_ms")
    )
    transport_success_ms = (
        observed_ms if transport == "HEALTHY" else _optional_ms(prior, "transport:last_success_ms")
    )
    evidence: list[dict[str, Any]] = [
        {
            "kind": "collector_run",
            "observed_at": observed_at,
            "enabled_lanes": enabled,
            "successful_lanes": successful,
            "failed_lanes": failed,
            "samples_collected": max(0, int(collected_count)),
            "samples_pushed": max(0, int(pushed_count)),
            "backfill": bool(backfill),
        },
        {
            "kind": "transport",
            "observed_at": observed_at,
            "state": transport,
        },
    ]
    if transport_error:
        exception_class = (
            type(transport_error).__name__
            if isinstance(transport_error, BaseException)
            else "TransportError"
        )
        evidence[-1]["error"] = {
            "reason": "push_failed",
            "exception_class": exception_class,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "state": overall,
        "observed_at": observed_at,
        "last_success_at": _iso_from_ms(data_success_ms),
        "data": {
            "state": data,
            "observed_at": observed_at,
            "last_success_at": _iso_from_ms(data_success_ms),
            "successful_lanes": successful,
            "failed_lanes": failed,
            "freshness_basis": "collector_lane_observation",
        },
        "transport": {
            "state": transport,
            "observed_at": observed_at,
            "last_success_at": _iso_from_ms(transport_success_ms),
        },
        "evidence": evidence,
    }


def validate_receipt(receipt: Any) -> dict[str, Any]:
    """Validate and return a receipt suitable for a read-only governance check."""
    if not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported collector receipt")
    _state(receipt.get("state"), "state")
    _parse_iso(receipt.get("observed_at"), "observed_at", allow_none=False)
    _parse_iso(receipt.get("last_success_at"), "last_success_at")
    for section in ("data", "transport"):
        value = receipt.get(section)
        if not isinstance(value, dict):
            raise ValueError(f"receipt {section} must be an object")
        _state(value.get("state"), f"{section}.state")
        _parse_iso(value.get("observed_at"), f"{section}.observed_at", allow_none=False)
        _parse_iso(value.get("last_success_at"), f"{section}.last_success_at")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(item, dict) for item in evidence):
        raise ValueError("receipt evidence must be a non-empty list of objects")
    return receipt


def receipt_path(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / RECEIPT_FILENAME


def write_receipt(state_dir: str | os.PathLike[str], receipt: dict[str, Any]) -> None:
    """Atomically replace the receipt while leaving collector state untouched."""
    validate_receipt(receipt)
    target = receipt_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_receipt(
    state_dir: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
    max_age_ms: int | None = None,
) -> dict[str, Any]:
    """Read a receipt, returning an explicit UNKNOWN/STALE view on failure."""
    path = receipt_path(state_dir)
    try:
        with path.open(encoding="utf-8") as stream:
            receipt = validate_receipt(json.load(stream))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "UNKNOWN",
            "observed_at": None,
            "last_success_at": None,
            "data": {"state": "UNKNOWN", "observed_at": None, "last_success_at": None},
            "transport": {"state": "UNKNOWN", "observed_at": None, "last_success_at": None},
            "evidence": [{"kind": "receipt", "state": "UNKNOWN", "reason": str(exc)[:500]}],
        }
    if max_age_ms is None or now_ms is None:
        return receipt
    data = dict(receipt["data"])
    if data["state"] != "HEALTHY":
        return receipt
    observed = _parse_iso(data["observed_at"], "data.observed_at", allow_none=False)
    age_ms = now_ms - int(observed.timestamp() * 1000)
    if age_ms <= max_age_ms:
        return receipt
    stale = dict(receipt)
    data["state"] = "STALE"
    stale["data"] = data
    if receipt["state"] == "HEALTHY":
        stale["state"] = "STALE"
    stale["freshness"] = {
        "state": "STALE",
        "scope": "data",
        "age_ms": age_ms,
        "max_age_ms": max_age_ms,
    }
    stale["evidence"] = list(receipt["evidence"]) + [
        {
            "kind": "freshness",
            "scope": "data",
            "state": "STALE",
            "age_ms": age_ms,
            "max_age_ms": max_age_ms,
        }
    ]
    return stale
