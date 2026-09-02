# models/oos_model.py
import sqlite3
import pandas as pd
from typing import Optional, List
import streamlit as st
from models.db import get_connection


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_oos_filter_options() -> dict:
    conn = get_connection()
    try:
        cursor = conn.cursor()

        def distinct(col: str) -> list[str]:
            cursor.execute(
                f"SELECT DISTINCT {col} FROM listing_oos WHERE {col} IS NOT NULL AND {col} <> ''"
            )
            return sorted({str(r[0]).strip() for r in cursor.fetchall() if r[0]})

        cursor.execute("SELECT MAX(snapshot_date) AS latest, MIN(snapshot_date) AS earliest FROM listing_oos")
        row = cursor.fetchone()

        cursor.execute("SELECT DISTINCT zone_sa FROM referentiel_pos WHERE zone_sa IS NOT NULL AND zone_sa <> ''")
        zone_sa_list = sorted({str(r[0]).strip() for r in cursor.fetchall() if r[0]})

        latest_val = row["latest"] if row else None
        earliest_val = row["earliest"] if row else None

        min_date = earliest_val[:10] if earliest_val and len(earliest_val) >= 10 else None
        max_date = latest_val[:10] if latest_val and len(latest_val) >= 10 else None

        return {
            "latest_snapshot_date": latest_val,
            "min_date": min_date,
            "max_date": max_date,
            "zone": distinct("zone"),
            "territory": distinct("territory"),
            "cluster": distinct("cluster"),
            "zone_sa": zone_sa_list,
            "segment_group": distinct("segment_group"),
        }
    finally:
        conn.close()


def get_oos_filter_options(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Dernier snapshot disponible + listes de filtres geo/segment/SA pour la vue OOS."""
    if conn is None:
        return _fetch_oos_filter_options()
    cursor = conn.cursor()

    def distinct(col: str) -> list[str]:
        cursor.execute(
            f"SELECT DISTINCT {col} FROM listing_oos WHERE {col} IS NOT NULL AND {col} <> ''"
        )
        return sorted({str(r[0]).strip() for r in cursor.fetchall() if r[0]})

    cursor.execute("SELECT MAX(snapshot_date) AS latest, MIN(snapshot_date) AS earliest FROM listing_oos")
    row = cursor.fetchone()

    cursor.execute("SELECT DISTINCT zone_sa FROM referentiel_pos WHERE zone_sa IS NOT NULL AND zone_sa <> ''")
    zone_sa_list = sorted({str(r[0]).strip() for r in cursor.fetchall() if r[0]})

    latest_val = row["latest"] if row else None
    earliest_val = row["earliest"] if row else None

    min_date = earliest_val[:10] if earliest_val and len(earliest_val) >= 10 else None
    max_date = latest_val[:10] if latest_val and len(latest_val) >= 10 else None

    return {
        "latest_snapshot_date": latest_val,
        "min_date": min_date,
        "max_date": max_date,
        "zone": distinct("zone"),
        "territory": distinct("territory"),
        "cluster": distinct("cluster"),
        "zone_sa": zone_sa_list,
        "segment_group": distinct("segment_group"),
    }


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_oos_listing(
    snapshot_date: Optional[str] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    zone: Optional[str] = None,
    zone_sa: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
    segment_group: Optional[str] = None,
    sitename: Optional[str] = None,
    is_oos: Optional[int] = None,
) -> pd.DataFrame:
    conn = get_connection()
    try:
        where_clauses = ["1=1"]
        params = []

        if snapshot_date:
            where_clauses.append("l.snapshot_date = ?")
            params.append(snapshot_date)
        if date_start:
            where_clauses.append("DATE(l.snapshot_date) >= DATE(?)")
            params.append(date_start)
        if date_end:
            where_clauses.append("DATE(l.snapshot_date) <= DATE(?)")
            params.append(date_end)
        if zone and zone not in ("Tous", "Toutes"):
            where_clauses.append("l.zone = ?")
            params.append(zone)
        if zone_sa and zone_sa not in ("Tous", "Toutes"):
            where_clauses.append("p.zone_sa = ?")
            params.append(zone_sa)
        if territory and territory not in ("Tous", "Toutes"):
            where_clauses.append("l.territory = ?")
            params.append(territory)
        if cluster and cluster not in ("Tous", "Toutes"):
            where_clauses.append("l.cluster = ?")
            params.append(cluster)
        if sitename and sitename not in ("Tous", "Toutes"):
            where_clauses.append("(s.sitename = ? OR l.site_key = ?)")
            params.extend([sitename, sitename])
        if segment_group and segment_group not in ("Tous", "Toutes"):
            where_clauses.append("l.segment_group = ?")
            params.append(segment_group)
        if is_oos is not None:
            where_clauses.append("l.is_oos = ?")
            params.append(is_oos)

        where_str = " AND ".join(where_clauses)
        query = f"""
        SELECT l.msisdn, l.day_target, l.float_amount, l.oos_pct, l.is_oos, l.last_trx_time,
               l.site_key, COALESCE(s.sitename, l.site_key) AS sitename, COALESCE(s.sitename, l.site_key) AS locality,
               l.cluster, l.territory, l.zone, p.zone_sa AS zone_sa, l.segment_group, l.snapshot_date
        FROM listing_oos l
        LEFT JOIN sites s ON l.site_key = s.site_key
        LEFT JOIN (
            SELECT agent_msisdn, MAX(zone_sa) AS zone_sa
            FROM referentiel_pos
            GROUP BY agent_msisdn
        ) p ON l.msisdn = p.agent_msisdn
        WHERE {where_str}
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_oos_listing(
    snapshot_date: Optional[str] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    zone: Optional[str] = None,
    zone_sa: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
    segment_group: Optional[str] = None,
    sitename: Optional[str] = None,
    is_oos: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Récupère les données listing_oos avec enrichissement du nom de site (sitename/locality)."""
    if conn is None:
        return _fetch_oos_listing(
            snapshot_date=snapshot_date,
            date_start=date_start,
            date_end=date_end,
            zone=zone,
            zone_sa=zone_sa,
            territory=territory,
            cluster=cluster,
            segment_group=segment_group,
            sitename=sitename,
            is_oos=is_oos,
        )
    where_clauses = ["1=1"]
    params = []

    if snapshot_date:
        where_clauses.append("l.snapshot_date = ?")
        params.append(snapshot_date)
    if date_start:
        where_clauses.append("DATE(l.snapshot_date) >= DATE(?)")
        params.append(date_start)
    if date_end:
        where_clauses.append("DATE(l.snapshot_date) <= DATE(?)")
        params.append(date_end)
    if zone and zone not in ("Tous", "Toutes"):
        where_clauses.append("l.zone = ?")
        params.append(zone)
    if zone_sa and zone_sa not in ("Tous", "Toutes"):
        where_clauses.append("p.zone_sa = ?")
        params.append(zone_sa)
    if territory and territory not in ("Tous", "Toutes"):
        where_clauses.append("l.territory = ?")
        params.append(territory)
    if cluster and cluster not in ("Tous", "Toutes"):
        where_clauses.append("l.cluster = ?")
        params.append(cluster)
    if sitename and sitename not in ("Tous", "Toutes"):
        where_clauses.append("(s.sitename = ? OR l.site_key = ?)")
        params.extend([sitename, sitename])
    if segment_group and segment_group not in ("Tous", "Toutes"):
        where_clauses.append("l.segment_group = ?")
        params.append(segment_group)
    if is_oos is not None:
        where_clauses.append("l.is_oos = ?")
        params.append(is_oos)

    where_str = " AND ".join(where_clauses)
    query = f"""
    SELECT l.msisdn, l.day_target, l.float_amount, l.oos_pct, l.is_oos, l.last_trx_time,
           l.site_key, COALESCE(s.sitename, l.site_key) AS sitename, COALESCE(s.sitename, l.site_key) AS locality,
           l.cluster, l.territory, l.zone, p.zone_sa AS zone_sa, l.segment_group, l.snapshot_date
    FROM listing_oos l
    LEFT JOIN sites s ON l.site_key = s.site_key
    LEFT JOIN (
        SELECT agent_msisdn, MAX(zone_sa) AS zone_sa
        FROM referentiel_pos
        GROUP BY agent_msisdn
    ) p ON l.msisdn = p.agent_msisdn
    WHERE {where_str}
    """
    return pd.read_sql_query(query, conn, params=params)


def find_frequent_commercial_for_unassigned_pos(
    unassigned_msisdns: List[str], 
    conn: Optional[sqlite3.Connection] = None
) -> pd.DataFrame:
    """
    Pour une liste de MSISDNs POS "Non attribué", recherche dans 'transactions'
    l'acteur qui a le plus fréquemment approvisionné le POS (Transfert).
    
    Retourne un DataFrame avec :
    ['agent_msisdn', 'ccial_deduit_msisdn', 'commercial_deduit', 'type_acteur', 'zone_sa_deduite', 'nb_trx']
    """
    if not unassigned_msisdns:
        return pd.DataFrame(columns=[
            "agent_msisdn", "ccial_deduit_msisdn", 
            "commercial_deduit", "type_acteur", "zone_sa_deduite", "nb_trx"
        ])

    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        placeholders = ",".join(["?"] * len(unassigned_msisdns))
        
        query = f"""
            WITH pos_ccial_stats AS (
                SELECT 
                    t.to_msisdn AS agent_msisdn,
                    t.from_msisdn AS ccial_msisdn,
                    COALESCE(c.nom_ccial, pr.nom, p.full_name, t.from_name, 'Inconnu') AS nom_ccial,
                    CASE 
                        WHEN c.ccial_msisdn IS NOT NULL THEN 'Commercial'
                        WHEN pr.msisdn_pr IS NOT NULL THEN 'PR / Caisse'
                        WHEN p.agent_msisdn IS NOT NULL THEN 'POS'
                        ELSE 'Inconnu'
                    END AS type_acteur,
                    COALESCE(c.zone_sa, pr.territoire, t.zone_sa_snapshot, 'N/A') AS zone_sa,
                    COUNT(t.id) AS nb_trx,
                    MAX(t.tx_date) AS max_tx_date
                FROM transactions t
                LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
                LEFT JOIN point_relay_referentiel pr ON t.from_msisdn = pr.msisdn_pr
                LEFT JOIN referentiel_pos p ON t.from_msisdn = p.agent_msisdn
                WHERE t.to_msisdn IN ({placeholders})
                GROUP BY t.to_msisdn, t.from_msisdn
            ),
            ranked_ccials AS (
                SELECT 
                    agent_msisdn,
                    ccial_msisdn,
                    nom_ccial,
                    type_acteur,
                    zone_sa,
                    nb_trx,
                    ROW_NUMBER() OVER (
                        PARTITION BY agent_msisdn 
                        ORDER BY nb_trx DESC, max_tx_date DESC
                    ) AS rn
                FROM pos_ccial_stats
            )
            SELECT 
                agent_msisdn,
                ccial_msisdn AS ccial_deduit_msisdn,
                nom_ccial AS commercial_deduit,
                type_acteur,
                zone_sa AS zone_sa_deduite,
                nb_trx
            FROM ranked_ccials
            WHERE rn = 1
        """
        
        df_result = pd.read_sql_query(query, conn, params=unassigned_msisdns)
        return df_result

    finally:
        if should_close:
            conn.close()

def insert_oos_rows(df: pd.DataFrame) -> int:
    """Insert/replace des lignes dans listing_oos (schéma réel uniquement)."""
    if df is None or df.empty:
        return 0

    # Colonnes réellement présentes dans la table
    TABLE_COLS = [
        "msisdn",
        "day_target",
        "float_amount",
        "oos_pct",
        "is_oos",
        "last_trx_time",
        "site_key",
        "cluster",
        "territory",
        "zone",
        "segment_group",
        "snapshot_date",
    ]

    work = df.copy()

    # Normalisation minimale
    if "msisdn" not in work.columns:
        raise ValueError("Colonne msisdn obligatoire")

    work["msisdn"] = work["msisdn"].astype(str).str.strip()

    if "snapshot_date" not in work.columns:
        from datetime import datetime
        work["snapshot_date"] = datetime.now().strftime("%Y-%m-%d")

    if "is_oos" not in work.columns:
        work["is_oos"] = 1

    # last_trx_time → texte ISO si datetime
    if "last_trx_time" in work.columns:
        work["last_trx_time"] = (
            pd.to_datetime(work["last_trx_time"], errors="coerce")
            .dt.strftime("%Y-%m-%d %H:%M:%S")
        )

    for col in ["day_target", "float_amount", "oos_pct"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    # Garder uniquement les colonnes de la table
    available = [c for c in TABLE_COLS if c in work.columns]
    work = work[available].drop_duplicates(subset=["msisdn", "snapshot_date"], keep="last")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        inserted = 0

        placeholders = ", ".join(["?"] * len(available))
        col_names = ", ".join(available)

        # Upsert : si (msisdn, snapshot_date) existe déjà → update
        update_sets = ", ".join(
            f"{c}=excluded.{c}" for c in available if c not in ("msisdn", "snapshot_date")
        )
        sql = f"""
            INSERT INTO listing_oos ({col_names})
            VALUES ({placeholders})
            ON CONFLICT(msisdn, snapshot_date) DO UPDATE SET
                {update_sets}
        """

        for _, row in work.iterrows():
            values = [None if pd.isna(row[c]) else row[c] for c in available]
            cursor.execute(sql, values)
            inserted += 1

        conn.commit()
        return inserted
    finally:
        conn.close()
