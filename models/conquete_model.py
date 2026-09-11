# models/conquete_model.py
"""
Couche d'accès données pour la Conquête de Territoire (Tâche 6).

Relocalise et étend deux fonctions qui vivaient dans transactions_model.py
(get_conquete_data, detect_empietement_sqlite) : même table `transactions`
que le segment "Commercial" de Performance (Tâche 5, bucket A confirmé —
aucune nouvelle table, aucune nouvelle synchronisation), mais avec un
filtrage SQL complet (zone/territoire/zone_sa/quartier, plus seulement
date+montant) et la règle d'empiètement corrigée.

Règle d'empiètement (corrigée, validée) :
    Avant : empiètement = même centre ET Zone_SA différente (les cas
    inter-centres étaient explicitement exclus).
    Maintenant : empiètement = Zone_SA différente, TOUJOURS — qu'il s'agisse
    du même centre (Intra-centre) ou d'un centre différent (Inter-centre).
    Les deux catégories restent distinguées via la colonne Type_Empietement
    plutôt que d'exclure l'une des deux, pour permettre un pilotage séparé
    (décision : deux taux distincts, pas un taux unique).
"""

import sqlite3
from typing import Any, Optional

import pandas as pd

from models.db import get_connection

CONQUETE_TX_TYPE = "Transfer"


def _with_conn(conn: Optional[sqlite3.Connection]):
    if conn is None:
        return get_connection(), True
    return conn, False


def get_conquete_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    zone_centre: Optional[str] = None,
    zone_territoire: Optional[str] = None,
    zone_sa: Optional[str] = None,
    quartier: Optional[str] = None,
    segment: Optional[str] = None,
    min_amount: float = 10000.0,
    excluded_msisdns: Optional[set] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Transactions de conquête enrichies (axe PDV + axe commercial), filtrées
    intégralement en SQL — zone_centre/zone_territoire/zone_sa/quartier
    n'étaient auparavant filtrés qu'en mémoire dans conquete_territoire.py.

    zone_centre/zone_territoire/zone_sa/quartier filtrent sur le PDV (To),
    cohérent avec l'usage historique de la page (centre "Centre II/III"
    sélectionné = univers de PDV, pas de commerciaux).
    """
    conn, close = _with_conn(conn)
    try:
        where, params = ["t.tx_type = ?", "t.amount >= ?"], [CONQUETE_TX_TYPE, min_amount]

        if start_date:
            where.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where.append("t.date_only <= ?")
            params.append(end_date)
        if zone_centre and zone_centre not in ("Tous", "Toutes"):
            where.append("p.zone_centre = ?")
            params.append(zone_centre)
        if zone_territoire and zone_territoire not in ("Tous", "Toutes"):
            where.append("p.zone_territoire = ?")
            params.append(zone_territoire)
        if zone_sa and zone_sa not in ("Tous", "Toutes"):
            where.append("p.zone_sa = ?")
            params.append(zone_sa)
        if quartier and quartier not in ("Tous", "Toutes"):
            where.append("p.secteur_cluster = ?")
            params.append(quartier)
        if segment and segment not in ("Tous", "Toutes"):
            where.append("p.segment_group = ?")
            params.append(segment)
        if excluded_msisdns:
            placeholders = ",".join(["?"] * len(excluded_msisdns))
            where.append(f"t.to_msisdn NOT IN ({placeholders})")
            params.extend(list(excluded_msisdns))

        query = f"""
        SELECT t.tx_date AS Date, t.date_only AS Date_only, t.hour AS Hour,
               t.tx_type AS Type, t.amount AS Amount, t.from_msisdn AS From_clean,
               t.to_msisdn AS To_clean,
               t.source_file AS source_file,
               -- axe PDV
               p.zone_sa AS Zone_SA_PDV, p.zone_centre AS Centre_PDV,
               p.zone_territoire AS Territoire_PDV, p.secteur_cluster AS Quartier_PDV,
               s.sitename AS Sitename_PDV, p.segment_group AS Segment_PDV,
               -- axe commercial
               c.zone_sa AS Zone_SA_Comm, c.zone_centre AS Centre_Comm,
               c.zone_territoire AS Territoire_Comm, c.nom_ccial AS Commercial,
               t.from_msisdn AS Commercial_MSISDN
        FROM transactions t
        LEFT JOIN referentiel_pos p ON t.to_msisdn = p.agent_msisdn
        LEFT JOIN sites s ON p.site_key = s.site_key
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        WHERE {' AND '.join(where)}
        """
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty and "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        return df
    finally:
        if close:
            conn.close()


def get_empietement(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    type_empietement: Optional[str] = None,  # 'Intra-centre' | 'Inter-centre' | None (les deux)
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Détection d'empiètement (règle corrigée) : tout commercial ayant touché
    un PDV hors de sa propre Zone_SA, que ce PDV soit dans le même centre
    (Intra-centre) ou un centre différent (Inter-centre). Les deux cas sont
    conservés et distingués (colonne Type_Empietement), jamais l'un exclu
    au profit de l'autre comme dans l'ancienne version.
    """
    conn, close = _with_conn(conn)
    try:
        where = [
            "c.zone_sa IS NOT NULL",
            "p.zone_sa IS NOT NULL",
            "c.zone_sa != p.zone_sa",   # seule condition d'empietement desormais
            "t.from_msisdn IS NOT NULL",
            "t.to_msisdn IS NOT NULL",
            "t.tx_type = ?",
        ]
        params: list[Any] = [CONQUETE_TX_TYPE]

        if start_date:
            where.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where.append("t.date_only <= ?")
            params.append(end_date)
        if type_empietement == "Intra-centre":
            where.append("c.zone_centre = p.zone_centre")
        elif type_empietement == "Inter-centre":
            where.append("(c.zone_centre IS NULL OR p.zone_centre IS NULL OR c.zone_centre != p.zone_centre)")

        query = f"""
        SELECT
            CASE WHEN c.zone_centre = p.zone_centre THEN 'Intra-centre' ELSE 'Inter-centre' END AS Type_Empietement,
            c.zone_centre AS Centre_Commercial,
            p.zone_centre AS Centre_PDV,
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
        WHERE {' AND '.join(where)}
        GROUP BY Type_Empietement, c.zone_centre, p.zone_centre, c.zone_sa, p.zone_sa,
                 c.nom_ccial, t.from_msisdn, t.to_msisdn
        ORDER BY Type_Empietement, nb_transactions DESC
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


def get_pos_portfolio_reference(
    zone_centre: Optional[str] = None,
    zone_territoire: Optional[str] = None,
    zone_sa: Optional[str] = None,
    quartier: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Référentiel POS complet pour la construction du portefeuille par
    commercial (fallback 3 niveaux : Ccial_En_Charge -> Zone_SA -> déduction
    depuis les transactions, logique métier conservée du controller).
    """
    conn, close = _with_conn(conn)
    try:
        where, params = ["1=1"], []
        if zone_centre and zone_centre not in ("Tous", "Toutes"):
            where.append("p.zone_centre = ?")
            params.append(zone_centre)
        if zone_territoire and zone_territoire not in ("Tous", "Toutes"):
            where.append("p.zone_territoire = ?")
            params.append(zone_territoire)
        if zone_sa and zone_sa not in ("Tous", "Toutes"):
            where.append("p.zone_sa = ?")
            params.append(zone_sa)
        if quartier and quartier not in ("Tous", "Toutes"):
            where.append("p.secteur_cluster = ?")
            params.append(quartier)

        query = f"""
        SELECT p.agent_msisdn AS Agent_MSISDN, p.zone_centre AS Zone_Centre,
               p.zone_territoire AS Zone_Territoire, p.zone_sa AS Zone_SA,
               p.secteur_cluster AS Quartier, p.site_key AS Site_Key,
               s.sitename AS Sitename, p.segment_group AS Segment_Group
        FROM referentiel_pos p
        LEFT JOIN sites s ON p.site_key = s.site_key
        WHERE {' AND '.join(where)}
        """
        return pd.read_sql_query(query, conn, params=params)
    finally:
        if close:
            conn.close()


def get_conquete_filter_options(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Bornes de dates + valeurs de filtres disponibles (univers PDV).

    Les valeurs invalides (None, vide, 'None', 'Non renseigné') sont
    systématiquement exclues pour ne pas polluer les listes déroulantes.
    """
    conn, close = _with_conn(conn)
    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT MIN(t.date_only) AS min_date, MAX(t.date_only) AS max_date
            FROM transactions t
            WHERE t.tx_type = ?
            """,
            (CONQUETE_TX_TYPE,),
        )
        row = cursor.fetchone()

        _INVALID = {"none", "non renseigné", "non renseigne", "n/a", "", "null"}

        def distinct_values(query: str) -> list[str]:
            cursor.execute(query)
            return sorted({
                str(r[0]).strip() for r in cursor.fetchall()
                if r[0] and str(r[0]).strip().lower() not in _INVALID
            })

        return {
            "min_date": row["min_date"] if row else None,
            "max_date": row["max_date"] if row else None,
            "zone_centre": distinct_values("SELECT DISTINCT zone_centre FROM referentiel_pos WHERE zone_centre IS NOT NULL"),
            "zone_territoire": distinct_values("SELECT DISTINCT zone_territoire FROM referentiel_pos WHERE zone_territoire IS NOT NULL"),
            "zone_sa": distinct_values("SELECT DISTINCT zone_sa FROM referentiel_pos WHERE zone_sa IS NOT NULL"),
            "quartier": distinct_values("SELECT DISTINCT secteur_cluster FROM referentiel_pos WHERE secteur_cluster IS NOT NULL"),
            "segment": distinct_values("SELECT DISTINCT segment_group FROM referentiel_pos WHERE segment_group IS NOT NULL"),
        }
    finally:
        if close:
            conn.close()


def get_conquete_evolution_series(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    zone_centre: Optional[str] = None,
    zone_territoire: Optional[str] = None,
    zone_sa: Optional[str] = None,
    segment: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Série quotidienne des POS touchés et du volume distribué.

    Utilisée pour le graphique d'évolution temporelle dans l'onglet
    Évolution & Géographie de Conquête de Territoire.

    Retourne: date_only, pos_touches, volume, nb_transactions, commerciaux_actifs
    """
    conn, close = _with_conn(conn)
    try:
        where = ["t.tx_type = ?", "t.amount >= ?"]
        params: list[Any] = [CONQUETE_TX_TYPE, 10000.0]

        if start_date:
            where.append("t.date_only >= ?")
            params.append(start_date)
        if end_date:
            where.append("t.date_only <= ?")
            params.append(end_date)
        if zone_centre and zone_centre not in ("Tous", "Toutes"):
            where.append("p.zone_centre = ?")
            params.append(zone_centre)
        if zone_territoire and zone_territoire not in ("Tous", "Toutes"):
            where.append("p.zone_territoire = ?")
            params.append(zone_territoire)
        if zone_sa and zone_sa not in ("Tous", "Toutes"):
            where.append("p.zone_sa = ?")
            params.append(zone_sa)
        if segment and segment not in ("Tous", "Toutes"):
            where.append("p.segment_group = ?")
            params.append(segment)

        query = f"""
        SELECT
            t.date_only AS date,
            COUNT(DISTINCT t.to_msisdn)  AS pos_touches,
            COUNT(DISTINCT c.ccial_msisdn) AS commerciaux_actifs,
            COUNT(*) AS nb_transactions,
            SUM(t.amount) AS volume
        FROM transactions t
        LEFT JOIN referentiel_pos p ON t.to_msisdn = p.agent_msisdn
        LEFT JOIN referentiel_commerciaux c ON t.from_msisdn = c.ccial_msisdn
        WHERE {' AND '.join(where)}
        GROUP BY t.date_only
        ORDER BY t.date_only
        """
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty and "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        return df
    finally:
        if close:
            conn.close()

def _load_master_caisse_msisdns() -> tuple[set[str], set[str]]:
    from utils.helpers import clean_phone
    from utils.turso_storage import load_setting

    masters, caisses = set(), set()

    df_m = load_setting("masters")
    if df_m is not None and not df_m.empty:
        col = next((c for c in ["NUM", "MSISDN", "msisdn"] if c in df_m.columns), None)
        if col:
            masters = {clean_phone(x) for x in df_m[col] if clean_phone(x)}

    df_c = load_setting("caisses")
    if df_c is not None and not df_c.empty:
        col = next((c for c in ["NUM", "MSISDN", "msisdn"] if c in df_c.columns), None)
        if col:
            caisses = {clean_phone(x) for x in df_c[col] if clean_phone(x)}

    return masters, caisses


def get_commercial_cash_flows(
    commercial_msisdns: list[str],
    master_msisdns: set[str],
    caisse_msisdns: set[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Par commercial :
      - Montant_recu     : Transfer To=commercial, From ∈ (masters ∪ caisses)
      - Montant_remonte  : Transfer From=commercial, To ∈ masters uniquement
    """
    if not commercial_msisdns:
        return pd.DataFrame(columns=[
            "Commercial_MSISDN", "Montant_recu", "Montant_remonte"
        ])

    conn, close = _with_conn(conn)
    try:
        placeholders = ",".join(["?"] * len(commercial_msisdns))
        params: list = list(commercial_msisdns)

        date_clause = ""
        if start_date:
            date_clause += " AND t.date_only >= ?"
            params.append(start_date)
        if end_date:
            date_clause += " AND t.date_only <= ?"
            params.append(end_date)

        # --- REÇU : From master/caisse → To commercial ---
        sources = list(master_msisdns | caisse_msisdns)
        if sources:
            src_ph = ",".join(["?"] * len(sources))
            q_recu = f"""
                SELECT t.to_msisdn AS Commercial_MSISDN,
                       SUM(t.amount) AS Montant_recu
                FROM transactions t
                WHERE t.tx_type = 'Transfer'
                  AND t.to_msisdn IN ({placeholders})
                  AND t.from_msisdn IN ({src_ph})
                  {date_clause}
                GROUP BY t.to_msisdn
            """
            # params order: commercial_msisdns + sources + dates
            params_recu = list(commercial_msisdns) + sources
            if start_date:
                params_recu.append(start_date)
            if end_date:
                params_recu.append(end_date)
            df_recu = pd.read_sql_query(q_recu, conn, params=params_recu)
        else:
            df_recu = pd.DataFrame(columns=["Commercial_MSISDN", "Montant_recu"])

        # --- REMONTÉ : From commercial → To master uniquement ---
        masters = list(master_msisdns)
        if masters:
            m_ph = ",".join(["?"] * len(masters))
            q_rem = f"""
                SELECT t.from_msisdn AS Commercial_MSISDN,
                       SUM(t.amount) AS Montant_remonte
                FROM transactions t
                WHERE t.tx_type = 'Transfer'
                  AND t.from_msisdn IN ({placeholders})
                  AND t.to_msisdn IN ({m_ph})
                  {date_clause}
                GROUP BY t.from_msisdn
            """
            params_rem = list(commercial_msisdns) + masters
            if start_date:
                params_rem.append(start_date)
            if end_date:
                params_rem.append(end_date)
            df_rem = pd.read_sql_query(q_rem, conn, params=params_rem)
        else:
            df_rem = pd.DataFrame(columns=["Commercial_MSISDN", "Montant_remonte"])

        out = pd.DataFrame({"Commercial_MSISDN": list(commercial_msisdns)})
        out = out.merge(df_recu, on="Commercial_MSISDN", how="left")
        out = out.merge(df_rem, on="Commercial_MSISDN", how="left")
        out["Montant_recu"] = pd.to_numeric(out["Montant_recu"], errors="coerce").fillna(0)
        out["Montant_remonte"] = pd.to_numeric(out["Montant_remonte"], errors="coerce").fillna(0)
        return out
    finally:
        if close:
            conn.close()