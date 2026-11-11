"""引擎状态机端到端测试：全部通过真实环回 HTTP 与门户交互。"""
import json
import tempfile
import unittest
from pathlib import Path

from rpa import portal as P
from rpa import store as S
from rpa.engine import Engine
from rpa.model import Material
from rpa.security import EvidenceStore
from source_site.__main__ import running

from helpers import control, count_submits


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cm = running()
        self.base, self.source = self.cm.__enter__()
        self.addCleanup(self.cm.__exit__, None, None, None)
        root = Path(self.tmp.name)
        self.store = S.TaskStore(root / "tasks.sqlite3", lease_seconds=0.2)
        self.evidence = EvidenceStore(root / "evidence")
        self.engine = Engine(self.store, self._portal_factory, self.evidence,
                             worker_id="test-worker")

    def _portal_factory(self, organization):
        creds = {
            "ORG-A": ("demo-org", "local-only"),
            "ORG-B": ("demo-org2", "local-only-2"),
        }
        account, password = creds[organization]
        return P.PortalClient(self.base, account=account, password=password,
                              timeout=3)

    def register(self, material):
        key, _ = self.store.upsert_material(material)
        return key

    def _wait_for_lease_expiry(self):
        import time
        time.sleep(0.3)

    def evidence_text(self):
        return "\n".join(
            p.read_text(encoding="utf-8")
            for p in Path(self.tmp.name).rglob("*.json")
        )


class HappyPathTests(EngineTestBase):
    def test_full_flow_confirms_with_receipt(self):
        key = self.register(Material("ORG-A", "DR-1", "2026-11-09", 80, "社区应急疏散"))
        result = self.engine.run_once()
        self.assertEqual(1, result["submitted"])
        task = self.store.get_task(key)
        self.assertEqual(S.CONFIRMED, task["status"])
        self.assertEqual("R1", task["last_receipt"])
        self.assertEqual(1, count_submits(self.base))

    def test_second_organization_uses_its_own_account_and_isolation(self):
        from rpa import portal as Pmod
        key = self.register(
            Material("ORG-B", "DR-9", "2026-11-10", 45, "社区急救技能演练"))
        self.engine.run_once()
        self.assertEqual(S.CONFIRMED, self.store.get_task(key)["status"])
        # ORG-A 账号在查询页看不到 ORG-B 的回执（跨组织隔离）
        org_a = self._portal_factory("ORG-A")
        rows = org_a.query_with_session(
            {"organization": "ORG-B", "activity_id": "DR-9"})
        self.assertEqual([], rows)
        # ORG-B 自己能查到
        org_b = self._portal_factory("ORG-B")
        rows = org_b.query_with_session(
            {"organization": "ORG-B", "activity_id": "DR-9"})
        self.assertEqual(1, len(rows))


class AmbiguousSubmissionTests(EngineTestBase):
    def test_lost_response_then_reconcile_confirms_without_second_submit(self):
        key = self.register(Material("ORG-A", "DR-1", "2026-11-09", 80, "社区应急疏散"))
        control(self.base, "lost-response")
        task = self.store.claim_submit("test-worker")
        outcome = self.engine.submit(task["task_key"])  # 提交断线
        self.assertEqual(S.SUBMIT_UNKNOWN, outcome)
        self.assertEqual(S.SUBMIT_UNKNOWN, self.store.get_task(key)["status"])
        self.assertEqual(1, count_submits(self.base))

        # 另一个工作进程接手：租约过期后只能核对，不能重提
        other = Engine(self.store, self._portal_factory, self.evidence,
                       worker_id="recovery-worker")
        control(self.base, "base")
        self.assertIsNone(other.store.claim_submit("recovery-worker"))
        self._wait_for_lease_expiry()
        claimed = other.store.claim_reconcile("recovery-worker")
        self.assertEqual(key, claimed["task_key"])
        outcome = other.reconcile(key)

        self.assertEqual(S.CONFIRMED, outcome)
        self.assertEqual("R1", self.store.get_task(key)["last_receipt"])
        # 整个过程只有一次提交请求
        self.assertEqual(1, count_submits(self.base))

    def test_query_delay_keeps_unknown_then_confirms(self):
        key = self.register(Material("ORG-A", "DR-2", "2026-11-10", 32, "企业灭火演练"))
        control(self.base, "lost-response")
        self.engine.run_once()
        control(self.base, "query-delay")
        outcome = self.engine.reconcile(key)
        self.assertEqual(S.SUBMIT_UNKNOWN, outcome)
        self.assertEqual(1, count_submits(self.base))  # 没有自动重提

        control(self.base, "base")
        outcome = self.engine.reconcile(key)
        self.assertEqual(S.CONFIRMED, outcome)
        self.assertEqual(1, count_submits(self.base))

    def test_restart_during_unknown_never_resubmits(self):
        key = self.register(Material("ORG-A", "DR-1", "2026-11-09", 80, "社区应急疏散"))
        control(self.base, "lost-response")
        task = self.store.claim_submit("test-worker")
        self.engine.submit(task["task_key"])
        self.assertEqual(S.SUBMIT_UNKNOWN, self.store.get_task(key)["status"])

        # 模拟整体重启：全新引擎实例、新 worker id，门户仍处于断线阶段。
        # lost-response 只丢提交响应、查询仍可用，因此重启进程只能通过查询页
        # 核对（绝不重新提交），并直接确认已写入的回执。
        restarted = Engine(
            S.TaskStore(Path(self.tmp.name) / "tasks.sqlite3",
                        lease_seconds=0.2),
            self._portal_factory, self.evidence, worker_id="after-restart")
        self._wait_for_lease_expiry()
        totals = restarted.run_until_idle()
        self.assertEqual(S.CONFIRMED,
                         restarted.store.get_task(key)["status"])
        self.assertEqual(1, count_submits(self.base))
        self.assertGreaterEqual(totals["reconciled"], 1)

        # 即使门户之后才恢复，也不会再产生第二次提交
        control(self.base, "base")
        restarted.run_until_idle()
        self.assertEqual(S.CONFIRMED,
                         restarted.store.get_task(key)["status"])
        self.assertEqual(1, count_submits(self.base))


class RejectionTests(EngineTestBase):
    def test_business_rejection_requires_manual_fix(self):
        # ORG-B 资料使用 ORG-B 账号本可成功；这里改用 ORG-A 会话提交，
        # 门户返回 422（组织不合法、written=false），引擎登记 REJECTED 而非重试。
        rogue = Material("ORG-B", "DR-7", "2026-11-11", 5, "跨组织提交")
        rk = self.register(rogue)

        def org_a_factory(organization):
            return P.PortalClient(self.base, account="demo-org",
                                  password="local-only", timeout=3)

        engine_a = Engine(self.store, org_a_factory, self.evidence,
                          worker_id="org-a-worker")
        task = self.store.claim_submit("org-a-worker")
        engine_a.submit(task["task_key"])
        self.assertEqual(S.REJECTED, self.store.get_task(rk)["status"])
        self.assertEqual(1, count_submits(self.base))
        # REJECTED 不会被再次领取提交
        self.assertIsNone(self.store.claim_submit("anyone"))
        # 修正资料/账号后由人工放行
        self.assertTrue(self.store.reset_for_retry(rk))

    def test_expired_session_is_handled_then_succeeds(self):
        key = self.register(Material("ORG-A", "DR-3", "2026-11-11", 12, "会话过期演练"))
        control(self.base, "expired")
        # 引擎内部完成重新登录，最终成功
        self.engine.run_once()
        control(self.base, "base")
        self.assertEqual(S.CONFIRMED, self.store.get_task(key)["status"])


class ConflictTests(EngineTestBase):
    def test_duplicate_same_natural_key_becomes_manual_conflict(self):
        material = Material("ORG-A", "DR-5", "2026-11-09", 80, "社区应急疏散")
        key = self.register(material)
        # 门户里预置同一自然键的两条内容相同回执（模拟工作人员重按产生的重复）
        client = self._portal_factory("ORG-A")
        client.login()
        for _ in range(2):
            token = client.fetch_form()
            client.submit(material.form_fields(), token)
        self.assertEqual(2, count_submits(self.base))

        # 引擎端任务此前并未成功，现在进入核对：发现两条 -> 人工冲突
        self.store.claim_submit("test-worker")
        self.store.mark_unknown(key, "test-worker", "模拟断线")
        outcome = self.engine.reconcile(key)
        self.assertEqual(S.CONFLICT, outcome)
        self.assertIn("2 条", self.store.get_task(key)["last_reason"])

    def test_different_content_receipt_is_conflict(self):
        material = Material("ORG-A", "DR-6", "2026-11-09", 80, "当前摘要")
        key = self.register(material)
        other = Material("ORG-A", "DR-6", "2026-11-09", 80, "不同摘要内容")
        client = self._portal_factory("ORG-A")
        client.login()
        token = client.fetch_form()
        client.submit(other.form_fields(), token)

        self.store.claim_submit("test-worker")
        self.store.mark_unknown(key, "test-worker", "模拟断线")
        outcome = self.engine.reconcile(key)
        self.assertEqual(S.CONFLICT, outcome)


class ManualReceiptTests(EngineTestBase):
    def test_manual_receipt_verified_against_portal(self):
        material = Material("ORG-A", "DR-8", "2026-11-09", 7, "物资点验")
        key = self.register(material)
        client = self._portal_factory("ORG-A")
        client.login()
        token = client.fetch_form()
        receipt = client.submit(material.form_fields(), token)

        result = self.engine.verify_manual_receipt(key, receipt["receipt"])
        self.assertEqual("VERIFIED", result["result_code"])
        self.assertEqual(S.CONFIRMED, self.store.get_task(key)["status"])

    def test_manual_receipt_other_organization_rejected(self):
        material_a = Material("ORG-A", "DR-8", "2026-11-09", 7, "物资点验")
        key_a = self.register(material_a)
        # ORG-B 账号写入一条自己的回执
        material_b = Material("ORG-B", "DR-8", "2026-11-09", 7, "物资点验")
        client_b = self._portal_factory("ORG-B")
        client_b.login()
        token = client_b.fetch_form()
        receipt_b = client_b.submit(material_b.form_fields(), token)

        # 工作人员把 ORG-B 的回执号补录给 ORG-A 任务：查询页不可见，拒绝确认
        result = self.engine.verify_manual_receipt(key_a, receipt_b["receipt"])
        self.assertNotEqual("VERIFIED", result["result_code"])
        self.assertNotEqual(S.CONFIRMED, self.store.get_task(key_a)["status"])

    def test_arbitrary_string_is_not_accepted(self):
        key = self.register(Material("ORG-A", "DR-99", "2026-11-09", 1, "x"))
        result = self.engine.verify_manual_receipt(key, "R-FAKE-999")
        self.assertEqual("NOT_VISIBLE", result["result_code"])
        self.assertNotEqual(S.CONFIRMED, self.store.get_task(key)["status"])


class EvidenceTests(EngineTestBase):
    def test_evidence_and_redaction_never_leak_secrets(self):
        self.register(Material("ORG-A", "DR-1", "2026-11-09", 80, "社区应急疏散"))
        self.engine.run_once()
        text = self.evidence_text()
        self.assertIn("R1", text)
        self.assertNotIn("local-only", text)
        self.assertNotIn("synthetic-orga", text)
        self.assertNotIn("session=", text.replace("session=***", ""))


if __name__ == "__main__":
    unittest.main()
