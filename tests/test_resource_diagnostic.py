"""Resource diagnostic: provenance, no fake shell output lines."""
import asyncio
import unittest

from app.core.diagnostics import (
    diagnose_resource_usage,
    format_resource_diagnosis_thai,
    format_resource_debug_appendix,
)


class TestResourceDiagnostic(unittest.TestCase):
    def test_format_has_source_and_collected_at(self):
        d = {
            "collected_at": "2026-01-01 00:00:00 UTC",
            "source_used": "psutil",
            "cpu_percent": 2.5,
            "process_cpu_percent": 0.1,
            "ram_percent": 25.0,
            "ram_used_mb": 512,
            "ram_total_mb": 2048,
            "disk_percent": 41.0,
            "docker_raw": None,
            "docker_status": "no_access",
            "docker_reason": "test",
            "server_metrics_row": None,
            "errors": [],
        }
        txt = format_resource_diagnosis_thai(d)
        self.assertIn("source_used", txt)
        self.assertIn("psutil", txt)
        self.assertIn("collected_at", txt)
        self.assertIn("2026-01-01", txt)
        self.assertNotIn("output:", txt.lower())

    def test_format_includes_docker_raw_only_when_present(self):
        d = {
            "collected_at": "x",
            "source_used": "psutil+docker_stats",
            "cpu_percent": 1.0,
            "process_cpu_percent": None,
            "ram_percent": 10.0,
            "ram_used_mb": 100,
            "ram_total_mb": 1000,
            "disk_percent": 20.0,
            "docker_raw": '{"CPUPerc":"3.00%"}',
            "docker_status": "ok",
            "docker_reason": "",
            "server_metrics_row": None,
            "errors": [],
        }
        txt = format_resource_diagnosis_thai(d)
        self.assertIn("docker", txt.lower())
        self.assertIn("CPUPerc", txt)

    def test_diagnose_runs_without_fake_output(self):
        async def _run():
            d = await diagnose_resource_usage()
            self.assertIn("collected_at", d)
            self.assertIn("source_used", d)
            self.assertNotIn("evidence", d)
            txt = format_resource_diagnosis_thai(d)
            self.assertNotRegex(txt, r"(?i)docker\s+stats[^\n]*\n\s*output\s*:")

        asyncio.run(_run())

    def test_debug_appendix_empty_without_debug_collect(self):
        self.assertEqual(format_resource_debug_appendix({}), "")


if __name__ == "__main__":
    unittest.main()
