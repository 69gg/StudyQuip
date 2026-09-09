"""Search order, current section identity and automatic indexing under concurrent edits."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from studyquip.ai import AIService, ModelProfile
from studyquip.api import create_app
from studyquip.auth import set_password
from studyquip.config import Settings
from studyquip.db import Database, initialize
from studyquip.jobs import JobStore
from studyquip.pipelines import PipelineContext, execute_search, retrieval_tools
from studyquip.question_index import QuestionIndex, reconcile_question_indexes
from studyquip.retrieval import embedding_fingerprint
from studyquip.schemas import SearchInput
from studyquip.textbook import TextbookService
from studyquip.worker import Worker


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    settings = Settings(data_dir=tmp_path, question_index_debounce_seconds=0)
    initialize(settings)
    database = Database(settings)
    yield database
    database.close()


def question(stem: str = "匀加速运动", subject: str = "physics") -> dict[str, Any]:
    return {
        "type": "composite",
        "subject_id": subject,
        "stem": "实验材料",
        "answer_confirmed": True,
        "parts": [
            {
                "id": "group",
                "type": "composite",
                "stem": "实验方法",
                "parts": [
                    {
                        "id": "leaf",
                        "type": "short_answer",
                        "stem": stem,
                        "answer": "速度不断增加",
                        "explanation": {
                            "summary": "加速度等于速度变化率",
                            "steps": ["确定初速度"],
                            "knowledge_points": ["运动学"],
                        },
                        "error_reason": "忘记单位换算",
                        "materials": [{"id": "m", "kind": "listening", "text": "The speed is increasing."}],
                    }
                ],
            }
        ],
    }


def profile(db: Database, dimensions: int = 2) -> dict[str, Any]:
    return db.put(
        "model",
        ModelProfile(
            role="embedding",
            model="fixture",
            api_key="fixture",
            base_url="https://fixture.invalid/v1",
            embedding_dimensions=dimensions,
        ).model_dump(),
    )


def test_section_search_filters_limits_current_versions_and_dimension_isolation(db: Database) -> None:
    q = db.put("question", question())
    db.put("question", question(subject="chemistry"))
    service = QuestionIndex(db)
    filters = {"subject_id": "physics", "question_types": ["short_answer"]}
    assert service.search("速度变化率", parts=["stem"], **filters) == []
    hit = service.search("速度变化率", parts=["stem", "explanation"], **filters)[0]
    assert hit["question_id"] == q["id"] and hit["part"] == "explanation" and hit["path"] == ["1", "1"]
    assert (
        service.search('速度 OR " *', parts=["answer", "explanation"], limit=1, **filters)[0]["question_id"]
        == q["id"]
    )
    assert service.search("速度变化率", parts=["explanation"], keyword_mode="phrase", **filters)
    assert not service.search("加速度 硬不存在", parts=["explanation"], keyword_mode="all", **filters)
    with pytest.raises(ValueError, match="输出条数"):
        service.search("速度", limit=db.settings.search_max_limit + 1)
    with db.write() as conn:
        targets, _ = service.eligible(conn, q["id"])
        stem = next(row for row in targets if row["text"] == "匀加速运动")
        answer = next(row for row in targets if row["part"] == "answer")
        service.store(stem, "two", [1.0, 0.0], conn)
        service.store(answer, "three", [1.0, 0.0, 0.0], conn)
    hits = service.search(
        "描述",
        parts=["stem", "answer"],
        mode="semantic",
        query_vector=[1.0, 0.0],
        space_fingerprint="two",
        **filters,
    )
    assert len(hits) == 1 and hits[0]["part"] == "stem"
    # A confirmation-only revision retains unchanged vectors.
    q = db.put("question", {**q, "answer_confirmed": False}, id=q["id"])
    assert service.search(
        "描述", parts=["stem"], mode="semantic", query_vector=[1.0, 0.0], space_fingerprint="two", **filters
    )
    assert not service.search("匀加速", confirmed_only=True, **filters)
    updated = copy.deepcopy(q)
    updated["parts"][0]["parts"][0].update(stem="自由落体", explanation_stale=True)
    q = db.put("question", updated, id=q["id"])
    assert not service.search("匀加速", **filters)
    assert not service.search("速度变化率", parts=["explanation"], **filters)
    assert service.search("自由落体", **filters)
    assert not service.search(
        "描述", parts=["stem"], mode="semantic", query_vector=[1.0, 0.0], space_fingerprint="two", **filters
    )
    with db.write() as conn:
        assert not service.store(stem, "two", [1.0, 0.0], conn)
    # Removing the whole tree also removes its FTS and vector projections.
    db.put("question", {**q, "deleted": True}, id=q["id"])
    assert not service.search("自由落体", **filters)
    with db.read() as conn:
        assert (
            conn.scalar(text("SELECT count(*) FROM question_parts WHERE question_id=:id"), {"id": q["id"]})
            == 0
        )


def test_citation_changes_exclude_derived_sections_and_startup_backfills(db: Database) -> None:
    book = db.put("book", {"title": "物理", "subject_id": "physics"})
    block = TextbookService(db).ingest_text(book["id"], "加速度的定义")[0]
    item = question()
    item["parts"][0]["parts"][0]["explanation"]["citations"] = [
        {"block_id": block["id"], "revision": block["revision"], "book_id": book["id"]}
    ]
    q = db.put("question", item)
    service = QuestionIndex(db)
    assert service.search("速度变化率", parts=["explanation"])
    db.put("block", {**block, "text": "新版定义"}, id=block["id"])
    assert not service.search("速度变化率", parts=["explanation"])
    assert service.search("匀加速")
    # Simulate a pre-migration question; reconciliation reuses its ID and revision.
    with db.write() as conn:
        conn.exec_driver_sql("DROP TABLE question_vectors")
        conn.exec_driver_sql("DROP TABLE question_fts")
        conn.exec_driver_sql("DROP TABLE question_parts")
        conn.exec_driver_sql("UPDATE alembic_version SET version_num='0001'")
    initialize(db.settings)
    assert db.get("question", q["id"]) == q
    model = profile(db)
    reconcile_question_indexes(db)
    reconcile_question_indexes(db)
    assert db.get("question", q["id"])["revision"] == q["revision"]
    assert service.search("匀加速")[0]["question_id"] == q["id"]
    assert len([job for job in JobStore(db).list() if job["kind"] == "question_index"]) == 1
    with db.read() as conn:
        assert service.pending(q["id"], ModelProfile.model_validate(model).model_dump(), conn)


@pytest.mark.asyncio
async def test_automatic_index_reuses_sections_and_follows_edits_and_model_space(db: Database) -> None:
    model = profile(db)
    db.settings.question_embedding_batch_size = 1
    q = db.put(
        "question", {"subject_id": "physics", "type": "short_answer", "stem": "原题干", "answer": "答案甲"}
    )
    calls: list[list[str]] = []
    changed = False

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal changed
        wire = json.loads(request.content)
        calls.append(wire["input"])
        if not changed:
            changed = True
            old = db.get("question", q["id"])
            db.put("question", {**old, "stem": "新题干"}, id=q["id"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0] + [0.5] * (wire["dimensions"] - 1)}
                    for i in range(len(wire["input"]))
                ],
                "usage": {"prompt_tokens": 2},
            },
        )

    transport = httpx.MockTransport(respond)
    worker = Worker(db.settings, db, AIService(db, db.settings, transport=transport))
    try:
        job = worker.jobs.claim(worker.owner)
        assert job and job["kind"] == "question_index"
        await worker.execute(job)
        assert worker.jobs.get(job["id"])["status"] == "completed"
        service = QuestionIndex(db)
        with db.read() as conn:
            assert service.pending(q["id"], ModelProfile.model_validate(model).model_dump(), conn) == []
            assert conn.scalar(text("SELECT count(*) FROM question_vectors")) == 2
        before = len(calls)
        current = db.get("question", q["id"])
        db.put("question", {**current, "answer_confirmed": True}, id=q["id"])
        assert worker.jobs.claim(worker.owner) is None
        current = db.get("question", q["id"])
        db.put("question", {**current, "answer": "答案乙"}, id=q["id"])
        await worker.execute(worker.jobs.claim(worker.owner))
        assert len(calls) == before + 1 and calls[-1] == ["答案乙"]
        # New dimensions enqueue automatically on reconciliation, without reusing old space.
        db.put("model", {**model, "embedding_dimensions": 3}, id=model["id"])
        reconcile_question_indexes(db)
        await worker.execute(worker.jobs.claim(worker.owner))
        with db.read() as conn:
            latest = service.profile(conn)
            assert not service.pending(q["id"], latest, conn)
            assert {row[0] for row in conn.execute(text("SELECT DISTINCT dim FROM question_vectors"))} == {
                2,
                3,
            }
        hits = service.search(
            "test",
            mode="semantic",
            parts=["stem", "answer"],
            query_vector=[1.0, 0.5, 0.5],
            space_fingerprint=embedding_fingerprint(latest, 3),
        )
        assert len(hits) == 1 and hits[0]["question_id"] == q["id"]
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_independent_order_api_and_llm_limits(db: Database) -> None:
    model = profile(db)
    first = db.put("question", {"stem": "banana", "type": "short_answer", "subject_id": "physics"})
    second = db.put("question", {"stem": "apple", "type": "short_answer", "subject_id": "physics"})
    fingerprint = embedding_fingerprint(ModelProfile.model_validate(model).model_dump(), 2)
    service = QuestionIndex(db)
    with db.write() as conn:
        for item, vector in [(first, [1.0, 0.0]), (second, [0.0, 1.0])]:
            target = service.eligible(conn, item["id"])[0][0]
            service.store(target, fingerprint, vector, conn)
    calls: list[str] = []
    mutate_second = False

    def respond(request: httpx.Request) -> httpx.Response:
        if mutate_second:
            current = db.get("question", second["id"])
            db.put("question", {**current, "stem": "pear"}, id=second["id"])
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    transport = httpx.MockTransport(respond)
    ai = AIService(db, db.settings, transport=transport)
    jobs = JobStore(db)
    for job in jobs.list():
        jobs.cancel(job["id"])

    async def search(methods: list[str]) -> list[dict[str, Any]]:
        specification = SearchInput(query="apple", target="question", methods=methods, limit=1)
        resource = db.put("search", specification.model_dump())
        jobs.enqueue("search", resource["id"])
        active = jobs.claim("fixture")
        ctx = PipelineContext(db, jobs, ai, db.settings, active)
        result = await execute_search(ctx, specification)
        await ctx.finish({"hits": result})
        return result

    try:
        assert [hit["question_id"] for hit in await search(["keyword"])] == [second["id"]]
        assert calls == []
        assert [hit["question_id"] for hit in await search(["semantic", "keyword"])] == [
            first["id"],
            second["id"],
        ]
        assert [hit["question_id"] for hit in await search(["keyword", "semantic"])] == [
            second["id"],
            first["id"],
        ]
        assert SearchInput(query="x").methods == ["keyword"]
        with pytest.raises(ValidationError):
            SearchInput(query="x", mode="hybrid")
        with pytest.raises(ValidationError):
            SearchInput(query="x", methods=["keyword", "keyword"])
        # Scope and keyword caps also apply to model tools before any embedding request.
        book = db.put("book", {"title": "Physics", "subject_id": "physics"})
        block = TextbookService(db).ingest_text(book["id"], "apple apple")[0]
        resource = db.put("search", {"query": "apple"})
        jobs.enqueue("search", resource["id"])
        ctx = PipelineContext(db, jobs, ai, db.settings, jobs.claim("fixture"))
        tools = retrieval_tools(ctx, [book["id"]], "physics")
        schema = tools["search_textbook"][1]
        assert "hybrid" not in json.dumps(schema)
        assert (await tools["search_textbook"][2]({"query": "apple", "limit": 1}))[0]["id"] == block["id"]
        with pytest.raises(ValueError, match="输出条数"):
            await tools["search_textbook"][2](
                {"query": "apple", "limit": db.settings.tool_search_max_limit + 1}
            )
        assert await retrieval_tools(ctx, [])["search_textbook"][2]({"query": "apple"}) == []
        set_password(db, "fixture-password")
        with TestClient(create_app(db.settings), base_url="http://127.0.0.1:8765") as web:
            login = web.post("/api/login", json={"password": "fixture-password"}).json()
            web.headers.update({"X-CSRF-Token": login["csrf_token"]})
            response = web.post("/api/search", json={"query": "apple", "target": "question", "limit": 1})
            assert response.status_code == 200 and response.json()[0]["question_id"] == second["id"]
            assert web.post("/api/search", json={"query": "apple", "limit": 99999}).status_code == 422
            assert web.post("/api/search", json={"query": "apple", "mode": "hybrid"}).status_code == 422
            assert web.get("/api/search/options").json()["default_methods"] == ["keyword"]
        # A keyword match edited while the later vector request runs is not published.
        mutate_second = True
        assert [hit["question_id"] for hit in await search(["keyword", "semantic"])] == [first["id"]]
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_index_discards_space_changed_during_request_and_counts_usage(db: Database) -> None:
    model = profile(db)
    item = db.put("question", {"subject_id": "physics", "type": "short_answer", "stem": "加速度"})
    requested_dimensions: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        requested_dimensions.append(wire["dimensions"])
        if len(requested_dimensions) == 1:
            db.put("model", {**model, "embedding_dimensions": 3}, id=model["id"])
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0] * wire["dimensions"]}],
                "usage": {"prompt_tokens": 7},
            },
        )

    worker = Worker(db.settings, db, AIService(db, db.settings, transport=httpx.MockTransport(respond)))
    job = worker.jobs.claim(worker.owner)
    await worker.execute(job)
    saved = worker.jobs.get(job["id"])
    assert saved["status"] == "completed" and requested_dimensions == [2, 3]
    assert saved["checkpoint"]["embedding_usage"] == {"requests": 2, "input_tokens": 14}
    with db.read() as conn:
        assert conn.scalar(text("SELECT count(*) FROM question_vectors WHERE dim=2")) == 0
        service = QuestionIndex(db)
        assert not service.pending(item["id"], service.profile(conn), conn)
