"""Unit tests for Ener Scan business summary helpers."""
import json
import unittest

from app.core.ener_scan_business import (
    aggregate_artifacts,
    build_recent_item,
    is_payment_approved,
    is_report_created,
    is_scan_completed,
    normalize_range,
    parse_amount_from_payload,
    safe_conversion_rate,
)


class EnerScanBusinessTest(unittest.TestCase):
    def test_normalize_range(self):
        self.assertEqual(normalize_range("7d"), "7d")
        self.assertEqual(normalize_range("today"), "today")
        self.assertEqual(normalize_range("invalid"), "7d")

    def test_conversion_zero_denominator(self):
        self.assertEqual(safe_conversion_rate(5, 0), 0.0)
        self.assertEqual(safe_conversion_rate(2, 4), 50.0)

    def test_event_type_counts(self):
        rows = [
            {
                "artifact_type": "scan_activity",
                "payload_json": json.dumps({"event_type": "scan_completed"}),
                "created_at": "2026-05-28 10:00:00",
                "source": "ener_scan",
                "tags": "[]",
            },
            {
                "artifact_type": "scan_report",
                "payload_json": json.dumps({"event_type": "report_created"}),
                "created_at": "2026-05-28 11:00:00",
                "source": "ener_scan",
                "tags": "[]",
            },
            {
                "artifact_type": "payment_event",
                "payload_json": json.dumps(
                    {"event_type": "payment_approved", "payload": {"amount": 99}}
                ),
                "created_at": "2026-05-28 12:00:00",
                "source": "ener_scan",
                "tags": "[]",
            },
        ]
        agg = aggregate_artifacts(rows, "7d")
        s = agg["summary"]
        self.assertEqual(s["scan_completed"], 1)
        self.assertEqual(s["report_created"], 1)
        self.assertEqual(s["payment_approved"], 1)
        self.assertEqual(s["estimated_revenue"], 99.0)
        self.assertEqual(s["scan_to_report_rate"], 100.0)
        self.assertEqual(s["report_to_payment_rate"], 100.0)

    def test_amount_parsing_safe(self):
        self.assertEqual(parse_amount_from_payload({"price": "150"}), 150.0)
        self.assertEqual(parse_amount_from_payload({"payload": {"total": 50}}), 50.0)
        self.assertEqual(parse_amount_from_payload({"amount": "bad"}), 0.0)

    def test_recent_hides_secrets(self):
        row = {
            "id": 1,
            "artifact_type": "payment_event",
            "title": "Pay",
            "summary": "ok",
            "external_id": "p1",
            "created_at": "2026-05-28 12:00:00",
            "payload_json": json.dumps(
                {
                    "event_type": "payment_approved",
                    "external_user_id": "U123",
                    "payload": {
                        "api_key": "secret",
                        "slip_image": "x",
                        "amount": 10,
                    },
                }
            ),
        }
        item = build_recent_item(row)
        self.assertEqual(item["external_user_id"], "U123")
        self.assertEqual(item["amount"], 10.0)
        self.assertNotIn("payload", item)
        self.assertNotIn("api_key", json.dumps(item))

    def test_type_helpers(self):
        self.assertTrue(is_scan_completed("scan_activity", ""))
        self.assertTrue(is_scan_completed("", "scan_completed"))
        self.assertTrue(is_report_created("scan_report", ""))
        self.assertTrue(is_payment_approved("payment_event", ""))


if __name__ == "__main__":
    unittest.main()
