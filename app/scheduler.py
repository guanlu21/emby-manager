import time
import datetime
from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .emby_client import EmbyClient, EmbyError


def _get_emby_client():
    url = db.get_setting("emby_url")
    key = db.get_setting("emby_api_key")
    if not url or not key:
        return None
    return EmbyClient(url, key)


def check_and_disable_expired():
    """
    扫描本地记录的用户，如果已过期且当前仍是 active，则调用 Emby 将其禁用。
    返回本次新到期(刚被停用)的用户列表。
    """
    now = int(time.time())
    newly_expired = []
    client = _get_emby_client()
    for u in db.list_users():
        if u["status"] != "active":
            continue
        if u["expire_at"] is None:
            continue
        if u["expire_at"] <= now:
            try:
                if client:
                    client.set_disabled(u["emby_user_id"], True)
                db.update_user(u["id"], status="expired")
                db.add_log(u["username"], "auto_expire", "到期自动停用")
                newly_expired.append(u)
            except EmbyError as e:
                db.add_log(u["username"], "auto_expire_failed", str(e))
    return newly_expired


def get_expiring_soon(days: int):
    """
    返回在 `days` 天内到期（且尚未到期）的活跃用户列表。
    """
    now = int(time.time())
    threshold = now + days * 86400
    result = []
    for u in db.list_users():
        if u["status"] != "active":
            continue
        if u["expire_at"] is None:
            continue
        if now < u["expire_at"] <= threshold:
            days_left = round((u["expire_at"] - now) / 86400, 1)
            result.append({
                "username": u["username"],
                "expire_at": u["expire_at"],
                "expire_date": datetime.datetime.fromtimestamp(u["expire_at"]).strftime("%Y-%m-%d"),
                "days_left": days_left,
                "note": u.get("note", ""),
            })
    result.sort(key=lambda x: x["days_left"])
    return result


def run_auto_backup():
    """
    定时自动备份。是否真的生成由「设置 - 备份」里的开关和间隔决定，
    没开启时这个任务什么都不做，不会占用磁盘。
    """
    from . import backup  # 延迟导入，避免循环依赖
    try:
        info = backup.maybe_auto_backup()
        if info:
            db.add_log("-", "backup_auto", f"自动备份 {info['filename']}")
    except Exception as e:  # 自动备份失败绝不能让调度器挂掉
        db.add_log("-", "backup_auto_failed", str(e))


scheduler = BackgroundScheduler()


def start_scheduler():
    # 每 5 分钟检查一次到期状态，让到期后的自动停用更及时
    scheduler.add_job(check_and_disable_expired, "interval", minutes=5, id="expire_check", replace_existing=True)
    # 每 30 分钟看一眼是否该做自动备份（到点才真正生成）
    scheduler.add_job(run_auto_backup, "interval", minutes=30, id="auto_backup", replace_existing=True)
    scheduler.start()
    # 启动时立即跑一次
    check_and_disable_expired()
    run_auto_backup()
