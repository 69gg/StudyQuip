"""One real-DB workflow covers extraction, canonical revision, index and explanation."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from studyquip.ai import AIService, ModelProfile
from studyquip.api import create_app
from studyquip.auth import set_password
from studyquip.config import Settings
from studyquip.context import node_source_fingerprint
from studyquip.db import ConflictError, Database, initialize, jobs_table
from studyquip.jobs import JobStore
from studyquip.pipelines import PipelineContext
from studyquip.progress import present_jobs
from studyquip.retrieval import RetrievalService, records
from studyquip.textbook import TextbookService
from studyquip.worker import Worker


@pytest.mark.asyncio
async def test_worker_startup_restores_legacy_review_pages_without_losing_text_or_schedule(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    db = Database(settings)
    try:
        jobs = JobStore(db)
        book = db.put("book", {"title": "旧待校对页"})
        cases: list[tuple[dict[str, Any], str]] = [
            ({"text": "PDF 提取字", "recognition_draft": "AI 草稿，下一页续接"}, "AI 草稿，下一页续接"),
            ({"text": "人工校正", "recognition_draft": "AI 草稿", "human_edited": True}, "人工校正"),
            ({"text": "旧 PDF 文字", "recognition_draft": "", "is_blank": True}, ""),
            ({"text": "没有候选时保留已有文字"}, "没有候选时保留已有文字"),
        ]
        pages = [
            db.put(
                "page",
                {
                    **data,
                    "book_id": book["id"],
                    "index": index,
                    "status": "needs_review",
                    "quality": "uncertain",
                    "issues": ["页尾截断"],
                    "original_text": "原件始终保留",
                },
            )
            for index, (data, _) in enumerate(cases)
        ]
        checkpoint = {"completed_units": ["already-committed"], "usage": {"requests": 7}}
        waiting = jobs.enqueue("book_process", book["id"], not_before=time.time() + 3600)
        other_jobs: list[dict[str, Any]] = []
        with db.write() as conn:
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == waiting["id"])
                .values(
                    status="waiting_review",
                    error="教材第 1 个输入页需要重新识别、校对或跳过",
                    checkpoint=checkpoint,
                )
            )
            # Review for other reasons and failed/cancelled jobs still require the user's resume.
            for status in ("failed", "cancelled", "waiting_review"):
                other = db.put("book", {"title": status}, conn=conn)
                db.put("page", {"book_id": other["id"], "status": "needs_review", "text": status}, conn=conn)
                job = jobs.enqueue("book_process", other["id"], conn=conn)
                conn.execute(
                    jobs_table.update()
                    .where(jobs_table.c.id == job["id"])
                    .values(status=status, error="另一个需要处理的原因", checkpoint=checkpoint)
                )
                other_jobs.append(jobs.get(job["id"], conn=conn))
        deleted = db.put("book", {"title": "已删除", "deleted": True})
        untouched = [
            db.put("page", {"book_id": book["id"], "status": status, "text": status})
            for status in ("processed", "skipped", "draft")
        ]
        untouched.append(db.put("page", {"book_id": deleted["id"], "status": "needs_review"}))

        # Exercise the startup hook in this temporary fixture, with task dispatch disabled.
        worker = Worker(settings, db)
        stop = asyncio.Event()
        stop.set()
        await worker.run(stop)
        for page, (_, expected) in zip(pages, cases, strict=True):
            current = db.get("page", page["id"])
            assert current["status"] == "draft" and current["text"] == expected
            assert current["original_text"] == page["original_text"]
            assert current["revision"] == page["revision"] + 1
            assert "quality" not in current and "issues" not in current
            assert db.history("page", page["id"])[0]["text"] == page["text"]
        resumed = jobs.get(waiting["id"])
        assert resumed["status"] == "queued" and resumed["error"] is None
        assert resumed["checkpoint"] == checkpoint and resumed["not_before"] == waiting["not_before"]
        assert resumed["attempts"] == 0
        assert all(jobs.get(job["id"]) == job for job in other_jobs)
        assert all(db.get("page", page["id"]) == page for page in untouched)
        assert jobs.restore_review_pages() == 0
        assert all(db.get("page", page["id"])["revision"] == page["revision"] + 1 for page in pages)
        projected = next(job for job in present_jobs(db) if job["id"] == waiting["id"])
        assert projected["progress"]["recognized_pages"] == 6
        assert projected["progress"]["review_pages"] == 0
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_checkpoint", [False, True])
async def test_resume_keeps_recognized_pages_and_full_unfinished_tool_history(
    tmp_path: Path, legacy_checkpoint: bool
) -> None:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    db = Database(settings)
    try:
        book = db.put("book", {"title": "续接样本", "asset_ids": []})
        root = TextbookService(db).ensure_root(book["id"])
        pages = [
            db.put(
                "page",
                {
                    "book_id": book["id"],
                    "index": index,
                    "page_index": index,
                    "source_asset_id": "fixture-original",
                    "status": status,
                    "text": f"第 {index + 1} 页正文",
                },
            )
            for index, status in enumerate(["processed", "draft", "skipped"])
        ]
        first_block = db.put(
            "block",
            {
                "book_id": book["id"],
                "node_id": root["id"],
                "text": pages[0]["text"],
                "order": 0,
                "source_page_ids": [pages[0]["id"]],
            },
        )
        configured = db.put(
            "model",
            {
                "role": "chat",
                "base_url": "https://fixture.invalid/v1",
                "api_key": "fixture",
                "model": "fixture",
                "context_tokens": None,
                "retries": 0,
            },
        )
        requests: list[dict[str, Any]] = []
        saved_transcript: list[dict[str, Any]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            wire = json.loads(request.content)
            requests.append(wire)
            if len(requests) == 2:
                # Interrupt after the response and read-tool result have been committed.
                raise RuntimeError("模拟中断")
            if len(requests) == 1:
                name, arguments = "read_block", {"block_id": first_block["id"]}
            else:
                if legacy_checkpoint:
                    assert wire["messages"][2:] == saved_transcript
                else:
                    assert wire["model"] == "latest-model"
                    assert wire["reasoning_effort"] == "high"
                    assert wire["messages"][2:] == []
                name, arguments = (
                    "submit_result",
                    {
                        "operations": [
                            {
                                "op": "insert",
                                "id": "continued-block",
                                "block": {
                                    "text": pages[1]["text"],
                                    "node_id": root["id"],
                                    "type": "paragraph",
                                },
                            }
                        ],
                        "reason": "续接第二页",
                        "working_summary": "已完成第二页",
                        "current_node_id": root["id"],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "opaque-fixture-" * 3000,
                                "tool_calls": [
                                    {
                                        "id": f"call-{len(requests)}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments, ensure_ascii=False),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )

        worker = Worker(settings, db, AIService(db, settings, transport=httpx.MockTransport(respond)))
        jobs = JobStore(db)
        task = jobs.enqueue("book_process", book["id"])
        claimed = jobs.claim(worker.owner)
        assert claimed
        await worker.execute(claimed)
        failed = jobs.get(task["id"])
        assert failed and failed["status"] == "failed" and "模型请求失败" in failed["error"]
        checkpoint = copy.deepcopy(failed["checkpoint"])
        stage = checkpoint["stages"][f"revise:{pages[1]['id']}:1:0:0"]
        assert stage["pending"] == [] and stage["rounds"] == 1
        saved_transcript = copy.deepcopy(stage["transcript"])
        assert len(saved_transcript) == 2
        if legacy_checkpoint:
            # Existing installations have these transcripts but no initial-input snapshot yet.
            stage.pop("request_context")
            checkpoint.pop("revision_plans")
            with db.write() as conn:
                conn.execute(
                    jobs_table.update().where(jobs_table.c.id == task["id"]).values(checkpoint=checkpoint)
                )
        else:
            # An obsolete material ceiling must not block a resumed unit; model edits take effect.
            checkpoint["revision_plans"][f"{pages[1]['id']}:1"]["context_budget"] = 512
            with db.write() as conn:
                conn.execute(
                    jobs_table.update().where(jobs_table.c.id == task["id"]).values(checkpoint=checkpoint)
                )
            db.put(
                "model",
                {**configured, "model": "latest-model", "reasoning_effort": "high"},
                id=configured["id"],
            )
        set_password(db, "fixture-password")
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
            session = client.post("/api/login", json={"password": "fixture-password"}).json()
            client.headers.update({"X-CSRF-Token": session["csrf_token"]})
            projected = client.get("/api/jobs").json()[0]
            assert projected["resume"]["available"] and projected["resume"]["has_saved_progress"]
            assert projected["progress"]["recognized_pages"] == 2
            assert projected["progress"]["processed_pages"] == 1
            assert "opaque-fixture" not in json.dumps(projected) and "checkpoint" not in projected
            response = client.post(f"/api/jobs/{task['id']}/resume", json={"delay_seconds": 3600})
            assert response.status_code == 200, response.text
            scheduled = response.json()
            assert scheduled["id"] == task["id"] and scheduled["not_before"] > time.time() + 3500
            repeated = client.post(f"/api/jobs/{task['id']}/resume", json={}).json()
            assert repeated["reused"] and repeated["not_before"] == scheduled["not_before"]
            assert jobs.get(task["id"])["checkpoint"] == checkpoint
            assert jobs.claim(worker.owner) is None
            assert client.post(f"/api/jobs/{task['id']}/reschedule", json={}).status_code == 200
        resumed = jobs.claim(worker.owner)
        assert resumed and resumed["id"] == task["id"] and resumed["lease_token"] != claimed["lease_token"]
        await worker.execute(resumed)
        current = jobs.get(task["id"])
        assert current and current["status"] == "completed"
        assert len(requests) == 3  # No repeated recognition, first-page revision, or tool read.
        assert db.get("page", pages[0]["id"]) == pages[0]
        assert db.get("block", first_block["id"]) == first_block
        assert db.get("page", pages[1]["id"])["status"] == "processed"
        assert len(records(db, "block", {"book_id": book["id"]})) == 2
        assert len(current["checkpoint"]["completed_units"]) == 1
        assert current["checkpoint"]["stages"][f"revise:{pages[1]['id']}:1:0:0"]["usage"]["requests"] == 2
        with pytest.raises(ConflictError, match="已结束"):
            jobs.resume(task["id"], time.time())
        # Reuse this saved input to check source fencing on a failed task, without dispatching again.
        for pending in jobs.list():
            if pending["kind"] == "book_index":
                jobs.cancel(pending["id"])
        with db.write() as conn:
            conn.execute(jobs_table.update().where(jobs_table.c.id == task["id"]).values(status="failed"))
        updated = db.put("book", {**book, "title": "用户修改了教材"}, id=book["id"])
        with pytest.raises(ConflictError, match="输入已修改"):
            jobs.resume(task["id"], time.time())
        projection = next(item for item in present_jobs(db) if item["id"] == task["id"])
        assert not projection["resume"]["available"]
        assert jobs.get(task["id"])["checkpoint"] == current["checkpoint"]
        db.put("book", {**updated, "deleted": True}, id=book["id"])
        with pytest.raises(ConflictError, match="已删除"):
            jobs.resume(task["id"], time.time())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_book_question_workflow_uses_confirmed_answers_and_fenced_results(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    db = Database(settings)
    subject = db.put("subject", {"name": "物理"})
    book = db.put(
        "book",
        {"title": "物理", "subject_id": subject["id"], "text": "牛顿第一定律又称惯性定律。", "asset_ids": []},
    )
    db.put(
        "model",
        {
            "name": "mock",
            "role": "chat",
            "base_url": "https://provider.example/v1",
            "api_key": "fixture",
            "model": "fixture",
            "context_tokens": None,
        },
    )
    db.put(
        "model",
        {
            "name": "mock embedding",
            "role": "embedding",
            "base_url": "https://provider.example/v1",
            "api_key": "fixture",
            "model": "fixture-embedding",
            "context_tokens": None,
            "embedding_dimensions": 3,
        },
    )
    embedding_inputs: list[list[str]] = []
    explanation_inputs: list[dict[str, Any]] = []
    original_reason = "当时把定律名字记混了，我好像没记牢。"
    optimized_reason = "我当时混淆了定律的名称，可能还没有记牢。"

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        assert "context_tokens" not in wire
        if request.url.path.endswith("/embeddings"):
            embedding_inputs.append(wire["input"])
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "fixture-embedding",
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [1.0, 0.5, 0.25]}
                        for index in range(len(wire["input"]))
                    ],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                },
            )
        prompt = wire["messages"][1]["content"][0]["text"]
        if "按原始顺序把当前教材草稿" in prompt:
            result: dict[str, Any] = {
                "operations": [
                    {
                        "op": "insert",
                        "id": "newton",
                        "block": {
                            "text": "牛顿第一定律又称惯性定律。",
                            "node_id": f"root:{book['id']}",
                            "type": "paragraph",
                        },
                    }
                ],
                "reason": "保存正文",
                "working_summary": "惯性定律",
                "open_anchors": [],
                "concepts": [
                    {
                        "name": "牛顿第一定律",
                        "aliases": ["惯性定律"],
                        "evidence": [
                            {"block_id": "newton", "revision": 1, "quote": "牛顿第一定律又称惯性定律。"}
                        ],
                    }
                ],
                "relations": [],
                "closed_node_ids": [],
            }
        elif "生成简短概述" in prompt:
            payload = json.loads(prompt.split("\n", 1)[1])
            result = {
                "summaries": [
                    {"node_id": item["node_id"], "summary": "介绍牛顿第一定律和惯性。"} for item in payload
                ]
            }
        elif "识别一道错题" in prompt:
            result = {
                "type": "short_answer",
                "stem": "牛顿第一定律又称什么？",
                "options": [],
                "answer_from_reference": "惯性定律",
                "reference_analysis": "参考答案给出了惯性定律。",
            }
        elif "生成讲解、分步分析" in prompt:
            explanation_inputs.append(json.loads(prompt.rsplit("\n", 1)[1]))
            result = {
                "summary": "牛顿第一定律也称惯性定律。",
                "steps": ["依据教材定义作答。"],
                "knowledge_points": ["惯性"],
                "citations": [{"block_id": "newton", "revision": 1, "quote": "牛顿第一定律又称惯性定律。"}],
                "answer_conflict": None,
                # 故意在未授权时也返回该字段，验证服务端不会接受。
                "error_reason_optimized": optimized_reason,
            }
        else:
            raise AssertionError(prompt[:100])
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "submit",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_result",
                                        "arguments": json.dumps(result, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    worker = Worker(settings, db, AIService(db, settings, transport=httpx.MockTransport(respond)))
    jobs = JobStore(db)

    async def run_next() -> dict[str, Any]:
        job = jobs.claim(worker.owner)
        assert job is not None
        await worker._dispatch(job)
        current = jobs.get(job["id"])
        assert current is not None
        assert current["status"] == "completed", current.get("error")
        return current

    jobs.enqueue("book_process", book["id"])
    await run_next()
    assert records(db, "page", {"book_id": book["id"]})[0]["status"] == "processed"
    assert len(records(db, "block", {"book_id": book["id"]})) == 1
    await run_next()  # Index job is enqueued in the book completion transaction.
    assert embedding_inputs
    indexed_book = db.get("book", book["id"])
    assert indexed_book and indexed_book["embedding_pending"] is False
    assert RetrievalService(db).search("惯性定律", book_ids=[book["id"]], mode="keyword")
    search = db.put("search", {"query": "惯性定律", "mode": "semantic", "book_ids": [book["id"]]})
    search_job = jobs.enqueue("search", search["id"])
    assert (await run_next())["result"]["hits"]
    projection = next(item for item in present_jobs(db) if item["id"] == search_job["id"])
    assert projection["progress"]["usage"] == {"requests": 1, "input_tokens": 10}
    assert not projection["progress"].get("embedding_activity")
    question = db.put(
        "question",
        {
            "subject_id": subject["id"],
            "type": "short_answer",
            "stem": "",
            "options": [],
            "answer": None,
            "answer_confirmed": False,
            "reference_text": "标准答案：惯性定律",
            "notes": "保留我的备注",
            "error_reason": original_reason,
            "book_ids": [],
        },
    )
    jobs.enqueue("question_extract", question["id"])
    await run_next()
    current = db.get("question", question["id"])
    assert current and current["answer"] == "惯性定律" and current["answer_confirmed"] is False
    assert current["notes"] == "保留我的备注"
    assert current["error_reason"] == original_reason
    current = db.put(
        "question",
        {**current, "answer_confirmed": True},
        id=current["id"],
        expected_revision=current["revision"],
    )
    jobs.enqueue("question_explain", question["id"])
    await run_next()
    explained = db.get("question", question["id"])
    assert explained and explained["explanation"]["has_textbook_evidence"]
    assert explained["explanation"]["citations"][0]["book_title"] == "物理"
    assert "page" not in explained["explanation"]["citations"][0]
    assert explained["error_reason"] == original_reason
    assert explained["explanation"]["error_reason_optimized"] is None
    assert not explanation_inputs[-1]["error_reason_optimization_enabled"]
    for enabled, reason, expected in (
        (True, original_reason, optimized_reason),
        (True, "  \n  ", None),
        (False, original_reason, None),
    ):
        current = db.get("question", question["id"])
        assert current is not None
        db.put(
            "question",
            {**current, "error_reason": reason, "optimize_error_reason": enabled, "explanation_stale": True},
            id=current["id"],
            expected_revision=current["revision"],
        )
        call_count = len(explanation_inputs)
        jobs.enqueue("question_explain", question["id"])
        await run_next()
        current = db.get("question", question["id"])
        assert current is not None
        assert current["error_reason"] == reason and current["notes"] == "保留我的备注"
        assert current["answer_confirmed"] and not current["explanation_stale"]
        assert current["explanation"]["error_reason_optimized"] == expected
        assert explanation_inputs[-1]["error_reason_optimization_enabled"] is bool(enabled and reason.strip())
        assert len(explanation_inputs) == call_count + 1  # 同一次讲解请求完成，不额外润色调用。
    page = records(db, "page", {"book_id": book["id"]})[0]
    suggestion = db.put("suggestion", {"book_id": book["id"], "status": "pending", "operations": []})
    contexts: list[PipelineContext] = []
    for kind, identifier, payload in [
        ("page_recognize", page["id"], {}),
        ("suggestion_regenerate", suggestion["id"], {"book_id": book["id"]}),
    ]:
        jobs.enqueue(kind, identifier, payload)
        child = jobs.claim(worker.owner)
        assert child is not None
        context = PipelineContext(db, jobs, worker.ai, settings, child)
        await context.guard()
        contexts.append(context)
    current_book = db.get("book", book["id"])
    assert current_book is not None
    db.put("book", {**current_book, "deleted": True}, id=book["id"])
    for context in contexts:
        with pytest.raises(ConflictError, match="教材已删除"):
            await context.guard()
        with pytest.raises(ConflictError, match="教材已删除"):
            await worker._dispatch(context.job)
        with pytest.raises(ConflictError, match="教材已删除"):
            await context.commit(
                mutate=lambda conn: db.put("marker", {"invalid": True}, id="deleted-write", conn=conn)
            )
        with pytest.raises(ConflictError, match="教材已删除"):
            await context.finish({"invalid": True})
    assert db.get("marker", "deleted-write") is None
    db.close()


@pytest.mark.asyncio
async def test_unlimited_summary_and_embedding_batches_do_not_reuse_old_partial_summaries(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    db = Database(settings)
    try:
        book = db.put("book", {"title": "不设上下文上限"})
        root = TextbookService(db).ensure_root(book["id"])
        sources: dict[str, str] = {}
        for index in range(2):
            node = db.put("node", {"book_id": book["id"], "parent_id": root["id"], "title": f"课题 {index}"})
            sources[node["id"]] = f"课题 {index} 的完整内容。" * 1500
            db.put("block", {"book_id": book["id"], "node_id": node["id"], "text": sources[node["id"]]})
            fingerprint = node_source_fingerprint(db, node["id"])
            db.put(
                "summary_part",
                {
                    "book_id": book["id"],
                    "node_id": node["id"],
                    "source_fp": fingerprint,
                    "text": "旧首段概述",
                },
                id=f"{node['id']}:{fingerprint}:0",
            )
        for role in ("chat", "embedding"):
            db.put(
                "model",
                {
                    "role": role,
                    "model": role,
                    "base_url": "https://fixture.invalid/v1",
                    "api_key": "fixture",
                    "context_tokens": None,
                    "embedding_dimensions": 3,
                },
            )
        summary_inputs: list[list[dict[str, Any]]] = []
        embedding_inputs: list[list[str]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            wire = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                embedding_inputs.append(wire["input"])
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": i, "embedding": [1.0, 0.5, 0.25]} for i in range(len(wire["input"]))
                        ]
                    },
                )
            prompt = wire["messages"][1]["content"][0]["text"]
            batch = json.loads(prompt.split("\n", 1)[1])
            summary_inputs.append(batch)
            result = {"summaries": [{"node_id": item["node_id"], "summary": "新完整概述"} for item in batch]}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "submit",
                                        "type": "function",
                                        "function": {
                                            "name": "submit_result",
                                            "arguments": json.dumps(result),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )

        worker = Worker(settings, db, AIService(db, settings, transport=httpx.MockTransport(respond)))
        task = worker.jobs.enqueue("book_index", book["id"])
        claimed = worker.jobs.claim(worker.owner)
        assert claimed
        await worker.execute(claimed)
        finished = worker.jobs.get(task["id"])
        assert finished["status"] == "completed", finished["error"]
        assert len(summary_inputs) == 2  # Both complete child texts together, then the root.
        assert {item["node_id"]: item["text"] for item in summary_inputs[0]} == sources
        assert all(db.get("node", node_id)["summary"] == "新完整概述" for node_id in sources)
        assert len(embedding_inputs) == 1
        assert all(source in embedding_inputs[0] for source in sources.values())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_embedding_change_restarts_index_in_new_space_without_mislabelling(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, summary_batch_size=1)
    initialize(settings)
    db = Database(settings)
    try:
        book = db.put("book", {"title": "空间切换测试"})
        for index in range(2):
            db.put("block", {"book_id": book["id"], "text": f"正文 {index}", "order": index})
        configured = db.put(
            "model",
            {
                "name": "embedding",
                "role": "embedding",
                "model": "before",
                "base_url": "https://provider.example/v1",
                "api_key": "fixture",
                "embedding_dimensions": 3,
            },
        )
        calls: list[tuple[str, int]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            wire = json.loads(request.content)
            calls.append((wire["model"], wire["dimensions"]))
            if len(calls) == 1:
                db.put(
                    "model", {**configured, "model": "after", "embedding_dimensions": 4}, id=configured["id"]
                )
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5] * wire["dimensions"]}]})

        worker = Worker(settings, db, AIService(db, settings, transport=httpx.MockTransport(respond)))
        jobs = JobStore(db)
        task = jobs.enqueue("book_index", book["id"])
        first = jobs.claim(worker.owner)
        assert first
        await worker.execute(first)
        assert jobs.get(task["id"])["status"] == "waiting_window"
        resumed = jobs.claim(worker.owner)
        assert resumed
        await worker.execute(resumed)
        assert jobs.get(task["id"])["status"] == "completed"
        assert calls == [("before", 3), ("after", 4), ("after", 4)]
        assert (
            len(
                RetrievalService(db).existing_embeddings(
                    book["id"], ModelProfile.model_validate(db.get("model", configured["id"])).model_dump()
                )
            )
            == 2
        )
        assert jobs.get(task["id"])["checkpoint"]["embedding_completed"] == 2
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("workload", ["queued_jobs", "book_pages"])
async def test_parallel_work_uses_model_capacity_without_hidden_task_or_page_limit(
    tmp_path: Path, workload: str
) -> None:
    settings = Settings(data_dir=tmp_path, worker_poll_seconds=0.01)
    initialize(settings)
    db = Database(settings)
    jobs = JobStore(db)
    limit, count = 12, 16
    profile = ModelProfile(
        base_url="https://provider.example/v1",
        api_key="fixture",
        model="concurrency-fixture",
        max_concurrency=limit,
    )
    release, saturated, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active, peak = 0, 0
    revision_order: list[str] = []
    recognition_count = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak, recognition_count
        wire = json.loads(request.content)
        if wire["model"] == "revision-fixture":
            prompt = wire["messages"][1]["content"][0]["text"]
            revision_order.append(json.loads(prompt.split("\n", 1)[1])["current_page"]["id"])
            result: dict[str, Any] = {"operations": [], "reason": "并发验收", "working_summary": "已校对"}
        else:
            active += 1
            peak = max(peak, active)
            if active == limit:
                saturated.set()
            try:
                await release.wait()
            finally:
                active -= 1
            if workload == "queued_jobs":
                result = {"ok": True}
            else:
                recognition_count += 1
                schema = wire["tools"][0]["function"]["parameters"]["properties"]
                assert "quality" not in schema and "issues" not in schema
                # Legacy model output is accepted without remediation calls or a review barrier.
                result = {"text": "页尾没有结束的句", "quality": "uncertain", "issues": ["句子截断"]}
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": wire["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "submit",
                                    "type": "function",
                                    "function": {"name": "submit_result", "arguments": json.dumps(result)},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    worker = Worker(settings, db, AIService(db, settings, transport=httpx.MockTransport(respond)))
    page_ids: list[str] = []
    if workload == "queued_jobs":
        for _ in range(count):
            model = db.put("model", profile.model_dump())
            jobs.enqueue("model_test", model["id"])
        execution = asyncio.create_task(worker.run(stop))
    else:
        db.put("model", profile.model_copy(update={"role": "vision"}).model_dump())
        db.put("model", profile.model_copy(update={"model": "revision-fixture"}).model_dump())
        book = db.put("book", {"title": "并行页识别验收", "asset_ids": []})
        TextbookService(db).ensure_root(book["id"])
        for index in range(count):
            page = db.put(
                "page",
                {
                    "book_id": book["id"],
                    "index": index,
                    "page_index": index,
                    "source_type": "image",
                    "source_asset_id": "fixture-source",
                    "status": "pending",
                },
            )
            page_ids.append(page["id"])
        jobs.enqueue("book_process", book["id"])
        claimed = jobs.claim(worker.owner)
        assert claimed is not None
        execution = asyncio.create_task(worker._dispatch(claimed))
    try:
        # Hold actual SDK transport responses: both paths must exceed the old ceiling
        # before any request completes, while the configured model cap still applies.
        await asyncio.wait_for(saturated.wait(), 10)
        assert peak == limit
        release.set()
        if workload == "queued_jobs":
            async with asyncio.timeout(15):
                while True:
                    states = await asyncio.to_thread(jobs.list)
                    assert all(job["status"] != "failed" for job in states), states
                    if all(job["status"] == "completed" for job in states):
                        break
                    await asyncio.sleep(0.01)
            stop.set()
        await asyncio.wait_for(execution, 15)
        assert peak == limit and active == 0
        assert not any(worker.ai.limiter._models.values())
        if workload == "book_pages":
            assert revision_order == page_ids
            assert recognition_count == count
            assert all(db.get("page", page_id)["status"] == "processed" for page_id in page_ids)
            assert all("quality" not in db.get("page", page_id) for page_id in page_ids)
    finally:
        release.set()
        stop.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        db.close()
