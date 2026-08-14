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
    get_commercial_referentiel,
    get_dotation_transactions,
    get_hvc_msisdns,
    get_hvc_quota_by_cds,
    get_mvc_lvc_msisdns,
    get_performance_filter_options,
    get_performance_transactions,
    get_point_relay_referentiel,
)
from models.reference_model import get_exclusions

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

def build_performance_context(filters: PerformanceFilters) -> PerformanceContext:
    segment = filters.segment
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
        return PerformanceContext(segment=segment, filters=filters, table=table, message="Aucune transaction sur la periode.")

    hvc_set = get_hvc_msisdns()
    others_set = get_mvc_lvc_msisdns() if _SEGMENT_HVC_OTHERS_SPLIT[segment] else set()

    hvc_others = _compute_hvc_others(tx, hvc_set, others_set, split=_SEGMENT_HVC_OTHERS_SPLIT[segment])
    dotation = _compute_dotation(filters, actor_pool)

    table = actor_pool.merge(hvc_others, on="Actor_MSISDN", how="left")
    table = table.merge(dotation, on="Actor_MSISDN", how="left")

    numeric_cols = ["Nb_Jours", "FD_HVC", "HVC_Serve", "Nb_Trans_HVC", "FD_Others", "Other_Serve", "Nb_Trans_Other", "Dotation_Montant"]
    for col in numeric_cols:
        if col not in table.columns:
            table[col] = 0
        table[col] = pd.to_numeric(table[col], errors="coerce").fillna(0)

    table["Sigma_FD"] = table["FD_HVC"] + table["FD_Others"]
    table["Sigma_POS_Serve"] = table["HVC_Serve"] + table["Other_Serve"]

    top_commerciaux = pd.DataFrame()

    if segment == "commercial":
        # Activite quotidienne + rotation : necessaires au Top 10 commercial
        # (precocite de 1ere transaction, TR_General).
        # Vérifier si la plage de recherche couvre un seul jour
        is_single_day = (filters.start_date == filters.end_date)

        # Calcul des activités quotidiennes adapté
        activity = _compute_daily_activity(tx, is_single_day=is_single_day)
        table = table.merge(activity, on="Actor_MSISDN", how="left")
        table["Nb_Jours"] = pd.to_numeric(table.get("Nb_Jours_Actifs"), errors="coerce").fillna(0)
        table = _add_rotation_rates(table)

        # Work Progress commercial : Serve + TR, "Other" au sens large (pas
        # HVC), cf. hypothese 6.
        work_progress = _compute_work_progress(tx, hvc_set, others_set=None, include_tr=True)
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

    if segment == "cds":
        quota = get_hvc_quota_by_cds().rename(columns={"nom_cds": "Actor_Nom"})
        table = table.merge(quota, on="Actor_Nom", how="left")
        table["nb_hvc_attribue"] = pd.to_numeric(table.get("nb_hvc_attribue"), errors="coerce").fillna(0).astype(int)

        # Taux couverture quota (%) = HVC_Serve / nb_hvc_attribue * 100.
        # None (pas 0) si nb_hvc_attribue == 0 : un CDS sans quota attribue n'a pas
        # un "taux de 0%", il n'a simplement pas de quota — la vue doit afficher
        # "N/A", pas confondre avec un CDS qui a un quota et ne le sert pas.
        # La ligne TOTAL doit sommer HVC_Serve et nb_hvc_attribue separement
        # puis diviser une seule fois — ne jamais moyenner les pourcentages
        # deja arrondis de cette colonne (biais).
        table["Taux_Couverture_Quota"] = table.apply(
            lambda row: round(row["HVC_Serve"] / row["nb_hvc_attribue"] * 100, 1)
            if row["nb_hvc_attribue"] > 0 else None,
            axis=1,
        )

    return PerformanceContext(segment=segment, filters=filters, table=table, top_commerciaux=top_commerciaux)


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
        return df.rename(columns={
            "ccial_msisdn": "Actor_MSISDN",
            "nom_ccial": "Actor_Nom",
            "zone_centre": "Zone_Centre",
            "zone_territoire": "Zone_Territoire",
            "zone_sa": "Zone_SA",
        })
    if segment == "pr_caisse":
        df = get_point_relay_referentiel(type_point=filters.type_point, territoire=filters.territoire)
        return df.rename(columns={
            "msisdn_pr": "Actor_MSISDN",
            "nom": "Actor_Nom",
            "territoire": "Territoire",
            "localisation": "Localisation",
            "type_point": "Type_Point",
        })
    if segment == "cds":
        df = get_cds_referentiel()
        return df.rename(columns={"cds_msisdn": "Actor_MSISDN", "nom_cds": "Actor_Nom"})
    raise ValueError(f"Segment inconnu: {segment}")


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

def _add_rotation_rates(table: pd.DataFrame) -> pd.DataFrame:
    """TR_HVC / TR_Other / TR_General = Nb_Trans / (Serve * Nb_Jours * 3) * 100, sur la periode complete.

    Utilisee UNIQUEMENT pour commercial : le Top 10 pr_caisse n'utilise pas
    de taux de rotation (cf. hypothese 7), donc ce calcul ne lui est plus
    applique.
    """
    table = table.copy()
    nb_jours = pd.to_numeric(table.get("Nb_Jours", 0), errors="coerce").replace(0, np.nan)

    def _rate(nb_trans_col: str, serve_col: str) -> pd.Series:
        nb_trans = pd.to_numeric(table.get(nb_trans_col, 0), errors="coerce")
        serve = pd.to_numeric(table.get(serve_col, 0), errors="coerce").replace(0, np.nan)
        return ((nb_trans / (serve * nb_jours * 3)) * 100).fillna(0).round(1)

    table["TR_HVC"] = _rate("Nb_Trans_HVC", "HVC_Serve")
    table["TR_Other"] = _rate("Nb_Trans_Other", "Other_Serve")

    nb_transactions = pd.to_numeric(table.get("Nb_Transactions", 0), errors="coerce")
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
    for col in ["Nb_Jours", "HVC_Serve", "TR_General", "Sigma_FD", "Premiere_Trans_min"]:
        if col not in work.columns:
            work[col] = 0
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    precocite = work["Premiere_Trans_min"].replace(0, np.nan).fillna(24 * 60)
    score = (
        _normalize(work["Nb_Jours"]) * 0.20
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