"""Exercise nested editing and render real PDFs using local HTTP fixtures only."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import Route, async_playwright, expect

from studyquip.ai import MODEL_ROLE_LABELS, ModelProfile
from studyquip.question_index import PART_LABELS


async def verify() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    dist = root / "frontend/dist"
    output = root / ".verification" / f"questions-{time.time_ns()}"
    output.mkdir(parents=True)
    image = io.BytesIO()
    Image.new("RGB", (300, 100), "#d7e2da").save(image, format="PNG")
    leaf: dict[str, Any] = {
        "id": "leaf",
        "revision": 1,
        "type": "single_choice",
        "subject_id": "english",
        "stem": "What time is it?",
        "options": [{"id": "a", "text": "Three o'clock."}, {"id": "b", "text": "Four o'clock."}],
        "answer": "a",
        "answer_confirmed": True,
        "wrong_answer": "b",
        "error_reason": "听错了数字，把 three 听成了 four。",
        "optimize_error_reason": True,
        "notes": "",
        "book_ids": [],
        "asset_ids": [],
        "figure_asset_ids": [],
        "reference_asset_ids": [],
        "reference_text": "",
        "parts": [],
        "materials": [],
        "explanation": {
            "summary": "录音中说的是 three o'clock。",
            "steps": ["识别时间表达。", "对照选项，选择 A。"],
            "knowledge_points": ["整点时间的表达"],
            "error_reason_optimized": "我混淆了 three 和 four 的发音。",
            "citations": [
                {
                    "book_title": "英语必修一",
                    "node_path": ["Listening", "Time"],
                    "quote": "It is three o'clock.",
                }
            ],
        },
    }
    group = {
        **copy.deepcopy(leaf),
        "id": "group",
        "type": "composite",
        "stem": "听录音，回答下列小题。",
        "error_reason": "",
        "wrong_answer": "",
        "optimize_error_reason": False,
        "options": [],
        "explanation": None,
        "parts": [copy.deepcopy(leaf)],
    }
    question = {
        **copy.deepcopy(group),
        "id": "question",
        "stem": "",
        "parts": [group],
        "materials": [
            {
                "id": "listening",
                "kind": "listening",
                "title": "听力材料",
                "text": "It is three o'clock. Let's go to the library.",
                "audio_asset_id": "audio",
            }
        ],
    }
    math = {
        **copy.deepcopy(leaf),
        "id": "math",
        "stem": r"函数 $f(x)=x^2$ 的图像如图，求 $f(2)$。",
        "answer": "b",
        "options": [{"id": "a", "text": "$2$"}, {"id": "b", "text": "$4$"}],
        "error_reason": "把平方看成了乘以二。",
        "rendered_figures": [
            {
                "id": "plot",
                "kind": "plot",
                "title": "y = x²",
                "plot": {
                    "x_min": -2,
                    "x_max": 2,
                    "y_min": 0,
                    "y_max": 5,
                    "x_label": "x",
                    "y_label": "y",
                    "series": [{"expression": "x^2", "label": "", "points": []}],
                },
            }
        ],
    }
    geometry = {
        **copy.deepcopy(leaf),
        "id": "svg",
        "type": "short_answer",
        "stem": r"如图，$AB\perp BC$，说明判断依据。",
        "answer": "直角定义。",
        "options": [],
        "figure_asset_ids": ["figure"],
        "figures": [{"id": "figure", "url": "/api/assets/figure/preview", "name": "实验背景"}],
        "rendered_figures": [
            {
                "id": "svg-diagram",
                "kind": "svg",
                "title": "几何示意图",
                "svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 160"><path d="M40 20V130H280" fill="none" stroke="black" stroke-width="2"/><path d="M40 110H60V130" fill="none" stroke="black"/><text x="20" y="20">A</text><text x="20" y="145">B</text><text x="285" y="140">C</text></svg>',
            }
        ],
    }
    practice = {
        "id": "practice",
        "mode": "practice",
        "include_answer": False,
        "include_explanation": False,
        "include_knowledge": False,
        "blank_lines": 3,
        "questions": [question, math, geometry],
    }
    review = {
        **practice,
        "id": "review",
        "mode": "review",
        "include_answer": True,
        "include_explanation": True,
        "include_knowledge": True,
        "questions": [
            question,
            math,
            geometry,
            *[
                {
                    **copy.deepcopy(leaf),
                    "id": f"long-{i}",
                    "stem": f"综合练习 {i + 1}：解释材料中的时间表达。",
                }
                for i in range(5)
            ],
        ],
    }
    models = [
        ModelProfile(
            id=role,
            role=role,
            base_url="https://fixture.invalid/v1",
            api_key="fixture",
            model="fixture",
            voice="fixture-voice",
        ).model_dump(exclude={"api_key"})
        for role in MODEL_ROLE_LABELS
    ]
    responses: dict[str, Any] = {
        "/api/search/options": {
            "parts": PART_LABELS,
            "default_limit": 12,
            "max_limit": 100,
            "default_methods": ["keyword"],
        },
        "/api/session": {"authenticated": True, "initialized": True, "csrf_token": "fixture"},
        "/api/subjects": [{"id": "english", "revision": 1, "name": "英语"}],
        "/api/books": [{"id": "book", "subject_id": "english", "title": "英语必修一", "asset_ids": []}],
        "/api/models": models,
        "/api/jobs": [],
        "/api/questions": [question],
        "/api/questions/question": question,
        "/api/questions/math": math,
        "/api/books/book/nodes": [{"id": "node", "title": "Listening", "parent_id": None}],
        "/api/export-snapshot/practice": practice,
        "/api/export-snapshot/review": review,
    }
    writes: list[dict[str, Any]] = []
    broken_font = False

    async def route_request(route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path == "/api/search" and route.request.method == "POST":
            body = route.request.post_data_json
            writes.append({"path": path, "body": body})
            await route.fulfill(
                json=[
                    {
                        "id": "part-math",
                        "question_id": "math",
                        "question_title": math["stem"],
                        "text": math["stem"],
                        "path": [],
                        "part": "stem",
                        "part_label": "题干",
                        "answer_confirmed": True,
                        "method": body["methods"][0],
                        "rank": 1,
                    }
                ]
            )
            return
        if path == "/api/assets/figure/preview":
            await route.fulfill(body=image.getvalue(), content_type="image/png")
            return
        if path == "/api/assets" and route.request.method == "POST":
            writes.append({"path": path})
            await route.fulfill(
                json={"id": "uploaded-audio", "revision": 1, "name": "audio.wav", "mime": "audio/wav"}
            )
            return
        if path.startswith("/api/assets/"):
            await route.fulfill(status=200, body=b"", content_type="audio/wav")
            return
        if path == "/api/subjects/english" and route.request.method == "PUT":
            body = route.request.post_data_json
            responses["/api/subjects"][0].update(body)
            writes.append({"path": path, "body": body})
            await route.fulfill(json=responses["/api/subjects"][0])
            return
        if path in responses:
            await route.fulfill(json=responses[path])
            return
        if path.startswith("/api/"):
            raise AssertionError(path)
        file = (dist / path.lstrip("/")).resolve()
        assert file.is_relative_to(dist)
        if broken_font and file.suffix in {".woff", ".woff2", ".otf", ".ttf"}:
            await route.abort()
            return
        if not file.is_file():
            file = dist / "index.html"
        await route.fulfill(
            body=file.read_bytes(),
            content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream",
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 980})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/*", route_request)
            await page.goto("http://studyquip.test/#questions?question=question")
            await page.get_by_label("当前编辑位置").select_option("0.0")
            await expect(page.get_by_role("textbox", name="题干", exact=True)).to_have_value(
                "What time is it?"
            )
            await page.get_by_role("textbox", name="题干", exact=True).fill("What is the time?")
            await page.get_by_label("当前编辑位置").select_option("0")
            await page.get_by_role("button", name="添加小题", exact=True).click()
            await page.get_by_role("combobox", name="题型", exact=True).select_option("composite")
            await page.get_by_role("button", name="添加小题", exact=True).click()
            await page.get_by_role("combobox", name="题型", exact=True).select_option("fill_blank")
            await page.get_by_role("textbox", name="题干", exact=True).fill("填空：The time is ____.")
            await page.get_by_role("button", name="添加听力", exact=True).click()
            await page.locator('input[type="file"][accept^="audio/"]').set_input_files(
                {"name": "audio.wav", "mimeType": "audio/wav", "buffer": b"fixture-audio"}
            )
            await expect(page.locator(".material-editor audio")).to_have_attribute(
                "src", "/api/assets/uploaded-audio/file"
            )
            await page.get_by_role("textbox", name="听力文稿（可选）", exact=True).fill("The time is three.")
            await page.screenshot(path=str(output / "nested-editor-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.emulate_media(color_scheme="dark")
            await page.screenshot(path=str(output / "nested-editor-mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert await page.locator(".editor-footer button").evaluate_all(
                "buttons => buttons.every(button => { const rect = button.getBoundingClientRect(); return rect.left >= 0 && rect.right <= innerWidth; })"
            )
            await page.get_by_role("button", name="预览", exact=True).click()
            await expect(page.get_by_text("1.2.1.", exact=True)).to_be_visible()
            await expect(page.get_by_text("What is the time?", exact=True)).to_be_visible()
            await page.goto("http://studyquip.test/#settings")
            await page.get_by_role("tab", name="科目管理").click()
            await page.locator(".subject-list button").filter(has_text="英语").click()
            await page.get_by_role("textbox", name="科目名称", exact=True).fill("英语听说")
            await page.get_by_role("button", name="保存名称").click()
            await expect(page.locator(".subject-list button").filter(has_text="英语听说")).to_be_visible()
            await page.get_by_role("tab", name="模型连接").click()
            await page.locator(".model-purpose-tabs").get_by_role("button", name="题目", exact=True).click()
            await page.locator(".model-row").first.get_by_role("button", name="编辑", exact=True).click()
            await expect(page.get_by_role("checkbox", name="流式请求", exact=False)).to_be_checked()
            await page.get_by_role("checkbox", name="流式请求", exact=False).uncheck()
            await page.get_by_role("button", name="关闭窗口").click()
            await page.goto("http://studyquip.test/#questions")
            await page.get_by_role("checkbox", name="选择此题导出", exact=True).check()
            await page.get_by_role("button", name="导出 PDF · 1", exact=True).click()
            await expect(page.locator(".paper-preview")).not_to_contain_text("Let's go to the library.")
            assert await page.locator(".paper-preview audio").count() == 0
            await page.get_by_text("检索相关题并加入打印", exact=True).click()
            await expect(page.get_by_role("combobox", name="检索方式", exact=True)).to_have_value("keyword")
            await page.get_by_role("textbox", name="检索内容", exact=True).fill("平方函数")
            await page.get_by_role("checkbox", name="解析", exact=True).check()
            await page.get_by_text("限定题型", exact=False).click()
            await page.get_by_role("checkbox", name="单选题", exact=True).check()
            await page.get_by_role("spinbutton", name="每种检索最多返回", exact=True).fill("5")
            await page.get_by_role("button", name="添加下一种检索", exact=True).click()
            await page.get_by_role("button", name="交换顺序", exact=True).click()
            await expect(page.get_by_role("combobox", name="第 1 种检索", exact=True)).to_have_value(
                "semantic"
            )
            await page.get_by_role("button", name="检索", exact=True).click()
            await page.get_by_role("button", name="加入打印", exact=True).click()
            await expect(page.locator(".export-order")).to_have_count(2)
            await expect(page.get_by_role("button", name="已加入", exact=True)).to_be_disabled()
            query = next(write["body"] for write in reversed(writes) if write["path"] == "/api/search")
            assert query["methods"] == ["semantic", "keyword"] and query["parts"] == ["stem", "explanation"]
            assert (
                query["question_types"] == ["single_choice"]
                and query["confirmed_only"]
                and query["limit"] == 5
            )
            await page.screenshot(path=str(output / "print-search-mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.goto("http://studyquip.test/#search")
            await page.get_by_text("限定教材范围", exact=False).click()
            await page.get_by_role("checkbox", name="英语必修一", exact=True).check()
            await page.get_by_role("combobox", name="限定目录", exact=False).select_option("node")
            await page.screenshot(path=str(output / "search-mobile.png"), full_page=True)
            await page.get_by_role("tab", name="错题", exact=True).click()
            await page.get_by_role("checkbox", name="答案", exact=True).check()
            await page.get_by_role("button", name="移除第 2 种检索", exact=True).click()
            await expect(page.get_by_role("combobox", name="检索方式", exact=True)).to_have_value("semantic")
            await page.get_by_role("textbox", name="检索内容", exact=True).fill("函数求值")
            await page.get_by_role("button", name="检索", exact=True).click()
            await expect(page.get_by_role("link", name="查看题目", exact=True)).to_have_attribute(
                "href", "#questions?question=math"
            )
            await page.evaluate("window.scrollTo(0,0)")
            await page.screenshot(path=str(output / "question-search-mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.set_viewport_size({"width": 1280, "height": 980})
            for mode in ("practice", "review"):
                await page.goto(f"http://studyquip.test/print/{mode}?token=fixture")
                await page.wait_for_function(
                    "window.__STUDYQUIP_PRINT_READY__ || window.__STUDYQUIP_PRINT_ERROR__"
                )
                assert await page.evaluate("window.__STUDYQUIP_PRINT_ERROR__ || null") is None
                assert await page.locator("[data-figure-error]").count() == 0
                assert await page.locator(".coordinate-plot").count() == 1
                assert await page.locator(".rendered-figure img").count() == 1
                if mode == "practice":
                    assert (
                        await page.get_by_text(
                            "It is three o'clock. Let's go to the library.", exact=True
                        ).count()
                        == 0
                    )
                    assert await page.locator(".answer-lines").count() == 1
                await page.pdf(
                    path=str(output / f"{mode}.pdf"),
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                )
            await page.screenshot(path=str(output / "review-screen.png"), full_page=True)
            assert not errors, errors
            broken_font = True
            failed_page = await browser.new_page()
            await failed_page.route("**/*", route_request)
            await failed_page.goto("http://studyquip.test/print/practice?token=fixture")
            await failed_page.wait_for_function("!!window.__STUDYQUIP_PRINT_ERROR__")
            assert not await failed_page.evaluate("window.__STUDYQUIP_PRINT_READY__")
        finally:
            await browser.close()
    report = {
        "directory": str(output),
        "nested_editing": True,
        "audio_upload_fixture": True,
        "subject_rename_fixture": True,
        "stream_toggle": True,
        "directory_scope": True,
        "question_section_filters": True,
        "ordered_search_default_single": True,
        "print_search_add_deduplicated": True,
        "pdfs": ["practice.pdf", "review.pdf"],
        "font_failure_detected": True,
        "browser_errors": errors,
        "app_processes_started": False,
        "external_requests": 0,
        "business_database_accessed": False,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False, indent=2))
