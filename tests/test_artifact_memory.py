"""Smoke tests for artifact_memory helpers."""
import json
import unittest

from app.core.artifact_memory import (
    map_event_type_to_artifact_type,
    _safe_payload_json,
    _sanitize_value,
)


class ArtifactMemoryTest(unittest.TestCase):
    def test_event_type_mapping(self):
        self.assertEqual(map_event_type_to_artifact_type("report_created"), "scan_report")
        self.assertEqual(map_event_type_to_artifact_type("scan_completed"), "scan_activity")
        self.assertEqual(map_event_type_to_artifact_type("payment_approved"), "payment_event")
        self.assertEqual(map_event_type_to_artifact_type("unknown"), "external_event")

    def test_redacts_secrets_and_images(self):
        payload = {
            "api_key": "secret123",
            "slip_image": "binary",
            "reportUrl": "https://example.com/r/x",
        }
        cleaned = _sanitize_value(payload)
        self.assertEqual(cleaned["api_key"], "[redacted]")
        self.assertEqual(cleaned["slip_image"], "[redacted]")
        self.assertEqual(cleaned["reportUrl"], "https://example.com/r/x")
        text = _safe_payload_json({"payload": cleaned})
        self.assertNotIn("secret123", text)


if __name__ == "__main__":
    unittest.main()
