"""显式运行的本机验收：隔离数据、真实浏览器和 PDF，不需要远程模型。

先构建 frontend 并安装 Chromium，然后 uv run python scripts/verify_local.py。
所有生成数据写入 .verification，不影响默认 data 目录；不会创建演示模型。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw
from playwright.async_api import Browser, Page, Route, async_playwright, expect

from studyquip.auth import set_password
from studyquip.config import Settings
from studyquip.db import Database, initialize
from studyquip.media import prepare_book_pages, store_upload
from studyquip.textbook import TextbookService


async def verify_empty_workspace(browser: Browser, source: Page, base: str, directory: Path) -> None:
    """同一测试会话的独立视图，只模拟列表为空，不修改测试库或正式资料。"""
    context = await browser.new_context(
        storage_state=await source.context.storage_state(),
        viewport={"width": 1440, "height": 1000},
        reduced_motion="reduce",
    )
    try:
        page = await context.new_page()

        async def empty_collection(route: Route) -> None:
            await route.fulfill(content_type="application/json", body="[]")

        await page.route(re.compile(r"/api/(subjects|books|questions|models|jobs)$"), empty_collection)
        await page.goto(f"{base}/#home")
        await page.get_by_role("heading", name="从第一道错题开始").wait_for()
        appearance = page.get_by_role("radiogroup", name="外观主题")
        system = appearance.get_by_role("radio", name="跟随系统", exact=True)
        light = appearance.get_by_role("radio", name="浅色", exact=True)
        dark = appearance.get_by_role("radio", name="深色", exact=True)
        html = page.locator("html")
        await page.emulate_media(color_scheme="light")
        await system.check()
        await expect(html).to_have_attribute("data-theme", "light")
        await page.emulate_media(color_scheme="dark")
        await expect(html).to_have_attribute("data-theme", "dark")
        await light.check()
        await page.emulate_media(color_scheme="light")
        await page.emulate_media(color_scheme="dark")
        await expect(html).to_have_attribute("data-theme", "light")
        await page.reload()
        await page.get_by_role("heading", name="从第一道错题开始").wait_for()
        await expect(light).to_be_checked()
        await expect(html).to_have_attribute("data-theme", "light")
        assert await page.evaluate("localStorage.getItem('studyquip-theme')") == "light"
        await page.screenshot(path=str(directory / "home-empty-desktop.png"), full_page=True)
        await light.press("ArrowRight")
        await expect(dark).to_be_checked()
        await expect(light).not_to_be_checked()
        await expect(system).not_to_be_checked()
        await expect(html).to_have_attribute("data-theme", "dark")
        await page.screenshot(path=str(directory / "home-empty-desktop-dark.png"), full_page=True)
        await page.set_viewport_size({"width": 320, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "窄屏首页溢出"
        await page.screenshot(path=str(directory / "home-empty-mobile-dark.png"), full_page=True)
        await (
            page.get_by_role("navigation", name="主导航").get_by_role("link", name="错题", exact=True).click()
        )
        await page.locator(".library-notice").wait_for()
        for theme, name in (("dark", "深色"), ("light", "浅色")):
            await appearance.get_by_role("radio", name=name, exact=True).check()
            await expect(html).to_have_attribute("data-theme", theme)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "资料提示条溢出"
            await page.screenshot(path=str(directory / f"notice-mobile-{theme}.png"), full_page=True)
        await page.locator(".library-notice").get_by_role("link", name="导入教材").click()
        await expect(page.get_by_role("dialog")).to_be_visible()
        await expect(page.get_by_role("textbox", name="教材名称", exact=True)).to_have_value("")
    finally:
        await context.close()


async def verify(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    directory = output / f"run-{time.time_ns()}"
    directory.mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    settings = Settings(data_dir=directory / "data", port=port)
    initialize(settings)
    db = Database(settings)
    password = secrets.token_urlsafe(24)
    set_password(db, password)
    subject = db.put("subject", {"name": "物理"})
    book = db.put("book", {"title": "物理 · 必修第一册", "subject_id": subject["id"], "status": "ready"})
    service = TextbookService(db)
    root = service.ensure_root(book["id"])
    node = service.set_node(book["id"], {"title": "课题一 · 运动与惯性", "parent_id": root["id"]})
    block = service.ingest_text(
        book["id"],
        "惯性是物体保持原有运动状态的性质。物体的加速度与合外力成正比，与质量成反比。"
        "初速度为零的匀加速直线运动，位移等于加速度与时间平方乘积的一半。",
    )[0]
    service.apply_operations(
        book["id"],
        str(time.time_ns()),
        [
            {
                "op": "update",
                "id": block["id"],
                "base_revision": block["revision"],
                "changes": {"node_id": node["id"]},
            }
        ],
        actor="human",
    )
    block = db.get("block", block["id"])
    canvas = Image.new("RGB", (720, 200), "white")
    draw = ImageDraw.Draw(canvas)
    draw.line((60, 150, 650, 150), fill="black", width=3)
    draw.rectangle((250, 60, 390, 145), outline=(30, 90, 90), width=4)
    draw.line((400, 95, 600, 95), fill="black", width=3)
    draw.polygon(((600, 95), (585, 87), (585, 103)), fill="black")
    buffer = io.BytesIO()
    canvas.save(buffer, "PNG")
    asset = store_upload(settings, "受力示意图.png", buffer.getvalue())
    db.put("asset", asset, id=asset["id"])
    explanation = {
        "citations": [
            {
                "book_id": book["id"],
                "book_title": book["title"],
                "node_path": [node["title"]],
                "block_id": block["id"],
                "revision": block["revision"],
                "quote": block["text"],
            }
        ],
        "has_textbook_evidence": True,
    }
    questions: list[dict[str, Any]] = []
    for kind, stem, answer, options in [
        (
            "single_choice",
            "关于惯性，下列说法正确的是（ ）。",
            "B",
            [{"id": "A", "text": "只有运动的物体具有惯性"}, {"id": "B", "text": "一切物体都具有惯性"}],
        ),
        (
            "short_answer",
            "如图，质量为 $m=2\\,\\mathrm{kg}$ 的物体受到 $F=6\\,\\mathrm{N}$ 的水平合力。求加速度并说明理由。",
            "$a=\\frac{F}{m}=3\\,\\mathrm{m/s^2}$。",
            [],
        ),
        ("fill_blank", "物体的位移满足 $x=2t^2$，则加速度为 ____ $\\mathrm{m/s^2}$。", ["4"], []),
    ]:
        details = {
            "single_choice": {
                "summary": "惯性是所有物体具有的性质，运动或静止都不影响物体是否有惯性。",
                "steps": ["A 将惯性限定为运动物体具有，错误。", "B 表述正确，因此选择 B。"],
                "knowledge_points": ["惯性"],
            },
            "short_answer": {
                "summary": "对物体应用牛顿第二定律，使用合外力和质量求加速度。",
                "steps": [
                    "以图中的物体为研究对象，沿水平合力方向建立正方向。题目给出的 $F$ 已经是合力，"
                    "无需再扣除一个假设的摩擦力。",
                    "根据 $F=ma$，得到 $a=F/m$。质量与力均已使用国际单位制，可以直接代入。",
                    "代入得 $a=6/2=3\\,\\mathrm{m/s^2}$。加速度方向与合外力相同，为图中水平向右。",
                    "加速度描述速度变化的快慢。仅由向右的加速度不能判断此刻速度的方向；如果物体原来"
                    "向左运动，也可能先减速，再停止并向右加速。",
                    "检查结果时可以固定质量比较力：合力增大，加速度应随之增大；固定合力增大质量，"
                    "加速度应减小。所得表达式与这两个关系一致。",
                ],
                "knowledge_points": ["牛顿第二定律", "合力与加速度方向", "国际单位制"],
            },
            "fill_blank": {
                "summary": "将题目给出的位移表达式与初速度为零的匀加速运动公式比较。",
                "steps": [
                    "标准形式为 $x=\\frac{1}{2}at^2$，题目为 $x=2t^2$。",
                    "比较 $t^2$ 的系数得到 $a/2=2$，因此 $a=4\\,\\mathrm{m/s^2}$。",
                ],
                "knowledge_points": ["匀加速直线运动", "位移时间关系"],
            },
        }[kind]
        questions.append(
            db.put(
                "question",
                {
                    "subject_id": subject["id"],
                    "type": kind,
                    "stem": stem,
                    "options": options,
                    "answer": answer,
                    "answer_confirmed": True,
                    "status": "ready",
                    "explanation": {**explanation, **details},
                    "explanation_stale": False,
                    "notes": "本机验收样本",
                    "figure_asset_ids": [asset["id"]] if kind == "short_answer" else [],
                    "book_ids": [book["id"]],
                },
            )
        )
    environment = {**os.environ, "STUDYQUIP_DATA_DIR": str(settings.data_dir), "STUDYQUIP_PORT": str(port)}
    log_path = directory / "server.log"
    report: dict[str, Any] = {"directory": str(directory), "platform": sys.platform}
    with log_path.open("w") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "studyquip", "run"], env=environment, stdout=log, stderr=log
        )
        base = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(base_url=base, timeout=15) as client:
                for attempt in range(100):
                    try:
                        if (await client.get("/api/health")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError(f"服务未启动，请查看 {log_path}")
                session = (await client.post("/api/login", json={"password": password})).json()
                client.headers.update({"X-CSRF-Token": session["csrf_token"], "Origin": base})
                snapshots: dict[str, str] = {}
                for mode in ("practice", "review"):
                    response = await client.post(
                        "/api/exports", json={"question_ids": [q["id"] for q in questions], "mode": mode}
                    )
                    response.raise_for_status()
                    job = response.json()
                    snapshots[mode] = job["resource_id"]
                    for _ in range(180):
                        current = next(
                            row for row in (await client.get("/api/jobs")).json() if row["id"] == job["id"]
                        )
                        if current["status"] in {"completed", "failed", "waiting_review"}:
                            break
                        await asyncio.sleep(0.3)
                    assert current["status"] == "completed", current.get("error")
                    pdf = await client.get(f"/api/exports/{job['resource_id']}/file")
                    pdf.raise_for_status()
                    (directory / f"{mode}.pdf").write_bytes(pdf.content)
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch()
                    try:
                        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(base)
                        await page.locator('input[type="password"]').fill(password)
                        await page.locator('button[type="submit"]').click()
                        await page.get_by_role("heading", name="首页", exact=True).wait_for()
                        await page.get_by_role("heading", name="最近错题", exact=True).wait_for()
                        await page.screenshot(path=str(directory / "home-desktop.png"), full_page=True)
                        await page.locator(f'a[href="#questions?question={questions[0]["id"]}"]').click()
                        await page.get_by_role("dialog").wait_for()
                        await expect(page.get_by_role("textbox", name="题干", exact=True)).to_have_value(
                            questions[0]["stem"]
                        )
                        await page.get_by_role("button", name="关闭窗口", exact=True).click()
                        navigation = page.get_by_role("navigation", name="主导航")
                        await navigation.get_by_role("link", name="首页", exact=True).click()
                        await page.get_by_role("link", name="导入教材", exact=True).click()
                        await page.get_by_role("dialog").wait_for()
                        await expect(page.get_by_role("textbox", name="教材名称", exact=True)).to_have_value(
                            ""
                        )
                        await page.get_by_role("button", name="关闭窗口", exact=True).click()
                        await navigation.get_by_role("link", name="首页", exact=True).click()
                        await page.locator(f'a[href="#books?book={book["id"]}"]').click()
                        await page.get_by_role("heading", name=book["title"], exact=True).wait_for()
                        await navigation.get_by_role("link", name="首页", exact=True).click()
                        await page.set_viewport_size({"width": 390, "height": 844})
                        await page.get_by_role("heading", name="最近错题", exact=True).wait_for()
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        await page.screenshot(path=str(directory / "home-mobile.png"), full_page=True)
                        await page.get_by_role("link", name="录入错题", exact=True).click()
                        await page.get_by_role("dialog").wait_for()
                        await expect(page.get_by_role("textbox", name="题干", exact=True)).to_have_value("")
                        await page.get_by_role("button", name="关闭窗口", exact=True).click()
                        report["home_shortcuts_and_recent_links"] = True
                        await verify_empty_workspace(browser, page, base, directory)
                        report["empty_home_and_notice"] = "桌面、320px 窄屏、浅色与深色；空列表为浏览器夹具"
                        report["theme_switch"] = "单选、键盘方向切换、刷新持久化、系统跟随及固定外观隔离通过"
                        await page.set_viewport_size({"width": 1440, "height": 1000})
                        await navigation.get_by_role("link", name="错题", exact=True).click()
                        await page.get_by_role("heading", name="错题", exact=True).wait_for()
                        await page.screenshot(path=str(directory / "desktop.png"), full_page=True)
                        await page.set_viewport_size({"width": 390, "height": 844})
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                            "移动页面横向溢出"
                        )
                        await page.screenshot(path=str(directory / "mobile.png"), full_page=True)
                        await (
                            page.get_by_role("radiogroup", name="外观主题")
                            .get_by_role("radio", name="深色", exact=True)
                            .check()
                        )
                        await page.wait_for_timeout(250)
                        await page.screenshot(path=str(directory / "mobile-dark.png"), full_page=True)
                        await page.get_by_role("button", name="录入错题", exact=True).click()
                        await page.get_by_role("dialog").wait_for()
                        await page.wait_for_timeout(250)
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                            "手机题目表单溢出"
                        )
                        await page.screenshot(
                            path=str(directory / "mobile-question-form.png"), full_page=True
                        )
                        reason = page.get_by_label("做错原因（可留空）")
                        optimize = page.get_by_label("让 AI 优化表述")
                        await expect(reason).to_have_value("")
                        await expect(optimize).not_to_be_checked()
                        original_reason = "我当时漏看了条件，把初速度当成了零。"
                        await reason.fill(original_reason)
                        async with page.expect_response(
                            lambda response: (
                                response.url == f"{base}/api/questions" and response.request.method == "POST"
                            )
                        ) as saved_response:
                            await page.get_by_role("button", name="保存", exact=True).click()
                        saved = await (await saved_response.value).json()
                        assert saved["error_reason"] == original_reason
                        assert saved["optimize_error_reason"] is False
                        await optimize.check()
                        question_url = f"{base}/api/questions/{saved['id']}"
                        async with page.expect_response(
                            lambda response: response.url == question_url and response.request.method == "PUT"
                        ) as updated_response:
                            await page.get_by_role("button", name="保存", exact=True).click()
                        updated = await (await updated_response.value).json()
                        assert updated["error_reason"] == original_reason
                        assert updated["optimize_error_reason"] is True
                        async with page.expect_response(question_url):
                            await page.get_by_role("button", name="载入最新结果").click()
                        await expect(reason).to_have_value(original_reason)
                        await expect(optimize).to_be_checked()
                        await reason.scroll_into_view_if_needed()
                        await page.screenshot(path=str(directory / "mobile-error-reason.png"), full_page=True)
                        report["error_reason_saved_and_reloaded"] = True
                        await page.get_by_role("button", name="关闭窗口", exact=True).click()
                        await page.locator('a[href="#settings"]').click()
                        await page.get_by_role("button", name="＋ 添加模型配置").click()
                        await page.wait_for_timeout(250)
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                            "手机模型表单溢出"
                        )
                        await page.screenshot(path=str(directory / "mobile-model-form.png"), full_page=True)
                        assert not errors, errors
                        snap = db.get("export", snapshots["review"])
                        font_page = await browser.new_page()
                        await font_page.route("**/fonts/*.otf", lambda route: route.abort())
                        await font_page.goto(f"{base}/print/{snap['id']}?token={snap['token']}")
                        await font_page.wait_for_function("!!window.__STUDYQUIP_PRINT_ERROR__")
                        report["font_failure_detected"] = True
                        report["browser_errors"] = errors
                        report["mobile_viewport"] = "390×844（模拟视口，非真实手机）"
                    finally:
                        await browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            db.close()
    import pypdfium2 as pdfium

    for mode in ("practice", "review"):
        with pdfium.PdfDocument(directory / f"{mode}.pdf") as pdf:
            all_text = ""
            for index in range(len(pdf)):
                with closing(pdf[index]) as page:
                    width, height = page.get_size()
                    assert abs(width - 595.3) < 2 and abs(height - 841.9) < 2
                    with closing(page.get_textpage()) as textpage:
                        all_text += textpage.get_text_bounded()
                    bitmap = page.render(scale=1.4)
                    try:
                        bitmap.to_pil().save(directory / f"{mode}-page-{index + 1}.png")
                    finally:
                        bitmap.close()
            assert "StudyQuip" in all_text and "惯性" in all_text
            assert ("正确答案" in all_text) is (mode == "review")
            report[mode] = {"pages": len(pdf), "text_characters": len(all_text)}
    imported = store_upload(settings, "验收教材.pdf", (directory / "review.pdf").read_bytes())
    pages = await asyncio.to_thread(prepare_book_pages, settings, imported)
    assert pages and "StudyQuip" in pages[0]["text"] and pages[0]["image_asset"]
    report["pdf_import_pages"] = len(pages)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify(Path(".verification").resolve())), ensure_ascii=False, indent=2))
