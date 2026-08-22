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

import pandas as pd

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

def build_oos_hvc_context(filters: OosHvcFilters) -> OosHvcContext:
    options = _load_filter_options()
    hvc_msisdns = _get_hvc_msisdns()

    listing_ctx = _build_listing_context(filters, hvc_msisdns)
    variation_ctx = _build_variation_context(filters)
    global_kpis = _build_global_kpis(filters, listing_ctx, variation_ctx, hvc_msisdns)

    return OosHvcContext(
        filters=filters,
        global_kpis=global_kpis,
        listing=listing_ctx,
        variation=variation_ctx,
        filter_options=options,
    )


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

#     # GARDE-FOU FINAL : Élimine tout doublon résiduel sur le MSISDN pour ce snapshot
#     df = df.drop_duplicates(subset=["msisdn"]).reset_index(drop=True)

#     par_cluster = _compute_cluster_coverage(df, ref_df, filters)

#     return OosListingContext(table=df, par_cluster=par_cluster)

def _build_listing_context(
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

def _compute_cluster_coverage(
    oos_df: pd.DataFrame,
    ref_df: pd.DataFrame,
    filters: OosHvcFilters,
) -> pd.DataFrame:
    if ref_df.empty or "secteur_cluster" not in ref_df.columns:
        # Essayer avec la colonne aliasée 'quartier'
        if ref_df.empty or "quartier" not in ref_df.columns:
            return pd.DataFrame()
        cluster_col_ref = "quartier"
    else:
        cluster_col_ref = "secteur_cluster"

    ref_filtered = _filter_ref(ref_df, filters)
    ref_counts = ref_filtered.groupby(cluster_col_ref).size().reset_index(name="nb_total")
    ref_counts = ref_counts.rename(columns={cluster_col_ref: "secteur_cluster"})

    if "cluster" not in oos_df.columns:
        return pd.DataFrame()

    oos_counts = oos_df.groupby("cluster")["msisdn"].nunique().reset_index(name="nb_oos")
    oos_counts = oos_counts.rename(columns={"cluster": "secteur_cluster"})
    coverage = ref_counts.merge(oos_counts, on="secteur_cluster", how="left")
    coverage["nb_oos"] = coverage["nb_oos"].fillna(0).astype(int)
    coverage["taux_oos_pct"] = (
        coverage["nb_oos"] / coverage["nb_total"].replace(0, pd.NA) * 100
    ).round(1)
    return coverage.sort_values("taux_oos_pct", ascending=False).reset_index(drop=True)


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


def _build_progression_series(filters: OosHvcFilters) -> pd.DataFrame:
    """Serie temporelle complete : moyenne de day_hvc et oos_pct par snapshot et par heure.

    Chaque ligne = un snapshot horodate. C'est la source du graphe de
    progression horaire J vs J-1.
    """
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


def build_hvc_frequently_oos(filters: OosHvcFilters, min_snapshots: int = 2) -> pd.DataFrame:
    try:
        all_oos = get_oos_listing(
            zone=filters.zone if filters.zone != "Toutes" else None,
            territory=filters.territory if filters.territory != "Toutes" else None,
            cluster=filters.cluster if filters.cluster != "Tous" else None,
            # Plus de segment_group="HVC" ici — filtrage fait ci-dessous.
        )
    except Exception:
        return pd.DataFrame()

    if all_oos.empty or "msisdn" not in all_oos.columns or "snapshot_date" not in all_oos.columns:
        return pd.DataFrame()

    if "segment_group" in all_oos.columns:
        all_oos = all_oos[all_oos["segment_group"].astype(str).str.contains("HVC", case=False, na=False)]
    if all_oos.empty:
        return pd.DataFrame()

    grp = (
        all_oos.groupby("msisdn")
        .agg(
            nb_snapshots_oos=("snapshot_date", "nunique"),
            pct_oos_moy=("oos_pct", "mean"),
            last_snapshot_date=("snapshot_date", "max"),
            territory=("territory", "first"),
            cluster=("cluster", "first"),
            segment_group=("segment_group", "first"),
        )
        .reset_index()
    )
    result = grp[grp["nb_snapshots_oos"] >= min_snapshots].copy()
    result["pct_oos_moy"] = result["pct_oos_moy"].round(1)
    return result.sort_values(["nb_snapshots_oos", "pct_oos_moy"], ascending=[False, False]).reset_index(drop=True)

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


def get_oos_full_dataset(filters: OosHvcFilters) -> pd.DataFrame:
    """Charge l'ensemble des captures OOS selon les filtres (y compris la plage de dates)
    avec enrichissement complet du commercial et du nom POS."""
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

    if df.empty:
        return pd.DataFrame()

    if "msisdn" in df.columns:
        df["msisdn"] = df["msisdn"].astype(str).str.strip()
        df = df[~df["msisdn"].astype(str).str.upper().isin(["TOTAL", "NONE", "NAN", ""])]

    # Enrichissement Nom POS et Zone SA depuis referentiel_pos
    ref_df = get_all_pos()
    if ref_df is not None and not ref_df.empty and "msisdn" in df.columns:
        msisdn_ref = next((c for c in ["msisdn", "agent_msisdn"] if c in ref_df.columns), None)
        name_col = next((c for c in ["full_name", "profile", "nom_pos", "Full name"] if c in ref_df.columns), None)
        zone_sa_col = next((c for c in ["zone_sa", "Zone_SA", "sa_incharge"] if c in ref_df.columns), None)
        if msisdn_ref:
            cols = [msisdn_ref]
            if name_col:
                cols.append(name_col)
            if zone_sa_col:
                cols.append(zone_sa_col)
            ref_sub = ref_df[cols].copy()
            ref_sub[msisdn_ref] = ref_sub[msisdn_ref].astype(str).str.strip()
            rename_map = {msisdn_ref: "msisdn"}
            if name_col:
                rename_map[name_col] = "full_name_ref"
            if zone_sa_col:
                rename_map[zone_sa_col] = "zone_sa_ref"
            ref_clean = ref_sub.rename(columns=rename_map).drop_duplicates(subset=["msisdn"], keep="last")

            if "full_name" not in df.columns and "full_name_ref" in ref_clean.columns:
                df = df.merge(ref_clean[["msisdn", "full_name_ref"]], on="msisdn", how="left").rename(columns={"full_name_ref": "full_name"})
            if "zone_sa" in df.columns and "zone_sa_ref" in ref_clean.columns:
                df["zone_sa"] = df["zone_sa"].fillna(df["msisdn"].map(ref_clean.set_index("msisdn")["zone_sa_ref"]))

    if "full_name" not in df.columns or df["full_name"].isna().all():
        df["full_name"] = "POS_" + df["msisdn"].astype(str)
    else:
        df["full_name"] = df["full_name"].fillna("POS_" + df["msisdn"].astype(str))

    # Enrichissement Commercial
    try:
        mapping = get_hvc_commercial_mapping()
        if not mapping.empty and "msisdn" in df.columns:
            mapping = mapping.copy()
            mapping["hvc_msisdn"] = mapping["hvc_msisdn"].astype(str).str.strip()
            mapping_clean = mapping[["hvc_msisdn", "commercial"]].drop_duplicates(subset=["hvc_msisdn"])
            df = df.merge(mapping_clean.rename(columns={"hvc_msisdn": "msisdn"}), on="msisdn", how="left")
            df["commercial"] = df["commercial"].fillna("Non attribué")
        else:
            df["commercial"] = "Non attribué"
    except Exception:
        df["commercial"] = "Non attribué"

    # Convertir snapshot_date en datetime et string date
    if "snapshot_date" in df.columns:
        df["_ts"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
        df["_day"] = df["_ts"].dt.strftime("%Y-%m-%d")

    return df


def _format_hours_to_time_str(hours_float: float) -> str:
    """Convertit un nombre d'heures float (ex: 2.5) en format heure lisible (ex: '02h 30m')."""
    if pd.isna(hours_float) or hours_float <= 0:
        return "00h 00m"
    tot_minutes = int(round(float(hours_float) * 60))
    h = tot_minutes // 60
    m = tot_minutes % 60
    return f"{h:02d}h {m:02d}m"


def compute_commercial_ranking(df_full: pd.DataFrame) -> pd.DataFrame:
    """Calcul du classement des commerciaux (Score /100, Taux Résolution, Fréquence Moyenne).

    Sans aucune jointure avec la table transactions.
    Formule:
    - POS Corrigés: POS ayant disparu lors d'une capture ultérieure de la même journée.
    - Poids Correction (70 max) = (POS Corrigés / POS Uniques OOS) * 70
    - Bonus Stabilité (30 max) = MAX(0, 30 - (Fréquence Moyenne OOS par POS * 10))
    - Score Final = Poids Correction + Bonus Stabilité
    """
    if df_full.empty or "msisdn" not in df_full.columns or "snapshot_date" not in df_full.columns:
        return pd.DataFrame(columns=[
            "Rang", "Commercial", "POS Uniques OOS", "Cumul Apparitions OOS",
            "Fréquence Moy.", "POS Corrigés", "Taux Résolution (%)", "Score Final (/100)"
        ])

    df = df_full.copy()
    if "commercial" in df.columns:
        unassigned_labels = ["NON ATTRIBUÉ", "NON ATTRIBUE", "UNASSIGNED", "N/A", "NONE", "NAN", ""]
        df = df[~df["commercial"].astype(str).str.strip().str.upper().isin(unassigned_labels)]

    if df.empty or "msisdn" not in df.columns or "snapshot_date" not in df.columns:
        return pd.DataFrame(columns=[
            "Rang", "Commercial", "POS Uniques OOS", "Cumul Apparitions OOS",
            "Fréquence Moy.", "POS Corrigés", "Taux Résolution (%)", "Score Final (/100)"
        ])

    if "_day" not in df.columns:
        df["_ts"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
        df["_day"] = df["_ts"].dt.strftime("%Y-%m-%d")

    # Déterminer pour chaque jour la dernière capture exécutée
    daily_last_snap = df.groupby("_day")["snapshot_date"].max().to_dict()
    df["is_last_snap_of_day"] = df.apply(lambda r: r["snapshot_date"] == daily_last_snap.get(r["_day"]), axis=1)

    # Récapitulatif par POS et par jour
    pos_day_grp = df.groupby(["msisdn", "_day", "commercial"]).agg(
        appearances=("snapshot_date", "count"),
        in_last_snap=("is_last_snap_of_day", "any")
    ).reset_index()

    # Un POS est corrigé le jour d s'il est apparu dans l'OOS mais n'était plus présent lors de la dernière capture du jour
    pos_day_grp["corrected"] = ~pos_day_grp["in_last_snap"]

    # Identification des POS uniques corrigés par commercial sur la période
    pos_corrected_by_comm = (
        pos_day_grp[pos_day_grp["corrected"]]
        .groupby("commercial")["msisdn"]
        .nunique()
        .to_dict()
    )

    # Statistiques globales par commercial
    comm_stats = df.groupby("commercial").agg(
        pos_uniques=("msisdn", "nunique"),
        cumul_apparitions=("snapshot_date", "count")
    ).reset_index()

    comm_stats["pos_corriges"] = comm_stats["commercial"].map(pos_corrected_by_comm).fillna(0).astype(int)
    comm_stats["frequence_moy"] = (comm_stats["cumul_apparitions"] / comm_stats["pos_uniques"].replace(0, 1)).round(2)
    comm_stats["taux_resolution"] = (comm_stats["pos_corriges"] / comm_stats["pos_uniques"].replace(0, 1) * 100.0).round(0).astype(int)

    # Calcul du Score Final (/100) (float à 2 décimales)
    poids_corr = (comm_stats["pos_corriges"] / comm_stats["pos_uniques"].replace(0, 1)) * 70.0
    bonus_stab = (30.0 - comm_stats["frequence_moy"] * 10.0).clip(lower=0.0)
    comm_stats["score_final"] = (poids_corr + bonus_stab).round(2)

    # Tri par score décroissant puis POS uniques
    comm_stats = comm_stats.sort_values(by=["score_final", "pos_uniques"], ascending=[False, False]).reset_index(drop=True)

    # Assignation des rangs avec emojis
    def _format_rank(idx: int) -> str:
        if idx == 0: return "🥇 1"
        if idx == 1: return "🥈 2"
        if idx == 2: return "🥉 3"
        return str(idx + 1)

    comm_stats["Rang"] = [ _format_rank(i) for i in range(len(comm_stats)) ]

    ranking_df = comm_stats.rename(columns={
        "commercial": "Commercial",
        "pos_uniques": "POS Uniques OOS",
        "cumul_apparitions": "Cumul Apparitions OOS",
        "frequence_moy": "Fréquence Moy.",
        "pos_corriges": "POS Corrigés",
        "taux_resolution": "Taux Résolution (%)",
        "score_final": "Score Final (/100)",
    })

    cols_order = [
        "Rang", "Commercial", "POS Uniques OOS", "Cumul Apparitions OOS",
        "Fréquence Moy.", "POS Corrigés", "Taux Résolution (%)", "Score Final (/100)"
    ]
    return ranking_df[cols_order]


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


def compute_frequently_oos_metrics(
    df_full: pd.DataFrame,
    behavior_filter: str = "Tous"
) -> pd.DataFrame:
    """Calcul des métriques avancées pour le tableau POS Fréquemment en OOS.

    Métriques:
    - %OOS Moyen: % de captures OOS par jour (entier %OOS Moyen).
    - Durée Moyenne OOS (Heures): format lisible 'XXh YYm'.
    - Float Moyen OOS: solde moyen (entier).
    - Nombre d'apparitions (Jour): nombre moyen d'apparitions par jour (entier).
    - Colonne Site: site d'attachement du POS.
    - Statut: "Chronique", "Récurrent", "Nouveau", "Occasionnel".
    """
    if df_full.empty or "msisdn" not in df_full.columns or "snapshot_date" not in df_full.columns:
        return pd.DataFrame(columns=[
            "Nom POS", "Numéro POS", "Site", "Zone_SA", "Commercial Attribué",
            "Nombre d'apparitions (Jour)", "%OOS Moyen", "Durée Moyenne OOS (Heures)",
            "Float Moyen OOS", "Statut"
        ])

    df = df_full.copy()
    if "_day" not in df.columns:
        df["_ts"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
        df["_day"] = df["_ts"].dt.strftime("%Y-%m-%d")

    # Nombre total de snapshots distincts exécutés par jour
    total_snaps_per_day = df.groupby("_day")["snapshot_date"].nunique().to_dict()

    # Agrégation quotidienne par POS
    pos_daily = df.groupby(["msisdn", "_day"]).agg(
        full_name=("full_name", "first"),
        site=("sitename", "first"),
        zone_sa=("zone_sa", "first"),
        commercial=("commercial", "first"),
        appearances=("snapshot_date", "count"),
        min_ts=("_ts", "min"),
        max_ts=("_ts", "max"),
        float_sum=("float_amount", "sum"),
        float_count=("float_amount", "count"),
        last_snap=("snapshot_date", "max")
    ).reset_index()

    pos_daily["total_day_snaps"] = pos_daily["_day"].map(total_snaps_per_day)
    pos_daily["daily_oos_pct"] = (pos_daily["appearances"] / pos_daily["total_day_snaps"].replace(0, 1)) * 100.0
    pos_daily["daily_duration_hours"] = (pos_daily["max_ts"] - pos_daily["min_ts"]).dt.total_seconds() / 3600.0

    all_snaps_sorted = sorted(df["snapshot_date"].dropna().unique())
    total_snaps_count = len(all_snaps_sorted)
    latest_snap_dt = all_snaps_sorted[-1] if all_snaps_sorted else None
    oos_pairs = set(zip(df["msisdn"].astype(str).str.strip(), df["snapshot_date"]))

    def _get_streak(m: str) -> int:
        s_count = 0
        clean_m = str(m).strip()
        for s in reversed(all_snaps_sorted):
            if (clean_m, s) in oos_pairs:
                s_count += 1
            else:
                break
        return s_count

    # Agrégation sur la période par POS
    pos_summary = pos_daily.groupby("msisdn").agg(
        full_name=("full_name", "first"),
        site=("site", "first"),
        zone_sa=("zone_sa", "first"),
        commercial=("commercial", "first"),
        nb_jours_oos=("_day", "nunique"),
        total_apparitions=("appearances", "sum"),
        avg_apparitions_jour=("appearances", "mean"),
        pct_oos_moyen=("daily_oos_pct", "mean"),
        duree_moyenne_heures=("daily_duration_hours", "mean"),
        float_sum=("float_sum", "sum"),
        float_count=("float_count", "sum"),
        last_snap=("last_snap", "max")
    ).reset_index()

    pos_summary["float_moyen_oos"] = np.where(
        pos_summary["float_count"] > 0,
        pos_summary["float_sum"] / pos_summary["float_count"],
        0.0
    )

    pos_summary["streak"] = pos_summary["msisdn"].apply(_get_streak)

    # Typage & Formatage
    pos_summary["avg_apparitions_jour_int"] = pos_summary["avg_apparitions_jour"].round(0).astype(int)
    pos_summary["pct_oos_moyen_int"] = pos_summary["pct_oos_moyen"].round(0).astype(int)
    pos_summary["float_moyen_oos_int"] = pos_summary["float_moyen_oos"].round(0).astype(int)
    pos_summary["duree_moyenne_fmt"] = pos_summary["duree_moyenne_heures"].apply(_format_hours_to_time_str)

    # Évaluation du statut dynamique
    pos_summary["Statut"] = pos_summary.apply(
        lambda r: compute_pos_status(
            rupture_count=int(r["total_apparitions"]),
            nb_days=int(r["nb_jours_oos"]),
            avg_daily_app=float(r["avg_apparitions_jour"]),
            pct_oos=float(r["pct_oos_moyen"]),
            streak=int(r["streak"]),
            is_latest=(r["last_snap"] == latest_snap_dt),
            total_snaps=total_snaps_count
        ),
        axis=1
    )

    # Filtre de comportement
    if "Chronique" in behavior_filter:
        pos_summary = pos_summary[pos_summary["Statut"] == "Chronique"]
    elif "Récurrent" in behavior_filter:
        pos_summary = pos_summary[pos_summary["Statut"] == "Récurrent"]
    elif "Nouveau" in behavior_filter:
        pos_summary = pos_summary[pos_summary["Statut"] == "Nouveau"]
    elif "Occasionnel" in behavior_filter:
        pos_summary = pos_summary[pos_summary["Statut"] == "Occasionnel"]

    pos_summary = pos_summary.sort_values(by=["total_apparitions", "pct_oos_moyen"], ascending=[False, False]).reset_index(drop=True)

    result_df = pd.DataFrame({
        "Nom POS": pos_summary["full_name"],
        "Numéro POS": pos_summary["msisdn"],
        "Site": pos_summary["site"],
        "Zone_SA": pos_summary["zone_sa"].fillna("N/A"),
        "Commercial Attribué": pos_summary["commercial"],
        "Nombre d'apparitions (Jour)": pos_summary["avg_apparitions_jour_int"],
        "%OOS Moyen": pos_summary["pct_oos_moyen_int"],
        "Durée Moyenne OOS (Heures)": pos_summary["duree_moyenne_fmt"],
        "Float Moyen OOS": pos_summary["float_moyen_oos_int"],
        "Statut": pos_summary["Statut"]
    })

    return result_df


def enrich_oos_with_history(df: pd.DataFrame, filters: Optional[OosHvcFilters] = None) -> pd.DataFrame:
    """Enrichit un DataFrame de POS OOS avec les colonnes :
    - Rupture: Nombre d'apparitions du POS sur la période.
    - Statut: "Chronique", "Récurrent", "Nouveau", "Occasionnel".
    - Historique: Suite de carrés (🟥 = OOS, 🟩 = Absent de l'OOS) sur les snapshots de la période.
    """
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
            all_oos["_ts"] = pd.to_datetime(all_oos["snapshot_date"], errors="coerce")
            all_oos["_day"] = all_oos["_ts"].dt.strftime("%Y-%m-%d")

            # Tous les snapshots de la période sélectionnée (triés chronologiquement)
            recent_snaps = sorted([s for s in all_oos["snapshot_date"].unique() if s])
            total_snaps = len(recent_snaps)
            latest_snap = recent_snaps[-1] if recent_snaps else None
            oos_pairs = set(zip(all_oos["msisdn"], all_oos["snapshot_date"]))

            # Calcul des apparitions par jour pour évaluer le statut à l'échelle d'une journée
            pos_daily_counts = all_oos.groupby(["msisdn", "_day"])["snapshot_date"].count().reset_index(name="daily_app")
            pos_avg_daily = pos_daily_counts.groupby("msisdn")["daily_app"].mean().reset_index(name="avg_daily_app")

            grp = all_oos.groupby("msisdn").agg(
                rupture_count=("snapshot_date", "count"),
                nb_days=("_day", "nunique"),
                last_snap=("snapshot_date", "max")
            ).reset_index()

            grp = grp.merge(pos_avg_daily, on="msisdn", how="left")
            grp["avg_daily_app"] = grp["avg_daily_app"].fillna(1.0)
            grp["pct_oos"] = (grp["rupture_count"] / max(1, total_snaps)) * 100.0

            # Streak consécutif depuis le dernier snapshot
            def _get_streak(m: str) -> int:
                s_count = 0
                clean_m = str(m).strip()
                for s in reversed(recent_snaps):
                    if (clean_m, s) in oos_pairs:
                        s_count += 1
                    else:
                        break
                return s_count

            grp["streak"] = grp["msisdn"].apply(_get_streak)

            grp["statut_calc"] = grp.apply(
                lambda r: compute_pos_status(
                    rupture_count=int(r["rupture_count"]),
                    nb_days=int(r["nb_days"]),
                    avg_daily_app=float(r["avg_daily_app"]),
                    pct_oos=float(r["pct_oos"]),
                    streak=int(r["streak"]),
                    is_latest=(r["last_snap"] == latest_snap),
                    total_snaps=total_snaps
                ),
                axis=1
            )

            def _build_hist(m: str) -> str:
                return " ".join(["🟥" if (m, s) in oos_pairs else "🟩" for s in recent_snaps])

            grp["historique_calc"] = grp["msisdn"].apply(_build_hist)

            out = out.merge(
                grp[["msisdn", "rupture_count", "statut_calc", "historique_calc"]],
                on="msisdn",
                how="left"
            )

            out["Rupture"] = out["rupture_count"].fillna(1).astype(int)
            out["Statut"] = out["statut_calc"].fillna("Occasionnel")
            out["Historique"] = out["historique_calc"].fillna("🟥")

            out.drop(columns=["rupture_count", "statut_calc", "historique_calc"], inplace=True, errors="ignore")
        else:
            out["Rupture"] = 1
            out["Statut"] = "Occasionnel"
            out["Historique"] = "🟥"
    except Exception:
        out["Rupture"] = 1
        out["Statut"] = "Occasionnel"
        out["Historique"] = "🟥"

    return out