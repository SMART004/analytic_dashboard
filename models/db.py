# models/db.py
import sqlite3
from pathlib import Path
from typing import Optional
from utils.config_storage import LOCAL_STORAGE_PATH

DEFAULT_DB_PATH = LOCAL_STORAGE_PATH / "dashboard.db"

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

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=OFF;")  # Géré manuellement selon l'ordre d'ingestion
    conn.execute("PRAGMA busy_timeout=10000;")

    resolved_path = str(db_path)
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