"""面向页面的任务投影；不返回工具上下文、原图或凭据。"""

from collections import Counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import Database
from .jobs import ACTIVE_STATUSES, JobStore


class JobProgress:
    def __init__(self, db: Database, conn: Connection) -> None:
        self.db, self.conn = db, conn
        self.now = JobStore.now(conn)
        self.books: dict[str, dict[str, Any]] = {}

    def book(self, book_id: str) -> dict[str, Any]:
        if book_id in self.books:
            return self.books[book_id]
        rows = (
            self.conn.execute(
                text("""SELECT id, json_extract(data,'$.index') AS position,
            json_extract(data,'$.status') AS status, updated_at FROM records
            WHERE kind='page' AND json_extract(data,'$.book_id')=:book"""),
                {"book": book_id},
            )
            .mappings()
            .all()
        )
        states = Counter(row["status"] for row in rows)
        counts = dict(
            self.conn.execute(
                text("""SELECT kind, count(*) FROM records
            WHERE kind IN ('block','node') AND json_extract(data,'$.book_id')=:book
            AND COALESCE(json_extract(data,'$.archived'),0)=0 GROUP BY kind"""),
                {"book": book_id},
            ).all()
        )
        summaries = self.conn.execute(
            text("""SELECT count(*) FROM records WHERE kind='node'
            AND json_extract(data,'$.book_id')=:book AND json_extract(data,'$.summary') != ''
            AND COALESCE(json_extract(data,'$.summary_stale'),0)=0
            AND COALESCE(json_extract(data,'$.archived'),0)=0"""),
            {"book": book_id},
        ).scalar_one()
        result = {
            "total_pages": len(rows),
            "recognized_pages": states["draft"] + states["processed"],
            "processed_pages": states["processed"],
            "review_pages": states["needs_review"],
            "skipped_pages": states["skipped"],
            "pending_pages": states["pending"],
            "blocks": counts.get("block", 0),
            "nodes": counts.get("node", 0),
            "summarized_nodes": summaries,
            "page_numbers": {row["id"]: (row["position"] or 0) + 1 for row in rows},
        }
        self.books[book_id] = result
        return result

    def present(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = {
            "book_process": "book",
            "book_index": "book",
            "page_recognize": "page",
            "suggestion_regenerate": "suggestion",
            "question_extract": "question",
            "question_explain": "question",
            "model_test": "model",
            "search": "search",
            "export_pdf": "export",
        }.get(job["kind"])
        source = self.db.get(kind, job["resource_id"], self.conn) if kind else None
        source = source or {}
        book_id = job["resource_id"] if kind == "book" else source.get("book_id")
        title = source.get("title") or source.get("name") or source.get("stem") or source.get("query")
        if book_id and kind != "book":
            title = (self.db.get("book", book_id, self.conn) or {}).get("title")
        checkpoint = job.get("checkpoint") or {}
        stages = checkpoint.get("stages", {})
        usage: Counter[str] = Counter()
        active: list[dict[str, Any]] = []
        book = self.book(book_id) if book_id else {}
        for key, state in stages.items():
            usage.update(state.get("usage", {}))
            if "result" not in state and state.get("activity"):
                parts = key.split(":")
                page = book.get("page_numbers", {}).get(parts[1]) if len(parts) > 1 else None
                active.append({**state["activity"], "page": page, "rounds": state.get("rounds", 0)})
        usage.update(checkpoint.get("embedding_usage", {}))
        usage.update(checkpoint.get("query_embedding_usage", {}))
        if not usage and isinstance(job.get("result"), dict) and job["result"].get("usage"):
            measured = job["result"]["usage"]
            usage.update(
                {
                    "requests": measured.get("requests", 1),
                    "input_tokens": measured.get("input_tokens", measured.get("prompt_tokens", 0)) or 0,
                    "output_tokens": measured.get("output_tokens", measured.get("completion_tokens", 0)) or 0,
                }
            )
        progress = {
            **{key: value for key, value in book.items() if key != "page_numbers"},
            **{
                key: checkpoint[key]
                for key in ("phase", "embedding_total", "embedding_completed", "current_page")
                if key in checkpoint
            },
            "completed_units": len(checkpoint.get("completed_units", [])),
            "completed_stages": sum("result" in state for state in stages.values()),
            "usage": dict(usage),
            "active_requests": active,
            "last_activity_at": checkpoint.get("last_activity_at", job["updated_at"]),
        }
        if checkpoint.get("embedding_activity"):
            progress["embedding_activity"] = checkpoint["embedding_activity"]
        recovering = job["status"] == "running" and (job.get("lease_until") or 0) <= self.now
        return {
            **{
                key: job.get(key)
                for key in (
                    "id",
                    "kind",
                    "resource_id",
                    "status",
                    "created_at",
                    "updated_at",
                    "not_before",
                    "error",
                    "result",
                    "attempts",
                    "bypass_window",
                    "reused",
                )
            },
            "resource_title": (str(title)[:120] if title else None),
            "book_id": book_id,
            "question_ids": source.get("question_ids", []) if kind == "export" else [],
            "blocking": job["status"] in ACTIVE_STATUSES,
            "recovering": recovering,
            "progress": progress,
            "waiting_reason": "执行进程已离线或租约过期，等待 worker 恢复；请勿重复提交"
            if recovering
            else None,
            "input": source if kind == "search" else None,
        }


def present_jobs(db: Database) -> list[dict[str, Any]]:
    with db.read() as conn:
        view = JobProgress(db, conn)
        return [view.present(job) for job in JobStore(db).list(conn=conn, include_active=True)]
