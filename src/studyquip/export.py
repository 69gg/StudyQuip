"""快照化导出；渲染结果只有通过任务 fencing 后才能发布。"""

import asyncio
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from .config import Settings
from .db import Database
from .media import safe_path
from .schemas import ExportInput, validate_question


def explanation_stale(db: Database, question: dict[str, Any]) -> bool:
    if question.get("explanation_stale"):
        return True
    explanation = question.get("explanation") or {}
    references = explanation.get("references", explanation.get("citations", []))
    for reference in references:
        if not isinstance(reference, dict):
            continue
        block_id = reference.get("block_id")
        if reference.get("book_id"):
            book = db.get("book", reference["book_id"])
            if not book or book.get("deleted"):
                return True
        revision = reference.get("revision", reference.get("block_revision"))
        if block_id and revision is not None:
            block = db.get("block", block_id)
            if not block or block.get("revision") != revision or block.get("archived"):
                return True
    return False


def create_snapshot(db: Database, spec: ExportInput) -> dict[str, Any]:
    if spec.mode == "practice":
        spec = spec.model_copy(
            update={"include_answer": False, "include_explanation": False, "include_knowledge": False}
        )
    if len(set(spec.question_ids)) != len(spec.question_ids):
        raise ValueError("导出题目不能重复")
    questions: list[dict[str, Any]] = []
    for id in spec.question_ids:
        question = db.get("question", id)
        if not question or question.get("deleted"):
            raise ValueError("所选题目不存在")
        validate_question(question)
        if spec.mode == "review" and (spec.include_explanation or spec.include_knowledge):
            if not question.get("explanation"):
                raise ValueError("所选题目尚无讲解，请先生成或取消包含讲解和知识点")
            if explanation_stale(db, question):
                raise ValueError("讲解已过期，请重新生成或取消包含讲解和知识点")
        questions.append(question)
    return db.put(
        "export",
        {
            **spec.model_dump(),
            "questions": questions,
            "status": "queued",
            "token": secrets.token_urlsafe(32),
            "token_expires_at": None,
        },
    )


def check_export_token(db: Database, id: str, token: str | None, asset_id: str | None = None) -> bool:
    snapshot = db.get("export", id)
    if not snapshot or not token or not secrets.compare_digest(snapshot.get("token", ""), token):
        return False
    expiry = snapshot.get("token_expires_at")
    if expiry is None or expiry < time.time():
        return False
    if asset_id:
        return any(asset_id in question.get("figure_asset_ids", []) for question in snapshot["questions"])
    return True


def public_snapshot(db: Database, snapshot: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    clean = {
        key: value for key, value in snapshot.items() if key not in {"token", "token_expires_at", "path"}
    }
    questions: list[dict[str, Any]] = []
    for question in snapshot["questions"]:
        figures: list[dict[str, Any]] = []
        for asset_id in question.get("figure_asset_ids", []):
            asset = db.get("asset", asset_id)
            if not asset:
                raise ValueError("导出配图资料缺失，请恢复原件或重新选择配图")
            query = "?" + urlencode({"export_id": snapshot["id"], "token": token}) if token else ""
            figures.append(
                {"id": asset_id, "name": asset["name"], "url": f"/api/assets/{asset_id}/preview{query}"}
            )
        questions.append({**question, "figures": figures})
    clean["questions"] = questions
    if snapshot.get("status") == "completed":
        clean["url"] = f"/api/exports/{snapshot['id']}/file"
    return clean


async def render_export(
    settings: Settings, db: Database, snapshot_id: str, attempt_token: str
) -> dict[str, Any]:
    snapshot = await asyncio.to_thread(db.get, "export", snapshot_id)
    if not snapshot:
        raise ValueError("导出快照不存在")
    token = snapshot["token"]
    # 只更新短期读取凭证；PDF结果发布由 worker 的租约事务完成。
    await asyncio.to_thread(
        db.put,
        "export",
        {
            **snapshot,
            "token_expires_at": time.time() + settings.pdf_timeout_seconds * 2,
        },
        id=snapshot_id,
        expected_revision=snapshot["revision"],
    )
    directory = settings.files_dir / "exports"
    directory.mkdir(exist_ok=True)
    destination = directory / f"{snapshot_id}-{attempt_token}.pdf"
    host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
    url = f"http://{host}:{settings.port}/print/{snapshot_id}?{urlencode({'token': token})}"
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                page.set_default_timeout(settings.pdf_timeout_seconds * 1000)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_function(
                    "window.__STUDYQUIP_PRINT_READY__ === true || !!window.__STUDYQUIP_PRINT_ERROR__"
                )
                error = await page.evaluate("window.__STUDYQUIP_PRINT_ERROR__ || null")
                if error:
                    raise RuntimeError(f"导出页面准备失败：{error}")
                await page.evaluate(
                    "async () => { await document.fonts.ready; "
                    "await Promise.all([...document.images].map(i => i.decode())); }"
                )
                await page.pdf(
                    path=str(destination), format="A4", print_background=True, prefer_css_page_size=True
                )
            finally:
                await browser.close()
        return {
            "snapshot_id": snapshot_id,
            "path": str(destination.relative_to(settings.files_dir)),
            "url": f"/api/exports/{snapshot_id}/file",
            "status": "completed",
        }
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def export_file(settings: Settings, snapshot: dict[str, Any]) -> Path:
    if snapshot.get("status") != "completed" or not snapshot.get("path"):
        raise ValueError("PDF 尚未生成完成")
    return safe_path(settings, snapshot["path"])
