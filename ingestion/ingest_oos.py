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
    Ingere un fichier "Listing OOS" (une ligne = un POS actuellement en
    rupture) pour une date donnee. Colonnes attendues : MSISDN, Day_Target,
    Float, OOS, Last Trx Time, Locality/SITENAME, Cluster, Territory, Zone,
    Segment Group — memes noms que l'ancienne REQUIRED_COLS de
    listing_pos_oos.py, retrouves ici independamment de la casse/espaces.

    is_oos est mis a 1 pour toutes les lignes ingerees : le fichier source
    EST la liste des POS en rupture (comme dans l'ancienne page), il n'y a
    pas de flag a calculer.
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    msisdn_col = _find_col(df, ["MSISDN"])
    if not msisdn_col:
        return 0

    day_target_col = _find_col(df, ["Day_Target", "Day Target"])
    float_col = _find_col(df, ["Float"])
    oos_col = _find_col(df, ["OOS"])
    last_trx_col = _find_col(df, ["Last Trx Time", "Last_Trx_Time"])
    site_col = _find_col(df, ["Locality", "SITENAME", "Sitename"])
    cluster_col = _find_col(df, ["Cluster"])
    territory_col = _find_col(df, ["Territory"])
    zone_col = _find_col(df, ["Zone"])
    segment_col = _find_col(df, ["Segment Group", "Segment_Group"])

    cursor = conn.cursor()
    count = 0
    for _, r in df.iterrows():
        msisdn = clean_phone(r.get(msisdn_col))
        if not msisdn:
            continue

        site_key = normalize_site_key(r.get(site_col)) if site_col else None
        if site_key and site_col and pd.notna(r.get(site_col)):
            cursor.execute(
                "INSERT OR IGNORE INTO sites (site_key, sitename) VALUES (?, ?)",
                (site_key, str(r.get(site_col)).strip()),
            )

        day_target = pd.to_numeric(r.get(day_target_col), errors="coerce") if day_target_col else None
        float_amount = pd.to_numeric(r.get(float_col), errors="coerce") if float_col else None
        oos_val = pd.to_numeric(r.get(oos_col), errors="coerce") if oos_col else None

        cursor.execute(
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
            (
                msisdn,
                float(day_target) if day_target is not None and pd.notna(day_target) else 0.0,
                float(float_amount) if float_amount is not None and pd.notna(float_amount) else 0.0,
                float(oos_val) if oos_val is not None and pd.notna(oos_val) else 0.0,
                str(r.get(last_trx_col)).strip() if last_trx_col and pd.notna(r.get(last_trx_col)) else None,
                site_key,
                str(r.get(cluster_col)).strip() if cluster_col and pd.notna(r.get(cluster_col)) else None,
                str(r.get(territory_col)).strip() if territory_col and pd.notna(r.get(territory_col)) else None,
                str(r.get(zone_col)).strip() if zone_col and pd.notna(r.get(zone_col)) else None,
                str(r.get(segment_col)).strip() if segment_col and pd.notna(r.get(segment_col)) else None,
                snapshot_date,
            ),
        )
        count += 1

    conn.commit()
    return count


def ingest_hvc_variation_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    snapshot_timestamp: str,
    source_file: str,
) -> int:
    """
    Ingere un export HVC brut (une ligne = un site, colonnes SITENAME,
    "#day HVC", "%OOS HVC" — meme format que l'onglet "Export" lu par
    l'ancien variations_hvc.py::load_site_level_data_bytes) pour un
    horodatage donne.

    %OOS HVC est stocke tel quel (fraction 0-1, comme dans le fichier
    source) : c'est le controller/la vue qui multiplient par 100 a
    l'affichage, exactement comme le faisait l'ancien
    compute_multiindex_variation (merged["%OOS_old"] = ... * 100).
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    site_col = _find_col(df, ["SITENAME", "Sitename"])
    day_col = _find_col(df, ["#day HVC", "#HVC"])
    oos_col = _find_col(df, ["%OOS HVC", "OOS HVC"])
    if not site_col:
        return 0

    cursor = conn.cursor()
    count = 0
    for _, r in df.iterrows():
        site_key = normalize_site_key(r.get(site_col))
        if not site_key or site_key == "TOTAL":
            continue

        cursor.execute(
            "INSERT OR IGNORE INTO sites (site_key, sitename) VALUES (?, ?)",
            (site_key, str(r.get(site_col)).strip()),
        )

        day_hvc = pd.to_numeric(r.get(day_col), errors="coerce") if day_col else None
        oos_pct = pd.to_numeric(r.get(oos_col), errors="coerce") if oos_col else None

        cursor.execute(
            """
            INSERT INTO hvc_variations (site_key, day_hvc, oos_pct, snapshot_timestamp, source_file)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(site_key, snapshot_timestamp) DO UPDATE SET
                day_hvc=excluded.day_hvc,
                oos_pct=excluded.oos_pct,
                source_file=excluded.source_file
            """,
            (
                site_key,
                float(day_hvc) if day_hvc is not None and pd.notna(day_hvc) else 0.0,
                float(oos_pct) if oos_pct is not None and pd.notna(oos_pct) else 0.0,
                snapshot_timestamp,
                source_file,
            ),
        )
        count += 1

    conn.commit()
    return count