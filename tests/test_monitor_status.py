"""cmd_status: no LLM hallucination when logs unavailable or no error evidence."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.agents import monitor_agent as ma


def _sample_stats(ok: bool = True) -> dict:
    if ok:
        return {
            "cpu_percent": 10.0,
            "ram_used_gb": 1.0,
            "ram_total_gb": 8.0,
            "ram_percent": 20.0,
            "disk_used_gb": 10.0,
            "disk_total_gb": 100.0,
            "disk_percent": 30.0,
            "top_processes": ["python CPU:1.0% RAM:2.0%"],
        }
    return {
        "cpu_percent": 95.0,
        "ram_used_gb": 7.0,
        "ram_total_gb": 8.0,
        "ram_percent": 92.0,
        "disk_used_gb": 10.0,
        "disk_total_gb": 100.0,
        "disk_percent": 30.0,
        "top_processes": ["python CPU:90.0% RAM:50.0%"],
    }


class TestMonitorStatusHelpers(unittest.TestCase):
    def test_docker_logs_unavailable(self):
        self.assertTrue(ma._docker_logs_unavailable(""))
        self.assertTrue(ma._docker_logs_unavailable("ดึง logs ไม่ได้: denied"))
        self.assertTrue(ma._docker_logs_unavailable("ไม่พบ container ener-ai"))
        self.assertFalse(ma._docker_logs_unavailable("ไม่พบ error"))

    def test_logs_filter_reported_no_errors(self):
        self.assertTrue(ma._logs_filter_reported_no_errors("ไม่พบ error"))
        self.assertTrue(ma._logs_filter_reported_no_errors("ไม่มี logs"))
        self.assertFalse(ma._logs_filter_reported_no_errors("error: boom"))


class TestCmdStatus(unittest.TestCase):
    def test_metrics_ok_logs_unavailable_no_llm(self):
        async def _run():
            with (
                patch.object(ma, "get_server_stats", return_value=_sample_stats(True)),
                patch.object(
                    ma,
                    "get_docker_logs",
                    return_value="ดึง logs ไม่ได้: permission denied",
                ),
                patch.object(ma, "_analyze_with_groq", new_callable=AsyncMock) as mock_llm,
            ):
                mock_llm.return_value = "LLM_SHOULD_NOT_RUN"
                out = await ma.cmd_status()
            self.assertNotIn("LLM_SHOULD_NOT_RUN", out)
            self.assertIn("no_log_access", out)
            self.assertIn("สถานะจาก metric: CPU/RAM/Disk OK", out)
            self.assertIn("mount `/var/log/ener-ai`", out)
            mock_llm.assert_not_called()

        asyncio.run(_run())

    def test_metrics_ok_clean_logs_no_llm(self):
        async def _run():
            with (
                patch.object(ma, "get_server_stats", return_value=_sample_stats(True)),
                patch.object(ma, "get_docker_logs", return_value="ไม่พบ error"),
                patch.object(ma, "_analyze_with_groq", new_callable=AsyncMock) as mock_llm,
            ):
                mock_llm.return_value = "LLM_SHOULD_NOT_RUN"
                out = await ma.cmd_status()
            self.assertNotIn("LLM_SHOULD_NOT_RUN", out)
            self.assertIn("ปกติดี", out)
            mock_llm.assert_not_called()

        asyncio.run(_run())

    def test_metrics_bad_clean_logs_no_llm(self):
        async def _run():
            with (
                patch.object(ma, "get_server_stats", return_value=_sample_stats(False)),
                patch.object(ma, "get_docker_logs", return_value="ไม่พบ error"),
                patch.object(ma, "_analyze_with_groq", new_callable=AsyncMock) as mock_llm,
            ):
                mock_llm.return_value = "LLM_SHOULD_NOT_RUN"
                out = await ma.cmd_status()
            self.assertNotIn("LLM_SHOULD_NOT_RUN", out)
            self.assertIn("ไม่มีหลักฐาน error", out)
            self.assertIn("ไม่เรียก LLM", out)
            mock_llm.assert_not_called()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
