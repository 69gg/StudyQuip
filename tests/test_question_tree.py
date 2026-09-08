"""Recursive content, subject identity and resumable audio share real temporary storage."""

from __future__ import annotations

import copy
import io
import json
import time
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from studyquip.ai import AIService, ModelProfile
from studyquip.api import create_app
from studyquip.auth import set_password
from studyquip.config import Settings
from studyquip.db import ConflictError, Database, initialize
from studyquip.export import create_snapshot, public_snapshot
from studyquip.jobs import JobStore
from studyquip.media import store_upload
from studyquip.pipelines import QuestionDraft, merge_question_draft
from studyquip.question_tree import FigureSpec, audio_work, walk_questions
from studyquip.schemas import ExportInput, QuestionInput, validate_question
from studyquip.subjects import ensure_default_subjects, save_subject
from studyquip.worker import Worker


@pytest.fixture
def db(tmp_path: Path) -> Any:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    database = Database(settings)
    yield database
    database.close()


def tree(subject: str) -> dict[str, Any]:
    leaf = {
        "id": "leaf",
        "type": "single_choice",
        "stem": "What time is it?",
        "options": [{"id": "a", "text": "Three."}, {"id": "b", "text": "Four."}],
        "answer": "a",
        "error_reason": "听错了数字",
        "notes": "保留用户备注",
        "figure_asset_ids": [],
        "explanation": {"summary": "It is three.", "citations": []},
    }
    return {
        "subject_id": subject,
        "type": "composite",
        "stem": "",
        "parts": [{"id": "group", "type": "composite", "stem": "根据材料回答", "parts": [leaf]}],
        "materials": [
            {"id": "listening", "kind": "listening", "title": "对话", "text": "It is three o'clock."}
        ],
        "answer_confirmed": True,
    }


def test_default_subject_identity_recursive_save_confirmation_and_export(db: Database) -> None:
    original = db.put("subject", {"name": "英语"})
    ensure_default_subjects(db, db.settings.default_subjects)
    ensure_default_subjects(db, db.settings.default_subjects)
    assert len(db.list("subject")) == 5
    assert save_subject(db, "英语")["id"] == original["id"]
    item = db.put("question", tree(original["id"]))
    renamed = save_subject(db, "外语", original["id"], original["revision"])
    ensure_default_subjects(db, db.settings.default_subjects)
    assert len(db.list("subject")) == 5 and not any(s["name"] == "英语" for s in db.list("subject"))
    with pytest.raises(ConflictError):
        save_subject(db, "数学", original["id"], renamed["revision"])
    assert db.get("question", item["id"])["subject_id"] == original["id"]
    validate_question(item)
    set_password(db, "fixture-password")
    with TestClient(create_app(db.settings), base_url="http://127.0.0.1:8765") as client:
        session = client.post("/api/login", json={"password": "fixture-password"}).json()
        client.headers.update({"X-CSRF-Token": session["csrf_token"]})
        public = client.get(f"/api/questions/{item['id']}").json()
        saved = client.put(f"/api/questions/{item['id']}", json=public)
        assert saved.status_code == 200, saved.text
        current = saved.json()
        assert current["answer_confirmed"] and not current["explanation_stale"]
        assert current["parts"][0]["parts"][0]["explanation"]["summary"] == "It is three."
        current["parts"][0]["parts"][0]["stem"] = "What is the time?"
        current = client.put(f"/api/questions/{item['id']}", json=current).json()
        assert not current["answer_confirmed"] and current["explanation_stale"]
        assert current["parts"][0]["parts"][0]["explanation_stale"]
        confirmed = client.post(
            f"/api/questions/{item['id']}/confirm", json={"revision": current["revision"]}
        )
        assert confirmed.status_code == 200
    snapshot = create_snapshot(db, ExportInput(question_ids=[item["id"]], mode="practice"))
    assert public_snapshot(db, snapshot)["questions"][0]["parts"][0]["parts"][0]["answer"] == "a"
    with pytest.raises(ValueError, match="过期"):
        create_snapshot(db, ExportInput(question_ids=[item["id"]], mode="review"))
    bad = copy.deepcopy(item)
    bad["parts"][0]["parts"][0]["answer"] = "missing-option"
    with pytest.raises(ValueError, match="选项 ID"):
        validate_question(bad)
    bad = copy.deepcopy(item)
    bad["parts"].append(copy.deepcopy(bad["parts"][0]))
    with pytest.raises(ValidationError, match="ID 不能重复"):
        QuestionInput.model_validate(bad)


def test_ai_tree_draft_preserves_ids_answers_and_rejects_unsafe_figures() -> None:
    original = tree("subject")
    child = original["parts"][0]["parts"][0]
    draft = QuestionDraft.model_validate(
        {
            "type": "composite",
            "stem": "",
            "formatting_issues": [],
            "parts": [
                {
                    "id": "group",
                    "type": "composite",
                    "stem": "请根据材料作答",
                    "formatting_issues": [],
                    "parts": [
                        {
                            "id": "leaf",
                            "type": "single_choice",
                            "stem": "What is the time?",
                            "options": [
                                {"id": "b", "text": "Four o'clock."},
                                {"id": "a", "text": "Three o'clock."},
                            ],
                            "formatting_issues": [],
                            "answer_from_reference": "b",
                        }
                    ],
                }
            ],
        }
    )
    merged = merge_question_draft(original, draft, True)
    edited = merged["parts"][0]["parts"][0]
    assert edited["stem"] == "What is the time?" and edited["answer"] == "a"
    assert [option["id"] for option in edited["options"]] == ["a", "b"]
    assert edited["error_reason"] == child["error_reason"] and edited["notes"] == child["notes"]
    assert merged["materials"] == original["materials"]
    fresh = merge_question_draft({"type": "composite", "stem": "", "parts": []}, draft, False)
    fresh_ids = [node["id"] for node in walk_questions(fresh) if node.get("id")]
    assert len(fresh_ids) == len(set(fresh_ids)) and "leaf" not in fresh_ids and "group" not in fresh_ids
    draft.parts = []
    assert merge_question_draft(original, draft, True)["parts"] == original["parts"]
    for svg in [
        "<svg><script>alert(1)</script></svg>",
        '<svg><image href="https://example.invalid" /></svg>',
        '<svg onload="x()" />',
        '<svg><path fill="url(https://example.invalid)" /></svg>',
    ]:
        with pytest.raises(ValidationError):
            FigureSpec(kind="svg", svg=svg)
    assert FigureSpec(kind="svg", svg='<svg viewBox="0 0 30 20"><path d="M0 0L30 20" stroke="black" /></svg>')


@pytest.mark.asyncio
async def test_audio_completion_resumes_finished_materials_with_latest_config(db: Database) -> None:
    settings = db.settings
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 3200)
    asset = store_upload(settings, "recording.wav", buffer.getvalue())
    db.put("asset", asset, id=asset["id"])
    question = tree(db.put("subject", {"name": "英语"})["id"])
    question["materials"] = [
        {"id": "recorded", "kind": "listening", "text": "", "audio_asset_id": asset["id"]},
        {"id": "written", "kind": "listening", "text": "Hello, world."},
        {"id": "complete", "kind": "listening", "text": "Do not replace.", "audio_asset_id": asset["id"]},
    ]
    question = db.put("question", question)
    for role in ("speech_recognition", "speech_synthesis"):
        db.put(
            "model",
            ModelProfile(
                role=role,
                model=role,
                voice="fixture-voice",
                api_key="fixture",
                base_url="https://fixture.invalid/v1",
                retries=0,
            ).model_dump(),
        )
    called: list[str] = []
    fail = True

    def respond(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path.endswith("/transcriptions"):
            assert b"recording.wav" in request.content
            return httpx.Response(200, json={"text": "Recognized transcript."})
        wire = json.loads(request.content)
        assert wire["voice"] == "fixture-voice" and wire["input"] == "Hello, world."
        assert "thinking" not in wire and "stream" not in wire
        if fail:
            return httpx.Response(503, json={"error": {"message": "fixture outage"}})
        assert wire["model"] == "updated-speech-model"
        return httpx.Response(200, content=b"ID3-fixture-audio", headers={"content-type": "audio/mpeg"})

    worker = Worker(settings, db, AIService(db, settings, transport=httpx.MockTransport(respond)))
    jobs = JobStore(db)
    job = jobs.enqueue("question_audio", question["id"])
    await worker.execute(jobs.claim(worker.owner))
    failed = jobs.get(job["id"])
    assert failed["status"] == "failed" and "recorded" in failed["checkpoint"]["audio_results"]
    assert db.get("question", question["id"])["materials"][0]["text"] == ""
    profile = db.list("model", filters={"role": "speech_synthesis"})[0]
    db.put("model", {**profile, "model": "updated-speech-model"}, id=profile["id"])
    fail = False
    jobs.resume(job["id"], time.time())
    await worker.execute(jobs.claim(worker.owner))
    current = db.get("question", question["id"])
    assert jobs.get(job["id"])["status"] == "completed"
    assert called.count("/v1/audio/transcriptions") == 1 and len(called) == 3
    assert current["materials"][0]["text"] == "Recognized transcript."
    assert current["materials"][1]["audio_asset_id"] and current["materials"][1]["audio_generated_from"]
    assert current["materials"][2] == question["materials"][2]
    assert not list(audio_work(current))
    current["materials"][1]["text"] = "Changed transcript."
    assert [role for _, _, role in audio_work(current)] == ["speech_synthesis"]


@pytest.mark.asyncio
async def test_recursive_explanation_carries_ancestors_and_scoped_tools(db: Database) -> None:
    subject = db.put("subject", {"name": "英语"})
    root = db.put("question", tree(subject["id"]))
    db.put(
        "model",
        ModelProfile(
            role="question_text",
            stream=False,
            model="fixture",
            api_key="fixture",
            base_url="https://fixture.invalid/v1",
            retries=0,
        ).model_dump(),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        prompt = wire["messages"][1]["content"][0]["text"]
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        assert payload["question"]["id"] == "leaf"
        assert payload["ancestor_materials"][0]["materials"][0]["text"] == "It is three o'clock."
        assert payload["allowed_book_ids"] == []
        assert "search_textbook" in {item["function"]["name"] for item in wire["tools"]}
        return httpx.Response(
            200,
            json={
                "id": "test",
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
                                    "id": "call",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_result",
                                        "arguments": json.dumps(
                                            {
                                                "summary": "正确识别时间",
                                                "steps": ["听取时间。"],
                                                "knowledge_points": ["时间表达"],
                                                "citations": [],
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    worker = Worker(db.settings, db, AIService(db, db.settings, transport=httpx.MockTransport(respond)))
    job = worker.jobs.enqueue("question_explain", root["id"])
    await worker.execute(worker.jobs.claim(worker.owner))
    result = worker.jobs.get(job["id"])
    assert result["status"] == "completed", result["error"]
    current = db.get("question", root["id"])
    assert list(walk_questions(current))[-1]["explanation"]["summary"] == "正确识别时间"
    assert not current["explanation_stale"]
    assert create_snapshot(db, ExportInput(question_ids=[root["id"]], mode="review"))
