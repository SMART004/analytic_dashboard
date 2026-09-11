# ingestion/ingest.py
"""
Point d'entrée unique pour l'ingestion des fichiers de paramétrage et des
transactions dans SQLite. Regroupe schéma, ingestion référentielle et
ingestion transactionnelle (fusionné depuis services/ingestion.py).
"""

import hashlib
import logging
import sqlite3
import re
import unicodedata
from pathlib import Path
from typing import Optional

import pandas as pd
import polars as pl

from models.db import get_connection, execute_schema_file
from utils.config_storage import DATA_PATH
from utils.helpers import clean_phone
from utils.turso_storage import load_setting

logger = logging.getLogger(__name__)

ZONE_MAPPING = {
    "WD": "WILLY DISTRIBUTION",
    "FLASH": "ETS FLASH SERVICES",
    "LTC": "LTC",
    "PASCAL": "PASCAL SARL",
    "SHALOME": "ETS SHALOME SERVICES",
    "VICTORY": "VICTORY LIMITED",
    "SODISERV": "SODISERV SARL",
}


def normalize_zone_sa(v) -> str:
    if pd.isna(v) or str(v).strip() == "":
        return ""
    t = str(v).strip().upper()
    if t in ZONE_MAPPING:
        return ZONE_MAPPING[t]
    for k, val in ZONE_MAPPING.items():
        if k.strip().upper() == t:
            return val
    return t


def normalize_site_key(sitename) -> str | None:
    if pd.isna(sitename):
        return None
    s = str(sitename).strip().upper()
    return s if s and s != "NAN" and s != "NONE" else None


def _normalize_col_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value).strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _first_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    normalized_cols = {
        _normalize_col_name(col): col
        for col in df.columns
    }
    for col in candidates:
        if col in df.columns:
            return col
        normalized = _normalize_col_name(col)
        if normalized in normalized_cols:
            return normalized_cols[normalized]
    return None


# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------

def ingest_sites(conn: sqlite3.Connection):
    """Ingère les sites depuis sites_etoudi et zones dans la table sites."""
    sites_dict = {}

    # 1. sites_etoudi
    df_etoudi = load_setting("sites_etoudi")
    if df_etoudi is not None and not df_etoudi.empty:
        df_etoudi.columns = [str(c).strip() for c in df_etoudi.columns]
        s_col = _first_col(df_etoudi, ["SITENAME", "sitename", "Site", "Nom_Site"])
        q_col = _first_col(df_etoudi, ["quartier", "Quartier", "QUARTIER"])
        d_col = _first_col(df_etoudi, ["DSM", "dsm_name", "DSM Name", "dsm"])

        if s_col:
            for _, r in df_etoudi.iterrows():
                key = normalize_site_key(r.get(s_col))
                if key and key not in sites_dict:
                    sites_dict[key] = {
                        "site_key": key,
                        "sitename": str(r.get(s_col)).strip(),
                        "zone_new": None,
                        "territory_correct": None,
                        "isl_terr": None,
                        "quartier": str(r.get(q_col)).strip() if q_col and pd.notna(r.get(q_col)) else None,
                        "dsm_name": str(r.get(d_col)).strip() if d_col and pd.notna(r.get(d_col)) else None,
                    }

    # 2. zones
    df_zones = load_setting("zones")
    if df_zones is not None and not df_zones.empty:
        df_zones.columns = [str(c).strip() for c in df_zones.columns]
        s_col = _first_col(df_zones, ["SITENAME", "sitename", "Site"])
        z_col = _first_col(df_zones, ["ZONE", "zone_new", "Zone_New", "Zone"])
        t_col = _first_col(df_zones, ["TERRITORY CORRECT", "territory_correct", "Territory"])
        i_col = _first_col(df_zones, ["ISL_TERR", "isl_terr", "ISL Terr"])

        if s_col:
            for _, r in df_zones.iterrows():
                key = normalize_site_key(r.get(s_col))
                if key:
                    if key not in sites_dict:
                        sites_dict[key] = {
                            "site_key": key,
                            "sitename": str(r.get(s_col)).strip(),
                            "zone_new": str(r.get(z_col)).strip() if z_col and pd.notna(r.get(z_col)) else None,
                            "territory_correct": str(r.get(t_col)).strip() if t_col and pd.notna(r.get(t_col)) else None,
                            "isl_terr": str(r.get(i_col)).strip() if i_col and pd.notna(r.get(i_col)) else None,
                            "quartier": None,
                            "dsm_name": None,
                        }
                    else:
                        if z_col and pd.notna(r.get(z_col)):
                            sites_dict[key]["zone_new"] = str(r.get(z_col)).strip()
                        if t_col and pd.notna(r.get(t_col)):
                            sites_dict[key]["territory_correct"] = str(r.get(t_col)).strip()
                        if i_col and pd.notna(r.get(i_col)):
                            sites_dict[key]["isl_terr"] = str(r.get(i_col)).strip()

    cursor = conn.cursor()
    for item in sites_dict.values():
        cursor.execute(
            """
            INSERT INTO sites (site_key, sitename, zone_new, territory_correct, isl_terr, quartier, dsm_name)
            VALUES (:site_key, :sitename, :zone_new, :territory_correct, :isl_terr, :quartier, :dsm_name)
            ON CONFLICT(site_key) DO UPDATE SET
                zone_new=COALESCE(excluded.zone_new, sites.zone_new),
                territory_correct=COALESCE(excluded.territory_correct, sites.territory_correct),
                isl_terr=COALESCE(excluded.isl_terr, sites.isl_terr),
                quartier=COALESCE(excluded.quartier, sites.quartier),
                dsm_name=COALESCE(excluded.dsm_name, sites.dsm_name)
            """,
            item,
        )
    conn.commit()
    logger.info(f"Ingested {len(sites_dict)} sites into SQLite.")


def ingest_referentiel_pos(conn: sqlite3.Connection):
    """Ingère les PDV/agents depuis maitre_pos et maitre_pos_III."""
    cursor = conn.cursor()

    sources = [
        ("maitre_pos", "Centre II"),
        ("maitre_pos_III", "Centre III"),
    ]

    total_inserted = 0
    for folder_name, default_centre in sources:
        df = load_setting(folder_name)
        if df is None or df.empty:
            continue

        df.columns = [str(c).strip() for c in df.columns]
        msisdn_col = _first_col(df, ["agent_msisdn", "MSISDN", "Agent MSISDN", "Agent_MSISDN"])
        name_col = _first_col(df, ["full_name", "Full Name", "Agent Name", "Nom", "agent_name"])
        centre_col = _first_col(df, ["zone", "Zone", "Zone_Centre", "Centre"])
        terr_col = _first_col(df, ["territory", "Territory", "Zone_Territoire", "TERRITOIRE"])
        sa_col = _first_col(df, ["sa_incharge", "Zone_SA", "zone_sa", "SA"])
        cluster_col = _first_col(df, ["quartier", "Cluster", "cluster", "Secteur", "Locality"])
        site_col = _first_col(df, ["sitename", "Sitename", "Site", "site_id"])
        group_col = _first_col(df, ["segment_group", "Segment", "Segment_Group"])
        day_t_col = _first_col(df, ["day_target", "Day Target", "Target"])
        oos_t_col = _first_col(df, ["oos_target", "OOS Target"])

        if not msisdn_col:
            continue

        for _, r in df.iterrows():
            clean_num = clean_phone(r.get(msisdn_col))
            if not clean_num:
                continue

            site_key = normalize_site_key(r.get(site_col)) if site_col else None
            if site_key and pd.notna(r.get(site_col)):
                cursor.execute(
                    "INSERT OR IGNORE INTO sites (site_key, sitename) VALUES (?, ?)",
                    (site_key, str(r.get(site_col)).strip()),
                )

            zone_centre = str(r.get(centre_col)).strip() if centre_col and pd.notna(r.get(centre_col)) else default_centre
            zone_terr = str(r.get(terr_col)).strip() if terr_col and pd.notna(r.get(terr_col)) else None
            zone_sa = normalize_zone_sa(r.get(sa_col)) if sa_col else None
            full_name = str(r.get(name_col)).strip() if name_col and pd.notna(r.get(name_col)) else None
            secteur = str(r.get(cluster_col)).strip() if cluster_col and pd.notna(r.get(cluster_col)) else None
            segment = str(r.get(group_col)).strip() if group_col and pd.notna(r.get(group_col)) else None

            day_target = pd.to_numeric(r.get(day_t_col), errors="coerce") if day_t_col else 0.0
            oos_target = pd.to_numeric(r.get(oos_t_col), errors="coerce") if oos_t_col else 0.0

            cursor.execute(
                """
                INSERT INTO referentiel_pos (
                    agent_msisdn, source_master, full_name, zone_centre,
                    zone_territoire, zone_sa, secteur_cluster, site_key,
                    segment_group, day_target, oos_target
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_msisdn, source_master) DO UPDATE SET
                    full_name=COALESCE(excluded.full_name, referentiel_pos.full_name),
                    zone_centre=COALESCE(excluded.zone_centre, referentiel_pos.zone_centre),
                    zone_territoire=COALESCE(excluded.zone_territoire, referentiel_pos.zone_territoire),
                    zone_sa=COALESCE(excluded.zone_sa, referentiel_pos.zone_sa),
                    secteur_cluster=COALESCE(excluded.secteur_cluster, referentiel_pos.secteur_cluster),
                    site_key=COALESCE(excluded.site_key, referentiel_pos.site_key),
                    segment_group=COALESCE(excluded.segment_group, referentiel_pos.segment_group),
                    day_target=COALESCE(excluded.day_target, referentiel_pos.day_target),
                    oos_target=COALESCE(excluded.oos_target, referentiel_pos.oos_target)
                """,
                (
                    clean_num,
                    folder_name,
                    full_name,
                    zone_centre,
                    zone_terr,
                    zone_sa,
                    secteur,
                    site_key,
                    segment,
                    float(day_target) if pd.notna(day_target) else 0.0,
                    float(oos_target) if pd.notna(oos_target) else 0.0,
                ),
            )
            total_inserted += 1

    conn.commit()
    logger.info(f"Ingested {total_inserted} POS/agents into referentiel_pos.")


def ingest_referentiel_commerciaux(conn: sqlite3.Connection):
    """Ingère le référentiel commercial depuis le paramétrage 'commerciaux'."""
    df = load_setting("commerciaux")
    if df is None or df.empty:
        return

    df.columns = [str(c).strip() for c in df.columns]
    msisdn_col = _first_col(df, ["Ccial_MSISDN", "Commercial_MSISDN", "MSISDN", "NUM"])
    name_col = _first_col(df, ["Nom_Ccial", "Nom", "Commercial", "Name"])
    centre_col = _first_col(df, ["Zone_Centre", "Centre_Maitre", "zone", "Centre"])
    terr_col = _first_col(df, ["Zone_Territoire", "Territoire", "territory"])
    sa_col = _first_col(df, ["Zone_SA", "zone_sa", "SA"])

    if not msisdn_col:
        return

    cursor = conn.cursor()
    count = 0
    for _, r in df.iterrows():
        clean_num = clean_phone(r.get(msisdn_col))
        if not clean_num:
            continue

        nom = str(r.get(name_col)).strip() if name_col and pd.notna(r.get(name_col)) else clean_num
        centre = str(r.get(centre_col)).strip() if centre_col and pd.notna(r.get(centre_col)) else None
        terr = str(r.get(terr_col)).strip() if terr_col and pd.notna(r.get(terr_col)) else None
        sa = normalize_zone_sa(r.get(sa_col)) if sa_col else None

        cursor.execute(
            """
            INSERT INTO referentiel_commerciaux (ccial_msisdn, nom_ccial, zone_centre, zone_territoire, zone_sa)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ccial_msisdn) DO UPDATE SET
                nom_ccial=excluded.nom_ccial,
                zone_centre=COALESCE(excluded.zone_centre, referentiel_commerciaux.zone_centre),
                zone_territoire=COALESCE(excluded.zone_territoire, referentiel_commerciaux.zone_territoire),
                zone_sa=COALESCE(excluded.zone_sa, referentiel_commerciaux.zone_sa)
            """,
            (clean_num, nom, centre, terr, sa),
        )
        count += 1

    conn.commit()
    logger.info(f"Ingested {count} commercials into referentiel_commerciaux.")


def ingest_exclusions(conn: sqlite3.Connection):
    """Ingère les exclusions (comptes internes) des 5 sources de paramétrage."""
    cursor = conn.cursor()

    mapping = [
        ("commerciaux", "commercial", ["Ccial_MSISDN", "Commercial_MSISDN", "NUM", "MSISDN"]),
        ("caisses", "caisse", ["NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]),
        ("masters", "master", ["NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]),
        ("cds", "cds", ["NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]),
        ("pos_relay_caisse", "pos_relay_caisse", ["MSISDN_PR", "NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]),
    ]

    total = 0
    for folder_name, category, phone_cols in mapping:
        df = load_setting(folder_name)
        if df is None or df.empty:
            continue

        df.columns = [str(c).strip() for c in df.columns]
        m_col = _first_col(df, phone_cols)
        label_col = _first_col(df, ["Nom_Ccial", "Nom", "Label", "Nom_Caisse", "Nom_Master", "PR_Name", "MASTER", "CAISSE", "CDS"])
        terr_col = _first_col(df, ["Zone_Territoire", "Territoire", "Territory"])
        loc_col = _first_col(df, ["Localisation", "Quartier", "Secteur", "Zone_SA"])

        if not m_col:
            continue

        for _, r in df.iterrows():
            clean_num = clean_phone(r.get(m_col))
            if not clean_num or clean_num == "None":
                continue

            label = str(r.get(label_col)).strip() if label_col and pd.notna(r.get(label_col)) else None
            terr = str(r.get(terr_col)).strip() if terr_col and pd.notna(r.get(terr_col)) else None
            loc = str(r.get(loc_col)).strip() if loc_col and pd.notna(r.get(loc_col)) else None

            cursor.execute(
                """
                INSERT INTO exclusions_reference (msisdn, category, label, territoire, localisation)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(msisdn, category) DO UPDATE SET
                    label=COALESCE(excluded.label, exclusions_reference.label),
                    territoire=COALESCE(excluded.territoire, exclusions_reference.territoire),
                    localisation=COALESCE(excluded.localisation, exclusions_reference.localisation)
                """,
                (clean_num, category, label, terr, loc),
            )
            total += 1

    conn.commit()
    logger.info(f"Ingested {total} exclusions into exclusions_reference.")


def ingest_hvc_mapping(conn: sqlite3.Connection):
    """Ingère le mapping HVC -> Commercial (nom seul accepté si pas de MSISDN)."""
    df = load_setting("hvc_commercial")
    if df is None or df.empty:
        return

    df.columns = [str(c).strip() for c in df.columns]

    hvc_col = _first_col(df, [
        "HVC_MSISDN", "HVC_ MSISDN", "hvc_msisdn",
        "MSISDN_HVC", "MSISDN", "HVC MSISDN"
    ])
    name_col = _first_col(df, [
        "Ccial en charge", "ccial_en_charge", "Nom_Ccial",
        "Commercial", "Ccial"
    ])
    ccial_col = _first_col(df, [
        "Ccial_MSISDN", "ccial_msisdn", "MSISDN_Ccial", "Commercial_MSISDN"
    ])

    if not hvc_col:
        logger.warning("Colonne HVC_MSISDN introuvable dans hvc_commercial")
        return

    cursor = conn.cursor()
    cursor.execute("DELETE FROM hvc_commercial_mapping")
    count = 0

    for _, r in df.iterrows():
        clean_hvc = clean_phone(r.get(hvc_col))
        if not clean_hvc:
            continue

        nom = None
        if name_col and pd.notna(r.get(name_col)):
            nom = str(r.get(name_col)).strip() or None

        clean_ccial = None
        if ccial_col and pd.notna(r.get(ccial_col)):
            clean_ccial = clean_phone(r.get(ccial_col)) or None

        # On n’insère dans referentiel_commerciaux que si on a un MSISDN
        if clean_ccial:
            cursor.execute(
                """
                INSERT OR IGNORE INTO referentiel_commerciaux (ccial_msisdn, nom_ccial)
                VALUES (?, ?)
                """,
                (clean_ccial, nom or clean_ccial),
            )

        # Insert mapping : ccial_msisdn peut être NULL
        cursor.execute(
            """
            INSERT INTO hvc_commercial_mapping (hvc_msisdn, ccial_msisdn, ccial_en_charge)
            VALUES (?, ?, ?)
            ON CONFLICT(hvc_msisdn) DO UPDATE SET
                ccial_msisdn    = excluded.ccial_msisdn,
                ccial_en_charge = excluded.ccial_en_charge
            """,
            (clean_hvc, clean_ccial, nom),
        )
        count += 1

    conn.commit()
    logger.info(f"Ingested {count} HVC mappings (nom commercial).")


def ingest_cds_referentiel(conn: sqlite3.Connection):
    """Ingère le référentiel CDS (fichier settings 'cds' : colonnes NUM / CDS)."""
    df = load_setting("cds")
    if df is None or df.empty:
        return

    df.columns = [str(c).strip() for c in df.columns]
    num_col = _first_col(df, ["NUM", "num", "MSISDN"])
    nom_col = _first_col(df, ["CDS", "cds", "Nom", "NOM_CDS"])

    if not num_col or not nom_col:
        logger.warning("Colonnes NUM / CDS introuvables dans le paramétrage cds.")
        return

    cursor = conn.cursor()
    cursor.execute("DELETE FROM hvc_cds_assignments")
    count = 0
    for _, r in df.iterrows():
        clean_num = clean_phone(r.get(num_col))
        nom_cds = str(r.get(nom_col)).strip() if pd.notna(r.get(nom_col)) else None
        if not clean_num or not nom_cds:
            continue

        cursor.execute(
            """
            INSERT INTO cds_referentiel (cds_msisdn, nom_cds)
            VALUES (?, ?)
            ON CONFLICT(cds_msisdn) DO UPDATE SET nom_cds=excluded.nom_cds
            """,
            (clean_num, nom_cds),
        )
        count += 1

    conn.commit()
    logger.info(f"Ingested {count} entries into cds_referentiel.")


def ingest_point_relay_referentiel(conn: sqlite3.Connection):
    """
    Ingère le référentiel Point Relais & Caisses (fichier settings
    'pos_relay_caisse' : MSISDN_PR / TERRITOIRE / Localisation /
    Nom du point de relais). Le type (Point Relais vs Caisses) est dérivé du
    préfixe "CAISSE" dans le nom, comme dans l'ancienne
    prepare_point_relay_caisse_config.
    """
    df = load_setting("pos_relay_caisse")
    if df is None or df.empty:
        return

    df.columns = [str(c).strip() for c in df.columns]
    msisdn_col = _first_col(df, ["MSISDN_PR", "msisdn_pr", "MSISDN", "NUM"])
    terr_col = _first_col(df, ["TERRITOIRE", "Territoire", "Territory"])
    loc_col = _first_col(df, ["Localisation", "localisation", "LOCALISATION"])
    nom_col = _first_col(df, ["Nom du point de relais", "Nom_Point_Relais", "Nom", "PR_Name"])

    if not msisdn_col or not nom_col:
        logger.warning("Colonnes MSISDN_PR / Nom du point de relais introuvables dans pos_relay_caisse.")
        return

    cursor = conn.cursor()
    count = 0
    for _, r in df.iterrows():
        clean_num = clean_phone(r.get(msisdn_col))
        nom = str(r.get(nom_col)).strip() if pd.notna(r.get(nom_col)) else None
        if not clean_num or not nom:
            continue

        territoire = str(r.get(terr_col)).strip() if terr_col and pd.notna(r.get(terr_col)) else "NON RENSEIGNE"
        localisation = str(r.get(loc_col)).strip() if loc_col and pd.notna(r.get(loc_col)) else "NON RENSEIGNE"
        type_point = "Caisses" if nom.upper().startswith("CAISSE") else "Point Relais"

        cursor.execute(
            """
            INSERT INTO point_relay_referentiel (msisdn_pr, nom, territoire, localisation, type_point)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(msisdn_pr) DO UPDATE SET
                nom=excluded.nom,
                territoire=excluded.territoire,
                localisation=excluded.localisation,
                type_point=excluded.type_point
            """,
            (clean_num, nom, territoire, localisation, type_point),
        )
        count += 1

    conn.commit()
    logger.info(f"Ingested {count} entries into point_relay_referentiel.")


def ingest_hvc_cds_assignments(conn: sqlite3.Connection):
    """
    Ingère le quota HVC attribué à chaque CDS depuis le paramétrage 'hvc_cds'
    (colonnes NUM_HVC / NOM_CDS). Un HVC ne peut être assigné qu'à un seul CDS ;
    un re-upload du fichier remplace l'assignation existante pour ce HVC.
    """
    df = load_setting("hvc_cds")
    if df is None or df.empty:
        return

    df.columns = [str(c).strip() for c in df.columns]
    hvc_col = _first_col(df, ["NUM_HVC", "num_hvc", "HVC_MSISDN", "hvc_msisdn", "MSISDN_HVC"])
    cds_col = _first_col(df, ["NOM_CDS", "nom_cds", "CDS", "cds"])

    if not hvc_col or not cds_col:
        logger.warning("Colonnes NUM_HVC / NOM_CDS introuvables dans le paramétrage hvc_cds.")
        return

    cursor = conn.cursor()
    count = 0
    for _, r in df.iterrows():
        clean_hvc = clean_phone(r.get(hvc_col))
        nom_cds = str(r.get(cds_col)).strip() if pd.notna(r.get(cds_col)) else None
        if not clean_hvc or not nom_cds:
            continue

        cursor.execute(
            """
            INSERT INTO hvc_cds_assignments (hvc_msisdn, cds_nom)
            VALUES (?, ?)
            ON CONFLICT(hvc_msisdn) DO UPDATE SET
                cds_nom=excluded.cds_nom
            """,
            (clean_hvc, nom_cds),
        )
        count += 1

    conn.commit()
    logger.info(f"Ingested {count} HVC->CDS assignments into hvc_cds_assignments.")


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def _load_reference_lookups(conn: sqlite3.Connection):
    """Charge les maps de correspondance en mémoire pour le calcul du snapshot."""
    cursor = conn.cursor()

    comm_map = {}
    for row in cursor.execute("SELECT ccial_msisdn, zone_sa, zone_territoire FROM referentiel_commerciaux"):
        comm_map[row["ccial_msisdn"]] = (row["zone_sa"], row["zone_territoire"])

    pos_map = {}
    for row in cursor.execute("SELECT agent_msisdn, zone_sa, zone_territoire, site_key FROM referentiel_pos"):
        pos_map[row["agent_msisdn"]] = (row["zone_sa"], row["zone_territoire"], row["site_key"])

    return comm_map, pos_map


def _ingest_transaction_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    source_filename: str,
    content_hash: str,
) -> int:
    """
    Cœur d'insertion partagé par ingest_transaction_file (fichiers locaux) et
    ingest_transaction_dataframe (DataFrames déjà chargés depuis un bucket
    le stockage de fichiers, cf. ingestion/upload_sync.py). Gèle le snapshot zone/territoire
    au moment de l'ingestion, comme avant.
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    date_col = _first_col(df, ["Date", "tx_date", "Date_Time", "Timestamp"])
    type_col = _first_col(df, ["Type", "tx_type", "Transaction Type", "TYPE"])
    amt_col = _first_col(df, ["Amount", "amount", "Montant", "Value"])
    from_col = _first_col(df, ["From", "from_msisdn", "From_MSISDN", "Sender"])
    to_col = _first_col(df, ["To", "to_msisdn", "To_MSISDN", "Receiver"])
    from_n_col = _first_col(df, ["From Name", "From name", "From_Name", "from_name"])
    to_n_col = _first_col(df, ["To Name", "To name", "To_Name", "to_name"])
    bal_col = _first_col(df, ["Balance", "balance", "Solde"])

    if not date_col or not amt_col:
        return 0

    comm_map, pos_map = _load_reference_lookups(conn)
    cursor = conn.cursor()

    type_map = {
        "cash out": "Cash out",
        "cash_out": "Cash out",
        "cash in": "Cash in",
        "cash_in": "Cash in",
        "transfer": "Transfer",
    }

    records = []

    for _, r in df.iterrows():
        dt_val = pd.to_datetime(r.get(date_col), errors="coerce")
        if pd.isna(dt_val):
            continue

        tx_date = dt_val.strftime("%Y-%m-%d %H:%M:%S")
        date_only = dt_val.strftime("%Y-%m-%d")
        hour = dt_val.hour

        raw_type = str(r.get(type_col, "")).strip() if type_col else ""
        tx_type = type_map.get(raw_type.lower(), raw_type)

        amount = abs(float(pd.to_numeric(r.get(amt_col), errors="coerce") or 0.0))
        from_msisdn = clean_phone(r.get(from_col)) if from_col else None
        to_msisdn = clean_phone(r.get(to_col)) if to_col else None
        from_name = str(r.get(from_n_col)).strip() if from_n_col and pd.notna(r.get(from_n_col)) else None
        to_name = str(r.get(to_n_col)).strip() if to_n_col and pd.notna(r.get(to_n_col)) else None
        balance = float(pd.to_numeric(r.get(bal_col), errors="coerce")) if bal_col and pd.notna(r.get(bal_col)) else None

        # Résolution du snapshot :
        # Priorité 1 : commercial from_msisdn
        # Priorité 2 : agent to_msisdn
        zone_sa_snap = None
        terr_snap = None
        site_key_snap = None

        if from_msisdn and from_msisdn in comm_map:
            zone_sa_snap, terr_snap = comm_map[from_msisdn]

        if not zone_sa_snap and to_msisdn and to_msisdn in pos_map:
            zone_sa_snap, terr_snap, site_key_snap = pos_map[to_msisdn]

        if not site_key_snap and to_msisdn and to_msisdn in pos_map:
            site_key_snap = pos_map[to_msisdn][2]

        records.append((
            tx_date, date_only, hour, tx_type, amount,
            from_msisdn, to_msisdn, from_name, to_name, balance,
            zone_sa_snap, terr_snap, site_key_snap, source_filename, content_hash,
        ))

    if records:
        cursor.executemany(
            """
            INSERT OR IGNORE INTO transactions (
                tx_date, date_only, hour, tx_type, amount,
                from_msisdn, to_msisdn, from_name, to_name, balance,
                zone_sa_snapshot, territoire_snapshot, site_key_snapshot, source_file, file_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        conn.commit()

    return len(records)


def ingest_transaction_file(conn: sqlite3.Connection, file_path: Path) -> int:
    """Ingère un fichier de transactions unique (chemin local) avec gel du snapshot.
    Utilise Polars+calamine pour la lecture Excel (plus rapide que pandas)."""
    if not file_path.exists():
        return 0

    suffix = file_path.suffix.lower()
    raw_bytes = file_path.read_bytes()
    try:
        if suffix == ".csv":
            df_pl = pl.read_csv(file_path, infer_schema_length=0)
        else:
            df_pl = pl.read_excel(file_path, engine="calamine")
        df = df_pl.to_pandas()
    except Exception:
        # Fallback pandas si Polars échoue (format non supporté)
        if suffix == ".csv":
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)

    if df is None or df.empty:
        return 0

    content_hash = hashlib.md5(raw_bytes).hexdigest()
    return _ingest_transaction_dataframe(conn, df, file_path.name, content_hash)


def ingest_transaction_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    source_filename: str,
) -> int:
    """
    Ingère un DataFrame de transactions déjà chargé en mémoire (cas des 3
    buckets de performance — bloquant 3, Tâche 5). Le fichier
    source n'existe jamais sur disque local ici : le hash est calculé sur le
    contenu du DataFrame plutôt que sur des octets de fichier, à titre
    purement informatif (traçabilité de source_file/file_hash) — la
    déduplication réelle reste garantie par la contrainte UNIQUE
    (tx_date, from_msisdn, to_msisdn, amount, tx_type) de la table
    transactions, indépendante de la valeur exacte du hash.
    """
    if df is None or df.empty:
        return 0

    content_hash = hashlib.md5(
        pd.util.hash_pandas_object(df, index=False).values.tobytes()
    ).hexdigest()
    return _ingest_transaction_dataframe(conn, df, source_filename, content_hash)


def ingest_all_transactions(conn: sqlite3.Connection, folder_path: Path | str | None = None):
    """Ingère tous les fichiers de transactions du dossier conquete_transactions."""
    if folder_path is None:
        folder_path = DATA_PATH / "conquete_transactions"
    else:
        folder_path = Path(folder_path)

    if not folder_path.exists():
        logger.warning(f"Transactions directory {folder_path} does not exist.")
        return

    files = list(folder_path.glob("*.csv")) + list(folder_path.glob("*.xlsx"))
    total_files = len(files)
    total_tx = 0
    for f in files:
        total_tx += ingest_transaction_file(conn, f)

    logger.info(f"Ingested {total_tx} transactions from {total_files} files.")

def update_pos_targets_from_file(df: pd.DataFrame, conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Met à jour oos_target et day_target dans referentiel_pos
    en mappant sur agent_msisdn (clean_phone).

    Si le même MSISDN existe pour maitre_pos ET maitre_pos_III,
    les deux lignes sont mises à jour.
    """
    if df is None or df.empty:
        return {"updated": 0, "not_found": 0, "rows_file": 0}

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    msisdn_col = _first_col(df, [
        "MSISDN", "agent_msisdn", "Agent MSISDN", "msisdn",
        "NUM", "phone", "Telephone", "Téléphone",
    ])
    oos_col = _first_col(df, [
        "oos_target", "OOS_target", "OOS target", "OOS Target",
        "oos target", "OOS_TARGET", "Oos_Target",
    ])
    day_col = _first_col(df, [
        "day_target", "Day_Target", "Day Target", "DAY_TARGET",
        "day target", "DayTarget",
    ])

    if not msisdn_col:
        raise ValueError(
            "Colonne MSISDN introuvable. "
            "Variantes : MSISDN, agent_msisdn, Agent MSISDN, NUM…"
        )
    if not oos_col and not day_col:
        raise ValueError("Aucune colonne oos_target ni day_target trouvée.")

    work = pd.DataFrame()
    work["msisdn_clean"] = df[msisdn_col].apply(clean_phone)
    work = work[work["msisdn_clean"].notna() & (work["msisdn_clean"] != "")]

    if oos_col:
        work["oos_target"] = pd.to_numeric(df.loc[work.index, oos_col], errors="coerce")
    if day_col:
        work["day_target"] = pd.to_numeric(df.loc[work.index, day_col], errors="coerce")

    work = work.drop_duplicates(subset=["msisdn_clean"], keep="last")

    close = False
    if conn is None:
        conn = get_connection()
        close = True

    try:
        cursor = conn.cursor()
        updated = 0
        not_found = 0

        for _, row in work.iterrows():
            msisdn = row["msisdn_clean"]
            sets = []
            params = []

            if "oos_target" in work.columns and pd.notna(row.get("oos_target")):
                sets.append("oos_target = ?")
                params.append(float(row["oos_target"]))

            if "day_target" in work.columns and pd.notna(row.get("day_target")):
                sets.append("day_target = ?")
                params.append(float(row["day_target"]))

            if not sets:
                continue

            # Met à jour TOUTES les lignes de ce MSISDN
            # (maitre_pos et/ou maitre_pos_III)
            sql = f"""
                UPDATE referentiel_pos
                SET {', '.join(sets)}
                WHERE agent_msisdn = ?
            """
            params.append(msisdn)
            cursor.execute(sql, params)

            if cursor.rowcount > 0:
                updated += cursor.rowcount
            else:
                not_found += 1

        conn.commit()
        return {
            "updated": updated,
            "not_found": not_found,
            "rows_file": len(work),
        }
    finally:
        if close:
            conn.close()

def migrate_hvc_commercial_mapping(conn: sqlite3.Connection) -> None:
    """
    Rend la colonne ccial_msisdn nullable dans hvc_commercial_mapping.
    Idempotente : peut être appelée plusieurs fois sans risque.
    """
    cursor = conn.cursor()

    # Vérifier si la table existe
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hvc_commercial_mapping'"
    )
    if not cursor.fetchone():
        # Table absente → on la crée déjà correcte
        cursor.execute("""
            CREATE TABLE hvc_commercial_mapping (
                hvc_msisdn      TEXT PRIMARY KEY,
                ccial_msisdn    TEXT,          -- nullable
                ccial_en_charge TEXT
            )
        """)
        conn.commit()
        return

    # Vérifier si ccial_msisdn est encore NOT NULL
    cursor.execute("PRAGMA table_info(hvc_commercial_mapping)")
    columns = {row[1]: row for row in cursor.fetchall()}
    # row = (cid, name, type, notnull, dflt_value, pk)
    ccial_info = columns.get("ccial_msisdn")
    if ccial_info is None:
        # Colonne absente → on la rajoute
        cursor.execute("ALTER TABLE hvc_commercial_mapping ADD COLUMN ccial_msisdn TEXT")
        conn.commit()
        return

    notnull = ccial_info[3]  # 1 = NOT NULL, 0 = nullable
    if notnull == 0:
        # Déjà nullable → rien à faire
        return

    # ========== Migration nécessaire ==========
    conn.execute("PRAGMA foreign_keys = OFF")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hvc_commercial_mapping_new (
            hvc_msisdn      TEXT PRIMARY KEY,
            ccial_msisdn    TEXT,          -- nullable
            ccial_en_charge TEXT
        )
    """)

    # Copier les données existantes
    cursor.execute("""
        INSERT OR IGNORE INTO hvc_commercial_mapping_new
            (hvc_msisdn, ccial_msisdn, ccial_en_charge)
        SELECT hvc_msisdn, ccial_msisdn, ccial_en_charge
        FROM hvc_commercial_mapping
    """)

    cursor.execute("DROP TABLE hvc_commercial_mapping")
    cursor.execute("ALTER TABLE hvc_commercial_mapping_new RENAME TO hvc_commercial_mapping")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_referentiel_ingestion(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """
    Ingère tous les référentiels (sites, POS, commerciaux, exclusions, CDS,
    Point Relais/Caisses, mapping HVC, quota HVC/CDS) depuis les fichiers
    settings courants — TOUT SAUF les transactions (volumineuses, gérées à
    part par sync_segment_to_sqlite / ingest_all_transactions).

    Pensée pour être appelée à chaque upload réussi dans pages/settings.py :
    idempotente (ON CONFLICT DO UPDATE / INSERT OR IGNORE partout), donc sans
    risque de duplication si rappelée plusieurs fois. C'est le chaînon qui
    manquait entre "fichier settings uploadé vers le stockage distant" et "table
    SQLite à jour" — avant ce correctif, aucune page ne relisait plus les
    settings en direct, mais rien ne les poussait non plus vers SQLite après
    upload.
    """
    close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        logger.info("Ingesting sites...")
        ingest_sites(conn)

        logger.info("Ingesting referentiel_pos...")
        ingest_referentiel_pos(conn)

        logger.info("Ingesting referentiel_commerciaux...")
        ingest_referentiel_commerciaux(conn)

        logger.info("Ingesting exclusions_reference...")
        ingest_exclusions(conn)

        logger.info("Ingesting cds_referentiel...")
        ingest_cds_referentiel(conn)

        logger.info("Ingesting point_relay_referentiel...")
        ingest_point_relay_referentiel(conn)

        logger.info("Ingesting hvc_commercial_mapping...")
        migrate_hvc_commercial_mapping(conn)
        ingest_hvc_mapping(conn)

        logger.info("Ingesting hvc_cds_assignments (quota HVC par CDS)...")
        ingest_hvc_cds_assignments(conn)

        counts = {
            "sites": conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0],
            "referentiel_pos": conn.execute("SELECT COUNT(*) FROM referentiel_pos").fetchone()[0],
            "referentiel_commerciaux": conn.execute("SELECT COUNT(*) FROM referentiel_commerciaux").fetchone()[0],
            "exclusions_reference": conn.execute("SELECT COUNT(*) FROM exclusions_reference").fetchone()[0],
            "cds_referentiel": conn.execute("SELECT COUNT(*) FROM cds_referentiel").fetchone()[0],
            "point_relay_referentiel": conn.execute("SELECT COUNT(*) FROM point_relay_referentiel").fetchone()[0],
            "hvc_commercial_mapping": conn.execute("SELECT COUNT(*) FROM hvc_commercial_mapping").fetchone()[0],
            "hvc_cds_assignments": conn.execute("SELECT COUNT(*) FROM hvc_cds_assignments").fetchone()[0],
        }
        logger.info(f"Referentiel ingestion completed: {counts}")
        return counts
    finally:
        if close:
            conn.close()


def run_ingestion(db_path: str | Path | None = None):
    """Exécute le pipeline complet d'ingestion dans SQLite (référentiels + transactions)."""
    conn = get_connection(db_path)
    try:
        logger.info("Initializing SQLite v1.1 schema...")
        execute_schema_file(conn)

        run_referentiel_ingestion(conn)

        logger.info("Ingesting transaction files...")
        ingest_all_transactions(conn)

        logger.info("Ingestion completed successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_ingestion()
