import datetime
import json
import secrets
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


def _calc_expire_at(duration_preset: str, expire_date: str, base_ts: int = None):
    """
    统一计算到期时间戳。duration_preset 为空字符串代表"永久"（返回 None）。
    """
    try:
        if duration_preset == "custom" and expire_date:
            dt = datetime.datetime.strptime(expire_date, "%Y-%m-%d")
            return int(dt.timestamp())
        elif duration_preset and duration_preset != "custom":
            base = base_ts if base_ts is not None else int(time.time())
            return base + int(duration_preset) * 86400
    except ValueError:
        pass
    return None


_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_random_password(length: int = 10) -> str:
    """生成随机密码，去掉容易看混的字符 (0/O, 1/l/I 等)。"""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _do_disable(u: dict):
    client = _get_client_or_none()
    if client:
        client.set_disabled(u["emby_user_id"], True)
    db.update_user(u["id"], status="disabled")
    db.add_log(u["username"], "manual_disable", "")


def _do_enable(u: dict):
    client = _get_client_or_none()
    if client:
        client.set_disabled(u["emby_user_id"], False)
    db.update_user(u["id"], status="active")
    db.add_log(u["username"], "manual_enable", "")


def _do_delete(u: dict):
    client = _get_client_or_none()
    if client:
        client.delete_user(u["emby_user_id"])
    db.delete_user_row(u["id"])
    db.add_log(u["username"], "delete", "")


def _do_extend(u: dict, days: int):
    base = u["expire_at"] if u["expire_at"] and u["expire_at"] > int(time.time()) else int(time.time())
    new_expire = base + days * 86400
    updates = {"expire_at": new_expire}
    if u["status"] in ("expired", "disabled") and new_expire > int(time.time()):
        client = _get_client_or_none()
        try:
            if client:
                client.set_disabled(u["emby_user_id"], False)
            updates["status"] = "active"
        except EmbyError:
            pass
    db.update_user(u["id"], **updates)
    db.add_log(u["username"], "extend", f"+{days}天")


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
                  selected_lib_ids=[], error=error, form=None, live_policy=None)


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
    enable_download_transcode: str = Form(None),
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
            emby_id, library_ids, bool(enable_download),
            bool(enable_download_transcode), bool(enable_upload),
        )
        db.add_user(
            emby_user_id=emby_id, username=username, expire_at=expire_at,
            library_ids=library_ids, enable_download=bool(enable_download),
            enable_download_transcode=bool(enable_download_transcode),
            enable_upload=bool(enable_upload), note=note,
        )
        db.add_log(username, "create", f"expire_at={expire_at}")
    except EmbyError as e:
        client2 = _get_client_or_none()
        libraries = client2.list_libraries() if client2 else []
        return render(request, "user_form.html", mode="new", user=None, libraries=libraries,
                      selected_lib_ids=library_ids, error=f"创建失败: {e}",
                      form={"username": username, "password": password, "note": note},
                      live_policy=None)

    return RedirectResponse(f"/?msg=用户 {username} 创建成功", status_code=303)


# ---------------- 导入已有 Emby 用户 ----------------

@app.get("/users/import", response_class=HTMLResponse)
def import_list_page(request: Request, q: str = ""):
    guard = _guard(request)
    if guard:
        return guard
    client = _get_client_or_none()
    if not client:
        return RedirectResponse("/settings?error=请先配置Emby连接信息", status_code=303)

    managed_ids = {u["emby_user_id"] for u in db.list_users()}
    error = ""
    candidates = []
    try:
        emby_users = client.list_emby_users()
        libraries = client.list_libraries()
        lib_name_by_id = {l["Id"]: l["Name"] for l in libraries}
        for eu in emby_users:
            if eu.get("Id") in managed_ids:
                continue
            name = eu.get("Name", "")
            if q and q.lower() not in name.lower():
                continue
            policy = eu.get("Policy", {}) or {}
            enabled_ids = policy.get("EnabledFolders") or []
            lib_names = [lib_name_by_id.get(i, i) for i in enabled_ids]
            candidates.append({
                "id": eu.get("Id"),
                "name": name,
                "is_admin": policy.get("IsAdministrator", False),
                "enable_all_folders": policy.get("EnableAllFolders", False),
                "library_names": lib_names,
                "is_disabled": policy.get("IsDisabled", False),
            })
    except EmbyError as e:
        error = str(e)

    return render(request, "import_list.html", candidates=candidates, error=error, q=q)


@app.get("/users/import/{emby_user_id}", response_class=HTMLResponse)
def import_user_page(request: Request, emby_user_id: str, error: str = ""):
    guard = _guard(request)
    if guard:
        return guard
    if db.get_user_by_emby_id(emby_user_id):
        return RedirectResponse("/?error=该用户已在管理列表中", status_code=303)
    client = _get_client_or_none()
    if not client:
        return RedirectResponse("/settings?error=请先配置Emby连接信息", status_code=303)
    try:
        emby_user = client.get_user(emby_user_id)
        libraries = client.list_libraries()
    except EmbyError as e:
        return RedirectResponse(f"/users/import?error={e}", status_code=303)

    policy = emby_user.get("Policy", {}) or {}
    enable_all = policy.get("EnableAllFolders", False)
    if enable_all:
        # 原本不限制媒体库，默认帮它全选，方便管理员直接确认保存
        selected_lib_ids = [l["Id"] for l in libraries]
    else:
        selected_lib_ids = policy.get("EnabledFolders") or []

    fake_user = {
        "id": None,
        "username": emby_user.get("Name"),
        "note": "",
        "enable_download": policy.get("EnableContentDownloading", False),
        "enable_download_transcode": policy.get("EnableMediaConversion", False),
        "enable_upload": policy.get("AllowCameraUpload", False),
        "expire_date_input": "",
    }

    return render(request, "user_form.html", mode="import", user=fake_user,
                  libraries=libraries, selected_lib_ids=selected_lib_ids,
                  error=error, form=None, live_policy=None,
                  emby_user_id=emby_user_id, enable_all_folders_hint=enable_all)


@app.post("/users/import/{emby_user_id}")
def do_import_user(
    request: Request,
    emby_user_id: str,
    note: str = Form(""),
    duration_preset: str = Form(""),
    expire_date: str = Form(""),
    library_ids: list = Form([]),
    enable_download: str = Form(None),
    enable_download_transcode: str = Form(None),
    enable_upload: str = Form(None),
):
    guard = _guard(request)
    if guard:
        return guard
    if db.get_user_by_emby_id(emby_user_id):
        return RedirectResponse("/?error=该用户已在管理列表中", status_code=303)
    client = _get_client_or_none()
    if not client:
        return RedirectResponse("/settings?error=请先配置Emby连接信息", status_code=303)

    expire_at = None
    try:
        if duration_preset == "custom" and expire_date:
            dt = datetime.datetime.strptime(expire_date, "%Y-%m-%d")
            expire_at = int(dt.timestamp())
        elif duration_preset and duration_preset != "custom":
            expire_at = int(time.time()) + int(duration_preset) * 86400
    except ValueError:
        pass

    username = emby_user_id
    try:
        emby_user = client.get_user(emby_user_id)
        username = emby_user.get("Name")
        client.set_libraries_and_permissions(
            emby_user_id, library_ids, bool(enable_download),
            bool(enable_download_transcode), bool(enable_upload),
        )
        db.add_user(
            emby_user_id=emby_user_id, username=username, expire_at=expire_at,
            library_ids=library_ids, enable_download=bool(enable_download),
            enable_download_transcode=bool(enable_download_transcode),
            enable_upload=bool(enable_upload), note=note,
        )
        db.add_log(username, "import", "从已有 Emby 用户导入")
    except EmbyError as e:
        return RedirectResponse(f"/users/import/{emby_user_id}?error={e}", status_code=303)

    return RedirectResponse(f"/?msg=已导入用户 {username}", status_code=303)


# ---------------- 编辑用户 ----------------

@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_page(request: Request, user_id: int, error: str = ""):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    u = _decorate_user(u)
    client = _get_client_or_none()
    libraries, live_policy = [], None
    # 复选框默认用本地记录的 library_ids 兜底；如果能连上 Emby，优先用
    # Emby 里实时的 EnabledFolders 来勾选，这样才是真正生效的状态
    # （避免本地记录跟 Emby 实际状态不一致时，页面显示的勾选状态是错的）。
    selected_lib_ids = u["library_ids"]
    if client:
        try:
            libraries = client.list_libraries()
        except EmbyError as e:
            error = str(e)
        try:
            live_policy = client.get_effective_policy_summary(u["emby_user_id"], libraries)
            if not live_policy.get("enable_all_folders"):
                selected_lib_ids = live_policy.get("enabled_folder_ids") or []
        except EmbyError as e:
            live_policy = {"error": str(e)}
    return render(request, "user_form.html", mode="edit", user=u, libraries=libraries,
                  selected_lib_ids=selected_lib_ids, error=error, form=None,
                  live_policy=live_policy)


@app.post("/users/{user_id}/update")
def update_user(
    request: Request,
    user_id: int,
    note: str = Form(""),
    duration_preset: str = Form(""),
    expire_date: str = Form(""),
    library_ids: list = Form([]),
    enable_download: str = Form(None),
    enable_download_transcode: str = Form(None),
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
                u["emby_user_id"], library_ids, bool(enable_download),
                bool(enable_download_transcode), bool(enable_upload),
            )
        db.update_user(
            user_id, note=note, expire_at=expire_at,
            library_ids=json.dumps(library_ids),
            enable_download=int(bool(enable_download)),
            enable_download_transcode=int(bool(enable_download_transcode)),
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
    _do_extend(u, days)
    return RedirectResponse(f"/?msg=已为 {u['username']} 续期 {days} 天", status_code=303)


@app.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int):
    guard = _guard(request)
    if guard:
        return guard
    u = db.get_user(user_id)
    if not u:
        return RedirectResponse("/?error=用户不存在", status_code=303)
    try:
        _do_disable(u)
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
    try:
        _do_enable(u)
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
    try:
        _do_delete(u)
    except EmbyError as e:
        return RedirectResponse(f"/?error=删除失败: {e}", status_code=303)
    return RedirectResponse(f"/?msg=已删除 {u['username']}", status_code=303)


# ---------------- 批量操作 ----------------

@app.get("/users/batch-new", response_class=HTMLResponse)
def batch_new_page(request: Request):
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
    return render(request, "batch_new.html", libraries=libraries, error=error,
                  results=None, form=None)


@app.post("/users/batch-new", response_class=HTMLResponse)
def batch_create_users(
    request: Request,
    usernames: str = Form(...),
    password_mode: str = Form("random"),
    fixed_password: str = Form(""),
    note: str = Form(""),
    duration_preset: str = Form(""),
    expire_date: str = Form(""),
    library_ids: list = Form([]),
    enable_download: str = Form(None),
    enable_download_transcode: str = Form(None),
    enable_upload: str = Form(None),
):
    guard = _guard(request)
    if guard:
        return guard

    client = _get_client_or_none()
    if not client:
        return RedirectResponse("/settings?error=请先配置Emby连接信息", status_code=303)

    expire_at = _calc_expire_at(duration_preset, expire_date)

    # 一行一个用户名，去空行、去重（保持顺序）
    seen = set()
    names = []
    for line in usernames.splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    results = []
    for username in names:
        password = (
            fixed_password if password_mode == "fixed" and fixed_password
            else _gen_random_password()
        )
        try:
            emby_user = client.create_user(username)
            emby_id = emby_user["Id"]
            client.set_password(emby_id, password)
            client.set_libraries_and_permissions(
                emby_id, library_ids, bool(enable_download),
                bool(enable_download_transcode), bool(enable_upload),
            )
            db.add_user(
                emby_user_id=emby_id, username=username, expire_at=expire_at,
                library_ids=library_ids, enable_download=bool(enable_download),
                enable_download_transcode=bool(enable_download_transcode),
                enable_upload=bool(enable_upload), note=note,
            )
            db.add_log(username, "create", f"批量创建 expire_at={expire_at}")
            results.append({"username": username, "password": password,
                             "ok": True, "message": "创建成功"})
        except EmbyError as e:
            results.append({"username": username, "password": "",
                             "ok": False, "message": str(e)})

    try:
        libraries = client.list_libraries()
    except EmbyError:
        libraries = []
    ok_count = sum(1 for r in results if r["ok"])
    return render(request, "batch_new.html", libraries=libraries, error="",
                  results=results, ok_count=ok_count, total_count=len(results),
                  form=None)


@app.post("/users/batch-extend")
def batch_extend_users(request: Request, user_ids: list = Form([]), days: int = Form(...)):
    guard = _guard(request)
    if guard:
        return guard
    ok, fail = 0, 0
    for uid in user_ids:
        u = db.get_user(int(uid))
        if not u:
            fail += 1
            continue
        _do_extend(u, days)
        ok += 1
    msg = f"批量续期完成：成功 {ok} 个" + (f"，失败 {fail} 个" if fail else "")
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.post("/users/batch-disable")
def batch_disable_users(request: Request, user_ids: list = Form([])):
    guard = _guard(request)
    if guard:
        return guard
    ok, fail = 0, 0
    for uid in user_ids:
        u = db.get_user(int(uid))
        if not u:
            fail += 1
            continue
        try:
            _do_disable(u)
            ok += 1
        except EmbyError:
            fail += 1
    msg = f"批量停用完成：成功 {ok} 个" + (f"，失败 {fail} 个" if fail else "")
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.post("/users/batch-enable")
def batch_enable_users(request: Request, user_ids: list = Form([])):
    guard = _guard(request)
    if guard:
        return guard
    ok, fail = 0, 0
    for uid in user_ids:
        u = db.get_user(int(uid))
        if not u:
            fail += 1
            continue
        try:
            _do_enable(u)
            ok += 1
        except EmbyError:
            fail += 1
    msg = f"批量启用完成：成功 {ok} 个" + (f"，失败 {fail} 个" if fail else "")
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.post("/users/batch-delete")
def batch_delete_users(request: Request, user_ids: list = Form([])):
    guard = _guard(request)
    if guard:
        return guard
    ok, fail = 0, 0
    for uid in user_ids:
        u = db.get_user(int(uid))
        if not u:
            fail += 1
            continue
        try:
            _do_delete(u)
            ok += 1
        except EmbyError:
            fail += 1
    msg = f"批量删除完成：成功 {ok} 个" + (f"，失败 {fail} 个" if fail else "")
    return RedirectResponse(f"/?msg={msg}", status_code=303)


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
