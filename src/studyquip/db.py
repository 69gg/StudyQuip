"""短写事务、只读连接与版本化记录存储。"""

from __future__ import annotations

import copy
import os
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import portalocker
import sqlite_vec
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from .config import Settings
from .doctor import require_runtime

SCHEMA_REVISION = "0002"
metadata = MetaData()
records = Table(
    "records",
    metadata,
    Column("kind", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
record_history = Table(
    "record_history",
    metadata,
    Column("kind", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("snapshot", JSON, nullable=False),
)
jobs_table = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("resource_id", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("checkpoint", JSON, nullable=False),
    Column("status", String, nullable=False, index=True),
    Column("not_before", Float, nullable=False),
    Column("bypass_window", Integer, nullable=False),
    Column("owner", String),
    Column("lease_token", String),
    Column("lease_until", Float),
    Column("attempts", Integer, nullable=False),
    Column("error", String),
    Column("result", JSON),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)


class ConflictError(ValueError):
    """请求基于过期业务版本。"""


def make_engine(settings: Settings, *, readonly: bool = False) -> Engine:
    uri = settings.db_path.as_uri() + ("?mode=ro" if readonly else "?mode=rw")

    def connect() -> sqlite3.Connection:
        dbapi = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        dbapi.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
        dbapi.execute("PRAGMA foreign_keys=ON")
        dbapi.execute("PRAGMA synchronous=FULL")
        dbapi.enable_load_extension(True)
        try:
            sqlite_vec.load(dbapi)
        finally:
            dbapi.enable_load_extension(False)
        if readonly:
            dbapi.execute("PRAGMA query_only=ON")
        return dbapi

    engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)

    @event.listens_for(engine, "begin")
    def begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN" if readonly else "BEGIN IMMEDIATE")

    return engine


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rw = make_engine(settings)
        self.ro = make_engine(settings, readonly=True)

    @contextmanager
    def write(self) -> Iterator[Connection]:
        with self.rw.begin() as conn:
            yield conn

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.ro.connect() as conn:
            with conn.begin():
                yield conn

    @staticmethod
    def _record(row: Any) -> dict[str, Any]:
        return {
            **copy.deepcopy(row["data"]),
            "id": row["id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get(self, kind: str, id: str, conn: Connection | None = None) -> dict[str, Any] | None:
        if conn is None:
            with self.read() as active:
                return self.get(kind, id, active)
        row = (
            conn.execute(select(records).where(records.c.kind == kind, records.c.id == id)).mappings().first()
        )
        return self._record(row) if row else None

    def list(
        self,
        kind: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 1000,
        offset: int = 0,
        conn: Connection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.read() as active:
                return self.list(kind, filters=filters, limit=limit, offset=offset, conn=active)
        query = select(records).where(records.c.kind == kind)
        for key, value in (filters or {}).items():
            expr = records.c.data[key].as_string()
            query = query.where(expr.in_(value) if isinstance(value, (list, tuple, set)) else expr == value)
        rows = conn.execute(
            query.order_by(records.c.created_at, records.c.id).limit(limit).offset(offset)
        ).mappings()
        return [self._record(row) for row in rows]

    def put(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        id: str | None = None,
        expected_revision: int | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            with self.write() as active:
                return self.put(kind, data, id=id, expected_revision=expected_revision, conn=active)
        record_id = id or str(uuid.uuid4())
        current = self.get(kind, record_id, conn)
        if expected_revision is not None and (current or {}).get("revision", 0) != expected_revision:
            raise ConflictError("内容已被更新，请刷新后重试")
        now = time.time()
        clean = {
            key: copy.deepcopy(value)
            for key, value in data.items()
            if key not in {"id", "revision", "created_at", "updated_at"}
        }
        revision = current["revision"] + 1 if current else 1
        created = current["created_at"] if current else now
        values = {"revision": revision, "data": clean, "updated_at": now}
        if current:
            conn.execute(
                records.update().where(records.c.kind == kind, records.c.id == record_id).values(**values)
            )
        else:
            conn.execute(records.insert().values(kind=kind, id=record_id, created_at=created, **values))
        snapshot = {**clean, "id": record_id, "revision": revision, "created_at": created, "updated_at": now}
        conn.execute(
            record_history.insert().values(kind=kind, id=record_id, revision=revision, snapshot=snapshot)
        )
        if kind == "question":
            from .question_index import QuestionIndex

            QuestionIndex(self).sync(snapshot, conn)
        return snapshot

    def delete(self, kind: str, id: str, *, conn: Connection | None = None) -> None:
        if conn is None:
            with self.write() as active:
                self.delete(kind, id, conn=active)
            return
        conn.execute(records.delete().where(and_(records.c.kind == kind, records.c.id == id)))
        if kind == "question":
            from .question_index import QuestionIndex

            QuestionIndex(self).sync({"id": id, "deleted": True}, conn)

    def history(self, kind: str, id: str, conn: Connection | None = None) -> list[dict[str, Any]]:
        if conn is None:
            with self.read() as active:
                return self.history(kind, id, active)
        return list(
            conn.execute(
                select(record_history.c.snapshot)
                .where(record_history.c.kind == kind, record_history.c.id == id)
                .order_by(record_history.c.revision)
            ).scalars()
        )

    def secret(self) -> bytes:
        return (self.settings.data_dir / ".secret").read_bytes()

    def close(self) -> None:
        self.rw.dispose()
        self.ro.dispose()


def file_lock(path: Path, *, shared: bool, timeout: float = 3) -> portalocker.Lock:
    flags = (portalocker.LOCK_SH if shared else portalocker.LOCK_EX) | portalocker.LOCK_NB
    return portalocker.Lock(str(path), mode="a+b", timeout=timeout, flags=flags)


def initialize(settings: Settings) -> None:
    require_runtime()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(settings.data_dir / ".schema.lock", shared=False, timeout=settings.lock_timeout_seconds):
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        secret_path = settings.data_dir / ".secret"
        if not secret_path.exists():
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        engine = make_engine(settings)
        try:
            with engine.begin() as conn:
                config.attributes["connection"] = conn
                command.upgrade(config, "head")
        finally:
            engine.dispose()
        settings.files_dir.mkdir(exist_ok=True)


@contextmanager
def runtime_lock(settings: Settings, worker: bool = False) -> Iterator[None]:
    require_runtime()
    if not settings.db_path.exists():
        raise RuntimeError("数据库尚未初始化，请运行 uv run studyquip init")
    with ExitStack() as stack:
        stack.enter_context(
            file_lock(settings.data_dir / ".schema.lock", shared=True, timeout=settings.lock_timeout_seconds)
        )
        if worker:
            stack.enter_context(
                file_lock(
                    settings.data_dir / ".worker.lock", shared=False, timeout=settings.lock_timeout_seconds
                )
            )
        with sqlite3.connect(settings.db_path.as_uri() + "?mode=ro", uri=True) as conn:
            try:
                version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            except sqlite3.OperationalError as exc:
                raise RuntimeError("数据库架构未初始化，请停止应用后运行 studyquip upgrade") from exc
            if not version or version[0] != SCHEMA_REVISION:
                raise RuntimeError("数据库版本不匹配，请停止 Web/worker 后运行 studyquip upgrade")
        yield
