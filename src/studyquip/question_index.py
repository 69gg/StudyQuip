"""Transactional question projections; model calls belong exclusively to the worker."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import Database
from .export import explanation_stale
from .lexical import canonical_text, compile_match, index_text, lexical_fingerprint, normalize
from .question_tree import walk_questions
from .retrieval import embedding_fingerprint, records, search_limit, vector_blob

PART_LABELS = {
    "stem": "题干",
    "options": "选项",
    "answer": "答案",
    "explanation": "解析",
    "knowledge": "知识点",
    "material": "材料",
    "reference": "参考解析",
    "error_reason": "错因",
}
DERIVED_PARTS = {"explanation", "knowledge"}
PROJECTION_VERSION = "question-sections-v1"


def initialize_question_indexes(conn: Connection) -> None:
    conn.exec_driver_sql("""CREATE TABLE IF NOT EXISTS question_parts (
        id TEXT PRIMARY KEY, question_id TEXT NOT NULL, node_id TEXT NOT NULL,
        part TEXT NOT NULL, question_revision INTEGER NOT NULL, node_type TEXT NOT NULL,
        path TEXT NOT NULL, text TEXT NOT NULL, content_fp TEXT NOT NULL, lexical_fp TEXT NOT NULL)""")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS question_parts_owner ON question_parts(question_id,part)"
    )
    conn.exec_driver_sql("""CREATE VIRTUAL TABLE IF NOT EXISTS question_fts USING fts5(
        part_id UNINDEXED, body, tokenize='unicode61 remove_diacritics 0')""")
    conn.exec_driver_sql("""CREATE TABLE IF NOT EXISTS question_vectors (
        part_id TEXT NOT NULL REFERENCES question_parts(id) ON DELETE CASCADE,
        content_fp TEXT NOT NULL, space_fp TEXT NOT NULL, dim INTEGER NOT NULL,
        dtype TEXT NOT NULL DEFAULT 'float32', embedding BLOB NOT NULL,
        PRIMARY KEY(part_id,space_fp),
        CHECK(dim > 0 AND length(embedding) = 4 * dim AND dtype = 'float32'))""")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS question_vectors_space ON question_vectors(space_fp,dim)"
    )


def plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(plain(item) for item in value)
    return str(value)


def sections(question: dict[str, Any], *, stale: bool = False) -> list[dict[str, Any]]:
    """Each tree node keeps its identity; unchanged text keeps its embedding fingerprint."""
    output: list[dict[str, Any]] = []
    paths: dict[str, list[str]] = {question["id"]: []}
    for node in walk_questions(question):
        node_id = node.get("id") or question["id"]
        path = paths.get(node_id, [])
        for number, child in enumerate(node.get("parts", []), 1):
            paths[child["id"]] = [*path, str(number)]
        explanation = {} if stale or node.get("explanation_stale") else node.get("explanation") or {}
        options = node.get("options", [])
        answers = node.get("answer")
        if node.get("type") in {"single_choice", "multiple_choice"}:
            labels = {item["id"]: item.get("text", "") for item in options}
            answers = [
                labels.get(str(value), str(value))
                for value in (answers if isinstance(answers, list) else [answers])
                if value is not None
            ]
        values = {
            "stem": node.get("stem", ""),
            "options": "\n".join(
                f"{number}. {item.get('text', '')}" for number, item in enumerate(options, 1)
            ),
            "answer": plain(answers),
            "explanation": "\n".join(
                filter(None, [explanation.get("summary", ""), plain(explanation.get("steps", []))])
            ),
            "knowledge": plain(explanation.get("knowledge_points", [])),
            "material": "\n\n".join(
                "\n".join(filter(None, [item.get("title", ""), item.get("text", "")]))
                for item in node.get("materials", [])
            ),
            "reference": "\n".join(
                filter(None, [node.get("reference_text", ""), node.get("reference_analysis", "")])
            ),
            "error_reason": plain(node.get("error_reason", "")),
        }
        for part, value in values.items():
            content = canonical_text(value).strip()
            if not content:
                continue
            identifier = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"studyquip:question:{question['id']}:{node_id}:{part}")
            )
            fingerprint = hashlib.sha256((PROJECTION_VERSION + "\0" + content).encode()).hexdigest()
            output.append(
                {
                    "id": identifier,
                    "question_id": question["id"],
                    "node_id": node_id,
                    "part": part,
                    "question_revision": question["revision"],
                    "node_type": node.get("type", ""),
                    "path": json.dumps(path),
                    "text": content,
                    "content_fp": fingerprint,
                    "lexical_fp": lexical_fingerprint(),
                }
            )
    return output


class QuestionIndex:
    def __init__(self, db: Database) -> None:
        self.db = db

    def profile(self, conn: Connection) -> dict[str, Any] | None:
        from .ai import ModelProfile

        profiles = self.db.list("model", filters={"role": "embedding"}, limit=1, conn=conn)
        return ModelProfile.model_validate(profiles[0]).model_dump() if profiles else None

    def sync(self, question: dict[str, Any], conn: Connection, *, enqueue: bool = True) -> None:
        previous = {
            row["id"]: dict(row)
            for row in conn.execute(
                text("SELECT * FROM question_parts WHERE question_id=:id"), {"id": question["id"]}
            ).mappings()
        }
        current = (
            []
            if question.get("deleted")
            else sections(question, stale=explanation_stale(self.db, question, conn))
        )
        retained = {part["id"] for part in current}
        for identifier in previous.keys() - retained:
            conn.execute(text("DELETE FROM question_fts WHERE part_id=:id"), {"id": identifier})
            conn.execute(text("DELETE FROM question_parts WHERE id=:id"), {"id": identifier})
        for part in current:
            old = previous.get(part["id"])
            changed = not old or old["content_fp"] != part["content_fp"]
            if changed:
                conn.execute(text("DELETE FROM question_vectors WHERE part_id=:id"), part)
            if changed or old["lexical_fp"] != part["lexical_fp"]:
                conn.execute(text("DELETE FROM question_fts WHERE part_id=:id"), part)
                conn.execute(
                    text("INSERT INTO question_fts(part_id,body) VALUES (:id,:body)"),
                    {"id": part["id"], "body": index_text(part["text"])},
                )
            conn.execute(
                text("""INSERT INTO question_parts
                (id,question_id,node_id,part,question_revision,node_type,path,text,content_fp,lexical_fp)
                VALUES (:id,:question_id,:node_id,:part,:question_revision,:node_type,:path,:text,:content_fp,:lexical_fp)
                ON CONFLICT(id) DO UPDATE SET question_revision=excluded.question_revision,
                node_type=excluded.node_type,path=excluded.path,text=excluded.text,
                content_fp=excluded.content_fp,lexical_fp=excluded.lexical_fp"""),
                part,
            )
        if (
            enqueue
            and current
            and (profile := self.profile(conn))
            and self.pending(question["id"], profile, conn)
        ):
            from .jobs import JobStore

            jobs = JobStore(self.db)
            jobs.enqueue(
                "question_index",
                question["id"],
                not_before=jobs.now(conn) + self.db.settings.question_index_debounce_seconds,
                conn=conn,
            )

    def eligible(
        self,
        conn: Connection,
        question_id: str | None = None,
        *,
        subject_id: str | None = None,
        parts: list[str] | None = None,
        question_types: list[str] | None = None,
        confirmed_only: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        if question_id:
            current = self.db.get("question", question_id, conn=conn)
            candidates = [current] if current else []
        else:
            candidates = records(
                self.db, "question", {"subject_id": subject_id} if subject_id else None, conn
            )
        questions = {
            item["id"]: item
            for item in candidates
            if not item.get("deleted")
            and (not subject_id or item.get("subject_id") == subject_id)
            and (not confirmed_only or item.get("answer_confirmed"))
        }
        rows = conn.execute(
            text("SELECT * FROM question_parts WHERE question_id IN (SELECT value FROM json_each(:ids))"),
            {"ids": json.dumps(list(questions))},
        ).mappings()
        stale: dict[str, bool] = {}
        output: list[dict[str, Any]] = []
        for row in rows:
            question = questions[row["question_id"]]
            if row["question_revision"] != question["revision"] or (
                parts is not None and row["part"] not in parts
            ):
                continue
            if (
                question_types
                and row["node_type"] not in question_types
                and question.get("type") not in question_types
            ):
                continue
            if row["part"] in DERIVED_PARTS:
                if question["id"] not in stale:
                    stale[question["id"]] = explanation_stale(self.db, question, conn)
                if stale[question["id"]]:
                    continue
            output.append(dict(row))
        return output, questions

    def pending(self, question_id: str, profile: dict[str, Any], conn: Connection) -> list[dict[str, Any]]:
        targets, _ = self.eligible(conn, question_id)
        vectors = conn.execute(
            text("""SELECT v.part_id,v.content_fp,v.space_fp,v.dim FROM question_vectors v
            JOIN question_parts p ON p.id=v.part_id WHERE p.question_id=:id"""),
            {"id": question_id},
        ).mappings()
        existing = {
            (row["part_id"], row["content_fp"])
            for row in vectors
            if (not profile.get("embedding_dimensions") or profile["embedding_dimensions"] == row["dim"])
            and row["space_fp"] == embedding_fingerprint(profile, row["dim"])
        }
        return [part for part in targets if (part["id"], part["content_fp"]) not in existing]

    def store(self, target: dict[str, Any], space_fp: str, vector: list[float], conn: Connection) -> bool:
        current = conn.execute(text("SELECT content_fp FROM question_parts WHERE id=:id"), target).scalar()
        if current != target["content_fp"]:
            return False
        conn.execute(
            text("""INSERT OR REPLACE INTO question_vectors(part_id,content_fp,space_fp,dim,embedding)
            VALUES (:id,:content_fp,:space_fp,:dim,:embedding)"""),
            {**target, "space_fp": space_fp, "dim": len(vector), "embedding": vector_blob(vector)},
        )
        return True

    def search(
        self,
        query: str,
        *,
        mode: str = "keyword",
        keyword_mode: str = "any",
        limit: int | None = None,
        subject_id: str | None = None,
        parts: list[str] | None = None,
        question_types: list[str] | None = None,
        confirmed_only: bool = False,
        query_vector: list[float] | None = None,
        space_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = search_limit(self.db, limit)
        if mode not in {"keyword", "semantic"}:
            raise ValueError("仅支持独立的关键词或向量检索")
        if parts is not None and (not parts or not set(parts) <= PART_LABELS.keys()):
            raise ValueError("请选择有效的题目部分")
        if not normalize(query):
            return []
        with self.db.read() as conn:
            rows, questions = self.eligible(
                conn,
                subject_id=subject_id,
                parts=parts or ["stem"],
                question_types=question_types,
                confirmed_only=confirmed_only,
            )
            by_id = {row["id"]: row for row in rows}
            parameters: dict[str, Any] = {"ids": json.dumps(list(by_id)), "n": limit}
            if mode == "semantic":
                if query_vector is None or not space_fingerprint:
                    raise ValueError("向量检索需要嵌入模型与查询向量")
                parameters.update(q=vector_blob(query_vector), fp=space_fingerprint, dim=len(query_vector))
                sql = """WITH eligible AS MATERIALIZED (
                    SELECT p.id,p.question_id,v.embedding,v.dim FROM question_vectors v JOIN question_parts p ON p.id=v.part_id
                    WHERE p.id IN (SELECT value FROM json_each(:ids)) AND v.content_fp=p.content_fp
                    AND v.space_fp=:fp AND v.dim=:dim AND v.dtype='float32' AND length(v.embedding)=4*:dim
                ), scored AS MATERIALIZED (
                    SELECT id,question_id,CASE WHEN dim=:dim AND length(embedding)=4*:dim
                    THEN vec_distance_cosine(embedding,:q) END AS score FROM eligible
                )"""
            elif keyword_mode == "phrase":
                parameters["query"] = normalize(query)
                # Literal phrase matching uses the same Unicode normalization as evidence.
                candidates = [row for row in rows if parameters["query"] in normalize(row["text"])]
                seen: set[str] = set()
                ranked: list[tuple[str, float | None]] = []
                for row in sorted(
                    candidates, key=lambda item: (item["question_id"], item["path"], item["id"])
                ):
                    if row["question_id"] not in seen:
                        seen.add(row["question_id"])
                        ranked.append((row["id"], None))
                return self._hits(ranked[:limit], by_id, questions, mode)
            else:
                if any(row["lexical_fp"] != lexical_fingerprint() for row in rows):
                    raise ValueError("词法配置已变化，等待题目索引重建")
                expression = compile_match(query, keyword_mode)
                if not expression:
                    return []
                parameters["query"] = expression
                sql = """WITH scored AS MATERIALIZED (
                    SELECT p.id,p.question_id,bm25(question_fts) AS score FROM question_fts f
                    JOIN question_parts p ON p.id=f.part_id WHERE question_fts MATCH :query
                    AND p.id IN (SELECT value FROM json_each(:ids)))"""
            # A question occupies one result slot, using its best matching selected section.
            sql += """, ranked AS (
                SELECT *,row_number() OVER (PARTITION BY question_id ORDER BY score,id) AS position FROM scored)
                SELECT id,score FROM ranked WHERE position=1 ORDER BY score,id LIMIT :n"""
            ranked = [(row["id"], row["score"]) for row in conn.execute(text(sql), parameters).mappings()]
            return self._hits(ranked, by_id, questions, mode)

    @staticmethod
    def _hits(
        ranked: list[tuple[str, float | None]],
        parts: dict[str, dict[str, Any]],
        questions: dict[str, dict[str, Any]],
        method: str,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for rank, (identifier, score) in enumerate(ranked, 1):
            part = parts[identifier]
            question = questions[part["question_id"]]
            title = next(
                (node.get("stem") for node in walk_questions(question) if node.get("stem")), "未填写题干"
            )
            matched = next(
                (node for node in walk_questions(question) if node.get("id") == part["node_id"]), question
            )
            title = matched.get("stem") or title
            output.append(
                {
                    **part,
                    "path": json.loads(part["path"]),
                    "question_title": title,
                    "subject_id": question.get("subject_id"),
                    "question_type": question.get("type"),
                    "answer_confirmed": question.get("answer_confirmed", False),
                    "part_label": PART_LABELS[part["part"]],
                    "method": method,
                    "rank": rank,
                    "score": score,
                }
            )
        return output


def reconcile_question_indexes(db: Database) -> None:
    """One short transaction per question, on startup or a changed embedding space."""
    service = QuestionIndex(db)
    for question in records(db, "question"):
        with db.write() as conn:
            current = db.get("question", question["id"], conn=conn)
            if current:
                service.sync(current, conn)
