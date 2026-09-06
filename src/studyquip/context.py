"""Page-based contexts with optional, explicitly configured token limits."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from sqlalchemy.engine import Connection

from .retrieval import records

if TYPE_CHECKING:
    from .db import Database


def estimate_tokens(value: Any) -> int:
    """Conservative fallback for unknown tokenizers, including non-ASCII text."""
    return max(1, len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))


def node_source_fingerprint(db: Database, node_id: str, conn: Connection | None = None) -> str:
    node = db.get("node", node_id, conn=conn)
    if not node:
        raise ValueError("目录不存在")
    blocks = records(db, "block", {"book_id": node["book_id"]}, conn)
    children = records(db, "node", {"book_id": node["book_id"]}, conn)
    sources = {
        "title": node.get("title"),
        "blocks": sorted(
            (item["id"], item["revision"])
            for item in blocks
            if item.get("node_id") == node_id and not item.get("archived")
        ),
        "children": sorted(
            (item["id"], item.get("summary_source_fp"), item.get("summary_stale", True))
            for item in children
            if item.get("parent_id") == node_id
        ),
    }
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()


class ContextBuilder:
    def __init__(self, db: Database, token_budget: int | None = None) -> None:
        if token_budget is not None and token_budget < 512:
            raise ValueError("教材上下文预算过小")
        self.db = db
        self.token_budget = token_budget

    def units(self, text: str, budget: int | None = None) -> list[str]:
        """Lossless units: source characters are neither stripped nor discarded."""
        maximum = budget
        if maximum is None:
            if self.token_budget is None:
                return [text]
            maximum = max(128, self.token_budget // 4)
        units: list[str] = []
        current = ""
        for paragraph in text.splitlines(keepends=True):
            if current and estimate_tokens(current + paragraph) > maximum:
                units.append(current)
                current = ""
            if estimate_tokens(paragraph) <= maximum:
                current += paragraph
            else:
                # Oversized paragraphs use explicit adjacent units, not silent truncation.
                fragment = ""
                for char in paragraph:
                    if fragment and estimate_tokens(fragment + char) > maximum:
                        units.append(fragment)
                        fragment = ""
                    fragment += char
                current = fragment
        if current or not units:
            units.append(current)
        return units

    def build(
        self,
        book_id: str,
        page_id: str,
        tools: Any = None,
        unit_index: int = 0,
        unit_budget: int | None = None,
    ) -> dict[str, Any]:
        with self.db.read() as conn:
            book = self.db.get("book", book_id, conn=conn)
            page = self.db.get("page", page_id, conn=conn)
            if not book or not page or page.get("book_id") != book_id:
                raise ValueError("教材页面不存在")
            pages = sorted(
                records(self.db, "page", {"book_id": book_id}, conn),
                key=lambda item: (
                    item.get("index", item.get("order", item.get("page_index", 0))),
                    item["id"],
                ),
            )
            position = next(index for index, item in enumerate(pages) if item["id"] == page_id)
            work = self.db.get("working_memory", book_id, conn=conn) or {}
            nodes = {item["id"]: item for item in records(self.db, "node", {"book_id": book_id}, conn)}
            current_node = work.get("current_node_id", f"root:{book_id}")
            chain: list[dict[str, Any]] = []
            seen: set[str] = set()
            while current_node and current_node not in seen:
                seen.add(current_node)
                node = nodes.get(current_node)
                if not node:
                    break
                chain.append({key: node.get(key) for key in ("id", "parent_id", "title", "revision")})
                current_node = node.get("parent_id")
            chain.reverse()
            # Keep access handles for distant ancestors, rather than all full titles.
            if self.token_budget is not None:
                ancestor_budget = self.token_budget // 12
                while len(chain) > 3 and estimate_tokens(chain) > ancestor_budget:
                    del chain[1]
            adjacent_pages = pages[max(0, position - 2) : position]
            gap = next(
                (
                    index
                    for index, item in reversed(list(enumerate(adjacent_pages)))
                    if item.get("status") == "skipped"
                ),
                None,
            )
            if gap is not None:
                adjacent_pages = adjacent_pages[gap + 1 :]
            near_ids = {item["id"] for item in adjacent_pages} | {page_id}
            blocks = [
                block
                for block in records(self.db, "block", {"book_id": book_id}, conn)
                if not block.get("archived") and near_ids.intersection(block.get("source_page_ids", []))
            ]
            blocks.sort(key=lambda item: (item.get("order", 0), item["id"]))
            sibling_ids = {item["id"] for item in chain}
            outline = [
                {key: item.get(key) for key in ("id", "parent_id", "title", "order", "revision")}
                for item in nodes.values()
                if item.get("parent_id") in sibling_ids or item["id"] in sibling_ids
            ]
            outline.sort(key=lambda item: (item.get("order", 0), item["id"]))
            suggestions = [
                item
                for item in records(self.db, "suggestion", {"book_id": book_id}, conn)
                if item.get("status") == "pending"
                and any(block["id"] in item.get("base_revisions", {}) for block in blocks)
            ]
            reserved = estimate_tokens(tools or [])
            if self.token_budget is not None and reserved >= self.token_budget:
                raise ValueError("工具定义超过显式上下文预算，请提高或清空模型上下文配置")
            if unit_budget is None and self.token_budget is not None:
                unit_budget = min(self.token_budget // 4, max(128, (self.token_budget - reserved) // 3))
            units = self.units(page.get("text", ""), unit_budget)
            if not (0 <= unit_index < len(units)):
                raise ValueError("页面处理单元越界")
            context: dict[str, Any] = {
                "book": {
                    "id": book_id,
                    "title": book.get("title", ""),
                    "subject_id": book.get("subject_id"),
                    "revision": book["revision"],
                },
                "ancestors": chain,
                "outline": outline,
                "outline_partial": False,
                "current_page": {
                    "id": page_id,
                    "index": page.get("index", page.get("page_index", position)),
                    "revision": page["revision"],
                    "text": units[unit_index],
                    "unit_index": unit_index,
                    "unit_count": len(units),
                    "unit_budget": unit_budget,
                    "gap_before": position > 0 and pages[position - 1].get("status") == "skipped",
                },
                "previous_blocks": blocks,
                "next_page": {
                    "id": pages[position + 1]["id"],
                    "text": pages[position + 1].get("text", ""),
                    "read_only": True,
                }
                if position + 1 < len(pages)
                and pages[position + 1].get("status")
                in {"draft", "recognized", "ready", "completed", "processed"}
                else None,
                "working_memory": {
                    key: value for key, value in work.items() if key not in {"id", "created_at", "updated_at"}
                },
                "suggestions": [
                    {key: item.get(key) for key in ("id", "reason", "base_revisions")} for item in suggestions
                ],
                "budget": self.token_budget,
                "read_limits": {
                    "max_result_tokens": self.token_budget,
                    "must_read_latest_before_edit": True,
                },
            }

            # Include the estimate field itself before checking; do not append a fixed margin afterward.
            context["token_estimate"] = self.token_budget or 0

            def size() -> int:
                return estimate_tokens(context) + reserved

            def over_budget() -> bool:
                return self.token_budget is not None and size() > self.token_budget

            # Drop optional *whole* units. Never truncate target text, evidence or unfinished calls.
            if over_budget():
                context["next_page"] = None
            while context["outline"] and over_budget():
                context["outline"].pop()
                context["outline_partial"] = True
            while context["previous_blocks"] and over_budget():
                omitted = context["previous_blocks"].pop(0)
                context.setdefault("previous_block_handles", []).append(
                    {
                        "id": omitted["id"],
                        "revision": omitted["revision"],
                        "human_protected": omitted.get("human_protected", False),
                    }
                )
            if over_budget() and context["working_memory"]:
                context["working_memory"] = {
                    key: value
                    for key, value in context["working_memory"].items()
                    if key
                    in {
                        "current_node_id",
                        "gap_page_id",
                        "open_paragraph",
                        "outline_uncertain",
                        "pending_anchors",
                        "open_anchors",
                    }
                }
                context["working_memory_summary_omitted"] = True
            if over_budget():
                raise ValueError("必要上下文超过显式预算，请提高或清空模型上下文配置")
            context["token_estimate"] = size()
            return context

    def read_block(
        self, book_id: str, block_id: str, start: int | None = None, end: int | None = None
    ) -> dict[str, Any]:
        block = self.db.get("block", block_id)
        if not block or block.get("book_id") != book_id or block.get("archived"):
            raise ValueError("正文不在可读取范围内")
        source = block.get("text", "")
        begin, finish = (0 if start is None else start), (len(source) if end is None else end)
        if not (0 <= begin <= finish <= len(source)):
            raise ValueError("读取区间越界")
        result = {
            **block,
            "text": source[begin:finish],
            "start": begin,
            "end": finish,
            "total_codepoints": len(source),
        }
        if self.token_budget is not None and estimate_tokens(result) > self.token_budget:
            raise ValueError("正文过长，请使用 start/end 分段读取；坐标单位为 Unicode 码点")
        return result
