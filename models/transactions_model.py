# models/transactions_model.py
import sqlite3
import pandas as pd
from typing import Optional, List, Dict, Any
from models.db import get_connection
from models.reference_model import get_global_excluded_numbers
from app_config.settings import MIN_TRANSFER_AMOUNT


def get_dashboard_filter_options(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Retourne les options de filtres du dashboard sans charger toutes les transactions."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cursor = conn.cursor()

        _INVALID = {"none", "non renseigné", "non renseigne", "n/a", "", "null"}

        def distinct_values(query: str) -> list[str]:
            cursor.execute(query)
            return sorted(
                {
                    str(row[0]).strip()
                    for row in cursor.fetchall()
                    if row[0] is not None and str(row[0]).strip().lower() not in _INVALID
                }
            )

        cursor.execute("SELECT MIN(date_only) AS min_date, MAX(date_only) AS max_date FROM transactions")
        date_row = cursor.fetchone()

        raw_territories = distinct_values(
            """
            SELECT DISTINCT value
            FROM (
                SELECT COALESCE(c.zone_territoire, t.territoire_snapshot, '') AS value
                FROM transactions t
                LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
                UNION
                SELECT COALESCE(zone_territoire, '') AS value
                FROM referentiel_pos
            )
            WHERE value <> ''
            """
        )

        return {
            "min_date": date_row["min_date"] if date_row else None,
            "max_date": date_row["max_date"] if date_row else None,
            "centres": distinct_values(
                """
                SELECT DISTINCT COALESCE(c.zone_centre, '') AS value
                FROM transactions t
                LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
                WHERE value <> ''
                """
            ),
            "territoires": _dedupe_dashboard_territories(raw_territories),
            "commerciaux": distinct_values(
                """
                SELECT DISTINCT COALESCE(c.nom_ccial, '') AS value
                FROM transactions t
                LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
                WHERE value <> ''
                """
            ),
            "segments": distinct_values(
                """
                SELECT DISTINCT segment_group
                FROM referentiel_pos
                WHERE segment_group IS NOT NULL AND TRIM(segment_group) <> ''
                """
            ),
        }
    finally:
        if close:
            conn.close()


def get_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    hour_start: Optional[int] = None,
    hour_end: Optional[int] = None,
    tx_types: Optional[List[str]] = None,
    min_amount: Optional[float] = None,
    zone_centre: Optional[str] = None,
    zone_sa: Optional[str] = None,
    territory: Optional[str] = None,
    commercial: Optional[str] = None,
    segment: Optional[str] = None,
    excluded_msisdns: Optional[set] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Requête paramétrée générique sur les transactions avec filtrage SQL."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        where_clauses = ["1=1"]
        params: List[Any] = []

        if start_date:
            where_clauses.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("t.date_only <= ?")
            params.append(end_date)
        if hour_start is not None and hour_end is not None:
            where_clauses.append("t.hour >= ? AND t.hour < ?")
            params.extend([hour_start, hour_end])
        if tx_types:
            placeholders = ",".join(["?"] * len(tx_types))
            where_clauses.append(f"t.tx_type IN ({placeholders})")
            params.extend(tx_types)
        if min_amount is not None:
            where_clauses.append("t.amount >= ?")
            params.append(min_amount)
        if zone_centre and zone_centre not in ("Tous", "Toutes"):
            where_clauses.append("(p.zone_centre = ? OR c.zone_centre = ?)")
            params.extend([zone_centre, zone_centre])
        if territory and territory not in ("Tous", "Toutes"):
            where_clauses.append(
                """
                (
                    c.zone_territoire = ?
                    OR t.territoire_snapshot = ?
                    OR p.zone_territoire = ?
                    OR UPPER(REPLACE(COALESCE(c.zone_territoire, t.territoire_snapshot, ''), 'YAOUNDE ', '')) =
                       UPPER(REPLACE(?, 'YAOUNDE ', ''))
                    OR UPPER(REPLACE(COALESCE(p.zone_territoire, ''), 'YAOUNDE ', '')) =
                       UPPER(REPLACE(?, 'YAOUNDE ', ''))
                )
                """
            )
            params.extend([territory, territory, territory, territory, territory])
        if commercial and commercial not in ("Tous", "Toutes"):
            where_clauses.append("c.nom_ccial = ?")
            params.append(commercial)
        if segment and segment not in ("Tous", "Toutes"):
            where_clauses.append("p.segment_group = ?")
            params.append(segment)
        if zone_sa:
            where_clauses.append("(c.zone_sa = ? OR t.zone_sa_snapshot = ?)")
            params.extend([zone_sa, zone_sa])
        if excluded_msisdns:
            excl_placeholders = ",".join(["?"] * len(excluded_msisdns))
            where_clauses.append(f"t.to_msisdn NOT IN ({excl_placeholders})")
            params.extend(list(excluded_msisdns))

        where_str = " AND ".join(where_clauses)
        query = f"""
        SELECT t.id, t.tx_date AS Date, t.date_only AS Date_only, t.hour AS Hour,
               t.tx_type AS Type, t.amount AS Amount, t.amount AS Amount_num,
               t.from_msisdn AS From_clean, t.to_msisdn AS To_clean,
               t.from_name AS From_name, t.to_name AS To_name,
               c.nom_ccial AS Nom_Ccial,
               COALESCE(p.zone_centre, c.zone_centre) AS Zone_Centre,
               COALESCE(p.zone_territoire, t.territoire_snapshot, c.zone_territoire) AS Zone_Territoire,
               c.zone_sa AS Zone_SA,
               t.zone_sa_snapshot, t.territoire_snapshot, t.site_key_snapshot,
               p.secteur_cluster AS Cluster, p.site_key AS site_key,
               s.sitename AS Sitename
        FROM transactions t
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        LEFT JOIN referentiel_pos p ON t.to_msisdn = p.agent_msisdn
        LEFT JOIN sites s ON p.site_key = s.site_key
        WHERE {where_str}
        """
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty and "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        return df
    finally:
        if close:
            conn.close()


def get_served_pos_count(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    zone_centre: Optional[str] = None,
    excluded_msisdns: Optional[set] = None,
    min_amount: float = MIN_TRANSFER_AMOUNT,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Calcule le nombre de PDV servis distincts via agrégation SQL."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        where_clauses = ["t.tx_type = 'Transfer'", "t.amount >= ?", "t.to_msisdn IS NOT NULL"]
        params: List[Any] = [min_amount]

        if start_date:
            where_clauses.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("t.date_only <= ?")
            params.append(end_date)
        if zone_centre and zone_centre not in ("Tous", "Toutes"):
            where_clauses.append("(c.zone_centre = ? OR t.zone_sa_snapshot = ?)")
            params.extend([zone_centre, zone_centre])
        if excluded_msisdns:
            excl_placeholders = ",".join(["?"] * len(excluded_msisdns))
            where_clauses.append(f"t.to_msisdn NOT IN ({excl_placeholders})")
            params.extend(list(excluded_msisdns))

        where_str = " AND ".join(where_clauses)
        query = f"""
        SELECT COUNT(DISTINCT t.to_msisdn) AS cnt
        FROM transactions t
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        WHERE {where_str}
        """
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        if close:
            conn.close()


def _dedupe_dashboard_territories(values: list[str]) -> list[str]:
    """Avoid showing both 'ETOUDI' and 'YAOUNDE ETOUDI' in dashboard filters."""
    by_key: dict[str, str] = {}
    for value in values:
        key = str(value).strip().upper().replace("YAOUNDE ", "")
        current = by_key.get(key)
        if current is None or len(str(value)) < len(current):
            by_key[key] = str(value).strip()
    return sorted(by_key.values())


def get_dashboard_data(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    centre: Optional[str] = None,
    territoire: Optional[str] = None,
    commercial: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Données consolidées pour pages/dashboard.py (montant distribué, PDV servis).
    Fusionné depuis services/transaction_service.py.
    """
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        excluded = get_global_excluded_numbers(conn)
        excluded_placeholders = ",".join(["?"] * len(excluded)) if excluded else "''"

        where_clauses = ["1=1"]
        params: List[Any] = []

        if start_date:
            where_clauses.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("t.date_only <= ?")
            params.append(end_date)
        if centre and centre != "Toutes":
            where_clauses.append("(c.zone_centre = ? OR t.zone_sa_snapshot = ?)")
            params.extend([centre, centre])
        if territoire and territoire != "Toutes":
            where_clauses.append("(c.zone_territoire = ? OR t.territoire_snapshot = ?)")
            params.extend([territoire, territoire])
        if commercial and commercial != "Tous":
            where_clauses.append("c.nom_ccial = ?")
            params.append(commercial)

        where_str = " AND ".join(where_clauses)

        tx_query = f"""
        SELECT t.tx_date AS Date, t.date_only AS Date_only, t.hour AS Hour,
               t.tx_type AS Type, t.amount AS Amount_num, t.from_msisdn AS From_clean,
               t.to_msisdn AS To_clean, t.from_name AS From_name, t.to_name AS To_name,
               c.nom_ccial AS Nom_Ccial, c.zone_centre AS Zone_Centre,
               c.zone_territoire AS Zone_Territoire, c.zone_sa AS Zone_SA
        FROM transactions t
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        WHERE {where_str}
        """
        df_tx = pd.read_sql_query(tx_query, conn, params=params)

        dist_query = f"""
        SELECT COALESCE(SUM(t.amount), 0) AS total_dist
        FROM transactions t
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        WHERE {where_str} AND t.tx_type IN ('Transfer', 'Cash out')
        """
        cursor = conn.cursor()
        cursor.execute(dist_query, params)
        row = cursor.fetchone()
        distributed_amount = float(row["total_dist"]) if row else 0.0

        served_query = f"""
        SELECT COUNT(DISTINCT t.to_msisdn) AS served_count
        FROM transactions t
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        WHERE {where_str}
          AND t.tx_type = 'Transfer'
          AND t.amount >= 10000
          AND t.to_msisdn IS NOT NULL
          AND t.to_msisdn NOT IN ({excluded_placeholders})
        """
        cursor.execute(served_query, params + list(excluded))
        row = cursor.fetchone()
        served_pos_count = int(row["served_count"]) if row else 0

        return {
            "df_tx": df_tx,
            "distributed_amount": distributed_amount,
            "served_pos_count": served_pos_count,
        }
    finally:
        if close:
            conn.close()


def detect_empietement_sqlite(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Détection d'empiètement intra-centre via SQL.
    Fusionné depuis services/transaction_service.py.
    """
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        where_clauses = [
            "c.zone_centre IS NOT NULL",
            "p.zone_centre IS NOT NULL",
            "c.zone_centre = p.zone_centre",  # Même centre
            "c.zone_sa != p.zone_sa",          # Zone_SA différente
            "t.from_msisdn IS NOT NULL",
            "t.to_msisdn IS NOT NULL",
        ]
        params: List[Any] = []

        if start_date:
            where_clauses.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("t.date_only <= ?")
            params.append(end_date)

        where_str = " AND ".join(where_clauses)

        query = f"""
        SELECT c.zone_centre AS Centre,
               c.zone_sa AS "Zone commerciale",
               p.zone_sa AS "Zone PDV",
               c.nom_ccial AS "Nom commercial",
               t.from_msisdn AS "MSISDN commercial",
               t.to_msisdn AS PDV,
               COUNT(*) AS nb_transactions,
               SUM(t.amount) AS volume
        FROM transactions t
        JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        JOIN referentiel_pos p ON t.to_msisdn = p.agent_msisdn
        WHERE {where_str}
        GROUP BY c.zone_centre, c.zone_sa, p.zone_sa, c.nom_ccial, t.from_msisdn, t.to_msisdn
        ORDER BY Centre, nb_transactions DESC
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()
