# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Web Dashboard Server
Admin + User system with per-user isolation, limits, expiry, VIP & bulk upload
"""

import asyncio
import json
import os
import time
import secrets
from typing import Dict, List, Any, Optional
from aiohttp import web

from user_manager import (
    create_user, validate_user, get_user, add_account_to_user,
    add_accounts_bulk, remove_account_from_user, list_all_users,
    delete_user, extend_user_time, get_admin_credentials,
    is_user_expired, is_user_vip, set_user_vip,
    get_user_accounts_export, MAX_ACCOUNT_LIMIT,
)

# Optional — levels helper for progress on export
try:
    from levels import get_level_progress, get_level_from_exp
    _HAS_LEVELS = True
except Exception:
    _HAS_LEVELS = False
    def get_level_progress(exp):  # fallback
        return {"level": 1, "next_level": 2, "current_in_level": 0,
                "total_needed": 1, "remaining": 1, "percent": 0.0, "is_max": False}
    def get_level_from_exp(exp):
        return 1


# ==================== GLOBAL BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_links: Dict[str, str] = {}
        # NEW:
        self.paused_accounts: set = set()        # UID strings
        self.restart_requests: set = set()       # UID strings
        self.account_logs: Dict[str, List[Dict]] = {}  # per-account live log

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def add_account_log(self, uid: str, message: str, level: str = "info"):
        """Per-account live activity log (last 5)."""
        uid_str = str(uid)
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "message": message,
            "level": level,
        }
        lst = self.account_logs.setdefault(uid_str, [])
        lst.insert(0, entry)
        if len(lst) > 5:
            del lst[5:]

    def register_account(self, uid: str, nickname: str, region: str,
                         level: int, exp: int, likes: int = 0,
                         owner: Optional[str] = None):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "display_uid": uid_str,
                "actual_uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "owner": owner,
                "added_at": time.time(),
                "session_start": time.time(),
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
            if owner:
                acc["owner"] = owner
            if "session_start" not in acc:
                acc["session_start"] = time.time()
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP!",
                         "success", uid_str)
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()


# ==================== TEMPLATE LOADER ====================
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def load_template(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"<h1>{name} not found</h1>"


# ==================== SESSION MANAGEMENT ====================
_sessions: Dict[str, Dict[str, Any]] = {}
_session_lock = asyncio.Lock()
SESSION_TTL = 86400


def _gen_session_id() -> str:
    return secrets.token_urlsafe(32)


async def create_session(username: str, is_admin: bool = False) -> str:
    async with _session_lock:
        sid = _gen_session_id()
        _sessions[sid] = {
            "username": username,
            "is_admin": is_admin,
            "created_at": time.time()
        }
        return sid


async def get_session(sid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not sid:
        return None
    async with _session_lock:
        s = _sessions.get(sid)
        if not s:
            return None
        if time.time() - s["created_at"] > SESSION_TTL:
            del _sessions[sid]
            return None
        return s


async def destroy_session(sid: Optional[str]):
    if not sid:
        return
    async with _session_lock:
        _sessions.pop(sid, None)


def _get_sid(request: web.Request) -> Optional[str]:
    return request.cookies.get("sid")


def _json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"status": "error", "error": msg}, status=status)


# ==================== POPUP CONFIG ====================
POPUP_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "popup_config.json")

DEFAULT_POPUP = {
    "enabled": True,
    "header": "Important Update",
    "title": "IND Server Maintenance",
    "message": "Due to game restrictions in IND server, our Level Up system can only farm 5K exp per day. So we decided to close IND server until our devs fix this. We will be back online for IND server soon.",
    "button_text": "Chat Now",
    "button_link": "https://t.me/",
    "icon_class": "fa-solid fa-circle-check",
    "icon_color": "#22c55e",
    "updated_at": 0
}


def _load_popup_config() -> Dict[str, Any]:
    if not os.path.exists(POPUP_CONFIG_FILE):
        return dict(DEFAULT_POPUP)
    try:
        with open(POPUP_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = dict(DEFAULT_POPUP)
        cfg.update(data or {})
        return cfg
    except Exception:
        return dict(DEFAULT_POPUP)


def _save_popup_config(cfg: Dict[str, Any]):
    try:
        tmp = POPUP_CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, POPUP_CONFIG_FILE)
    except Exception as e:
        print(f"[POPUP] Save error: {e}")


async def api_public_popup(request: web.Request) -> web.Response:
    cfg = _load_popup_config()
    return web.json_response({"status": "ok", "popup": cfg})


async def api_admin_get_popup(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    cfg = _load_popup_config()
    return web.json_response({"status": "ok", "popup": cfg})


async def api_admin_save_popup(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        cfg = _load_popup_config()

        for key in ("enabled", "header", "title", "message",
                    "button_text", "button_link", "icon_class", "icon_color"):
            if key in data:
                if key == "enabled":
                    cfg[key] = bool(data[key])
                else:
                    cfg[key] = str(data[key])

        cfg["updated_at"] = int(time.time())
        _save_popup_config(cfg)
        return web.json_response({"status": "ok", "popup": cfg})
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== PAGE ROUTES ====================

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=load_template("landing.html"),
                        content_type="text/html", charset="utf-8")


async def handle_login_page(request: web.Request) -> web.Response:
    return web.Response(text=load_template("login.html"),
                        content_type="text/html", charset="utf-8")


async def handle_admin_login_page(request: web.Request) -> web.Response:
    return web.Response(text=load_template("admin_login.html"),
                        content_type="text/html", charset="utf-8")


async def handle_admin_page(request: web.Request) -> web.Response:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or not sess.get("is_admin"):
        return web.HTTPFound("/admin-login")
    return web.Response(text=load_template("admin.html"),
                        content_type="text/html", charset="utf-8")


async def handle_dashboard_page(request: web.Request) -> web.Response:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or sess.get("is_admin"):
        return web.HTTPFound("/login")

    username = sess["username"]
    if await is_user_expired(username):
        await destroy_session(sid)
        return web.HTTPFound("/login?expired=1")

    return web.Response(text=load_template("dashboard.html"),
                        content_type="text/html", charset="utf-8")


# ==================== AUTH API ====================

async def api_user_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()

        # Allow password-only login (username optional — server finds by password)
        if not password:
            return _json_error("Password required")

        if not username:
            # Password-only mode: find user by password
            users = await list_all_users()
            matches = [u for u in users if u.get("password") == password]
            if len(matches) == 0:
                return _json_error("Invalid password", 401)
            if len(matches) > 1:
                return _json_error("Multiple users share this password — contact admin", 401)
            username = matches[0]["username"]

        result = await validate_user(username, password)
        if "error" in result:
            return _json_error(result["error"], 401)

        sid = await create_session(username, is_admin=False)
        resp = web.json_response({
            "status": "ok",
            "username": username,
            "account_limit": result["account_limit"],
            "is_vip": result.get("is_vip", False),
            "expires_at": result["expires_at"]
        })
        resp.set_cookie("sid", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL)
        return resp
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        password = str(data.get("password", "")).strip()

        if not password:
            return _json_error("Password required")

        admin_creds = get_admin_credentials()
        if password != admin_creds["password"]:
            return _json_error("Invalid admin password", 401)

        sid = await create_session(admin_creds["username"], is_admin=True)
        resp = web.json_response({"status": "ok"})
        resp.set_cookie("sid", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL)
        return resp
    except Exception as e:
        return _json_error(str(e), 500)


async def api_logout(request: web.Request) -> web.Response:
    sid = _get_sid(request)
    await destroy_session(sid)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie("sid")
    return resp


# ==================== ADMIN API ====================

async def _require_admin(request: web.Request) -> Optional[web.Response]:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or not sess.get("is_admin"):
        return _json_error("Unauthorized", 401)
    return None


async def api_admin_create_user(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        result = await create_user(
            username=data.get("username", ""),
            password=data.get("password", ""),
            account_limit=data.get("account_limit", 1),
            duration_value=data.get("duration_value", 1),
            duration_unit=data.get("duration_unit", "hours"),
            is_vip=bool(data.get("is_vip", False))
        )
        if "error" in result:
            return _json_error(result["error"])
        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_list_users(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        users = await list_all_users()
        return web.json_response({"status": "ok", "users": users})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_delete_user(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        result = await delete_user(username)
        if "error" in result:
            return _json_error(result["error"])

        for key, task in list(bot_state.account_workers.items()):
            if key.startswith(f"{username}::"):
                task.cancel()
                del bot_state.account_workers[key]

        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_extend_user(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        result = await extend_user_time(
            username=data.get("username", ""),
            duration_value=data.get("duration_value", 1),
            duration_unit=data.get("duration_unit", "hours")
        )
        if "error" in result:
            return _json_error(result["error"])
        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_set_vip(request: web.Request) -> web.Response:
    """Toggle VIP status for a user."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        is_vip = bool(data.get("is_vip", False))
        result = await set_user_vip(username, is_vip)
        if "error" in result:
            return _json_error(result["error"])
        return web.json_response(result)
    except Exception as e:
        return _json_error(str(e), 500)


async def api_admin_export_user(request: web.Request) -> web.Response:
    """
    Download user's all accounts as JSON.
    Enriches each account with nickname/level/exp from bot_state if available.
    """
    err = await _require_admin(request)
    if err:
        return err
    try:
        username = request.match_info.get("username", "").strip()
        if not username:
            return _json_error("username required")

        export = await get_user_accounts_export(username)
        if not export:
            return _json_error("User not found", 404)

        # Enrich with live bot_state data
        for acc in export.get("accounts", []):
            uid = str(acc.get("uid") or "")
            if not uid:
                continue
            live = bot_state.accounts.get(uid)
            if live:
                exp = int(live.get("current_exp") or 0)
                acc["nickname"] = live.get("nickname") or ""
                acc["level"] = live.get("level") or get_level_from_exp(exp)
                acc["current_exp"] = exp
                acc["gained_exp"] = live.get("gained_exp") or 0
                acc["matches_played"] = live.get("matches_played") or 0
                acc["region"] = live.get("region") or "BD"
                acc["status"] = live.get("status") or "OFFLINE"
            else:
                acc["nickname"] = ""
                acc["level"] = 1
                acc["current_exp"] = 0
                acc["gained_exp"] = 0
                acc["matches_played"] = 0
                acc["region"] = "BD"
                acc["status"] = "OFFLINE"

        filename = f"{username}_accounts.json"
        body = json.dumps(export, indent=2, ensure_ascii=False)

        return web.Response(
            body=body,
            content_type="application/json",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== USER DASHBOARD API ====================

async def _require_user(request: web.Request) -> Optional[web.Response]:
    sid = _get_sid(request)
    sess = await get_session(sid)
    if not sess or sess.get("is_admin"):
        return _json_error("Unauthorized", 401)
    return None


def _build_user_accounts(username: str, user: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = time.time()
    result_accounts = []

    for acc in user.get("accounts", []):
        user_uid = str(acc.get("uid") or acc.get("token", "")[:20])
        user_token_prefix = str(acc.get("token", ""))[:20] if acc.get("token") else ""
        merged = None

        for bot_uid, bot_acc in bot_state.accounts.items():
            if bot_acc.get("owner") != username:
                continue
            bot_uid_str = str(bot_uid)
            bot_actual = str(bot_acc.get("actual_uid", ""))
            bot_display = str(bot_acc.get("display_uid", ""))

            if (bot_uid_str == user_uid or
                    bot_actual == user_uid or
                    bot_display == user_uid or
                    (user_token_prefix and bot_uid_str == user_token_prefix)):
                merged = dict(bot_acc)
                real_uid = bot_actual or bot_uid_str
                merged["uid"] = real_uid
                merged["display_uid"] = real_uid
                merged["actual_uid"] = real_uid
                merged["user_input_uid"] = user_uid
                merged["added_at"] = acc.get("added_at", bot_acc.get("added_at", now))
                break

        if merged is None:
            merged = {
                "uid": user_uid,
                "display_uid": user_uid,
                "actual_uid": user_uid,
                "user_input_uid": user_uid,
                "nickname": f"Player_{user_uid[:6]}",
                "region": "BD",
                "level": 1,
                "initial_exp": 0,
                "current_exp": 0,
                "gained_exp": 0,
                "likes": 0,
                "status": "CONNECTING",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "added_at": acc.get("added_at", now),
                "session_start": acc.get("added_at", now),
                "owner": username
            }

        result_accounts.append(merged)

    return result_accounts


async def api_user_stats(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        user = await get_user(username)
        if not user:
            return _json_error("User not found", 404)

        now = time.time()
        remaining = max(0, user.get("expires_at", 0) - now)

        result_accounts = _build_user_accounts(username, user)

        # Override with freshest bot_state data
        now_ts = time.time()
        for acc in result_accounts:
            real_uid = str(acc.get("actual_uid") or acc.get("uid") or "")
            user_input_uid = str(acc.get("user_input_uid") or "")

            fresh_bot_data = None
            for check_key in [real_uid, user_input_uid, str(acc.get("uid", ""))]:
                if check_key and check_key in bot_state.accounts:
                    candidate = bot_state.accounts[check_key]
                    if candidate.get("owner") == username:
                        if fresh_bot_data is None:
                            fresh_bot_data = candidate
                        elif candidate.get("current_exp", 0) > fresh_bot_data.get("current_exp", 0):
                            fresh_bot_data = candidate

            if fresh_bot_data:
                acc["current_exp"] = fresh_bot_data.get("current_exp", acc.get("current_exp", 0))
                acc["gained_exp"] = fresh_bot_data.get("gained_exp", acc.get("gained_exp", 0))
                acc["level"] = fresh_bot_data.get("level", acc.get("level", 1))
                acc["nickname"] = fresh_bot_data.get("nickname", acc.get("nickname"))
                acc["region"] = fresh_bot_data.get("region", acc.get("region", "BD"))
                acc["status"] = fresh_bot_data.get("status", acc.get("status", "ONLINE"))
                acc["matches_played"] = fresh_bot_data.get("matches_played", acc.get("matches_played", 0))
                acc["active_matches"] = fresh_bot_data.get("active_matches", acc.get("active_matches", 0))
                acc["last_updated"] = fresh_bot_data.get("last_updated", acc.get("last_updated", "--"))
                acc["likes"] = fresh_bot_data.get("likes", acc.get("likes", 0))
                acc["session_start"] = fresh_bot_data.get("session_start", acc.get("session_start", now_ts))

            # Pause / restart flags
            acc["is_paused"] = str(real_uid) in bot_state.paused_accounts
            acc["is_restarting"] = str(real_uid) in bot_state.restart_requests

            # Uptime
            start = acc.get("session_start") or now_ts
            acc["uptime_seconds"] = int(max(0, now_ts - start))

            # Level progress
            prog = get_level_progress(acc.get("current_exp", 0))
            acc["progress"] = prog

            # Live log (last 5)
            acc["live_log"] = bot_state.account_logs.get(real_uid, [])[:5]

        total_gained = sum(a.get("gained_exp", 0) for a in result_accounts)
        total_matches = sum(a.get("matches_played", 0) for a in result_accounts)

        return web.json_response({
            "status": "ok",
            "username": username,
            "account_limit": user.get("account_limit", 1),
            "is_vip": bool(user.get("is_vip", False)),
            "accounts_used": len(user.get("accounts", [])),
            "remaining_seconds": int(remaining),
            "expires_at": user.get("expires_at", 0),
            "accounts": result_accounts,
            "total_gained_exp": total_gained,
            "total_matches": total_matches,
            "created_at": user.get("created_at", 0),
            "total_accounts_added": user.get("total_accounts_added", 0),
            "server_time": now_ts,
        })
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_add_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()

        account_payload = {}
        if data.get("token"):
            account_payload["token"] = str(data["token"]).strip()
        elif data.get("uid") and data.get("password"):
            account_payload["uid"] = str(data["uid"]).strip()
            account_payload["password"] = str(data["password"]).strip()
        else:
            return _json_error("Provide UID+Password or Token")

        result = await add_account_to_user(username, account_payload)
        if "error" in result:
            return _json_error(result["error"])

        cb = bot_state.refresh_callbacks.get("on_user_account_added")
        if cb:
            asyncio.create_task(cb(username, account_payload))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_upload_accounts(request: web.Request) -> web.Response:
    """
    VIP-only bulk upload.
    Accepts JSON body:
      { "accounts": [ {"uid": "...", "password": "..."}, ... ] }
    Or raw text via 'text' field: "uid:pass\nuid:pass"
    """
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        # VIP gate
        if not await is_user_vip(username):
            return _json_error("VIP feature only", 403)

        data = await request.json()
        raw_accounts = data.get("accounts")
        raw_text = data.get("text")

        entries: List[Dict[str, str]] = []

        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("uid") or "").strip()
                pwd = str(item.get("password") or "").strip()
                if uid and pwd:
                    entries.append({"uid": uid, "password": pwd})
        elif isinstance(raw_text, str) and raw_text.strip():
            for line in raw_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                uid, _, pwd = line.partition(":")
                uid = uid.strip()
                pwd = pwd.strip()
                if uid and pwd:
                    entries.append({"uid": uid, "password": pwd})

        if not entries:
            return _json_error("No valid uid:password entries found")

        # Get limit and check
        user = await get_user(username)
        if not user:
            return _json_error("User not found", 404)
        limit = int(user.get("account_limit", 1))

        result = await add_accounts_bulk(username, entries)
        if "error" in result:
            return _json_error(result["error"])

        # Launch workers for each added account (staggered)
        cb = bot_state.refresh_callbacks.get("on_user_account_added")
        if cb:
            added_keys = [e for e in entries][: result.get("added", 0)]
            for i, entry in enumerate(added_keys):
                asyncio.create_task(_delayed_launch(cb, username, entry, i * 0.8))

        return web.json_response({
            "status": "ok",
            "added": result.get("added", 0),
            "skipped": result.get("skipped", 0),
            "limit_reached": result.get("limit_reached", False),
            "message": result.get("message", ""),
            "limit": limit,
        })
    except Exception as e:
        return _json_error(str(e), 500)


async def _delayed_launch(cb, username, entry, delay):
    """Staggered launch to avoid overwhelming the login API."""
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        await cb(username, entry)
    except Exception as e:
        print(f"[UPLOAD] launch error: {e}")


async def api_user_remove_account(request: web.Request) -> web.Response:
    """
    Remove a single account safely.
    Cancels ONLY the matching worker — other accounts keep running.
    """
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        user = await get_user(username)
        if not user:
            return _json_error("User not found")

        # ===== Build ALL possible keys for this account =====
        all_keys = set()
        all_keys.add(acc_id)

        # Scan user_manager accounts
        for acc in user.get("accounts", []):
            uid_val = str(acc.get("uid") or "")
            token_val = str(acc.get("token") or "")
            token_prefix = token_val[:20] if token_val else ""

            if acc_id in (uid_val, token_prefix):
                all_keys.add(uid_val)
                if token_prefix:
                    all_keys.add(token_prefix)
                    all_keys.add(f"tok_{token_prefix}")

        # Scan bot_state.accounts for matching entries owned by this user
        for bot_uid, ba in list(bot_state.accounts.items()):
            if ba.get("owner") != username:
                continue
            bot_uid_str = str(bot_uid)
            actual = str(ba.get("actual_uid", ""))
            display = str(ba.get("display_uid", ""))
            uid_field = str(ba.get("uid", ""))
            user_input = str(ba.get("user_input_uid", ""))

            if acc_id in (bot_uid_str, actual, display, uid_field, user_input):
                all_keys.add(bot_uid_str)
                if actual:
                    all_keys.add(actual)
                if display:
                    all_keys.add(display)
                if uid_field:
                    all_keys.add(uid_field)
                if user_input:
                    all_keys.add(user_input)

        # ===== Remove from user_manager (try each key) =====
        removed = False
        for k in list(all_keys):
            if not k:
                continue
            result = await remove_account_from_user(username, k)
            if "status" in result:
                removed = True
                break

        if not removed:
            return _json_error("Account not found")

        # ===== Cancel ONLY the worker that matches one of our keys =====
        workers_to_cancel = []
        for worker_key in list(bot_state.account_workers.keys()):
            if not worker_key.startswith(f"{username}::"):
                continue
            parts = worker_key.split("::", 1)
            if len(parts) < 2:
                continue
            worker_id = parts[1]
            if worker_id in all_keys:
                workers_to_cancel.append(worker_key)

        for wk in workers_to_cancel:
            try:
                bot_state.account_workers[wk].cancel()
            except Exception:
                pass
            try:
                del bot_state.account_workers[wk]
            except Exception:
                pass

        # ===== Remove state ONLY for matched keys (not others) =====
        for k in all_keys:
            if not k:
                continue
            bot_state.accounts.pop(k, None)
            bot_state.account_credentials.pop(k, None)
            bot_state.account_credentials.pop(f"tok_{k}", None)
            bot_state.paused_accounts.discard(k)
            bot_state.restart_requests.discard(k)
            bot_state.account_logs.pop(k, None)

        bot_state.recalc_totals()

        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_refresh(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        candidates = {acc_id}

        user = await get_user(username)
        if user:
            for acc in user.get("accounts", []):
                uid_val = str(acc.get("uid") or "")
                token_val = str(acc.get("token") or "")
                if uid_val:
                    candidates.add(uid_val)
                if token_val:
                    candidates.add(token_val[:20])
                    candidates.add(f"tok_{token_val[:20]}")

        for bot_uid, ba in bot_state.accounts.items():
            if ba.get("owner") != username:
                continue
            bot_uid_str = str(bot_uid)
            match = (bot_uid_str == acc_id or
                     str(ba.get("actual_uid", "")) == acc_id or
                     str(ba.get("display_uid", "")) == acc_id or
                     str(ba.get("uid", "")) == acc_id)
            if match:
                candidates.add(bot_uid_str)
                if ba.get("actual_uid"):
                    candidates.add(str(ba["actual_uid"]))
                if ba.get("display_uid"):
                    candidates.add(str(ba["display_uid"]))
                if ba.get("uid"):
                    candidates.add(str(ba["uid"]))

        for c in list(candidates):
            if c and not c.startswith("tok_") and len(c) >= 15:
                candidates.add(f"tok_{c}")

        cb = bot_state.refresh_callbacks.get("on_refresh_account")
        if cb:
            for c in candidates:
                if c:
                    try:
                        asyncio.create_task(cb(c))
                    except Exception:
                        pass

        await asyncio.sleep(1.5)

        updated_account = None
        if user:
            all_accounts = _build_user_accounts(username, user)
            for a in all_accounts:
                if (str(a.get("uid", "")) == acc_id or
                        str(a.get("actual_uid", "")) == acc_id or
                        str(a.get("display_uid", "")) == acc_id or
                        str(a.get("user_input_uid", "")) == acc_id):
                    updated_account = a
                    break

        if not updated_account:
            for bot_uid, ba in bot_state.accounts.items():
                if ba.get("owner") != username:
                    continue
                if (str(bot_uid) == acc_id or
                        str(ba.get("actual_uid", "")) == acc_id or
                        str(ba.get("display_uid", "")) == acc_id):
                    updated_account = dict(ba)
                    updated_account["uid"] = str(ba.get("actual_uid", bot_uid))
                    updated_account["display_uid"] = updated_account["uid"]
                    updated_account["user_input_uid"] = acc_id
                    break

        # Enrich with progress/uptime
        if updated_account:
            real_uid = str(updated_account.get("actual_uid") or updated_account.get("uid") or "")
            updated_account["is_paused"] = real_uid in bot_state.paused_accounts
            updated_account["is_restarting"] = real_uid in bot_state.restart_requests
            start = updated_account.get("session_start") or time.time()
            updated_account["uptime_seconds"] = int(max(0, time.time() - start))
            updated_account["progress"] = get_level_progress(updated_account.get("current_exp", 0))
            updated_account["live_log"] = bot_state.account_logs.get(real_uid, [])[:5]

        return web.json_response({
            "status": "ok",
            "account": updated_account
        })
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== PAUSE / RESUME / RESTART ====================

async def api_user_pause_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        # Verify ownership
        user = await get_user(username)
        if not user:
            return _json_error("User not found", 404)

        owned = False
        for acc in user.get("accounts", []):
            if str(acc.get("uid") or acc.get("token", "")[:20]) == acc_id:
                owned = True
                break
        if not owned and acc_id not in bot_state.accounts:
            return _json_error("Account not found", 404)

        bot_state.paused_accounts.add(acc_id)
        # Also add actual_uid variant if exists
        live = bot_state.accounts.get(acc_id)
        if live:
            real = str(live.get("actual_uid") or "")
            if real:
                bot_state.paused_accounts.add(real)
            bot_state.update_status(acc_id, "PAUSED")
            if real:
                bot_state.update_status(real, "PAUSED")
            bot_state.add_account_log(acc_id, "Paused by user", "warning")

        return web.json_response({"status": "ok", "paused": True})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_resume_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        bot_state.paused_accounts.discard(acc_id)
        live = bot_state.accounts.get(acc_id)
        if live:
            real = str(live.get("actual_uid") or "")
            if real:
                bot_state.paused_accounts.discard(real)
            bot_state.update_status(acc_id, "SEARCHING")
            if real:
                bot_state.update_status(real, "SEARCHING")
            bot_state.add_account_log(acc_id, "Resumed by user", "success")

        return web.json_response({"status": "ok", "paused": False})
    except Exception as e:
        return _json_error(str(e), 500)


async def api_user_restart_account(request: web.Request) -> web.Response:
    err = await _require_user(request)
    if err:
        return err
    try:
        sid = _get_sid(request)
        sess = await get_session(sid)
        username = sess["username"]

        data = await request.json()
        acc_id = str(data.get("account_id", "")).strip()
        if not acc_id:
            return _json_error("account_id required")

        # Also add actual_uid variant
        bot_state.restart_requests.add(acc_id)
        live = bot_state.accounts.get(acc_id)
        if live:
            real = str(live.get("actual_uid") or "")
            if real:
                bot_state.restart_requests.add(real)
            bot_state.add_account_log(acc_id, "Restart requested by user", "warning")
            bot_state.update_status(acc_id, "RESTARTING")

        return web.json_response({"status": "ok", "restarting": True})
    except Exception as e:
        return _json_error(str(e), 500)


# ==================== SERVER START ====================

async def start_web_dashboard(host: str = "0.0.0.0", port: int = 20335):
    app = web.Application(client_max_size=10 * 1024 * 1024)  # 10MB max upload

    app.router.add_get("/", handle_root)
    app.router.add_get("/login", handle_login_page)
    app.router.add_get("/admin-login", handle_admin_login_page)
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_get("/dashboard", handle_dashboard_page)

    app.router.add_post("/api/login", api_user_login)
    app.router.add_post("/api/admin/login", api_admin_login)
    app.router.add_post("/api/logout", api_logout)

    # Admin
    app.router.add_post("/api/admin/create-user", api_admin_create_user)
    app.router.add_get("/api/admin/users", api_admin_list_users)
    app.router.add_post("/api/admin/delete-user", api_admin_delete_user)
    app.router.add_post("/api/admin/extend-user", api_admin_extend_user)
    app.router.add_post("/api/admin/set-vip", api_admin_set_vip)
    app.router.add_get("/api/admin/export-user/{username}", api_admin_export_user)

    # User
    app.router.add_get("/api/user/stats", api_user_stats)
    app.router.add_post("/api/user/add-account", api_user_add_account)
    app.router.add_post("/api/user/upload-accounts", api_user_upload_accounts)
    app.router.add_post("/api/user/remove-account", api_user_remove_account)
    app.router.add_post("/api/user/refresh", api_user_refresh)
    app.router.add_post("/api/user/pause-account", api_user_pause_account)
    app.router.add_post("/api/user/resume-account", api_user_resume_account)
    app.router.add_post("/api/user/restart-account", api_user_restart_account)

    # Popup
    app.router.add_get("/api/public/popup", api_public_popup)
    app.router.add_get("/api/admin/get-popup", api_admin_get_popup)
    app.router.add_post("/api/admin/save-popup", api_admin_save_popup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")