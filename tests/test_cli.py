"""命令行端到端测试。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from energy_ledger.cli import main

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = str(self.dir / "cli.db")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--db", self.db, *args])
        text = buffer.getvalue().strip()
        return code, json.loads(text) if text else {}

    def seed(self) -> None:
        self.run_cli("fetch", "meters", "--origin", "m", "--path", str(SAMPLES / "meters.csv"))
        self.run_cli("fetch", "production", "--origin", "p", "--path", str(SAMPLES / "production.csv"))
        self.run_cli("fetch", "factors", "--origin", "f", "--path", str(SAMPLES / "factors.csv"))
        self.run_cli("mapping", "add", "E-2", "L-A", "--from", "2026-09-01", "--to", "2026-12-31")
        self.run_cli("scope", "add", "L-A", "--from", "2026-09-01", "--to", "2026-12-31")

    def test_compute_and_publish_via_cli(self):
        self.seed()
        code, draft = self.run_cli("compute", "2026-09")
        self.assertEqual(code, 0)
        self.assertEqual(draft["comparable_lines"], ["L-A"])
        self.assertAlmostEqual(draft["ranking"][0]["converted_per_unit"], 8700 * 0.73 / 1000)

        code, published = self.run_cli("publish", "2026-09", "--note", "demo", "--allow-partial")
        self.assertEqual(code, 0)
        self.assertEqual(published["release"]["revision"], 1)

        code, exported = self.run_cli("release", "export", "2026-09")
        self.assertEqual(code, 0)
        self.assertEqual(exported["release"]["revision"], 1)
        self.assertTrue(exported["inputs"]["meter_observations"])

    def test_blocked_publish_returns_exit_code(self):
        self.seed()
        code, payload = self.run_cli("publish", "2026-09")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "NotReadyError")

    def test_trace_and_impact(self):
        self.seed()
        self.run_cli("publish", "2026-09", "--allow-partial")
        code, traced = self.run_cli("trace", "2026-09", "L-A")
        self.assertEqual(code, 0)
        self.assertEqual(traced["metric"]["status"], "ready")
        self.assertIn("E-2", traced["raw"]["meter_observations"])

        code, impact = self.run_cli("impact", "2026-09")
        self.assertEqual(code, 0)
        self.assertIn("changed", impact)

    def test_pages_flag_then_resume(self):
        code, first = self.run_cli(
            "fetch", "meters", "--origin", "m", "--path", str(SAMPLES / "meters.csv"),
            "--page-size", "2", "--pages", "1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(first["status"], "open")
        self.assertEqual(first["processed_pages"], 1)
        code, resumed = self.run_cli("resume", str(first["batch_id"]))
        self.assertEqual(code, 0)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["records_stored"], 3)

    def test_conflict_returns_exit_code(self):
        self.seed()
        # 同一读数号同版本改数值，应拒绝。
        fixed = self.dir / "fix.csv"
        fixed.write_text(
            "reading,meter,line,period,kwh,version\nR2,E-2,L-A,2026-09,1,v2\n",
            encoding="utf-8",
        )
        code, payload = self.run_cli("fetch", "meters", "--origin", "bad", "--path", str(fixed))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "ConflictError")


if __name__ == "__main__":
    unittest.main()
