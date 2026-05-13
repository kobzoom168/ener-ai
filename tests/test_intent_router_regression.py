"""Regression tests: NL intents must not mis-route content/planning to diag_resource."""
import unittest

from app.core import diagnostics as diag
from app.core.tool_router import classify_message_intents


class TestIntentRouterRegression(unittest.TestCase):
    def test_casual_content_not_diag(self):
        for msg in (
            "คิด content ขายพระให้หน่อย",
            "ช่วยเขียน caption TikTok",
            "ผมเหนื่อยมากวันนี้",
        ):
            with self.subTest(msg=msg):
                self.assertNotIn("diag_resource", classify_message_intents(msg))
                self.assertNotIn("work_update", classify_message_intents(msg))

    def test_news_planning_not_diag(self):
        for msg in (
            "ช่วยสรุปข่าว AI วันนี้",
            "ช่วยวางแผน migration DB to AWS",
            "เช็ค traffic network ถ้าเอา DB ขึ้น AWS infra จะพอไหม",
        ):
            with self.subTest(msg=msg):
                self.assertNotIn("diag_resource", classify_message_intents(msg))

    def test_customer_server_not_diag(self):
        self.assertNotIn("diag_resource", classify_message_intents("server ลูกค้าล่ม"))

    def test_ener_ai_server_checks_diag_resource(self):
        self.assertEqual(
            classify_message_intents("เช็ค server ของ Ener-AI ตอนนี้"),
            ["diag_resource"],
        )
        self.assertEqual(classify_message_intents("เช็ค server"), ["diag_resource"])

    def test_memory_help_not_diag_resource(self):
        self.assertNotIn(
            "diag_resource",
            classify_message_intents("memory ไม่จำข้อมูลที่ผมบอก"),
        )

    def test_memorykeeper_agent(self):
        self.assertEqual(classify_message_intents("memorykeeper ล้ม"), ["diag_agent"])

    def test_memory_usage_diag_resource(self):
        self.assertEqual(
            classify_message_intents("memory usage เท่าไหร่"),
            ["diag_resource"],
        )

    def test_detect_target_scope(self):
        self.assertEqual(diag.detect_target_scope("server ลูกค้าล่ม"), "external_customer_system")
        self.assertEqual(diag.detect_target_scope("ช่วยวางแผน migration"), "general_planning")
        self.assertEqual(diag.detect_target_scope("เช็ค cpu"), "ener_ai_system")
        self.assertEqual(diag.detect_target_scope("งานโรงบาล\nList Today"), "work_report")


if __name__ == "__main__":
    unittest.main()
