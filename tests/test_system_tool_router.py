"""Natural-language routing to monitor tools (CPU/RAM/logs/errors)."""
import unittest

from app.core.tool_router import classify_system_tool_intent


class TestSystemToolRouter(unittest.TestCase):
    def test_cpu_ener_ai(self):
        self.assertEqual(
            classify_system_tool_intent("เช็ค CPU ของระบบ Ener AI"),
            "server",
        )

    def test_ram_polite(self):
        self.assertEqual(classify_system_tool_intent("ดู RAM ให้หน่อย"), "server")

    def test_system_status_thai(self):
        self.assertEqual(classify_system_tool_intent("ระบบตอนนี้เป็นไง"), "status")

    def test_errors_nl(self):
        self.assertEqual(classify_system_tool_intent("มี error ไหม"), "errors")

    def test_logs_nl(self):
        self.assertEqual(classify_system_tool_intent("ดู logs ล่าสุด"), "logs")

    def test_customer_not_system_tool(self):
        self.assertIsNone(classify_system_tool_intent("ลูกค้าไม่ตอบ"))

    def test_bot_diagnostic_not_server(self):
        self.assertIsNone(classify_system_tool_intent("bot ไม่ตอบ"))

    def test_otp_diagnostic_not_system_tool(self):
        self.assertIsNone(classify_system_tool_intent("ทำไม otp ส่งตลอด"))


if __name__ == "__main__":
    unittest.main()
