# models/hvc_model.py
from __future__ import annotations

import sqlite3
from typing import Optional, Set

import pandas as pd

from models.db import get_connection


def get_hvc_snapshot_timestamps(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    """Tous les horodatages de snapshot disponibles, triés chronologiquement."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT snapshot_timestamp FROM hvc_variations ORDER BY snapshot_timestamp"
        )
        return [r["snapshot_timestamp"] for r in cursor.fetchall()]
    finally:
        if close:
            conn.close()


def get_hvc_variations(
    site_key: Optional[str] = None,
    snapshot_timestamp: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Récupère les données de variations HVC."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        where_clauses = ["1=1"]
        params: list = []

        if site_key:
            where_clauses.append("site_key = ?")
            params.append(site_key)
        if snapshot_timestamp:
            where_clauses.append("snapshot_timestamp = ?")
            params.append(snapshot_timestamp)

        where_str = " AND ".join(where_clauses)
        query = (
            "SELECT id, site_key, day_hvc, oos_pct, snapshot_timestamp, source_file "
            f"FROM hvc_variations WHERE {where_str}"
        )
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


def get_hvc_msisdns(conn: Optional[sqlite3.Connection] = None) -> Set[str]:
    """Retourne l'ensemble des MSISDN HVC (pour le KPI 'HVC en rupture')."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        # Priorité 1 : table de mapping dédiée
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT hvc_msisdn FROM hvc_commercial_mapping WHERE hvc_msisdn IS NOT NULL")
            rows = cursor.fetchall()
            if rows:
                return {str(r["hvc_msisdn"]).strip() for r in rows if r["hvc_msisdn"]}
        except Exception:
            pass

        # Priorité 2 : référentiel POS avec segment HVC
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT msisdn
                FROM referentiel_pos
                WHERE UPPER(COALESCE(segment_group, '')) LIKE '%HVC%'
                   OR UPPER(COALESCE(segment_group, '')) LIKE '%1-HVC%'
                """
            )
            rows = cursor.fetchall()
            return {str(r["msisdn"]).strip() for r in rows if r["msisdn"]}
        except Exception:
            return set()
    finally:
        if close:
            conn.close()


def get_hvc_commercial_mapping(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Mapping HVC → Commercial (utilise le nom même sans MSISDN ccial)."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        query = """
            SELECT
                m.hvc_msisdn,
                m.ccial_msisdn,
                COALESCE(m.ccial_en_charge, c.nom_ccial) AS commercial
            FROM hvc_commercial_mapping m
            LEFT JOIN referentiel_commerciaux c
                ON m.ccial_msisdn = c.ccial_msisdn
        """
        df = pd.read_sql_query(query, conn)
        return df
    except Exception:
        return pd.DataFrame(columns=["hvc_msisdn", "ccial_msisdn", "commercial"])
    finally:
        if close:
            conn.close()