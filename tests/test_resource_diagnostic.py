"""Resource diagnostic: provenance, no fake shell output lines."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.core import diagnostics as diag_mod
from app.core.diagnostics import (
    collect_resource_usage,
    diagnose_resource_usage,
    format_resource_diagnosis_thai,
    format_resource_debug_appendix,
)


class TestResourceDiagnostic(unittest.TestCase):
    def test_format_has_source_and_collected_at(self):
        d = {
            "collected_at": "2026-01-01 00:00:00 UTC",
            "source_used": "psutil",
            "server_metrics_status": "absent",
            "server_metrics_age_sec": None,
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
        self.assertIn("สุ่มตัวอย่างสั้น", txt)
        self.assertNotIn("ฉบับเต็ม", txt)

    def test_format_short_provenance_by_default(self):
        d = {
            "collected_at": "x",
            "source_used": "psutil",
            "server_metrics_status": "absent",
            "server_metrics_age_sec": None,
            "cpu_percent": 1.0,
            "process_cpu_percent": None,
            "ram_percent": 10.0,
            "ram_used_mb": 100,
            "ram_total_mb": 1000,
            "disk_percent": 20.0,
            "docker_raw": None,
            "docker_status": "skipped",
            "docker_reason": "",
            "server_metrics_row": None,
            "errors": [],
        }
        short = format_resource_diagnosis_thai(d, verbose_provenance=False)
        self.assertNotIn("ฉบับเต็ม", short)
        verbose = format_resource_diagnosis_thai(d, verbose_provenance=True)
        self.assertIn("ฉบับเต็ม", verbose)

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

    def test_collect_skips_docker_when_container_empty(self):
        async def _run():
            with patch.object(diag_mod.settings, "docker_stats_container", ""):
                out = await collect_resource_usage()
            self.assertEqual(out["docker_status"], "skipped")

        asyncio.run(_run())

    def test_stale_server_metrics_not_primary_for_values(self):
        async def _run():
            fake = {
                "server_metrics": {
                    "cpu_percent": 99.0,
                    "ram_percent": 50.0,
                    "ram_used_mb": 1,
                    "ram_total_mb": 2,
                    "disk_percent": 3.0,
                    "recorded_at": "2000-01-01 00:00:00",
                },
                "server_metrics_status": "stale",
                "server_metrics_age_sec": 999999.0,
                "psutil": {
                    "cpu_percent": 2.5,
                    "process_cpu_percent": 0.1,
                    "ram_percent": 25.0,
                    "ram_used_mb": 512,
                    "ram_total_mb": 2048,
                    "disk_percent": 41.0,
                },
                "docker_stats": None,
                "docker_status": "skipped",
                "docker_reason": "",
                "errors": [],
            }
            with patch("app.core.diagnostics.collect_resource_usage", AsyncMock(return_value=fake)):
                d = await diagnose_resource_usage()
            self.assertEqual(d["cpu_percent"], 2.5)
            self.assertEqual(d["source_used"], "psutil")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
