"""domain_scope + work_query routing."""
import os
import unittest

from app.core import domain_scope as dom
from app.core.tool_router import classify_message_intents


class TestDomainScopeDetection(unittest.TestCase):
    def test_hospital_signals(self):
        self.assertEqual(
            dom.detect_domain_scope("งานโรงบาล\nList Today\nProject 1: PBX"),
            dom.DOMAIN_HOSPITAL_WORK,
        )

    def test_personal_business(self):
        self.assertEqual(
            dom.detect_domain_scope("วันนี้ต้องถ่ายรูปพระกี่องค์"),
            dom.DOMAIN_PERSONAL_BUSINESS,
        )
        self.assertEqual(
            dom.detect_domain_scope("งานส่วนตัวมีอะไรบ้าง"),
            dom.DOMAIN_PERSONAL_BUSINESS,
        )

    def test_ener_ai_system(self):
        self.assertEqual(
            dom.detect_domain_scope("งานระบบมีอะไรค้าง"),
            dom.DOMAIN_ENER_AI_SYSTEM,
        )
        self.assertEqual(
            dom.detect_domain_scope("ทำไม OTP ส่งตลอด"),
            dom.DOMAIN_ENER_AI_SYSTEM,
        )

    def test_personal_life(self):
        self.assertEqual(
            dom.detect_domain_scope("ผมเหนื่อยมากวันนี้"),
            dom.DOMAIN_PERSONAL_LIFE,
        )

    def test_work_query_intent(self):
        self.assertEqual(
            classify_message_intents("ตอนนี้มีงานอะไรบ้างครับ"),
            ["work_query"],
        )

    def test_otp_complaint_not_work_query(self):
        intents = classify_message_intents("ทำไม OTP ส่งตลอด")
        self.assertIn("diag_otp", intents)
        self.assertNotIn("work_query", intents)


@unittest.skipUnless(os.environ.get("ENER_AI_DOMAIN_DB_TEST") == "1", "set ENER_AI_DOMAIN_DB_TEST=1 to run DB snapshot test")
class TestDomainScopeSnapshotIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_work_query_uses_hospital_snapshot_after_persist(self):
        from app.core.database import init_db

        await init_db()
        cid = "test_chat_domain"
        wall = (
            "งานโรงบาล\nList Today\nProject 1: X\nCurrent Status: ok\n% Complete: 10%\n"
        )
        foot = await dom.persist_hospital_work_snapshot_for_chat(cid, wall)
        self.assertIn("Snapshot", foot)
        reply = await dom.format_work_query_reply_thai("ตอนนี้มีงานอะไรบ้าง", cid)
        self.assertIn("snapshot", reply.lower())
        self.assertIn("task db", reply.lower())


if __name__ == "__main__":
    unittest.main()
