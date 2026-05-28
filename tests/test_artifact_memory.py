"""Tests for artifact_memory helpers and backfill/dedup behavior."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.artifact_memory import (
    backfill_external_event_artifacts,
    map_event_type_to_artifact_type,
    reconstruct_agent_event_payload,
    store_external_event_artifact,
    _safe_payload_json,
    _sanitize_value,
)
from app.core.database import get_db, init_db
from app.core.event_log import log_event


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

    def test_reconstruct_agent_event_payload(self):
        row = {
            "id": 42,
            "event_type": "report_created",
            "triggered_by": "ener_scan",
            "summary": "test summary",
            "tags": json.dumps(["ener_scan", "ener-scan", "report_created"]),
            "context": json.dumps(
                {
                    "source": "ener_scan",
                    "project_slug": "ener-scan",
                    "payload": {"reportId": "r1"},
                    "external_object_id": "r1",
                }
            ),
        }
        payload = reconstruct_agent_event_payload(row)
        self.assertEqual(payload["event_id"], 42)
        self.assertEqual(payload["project_slug"], "ener-scan")
        self.assertEqual(payload["payload"]["reportId"], "r1")


class ArtifactMemoryDbTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _run(self, coro):
        return asyncio.run(coro)

    def _patch_db(self):
        return patch("app.core.database.DB_PATH", Path(self.db_path))

    def test_duplicate_event_id_returns_existing_artifact(self):
        async def _test():
            with self._patch_db():
                await init_db()
                event_id = await log_event(
                    agent_name="AIGatewayEvent",
                    event_type="report_created",
                    summary="dup test",
                    tags=["ener_scan", "ener-scan"],
                    triggered_by="ener_scan",
                    context=json.dumps(
                        {
                            "source": "ener_scan",
                            "project_slug": "ener-scan",
                            "payload": {},
                        }
                    ),
                )
                first = await store_external_event_artifact(
                    {
                        "event_id": event_id,
                        "source": "ener_scan",
                        "event_type": "report_created",
                        "project_slug": "ener-scan",
                        "summary": "dup test",
                        "payload": {},
                    }
                )
                second = await store_external_event_artifact(
                    {
                        "event_id": event_id,
                        "source": "ener_scan",
                        "event_type": "report_created",
                        "project_slug": "ener-scan",
                        "summary": "dup test",
                        "payload": {},
                    }
                )
                self.assertTrue(first.get("ok"))
                self.assertTrue(second.get("ok"))
                self.assertEqual(first["artifact_id"], second["artifact_id"])
                self.assertFalse(first.get("existing"))
                self.assertTrue(second.get("existing"))
                async with get_db() as db:
                    cur = await db.execute(
                        "SELECT COUNT(*) AS cnt FROM project_artifacts WHERE event_id = ?",
                        (event_id,),
                    )
                    self.assertEqual(int((await cur.fetchone())["cnt"]), 1)

        self._run(_test())

    def test_backfill_creates_and_skips(self):
        async def _test():
            with self._patch_db():
                await init_db()
                event_id = await log_event(
                    agent_name="AIGatewayEvent",
                    event_type="scan_completed",
                    summary="backfill create",
                    tags=["ener_scan", "ener-scan", "scan_completed"],
                    triggered_by="ener_scan",
                    context=json.dumps(
                        {
                            "source": "ener_scan",
                            "project_slug": "ener-scan",
                            "payload": {"jobId": "j1"},
                        }
                    ),
                )
                result = await backfill_external_event_artifacts(
                    source="ener_scan",
                    project_slug="ener-scan",
                    limit=500,
                )
                self.assertTrue(result["ok"])
                self.assertGreaterEqual(result["created"], 1)
                self.assertGreaterEqual(result["scanned"], 1)

                again = await backfill_external_event_artifacts(
                    source="ener_scan",
                    project_slug="ener-scan",
                    limit=500,
                )
                self.assertTrue(again["ok"])
                self.assertGreaterEqual(again["skipped"], 1)

                stored = await store_external_event_artifact(
                    {
                        "event_id": event_id,
                        "source": "ener_scan",
                        "event_type": "scan_completed",
                        "project_slug": "ener-scan",
                        "summary": "already stored",
                        "payload": {},
                    }
                )
                self.assertTrue(stored.get("existing"))

        self._run(_test())


if __name__ == "__main__":
    unittest.main()
