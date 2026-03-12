import os
import hashlib
from typing import Optional, Dict

import psycopg2
from psycopg2.extras import RealDictCursor


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _get_db_conn():
    db_url = os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL or UPWORK_DATABASE_URL must be set for auth.")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def init_auth_db() -> None:
    """
    Ensure the users table exists.
    """
    conn = _get_db_conn()
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
    conn.close()


def create_user(email: str, password: str) -> Optional[str]:
    """
    Create a user with email + password.
    Returns user_id on success, None if email already exists or invalid.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    is_admin = email == "muhammad.haseeb@integriti.io"
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
    """
    Return user dict if email/password is valid, else None.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    pw_hash = _hash_password(password)
    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, is_admin
                    FROM users
                    WHERE email = %s;
                    """,
                    (email,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                if row["password_hash"] != pw_hash:
                    return None
                # Backfill admin flag if needed
                if email == "muhammad.haseeb@integriti.io" and not row.get("is_admin"):
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
    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, is_admin
                    FROM users
                    WHERE id = %s;
                    """,
                    (int(user_id),),
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
    """
    Return mapping of user_id -> user dict.
    """
    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, is_admin
                    FROM users;
                    """
                )
                rows = cur.fetchall() or []
                out: Dict[str, dict] = {}
                for row in rows:
                    uid = str(row["id"])
                    out[uid] = {
                        "id": uid,
                        "email": row["email"],
                        "password_hash": row["password_hash"],
                        "is_admin": bool(row["is_admin"]),
                    }
                return out
    finally:
        conn.close()

