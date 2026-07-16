import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
from io import BytesIO

from utils.storage import upload_file, get_all_files
from utils.helpers import clean_phone
from utils.supabase import load_setting

# ─────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────
BUCKET_TRANSACTIONS = "conquete_transactions"

TRANSACTION_TYPES_ENTRANTS = ["Cash in", "cash in", "CASH IN", "Cash In"]
TRANSACTION_TYPES_SORTANTS = ["Cash out", "cash out", "CASH OUT", "Cash Out"]
TRANSACTION_TYPES_TRANSFER = ["Transfer", "transfer", "TRANSFER"]

COULEURS_CARTE = {
    "touche":     "#22c55e",
    "non_touche": "#ef4444",
    "nouveau":    "#f97316",
    "perdu":      "#6b7280",
    "hors_zone":  "#3b82f6",
}

ZONE_MAPPING = {
    "WD":       "WILLY DISTRIBUTION",
    "FLASH":    "ETS FLASH SERVICES",
    "LTC":      "LTC",
    "PASCAL":   "PASCAL SARL",
    "SHALOME":  "ETS SHALOME SERVICES",
    "VICTORY":  "VICTORY LIMITED",
    "SODISERV": "SODISERV SARL",
}

# Zones appartenant au Centre III
# (utilisé pour inclure PASCAL même sans PDV dans maitre_pos_III)
ZONES_CENTRE_III = {"PASCAL SARL"}


# ─────────────────────────────────────────────
# OUTILS INTERNES
# ─────────────────────────────────────────────

def _normalize_zone_sa(v) -> str:
    if pd.isna(v) or str(v).strip() == "":
        return ""
    t = str(v).strip().upper()
    return ZONE_MAPPING.get(t, t)

def _safe_df(df) -> pd.DataFrame:
    return df.copy() if isinstance(df, pd.DataFrame) and not df.empty else pd.DataFrame()

def _pick_col(df: pd.DataFrame, candidates: list) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None

def _flux_amount(tx: pd.DataFrame, types: list) -> float:
    if tx.empty or "Type" not in tx.columns or "Amount" not in tx.columns:
        return 0.0
    mask = tx["Type"].astype(str).str.strip().str.upper().isin([t.upper() for t in types])
    return float(tx.loc[mask, "Amount"].sum())

def _safe_setting_df(name: str) -> pd.DataFrame:
    df = load_setting(name)
    return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()


# ─────────────────────────────────────────────
# CHARGEMENT & PRÉPARATION DES RÉFÉRENTIELS
# ─────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=300)
def load_transactions() -> pd.DataFrame:
    df = get_all_files(BUCKET_TRANSACTIONS)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    if "Amount" in df.columns:
        df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0).abs()
    if "Type" in df.columns:
        df["Type"] = df["Type"].astype(str).str.strip()
    for col in ["From", "To"]:
        if col in df.columns:
            df[col] = df[col].apply(clean_phone)
    return df


def _prepare_pdv(df: pd.DataFrame, centre_label: str) -> pd.DataFrame:
    df = _safe_df(df)
    if df.empty:
        return df
    df["zone"] = centre_label
    if "agent_msisdn" in df.columns:
        df["agent_msisdn"] = df["agent_msisdn"].apply(clean_phone)
    # Zone_SA_Normalisee depuis sa_incharge (prioritaire) ou zone
    if "sa_incharge" in df.columns:
        df["Zone_SA_Normalisee"] = df["sa_incharge"].apply(_normalize_zone_sa)
    elif "zone" in df.columns:
        df["Zone_SA_Normalisee"] = df["zone"].apply(_normalize_zone_sa)
    for c in ["territory", "quartier", "sitename", "site_id", "segment_group"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df


def _prepare_commerciaux(df: pd.DataFrame) -> pd.DataFrame:
    df = _safe_df(df)
    if df.empty:
        return df
    if "Ccial_MSISDN" in df.columns:
        df["Ccial_MSISDN"] = df["Ccial_MSISDN"].apply(clean_phone)
    if "Zone_SA" in df.columns:
        df["Zone_SA_Normalisee"] = df["Zone_SA"].apply(_normalize_zone_sa)
    if "Nom_Ccial" in df.columns:
        df["Nom_Ccial"] = df["Nom_Ccial"].astype(str).str.strip()
    return df


def charger_referentiels() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Retourne (pdv, comm) prêts à l'emploi.
    pdv contient zone, Zone_SA_Normalisee, agent_msisdn (nettoyé).
    comm contient Ccial_MSISDN (nettoyé), Zone_SA_Normalisee, Nom_Ccial, Zone_Centre.
    """
    mp2  = _prepare_pdv(_safe_setting_df("maitre_pos"),     "Centre II")
    mp3  = _prepare_pdv(_safe_setting_df("maitre_pos_III"), "Centre III")
    pdv  = pd.concat([mp2, mp3], ignore_index=True).drop_duplicates()

    comm = _prepare_commerciaux(_safe_setting_df("commerciaux"))
    return pdv, comm


# ─────────────────────────────────────────────
# LOGIQUE DES ZONES PAR CENTRE
# ─────────────────────────────────────────────

def _zones_pour_centre(centre: str, pdv: pd.DataFrame, comm: pd.DataFrame) -> list[str]:
    """
    Retourne la liste triée des Zone_SA_Normalisee pour un centre donné.
    - Les zones viennent des PDV ET des commerciaux (pour inclure PASCAL Centre III
      même sans PDV dans maitre_pos_III).
    - Si centre == "Tous", retourne toutes les zones.
    """
    zones = set()

    if centre == "Tous" or not centre:
        # Toutes les zones PDV
        if not pdv.empty and "Zone_SA_Normalisee" in pdv.columns:
            zones.update(pdv["Zone_SA_Normalisee"].dropna().unique())
        # Toutes les zones commerciaux
        if not comm.empty and "Zone_SA_Normalisee" in comm.columns:
            zones.update(comm["Zone_SA_Normalisee"].dropna().unique())
    else:
        # Zones des PDV du centre
        if not pdv.empty and "Zone_SA_Normalisee" in pdv.columns and "zone" in pdv.columns:
            zones.update(
                pdv.loc[pdv["zone"] == centre, "Zone_SA_Normalisee"].dropna().unique()
            )
        # Zones des commerciaux du centre (clé: Zone_Centre sur comm si dispo,
        # sinon on cherche les zones commerciaux qui ont des PDV dans ce centre)
        if not comm.empty and "Zone_SA_Normalisee" in comm.columns:
            if "Zone_Centre" in comm.columns:
                zones.update(
                    comm.loc[comm["Zone_Centre"] == centre, "Zone_SA_Normalisee"].dropna().unique()
                )
            else:
                # Fallback : inclure les zones connues du Centre III (PASCAL, etc.)
                if "III" in str(centre):
                    zones.update(ZONES_CENTRE_III)

    return sorted(z for z in zones if z)


# ─────────────────────────────────────────────
# ENRICHISSEMENT DES TRANSACTIONS
# ─────────────────────────────────────────────

def enrichir_transactions(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
) -> pd.DataFrame:
    """
    Ajoute sur chaque transaction :
      - To_clean       : MSISDN PDV normalisé
      - From_clean     : MSISDN commercial normalisé
      - Zone_SA_PDV    : zone du PDV (depuis le référentiel PDV)
      - Centre_PDV     : centre du PDV
      - Territoire_PDV / Quartier_PDV / Sitename_PDV / Segment_PDV
      - Zone_SA_Comm   : zone du commercial (depuis le fichier commerciaux)
      - Centre_Comm    : centre du commercial
      - Commercial     : nom du commercial
    """
    if tx is None or tx.empty:
        return pd.DataFrame()

    result = tx.copy()
    result["From_clean"] = result["From"].apply(clean_phone) if "From" in result.columns else ""
    result["To_clean"]   = result["To"].apply(clean_phone)   if "To"   in result.columns else ""

    # ── Côté PDV (To_clean) ──
    if not pdv.empty and "agent_msisdn" in pdv.columns:
        pdv_sub = pdv.copy()
        pdv_sub["_key"] = pdv_sub["agent_msisdn"].apply(clean_phone)

        rename = {
            "Zone_SA_Normalisee": "Zone_SA_PDV",
            "zone":      "Centre_PDV",
            "territory":          "Territoire_PDV",
            "quartier":           "Quartier_PDV",
            "sitename":           "Sitename_PDV",
            "site_id":            "Site_ID_PDV",
            "segment_group":      "Segment_PDV",
        }
        keep = ["_key"] + [c for c in rename if c in pdv_sub.columns]
        pdv_sub = (
            pdv_sub[keep]
            .rename(columns={**rename, "_key": "To_clean"})
            .drop_duplicates(subset=["To_clean"], keep="first")
        )
        result = result.merge(pdv_sub, on="To_clean", how="left")

    # ── Côté commercial (From_clean) ──
    if not comm.empty and "Ccial_MSISDN" in comm.columns:
        comm_sub = comm.copy()
        comm_sub["_key"] = comm_sub["Ccial_MSISDN"].apply(clean_phone)

        rename_c = {
            "Zone_SA_Normalisee": "Zone_SA_Comm",
            "Nom_Ccial":          "Commercial",
        }
        if "Zone_Centre" in comm_sub.columns:
            rename_c["Zone_Centre"] = "Centre_Comm"
        if "Zone_Territoire" in comm_sub.columns:
            rename_c["Zone_Territoire"] = "Territoire_Comm"

        keep_c = ["_key"] + [c for c in rename_c if c in comm_sub.columns]
        comm_sub = (
            comm_sub[keep_c]
            .rename(columns={**rename_c, "_key": "From_clean"})
            .drop_duplicates(subset=["From_clean"], keep="first")
        )
        result = result.merge(comm_sub, on="From_clean", how="left")

    # Garantir l'existence des colonnes clés
    for col in ["Zone_SA_PDV", "Zone_SA_Comm", "Centre_PDV", "Centre_Comm", "Commercial"]:
        if col not in result.columns:
            result[col] = np.nan

    return result


# ─────────────────────────────────────────────
# FILTRES
# ─────────────────────────────────────────────

def render_filtres(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
) -> dict:
    st.sidebar.markdown("## 🔍 Filtres")
    filtres = {}

    # ── Date ──
    if not tx.empty and "Date" in tx.columns:
        dates = tx["Date"].dropna()
        if not dates.empty:
            min_d, max_d = dates.min().date(), dates.max().date()
            filtres["date_range"] = st.sidebar.date_input(
                "Période",
                value=(max(min_d, max_d - timedelta(days=6)), max_d),
                min_value=min_d, max_value=max_d,
            )
        else:
            filtres["date_range"] = None
    else:
        filtres["date_range"] = None

    # ── Centre ──
    centres = ["Tous"]
    if not pdv.empty and "zone" in pdv.columns:
        centres += sorted(pdv["zone"].dropna().unique().tolist())
    default_idx = centres.index("Centre II") if "Centre II" in centres else 0
    filtres["centre"] = st.sidebar.selectbox("Centre", centres, index=default_idx)

    # ── Zone_SA — dépend du centre sélectionné ──
    zones_disponibles = _zones_pour_centre(filtres["centre"], pdv, comm)
    filtres["zones_sa"] = st.sidebar.multiselect(
        "Zone_SA", zones_disponibles, default=zones_disponibles
    )

    # ── Commercial — filtré par zones sélectionnées ──
    # if not comm.empty and "Ccial_MSISDN" in comm.columns and "Zone_SA_Normalisee" in comm.columns:
    #     comm_filt = comm[comm["Zone_SA_Normalisee"].isin(filtres["zones_sa"])] if filtres["zones_sa"] else comm
    #     opts_comm = sorted(comm_filt["Ccial_MSISDN"].dropna().unique().tolist())
    # else:
    #     opts_comm = []
    # filtres["commerciaux"] = st.sidebar.multiselect("Commercial (MSISDN)", opts_comm, default=[])

    # ── Territoire / Quartier — filtré par PDV de la sélection ──
    pdv_sel = pdv[pdv["Zone_SA_Normalisee"].isin(filtres["zones_sa"])] if not pdv.empty and "Zone_SA_Normalisee" in pdv.columns and filtres["zones_sa"] else pdv
    if not pdv.empty and "zone" in pdv.columns and filtres["centre"] != "Tous":
        pdv_sel = pdv_sel[pdv_sel["zone"] == filtres["centre"]]

    terr_col  = _pick_col(pdv_sel, ["territory", "Territory"])
    quart_col = _pick_col(pdv_sel, ["quartier", "Quartier"])
    filtres["territoires"] = st.sidebar.multiselect(
        "Territoire",
        sorted(pdv_sel[terr_col].dropna().unique().tolist()) if terr_col else [],
        default=[],
    )
    filtres["quartiers"] = st.sidebar.multiselect(
        "Quartier",
        sorted(pdv_sel[quart_col].dropna().unique().tolist()) if quart_col else [],
        default=[],
    )

    return filtres


def appliquer_filtres(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
    filtres: dict,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    # ── PDV ──
    pdv_f = pdv.copy()
    if filtres.get("centre", "Tous") != "Tous" and "zone" in pdv_f.columns:
        pdv_f = pdv_f[pdv_f["zone"] == filtres["centre"]]
    if filtres.get("zones_sa") and "Zone_SA_Normalisee" in pdv_f.columns:
        pdv_f = pdv_f[pdv_f["Zone_SA_Normalisee"].isin(filtres["zones_sa"])]
    terr_col  = _pick_col(pdv_f, ["territory", "Territory"])
    quart_col = _pick_col(pdv_f, ["quartier", "Quartier"])
    if filtres.get("territoires") and terr_col:
        pdv_f = pdv_f[pdv_f[terr_col].isin(filtres["territoires"])]
    if filtres.get("quartiers") and quart_col:
        pdv_f = pdv_f[pdv_f[quart_col].isin(filtres["quartiers"])]

    # ── Commerciaux ──
    comm_f = comm.copy()
    if filtres.get("zones_sa") and "Zone_SA_Normalisee" in comm_f.columns:
        # Inclure aussi les zones commerciaux même sans PDV (cas PASCAL)
        comm_f = comm_f[comm_f["Zone_SA_Normalisee"].isin(filtres["zones_sa"])]
    if filtres.get("commerciaux") and "Ccial_MSISDN" in comm_f.columns:
        comm_f = comm_f[comm_f["Ccial_MSISDN"].isin(filtres["commerciaux"])]

    # ── Transactions : date uniquement ──
    tx_f = tx.copy()
    dr = filtres.get("date_range")
    if dr and len(dr) == 2 and not tx_f.empty and "Date" in tx_f.columns:
        tx_f = tx_f[(tx_f["Date"].dt.date >= dr[0]) & (tx_f["Date"].dt.date <= dr[1])]

    return tx_f, pdv_f, comm_f


# ─────────────────────────────────────────────
# KPIs GLOBAUX
# ─────────────────────────────────────────────

def calculer_kpis(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    tx_precedent: pd.DataFrame | None = None,
) -> dict:
    kpis = dict(pdv_total=0, pdv_touches=0, pdv_non_touches=0,
                taux_couverture=0, moy_pdv_par_jour=0,
                nouveaux_pdv=0, pdv_perdus=0,
                volume_total=0, flux_entrants=0, flux_sortants=0, flux_transfer=0)

    if pdv.empty or "agent_msisdn" not in pdv.columns:
        return kpis

    ref_set = set(pdv["agent_msisdn"].apply(clean_phone).dropna().unique())
    kpis["pdv_total"] = len(ref_set)

    touches_set = set()
    if not tx.empty and "To_clean" in tx.columns:
        touches_set = set(tx["To_clean"].dropna().unique()) & ref_set

    kpis["pdv_touches"]     = len(touches_set)
    kpis["pdv_non_touches"] = len(ref_set - touches_set)
    kpis["taux_couverture"] = round(len(touches_set) / len(ref_set) * 100, 1) if ref_set else 0

    nb_jours = tx["Date"].dt.date.nunique() if not tx.empty and "Date" in tx.columns else 0
    kpis["moy_pdv_par_jour"] = round(len(touches_set) / nb_jours, 1) if nb_jours else 0

    if tx_precedent is not None and not tx_precedent.empty and "To_clean" in tx_precedent.columns:
        prec_set = set(tx_precedent["To_clean"].dropna().unique()) & ref_set
        kpis["nouveaux_pdv"] = len(touches_set - prec_set)
        kpis["pdv_perdus"]   = len(prec_set - touches_set)

    if not tx.empty and "Amount" in tx.columns:
        kpis["volume_total"]  = float(tx["Amount"].sum())
    kpis["flux_entrants"] = _flux_amount(tx, TRANSACTION_TYPES_ENTRANTS)
    kpis["flux_sortants"] = _flux_amount(tx, TRANSACTION_TYPES_SORTANTS)
    kpis["flux_transfer"] = _flux_amount(tx, TRANSACTION_TYPES_TRANSFER)

    return kpis


# ─────────────────────────────────────────────
# TABLEAU STRATÉGIQUE (Zone_SA)
# ─────────────────────────────────────────────

def _zones_universe_tx_pdv(tx: pd.DataFrame, pdv: pd.DataFrame, comm: pd.DataFrame) -> list[str]:
    """Union des zones PDV + zones commerciaux (pour inclure PASCAL Centre III)."""
    zones = set()
    if not pdv.empty and "Zone_SA_Normalisee" in pdv.columns:
        zones.update(pdv["Zone_SA_Normalisee"].dropna().unique())
    if not comm.empty and "Zone_SA_Normalisee" in comm.columns:
        zones.update(comm["Zone_SA_Normalisee"].dropna().unique())
    return sorted(z for z in zones if z)


def construire_tableau_strategique(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
    tx_prec: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Logique par Zone_SA :
      PDV total          = nb PDV du portefeuille (référentiel PDV, cette zone)
      PDV touchés        = PDV du portefeuille touchés par les commerciaux de CETTE zone
      PDV non touchés    = portefeuille - touchés portefeuille
      Couverture         = touchés portefeuille / portefeuille
      PDV hors portef.   = PDV hors portefeuille touchés par les commerciaux de cette zone
      Taux intrusion     = hors portef. / (touchés portef. + hors portef.)
      Volume             = total des transactions des commerciaux de cette zone
    """
    toutes_zones = _zones_universe_tx_pdv(tx, pdv, comm)
    if not toutes_zones:
        return pd.DataFrame()

    lignes = []
    for zone in toutes_zones:
        # Portefeuille PDV de cette zone
        portefeuille = set()
        if not pdv.empty and "agent_msisdn" in pdv.columns and "Zone_SA_Normalisee" in pdv.columns:
            portefeuille = set(
                pdv.loc[pdv["Zone_SA_Normalisee"] == zone, "agent_msisdn"]
                .apply(clean_phone).dropna().unique()
            )

        # Transactions des commerciaux de CETTE zone
        tx_zone = pd.DataFrame()
        if not tx.empty and "Zone_SA_Comm" in tx.columns:
            tx_zone = tx[tx["Zone_SA_Comm"] == zone].copy()

        touches_portef  = set(tx_zone["To_clean"].dropna().unique()) & portefeuille if not tx_zone.empty and "To_clean" in tx_zone.columns else set()
        hors_portef     = (set(tx_zone["To_clean"].dropna().unique()) - portefeuille) if not tx_zone.empty and "To_clean" in tx_zone.columns else set()

        # Période précédente
        nouveaux = perdus = 0
        if tx_prec is not None and not tx_prec.empty and "Zone_SA_Comm" in tx_prec.columns and "To_clean" in tx_prec.columns:
            tx_prec_zone = tx_prec[tx_prec["Zone_SA_Comm"] == zone]
            prec_total = set(tx_prec_zone["To_clean"].dropna().unique())
            total_actuel = touches_portef | hors_portef
            nouveaux = len(total_actuel - prec_total)
            perdus   = len(prec_total - total_actuel)

        vol      = float(tx_zone["Amount"].sum()) if not tx_zone.empty and "Amount" in tx_zone.columns else 0.0
        entrants = _flux_amount(tx_zone, TRANSACTION_TYPES_ENTRANTS)
        sortants = _flux_amount(tx_zone, TRANSACTION_TYPES_SORTANTS)
        transfers= _flux_amount(tx_zone, TRANSACTION_TYPES_TRANSFER)

        n_portef = len(portefeuille)
        n_touch  = len(touches_portef)
        n_hors   = len(hors_portef)
        denom    = n_touch + n_hors

        lignes.append({
            "Zone_SA":            zone,
            "PDV total":          n_portef,
            "PDV touchés":        n_touch,
            "PDV non touchés":    max(n_portef - n_touch, 0),
            "Couverture (%)":     round(n_touch / n_portef * 100, 1) if n_portef else 0,
            "Nouveaux PDV":       nouveaux,
            "PDV perdus":         perdus,
            "PDV hors portef.":   n_hors,
            "Taux intrusion (%)": round(n_hors / denom * 100, 1) if denom else 0,
            "Volume traité":      vol,
            "Cash In":            entrants,
            "Cash Out":           sortants,
            "Transfer":           transfers,
        })

    return (
        pd.DataFrame(lignes)
        .sort_values("Couverture (%)", ascending=False)
        .reset_index(drop=True)
    )


def analyser_zones_sa(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
    tx_prec: pd.DataFrame | None = None,
) -> pd.DataFrame:
    df = construire_tableau_strategique(tx, pdv, comm, tx_prec)
    if df.empty:
        return df
    cols = ["Zone_SA", "PDV total", "PDV touchés", "PDV non touchés",
            "Couverture (%)", "PDV hors portef.", "Taux intrusion (%)", "Volume traité",
            "Cash In", "Cash Out", "Transfer"]
    return df[[c for c in cols if c in df.columns]].copy()


# ─────────────────────────────────────────────
# EMPIÈTEMENT
# ─────────────────────────────────────────────

def detecter_empietement(
    tx: pd.DataFrame,
    comm: pd.DataFrame,
) -> pd.DataFrame:
    """
    Empiètement : Zone_SA_Comm ≠ Zone_SA_PDV.
    Limité aux vrais commerciaux du fichier commerciaux.
    """
    if tx is None or tx.empty:
        return pd.DataFrame()
    if "Zone_SA_Comm" not in tx.columns or "Zone_SA_PDV" not in tx.columns:
        return pd.DataFrame()

    df = tx.copy()

    # Limiter aux vrais commerciaux
    if not comm.empty and "Ccial_MSISDN" in comm.columns:
        vrais = set(comm["Ccial_MSISDN"].apply(clean_phone).dropna().unique())
        df = df[df["From_clean"].isin(vrais)]

    mask = (
        df["Zone_SA_Comm"].notna() &
        df["Zone_SA_PDV"].notna() &
        (df["Zone_SA_Comm"] != df["Zone_SA_PDV"]) &
        df["To_clean"].notna() &
        df["From_clean"].notna()
    )
    df = df[mask].copy()
    if df.empty:
        return pd.DataFrame()

    group = ["Zone_SA_Comm", "Zone_SA_PDV", "From_clean", "To_clean"]
    if "Commercial" in df.columns:
        group.insert(2, "Commercial")

    agg = (
        df.groupby(group, dropna=False)
        .agg(
            nb_transactions=("Amount", "count") if "Amount" in df.columns else ("To_clean", "count"),
            volume=("Amount", "sum") if "Amount" in df.columns else ("To_clean", "size"),
        )
        .reset_index()
        .rename(columns={
            "Zone_SA_Comm":  "Zone commerciale",
            "Zone_SA_PDV":   "Zone PDV",
            "From_clean":    "MSISDN commercial",
            "To_clean":      "PDV",
            "Commercial":    "Nom commercial",
        })
        .sort_values("nb_transactions", ascending=False)
        .reset_index(drop=True)
    )
    return agg


def matrice_empietement(tx: pd.DataFrame, comm: pd.DataFrame) -> pd.DataFrame:
    emp = detecter_empietement(tx, comm)
    if emp.empty:
        return pd.DataFrame()
    return emp.pivot_table(
        index="Zone PDV",
        columns="Zone commerciale",
        values="nb_transactions",
        aggfunc="sum",
        fill_value=0,
    )


def detecter_pdv_multi_visites(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
) -> pd.DataFrame:
    """
    PDV touchés par des commerciaux de Zones_SA DIFFÉRENTES uniquement.
    """
    if tx is None or tx.empty:
        return pd.DataFrame()
    if "Zone_SA_Comm" not in tx.columns or "To_clean" not in tx.columns:
        return pd.DataFrame()

    df = tx.copy()

    # Limiter aux vrais commerciaux et vrais PDV
    if not comm.empty and "Ccial_MSISDN" in comm.columns:
        vrais_comm = set(comm["Ccial_MSISDN"].apply(clean_phone).dropna().unique())
        df = df[df["From_clean"].isin(vrais_comm)]
    if not pdv.empty and "agent_msisdn" in pdv.columns:
        vrais_pdv = set(pdv["agent_msisdn"].apply(clean_phone).dropna().unique())
        df = df[df["To_clean"].isin(vrais_pdv)]

    df = df[df["Zone_SA_Comm"].notna()].copy()
    if df.empty:
        return pd.DataFrame()

    agg = (
        df.groupby("To_clean")
        .agg(
            Zone_propriétaire=("Zone_SA_PDV", lambda s: s.dropna().mode()[0] if not s.dropna().empty else ""),
            nb_zones_visiteuses=("Zone_SA_Comm", lambda s: s.dropna().nunique()),
            zones_visiteuses=("Zone_SA_Comm", lambda s: ", ".join(sorted(s.dropna().unique()))),
            nb_commerciaux=("From_clean", "nunique"),
            nb_transactions=("From_clean", "count"),
            volume=("Amount", "sum") if "Amount" in df.columns else ("From_clean", "count"),
        )
        .reset_index()
        .rename(columns={"To_clean": "PDV (MSISDN)"})
    )

    # Garder uniquement les PDV visités par ≥ 2 zones commerciales distinctes
    multi = agg[agg["nb_zones_visiteuses"] > 1].sort_values(
        "nb_zones_visiteuses", ascending=False
    ).reset_index(drop=True)

    return multi


# ─────────────────────────────────────────────
# PERFORMANCE COMMERCIAUX
# ─────────────────────────────────────────────

def analyser_performances_commerciaux(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
) -> pd.DataFrame:
    """
    Par commercial :
      - Centre
      - Zone_SA
      - PDV touchés           : PDV uniques touchés sur la période
      - PDV non touchés       : PDV de son portefeuille non touchés
      - PDV total             : taille de son portefeuille
      - Taux de couverture
      - Moy PDV touchés/jour
      - Moy PDV non touchés/jour  (PDV non touchés / nb jours actifs)
      - Montant total descendu
      - Montant moyen/jour
      - Zones de distribution (sitenames des PDV de son portefeuille)
    """
    if tx.empty or comm.empty:
        return pd.DataFrame()
    if "Zone_SA_Comm" not in tx.columns or "From_clean" not in tx.columns:
        return pd.DataFrame()

    lignes = []

    for _, row in comm.iterrows():
        msisdn = row.get("Ccial_MSISDN", "")
        if not msisdn:
            continue

        nom      = row.get("Nom_Ccial", msisdn)
        zone     = row.get("Zone_SA_Normalisee", "")
        centre   = row.get("Zone_Centre", "")

        # Portefeuille PDV du commercial = PDV de sa Zone_SA
        portefeuille = set()
        sitenames    = []
        if not pdv.empty and "agent_msisdn" in pdv.columns and "Zone_SA_Normalisee" in pdv.columns:
            pdv_zone = pdv[pdv["Zone_SA_Normalisee"] == zone]
            portefeuille = set(pdv_zone["agent_msisdn"].apply(clean_phone).dropna().unique())
            if "sitename" in pdv_zone.columns:
                sitenames = sorted(pdv_zone["sitename"].dropna().unique().tolist())

        # Transactions de CE commercial uniquement
        tx_comm = tx[tx["From_clean"] == msisdn].copy()

        if tx_comm.empty:
            # Commercial sans transaction
            lignes.append({
                "Commercial":            nom,
                "MSISDN":                msisdn,
                "Zone_SA":               zone,
                "Centre":                centre,
                "PDV total":             len(portefeuille),
                "PDV touchés":           0,
                "PDV non touchés":       len(portefeuille),
                "Taux couverture (%)":   0.0,
                "Moy PDV touchés/jour":  0.0,
                "Moy PDV non touchés/j": 0.0,
                "Montant total":         0.0,
                "Montant moyen/jour":    0.0,
                "Zones de distribution": "; ".join(sitenames[:20]),
            })
            continue

        # PDV du portefeuille touchés par ce commercial
        touches = set(tx_comm["To_clean"].dropna().unique()) & portefeuille
        non_touches = portefeuille - touches

        nb_jours = tx_comm["Date"].dt.date.nunique() if "Date" in tx_comm.columns else 0
        montant  = float(tx_comm["Amount"].sum()) if "Amount" in tx_comm.columns else 0.0

        lignes.append({
            "Commercial":            nom,
            "MSISDN":                msisdn,
            "Zone_SA":               zone,
            "Centre":                centre,
            "PDV total":             len(portefeuille),
            "PDV touchés":           len(touches),
            "PDV non touchés":       len(non_touches),
            "Taux couverture (%)":   round(len(touches) / len(portefeuille) * 100, 1) if portefeuille else 0.0,
            "Moy PDV touchés/jour":  round(len(touches) / nb_jours, 1) if nb_jours else 0.0,
            "Moy PDV non touchés/j": round(len(non_touches) / nb_jours, 1) if nb_jours else 0.0,
            "Montant total":         montant,
            "Montant moyen/jour":    round(montant / nb_jours, 0) if nb_jours else 0.0,
            "Zones de distribution": "; ".join(sitenames[:20]),
        })

    if not lignes:
        return pd.DataFrame()

    return (
        pd.DataFrame(lignes)
        .sort_values(["Centre", "Zone_SA", "Taux couverture (%)"], ascending=[True, True, False])
        .reset_index(drop=True)
    )


# ─────────────────────────────────────────────
# VISUALISATIONS
# ─────────────────────────────────────────────

def chart_evolution_pdv(tx: pd.DataFrame) -> go.Figure:
    if tx.empty or "Date" not in tx.columns or "To_clean" not in tx.columns:
        return go.Figure()
    daily = (
        tx.groupby(tx["Date"].dt.date)["To_clean"]
        .nunique().reset_index(name="PDV touchés")
        .rename(columns={"Date": "date"})
    )
    fig = px.line(daily, x="date", y="PDV touchés",
                  title="Évolution journalière des PDV touchés",
                  markers=True, color_discrete_sequence=["#22c55e"])
    fig.update_layout(template="plotly_white")
    return fig


def chart_repartition_zone(df_zone: pd.DataFrame) -> go.Figure:
    if df_zone is None or df_zone.empty or "PDV touchés" not in df_zone.columns:
        return go.Figure()
    fig = px.bar(
        df_zone.sort_values("PDV touchés", ascending=True),
        x="PDV touchés", y="Zone_SA", orientation="h",
        title="PDV touchés par Zone_SA",
        color="Couverture (%)" if "Couverture (%)" in df_zone.columns else None,
        color_continuous_scale="RdYlGn",
    )
    fig.update_layout(template="plotly_white")
    return fig


def chart_flux(kpis: dict) -> go.Figure:
    vals = [kpis.get("flux_entrants", 0), kpis.get("flux_transfer", 0), kpis.get("flux_sortants", 0)]
    fig = go.Figure(go.Bar(
        x=["Cash In", "Transfer", "Cash Out"],
        y=vals,
        marker_color=["#22c55e", "#3b82f6", "#ef4444"],
        text=[f"{v:,.0f}" for v in vals],
        textposition="outside",
    ))
    fig.update_layout(title="Flux financiers", template="plotly_white", yaxis_title="Montant")
    return fig


def chart_matrice_empietement(mat: pd.DataFrame) -> go.Figure:
    if mat is None or mat.empty:
        return go.Figure()
    fig = px.imshow(mat, text_auto=True, color_continuous_scale="Blues",
                    title="Matrice Zone propriétaire vs Zone visiteuse",
                    labels={"x": "Zone commerciale", "y": "Zone PDV", "color": "Transactions"})
    fig.update_layout(template="plotly_white")
    return fig


def chart_couverture_quartier(tx: pd.DataFrame, pdv: pd.DataFrame) -> pd.DataFrame:
    quart_col = _pick_col(pdv, ["quartier", "Quartier"])
    if pdv.empty or not quart_col or "agent_msisdn" not in pdv.columns:
        return pd.DataFrame()
    touches = set(tx["To_clean"].dropna().unique()) if not tx.empty and "To_clean" in tx.columns else set()
    lignes = []
    for q, grp in pdv.groupby(quart_col):
        pdv_q = set(grp["agent_msisdn"].apply(clean_phone).dropna().unique())
        if not pdv_q:
            continue
        t = pdv_q & touches
        lignes.append({
            "Quartier": q,
            "PDV total": len(pdv_q),
            "PDV touchés": len(t),
            "PDV non touchés": len(pdv_q) - len(t),
            "Couverture (%)": round(len(t) / len(pdv_q) * 100, 1),
        })
    return pd.DataFrame(lignes).sort_values("Couverture (%)", ascending=False).reset_index(drop=True)


# ─────────────────────────────────────────────
# SECTION UPLOAD
# ─────────────────────────────────────────────

def render_upload_section():
    st.subheader("📁 Chargement des transactions")
    st.caption("Colonnes attendues : Date, From, To, Amount, Type")
    tx_files = st.file_uploader(
        "Upload Transactions", type=["csv", "xlsx", "xls"],
        accept_multiple_files=True, key="ct_tx_uploader",
    )
    if tx_files:
        for f in tx_files:
            if not upload_file(f, BUCKET_TRANSACTIONS):
                st.warning(f"⚠️ {f.name} déjà présent ou erreur")
        load_transactions.clear()
    if st.button("🔄 Vider le cache et recharger", key="ct_clear_cache"):
        load_transactions.clear()
        st.rerun()


# ─────────────────────────────────────────────
# RENDU DES ONGLETS
# ─────────────────────────────────────────────

def _style_tableau(df: pd.DataFrame, grad_cols: list[str] = None, fmt: dict = None):
    s = df.style
    for col in (grad_cols or []):
        if col in df.columns:
            cmap = "Reds" if "intrusion" in col.lower() else "RdYlGn"
            s = s.background_gradient(subset=[col], cmap=cmap)
    if fmt:
        s = s.format({k: v for k, v in fmt.items() if k in df.columns})
    return s


def render_vue_globale(kpis: dict, tx: pd.DataFrame, df_zone: pd.DataFrame):
    st.subheader("📊 Vue globale")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("PDV touchés",       f"{kpis.get('pdv_touches',0):,}",
              delta=f"+{kpis.get('nouveaux_pdv',0)} nouveaux")
    c2.metric("PDV non touchés",   f"{kpis.get('pdv_non_touches',0):,}",
              delta=f"-{kpis.get('pdv_perdus',0)} perdus", delta_color="inverse")
    c3.metric("Taux de couverture", f"{kpis.get('taux_couverture',0)} %")
    c4.metric("Moy. PDV/jour",     f"{kpis.get('moy_pdv_par_jour',0)}")
    st.divider()
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Volume traité", f"{kpis.get('volume_total',0):,.0f}")
    c6.metric("Cash In",       f"{kpis.get('flux_entrants',0):,.0f}")
    c7.metric("Cash Out",      f"{kpis.get('flux_sortants',0):,.0f}")
    c8.metric("Transfer",      f"{kpis.get('flux_transfer',0):,.0f}")
    st.divider()
    ca, cb = st.columns(2)
    with ca: st.plotly_chart(chart_evolution_pdv(tx), use_container_width=True, key="g_evol")
    with cb: st.plotly_chart(chart_flux(kpis), use_container_width=True, key="g_flux")
    st.plotly_chart(chart_repartition_zone(df_zone), use_container_width=True, key="g_zones")


def render_analyse_zone_sa(df_zone: pd.DataFrame, tx: pd.DataFrame):
    st.subheader("🗺️ Analyse Zone_SA")
    if df_zone is None or df_zone.empty:
        st.info("Aucune donnée disponible.")
        return
    fmt = {"Couverture (%)": "{:.1f}", "Taux intrusion (%)": "{:.1f}",
           "Volume traité": "{:,.0f}", "Cash In": "{:,.0f}",
           "Cash Out": "{:,.0f}", "Transfer": "{:,.0f}"}
    st.markdown("#### Classement des zones")
    st.dataframe(
        _style_tableau(df_zone, ["Couverture (%)", "Taux intrusion (%)"], fmt),
        use_container_width=True, hide_index=True,
    )
    st.plotly_chart(
        px.bar(df_zone, x="Zone_SA", y="Couverture (%)",
               color="Couverture (%)", color_continuous_scale="RdYlGn",
               title="Taux de couverture par Zone_SA"),
        use_container_width=True, key="z_bar"
    )
    if "Volume traité" in df_zone.columns:
        st.plotly_chart(
            px.pie(df_zone[df_zone["Volume traité"] > 0],
                   names="Zone_SA", values="Volume traité",
                   title="Répartition du volume traité par Zone_SA"),
            use_container_width=True, key="z_pie"
        )


def render_empietement(tx: pd.DataFrame, pdv: pd.DataFrame, comm: pd.DataFrame):
    st.subheader("🔀 Détection d'empiètement entre Zones_SA")

    if "Zone_SA_Comm" not in tx.columns or "Zone_SA_PDV" not in tx.columns:
        st.warning("Enrichissement manquant — vérifiez le référentiel commerciaux.")
        return

    emp   = detecter_empietement(tx, comm)
    mat   = matrice_empietement(tx, comm)
    multi = detecter_pdv_multi_visites(tx, pdv, comm)

    if emp.empty:
        st.info("Aucun empiètement détecté sur la période.")
    else:
        st.markdown(f"**{len(emp):,} cas d'empiètement détectés**")
        st.caption("Un cas = un commercial ayant touché un PDV hors de sa Zone_SA")
        st.dataframe(emp, use_container_width=True, hide_index=True)

    if not mat.empty:
        st.markdown("#### Matrice Zone propriétaire (PDV) vs Zone visiteuse (commercial)")
        st.plotly_chart(chart_matrice_empietement(mat), use_container_width=True, key="e_mat")

    st.divider()
    st.markdown("#### PDV visités par des commerciaux de Zones_SA différentes")
    st.caption("Uniquement les PDV touchés par ≥ 2 zones commerciales distinctes")
    if not multi.empty:
        st.dataframe(multi, use_container_width=True, hide_index=True)
    else:
        st.info("Aucun PDV multi-zones sur la période.")


def render_geographie(tx: pd.DataFrame, pdv: pd.DataFrame):
    st.subheader("🌍 Couverture géographique")
    df_q = chart_couverture_quartier(tx, pdv)
    if df_q.empty:
        st.info("Colonne 'quartier' absente du référentiel PDV.")
        return

    st.markdown("#### Couverture par quartier")
    st.dataframe(
        _style_tableau(df_q, ["Couverture (%)"], {"Couverture (%)": "{:.1f}"}),
        use_container_width=True, hide_index=True,
    )
    st.plotly_chart(
        px.bar(df_q, x="Quartier", y="Couverture (%)",
               color="Couverture (%)", color_continuous_scale="RdYlGn",
               title="Couverture par quartier").update_xaxes(tickangle=45),
        use_container_width=True, key="geo_bar"
    )

    zone_col  = _pick_col(pdv, ["Zone_SA_Normalisee", "zone", "Zone_SA"])
    terr_col  = _pick_col(pdv, ["territory", "Territory"])
    quart_col = _pick_col(pdv, ["quartier", "Quartier"])

    if zone_col and quart_col and "agent_msisdn" in pdv.columns:
        touches_set = set(tx["To_clean"].dropna().unique()) if not tx.empty and "To_clean" in tx.columns else set()
        ps = pdv.copy()
        if "agent_msisdn_clean" not in ps.columns:
            ps["agent_msisdn_clean"] = ps["agent_msisdn"].apply(clean_phone)
        ps["statut"] = ps["agent_msisdn_clean"].map(
            lambda m: "Touché" if m in touches_set else "Non touché"
        )
        path = [zone_col] + ([terr_col] if terr_col else []) + [quart_col, "statut"]
        agg  = ps.groupby(path).size().reset_index(name="PDV")
        fig_tree = px.treemap(
            agg, path=path, values="PDV", color="statut",
            color_discrete_map={"Touché": COULEURS_CARTE["touche"], "Non touché": COULEURS_CARTE["non_touche"]},
            title="PDV touchés / non touchés par Zone_SA → Quartier",
        )
        fig_tree.update_traces(textinfo="label+value+percent parent")
        fig_tree.update_layout(height=550)
        st.plotly_chart(fig_tree, use_container_width=True, key="geo_tree")

        agg_s = ps.groupby([quart_col, "statut"]).size().reset_index(name="PDV")
        st.plotly_chart(
            px.bar(agg_s, x=quart_col, y="PDV", color="statut",
                   color_discrete_map={"Touché": COULEURS_CARTE["touche"], "Non touché": COULEURS_CARTE["non_touche"]},
                   barmode="stack", title="PDV touchés vs non touchés par quartier")
            .update_xaxes(tickangle=45),
            use_container_width=True, key="geo_stk"
        )


def render_evolution(tx: pd.DataFrame):
    st.subheader("📈 Évolution temporelle")
    if tx.empty or "Date" not in tx.columns:
        st.info("Aucune donnée de transaction disponible.")
        return
    tx_d = tx.copy()
    tx_d["date"] = tx_d["Date"].dt.date
    to_col = "To_clean" if "To_clean" in tx_d.columns else "To"

    st.plotly_chart(
        px.line(tx_d.groupby("date")[to_col].nunique().reset_index(name="PDV touchés"),
                x="date", y="PDV touchés", title="PDV touchés par jour",
                markers=True, color_discrete_sequence=["#22c55e"]),
        use_container_width=True, key="ev_pdv"
    )
    if "Amount" in tx_d.columns:
        st.plotly_chart(
            px.area(tx_d.groupby("date")["Amount"].sum().reset_index(name="Volume"),
                    x="date", y="Volume", title="Volume traité par jour",
                    color_discrete_sequence=["#3b82f6"]),
            use_container_width=True, key="ev_vol"
        )
    if "Zone_SA_Comm" in tx_d.columns:
        daily_z = tx_d.groupby(["date", "Zone_SA_Comm"])[to_col].nunique().reset_index(name="PDV touchés")
        st.plotly_chart(
            px.line(daily_z, x="date", y="PDV touchés", color="Zone_SA_Comm",
                    title="PDV touchés par Zone_SA (journalier)", markers=True),
            use_container_width=True, key="ev_zone"
        )


def render_tableau_strategique(df_tableau: pd.DataFrame):
    st.subheader("📋 Tableau stratégique de pilotage")
    if df_tableau.empty:
        st.info("Aucune donnée disponible.")
        return
    fmt = {"Couverture (%)": "{:.1f}", "Taux intrusion (%)": "{:.1f}",
           "Volume traité": "{:,.0f}", "Cash In": "{:,.0f}",
           "Cash Out": "{:,.0f}", "Transfer": "{:,.0f}"}
    st.dataframe(
        _style_tableau(df_tableau, ["Couverture (%)", "Taux intrusion (%)"], fmt),
        use_container_width=True, hide_index=True,
    )
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as w:
        df_tableau.to_excel(w, index=False, sheet_name="Tableau stratégique")
    output.seek(0)
    st.download_button(
        "⬇️ Exporter en Excel", data=output,
        file_name=f"conquete_territoire_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_performances_commerciaux(
    tx: pd.DataFrame,
    pdv: pd.DataFrame,
    comm: pd.DataFrame,
):
    st.subheader("👤 Performances des commerciaux")

    df = analyser_performances_commerciaux(tx, pdv, comm)
    if df.empty:
        st.info("Aucune donnée disponible.")
        return

    # ── Filtres rapides par Centre ──
    centres_dispo = sorted(df["Centre"].dropna().unique().tolist())
    if centres_dispo:
        centre_sel = st.radio("Afficher", ["Tous"] + centres_dispo, horizontal=True, key="perf_centre_radio")
        if centre_sel != "Tous":
            df = df[df["Centre"] == centre_sel]

    fmt = {
        "Taux couverture (%)":   "{:.1f}",
        "Moy PDV touchés/jour":  "{:.1f}",
        "Moy PDV non touchés/j": "{:.1f}",
        "Montant total":         "{:,.0f}",
        "Montant moyen/jour":    "{:,.0f}",
    }
    st.dataframe(
        _style_tableau(df, ["Taux couverture (%)"], fmt),
        use_container_width=True, hide_index=True,
    )

    st.divider()

    # ── Graphiques ──
    if not df.empty:
        col_a, col_b = st.columns(2)
        with col_a:
            st.plotly_chart(
                px.bar(df.sort_values("Taux couverture (%)", ascending=True),
                       x="Taux couverture (%)", y="Commercial", orientation="h",
                       color="Taux couverture (%)", color_continuous_scale="RdYlGn",
                       title="Taux de couverture par commercial"),
                use_container_width=True, key="perf_couv"
            )
        with col_b:
            st.plotly_chart(
                px.bar(df.sort_values("Montant total", ascending=True),
                       x="Montant total", y="Commercial", orientation="h",
                       color="Zone_SA", title="Montant total par commercial"),
                use_container_width=True, key="perf_montant"
            )

        # PDV touchés vs non touchés
        st.plotly_chart(
            px.bar(
                df.melt(id_vars=["Commercial", "Zone_SA"],
                        value_vars=["PDV touchés", "PDV non touchés"],
                        var_name="Statut", value_name="Nb PDV"),
                x="Commercial", y="Nb PDV", color="Statut",
                color_discrete_map={"PDV touchés": COULEURS_CARTE["touche"],
                                     "PDV non touchés": COULEURS_CARTE["non_touche"]},
                barmode="stack", title="PDV touchés vs non touchés par commercial",
            ).update_xaxes(tickangle=45),
            use_container_width=True, key="perf_pdv_stack"
        )

    # ── Export ──
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Performances commerciaux")
    output.seek(0)
    st.download_button(
        "⬇️ Exporter performances", data=output,
        file_name=f"performances_commerciaux_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ─────────────────────────────────────────────
# ENTRÉE PRINCIPALE
# ─────────────────────────────────────────────

def show_conquete_territoire():
    st.title("🏆 Conquête de Territoire")
    st.markdown("*Mesure de la couverture marché par les équipes commerciales*")

    # ── Chargement ──
    with st.spinner("Chargement des données..."):
        tx_raw      = load_transactions()
        pdv, comm   = charger_referentiels()

    if pdv.empty:
        st.error("Référentiel PDV non chargé. Allez dans Settings > Maître POS.")
        return
    if comm.empty:
        st.warning("Fichier Commerciaux non chargé. Certaines analyses seront limitées.")

    # ── Filtres ──
    filtres = render_filtres(tx_raw, pdv, comm)
    tx_f, pdv_f, comm_f = appliquer_filtres(tx_raw, pdv, comm, filtres)

    # ── Enrichissement ──
    tx_enrichi = enrichir_transactions(tx_f, pdv_f, comm_f)

    # ── Période précédente ──
    tx_prec = pd.DataFrame()
    dr = filtres.get("date_range")
    if dr and len(dr) == 2 and not tx_raw.empty and "Date" in tx_raw.columns:
        d0, d1   = dr
        delta    = (d1 - d0).days + 1
        p_end    = d0 - timedelta(days=1)
        p_start  = p_end - timedelta(days=delta - 1)
        tx_prec_raw = tx_raw[
            (tx_raw["Date"].dt.date >= p_start) &
            (tx_raw["Date"].dt.date <= p_end)
        ].copy()
        if not tx_prec_raw.empty:
            tx_prec = enrichir_transactions(tx_prec_raw, pdv_f, comm_f)

    # ── Calculs ──
    kpis    = calculer_kpis(tx_enrichi, pdv_f, tx_prec if not tx_prec.empty else None)
    df_zone = analyser_zones_sa(tx_enrichi, pdv_f, comm_f, tx_prec if not tx_prec.empty else None)
    df_tab  = construire_tableau_strategique(tx_enrichi, pdv_f, comm_f, tx_prec if not tx_prec.empty else None)

    # ── Onglets ──
    tabs = st.tabs([
        "📊 Vue globale",
        "🗺️ Zone_SA",
        "🔀 Empiètement",
        "🌍 Géographie",
        "📈 Évolution",
        "📋 Tableau stratégique",
        "👤 Performances commerciaux",
        "📁 Upload",
    ])

    with tabs[0]: render_vue_globale(kpis, tx_enrichi, df_zone)
    with tabs[1]: render_analyse_zone_sa(df_zone, tx_enrichi)
    with tabs[2]: render_empietement(tx_enrichi, pdv_f, comm_f)
    with tabs[3]: render_geographie(tx_enrichi, pdv_f)
    with tabs[4]: render_evolution(tx_enrichi)
    with tabs[5]: render_tableau_strategique(df_tab)
    with tabs[6]: render_performances_commerciaux(tx_enrichi, pdv_f, comm_f)
    with tabs[7]: render_upload_section()
