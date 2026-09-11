"""Stockage de fichiers dans la base libSQL/Turso."""
from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Any

import pandas as pd
import streamlit as st

from models.db import get_connection


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stored_files (
            bucket TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            content BLOB NOT NULL,
            content_type TEXT,
            content_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bucket, file_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stored_files_bucket ON stored_files(bucket)"
    )
    conn.commit()


def _read_dataframe(content: bytes, file_name: str) -> pd.DataFrame:
    buffer = BytesIO(content)
    buffer.name = file_name
    if file_name.lower().endswith(".csv"):
        return pd.read_csv(buffer)
    return pd.read_excel(buffer)


def save_bytes(bucket: str, file_path: str, content: bytes, file_name: str | None = None, content_type: str | None = None) -> None:
    file_name = file_name or file_path.rsplit("/", 1)[-1]
    digest = hashlib.md5(content).hexdigest()
    with get_connection() as conn:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO stored_files
                (bucket, file_path, file_name, content, content_type, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket, file_path) DO UPDATE SET
                file_name=excluded.file_name,
                content=excluded.content,
                content_type=excluded.content_type,
                content_hash=excluded.content_hash,
                created_at=CURRENT_TIMESTAMP
            """,
            (bucket, file_path, file_name, content, content_type, digest),
        )


def load_bytes(bucket: str, file_path: str) -> bytes | None:
    with get_connection() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT content FROM stored_files WHERE bucket = ? AND file_path = ?",
            (bucket, file_path),
        ).fetchone()
    return bytes(row[0]) if row else None


def list_objects(bucket: str, prefix: str = "") -> list[dict[str, Any]]:
    with get_connection() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT file_path, file_name, content_hash, created_at
            FROM stored_files
            WHERE bucket = ? AND file_path LIKE ?
            ORDER BY file_path
            """,
            (bucket, f"{prefix}%"),
        ).fetchall()
    return [
        {"name": row[0], "file_name": row[1], "content_hash": row[2], "created_at": row[3]}
        for row in rows
    ]


def delete_objects(bucket: str, prefix: str = "") -> int:
    with get_connection() as conn:
        _ensure_table(conn)
        cursor = conn.execute(
            "DELETE FROM stored_files WHERE bucket = ? AND file_path LIKE ?",
            (bucket, f"{prefix}%"),
        )
        return cursor.rowcount


def save_file(file, folder_name: str) -> str:
    """Remplace les fichiers d'un dossier de configuration par le nouveau fichier."""
    delete_objects("settings", f"{folder_name}/")
    path = f"{folder_name}/{file.name}"
    save_bytes("settings", path, file.getvalue(), file.name, getattr(file, "type", None))
    return path


def load_setting(folder_name: str) -> pd.DataFrame | None:
    objects = list_objects("settings", f"{folder_name}/")
    if not objects:
        return None
    content = load_bytes("settings", objects[0]["name"])
    return _read_dataframe(content, objects[0]["file_name"]) if content is not None else None


@st.cache_data(show_spinner=False)
def get_file_from_turso(file_path: str) -> pd.DataFrame | None:
    content = load_bytes("settings", file_path)
    if content is None:
        return None
    return _read_dataframe(content, file_path)


def handle_upload(tab_title, uploader_label, uploader_key, session_key, folder_name):
    uploaded_file = st.file_uploader(
        uploader_label, key=uploader_key, type=["xlsx", "xls", "csv"]
    )
    uploaded = False
    if uploaded_file:
        signature = hashlib.md5(uploaded_file.getvalue()).hexdigest()
        state_key = f"{uploader_key}_last_uploaded_sig"
        if st.session_state.get(state_key) != signature:
            save_file(uploaded_file, folder_name)
            st.session_state[state_key] = signature
            uploaded = True
        st.session_state[session_key] = load_setting(folder_name)
        st.success("Fichier chargé")
    elif session_key not in st.session_state:
        loaded = load_setting(folder_name)
        if loaded is not None:
            st.session_state[session_key] = loaded
    return uploaded


def dataframe_from_bytes(content: bytes, file_name: str) -> pd.DataFrame:
    return _read_dataframe(content, file_name)
