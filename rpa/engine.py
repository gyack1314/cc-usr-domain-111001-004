"""上报状态机。

关键纪律：
1. 提交请求超时/断连一律记为 SUBMIT_UNKNOWN，只能通过查询页核对，禁止自动重提；
2. 只有查询到“唯一一条内容相同”的回执才确认成功；多条或内容不同转人工冲突；
3. 查询暂时查不到保持 SUBMIT_UNKNOWN，等待下次核对，不触发第二次提交；
4. 只有门户明确未写入的拒绝（403/422，written=False）才允许修正后重试；
5. 人工补录的回执也必须回到门户查询页验证组织归属与内容。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from . import portal as portal_mod
from . import store as store_mod
from .model import Material
from .security import EvidenceStore

log = logging.getLogger("rpa.engine")


class Engine:
    def __init__(
        self,
        store: store_mod.TaskStore,
        portal_factory: Callable[[str], portal_mod.PortalClient],
        evidence: EvidenceStore,
        worker_id: str | None = None,
        reconcile_delay: float = 2.0,
    ):
        self.store = store
        self.portal_factory = portal_factory
        self.evidence = evidence
        self.worker_id = worker_id or "worker-" + uuid.uuid4().hex[:8]
        self.reconcile_delay = reconcile_delay

    # ---- 材料 ---------------------------------------------------------
    def _material(self, task: dict[str, Any]) -> Material:
        data = json.loads(task["material_json"])
        return Material(**data)

    def _portal(self, organization: str) -> portal_mod.PortalClient:
        return self.portal_factory(organization)

    def _evidence(self, task_key: str, kind: str, detail: dict[str, Any]):
        path = self.evidence.write(task_key, kind, detail)
        log.info("证据已留存 %s %s -> %s", task_key, kind, path)
        return path

    # ---- 单轮工作：先核对遗留，再提交新任务 ---------------------------
    def run_once(self) -> dict[str, int]:
        stats = {"reconciled": 0, "submitted": 0, "idle": 0}
        task = self.store.claim_reconcile(self.worker_id)
        if task is not None:
            self.reconcile(task["task_key"])
            stats["reconciled"] += 1
            return stats
        task = self.store.claim_submit(self.worker_id)
        if task is None:
            stats["idle"] += 1
            return stats
        key = task["task_key"]
        self.submit(key)
        stats["submitted"] += 1
        # 提交后若处于待核实（断线/超时），同轮立即查询核对一次；
        # 查询延迟时保持 SUBMIT_UNKNOWN，绝不再发起第二次提交。
        refreshed = self.store.get_task(key)
        if refreshed and refreshed["status"] == store_mod.SUBMIT_UNKNOWN:
            self.reconcile(key)
            stats["reconciled"] += 1
        return stats

    def run_until_idle(self, max_rounds: int = 100,
                       busy_wait_seconds: float = 0.25) -> dict[str, int]:
        totals = {"reconciled": 0, "submitted": 0, "idle": 0, "rounds": 0}
        import time
        for _ in range(max_rounds):
            stats = self.run_once()
            totals["rounds"] += 1
            for key in ("reconciled", "submitted", "idle"):
                totals[key] += stats[key]
            if stats["idle"]:
                # 没有可立即领取的任务。若仍有在途/待核实任务（可能属于其他
                # 进程），等待其租约过期后接管核对，避免遗留悬空状态。
                if not self._has_guarded_work():
                    break
                if busy_wait_seconds:
                    time.sleep(busy_wait_seconds)
        return totals

    def _has_guarded_work(self) -> bool:
        for task in self.store.list_tasks():
            if task["status"] in (store_mod.SUBMITTING,
                                  store_mod.SUBMIT_UNKNOWN):
                return True
        return False

    # ---- 提交 ---------------------------------------------------------
    def submit(self, task_key: str) -> str:
        task = self.store.get_task(task_key)
        if task is None:
            raise KeyError(task_key)
        # 存储层之外再加一道防线：待核实/已确认任务绝不允许重新提交。
        if task["status"] in store_mod.SUBMIT_GUARDED_STATES:
            log.warning("任务 %s 处于 %s，跳过提交以防重复上报",
                        task_key, task["status"])
            return task["status"]
        material = self._material(task)
        fields = material.form_fields()
        portal = self._portal(material.organization)
        try:
            receipt = portal.submit_with_session(fields)
        except portal_mod.AmbiguousSubmission as exc:
            reason = str(exc)
            self.store.mark_unknown(task_key, self.worker_id, reason)
            path = self._evidence(task_key, "submit_ambiguous", {
                "outcome": "SUBMIT_UNKNOWN",
                "reason": reason,
                "fields": fields,
                "natural_key": material.key,
                "content_hash": material.content_digest,
            })
            self.store.record_attempt(
                task_key, "submit", "ambiguous",
                {"reason": reason}, evidence_path=path)
            log.warning("任务 %s 提交结果不明，转待核实，禁止自动重提", task_key)
            return store_mod.SUBMIT_UNKNOWN
        except (portal_mod.CSRFTokenInvalid, portal_mod.SessionExpired,
                portal_mod.LoginFailed, portal_mod.BusinessRejected) as exc:
            # 门户明确拒绝且未写入：登记 REJECTED，等待人工修正后放行。
            reason = f"{type(exc).__name__}: {exc}"
            self.store.mark_terminal(
                task_key, self.worker_id, store_mod.REJECTED,
                reason=reason)
            path = self._evidence(task_key, "submit_rejected", {
                "outcome": "REJECTED",
                "error_type": type(exc).__name__,
                "status": exc.status,
                "payload": exc.payload,
                "fields": fields,
            })
            self.store.record_attempt(
                task_key, "submit", "rejected",
                {"reason": reason, "status": exc.status},
                evidence_path=path)
            log.warning("任务 %s 被门户明确拒绝（未写入）: %s", task_key, reason)
            return store_mod.REJECTED
        except portal_mod.PortalError as exc:
            # 其他门户异常不假定写入与否：保守转待核实，由查询页裁决。
            reason = f"{type(exc).__name__}: {exc}"
            self.store.mark_unknown(task_key, self.worker_id, reason)
            self._evidence(task_key, "submit_unknown_error", {
                "outcome": "SUBMIT_UNKNOWN", "reason": reason,
                "fields": fields})
            self.store.record_attempt(
                task_key, "submit", "unknown", {"reason": reason})
            log.warning("任务 %s 出现未分类门户异常，转待核实: %s",
                        task_key, reason)
            return store_mod.SUBMIT_UNKNOWN

        # 明确收到完整回执
        self.store.mark_terminal(
            task_key, self.worker_id, store_mod.CONFIRMED,
            reason="提交页直接返回完整回执",
            receipt=str(receipt.get("receipt")))
        path = self._evidence(task_key, "submit_confirmed", {
            "outcome": "CONFIRMED", "receipt": receipt, "fields": fields,
        })
        self.store.record_attempt(
            task_key, "submit", "confirmed",
            {"receipt": receipt}, evidence_path=path)
        log.info("任务 %s 提交成功，回执 %s",
                 task_key, receipt.get("receipt"))
        return store_mod.CONFIRMED

    # ---- 查询核对 -----------------------------------------------------
    def reconcile(self, task_key: str) -> str:
        task = self.store.get_task(task_key)
        if task is None:
            raise KeyError(task_key)
        material = self._material(task)
        portal = self._portal(material.organization)
        filters = {
            "organization": material.organization,
            "activity_id": material.activity_id,
        }
        try:
            receipts = portal.query_with_session(filters)
        except portal_mod.PortalError as exc:
            reason = f"查询页暂不可用: {type(exc).__name__}: {exc}"
            self.store.release(task_key, self.worker_id,
                               store_mod.SUBMIT_UNKNOWN)
            self.store.record_attempt(
                task_key, "reconcile", "query_failed", {"reason": reason})
            log.warning("任务 %s 查询失败，保持待核实: %s", task_key, reason)
            return store_mod.SUBMIT_UNKNOWN

        same, different = _classify_receipts(receipts, material)
        detail = {
            "query_filters": filters,
            "visible_receipts": receipts,
            "same_content_receipts": same,
            "different_content_receipts": different,
            "expected_content_hash": material.content_digest,
            "expected_summary_hash": material.summary_digest,
        }
        if len(same) == 1 and not different:
            receipt_id = str(same[0].get("receipt"))
            self.store.mark_terminal(
                task_key, self.worker_id, store_mod.CONFIRMED,
                reason="查询页核对：唯一一条内容相同的回执",
                receipt=receipt_id)
            path = self._evidence(task_key, "reconcile_confirmed",
                                  {"outcome": "CONFIRMED", **detail})
            self.store.record_attempt(
                task_key, "reconcile", "confirmed",
                {"receipt": receipt_id}, evidence_path=path)
            log.info("任务 %s 核对成功，回执 %s", task_key, receipt_id)
            return store_mod.CONFIRMED

        if len(same) > 1 or different:
            reason = _conflict_reason(len(same), len(different))
            self.store.mark_terminal(
                task_key, self.worker_id, store_mod.CONFLICT,
                reason=reason,
                receipt=",".join(str(r.get("receipt")) for r in receipts))
            path = self._evidence(task_key, "reconcile_conflict",
                                  {"outcome": "CONFLICT", "reason": reason,
                                   **detail})
            self.store.record_attempt(
                task_key, "reconcile", "conflict",
                {"reason": reason, "receipts": receipts},
                evidence_path=path)
            log.warning("任务 %s 转人工冲突: %s", task_key, reason)
            return store_mod.CONFLICT

        # 查询暂时查不到：保持待核实，绝不自动再提交。
        reason = "查询页暂未检索到回执，保持待核实，禁止自动重提"
        self.store.release(task_key, self.worker_id,
                           store_mod.SUBMIT_UNKNOWN)
        path = self._evidence(task_key, "reconcile_pending",
                              {"outcome": "SUBMIT_UNKNOWN", **detail})
        self.store.record_attempt(
            task_key, "reconcile", "not_found_yet",
            {"reason": reason}, evidence_path=path)
        log.info("任务 %s 暂未查到回执，继续待核实", task_key)
        return store_mod.SUBMIT_UNKNOWN

    # ---- 人工补录回执的门户验证 --------------------------------------
    def verify_manual_receipt(self, task_key: str, receipt_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_key)
        if task is None:
            raise KeyError(task_key)
        material = self._material(task)
        portal = self._portal(material.organization)
        result: dict[str, Any] = {
            "task_key": task_key,
            "receipt_input": receipt_id,
            "organization": material.organization,
        }
        try:
            receipts = portal.query_with_session({
                "organization": material.organization,
                "activity_id": material.activity_id,
            })
        except portal_mod.PortalError as exc:
            result.update(result_code="QUERY_FAILED",
                          result="查询页不可用，无法验证补录回执",
                          detail=str(exc))
            self._evidence(task_key, "manual_receipt_unverified", result)
            return result

        target = [r for r in receipts if str(r.get("receipt")) == str(receipt_id)]
        if not target:
            result.update(
                result_code="NOT_VISIBLE",
                result="本账号在查询页看不到该回执，拒绝确认（可能属其他组织）",
                visible_receipts=receipts,
            )
            self._evidence(task_key, "manual_receipt_rejected", result)
            return result

        same, different = _classify_receipts(target, material)
        if len(same) == 1 and not different:
            self.store.attach_receipt_manual(
                task_key, str(receipt_id),
                {"result": "回执在门户可见且内容相同",
                 "organization": material.organization})
            result.update(
                result_code="VERIFIED",
                result="回执在门户可见，组织归属与内容均一致",
                receipt=target[0])
            self._evidence(task_key, "manual_receipt_verified", result)
            self.store.record_attempt(
                task_key, "manual_verify", "verified", result)
            return result

        result.update(
            result_code="CONTENT_MISMATCH",
            result="回执可见但内容与待报资料不一致，拒绝确认",
            receipt=target[0],
            expected_content_hash=material.content_digest,
        )
        self._evidence(task_key, "manual_receipt_rejected", result)
        self.store.record_attempt(
            task_key, "manual_verify", "rejected", result)
        return result


def _classify_receipts(receipts: list[dict[str, Any]],
                       material: Material):
    """按门户回执中的 summary_hash 与本地内容哈希判同。"""
    same, different = [], []
    for receipt in receipts:
        digest_match = receipt.get("summary_hash") == material.summary_digest
        field_match = (
            str(receipt.get("organization")) == material.organization
            and str(receipt.get("activity_id")) == material.activity_id
            and str(receipt.get("date")) == material.date
            and _int_eq(receipt.get("participants"), material.participants)
        )
        if digest_match and field_match:
            same.append(receipt)
        else:
            different.append(receipt)
    return same, different


def _int_eq(value, expected: int) -> bool:
    try:
        return int(value) == expected
    except (TypeError, ValueError):
        return False


def _conflict_reason(same_count: int, different_count: int) -> str:
    parts = []
    if same_count > 1:
        parts.append(f"存在 {same_count} 条内容相同的回执")
    if different_count:
        parts.append(f"存在 {different_count} 条内容不同的回执")
    return "人工冲突：" + "；".join(parts)
