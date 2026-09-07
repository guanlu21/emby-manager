import datetime
import json
import time

from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, auth
from .emby_client import EmbyClient, EmbyError
from .scheduler import start_scheduler, check_and_disable_expired, get_expiring_soon

app = FastAPI(title="Emby 用户管理")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def on_startup():
    db.init_db()
    start_scheduler()


def setup_done() -> bool:
    return db.get_setting("setup_done", "0") == "1"


def render(request, name, **ctx):
    ctx["request"] = request
    ctx["logged_in"] = auth.is_logged_in(request)
    return templates.TemplateResponse(name, ctx)


# ---------------- 登录 / 初始化 ----------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", setup_done=setup_done())


@app.post("/setup")
def do_setup(request: Request, password: str = Form(...), password2: str = Form(...)):
    if setup_done():
        return RedirectResponse("/login", status_code=303)
    if password != password2:
        return render(request, "login.html", setup_done=False, error="两次输入的密码不一致")
    if len(password) < 4:
        return render(request, "login.html", setup_done=False, error="密码至少 4 位")
    auth.set_admin_password(password)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.create_session_token(),
                     max_age=auth.SESSION_MAX_AGE, httponly=True)
    return resp


@app.post("/login")
def do_login(request: Request, password: str = Form(...)):
    if not auth.verify_password(password):
        return render(request, "login.html", setup_done=setup_done(), error="密码错误")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.create_session_token(),
                     max_age=auth.SESSION_MAX_AGE, httponly=True)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


def _guard(request: Request):
    """未登录/未初始化则返回重定向 Response，否则返回 None"""
    if not setup_done():
        return RedirectResponse("/login", status_code=303)
    if not auth.is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


# ---------------- 工具函数 ----------------

def _get_client_or_none():
    url = db.get_setting("emby_url")
    key = db.get_setting("emby_api_key")
    if not url or not key:
        return None
    return EmbyClient(url, key)


def _decorate_user(u: dict) -> dict:
    u = dict(u)
    u["library_ids"] = json.loads(u.get("library_ids") or "[]")
    u["library_count"] = len(u["library_ids"])
    if u.get("expire_at"):
        u["expire_at_str"] = datetime.datetime.fromtimestamp(u["expire_at"]).strftime("%Y-%m-%d")
        u["expire_date_input"] = datetime.datetime.fromtimestamp(u["expire_at"]).strftime("%Y-%m-%d")
        days_left = (u["expire_at"] - int(time.time())) / 86400
        u["days_left"] = round(days_left, 1)
    else:
        u["expire_at_str"] = None
        u["expire_date_input"] = ""
        u["days_left"] = None
    return u


# ---------------- 仪表盘 ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str = "", error: str = ""):
    guard = _guard(request)
    if guard:
        return guard
    users = [_decorate_user(u) for u in db.list_users()]
    return render(request, "dashboard.html", users=users, msg=msg, error=error,
                  emby_configured=bool(db.get_setting("emby_url") and db.get_setting("emby_api_key")))


# ---------------- 新建用户 ----------------

@app.get("/users/new", response_class=HTMLResponse)
def new_user_page(request: Request):
    guard = _guard(request)
    if guard:
        return guard
    client = _get_client_or_none()
    libraries, error = [], ""
    if client:
        try:
            libraries = client.list_libraries()
        except EmbyError as e:
            error = str(e)
    else:
        error = "尚未配置 Emby 连接信息，请先前往设置页面配置"
    return render(request, "user_form.html", mode="new", user=None, libraries=libraries,
                  selected_lib_ids=[], error=error, form=None)


@app.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    note: str = Form(""),
    duration_preset: str = Form(""),
    expire_date: str = Form(""),
    library_ids: list = Form([]),
    enable_download: str = Form(None),
    enable_upload: str = Form(None),
):
    guard = _guard(request)
    if guard:
        return guard

    client = _get_client_or_none()
    if not client:
        return RedirectResponse("/settings?error=" + "请先配置Emby连接信息", status_code=303)

    # 计算到期时间戳
    expire_at = None
    try:
        if duration_preset == "custom" and expire_date:
            dt = datetime.datetime.strptime(expire_date, "%Y-%m-%d")
            expire_at = int(dt.timestamp())
        elif duration_preset and duration_preset != "custom":
            expire_at = int(time.time()) + int(duration_preset) * 86400
    except ValueError:
        pass

    try:
        emby_user = client.create_user(username)
        emby_id = emby_user["Id"]
        client.set_password(emby_id, password)
        client.set_libraries_and_permissions(
            emby_id, library_ids, bool(enable_download), bool(enable_upload)
        )
        db.add_user(
            emby_user_id=emby_id, username=username, expire_at=expire_at,
            library_ids=library_ids, enable_download=bool(enable_download),
            enable_upload=bool(enable_upload), note=note,
        )
        db.add_log(username, "create", f"expire_at={expire_at}")
    except EmbyError as e:
        client2 = _get_client_or_none()
        libraries = client2.list_libraries() if client2 else []
        return render(request, "user_form.html", mode="new", user=None, libraries=libraries,
                      selected_lib_ids=library_ids, error=f"创建失败: {e}",
                      form={"username": username, "password": password, "note": note})

    return RedirectResponse(f"/?msg=用户 {username} 创建成功", status_code=303)


# ---------------- 编辑用户 ----------------

@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_page(request: Request, user_id: int):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    u = _decorate_user(u)
    client = _get_client_or_none()
    libraries, error = [], ""
    if client:
        try:
            libraries = client.list_libraries()
        except EmbyError as e:
            error = str(e)
    return render(request, "user_form.html", mode="edit", user=u, libraries=libraries,
                  selected_lib_ids=u["library_ids"], error=error, form=None)


@app.post("/users/{user_id}/update")
def update_user(
    request: Request,
    user_id: int,
    note: str = Form(""),
    duration_preset: str = Form(""),
    expire_date: str = Form(""),
    library_ids: list = Form([]),
    enable_download: str = Form(None),
    enable_upload: str = Form(None),
):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)

    expire_at = u["expire_at"]
    try:
        if duration_preset == "custom" and expire_date:
            dt = datetime.datetime.strptime(expire_date, "%Y-%m-%d")
            expire_at = int(dt.timestamp())
        elif duration_preset == "":
            expire_at = None
        elif duration_preset:
            expire_at = int(time.time()) + int(duration_preset) * 86400
    except ValueError:
        pass

    client = _get_client_or_none()
    try:
        if client:
            client.set_libraries_and_permissions(
                u["emby_user_id"], library_ids, bool(enable_download), bool(enable_upload)
            )
        db.update_user(
            user_id, note=note, expire_at=expire_at,
            library_ids=json.dumps(library_ids),
            enable_download=int(bool(enable_download)),
            enable_upload=int(bool(enable_upload)),
        )
        db.add_log(u["username"], "update", "")
    except EmbyError as e:
        return RedirectResponse(f"/users/{user_id}/edit?error={e}", status_code=303)

    return RedirectResponse("/?msg=修改成功", status_code=303)


# ---------------- 续期 / 停用 / 启用 / 删除 ----------------

@app.post("/users/{user_id}/extend")
def extend_user(request: Request, user_id: int, days: int = Form(...)):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    base = u["expire_at"] if u["expire_at"] and u["expire_at"] > int(time.time()) else int(time.time())
    new_expire = base + days * 86400
    updates = {"expire_at": new_expire}
    # 如果用户当前是因到期被禁用的状态，续期后自动恢复启用
    if u["status"] in ("expired", "disabled") and new_expire > int(time.time()):
        client = _get_client_or_none()
        try:
            if client:
                client.set_disabled(u["emby_user_id"], False)
            updates["status"] = "active"
        except EmbyError:
            pass
    db.update_user(user_id, **updates)
    db.add_log(u["username"], "extend", f"+{days}天")
    return RedirectResponse(f"/?msg=已为 {u['username']} 续期 {days} 天", status_code=303)


@app.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    client = _get_client_or_none()
    try:
        if client:
            client.set_disabled(u["emby_user_id"], True)
        db.update_user(user_id, status="disabled")
        db.add_log(u["username"], "manual_disable", "")
    except EmbyError as e:
        return RedirectResponse(f"/?error=停用失败: {e}", status_code=303)
    return RedirectResponse(f"/?msg=已停用 {u['username']}", status_code=303)


@app.post("/users/{user_id}/enable")
def enable_user(request: Request, user_id: int):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    client = _get_client_or_none()
    try:
        if client:
            client.set_disabled(u["emby_user_id"], False)
        db.update_user(user_id, status="active")
        db.add_log(u["username"], "manual_enable", "")
    except EmbyError as e:
        return RedirectResponse(f"/?error=启用失败: {e}", status_code=303)
    return RedirectResponse(f"/?msg=已启用 {u['username']}", status_code=303)


@app.post("/users/{user_id}/delete")
def delete_user(request: Request, user_id: int):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    client = _get_client_or_none()
    try:
        if client:
            client.delete_user(u["emby_user_id"])
        db.delete_user_row(user_id)
        db.add_log(u["username"], "delete", "")
    except EmbyError as e:
        return RedirectResponse(f"/?error=删除失败: {e}", status_code=303)
    return RedirectResponse(f"/?msg=已删除 {u['username']}", status_code=303)


# ---------------- 设置 ----------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", error: str = ""):
    guard = _guard(request)
    if guard:
        return guard
    return render(request, "settings.html", settings=db.get_all_settings(), msg=msg, error=error)


@app.post("/settings/emby")
def save_emby_settings(request: Request, emby_url: str = Form(...), emby_api_key: str = Form(...)):
    guard = _guard(request)
    if guard:
        return guard
    try:
        client = EmbyClient(emby_url, emby_api_key)
        info = client.test_connection()
        db.set_setting("emby_url", emby_url.rstrip("/"))
        db.set_setting("emby_api_key", emby_api_key)
        return RedirectResponse(
            f"/settings?msg=连接成功，服务器: {info.get('ServerName', 'Emby')} v{info.get('Version','')}",
            status_code=303,
        )
    except EmbyError as e:
        return RedirectResponse(f"/settings?error=连接失败: {e}", status_code=303)


@app.post("/settings/password")
def change_password(request: Request, password: str = Form(...), password2: str = Form(...)):
    guard = _guard(request)
    if guard:
        return guard
    if password != password2:
        return RedirectResponse("/settings?error=两次输入的密码不一致", status_code=303)
    auth.set_admin_password(password)
    return RedirectResponse("/settings?msg=密码已修改", status_code=303)


@app.post("/settings/reset-token")
def reset_token(request: Request):
    guard = _guard(request)
    if guard:
        return guard
    import secrets
    db.set_setting("qinglong_token", secrets.token_hex(16))
    return RedirectResponse("/settings?msg=Token 已重置", status_code=303)


# ---------------- 青龙面板对接接口 ----------------

@app.get("/api/qinglong/report")
def qinglong_report(token: str = Query(...), days: int = Query(3)):
    real_token = db.get_setting("qinglong_token")
    if not real_token or token != real_token:
        return JSONResponse({"error": "invalid token"}, status_code=403)

    newly_expired = check_and_disable_expired()
    expiring_soon = get_expiring_soon(days)

    return JSONResponse({
        "expiring_soon": expiring_soon,
        "expired_today": [
            {"username": u["username"], "note": u.get("note", "")} for u in newly_expired
        ],
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/api/health")
def health():
    return {"status": "ok"}
