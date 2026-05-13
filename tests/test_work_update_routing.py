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

    def test_long_project_report_is_work_update_not_resource(self):
        text = (
            "List Today\n"
            "Project 1: something\n"
            "Current Status: ok\n"
            + "filler line\n" * 40
            + "\nเตรียมเรื่อง network, server infra AWS DB\n"
        )
        self.assertTrue(diag.is_work_update_message(text))
        self.assertEqual(classify_message_intents(text), ["work_update"])
        self.assertNotIn("diag_resource", classify_message_intents(text))

    def test_short_hospital_with_project_hints(self):
        text = "งานโรงบาลวันนี้\nBackup Solution เหลือ Training\nHost VM รอราคา"
        self.assertTrue(diag.is_work_update_message(text))
        self.assertEqual(classify_message_intents(text), ["work_update"])

    def test_hospital_with_list_today_short_message(self):
        text = "งานโรงบาล\nList Today"
        self.assertTrue(diag.is_work_update_message(text))
        self.assertEqual(classify_message_intents(text), ["work_update"])

    def test_hospital_mood_only_not_work_update(self):
        text = "งานโรงบาล วันนี้เหนื่อยมาก"
        self.assertFalse(diag.is_work_update_message(text))
        self.assertNotEqual(classify_message_intents(text), ["work_update"])

    def test_lowercase_english_markers(self):
        text = (
            "list today\n"
            "project 1: x\n"
            "current status: ok\n"
            "% complete: 10%\n"
            + "pad\n" * 30
        )
        self.assertTrue(diag.is_work_update_message(text))
        self.assertEqual(classify_message_intents(text), ["work_update"])

    def test_ack_states_not_persisted_to_db(self):
        ack = diag.format_work_update_ack_thai(
            "งานโรงบาล\nBackup Solution: ok\nHost VM: pending\n"
        )
        self.assertIn("ยังไม่ได้บันทึก", ack)

    def test_work_update_system_tool_returns_none(self):
        self.assertIsNone(classify_system_tool_intent(_hospital_wall()))

    def test_migration_project_only_not_long_report_shape(self):
        text = "Project Alpha scope\nMigration DB notes\n" + "detail\n" * 45
        self.assertFalse(diag.looks_like_long_project_report(text))
        self.assertFalse(diag.is_work_update_message(text))


if __name__ == "__main__":
    unittest.main()
