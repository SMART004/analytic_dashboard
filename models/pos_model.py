# models/pos_model.py
import sqlite3
import pandas as pd
from typing import Optional
import streamlit as st
from models.db import get_connection


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_all_sites() -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT site_key, sitename, zone_new, territory_correct, isl_terr, quartier, dsm_name FROM sites"
        return pd.read_sql_query(query, conn)
    finally:
        conn.close()


def get_all_sites(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Récupère tous les sites de la table sites."""
    if conn is None:
        return _fetch_all_sites()
    query = "SELECT site_key, sitename, zone_new, territory_correct, isl_terr, quartier, dsm_name FROM sites"
    return pd.read_sql_query(query, conn)


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_all_pos(
    source_master: Optional[str] = None,
    zone_centre: Optional[str] = None,
    zone_sa: Optional[str] = None,
    territory: Optional[str] = None,
    segment: Optional[str] = None,
) -> pd.DataFrame:
    conn = get_connection()
    try:
        where_clauses = ["1=1"]
        params = []

        if source_master:
            where_clauses.append("p.source_master = ?")
            params.append(source_master)
        if zone_centre and zone_centre != "Tous" and zone_centre != "Toutes":
            where_clauses.append("p.zone_centre = ?")
            params.append(zone_centre)
        if zone_sa:
            where_clauses.append("p.zone_sa = ?")
            params.append(zone_sa)
        if territory and territory not in ("Tous", "Toutes"):
            where_clauses.append(
                """
                (
                    p.zone_territoire = ?
                    OR UPPER(REPLACE(COALESCE(p.zone_territoire, ''), 'YAOUNDE ', '')) =
                       UPPER(REPLACE(?, 'YAOUNDE ', ''))
                )
                """
            )
            params.extend([territory, territory])
        if segment and segment not in ("Tous", "Toutes"):
            where_clauses.append("p.segment_group = ?")
            params.append(segment)

        where_str = " AND ".join(where_clauses)
        query = f"""
        SELECT p.agent_msisdn, p.source_master, p.full_name, p.zone_centre AS zone,
               p.zone_territoire AS territory, p.zone_sa AS sa_incharge,
               p.zone_sa AS Zone_SA_Normalisee, p.secteur_cluster AS quartier,
               p.site_key, s.sitename, p.segment_group, p.day_target, p.oos_target
        FROM referentiel_pos p
        LEFT JOIN sites s ON p.site_key = s.site_key
        WHERE {where_str}
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_all_pos(
    source_master: Optional[str] = None,
    zone_centre: Optional[str] = None,
    zone_sa: Optional[str] = None,
    territory: Optional[str] = None,
    segment: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Récupère les POS/agents filtrés, jointés avec sites."""
    if conn is None:
        return _fetch_all_pos(source_master, zone_centre, zone_sa, territory, segment)

    where_clauses = ["1=1"]
    params = []

    if source_master:
        where_clauses.append("p.source_master = ?")
        params.append(source_master)
    if zone_centre and zone_centre != "Tous" and zone_centre != "Toutes":
        where_clauses.append("p.zone_centre = ?")
        params.append(zone_centre)
    if zone_sa:
        where_clauses.append("p.zone_sa = ?")
        params.append(zone_sa)
    if territory and territory not in ("Tous", "Toutes"):
        where_clauses.append(
            """
            (
                p.zone_territoire = ?
                OR UPPER(REPLACE(COALESCE(p.zone_territoire, ''), 'YAOUNDE ', '')) =
                   UPPER(REPLACE(?, 'YAOUNDE ', ''))
            )
            """
        )
        params.extend([territory, territory])
    if segment and segment not in ("Tous", "Toutes"):
        where_clauses.append("p.segment_group = ?")
        params.append(segment)

    where_str = " AND ".join(where_clauses)
    query = f"""
    SELECT p.agent_msisdn, p.source_master, p.full_name, p.zone_centre AS zone,
           p.zone_territoire AS territory, p.zone_sa AS sa_incharge,
           p.zone_sa AS Zone_SA_Normalisee, p.secteur_cluster AS quartier,
           p.site_key, s.sitename, p.segment_group, p.day_target, p.oos_target
    FROM referentiel_pos p
    LEFT JOIN sites s ON p.site_key = s.site_key
    WHERE {where_str}
    """
    return pd.read_sql_query(query, conn, params=params)


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_pos_referentiel_count(
    zone: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
    zone_sa: Optional[str] = None,
    segment_group: Optional[str] = None,
) -> int:
    conn = get_connection()
    try:
        where_clauses = ["1=1"]
        params = []

        if zone and zone not in ("Tous", "Toutes", ""):
            where_clauses.append("(zone_centre = ? OR LOWER(zone_centre) = LOWER(?))")
            params.extend([zone, zone])

        if territory and territory not in ("Tous", "Toutes", ""):
            where_clauses.append(
                "("
                "  zone_territoire = ?"
                "  OR UPPER(REPLACE(COALESCE(zone_territoire,''), 'YAOUNDE ', '')) ="
                "     UPPER(REPLACE(?, 'YAOUNDE ', ''))"
                ")"
            )
            params.extend([territory, territory])

        if cluster and cluster not in ("Tous", "Toutes", ""):
            where_clauses.append("(secteur_cluster = ? OR LOWER(secteur_cluster) = LOWER(?))")
            params.extend([cluster, cluster])

        if zone_sa and zone_sa not in ("Tous", "Toutes", ""):
            where_clauses.append("(zone_sa = ? OR LOWER(zone_sa) = LOWER(?))")
            params.extend([zone_sa, zone_sa])

        if segment_group and segment_group not in ("Tous", "Toutes", ""):
            where_clauses.append("(segment_group = ? OR LOWER(segment_group) = LOWER(?))")
            params.extend([segment_group, segment_group])

        where_str = " AND ".join(where_clauses)
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM referentiel_pos WHERE {where_str}", params)
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_pos_referentiel_count(
    zone: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
    zone_sa: Optional[str] = None,
    segment_group: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Compte les POS du référentiel avec les mêmes filtres que listing_oos."""
    if conn is None:
        return _fetch_pos_referentiel_count(zone, territory, cluster, zone_sa, segment_group)

    where_clauses = ["1=1"]
    params = []

    if zone and zone not in ("Tous", "Toutes", ""):
        where_clauses.append("(zone_centre = ? OR LOWER(zone_centre) = LOWER(?))")
        params.extend([zone, zone])

    if territory and territory not in ("Tous", "Toutes", ""):
        where_clauses.append(
            "("
            "  zone_territoire = ?"
            "  OR UPPER(REPLACE(COALESCE(zone_territoire,''), 'YAOUNDE ', '')) ="
            "     UPPER(REPLACE(?, 'YAOUNDE ', ''))"
            ")"
        )
        params.extend([territory, territory])

    if cluster and cluster not in ("Tous", "Toutes", ""):
        where_clauses.append("(secteur_cluster = ? OR LOWER(secteur_cluster) = LOWER(?))")
        params.extend([cluster, cluster])

    if zone_sa and zone_sa not in ("Tous", "Toutes", ""):
        where_clauses.append("(zone_sa = ? OR LOWER(zone_sa) = LOWER(?))")
        params.extend([zone_sa, zone_sa])

    if segment_group and segment_group not in ("Tous", "Toutes", ""):
        where_clauses.append("(segment_group = ? OR LOWER(segment_group) = LOWER(?))")
        params.extend([segment_group, segment_group])

    where_str = " AND ".join(where_clauses)
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM referentiel_pos WHERE {where_str}", params)
    row = cursor.fetchone()
    return int(row[0]) if row else 0

