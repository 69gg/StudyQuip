"""Independent search stages with explicit order and no score fusion."""

from typing import Any

from .db import Database
from .question_index import QuestionIndex
from .retrieval import RetrievalService, search_limit
from .schemas import SearchInput


def search_stage(
    db: Database,
    request: SearchInput,
    method: str,
    *,
    query_vector: list[float] | None = None,
    space_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    limit = search_limit(db, request.limit)
    common = {
        "mode": method,
        "keyword_mode": request.keyword_mode,
        "limit": limit,
        "subject_id": request.subject_id,
        "query_vector": query_vector,
        "space_fingerprint": space_fingerprint,
    }
    if request.target == "question":
        return QuestionIndex(db).search(
            request.query,
            **common,
            parts=request.parts,
            question_types=request.question_types,
            confirmed_only=request.confirmed_only,
        )
    return RetrievalService(db).search(
        request.query, **common, book_ids=request.book_ids, node_id=request.node_id
    )


def append_stage(output: list[dict[str, Any]], hits: list[dict[str, Any]], step: int) -> None:
    seen = {hit.get("question_id") or hit["id"] for hit in output}
    for hit in hits:
        identity = hit.get("question_id") or hit["id"]
        if identity not in seen:
            output.append({**hit, "step": step})
            seen.add(identity)


def current_hits(db: Database, request: SearchInput, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A slow later stage must not publish now-obsolete earlier matches."""
    with db.read() as conn:
        if request.target == "question":
            parts, _ = QuestionIndex(db).eligible(
                conn,
                subject_id=request.subject_id,
                parts=request.parts,
                question_types=request.question_types,
                confirmed_only=request.confirmed_only,
            )
            current = {part["id"]: part for part in parts}
            return [
                hit
                for hit in hits
                if hit["id"] in current
                and current[hit["id"]]["question_revision"] == hit["question_revision"]
                and current[hit["id"]]["content_fp"] == hit["content_fp"]
            ]
        service = RetrievalService(db)
        books = service.scoped_books(request.book_ids, request.subject_id, conn)
        nodes = service.descendants(request.node_id, books, conn) if request.node_id else None
        output: list[dict[str, Any]] = []
        for hit in hits:
            block = db.get("block", hit["id"], conn=conn)
            if (
                block
                and block["book_id"] in books
                and not block.get("archived")
                and block["revision"] == hit["revision"]
                and (nodes is None or block.get("node_id") in nodes)
            ):
                output.append(hit)
        return output
