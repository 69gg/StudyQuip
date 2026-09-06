"""检查当前解释器的真实能力，不依赖声明版本。"""

import importlib.util
import platform
import sqlite3
import sys
from typing import Any

import sqlite_vec

MINIMUM_SQLITE = (3, 51, 3)


def diagnose() -> dict[str, Any]:
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
        "checks": {},
        "errors": [],
    }
    checks = report["checks"]
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
        report["errors"].append("SQLite 必须 ≥3.51.3；当前链接版本不满足 WAL 基线")
    with sqlite3.connect(":memory:") as conn:
        report["sqlite_source_id"] = conn.execute("SELECT sqlite_source_id()").fetchone()[0]
        try:
            conn.execute("CREATE VIRTUAL TABLE doctor_fts USING fts5(body)")
            conn.execute("INSERT INTO doctor_fts VALUES ('StudyQuip')")
            checks["fts5"] = (
                conn.execute(
                    "SELECT count(*) FROM doctor_fts WHERE doctor_fts MATCH ?", ('"StudyQuip"',)
                ).fetchone()[0]
                == 1
            )
        except sqlite3.Error as exc:
            checks["fts5"] = False
            report["errors"].append(f"FTS5 不可用：{exc}")
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            report["sqlite_vec"] = conn.execute("SELECT vec_version()").fetchone()[0]
            value = conn.execute(
                "SELECT vec_distance_cosine(?, ?)",
                (sqlite_vec.serialize_float32([1.0, 0.0]), sqlite_vec.serialize_float32([1.0, 0.0])),
            ).fetchone()[0]
            checks["sqlite_vec"] = abs(value) < 1e-6
        except (sqlite3.Error, AttributeError, OSError) as exc:
            checks["sqlite_vec"] = False
            report["errors"].append(f"sqlite-vec 无法加载：{exc}")
        finally:
            if hasattr(conn, "enable_load_extension"):
                conn.enable_load_extension(False)
    checks["shared_lock_backend"] = (
        platform.system() != "Windows" or importlib.util.find_spec("win32file") is not None
    )
    report["lock_backend"] = "win32/LockFileEx" if platform.system() == "Windows" else "fcntl/flock"
    if not checks["shared_lock_backend"]:
        report["errors"].append("Windows 共享锁需要 portalocker[win32]，请 uv sync --locked")
    report["ok"] = not report["errors"] and all(checks.values())
    if not report["ok"]:
        report["instructions"] = [
            "按原安装来源升级到项目固定的 uv 版本（见 pyproject.toml）。",
            "uv python install --reinstall 3.13.15",
            "uv sync --locked --managed-python --python 3.13.15",
            "uv run studyquip doctor",
            "不要仅升级系统 sqlite3 命令；报告上述解释器路径、平台及 sqlite_source_id。",
        ]
    return report


def require_runtime() -> dict[str, Any]:
    report = diagnose()
    if not report["ok"]:
        import json

        raise RuntimeError(json.dumps(report, ensure_ascii=False, indent=2))
    return report
