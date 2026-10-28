"""
备份与恢复。

设计要点：
- 备份的是一个 zip 包，里面只有两样东西：
    app.db      SQLite 数据库的在线快照（用 sqlite3 的 backup API 生成，
                不直接拷文件，避免拷到写了一半的库）
    meta.json   备份元信息（生成时间、来源、用户数、版本、架构等）
- 恢复时先做一次"恢复前"自动备份，再校验上传文件，校验通过才替换在线数据库，
  最后跑一遍 init_db() 把老版本可能缺的字段补齐。任何一个环节失败都保持原库不动。
- 备份文件统一放在数据卷内（默认 /app/data/backups），容器重建不丢。
"""
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import datetime

from . import db

APP_VERSION = "1.2.0"

# 备份目录默认跟着数据库走：数据卷挂载 /app/data，备份也就落在数据卷里
BACKUP_DIR = os.environ.get(
    "BACKUP_DIR", os.path.join(os.path.dirname(db.DB_PATH) or ".", "backups")
)

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
REQUIRED_TABLES = ("settings", "users", "logs")


class BackupError(Exception):
    """备份/恢复过程中可预期的错误（用于给用户看的中文提示）"""


# ---------------- 小工具 ----------------

def human_size(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return (f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}")
        num /= 1024
    return f"{num:.1f} GB"


def _ensure_dir() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def _ts_str(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def runtime_info() -> dict:
    return {
        "version": APP_VERSION,
        "arch": platform.machine() or "unknown",
        "python": platform.python_version(),
        "db_path": db.DB_PATH,
        "backup_dir": BACKUP_DIR,
    }


def db_stats() -> dict:
    """当前在线数据库的规模统计（页面上给用户一个直观的参照）"""
    stats = {"users": 0, "active": 0, "logs": 0, "db_size": 0, "db_size_text": "-"}
    try:
        with db.get_conn() as conn:
            stats["users"] = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE status != 'deleted'"
            ).fetchone()["c"]
            stats["active"] = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE status = 'active'"
            ).fetchone()["c"]
            stats["logs"] = conn.execute("SELECT COUNT(*) AS c FROM logs").fetchone()["c"]
    except sqlite3.Error:
        pass
    try:
        if os.path.exists(db.DB_PATH):
            stats["db_size"] = os.path.getsize(db.DB_PATH)
            stats["db_size_text"] = human_size(stats["db_size"])
    except OSError:
        pass
    return stats


# ---------------- 生成备份 ----------------

def _snapshot_db(tmp_path: str) -> None:
    """用 sqlite3 的在线备份 API 导出一份一致的数据库快照"""
    if not os.path.exists(db.DB_PATH):
        raise BackupError("数据库文件还不存在，暂时没有可备份的数据")
    src = sqlite3.connect(db.DB_PATH)
    try:
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as e:
        raise BackupError(f"读取数据库失败: {e}")
    finally:
        src.close()


def _unique_name(kind: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"emby-manager-{stamp}-{kind}"
    name = f"{base}.zip"
    n = 2
    while os.path.exists(os.path.join(BACKUP_DIR, name)):
        name = f"{base}-{n}.zip"
        n += 1
    return name


def create_backup(kind: str = "manual", note: str = "") -> dict:
    """
    生成一个备份包，返回元信息（含 filename / size / path）。
    kind: manual（手动） / auto（自动） / pre-restore（恢复前）
    """
    _ensure_dir()
    filename = _unique_name(kind)
    final_path = os.path.join(BACKUP_DIR, filename)
    tmp_db = os.path.join(BACKUP_DIR, f".snapshot-{os.getpid()}-{int(time.time() * 1000)}.db")
    try:
        _snapshot_db(tmp_db)
        created_ts = int(time.time())
        stats = db_stats()
        meta = {
            "app": "emby-manager",
            "version": APP_VERSION,
            "created_at": _ts_str(created_ts),
            "created_ts": created_ts,
            "kind": kind,
            "note": note,
            "arch": platform.machine() or "unknown",
            "python": platform.python_version(),
            "db_bytes": os.path.getsize(tmp_db),
            "stats": {
                "users": stats["users"],
                "active": stats["active"],
                "logs": stats["logs"],
            },
        }
        with zipfile.ZipFile(final_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "app.db")
            zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    finally:
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass

    size = os.path.getsize(final_path)
    return {
        "filename": filename,
        "path": final_path,
        "size": size,
        "size_text": human_size(size),
        "created_ts": meta["created_ts"],
        "created_at": meta["created_at"],
        "kind": kind,
        "meta": meta,
    }


# ---------------- 列出 / 删除 ----------------

def _read_meta(path: str) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            if "meta.json" in zf.namelist():
                return json.loads(zf.read("meta.json").decode("utf-8", "replace"))
    except (zipfile.BadZipFile, OSError, ValueError):
        pass
    return {}


def list_backups() -> list:
    """按时间倒序列出备份目录下的所有备份包"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for name in os.listdir(BACKUP_DIR):
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            mtime = int(os.path.getmtime(path))
        except OSError:
            continue
        meta = _read_meta(path)
        created_ts = int(meta.get("created_ts") or mtime)
        stats = meta.get("stats") or {}
        items.append({
            "filename": name,
            "path": path,
            "size": size,
            "size_text": human_size(size),
            "created_ts": created_ts,
            "created_at": _ts_str(created_ts),
            "kind": meta.get("kind") or "unknown",
            "note": meta.get("note") or "",
            "version": meta.get("version") or "",
            "arch": meta.get("arch") or "",
            "users": stats.get("users"),
            "logs": stats.get("logs"),
            "valid_meta": bool(meta),
        })
    items.sort(key=lambda x: x["created_ts"], reverse=True)
    return items


def _safe_path(name: str) -> str:
    """只允许删除/下载备份目录下的普通 zip 文件名，避免路径穿越"""
    if not name or not _NAME_RE.match(name) or os.path.basename(name) != name:
        raise BackupError("文件名不合法")
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(path):
        raise BackupError("备份文件不存在或已被删除")
    return path


def backup_file_path(name: str) -> str:
    return _safe_path(name)


def delete_backup(name: str) -> bool:
    path = _safe_path(name)
    try:
        os.remove(path)
        return True
    except OSError as e:
        raise BackupError(f"删除失败: {e}")


# ---------------- 校验与恢复 ----------------

def _extract_db_bytes(data: bytes):
    """
    从上传内容里取出数据库字节。
    支持两种形态：本工具的 zip 备份包，或直接上传的 app.db 文件。
    返回 (db_bytes, meta)
    """
    if not data:
        raise BackupError("上传的文件是空的")
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                meta = {}
                if "meta.json" in names:
                    try:
                        meta = json.loads(zf.read("meta.json").decode("utf-8", "replace"))
                    except ValueError:
                        meta = {}
                target = None
                for n in names:
                    if os.path.basename(n) == "app.db":
                        target = n
                        break
                if target is None:
                    for n in names:
                        if n.lower().endswith((".db", ".sqlite", ".sqlite3")):
                            target = n
                            break
                if target is None:
                    raise BackupError("压缩包里没有找到 app.db，这不是本工具的备份文件")
                return zf.read(target), meta
        except zipfile.BadZipFile:
            raise BackupError("压缩包已损坏，无法读取")
    if data[:16] == b"SQLite format 3\x00":
        return data, {}
    raise BackupError("文件格式不对，请上传本工具导出的 .zip 备份包（或 app.db 文件）")


def _validate_db_file(path: str) -> dict:
    """确认这是一个结构正确的本工具数据库，返回统计信息"""
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as e:
        raise BackupError(f"无法打开数据库: {e}")
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as e:
            raise BackupError(f"数据库文件损坏: {e}")
        if not row or str(row[0]).lower() != "ok":
            raise BackupError(f"数据库完整性检查未通过: {row[0] if row else '未知错误'}")
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.DatabaseError as e:
            raise BackupError(f"读取数据库结构失败: {e}")
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise BackupError(
                "这个数据库不是 Emby 用户管理面板的数据（缺少表：%s）" % "、".join(missing)
            )
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        except sqlite3.DatabaseError as e:
            raise BackupError(f"读取用户表结构失败: {e}")
        if "emby_user_id" not in cols or "username" not in cols:
            raise BackupError("用户表结构不正确（缺少 emby_user_id / username 字段）")
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "active": conn.execute(
                "SELECT COUNT(*) FROM users WHERE status = 'active'"
            ).fetchone()[0],
            "logs": conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0],
            "settings": conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0],
        }
    except sqlite3.DatabaseError as e:
        raise BackupError(f"数据库文件损坏: {e}")
    finally:
        conn.close()


def restore_from_upload(data: bytes, original_name: str = "", note: str = "") -> dict:
    """
    用上传的备份内容覆盖在线数据库。
    失败时抛 BackupError，且在线数据库保持原样。
    """
    db_bytes, meta = _extract_db_bytes(data)
    _ensure_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_path = os.path.join(BACKUP_DIR, f".restore-{os.getpid()}-{stamp}.db")
    with open(tmp_path, "wb") as f:
        f.write(db_bytes)

    try:
        incoming = _validate_db_file(tmp_path)
        before = db_stats()

        # 1) 先给当前数据留一份"恢复前"备份，恢复错了还能退回来
        pre_backup = None
        if os.path.exists(db.DB_PATH):
            try:
                pre_backup = create_backup(
                    kind="pre-restore",
                    note=f"从 {original_name or '上传文件'} 恢复前的自动备份",
                )
            except BackupError:
                pre_backup = None

        # 2) 替换数据库文件。数据目录与备份目录同一文件系统，用原子替换；
        #    跨文件系统（比如把 BACKUP_DIR 挂到别处）时退化为拷贝。
        try:
            os.replace(tmp_path, db.DB_PATH)
        except OSError:
            shutil.copyfile(tmp_path, db.DB_PATH)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        # 3) 顺手清掉可能残留的 WAL/SHM，避免读到旧库的增量日志
        for suffix in ("-wal", "-shm"):
            leftover = db.DB_PATH + suffix
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

        # 4) 老版本备份包可能缺字段/缺默认设置，跑一遍初始化补齐
        db.init_db()
        after = db_stats()
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    db.add_log(
        "-",
        "restore",
        f"从 {original_name or '上传文件'} 恢复数据：用户 {before['users']} → {after['users']} 个",
    )
    return {
        "original_name": original_name,
        "backup_version": meta.get("version") or "",
        "backup_created_at": meta.get("created_at") or "",
        "backup_kind": meta.get("kind") or "",
        "incoming": incoming,
        "before": before,
        "after": after,
        "pre_backup": (pre_backup or {}).get("filename") or "",
    }


# ---------------- 自动备份 ----------------

def auto_backup_enabled() -> bool:
    return (db.get_setting("backup_auto_enabled", "0") or "0") == "1"


def _int_setting(key: str, default: int) -> int:
    try:
        return max(1, int(db.get_setting(key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def prune_auto_backups() -> int:
    """只保留最近 N 份自动备份，返回删除数量"""
    keep = _int_setting("backup_auto_keep", 7)
    autos = [b for b in list_backups() if b["kind"] == "auto"]
    removed = 0
    for item in autos[keep:]:
        try:
            os.remove(item["path"])
            removed += 1
        except OSError:
            pass
    return removed


def maybe_auto_backup() -> dict | None:
    """供定时任务调用：到点才生成，未开启则直接返回 None"""
    if not auto_backup_enabled():
        return None
    hours = _int_setting("backup_auto_interval_hours", 24)
    autos = [b for b in list_backups() if b["kind"] == "auto"]
    if autos:
        elapsed = time.time() - autos[0]["created_ts"]
        if elapsed < hours * 3600 - 60:
            return None
    info = create_backup(kind="auto", note=f"每 {hours} 小时自动备份")
    prune_auto_backups()
    return info