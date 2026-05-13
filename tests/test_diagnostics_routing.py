"""Routing + communication guard for diagnostics vs Main Agent context."""
import unittest

from app.core.diagnostics import (
    classify_diagnostic_intent,
    communication_followup_reply_thai,
    is_communication_followup_intent,
)


class TestDiagnosticRouting(unittest.TestCase):
    def test_customer_no_diagnostic(self):
        self.assertIsNone(classify_diagnostic_intent("ลูกค้าไม่ตอบ"))
        self.assertTrue(is_communication_followup_intent("ลูกค้าไม่ตอบ"))

    def test_vendor_team_communication(self):
        self.assertIsNone(classify_diagnostic_intent("vendor ไม่ตอบ"))
        self.assertTrue(is_communication_followup_intent("vendor ไม่ตอบ"))
        self.assertIsNone(classify_diagnostic_intent("ทีมไม่ตอบ"))
        self.assertTrue(is_communication_followup_intent("ทีมไม่ตอบ"))

    def test_bot_system_telegram_still_diagnostic(self):
        self.assertEqual(classify_diagnostic_intent("bot ไม่ตอบ"), "bot")
        self.assertEqual(classify_diagnostic_intent("ระบบไม่ตอบ"), "bot")
        self.assertEqual(classify_diagnostic_intent("telegram ไม่ตอบ"), "bot")

    def test_otp_diagnostic(self):
        self.assertEqual(classify_diagnostic_intent("ทำไม otp ส่งตลอด"), "otp")

    def test_resource_diagnostic_intents(self):
        self.assertEqual(classify_diagnostic_intent("ซีพียูเท่าไหร่ตอนนี้"), "resource")
        self.assertEqual(classify_diagnostic_intent("Ram ละ"), "resource")
        self.assertEqual(classify_diagnostic_intent("ใช้ mem เท่าไหร่"), "resource")
        self.assertEqual(classify_diagnostic_intent("container หนักไหม"), "resource")

    def test_mixed_customer_and_tech_goes_diagnostic(self):
        self.assertEqual(classify_diagnostic_intent("ลูกค้าไม่ตอบ webhook เพี้ยน"), "bot")

    def test_communication_reply_has_no_engineering_terms(self):
        reply = communication_followup_reply_thai("ลูกค้าไม่ตอบ")
        low = reply.lower()
        banned = ("ssh", "server", "otp", "source code", "repo", "diagnostic", "webhook")
        for w in banned:
            with self.subTest(word=w):
                self.assertNotIn(w, low)


if __name__ == "__main__":
    unittest.main()
