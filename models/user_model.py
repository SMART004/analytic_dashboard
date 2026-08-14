# models/user_model.py
from __future__ import annotations
import sqlite3
from typing import Optional, Dict, Any
from app_config.settings import DEFAULT_DB_FILENAME

def init_user_table(conn: Optional[sqlite3.Connection] = None) -> None:
    """Crée la table users si elle n'existe pas."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DEFAULT_DB_FILENAME)
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
        conn.commit()
    finally:
        if close_conn:
            conn.close()

def get_user_by_username(username: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """Récupère un utilisateur par son username."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DEFAULT_DB_FILENAME)
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