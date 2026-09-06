"""单用户密码认证与服务器端会话。"""

import hashlib
import secrets
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .db import Database

HASHER = PasswordHasher()
COOKIE_NAME = "studyquip_session"


def set_password(db: Database, password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    with db.write() as conn:
        db.put("auth", {"password_hash": HASHER.hash(password)}, id="owner", conn=conn)
        for session in db.list("session", conn=conn):
            db.delete("session", session["id"], conn=conn)


def authenticate(db: Database, password: str) -> bool:
    user = db.get("auth", "owner")
    if not user:
        return False
    try:
        return HASHER.verify(user["password_hash"], password)
    except (VerificationError, InvalidHashError):
        return False


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Database) -> tuple[str, dict[str, Any]]:
    token = secrets.token_urlsafe(32)
    session = db.put(
        "session",
        {
            "csrf_token": secrets.token_urlsafe(32),
            "expires_at": time.time() + db.settings.session_hours * 3600,
        },
        id=token_id(token),
    )
    return token, session


def get_session(db: Database, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    session = db.get("session", token_id(token))
    if not session or session["expires_at"] <= time.time():
        return None
    return session
