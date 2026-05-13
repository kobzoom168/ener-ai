"""Multi-intent NL diagnostic: one provenance footer, quick summary, OTP copy."""
import unittest

from app.core import diagnostics as diag


class TestMultiIntentQuickSummary(unittest.TestCase):
    def test_bullets_order_follows_intents(self):
        cache = {
            "diag_resource": {"cpu_percent": 10.0, "ram_percent": 20.0, "disk_percent": 30.0},
            "diag_otp": {
                "evidence": {
                    "otp_events": [],
                    "analysis": {},
                    "otp_state": {"seconds_since_last_admin_otp_sent": 7200},
                },
                "errors": [],
            },
        }
        intents = ["diag_resource", "diag_otp"]
        s = diag.format_multi_intent_quick_summary_bullets(intents, cache)
        self.assertIn("**Resource:**", s)
        self.assertIn("**OTP:**", s)
        self.assertLess(s.index("Resource"), s.index("OTP"))

    def test_router_style_summary_block_prefix(self):
        """Router prepends this exact heading before bullets."""
        cache = {"diag_otp": {"evidence": {"otp_events": [], "analysis": {}, "otp_state": {}}, "errors": []}}
        summary = diag.format_multi_intent_quick_summary_bullets(["diag_otp"], cache)
        block = "**สรุปเร็ว:**\n" + summary
        self.assertTrue(block.startswith("**สรุปเร็ว:**"))


class TestCombinedProvenanceFooter(unittest.TestCase):
    def test_single_footer_when_sections_omit(self):
        d_otp = {
            "evidence": {
                "otp_state": {
                    "has_admin_otp": False,
                    "admin_otp_expires_in": 0,
                    "seconds_since_last_admin_otp_sent": 7200,
                    "has_terminal_otp": False,
                },
                "analysis": {},
                "otp_events": [],
                "app_logs": {},
                "git": {},
            },
            "errors": [],
        }
        rows = [
            {
                "created_at": "2026-01-01 00:00:00",
                "agent_name": "MemoryKeeper",
                "result": 1,
                "summary": "ok",
            }
        ] * 7
        d_agent = {"evidence": {"memory_agent_events": rows, "failed_ai_runs": []}, "errors": []}
        f1 = diag.format_otp_diagnosis_thai(d_otp, include_provenance_footer=False)
        f2 = diag.format_agent_diagnosis_thai(
            d_agent, include_provenance_footer=False, max_events=5
        )
        foot = diag._diag_provenance_footer(verbose=False)
        marker = "**หลักฐาน:** อิงข้อมูลจาก collector"
        combined = f1 + "\n\n" + f2 + foot
        self.assertEqual(combined.count(marker), 1)
        self.assertNotIn("/diag memory", f1)
        self.assertIn("/diag memory", f2)


class TestOtpFormatterNoAuditLongGap(unittest.TestCase):
    def test_human_lines_when_no_events_and_last_sent_over_1h(self):
        d = {
            "evidence": {
                "otp_state": {
                    "has_admin_otp": False,
                    "admin_otp_expires_in": 0,
                    "seconds_since_last_admin_otp_sent": 7200,
                    "has_terminal_otp": False,
                },
                "analysis": {},
                "otp_events": [],
                "app_logs": {},
                "git": {},
            },
            "errors": [],
        }
        txt = diag.format_otp_diagnosis_thai(d, include_provenance_footer=False)
        self.assertIn("ไม่พบว่า OTP ยังวนอยู่", txt)
        self.assertIn("ชม.", txt)
        self.assertIn("otp_audit_logs", txt)


if __name__ == "__main__":
    unittest.main()
