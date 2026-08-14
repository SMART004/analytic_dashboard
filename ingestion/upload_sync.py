# ingestion/upload_sync.py
"""
Pont entre les 3 buckets Supabase Storage de performance (Commercial,
PR & Caisses, CDS — Q3 : 3 buckets distincts conservés) et la table SQLite
``transactions``.

Avant la migration, chaque page (perf.py / cds_perf.py / pr_caisse_perf.py)
uploadait vers son bucket puis lisait/concaténait les DataFrames directement
en mémoire à chaque interaction, sans jamais toucher SQLite. Après la
Tâche 5, la page ne doit plus lire les buckets à la volée : elle upload,
synchronise vers SQLite une fois, puis lit exclusivement via
models/performance_model.py. Ce module est le point de jonction unique.

Une seule fonction de chargement parallélisée (reprise de l'ancienne
perf.py::load_perf_folders, la meilleure des trois implémentations
dupliquées identifiées en Tâche 5.1) est utilisée pour les 3 segments.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Literal, Any

import pandas as pd

from datetime import date, datetime

from ingestion.ingest import ingest_transaction_dataframe
from ingestion.ingest_oos import ingest_listing_oos_dataframe, ingest_hvc_variation_dataframe
from models.db import get_connection
from utils.storage import get_files_by_month, list_month_folders, upload_file_by_month

logger = logging.getLogger(__name__)

Segment = Literal["commercial", "pr_caisse", "cds"]

# Q3 (validé) : 3 buckets distincts conservés, une seule fonction de
# chargement parallélisée partagée entre les 3.
SEGMENT_BUCKETS: dict[Segment, str] = {
    "commercial": "performance-result-files",
    "pr_caisse": "performance-pos-caisse-result-files",
    "cds": "performance-cds-result-files",
}


def get_bucket_for_segment(segment: Segment) -> str:
    try:
        return SEGMENT_BUCKETS[segment]
    except KeyError as exc:
        raise ValueError(f"Segment inconnu: {segment}") from exc


def list_available_folders(segment: Segment) -> list[str]:
    """Dossiers (mois) disponibles dans le bucket du segment."""
    bucket = get_bucket_for_segment(segment)
    folders = list_month_folders(bucket=bucket)
    return list(folders) if folders else []


def upload_segment_files(segment: Segment, uploaded_files: list) -> int:
    """Upload une liste de fichiers Streamlit vers le bucket du segment.

    Retourne le nombre de fichiers effectivement uploadés. Vide les caches
    Streamlit de list_month_folders/get_files_by_month pour que la
    synchronisation suivante voie les nouveaux fichiers.
    """
    if not uploaded_files:
        return 0

    bucket = get_bucket_for_segment(segment)
    uploaded_count = 0
    for file in uploaded_files:
        if upload_file_by_month(bucket=bucket, uploaded_file=file):
            uploaded_count += 1

    if uploaded_count:
        get_files_by_month.clear()
        list_month_folders.clear()

    return uploaded_count


def load_bucket_folders(
    bucket: str,
    folders: list[str],
    max_workers: int = 8,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Chargement parallélisé de plusieurs dossiers d'un bucket (une seule
    implémentation partagée par les 3 segments — reprise de l'ancienne
    perf.py::load_perf_folders, qui était la seule des trois versions
    dupliquées à paralléliser via ThreadPoolExecutor).
    """
    if not folders:
        return pd.DataFrame(), []

    dfs: list[pd.DataFrame] = []
    loaded_folders: list[str] = []

    def _load_single_folder(folder: str) -> tuple[str, pd.DataFrame | None]:
        try:
            df = get_files_by_month(bucket=bucket, selected_month=folder, max_workers=10)
            return folder, df
        except Exception as exc:
            logger.warning(f"Erreur chargement dossier {folder} (bucket {bucket}): {exc}")
            return folder, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_load_single_folder, folder): folder for folder in folders}
        for future in concurrent.futures.as_completed(futures):
            folder, df = future.result()
            if df is not None and not df.empty:
                df = df.copy()
                df["source_folder"] = folder
                dfs.append(df)
                loaded_folders.append(folder)

    if not dfs:
        return pd.DataFrame(), []

    combined = pd.concat(dfs, ignore_index=True)
    combined.columns = [str(c).strip() for c in combined.columns]
    return combined, loaded_folders


def sync_segment_to_sqlite(
    segment: Segment,
    folders: list[str],
    conn=None,
) -> dict[str, int]:
    """
    Charge un ou plusieurs dossiers du bucket d'un segment et les ingère
    dans SQLite. Un fichier déjà ingéré (même tx_date/from/to/amount/type)
    est silencieusement ignoré grâce à la contrainte UNIQUE de la table
    transactions — cette fonction est donc idempotente et peut être
    rappelée à chaque visite de page sans dupliquer les données.

    Retourne {"lignes_chargees": N, "lignes_inserees": M, "fichiers": K}.
    """
    bucket = get_bucket_for_segment(segment)
    df, loaded_folders = load_bucket_folders(bucket, folders)

    if df.empty:
        return {"lignes_chargees": 0, "lignes_inserees": 0, "fichiers": 0}

    close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        source_col = "source_file" if "source_file" in df.columns else None
        total_inserted = 0
        nb_fichiers = 0

        if source_col:
            for source_filename, group in df.groupby(source_col, dropna=False):
                nb_fichiers += 1
                total_inserted += ingest_transaction_dataframe(
                    conn, group, str(source_filename) or f"{bucket}_sans_nom"
                )
        else:
            # Pas de colonne source_file exploitable : on ingère le dossier
            # entier comme une seule unité de traçabilité.
            nb_fichiers = 1
            label = f"{bucket}/{'+'.join(loaded_folders)}"
            total_inserted = ingest_transaction_dataframe(conn, df, label)

        return {
            "lignes_chargees": len(df),
            "lignes_inserees": total_inserted,
            "fichiers": nb_fichiers,
        }
    finally:
        if close:
            conn.close()


def sync_oos_to_sqlite(uploaded_file: Any) -> dict:
    """Lit un fichier OOS uploade, detecte la date snapshot, ingere dans SQLite.
 
    Retourne {"lignes_inserees": int, "snapshot_date": str} ou {"error": str}.
    """
 
    try:
        if uploaded_file.name.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)
 
        # snapshot_date : colonne dediee si presente, sinon date du jour.
        if "snapshot_date" in df.columns:
            snapshot_date = str(df["snapshot_date"].iloc[0])
        elif "date" in df.columns:
            snapshot_date = str(pd.to_datetime(df["date"].iloc[0]).date())
        else:
            snapshot_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
 
        conn = get_connection()
        try:
            inserted = ingest_listing_oos_dataframe(conn, df, snapshot_date)
        finally:
            conn.close()
 
        return {"lignes_inserees": inserted, "snapshot_date": snapshot_date}
 
    except Exception as exc:
        logger.error("sync_oos_to_sqlite : %s", exc)
        return {"error": str(exc)}
 
 
def sync_hvc_variation_to_sqlite(uploaded_file: Any) -> dict:
    """Lit un fichier HVC uploade, horodate le snapshot, ingere dans SQLite.
 
    Retourne {"lignes_inserees": int, "snapshot_timestamp": str} ou {"error": str}.
    """
 
    try:
        if uploaded_file.name.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)
 
        # Horodatage : extrait du nom de fichier si format ISO reconnu,
        # sinon datetime.now() — meme logique que variations_hvc.py original
        # qui utilisait l'horodatage du fichier Supabase Storage.
        import re
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2}[T_]\d{2}[:-]\d{2})", uploaded_file.name)
        if ts_match:
            snapshot_timestamp = ts_match.group(1).replace("_", "T").replace("-", ":", 2)
        else:
            snapshot_timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
 
        conn = get_connection()
        try:
            inserted = ingest_hvc_variation_dataframe(conn, df, snapshot_timestamp, uploaded_file.name)
        finally:
            conn.close()
 
        return {"lignes_inserees": inserted, "snapshot_timestamp": snapshot_timestamp}
 
    except Exception as exc:
        logger.error("sync_hvc_variation_to_sqlite : %s", exc)
        return {"error": str(exc)}
