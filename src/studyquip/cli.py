"""本地启动、运行时诊断与显式迁移命令。"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from typing import Annotated

import typer
import uvicorn

from .auth import set_password
from .config import Settings
from .db import Database, initialize
from .doctor import diagnose

app = typer.Typer(help="StudyQuip · 个人错题与教材学习工作台", no_args_is_help=True)


@app.command()
def doctor() -> None:
    """检查实际解释器、SQLite、全文／向量扩展及共享锁后端。"""
    report = diagnose()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(1)


@app.command()
def init() -> None:
    """初始化数据库并交互设置单用户密码。"""
    settings = Settings()
    initialize(settings)
    db = Database(settings)
    try:
        if not db.get("auth", "owner"):
            password = os.environ.get("STUDYQUIP_INIT_PASSWORD") or typer.prompt(
                "设置登录密码（至少 8 个字符）", hide_input=True, confirmation_prompt=True
            )
            set_password(db, password)
        typer.echo(f"已初始化：{settings.data_dir}\n运行 uv run studyquip run 启动 Web 和 worker。")
    finally:
        db.close()


@app.command()
def upgrade() -> None:
    """停止 Web/worker 后升级数据库架构。"""
    initialize(Settings())
    typer.echo("数据库已升级到当前版本。")


@app.command()
def password() -> None:
    """更改登录密码并使旧会话失效。"""
    settings = Settings()
    if not settings.db_path.exists():
        raise typer.BadParameter("请先运行 studyquip init")
    db = Database(settings)
    try:
        value = typer.prompt("新密码（至少 8 个字符）", hide_input=True, confirmation_prompt=True)
        set_password(db, value)
    finally:
        db.close()
    typer.echo("密码已更新，旧会话已失效。")


@app.command()
def web(
    host: Annotated[str | None, typer.Option()] = None, port: Annotated[int | None, typer.Option()] = None
) -> None:
    """启动 Web；模型任务需要同时启动 worker。"""
    from .api import create_app

    settings = Settings()
    if host:
        settings.host = host
    if port:
        settings.port = port
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)


@app.command()
def worker() -> None:
    """启动唯一的后台 worker，处理到期任务。"""
    from .worker import run_worker

    asyncio.run(run_worker(Settings()))


@app.command()
def run(
    host: Annotated[str | None, typer.Option()] = None, port: Annotated[int | None, typer.Option()] = None
) -> None:
    """同时启动 Web 与 worker，Ctrl+C 一起停止。"""
    from .db import runtime_lock

    settings = Settings()
    with runtime_lock(settings):
        pass
    environment = os.environ.copy()
    if host:
        environment["STUDYQUIP_HOST"] = host
    if port:
        environment["STUDYQUIP_PORT"] = str(port)
    children = [
        subprocess.Popen(
            [sys.executable, "-m", "studyquip", name],
            env=environment,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        for name in ("web", "worker")
    ]
    handled_signals = [signal.SIGTERM]
    if sys.platform == "win32":
        handled_signals.append(signal.SIGBREAK)
    previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}

    def stop_children(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    for signum in handled_signals:
        signal.signal(signum, stop_children)
    try:
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        for child in children:
            if child.poll() is None:
                if sys.platform == "win32":
                    try:
                        child.send_signal(signal.CTRL_BREAK_EVENT)
                    except OSError:
                        # A detached Windows process may not have a console event channel.
                        child.terminate()
                else:
                    child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    app()
