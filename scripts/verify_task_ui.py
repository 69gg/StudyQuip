"""真实 Chromium + 本地构建 + HTTP 路由夹具；不启动 Web/worker，不访问业务数据库或模型。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Route, async_playwright, expect

from studyquip.ai import MODEL_ROLE_LABELS, ModelProfile
from studyquip.question_index import PART_LABELS


async def verify() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    dist = root / "frontend/dist"
    assert (dist / "index.html").exists(), "请先构建前端"
    output = root / ".verification" / f"task-ui-{time.time_ns()}"
    output.mkdir(parents=True)
    now = time.time()
    subject = {"id": "subject", "revision": 1, "name": "物理"}
    book = {
        "id": "book",
        "revision": 1,
        "title": "进度验收教材",
        "subject_id": "subject",
        "asset_ids": [],
        "status": "draft",
    }
    question = {
        "id": "question",
        "revision": 1,
        "stem": r"已知 $\frac{x^2}{2}=8$，求 x 的取值。另有 1/2x 待核对。",
        "type": "short_answer",
        "subject_id": "subject",
        "answer": r"$x=\pm4$",
        "answer_confirmed": True,
        "options": [],
        "asset_ids": [],
        "reference_asset_ids": [],
        "figure_asset_ids": [],
        "book_ids": [],
        "notes": "",
        "reference_text": "",
        "formatting_warnings": ["题干：1/2x 的分母范围不明确，已保留原文。"],
    }
    model = ModelProfile(
        id="model",
        revision=1,
        name="验收模型",
        role="embedding",
        base_url="https://fixture.invalid/v1",
        api_key="fixture",
        model="fixture",
    ).model_dump(exclude={"api_key"})
    models = [
        model,
        *(
            {**model, "id": role, "name": label, "role": role, "model": f"fixture-{role}"}
            for role, label in MODEL_ROLE_LABELS.items()
            if role != "embedding"
        ),
    ]

    def job(kind: str, resource_id: str) -> dict[str, Any]:
        return {
            "id": kind,
            "kind": kind,
            "resource_id": resource_id,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "attempts": 1,
            "blocking": True,
            "progress": {},
        }

    book_job = job("book_process", "book")
    book_job.update(
        {
            "book_id": "book",
            "resource_title": book["title"],
            "progress": {
                "phase": "识别教材草稿",
                "total_pages": 160,
                "recognized_pages": 15,
                "processed_pages": 0,
                "review_pages": 1,
                "skipped_pages": 2,
                "blocks": 0,
                "nodes": 1,
                "summarized_nodes": 0,
                "usage": {"requests": 15, "input_tokens": 20000, "output_tokens": 26000},
                "active_requests": [
                    {
                        "state": "retrying",
                        "model": "vision-fixture",
                        "revision": 2,
                        "page": 16,
                        "at": now,
                        "next_at": now + 2,
                        "format_attempt": 1,
                        "format_limit": 2,
                        "reason": "模型没有调用结构化结果工具；已要求重新提交。",
                    }
                ],
                "last_activity_at": now,
            },
        }
    )
    search_job = job("search", "search-input")
    search_job["input"] = {
        "query": "惯性",
        "methods": ["keyword"],
        "book_ids": ["book"],
        "subject_id": "subject",
    }
    export_job = {**job("export_pdf", "export"), "question_ids": ["question"]}
    jobs = [book_job, job("question_explain", "question"), job("model_test", "model"), search_job, export_job]
    responses: dict[str, Any] = {
        "/api/search/options": {
            "parts": PART_LABELS,
            "default_limit": 12,
            "max_limit": 100,
            "default_methods": ["keyword"],
        },
        "/api/session": {"authenticated": True, "initialized": True, "csrf_token": "fixture"},
        "/api/subjects": [subject],
        "/api/books": [book],
        "/api/books/book": book,
        "/api/books/book/nodes": [],
        "/api/books/book/blocks": [],
        "/api/books/book/suggestions": [],
        "/api/books/book/pages": [
            {"id": "page", "revision": 1, "status": "pending", "source_type": "text", "text": ""}
        ],
        "/api/questions": [question],
        "/api/questions/question": question,
        "/api/models": models,
        "/api/jobs": jobs,
    }
    api_failure = False

    async def route_request(route: Route) -> None:
        path = urlsplit(route.request.url).path
        assert route.request.method == "GET", "被禁用的处理入口不应提交请求"
        if path in responses:
            if path == "/api/jobs" and api_failure:
                await route.fulfill(status=503, json={"detail": "fixture offline"})
            else:
                await route.fulfill(json=responses[path])
            return
        assert not path.startswith("/api/"), path
        file = (dist / path.lstrip("/")).resolve()
        assert file.is_relative_to(dist)
        if not file.is_file():
            file = dist / "index.html"
        await route.fulfill(
            body=file.read_bytes(),
            content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream",
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/*", route_request)
            await page.goto("http://studyquip.test/#home")
            await expect(page.locator('.home-task-row[href="#tasks?job=book_process"]')).to_contain_text(
                "已识别 15 / 160 页"
            )
            await expect(page.locator(".home-book-row")).to_contain_text("处理中")
            await page.goto("http://studyquip.test/#books?book=book")
            await expect(page.get_by_role("button", name="已有处理任务", exact=True)).to_be_disabled()
            await expect(page.get_by_role("progressbar", name="原页识别进度")).to_have_attribute(
                "value", "15"
            )
            await page.reload()
            await expect(page.get_by_role("button", name="已有处理任务", exact=True)).to_be_disabled()
            await expect(page.get_by_role("progressbar", name="原页识别进度")).to_have_attribute("max", "160")
            await page.screenshot(path=str(output / "book-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.emulate_media(color_scheme="dark")
            await page.screenshot(path=str(output / "book-mobile-dark.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.get_by_role("button", name="原页校对", exact=True).click()
            await page.get_by_text("原页 1", exact=True).click()
            await expect(page.get_by_role("button", name="重新识别", exact=True)).to_be_disabled()
            editor = page.get_by_role("textbox", name="本页提取内容", exact=True)
            await editor.fill("用户正在校对，尚未保存。")
            responses["/api/books/book/pages"][0].update({"revision": 2, "text": "后台刚完成识别。"})
            await page.get_by_role("button", name="刷新结果", exact=True).click()
            await expect(
                page.get_by_text("后台已更新此页，已保留你的未保存内容。保存前请与最新草稿核对。", exact=True)
            ).to_be_visible()
            await expect(editor).to_have_value("用户正在校对，尚未保存。")
            book_job["progress"].update(
                {
                    "phase": "顺序修订教材",
                    "recognized_pages": 160,
                    "processed_pages": 15,
                    "current_page": 16,
                    "blocks": 74,
                    "review_pages": 0,
                    "skipped_pages": 0,
                    "completed_units": 15,
                    "completed_stages": 175,
                    "usage": {"requests": 176, "input_tokens": 267711, "output_tokens": 222437},
                    "enrichment": {"concepts": 4, "relations": 1, "rejected": 6},
                    "tool_errors": 2,
                    "tool_events": [
                        {
                            "name": "submit_result",
                            "round": 1,
                            "page": 15,
                            "at": now - 310,
                            "duration_seconds": 0.03,
                            "status": "completed",
                        },
                        {
                            "name": "read_block",
                            "round": 2,
                            "page": 16,
                            "at": now - 305,
                            "duration_seconds": 0.02,
                            "status": "error",
                            "reason": "块不存在，请使用上下文中已有的正文块 ID。",
                        },
                    ],
                }
            )
            await page.goto("http://studyquip.test/#tasks?job=book_process")
            await expect(page.get_by_text("进度验收教材", exact=True)).to_be_visible()
            await expect(page.locator(".request-detail").filter(has_text="vision-fixture")).to_contain_text(
                "原页 16"
            )
            await expect(page.get_by_text("结果格式修复 1/2", exact=False)).to_be_visible()
            await expect(page.get_by_text("模型没有调用结构化结果工具", exact=False)).to_be_visible()
            await expect(page.get_by_text("已完成正文单元 15", exact=False)).to_be_visible()
            await expect(page.get_by_text("6 条概念或关系未通过校验", exact=False)).to_be_visible()
            activity = book_job["progress"]["active_requests"][0]
            activity.update(
                {
                    "state": "requesting",
                    "streaming": True,
                    "stream_phase": "arguments",
                    "model": "text-fixture",
                    "rounds": 2,
                    "attempt": 3,
                    "retry_limit": 2,
                    "at": now - 40,
                    "first_received_at": now - 20,
                    "last_received_at": now,
                    "received_events": 24,
                    "reasoning_characters": 824,
                    "tool_argument_characters": 512,
                    "tool_names": ["submit_result"],
                    "last_failure": {
                        "status": 504,
                        "reason": "上游网关超时（HTTP 504），本次等待 303 秒",
                        "elapsed_seconds": 303,
                        "timeout_seconds": 12000,
                        "attempt": 2,
                        "at": now - 45,
                    },
                }
            )
            await page.reload()
            await expect(page.get_by_text("正在生成工具参数", exact=False)).to_be_visible()
            await expect(page.get_by_text("网络重试 2/2", exact=False)).to_be_visible()
            await expect(page.get_by_text("上游网关超时（HTTP 504）", exact=False)).to_be_visible()
            await page.get_by_text("最近工具活动", exact=False).click()
            await expect(
                page.get_by_text("块不存在，请使用上下文中已有的正文块 ID。", exact=True)
            ).to_be_visible()
            await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            await page.screenshot(path=str(output / "tasks-mobile-dark.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.emulate_media(color_scheme="light")
            await page.screenshot(path=str(output / "tasks-desktop-light.png"), full_page=True)
            await page.emulate_media(color_scheme="dark")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto("http://studyquip.test/#questions?question=question")
            await expect(page.get_by_role("button", name="题目处理中", exact=True)).to_be_disabled()
            await expect(page.get_by_role("button", name="已有处理任务", exact=True)).to_be_disabled()
            await expect(page.get_by_text("表述需要核对", exact=True)).to_be_visible()
            await expect(page.get_by_text(question["formatting_warnings"][0], exact=True)).to_be_visible()
            await page.get_by_role("button", name="预览", exact=True).click()
            await expect(page.locator(".question-prompt .katex")).to_be_visible()
            assert await page.locator(".katex-error").count() == 0
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(output / "question-formulas-mobile-dark.png"), full_page=True)
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.screenshot(path=str(output / "question-formulas-desktop-dark.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto("http://studyquip.test/#settings")
            for group, roles in {
                "题目": ("question_vision", "question_text"),
                "教材": ("book_vision", "book_text"),
                "检索": ("embedding",),
                "听力": ("speech_recognition", "speech_synthesis"),
            }.items():
                await (
                    page.locator(".model-purpose-tabs").get_by_role("button", name=group, exact=True).click()
                )
                for role in roles:
                    await expect(
                        page.locator(".record-meta").get_by_text(MODEL_ROLE_LABELS[role], exact=True)
                    ).to_be_visible()
                if group == "检索":
                    await expect(page.get_by_role("button", name="测试已安排", exact=True)).to_be_disabled()
            await page.screenshot(path=str(output / "models-mobile-dark.png"), full_page=True)
            await page.locator(".model-purpose-tabs").get_by_role("button", name="教材", exact=True).click()
            await (
                page.locator(".model-row")
                .filter(has=page.locator(".record-meta").get_by_text("教材文本模型", exact=True))
                .get_by_role("button", name="编辑", exact=True)
                .click()
            )
            purpose = page.get_by_label("用途", exact=False)
            await expect(purpose).to_have_value("book_text")
            assert await purpose.locator("option").all_text_contents() == list(MODEL_ROLE_LABELS.values())
            for role in MODEL_ROLE_LABELS:
                await purpose.select_option(role)
                await expect(purpose).to_have_value(role)
            await purpose.select_option("book_text")
            await expect(
                page.get_by_text("顺序整理教材、衔接跨页内容、生成目录概述与修改建议。", exact=True)
            ).to_be_visible()
            await page.get_by_text("高级参数", exact=True).click()
            await expect(page.get_by_role("spinbutton", name="自动重试次数", exact=False)).to_have_value("2")
            await expect(
                page.get_by_text("网络错误和模型格式错误分别使用此次数；0 表示不自动重试。", exact=True)
            ).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(output / "model-purpose-mobile-dark.png"), full_page=True)
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.get_by_role("spinbutton", name="自动重试次数", exact=False).evaluate(
                "element => element.scrollIntoView({block: 'center'})"
            )
            await page.screenshot(path=str(output / "model-purpose-desktop-dark.png"), full_page=True)
            await page.get_by_role("button", name="关闭窗口", exact=True).click()
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto("http://studyquip.test/#questions")
            await page.get_by_role("checkbox", name="选择此题导出", exact=True).check()
            await page.get_by_role("button", name="导出 PDF · 1", exact=True).click()
            await expect(page.get_by_role("button", name="生成 PDF", exact=True)).to_be_disabled()
            await page.goto("http://studyquip.test/#search")
            await expect(page.get_by_role("textbox", name="检索内容", exact=True)).to_have_value("惯性")
            await expect(page.get_by_role("button", name="检索处理中", exact=True)).to_be_disabled()
            search_job.update(
                {
                    "status": "completed",
                    "blocking": False,
                    "result": {"hits": [{"text": "查询完成后的教材证据", "book_title": book["title"]}]},
                }
            )
            await expect(page.get_by_role("button", name="检索", exact=True)).to_be_enabled(timeout=8000)
            await expect(page.get_by_text("查询完成后的教材证据", exact=True)).to_be_visible()
            await page.reload()
            await expect(page.get_by_text("查询完成后的教材证据", exact=True)).to_be_visible()
            book_job.update(
                {
                    "recovering": True,
                    "waiting_reason": "执行进程已离线或租约过期，等待 worker 恢复；请勿重复提交",
                }
            )
            await page.goto("http://studyquip.test/#books?book=book")
            await expect(page.get_by_text(book_job["waiting_reason"], exact=True)).to_be_visible()
            await expect(page.get_by_role("button", name="已有处理任务", exact=True)).to_be_disabled()
            api_failure = True
            await expect(
                page.get_by_text("暂时无法获取任务状态，已暂停重复提交入口。", exact=True)
            ).to_be_visible(timeout=8000)
            await expect(page.get_by_role("button", name="已有处理任务", exact=True)).to_be_disabled()
            assert not errors, errors
        finally:
            await browser.close()
    report = {
        "directory": str(output),
        "app_processes_started": False,
        "external_model_requests": 0,
        "refresh_and_duplicate_guards": ["book", "page", "question", "model_test", "export", "search"],
        "search_restored_after_reload": True,
        "unsaved_page_edits_preserved": True,
        "home_shares_task_progress": True,
        "expired_lease_and_api_failure_guarded": True,
        "separate_model_purposes": list(MODEL_ROLE_LABELS),
        "question_formulas_and_review_notices": True,
        "format_retry_progress_and_settings": True,
        "tool_history_gateway_diagnosis_and_stream_phase": True,
        "body_units_separate_from_recognition": True,
        "mobile_overflow": False,
        "browser_errors": errors,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False, indent=2))
