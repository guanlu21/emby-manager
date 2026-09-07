"""
SQLite 数据访问层。
所有数据保存在 /app/data/app.db (通过 volume 挂载持久化)。
"""
import sqlite3
import json
import os
import time
import secrets
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/app/data/app.db")


def _row_to_dict(row):
    return dict(row) if row is not None else None


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emby_user_id TEXT UNIQUE NOT NULL,
                username TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                expire_at INTEGER,               -- unix timestamp, NULL = 永久
                library_ids TEXT DEFAULT '[]',   -- JSON array
                enable_download INTEGER DEFAULT 0,
                enable_upload INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',    -- active / disabled / expired / deleted
                last_notified_date TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                username TEXT,
                action TEXT,
                detail TEXT
            )
        """)
        # 默认设置
        defaults = {
            "emby_url": "",
            "emby_api_key": "",
            "admin_password_hash": "",
            "admin_password_salt": "",
            "qinglong_token": secrets.token_hex(16),
            "setup_done": "0",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )


# ---------------- settings ----------------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_all_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# ---------------- users ----------------

def add_user(emby_user_id, username, expire_at, library_ids, enable_download, enable_upload, note=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users
               (emby_user_id, username, note, created_at, expire_at, library_ids,
                enable_download, enable_upload, status)
               VALUES (?,?,?,?,?,?,?,?, 'active')""",
            (
                emby_user_id, username, note, int(time.time()), expire_at,
                json.dumps(library_ids), int(enable_download), int(enable_upload),
            ),
        )


def list_users(include_deleted=False):
    with get_conn() as conn:
        if include_deleted:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users WHERE status != 'deleted' ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_user(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_dict(row)


def get_user_by_emby_id(emby_user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE emby_user_id=?", (emby_user_id,)).fetchone()
        return _row_to_dict(row)


def update_user(user_id, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values())
    values.append(user_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {keys} WHERE id=?", values)


def delete_user_row(user_id):
    with get_conn() as conn:
        conn.execute("UPDATE users SET status='deleted' WHERE id=?", (user_id,))


def add_log(username, action, detail=""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO logs (ts, username, action, detail) VALUES (?,?,?,?)",
            (int(time.time()), username, action, detail),
        )


def recent_logs(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
