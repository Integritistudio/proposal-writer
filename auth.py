import json
import hashlib
from pathlib import Path
from typing import Optional, Dict

from config import BASE_DIR


USERS_PATH = BASE_DIR / "learning" / "users.json"


def _ensure_users_file():
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not USERS_PATH.exists():
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _load_users() -> Dict[str, dict]:
    if not USERS_PATH.exists():
        return {}
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users: Dict[str, dict]) -> None:
    _ensure_users_file()
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user(email: str, password: str) -> Optional[str]:
    """
    Create a user with email + password.
    Returns user_id on success, None if email already exists or invalid.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    users = _load_users()
    for uid, data in users.items():
        if data.get("email") == email:
            return None
    user_id = f"user_{len(users) + 1}"
    is_admin = email == "muhammad.haseeb@integriti.io"
    users[user_id] = {
        "id": user_id,
        "email": email,
        "password_hash": _hash_password(password),
        "is_admin": is_admin,
    }
    _save_users(users)
    return user_id


def authenticate(email: str, password: str) -> Optional[dict]:
    """
    Return user dict if email/password is valid, else None.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    users = _load_users()
    pw_hash = _hash_password(password)
    for user in users.values():
        if user.get("email") == email and user.get("password_hash") == pw_hash:
            # Backfill admin flag if missing
            if email == "muhammad.haseeb@integriti.io" and not user.get("is_admin"):
                user["is_admin"] = True
                _save_users(users)
            return user
    return None


def get_user(user_id: str) -> Optional[dict]:
    users = _load_users()
    return users.get(user_id)

