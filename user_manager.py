# -*- coding: utf-8 -*-
"""
User Manager — Admin-created users with account limits, expiry, VIP & bulk support
Stores users in users.json with per-user accounts isolation
"""

import json
import os
import time
import secrets
import string
import asyncio
from typing import Dict, List, Optional, Any

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
_users_lock = asyncio.Lock()

MAX_ACCOUNT_LIMIT = 1000  # hard cap


# ==================== PASSWORD GENERATOR ====================
def generate_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ==================== STORAGE ====================
def _load_users() -> Dict[str, Any]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _save_users(data: Dict[str, Any]):
    try:
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        print(f"[USER-MANAGER] Save error: {e}")


# ==================== USER OPERATIONS ====================

async def create_user(username: str, password: str, account_limit: int,
                      duration_value: int, duration_unit: str,
                      is_vip: bool = False) -> Dict[str, Any]:
    """
    Create a new user with account limit, expiry and VIP flag.
    duration_unit: "minutes" | "hours" | "days"
    """
    async with _users_lock:
        username = str(username).strip()
        password = str(password).strip()

        if not username or not password:
            return {"error": "Username and password required"}

        if len(username) < 3:
            return {"error": "Username must be at least 3 characters"}

        if len(password) < 4:
            return {"error": "Password must be at least 4 characters"}

        try:
            account_limit = int(account_limit)
            if account_limit < 1:
                return {"error": "Account limit must be at least 1"}
            if account_limit > MAX_ACCOUNT_LIMIT:
                return {"error": f"Account limit cannot exceed {MAX_ACCOUNT_LIMIT}"}
        except Exception:
            return {"error": "Invalid account limit"}

        try:
            duration_value = int(duration_value)
            if duration_value < 1:
                return {"error": "Duration must be at least 1"}
        except Exception:
            return {"error": "Invalid duration"}

        unit = str(duration_unit).lower().strip()
        if unit not in ("minutes", "hours", "days"):
            return {"error": "Invalid duration unit"}

        multiplier = {"minutes": 60, "hours": 3600, "days": 86400}[unit]
        duration_seconds = duration_value * multiplier

        users = _load_users()

        if username in users:
            return {"error": "Username already exists"}

        now = time.time()
        expiry = now + duration_seconds

        users[username] = {
            "username": username,
            "password": password,
            "account_limit": account_limit,
            "is_vip": bool(is_vip),
            "duration_seconds": duration_seconds,
            "duration_value": duration_value,
            "duration_unit": unit,
            "created_at": now,
            "expires_at": expiry,
            "accounts": [],
            "total_accounts_added": 0,
            "last_login": None,
            "is_active": True
        }
        _save_users(users)

        return {
            "status": "ok",
            "username": username,
            "password": password,
            "account_limit": account_limit,
            "is_vip": bool(is_vip),
            "expires_at": expiry,
            "duration_seconds": duration_seconds
        }


async def validate_user(username: str, password: str) -> Dict[str, Any]:
    """Validate login. Returns user dict if valid, else {'error': '...'}."""
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()
        password = str(password).strip()

        if username not in users:
            return {"error": "Invalid username or password"}

        user = users[username]

        if user.get("password") != password:
            return {"error": "Invalid username or password"}

        now = time.time()
        if now >= user.get("expires_at", 0):
            return {"error": "Account expired. Contact admin."}

        user["last_login"] = now
        _save_users(users)

        return {
            "status": "ok",
            "username": username,
            "account_limit": user.get("account_limit", 1),
            "is_vip": bool(user.get("is_vip", False)),
            "expires_at": user.get("expires_at", 0),
            "accounts": user.get("accounts", []),
            "total_accounts_added": user.get("total_accounts_added", 0)
        }


async def get_user(username: str) -> Optional[Dict[str, Any]]:
    async with _users_lock:
        users = _load_users()
        return users.get(str(username).strip())


async def is_user_expired(username: str) -> bool:
    user = await get_user(username)
    if not user:
        return True
    return time.time() >= user.get("expires_at", 0)


async def is_user_vip(username: str) -> bool:
    user = await get_user(username)
    if not user:
        return False
    return bool(user.get("is_vip", False))


# ==================== ACCOUNT OPERATIONS ====================

def _account_key(account_data: Dict[str, Any]) -> str:
    """Unique key for an account (uid or token prefix)."""
    if account_data.get("uid"):
        return str(account_data["uid"]).strip()
    if account_data.get("token"):
        return str(account_data["token"])[:20]
    return ""


async def add_account_to_user(username: str, account_data: Dict[str, Any]) -> Dict[str, Any]:
    """Add a single account to user's list. Checks account_limit and expiry."""
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()

        if username not in users:
            return {"error": "User not found"}

        user = users[username]

        if time.time() >= user.get("expires_at", 0):
            return {"error": "Account expired"}

        accounts = user.get("accounts", [])
        limit = user.get("account_limit", 1)

        if len(accounts) >= limit:
            return {"error": f"Account limit reached ({limit}). Remove one first."}

        new_key = _account_key(account_data)
        if not new_key:
            return {"error": "Invalid account data"}

        for acc in accounts:
            if _account_key(acc) == new_key:
                return {"error": "Account already added"}

        account_data["added_at"] = time.time()
        accounts.append(account_data)
        user["accounts"] = accounts
        user["total_accounts_added"] = user.get("total_accounts_added", 0) + 1
        _save_users(users)

        return {"status": "ok", "accounts": accounts}


async def add_accounts_bulk(username: str, accounts_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Add multiple accounts at once. Respects account_limit.
    Returns: {
        "status": "ok",
        "added": N,
        "skipped": M,
        "skipped_reasons": [...],
        "limit_reached": bool,
        "message": "..."
    }
    """
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()

        if username not in users:
            return {"error": "User not found"}

        user = users[username]

        if time.time() >= user.get("expires_at", 0):
            return {"error": "Account expired"}

        accounts = user.get("accounts", [])
        limit = user.get("account_limit", 1)

        existing_keys = {_account_key(a) for a in accounts if _account_key(a)}

        added = 0
        skipped = 0
        skipped_reasons = []
        limit_reached = False

        for entry in accounts_list:
            if len(accounts) >= limit:
                limit_reached = True
                remaining = len(accounts_list) - added - skipped
                skipped += remaining
                skipped_reasons.append(f"Limit {limit} reached — {remaining} skipped")
                break

            key = _account_key(entry)
            if not key:
                skipped += 1
                skipped_reasons.append(f"Invalid entry: {entry}")
                continue

            if key in existing_keys:
                skipped += 1
                skipped_reasons.append(f"Duplicate: {key}")
                continue

            entry["added_at"] = time.time()
            accounts.append(entry)
            existing_keys.add(key)
            added += 1

        user["accounts"] = accounts
        user["total_accounts_added"] = user.get("total_accounts_added", 0) + added
        _save_users(users)

        return {
            "status": "ok",
            "added": added,
            "skipped": skipped,
            "skipped_reasons": skipped_reasons[:20],  # cap for readability
            "limit_reached": limit_reached,
            "current_total": len(accounts),
            "limit": limit,
            "message": f"Added {added} account(s)" + (
                f", skipped {skipped} (limit reached)" if limit_reached else ""
            )
        }


async def remove_account_from_user(username: str, account_identifier: str) -> Dict[str, Any]:
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()

        if username not in users:
            return {"error": "User not found"}

        user = users[username]
        accounts = user.get("accounts", [])

        new_accounts = []
        removed = False
        for acc in accounts:
            if _account_key(acc) == str(account_identifier):
                removed = True
                continue
            new_accounts.append(acc)

        if not removed:
            return {"error": "Account not found"}

        user["accounts"] = new_accounts
        _save_users(users)

        return {"status": "ok", "accounts": new_accounts}


async def list_all_users() -> List[Dict[str, Any]]:
    async with _users_lock:
        users = _load_users()
        now = time.time()
        result = []
        for uname, u in users.items():
            remaining = max(0, u.get("expires_at", 0) - now)
            result.append({
                "username": uname,
                "password": u.get("password", ""),
                "account_limit": u.get("account_limit", 1),
                "is_vip": bool(u.get("is_vip", False)),
                "accounts_count": len(u.get("accounts", [])),
                "total_accounts_added": u.get("total_accounts_added", 0),
                "created_at": u.get("created_at", 0),
                "expires_at": u.get("expires_at", 0),
                "remaining_seconds": int(remaining),
                "is_active": remaining > 0,
                "last_login": u.get("last_login"),
                "duration_value": u.get("duration_value", 0),
                "duration_unit": u.get("duration_unit", "hours")
            })
        result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return result


async def delete_user(username: str) -> Dict[str, Any]:
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()
        if username not in users:
            return {"error": "User not found"}
        del users[username]
        _save_users(users)
        return {"status": "ok"}


async def extend_user_time(username: str, duration_value: int, duration_unit: str) -> Dict[str, Any]:
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()
        if username not in users:
            return {"error": "User not found"}

        try:
            duration_value = int(duration_value)
            if duration_value < 1:
                return {"error": "Duration must be at least 1"}
        except Exception:
            return {"error": "Invalid duration"}

        unit = str(duration_unit).lower().strip()
        if unit not in ("minutes", "hours", "days"):
            return {"error": "Invalid duration unit"}

        multiplier = {"minutes": 60, "hours": 3600, "days": 86400}[unit]
        extra = duration_value * multiplier

        user = users[username]
        current_expiry = user.get("expires_at", time.time())
        base = max(current_expiry, time.time())
        user["expires_at"] = base + extra
        user["duration_value"] = duration_value
        user["duration_unit"] = unit
        user["duration_seconds"] = extra
        _save_users(users)

        return {"status": "ok", "expires_at": user["expires_at"]}


async def set_user_vip(username: str, is_vip: bool) -> Dict[str, Any]:
    async with _users_lock:
        users = _load_users()
        username = str(username).strip()
        if username not in users:
            return {"error": "User not found"}
        users[username]["is_vip"] = bool(is_vip)
        _save_users(users)
        return {"status": "ok", "is_vip": bool(is_vip)}


async def get_user_accounts_export(username: str) -> Optional[Dict[str, Any]]:
    """
    Returns a JSON-serializable dict with user's all accounts and their metadata.
    Used by admin download endpoint.
    """
    user = await get_user(username)
    if not user:
        return None

    accounts_out = []
    for acc in user.get("accounts", []):
        accounts_out.append({
            "uid": acc.get("uid") or acc.get("token", "")[:20],
            "password": acc.get("password", ""),
            "token": acc.get("token", ""),
            "added_at": acc.get("added_at", 0),
        })

    return {
        "username": username,
        "is_vip": bool(user.get("is_vip", False)),
        "account_limit": user.get("account_limit", 1),
        "total_accounts": len(accounts_out),
        "total_accounts_added": user.get("total_accounts_added", 0),
        "expires_at": user.get("expires_at", 0),
        "created_at": user.get("created_at", 0),
        "exported_at": time.time(),
        "accounts": accounts_out,
    }


def get_admin_credentials() -> Dict[str, str]:
    return {
        "username": "14444446555",
        "password": "8325703815"
    }