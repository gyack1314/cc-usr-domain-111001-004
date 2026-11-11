"""本地任务存储：领取、租约与重启恢复。

多进程共享同一个 SQLite 文件（WAL + BEGIN IMMEDIATE），保证同一条待报资料
同一时刻只有一个工作进程能越过领取点。任务一旦进入 SUBMITTING / SUBMIT_UNKNOWN，
任何过期租约都只能被“核对领取”，不能被重新提交，从存储层防止重复上报。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# 逐项状态
PENDING = "PENDING"                # 待提交
SUBMITTING = "SUBMITTING"          # 已领取、提交请求在途或结果未落地
SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"  # 提交结果不明，等待查询核对
CONFIRMED = "CONFIRMED"            # 唯一一条内容相同回执，确认成功
CONFLICT = "CONFLICT"              # 查询发现多条/内容不同，转人工
REJECTED = "REJECTED"              # 门户明确未写入的拒绝（可修正后重试）
NEEDS_ATTENTION = "NEEDS_ATTENTION"  # 其他需要人工处理的情况

TERMINAL_STATES = (CONFIRMED, CONFLICT, REJECTED, NEEDS_ATTENTION)
# 这些状态下绝不允许再次发起提交请求。
# 注意 SUBMITTING 不在其中：它是“已领取、提交在途”的正常工作状态，属主持有的
# 正是要执行提交；SUBMITTING 的孤儿租约只能走 claim_reconcile，不会被二次领取。
SUBMIT_GUARDED_STATES = (SUBMIT_UNKNOWN, CONFIRMED, CONFLICT)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStore:
    def __init__(self, path: str | Path, lease_seconds: float = 60.0,
                 now: Callable[[], datetime] = utcnow):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.now = now
        self._local = __import__("threading").local()
        self._init_schema()

    # ---- 连接与结构 ---------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path), timeout=10, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_key        TEXT PRIMARY KEY,
                organization    TEXT NOT NULL,
                activity_id     TEXT NOT NULL,
                material_json   TEXT NOT NULL,
                summary_hash    TEXT NOT NULL,
                content_hash    TEXT NOT NULL,
                status          TEXT NOT NULL,
                lease_owner     TEXT,
                lease_expires_at TEXT,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_receipt    TEXT,
                last_reason     TEXT,
                updated_at      TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key        TEXT NOT NULL,
                attempt_no      INTEGER NOT NULL,
                phase           TEXT NOT NULL,
                outcome         TEXT NOT NULL,
                detail_json     TEXT NOT NULL,
                evidence_path   TEXT,
                at              TEXT NOT NULL,
                FOREIGN KEY(task_key) REFERENCES tasks(task_key)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            """
        )

    # ---- 工具 ---------------------------------------------------------
    def _iso(self, moment: datetime | None = None) -> str:
        return (moment or self.now()).isoformat()

    def record_attempt(self, task_key: str, phase: str, outcome: str,
                       detail: dict[str, Any], evidence_path: str | Path | None = None) -> int:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM attempts "
                "WHERE task_key=?", (task_key,)
            ).fetchone()
            attempt_no = row["n"] + 1
            conn.execute(
                "INSERT INTO attempts(task_key, attempt_no, phase, outcome, "
                "detail_json, evidence_path, at) VALUES(?,?,?,?,?,?,?)",
                (task_key, attempt_no, phase, outcome,
                 json.dumps(detail, ensure_ascii=False, sort_keys=True),
                 str(evidence_path) if evidence_path else None, self._iso()),
            )
            conn.execute(
                "UPDATE tasks SET attempts=? WHERE task_key=?",
                (attempt_no, task_key),
            )
            conn.execute("COMMIT")
            return attempt_no
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- 资料导入 -----------------------------------------------------
    def upsert_material(self, material) -> tuple[str, bool]:
        """按自然键登记资料；已存在且内容变化时不覆盖，返回 (task_key, created)。"""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status, content_hash FROM tasks WHERE task_key=?",
                (material.key,),
            ).fetchone()
            now = self._iso()
            if row is None:
                conn.execute(
                    "INSERT INTO tasks(task_key, organization, activity_id, "
                    "material_json, summary_hash, content_hash, status, "
                    "attempts, updated_at, created_at) "
                    "VALUES(?,?,?,?,?,?,?,0,?,?)",
                    (material.key, material.organization, material.activity_id,
                     json.dumps(material.as_dict(), ensure_ascii=False),
                     material.summary_digest, material.content_digest,
                     PENDING, now, now),
                )
                conn.execute("COMMIT")
                return material.key, True
            if row["content_hash"] != material.content_digest:
                # 自然键相同但内容变化：保留原任务，另立人工处理，避免静默改写。
                conn.execute(
                    "UPDATE tasks SET last_reason=?, updated_at=? "
                    "WHERE task_key=?",
                    ("导入资料与已登记内容不一致，需人工确认自然键复用", now,
                     material.key),
                )
            conn.execute("COMMIT")
            return material.key, False
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- 领取 ---------------------------------------------------------
    def claim_submit(self, worker_id: str) -> dict[str, Any] | None:
        """原子领取一条可提交任务。SUBMITTING/SUBMIT_UNKNOWN 永不会被此方法领取。"""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status=? "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at LIMIT 1",
                (PENDING, self._iso()),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            expires = self.now() + timedelta(seconds=self.lease_seconds)
            conn.execute(
                "UPDATE tasks SET status=?, lease_owner=?, lease_expires_at=?, "
                "attempts=attempts+1, updated_at=? WHERE task_key=?",
                (SUBMITTING, worker_id, self._iso(expires), self._iso(),
                 row["task_key"]),
            )
            conn.execute("COMMIT")
            return self.get_task(row["task_key"])
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def claim_reconcile(self, worker_id: str) -> dict[str, Any] | None:
        """领取一条待核对任务（含租约过期的在途任务），只允许查询核对。"""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY updated_at LIMIT 1",
                (SUBMIT_UNKNOWN, SUBMITTING, self._iso()),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            expires = self.now() + timedelta(seconds=self.lease_seconds)
            # SUBMITTING 的孤儿租约只升级为 UNKNOWN，绝不退回 PENDING。
            conn.execute(
                "UPDATE tasks SET status=?, lease_owner=?, lease_expires_at=?, "
                "updated_at=? WHERE task_key=?",
                (SUBMIT_UNKNOWN, worker_id, self._iso(expires), self._iso(),
                 row["task_key"]),
            )
            conn.execute("COMMIT")
            return self.get_task(row["task_key"])
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def heartbeat(self, task_key: str, worker_id: str) -> bool:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT lease_owner FROM tasks WHERE task_key=?", (task_key,)
            ).fetchone()
            if row is None or row["lease_owner"] != worker_id:
                conn.execute("ROLLBACK")
                return False
            expires = self.now() + timedelta(seconds=self.lease_seconds)
            conn.execute(
                "UPDATE tasks SET lease_expires_at=?, updated_at=? WHERE task_key=?",
                (self._iso(expires), self._iso(), task_key),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def release(self, task_key: str, worker_id: str, to_status: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE tasks SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=? WHERE task_key=? AND lease_owner=?",
            (to_status, self._iso(), task_key, worker_id),
        )

    # ---- 状态迁移（均带属主校验，防止陈旧工作进程覆盖新状态）----------
    def _update_owned(self, task_key: str, worker_id: str, **fields) -> bool:
        conn = self._conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [self._iso(), task_key, worker_id]
        cur = conn.execute(
            f"UPDATE tasks SET {sets}, updated_at=? "
            "WHERE task_key=? AND lease_owner=?",
            params,
        )
        return cur.rowcount == 1

    def mark_unknown(self, task_key: str, worker_id: str, reason: str) -> None:
        # 保留租约：属主可立即自行核对；租约过期后其他进程只能接管核对，
        # 任务永远不会因此退回到可重新提交的状态。
        self._update_owned(
            task_key, worker_id, status=SUBMIT_UNKNOWN,
            last_reason=reason,
        )

    def mark_terminal(self, task_key: str, worker_id: str, status: str,
                      reason: str, receipt: str | None = None) -> bool:
        if status not in TERMINAL_STATES:
            raise ValueError(f"非终态: {status}")
        return self._update_owned(
            task_key, worker_id, status=status, lease_owner=None,
            lease_expires_at=None, last_reason=reason,
            last_receipt=receipt,
        )

    def attach_receipt_manual(self, task_key: str, receipt: str,
                              verification: dict[str, Any]) -> bool:
        """工作人员补录回执并通过门户验证后，登记 CONFIRMED。"""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_key=?", (task_key,)
            ).fetchone()
            if row is None or row["status"] == CONFIRMED:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "UPDATE tasks SET status=?, lease_owner=NULL, "
                "lease_expires_at=NULL, last_receipt=?, last_reason=?, "
                "updated_at=? WHERE task_key=?",
                (CONFIRMED, receipt,
                 "人工补录回执并已在门户验证: " + verification.get("result", ""),
                 self._iso(), task_key),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def reset_for_retry(self, task_key: str, reason: str = "资料已修正，人工放行重试") -> bool:
        """仅允许对门户明确拒绝（REJECTED）的任务修正后放行。"""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_key=?", (task_key,)
            ).fetchone()
            if row is None or row["status"] != REJECTED:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "UPDATE tasks SET status=?, lease_owner=NULL, "
                "lease_expires_at=NULL, last_reason=?, updated_at=? "
                "WHERE task_key=?",
                (PENDING, reason, self._iso(), task_key),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- 查询视图 -----------------------------------------------------
    def get_task(self, task_key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM tasks WHERE task_key=?", (task_key,)
        ).fetchone()
        return dict(row) if row else None

    def list_tasks(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM tasks ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_attempts(self, task_key: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM attempts WHERE task_key=? ORDER BY id", (task_key,)
        ).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        result = {s: 0 for s in (PENDING, SUBMITTING, SUBMIT_UNKNOWN,
                                  CONFIRMED, CONFLICT, REJECTED, NEEDS_ATTENTION)}
        for row in rows:
            result[row["status"]] = row["n"]
        return result

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
