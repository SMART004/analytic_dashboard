"""ingestion/upload_sync.py

Pont entre les 3 buckets Supabase des pages de performance et la table
SQLite `transactions`. Corrige le Bloquant 3 identifie en revue de la
Tache 5.4 : sans ce module, les fichiers uploades depuis
views/performance_view.py n'etaient jamais lus dans SQLite — seul
DATA_PATH / "conquete_transactions" (dossier local) etait pris en compte
par ingestion.ingest.ingest_all_transactions.

Decision Q3 (rappel) : 3 buckets distincts inchanges
(performance-result-files, performance-pos-caisse-result-files,
performance-cds-result-files), mais UNE SEULE fonction de chargement
parallelise (version perf.py, deja utilisee par les 3 pages historiques
via utils.storage.get_files_by_month) — reutilisee ici telle quelle,
jamais reimplementee.

Ce module ne fait que l'orchestration (upload -> liste des dossiers ->
ingestion SQLite). Le coeur d'ingestion (nettoyage, snapshot zone/site,
deduplication par contrainte UNIQUE) reste dans ingestion.ingest, via
ingest_transaction_dataframe — deja present dans ce fichier, concu pour
ce cas d'usage precis (DataFrame deja charge en memoire, pas un fichier
sur disque).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from ingestion.ingest import ingest_transaction_dataframe
from models.db import get_connection
from utils.storage import get_files_by_month, list_month_folders, upload_file_by_month

logger = logging.getLogger(__name__)

# Un bucket par segment — inchange (decision Q3), aligne sur les constantes
# BUCKET_NAME historiques de perf.py / cds_perf.py / pr_caisse_perf.py.
BUCKET_BY_SEGMENT: dict[str, str] = {
    "commercial": "performance-result-files",
    "pr_caisse": "performance-pos-caisse-result-files",
    "cds": "performance-cds-result-files",
}


def upload_files(segment: str, uploaded_files: list[Any]) -> int:
    """Upload des fichiers vers le bucket du segment concerne.

    Retourne le nombre de fichiers effectivement uploades. Vide le cache
    Streamlit des fonctions de listing/chargement pour que les nouveaux
    fichiers soient visibles immediatement.
    """
    bucket = BUCKET_BY_SEGMENT[segment]
    count = 0
    for file in uploaded_files:
        if upload_file_by_month(bucket=bucket, uploaded_file=file):
            count += 1

    if count:
        # Meme reflexe que perf.py/cds_perf.py/pr_caisse_perf.py : vider le
        # cache pour que le nouveau dossier apparaisse dans le selectbox.
        get_files_by_month.clear()
        list_month_folders.clear()

    return count


def available_folders(segment: str) -> list[str]:
    """Dossiers (mois) disponibles dans le bucket du segment."""
    bucket = BUCKET_BY_SEGMENT[segment]
    folders = list_month_folders(bucket=bucket)
    return folders or []


def sync_folder_to_sqlite(segment: str, folder: str) -> int:
    """Charge un dossier depuis le bucket du segment et l'ingere dans SQLite.

    Retourne le nombre de transactions effectivement inserees (les doublons
    deja presents sont ignores par la contrainte UNIQUE de la table
    transactions, pas recomptes ici comme "inseres").

    Le DataFrame retourne par get_files_by_month contient une colonne
    source_file (un fichier du dossier peut regrouper plusieurs lignes) —
    on ingere groupe par groupe pour que source_file/file_hash restent
    traces correctement par fichier d'origine, pas par dossier entier.
    """
    bucket = BUCKET_BY_SEGMENT[segment]
    df = get_files_by_month(bucket=bucket, selected_month=folder, max_workers=10)
    if df is None or df.empty:
        return 0

    conn = get_connection()
    total = 0
    try:
        source_col = "source_file" if "source_file" in df.columns else None
        if source_col:
            for source_name, group in df.groupby(source_col):
                total += ingest_transaction_dataframe(conn, group, str(source_name))
        else:
            # Repli si get_files_by_month ne fournit pas de colonne
            # source_file pour une raison quelconque : on ingere le dossier
            # entier comme une seule source nommee.
            total += ingest_transaction_dataframe(conn, df, f"{bucket}/{folder}")
    finally:
        conn.close()

    return total


def sync_all_available(segment: str) -> dict[str, int]:
    """Synchronise tous les dossiers disponibles d'un segment vers SQLite.

    Utile pour un rattrapage complet (ex: premiere mise en place de SQLite
    sur un bucket qui contient deja plusieurs mois d'historique) — a
    utiliser avec prudence sur de gros volumes, chaque dossier etant charge
    et ingere sequentiellement.
    """
    results: dict[str, int] = {}
    for folder in available_folders(segment):
        results[folder] = sync_folder_to_sqlite(segment, folder)
    return results