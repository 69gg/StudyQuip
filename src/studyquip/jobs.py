"""持久化队列；所有提交都带认领令牌并使用数据库时钟。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Connection

from .db import ConflictError, Database, jobs_table

Mutation = Callable[[Connection], Any]
ACTIVE_STATUSES = ("queued", "running", "waiting_window", "waiting_review")
JOB_FAMILIES = (
    ("book_process", "book_index"),
    ("question_extract", "question_explain"),
)
JOB_RESOURCE_KINDS = {
    "book_process": "book",
    "book_index": "book",
    "page_recognize": "page",
    "suggestion_regenerate": "suggestion",
    "question_extract": "question",
    "question_explain": "question",
    "model_test": "model",
    "search": "search",
    "export_pdf": "export",
}
RESUMABLE_STATUSES = ("failed", "cancelled", "waiting_review")


def source_fingerprint(record: dict[str, Any], kind: str) -> str:
    keys = ("title", "subject_id", "text", "asset_ids") if kind == "book" else ("revision",)
    return hashlib.sha256(
        json.dumps({key: record.get(key) for key in keys}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class LeaseLost(RuntimeError):
    """执行尝试已取消、过期或被新的执行者替代。"""


class JobStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def now(conn: Connection) -> float:
        return float(conn.scalar(select(func.unixepoch("subsec"))))

    def active_for(
        self, kind: str, resource_id: str, conn: Connection, exclude_id: str | None = None
    ) -> dict[str, Any] | None:
        family = next((group for group in JOB_FAMILIES if kind in group), (kind,))
        query = select(jobs_table).where(
            jobs_table.c.kind.in_(family),
            jobs_table.c.resource_id == resource_id,
            jobs_table.c.status.in_(ACTIVE_STATUSES),
        )
        if exclude_id:
            query = query.where(jobs_table.c.id != exclude_id)
        row = conn.execute(query.order_by(jobs_table.c.created_at)).mappings().first()
        return dict(row) if row else None

    def request(
        self, kind: str, specification: dict[str, Any], create: Mutation, *, conn: Connection | None = None
    ) -> dict[str, Any]:
        """同一导出或检索请求的资源创建和认领键在同一写事务中提交。"""
        if conn is None:
            with self.db.write() as active:
                return self.request(kind, specification, create, conn=active)
        key = hashlib.sha256(
            json.dumps(specification, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        row = (
            conn.execute(
                select(jobs_table).where(
                    jobs_table.c.kind == kind,
                    jobs_table.c.status.in_(ACTIVE_STATUSES),
                    jobs_table.c.payload["request_key"].as_string() == key,
                )
            )
            .mappings()
            .first()
        )
        if row:
            return {**dict(row), "reused": True}
        resource = create(conn)
        return self.enqueue(kind, resource["id"], {"request_key": key}, conn=conn)

    def configuration_changed(self, conn: Connection) -> None:
        """窗口等待来自一次已到期执行；重新评估窗口，不提前用户预约的 queued 任务。"""
        now = self.now(conn)
        conn.execute(
            jobs_table.update()
            .where(jobs_table.c.status == "waiting_window")
            .values(
                not_before=now,
                error="模型配置已更新，等待重新检查可用时段",
                updated_at=now,
            )
        )

    def enqueue(
        self,
        kind: str,
        resource_id: str,
        payload: dict[str, Any] | None = None,
        not_before: float | None = None,
        bypass_window: bool = False,
        *,
        conn: Connection | None = None,
        predecessor_id: str | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.enqueue(
                    kind,
                    resource_id,
                    payload,
                    not_before,
                    bypass_window,
                    conn=active,
                    predecessor_id=predecessor_id,
                )
        if predecessor_id:
            predecessor = self.get(predecessor_id, conn)
            if not (
                kind == "book_index"
                and predecessor
                and predecessor["kind"] == "book_process"
                and predecessor["resource_id"] == resource_id
                and predecessor["status"] == "running"
            ):
                raise ValueError("无效的教材后续任务")
        existing = self.active_for(kind, resource_id, conn, predecessor_id)
        if existing:
            if existing["kind"] == kind:
                return {**existing, "reused": True}
            raise ConflictError("该内容已有未结束的处理任务，请在任务页查看、继续或取消")
        now = self.now(conn)
        job = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "resource_id": resource_id,
            "payload": payload or {},
            "checkpoint": {},
            "status": "queued",
            "not_before": not_before if not_before is not None else now,
            "bypass_window": int(bypass_window),
            "owner": None,
            "lease_token": None,
            "lease_until": None,
            "attempts": 0,
            "error": None,
            "result": None,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(jobs_table.insert().values(**job))
        return job

    def get(self, id: str, conn: Connection | None = None) -> dict[str, Any] | None:
        if conn is None:
            with self.db.read() as active:
                return self.get(id, active)
        row = conn.execute(select(jobs_table).where(jobs_table.c.id == id)).mappings().first()
        return dict(row) if row else None

    def list(
        self, limit: int = 100, *, conn: Connection | None = None, include_active: bool = False
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.db.read() as active:
                return self.list(limit, conn=active, include_active=include_active)
        query = select(jobs_table).order_by(jobs_table.c.created_at.desc())
        if include_active:
            recent = select(jobs_table.c.id).order_by(jobs_table.c.created_at.desc()).limit(limit)
            query = query.where(or_(jobs_table.c.status.in_(ACTIVE_STATUSES), jobs_table.c.id.in_(recent)))
        else:
            query = query.limit(limit)
        return [dict(row) for row in conn.execute(query).mappings()]

    def claim(self, owner: str) -> dict[str, Any] | None:
        with self.db.write() as conn:
            now = self.now(conn)
            eligible = or_(
                and_(jobs_table.c.status.in_(["queued", "waiting_window"]), jobs_table.c.not_before <= now),
                and_(jobs_table.c.status == "running", jobs_table.c.lease_until <= now),
            )
            candidates = conn.execute(
                select(jobs_table).where(eligible).order_by(jobs_table.c.not_before, jobs_table.c.created_at)
            ).mappings()
            row: dict[str, Any] | None = None
            for candidate in candidates:
                dependency_ids = candidate["payload"].get("depends_on", [])
                dependencies = [self.get(dep, conn) for dep in dependency_ids]
                if any(dep is None or dep["status"] in {"failed", "cancelled"} for dep in dependencies):
                    conn.execute(
                        jobs_table.update()
                        .where(jobs_table.c.id == candidate["id"])
                        .values(
                            status="failed",
                            error="依赖任务不存在、失败或已取消，请修复依赖后重试",
                            updated_at=now,
                        )
                    )
                    continue
                if all(dep and dep["status"] == "completed" for dep in dependencies):
                    row = dict(candidate)
                    break
            if row is None:
                return None
            values = {
                "status": "running",
                "owner": owner,
                "lease_token": str(uuid.uuid4()),
                "lease_until": now + self.db.settings.lease_seconds,
                "updated_at": now,
                "attempts": row["attempts"] + 1,
                "error": None,
            }
            conn.execute(jobs_table.update().where(jobs_table.c.id == row["id"]).values(**values))
            return {**row, **values}

    def assert_lease(self, conn: Connection, id: str, owner: str, token: str) -> dict[str, Any]:
        row = self.get(id, conn)
        if (
            not row
            or row["status"] != "running"
            or row["owner"] != owner
            or row["lease_token"] != token
            or (row["lease_until"] or 0) <= self.now(conn)
        ):
            raise LeaseLost("任务租约已失效，旧结果不能提交")
        return row

    def renew(self, id: str, owner: str, token: str) -> bool:
        with self.db.write() as conn:
            now = self.now(conn)
            result = conn.execute(
                jobs_table.update()
                .where(
                    jobs_table.c.id == id,
                    jobs_table.c.owner == owner,
                    jobs_table.c.lease_token == token,
                    jobs_table.c.status == "running",
                    jobs_table.c.lease_until > now,
                )
                .values(lease_until=now + self.db.settings.lease_seconds, updated_at=now)
            )
            return result.rowcount == 1

    def checkpoint(
        self, id: str, owner: str, token: str, data: dict[str, Any], mutate: Mutation | None = None
    ) -> dict[str, Any]:
        with self.db.write() as conn:
            row = self.assert_lease(conn, id, owner, token)
            if mutate:
                mutate(conn)
            self.assert_lease(conn, id, owner, token)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == id)
                .values(checkpoint={**row["checkpoint"], **data}, updated_at=self.now(conn))
            )
            return self.get(id, conn) or {}

    def finish(
        self,
        id: str,
        owner: str,
        token: str,
        result: dict[str, Any] | None = None,
        mutate: Mutation | None = None,
    ) -> dict[str, Any]:
        with self.db.write() as conn:
            self.assert_lease(conn, id, owner, token)
            if mutate:
                mutate(conn)
            self.assert_lease(conn, id, owner, token)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == id)
                .values(
                    status="completed",
                    result=result or {},
                    lease_until=None,
                    owner=None,
                    lease_token=None,
                    updated_at=self.now(conn),
                )
            )
            return self.get(id, conn) or {}

    def fail(
        self, id: str, owner: str, token: str, error: str, *, retry_at: float | None = None
    ) -> dict[str, Any]:
        with self.db.write() as conn:
            self.assert_lease(conn, id, owner, token)
            changes: dict[str, Any] = {
                "status": "queued" if retry_at is not None else "failed",
                "error": error,
                "lease_until": None,
                "owner": None,
                "lease_token": None,
                "updated_at": self.now(conn),
            }
            if retry_at is not None:
                changes["not_before"] = retry_at
            conn.execute(jobs_table.update().where(jobs_table.c.id == id).values(**changes))
            return self.get(id, conn) or {}

    def defer(self, id: str, owner: str, token: str, until: float, reason: str) -> dict[str, Any]:
        with self.db.write() as conn:
            self.assert_lease(conn, id, owner, token)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == id)
                .values(
                    status="waiting_window",
                    not_before=until,
                    error=reason,
                    owner=None,
                    lease_token=None,
                    lease_until=None,
                    updated_at=self.now(conn),
                )
            )
            return self.get(id, conn) or {}

    def wait_for_review(self, id: str, owner: str, token: str, reason: str) -> dict[str, Any]:
        with self.db.write() as conn:
            self.assert_lease(conn, id, owner, token)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == id)
                .values(
                    status="waiting_review",
                    error=reason,
                    owner=None,
                    lease_token=None,
                    lease_until=None,
                    updated_at=self.now(conn),
                )
            )
            return self.get(id, conn) or {}

    def wake_book(self, book_id: str, *, conn: Connection | None = None) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.wake_book(book_id, conn=active)
        existing = [
            dict(row)
            for row in conn.execute(
                select(jobs_table)
                .where(
                    jobs_table.c.kind == "book_process",
                    jobs_table.c.resource_id == book_id,
                    jobs_table.c.status.in_(["queued", "running", "waiting_window", "waiting_review"]),
                )
                .order_by(jobs_table.c.created_at)
            ).mappings()
        ]
        active_job = next((job for job in existing if job["status"] != "waiting_review"), None)
        if active_job:
            return active_job
        if existing:
            selected = existing[0]
            now = self.now(conn)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == selected["id"])
                .values(
                    status="queued",
                    not_before=now,
                    error=None,
                    owner=None,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            return self.get(selected["id"], conn) or {}
        return self.enqueue("book_process", book_id, conn=conn)

    def cancel(self, id: str, *, conn: Connection | None = None) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.cancel(id, conn=active)
        row = self.get(id, conn)
        if not row:
            raise ValueError("任务不存在")
        if row["status"] not in {"completed", "cancelled"}:
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == id)
                .values(
                    status="cancelled",
                    lease_until=None,
                    owner=None,
                    lease_token=None,
                    updated_at=self.now(conn),
                )
            )
        return self.get(id, conn) or {}

    def resume_waiting(self, kind: str, resource_id: str, *, conn: Connection | None = None) -> None:
        if conn is None:
            with self.db.write() as active:
                self.resume_waiting(kind, resource_id, conn=active)
            return
        conn.execute(
            jobs_table.update()
            .where(
                jobs_table.c.kind == kind,
                jobs_table.c.resource_id == resource_id,
                jobs_table.c.status == "waiting_review",
            )
            .values(status="queued", not_before=self.now(conn), error=None, updated_at=self.now(conn))
        )

    def resume_problem(self, row: dict[str, Any], conn: Connection) -> str | None:
        """检查恢复前提；执行和提交时仍须再次校验版本与租约。"""
        if self.active_for(row["kind"], row["resource_id"], conn, row["id"]):
            return "已有同一内容的处理任务，不能重复恢复旧任务"
        kind = JOB_RESOURCE_KINDS.get(row["kind"])
        if kind:
            source = self.db.get(kind, row["resource_id"], conn)
            if not source or source.get("deleted"):
                return "任务来源已删除，不能继续处理"
            if source.get("book_id"):
                book = self.db.get("book", source["book_id"], conn)
                if not book or book.get("deleted"):
                    return "教材已删除，不能继续处理"
            expected = (row.get("checkpoint") or {}).get("source_fingerprint")
            if expected and expected != source_fingerprint(source, kind):
                return "任务输入已修改，请为当前版本重新发起处理；旧检查点保留"
        return None

    def resume(self, id: str, not_before: float, bypass_window: bool = False) -> dict[str, Any]:
        return self.reschedule(id, not_before, bypass_window, resume=True)

    def reschedule(
        self, id: str, not_before: float, bypass_window: bool = False, *, resume: bool = False
    ) -> dict[str, Any]:
        with self.db.write() as conn:
            row = self.get(id, conn)
            if not row:
                raise ValueError("任务不存在")
            if resume and row["status"] in {"queued", "running", "waiting_window"}:
                # Double clicks and HTTP retries keep the first submission's schedule and lease.
                return {**row, "reused": True}
            if resume and row["status"] not in RESUMABLE_STATUSES:
                raise ConflictError("该任务已结束，不能继续处理")
            if row["status"] in {"running", "completed"}:
                raise ValueError("运行中或已完成的任务不能改期；请先取消或创建新任务")
            problem = self.resume_problem(row, conn)
            if problem:
                raise ConflictError(problem)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == id)
                .values(
                    status="queued",
                    not_before=not_before,
                    bypass_window=int(bypass_window),
                    error=None,
                    owner=None,
                    lease_token=None,
                    lease_until=None,
                    updated_at=self.now(conn),
                )
            )
            return self.get(id, conn) or {}
