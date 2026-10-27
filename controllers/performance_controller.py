"""Performance controller (Tache 5.3 — corrections post-revue).

Point d'orchestration unique pour les trois segments de performance —
Commercial, PR & Caisses, CDS — qui etaient jusqu'ici dupliques entre
pages/perf.py, pages/cds_perf.py et pages/pr_caisse_perf.py.

Principes
---------
- Tout le filtrage SQL (date, heure, type de transaction, montant, jointure
  acteur) est fait dans models.performance_model.get_performance_transactions
  / get_dotation_transactions. Ce controller ne fait que l'agregation
  metier sur les DataFrames deja reduits qui en resultent.
- La regle d'exclusion des comptes internes est UNIQUE et vient de
  controllers.exclusion (jamais reimplementee ici).
- Le filtre HEURE est applique ici et NULLE PART AILLEURS dans l'app — il
  ne doit jamais fuiter vers dashboard_controller / conquete_controller /
  oos_controller. Pour le segment "cds", il est explicitement coupe a None
  dans _load_and_scope_transactions, meme si la vue envoie des valeurs par
  erreur (pas seulement documente ici, verrouille dans le code).
- Tendance intraday [14h-17h] (POS_serve / New) : UNIQUEMENT pour pr_caisse,
  et uniquement sur les lignes Type_Point == "Caisses". Le CDS n'a plus de
  tendance (correction post-revue : elle avait ete laissee par erreur).
- Work Progress (3 fenetres intraday, decompte Serve) : commercial (avec
  taux de rotation TR) ET pr_caisse/Caisses (Serve seul, sans TR — correction
  post-revue). Jamais pour Point Relais ni pour CDS.
- Le Top 10 (score composite) est calcule pour "commercial" ET "pr_caisse"
  (decision Q1), jamais pour "cds" — mais avec des ponderations DIFFERENTES
  entre les deux segments (cf. hypothese 7 ci-dessous, corrigee).

Hypotheses/decisions actees — statut a jour apres la revue :
1. Le CDS ne suit que le segment HVC (pas de split Others/MVC-LVC) : c'est
   le comportement de cds_perf.py (get_hvc_numbers, pas de gestion MVC/LVC).
2. Dotation Commercial : au plus UNE transaction Master ET UNE transaction
   Caisse par (jour, commercial), recues avant 11h — logique
   `first_master`/`first_caisse` `.head(1)` de perf.py.
3. Dotation PR/Caisse : dotateur "principal" (Commercial ou CDS ayant le
   plus de transactions vers ce PR/Caisse), sans limite d'heure.
4. Dotation CDS : Master UNIQUEMENT (correction post-revue — la Caisse avait
   ete ajoutee par erreur ; on revient au comportement d'origine de
   cds_perf.py, qui ne suivait que Master), dotateur principal = celui avec
   le plus de transactions vers ce CDS, sans limite d'heure.
5. Tendance intraday (POS_serve / New) : UNIQUEMENT pr_caisse/Caisses. Retiree
   de CDS (correction post-revue).
6. Work Progress (3 fenetres) : commercial (Serve + TR, "Other" = tout ce qui
   n'est pas HVC, cf. compute_time_progress_work d'origine) ET pr_caisse/
   Caisses (Serve UNIQUEMENT, sans TR — demande explicite ; ici "Other" suit
   le split strict MVC/LVC du tableau principal, pas la definition large de
   commercial, par coherence avec le reste du tableau PR/Caisses).
7. Top 10 : deux formules DIFFERENTES, non partagees :
   - commercial : 5 criteres a 20% chacun (Nb_Jours, HVC_Serve, TR_General,
     Sigma_FD, precocite de la 1ere transaction) — inchange.
   - pr_caisse : 3 criteres a poids egal (~33.3% chacun), SANS TR ni
     precocite : Nb_Jours, Sigma_POS_Serve (tous segments, pas seulement
     HVC), Sigma_FD. Restreint aux lignes Type_Point == "Point Relais"
     uniquement (jamais les Caisses).

Note technique (pas une decision metier) : models.performance_model.
get_performance_transactions ne prend pas encore de filtres
zone_centre/zone_territoire/zone_sa/territoire/type_point en parametres
SQL — ce controller filtre donc ces dimensions en memoire apres la
jointure. Si le volume de transactions devient significatif, on pourra
pousser ces filtres au niveau SQL dans un futur passage sur le modele.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from app_config.settings import MIN_TRANSFER_AMOUNT
from controllers.exclusion import get_excluded_msisdns
from models.performance_model import (
    Segment,
    get_cds_referentiel,
    get_cds_commercial_flows,
    get_commercial_referentiel,
    get_dotation_transactions,
    get_float_up_transactions,
    get_hvc_msisdns,
    get_hvc_quota_by_cds,
    get_hvc_cds_assignments,
    get_mvc_lvc_msisdns,
    get_performance_filter_options,
    get_performance_transactions,
    get_point_relay_referentiel,
)
from models.reference_model import get_exclusions
from models.db import get_connection
from utils.helpers import clean_phone
from models.performance_polars import compute_coverage_persistence
from models.performance_polars import (
    compute_daily_activity_polars,
    compute_hvc_others_polars,
    compute_pr_portfolio_metrics,
)

# ---------------------------------------------------------------------------
# Configuration par segment
# ---------------------------------------------------------------------------

WORK_PROGRESS_WINDOWS: dict[str, tuple[int, int]] = {
    "6h-9h50": (6 * 60, 9 * 60 + 50),
    "9h50-13h50": (9 * 60 + 50, 13 * 60 + 50),
    "13h50-17h50": (13 * 60 + 50, 17 * 60 + 50),
}
AFTERNOON_TREND_WINDOW = (14, 17)   # heures, borne haute exclue
MORNING_TREND_WINDOW = (6, 14)      # heures, borne haute incluse

_DOTATION_RULES: dict[Segment, dict[str, Any]] = {
    "commercial": {"sources": ("master", "caisse"), "single_per_source": True, "hour_max": 11},
    "pr_caisse": {"sources": ("commercial", "cds"), "single_per_source": False, "hour_max": None},
    # Correction post-revue : CDS = Master UNIQUEMENT (comportement d'origine
    # de cds_perf.py ; la Caisse avait ete ajoutee par erreur dans une
    # iteration precedente et n'a jamais ete demandee).
    "cds": {"sources": ("master",), "single_per_source": False, "hour_max": None},
}

_SEGMENT_HVC_OTHERS_SPLIT: dict[Segment, bool] = {
    "commercial": True,
    "pr_caisse": True,
    "cds": False,  # cf. hypothese 1
}

# Segments beneficiant du Top 10 (score composite) — Commercial + PR_Caisses,
# jamais CDS. Les deux segments utilisent des formules DIFFERENTES (cf.
# _rank_top_actors et hypothese 7).
_TOP10_SEGMENTS: set[Segment] = {"commercial", "pr_caisse"}


@dataclass(frozen=True)
class PerformanceFilters:
    segment: Segment
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    hour_start: Optional[int] = None   # actif uniquement si start_date == end_date (decide par la vue)
    hour_end: Optional[int] = None
    zone_centre: str = "Toutes"        # commercial uniquement (Centre II / Centre III)
    zone_territoire: str = "Toutes"    # commercial uniquement
    zone_sa: str = "Toutes"            # commercial uniquement
    type_point: str = "Tous"           # pr_caisse uniquement (Point Relais / Caisses)
    territoire: str = "Toutes"         # pr_caisse uniquement
    mapping_hash: Optional[str] = None # Hash du fichier de mapping manuel si fourni
    tr_mode: str = "actifs"            # "actifs" (jours travaillés) ou "ouvres_26j" (6j/7, standard 26j)


def count_working_days_6j(start_date: Optional[str], end_date: Optional[str]) -> int:
    """Calcule le nombre de jours ouvrés (6j/7, dimanches exclus) sur la période.
    Pour un mois complet (28 à 31 jours), retourne la norme standard de 26 jours.
    Pour une période partielle ou personnalisée, compte les jours réels hors dimanches.
    """
    if not start_date or not end_date:
        return 26
    try:
        dt_start = pd.to_datetime(start_date).date()
        dt_end = pd.to_datetime(end_date).date()
        if dt_start > dt_end:
            dt_start, dt_end = dt_end, dt_start
        total_days = (dt_end - dt_start).days + 1
        if 28 <= total_days <= 31:
            return 26
        dates = pd.date_range(start=dt_start, end=dt_end)
        working_days = sum(1 for d in dates if d.weekday() != 6)
        return max(1, working_days)
    except Exception:
        return 26


@dataclass(frozen=True)
class PerformanceContext:
    segment: Segment
    filters: PerformanceFilters
    table: pd.DataFrame                                   # une ligne par acteur, colonnes numeriques pretes pour la vue
    top_commerciaux: pd.DataFrame = field(default_factory=pd.DataFrame)  # commercial + pr_caisse uniquement
    is_empty: bool = False
    message: str = ""


def load_filter_options(segment: Segment) -> dict[str, Any]:
    """Options de filtre (bornes de dates + listes specifiques au segment)."""
    try:
        return get_performance_filter_options(segment)
    except Exception as exc:
        return {"min_date": None, "max_date": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_performance_context(
    filters: PerformanceFilters,
    manual_mapping: Optional[pd.DataFrame] = None,
) -> PerformanceContext:
    segment = filters.segment
    if manual_mapping is None:
        try:
            import streamlit as st
            manual_mapping = st.session_state.get("perf_commercial_manual_mapping")
        except Exception:
            manual_mapping = None

    try:
        excluded = get_excluded_msisdns()
        actor_pool = _load_actor_pool(filters)
        tx = _load_and_scope_transactions(filters, excluded)
    except Exception as exc:
        return _empty_context(filters, f"Impossible de charger les donnees de performance ({segment}): {exc}")

    if actor_pool.empty:
        return _empty_context(filters, "Aucun acteur trouve pour ce segment/ces filtres.")

    base_cols = ["Nb_Jours", "FD_HVC", "HVC_Serve", "FD_Others", "Other_Serve", "Sigma_FD", "Sigma_POS_Serve"]

    if tx.empty:
        table = actor_pool.copy()
        for col in base_cols:
            table[col] = 0
        table["FU"] = 0
        table["POS_Touches"] = 0
        table["POS_Attribues"] = 0
        table["Taux_Couverture"] = 0.0
        table["POS_Touches_HVC"] = 0
        table["POS_Attribues_HVC"] = 0
        table["Taux_Couverture_HVC"] = 0.0
        table["POS_Touches_Others"] = 0
        table["POS_Attribues_Others"] = 0
        table["Taux_Couverture_Others"] = 0.0
        table["HVC_Wallet"] = 0
        table["HVC_No_Wallet"] = 0
        table["Sigma_HVC_Serve"] = 0
        table["Others_Wallet"] = 0
        table["Others_No_Wallet"] = 0
        table["Tot_No_Wallet"] = 0
        table["Sigma_Others_Serve"] = 0
        table["Nb_Trans_HVC_Wallet"] = 0
        table["Nb_Trans_HVC_No_Wallet"] = 0
        table["Nb_Trans_Others_Wallet"] = 0
        table["Nb_Trans_Others_No_Wallet"] = 0
        table["FD_HVC_Wallet"] = 0
        table["FD_Others_Wallet"] = 0
        table["FD_Tot_Wallet"] = 0
        table["FD_HVC_No_Wallet"] = 0
        table["FD_Others_No_Wallet"] = 0
        table["FD_Tot_No_Wallet"] = 0
        table["TR_HVC_Wallet"] = 0.0
        table["TR_HVC_No_Wallet"] = 0.0
        table["TR_HVC_General"] = 0.0
        table["TR_Others_Wallet"] = 0.0
        table["TR_Others_No_Wallet"] = 0.0
        table["TR_Others_General"] = 0.0
        table["TR_HVC"] = 0.0
        table["TR_Other"] = 0.0
        table["TR_General"] = 0.0
        table["Taux_Persistance"] = None

        cov = compute_coverage_persistence(tx, segment, filters.start_date, filters.end_date)
        if not cov.empty and "Actor_MSISDN" in cov.columns:
            if "POS_Attribues" in cov.columns:
                table["POS_Attribues"] = table["Actor_MSISDN"].map(cov.set_index("Actor_MSISDN")["POS_Attribues"]).fillna(0).astype(int)
            if "Taux_Persistance" in cov.columns:
                table["Taux_Persistance"] = table["Actor_MSISDN"].map(cov.set_index("Actor_MSISDN")["Taux_Persistance"])

        if segment == "pr_caisse" and "Type_Point" in table.columns:
            caisse_rows = table["Type_Point"].eq("Caisses")
            table.loc[caisse_rows, ["POS_Touches", "POS_Attribues", "Taux_Couverture"]] = 0
            table.loc[caisse_rows, "Taux_Persistance"] = None
        if segment == "cds":
            table = _build_cds_table(actor_pool, tx, filters)
            return PerformanceContext(segment=segment, filters=filters, table=table, message="Aucune transaction sur la periode.")
        return PerformanceContext(segment=segment, filters=filters, table=table, message="Aucune transaction sur la periode.")

    if segment == "cds":
        table = _build_cds_table(actor_pool, tx, filters)
        return PerformanceContext(segment=segment, filters=filters, table=table)

    hvc_set = get_hvc_msisdns()
    others_set = get_mvc_lvc_msisdns() if _SEGMENT_HVC_OTHERS_SPLIT[segment] else set()

    if segment == "pr_caisse":
        performance_conn = get_connection()
        try:
            hvc_others = compute_pr_portfolio_metrics(
                tx, hvc_set, others_set, filters.start_date, filters.end_date, performance_conn
            )
        finally:
            performance_conn.close()
    else:
        hvc_others = compute_hvc_others_polars(tx, hvc_set, others_set, split=_SEGMENT_HVC_OTHERS_SPLIT[segment])
        if segment == "cds" and not tx.empty:
            cds_map = get_hvc_cds_assignments()
            tx_mapped = tx.merge(cds_map, left_on="To_clean", right_on="hvc_msisdn", how="left")
            tx_mapped["In_Portfolio"] = (tx_mapped["cds_nom"] == tx_mapped["Actor_Nom"])
            tx_out = tx_mapped[~tx_mapped["In_Portfolio"] & tx_mapped["To_clean"].isin(hvc_set)]
            
            if not tx_out.empty:
                hp_metrics = tx_out.groupby("Actor_MSISDN").agg(
                    Hors_Portefeuille_POS=("To_clean", "nunique"),
                    FD_Hors_Portefeuille=("Amount", "sum")
                ).reset_index()
                hvc_others = hvc_others.merge(hp_metrics, on="Actor_MSISDN", how="left")
                hvc_others["Hors_Portefeuille_POS"] = hvc_others.get("Hors_Portefeuille_POS", 0).fillna(0)
                hvc_others["FD_Hors_Portefeuille"] = hvc_others.get("FD_Hors_Portefeuille", 0).fillna(0)
                
                hvc_others["HVC_Serve"] = (hvc_others["HVC_Serve"] - hvc_others["Hors_Portefeuille_POS"]).clip(lower=0)
                hvc_others["FD_HVC"] = (hvc_others["FD_HVC"] - hvc_others["FD_Hors_Portefeuille"]).clip(lower=0)

    dotation = _compute_dotation(filters, actor_pool)
    float_up = _compute_float_up(filters, actor_pool)

    table = actor_pool.merge(hvc_others, on="Actor_MSISDN", how="left")
    table = table.merge(dotation, on="Actor_MSISDN", how="left")
    table = table.merge(float_up, on="Actor_MSISDN", how="left")

    if segment == "pr_caisse":
        assigned_only = compute_coverage_persistence(
            pd.DataFrame(), "pr_caisse", filters.start_date, filters.end_date
        )
        if not assigned_only.empty:
            assigned_map = assigned_only.set_index("Actor_MSISDN")["POS_Attribues"]
            table["POS_Attribues"] = pd.to_numeric(
                table["POS_Attribues"], errors="coerce"
            ).fillna(table["Actor_MSISDN"].map(assigned_map)).fillna(0).astype(int)
    elif segment == "commercial":
        coverage = _compute_commercial_coverage(
            tx, filters.start_date, filters.end_date, manual_mapping=manual_mapping,
            hvc_set=hvc_set, others_set=others_set
        )
        overlap = [c for c in coverage.columns if c in table.columns and c != "Actor_MSISDN"]
        if overlap:
            table = table.drop(columns=overlap)
        table = table.merge(coverage, on="Actor_MSISDN", how="left")
    else:
        # Calcul de Couverture et Persistance
        coverage = compute_coverage_persistence(tx, segment, filters.start_date, filters.end_date)
        table = table.merge(coverage, on="Actor_MSISDN", how="left")

    numeric_cols = [
        "Nb_Jours", "FD_HVC", "HVC_Serve", "Nb_Trans_HVC", "FD_Others",
        "Other_Serve", "Nb_Trans_Other", "Hors_Portefeuille_POS",
        "FD_Hors_Portefeuille", "POS_Touches", "POS_Attribues",
        "POS_Touches_HVC", "POS_Attribues_HVC",
        "POS_Touches_Others", "POS_Attribues_Others",
        "HVC_Wallet", "HVC_No_Wallet", "Sigma_HVC_Serve",
        "Others_Wallet", "Others_No_Wallet", "Sigma_Others_Serve",
        "Nb_Trans_HVC_Wallet", "Nb_Trans_HVC_No_Wallet",
        "Nb_Trans_Others_Wallet", "Nb_Trans_Others_No_Wallet",
        "Tot_No_Wallet", "FD_HVC_Wallet", "FD_Others_Wallet", "FD_Tot_Wallet",
        "FD_HVC_No_Wallet", "FD_Others_No_Wallet", "FD_Tot_No_Wallet",
        "Dotation_Montant", "FU",
    ]
    for col in numeric_cols:
        if col not in table.columns:
            table[col] = 0
        table[col] = pd.to_numeric(table[col], errors="coerce").fillna(0)

    for rate_col in [
        "Taux_Couverture", "Taux_Couverture_HVC", "Taux_Couverture_Others",
        "TR_HVC_Wallet", "TR_HVC_No_Wallet", "TR_HVC_General",
        "TR_Others_Wallet", "TR_Others_No_Wallet", "TR_Others_General",
        "TR_HVC", "TR_Other", "TR_General",
    ]:
        if rate_col not in table.columns:
            table[rate_col] = 0.0
        else:
            table[rate_col] = pd.to_numeric(table[rate_col], errors="coerce").fillna(0.0)

    if segment == "pr_caisse":
        table["HVC_Wallet"] = pd.to_numeric(table.get("HVC_Serve", 0), errors="coerce").fillna(0).astype(int)
        table["FD_HVC_Wallet"] = pd.to_numeric(table.get("FD_HVC", 0), errors="coerce").fillna(0).astype(int)
        table["Others_Wallet"] = pd.to_numeric(table.get("Other_Serve", 0), errors="coerce").fillna(0).astype(int)
        table["FD_Others_Wallet"] = pd.to_numeric(table.get("FD_Others", 0), errors="coerce").fillna(0).astype(int)

        table["HVC_No_Wallet"] = pd.to_numeric(table.get("HVC_No_Wallet", 0), errors="coerce").fillna(0).astype(int)
        table["FD_HVC_No_Wallet"] = pd.to_numeric(table.get("FD_HVC_No_Wallet", 0), errors="coerce").fillna(0).astype(int)
        table["Others_No_Wallet"] = pd.to_numeric(table.get("Others_No_Wallet", 0), errors="coerce").fillna(0).astype(int)
        table["FD_Others_No_Wallet"] = pd.to_numeric(table.get("FD_Others_No_Wallet", 0), errors="coerce").fillna(0).astype(int)

        table["HVC_Total_Served"] = table["HVC_Wallet"] + table["HVC_No_Wallet"]
        table["FD_HVC_Total"] = table["FD_HVC_Wallet"] + table["FD_HVC_No_Wallet"]
        table["Others_Total_Served"] = table["Others_Wallet"] + table["Others_No_Wallet"]
        table["FD_Others_Total"] = table["FD_Others_Wallet"] + table["FD_Others_No_Wallet"]

        table["Sigma_FD"] = table["FD_HVC_Total"] + table["FD_Others_Total"]
        table["Sigma_POS_Serve"] = table["HVC_Total_Served"] + table["Others_Total_Served"]
    elif segment == "commercial":
        table["HVC_Total_Served"] = pd.to_numeric(table.get("Sigma_HVC_Serve", 0), errors="coerce").fillna(0).astype(int)
        table["FD_HVC_Total"] = pd.to_numeric(table.get("FD_HVC", 0), errors="coerce").fillna(0).astype(int)
        table["Others_Total_Served"] = pd.to_numeric(table.get("Sigma_Others_Serve", 0), errors="coerce").fillna(0).astype(int)
        table["FD_Others_Total"] = pd.to_numeric(table.get("FD_Others", 0), errors="coerce").fillna(0).astype(int)
        table["Sigma_FD"] = (table["FD_HVC"] + table["FD_Others"]).round(0).astype(int)
        table["Sigma_POS_Serve"] = (table["Sigma_HVC_Serve"] + table["Sigma_Others_Serve"]).astype(int)
        table["HVC_Serve"] = table["Sigma_HVC_Serve"]
        table["Other_Serve"] = table["Sigma_Others_Serve"]
    else: # cds
        table["Sigma_FD"] = (table["FD_HVC"] + table["FD_Hors_Portefeuille"]).round(0).astype(int)
        table["Sigma_POS_Serve"] = (table["HVC_Serve"] + table["Hors_Portefeuille_POS"]).astype(int)

    top_commerciaux = pd.DataFrame()

    if segment == "commercial":
        # Activite quotidienne + rotation : necessaires au Top 10 commercial
        # (precocite de 1ere transaction, TR_General).
        # Vérifier si la plage de recherche couvre un seul jour
        is_single_day = (filters.start_date == filters.end_date)

        # Calcul des activités quotidiennes adapté
        activity = compute_daily_activity_polars(tx, is_single_day=is_single_day)
        table = table.merge(activity, on="Actor_MSISDN", how="left")
        table["Nb_Jours_Actifs"] = pd.to_numeric(table.get("Nb_Jours_Actifs"), errors="coerce").fillna(0)
        table["Nb_Jours"] = table["Nb_Jours_Actifs"]

        # Approche calendaire 6j/7 (dimanches exclus — standard 26 jours)
        nb_ouvres = count_working_days_6j(filters.start_date, filters.end_date)
        table["Nb_Jours_Ouvres"] = nb_ouvres

        tr_mode = getattr(filters, "tr_mode", "actifs")
        table = _add_rotation_rates(table, tr_mode=tr_mode, nb_jours_ouvres=nb_ouvres)

        # Work Progress commercial : Serve + TR, "Other" au sens large (pas
        # HVC), cf. hypothese 6.
        work_progress = _compute_work_progress(
            tx, hvc_set, others_set=None, include_tr=True,
            tr_mode=tr_mode, nb_jours_ouvres=nb_ouvres
        )
        table = table.merge(work_progress, on="Actor_MSISDN", how="left")

    elif segment == "pr_caisse":
        # Work Progress et Tendance intraday : uniquement sur les Caisses
        # (jamais les Points Relais), sans TR pour le Work Progress.
        caisses_tx = tx[tx.get("Type_Point") == "Caisses"] if "Type_Point" in tx.columns else tx.iloc[0:0]

        work_progress = _compute_work_progress(caisses_tx, hvc_set, others_set=others_set, include_tr=False)
        table = table.merge(work_progress, on="Actor_MSISDN", how="left")

        trend = _compute_afternoon_trend(caisses_tx)
        table = table.merge(trend, on="Actor_MSISDN", how="left")
        for col in ["POS_serve", "New"]:
            if col not in table.columns:
                table[col] = 0
            table[col] = pd.to_numeric(table[col], errors="coerce").fillna(0).astype(int)

    if segment in _TOP10_SEGMENTS:
        top_commerciaux = _rank_top_actors(table, segment)

    return PerformanceContext(segment=segment, filters=filters, table=table, top_commerciaux=top_commerciaux)


def _build_cds_table(actor_pool: pd.DataFrame, tx: pd.DataFrame, filters: PerformanceFilters) -> pd.DataFrame:
    """Calcul optimisé et fidèle aux règles métiers pour le segment CDS :
    - HVC_Wallet (Served, FD) : Portefeuille HVC attribué au CDS dans hvc_cds_assignments
    - HVC_No_Wallet (Served, FD) : Clients HVC hors portefeuille du CDS
    - Others (Served, FD) : Clients hors HVC
    - Synthèse POS (Somme_POS_Serve) : HVC_Wallet_Served + HVC_No_Wallet_Served + Others_Served (exclut les commerciaux)
    - Commerciaux (Served, FD) : Réapprovisionnement des commerciaux par le CDS
    - Finances (Somme_FD) : HVC_Wallet_FD + HVC_No_Wallet_FD + Others_FD + Ccial_FD
    - Objectifs & Suivi : Nb_HVC_Attribue, Taux_Couverture_(%) = (HVC_Wallet_Served / Nb_HVC_Attribue) * 100
    - Dotation : Nom, Montant reçus du master
    """
    assignments_df = get_hvc_cds_assignments()
    if not assignments_df.empty:
        assignments_df["clean_hvc"] = assignments_df["hvc_msisdn"].map(clean_phone)
        assigned_dict = assignments_df.groupby("cds_nom")["clean_hvc"].apply(set).to_dict()
    else:
        assigned_dict = {}

    hvc_set = get_hvc_msisdns()

    comm_flows = get_cds_commercial_flows(
        start_date=filters.start_date,
        end_date=filters.end_date,
        min_amount=MIN_TRANSFER_AMOUNT,
    )
    dotation = _compute_dotation(filters, actor_pool)

    rows = []
    for _, actor in actor_pool.iterrows():
        nom = actor["Actor_Nom"]
        msisdn = str(actor["Actor_MSISDN"])
        assigned_pos = assigned_dict.get(nom, set())
        nb_hvc_attribue = len(assigned_pos)

        tx_cds = tx[tx["Actor_MSISDN"] == msisdn] if not tx.empty else pd.DataFrame()
        if tx_cds.empty:
            nb_jours = 0
            hvc_w_served = hvc_w_fd = 0
            hvc_nw_served = hvc_nw_fd = 0
            oth_served = oth_fd = 0
            somme_pos_serve = 0
        else:
            nb_jours = int(tx_cds["Date_only"].nunique())
            to_clean = tx_cds["To_clean"].astype(str)
            amounts = pd.to_numeric(tx_cds["Amount"], errors="coerce").fillna(0)

            is_w = to_clean.isin(assigned_pos)
            is_nw = ~is_w & to_clean.isin(hvc_set)
            is_oth = ~is_w & ~to_clean.isin(hvc_set)

            hvc_w_served = int(to_clean[is_w].nunique())
            hvc_w_fd = int(round(amounts[is_w].sum()))
            hvc_nw_served = int(to_clean[is_nw].nunique())
            hvc_nw_fd = int(round(amounts[is_nw].sum()))
            oth_served = int(to_clean[is_oth].nunique())
            oth_fd = int(round(amounts[is_oth].sum()))
            somme_pos_serve = hvc_w_served + hvc_nw_served + oth_served

        c_row = comm_flows[comm_flows["Actor_MSISDN"] == msisdn] if not comm_flows.empty else pd.DataFrame()
        ccial_served = int(c_row["Ccial_Serve"].iloc[0]) if not c_row.empty else 0
        ccial_fd = int(round(c_row["FD_Commercial"].iloc[0])) if not c_row.empty else 0

        somme_fd = hvc_w_fd + hvc_nw_fd + oth_fd + ccial_fd
        taux_couv = round(hvc_w_served / nb_hvc_attribue * 100, 1) if nb_hvc_attribue > 0 else 0.0

        d_row = dotation[dotation["Actor_MSISDN"] == msisdn] if not dotation.empty else pd.DataFrame()
        dot_nom = str(d_row["Dotation_Nom"].iloc[0]) if not d_row.empty and pd.notna(d_row["Dotation_Nom"].iloc[0]) else ""
        dot_montant = int(round(pd.to_numeric(d_row["Dotation_Montant"].iloc[0], errors="coerce"))) if not d_row.empty else 0

        rows.append({
            "Actor_Nom": nom,
            "Actor_MSISDN": msisdn,
            "Nb_Jours": nb_jours,
            "HVC_Wallet_Served": hvc_w_served,
            "HVC_Wallet_FD": hvc_w_fd,
            "HVC_No_Wallet_Served": hvc_nw_served,
            "HVC_No_Wallet_FD": hvc_nw_fd,
            "Others_Served": oth_served,
            "Others_FD": oth_fd,
            "Somme_POS_Serve": somme_pos_serve,
            "Ccial_Serve": ccial_served,
            "FD_Commercial": ccial_fd,
            "Somme_FD": somme_fd,
            "Nb_HVC_Attribue": nb_hvc_attribue,
            "Taux_Couverture_Quota": taux_couv,
            "Dotation_Nom": dot_nom,
            "Dotation_Montant": dot_montant,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Chargement / cadrage
# ---------------------------------------------------------------------------

def _load_actor_pool(filters: PerformanceFilters) -> pd.DataFrame:
    segment = filters.segment
    if segment == "commercial":
        df = get_commercial_referentiel(
            zone_centre=filters.zone_centre,
            zone_territoire=filters.zone_territoire,
            zone_sa=filters.zone_sa,
        )
        df = df.rename(columns={
            "ccial_msisdn": "Actor_MSISDN",
            "nom_ccial": "Actor_Nom",
            "zone_centre": "Zone_Centre",
            "zone_territoire": "Zone_Territoire",
            "zone_sa": "Zone_SA",
        })
    elif segment == "pr_caisse":
        df = get_point_relay_referentiel(type_point=filters.type_point, territoire=filters.territoire)
        df = df.rename(columns={
            "msisdn_pr": "Actor_MSISDN",
            "nom": "Actor_Nom",
            "territoire": "Territoire",
            "localisation": "Localisation",
            "type_point": "Type_Point",
        })
    elif segment == "cds":
        df = get_cds_referentiel()
        df = df.rename(columns={"cds_msisdn": "Actor_MSISDN", "nom_cds": "Actor_Nom"})
    else:
        raise ValueError(f"Segment inconnu: {segment}")
    
    if not df.empty and "Actor_MSISDN" in df.columns:
        df["Actor_MSISDN"] = df["Actor_MSISDN"].map(clean_phone)
    return df


def _load_and_scope_transactions(filters: PerformanceFilters, excluded: frozenset) -> pd.DataFrame:
    # Le filtre heure est reserve aux segments commercial/pr_caisse (CDS n'a
    # ni filtre heure ni Work Progress/Tendance). On coupe ici, pas seulement
    # dans la vue, pour que ce soit vrai meme si la vue envoie ces
    # parametres par erreur.
    hour_start = filters.hour_start if filters.segment != "cds" else None
    hour_end = filters.hour_end if filters.segment != "cds" else None

    tx = get_performance_transactions(
        segment=filters.segment,
        start_date=filters.start_date,
        end_date=filters.end_date,
        hour_start=hour_start,
        hour_end=hour_end,
        tx_types=["Transfer"],
        min_amount=MIN_TRANSFER_AMOUNT,
    )
    if tx.empty:
        return tx

    # Les transactions historiques peuvent avoir ete ingerees avant la
    # normalisation. On recanonise les deux numeros avant tout filtre/metrique.
    for column in ("From_clean", "To_clean", "Actor_MSISDN"):
        if column in tx.columns:
            tx[column] = tx[column].map(clean_phone)
    tx["Amount"] = pd.to_numeric(tx["Amount"], errors="coerce").fillna(0)
    tx = tx[tx["Amount"] >= MIN_TRANSFER_AMOUNT].copy()
    if tx.empty:
        return tx

    # tx = tx[tx["To_clean"].notna() & ~tx["To_clean"].isin(excluded)].copy()
    tx = tx[~tx["To_clean"].isin(excluded)]

    if filters.segment == "commercial":
        if filters.zone_centre not in ("Toutes", "Tous"):
            tx = tx[tx["Zone_Centre"] == filters.zone_centre]
        if filters.zone_territoire not in ("Toutes", "Tous"):
            tx = tx[tx["Zone_Territoire"] == filters.zone_territoire]
        if filters.zone_sa not in ("Toutes", "Tous"):
            tx = tx[tx["Zone_SA"] == filters.zone_sa]
    elif filters.segment == "pr_caisse":
        if filters.type_point not in ("Tous",):
            tx = tx[tx["Type_Point"] == filters.type_point]
        if filters.territoire not in ("Toutes", "Tous"):
            tx = tx[tx["Actor_Territoire"] == filters.territoire]

    return tx


# ---------------------------------------------------------------------------
# HVC / Others
# ---------------------------------------------------------------------------

# def _compute_hvc_others(tx: pd.DataFrame, hvc_set: set, others_set: set, split: bool) -> pd.DataFrame:
#     """FD (montant), Serve (POS distincts) et Nb_Trans par acteur et par segment."""
#     work = tx.copy()

#     def _assign(msisdn: str) -> str:
#         if msisdn in hvc_set:
#             return "HVC"
#         return "OTHER"

#     work["Segment"] = work["To_clean"].apply(_assign)
#     keep = ["HVC", "OTHER"] if split else ["HVC"]
#     work = work[work["Segment"].isin(keep)]

#     cols = ["Actor_MSISDN", "Nb_Jours", "FD_HVC", "HVC_Serve", "Nb_Trans_HVC"]
#     if split:
#         cols += ["FD_Others", "Other_Serve", "Nb_Trans_Other"]
#     if work.empty:
#         return pd.DataFrame(columns=cols)

#     days = work.groupby("Actor_MSISDN")["Date_only"].nunique().reset_index(name="Nb_Jours")
#     metrics = (
#         work.groupby(["Actor_MSISDN", "Segment"])
#         .agg(FD=("Amount", "sum"), Serve=("To_clean", "nunique"), Nb_Trans=("To_clean", "size"))
#         .reset_index()
#     )
#     pivot = metrics.pivot_table(index="Actor_MSISDN", columns="Segment", values=["FD", "Serve", "Nb_Trans"], fill_value=0)
#     pivot.columns = [f"{a}_{b}" for a, b in pivot.columns]
#     pivot = pivot.reset_index().rename(columns={
#         "FD_HVC": "FD_HVC", "Serve_HVC": "HVC_Serve", "Nb_Trans_HVC": "Nb_Trans_HVC",
#         "FD_OTHER": "FD_Others", "Serve_OTHER": "Other_Serve", "Nb_Trans_OTHER": "Nb_Trans_Other",
#     })
#     for col in ["FD_HVC", "HVC_Serve", "Nb_Trans_HVC", "FD_Others", "Other_Serve", "Nb_Trans_Other"]:
#         if col not in pivot.columns:
#             pivot[col] = 0

#     return pivot.merge(days, on="Actor_MSISDN", how="left")

def _compute_hvc_others(tx: pd.DataFrame, hvc_set: set, others_set: set, split: bool) -> pd.DataFrame:
    """Calcul FD, POS Servi et Nb_Trans par segment sans perdre les OTHER."""
    work = tx.copy()

    def _assign(msisdn: str) -> str:
        if msisdn in hvc_set:
            return "HVC"
        return "OTHER"

    work["Segment"] = work["To_clean"].apply(_assign)

    # Ne JAMAIS filtrer OTHER ici pour conserver le comptage global des POS Servis
    days = work.groupby("Actor_MSISDN")["Date_only"].nunique().reset_index(name="Nb_Jours")
    metrics = (
        work.groupby(["Actor_MSISDN", "Segment"])
        .agg(FD=("Amount", "sum"), Serve=("To_clean", "nunique"), Nb_Trans=("To_clean", "size"))
        .reset_index()
    )
    
    pivot = metrics.pivot_table(index="Actor_MSISDN", columns="Segment", values=["FD", "Serve", "Nb_Trans"], fill_value=0)
    pivot.columns = [f"{a}_{b}" for a, b in pivot.columns]
    pivot = pivot.reset_index().rename(columns={
        "FD_HVC": "FD_HVC", "Serve_HVC": "HVC_Serve", "Nb_Trans_HVC": "Nb_Trans_HVC",
        "FD_OTHER": "FD_Others", "Serve_OTHER": "Other_Serve", "Nb_Trans_OTHER": "Nb_Trans_Other",
    })

    for col in ["FD_HVC", "HVC_Serve", "Nb_Trans_HVC", "FD_Others", "Other_Serve", "Nb_Trans_Other"]:
        if col not in pivot.columns:
            pivot[col] = 0

    return pivot.merge(days, on="Actor_MSISDN", how="left")

# ---------------------------------------------------------------------------
# Dotation (regles differentes par segment, meme mecanique de fond)
# ---------------------------------------------------------------------------

def _dotation_source_maps(sources: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """{source: {msisdn: libelle}} pour les sources de dotation demandees."""
    result: dict[str, dict[str, str]] = {}
    for source in sources:
        if source in ("master", "caisse"):
            df = get_exclusions(category=source)
            result[source] = dict(zip(df["msisdn"], df["label"].fillna(df["msisdn"]))) if not df.empty else {}
        elif source == "commercial":
            df = get_commercial_referentiel()
            result[source] = dict(zip(df["ccial_msisdn"], df["nom_ccial"])) if not df.empty else {}
        elif source == "cds":
            df = get_cds_referentiel()
            result[source] = dict(zip(df["cds_msisdn"], df["nom_cds"])) if not df.empty else {}
    return result


def _compute_dotation(filters: PerformanceFilters, actor_pool: pd.DataFrame) -> pd.DataFrame:
    rule = _DOTATION_RULES[filters.segment]
    dotation_tx = get_dotation_transactions(
        segment=filters.segment,
        start_date=filters.start_date,
        end_date=filters.end_date,
        hour_max=rule["hour_max"],
    )
    empty = pd.DataFrame(columns=["Actor_MSISDN", "Dotation_Nom", "Dotation_Montant"])
    if dotation_tx.empty:
        return empty

    source_maps = _dotation_source_maps(rule["sources"])
    combined_label: dict[str, str] = {}
    for mapping in source_maps.values():
        for msisdn, label in mapping.items():
            combined_label.setdefault(msisdn, label)

    dotation_tx = dotation_tx.copy()
    dotation_tx["Source_Label"] = dotation_tx["From_clean"].map(combined_label)
    dotation_tx = dotation_tx[dotation_tx["Source_Label"].notna()]
    if dotation_tx.empty:
        return empty

    if rule["single_per_source"]:
        # Commercial : au plus 1 transaction par source (Master / Caisse), par jour et par acteur.
        dotation_tx["Source_Type"] = np.where(
            dotation_tx["From_clean"].isin(source_maps.get("master", {})), "master", "caisse"
        )
        dotation_tx = (
            dotation_tx.sort_values("Date")
            .groupby(["Date_only", "Actor_MSISDN", "Source_Type"], as_index=False)
            .head(1)
        )

        agg = dotation_tx.groupby("Actor_MSISDN").agg(Dotation_Montant=("Amount", "sum")).reset_index()
        labels = (
            dotation_tx.groupby("Actor_MSISDN")["Source_Label"]
            .apply(lambda values: " + ".join(sorted(set(values.dropna()))))
            .reset_index(name="Dotation_Nom")
        )
        agg = agg.merge(labels, on="Actor_MSISDN", how="left")

        dotation_tx["Minutes"] = dotation_tx["Hour"] * 60 + dotation_tx["Date"].dt.minute
        daily_heure = dotation_tx.groupby(["Date_only", "Actor_MSISDN"])["Minutes"].min().reset_index()
        heure_moy = daily_heure.groupby("Actor_MSISDN")["Minutes"].mean().reset_index(name="Heure_Dotation_min")
        agg = agg.merge(heure_moy, on="Actor_MSISDN", how="left")
    else:
        # PR/Caisse et CDS : dotateur principal = celui avec le plus de
        # transactions vers l'acteur, parmi les sources autorisees pour ce
        # segment (cf. _DOTATION_RULES — CDS = Master uniquement).
        freq = dotation_tx.groupby(["Actor_MSISDN", "Source_Label"]).size().reset_index(name="Nb")
        idx = freq.groupby("Actor_MSISDN")["Nb"].idxmax()
        principal = freq.loc[idx, ["Actor_MSISDN", "Source_Label"]].rename(columns={"Source_Label": "Dotation_Nom"})
        amounts = dotation_tx.groupby("Actor_MSISDN")["Amount"].sum().reset_index(name="Dotation_Montant")
        agg = principal.merge(amounts, on="Actor_MSISDN", how="left")

    return agg


def _compute_float_up(filters: PerformanceFilters, actor_pool: pd.DataFrame) -> pd.DataFrame:
    """Calcule le montant de Float Up (FU / retours de float) par acteur."""
    fu_tx = get_float_up_transactions(
        segment=filters.segment,
        start_date=filters.start_date,
        end_date=filters.end_date,
        hour_start=filters.hour_start,
        hour_end=filters.hour_end,
    )
    empty = pd.DataFrame(columns=["Actor_MSISDN", "FU"])
    if fu_tx.empty:
        return empty

    agg = fu_tx.groupby("Actor_MSISDN")["Amount"].sum().reset_index(name="FU")
    return agg


def normalize_commercial_mapping(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise un fichier d'attribution POS <-> Commercial (Excel ou CSV).
    Colonnes reconnues :
    - POS : AGENT MSISDN, Agent_MSISDN, agent_msisdn, POS_MSISDN, MSISDN_POS, MSISDN, POS, Numero POS, HVC_ MSISDN, HVC_MSISDN, HVC MSISDN, NUM_HVC, Numero du point, PDV
    - Commercial Nom : Commercial, COMMERCIAL, Nom_Ccial, NOM_CCIAL, Nom Commercial, CCIAL, Nom, Ccial en charge, Commercial en charge, Nom du point de relais, NOM_CDS
    - Commercial Numéro : Numero, Numéro, NUMERO, MSISDN_Commercial, Ccial_MSISDN, CCIAL_MSISDN, MSISDN_CCIAL, Telephone, MSISDN_PR, Parent MSISDN
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["To_clean", "Commercial", "Commercial_MSISDN"])

    work = df.copy()
    work.columns = [str(c).strip() for c in work.columns]
    lookup = {str(c).lower().replace(" ", "").replace("_", ""): c for c in work.columns}

    def pick(*cands):
        for c in cands:
            k = c.lower().replace(" ", "").replace("_", "")
            if k in lookup:
                return lookup[k]
        return None

    col_pos = pick(
        "AGENT MSISDN", "Agent_MSISDN", "agent_msisdn", "POS_MSISDN", "MSISDN_POS",
        "MSISDN", "POS", "Numero POS", "Numero_POS", "PDV",
        "HVC_MSISDN", "HVC MSISDN", "HVC_ MSISDN", "NUM_HVC", "POS MSISDN", "Agent MSISDN", "Numero du point", "Numero_du_point"
    )
    col_name = pick(
        "Commercial", "COMMERCIAL", "Nom_Ccial", "NOM_CCIAL", "Nom Commercial", "CCIAL", "Nom",
        "Ccial en charge", "Commercial en charge", "Commercial en charge ", "Nom du point de relais", "NOM_CDS"
    )
    col_num = pick(
        "Numero", "Numéro", "NUMERO", "MSISDN_Commercial", "Ccial_MSISDN", "CCIAL_MSISDN",
        "MSISDN_CCIAL", "Telephone", "MSISDN_PR", "Parent MSISDN", "Parent_MSISDN"
    )

    if not col_pos or not col_name:
        return pd.DataFrame()

    out = pd.DataFrame({
        "To_clean": work[col_pos].astype(str).apply(clean_phone),
        "Commercial": work[col_name].astype(str).str.strip(),
    })

    if col_num:
        out["Commercial_MSISDN"] = (
            work[col_num]
            .apply(lambda x: "" if pd.isna(x) else str(x).split(".")[0].strip())
            .apply(clean_phone)
        )
    else:
        out["Commercial_MSISDN"] = None

    out = out[
        out["To_clean"].notna()
        & (out["To_clean"].astype(str).str.len() > 0)
        & out["Commercial"].notna()
        & ~out["Commercial"].str.lower().isin({"", "nan", "none", "n/a", "null"})
    ].copy()

    # Déduplication par couple (Commercial, POS) pour préserver le portefeuille de chaque commercial
    # (une déduplication sur To_clean seul évincerait à tort les POS attribués à des commerciaux apparaissant plus bas dans le fichier)
    out = out.drop_duplicates(subset=["Commercial", "To_clean"], keep="first").reset_index(drop=True)

    # Enrichissement automatique du MSISDN commercial via le référentiel s'il n'est pas fourni dans le fichier
    try:
        from models.reference_model import get_all_commerciaux
        ref_comm = get_all_commerciaux()
        if not ref_comm.empty and "Nom_Ccial" in ref_comm.columns and "Ccial_MSISDN" in ref_comm.columns:
            import unicodedata, re
            def _get_name_tokens(s: Any) -> set[str]:
                if not s or pd.isna(s):
                    return set()
                norm = unicodedata.normalize("NFKD", str(s)).encode("ASCII", "ignore").decode("utf-8")
                return {t for t in re.findall(r"[A-Za-z0-9]+", norm.upper()) if len(t) > 1 and t not in ("MD", "ETS", "SARL", "CCIAL", "MR", "MME")}

            ref_map = []
            for _, r in ref_comm.dropna(subset=["Nom_Ccial", "Ccial_MSISDN"]).iterrows():
                toks = _get_name_tokens(r["Nom_Ccial"])
                if toks:
                    ref_map.append((toks, clean_phone(r["Ccial_MSISDN"])))

            def _resolve_comm_msisdn(row):
                curr = row.get("Commercial_MSISDN")
                if curr and pd.notna(curr) and str(curr).strip() not in ("", "None", "nan"):
                    return curr
                c_toks = _get_name_tokens(row["Commercial"])
                if not c_toks:
                    return curr
                for r_toks, r_msisdn in ref_map:
                    if c_toks == r_toks or (len(c_toks) >= 2 and len(r_toks) >= 2 and (c_toks.issubset(r_toks) or r_toks.issubset(c_toks))):
                        return r_msisdn
                return curr

            out["Commercial_MSISDN"] = out.apply(_resolve_comm_msisdn, axis=1)
    except Exception:
        pass

    return out


def _compute_commercial_coverage(
    tx: pd.DataFrame,
    start_date: Optional[str],
    end_date: Optional[str],
    manual_mapping: Optional[pd.DataFrame] = None,
    hvc_set: Optional[set[str]] = None,
    others_set: Optional[set[str]] = None,
) -> pd.DataFrame:
    """Calcule POS_Touches, POS_Attribues, Taux_Couverture, et les métriques de couverture
    HVC et Autres(MVC/LVC), ainsi que Taux_Persistance pour les commerciaux.
    - Si manual_mapping est fourni : utilise les POS attribués définis dans le fichier de mapping.
    - Sinon (mode auto) : attribution automatique basée sur transactions >= 3 TX (comme conquete_view).
    """
    if hvc_set is None:
        hvc_set = get_hvc_msisdns()
    if others_set is None:
        others_set = get_mvc_lvc_msisdns()

    clean_hvc_set = {clean_phone(x) for x in hvc_set if x}
    clean_others_set = {clean_phone(x) for x in others_set if x}

    base_cov = compute_coverage_persistence(tx, "commercial", start_date, end_date)
    persistence_map = (
        dict(zip(base_cov["Actor_MSISDN"], base_cov["Taux_Persistance"]))
        if not base_cov.empty and "Taux_Persistance" in base_cov.columns
        else {}
    )

    if tx.empty:
        for c in [
            "POS_Touches", "POS_Attribues", "Taux_Couverture",
            "POS_Touches_HVC", "POS_Attribues_HVC", "Taux_Couverture_HVC",
            "POS_Touches_Others", "POS_Attribues_Others", "Taux_Couverture_Others",
            "HVC_Wallet", "HVC_No_Wallet", "Sigma_HVC_Serve",
            "Others_Wallet", "Others_No_Wallet", "Sigma_Others_Serve", "Sigma_POS_Serve",
            "Nb_Trans_HVC_Wallet", "Nb_Trans_HVC_No_Wallet", "Nb_Trans_HVC",
            "Nb_Trans_Others_Wallet", "Nb_Trans_Others_No_Wallet", "Nb_Trans_Other",
            "Tot_No_Wallet", "FD_HVC_Wallet", "FD_Others_Wallet", "FD_Tot_Wallet",
            "FD_HVC_No_Wallet", "FD_Others_No_Wallet", "FD_Tot_No_Wallet",
        ]:
            base_cov[c] = 0
        return base_cov

    rows = []
    has_manual = manual_mapping is not None and isinstance(manual_mapping, pd.DataFrame) and not manual_mapping.empty

    if has_manual:
        mapping_df = manual_mapping.copy()
        mapping_df["_clean_pos"] = mapping_df["To_clean"].fillna("").astype(str).str.strip().map(clean_phone)
        mapping_df["_clean_msisdn"] = (
            mapping_df["Commercial_MSISDN"].fillna("").astype(str).str.strip().map(clean_phone)
            if "Commercial_MSISDN" in mapping_df.columns
            else pd.Series("", index=mapping_df.index)
        )
        mapping_df["_clean_nom"] = (
            mapping_df["Commercial"].fillna("").astype(str).str.strip().str.upper()
            if "Commercial" in mapping_df.columns
            else pd.Series("", index=mapping_df.index)
        )
        import unicodedata, re
        def _get_name_tokens(s: Any) -> set[str]:
            if not s or pd.isna(s):
                return set()
            norm = unicodedata.normalize("NFKD", str(s)).encode("ASCII", "ignore").decode("utf-8")
            return {t for t in re.findall(r"[A-Za-z0-9]+", norm.upper()) if len(t) > 1 and t not in ("MD", "ETS", "SARL", "CCIAL", "MR", "MME")}

        def _tokens_match(t1: set[str], t2: set[str]) -> bool:
            if not t1 or not t2:
                return False
            if t1 == t2:
                return True
            common = t1.intersection(t2)
            if len(common) >= 2:
                return True
            if len(common) == 1 and (len(t1) == 1 or len(t2) == 1):
                tok = next(iter(common))
                if len(tok) >= 4:
                    return True
            return False

        mapping_df["_tokens"] = mapping_df["Commercial"].apply(_get_name_tokens)
    else:
        # Mode Automatique : attribution primaire des POS avec >= 3 transactions
        pairs = tx.groupby(["Actor_MSISDN", "To_clean"]).size().reset_index(name="tx_count")
        if not pairs.empty:
            assigned_auto = (
                pairs[pairs["tx_count"] >= 3]
                .sort_values(["To_clean", "tx_count"], ascending=[True, False])
                .drop_duplicates(subset=["To_clean"], keep="first")
            )
        else:
            assigned_auto = pd.DataFrame(columns=["Actor_MSISDN", "To_clean"])

    for msisdn in tx["Actor_MSISDN"].dropna().unique():
        tx_comm = tx[tx["Actor_MSISDN"] == msisdn]

        # 1. Détermination du portefeuille assigné
        if has_manual:
            comm_nom = (
                str(tx_comm["Actor_Nom"].iloc[0]).strip().upper()
                if "Actor_Nom" in tx_comm.columns and not tx_comm.empty and pd.notna(tx_comm["Actor_Nom"].iloc[0])
                else ""
            )
            comm_tokens = _get_name_tokens(comm_nom) if comm_nom else set()
            msisdn_9 = str(msisdn)[-9:]
            def _match_msisdn(val: Any) -> bool:
                s = str(val).strip() if pd.notna(val) else ""
                if not s or s.lower() in ("nan", "none", ""):
                    return False
                return s[-9:] == msisdn_9 if len(s) >= 9 else s == str(msisdn)

            mask = mapping_df["_clean_msisdn"].apply(_match_msisdn)
            if comm_nom:
                mask = mask | (mapping_df["_clean_nom"] == comm_nom)
            if comm_tokens:
                token_mask = mapping_df["_tokens"].apply(lambda t: _tokens_match(comm_tokens, t))
                mask = mask | token_mask

            assigned_pos = set(mapping_df.loc[mask, "_clean_pos"].dropna())
        else:
            assigned_pos = set(assigned_auto[assigned_auto["Actor_MSISDN"] == msisdn]["To_clean"].astype(str))

        assigned_pos = {p for p in assigned_pos if p}
        n_attribues = len(assigned_pos)
        assigned_hvc = assigned_pos.intersection(clean_hvc_set)
        assigned_others = assigned_pos - assigned_hvc
        n_attribues_hvc = len(assigned_hvc)
        n_attribues_others = len(assigned_others)

        # 2. Décomposition des POS servis (Wallet vs No_Wallet)
        touched_all = set(tx_comm["To_clean"].astype(str).dropna())

        # Portefeuille (Wallet)
        touched_assigned_hvc = touched_all.intersection(assigned_hvc)
        touched_assigned_others = touched_all.intersection(assigned_others)
        touched_assigned = touched_assigned_hvc | touched_assigned_others
        hvc_wallet = len(touched_assigned_hvc)
        others_wallet = len(touched_assigned_others)
        n_touches = len(touched_assigned)

        # Hors-Portefeuille (No_Wallet)
        outside_pos = touched_all - assigned_pos
        touched_outside_hvc = outside_pos.intersection(clean_hvc_set)
        touched_outside_others = outside_pos - touched_outside_hvc
        hvc_no_wallet = len(touched_outside_hvc)
        others_no_wallet = len(touched_outside_others)

        # Sommes POS servis
        sigma_hvc_serve = hvc_wallet + hvc_no_wallet
        sigma_others_serve = others_wallet + others_no_wallet
        sigma_pos_serve = sigma_hvc_serve + sigma_others_serve

        # 3. Décompte des transactions (pour les TR Wallet / No_Wallet)
        to_clean_series = tx_comm["To_clean"].astype(str)
        is_assigned = to_clean_series.isin(assigned_pos)
        is_hvc = to_clean_series.isin(clean_hvc_set)

        nb_trans_hvc_wallet = int((is_assigned & is_hvc).sum())
        nb_trans_hvc_no_wallet = int((~is_assigned & is_hvc).sum())
        nb_trans_hvc = nb_trans_hvc_wallet + nb_trans_hvc_no_wallet

        nb_trans_others_wallet = int((is_assigned & ~is_hvc).sum())
        nb_trans_others_no_wallet = int((~is_assigned & ~is_hvc).sum())
        nb_trans_other = nb_trans_others_wallet + nb_trans_others_no_wallet

        # Montants FD (Ventes) Wallet vs No Wallet
        amounts = pd.to_numeric(tx_comm["Amount"], errors="coerce").fillna(0)
        fd_hvc_wallet = int(round(amounts[is_assigned & is_hvc].sum()))
        fd_hvc_no_wallet = int(round(amounts[~is_assigned & is_hvc].sum()))
        fd_others_wallet = int(round(amounts[is_assigned & ~is_hvc].sum()))
        fd_others_no_wallet = int(round(amounts[~is_assigned & ~is_hvc].sum()))
        fd_tot_wallet = fd_hvc_wallet + fd_others_wallet
        fd_tot_no_wallet = fd_hvc_no_wallet + fd_others_no_wallet
        tot_no_wallet = hvc_no_wallet + others_no_wallet

        # 4. Taux de couverture
        taux = round(n_touches / n_attribues * 100, 1) if n_attribues > 0 else 0.0
        taux_hvc = round(hvc_wallet / n_attribues_hvc * 100, 1) if n_attribues_hvc > 0 else 0.0
        taux_others = round(others_wallet / n_attribues_others * 100, 1) if n_attribues_others > 0 else 0.0

        rows.append({
            "Actor_MSISDN": msisdn,
            "POS_Touches": n_touches,
            "POS_Attribues": n_attribues,
            "Taux_Couverture": taux,
            "POS_Touches_HVC": hvc_wallet,
            "POS_Attribues_HVC": n_attribues_hvc,
            "Taux_Couverture_HVC": taux_hvc,
            "POS_Touches_Others": others_wallet,
            "POS_Attribues_Others": n_attribues_others,
            "Taux_Couverture_Others": taux_others,
            "HVC_Wallet": hvc_wallet,
            "HVC_No_Wallet": hvc_no_wallet,
            "Tot_No_Wallet": tot_no_wallet,
            "Sigma_HVC_Serve": sigma_hvc_serve,
            "Others_Wallet": others_wallet,
            "Others_No_Wallet": others_no_wallet,
            "Sigma_Others_Serve": sigma_others_serve,
            "Sigma_POS_Serve": sigma_pos_serve,
            "Nb_Trans_HVC_Wallet": nb_trans_hvc_wallet,
            "Nb_Trans_HVC_No_Wallet": nb_trans_hvc_no_wallet,
            "Nb_Trans_HVC": nb_trans_hvc,
            "Nb_Trans_Others_Wallet": nb_trans_others_wallet,
            "Nb_Trans_Others_No_Wallet": nb_trans_others_no_wallet,
            "Nb_Trans_Other": nb_trans_other,
            "FD_HVC_Wallet": fd_hvc_wallet,
            "FD_Others_Wallet": fd_others_wallet,
            "FD_Tot_Wallet": fd_tot_wallet,
            "FD_HVC_No_Wallet": fd_hvc_no_wallet,
            "FD_Others_No_Wallet": fd_others_no_wallet,
            "FD_Tot_No_Wallet": fd_tot_no_wallet,
            "Taux_Persistance": persistence_map.get(msisdn),
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Tendance intraday — UNIQUEMENT pr_caisse/Caisses (plus de CDS)
# ---------------------------------------------------------------------------

def _compute_afternoon_trend(tx: pd.DataFrame) -> pd.DataFrame:
    """POS_serve / New : POS distincts servis entre 14h et 17h, et nouveaux vs 6h-14h.

    Appelee uniquement avec un ``tx`` deja restreint a Type_Point == "Caisses"
    (cf. build_performance_context) : plus de parametre de restriction ici,
    le filtrage est fait par l'appelant pour eviter toute ambiguite.
    """
    if tx.empty:
        return pd.DataFrame(columns=["Actor_MSISDN", "POS_serve", "New"])

    afternoon = tx[(tx["Hour"] >= AFTERNOON_TREND_WINDOW[0]) & (tx["Hour"] < AFTERNOON_TREND_WINDOW[1])]
    morning = tx[(tx["Hour"] >= MORNING_TREND_WINDOW[0]) & (tx["Hour"] <= MORNING_TREND_WINDOW[1])]

    if afternoon.empty:
        return pd.DataFrame(columns=["Actor_MSISDN", "POS_serve", "New"])

    pos_group = afternoon.groupby("Actor_MSISDN")["To_clean"].nunique().reset_index(name="POS_serve")

    morning_clients = morning.groupby("Actor_MSISDN")["To_clean"].agg(lambda values: set(values.dropna())).to_dict()
    afternoon_pairs = afternoon[["Actor_MSISDN", "To_clean"]].dropna().drop_duplicates()
    is_new = afternoon_pairs.apply(
        lambda row: row["To_clean"] not in morning_clients.get(row["Actor_MSISDN"], set()), axis=1
    )
    new_group = (
        afternoon_pairs[is_new].groupby("Actor_MSISDN")["To_clean"].nunique().reset_index(name="New")
        if is_new.any() else pd.DataFrame(columns=["Actor_MSISDN", "New"])
    )

    return pos_group.merge(new_group, on="Actor_MSISDN", how="left")


# ---------------------------------------------------------------------------
# Activite quotidienne (commercial uniquement)
# ---------------------------------------------------------------------------

# def _compute_daily_activity(tx: pd.DataFrame) -> pd.DataFrame:
#     """Nb_Transactions total + heure moyenne de 1ere/derniere transaction + Nb_Jours actifs."""
#     if tx.empty:
#         return pd.DataFrame(columns=["Actor_MSISDN", "Nb_Transactions", "Premiere_Trans_min", "Derniere_Trans_min", "Nb_Jours_Actifs"])

#     work = tx.copy()
#     work["Minutes"] = work["Hour"] * 60 + work["Date"].dt.minute
#     per_day = work.groupby(["Actor_MSISDN", "Date_only"]).agg(
#         Nb=("To_clean", "size"),
#         Premiere=("Minutes", "min"),
#         Derniere=("Minutes", "max"),
#     ).reset_index()

#     return per_day.groupby("Actor_MSISDN").agg(
#         Nb_Transactions=("Nb", "sum"),
#         Premiere_Trans_min=("Premiere", "mean"),
#         Derniere_Trans_min=("Derniere", "mean"),
#         Nb_Jours_Actifs=("Date_only", "nunique"),
#     ).reset_index()

def _compute_daily_activity(tx: pd.DataFrame, is_single_day: bool = True) -> pd.DataFrame:
    """Calcul de la 1ère et dernière heure de transaction :
    - Sur un seul jour : MIN pour la 1ère transaction, MAX pour la dernière transaction.
    - Sur plusieurs jours : MOYENNE de la 1ère heure et MOYENNE de la dernière heure quotidienne.
    """
    if tx.empty:
        return pd.DataFrame(columns=[
            "Actor_MSISDN", "Nb_Transactions", 
            "Premiere_Trans_min", "Derniere_Trans_min", "Nb_Jours_Actifs"
        ])

    work = tx.copy()
    work["Minutes"] = work["Hour"] * 60 + work["Date"].dt.minute

    # Étape 1 : Min et Max de CHAQUE journée
    per_day = work.groupby(["Actor_MSISDN", "Date_only"]).agg(
        Nb=("To_clean", "size"),
        Premiere_Jour=("Minutes", "min"),
        Derniere_Jour=("Minutes", "max"),
    ).reset_index()

    # Étape 2 : Agrégation selon le nombre de jours
    if is_single_day:
        # Sur la même journée -> min exact et max exact (ex: 17:16)
        agg_func_prem = "min"
        agg_func_dern = "max"
    else:
        # Sur plusieurs jours -> moyenne des heures d'ouverture/fermeture
        agg_func_prem = "mean"
        agg_func_dern = "mean"

    return per_day.groupby("Actor_MSISDN").agg(
        Nb_Transactions=("Nb", "sum"),
        Premiere_Trans_min=("Premiere_Jour", agg_func_prem),
        Derniere_Trans_min=("Derniere_Jour", agg_func_dern),
        Nb_Jours_Actifs=("Date_only", "nunique"),
    ).reset_index()

# ---------------------------------------------------------------------------
# Work Progress (3 fenetres intraday) — commercial (Serve+TR) et
# pr_caisse/Caisses (Serve seul, sans TR)
# ---------------------------------------------------------------------------

# def _compute_work_progress(
#     tx: pd.DataFrame,
#     hvc_set: set,
#     others_set: Optional[set] = None,
#     include_tr: bool = True,
# ) -> pd.DataFrame:
#     """Serve (HVC/Other) par fenetre intraday, + TR optionnel.

#     - commercial (``others_set=None``, ``include_tr=True``) : "Other" = tout
#       ce qui n'est pas HVC (comportement heritie de
#       ``compute_time_progress_work``, cf. hypothese 6 en tete de fichier).
#     - pr_caisse/Caisses (``others_set=<mvc/lvc>``, ``include_tr=False``) :
#       split strict HVC/Other (UNKNOWN exclu, coherent avec le reste du
#       tableau PR/Caisses), Serve uniquement — pas de colonnes TR du tout.
#     """
#     if tx.empty:
#         return pd.DataFrame(columns=["Actor_MSISDN"])

#     work = tx.copy()
#     work["Minutes"] = work["Hour"] * 60 + work["Date"].dt.minute
#     result = pd.DataFrame({"Actor_MSISDN": work["Actor_MSISDN"].dropna().unique()})

#     for label, (start_minute, end_minute) in WORK_PROGRESS_WINDOWS.items():
#         window = work[(work["Minutes"] >= start_minute) & (work["Minutes"] < end_minute)].copy()

#         if others_set is not None:
#             window["Progress_Segment"] = np.where(
#                 window["To_clean"].isin(hvc_set), 
#                 "HVC", 
#                 "OTHER"
#             )
#             window = window[window["Progress_Segment"] != "UNKNOWN"]
#         else:
#             window["Progress_Segment"] = np.where(window["To_clean"].isin(hvc_set), "HVC", "OTHER")

#         serve_col, other_serve_col = f"HVC Serve {label}", f"Other Serve {label}"
#         tr_col, other_tr_col = f"TR_HVC {label}", f"TR_Other {label}"
#         out_cols = [serve_col, other_serve_col] + ([tr_col, other_tr_col] if include_tr else [])

#         agg_kwargs: dict[str, tuple[str, str]] = {"Serve": ("To_clean", "nunique")}
#         if include_tr:
#             agg_kwargs["Nb_Trans"] = ("To_clean", "size")
#             agg_kwargs["Nb_Jours"] = ("Date_only", "nunique")

#         grouped = window.groupby(["Actor_MSISDN", "Progress_Segment"]).agg(**agg_kwargs).reset_index()

#         if grouped.empty:
#             metrics = pd.DataFrame(columns=["Actor_MSISDN"] + out_cols)
#         else:
#             value_cols = ["Serve"]
#             rename_map = {"Serve_HVC": serve_col, "Serve_OTHER": other_serve_col}
#             if include_tr:
#                 denom = grouped["Serve"].replace(0, np.nan) * grouped["Nb_Jours"].replace(0, np.nan) * 3
#                 grouped["TR"] = ((grouped["Nb_Trans"] / denom) * 100).fillna(0).round(1)
#                 value_cols.append("TR")
#                 rename_map.update({"TR_HVC": tr_col, "TR_OTHER": other_tr_col})

#             metrics = grouped.pivot_table(index="Actor_MSISDN", columns="Progress_Segment", values=value_cols, fill_value=0)
#             metrics.columns = [f"{a}_{b}" for a, b in metrics.columns]
#             metrics = metrics.reset_index().rename(columns=rename_map)

#         result = result.merge(metrics, on="Actor_MSISDN", how="left")
#         for col in out_cols:
#             if col not in result.columns:
#                 result[col] = 0
#             result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0)

#     return result

def _compute_work_progress(
    tx: pd.DataFrame,
    hvc_set: set,
    others_set: Optional[set] = None,
    include_tr: bool = True,
    tr_mode: str = "actifs",
    nb_jours_ouvres: Optional[int] = None,
) -> pd.DataFrame:
    """Serve (HVC/Other) par fenetre intraday, + TR optionnel."""
    if tx.empty:
        return pd.DataFrame(columns=["Actor_MSISDN"])

    work = tx.copy()
    work["Minutes"] = work["Hour"] * 60 + work["Date"].dt.minute
    result = pd.DataFrame({"Actor_MSISDN": work["Actor_MSISDN"].dropna().unique()})

    for label, (start_minute, end_minute) in WORK_PROGRESS_WINDOWS.items():
        window = work[(work["Minutes"] >= start_minute) & (work["Minutes"] < end_minute)].copy()

        serve_col, other_serve_col = f"HVC Serve {label}", f"Other Serve {label}"
        tr_col, other_tr_col = f"TR_HVC {label}", f"TR_Other {label}"
        out_cols = [serve_col, other_serve_col] + ([tr_col, other_tr_col] if include_tr else [])

        # Si aucune transaction dans cette fenêtre horaire
        if window.empty:
            metrics = pd.DataFrame(columns=["Actor_MSISDN"] + out_cols)
            result = result.merge(metrics, on="Actor_MSISDN", how="left")
            continue

        # Attribution des segments
        if others_set is not None:
            window["Progress_Segment"] = np.where(
                window["To_clean"].isin(hvc_set), "HVC",
                np.where(window["To_clean"].isin(others_set), "OTHER", "UNKNOWN"),
            )
            window = window[window["Progress_Segment"] != "UNKNOWN"]
        else:
            window["Progress_Segment"] = np.where(window["To_clean"].isin(hvc_set), "HVC", "OTHER")

        if window.empty:
            metrics = pd.DataFrame(columns=["Actor_MSISDN"] + out_cols)
            result = result.merge(metrics, on="Actor_MSISDN", how="left")
            continue

        agg_kwargs: dict[str, tuple[str, str]] = {"Serve": ("To_clean", "nunique")}
        if include_tr:
            agg_kwargs["Nb_Trans"] = ("To_clean", "size")
            agg_kwargs["Nb_Jours"] = ("Date_only", "nunique")

        grouped = window.groupby(["Actor_MSISDN", "Progress_Segment"]).agg(**agg_kwargs).reset_index()

        if grouped.empty:
            metrics = pd.DataFrame(columns=["Actor_MSISDN"] + out_cols)
        else:
            value_cols = ["Serve"]
            rename_map = {"Serve_HVC": serve_col, "Serve_OTHER": other_serve_col}
            if include_tr:
                if tr_mode == "ouvres_26j" and nb_jours_ouvres:
                    denom = grouped["Serve"].replace(0, np.nan) * float(nb_jours_ouvres) * 3
                else:
                    denom = grouped["Serve"].replace(0, np.nan) * grouped["Nb_Jours"].replace(0, np.nan) * 3
                grouped["TR"] = ((grouped["Nb_Trans"] / denom) * 100).fillna(0).round(1)
                value_cols.append("TR")
                rename_map.update({"TR_HVC": tr_col, "TR_OTHER": other_tr_col})

            metrics = grouped.pivot_table(index="Actor_MSISDN", columns="Progress_Segment", values=value_cols, fill_value=0)
            metrics.columns = [f"{a}_{b}" for a, b in metrics.columns]
            metrics = metrics.reset_index().rename(columns=rename_map)

        result = result.merge(metrics, on="Actor_MSISDN", how="left")

    return result

# ---------------------------------------------------------------------------
# Rotation (commercial uniquement — le Top 10 pr_caisse n'utilise pas TR)
# ---------------------------------------------------------------------------

def _add_rotation_rates(
    table: pd.DataFrame,
    tr_mode: str = "actifs",
    nb_jours_ouvres: Optional[int] = None,
) -> pd.DataFrame:
    """Calcul des taux de rotation (TR) scindés pour les commerciaux :
    - TR_HVC_Wallet / TR_HVC_No_Wallet / TR_HVC_General
    - TR_Others_Wallet / TR_Others_No_Wallet / TR_Others_General
    - TR_General (total)
    Formule : Nb_Trans / (Serve * Nb_Jours * 3) * 100
    - Mode 'actifs' : basé sur les jours réellement travaillés par chaque commercial.
    - Mode 'ouvres_26j' : basé sur les jours ouvrés théoriques (6j/7, dimanches exclus — standard 26j).
    """
    table = table.copy()
    if tr_mode == "ouvres_26j" and nb_jours_ouvres:
        nb_jours = pd.Series(float(nb_jours_ouvres), index=table.index, dtype=float)
    else:
        nb_jours = pd.to_numeric(table.get("Nb_Jours_Actifs", table.get("Nb_Jours", 0)), errors="coerce").replace(0, np.nan)

    # Assurer que Nb_Trans_HVC et Nb_Trans_Other sont toujours calculés et cohérents
    nb_trans_hvc = pd.to_numeric(table.get("Nb_Trans_HVC", 0), errors="coerce").fillna(0)
    if (nb_trans_hvc == 0).all():
        nb_trans_hvc = (
            pd.to_numeric(table.get("Nb_Trans_HVC_Wallet", 0), errors="coerce").fillna(0)
            + pd.to_numeric(table.get("Nb_Trans_HVC_No_Wallet", 0), errors="coerce").fillna(0)
        )
        table["Nb_Trans_HVC"] = nb_trans_hvc

    nb_trans_others = pd.to_numeric(table.get("Nb_Trans_Other", 0), errors="coerce").fillna(0)
    if (nb_trans_others == 0).all():
        nb_trans_others = (
            pd.to_numeric(table.get("Nb_Trans_Others_Wallet", 0), errors="coerce").fillna(0)
            + pd.to_numeric(table.get("Nb_Trans_Others_No_Wallet", 0), errors="coerce").fillna(0)
        )
        table["Nb_Trans_Other"] = nb_trans_others

    def _rate(nb_trans_col: str, serve_col: str) -> pd.Series:
        nb_trans = pd.to_numeric(table.get(nb_trans_col, 0), errors="coerce")
        serve = pd.to_numeric(table.get(serve_col, 0), errors="coerce").replace(0, np.nan)
        return ((nb_trans / (serve * nb_jours * 3)) * 100).fillna(0).round(1)

    # Scission TR HVC
    table["TR_HVC_Wallet"] = _rate("Nb_Trans_HVC_Wallet", "HVC_Wallet")
    table["TR_HVC_No_Wallet"] = _rate("Nb_Trans_HVC_No_Wallet", "HVC_No_Wallet")
    table["TR_HVC_General"] = _rate("Nb_Trans_HVC", "Sigma_HVC_Serve")
    table["TR_HVC"] = table["TR_HVC_General"]

    # Scission TR Others
    table["TR_Others_Wallet"] = _rate("Nb_Trans_Others_Wallet", "Others_Wallet")
    table["TR_Others_No_Wallet"] = _rate("Nb_Trans_Others_No_Wallet", "Others_No_Wallet")
    table["TR_Others_General"] = _rate("Nb_Trans_Other", "Sigma_Others_Serve")
    table["TR_Other"] = table["TR_Others_General"]

    # TR General Total
    nb_transactions = pd.to_numeric(table.get("Nb_Transactions", 0), errors="coerce").fillna(0)
    if (nb_transactions == 0).all():
        nb_transactions = table["Nb_Trans_HVC"] + table["Nb_Trans_Other"]
        table["Nb_Transactions"] = nb_transactions

    sigma_serve = pd.to_numeric(table.get("Sigma_POS_Serve", 0), errors="coerce").replace(0, np.nan)
    table["TR_General"] = ((nb_transactions / (sigma_serve * nb_jours * 3)) * 100).fillna(0).round(1)

    return table


# ---------------------------------------------------------------------------
# Top 10 — DEUX formules distinctes (commercial vs pr_caisse)
# ---------------------------------------------------------------------------

def _normalize(series: pd.Series) -> pd.Series:
    span = series.max() - series.min()
    return (series - series.min()) / span if span > 0 else pd.Series(0.0, index=series.index)


def _rank_top_actors(table: pd.DataFrame, segment: Segment, top_n: int = 10) -> pd.DataFrame:
    """Classement Top 10, formule dependante du segment (cf. hypothese 7) :

    - commercial : 5 criteres a 20% chacun (Nb_Jours, HVC_Serve, TR_General,
      Sigma_FD, precocite de la 1ere transaction).
    - pr_caisse : 3 criteres a poids egal (~33.3% chacun), SANS TR ni
      precocite (Nb_Jours, Sigma_POS_Serve tous segments, Sigma_FD),
      restreint aux Points Relais uniquement (jamais les Caisses).
    """
    if table.empty:
        return pd.DataFrame()

    if segment == "pr_caisse":
        work = table[table.get("Type_Point") == "Point Relais"].copy()
        if work.empty:
            return pd.DataFrame()
        for col in ["Nb_Jours", "Sigma_POS_Serve", "Sigma_FD"]:
            if col not in work.columns:
                work[col] = 0
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

        score = (
            _normalize(work["Nb_Jours"]) / 3
            + _normalize(work["Sigma_POS_Serve"]) / 3
            + _normalize(work["Sigma_FD"]) / 3
        )
        work["Score_Global"] = score.round(3)
        return work.sort_values("Score_Global", ascending=False).head(top_n).reset_index(drop=True)

    # commercial : formule historique inchangee.
    work = table.copy()
    col_nb_j = "Nb_Jours_Actifs" if "Nb_Jours_Actifs" in work.columns else "Nb_Jours"
    for col in [col_nb_j, "HVC_Serve", "TR_General", "Sigma_FD", "Premiere_Trans_min"]:
        if col not in work.columns:
            work[col] = 0
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    precocite = work["Premiere_Trans_min"].replace(0, np.nan).fillna(24 * 60)
    score = (
        _normalize(work[col_nb_j]) * 0.20
        + _normalize(work["HVC_Serve"]) * 0.20
        + _normalize(work["TR_General"]) * 0.20
        + _normalize(work["Sigma_FD"]) * 0.20
        + (1 - _normalize(precocite)) * 0.20
    )
    work["Score_Global"] = score.round(3)
    return work.sort_values("Score_Global", ascending=False).head(top_n).reset_index(drop=True)


def _empty_context(filters: PerformanceFilters, message: str) -> PerformanceContext:
    return PerformanceContext(
        segment=filters.segment,
        filters=filters,
        table=pd.DataFrame(),
        top_commerciaux=pd.DataFrame(),
        is_empty=True,
        message=message,
    )


def build_unserved_pos_export(
    filters: PerformanceFilters,
    manual_mapping: Optional[pd.DataFrame] = None,
) -> bytes:
    """Génère un classeur Excel multi-feuilles contenant :
    1. 'Attribués non servis' : POS attribués à un commercial qui n'ont reçu aucune
       transaction de ce commercial sur la période / filtres sélectionnés.
    2. 'Non attribués non servis' : POS du référentiel (selon les filtres géographiques)
       non attribués à aucun commercial et n'ayant reçu aucune transaction sur la période.
    3. 'Synthèse' : Récapitulatif par commercial et par zone.
    """
    import io
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    if manual_mapping is None:
        try:
            import streamlit as st
            manual_mapping = st.session_state.get("perf_commercial_manual_mapping")
        except Exception:
            manual_mapping = None

    excluded = get_excluded_msisdns()
    actor_pool = _load_actor_pool(filters)
    tx = _load_and_scope_transactions(filters, excluded)

    actor_msisdn = getattr(filters, "actor_msisdn", None)
    if actor_msisdn and actor_msisdn not in ("Tous", "Toutes", None):
        clean_actor = clean_phone(actor_msisdn)
        actor_pool = actor_pool[actor_pool["Actor_MSISDN"] == clean_actor]
        if not tx.empty:
            tx = tx[tx["Actor_MSISDN"] == clean_actor]

    # Référentiel POS
    conn = get_connection()
    try:
        ref_pos_df = pd.read_sql_query("""
            SELECT 
                TRIM(p.agent_msisdn) AS pos_msisdn,
                MAX(p.full_name) AS pos_name,
                MAX(p.zone_centre) AS zone_centre,
                MAX(p.zone_territoire) AS zone_territoire,
                MAX(p.zone_sa) AS zone_sa,
                MAX(p.secteur_cluster) AS secteur_cluster,
                MAX(COALESCE(s.sitename, p.site_key)) AS site,
                MAX(p.segment_group) AS segment_group
            FROM referentiel_pos p
            LEFT JOIN sites s ON p.site_key = s.site_key
            WHERE p.agent_msisdn IS NOT NULL AND TRIM(p.agent_msisdn) <> ''
            GROUP BY TRIM(p.agent_msisdn)
        """, conn)
    finally:
        conn.close()

    ref_pos_df["clean_pos"] = ref_pos_df["pos_msisdn"].map(clean_phone)
    pos_lookup = {r["clean_pos"]: r for r in ref_pos_df.to_dict("records")}

    has_manual = manual_mapping is not None and isinstance(manual_mapping, pd.DataFrame) and not manual_mapping.empty

    import unicodedata, re
    def _get_name_tokens(s: Any) -> set[str]:
        if not s or pd.isna(s):
            return set()
        norm = unicodedata.normalize("NFKD", str(s)).encode("ASCII", "ignore").decode("utf-8")
        return {t for t in re.findall(r"[A-Za-z0-9]+", norm.upper()) if len(t) > 1 and t not in ("MD", "ETS", "SARL", "CCIAL", "MR", "MME")}

    def _tokens_match(t1: set[str], t2: set[str]) -> bool:
        if not t1 or not t2:
            return False
        if t1 == t2:
            return True
        common = t1.intersection(t2)
        if len(common) >= 2:
            return True
        if len(common) == 1 and (len(t1) == 1 or len(t2) == 1):
            tok = next(iter(common))
            if len(tok) >= 4:
                return True
        return False

    if has_manual:
        mapping_df = manual_mapping.copy()
        mapping_df["_clean_pos"] = mapping_df["To_clean"].fillna("").astype(str).str.strip().map(clean_phone)
        mapping_df["_clean_msisdn"] = (
            mapping_df["Commercial_MSISDN"].fillna("").astype(str).str.strip().map(clean_phone)
            if "Commercial_MSISDN" in mapping_df.columns
            else pd.Series("", index=mapping_df.index)
        )
        mapping_df["_clean_nom"] = (
            mapping_df["Commercial"].fillna("").astype(str).str.strip().str.upper()
            if "Commercial" in mapping_df.columns
            else pd.Series("", index=mapping_df.index)
        )
        mapping_df["_tokens"] = mapping_df["Commercial"].apply(_get_name_tokens)
    else:
        mapping_df = pd.DataFrame()
        pairs = tx.groupby(["Actor_MSISDN", "To_clean"]).size().reset_index(name="tx_count") if not tx.empty else pd.DataFrame()
        if not pairs.empty:
            assigned_auto = (
                pairs[pairs["tx_count"] >= 3]
                .sort_values(["To_clean", "tx_count"], ascending=[True, False])
                .drop_duplicates(subset=["To_clean"], keep="first")
            )
        else:
            assigned_auto = pd.DataFrame(columns=["Actor_MSISDN", "To_clean"])

    rows_attrib = []
    all_assigned_pos = set()

    for _, actor in actor_pool.iterrows():
        msisdn = actor.get("Actor_MSISDN")
        comm_nom = str(actor.get("Actor_Nom", "")).strip().upper()
        zone_c = actor.get("Zone_Centre", "")
        zone_t = actor.get("Zone_Territoire", "")
        zone_sa = actor.get("Zone_SA", "")

        tx_comm = tx[tx["Actor_MSISDN"] == msisdn] if not tx.empty else pd.DataFrame()
        touched_pos = set(tx_comm["To_clean"].dropna()) if not tx_comm.empty else set()

        if has_manual:
            comm_tokens = _get_name_tokens(comm_nom) if comm_nom else set()
            msisdn_9 = str(msisdn)[-9:]
            def _match_msisdn(val: Any) -> bool:
                s = str(val).strip() if pd.notna(val) else ""
                if not s or s.lower() in ("nan", "none", ""):
                    return False
                return s[-9:] == msisdn_9 if len(s) >= 9 else s == str(msisdn)

            mask = mapping_df["_clean_msisdn"].apply(_match_msisdn)
            if comm_nom:
                mask = mask | (mapping_df["_clean_nom"] == comm_nom)
            if comm_tokens:
                token_mask = mapping_df["_tokens"].apply(lambda t: _tokens_match(comm_tokens, t))
                mask = mask | token_mask

            assigned_pos = set(mapping_df.loc[mask, "_clean_pos"].dropna())
        else:
            assigned_pos = (
                set(assigned_auto[assigned_auto["Actor_MSISDN"] == msisdn]["To_clean"].astype(str))
                if not assigned_auto.empty
                else set()
            )

        assigned_pos = {p for p in assigned_pos if p}
        all_assigned_pos |= assigned_pos

        unserved_pos = assigned_pos - touched_pos
        for pos in sorted(unserved_pos):
            p_info = pos_lookup.get(pos, {})
            rows_attrib.append({
                "Numero_POS": pos,
                "Nom_POS": p_info.get("pos_name") or "",
                "Segment_POS": p_info.get("segment_group") or "",
                "Commercial_Attribue": actor.get("Actor_Nom", ""),
                "Commercial_MSISDN": msisdn or "",
                "Zone_Centre": p_info.get("zone_centre") or zone_c,
                "Zone_Territoire": p_info.get("zone_territoire") or zone_t,
                "Zone_SA": p_info.get("zone_sa") or zone_sa,
                "Secteur_Cluster": p_info.get("secteur_cluster") or "",
                "Site": p_info.get("site") or "",
                "Statut": "Attribué non servi",
            })

    cols_attrib = [
        "Numero_POS", "Nom_POS", "Segment_POS", "Commercial_Attribue", "Commercial_MSISDN",
        "Zone_Centre", "Zone_Territoire", "Zone_SA", "Secteur_Cluster", "Site", "Statut"
    ]
    df_attrib = pd.DataFrame(rows_attrib, columns=cols_attrib) if rows_attrib else pd.DataFrame(columns=cols_attrib)

    # 2. Non attribués et non servis
    all_touched_pos = set(tx["To_clean"].dropna()) if not tx.empty else set()

    ref_in_scope = ref_pos_df.copy()
    if filters.zone_centre not in ("Toutes", "Tous", None):
        ref_in_scope = ref_in_scope[ref_in_scope["zone_centre"] == filters.zone_centre]
    if filters.zone_territoire not in ("Toutes", "Tous", None):
        ref_in_scope = ref_in_scope[ref_in_scope["zone_territoire"] == filters.zone_territoire]
    if filters.zone_sa not in ("Toutes", "Tous", None):
        ref_in_scope = ref_in_scope[ref_in_scope["zone_sa"] == filters.zone_sa]

    unassigned_unserved = ref_in_scope[
        ~ref_in_scope["clean_pos"].isin(all_assigned_pos)
        & ~ref_in_scope["clean_pos"].isin(all_touched_pos)
    ]

    rows_non_attrib = []
    for _, r in unassigned_unserved.iterrows():
        rows_non_attrib.append({
            "Numero_POS": r["clean_pos"],
            "Nom_POS": r["pos_name"] or "",
            "Segment_POS": r["segment_group"] or "",
            "Zone_Centre": r["zone_centre"] or "",
            "Zone_Territoire": r["zone_territoire"] or "",
            "Zone_SA": r["zone_sa"] or "",
            "Secteur_Cluster": r["secteur_cluster"] or "",
            "Site": r["site"] or "",
            "Commercial_Attribue": "Non attribué",
            "Statut": "Non attribué non servi",
        })
    cols_non_attrib = [
        "Numero_POS", "Nom_POS", "Segment_POS", "Zone_Centre", "Zone_Territoire",
        "Zone_SA", "Secteur_Cluster", "Site", "Commercial_Attribue", "Statut"
    ]
    df_non_attrib = pd.DataFrame(rows_non_attrib, columns=cols_non_attrib) if rows_non_attrib else pd.DataFrame(columns=cols_non_attrib)

    # 3. Synthèse
    recap_comm = (
        df_attrib.groupby(["Zone_SA", "Commercial_Attribue", "Commercial_MSISDN"]).size().reset_index(name="Nb_Attribues_Non_Servis")
        if not df_attrib.empty
        else pd.DataFrame(columns=["Zone_SA", "Commercial_Attribue", "Commercial_MSISDN", "Nb_Attribues_Non_Servis"])
    )
    recap_zone = (
        df_non_attrib.groupby(["Zone_Centre", "Zone_Territoire", "Zone_SA"]).size().reset_index(name="Nb_Non_Attribues_Non_Servis")
        if not df_non_attrib.empty
        else pd.DataFrame(columns=["Zone_Centre", "Zone_Territoire", "Zone_SA", "Nb_Non_Attribues_Non_Servis"])
    )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_attrib.to_excel(writer, sheet_name="Attribues_Non_Servis", index=False)
        df_non_attrib.to_excel(writer, sheet_name="Non_Attribues_Non_Servis", index=False)
        recap_comm.to_excel(writer, sheet_name="Synthese_Commerciaux", index=False)
        recap_zone.to_excel(writer, sheet_name="Synthese_Zones", index=False)

        wb = writer.book
        header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        thin_border = Border(
            left=Side(style="thin", color="E5E7EB"),
            right=Side(style="thin", color="E5E7EB"),
            top=Side(style="thin", color="E5E7EB"),
            bottom=Side(style="thin", color="E5E7EB"),
        )
        for ws in wb.worksheets:
            ws.views.sheetView[0].showGridLines = True
            for col_idx in range(1, ws.max_column + 1):
                cell = ws.cell(row=1, column=col_idx)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
                col_letter = get_column_letter(col_idx)
                max_len = max(len(str(ws.cell(row=r, column=col_idx).value or "")) for r in range(1, min(ws.max_row + 1, 100)))
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=ws.max_column):
                for cell in row:
                    cell.border = thin_border

    return output.getvalue()

