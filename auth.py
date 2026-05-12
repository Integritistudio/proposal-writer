import os
import json
import hashlib
import uuid
from pathlib import Path
from typing import Optional, Dict

# Local file used when no DATABASE_URL is configured (dev / single-machine use).
_LOCAL_USERS_FILE = Path(__file__).resolve().parent / "learning" / "users.json"

ADMIN_EMAIL = "muhammad.haseeb@integriti.io"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL"))


def _get_db_conn():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    db_url = os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


# ── Local JSON helpers ────────────────────────────────────────────────────────

def _load_local_users() -> dict:
    if not _LOCAL_USERS_FILE.exists():
        return {}
    try:
        with open(_LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_local_users(users: dict) -> None:
    _LOCAL_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def init_auth_db() -> None:
    if not _use_db():
        _LOCAL_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        return
    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        is_admin BOOLEAN NOT NULL DEFAULT FALSE
                    );
                    """
                )
    finally:
        conn.close()


def create_user(email: str, password: str) -> Optional[str]:
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    is_admin = email == ADMIN_EMAIL

    if not _use_db():
        users = _load_local_users()
        # Check duplicate
        for u in users.values():
            if u["email"] == email:
                return None
        uid = str(uuid.uuid4())
        users[uid] = {
            "id": uid,
            "email": email,
            "password_hash": _hash_password(password),
            "is_admin": is_admin,
        }
        _save_local_users(users)
        return uid

    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password_hash, is_admin)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (email) DO NOTHING
                    RETURNING id;
                    """,
                    (email, _hash_password(password), is_admin),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return str(row["id"])
    finally:
        conn.close()


def authenticate(email: str, password: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    pw_hash = _hash_password(password)

    if not _use_db():
        users = _load_local_users()
        for u in users.values():
            if u["email"] == email and u["password_hash"] == pw_hash:
                u["is_admin"] = u.get("is_admin", False) or email == ADMIN_EMAIL
                return dict(u)
        return None

    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email, password_hash, is_admin FROM users WHERE email = %s;",
                    (email,),
                )
                row = cur.fetchone()
                if not row or row["password_hash"] != pw_hash:
                    return None
                if email == ADMIN_EMAIL and not row.get("is_admin"):
                    cur.execute("UPDATE users SET is_admin = TRUE WHERE id = %s;", (row["id"],))
                    row["is_admin"] = True
                return {
                    "id": str(row["id"]),
                    "email": row["email"],
                    "password_hash": row["password_hash"],
                    "is_admin": bool(row["is_admin"]),
                }
    finally:
        conn.close()


def get_user(user_id: str) -> Optional[dict]:
    if not user_id:
        return None

    if not _use_db():
        users = _load_local_users()
        u = users.get(str(user_id))
        return dict(u) if u else None

    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email, password_hash, is_admin FROM users WHERE id::text = %s;",
                    (str(user_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": str(row["id"]),
                    "email": row["email"],
                    "password_hash": row["password_hash"],
                    "is_admin": bool(row["is_admin"]),
                }
    finally:
        conn.close()


def get_all_users() -> Dict[str, dict]:
    if not _use_db():
        users = _load_local_users()
        return {uid: dict(u) for uid, u in users.items()}

    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, email, password_hash, is_admin FROM users;")
                rows = cur.fetchall() or []
                return {
                    str(row["id"]): {
                        "id": str(row["id"]),
                        "email": row["email"],
                        "password_hash": row["password_hash"],
                        "is_admin": bool(row["is_admin"]),
                    }
                    for row in rows
                }
    finally:
        conn.close()
