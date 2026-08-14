# models/reference_model.py
import sqlite3
import pandas as pd
from typing import Optional, Set, Dict, Any
from models.db import get_connection
from utils.helpers import clean_phone


def get_all_commerciaux(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Retourne le référentiel commercial sous forme de DataFrame."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        query = """
        SELECT ccial_msisdn AS Ccial_MSISDN, nom_ccial AS Nom_Ccial,
               zone_centre AS Zone_Centre, zone_territoire AS Zone_Territoire,
               zone_sa AS Zone_SA, zone_sa AS Zone_SA_Normalisee
        FROM referentiel_commerciaux
        """
        return pd.read_sql_query(query, conn)
    finally:
        if close:
            conn.close()


def get_exclusions(conn: Optional[sqlite3.Connection] = None, category: Optional[str] = None) -> pd.DataFrame:
    """Retourne le référentiel des exclusions (comptes internes)."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        if category:
            query = "SELECT msisdn, category, label, territoire, localisation FROM exclusions_reference WHERE category = ?"
            return pd.read_sql_query(query, conn, params=(category,))
        else:
            query = "SELECT msisdn, category, label, territoire, localisation FROM exclusions_reference"
            return pd.read_sql_query(query, conn)
    finally:
        if close:
            conn.close()


def get_global_excluded_numbers(conn: Optional[sqlite3.Connection] = None, categories: Optional[list[str]] = None) -> Set[str]:
    """Retourne l'ensemble unifié des numéros exclus (comptes internes)."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cursor = conn.cursor()
        if categories:
            placeholders = ",".join(["?"] * len(categories))
            query = f"SELECT DISTINCT msisdn FROM exclusions_reference WHERE category IN ({placeholders})"
            cursor.execute(query, categories)
        else:
            query = "SELECT DISTINCT msisdn FROM exclusions_reference"
            cursor.execute(query)
        return {r["msisdn"] for r in cursor.fetchall() if r["msisdn"]}
    finally:
        if close:
            conn.close()


def resolve_agent_zone(agent_number: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """
    Résout centre/territoire/zone/secteur pour un numéro d'agent ou de commercial.
    Fusionné depuis services/reference_service.py (recherche commercial puis PDV).
    """
    number = clean_phone(agent_number)
    if not number:
        return {}

    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT ccial_msisdn, nom_ccial, zone_centre, zone_territoire, zone_sa "
            "FROM referentiel_commerciaux WHERE ccial_msisdn = ?",
            (number,),
        )
        comm_row = cursor.fetchone()
        if comm_row:
            return {
                "type": "commercial",
                "Zone_Centre": comm_row["zone_centre"],
                "Zone_Territoire": comm_row["zone_territoire"],
                "Zone_SA": comm_row["zone_sa"],
                "Secteur": None,
                "Nom_Ccial": comm_row["nom_ccial"],
            }

        cursor.execute(
            "SELECT agent_msisdn, zone_centre, zone_territoire, zone_sa, secteur_cluster "
            "FROM referentiel_pos WHERE agent_msisdn = ?",
            (number,),
        )
        pos_row = cursor.fetchone()
        if pos_row:
            return {
                "type": "pdv",
                "Zone_Centre": pos_row["zone_centre"],
                "Zone_Territoire": pos_row["zone_territoire"],
                "Zone_SA": pos_row["zone_sa"],
                "Secteur": pos_row["secteur_cluster"],
            }

        return {}
    finally:
        if close:
            conn.close()