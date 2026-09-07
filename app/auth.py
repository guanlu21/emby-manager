import hashlib
import os
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException

from . import db

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
serializer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_COOKIE = "emby_mgr_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 天


def hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return digest, salt


def verify_password(password: str) -> bool:
    stored_hash = db.get_setting("admin_password_hash", "")
    salt = db.get_setting("admin_password_salt", "")
    if not stored_hash:
        return False
    digest, _ = hash_password(password, salt)
    return secrets.compare_digest(digest, stored_hash)


def set_admin_password(password: str):
    digest, salt = hash_password(password)
    db.set_setting("admin_password_hash", digest)
    db.set_setting("admin_password_salt", salt)
    db.set_setting("setup_done", "1")


def create_session_token() -> str:
    return serializer.dumps({"ok": True})


def verify_session_token(token: str) -> bool:
    try:
        serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired, Exception):
        return False


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    return verify_session_token(token)


def require_login(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
