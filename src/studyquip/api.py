"""同源 Web API；用户写入受会话、CSRF 与版本校验保护。"""

import asyncio
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError

from .auth import COOKIE_NAME, authenticate, create_session, get_session, token_id
from .config import Settings
from .db import ConflictError, Database, runtime_lock
from .export import check_export_token, create_snapshot, explanation_stale, export_file, public_snapshot
from .jobs import JobStore
from .media import crop_asset, safe_path, store_upload
from .schemas import BookInput, ExportInput, QuestionInput, ScheduleInput, SearchInput, validate_question


class LoginInput(BaseModel):
    password: str


class SubjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RevisionInput(BaseModel):
    revision: int


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    db = Database(config)
    jobs = JobStore(db)
    login_attempts: dict[str, list[float]] = defaultdict(list)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        with runtime_lock(config):
            application.state.db = db
            yield
        db.close()

    application = FastAPI(
        title="StudyQuip", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @application.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(ValueError)
    async def value_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @application.exception_handler(OperationalError)
    async def database_error(request: Request, exc: OperationalError) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": "数据库暂时繁忙，请稍后重试"}, headers={"Retry-After": "1"}
        )

    @application.middleware("http")
    async def session_boundary(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith("/api/"):
            public = path in {"/api/session", "/api/login", "/api/health"}
            session = await asyncio.to_thread(get_session, db, request.cookies.get(COOKIE_NAME))
            token_access = False
            if request.method == "GET" and path.startswith("/api/export-snapshot/"):
                token_access = await asyncio.to_thread(
                    check_export_token, db, path.rsplit("/", 1)[-1], request.query_params.get("token")
                )
            if (
                request.method == "GET"
                and path.startswith("/api/assets/")
                and request.query_params.get("export_id")
            ):
                pieces = path.strip("/").split("/")
                token_access = await asyncio.to_thread(
                    check_export_token,
                    db,
                    request.query_params["export_id"],
                    request.query_params.get("token"),
                    pieces[2],
                )
            if not public and not session and not token_access:
                return JSONResponse(status_code=401, content={"detail": "请先登录"})
            request.state.session = session
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if not origin and request.headers.get("referer"):
                    parsed = urlsplit(request.headers["referer"])
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                if origin and origin.rstrip("/") not in config.origins:
                    return JSONResponse(status_code=403, content={"detail": "请求来源不在允许列表中"})
                if path != "/api/login":
                    supplied = request.headers.get("x-csrf-token", "")
                    if not session or not secrets.compare_digest(supplied, session["csrf_token"]):
                        return JSONResponse(
                            status_code=403, content={"detail": "CSRF 校验失败，请刷新后重试"}
                        )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require(kind: str, id: str) -> dict[str, Any]:
        record = db.get(kind, id)
        if not record or record.get("deleted"):
            raise HTTPException(404, "内容不存在")
        return record

    def present_job(job: dict[str, Any]) -> dict[str, Any]:
        from .progress import JobProgress

        with db.read() as conn:
            return JobProgress(db, conn).present(job)

    def enqueue(
        kind: str, id: str, body: ScheduleInput, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return present_job(
            jobs.enqueue(kind, id, payload, not_before=body.timestamp(), bypass_window=body.bypass_window)
        )

    def public_asset(asset: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in asset.items() if key not in {"path", "preview_path"}}

    def present_question(question: dict[str, Any]) -> dict[str, Any]:
        return {
            "error_reason": "",
            "optimize_error_reason": False,
            **question,
            "explanation_stale": explanation_stale(db, question),
            "figures": [
                {
                    "id": id,
                    "url": f"/api/assets/{id}/preview",
                    "name": (db.get("asset", id) or {}).get("name", "配图"),
                }
                for id in question.get("figure_asset_ids", [])
            ],
        }

    def validate_links(data: dict[str, Any], *, images_only: bool = False) -> None:
        if data.get("subject_id"):
            require("subject", data["subject_id"])
        for key in ("asset_ids", "reference_asset_ids", "figure_asset_ids"):
            for asset_id in data.get(key, []):
                asset = require("asset", asset_id)
                if images_only and not asset["mime"].startswith("image/"):
                    raise ValueError("题目、参考解析和配图附件仅支持图片")
        for book_id in data.get("book_ids", []):
            book = require("book", book_id)
            if book.get("subject_id") != data.get("subject_id"):
                raise ValueError("选中的教材与题目科目不一致")

    def wake_book(book_id: str) -> None:
        jobs.wake_book(book_id)

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @application.get("/api/session")
    def session_info(request: Request) -> dict[str, Any]:
        session = request.state.session
        return {
            "authenticated": bool(session),
            "csrf_token": session["csrf_token"] if session else None,
            "initialized": db.get("auth", "owner") is not None,
        }

    @application.post("/api/login")
    def login(body: LoginInput, request: Request, response: Response) -> dict[str, Any]:
        ip = request.client.host if request.client else "local"
        now = time.time()
        login_attempts[ip] = [attempt for attempt in login_attempts[ip] if attempt > now - 60]
        if len(login_attempts[ip]) >= 5:
            raise HTTPException(429, "尝试次数过多，请一分钟后重试")
        login_attempts[ip].append(now)
        if not authenticate(db, body.password):
            raise HTTPException(401, "密码错误")
        login_attempts[ip] = []
        token, session = create_session(db)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=False,
            samesite="lax",
            max_age=config.session_hours * 3600,
        )
        return {"authenticated": True, "csrf_token": session["csrf_token"], "initialized": True}

    @application.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            db.delete("session", token_id(token))
        response.delete_cookie(COOKIE_NAME)
        return {"authenticated": False}

    @application.get("/api/subjects")
    def subjects() -> list[dict[str, Any]]:
        return db.list("subject")

    @application.post("/api/subjects")
    def add_subject(body: SubjectInput) -> dict[str, Any]:
        name = body.name.strip()
        if not name:
            raise ValueError("科目名称不能为空")
        existing = db.list("subject", filters={"name": name})
        return existing[0] if existing else db.put("subject", {"name": name})

    @application.get("/api/questions")
    def questions(subject_id: str | None = None) -> list[dict[str, Any]]:
        filters = {"subject_id": subject_id} if subject_id else None
        return [present_question(q) for q in db.list("question", filters=filters) if not q.get("deleted")]

    @application.post("/api/questions")
    def add_question(body: QuestionInput) -> dict[str, Any]:
        data = body.model_dump(exclude={"revision"})
        validate_links(data, images_only=True)
        return present_question(
            db.put(
                "question",
                {
                    **data,
                    "answer_confirmed": False,
                    "status": "draft",
                    "explanation": None,
                    "explanation_stale": False,
                },
            )
        )

    @application.get("/api/questions/{id}")
    def get_question(id: str) -> dict[str, Any]:
        return present_question(require("question", id))

    @application.put("/api/questions/{id}")
    def update_question(id: str, body: QuestionInput) -> dict[str, Any]:
        old = {"error_reason": "", "optimize_error_reason": False, **require("question", id)}
        if body.revision is None:
            raise ValueError("保存时需要内容版本")
        data = body.model_dump(exclude={"revision"})
        validate_links(data, images_only=True)
        changed = any(data[key] != old.get(key) for key in data)
        answer_changed = any(
            data[key] != old.get(key)
            for key in (
                "type",
                "stem",
                "options",
                "answer",
                "asset_ids",
                "reference_text",
                "reference_asset_ids",
            )
        )
        merged = {
            **old,
            **data,
            "explanation_stale": old.get("explanation_stale", False)
            or (changed and bool(old.get("explanation"))),
        }
        if answer_changed:
            merged.update(answer_confirmed=False, status="draft")
        return present_question(db.put("question", merged, id=id, expected_revision=body.revision))

    @application.delete("/api/questions/{id}")
    def delete_question(id: str) -> dict[str, bool]:
        old = require("question", id)
        db.put("question", {**old, "deleted": True}, id=id, expected_revision=old["revision"])
        for job in jobs.list(limit=10000):
            if job["resource_id"] == id and job["status"] not in {"completed", "cancelled"}:
                jobs.cancel(job["id"])
        return {"deleted": True}

    @application.post("/api/questions/{id}/confirm")
    def confirm_question(id: str, body: RevisionInput) -> dict[str, Any]:
        old = require("question", id)
        validate_question(old, require_confirmed=False)
        return present_question(
            db.put(
                "question",
                {**old, "answer_confirmed": True, "status": "ready"},
                id=id,
                expected_revision=body.revision,
            )
        )

    @application.post("/api/questions/{id}/extract")
    def extract_question(id: str, body: ScheduleInput = Body(default=ScheduleInput())) -> dict[str, Any]:
        require("question", id)
        return enqueue("question_extract", id, body)

    @application.post("/api/questions/{id}/explain")
    def explain_question(id: str, body: ScheduleInput = Body(default=ScheduleInput())) -> dict[str, Any]:
        question = require("question", id)
        validate_question(question)
        return enqueue("question_explain", id, body)

    @application.get("/api/books")
    def books() -> list[dict[str, Any]]:
        return [book for book in db.list("book") if not book.get("deleted")]

    @application.post("/api/books")
    def add_book(body: BookInput) -> dict[str, Any]:
        from .textbook import TextbookService

        data = body.model_dump(exclude={"revision"})
        validate_links(data)
        with db.write() as conn:
            book = db.put("book", {**data, "status": "draft"}, conn=conn)
            TextbookService(db).ensure_root(book["id"], conn=conn)
            return db.get("book", book["id"], conn) or book

    @application.get("/api/books/{id}")
    def get_book(id: str) -> dict[str, Any]:
        return require("book", id)

    @application.put("/api/books/{id}")
    def update_book(id: str, body: BookInput) -> dict[str, Any]:
        from .retrieval import RetrievalService
        from .textbook import TextbookService

        old = require("book", id)
        if body.revision is None:
            raise ValueError("保存时需要内容版本")
        data = body.model_dump(exclude={"revision"})
        validate_links(data)
        with db.write() as conn:
            saved = db.put("book", {**old, **data}, id=id, expected_revision=body.revision, conn=conn)
            if old["title"] != saved["title"]:
                root = TextbookService(db).ensure_root(id, conn=conn)
                db.put("node", {**root, "title": saved["title"]}, id=root["id"], conn=conn)
            if old["title"] != saved["title"] or old["subject_id"] != saved["subject_id"]:
                RetrievalService(db).rebuild(id, conn=conn)
            previous_budget = old.get("extra_processing_budget")
            next_budget = saved.get("extra_processing_budget")
            if previous_budget is not None and (next_budget is None or next_budget > previous_budget):
                jobs.resume_waiting("book_index", id, conn=conn)
        return saved

    @application.delete("/api/books/{id}")
    def delete_book(id: str) -> dict[str, bool]:
        old = require("book", id)
        with db.write() as conn:
            db.put("book", {**old, "deleted": True}, id=id, expected_revision=old["revision"], conn=conn)
            pages = {
                record["id"] for record in db.list("page", filters={"book_id": id}, limit=100000, conn=conn)
            }
            suggestions = {
                record["id"]
                for record in db.list("suggestion", filters={"book_id": id}, limit=100000, conn=conn)
            }
            for job in jobs.list(limit=100000, conn=conn):
                related = (
                    (job["kind"] in {"book_process", "book_index"} and job["resource_id"] == id)
                    or (job["kind"] == "page_recognize" and job["resource_id"] in pages)
                    or (job["kind"] == "suggestion_regenerate" and job["resource_id"] in suggestions)
                )
                if related and job["status"] not in {"completed", "cancelled"}:
                    jobs.cancel(job["id"], conn=conn)
        return {"deleted": True}

    @application.post("/api/books/{id}/process")
    def process_book(id: str, body: ScheduleInput = Body(default=ScheduleInput())) -> dict[str, Any]:
        require("book", id)
        return enqueue("book_process", id, body)

    @application.get("/api/books/{id}/pages")
    def book_pages(id: str) -> list[dict[str, Any]]:
        require("book", id)
        return sorted(
            db.list("page", filters={"book_id": id}, limit=100000),
            key=lambda page: (
                page.get("index", page.get("order", page.get("page_index", 0))),
                page["created_at"],
            ),
        )

    @application.get("/api/books/{id}/nodes")
    def book_nodes(id: str) -> list[dict[str, Any]]:
        require("book", id)
        return db.list("node", filters={"book_id": id}, limit=100000)

    @application.get("/api/books/{id}/blocks")
    def book_blocks(id: str) -> list[dict[str, Any]]:
        from .textbook import TextbookService

        require("book", id)
        return TextbookService(db).list_blocks(id)

    @application.get("/api/books/{id}/suggestions")
    def book_suggestions(id: str) -> list[dict[str, Any]]:
        require("book", id)
        return db.list("suggestion", filters={"book_id": id}, limit=10000)

    @application.put("/api/books/{id}/pages/{page_id}")
    def edit_page(id: str, page_id: str, body: dict[str, Any] = Body()) -> dict[str, Any]:
        from .textbook import TextbookService

        require("book", id)
        with db.write() as conn:
            page = db.get("page", page_id, conn)
            if not page or page.get("book_id") != id:
                raise HTTPException(404, "页面不存在")
            if page["revision"] != body.get("revision"):
                raise ConflictError("页面已被更新，请刷新")
            result = TextbookService(db).upsert_page_draft(
                id, page_id, str(body.get("text", "")), status="draft", conn=conn
            )
            current = db.get("page", page_id, conn) or result
            result = db.put("page", {**current, "human_edited": True}, id=page_id, conn=conn)
        wake_book(id)
        return result

    @application.post("/api/books/{id}/pages/{page_id}/recognize")
    def recognize_page(
        id: str, page_id: str, body: ScheduleInput = Body(default=ScheduleInput())
    ) -> dict[str, Any]:
        require("book", id)
        page = require("page", page_id)
        if page.get("book_id") != id:
            raise HTTPException(404, "页面不存在")
        return enqueue("page_recognize", page_id, body, {"book_id": id})

    @application.post("/api/books/{id}/pages/{page_id}/skip")
    def skip_page(id: str, page_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from .textbook import TextbookService

        require("book", id)
        result = TextbookService(db).skip_page(id, page_id, reason=body.get("reason", "用户跳过"))
        wake_book(id)
        return result

    @application.post("/api/books/{id}/pages/{page_id}/text-only")
    def page_text_only(id: str, page_id: str) -> dict[str, Any]:
        from .textbook import TextbookService

        require("book", id)
        page = require("page", page_id)
        if page.get("book_id") != id:
            raise HTTPException(404, "页面不存在")
        text = page.get("original_text", page.get("native_text", ""))
        if not text.strip():
            raise ValueError("该页没有可提取的文字，请手动校对或跳过")
        with db.write() as conn:
            TextbookService(db).upsert_page_draft(id, page_id, text, status="draft", conn=conn)
            current = db.get("page", page_id, conn) or page
            result = db.put(
                "page", {**current, "visual_incomplete": True, "human_edited": True}, id=page_id, conn=conn
            )
        wake_book(id)
        return result

    @application.post("/api/books/{id}/operations")
    def book_operations(id: str, body: dict[str, Any] = Body()) -> dict[str, Any]:
        from .textbook import TextbookService

        require("book", id)
        group_id = body.get("operation_group_id") or body.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("操作组必须提供一次性 operation_group_id")
        return TextbookService(db).apply_operations(
            id, group_id, body.get("operations", []), reason=body.get("reason", "人工校对"), actor="human"
        )

    @application.post("/api/books/{id}/suggestions/{suggestion_id}/accept")
    def accept_suggestion(id: str, suggestion_id: str) -> dict[str, Any]:
        from .textbook import TextbookService

        require("book", id)
        return TextbookService(db).accept_suggestion(id, suggestion_id)

    @application.post("/api/books/{id}/suggestions/{suggestion_id}/ignore")
    def ignore_suggestion(id: str, suggestion_id: str) -> dict[str, Any]:
        from .textbook import TextbookService

        require("book", id)
        return TextbookService(db).ignore_suggestion(id, suggestion_id)

    @application.post("/api/books/{id}/suggestions/{suggestion_id}/regenerate")
    def regenerate_suggestion(
        id: str, suggestion_id: str, body: ScheduleInput = Body(default=ScheduleInput())
    ) -> dict[str, Any]:
        require("book", id)
        suggestion = require("suggestion", suggestion_id)
        if suggestion.get("book_id") != id:
            raise HTTPException(404, "建议不存在")
        return enqueue("suggestion_regenerate", suggestion_id, body, {"book_id": id})

    @application.get("/api/books/{id}/blocks/{block_id}/history")
    def block_history(id: str, block_id: str) -> list[dict[str, Any]]:
        require("book", id)
        history = db.history("block", block_id)
        return [record for record in history if record.get("book_id") == id]

    def present_model(model: dict[str, Any]) -> dict[str, Any]:
        from .ai import ModelProfile
        from .scheduling import CapacityLimiter

        profile = ModelProfile.model_validate(model)
        limiter = CapacityLimiter(db.secret())
        limiter.configure(ModelProfile.model_validate(other) for other in db.list("model"))
        model_cap, credential_cap = limiter.effective(profile)
        clean = {key: value for key, value in model.items() if key != "api_key"}
        clean.update(
            has_api_key=bool(model.get("api_key")),
            effective_max_concurrency=model_cap,
            effective_credential_max_concurrency=credential_cap,
        )
        return clean

    @application.get("/api/models")
    def models() -> list[dict[str, Any]]:
        return [present_model(model) for model in db.list("model")]

    @application.post("/api/models")
    def add_model(body: dict[str, Any] = Body()) -> dict[str, Any]:
        from .ai import ModelProfile

        data = ModelProfile.model_validate(body).model_dump(mode="json")
        with db.write() as conn:
            saved = db.put("model", data, conn=conn)
            jobs.configuration_changed(conn)
        return present_model(saved)

    @application.put("/api/models/{id}")
    def update_model(id: str, body: dict[str, Any] = Body()) -> dict[str, Any]:
        from .ai import ModelProfile

        old = require("model", id)
        merged = {**old, **body}
        if not body.get("api_key"):
            merged["api_key"] = old.get("api_key", "")
        data = ModelProfile.model_validate(merged).model_dump(mode="json")
        if body.get("revision") is None:
            raise ValueError("保存时需要配置版本")
        with db.write() as conn:
            saved = db.put("model", data, id=id, expected_revision=body["revision"], conn=conn)
            jobs.configuration_changed(conn)
        return present_model(saved)

    @application.delete("/api/models/{id}")
    def delete_model(id: str) -> dict[str, bool]:
        require("model", id)
        with db.write() as conn:
            db.delete("model", id, conn=conn)
            jobs.configuration_changed(conn)
        return {"deleted": True}

    @application.post("/api/models/{id}/test")
    def test_model(id: str, body: ScheduleInput = Body(default=ScheduleInput())) -> dict[str, Any]:
        require("model", id)
        return enqueue("model_test", id, body)

    @application.post("/api/assets")
    async def upload(file: UploadFile = File()) -> dict[str, Any]:
        content = await file.read(config.max_upload_mb * 1024 * 1024 + 1)
        asset = await asyncio.to_thread(store_upload, config, file.filename or "image.jpg", content)
        saved = await asyncio.to_thread(db.put, "asset", asset, id=asset["id"])
        return public_asset(saved)

    @application.get("/api/assets/{id}")
    def asset_metadata(id: str) -> dict[str, Any]:
        return public_asset(require("asset", id))

    @application.get("/api/assets/{id}/file")
    def asset_file(id: str) -> FileResponse:
        asset = require("asset", id)
        return FileResponse(
            safe_path(config, asset["path"]), media_type=asset["mime"], filename=asset["name"]
        )

    @application.get("/api/assets/{id}/preview")
    def asset_preview(id: str) -> FileResponse:
        asset = require("asset", id)
        return FileResponse(
            safe_path(config, asset.get("preview_path") or asset["path"]),
            media_type="image/jpeg" if asset.get("preview_path") else asset["mime"],
        )

    @application.post("/api/assets/{id}/crop")
    def crop(id: str, body: dict[str, Any] = Body()) -> dict[str, Any]:
        asset = require("asset", id)
        cropped = crop_asset(config, asset, body.get("box", []))
        return public_asset(db.put("asset", cropped, id=cropped["id"]))

    @application.post("/api/search")
    def search(body: SearchInput) -> Any:
        from .retrieval import RetrievalService

        data = body.model_dump()
        embedding_models = db.list("model", filters={"role": "embedding"})
        if body.mode in {"semantic", "hybrid"} and embedding_models:
            job = jobs.request("search", data, lambda conn: db.put("search", data, conn=conn))
            return JSONResponse(status_code=202, content={"job": present_job(job)})
        if body.mode == "semantic":
            raise ValueError("请先配置嵌入模型")
        return RetrievalService(db).search(**data)

    @application.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        from .progress import present_jobs

        return present_jobs(db)

    @application.post("/api/jobs/{id}/cancel")
    def cancel_job(id: str) -> dict[str, Any]:
        return present_job(jobs.cancel(id))

    @application.post("/api/jobs/{id}/retry")
    @application.post("/api/jobs/{id}/resume")
    def resume_job(id: str, body: ScheduleInput | None = None) -> dict[str, Any]:
        job = jobs.get(id)
        if not job:
            raise HTTPException(404, "任务不存在")
        timing = body or ScheduleInput(bypass_window=bool(job["bypass_window"]))
        return present_job(jobs.resume(id, timing.timestamp(), timing.bypass_window))

    @application.post("/api/jobs/{id}/reschedule")
    def reschedule_job(id: str, body: ScheduleInput) -> dict[str, Any]:
        return present_job(jobs.reschedule(id, body.timestamp(), body.bypass_window))

    @application.post("/api/exports")
    def add_export(body: ExportInput) -> dict[str, Any]:
        with db.write() as conn:
            selected = [db.get("question", id, conn=conn) for id in body.question_ids]
            if any(not question or question.get("deleted") for question in selected):
                raise ValueError("所选题目不存在")
            specification = {
                **body.model_dump(),
                "revisions": [question["revision"] for question in selected if question],
            }
            job = jobs.request(
                "export_pdf", specification, lambda active: create_snapshot(db, body, active), conn=conn
            )
        return present_job(job)

    @application.get("/api/exports/{id}")
    def get_export(id: str) -> dict[str, Any]:
        return public_snapshot(db, require("export", id))

    @application.get("/api/exports/{id}/file")
    def download_export(id: str) -> FileResponse:
        snapshot = require("export", id)
        return FileResponse(
            export_file(config, snapshot), media_type="application/pdf", filename=f"StudyQuip-{id[:8]}.pdf"
        )

    @application.get("/api/export-snapshot/{id}")
    def snapshot_content(id: str, token: str) -> dict[str, Any]:
        if not check_export_token(db, id, token):
            raise HTTPException(403, "导出凭证无效或已过期")
        return public_snapshot(db, require("export", id), token=token)

    @application.get("/{path:path}")
    def frontend(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        target = (config.frontend_dir / path).resolve()
        if target.is_relative_to(config.frontend_dir) and target.is_file():
            return FileResponse(target)
        index = config.frontend_dir / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(
            status_code=503, content={"detail": "前端尚未构建，请在 frontend 执行 pnpm build"}
        )

    return application
