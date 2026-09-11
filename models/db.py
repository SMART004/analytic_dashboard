# models/db.py
import sqlite3
import os
import logging
from pathlib import Path
from typing import Optional
from utils.config_storage import LOCAL_STORAGE_PATH, USE_LOCAL_STORAGE

logger = logging.getLogger(__name__)

try:
    import libsql  # type: ignore[import-not-found]
except ImportError:
    libsql = None

try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:
    _HAS_STREAMLIT = False

DEFAULT_DB_PATH = LOCAL_STORAGE_PATH / "dashboard.db"


class _LibsqlConnection:
    """Adapteur DB-API qui synchronise chaque transaction vers libSQL."""

    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)

    def __setattr__(self, name, value):
        if name == "_connection":
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        self._connection.commit()
        self._connection.sync()

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.commit()
        else:
            self._connection.rollback()
        self.close()
        return False

    def close(self):
        try:
            self._connection.commit()
            self._connection.sync()
        finally:
            self._connection.close()


def _libsql_settings() -> tuple[str | None, str | None]:
    if USE_LOCAL_STORAGE:
        return None, None
    url = os.getenv("LIBSQL_URL") or os.getenv("TURSO_DATABASE_URL")
    token = os.getenv("LIBSQL_AUTH_TOKEN") or os.getenv("TURSO_AUTH_TOKEN")
    if not url or not token:
        raise RuntimeError(
            "USE_LOCAL_STORAGE=false exige TURSO_DATABASE_URL et TURSO_AUTH_TOKEN."
        )
    return url, token

# Chemins de base déjà synchronisés avec le schéma courant dans ce process.
# Toutes les instructions de models/schema.sql sont en CREATE TABLE/INDEX
# IF NOT EXISTS (jamais de DROP/ALTER destructif), donc ré-exécuter le
# script est sans risque — on limite juste l'exécution à une fois par
# chemin de base pour éviter de la refaire à chaque connexion.
_schema_synced_paths: set[str] = set()


def get_db_path() -> Path:
    db_path = DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """
    Retourne une connexion SQLite configurée (WAL, foreign_keys off pendant
    l'ingestion, busy_timeout). Point d'entrée unique pour toute connexion DB.

    Applique aussi le schéma courant (models/schema.sql) de façon idempotente
    si ce n'est pas déjà fait dans ce process. Corrige le cas d'une base
    existante créée avant l'ajout de nouvelles tables (ex: point_relay_
    referentiel, cds_referentiel, hvc_cds_assignments — Tâche 5.2) : sans ce
    correctif, une base ancienne ne voit jamais ces tables tant qu'une
    ingestion complète n'est pas relancée manuellement.
    """
    if db_path is None:
        db_path = get_db_path()

    libsql_url, libsql_token = _libsql_settings()
    if libsql_url:
        if libsql is None:
            raise RuntimeError(
                "LIBSQL_URL est configuré mais libsql n'est pas installé. "
                "Installez les dépendances de requirements.txt."
            )
        conn = _LibsqlConnection(
            libsql.connect(
                str(db_path),
                sync_url=libsql_url,
                auth_token=libsql_token,
            )
        )
        conn.sync()
    else:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=OFF;")  # Géré manuellement selon l'ordre d'ingestion
    conn.execute("PRAGMA busy_timeout=10000;")

    resolved_path = f"{db_path}|{libsql_url or 'sqlite'}"
    if resolved_path not in _schema_synced_paths:
        execute_schema_file(conn)
        _schema_synced_paths.add(resolved_path)

    return conn


def execute_schema_file(conn: sqlite3.Connection, schema_file: Optional[Path | str] = None):
    """Exécute le script DDL de création du schéma (models/schema.sql par défaut)."""
    if schema_file is None:
        schema_file = Path(__file__).parent / "schema.sql"

    with open(schema_file, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    cursor = conn.cursor()
    cursor.executescript(schema_sql)
    conn.commit()


def _make_read_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Connexion lecture seule optimisée (WAL, check_same_thread=False).
    N'applique PAS le schéma — uniquement pour les vues."""
    if db_path is None:
        db_path = get_db_path()
    libsql_url, libsql_token = _libsql_settings()
    if libsql_url:
        if libsql is None:
            raise RuntimeError(
                "LIBSQL_URL est configuré mais libsql n'est pas installé."
            )
        conn = _LibsqlConnection(
            libsql.connect(
                str(db_path),
                sync_url=libsql_url,
                auth_token=libsql_token,
            )
        )
        conn.sync()
    else:
        conn = sqlite3.connect(
            str(db_path),
            timeout=30.0,
            check_same_thread=False,  # nécessaire pour @st.cache_resource
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA query_only=ON;")
    return conn


if _HAS_STREAMLIT:
    @st.cache_resource(show_spinner=False)
    def get_cached_connection(db_path_str: Optional[str] = None) -> sqlite3.Connection:
        """Connexion SQLite partagée et cachée par Streamlit (@st.cache_resource).

        Usage : dans les fonctions de lecture des modèles/controllers uniquement.
        Pour l'ingéstion (INSERT/UPDATE), utiliser get_connection() standard.

        Le cache est automatiquement invalide quand l'application redémarre.
        Pour forcer la réinitialisation après une ingéstion, appeler
        `get_cached_connection.clear()` depuis la vue qui vient d'insérer.
        """
        path = Path(db_path_str) if db_path_str else None
        return _make_read_connection(path)
else:
    def get_cached_connection(db_path_str: Optional[str] = None) -> sqlite3.Connection:  # type: ignore[misc]
        """Fallback sans cache (hors contexte Streamlit — tests, scripts)."""
        path = Path(db_path_str) if db_path_str else None
        return _make_read_connection(path)