"""SQLite 任务存储：原子领取、租约过期、重启恢复与防重提交。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rpa import store as S
from rpa.model import Material


def mat(activity="DR-1", org="ORG-A"):
    return Material(org, activity, "2026-11-09", 10, "合成摘要")


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 11, 11, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.db = Path(self.tmp.name) / "tasks.sqlite3"
        self.store = S.TaskStore(self.db, lease_seconds=30, now=self.clock)

    def test_claim_is_exclusive_between_workers(self):
        self.store.upsert_material(mat())
        first = self.store.claim_submit("w1")
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim_submit("w2"))

    def test_lease_expiry_allows_reconcile_but_never_resubmit(self):
        self.store.upsert_material(mat())
        claimed = self.store.claim_submit("w1")
        self.assertEqual(S.SUBMITTING, claimed["status"])
        self.clock.advance(31)

        # 过期租约不能作为新任务被重新提交
        self.assertIsNone(self.store.claim_submit("w2"))
        # 只能被核对领取，且升级为 SUBMIT_UNKNOWN
        recovered = self.store.claim_reconcile("w2")
        self.assertEqual(S.SUBMIT_UNKNOWN, recovered["status"])
        self.assertEqual("w2", recovered["lease_owner"])
        # 仍然不可提交
        self.assertIsNone(self.store.claim_submit("w2"))

    def test_unknown_task_reclaimed_only_for_reconcile(self):
        self.store.upsert_material(mat())
        task = self.store.claim_submit("w1")
        self.store.mark_unknown(task["task_key"], "w1", "断线")
        self.assertIsNone(self.store.claim_submit("w2"))
        # 租约未过期前其他进程不能接管；过期后只能按“核对”领取
        self.assertIsNone(self.store.claim_reconcile("w2"))
        self.clock.advance(31)
        rec = self.store.claim_reconcile("w2")
        self.assertEqual(task["task_key"], rec["task_key"])
        self.assertEqual(S.SUBMIT_UNKNOWN, rec["status"])

    def test_owned_update_ignores_stale_worker(self):
        self.store.upsert_material(mat())
        task = self.store.claim_submit("w1")
        key = task["task_key"]
        # w2 不是属主，迁移不生效
        self.assertFalse(self.store.mark_terminal(key, "w2", S.REJECTED, "x"))
        self.assertTrue(self.store.mark_terminal(key, "w1", S.REJECTED, "x"))
        self.assertEqual(S.REJECTED, self.store.get_task(key)["status"])

    def test_mark_unknown_keeps_lease_for_owner_reconcile(self):
        self.store.upsert_material(mat())
        task = self.store.claim_submit("w1")
        key = task["task_key"]
        self.store.mark_unknown(key, "w1", "断线")
        row = self.store.get_task(key)
        self.assertEqual(S.SUBMIT_UNKNOWN, row["status"])
        self.assertEqual("w1", row["lease_owner"])
        self.assertIsNotNone(row["lease_expires_at"])

    def test_reset_only_allowed_for_rejected(self):
        self.store.upsert_material(mat())
        key = self.store.upsert_material(mat())[0]
        self.assertFalse(self.store.reset_for_retry(key))  # PENDING 不放行
        task = self.store.claim_submit("w1")
        self.store.mark_terminal(key, "w1", S.REJECTED, "422")
        self.assertTrue(self.store.reset_for_retry(key))
        self.assertEqual(S.PENDING, self.store.get_task(key)["status"])

    def test_manual_receipt_attach_requires_existing_task(self):
        self.assertFalse(
            self.store.attach_receipt_manual("nope", "R9", {"result": "x"}))

    def test_persistence_across_store_reopen(self):
        self.store.upsert_material(mat())
        task = self.store.claim_submit("w1")
        self.store.mark_unknown(task["task_key"], "w1", "断线")
        store2 = S.TaskStore(self.db, lease_seconds=30, now=self.clock)
        reopened = store2.get_task(task["task_key"])
        self.assertEqual(S.SUBMIT_UNKNOWN, reopened["status"])
        # 重启后的新进程依旧不能直接重提
        self.assertIsNone(store2.claim_submit("new-process"))

    def test_upsert_detects_content_change_on_same_natural_key(self):
        self.store.upsert_material(mat())
        changed = Material("ORG-A", "DR-1", "2026-11-10", 99, "不同摘要")
        key, created = self.store.upsert_material(changed)
        self.assertFalse(created)
        self.assertIn("不一致", self.store.get_task(key)["last_reason"])

    def test_attempts_are_append_only_evidence_index(self):
        key, _ = self.store.upsert_material(mat())
        n1 = self.store.record_attempt(key, "submit", "ambiguous", {"a": 1})
        n2 = self.store.record_attempt(key, "reconcile", "not_found", {})
        self.assertEqual((1, 2), (n1, n2))
        rows = self.store.list_attempts(key)
        self.assertEqual(["submit", "reconcile"], [r["phase"] for r in rows])


if __name__ == "__main__":
    unittest.main()
