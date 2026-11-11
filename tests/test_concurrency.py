"""并发领取测试：多个真实工作进程争抢同一条待报资料。

要求：同一条资料最终只产生一次提交、一份确认。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from source_site.__main__ import running

REPO_ROOT = Path(__file__).resolve().parents[1]


class ConcurrentClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cm = running()
        self.base, _ = self.cm.__enter__()
        self.addCleanup(self.cm.__exit__, None, None, None)
        root = Path(self.tmp.name)
        self.submissions = root / "submissions.json"
        self.submissions.write_text(json.dumps([{
            "organization": "ORG-A",
            "activity_id": "CONC-1",
            "date": "2026-11-11",
            "participants": 21,
            "summary": "并发领取演练",
        }]), encoding="utf-8")
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps({
            "portal_base_url": self.base,
            "accounts": [
                {"organization": "ORG-A", "account": "demo-org",
                 "password": "local-only"},
            ],
            "db_path": str(root / "tasks.sqlite3"),
            "evidence_dir": str(root / "evidence"),
            "lease_seconds": 1,
            "timeout_seconds": 3,
        }), encoding="utf-8")

    def _run_worker(self, worker_id):
        return subprocess.Popen(
            [sys.executable, "-I", str(REPO_ROOT / "run_rpa.py"),
             "--config", str(self.config_path), "--worker-id", worker_id,
             "run", "--confirm", "--max-rounds", "20",
             str(self.submissions)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dict(os.environ),
        )

    def test_two_processes_one_submission(self):
        procs = [self._run_worker("proc-1"), self._run_worker("proc-2")]
        outputs = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=40)
            self.assertEqual(0, proc.returncode, stderr)
            outputs.append(stdout)

        # 最终任务必须确认成功；再通过查询页核对门户里只有一张回执
        import time
        import urllib.request
        deadline = time.monotonic() + 10
        status_text = ""
        while time.monotonic() < deadline:
            status = subprocess.run(
                [sys.executable, "-I", str(REPO_ROOT / "run_rpa.py"),
                 "--config", str(self.config_path), "status"],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                timeout=30, check=False,
            )
            status_text = status.stdout
            if "CONFIRMED" in status_text:
                break
            time.sleep(0.2)
        self.assertIn("CONFIRMED", status_text, outputs)
        self.assertEqual(1, status_text.count("CONC-1"))

        # 门户查询页对该自然键只返回一条回执，证明没有重复上报
        login = urllib.request.Request(
            self.base + "/login",
            data=b"account=demo-org&password=local-only",
            method="POST")
        with urllib.request.urlopen(login, timeout=5) as resp:
            cookie = resp.headers["Set-Cookie"].split(";")[0]
        q = (self.base + "/query?organization=ORG-A&activity_id=CONC-1")
        req = urllib.request.Request(q, headers={"Cookie": cookie})
        with urllib.request.urlopen(req, timeout=5) as resp:
            receipts = json.loads(resp.read().decode())["receipts"]
        self.assertEqual(1, len(receipts), outputs)
        self.assertEqual(1, len({r["receipt"] for r in receipts}))


if __name__ == "__main__":
    unittest.main()
