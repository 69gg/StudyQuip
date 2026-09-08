"""Id-preserving subject defaults and renaming."""

import unicodedata
from typing import Any

from .db import ConflictError, Database


def subject_name(value: str) -> str:
    name = unicodedata.normalize("NFC", value).strip()
    if not name:
        raise ValueError("科目名称不能为空")
    return name


def ensure_default_subjects(db: Database, names: list[str]) -> None:
    with db.write() as conn:
        state = db.get("app_state", "subject_defaults", conn) or {"subjects": {}}
        mapped = dict(state["subjects"])
        existing = db.list("subject", conn=conn)
        for value in names:
            name = subject_name(value)
            if name in mapped:
                continue  # A renamed default keeps its identity and is not recreated.
            match = next((row for row in existing if subject_name(row["name"]) == name), None)
            match = match or db.put("subject", {"name": name}, conn=conn)
            mapped[name] = match["id"]
            existing.append(match)
        if mapped != state["subjects"]:
            db.put("app_state", {"subjects": mapped}, id="subject_defaults", conn=conn)


def save_subject(
    db: Database, name: str, identifier: str | None = None, revision: int | None = None
) -> dict[str, Any]:
    name = subject_name(name)
    with db.write() as conn:
        current = db.get("subject", identifier, conn) if identifier else None
        if identifier and not current:
            raise ValueError("科目不存在")
        existing = next(
            (
                row
                for row in db.list("subject", conn=conn)
                if subject_name(row["name"]) == name and row["id"] != identifier
            ),
            None,
        )
        if existing:
            if identifier:
                raise ConflictError("已有同名科目，请使用其他名称；已有题目和教材不会被合并或删除")
            return existing
        return db.put(
            "subject", {**(current or {}), "name": name}, id=identifier, expected_revision=revision, conn=conn
        )
