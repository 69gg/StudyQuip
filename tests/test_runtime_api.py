"""只覆盖跨进程互斥、提交权与用户确认这些高风险边界。"""

from __future__ import annotations

import io
import multiprocessing
import time
from pathlib import Path
from typing import Any

import portalocker
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from studyquip.api import create_app
from studyquip.auth import set_password
from studyquip.config import Settings
from studyquip.db import ConflictError, Database, file_lock, initialize, jobs_table
from studyquip.export import public_snapshot
from studyquip.jobs import JobStore, LeaseLost
from studyquip.media import crop_asset, safe_path, store_upload
from studyquip.progress import present_jobs
from studyquip.schemas import validate_question


def _claim_process(directory: str, barrier: Any, queue: Any) -> None:
    db = Database(Settings(data_dir=Path(directory)))
    barrier.wait(timeout=15)
    claimed = JobStore(db).claim(str(multiprocessing.current_process().pid))
    queue.put(claimed["lease_token"] if claimed else None)
    db.close()


def _try_lock(path: str, shared: bool, queue: Any) -> None:
    try:
        with file_lock(Path(path), shared=shared, timeout=0.1):
            queue.put(True)
    except portalocker.exceptions.LockException:
        queue.put(False)


def _enqueue_process(directory: str, barrier: Any, queue: Any) -> None:
    db = Database(Settings(data_dir=Path(directory)))
    try:
        barrier.wait(timeout=15)
        queue.put(JobStore(db).enqueue("book_process", "book")["id"])
    finally:
        db.close()


@pytest.fixture
def database(tmp_path: Path) -> Any:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    db = Database(settings)
    yield db
    db.close()


def test_two_process_claim_and_stale_fencing(database: Database) -> None:
    jobs = JobStore(database)
    job = jobs.enqueue("probe", "resource")
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_claim_process, args=(str(database.settings.data_dir), barrier, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    values = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sum(value is not None for value in values) == 1
    old = jobs.get(job["id"])
    assert old
    with database.write() as conn:
        conn.execute(jobs_table.update().where(jobs_table.c.id == job["id"]).values(lease_until=0))
    new = jobs.claim("replacement")
    assert new and new["lease_token"] != old["lease_token"]
    assert not jobs.renew(old["id"], old["owner"], old["lease_token"])
    with pytest.raises(LeaseLost):
        jobs.finish(
            old["id"],
            old["owner"],
            old["lease_token"],
            mutate=lambda conn: database.put("probe", {"bad": True}, id="stale", conn=conn),
        )
    assert database.get("probe", "stale") is None

    def expire_during_commit(conn: Any) -> None:
        database.put("probe", {"bad": True}, id="mid-commit", conn=conn)
        conn.execute(jobs_table.update().where(jobs_table.c.id == new["id"]).values(lease_until=0))

    with pytest.raises(LeaseLost):
        jobs.finish(new["id"], new["owner"], new["lease_token"], mutate=expire_during_commit)
    assert database.get("probe", "mid-commit") is None
    with database.read() as conn:
        assert conn.scalar(select(jobs_table.c.lease_until).where(jobs_table.c.id == new["id"])) > 0


def test_waiting_dependencies_do_not_starve_ready_jobs(database: Database) -> None:
    jobs = JobStore(database)
    with database.write() as conn:
        future = jobs.enqueue("probe", "future", not_before=jobs.now(conn) + 3600, conn=conn)
        for index in range(101):
            jobs.enqueue("probe", str(index), {"depends_on": [future["id"]]}, conn=conn)
        invalid = jobs.enqueue("probe", "invalid", {"depends_on": ["missing"]}, conn=conn)
        ready = jobs.enqueue("probe", "ready", conn=conn)
    claimed = jobs.claim("worker")
    assert claimed and claimed["id"] == ready["id"]
    assert jobs.get(invalid["id"])["status"] == "failed"


def test_duplicate_submission_and_progress_survive_refresh_and_expired_lease(database: Database) -> None:
    database.put("book", {"title": "测试教材"}, id="book")
    database.put("page", {"book_id": "book", "index": 0, "status": "draft"}, id="page")
    database.put("page", {"book_id": "book", "index": 1, "status": "pending"})
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_enqueue_process, args=(str(database.settings.data_dir), barrier, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    identifiers = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert len(set(identifiers)) == 1
    jobs = JobStore(database)
    claimed = jobs.claim("worker")
    assert claimed
    assert jobs.enqueue("book_process", "book")["id"] == claimed["id"]
    with pytest.raises(ConflictError):
        jobs.enqueue("book_index", "book")
    jobs.checkpoint(
        claimed["id"],
        claimed["owner"],
        claimed["lease_token"],
        {
            "phase": "识别教材草稿",
            "stages": {
                "recognize:page:1:0": {
                    "transcript": ["data:image/png;base64,private-fixture"],
                    "activity": {"state": "requesting", "model": "fixture", "revision": 2},
                    "usage": {"requests": 1, "input_tokens": 8, "output_tokens": 3},
                }
            },
        },
    )
    with database.write() as conn:
        conn.execute(jobs_table.update().where(jobs_table.c.id == claimed["id"]).values(lease_until=0))
    # Older active tasks must remain visible beyond the recent-history limit.
    for index in range(101):
        item = jobs.enqueue("probe", str(index))
        jobs.cancel(item["id"])
    projected = next(item for item in present_jobs(database) if item["id"] == claimed["id"])
    assert projected["blocking"] and projected["recovering"]
    assert projected["resource_title"] == "测试教材"
    assert projected["progress"]["recognized_pages"] == 1
    assert projected["progress"]["total_pages"] == 2
    assert projected["progress"]["active_requests"][0]["page"] == 1
    assert "transcript" not in str(projected) and "private-fixture" not in str(projected)
    jobs.cancel(claimed["id"])
    replacement = jobs.enqueue("book_process", "book")
    with pytest.raises(ConflictError):
        jobs.reschedule(claimed["id"], 0)
    assert replacement["id"] != claimed["id"]


@pytest.mark.parametrize(
    "parent_shared,child_shared,expected",
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_native_cross_process_locks(
    tmp_path: Path, parent_shared: bool, child_shared: bool, expected: bool
) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    path = tmp_path / "persistent.lock"
    with file_lock(path, shared=parent_shared):
        child = context.Process(target=_try_lock, args=(str(path), child_shared, queue))
        child.start()
        assert queue.get(timeout=15) is expected
        child.join(timeout=15)
        assert child.exitcode == 0
    with file_lock(path, shared=False, timeout=0.1):
        assert path.exists()


def test_auth_confirmation_and_snapshot_boundary(database: Database) -> None:
    set_password(database, "test-password-only")
    with TestClient(create_app(database.settings), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/questions").status_code == 401
        session = client.post("/api/login", json={"password": "test-password-only"}).json()
        assert client.post("/api/subjects", json={"name": "物理"}).status_code == 403
        client.headers.update({"X-CSRF-Token": session["csrf_token"], "Origin": "http://127.0.0.1:8765"})
        subject = client.post("/api/subjects", json={"name": "物理"}).json()
        question = client.post(
            "/api/questions",
            json={
                "subject_id": subject["id"],
                "type": "short_answer",
                "stem": "什么是惯性？",
                "answer": "物体保持原有运动状态的性质。",
            },
        ).json()
        assert question["error_reason"] == "" and question["optimize_error_reason"] is False
        assert client.post(f"/api/questions/{question['id']}/explain", json={}).status_code == 422
        confirmed = client.post(
            f"/api/questions/{question['id']}/confirm", json={"revision": question["revision"]}
        ).json()
        assert confirmed["answer_confirmed"]
        exported = client.post("/api/exports", json={"question_ids": [question["id"]], "mode": "practice"})
        assert exported.status_code == 200, exported.text
        duplicate_export = client.post(
            "/api/exports", json={"question_ids": [question["id"]], "mode": "practice"}
        )
        assert duplicate_export.json()["id"] == exported.json()["id"]
        assert duplicate_export.json()["reused"] is True
        explanation = client.post(f"/api/questions/{question['id']}/explain", json={}).json()
        assert (
            client.post(f"/api/questions/{question['id']}/explain", json={}).json()["id"] == explanation["id"]
        )
        assert client.post(f"/api/questions/{question['id']}/extract", json={}).status_code == 409
        book = client.post(
            "/api/books", json={"title": "物理", "subject_id": subject["id"], "text": "惯性"}
        ).json()
        processing = client.post(f"/api/books/{book['id']}/process", json={}).json()
        repeated = client.post(f"/api/books/{book['id']}/process", json={}).json()
        assert repeated["id"] == processing["id"] and repeated["reused"]
        assert repeated["blocking"] and "checkpoint" not in repeated
        snapshot = database.get("export", exported.json()["resource_id"])
        assert snapshot and snapshot["questions"][0]["answer"] == confirmed["answer"]
        assert (
            not snapshot["include_answer"]
            and not snapshot["include_explanation"]
            and not snapshot["include_knowledge"]
        )
        assert client.get(f"/api/export-snapshot/{snapshot['id']}?token=wrong").status_code == 403
        with pytest.raises(ValueError, match="配图资料缺失"):
            public_snapshot(
                database,
                {**snapshot, "questions": [{**confirmed, "figure_asset_ids": ["missing-figure"]}]},
            )
        confirmed = database.put(
            "question",
            {**confirmed, "explanation": {"summary": "原讲解"}, "explanation_stale": False},
            id=confirmed["id"],
            expected_revision=confirmed["revision"],
        )
        saved_reason = client.put(
            f"/api/questions/{question['id']}",
            json={**confirmed, "error_reason": "  我当时漏看了条件。  ", "optimize_error_reason": True},
        )
        assert saved_reason.status_code == 200, saved_reason.text
        confirmed = saved_reason.json()
        assert confirmed["error_reason"] == "  我当时漏看了条件。  "
        assert confirmed["optimize_error_reason"] is True
        assert confirmed["answer_confirmed"] and confirmed["explanation_stale"]
        changed = client.put(f"/api/questions/{question['id']}", json={**confirmed, "answer": "修改后的答案"})
        assert changed.status_code == 200, changed.text
        assert not changed.json()["answer_confirmed"]
        assert database.get("export", snapshot["id"])["questions"][0]["answer"] == confirmed["answer"]
        with pytest.raises(ValueError, match="不能为空白"):
            validate_question({**confirmed, "type": "fill_blank", "answer": "   \n"})
        assert client.delete(f"/api/questions/{question['id']}").status_code == 200
        assert client.post("/api/exports", json={"question_ids": [question["id"]]}).status_code == 422
        book = client.post("/api/books", json={"title": "教材", "subject_id": subject["id"]}).json()
        with database.write() as conn:
            first = database.put("page", {"book_id": book["id"], "index": 0, "page_index": 5}, conn=conn)
            second = database.put("page", {"book_id": book["id"], "index": 1, "page_index": 0}, conn=conn)
            suggestion = database.put("suggestion", {"book_id": book["id"]}, conn=conn)
        page_order = client.get(f"/api/books/{book['id']}/pages").json()
        assert [page["id"] for page in page_order] == [first["id"], second["id"]]
        tasks = JobStore(database)
        child = tasks.enqueue("page_recognize", first["id"], {"book_id": book["id"]})
        related = tasks.enqueue("suggestion_regenerate", suggestion["id"], {"book_id": book["id"]})
        assert client.delete(f"/api/books/{book['id']}").status_code == 200
        assert tasks.get(child["id"])["status"] == tasks.get(related["id"])["status"] == "cancelled"
        assert (
            client.post(
                "/api/subjects", json={"name": "数学"}, headers={"Origin": "https://untrusted.invalid"}
            ).status_code
            == 403
        )


def test_model_edits_recheck_windows_and_search_requests_are_reused(database: Database) -> None:
    set_password(database, "test-password-only")
    with TestClient(create_app(database.settings), base_url="http://127.0.0.1:8765") as client:
        session = client.post("/api/login", json={"password": "test-password-only"}).json()
        client.headers.update({"X-CSRF-Token": session["csrf_token"]})
        model = client.post(
            "/api/models",
            json={
                "name": "嵌入",
                "role": "embedding",
                "api_key": "fixture",
                "base_url": "https://fixture.invalid/v1",
                "model": "fixture",
            },
        ).json()
        first = client.post("/api/search", json={"query": "惯性", "mode": "semantic"}).json()["job"]
        second = client.post("/api/search", json={"query": "惯性", "mode": "semantic"}).json()["job"]
        assert second["id"] == first["id"] and second["reused"]
        assert second["input"]["query"] == "惯性"
        jobs = JobStore(database)
        future = time.time() + 86400
        waiting = jobs.enqueue("model_test", model["id"])
        scheduled = jobs.enqueue("model_test", "scheduled", not_before=future)
        with database.write() as conn:
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == waiting["id"])
                .values(status="waiting_window", not_before=future)
            )
        changed = client.put(f"/api/models/{model['id']}", json={**model, "max_concurrency": 16})
        assert changed.status_code == 200
        assert jobs.get(waiting["id"])["not_before"] < future
        assert jobs.get(scheduled["id"])["not_before"] == future


def test_exif_and_heif_crop_preserve_original(database: Database) -> None:
    settings = database.settings
    image = Image.new("RGB", (80, 40), (10, 100, 180))
    exif = Image.Exif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    raw = buffer.getvalue()
    asset = store_upload(settings, "phone.jpg", raw)
    assert (asset["width"], asset["height"]) == (40, 80)
    assert safe_path(settings, asset["path"]).read_bytes() == raw
    cropped = crop_asset(settings, asset, [0, 10, 20, 40])
    assert (cropped["width"], cropped["height"]) == (20, 30)
    buffer = io.BytesIO()
    image.save(buffer, "HEIF")
    heif = store_upload(settings, "phone.heic", buffer.getvalue())
    assert heif["mime"] == "image/heif"
    assert safe_path(settings, heif["preview_path"]).is_file()
