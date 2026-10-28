"""Downloadable backups for Emby Manager's persistent SQLite database."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db

FORMAT = "emby-manager-backup"
VERSION = 1
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
REQUIRED_TABLES = {"settings", "users", "logs"}


class BackupError(ValueError):
    """A backup archive is invalid or cannot be safely restored."""


def create_backup() -> bytes:
    """Make a consistent SQLite snapshot and return it in a versioned ZIP archive."""
    db.init_db()
    with db.DB_LOCK:
        target = sqlite3.connect(":memory:")
        try:
            with db.get_conn() as source:
                source.backup(target)
            database = target.serialize()
        finally:
            target.close()

    if len(database) > MAX_ARCHIVE_BYTES:
        raise BackupError("数据库超过 256 MB 备份限制")
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": "app.db",
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(database) + len(manifest_bytes) > MAX_ARCHIVE_BYTES:
        raise BackupError("数据库和备份清单合计超过 256 MB 限制")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("app.db", database)
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise BackupError("生成的备份超过 256 MB 限制")
    return result


def validate_backup(raw: bytes) -> tuple[dict, bytes]:
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise BackupError("备份文件为空或超过 256 MB 限制")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError("文件不是有效的 ZIP 备份") from exc

    with archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise BackupError("备份中包含重复文件名")
        if set(names) != {"manifest.json", "app.db"} or any(i.is_dir() for i in infos):
            raise BackupError("备份内容不完整或包含不支持的文件")
        if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
            raise BackupError("备份解压后超过 256 MB 限制")
        try:
            manifest_bytes = archive.read("manifest.json")
            database = archive.read("app.db")
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise BackupError("备份内容损坏或无法解压") from exc

    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("备份清单不是有效 JSON") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("version") != VERSION or manifest.get("database") != "app.db"):
        raise BackupError("不支持此备份格式或版本")

    try:
        check = sqlite3.connect(":memory:")
        try:
            check.deserialize(database)
            integrity = check.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not integrity or integrity[0] != "ok" or not REQUIRED_TABLES.issubset(tables):
                raise BackupError("数据库完整性检查失败或缺少必要数据表")
        finally:
            check.close()
    except BackupError:
        raise
    except (sqlite3.DatabaseError, ValueError) as exc:
        raise BackupError("数据库无效或已损坏") from exc
    return manifest, database


def restore_backup(raw: bytes) -> dict:
    """Validate first, then atomically replace the configured SQLite database."""
    manifest, database = validate_backup(raw)
    database_path = Path(db.DB_PATH).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{database_path.name}.", suffix=".restore",
                                          dir=database_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(database)
            target.flush()
            os.fsync(target.fileno())
        # Validate the on-disk staged copy before touching the live database.
        check = sqlite3.connect(str(temporary_path))
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not integrity or integrity[0] != "ok" or not REQUIRED_TABLES.issubset(tables):
                raise BackupError("恢复文件写入后未通过数据库完整性检查")
        finally:
            check.close()

        with db.DB_LOCK:
            os.replace(temporary_path, database_path)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)
        return {"ok": True, "created_at": manifest.get("created_at")}
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"恢复失败：{exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
