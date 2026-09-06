"""One real-DB workflow covers extraction, canonical revision, index and explanation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from studyquip.ai import AIService
from studyquip.config import Settings
from studyquip.db import ConflictError, Database, initialize
from studyquip.jobs import JobStore
from studyquip.pipelines import PipelineContext
from studyquip.retrieval import RetrievalService, records
from studyquip.worker import Worker


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
