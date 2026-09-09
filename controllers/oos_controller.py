"""controllers/oos_hvc_controller.py

Controller unifie pour la page OOS & Variations HVC.

Deux domaines distincts, un seul controller :
- OOS Listing   : photo instantanee des POS en rupture (listing_oos)
- HVC Variation : evolution temporelle par snapshot (hvc_variations)

Points importants (issus du diagnostic Tache 6) :
1. listing_oos ne contient QUE des POS en rupture (is_oos == 1 toujours).
   Le vrai taux OOS = nb_oos / nb_referentiel est calcule en croisant
   avec referentiel_pos — jamais sur listing_oos seule.
2. oos_pct dans les deux tables est une fraction 0.0-1.0. Toute valeur
   <= 1.0 est multipliee par 100 pour l'affichage dans l'IHM.
3. Le filtre heure s'applique sur snapshot_timestamp (hvc_variations)
   ou last_trx_time (listing_oos) — pas sur la table transactions.
4. Le graphe de progression OOS/Day HVC est construit sur la serie de
   snapshots horodates de hvc_variations (Option A validee), ordonnee
   par snapshot_timestamp sur l'axe X.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from typing import Optional
from datetime import datetime

import numpy as np
import pandas as pd
import polars as pl
import streamlit as st

from models.hvc_model import get_hvc_variations, get_hvc_snapshot_timestamps, get_hvc_commercial_mapping
from models.oos_model import get_oos_listing, get_oos_filter_options, find_frequent_commercial_for_unassigned_pos, insert_oos_rows
from models.pos_model import get_all_pos, get_pos_referentiel_count
from utils.helpers import clean_phone


# ---------------------------------------------------------------------------
# Filtres
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OosHvcFilters:
    # Filtres communs (influencent les deux onglets)
    zone: str = "Toutes"
    territory: str = "Toutes"
    cluster: str = "Tous"
    zone_sa: str = "Toutes"
    hour_min: int = 0
    hour_max: int = 23

    # Plage de dates
    date_start: Optional[str] = None
    date_end: Optional[str] = None

    # Filtres specifiques OOS Listing
    snapshot_date: Optional[str] = None
    segment_group: str = "Tous"   # influence UNIQUEMENT le tableau listing_oos

    # Filtres specifiques HVC Variation
    snapshot_t1: Optional[str] = None
    snapshot_t2: Optional[str] = None


# ---------------------------------------------------------------------------
# Contextes de sortie
# ---------------------------------------------------------------------------

@dataclass
class OosKpis:
    total_pos_referentiel: int = 0
    nb_pos_oos: int = 0
    pct_oos_global: float = 0.0
    pct_oos_previous: float = 0.0
    day_hvc_moyen: float = 0.0
    nb_hvc_oos: int = 0
    frequence_oos: float = 0.0   # nb moyen de snapshots par POS ou il est en OOS
    latest_snapshot: Optional[str] = None
    latest_nb_lignes: int = 0


@dataclass
class OosListingContext:
    table: pd.DataFrame = field(default_factory=pd.DataFrame)
    par_cluster: pd.DataFrame = field(default_factory=pd.DataFrame)
    is_empty: bool = False
    message: str = ""


@dataclass
class HvcVariationContext:
    table: pd.DataFrame = field(default_factory=pd.DataFrame)
    top10: pd.DataFrame = field(default_factory=pd.DataFrame)
    flop10: pd.DataFrame = field(default_factory=pd.DataFrame)
    progression: pd.DataFrame = field(default_factory=pd.DataFrame)   # serie temporelle pour le graphe
    kpis_variation: dict = field(default_factory=dict)
    available_snapshots: list = field(default_factory=list)
    is_empty: bool = False
    message: str = ""


@dataclass
class OosHvcContext:
    filters: OosHvcFilters
    global_kpis: OosKpis
    listing: OosListingContext
    variation: HvcVariationContext
    filter_options: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Point d'entree principal
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def build_oos_hvc_context(filters: OosHvcFilters) -> OosHvcContext:
    options = _load_filter_options()
    hvc_msisdns = _get_hvc_msisdns()

    listing_ctx = _build_listing_context_uncached(filters, hvc_msisdns)
    variation_ctx = _build_variation_context(filters)
    global_kpis = _build_global_kpis(filters, listing_ctx, variation_ctx, hvc_msisdns)

    return OosHvcContext(
        filters=filters,
        global_kpis=global_kpis,
        listing=listing_ctx,
        variation=variation_ctx,
        filter_options=options,
    )


def clear_oos_cache() -> None:
    """Vide le cache Streamlit lors des opérations d'ingestion/modification."""
    st.cache_data.clear()


@st.cache_data(ttl=300, show_spinner=False)  # 5 minutes — options quasi-statiques
def load_filter_options() -> dict:
    return _load_filter_options()



# ---------------------------------------------------------------------------
# KPIs globaux (barre de statut en haut de page)
# ---------------------------------------------------------------------------

def _build_global_kpis(
    filters: OosHvcFilters,
    listing_ctx: OosListingContext,
    variation_ctx: HvcVariationContext,
    hvc_msisdns: set,
) -> OosKpis:
    kpis = OosKpis()

    # Total POS referentiel
    kpis.total_pos_referentiel = get_pos_referentiel_count(
        zone=filters.zone if filters.zone not in ("Toutes", "") else None,
        territory=filters.territory if filters.territory not in ("Toutes", "") else None,
        cluster=filters.cluster if filters.cluster not in ("Tous", "") else None,
        zone_sa=filters.zone_sa if filters.zone_sa not in ("Toutes", "") else None,
        segment_group=filters.segment_group if filters.segment_group not in ("Tous", "Toutes", "") else None,
    )

    # Depuis listing_oos
    if not listing_ctx.table.empty:
        kpis.nb_pos_oos = len(listing_ctx.table)
        
        # Calcul du Taux OOS Global réel basé sur le Référentiel
        kpis.pct_oos_global = (
            round(kpis.nb_pos_oos / kpis.total_pos_referentiel * 100, 1)
            if kpis.total_pos_referentiel else 0.0
        )
        
        # HVC en OOS
        if "segment_group" in listing_ctx.table.columns:
            seg = listing_ctx.table["segment_group"].astype(str).str.upper()
            kpis.nb_hvc_oos = int((seg.str.contains("HVC", na=False)).sum())

        # Frequence OOS
        try:
            all_oos = get_oos_listing(
                zone=filters.zone if filters.zone != "Toutes" else None,
                territory=filters.territory if filters.territory != "Toutes" else None,
                cluster=filters.cluster if filters.cluster != "Tous" else None,
                zone_sa=filters.zone_sa if filters.zone_sa not in ("Toutes", "") else None,
            )
            if not all_oos.empty and "msisdn" in all_oos.columns and "snapshot_date" in all_oos.columns:
                freq = all_oos.groupby("msisdn")["snapshot_date"].nunique()
                kpis.frequence_oos = round(freq.mean(), 1)
        except Exception:
            pass

    # Day HVC moyen depuis le dernier snapshot disponible
    if not variation_ctx.table.empty and "day_hvc_t2" in variation_ctx.table.columns:
        kpis.day_hvc_moyen = round(variation_ctx.table["day_hvc_t2"].mean(), 2)
    elif not variation_ctx.progression.empty and "day_hvc" in variation_ctx.progression.columns:
        latest = variation_ctx.progression.sort_values("snapshot_timestamp").iloc[-1]
        kpis.day_hvc_moyen = round(latest["day_hvc"], 2)
    
    # ---------------------------------------------------------
    # OOS précédent pour les graphiques (SANS écraser pct_oos_global !)
    # ---------------------------------------------------------

    if not variation_ctx.progression.empty:
        progression = (
            variation_ctx.progression
            .sort_values("snapshot_timestamp")
            .reset_index(drop=True)
        )

        # On prend uniquement la valeur précédente pour la comparaison (variation)
        if len(progression) >= 2:
            kpis.pct_oos_previous = float(
                progression.iloc[-2]["oos_pct"]
            )
        else:
            kpis.pct_oos_previous = kpis.pct_oos_global
            
        # Si pour une raison quelconque listing_ctx était vide, on utilise la valeur de la progression en fallback
        if listing_ctx.table.empty:
            kpis.pct_oos_global = float(progression.iloc[-1]["oos_pct"])

    # Fraicheur des donnees
    options = get_oos_filter_options()
    kpis.latest_snapshot = options.get("latest_snapshot_date")
    kpis.latest_nb_lignes = kpis.nb_pos_oos

    return kpis

# ---------------------------------------------------------------------------
# Listing OOS
# ---------------------------------------------------------------------------

# def _build_listing_context(
#     filters: OosHvcFilters,
#     hvc_msisdns: set,
# ) -> OosListingContext:
#     snapshot_date = filters.snapshot_date
#     if not snapshot_date:
#         # Tient enfin la promesse du help text : vide = dernier snapshot,
#         # pas "tout l'historique confondu".
#         options = get_oos_filter_options()
#         snapshot_date = options.get("latest_snapshot_date")
#     # Charge le référentiel pour l'enrichissement nom du POS
#     ref_df = get_all_pos()
#     try:
#         df = get_oos_listing(
#             snapshot_date=snapshot_date,
#             zone=filters.zone if filters.zone != "Toutes" else None,
#             territory=filters.territory if filters.territory != "Toutes" else None,
#             cluster=filters.cluster if filters.cluster != "Tous" else None,
#             segment_group=filters.segment_group if filters.segment_group != "Tous" else None,
#         )
#     except Exception as exc:
#         return OosListingContext(is_empty=True, message=f"Erreur chargement OOS : {exc}")

#         # ---------- Enrichissement Commercial + Nom POS ----------
#     try:
#         mapping = get_hvc_commercial_mapping()
#         if not mapping.empty and "msisdn" in df.columns:
#             mapping = mapping.copy()
#             mapping["hvc_msisdn"] = mapping["hvc_msisdn"].astype(str).str.strip()
#             df["msisdn"] = df["msisdn"].astype(str).str.strip()

#             df = df.merge(
#                 mapping[["hvc_msisdn", "commercial"]].rename(columns={"hvc_msisdn": "msisdn"}),
#                 on="msisdn",
#                 how="left",
#             )
#             df["commercial"] = df["commercial"].fillna("Non attribué")
#     except Exception:
#         df["commercial"] = "Non attribué"

#     # Nom du POS depuis le référentiel
#     if not ref_df.empty and "msisdn" in df.columns:
#         name_col = next((c for c in ["full_name", "profile", "nom_pos", "Full name"] if c in ref_df.columns), None)
#         msisdn_ref = next((c for c in ["msisdn", "agent_msisdn"] if c in ref_df.columns), None)
#         if name_col and msisdn_ref:
#             ref_names = ref_df[[msisdn_ref, name_col]].copy()
#             ref_names[msisdn_ref] = ref_names[msisdn_ref].astype(str).str.strip()
#             ref_names = ref_names.rename(columns={msisdn_ref: "msisdn", name_col: "full_name"})
#             df = df.merge(ref_names, on="msisdn", how="left")

#     # Filtre heure sur last_trx_time si disponible
#     if not df.empty and "last_trx_time" in df.columns and (filters.hour_min > 0 or filters.hour_max < 23):
#         try:
#             df["_hour"] = pd.to_datetime(df["last_trx_time"], errors="coerce").dt.hour
#             df = df[df["_hour"].between(filters.hour_min, filters.hour_max)].drop(columns=["_hour"])
#         except Exception:
#             pass

#     # Filtre zone_sa si disponible
#     if not df.empty and filters.zone_sa != "Toutes" and "zone_sa" in df.columns:
#         df = df[df["zone_sa"] == filters.zone_sa]

#     if df.empty:
#         return OosListingContext(is_empty=True, message="Aucun POS en rupture pour ces filtres.")

#     # Normalise oos_pct : fraction 0-1 -> pourcentage
#     if "oos_pct" in df.columns:
#         df["oos_pct"] = pd.to_numeric(df["oos_pct"], errors="coerce").fillna(0).astype(int)
#         #  = vals.apply(lambda v: v * 100 if v <= 1.0 else v).round(1)

#     # Vrai taux OOS par cluster (croisement referentiel)
#     par_cluster = _compute_cluster_coverage(df, ref_df, filters)

#     return OosListingContext(table=df, par_cluster=par_cluster)

# def _build_listing_context(
#     filters: OosHvcFilters,
#     hvc_msisdns: set,
# ) -> OosListingContext:
#     snapshot_date = filters.snapshot_date
#     if not snapshot_date:
#         options = get_oos_filter_options()
#         snapshot_date = options.get("latest_snapshot_date")
        
#     ref_df = get_all_pos()
#     try:
#         df = get_oos_listing(
#             snapshot_date=snapshot_date,
#             zone=filters.zone if filters.zone != "Toutes" else None,
#             territory=filters.territory if filters.territory != "Toutes" else None,
#             cluster=filters.cluster if filters.cluster != "Tous" else None,
#             segment_group=filters.segment_group if filters.segment_group != "Tous" else None,
#         )
#     except Exception as exc:
#         return OosListingContext(is_empty=True, message=f"Erreur chargement OOS : {exc}")

#     if df.empty:
#         return OosListingContext(is_empty=True, message="Aucun POS en rupture pour ces filtres.")

#     # Nettoyage MSISDN initial
#     if "msisdn" in df.columns:
#         df["msisdn"] = df["msisdn"].astype(str).str.strip()
        

#     # ---------- 1. Enrichissement Commercial (SÉCURISÉ) ----------
#     try:
#         mapping = get_hvc_commercial_mapping()
#         if not mapping.empty and "msisdn" in df.columns:
#             mapping = mapping.copy()
#             mapping["hvc_msisdn"] = mapping["hvc_msisdn"].astype(str).str.strip()
            
#             # SÉCURITÉ ANTI-DOUBLON : Conserve un seul commercial par MSISDN
#             mapping_clean = mapping[["hvc_msisdn", "commercial"]].drop_duplicates(subset=["hvc_msisdn"])

#             df = df.merge(
#                 mapping_clean.rename(columns={"hvc_msisdn": "msisdn"}),
#                 on="msisdn",
#                 how="left",
#             )
#             df["commercial"] = df["commercial"].fillna("Non attribué")
#     except Exception:
#         df["commercial"] = "Non attribué"

#     # ---------- 2. Nom du POS depuis le Référentiel (SÉCURISÉ) ----------
#     if not ref_df.empty and "msisdn" in df.columns:
#         name_col = next((c for c in ["full_name", "profile", "nom_pos", "Full name"] if c in ref_df.columns), None)
#         msisdn_ref = next((c for c in ["msisdn", "agent_msisdn"] if c in ref_df.columns), None)
#         zone_sa_col = next((c for c in ["zone_sa", "Zone_SA"] if c in ref_df.columns), None)

#         if msisdn_ref and zone_sa_col:
#             ref_sa = ref_df[[msisdn_ref, zone_sa_col]].copy()
#             ref_sa[msisdn_ref] = ref_sa[msisdn_ref].astype(str).str.strip()
#             ref_sa = (
#                 ref_sa
#                 .rename(columns={msisdn_ref: "msisdn", zone_sa_col: "zone_sa"})
#                 .drop_duplicates(subset=["msisdn"], keep="last")
#             )
#             if "zone_sa" in df.columns:
#                 df = df.drop(columns=["zone_sa"])
#             df = df.merge(ref_sa, on="msisdn", how="left")
#         if name_col and msisdn_ref:
#             ref_names = ref_df[[msisdn_ref, name_col]].copy()
#             ref_names[msisdn_ref] = ref_names[msisdn_ref].astype(str).str.strip()
            
#             # SÉCURITÉ ANTI-DOUBLON : Conserve un seul nom par MSISDN dans le référentiel
#             ref_names_clean = ref_names.rename(columns={msisdn_ref: "msisdn", name_col: "full_name"}).drop_duplicates(subset=["msisdn"])
            
#             df = df.merge(ref_names_clean, on="msisdn", how="left")

#     # ---------- 3. Filtres Heure & Zone ----------
#     if "last_trx_time" in df.columns and (filters.hour_min > 0 or filters.hour_max < 23):
#         try:
#             df["_hour"] = pd.to_datetime(df["last_trx_time"], errors="coerce").dt.hour
#             df = df[df["_hour"].between(filters.hour_min, filters.hour_max)].drop(columns=["_hour"])
#         except Exception:
#             pass

#     if filters.zone_sa not in (None, "Toutes", "") and "zone_sa" in df.columns:
#         df = df[
#             df["zone_sa"].astype(str).str.strip() == str(filters.zone_sa).strip()
#         ]

#     if df.empty:
#         return OosListingContext(is_empty=True, message="Aucun POS en rupture pour ces filtres.")

#     # ---------- 4. Nettoyage Taux OOS & Garde-fou final ----------
#     if "oos_pct" in df.columns:
#         df["oos_pct"] = pd.to_numeric(df["oos_pct"], errors="coerce").fillna(0).astype(int)

def _build_listing_context_uncached(
    filters: OosHvcFilters,
    hvc_msisdns: set,
) -> OosListingContext:
    snapshot_date = filters.snapshot_date
    if not snapshot_date:
        options = get_oos_filter_options()
        snapshot_date = options.get("latest_snapshot_date")
        
    ref_df = get_all_pos()
    try:
        df = get_oos_listing(
            snapshot_date=snapshot_date,
            zone=filters.zone if filters.zone != "Toutes" else None,
            zone_sa=filters.zone_sa if filters.zone_sa != "Toutes" else None,
            territory=filters.territory if filters.territory != "Toutes" else None,
            cluster=filters.cluster if filters.cluster != "Tous" else None,
            segment_group=filters.segment_group if filters.segment_group != "Tous" else None,
        )
    except Exception as exc:
        return OosListingContext(is_empty=True, message=f"Erreur chargement OOS : {exc}")

    if df.empty:
        return OosListingContext(is_empty=True, message="Aucun POS en rupture pour ces filtres.")

    # Nettoyage MSISDN initial
    if "msisdn" in df.columns:
        df["msisdn"] = df["msisdn"].astype(str).str.strip()

    # ---------- 1. Enrichissement Zone SA & Ref POS ----------
    if not ref_df.empty and "msisdn" in df.columns:
        name_col = next((c for c in ["full_name", "profile", "nom_pos", "Full name"] if c in ref_df.columns), None)
        msisdn_ref = next((c for c in ["msisdn", "agent_msisdn"] if c in ref_df.columns), None)
        zone_sa_col = next((c for c in ["zone_sa", "Zone_SA", "sa_incharge"] if c in ref_df.columns), None)

        if msisdn_ref:
            cols_to_merge = [msisdn_ref]
            if name_col:
                cols_to_merge.append(name_col)
            if zone_sa_col:
                cols_to_merge.append(zone_sa_col)

            ref_sub = ref_df[cols_to_merge].copy()
            ref_sub[msisdn_ref] = ref_sub[msisdn_ref].astype(str).str.strip()
            
            # Renommage explicite & dédoublonnage strict
            rename_map = {msisdn_ref: "msisdn"}
            if name_col:
                rename_map[name_col] = "full_name"
            if zone_sa_col:
                rename_map[zone_sa_col] = "zone_sa_ref"

            ref_clean = ref_sub.rename(columns=rename_map).drop_duplicates(subset=["msisdn"], keep="last")

            if "zone_sa" in df.columns:
                df["zone_sa"] = df["zone_sa"].fillna(df["msisdn"].map(ref_clean.set_index("msisdn")["zone_sa_ref"]))
            else:
                df = df.merge(ref_clean[["msisdn", "zone_sa_ref"]], on="msisdn", how="left").rename(columns={"zone_sa_ref": "zone_sa"})

            if "full_name" not in df.columns and "full_name" in ref_clean.columns:
                df = df.merge(ref_clean[["msisdn", "full_name"]], on="msisdn", how="left")

    # ---------- 2. Enrichissement Commercial ----------
    try:
        mapping = get_hvc_commercial_mapping()
        if not mapping.empty and "msisdn" in df.columns:
            mapping = mapping.copy()
            mapping["hvc_msisdn"] = mapping["hvc_msisdn"].astype(str).str.strip()
            mapping_clean = mapping[["hvc_msisdn", "commercial"]].drop_duplicates(subset=["hvc_msisdn"])

            df = df.merge(
                mapping_clean.rename(columns={"hvc_msisdn": "msisdn"}),
                on="msisdn",
                how="left",
            )
            df["commercial"] = df["commercial"].fillna("Non attribué")
    except Exception:
        df["commercial"] = "Non attribué"

    # ---------- 3. Application du filtre Zone SA ----------
    if filters.zone_sa not in (None, "Toutes", "") and "zone_sa" in df.columns:
        target_sa = str(filters.zone_sa).strip().upper()
        df = df[df["zone_sa"].astype(str).str.strip().str.upper() == target_sa]

    # ---------- 4. Filtre Heures ----------
    if "last_trx_time" in df.columns and (filters.hour_min > 0 or filters.hour_max < 23):
        try:
            df["_hour"] = pd.to_datetime(df["last_trx_time"], errors="coerce").dt.hour
            df = df[df["_hour"].between(filters.hour_min, filters.hour_max)].drop(columns=["_hour"])
        except Exception:
            pass

    if df.empty:
        return OosListingContext(is_empty=True, message="Aucun POS en rupture pour ces filtres.")

    if "oos_pct" in df.columns:
        df["oos_pct"] = pd.to_numeric(df["oos_pct"], errors="coerce").fillna(0).astype(int)

    df = df.drop_duplicates(subset=["msisdn"]).reset_index(drop=True)
    df = enrich_oos_with_history(df, filters)
    par_cluster = _compute_cluster_coverage(df, ref_df, filters)

    return OosListingContext(table=df, par_cluster=par_cluster)


def _filter_ref(ref_df: pd.DataFrame, filters: OosHvcFilters) -> pd.DataFrame:
    """Filtre le référentiel POS avec alignement insensible à la casse sur Zone SA."""
    if ref_df is None or ref_df.empty:
        return pd.DataFrame()

    df = ref_df.copy()

    zone_col = next((c for c in ["zone", "zone_centre", "Zone", "Centre"] if c in df.columns), None)
    territory_col = next((c for c in ["territory", "zone_territoire", "Territoire", "Territory"] if c in df.columns), None)
    cluster_col = next((c for c in ["cluster", "secteur_cluster", "Cluster", "quartier"] if c in df.columns), None)
    zone_sa_col = next((c for c in ["zone_sa", "Zone_SA", "sa_incharge"] if c in df.columns), None)
    segment_col = next((c for c in ["segment_group", "Segment_group", "Segment"] if c in df.columns), None)

    if filters.zone not in (None, "Toutes", "") and zone_col:
        df = df[df[zone_col].astype(str).str.strip() == str(filters.zone).strip()]

    if filters.territory not in (None, "Toutes", "") and territory_col:
        df = df[df[territory_col].astype(str).str.strip() == str(filters.territory).strip()]

    if filters.cluster not in (None, "Tous", "") and cluster_col:
        df = df[df[cluster_col].astype(str).str.strip() == str(filters.cluster).strip()]

    if filters.zone_sa not in (None, "Toutes", "") and zone_sa_col:
        target_sa = str(filters.zone_sa).strip().upper()
        df = df[df[zone_sa_col].astype(str).str.strip().str.upper() == target_sa]

    if filters.segment_group not in (None, "Tous", "Toutes", "") and segment_col:
        df = df[df[segment_col].astype(str).str.strip() == str(filters.segment_group).strip()]

    return df

@st.cache_data(ttl=300, show_spinner=False)
def _compute_cluster_coverage(
    oos_df: pd.DataFrame,
    ref_df: pd.DataFrame,
    filters: OosHvcFilters,
) -> pd.DataFrame:
    if ref_df.empty or "secteur_cluster" not in ref_df.columns:
        if ref_df.empty or "quartier" not in ref_df.columns:
            return pd.DataFrame()
        cluster_col_ref = "quartier"
    else:
        cluster_col_ref = "secteur_cluster"

    ref_filtered = _filter_ref(ref_df, filters)
    if ref_filtered.empty:
        return pd.DataFrame()

    ref_lf = pl.from_pandas(ref_filtered).lazy()
    ref_counts = ref_lf.group_by(cluster_col_ref).len().rename({cluster_col_ref: "secteur_cluster", "len": "nb_total"})

    if oos_df.empty or "cluster" not in oos_df.columns:
        return ref_counts.with_columns([
            pl.lit(0).alias("nb_oos"),
            pl.lit(0.0).alias("taux_oos_pct")
        ]).collect().to_pandas()

    oos_lf = pl.from_pandas(oos_df).lazy()
    oos_counts = oos_lf.group_by("cluster").agg(pl.col("msisdn").n_unique().alias("nb_oos")).rename({"cluster": "secteur_cluster"})

    coverage = ref_counts.join(oos_counts, on="secteur_cluster", how="left").with_columns([
        pl.col("nb_oos").fill_null(0).cast(pl.Int64),
        (pl.col("nb_oos").fill_null(0) / pl.col("nb_total").replace(0, None) * 100.0).round(1).fill_null(0.0).alias("taux_oos_pct")
    ]).sort("taux_oos_pct", descending=True).collect()

    return coverage.to_pandas()


# ---------------------------------------------------------------------------
# HVC Variation
# ---------------------------------------------------------------------------

def _build_progression_from_listing_oos(filters: OosHvcFilters) -> pd.DataFrame:
    """
    Serie horaire a partir de listing_oos (snapshots d'upload).
    Taux OOS = nb POS en rupture / total referentiel * 100.
    """
    try:
        all_oos = get_oos_listing(
            zone=filters.zone if filters.zone != "Toutes" else None,
            territory=filters.territory if filters.territory != "Toutes" else None,
            cluster=filters.cluster if filters.cluster != "Tous" else None,
            segment_group=filters.segment_group if filters.segment_group not in ("Tous", "Toutes", "") else None,
        )
    except Exception:
        return pd.DataFrame()

    if all_oos.empty or "snapshot_date" not in all_oos.columns:
        return pd.DataFrame()

    # Enrichir zone_sa puis filtrer
    if filters.zone_sa not in (None, "Toutes", ""):
        ref_df = get_all_pos()
        msisdn_ref = next((c for c in ["agent_msisdn", "msisdn"] if c in ref_df.columns), None)
        zone_sa_col = next((c for c in ["zone_sa", "Zone_SA"] if c in ref_df.columns), None)
        if msisdn_ref and zone_sa_col and "msisdn" in all_oos.columns:
            ref_sa = (
                ref_df[[msisdn_ref, zone_sa_col]]
                .assign(**{msisdn_ref: ref_df[msisdn_ref].astype(str).str.strip()})
                .rename(columns={msisdn_ref: "msisdn", zone_sa_col: "zone_sa"})
                .drop_duplicates("msisdn")
            )
            all_oos["msisdn"] = all_oos["msisdn"].astype(str).str.strip()
            all_oos = all_oos.merge(ref_sa, on="msisdn", how="left")
            all_oos = all_oos[
                all_oos["zone_sa"].astype(str).str.strip() == str(filters.zone_sa).strip()
            ]

    total_ref = get_pos_referentiel_count(
        zone=filters.zone if filters.zone not in ("Toutes", "") else None,
        territory=filters.territory if filters.territory not in ("Toutes", "") else None,
        cluster=filters.cluster if filters.cluster not in ("Tous", "") else None,
        zone_sa=filters.zone_sa if filters.zone_sa not in ("Toutes", "") else None,
        segment_group=filters.segment_group if filters.segment_group not in ("Tous", "Toutes", "") else None,
    ) or 1

    # snapshot_date peut etre "YYYY-MM-DD" ou "YYYY-MM-DD HH:MM:SS"
    ts = pd.to_datetime(all_oos["snapshot_date"], errors="coerce")
    all_oos = all_oos.loc[ts.notna()].copy()
    all_oos["_ts"] = ts
    all_oos["date"] = ts.dt.strftime("%Y-%m-%d")
    all_oos["hour"] = ts.dt.hour

    rows = []
    for (d, h), grp in all_oos.groupby(["date", "hour"]):
        nb = grp["msisdn"].nunique() if "msisdn" in grp.columns else len(grp)
        rows.append({
            "snapshot_timestamp": f"{d} {h:02d}:00:00",
            "date": d,
            "hour": int(h),
            "day_hvc": 0.0,
            "oos_pct": round(nb / total_ref * 100, 1),
            "nb_sites": nb,
            "label": f"{d[8:10]}/{d[5:7]} {h:02d}h",
        })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["date", "hour"]).reset_index(drop=True)

@st.cache_data(ttl=300, show_spinner=False)
def _build_variation_context(filters: OosHvcFilters) -> HvcVariationContext:
    available = get_hvc_snapshot_timestamps()
    empty = HvcVariationContext(available_snapshots=available)

    # Serie complete pour le graphe de progression (tous snapshots disponibles)
    progression = _build_progression_series(filters)
    if progression.empty or progression["date"].nunique() < 1:
        progression = _build_progression_from_listing_oos(filters)

    # Comparaison T1 vs T2 pour top10/flop10 et KPIs de variation
    if not filters.snapshot_t1 or not filters.snapshot_t2:
        return HvcVariationContext(
            available_snapshots=available,
            progression=progression,
            is_empty=len(available) < 2,
            message="" if len(available) >= 2 else "Chargez au moins deux snapshots pour comparer.",
        )

    if filters.snapshot_t1 == filters.snapshot_t2:
        return HvcVariationContext(
            available_snapshots=available, progression=progression,
            is_empty=True, message="Les deux snapshots doivent etre differents.",
        )

    try:
        df_t1 = _load_hvc_snapshot(filters.snapshot_t1, filters)
        df_t2 = _load_hvc_snapshot(filters.snapshot_t2, filters)
    except Exception as exc:
        return HvcVariationContext(
            available_snapshots=available, progression=progression,
            is_empty=True, message=f"Erreur chargement snapshots : {exc}",
        )

    if df_t1.empty or df_t2.empty:
        return HvcVariationContext(
            available_snapshots=available, progression=progression,
            is_empty=True, message="Donnees insuffisantes pour un des snapshots.",
        )

    merged = df_t1.merge(
        df_t2[["site_key", "day_hvc", "oos_pct"]],
        on="site_key", how="inner", suffixes=("_t1", "_t2"),
    )
    if merged.empty:
        return HvcVariationContext(
            available_snapshots=available, progression=progression,
            is_empty=True, message="Aucun site commun entre les deux snapshots.",
        )

    # Normalise oos_pct en pourcentage
    for col in ["oos_pct_t1", "oos_pct_t2"]:
        merged[col] = merged[col].apply(lambda v: v * 100 if pd.notna(v) and v <= 1.0 else v).round(1)

    merged["delta_day_hvc"] = (merged["day_hvc_t2"] - merged["day_hvc_t1"]).round(2)
    merged["delta_oos_pct"] = (merged["oos_pct_t2"] - merged["oos_pct_t1"]).round(1)

    kpis_variation = {
        "day_hvc_t1": round(df_t1["day_hvc"].mean(), 2),
        "day_hvc_t2": round(df_t2["day_hvc"].mean(), 2),
        "delta_day_hvc_global": round(df_t2["day_hvc"].mean() - df_t1["day_hvc"].mean(), 2),
        "oos_t1_pct": round(df_t1["oos_pct"].apply(lambda v: v * 100 if v <= 1.0 else v).mean(), 1),
        "oos_t2_pct": round(df_t2["oos_pct"].apply(lambda v: v * 100 if v <= 1.0 else v).mean(), 1),
        "sites_ameliores": int((merged["delta_day_hvc"] > 0).sum()),
        "sites_deteriores": int((merged["delta_day_hvc"] < 0).sum()),
        "nb_sites": len(merged),
    }

    top10 = merged.nlargest(10, "delta_day_hvc")[
        ["site_key", "day_hvc_t1", "day_hvc_t2", "delta_day_hvc", "oos_pct_t1", "oos_pct_t2", "delta_oos_pct"]
    ].reset_index(drop=True)

    flop10 = merged.nsmallest(10, "delta_day_hvc")[
        ["site_key", "day_hvc_t1", "day_hvc_t2", "delta_day_hvc", "oos_pct_t1", "oos_pct_t2", "delta_oos_pct"]
    ].reset_index(drop=True)

    return HvcVariationContext(
        table=merged, top10=top10, flop10=flop10,
        progression=progression, kpis_variation=kpis_variation,
        available_snapshots=available,
    )


def _load_hvc_snapshot(snapshot_ts: str, filters: OosHvcFilters) -> pd.DataFrame:
    """Charge un snapshot HVC avec filtres geo."""
    df = get_hvc_variations(snapshot_timestamp=snapshot_ts)
    if df.empty:
        return df
    # Filtre heure sur snapshot_timestamp
    if filters.hour_min > 0 or filters.hour_max < 23:
        try:
            df["_hour"] = pd.to_datetime(df["snapshot_timestamp"], errors="coerce").dt.hour
            df = df[df["_hour"].between(filters.hour_min, filters.hour_max)].drop(columns=["_hour"])
        except Exception:
            pass
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _build_progression_series(filters: OosHvcFilters) -> pd.DataFrame:
    available = get_hvc_snapshot_timestamps()
    rows = []
    for ts in available:
        df = _load_hvc_snapshot(ts, filters)
        if df.empty:
            continue
        dt = pd.to_datetime(ts, errors="coerce")
        row = {
            "snapshot_timestamp": ts,
            "date": dt.strftime("%Y-%m-%d") if pd.notna(dt) else "",
            "hour": dt.hour if pd.notna(dt) else 0,
            "day_hvc": round(df["day_hvc"].mean(), 2) if "day_hvc" in df.columns else 0.0,
            "oos_pct": round(
                df["oos_pct"].apply(lambda v: v * 100 if pd.notna(v) and v <= 1.0 else v).mean(), 1
            ) if "oos_pct" in df.columns else 0.0,
            "nb_sites": len(df),
        }
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).sort_values("snapshot_timestamp").reset_index(drop=True)
    result["label"] = pd.to_datetime(result["snapshot_timestamp"], errors="coerce").dt.strftime("%d/%m %Hh%M")
    return result


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _load_filter_options() -> dict:
    options = get_oos_filter_options()
    options["snapshots"] = get_hvc_snapshot_timestamps()
    return options


def _get_hvc_msisdns() -> set:
    try:
        from models.hvc_model import get_hvc_msisdns
        return set(get_hvc_msisdns())
    except Exception:
        return set()


# def _filter_ref(ref_df: pd.DataFrame, filters: OosHvcFilters) -> pd.DataFrame:
#     """Filtre le référentiel POS avec les bons noms de colonnes.

#     Le filtrage doit être strictement aligné sur les filtres actifs pour que
#     total_pos_referentiel (dénominateur du taux OOS global) soit cohérent
#     avec nb_pos_oos (numérateur extrait de listing_oos déjà filtré).
#     """
#     if ref_df is None or ref_df.empty:
#         return pd.DataFrame()

#     df = ref_df.copy()

#     # Mapping flexible des colonnes possibles
#     zone_col = next((c for c in ["zone", "zone_centre", "Zone", "Centre"] if c in df.columns), None)
#     territory_col = next((c for c in ["territory", "zone_territoire", "Territoire", "Territory"] if c in df.columns), None)
#     cluster_col = next((c for c in ["cluster", "secteur_cluster", "Cluster", "quartier"] if c in df.columns), None)
#     zone_sa_col = next((c for c in ["zone_sa", "Zone_SA", "sa_incharge"] if c in df.columns), None)
#     segment_col = next((c for c in ["segment_group", "Segment_group", "Segment"] if c in df.columns), None)

#     if filters.zone not in (None, "Toutes", "") and zone_col:
#         df = df[df[zone_col].astype(str).str.strip() == str(filters.zone).strip()]

#     if filters.territory not in (None, "Toutes", "") and territory_col:
#         df = df[df[territory_col].astype(str).str.strip() == str(filters.territory).strip()]

#     if filters.cluster not in (None, "Tous", "") and cluster_col:
#         df = df[df[cluster_col].astype(str).str.strip() == str(filters.cluster).strip()]

#     if filters.zone_sa not in (None, "Toutes", "") and zone_sa_col:
#         df = df[df[zone_sa_col].astype(str).str.strip() == str(filters.zone_sa).strip()]

#     # ⚠️ Filtre segment : OBLIGATOIRE pour aligner le dénominateur sur le numérateur
#     if filters.segment_group not in (None, "Tous", "Toutes", "") and segment_col:
#         df = df[df[segment_col].astype(str).str.strip() == str(filters.segment_group).strip()]

#     return df


@st.cache_data(ttl=120, show_spinner=False)  # 2 minutes
def build_hvc_frequently_oos(filters: OosHvcFilters, min_snapshots: int = 2) -> pd.DataFrame:
    try:
        all_oos = get_oos_listing(
            zone=filters.zone if filters.zone != "Toutes" else None,
            territory=filters.territory if filters.territory != "Toutes" else None,
            cluster=filters.cluster if filters.cluster != "Tous" else None,
        )
    except Exception:
        return pd.DataFrame()

    if all_oos.empty or "msisdn" not in all_oos.columns or "snapshot_date" not in all_oos.columns:
        return pd.DataFrame()

    lf = pl.from_pandas(all_oos).lazy()
    if "segment_group" in all_oos.columns:
        lf = lf.filter(pl.col("segment_group").cast(pl.Utf8).str.to_uppercase().str.contains("HVC"))

    res = lf.group_by("msisdn").agg([
        pl.col("snapshot_date").n_unique().alias("nb_snapshots_oos"),
        pl.col("oos_pct").cast(pl.Float64, strict=False).mean().round(1).alias("pct_oos_moy"),
        pl.col("snapshot_date").max().alias("last_snapshot_date"),
        pl.col("territory").first(),
        pl.col("cluster").first(),
        pl.col("segment_group").first(),
    ]).filter(pl.col("nb_snapshots_oos") >= min_snapshots).sort(["nb_snapshots_oos", "pct_oos_moy"], descending=[True, True]).collect()

    return res.to_pandas()

def build_hvc_variation_only(
    filters: OosHvcFilters,
    snapshot_t1: str,
    snapshot_t2: str,
) -> HvcVariationContext:
    """Reconstruit uniquement le contexte de variation avec T1/T2 choisis."""
    new_filters = OosHvcFilters(
        zone=filters.zone,
        territory=filters.territory,
        cluster=filters.cluster,
        zone_sa=filters.zone_sa,
        hour_min=filters.hour_min,
        hour_max=filters.hour_max,
        snapshot_t1=snapshot_t1,
        snapshot_t2=snapshot_t2,
    )
    return _build_variation_context(new_filters)

def enrich_oos_listing_with_deduced_commercials(
    df_listing: pd.DataFrame, 
    conn: Optional[sqlite3.Connection] = None
) -> pd.DataFrame:
    if df_listing.empty:
        return df_listing

    df_out = df_listing.copy()

    # Colonne MSISDN du POS dans la table de départ
    msisdn_col = next((c for c in ["Numero du POS", "agent_msisdn", "msisdn", "MSISDN"] if c in df_out.columns), None)
    if not msisdn_col:
        df_out["MSISDN Intervenant"] = None
        df_out["Commercial (via transactions)"] = None
        df_out["Type Intervenant"] = None
        df_out["Zone_SA (déduit)"] = None
        return df_out

    # Colonne pour le commercial actuel
    ccial_col = next((c for c in ["ccial_name", "commercial", "nom_ccial", "ccial_in_charge", "Ccial en charge"] if c in df_out.columns), None)

    if not ccial_col:
        df_out["MSISDN Intervenant"] = None
        df_out["Commercial (via transactions)"] = None
        df_out["Type Intervenant"] = None
        df_out["Zone_SA (déduit)"] = None
        return df_out

    # Repérage des POS non attribués
    is_unassigned_mask = (
        df_out[ccial_col].isna() | 
        df_out[ccial_col].astype(str).str.strip().str.upper().isin(["NON ATTRIBUÉ", "NON ATTRIBUE", "UNASSIGNED", "", "NONE", "NAN"])
    )

    unassigned_msisdns = [
        str(m).strip() for m in df_out.loc[is_unassigned_mask, msisdn_col].dropna().unique().tolist()
        if str(m).strip()
    ]

    if unassigned_msisdns:
        df_deduced = find_frequent_commercial_for_unassigned_pos(unassigned_msisdns, conn=conn)
        
        if not df_deduced.empty:
            df_out["_merge_key"] = df_out[msisdn_col].astype(str).str.strip()
            df_deduced["agent_msisdn"] = df_deduced["agent_msisdn"].astype(str).str.strip()

            # Merge
            df_out = df_out.merge(
                df_deduced[["agent_msisdn", "ccial_deduit_msisdn", "commercial_deduit", "type_acteur", "zone_sa_deduite"]],
                left_on="_merge_key",
                right_on="agent_msisdn",
                how="left"
            )

            # Affectation uniquement pour les POS 'Non attribué'
            df_out["MSISDN Intervenant"] = df_out["ccial_deduit_msisdn"].where(is_unassigned_mask, None)
            df_out["Commercial (via transactions)"] = df_out["commercial_deduit"].where(is_unassigned_mask, None)
            df_out["Type Intervenant"] = df_out["type_acteur"].where(is_unassigned_mask, None)
            df_out["Zone_SA (déduit)"] = df_out["zone_sa_deduite"].where(is_unassigned_mask, None)

            # Nettoyage des colonnes temporaires
            df_out.drop(
                columns=["_merge_key", "agent_msisdn", "ccial_deduit_msisdn", "commercial_deduit", "type_acteur", "zone_sa_deduite"], 
                inplace=True, 
                errors="ignore"
            )
        else:
            df_out["MSISDN Intervenant"] = None
            df_out["Commercial (via transactions)"] = None
            df_out["Type Intervenant"] = None
            df_out["Zone_SA (déduit)"] = None
    else:
        df_out["MSISDN Intervenant"] = None
        df_out["Commercial (via transactions)"] = None
        df_out["Type Intervenant"] = None
        df_out["Zone_SA (déduit)"] = None

    return df_out


def read_tx_files(files) -> pd.DataFrame:
    """Lit les fichiers uploadés en mémoire uniquement (pas de stockage)."""
    frames = []
    for f in files:
        try:
            if f.name.lower().endswith(".csv"):
                df = pd.read_csv(f)
            else:
                df = pd.read_excel(f)
            df["source_file"] = f.name
            frames.append(df)
        except Exception as e:
            raise ValueError(f"Erreur lecture {f.name}: {e}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out.columns = [str(c).strip() for c in out.columns]
    return out


def extract_hvc_low_balance_from_tx(
    tx_df: pd.DataFrame,
    ref_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    1. Mappe From → référentiel POS (clean_phone)
    2. Garde uniquement les HVC
    3. Dernière balance + date dernière trx par POS (via From)
    4. Filtre Balance < oos_target
    """
    if tx_df.empty or ref_df.empty:
        return pd.DataFrame()

    df = tx_df.copy()

    # Colonnes transactions
    if "From" not in df.columns:
        raise ValueError("Colonne 'From' absente des transactions")
    df["From_clean"] = df["From"].apply(clean_phone)

    df["Date"] = pd.to_datetime(df.get("Date"), errors="coerce")
    balance_col = next((c for c in ["Balance", "balance", "Solde"] if c in df.columns), None)
    if balance_col is None:
        raise ValueError("Colonne Balance absente des transactions")
    df["Balance"] = pd.to_numeric(df[balance_col], errors="coerce")

    # Référentiel
    ref = ref_df.copy()
    msisdn_col = next((c for c in ["msisdn", "agent_msisdn", "MSISDN"] if c in ref.columns), None)
    if not msisdn_col:
        raise ValueError("Colonne MSISDN absente du référentiel POS")

    ref["msisdn_clean"] = ref[msisdn_col].apply(clean_phone)

    # oos_target
    oos_target_col = next(
        (c for c in ["oos_target", "OOS_target", "OOS target"] if c in ref.columns),
        None,
    )
    if not oos_target_col:
        raise ValueError("Colonne oos_target absente du référentiel")
    ref["oos_target"] = pd.to_numeric(ref[oos_target_col], errors="coerce")

    # oos_target
    day_target_col = next(
        (c for c in ["day_target", "Day_Target"] if c in ref.columns),
        None,
    )
    if not day_target_col:
        raise ValueError("Colonne day_target absente du référentiel")
    ref["day_target"] = pd.to_numeric(ref[day_target_col], errors="coerce")

    # Segment HVC
    seg_col = next((c for c in ["segment_group", "Segment Group", "Segment"] if c in ref.columns), None)
    if seg_col:
        ref["_is_hvc"] = ref[seg_col].astype(str).str.upper().str.contains("HVC", na=False)
    else:
        ref["_is_hvc"] = True

    ref_hvc = ref[ref["_is_hvc"]].copy()

    # Colonnes utiles du référentiel
    keep_ref = ["msisdn_clean", "oos_target", "day_target"]
    for c in ["full_name", "profile", "zone", "territory", "cluster", "quartier", "site_key",
              "segment_group", "Zone", "Territory", "Cluster"]:
        if c in ref_hvc.columns:
            keep_ref.append(c)
    ref_hvc = ref_hvc[keep_ref].drop_duplicates("msisdn_clean")

    # Join transactions → HVC
    joined = df.merge(
        ref_hvc,
        left_on="From_clean",
        right_on="msisdn_clean",
        how="inner",
    )
    if joined.empty:
        return pd.DataFrame()

    # Dernière transaction par POS (From)
    joined = joined.sort_values("Date")
    last = joined.groupby("From_clean", as_index=False).last()

    # Balance < oos_target
    last = last[last["Balance"].notna() & last["oos_target"].notna()]
    last = last[last["Balance"] < last["oos_target"]].copy()

    if last.empty:
        return pd.DataFrame()

    # Format type listing_oos
    result = pd.DataFrame({
        "msisdn": last["From_clean"],
        "full_name": last.get("full_name", last.get("profile", "N/A")),
        "float_amount": last["Balance"].round(0).astype(int),
        "day_target": last["day_target"].round(0).astype(int),
        "oos_pct": last["oos_target"].round(0).astype(int),
        "last_trx_time": last["Date"],
        "zone": last.get("zone", last.get("Zone", "N/A")),
        "territory": last.get("territory", last.get("Territory", "N/A")),
        "cluster": last.get("cluster", last.get("Cluster", last.get("quartier", "N/A"))),
        "site_key": last["site_key"].values if "site_key" in last.columns else None,
        "segment_group": last.get("segment_group", "1-HVC"),
        "source": "transactions_upload",
    })

    return result.reset_index(drop=True)


def inject_into_listing_oos(pos_df: pd.DataFrame, snapshot_ts: Optional[datetime] = None) -> int:
    """
    Ajoute les POS dans listing_oos avec le snapshot de l'heure d'upload.
    Les fichiers transactions ne sont PAS stockés.
    """
    if pos_df.empty:
        return 0

    snapshot_ts = snapshot_ts or datetime.now()
    snapshot_date = snapshot_ts.strftime("%Y-%m-%d %H:%M:%S")
    snapshot_hour = snapshot_ts.strftime("%Y-%m-%d %H:%M:%S")

    rows = pos_df.copy()
    rows["snapshot_date"] = snapshot_ts.strftime("%Y-%m-%d %H:%M:%S")
    rows["snapshot_timestamp"] = snapshot_hour
    rows["is_oos"] = 1

    # Adapter à ta fonction d'insert réelle
    # Exemple :
    return insert_oos_rows(rows)


# ---------------------------------------------------------------------------
# Business Logic Revision — Classement Commerciaux & Métriques Avancées OOS
# ---------------------------------------------------------------------------

import numpy as np


@st.cache_data(ttl=120, show_spinner=False)  # 2 minutes — requête + enrichissement Polars
def get_oos_full_dataset(filters: OosHvcFilters) -> pd.DataFrame:
    """Charge l'ensemble des captures OOS selon les filtres (y compris la plage de dates)
    avec enrichissement complet du commercial et du nom POS via Polars."""
    try:
        df = get_oos_listing(
            snapshot_date=filters.snapshot_date if filters.snapshot_date else None,
            date_start=filters.date_start,
            date_end=filters.date_end,
            zone=filters.zone if filters.zone != "Toutes" else None,
            zone_sa=filters.zone_sa if filters.zone_sa != "Toutes" else None,
            territory=filters.territory if filters.territory != "Toutes" else None,
            cluster=filters.cluster if filters.cluster != "Tous" else None,
            segment_group=filters.segment_group if filters.segment_group != "Tous" else None,
        )
    except Exception:
        return pd.DataFrame()

    if df.empty or "msisdn" not in df.columns:
        return pd.DataFrame()

    lf = pl.from_pandas(df).lazy()
    lf = lf.with_columns(pl.col("msisdn").cast(pl.Utf8).str.strip_chars())
    lf = lf.filter(~pl.col("msisdn").str.to_uppercase().is_in(["TOTAL", "NONE", "NAN", ""]))

    # Enrichissement Nom POS et Zone SA depuis referentiel_pos
    ref_df = get_all_pos()
    name_col = None
    zone_sa_col = None
    if ref_df is not None and not ref_df.empty:
        msisdn_ref = next((c for c in ["msisdn", "agent_msisdn"] if c in ref_df.columns), None)
        name_col = next((c for c in ["full_name", "profile", "nom_pos", "Full name"] if c in ref_df.columns), None)
        zone_sa_col = next((c for c in ["zone_sa", "Zone_SA", "sa_incharge"] if c in ref_df.columns), None)
        if msisdn_ref:
            ref_lf = pl.from_pandas(ref_df).lazy()
            ref_select = [pl.col(msisdn_ref).cast(pl.Utf8).str.strip_chars().alias("msisdn")]
            if name_col:
                ref_select.append(pl.col(name_col).cast(pl.Utf8).alias("full_name_ref"))
            if zone_sa_col:
                ref_select.append(pl.col(zone_sa_col).cast(pl.Utf8).alias("zone_sa_ref"))
            
            ref_clean = ref_lf.select(ref_select).unique(subset=["msisdn"], keep="last")
            lf = lf.join(ref_clean, on="msisdn", how="left")
            
            if "full_name" not in df.columns and name_col:
                lf = lf.with_columns(pl.col("full_name_ref").alias("full_name"))
            elif "full_name" in df.columns and name_col:
                lf = lf.with_columns(pl.coalesce(["full_name", "full_name_ref"]).alias("full_name"))
            
            if "zone_sa" in df.columns and zone_sa_col:
                lf = lf.with_columns(pl.coalesce(["zone_sa", "zone_sa_ref"]).alias("zone_sa"))
            elif "zone_sa" not in df.columns and zone_sa_col:
                lf = lf.with_columns(pl.col("zone_sa_ref").alias("zone_sa"))

    # Fallback full_name
    if "full_name" in df.columns or (ref_df is not None and not ref_df.empty and name_col):
        lf = lf.with_columns(
            pl.coalesce([pl.col("full_name"), pl.concat_str([pl.lit("POS_"), pl.col("msisdn")])]).alias("full_name")
        )
    else:
        lf = lf.with_columns(pl.concat_str([pl.lit("POS_"), pl.col("msisdn")]).alias("full_name"))

    # Enrichissement Commercial
    try:
        mapping = get_hvc_commercial_mapping()
        if not mapping.empty and "hvc_msisdn" in mapping.columns and "commercial" in mapping.columns:
            map_lf = pl.from_pandas(mapping).lazy().select([
                pl.col("hvc_msisdn").cast(pl.Utf8).str.strip_chars().alias("msisdn"),
                pl.col("commercial").cast(pl.Utf8)
            ]).unique(subset=["msisdn"], keep="last")
            lf = lf.join(map_lf, on="msisdn", how="left").with_columns(
                pl.col("commercial").fill_null("Non attribué")
            )
        else:
            lf = lf.with_columns(pl.lit("Non attribué").alias("commercial"))
    except Exception:
        lf = lf.with_columns(pl.lit("Non attribué").alias("commercial"))

    # _day et _ts
    if "snapshot_date" in df.columns:
        lf = lf.with_columns([
            pl.col("snapshot_date").str.slice(0, 10).alias("_day"),
            pl.col("snapshot_date").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False).alias("_ts")
        ])

    collected = lf.collect()
    return collected.to_pandas()


def _format_hours_to_time_str(hours_float: float) -> str:
    """Convertit un nombre d'heures float (ex: 2.5) en format heure lisible (ex: '02h 30m')."""
    if pd.isna(hours_float) or hours_float <= 0:
        return "00h 00m"
    tot_minutes = int(round(float(hours_float) * 60))
    h = tot_minutes // 60
    m = tot_minutes % 60
    return f"{h:02d}h {m:02d}m"


@st.cache_data(ttl=120, show_spinner=False)  # 2 minutes — calcul Polars intensif
def compute_commercial_ranking(df_full: pd.DataFrame) -> pd.DataFrame:
    """Calcul du classement des commerciaux avec Polars."""
    if df_full.empty or "msisdn" not in df_full.columns or "snapshot_date" not in df_full.columns:
        return pd.DataFrame(columns=[
            "Rang", "Commercial", "POS Uniques OOS", "Cumul Apparitions OOS",
            "Fréquence Moy.", "POS Corrigés", "Taux Résolution (%)", "Score Final (/100)"
        ])

    unassigned = {"NON ATTRIBUÉ", "NON ATTRIBUE", "UNASSIGNED", "N/A", "NONE", "NAN", ""}
    lf = pl.from_pandas(df_full).lazy()

    if "commercial" in df_full.columns:
        lf = lf.filter(
            pl.col("commercial").is_not_null() &
            ~pl.col("commercial").cast(pl.Utf8).str.strip_chars().str.to_uppercase().is_in(unassigned)
        )

    lf = lf.with_columns([
        pl.col("msisdn").cast(pl.Utf8).str.strip_chars(),
        pl.col("snapshot_date").str.slice(0, 10).alias("_day")
    ])

    lf = lf.with_columns(
        (pl.col("snapshot_date") == pl.col("snapshot_date").max().over("_day")).alias("is_last_snap")
    )

    pos_day = lf.group_by(["msisdn", "_day", "commercial"]).agg(
        pl.col("is_last_snap").any().alias("in_last_snap")
    )

    pos_corrected = pos_day.filter(~pl.col("in_last_snap")).group_by("commercial").agg(
        pl.col("msisdn").n_unique().alias("pos_corriges")
    )

    comm_stats = lf.group_by("commercial").agg([
        pl.col("msisdn").n_unique().alias("pos_uniques"),
        pl.len().alias("cumul_apparitions")
    ]).join(pos_corrected, on="commercial", how="left").with_columns(
        pl.col("pos_corriges").fill_null(0)
    )

    comm_stats = comm_stats.with_columns([
        (pl.col("cumul_apparitions") / pl.col("pos_uniques")).round(2).alias("frequence_moy"),
        (pl.col("pos_corriges") / pl.col("pos_uniques") * 100.0).round(0).cast(pl.Int64).alias("taux_resolution"),
        (
            (pl.col("pos_corriges") / pl.col("pos_uniques") * 70.0) +
            pl.max_horizontal(0.0, 30.0 - (pl.col("cumul_apparitions") / pl.col("pos_uniques") * 10.0))
        ).round(2).alias("score_final")
    ]).sort(["score_final", "pos_uniques"], descending=[True, True]).collect()

    def _format_rank(idx: int) -> str:
        if idx == 0: return "🥇 1"
        if idx == 1: return "🥈 2"
        if idx == 2: return "🥉 3"
        return str(idx + 1)

    ranks = [_format_rank(i) for i in range(len(comm_stats))]
    res = comm_stats.to_pandas()
    res["Rang"] = ranks
    return res.rename(columns={
        "commercial": "Commercial",
        "pos_uniques": "POS Uniques OOS",
        "cumul_apparitions": "Cumul Apparitions OOS",
        "frequence_moy": "Fréquence Moy.",
        "pos_corriges": "POS Corrigés",
        "taux_resolution": "Taux Résolution (%)",
        "score_final": "Score Final (/100)",
    })[["Rang", "Commercial", "POS Uniques OOS", "Cumul Apparitions OOS", "Fréquence Moy.", "POS Corrigés", "Taux Résolution (%)", "Score Final (/100)"]]


def compute_pos_status(
    rupture_count: int,
    nb_days: int,
    avg_daily_app: float,
    pct_oos: float,
    streak: int,
    is_latest: bool,
    total_snaps: int = 1,
) -> str:
    """Détermine le statut d'un POS en rupture OOS :
    - 'Chronique' : Rupture lourde / persistante (streak >= 3, ou >= 35% de temps en OOS avec >= 3 apparitions, ou >= 3.0 apparitions/jour sur plusieurs jours, ou >= 3 apparitions sur courte période <= 3 snaps).
    - 'Récurrent' : Ruptures fréquentes mais intermittentes (>= 2 jours distincts, ou >= 1.8 apparitions/j, ou >= 2 apparitions totales, ou streak == 2).
    - 'Nouveau' : POS apparu en rupture uniquement sur le dernier snapshot (1ère apparition).
    - 'Occasionnel' : Rupture ponctuelle et isolée (1 seule apparition sur la période, résolue par la suite).
    """
    if streak >= 3 or (total_snaps >= 3 and pct_oos >= 35.0 and rupture_count >= 3) or (nb_days >= 2 and avg_daily_app >= 3.0) or (total_snaps <= 3 and rupture_count >= 3):
        return "Chronique"
    if nb_days >= 2 or avg_daily_app >= 1.8 or rupture_count >= 2 or streak >= 2:
        return "Récurrent"
    if rupture_count == 1 and is_latest:
        return "Nouveau"
    return "Occasionnel"


@st.cache_data(ttl=120, show_spinner=False)  # 2 minutes
def compute_frequently_oos_metrics(
    df_full: pd.DataFrame,
    behavior_filter: str = "Tous"
) -> pd.DataFrame:
    """Calcul des métriques avancées pour le tableau POS Fréquemment en OOS avec Polars."""
    if df_full.empty or "msisdn" not in df_full.columns or "snapshot_date" not in df_full.columns:
        return pd.DataFrame(columns=[
            "Nom POS", "Numéro POS", "Site", "Zone_SA", "Commercial Attribué",
            "Nombre d'apparitions (Jour)", "%OOS Moyen", "Durée Moyenne OOS (Heures)",
            "Float Moyen OOS", "Statut"
        ])

    lf = pl.from_pandas(df_full).lazy()

    lf = lf.with_columns([
        pl.col("msisdn").cast(pl.Utf8).str.strip_chars(),
        pl.col("snapshot_date").str.slice(0, 10).alias("_day"),
        pl.col("snapshot_date").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False).alias("_ts"),
        pl.col("float_amount").cast(pl.Float64, strict=False).fill_null(0.0),
    ])

    lf = lf.with_columns(
        pl.col("snapshot_date").n_unique().over("_day").alias("total_day_snaps")
    )

    site_expr = pl.col("sitename").first().alias("site") if "sitename" in df_full.columns else pl.col("site").first().alias("site") if "site" in df_full.columns else pl.lit("N/A").alias("site")
    zone_sa_expr = pl.col("zone_sa").first() if "zone_sa" in df_full.columns else pl.lit("N/A").alias("zone_sa")
    comm_expr = pl.col("commercial").first() if "commercial" in df_full.columns else pl.lit("Non attribué").alias("commercial")

    pos_daily = lf.group_by(["msisdn", "_day"]).agg([
        pl.col("full_name").first(),
        site_expr,
        zone_sa_expr,
        comm_expr,
        pl.len().alias("appearances"),
        pl.col("_ts").min().alias("min_ts"),
        pl.col("_ts").max().alias("max_ts"),
        pl.col("float_amount").sum().alias("float_sum"),
        pl.col("float_amount").count().alias("float_count"),
        pl.col("snapshot_date").max().alias("last_snap"),
        pl.col("total_day_snaps").first().alias("total_day_snaps"),
    ]).with_columns([
        (pl.col("appearances") / pl.col("total_day_snaps").replace(0, 1) * 100.0).alias("daily_oos_pct"),
        ((pl.col("max_ts") - pl.col("min_ts")).dt.total_seconds() / 3600.0).fill_null(0.0).alias("daily_duration_hours")
    ])

    all_snaps = sorted([s for s in df_full["snapshot_date"].dropna().unique()])
    total_snaps_count = len(all_snaps)
    latest_snap_dt = all_snaps[-1] if all_snaps else None
    oos_pairs = set(zip(df_full["msisdn"].astype(str).str.strip(), df_full["snapshot_date"]))

    pos_summary = pos_daily.group_by("msisdn").agg([
        pl.col("full_name").first(),
        pl.col("site").first(),
        pl.col("zone_sa").first(),
        pl.col("commercial").first(),
        pl.col("_day").n_unique().alias("nb_jours_oos"),
        pl.col("appearances").sum().alias("total_apparitions"),
        pl.col("appearances").mean().alias("avg_apparitions_jour"),
        pl.col("daily_oos_pct").mean().alias("pct_oos_moyen"),
        pl.col("daily_duration_hours").mean().alias("duree_moyenne_heures"),
        pl.col("float_sum").sum().alias("total_float_sum"),
        pl.col("float_count").sum().alias("total_float_count"),
        pl.col("last_snap").max().alias("last_snap"),
    ]).with_columns(
        pl.when(pl.col("total_float_count") > 0)
        .then(pl.col("total_float_sum") / pl.col("total_float_count"))
        .otherwise(0.0)
        .alias("float_moyen_oos")
    ).collect()

    def _calc_streak(m: str) -> int:
        c = 0
        for s in reversed(all_snaps):
            if (m, s) in oos_pairs:
                c += 1
            else:
                break
        return c

    streaks = [_calc_streak(m) for m in pos_summary["msisdn"].to_list()]

    pos_summary = pos_summary.with_columns([
        pl.Series("streak", streaks, dtype=pl.Int64),
        pl.col("avg_apparitions_jour").round(0).cast(pl.Int64).alias("avg_apparitions_jour_int"),
        pl.col("pct_oos_moyen").round(0).cast(pl.Int64).alias("pct_oos_moyen_int"),
        pl.col("float_moyen_oos").round(0).cast(pl.Int64).alias("float_moyen_oos_int"),
    ])

    pos_summary = pos_summary.with_columns(
        pl.when(
            (pl.col("streak") >= 3) |
            ((total_snaps_count >= 3) & (pl.col("pct_oos_moyen") >= 35.0) & (pl.col("total_apparitions") >= 3)) |
            ((pl.col("nb_jours_oos") >= 2) & (pl.col("avg_apparitions_jour") >= 3.0)) |
            ((total_snaps_count <= 3) & (pl.col("total_apparitions") >= 3))
        ).then(pl.lit("Chronique"))
        .when(
            (pl.col("nb_jours_oos") >= 2) |
            (pl.col("avg_apparitions_jour") >= 1.8) |
            (pl.col("total_apparitions") >= 2) |
            (pl.col("streak") >= 2)
        ).then(pl.lit("Récurrent"))
        .when(
            (pl.col("total_apparitions") == 1) &
            (pl.col("last_snap") == latest_snap_dt)
        ).then(pl.lit("Nouveau"))
        .otherwise(pl.lit("Occasionnel"))
        .alias("Statut")
    )

    if "Chronique" in behavior_filter:
        pos_summary = pos_summary.filter(pl.col("Statut") == "Chronique")
    elif "Récurrent" in behavior_filter:
        pos_summary = pos_summary.filter(pl.col("Statut") == "Récurrent")
    elif "Nouveau" in behavior_filter:
        pos_summary = pos_summary.filter(pl.col("Statut") == "Nouveau")
    elif "Occasionnel" in behavior_filter:
        pos_summary = pos_summary.filter(pl.col("Statut") == "Occasionnel")

    pos_summary = pos_summary.sort(["total_apparitions", "pct_oos_moyen"], descending=[True, True])

    res = pos_summary.to_pandas()
    res["Durée Moyenne OOS (Heures)"] = res["duree_moyenne_heures"].apply(_format_hours_to_time_str)

    return pd.DataFrame({
        "Nom POS": res["full_name"],
        "Numéro POS": res["msisdn"],
        "Site": res["site"],
        "Zone_SA": res["zone_sa"].fillna("N/A"),
        "Commercial Attribué": res["commercial"],
        "Nombre d'apparitions (Jour)": res["avg_apparitions_jour_int"],
        "%OOS Moyen": res["pct_oos_moyen_int"],
        "Durée Moyenne OOS (Heures)": res["Durée Moyenne OOS (Heures)"],
        "Float Moyen OOS": res["float_moyen_oos_int"],
        "Statut": res["Statut"]
    })


def enrich_oos_with_history(df: pd.DataFrame, filters: Optional[OosHvcFilters] = None) -> pd.DataFrame:
    """Enrichit un DataFrame de POS OOS avec les colonnes Rupture, Statut, Historique via Polars."""
    if df is None or df.empty or "msisdn" not in df.columns:
        return df

    out = df.copy()
    out["msisdn"] = out["msisdn"].astype(str).str.strip()

    try:
        date_start = filters.date_start if filters else None
        date_end = filters.date_end if filters else None

        all_oos = get_oos_listing(date_start=date_start, date_end=date_end)
        if not all_oos.empty and "msisdn" in all_oos.columns and "snapshot_date" in all_oos.columns:
            all_oos["msisdn"] = all_oos["msisdn"].astype(str).str.strip()
            recent_snaps = sorted([s for s in all_oos["snapshot_date"].unique() if s])
            total_snaps = len(recent_snaps)
            latest_snap = recent_snaps[-1] if recent_snaps else None
            oos_pairs = set(zip(all_oos["msisdn"], all_oos["snapshot_date"]))

            all_lf = pl.from_pandas(all_oos).lazy()
            all_lf = all_lf.with_columns(
                pl.col("snapshot_date").str.slice(0, 10).alias("_day")
            )

            # Daily counts
            pos_daily_counts = all_lf.group_by(["msisdn", "_day"]).agg(
                pl.len().alias("daily_app")
            )
            pos_avg_daily = pos_daily_counts.group_by("msisdn").agg(
                pl.col("daily_app").mean().alias("avg_daily_app")
            )

            grp = all_lf.group_by("msisdn").agg([
                pl.len().alias("rupture_count"),
                pl.col("_day").n_unique().alias("nb_days"),
                pl.col("snapshot_date").max().alias("last_snap")
            ]).join(pos_avg_daily, on="msisdn", how="left").with_columns(
                pl.col("avg_daily_app").fill_null(1.0),
                (pl.col("rupture_count") / max(1, total_snaps) * 100.0).alias("pct_oos")
            ).collect()

            def _get_streak(m: str) -> int:
                s_count = 0
                clean_m = str(m).strip()
                for s in reversed(recent_snaps):
                    if (clean_m, s) in oos_pairs:
                        s_count += 1
                    else:
                        break
                return s_count

            streaks = [_get_streak(m) for m in grp["msisdn"].to_list()]
            grp = grp.with_columns(pl.Series("streak", streaks, dtype=pl.Int64))

            grp = grp.with_columns(
                pl.when(
                    (pl.col("streak") >= 3) |
                    ((total_snaps >= 3) & (pl.col("pct_oos") >= 35.0) & (pl.col("rupture_count") >= 3)) |
                    ((pl.col("nb_days") >= 2) & (pl.col("avg_daily_app") >= 3.0)) |
                    ((total_snaps <= 3) & (pl.col("rupture_count") >= 3))
                ).then(pl.lit("Chronique"))
                .when(
                    (pl.col("nb_days") >= 2) |
                    (pl.col("avg_daily_app") >= 1.8) |
                    (pl.col("rupture_count") >= 2) |
                    (pl.col("streak") >= 2)
                ).then(pl.lit("Récurrent"))
                .when(
                    (pl.col("rupture_count") == 1) &
                    (pl.col("last_snap") == latest_snap)
                ).then(pl.lit("Nouveau"))
                .otherwise(pl.lit("Occasionnel"))
                .alias("statut_calc")
            )

            def _build_hist(m: str) -> str:
                return " ".join(["🟥" if (m, s) in oos_pairs else "🟩" for s in recent_snaps])

            hist_map = {m: _build_hist(m) for m in out["msisdn"].unique()}
            grp_df = grp.to_pandas()
            stat_map = dict(zip(grp_df["msisdn"], grp_df["statut_calc"]))
            rup_map = dict(zip(grp_df["msisdn"], grp_df["rupture_count"]))

            out["Rupture"] = out["msisdn"].map(rup_map).fillna(1).astype(int)
            out["Statut"] = out["msisdn"].map(stat_map).fillna("Occasionnel")
            out["Historique"] = out["msisdn"].map(hist_map).fillna("🟥")
        else:
            out["Rupture"] = 1
            out["Statut"] = "Occasionnel"
            out["Historique"] = "🟥"
    except Exception:
        out["Rupture"] = 1
        out["Statut"] = "Occasionnel"
        out["Historique"] = "🟥"

    return out