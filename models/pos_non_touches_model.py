from __future__ import annotations

import sqlite3
from typing import Any, Optional

import pandas as pd

from models.db import get_connection


def get_pos_non_touches_filter_options(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cursor = conn.cursor()

        def values(query: str) -> list[str]:
            cursor.execute(query)
            return sorted({str(row[0]).strip() for row in cursor.fetchall() if row[0]})

        cursor.execute("SELECT MIN(date_only) AS min_date, MAX(date_only) AS max_date FROM transactions")
        row = cursor.fetchone()

        return {
            "min_date": row["min_date"] if row else None,
            "max_date": row["max_date"] if row else None,
            "segments": values("SELECT DISTINCT segment_group FROM referentiel_pos WHERE segment_group IS NOT NULL AND segment_group <> ''"),
            "sites": values(
                """
                SELECT DISTINCT COALESCE(s.sitename, p.site_key)
                FROM referentiel_pos p
                LEFT JOIN sites s ON p.site_key = s.site_key
                WHERE COALESCE(s.sitename, p.site_key) IS NOT NULL
                  AND COALESCE(s.sitename, p.site_key) <> ''
                """
            ),
            "zone_sa": values("SELECT DISTINCT zone_sa FROM referentiel_pos WHERE zone_sa IS NOT NULL AND zone_sa <> ''"),
            "zones": values("SELECT DISTINCT zone_centre FROM referentiel_pos WHERE zone_centre IS NOT NULL AND zone_centre <> ''"),
            "territories": values("SELECT DISTINCT zone_territoire FROM referentiel_pos WHERE zone_territoire IS NOT NULL AND zone_territoire <> ''"),
            "clusters": values("SELECT DISTINCT secteur_cluster FROM referentiel_pos WHERE secteur_cluster IS NOT NULL AND secteur_cluster <> ''"),
        }
    finally:
        if close:
            conn.close()


def get_total_pos_count(
    segment: Optional[str] = None,
    site: Optional[str] = None,
    zone_sa: Optional[str] = None,
    zone: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        where, params = _pos_filters(
            segment=segment,
            site=site,
            zone_sa=zone_sa,
            zone=zone,
            territory=territory,
            cluster=cluster,
        )
        query = f"""
        WITH pos_base AS (
            {_pos_base_sql()}
        )
        SELECT COUNT(DISTINCT pos_msisdn) AS total_pos
        FROM pos_base pb
        WHERE {where}
        """
        row = conn.execute(query, params).fetchone()
        return int(row["total_pos"] or 0) if row else 0
    finally:
        if close:
            conn.close()


def get_pos_non_touches_detail(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    segment: Optional[str] = None,
    site: Optional[str] = None,
    zone_sa: Optional[str] = None,
    zone: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        pos_where, params = _pos_filters(
            segment=segment,
            site=site,
            zone_sa=zone_sa,
            zone=zone,
            territory=territory,
            cluster=cluster,
        )
        touched_where = ["t.to_msisdn IS NOT NULL", "TRIM(t.to_msisdn) <> ''"]
        if start_date:
            touched_where.append("COALESCE(t.date_only, DATE(t.tx_date)) >= :start_date")
            params["start_date"] = start_date
        if end_date:
            touched_where.append("COALESCE(t.date_only, DATE(t.tx_date)) <= :end_date")
            params["end_date"] = end_date

        query = f"""
        WITH pos_base AS (
            {_pos_base_sql()}
        ),
        pos_filtered AS (
            SELECT pb.*
            FROM pos_base pb
            WHERE {pos_where}
        ),
        pos_touched_period AS (
            SELECT DISTINCT t.to_msisdn AS pos_msisdn
            FROM transactions t
            WHERE {" AND ".join(touched_where)}
        ),
        non_touched AS (
            SELECT pf.*
            FROM pos_filtered pf
            LEFT JOIN pos_touched_period pt ON pf.pos_msisdn = pt.pos_msisdn
            WHERE pt.pos_msisdn IS NULL
        ),
        last_intervention_ids AS (
            SELECT
                nt.pos_msisdn,
                (
                    SELECT t.id
                    FROM transactions t
                    WHERE t.to_msisdn = nt.pos_msisdn
                    ORDER BY t.tx_date DESC, t.id DESC
                    LIMIT 1
                ) AS last_tx_id
            FROM non_touched nt
        ),
        last_intervention AS (
            SELECT
                ids.pos_msisdn,
                TRIM(t.from_msisdn) AS intervenant_msisdn,
                COALESCE(c.nom_ccial, pr.nom, p_from.full_name, t.from_name, 'Inconnu') AS intervenant_name,
                CASE
                    WHEN c.ccial_msisdn IS NOT NULL THEN 'Commercial'
                    WHEN pr.msisdn_pr IS NOT NULL THEN 'PR / Caisse'
                    WHEN p_from.agent_msisdn IS NOT NULL THEN 'POS'
                    ELSE 'Inconnu'
                END AS intervenant_type,
                COALESCE(c.zone_sa, p_from.zone_sa, t.zone_sa_snapshot, 'N/A') AS intervenant_zone_sa,
                t.tx_date AS last_intervention_date
            FROM last_intervention_ids ids
            JOIN transactions t ON t.id = ids.last_tx_id
            LEFT JOIN referentiel_commerciaux c ON TRIM(t.from_msisdn) = TRIM(c.ccial_msisdn)
            LEFT JOIN point_relay_referentiel pr ON TRIM(t.from_msisdn) = TRIM(pr.msisdn_pr)
            LEFT JOIN referentiel_pos p_from ON TRIM(t.from_msisdn) = TRIM(p_from.agent_msisdn)
        ),
        hvc_attr AS (
            SELECT
                TRIM(m.hvc_msisdn) AS hvc_msisdn,
                MAX(TRIM(m.ccial_msisdn)) AS ccial_msisdn,
                MAX(COALESCE(m.ccial_en_charge, c.nom_ccial)) AS commercial_attribue
            FROM hvc_commercial_mapping m
            LEFT JOIN referentiel_commerciaux c ON TRIM(m.ccial_msisdn) = TRIM(c.ccial_msisdn)
            WHERE m.hvc_msisdn IS NOT NULL
            GROUP BY TRIM(m.hvc_msisdn)
        )
        SELECT
            nt.pos_name AS "Nom du POS",
            nt.pos_msisdn AS "Numero du POS",
            nt.site_name AS "Site",
            nt.territory AS "Territoire",
            nt.zone_sa AS "Zone_SA",
            nt.zone AS "Zone",
            nt.cluster AS "Cluster",
            li.intervenant_msisdn AS "MSISDN intervenant",
            li.intervenant_name AS "Nom intervenant",
            li.intervenant_type AS "Type intervenant",
            li.intervenant_zone_sa AS "Zone_SA intervenant",
            li.last_intervention_date AS "Derniere date d'intervention",
            ha.commercial_attribue AS "Commercial attribue"
        FROM non_touched nt
        LEFT JOIN last_intervention li ON nt.pos_msisdn = li.pos_msisdn
        LEFT JOIN hvc_attr ha ON nt.pos_msisdn = ha.hvc_msisdn
        ORDER BY
            CASE WHEN li.last_intervention_date IS NULL THEN 1 ELSE 0 END,
            DATETIME(li.last_intervention_date) ASC,
            nt.zone_sa,
            nt.territory,
            nt.cluster,
            nt.site_name
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


def _pos_base_sql() -> str:
    return """
        SELECT
            TRIM(p.agent_msisdn) AS pos_msisdn,
            MAX(p.full_name) AS pos_name,
            MAX(p.site_key) AS site_key,
            MAX(COALESCE(s.sitename, p.site_key)) AS site_name,
            MAX(p.zone_territoire) AS territory,
            MAX(p.zone_sa) AS zone_sa,
            MAX(p.zone_centre) AS zone,
            MAX(p.secteur_cluster) AS cluster,
            MAX(p.segment_group) AS segment
        FROM referentiel_pos p
        LEFT JOIN sites s ON p.site_key = s.site_key
        WHERE p.agent_msisdn IS NOT NULL
          AND TRIM(p.agent_msisdn) <> ''
        GROUP BY TRIM(p.agent_msisdn)
    """


def _pos_filters(
    segment: Optional[str] = None,
    site: Optional[str] = None,
    zone_sa: Optional[str] = None,
    zone: Optional[str] = None,
    territory: Optional[str] = None,
    cluster: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {}

    if segment and segment not in ("Tous", "Toutes"):
        where.append("pb.segment = :segment")
        params["segment"] = segment
    if site and site not in ("Tous", "Toutes"):
        where.append("(pb.site_name = :site OR pb.site_key = :site)")
        params["site"] = site
    if zone_sa and zone_sa not in ("Tous", "Toutes"):
        where.append("pb.zone_sa = :zone_sa")
        params["zone_sa"] = zone_sa
    if zone and zone not in ("Tous", "Toutes"):
        where.append("pb.zone = :zone")
        params["zone"] = zone
    if territory and territory not in ("Tous", "Toutes"):
        where.append("pb.territory = :territory")
        params["territory"] = territory
    if cluster and cluster not in ("Tous", "Toutes"):
        where.append("pb.cluster = :cluster")
        params["cluster"] = cluster

    return " AND ".join(where), params
