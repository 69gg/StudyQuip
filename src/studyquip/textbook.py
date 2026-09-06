"""Versioned textbook mutations, human review and evidence projections."""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .lexical import EvidenceError, canonical_text, locate_quote, normalize
from .retrieval import RetrievalService, records

if TYPE_CHECKING:
    from .db import Database


def new_id() -> str:
    return str(uuid.uuid4())


def payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class OperationRejected(ValueError):
    pass


class TextbookService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.retrieval = RetrievalService(db)

    def ensure_root(self, book_id: str, conn: Connection | None = None) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.ensure_root(book_id, active)
        book = self.db.get("book", book_id, conn=conn)
        if not book or book.get("deleted"):
            raise ValueError("教材不存在")
        root_id = f"root:{book_id}"
        root = self.db.get("node", root_id, conn=conn)
        if root:
            return root
        return self.db.put(
            "node",
            {
                "book_id": book_id,
                "parent_id": None,
                "title": book.get("title", ""),
                "order": 0,
                "is_root": True,
                "summary_stale": True,
            },
            id=root_id,
            conn=conn,
        )

    def list_blocks(
        self, book_id: str, node_id: str | None = None, conn: Connection | None = None
    ) -> list[dict[str, Any]]:
        blocks = [
            block
            for block in records(self.db, "block", {"book_id": book_id}, conn)
            if not block.get("archived") and (node_id is None or block.get("node_id") == node_id)
        ]
        return sorted(blocks, key=lambda block: (block.get("order", 0), block["id"]))

    def set_node(self, book_id: str, data: dict[str, Any], conn: Connection | None = None) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.set_node(book_id, data, active)
        receipt = self.apply_operations(
            book_id, new_id(), [{"op": "node", "node": data}], actor="human", conn=conn
        )
        if receipt["status"] != "accepted":
            raise ValueError(receipt.get("reason", "目录修改失败"))
        return receipt["nodes"][0]

    def ingest_text(
        self, book_id: str, text: str, page_id: str | None = None, conn: Connection | None = None
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.db.write() as active:
                return self.ingest_text(book_id, text, page_id, active)
        root = self.ensure_root(book_id, conn)
        paragraphs = [part for part in text.split("\n\n") if part.strip()]
        operations = [
            {
                "op": "insert",
                "block": {
                    "text": paragraph,
                    "node_id": root["id"],
                    "source_page_ids": [page_id] if page_id else [],
                    "type": "paragraph",
                },
            }
            for paragraph in paragraphs
        ]
        receipt = self.apply_operations(book_id, new_id(), operations, actor="import", conn=conn)
        return receipt.get("blocks", [])

    def upsert_page_draft(
        self, book_id: str, page_id: str, text: str, status: str = "draft", conn: Connection | None = None
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.upsert_page_draft(book_id, page_id, text, status, active)
        page = self.db.get("page", page_id, conn=conn) or {"book_id": book_id}
        if page.get("book_id") != book_id:
            raise ValueError("页面不属于此教材")
        return self.db.put("page", {**page, "text": text, "status": status}, id=page_id, conn=conn)

    def skip_page(
        self, book_id: str, page_id: str, reason: str = "用户跳过", conn: Connection | None = None
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.skip_page(book_id, page_id, reason, active)
        page = self.db.get("page", page_id, conn=conn)
        if not page or page.get("book_id") != book_id:
            raise ValueError("页面不存在")
        self.db.put(
            "working_memory",
            {
                "book_id": book_id,
                "gap_page_id": page_id,
                "open_paragraph": None,
                "outline_uncertain": True,
                "summary": "上一页已跳过，不得跨缺口续接或推测缺失目录。",
            },
            id=book_id,
            conn=conn,
        )
        return self.db.put(
            "page", {**page, "status": "skipped", "skip_reason": reason, "gap": True}, id=page_id, conn=conn
        )

    def apply_operations(
        self,
        book_id: str,
        group_id: str,
        operations: list[dict[str, Any]],
        reason: str = "",
        actor: str = "ai",
        sources: list[Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.apply_operations(
                    book_id, group_id, operations, reason, actor, sources, checkpoint, active
                )
        try:
            # A savepoint also rolls back late projection validation, while the outer
            # lease-fenced transaction can still commit the terminal rejection receipt.
            with conn.begin_nested():
                return self._apply_operations(
                    book_id, group_id, operations, reason, actor, sources, checkpoint, conn
                )
        except (OperationRejected, EvidenceError, ValueError, KeyError, TypeError) as exc:
            digest = payload_hash(
                {
                    "operations": operations,
                    "reason": reason,
                    "actor": actor,
                    "sources": sources,
                    "checkpoint": checkpoint,
                }
            )
            return self.db.put(
                "receipt",
                {
                    "book_id": book_id,
                    "group_id": group_id,
                    "payload_hash": digest,
                    "status": "rejected",
                    "reason": str(exc),
                },
                id=f"{book_id}:{group_id}",
                conn=conn,
            )

    def _apply_operations(
        self,
        book_id: str,
        group_id: str,
        operations: list[dict[str, Any]],
        reason: str = "",
        actor: str = "ai",
        sources: list[Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.apply_operations(
                    book_id, group_id, operations, reason, actor, sources, checkpoint, active
                )
        payload = {
            "operations": operations,
            "reason": reason,
            "actor": actor,
            "sources": sources,
            "checkpoint": checkpoint,
        }
        digest = payload_hash(payload)
        receipt_id = f"{book_id}:{group_id}"
        previous = self.db.get("receipt", receipt_id, conn=conn)
        if previous:
            if previous["payload_hash"] != digest:
                return {"status": "rejected", "reason": "idempotency_conflict", "group_id": group_id}
            return previous
        base = {"book_id": book_id, "group_id": group_id, "payload_hash": digest}
        if not group_id or not isinstance(operations, list):
            raise OperationRejected("invalid_group")
        root = self.ensure_root(book_id, conn)
        originals = {block["id"]: block for block in records(self.db, "block", {"book_id": book_id}, conn)}
        nodes_original = {node["id"]: node for node in records(self.db, "node", {"book_id": book_id}, conn)}
        staged = deepcopy(originals)
        nodes = deepcopy(nodes_original)
        changed: set[str] = set()
        changed_nodes: set[str] = set()
        merge_redirects: dict[str, str | None] = {}
        migrations: list[dict[str, Any]] = []
        prepared = deepcopy(operations)
        self._prepare_restores(book_id, prepared, originals, conn)
        protected: set[str] = set()
        for operation in prepared:
            if operation.get("op") == "insert":
                operation.setdefault("id", new_id())
            if operation.get("op") in {"insert", "node"}:
                object_kind = "block" if operation["op"] == "insert" else "node"
                object_id = (
                    operation.get("id") if object_kind == "block" else operation.get("node", {}).get("id")
                )
                existing = self.db.get(object_kind, object_id, conn=conn) if object_id else None
                if existing and existing.get("book_id") != book_id:
                    raise OperationRejected("object_id_out_of_scope")
                if object_id and existing is None and self.db.history(object_kind, object_id, conn=conn):
                    raise OperationRejected("object_id_reserved_by_history")
            targets = operation.get("ids", [operation.get("id")])
            protected.update(
                target
                for target in targets
                if target in originals and originals[target].get("human_protected")
            )
            if operation.get("op") == "node":
                node = nodes_original.get(operation.get("node", {}).get("id"))
                if node and node.get("human_protected"):
                    protected.add(node["id"])
        # Simulate the entire proposal before committing any safe insertion or suggestion.
        for operation in prepared:
            self._stage(
                book_id,
                operation,
                staged,
                nodes,
                originals,
                nodes_original,
                changed,
                changed_nodes,
                merge_redirects,
                migrations,
                root["id"],
                actor,
            )
        self._validate_tree(nodes)
        if checkpoint and checkpoint.get("current_node_id") and checkpoint["current_node_id"] not in nodes:
            raise OperationRejected("working_node_out_of_scope")
        restored = {operation["id"] for operation in prepared if operation.get("op") == "restore"}
        if restored:
            live_restored = {key: staged[key]["text"] for key in restored if not staged[key].get("archived")}
            for key in restored:
                migrations.append(
                    self._migration(book_id, originals[key], list(live_restored), live_restored)
                )
        if protected and actor == "ai":
            # New material is a canonical block even while joining protected text awaits review.
            safe_node_ids = changed_nodes - nodes_original.keys()
            for key in safe_node_ids:
                nodes_original[key] = self.db.put("node", nodes[key], id=key, conn=conn)
            safe = [operation for operation in prepared if operation["op"] == "insert"]
            safe_ids = {operation["id"] for operation in safe}
            for operation in safe:
                block = {
                    **operation["block"],
                    "book_id": book_id,
                    "node_id": operation["block"].get("node_id", root["id"]),
                    "text": canonical_text(operation["block"].get("text", "")),
                    "type": operation["block"].get("type", "paragraph"),
                    "order": operation["block"].get(
                        "order", max((item.get("order", 0) for item in originals.values()), default=0) + 1
                    ),
                    "human_protected": False,
                    "archived": False,
                }
                stored = self.db.put("block", block, id=operation["id"], conn=conn)
                originals[stored["id"]] = stored
                self.retrieval.index_block(stored, conn)
            pending = [
                operation
                for operation in prepared
                if not (operation["op"] == "insert" and operation["id"] in safe_ids)
                and not (operation["op"] == "node" and operation["node"].get("id") in safe_node_ids)
            ]
            for operation in pending:
                if operation.get("id") in safe_ids:
                    operation["base_revision"] = originals[operation["id"]]["revision"]
                for target in safe_ids & set(operation.get("ids", [])):
                    operation.setdefault("base_revisions", {})[target] = originals[target]["revision"]
            self._dirty_ancestors(
                book_id, {originals[key].get("node_id") for key in safe_ids} | set(safe_node_ids), conn
            )
            safe_nodes = [self.db.get("node", key, conn=conn) for key in safe_node_ids]
            proposal_fingerprint = payload_hash(
                {
                    "operations": pending,
                    "versions": {key: originals[key]["revision"] for key in changed if key in originals},
                    "node_versions": {
                        key: nodes_original[key]["revision"] for key in changed_nodes if key in nodes_original
                    },
                }
            )
            previous_suggestion = next(
                (
                    item
                    for item in records(self.db, "suggestion", {"book_id": book_id}, conn)
                    if item.get("fingerprint") == proposal_fingerprint
                    and item.get("status") in {"pending", "ignored"}
                ),
                None,
            )
            if previous_suggestion:
                if checkpoint is not None:
                    self.db.put("working_memory", {**checkpoint, "book_id": book_id}, id=book_id, conn=conn)
                return self.db.put(
                    "receipt",
                    {
                        **base,
                        "status": "accepted",
                        "suggestions": [previous_suggestion]
                        if previous_suggestion["status"] == "pending"
                        else [],
                        "ignored_suggestion_id": previous_suggestion["id"]
                        if previous_suggestion["status"] == "ignored"
                        else None,
                        "blocks": [originals[key] for key in safe_ids],
                        "nodes": safe_nodes,
                    },
                    id=receipt_id,
                    conn=conn,
                )
            suggestion = self.db.put(
                "suggestion",
                {
                    "book_id": book_id,
                    "status": "pending",
                    "reason": reason,
                    "operations": pending,
                    "before": [originals[key] for key in changed if key in originals]
                    + [nodes_original[key] for key in changed_nodes if key in nodes_original],
                    "after": [staged[key] for key in changed] + [nodes[key] for key in changed_nodes],
                    "base_revisions": {
                        key: value["revision"] for key, value in originals.items() if key in changed
                    },
                    "fingerprint": proposal_fingerprint,
                },
                conn=conn,
            )
            if checkpoint is not None:
                self.db.put("working_memory", {**checkpoint, "book_id": book_id}, id=book_id, conn=conn)
            return self.db.put(
                "receipt",
                {
                    **base,
                    "status": "accepted",
                    "suggestions": [suggestion],
                    "blocks": [originals[key] for key in safe_ids],
                    "nodes": safe_nodes,
                },
                id=receipt_id,
                conn=conn,
            )
        output_nodes: list[dict[str, Any]] = []
        for key in changed_nodes:
            output_nodes.append(self.db.put("node", nodes[key], id=key, conn=conn))
        output: list[dict[str, Any]] = []
        for key in changed:
            stored = self.db.put("block", staged[key], id=key, conn=conn)
            staged[key] = stored
            output.append(stored)
            conn.execute(text("DELETE FROM vectors WHERE object_id=:id AND object_kind='block'"), {"id": key})
        self._materialize_redirects(book_id, merge_redirects, conn)
        for migration in migrations:
            migration["new_revisions"] = {key: staged[key]["revision"] for key in migration["new_ids"]}
            self.db.put("span_migration", migration, conn=conn)
        self._refresh_evidence(book_id, changed, staged, migrations, conn)
        dirty_nodes = {staged[key].get("node_id") for key in changed}
        dirty_nodes.update(changed_nodes)
        self._dirty_ancestors(book_id, dirty_nodes, conn)
        output_nodes = [self.db.get("node", node["id"], conn=conn) for node in output_nodes]
        # Node edits can change paths for many descendants; one transaction rebuilds current FTS.
        if changed_nodes:
            self.retrieval.rebuild(book_id, conn)
        else:
            for stored in output:
                self.retrieval.index_block(stored, conn)
        self._stale_explanations(book_id, conn)
        if checkpoint is not None:
            self.db.put("working_memory", {**checkpoint, "book_id": book_id}, id=book_id, conn=conn)
        return self.db.put(
            "receipt",
            {**base, "status": "accepted", "blocks": output, "nodes": output_nodes, "suggestions": []},
            id=receipt_id,
            conn=conn,
        )

    def _prepare_restores(
        self,
        book_id: str,
        operations: list[dict[str, Any]],
        originals: dict[str, dict[str, Any]],
        conn: Connection,
    ) -> None:
        restores = [operation for operation in operations if operation.get("op") == "restore"]
        if not restores:
            return
        restore_ids = {operation.get("id") for operation in restores}
        if len(restore_ids) != len(restores):
            raise OperationRejected("duplicate_restore_target")
        if any(operation.get("op") != "restore" for operation in operations):
            raise OperationRejected("restore_group_must_only_contain_restores")
        migrations = records(self.db, "span_migration", {"book_id": book_id}, conn)
        connected: dict[str, set[str]] = {}
        for migration in migrations:
            links = {migration["old_id"], *migration["new_ids"]}
            for key in links:
                connected.setdefault(key, set()).update(links - {key})
        for redirect in records(self.db, "redirect", {"book_id": book_id}, conn):
            if not redirect.get("current_id"):
                continue
            connected.setdefault(redirect["old_id"], set()).add(redirect["current_id"])
            connected.setdefault(redirect["current_id"], set()).add(redirect["old_id"])
        required: set[str] = set()
        for operation in restores:
            key = operation.get("id")
            current = originals.get(key)
            if not current or operation.get("base_revision") != current["revision"]:
                raise OperationRejected("block_revision_conflict")
            target_revision = operation.get("restore_revision")
            if target_revision is None:
                snapshot = {**current, "archived": True}
                snapshot.pop("merged_into", None)
            else:
                snapshot = next(
                    (
                        item
                        for item in self.db.history("block", key, conn=conn)
                        if item["revision"] == target_revision
                    ),
                    None,
                )
                if snapshot is None or snapshot.get("book_id") != book_id:
                    raise OperationRejected("restore_revision_missing")
            operation["_snapshot"] = snapshot
            crossed = (
                current.get("archived")
                or target_revision is None
                or any(
                    migration["old_id"] == key
                    and migration["old_revision"] >= target_revision
                    and (len(migration["new_ids"]) != 1 or migration["new_ids"] != [key])
                    for migration in migrations
                )
                if target_revision is not None
                else True
            )
            # A merge survivor has a same-ID migration; redirects still reveal topology.
            crossed = crossed or (
                target_revision is not None
                and any(
                    migration["old_id"] == key and migration["old_revision"] >= target_revision
                    for migration in migrations
                )
                and key in connected
            )
            if crossed:
                frontier = [key]
                while frontier:
                    member = frontier.pop()
                    if member in required:
                        continue
                    required.add(member)
                    frontier.extend(connected.get(member, set()) - required)
        if not required <= restore_ids:
            raise OperationRejected("restore_requires_topology_group: " + ",".join(sorted(required)))
        for operation in restores:
            target = operation["_snapshot"].get("merged_into")
            if operation["_snapshot"].get("archived") and target:
                target_snapshot = next(
                    (item["_snapshot"] for item in restores if item["id"] == target), originals.get(target)
                )
                if not target_snapshot or target_snapshot.get("archived"):
                    raise OperationRejected("restore_redirect_target_archived")

    def _stage(
        self,
        book_id: str,
        operation: dict[str, Any],
        blocks: dict[str, dict[str, Any]],
        nodes: dict[str, dict[str, Any]],
        originals: dict[str, dict[str, Any]],
        old_nodes: dict[str, dict[str, Any]],
        changed: set[str],
        changed_nodes: set[str],
        redirects: dict[str, str | None],
        migrations: list[dict[str, Any]],
        root_id: str,
        actor: str,
    ) -> None:
        kind = operation.get("op")
        if kind == "node":
            data = operation["node"]
            key = data.get("id") or new_id()
            data["id"] = key
            current = old_nodes.get(key)
            if current and data.get("base_revision") != current["revision"]:
                raise OperationRejected("node_revision_conflict")
            if current and current.get("is_root") and data.get("parent_id") is not None:
                raise OperationRejected("root_cannot_move")
            node = {
                **(nodes.get(key) or {}),
                **{field: value for field, value in data.items() if field in {"title", "parent_id", "order"}},
                "id": key,
                "book_id": book_id,
                "summary_stale": True,
            }
            node.setdefault("parent_id", root_id)
            if key != root_id and node.get("parent_id") is None:
                node["parent_id"] = root_id
            node.setdefault("order", len(nodes))
            if actor == "human":
                node["human_protected"] = True
            if not node.get("title", "").strip():
                raise OperationRejected("empty_node_title")
            nodes[key] = node
            changed_nodes.add(key)
            return
        if kind == "insert":
            key = operation["id"]
            if key in blocks:
                raise OperationRejected("block_id_exists")
            data = operation["block"]
            node_id = data.get("node_id", root_id)
            if node_id not in nodes:
                raise OperationRejected("node_out_of_scope")
            blocks[key] = {
                "id": key,
                "revision": 1,
                "book_id": book_id,
                "node_id": node_id,
                "text": canonical_text(data.get("text", "")),
                "type": data.get("type", "paragraph"),
                "order": data.get(
                    "order", max((item.get("order", 0) for item in blocks.values()), default=0) + 1
                ),
                "source_page_ids": data.get("source_page_ids", []),
                "asset_ids": data.get("asset_ids", []),
                "human_protected": actor == "human",
                "archived": False,
            }
            changed.add(key)
            return
        if kind == "restore":
            key = operation["id"]
            snapshot = operation["_snapshot"]
            blocks[key] = {
                **deepcopy(snapshot),
                "id": key,
                "book_id": book_id,
                "text": canonical_text(snapshot["text"]),
                "human_protected": actor == "human" or snapshot.get("human_protected", False),
            }
            redirects[key] = snapshot.get("merged_into") if snapshot.get("archived") else None
            if not snapshot.get("archived"):
                blocks[key].pop("merged_into", None)
            changed.add(key)
            return
        ids = operation.get("ids", [operation.get("id")])
        if not ids or len(set(ids)) != len(ids):
            raise OperationRejected("invalid_targets")
        for key in ids:
            if key not in blocks or blocks[key].get("archived"):
                raise OperationRejected("block_missing_or_archived")
            expected = (
                operation.get("base_revisions", {}).get(key)
                if kind == "merge"
                else operation.get("base_revision")
            )
            if key in originals and expected != originals[key]["revision"]:
                raise OperationRejected("block_revision_conflict")
        if kind == "merge":
            if len(ids) < 2:
                raise OperationRejected("merge_requires_two_blocks")
            ordered = sorted(ids, key=lambda key: (blocks[key].get("order", 0), key))
            survivor = ordered[0]
            original_contents = {key: deepcopy(blocks[key]) for key in ordered}
            joined = canonical_text(
                operation.get("text", "\n\n".join(blocks[key]["text"] for key in ordered))
            )
            blocks[survivor]["text"] = joined
            blocks[survivor]["source_page_ids"] = list(
                dict.fromkeys(page for key in ordered for page in blocks[key].get("source_page_ids", []))
            )
            for key in ordered[1:]:
                blocks[key]["archived"] = True
                blocks[key]["merged_into"] = survivor
                redirects[key] = survivor
            for key in ordered:
                migrations.append(
                    self._migration(book_id, original_contents[key], [survivor], {survivor: joined})
                )
            changed.update(ordered)
        elif kind == "split":
            key = ids[0]
            before = deepcopy(blocks[key])
            parts = operation.get("parts", [])
            if len(parts) < 2 or any(not isinstance(part, str) or not part for part in parts):
                raise OperationRejected("split_requires_nonempty_parts")
            next_order = min(
                (item["order"] for item in blocks.values() if item.get("order", 0) > before.get("order", 0)),
                default=before.get("order", 0) + 1,
            )
            new_ids = [key] + [new_id() for _ in parts[1:]]
            texts: dict[str, str] = {}
            for index, (new_key, part) in enumerate(zip(new_ids, parts, strict=True)):
                blocks[new_key] = {
                    **before,
                    "id": new_key,
                    "text": canonical_text(part),
                    "order": before.get("order", 0)
                    + index * (next_order - before.get("order", 0)) / len(parts),
                }
                if index:
                    blocks[new_key]["revision"] = 1
                changed.add(new_key)
                texts[new_key] = blocks[new_key]["text"]
            migrations.append(self._migration(book_id, before, new_ids, texts))
        elif kind in {"update", "move", "archive"}:
            key = ids[0]
            block = blocks[key]
            if kind == "archive":
                block["archived"] = True
            elif kind == "move":
                if operation.get("node_id") not in nodes:
                    raise OperationRejected("node_out_of_scope")
                block.update({"node_id": operation["node_id"], "order": operation["order"]})
            else:
                changes = operation.get("changes", {})
                unknown = set(changes) - {
                    "text",
                    "node_id",
                    "type",
                    "order",
                    "asset_ids",
                    "source_page_ids",
                    "human_protected",
                }
                if unknown or (actor != "human" and "human_protected" in changes):
                    raise OperationRejected("invalid_block_fields")
                if "node_id" in changes and changes["node_id"] not in nodes:
                    raise OperationRejected("node_out_of_scope")
                if "text_edit" in operation:
                    edit = operation["text_edit"]
                    start, end = edit["start"], edit["end"]
                    if (
                        not (0 <= start <= end <= len(block["text"]))
                        or block["text"][start:end] != edit["expected_text"]
                    ):
                        raise OperationRejected("text_edit_conflict")
                    changes["text"] = block["text"][:start] + edit["replacement"] + block["text"][end:]
                block.update(changes)
                block["text"] = canonical_text(block["text"])
            changed.add(key)
        else:
            raise OperationRejected("unknown_operation")
        if actor == "human":
            for key in changed:
                blocks[key]["human_protected"] = True

    def _validate_tree(self, nodes: dict[str, dict[str, Any]]) -> None:
        for key, node in nodes.items():
            visited = {key}
            current = node.get("parent_id")
            while current:
                if current in visited or current not in nodes:
                    raise OperationRejected("invalid_outline_tree")
                visited.add(current)
                current = nodes[current].get("parent_id")

    def _migration(
        self, book_id: str, before: dict[str, Any], new_ids: list[str], texts: dict[str, str]
    ) -> dict[str, Any]:
        ranges: list[dict[str, Any]] = []
        old_text = before["text"]
        for key in new_ids:
            # Exact retained substrings are migration hints; citations still undergo normalized validation.
            offset = texts[key].find(old_text)
            if offset >= 0 and texts[key].find(old_text, offset + 1) < 0:
                ranges.append(
                    {
                        "old_start": 0,
                        "old_end": len(old_text),
                        "new_id": key,
                        "new_start": offset,
                        "new_end": offset + len(old_text),
                    }
                )
            else:
                offset = old_text.find(texts[key])
                if offset >= 0 and old_text.find(texts[key], offset + 1) < 0:
                    ranges.append(
                        {
                            "old_start": offset,
                            "old_end": offset + len(texts[key]),
                            "new_id": key,
                            "new_start": 0,
                            "new_end": len(texts[key]),
                        }
                    )
        return {
            "book_id": book_id,
            "old_id": before["id"],
            "old_revision": before["revision"],
            "new_ids": new_ids,
            "ranges": ranges,
        }

    def _materialize_redirects(
        self, book_id: str, additions: dict[str, str | None], conn: Connection
    ) -> None:
        redirects = {
            row["old_id"]: row["current_id"]
            for row in records(self.db, "redirect", {"book_id": book_id}, conn)
            if row.get("current_id")
        }
        for old_id, target in additions.items():
            if target is None:
                redirects.pop(old_id, None)
                existing = self.db.get("redirect", f"{book_id}:{old_id}", conn=conn)
                if existing:
                    self.db.put(
                        "redirect",
                        {"book_id": book_id, "old_id": old_id, "current_id": None, "active": False},
                        id=f"{book_id}:{old_id}",
                        conn=conn,
                    )
            else:
                redirects[old_id] = target
        for old_id, target in redirects.items():
            seen = {old_id}
            while target in redirects:
                if target in seen:
                    raise OperationRejected("redirect_cycle")
                seen.add(target)
                target = redirects[target]
            if target in seen:
                raise OperationRejected("redirect_cycle")
            self.db.put(
                "redirect",
                {"book_id": book_id, "old_id": old_id, "current_id": target},
                id=f"{book_id}:{old_id}",
                conn=conn,
            )

    def resolve_id(self, book_id: str, block_id: str, conn: Connection | None = None) -> str:
        redirect = self.db.get("redirect", f"{book_id}:{block_id}", conn=conn)
        return redirect["current_id"] if redirect and redirect.get("current_id") else block_id

    def current_evidence(
        self,
        book_id: str,
        block_id: str,
        revision: int,
        quote: str,
        span_hint: tuple[int, int] | list[int] | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        book = self.db.get("book", book_id, conn=conn)
        block = self.db.get("block", block_id, conn=conn)
        if not book or book.get("deleted") or not block or block.get("book_id") != book_id:
            raise EvidenceError("source_out_of_scope")
        if block.get("archived") or block["revision"] != revision:
            raise EvidenceError("stale_source_revision")
        start, end = locate_quote(block["text"], quote, span_hint)
        return {
            "block_id": block_id,
            "revision": revision,
            "start": start,
            "end": end,
            "quote": block["text"][start:end],
            "valid": True,
        }

    def _refresh_evidence(
        self,
        book_id: str,
        changed: set[str],
        blocks: dict[str, dict[str, Any]],
        migrations: list[dict[str, Any]],
        conn: Connection,
    ) -> None:
        candidates_by_old = {item["old_id"]: item for item in migrations}
        for kind in ("relation", "concept", "citation_projection"):
            for record in records(self.db, kind, {"book_id": book_id}, conn):
                evidence = record.get("evidence", [])
                touched = False
                new_evidence: list[dict[str, Any]] = []
                for anchor in evidence:
                    if anchor.get("block_id") not in changed:
                        new_evidence.append(anchor)
                        continue
                    touched = True
                    old_id = anchor["block_id"]
                    migration = candidates_by_old.get(old_id)
                    ids = migration["new_ids"] if migration else [self.resolve_id(book_id, old_id, conn)]
                    matches: list[dict[str, Any]] = []
                    for key in ids:
                        block = blocks.get(key) or self.db.get("block", key, conn=conn)
                        if not block or block.get("archived"):
                            continue
                        hint: tuple[int, int] | None = None
                        if migration:
                            for span in migration["ranges"]:
                                if (
                                    span["new_id"] == key
                                    and span["old_start"] <= anchor.get("start", -1)
                                    and anchor.get("end", -1) <= span["old_end"]
                                ):
                                    hint = (
                                        span["new_start"] + anchor["start"] - span["old_start"],
                                        span["new_start"] + anchor["end"] - span["old_start"],
                                    )
                        try:
                            start, end = locate_quote(block["text"], anchor["quote"], hint)
                            matches.append(
                                {
                                    **anchor,
                                    "block_id": key,
                                    "revision": block["revision"],
                                    "start": start,
                                    "end": end,
                                    "valid": True,
                                }
                            )
                        except EvidenceError:
                            pass
                    new_evidence.append(
                        matches[0]
                        if len(matches) == 1
                        else {**anchor, "valid": False, "invalid_reason": "migration_unverifiable"}
                    )
                if touched:
                    data = {
                        **record,
                        "evidence": new_evidence,
                        "valid": bool(new_evidence) and all(anchor.get("valid") for anchor in new_evidence),
                    }
                    if "block_ids" in data:
                        data["block_ids"] = list(
                            dict.fromkeys(
                                anchor["block_id"] for anchor in new_evidence if anchor.get("valid")
                            )
                        )
                    self.db.put(kind, data, id=record["id"], conn=conn)

    def _dirty_ancestors(self, book_id: str, node_ids: set[str | None], conn: Connection) -> None:
        dirty: set[str] = set()
        for key in node_ids:
            while key and key not in dirty:
                node = self.db.get("node", key, conn=conn)
                if not node or node.get("book_id") != book_id:
                    break
                dirty.add(key)
                key = node.get("parent_id")
        for key in dirty:
            node = self.db.get("node", key, conn=conn)
            if node:
                self.db.put("node", {**node, "summary_stale": True}, id=key, conn=conn)
                conn.execute(
                    text("DELETE FROM vectors WHERE object_id=:id AND object_kind='node'"), {"id": key}
                )

    def _stale_explanations(self, book_id: str, conn: Connection) -> None:
        for question in records(self.db, "question", conn=conn):
            explanation = question.get("explanation") or {}
            if any(citation.get("book_id") == book_id for citation in explanation.get("citations", [])):
                self.db.put("question", {**question, "explanation_stale": True}, id=question["id"], conn=conn)

    def accept_suggestion(
        self, book_id: str, suggestion_id: str, conn: Connection | None = None
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.accept_suggestion(book_id, suggestion_id, active)
        suggestion = self.db.get("suggestion", suggestion_id, conn=conn)
        if not suggestion or suggestion.get("book_id") != book_id:
            raise ValueError("建议不存在")
        if suggestion["status"] != "pending":
            return suggestion
        receipt = self.apply_operations(
            book_id,
            new_id(),
            suggestion["operations"],
            reason=suggestion.get("reason", ""),
            actor="human",
            conn=conn,
        )
        status = "accepted" if receipt["status"] == "accepted" else "stale"
        return self.db.put(
            "suggestion", {**suggestion, "status": status, "receipt": receipt}, id=suggestion_id, conn=conn
        )

    def ignore_suggestion(
        self, book_id: str, suggestion_id: str, conn: Connection | None = None
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.ignore_suggestion(book_id, suggestion_id, active)
        suggestion = self.db.get("suggestion", suggestion_id, conn=conn)
        if not suggestion or suggestion.get("book_id") != book_id:
            raise ValueError("建议不存在")
        if suggestion["status"] != "pending":
            return suggestion
        return self.db.put("suggestion", {**suggestion, "status": "ignored"}, id=suggestion_id, conn=conn)

    def record_concept(
        self, book_id: str, data: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.record_concept(book_id, data, active)
        evidence = [
            self.current_evidence(
                book_id,
                anchor["block_id"],
                anchor["revision"],
                anchor["quote"],
                anchor.get("span_hint"),
                conn,
            )
            for anchor in data.get("evidence", [])
        ]
        if not evidence:
            raise EvidenceError("missing_evidence")
        name = normalize(data.get("name", ""))
        if not name:
            raise ValueError("概念名称不能为空")
        matching = next(
            (
                item
                for item in records(self.db, "concept", {"book_id": book_id}, conn)
                if item["name"] == name
            ),
            None,
        )
        requested = self.db.get("concept", data["id"], conn=conn) if data.get("id") else None
        if requested and requested.get("book_id") != book_id:
            raise EvidenceError("concept_out_of_scope")
        if data.get("id") and requested is None and self.db.history("concept", data["id"], conn=conn):
            raise EvidenceError("concept_id_reserved_by_history")
        previous = matching or requested or {}
        for anchor in previous.get("evidence", []):
            if not anchor.get("valid", True):
                continue
            try:
                retained = self.current_evidence(
                    book_id,
                    anchor["block_id"],
                    anchor["revision"],
                    anchor["quote"],
                    [anchor["start"], anchor["end"]],
                    conn,
                )
                if retained not in evidence:
                    evidence.append(retained)
            except EvidenceError:
                continue
        aliases = list(
            dict.fromkeys(
                normalize(value)
                for value in [*previous.get("aliases", []), *data.get("aliases", [])]
                if normalize(value)
            )
        )
        concept = self.db.put(
            "concept",
            {
                "book_id": book_id,
                "name": name,
                "aliases": aliases,
                "evidence": evidence,
                "block_ids": list(dict.fromkeys(anchor["block_id"] for anchor in evidence)),
                "valid": True,
            },
            id=(matching["id"] if matching else data.get("id")),
            conn=conn,
        )
        for key in set(concept["block_ids"]) | set(previous.get("block_ids", [])):
            block = self.db.get("block", key, conn=conn)
            if block:
                self.retrieval.index_block(block, conn)
        return concept

    def record_relation(
        self, book_id: str, data: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        if conn is None:
            with self.db.write() as active:
                return self.record_relation(book_id, data, active)
        try:
            for endpoint in ("source_id", "target_id"):
                value = next(
                    (
                        self.db.get(kind, data.get(endpoint, ""), conn=conn)
                        for kind in ("concept", "node", "block")
                        if self.db.get(kind, data.get(endpoint, ""), conn=conn)
                    ),
                    None,
                )
                if (
                    not value
                    or value.get("book_id") != book_id
                    or value.get("archived")
                    or value.get("valid") is False
                ):
                    raise EvidenceError("endpoint_out_of_scope")
            evidence = [
                self.current_evidence(
                    book_id, item["block_id"], item["revision"], item["quote"], item.get("span_hint"), conn
                )
                for item in data.get("evidence", [])
            ]
            if not evidence:
                raise EvidenceError("missing_evidence")
            relations = [
                item for item in records(self.db, "relation", {"book_id": book_id}, conn) if item.get("valid")
            ]
            identity = {
                "source_id": data["source_id"],
                "target_id": data["target_id"],
                "type": data.get("type", "related"),
            }
            same = next(
                (
                    item
                    for item in relations
                    if all(item.get(key) == value for key, value in identity.items())
                ),
                None,
            )
            others = [item for item in relations if same is None or item["id"] != same["id"]]
            settings = self.db.settings
            for anchor in evidence:
                if (
                    sum(
                        any(
                            item.get("block_id") == anchor["block_id"]
                            for item in relation.get("evidence", [])
                        )
                        for relation in others
                    )
                    >= settings.block_relation_limit
                ):
                    raise EvidenceError("block_relation_limit")
            for endpoint in (data["source_id"], data["target_id"]):
                if self.db.get("concept", endpoint, conn=conn):
                    if (
                        sum(
                            item.get("source_id") == endpoint or item.get("target_id") == endpoint
                            for item in others
                        )
                        >= settings.concept_relation_limit
                    ):
                        raise EvidenceError("concept_relation_limit")
                else:
                    kind = "node" if self.db.get("node", endpoint, conn=conn) else "block"
                    cap = settings.node_relation_limit if kind == "node" else settings.block_relation_limit
                    if (
                        sum(
                            item.get("source_id") == endpoint or item.get("target_id") == endpoint
                            for item in others
                        )
                        >= cap
                    ):
                        raise EvidenceError(f"{kind}_relation_limit")
            return self.db.put(
                "relation",
                {"book_id": book_id, **identity, "evidence": evidence, "valid": True},
                id=same["id"] if same else None,
                conn=conn,
            )
        except (EvidenceError, KeyError) as exc:
            return self.db.put(
                "relation_rejection",
                {"book_id": book_id, "proposal": data, "reason": str(exc), "valid": False},
                conn=conn,
            )
