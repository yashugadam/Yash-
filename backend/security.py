"""Single-user JWT authentication: password hashing, token create/verify, seed."""
import os
import bcrypt
import jwt as pyjwt
from datetime import datetime, timezone, timedelta
from fastapi import Request

from config import JWT_ALGORITHM, JWT_TTL_HOURS, logger
from db import db


def _jwt_secret():
    return os.environ["JWT_SECRET"]


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _create_token(username: str) -> str:
    payload = {"sub": username, "type": "access",
               "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS)}
    return pyjwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def _decode_token(token: str):
    try:
        payload = pyjwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except Exception:
        return None


def _bearer_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def _seed_auth_user():
    """Create/refresh the single login user from .env (username + bcrypt-hashed password)."""
    username = os.environ.get("AUTH_USERNAME", "").strip()
    password = os.environ.get("AUTH_PASSWORD", "")
    if not username or not password:
        logger.warning("AUTH_USERNAME/AUTH_PASSWORD not set — login will not work.")
        return
    existing = await db.auth_user.find_one({"_id": "singleton"})
    if not existing:
        await db.auth_user.insert_one(
            {"_id": "singleton", "username": username, "password_hash": _hash_password(password)})
    elif existing.get("username") != username or not _verify_password(password, existing.get("password_hash", "")):
        await db.auth_user.update_one({"_id": "singleton"},
            {"$set": {"username": username, "password_hash": _hash_password(password)}})
