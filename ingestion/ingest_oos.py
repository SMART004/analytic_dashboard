# ingestion/ingest_oos.py
"""
Ingestion des deux flux OOS/HVC (Tache 6.1) — comble le trou identifie en
revue : models/oos_model.py::get_oos_listing() et models/hvc_model.py::
get_hvc_variations() lisent deja listing_oos / hvc_variations, mais rien ne
les alimentait (les anciennes pages listing_pos_oos.py / variations_hvc.py
fonctionnaient 100% en memoire, upload -> affichage, jamais persiste).

Design distinct volontaire entre les deux tables (deja le cas dans
models/schema.sql, on ne le change pas) :
- listing_oos : UNIQUE(msisdn, snapshot_date) -> UNE photo par POS par jour.
  Un re-upload le meme jour REMPLACE les valeurs (ON CONFLICT DO UPDATE),
  car un listing OOS est un instantane complet, pas une transaction
  incrementale.
- hvc_variations : UNIQUE(site_key, snapshot_timestamp) -> PLUSIEURS
  releves par jour possibles (c'est deja comme ca que fonctionnait
  variations_hvc.py, avec un timestamp extrait du nom de fichier). C'est
  cette granularite horaire qui permet le graphe de progression intra-
  journaliere demande en Tache 6 — listing_oos n'a pas cette granularite et
  n'en a pas besoin (Q0 : c'est une photo, pas une serie).

Optimisation (refactor perf) :
- Les boucles iterrows() ont ete remplacees par une vectorisation pandas
  + executemany en batch pour un gain de 10-50x sur les gros fichiers.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Optional

import pandas as pd

from ingestion.ingest import normalize_site_key
from utils.helpers import clean_phone


def parse_file_timestamp(filename: str) -> Optional[datetime]:
    """Reprise telle quelle de variations_hvc.py::parse_file_timestamp —
    extrait un horodatage du nom de fichier (formats ISO, YYYYMMDD_HHMMSS,
    ou date seule). Deplacee ici pour etre partagee par l'ingestion, plutot
    que dupliquee dans la vue."""
    clean_fname = filename.split("__")[-1] if "__" in filename else filename

    match = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})", clean_fname)
    if match:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)}:{match.group(3)}:{match.group(4)}",
            "%Y-%m-%d %H:%M:%S",
        )

    match = re.search(r"(\d{8})_(\d{6})", clean_fname)
    if match:
        return datetime.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S")

    match = re.search(r"(\d{4}-\d{2}-\d{2})", clean_fname)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d")

    return None


def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    norm = {re.sub(r"[\s_]+", "", str(c)).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = re.sub(r"[\s_]+", "", candidate).strip().lower()
        if key in norm:
            return norm[key]
    return None


def ingest_listing_oos_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    snapshot_date: str,
) -> int:
    """
    Ingère un fichier "Listing OOS" (une ligne = un POS actuellement en
    rupture) pour une date donnée. Colonnes attendues : MSISDN, Day_Target,
    Float, OOS, Last Trx Time, Locality/SITENAME, Cluster, Territory, Zone,
    Segment Group — mêmes noms que l'ancienne REQUIRED_COLS de
    listing_pos_oos.py, retrouvés ici indépendamment de la casse/espaces.

    is_oos est mis a 1 pour toutes les lignes ingérées : le fichier source
    EST la liste des POS en rupture (comme dans l'ancienne page), il n'y a
    pas de flag a calculer.

    Optimisation : vectorisation pandas + executemany batch au lieu de iterrows.
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    msisdn_col = _find_col(df, ["MSISDN"])
    if not msisdn_col:
        return 0

    day_target_col = _find_col(df, ["Day_Target", "Day Target"])
    float_col     = _find_col(df, ["Float"])
    oos_col       = _find_col(df, ["OOS"])
    last_trx_col  = _find_col(df, ["Last Trx Time", "Last_Trx_Time"])
    site_col      = _find_col(df, ["Locality", "SITENAME", "Sitename"])
    cluster_col   = _find_col(df, ["Cluster"])
    territory_col = _find_col(df, ["Territory"])
    zone_col      = _find_col(df, ["Zone"])
    segment_col   = _find_col(df, ["Segment Group", "Segment_Group"])

    # ---------- Vectorisation ----------
    # MSISDN propre
    df["_msisdn"] = df[msisdn_col].apply(clean_phone)
    df = df[df["_msisdn"].notna() & (df["_msisdn"] != "")].copy()
    if df.empty:
        return 0

    # Colonnes numériques
    df["_day_target"]   = pd.to_numeric(df[day_target_col],   errors="coerce").fillna(0.0) if day_target_col   else 0.0
    df["_float_amount"] = pd.to_numeric(df[float_col],       errors="coerce").fillna(0.0) if float_col       else 0.0
    df["_oos_pct"]      = pd.to_numeric(df[oos_col],         errors="coerce").fillna(0.0) if oos_col         else 0.0

    # Colonnes texte — None si NaN
    def _str_or_none(series: pd.Series) -> pd.Series:
        return series.where(series.notna(), None).astype(object).apply(
            lambda v: str(v).strip() if v is not None else None
        )

    df["_last_trx"]   = _str_or_none(df[last_trx_col])   if last_trx_col   else None
    df["_site_key"]   = df[site_col].apply(lambda v: normalize_site_key(v)) if site_col else None
    df["_cluster"]    = _str_or_none(df[cluster_col])    if cluster_col    else None
    df["_territory"]  = _str_or_none(df[territory_col])  if territory_col  else None
    df["_zone"]       = _str_or_none(df[zone_col])       if zone_col       else None
    df["_segment"]    = _str_or_none(df[segment_col])    if segment_col    else None

    # Pré-insérer les sites manquants (INSERT OR IGNORE)
    if site_col:
        sites = df[["_site_key", site_col]].dropna(subset=["_site_key"]).drop_duplicates("_site_key")
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT OR IGNORE INTO sites (site_key, sitename) VALUES (?, ?)",
            [(row["_site_key"], str(row[site_col]).strip()) for _, row in sites.iterrows()]
        )

    # Construire les tuples en batch
    records = list(zip(
        df["_msisdn"],
        df["_day_target"],
        df["_float_amount"],
        df["_oos_pct"],
        df["_last_trx"]   if "_last_trx"  in df.columns else [None] * len(df),
        df["_site_key"]   if "_site_key"  in df.columns else [None] * len(df),
        df["_cluster"]    if "_cluster"   in df.columns else [None] * len(df),
        df["_territory"]  if "_territory" in df.columns else [None] * len(df),
        df["_zone"]       if "_zone"      in df.columns else [None] * len(df),
        df["_segment"]    if "_segment"   in df.columns else [None] * len(df),
        [snapshot_date] * len(df),
    ))

    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO listing_oos (
            msisdn, day_target, float_amount, oos_pct, is_oos, last_trx_time,
            site_key, cluster, territory, zone, segment_group, snapshot_date
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(msisdn, snapshot_date) DO UPDATE SET
            day_target=excluded.day_target,
            float_amount=excluded.float_amount,
            oos_pct=excluded.oos_pct,
            last_trx_time=excluded.last_trx_time,
            site_key=excluded.site_key,
            cluster=excluded.cluster,
            territory=excluded.territory,
            zone=excluded.zone,
            segment_group=excluded.segment_group
        """,
        records,
    )
    conn.commit()
    return len(records)


def ingest_hvc_variation_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    snapshot_timestamp: str,
    source_file: str,
) -> int:
    """
    Ingère un export HVC brut (une ligne = un site, colonnes SITENAME,
    "#day HVC", "%OOS HVC" — même format que l'onglet "Export" lu par
    l'ancien variations_hvc.py::load_site_level_data_bytes) pour un
    horodatage donné.

    %OOS HVC est stocké tel quel (fraction 0-1, comme dans le fichier
    source) : c'est le controller/la vue qui multiplient par 100 a
    l'affichage, exactement comme le faisait l'ancien
    compute_multiindex_variation (merged["%OOS_old"] = ... * 100).

    Optimisation : vectorisation pandas + executemany batch au lieu de iterrows.
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    site_col = _find_col(df, ["SITENAME", "Sitename"])
    day_col  = _find_col(df, ["#day HVC", "#HVC"])
    oos_col  = _find_col(df, ["%OOS HVC", "OOS HVC"])
    if not site_col:
        return 0

    # ---------- Vectorisation ----------
    df["_site_key"] = df[site_col].apply(normalize_site_key)
    # Exclure les lignes sans site ou ligne TOTAL
    df = df[df["_site_key"].notna() & (df["_site_key"] != "TOTAL")].copy()
    if df.empty:
        return 0

    df["_day_hvc"] = pd.to_numeric(df[day_col], errors="coerce").fillna(0.0) if day_col else 0.0
    df["_oos_pct"] = pd.to_numeric(df[oos_col], errors="coerce").fillna(0.0) if oos_col else 0.0

    # Pré-insérer les sites manquants
    sites = df[["_site_key", site_col]].drop_duplicates("_site_key")
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT OR IGNORE INTO sites (site_key, sitename) VALUES (?, ?)",
        [(row["_site_key"], str(row[site_col]).strip()) for _, row in sites.iterrows()]
    )

    # Batch principal
    records = list(zip(
        df["_site_key"],
        df["_day_hvc"],
        df["_oos_pct"],
        [snapshot_timestamp] * len(df),
        [source_file] * len(df),
    ))

    cursor.executemany(
        """
        INSERT INTO hvc_variations (site_key, day_hvc, oos_pct, snapshot_timestamp, source_file)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(site_key, snapshot_timestamp) DO UPDATE SET
            day_hvc=excluded.day_hvc,
            oos_pct=excluded.oos_pct,
            source_file=excluded.source_file
        """,
        records,
    )
    conn.commit()
    return len(records)
