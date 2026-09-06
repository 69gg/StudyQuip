"""Scoped lexical, vector and textbook-structure retrieval."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import sqlite_vec
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from .lexical import compile_match, index_text, lexical_fingerprint, normalize

if TYPE_CHECKING:
    from .db import Database


def initialize_indexes(conn: Connection) -> None:
    conn.exec_driver_sql("""CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
        block_id UNINDEXED, book_id UNINDEXED, node_id UNINDEXED, revision UNINDEXED,
        lexical_fp UNINDEXED, title, path, body, concepts, aliases, normalized UNINDEXED,
        tokenize='unicode61 remove_diacritics 0')""")
    conn.exec_driver_sql("""CREATE TABLE IF NOT EXISTS vectors (
        object_id TEXT NOT NULL, object_kind TEXT NOT NULL, book_id TEXT NOT NULL,
        revision INTEGER NOT NULL, space_fp TEXT NOT NULL, dim INTEGER NOT NULL,
        dtype TEXT NOT NULL DEFAULT 'float32', embedding BLOB NOT NULL,
        PRIMARY KEY(object_id, object_kind, revision, space_fp),
        CHECK(dim > 0 AND length(embedding) = 4 * dim AND dtype = 'float32'))""")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS vectors_scope ON vectors(space_fp,dim,book_id,object_kind,revision)"
    )


def records(
    db: Database, kind: str, filters: dict[str, Any] | None = None, conn: Connection | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = db.list(kind, filters=filters, limit=1000, offset=offset, conn=conn)
        result.extend(page)
        if len(page) < 1000:
            return result
        offset += len(page)


def node_path(db: Database, node_id: str | None, conn: Connection | None = None) -> list[str]:
    path: list[str] = []
    visited: set[str] = set()
    while node_id:
        if node_id in visited:
            raise ValueError("目录包含循环")
        visited.add(node_id)
        node = db.get("node", node_id, conn=conn)
        if not node:
            break
        if not node.get("is_root"):
            path.append(str(node.get("title", "")))
        node_id = node.get("parent_id")
    return list(reversed(path))


def embedding_fingerprint(profile: dict[str, Any], dimensions: int) -> str:
    values = {
        key: profile.get(key)
        for key in (
            "base_url",
            "model",
            "embedding_revision",
            "document_prefix",
            "query_prefix",
            "extra_body",
        )
    }
    values.update({"dimensions": dimensions, "dtype": "float32", "preprocessing": "nfc-v1"})
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class RetrievalService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def index_block(self, block: dict[str, Any], conn: Connection) -> None:
        conn.execute(text("DELETE FROM block_fts WHERE block_id=:id"), {"id": block["id"]})
        if block.get("archived"):
            return
        book = self.db.get("book", block["book_id"], conn=conn) or {}
        concepts = [
            item
            for item in records(self.db, "concept", {"book_id": block["book_id"]}, conn)
            if item.get("valid", True)
            and (block["id"] in item.get("block_ids", []) or item["id"] in block.get("concept_ids", []))
        ]
        names = " ".join(item.get("name", "") for item in concepts)
        aliases = " ".join(alias for item in concepts for alias in item.get("aliases", []))
        path = " / ".join(node_path(self.db, block.get("node_id"), conn))
        conn.execute(
            text("""INSERT INTO block_fts
            (block_id,book_id,node_id,revision,lexical_fp,title,path,body,concepts,aliases,normalized)
            VALUES (:id,:book,:node,:rev,:fp,:title,:path,:body,:concepts,:aliases,:normalized)"""),
            {
                "id": block["id"],
                "book": block["book_id"],
                "node": block.get("node_id", ""),
                "rev": block["revision"],
                "fp": lexical_fingerprint(),
                "title": index_text(book.get("title", "")),
                "path": index_text(path),
                "body": index_text(block.get("text", "")),
                "concepts": index_text(names),
                "aliases": index_text(aliases),
                "normalized": normalize(block.get("text", "")),
            },
        )

    def rebuild(self, book_id: str, conn: Connection | None = None) -> None:
        if conn is None:
            with self.db.write() as active:
                self.rebuild(book_id, active)
            return
        conn.execute(text("DELETE FROM block_fts WHERE book_id=:book"), {"book": book_id})
        for block in records(self.db, "block", {"book_id": book_id}, conn):
            self.index_block(block, conn)

    def embedding_targets(self, book_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            targets = [
                dict(block, object_kind="block")
                for block in records(self.db, "block", {"book_id": book_id}, conn)
                if not block.get("archived")
            ]
            targets.extend(
                dict(node, object_kind="node", text=node["summary"])
                for node in records(self.db, "node", {"book_id": book_id}, conn)
                if node.get("summary") and not node.get("summary_stale") and not node.get("archived")
            )
            return targets

    def existing_embeddings(self, book_id: str, profile: dict[str, Any]) -> set[tuple[str, str, int]]:
        with self.db.read() as conn:
            rows = (
                conn.execute(
                    text("""SELECT v.object_kind,v.object_id,v.revision,v.dim,v.space_fp
                FROM vectors v JOIN records r ON r.kind=v.object_kind AND r.id=v.object_id
                AND r.revision=v.revision WHERE v.book_id=:book
                AND COALESCE(json_extract(r.data,'$.archived'),0)=0"""),
                    {"book": book_id},
                )
                .mappings()
                .all()
            )
            return {
                (row["object_kind"], row["object_id"], row["revision"])
                for row in rows
                if (not profile.get("embedding_dimensions") or profile["embedding_dimensions"] == row["dim"])
                and row["space_fp"] == embedding_fingerprint(profile, row["dim"])
            }

    def has_embedding(
        self, object_id: str, revision: int, profile: dict[str, Any], object_kind: str = "block"
    ) -> bool:
        obj = self.db.get(object_kind, object_id)
        if not obj or obj["revision"] != revision:
            return False
        return (object_kind, object_id, revision) in self.existing_embeddings(obj["book_id"], profile)

    def store_embedding(
        self,
        object_id: str,
        book_id: str,
        revision: int,
        space_fingerprint: str,
        values: list[float],
        object_kind: str = "block",
        conn: Connection | None = None,
    ) -> None:
        if (
            not values
            or not all(math.isfinite(value) for value in values)
            or not any(value != 0 for value in values)
        ):
            raise ValueError("向量必须非空、非零并且只包含有限值")
        blob = sqlite_vec.serialize_float32(values)
        # float64 finite values can overflow when narrowed to float32.
        import struct

        if not all(math.isfinite(value) for value in struct.unpack(f"{len(values)}f", blob)):
            raise ValueError("向量超出 float32 范围")
        if conn is None:
            with self.db.write() as active:
                self.store_embedding(
                    object_id, book_id, revision, space_fingerprint, values, object_kind, active
                )
            return
        obj = self.db.get(object_kind, object_id, conn=conn)
        if not obj or obj.get("book_id") != book_id or obj["revision"] != revision or obj.get("archived"):
            raise ValueError("嵌入目标版本已过期")
        conn.execute(
            text("""INSERT OR REPLACE INTO vectors
            (object_id,object_kind,book_id,revision,space_fp,dim,dtype,embedding)
            VALUES (:id,:kind,:book,:rev,:fp,:dim,'float32',:blob)"""),
            {
                "id": object_id,
                "kind": object_kind,
                "book": book_id,
                "rev": revision,
                "fp": space_fingerprint,
                "dim": len(values),
                "blob": blob,
            },
        )

    def scoped_books(self, book_ids: list[str] | None, subject_id: str | None, conn: Connection) -> list[str]:
        books = records(self.db, "book", {"subject_id": subject_id} if subject_id else None, conn)
        eligible = {book["id"] for book in books if not book.get("deleted")}
        if book_ids:
            if not set(book_ids) <= eligible:
                raise ValueError("教材不在允许的科目范围内")
            return list(dict.fromkeys(book_ids))
        return sorted(eligible)

    def descendants(self, node_id: str, books: list[str], conn: Connection) -> set[str]:
        root = self.db.get("node", node_id, conn=conn)
        if not root or root["book_id"] not in books or root.get("archived"):
            raise ValueError("目录不在允许的教材范围内")
        nodes = records(self.db, "node", {"book_id": root["book_id"]}, conn)
        children: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            if node.get("archived"):
                continue
            children[node.get("parent_id")].append(node["id"])
        found = {node_id}
        stack = [node_id]
        while stack:
            for child in children[stack.pop()]:
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    def _vector_hits(
        self,
        conn: Connection,
        books: list[str],
        values: list[float],
        fingerprint: str,
        kind: str,
        limit: int,
        node_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        scope = ""
        params: dict[str, Any] = {
            "fp": fingerprint,
            "dim": len(values),
            "kind": kind,
            "books": books,
            "q": sqlite_vec.serialize_float32(values),
            "n": limit,
        }
        if node_ids is not None:
            scope = (
                " AND " + ("json_extract(r.data,'$.node_id')" if kind == "block" else "r.id") + " IN :nodes"
            )
            params["nodes"] = sorted(node_ids)
        statement = text(
            """WITH eligible AS MATERIALIZED (
            SELECT v.object_id,v.revision,v.embedding,v.dim FROM vectors v
            JOIN records r ON r.kind=:kind AND r.id=v.object_id AND r.revision=v.revision
            WHERE v.space_fp=:fp AND v.dim=:dim AND v.dtype='float32'
            AND length(v.embedding)=4*:dim AND v.object_kind=:kind AND v.book_id IN :books
            AND COALESCE(json_extract(r.data,'$.archived'),0)=0"""
            + scope
            + """)
            SELECT object_id,revision,CASE WHEN dim=:dim AND length(embedding)=4*:dim
            THEN vec_distance_cosine(embedding,:q) END AS distance FROM eligible
            ORDER BY distance LIMIT :n"""
        ).bindparams(bindparam("books", expanding=True))
        if node_ids is not None:
            statement = statement.bindparams(bindparam("nodes", expanding=True))
        return [dict(row) for row in conn.execute(statement, params).mappings()]

    def search(
        self,
        query: str,
        book_ids: list[str] | None = None,
        subject_id: str | None = None,
        node_id: str | None = None,
        mode: str = "hybrid",
        keyword_mode: str = "any",
        limit: int = 12,
        query_vector: list[float] | None = None,
        space_fingerprint: str | None = None,
        include_structure: bool = True,
        include_relations: bool = True,
    ) -> list[dict[str, Any]]:
        if mode not in {"hybrid", "keyword", "phrase", "semantic"}:
            raise ValueError("未知检索模式")
        if not normalize(query) or limit <= 0:
            return []
        with self.db.read() as conn:
            books = self.scoped_books(book_ids, subject_id, conn)
            if not books:
                return []
            blocks = {
                item["id"]: item
                for book in books
                for item in records(self.db, "block", {"book_id": book}, conn)
                if not item.get("archived")
            }
            allowed_nodes = self.descendants(node_id, books, conn) if node_id else None
            if allowed_nodes is not None:
                blocks = {key: item for key, item in blocks.items() if item.get("node_id") in allowed_nodes}
            if not blocks:
                return []
            # A lexical configuration change cannot silently query an obsolete index.
            old = (
                conn.execute(
                    text("SELECT DISTINCT lexical_fp FROM block_fts WHERE book_id IN :books").bindparams(
                        bindparam("books", expanding=True)
                    ),
                    {"books": books},
                )
                .scalars()
                .all()
            )
            if any(value != lexical_fingerprint() for value in old):
                raise ValueError("词法配置已变化，请重新建立教材索引")
            settings = self.db.settings
            candidates = settings.global_candidates
            channels: list[tuple[float, list[str]]] = []
            primary_hits: list[str] = []
            literal: list[str] = []
            expression = compile_match(query, keyword_mode)
            if mode in {"keyword", "hybrid"} and expression:
                statement = text(
                    """SELECT block_id, revision, bm25(block_fts) AS score FROM block_fts
                    WHERE block_fts MATCH :query AND book_id IN :books AND lexical_fp=:fp"""
                    + (" AND node_id IN :nodes" if allowed_nodes is not None else "")
                    + " ORDER BY score LIMIT :n"
                ).bindparams(bindparam("books", expanding=True))
                parameters: dict[str, Any] = {
                    "query": expression,
                    "books": books,
                    "fp": lexical_fingerprint(),
                    "n": max(candidates, limit),
                }
                if allowed_nodes is not None:
                    statement = statement.bindparams(bindparam("nodes", expanding=True))
                    parameters["nodes"] = sorted(allowed_nodes)
                rows = conn.execute(statement, parameters).mappings().all()
                literal = [
                    row["block_id"]
                    for row in rows
                    if row["block_id"] in blocks
                    and blocks[row["block_id"]]["revision"] == int(row["revision"])
                ]
                channels.append((settings.lexical_weight, literal[: max(candidates, limit)]))
                primary_hits.extend(literal[: max(candidates, limit)])
            if mode == "phrase":
                literal = [
                    key
                    for key, value in blocks.items()
                    if normalize(query) in normalize(value.get("text", ""))
                ]
                channels.append((settings.lexical_weight, literal))
                primary_hits.extend(literal)
            if query_vector is not None and mode in {"hybrid", "semantic"}:
                if (
                    not space_fingerprint
                    or not query_vector
                    or not all(math.isfinite(value) for value in query_vector)
                    or not any(query_vector)
                ):
                    raise ValueError("向量检索必须提供有效空间指纹和非零向量")
                rows = self._vector_hits(
                    conn,
                    books,
                    query_vector,
                    space_fingerprint,
                    "block",
                    max(candidates, limit),
                    allowed_nodes,
                )
                channels.append(
                    (
                        settings.dense_weight,
                        [row["object_id"] for row in rows if row["object_id"] in blocks][
                            : max(candidates, limit)
                        ],
                    )
                )
                primary_hits.extend(row["object_id"] for row in rows if row["object_id"] in blocks)
            elif mode == "semantic":
                raise ValueError("语义检索需要已配置的嵌入模型与查询向量")
            if mode == "hybrid" and include_structure:
                query_words = set(index_text(query).split())
                nodes = [
                    node
                    for book in books
                    for node in records(self.db, "node", {"book_id": book}, conn)
                    if not node.get("archived")
                ]
                ranked_nodes = sorted(
                    nodes,
                    key=lambda item: len(
                        query_words.intersection(
                            index_text(
                                item.get("title", "")
                                + " "
                                + (item.get("summary", "") if not item.get("summary_stale") else "")
                            ).split()
                        )
                    ),
                    reverse=True,
                )
                node_vectors = (
                    self._vector_hits(
                        conn,
                        books,
                        query_vector,
                        space_fingerprint,
                        "node",
                        settings.directory_candidates,
                        allowed_nodes,
                    )
                    if query_vector and space_fingerprint
                    else []
                )
                node_scores: dict[str, float] = defaultdict(float)
                for rank, node in enumerate(ranked_nodes, start=1):
                    if query_words.intersection(
                        index_text(
                            node.get("title", "")
                            + " "
                            + (node.get("summary", "") if not node.get("summary_stale") else "")
                        ).split()
                    ):
                        node_scores[node["id"]] += 1 / (settings.rrf_k + rank)
                for rank, row in enumerate(node_vectors, start=1):
                    node_scores[row["object_id"]] += 1 / (settings.rrf_k + rank)
                ranked_nodes = sorted(
                    (node for node in nodes if node["id"] in node_scores),
                    key=lambda node: -node_scores[node["id"]],
                )
                routed: list[str] = []
                for node in ranked_nodes[: settings.directory_candidates]:
                    subtree = self.descendants(node["id"], books, conn)
                    scoped = [key for key, item in blocks.items() if item.get("node_id") in subtree]
                    scoped.sort(
                        key=lambda key: len(
                            query_words.intersection(index_text(blocks[key].get("text", "")).split())
                        ),
                        reverse=True,
                    )
                    routed.extend(scoped[: settings.subtree_candidates])
                channels.append((settings.directory_weight, routed))
                expanded: list[str] = []
                direct = primary_hits
                frontier = set(literal[:candidates] + routed) or set(direct[:limit])
                relations = [
                    relation
                    for book in books
                    for relation in records(self.db, "relation", {"book_id": book}, conn)
                    if relation.get("valid")
                ]
                for _ in range(settings.relation_hops if include_relations else 0):
                    next_frontier: set[str] = set()
                    for relation in relations:
                        anchors = relation.get("evidence", [])
                        anchor_ids = {anchor["block_id"] for anchor in anchors if anchor.get("valid", True)}
                        if anchor_ids & frontier:
                            next_frontier.update(anchor_ids & blocks.keys())
                            for endpoint in (relation.get("source_id"), relation.get("target_id")):
                                concept = self.db.get("concept", endpoint or "", conn=conn)
                                if concept and concept.get("book_id") in books:
                                    next_frontier.update(set(concept.get("block_ids", [])) & blocks.keys())
                    expanded.extend(sorted(next_frontier, key=lambda key: (blocks[key].get("order", 0), key)))
                    frontier.update(next_frontier)
                # Nearby source blocks enrich context without crossing book or subtree scope.
                ordered_blocks = sorted(
                    blocks.values(), key=lambda item: (item["book_id"], item.get("order", 0))
                )
                for position, block in enumerate(ordered_blocks):
                    needs_neighbor = block.get("type") == "figure" or not block.get(
                        "text", ""
                    ).rstrip().endswith(("。", ".", "！", "!", "？", "?", "；", ";"))
                    if include_relations and block["id"] in frontier and needs_neighbor:
                        for neighbor in ordered_blocks[max(0, position - 1) : position + 2]:
                            if (
                                neighbor["book_id"] == block["book_id"]
                                and neighbor.get("node_id") == block.get("node_id")
                                and neighbor["id"] != block["id"]
                            ):
                                expanded.append(neighbor["id"])
                channels.append((settings.relation_weight, expanded))
            scores: dict[str, float] = defaultdict(float)
            methods: dict[str, list[int]] = defaultdict(list)
            rrf = settings.rrf_k
            for channel_number, (weight, keys) in enumerate(channels):
                for rank, key in enumerate(dict.fromkeys(keys), start=1):
                    if key in blocks:
                        scores[key] += weight / (rrf + rank)
                        methods[key].append(channel_number)
            ordered = sorted(scores, key=lambda key: (-scores[key], blocks[key].get("order", 0), key))
            selected = ordered[:limit]
            direct_candidates = primary_hits
            if direct_candidates and not set(selected).intersection(direct_candidates):
                selected = selected[: max(0, limit - 1)] + [direct_candidates[0]]
            output: list[dict[str, Any]] = []
            for key in selected:
                block = blocks[key]
                book = self.db.get("book", block["book_id"], conn=conn) or {}
                output.append(
                    {
                        **block,
                        "score": scores[key],
                        "book_title": book.get("title", ""),
                        "path": node_path(self.db, block.get("node_id"), conn),
                        "channels": methods[key],
                    }
                )
            return output
