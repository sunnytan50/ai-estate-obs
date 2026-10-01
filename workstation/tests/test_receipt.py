"""Tests for the collector's operational freshness/transport receipt."""

import sys
import tempfile
import unittest
from pathlib import Path

_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

from aiobs_collector.core import Sample  # noqa: E402
from aiobs_collector.receipt import (  # noqa: E402
    build_receipt,
    complete_lane_success,
    lane_outcomes,
    load_receipt,
    receipt_path,
    validate_receipt,
    write_receipt,
)


class ReceiptTests(unittest.TestCase):
    NOW = 1_759_000_000_000

    def test_receipt_keeps_data_freshness_separate_from_transport(self):
        samples = [
            Sample("aiobs_lane_up", {"lane": "tokscale"}, 1.0, self.NOW),
            Sample("aiobs_lane_up", {"lane": "openrouter"}, 0.0, self.NOW),
        ]
        successful, failed = lane_outcomes(samples, ["tokscale", "openrouter"])
        receipt = build_receipt(
            observed_ms=self.NOW,
            enabled_lanes=["tokscale", "openrouter"],
            successful_lanes=successful,
            failed_lanes=failed,
            collected_count=3,
            pushed_count=3,
            transport_state="HEALTHY",
        )
        validate_receipt(receipt)
        self.assertEqual(receipt["state"], "STALE")
        self.assertEqual(receipt["data"]["state"], "STALE")
        self.assertEqual(receipt["transport"]["state"], "HEALTHY")
        self.assertIsNone(receipt["last_success_at"])
        self.assertEqual(receipt["data"]["freshness_basis"], "collector_lane_observation")
        self.assertEqual(receipt["transport"]["last_success_at"], receipt["observed_at"])

    def test_blocked_transport_is_explicit_and_preserves_prior_success(self):
        prior = {"collector:last_success_ms": self.NOW - 60_000, "transport:last_success_ms": self.NOW - 60_000}
        receipt = build_receipt(
            observed_ms=self.NOW,
            enabled_lanes=["tokscale"],
            successful_lanes=[],
            failed_lanes=["tokscale"],
            collected_count=1,
            pushed_count=0,
            transport_state="BLOCKED",
            prior_state=prior,
            transport_error=RuntimeError("connection refused"),
        )
        self.assertEqual(receipt["state"], "BLOCKED")
        self.assertEqual(receipt["data"]["state"], "STALE")
        self.assertEqual(receipt["transport"]["state"], "BLOCKED")
        self.assertEqual(receipt["transport"]["last_success_at"], "2025-09-27T19:05:40.000Z")
        self.assertEqual(
            receipt["evidence"][-1]["error"],
            {"reason": "push_failed", "exception_class": "RuntimeError"},
        )
        self.assertNotIn("connection refused", str(receipt))

    def test_complete_lane_success_requires_every_enabled_lane(self):
        self.assertFalse(complete_lane_success(["tokscale", "openrouter"], ["tokscale"]))
        self.assertTrue(complete_lane_success(["tokscale", "openrouter"], ["openrouter", "tokscale"]))
        self.assertFalse(complete_lane_success(["tokscale"], ["tokscale"], "PAUSED"))

    def test_empty_lanes_are_paused(self):
        receipt = build_receipt(
            observed_ms=self.NOW,
            enabled_lanes=[],
            successful_lanes=[],
            failed_lanes=[],
            collected_count=0,
            pushed_count=0,
            transport_state="HEALTHY",
        )
        self.assertEqual(receipt["state"], "PAUSED")
        self.assertEqual(receipt["data"]["state"], "PAUSED")

    def test_data_freshness_ages_even_when_transport_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = build_receipt(
                observed_ms=self.NOW,
                enabled_lanes=["tokscale"],
                successful_lanes=["tokscale"],
                failed_lanes=[],
                collected_count=2,
                pushed_count=0,
                transport_state="BLOCKED",
            )
            self.assertEqual(receipt["state"], "BLOCKED")
            self.assertEqual(receipt["data"]["state"], "HEALTHY")
            write_receipt(directory, receipt)
            aged = load_receipt(directory, now_ms=self.NOW + 901_000, max_age_ms=900_000)
            self.assertEqual(aged["state"], "BLOCKED")
            self.assertEqual(aged["data"]["state"], "STALE")
            self.assertEqual(aged["freshness"]["scope"], "data")

    def test_write_load_and_age_assessment_are_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = build_receipt(
                observed_ms=self.NOW,
                enabled_lanes=["tokscale"],
                successful_lanes=["tokscale"],
                failed_lanes=[],
                collected_count=2,
                pushed_count=2,
                transport_state="HEALTHY",
            )
            write_receipt(directory, receipt)
            self.assertEqual(load_receipt(directory), receipt)
            stale = load_receipt(directory, now_ms=self.NOW + 901_000, max_age_ms=900_000)
            self.assertEqual(stale["state"], "STALE")
            self.assertEqual(stale["freshness"]["state"], "STALE")
            self.assertEqual(list(Path(directory).glob(".*collector-receipt.json.*")), [])

    def test_missing_or_corrupt_receipt_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            unknown = load_receipt(directory)
            self.assertEqual(unknown["state"], "UNKNOWN")
            receipt_path(directory).write_text("{broken", encoding="utf-8")
            self.assertEqual(load_receipt(directory)["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
