# models/user_model.py
from __future__ import annotations
import sqlite3
import hashlib
import secrets
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from models.db import get_connection
from utils.config_storage import LOCAL_STORAGE_PATH

LEGACY_DB_PATH = Path("dashboard.db")


def _migrate_legacy_users(conn) -> None:
    """Copie les anciens comptes racine dans la base centralisée une seule fois."""
    target_path = (LOCAL_STORAGE_PATH / "dashboard.db").resolve()
    legacy_path = LEGACY_DB_PATH.resolve()
    if target_path == legacy_path or not legacy_path.exists():
        return

    legacy_conn = sqlite3.connect(str(legacy_path))
    try:
        legacy_rows = legacy_conn.execute(
            "SELECT username, password_hash, role, created_at FROM users"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    finally:
        legacy_conn.close()

    for username, password_hash, role, created_at in legacy_rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, password_hash, role, created_at),
        )

def init_user_table(conn: Optional[sqlite3.Connection] = None) -> None:
    """Crée la table users si elle n'existe pas."""
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _migrate_legacy_users(conn)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        if close_conn:
            conn.close()

def get_user_by_username(username: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """Récupère un utilisateur par son username."""
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "password_hash": row[2], "role": row[3]}
        return None
    finally:
        if close_conn:
            conn.close()


def list_users(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Retourne les utilisateurs sans exposer leur mot de passe."""
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY username COLLATE NOCASE"
        )
        return [
            {"id": row[0], "username": row[1], "role": row[2], "created_at": row[3]}
            for row in cursor.fetchall()
        ]
    finally:
        if close_conn:
            conn.close()


def create_user(username: str, password_hash: str, role: str) -> None:
    """Crée un utilisateur et laisse SQLite signaler un nom déjà utilisé."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, password_hash, role),
        )


def update_user_role(user_id: int, role: str) -> None:
    """Modifie le rôle d'un utilisateur existant."""
    with get_connection() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def update_user_password(user_id: int, password_hash: str) -> None:
    """Remplace le mot de passe haché d'un utilisateur."""
    with get_connection() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def delete_user(user_id: int) -> None:
    """Supprime un utilisateur par son identifiant."""
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def create_auth_session(username: str, days: int = 7) -> str:
    """Crée un jeton opaque dont seule l'empreinte est stockée en base."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
            (token_hash, username, expires_at),
        )
    return token


def get_user_by_session_token(token: str) -> Optional[Dict[str, Any]]:
    """Retourne le compte lié à un jeton encore valide."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.role
            FROM auth_sessions s
            JOIN users u ON u.username = s.username
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "role": row[2]}


def delete_auth_session(token: str) -> None:
    """Révoque un jeton de session."""
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))