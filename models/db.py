# models/db.py
import sqlite3
import os
import logging
import re
from collections.abc import Mapping
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

_NAMED_PARAMETER = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _normalize_parameters(args):
    """Convertit les mappings SQLite en paramètres positionnels libSQL."""
    if len(args) < 2 or not isinstance(args[1], Mapping):
        return args

    query = args[0]
    parameters = args[1]
    names = _NAMED_PARAMETER.findall(query)
    query = _NAMED_PARAMETER.sub("?", query)
    values = tuple(parameters[name] for name in names)
    return (query, values, *args[2:])


class _LibsqlConnection:
    """Adapteur DB-API qui synchronise chaque transaction vers libSQL."""

    def __init__(self, connection, sync_enabled: bool = True):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_sync_enabled", sync_enabled)

    def __setattr__(self, name, value):
        if name in {"_connection", "_sync_enabled"}:
            object.__setattr__(self, name, value)
        elif name == "row_factory":
            object.__setattr__(self, name, value)
        else:
            try:
                setattr(self._connection, name, value)
            except AttributeError:
                object.__setattr__(self, name, value)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def _sync(self):
        if not self._sync_enabled:
            return
        try:
            self._connection.sync()
        except ValueError as exc:
            # sync() est réservé aux connexions ouvertes avec sync_url.
            if "mode" not in str(exc).lower():
                raise

    def cursor(self, *args, **kwargs):
        return _LibsqlCursor(self._connection.cursor(*args, **kwargs), self)

    def execute(self, *args, **kwargs):
        normalized = _normalize_parameters(args)
        return _LibsqlCursor(self._connection.execute(*normalized, **kwargs), self)

    def executemany(self, *args, **kwargs):
        if len(args) >= 2 and isinstance(args[1], (list, tuple)):
            normalized_query = _normalize_parameters((args[0], args[1][0])) if args[1] else args
            if normalized_query is not args:
                query, _ = normalized_query
                parameters = [
                    _normalize_parameters((args[0], values))[1]
                    if isinstance(values, Mapping) else values
                    for values in args[1]
                ]
                args = (query, parameters, *args[2:])
        return _LibsqlCursor(self._connection.executemany(*args, **kwargs), self)

    def executescript(self, script):
        result = self._connection.executescript(script)
        self._sync()
        return result

    def commit(self):
        try:
            self._connection.commit()
        except ValueError as exc:
            # Hrana peut déjà avoir clôturé la transaction après executescript.
            if "no transaction is active" not in str(exc):
                raise
            return
        self._sync()

    def rollback(self):
        return self._connection.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    def close(self):
        # Ne pas appeler commit ici : la transaction peut déjà être fermée
        # par Hrana, et les appelants qui écrivent font explicitement commit().
        self._connection.close()


class _LibsqlCursor:
    """Curseur libSQL avec support des lignes indexables par nom de colonne."""

    def __init__(self, cursor, connection):
        self._cursor = cursor
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, *args, **kwargs):
        self._cursor.execute(*_normalize_parameters(args), **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        if len(args) >= 2 and isinstance(args[1], (list, tuple)) and args[1]:
            normalized_query = _normalize_parameters((args[0], args[1][0]))
            if normalized_query is not args:
                query, _ = normalized_query
                parameters = [
                    _normalize_parameters((args[0], values))[1]
                    if isinstance(values, Mapping) else values
                    for values in args[1]
                ]
                args = (query, parameters, *args[2:])
        self._cursor.executemany(*args, **kwargs)
        return self

    def _row(self, values):
        if values is None or getattr(self._connection, "row_factory", None) is None:
            return values
        columns = [column[0] for column in (self.description or [])]
        return _NamedRow(columns, values)

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._row(row) for row in rows]

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _NamedRow:
    """Petit équivalent de sqlite3.Row pour les curseurs libSQL."""

    def __new__(cls, columns, values):
        return super().__new__(cls)

    def __init__(self, columns, values):
        self._columns = columns
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self._columns.index(key)
            except ValueError as exc:
                raise IndexError(key) from exc
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return repr(self._values)

    def keys(self):
        return self._columns


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
                autocommit=True,
            ),
            sync_enabled=True,
        )
    else:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    if not libsql_url:
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

    conn.executescript(schema_sql)
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
                autocommit=True,
            ),
            sync_enabled=True,
        )
    else:
        conn = sqlite3.connect(
            str(db_path),
            timeout=30.0,
            check_same_thread=False,  # nécessaire pour @st.cache_resource
        )
    conn.row_factory = sqlite3.Row
    if not libsql_url:
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