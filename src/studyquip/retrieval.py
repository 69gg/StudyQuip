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
        blob = vector_blob(values)
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
            ORDER BY distance, object_id LIMIT :n"""
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
        mode: str = "keyword",
        keyword_mode: str = "any",
        limit: int | None = None,
        query_vector: list[float] | None = None,
        space_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        if mode not in {"keyword", "phrase", "semantic"}:
            raise ValueError("仅支持独立的关键词或向量检索")
        limit = search_limit(self.db, limit)
        if not normalize(query):
            return []
        if mode == "phrase":
            mode, keyword_mode = "keyword", "phrase"
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
            ranked: list[tuple[str, float | None]] = []
            if mode == "semantic":
                if not space_fingerprint or query_vector is None:
                    raise ValueError("向量检索需要已配置的嵌入模型与查询向量")
                vector_blob(query_vector)
                ranked = [
                    (row["object_id"], row["distance"])
                    for row in self._vector_hits(
                        conn, books, query_vector, space_fingerprint, "block", limit, allowed_nodes
                    )
                    if row["object_id"] in blocks
                ]
            elif keyword_mode == "phrase":
                ranked = [
                    (block["id"], None)
                    for block in sorted(
                        blocks.values(), key=lambda row: (row["book_id"], row.get("order", 0), row["id"])
                    )
                    if normalize(query) in normalize(block.get("text", ""))
                ][:limit]
            else:
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
                expression = compile_match(query, keyword_mode)
                if expression:
                    statement = text(
                        """SELECT f.block_id, bm25(block_fts) AS score FROM block_fts f
                        JOIN records r ON r.kind='block' AND r.id=f.block_id AND r.revision=f.revision
                        WHERE block_fts MATCH :query AND f.book_id IN :books AND lexical_fp=:fp
                        AND COALESCE(json_extract(r.data,'$.archived'),0)=0"""
                        + (" AND f.node_id IN :nodes" if allowed_nodes is not None else "")
                        + " ORDER BY score, f.block_id LIMIT :n"
                    ).bindparams(bindparam("books", expanding=True))
                    parameters: dict[str, Any] = {
                        "query": expression,
                        "books": books,
                        "fp": lexical_fingerprint(),
                        "n": limit,
                    }
                    if allowed_nodes is not None:
                        statement = statement.bindparams(bindparam("nodes", expanding=True))
                        parameters["nodes"] = sorted(allowed_nodes)
                    ranked = [
                        (row["block_id"], row["score"])
                        for row in conn.execute(statement, parameters).mappings()
                        if row["block_id"] in blocks
                    ]
            output: list[dict[str, Any]] = []
            for rank, (key, score) in enumerate(ranked, 1):
                block = blocks[key]
                book = self.db.get("book", block["book_id"], conn=conn) or {}
                output.append(
                    {
                        **block,
                        "score": score,
                        "method": mode,
                        "rank": rank,
                        "book_title": book.get("title", ""),
                        "path": node_path(self.db, block.get("node_id"), conn),
                    }
                )
            return output


def search_limit(db: Database, requested: int | None, *, tool: bool = False) -> int:
    value = requested if requested is not None else db.settings.search_default_limit
    maximum = (
        min(db.settings.search_max_limit, db.settings.tool_search_max_limit)
        if tool
        else db.settings.search_max_limit
    )
    if not 1 <= value <= maximum:
        raise ValueError(f"每种检索的输出条数必须在 1 到 {maximum} 之间")
    return value


def vector_blob(values: list[float]) -> bytes:
    import struct

    if not values or not all(math.isfinite(value) for value in values) or not any(values):
        raise ValueError("向量必须非空、非零并且只包含有限值")
    blob = sqlite_vec.serialize_float32(values)
    narrowed = struct.unpack(f"{len(values)}f", blob)
    if not all(math.isfinite(value) for value in narrowed) or not any(narrowed):
        raise ValueError("向量超出 float32 有效范围")
    return blob
