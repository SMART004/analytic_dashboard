# models/performance_model.py
"""
Couche d'accès données pour les pages de performance (Tâche 5).

Un seul point d'entrée par segment ("commercial", "pr_caisse", "cds") pour ne
plus dupliquer trois fois la même logique de lecture (cf. cds_perf.py /
pr_caisse_perf.py / perf.py avant fusion). Le filtrage (date, heure, montant,
acteur) est fait en SQL ; seule l'agrégation métier spécifique à chaque
segment (dotations, fenêtres Work Progress, quota HVC) reste au niveau du
controller, sur un volume de lignes déjà réduit.
"""

import sqlite3
from typing import Any, Literal, Optional

import pandas as pd

from models.db import get_connection

Segment = Literal["commercial", "pr_caisse", "cds"]

_ACTOR_TABLES: dict[Segment, dict[str, str]] = {
    "commercial": {
        "table": "referentiel_commerciaux",
        "id_col": "ccial_msisdn",
        "name_col": "nom_ccial",
    },
    "cds": {
        "table": "cds_referentiel",
        "id_col": "cds_msisdn",
        "name_col": "nom_cds",
    },
    "pr_caisse": {
        "table": "point_relay_referentiel",
        "id_col": "msisdn_pr",
        "name_col": "nom",
    },
}


def _with_conn(conn: Optional[sqlite3.Connection]):
    """Return (connection, should_close)."""
    if conn is None:
        return get_connection(), True
    return conn, False


# ---------------------------------------------------------------------------
# Référentiels par segment
# ---------------------------------------------------------------------------

def get_commercial_referentiel(
    zone_centre: Optional[str] = None,
    zone_territoire: Optional[str] = None,
    zone_sa: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Référentiel commerciaux, filtré au niveau SQL."""
    conn, close = _with_conn(conn)
    try:
        where, params = ["1=1"], []
        if zone_centre and zone_centre not in ("Tous", "Toutes"):
            where.append("zone_centre = ?")
            params.append(zone_centre)
        if zone_territoire and zone_territoire not in ("Tous", "Toutes"):
            where.append("zone_territoire = ?")
            params.append(zone_territoire)
        if zone_sa and zone_sa not in ("Tous", "Toutes"):
            where.append("zone_sa = ?")
            params.append(zone_sa)
        query = f"""
            SELECT ccial_msisdn, nom_ccial, zone_centre, zone_territoire, zone_sa
            FROM referentiel_commerciaux
            WHERE {' AND '.join(where)}
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


def get_cds_referentiel(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Référentiel CDS (cds_msisdn, nom_cds)."""
    conn, close = _with_conn(conn)
    try:
        return pd.read_sql_query("SELECT cds_msisdn, nom_cds FROM cds_referentiel", conn)
    finally:
        if close:
            conn.close()


def get_point_relay_referentiel(
    type_point: Optional[str] = None,
    territoire: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Référentiel Point Relais & Caisses, filtrable par type et territoire."""
    conn, close = _with_conn(conn)
    try:
        where, params = ["1=1"], []
        if type_point and type_point not in ("Tous",):
            where.append("type_point = ?")
            params.append(type_point)
        if territoire and territoire not in ("Tous", "Toutes"):
            where.append("territoire = ?")
            params.append(territoire)
        query = f"""
            SELECT msisdn_pr, nom, territoire, localisation, type_point
            FROM point_relay_referentiel
            WHERE {' AND '.join(where)}
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


def get_actor_referentiel(segment: Segment, conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Référentiel générique par segment, colonnes normalisées (msisdn, nom)."""
    conn, close = _with_conn(conn)
    try:
        meta = _ACTOR_TABLES[segment]
        query = f"SELECT {meta['id_col']} AS msisdn, {meta['name_col']} AS nom FROM {meta['table']}"
        return pd.read_sql_query(query, conn)
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Segmentation HVC / Others (issue de referentiel_pos.segment_group)
# ---------------------------------------------------------------------------

def get_hvc_msisdns(conn: Optional[sqlite3.Connection] = None) -> set[str]:
    """MSISDN des POS segment HVC ('1-HVC')."""
    conn, close = _with_conn(conn)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT agent_msisdn FROM referentiel_pos "
            "WHERE UPPER(TRIM(segment_group)) LIKE '%HVC%'"
        )
        return {row["agent_msisdn"] for row in cursor.fetchall() if row["agent_msisdn"]}
    finally:
        if close:
            conn.close()


def get_mvc_lvc_msisdns(conn: Optional[sqlite3.Connection] = None) -> set[str]:
    """MSISDN des POS segment MVC/LVC (segment 'Others' des pages perf)."""
    conn, close = _with_conn(conn)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT agent_msisdn FROM referentiel_pos "
            "WHERE UPPER(TRIM(segment_group)) IN ('2-MVC', '3-LVC', 'MVC', 'LVC')"
        )
        return {row["agent_msisdn"] for row in cursor.fetchall() if row["agent_msisdn"]}
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Quota HVC par CDS
# ---------------------------------------------------------------------------

def get_hvc_quota_by_cds(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """
    Quota HVC attribué à chaque CDS : nb_HVC = COUNT(*) des HVC assignés à ce
    CDS dans hvc_cds_assignments. Calculé à la volée, jamais stocké en dur.
    """
    conn, close = _with_conn(conn)
    try:
        query = """
            SELECT cds_nom AS nom_cds, COUNT(*) AS nb_hvc_attribue
            FROM hvc_cds_assignments
            GROUP BY cds_nom
        """
        return pd.read_sql_query(query, conn)
    finally:
        if close:
            conn.close()


def get_hvc_cds_assignments(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Détail brut des assignations HVC -> CDS (hvc_msisdn, cds_nom)."""
    conn, close = _with_conn(conn)
    try:
        return pd.read_sql_query(
            "SELECT hvc_msisdn, cds_nom FROM hvc_cds_assignments", conn
        )
    finally:
        if close:
            conn.close()


def get_cds_commercial_flows(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_amount: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Montant CDS -> commerciaux et commerciaux distincts servis par CDS."""
    conn, close = _with_conn(conn)
    try:
        where, params = ["t.tx_type = 'Transfer'"], []
        if start_date:
            where.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where.append("t.date_only <= ?")
            params.append(end_date)
        if min_amount is not None:
            where.append("t.amount >= ?")
            params.append(min_amount)

        query = f"""
            SELECT
                cds.cds_msisdn AS Actor_MSISDN,
                SUM(t.amount) AS FD_Commercial,
                COUNT(DISTINCT c.ccial_msisdn) AS Ccial_Serve
            FROM transactions t
            INNER JOIN cds_referentiel cds ON t.from_msisdn = cds.cds_msisdn
            INNER JOIN referentiel_commerciaux c ON t.to_msisdn = c.ccial_msisdn
            WHERE {' AND '.join(where)}
            GROUP BY cds.cds_msisdn
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Transactions par segment (filtrage SQL complet : date, heure, montant, acteur)
# ---------------------------------------------------------------------------

def get_performance_transactions(
    segment: Segment,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    hour_start: Optional[int] = None,
    hour_end: Optional[int] = None,
    tx_types: Optional[list[str]] = None,
    min_amount: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Transactions dont l'acteur (From) appartient au référentiel du segment
    demandé, enrichies avec le nom/zone de l'acteur et les infos du POS
    destinataire (segment_group, site). Le filtrage heure n'a de sens que
    pour les segments 'commercial' et 'pr_caisse' (Work Progress) ; il reste
    disponible ici pour 'cds' mais le controller ne doit pas l'exposer côté UI.
    """
    conn, close = _with_conn(conn)
    try:
        meta = _ACTOR_TABLES[segment]
        actor_table, actor_id, actor_name = meta["table"], meta["id_col"], meta["name_col"]

        where, params = ["1=1"], []
        if start_date:
            where.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where.append("t.date_only <= ?")
            params.append(end_date)
        if hour_start is not None and hour_end is not None:
            where.append("t.hour >= ? AND t.hour <= ?")
            params.extend([hour_start, hour_end])
        if tx_types:
            placeholders = ",".join(["?"] * len(tx_types))
            where.append(f"t.tx_type IN ({placeholders})")
            params.extend(tx_types)
        if min_amount is not None:
            where.append("t.amount >= ?")
            params.append(min_amount)

        extra_cols = ""
        if segment == "pr_caisse":
            extra_cols = ", a.territoire AS Actor_Territoire, a.localisation AS Actor_Localisation, a.type_point AS Type_Point"
        elif segment == "commercial":
            extra_cols = ", a.zone_centre AS Zone_Centre, a.zone_territoire AS Zone_Territoire, a.zone_sa AS Zone_SA"

        query = f"""
            SELECT
                t.id, t.tx_date AS Date, t.date_only AS Date_only, t.hour AS Hour,
                t.tx_type AS Type, t.amount AS Amount,
                t.from_msisdn AS From_clean, t.to_msisdn AS To_clean,
                a.{actor_id} AS Actor_MSISDN, a.{actor_name} AS Actor_Nom,
                p.segment_group AS POS_Segment, p.site_key AS POS_Site_Key
                {extra_cols}
            FROM transactions t
            INNER JOIN {actor_table} a ON t.from_msisdn = a.{actor_id}
            LEFT JOIN referentiel_pos p ON t.to_msisdn = p.agent_msisdn
            WHERE {' AND '.join(where)}
        """
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty and "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        return df
    finally:
        if close:
            conn.close()


def get_dotation_transactions(
    segment: Segment,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    hour_max: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Transactions de dotation reçues par les acteurs du segment (Transfer où
    To = acteur du segment, From = source de dotation quelconque, filtré
    ensuite côté controller selon les catégories d'exclusion pertinentes :
    Master/Caisse -> Commercial ou CDS, Commercial/CDS -> PR).
    """
    conn, close = _with_conn(conn)
    try:
        meta = _ACTOR_TABLES[segment]
        actor_table, actor_id, actor_name = meta["table"], meta["id_col"], meta["name_col"]

        where, params = ["t.tx_type = 'Transfer'"], []
        if start_date:
            where.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where.append("t.date_only <= ?")
            params.append(end_date)
        if hour_max is not None:
            where.append("t.hour < ?")
            params.append(hour_max)

        query = f"""
            SELECT
                t.id, t.tx_date AS Date, t.date_only AS Date_only, t.hour AS Hour,
                t.amount AS Amount, t.from_msisdn AS From_clean, t.to_msisdn AS To_clean,
                a.{actor_id} AS Actor_MSISDN, a.{actor_name} AS Actor_Nom
            FROM transactions t
            INNER JOIN {actor_table} a ON t.to_msisdn = a.{actor_id}
            WHERE {' AND '.join(where)}
        """
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty and "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        return df
    finally:
        if close:
            conn.close()


def get_performance_filter_options(
    segment: Segment,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Bornes de dates et valeurs de filtres disponibles pour un segment."""
    conn, close = _with_conn(conn)
    try:
        meta = _ACTOR_TABLES[segment]
        actor_table, actor_id = meta["table"], meta["id_col"]

        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT MIN(t.date_only) AS min_date, MAX(t.date_only) AS max_date
            FROM transactions t
            INNER JOIN {actor_table} a ON t.from_msisdn = a.{actor_id}
            """
        )
        row = cursor.fetchone()

        options: dict[str, Any] = {
            "min_date": row["min_date"] if row else None,
            "max_date": row["max_date"] if row else None,
        }

        if segment == "commercial":
            cursor.execute("SELECT DISTINCT zone_territoire FROM referentiel_commerciaux WHERE zone_territoire IS NOT NULL")
            options["zone_territoire"] = sorted({r["zone_territoire"] for r in cursor.fetchall() if r["zone_territoire"]})
            cursor.execute("SELECT DISTINCT zone_sa FROM referentiel_commerciaux WHERE zone_sa IS NOT NULL")
            options["zone_sa"] = sorted({r["zone_sa"] for r in cursor.fetchall() if r["zone_sa"]})
        elif segment == "pr_caisse":
            cursor.execute("SELECT DISTINCT territoire FROM point_relay_referentiel WHERE territoire IS NOT NULL")
            options["territoire"] = sorted({r["territoire"] for r in cursor.fetchall() if r["territoire"]})

        return options
    finally:
        if close:
            conn.close()
