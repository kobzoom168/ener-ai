"""work_update intent: hospital standup must not route to diag_resource."""
import unittest

from app.core import diagnostics as diag
from app.core.tool_router import classify_message_intents, classify_system_tool_intent


def _hospital_wall() -> str:
    return (
        "งานโรงบาล\n"
        "List Today 11 พ.ค. - 15 พ.ค. 2026\n"
        "Project 1: Cloud Contact Center และ Cloud PBX\n"
        "Current Status: In Progress\n"
        "% Complete: 5%\n"
        "สิ่งที่ต้องทำวันนี้(13-May-2026)\n"
        "Migration DB to AWS\n"
        "เช็คดูเรื่อง traffic network ว่าถ้าเอา DB ขึ้น AWS infra จะพอไหม "
        "(เตรียมเรื่อง network, server infra)\n"
    )


class TestWorkUpdateRouting(unittest.TestCase):
    def test_hospital_update_is_work_update_not_resource(self):
        text = _hospital_wall()
        self.assertTrue(diag.is_work_update_message(text))
        self.assertEqual(classify_message_intents(text), ["work_update"])
        self.assertNotIn("diag_resource", classify_message_intents(text))

    def test_short_cpu_ram_lines_remain_diag_resource(self):
        text = "ซีพียูเท่าไหร่ตอนนี้\nRam ละ"
        self.assertEqual(classify_message_intents(text), ["diag_resource"])

    def test_memorykeeper_still_agent(self):
        self.assertEqual(classify_message_intents("memorykeeper ล้ม"), ["diag_agent"])

    def test_short_check_server_is_diag_resource(self):
        self.assertEqual(classify_message_intents("เช็ค server"), ["diag_resource"])

    def test_long_project_report_network_not_resource_without_work_signals(self):
        text = (
            "List Today\n"
            "Project 1: something\n"
            "Current Status: ok\n"
            + "filler line\n" * 40
            + "\nเตรียมเรื่อง network, server infra AWS DB\n"
        )
        self.assertFalse(diag.is_work_update_message(text))
        intents = classify_message_intents(text)
        self.assertNotIn("diag_resource", intents)

    def test_work_update_system_tool_returns_none(self):
        self.assertIsNone(classify_system_tool_intent(_hospital_wall()))


if __name__ == "__main__":
    unittest.main()
