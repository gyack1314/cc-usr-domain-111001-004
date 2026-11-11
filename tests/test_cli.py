"""CLI 与预览保护测试：预览阶段不得发起任何门户请求。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from source_site.__main__ import running

from helpers import count_submits

REPO_ROOT = Path(__file__).resolve().parents[1]


class CliPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cm = running()
        self.base, _ = self.cm.__enter__()
        self.addCleanup(self.cm.__exit__, None, None, None)
        root = Path(self.tmp.name)
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps({
            "portal_base_url": self.base,
            "accounts": [
                {"organization": "ORG-A", "account": "demo-org",
                 "password": "local-only"},
                {"organization": "ORG-B", "account": "demo-org2",
                 "password": "local-only-2"},
            ],
            "db_path": str(root / "tasks.sqlite3"),
            "evidence_dir": str(root / "evidence"),
            "timeout_seconds": 3,
            "lease_seconds": 30,
        }), encoding="utf-8")

    def _run(self, *args):
        env = dict(os.environ)
        return subprocess.run(
            [sys.executable, "-I", str(REPO_ROOT / "run_rpa.py"),
             "--config", str(self.config_path), *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
            env=env, check=False,
        )

    def test_preview_makes_zero_http_calls(self):
        proc = self._run("preview", str(REPO_ROOT / "sources" / "submissions.json"))
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(0, payload["portal_calls"])
        self.assertEqual(2, payload["task_count"])
        first = payload["tasks"][0]
        self.assertEqual("ORG-A|DR-1", first["natural_key"])
        self.assertIn("summary_hash", first["fields"])
        self.assertEqual(0, count_submits(self.base))

    def test_preview_reports_invalid_materials_without_http(self):
        proc = self._run(
            "preview", str(REPO_ROOT / "tests" / "fixtures" /
                           "invalid_submissions.json"))
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(0, payload["task_count"])
        self.assertEqual(2, len(payload["invalid"]))
        self.assertEqual(0, count_submits(self.base))

    def test_run_without_confirm_does_not_submit(self):
        proc = self._run("run", str(REPO_ROOT / "sources" / "submissions.json"))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("预览保护", proc.stdout)
        self.assertEqual(0, count_submits(self.base))

    def test_run_confirm_then_status_shows_receipts(self):
        proc = self._run("run", "--confirm",
                         str(REPO_ROOT / "sources" / "submissions.json"))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(2, count_submits(self.base))
        status = self._run("status")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("CONFIRMED", status.stdout)
        detail = self._run("status", "ORG-A|DR-1")
        body = json.loads(detail.stdout)
        self.assertEqual("R1", body["last_receipt"])
        self.assertGreaterEqual(len(body["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
