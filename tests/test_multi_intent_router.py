"""Multi-intent NL classification (system + communication + diagnostics)."""
import unittest

from app.core.tool_router import classify_message_intents


class TestMultiIntentRouter(unittest.TestCase):
    def test_status_errors_logs_combo(self):
        text = "ระบบตอนนี้เป็นไง มี error ไหม ดู logs ล่าสุด"
        self.assertEqual(
            classify_message_intents(text),
            ["system_status", "system_errors", "system_logs"],
        )

    def test_communication_bot_otp_combo(self):
        text = "ลูกค้าไม่ตอบ bot ไม่ตอบ ทำไม otp ส่งตลอด"
        self.assertEqual(
            classify_message_intents(text),
            ["communication", "diag_bot", "diag_otp"],
        )

    def test_server_only(self):
        self.assertEqual(
            classify_message_intents("เช็ค server ของระบบ Ener AI"),
            ["system_server"],
        )

    def test_diag_resource_thai_cpu(self):
        self.assertEqual(
            classify_message_intents("ซีพียูเท่าไหร่ตอนนี้"),
            ["diag_resource"],
        )

    def test_diag_resource_ram_polite(self):
        self.assertEqual(classify_message_intents("Ram ละ"), ["diag_resource"])

    def test_diag_resource_drops_parallel_system_server(self):
        self.assertEqual(
            classify_message_intents("เช็ค CPU ของระบบ Ener AI"),
            ["diag_resource"],
        )

    def test_diag_resource_memory_usage_thai(self):
        self.assertEqual(
            classify_message_intents("memory usage เท่าไหร่"),
            ["diag_resource"],
        )

    def test_memorykeeper_is_diag_agent(self):
        self.assertEqual(classify_message_intents("memorykeeper ล้ม"), ["diag_agent"])

    def test_communication_only(self):
        self.assertEqual(classify_message_intents("ลูกค้าไม่ตอบ"), ["communication"])

    def test_diag_bot_only(self):
        self.assertEqual(classify_message_intents("bot ไม่ตอบ"), ["diag_bot"])

    def test_diag_otp_only(self):
        self.assertEqual(classify_message_intents("ทำไม otp ส่งตลอด"), ["diag_otp"])

    def test_multiline_merge(self):
        text = "เช็ค CPU\nมี error ไหม"
        self.assertEqual(
            classify_message_intents(text),
            ["system_server", "system_errors"],
        )

    def test_max_four_intents(self):
        text = (
            "ลูกค้าไม่ตอบ bot ไม่ตอบ ทำไม otp ส่งตลอด "
            "ระบบตอนนี้เป็นไง มี error ไหม ดู logs ล่าสุด เช็ค ram"
        )
        intents = classify_message_intents(text)
        self.assertLessEqual(len(intents), 4)


if __name__ == "__main__":
    unittest.main()
