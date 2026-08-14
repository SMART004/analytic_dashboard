from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from controllers.kpis import kpi_encroachment_rates_by_type
from models.conquete_model import (
    get_conquete_filter_options,
    get_conquete_transactions,
    get_empietement,
    get_conquete_evolution_series,
)
from models.pos_model import get_all_pos
from utils.helpers import clean_phone


@dataclass(frozen=True)
class ConqueteFilters:
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    zone_centre: str = "Toutes"
    zone_territoire: str = "Toutes"
    zone_sa: str = "Toutes"
    quartier: str = "Toutes"
    segment: str = "Tous"
    type_empietement: str = "Tous"


@dataclass(frozen=True)
class ConqueteContext:
    filters: ConqueteFilters
    transactions: pd.DataFrame
    encroachment: pd.DataFrame
    encroachment_matrix: pd.DataFrame    # matrice croisée Zone SA Commercial vs PDV
    kpis: dict[str, Any]
    strategic_table: pd.DataFrame        # tableau stratégique BI enrichi
    commercial_portfolio: pd.DataFrame
    evolution_series: pd.DataFrame       # série temporelle quotidienne
    is_empty: bool = False
    message: str = ""

def _only_confirmed_commerciaux(tx: pd.DataFrame) -> pd.DataFrame:
    """Garde uniquement les TX dont From est dans referentiel_commerciaux."""
    if tx is None or tx.empty:
        return pd.DataFrame()
    work = tx.copy()
    if "Commercial" not in work.columns:
        return pd.DataFrame()
    mask = (
        work["Commercial"].notna()
        & work["Commercial"].astype(str).str.strip().ne("")
        & ~work["Commercial"].astype(str).str.strip().str.lower().isin(
            {"none", "nan", "n/a", "inconnu", "non renseigné", "non renseigne"}
        )
    )
    return work.loc[mask].copy()


def load_filter_options() -> dict[str, Any]:
    try:
        return get_conquete_filter_options()
    except Exception as exc:
        return {
            "min_date": None, "max_date": None,
            "zone_centre": [], "zone_territoire": [], "zone_sa": [],
            "quartier": [], "segment": [], "error": str(exc),
        }


def build_conquete_context(filters: ConqueteFilters) -> ConqueteContext:
    """Construit le contexte complet pour la page Conquête de Territoire.

    Logique métier Mobile Money :
    - Un commercial est "actif" s'il a effectué ≥1 Transfer de ≥10 000 FCFA
      sur la période sélectionnée.
    - Le Taux de Couverture = PDV touchés / Total PDV Référentiel × 100
    - Le Tableau Stratégique = agrégation Zone_SA avec les 9 KPIs décisionnels
    - La Matrice d'Empiètement = croisement Zone SA Commercial vs Zone SA PDV
    """
    try:
        tx = get_conquete_transactions(
            start_date=filters.start_date,
            end_date=filters.end_date,
            zone_centre=filters.zone_centre if filters.zone_centre not in ("Toutes", "Tous") else None,
            zone_territoire=filters.zone_territoire if filters.zone_territoire not in ("Toutes", "Tous") else None,
            zone_sa=filters.zone_sa if filters.zone_sa not in ("Toutes", "Tous") else None,
            quartier=filters.quartier if filters.quartier not in ("Toutes", "Tous") else None,
            segment=filters.segment if filters.segment not in ("Tous", "Toutes") else None,
            min_amount=10000.0,
        )
    except Exception as exc:
        return _empty_context(filters, f"Impossible de charger les données de conquête : {exc}")

    if tx.empty:
        return _empty_context(filters, "Aucune transaction trouvée pour la période et les filtres sélectionnés.")

    # Référentiel POS pour le total (dénominateur)
    try:
        ref_df = get_all_pos(
            zone_centre=filters.zone_centre if filters.zone_centre not in ("Toutes", "Tous") else None,
            zone_sa=filters.zone_sa if filters.zone_sa not in ("Toutes", "Tous") else None,
            territory=filters.zone_territoire if filters.zone_territoire not in ("Toutes", "Tous") else None,
            segment=filters.segment if filters.segment not in ("Tous", "Toutes") else None,
        )
    except Exception:
        ref_df = pd.DataFrame()

    # Total POS référentiel dans la sélection
    total_pos_ref = int(ref_df["agent_msisdn"].nunique()) if not ref_df.empty and "agent_msisdn" in ref_df.columns else 0

    # Référentiel Commerciaux (total enregistrés)
    try:
        from models.db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT ccial_msisdn) AS total FROM referentiel_commerciaux WHERE ccial_msisdn IS NOT NULL")
        row = cursor.fetchone()
        total_commerciaux_ref = int(row["total"]) if row else 0
        conn.close()
    except Exception:
        total_commerciaux_ref = 0

    # Commerciaux ACTIFS = ont réellement réalisé ≥1 Transfer dans la période
    # Comptage UNIQUE par MSISDN, en excluant les MSISDN non reconnus comme commerciaux
    # (ceux dont nom_ccial IS NULL dans le LEFT JOIN referentiel_commerciaux)
    if "Commercial_MSISDN" in tx.columns and "Commercial" in tx.columns:
        # "Commercial" = nom_ccial — NULL si le from_msisdn n'est pas un commercial enregistré
        confirmed_mask = tx["Commercial"].notna() & (tx["Commercial"].astype(str).str.strip() != "")
        touched_commerciaux = int(tx.loc[confirmed_mask, "Commercial_MSISDN"].dropna().nunique())
    elif "Commercial_MSISDN" in tx.columns:
        touched_commerciaux = int(tx["Commercial_MSISDN"].dropna().nunique())
    else:
        touched_commerciaux = 0

    # Empiètement
    emp = get_empietement(
        start_date=filters.start_date,
        end_date=filters.end_date,
        type_empietement=None if filters.type_empietement in ("Tous", "Toutes") else filters.type_empietement,
    )
    if not emp.empty:
        encroachment = emp.copy()
        encroachment["Type_Empietement"] = encroachment.get(
            "Type_Empietement", pd.Series(index=encroachment.index, dtype="object")
        ).fillna("Intra-centre")
    else:
        encroachment = emp

    rates = kpi_encroachment_rates_by_type(encroachment, tx)

    # Taux de couverture global
    pdv_touches = int(tx["To_clean"].dropna().nunique()) if "To_clean" in tx.columns else 0
    taux_couverture = round(pdv_touches / total_pos_ref * 100, 1) if total_pos_ref > 0 else 0.0

    # Panier moyen par transaction
    nb_tx = int(len(tx))
    volume = float(tx["Amount"].sum()) if "Amount" in tx.columns else 0.0
    panier_moyen = round(volume / nb_tx, 0) if nb_tx > 0 else 0.0

    kpis = {
        "nb_transactions": nb_tx,
        "volume": volume,
        "touched_pos": pdv_touches,
        "total_pos_ref": total_pos_ref,
        "pos_non_touches": max(0, total_pos_ref - pdv_touches),
        "taux_couverture": taux_couverture,
        "touched_commerciaux": touched_commerciaux,
        "total_commerciaux_ref": total_commerciaux_ref,
        "panier_moyen": panier_moyen,
        "empietement_intra": rates.get("Intra-centre", 0.0),
        "empietement_inter": rates.get("Inter-centre", 0.0),
    }

    # Tableau stratégique BI par Zone_SA
    strategic = _build_strategic_table(tx, ref_df, encroachment)

    # Matrice d'empiètement Zone SA Commercial × Zone SA PDV
    encroachment_matrix = _build_encroachment_matrix(encroachment)

    # Portefeuille commercial
    portfolio = _build_commercial_portfolio(tx)

    # Série d'évolution quotidienne
    try:
        evolution_series = get_conquete_evolution_series(
            start_date=filters.start_date,
            end_date=filters.end_date,
            zone_centre=filters.zone_centre if filters.zone_centre not in ("Toutes", "Tous") else None,
            zone_territoire=filters.zone_territoire if filters.zone_territoire not in ("Toutes", "Tous") else None,
            zone_sa=filters.zone_sa if filters.zone_sa not in ("Toutes", "Tous") else None,
            segment=filters.segment if filters.segment not in ("Tous", "Toutes") else None,
        )
    except Exception:
        evolution_series = pd.DataFrame()

    return ConqueteContext(
        filters=filters,
        transactions=tx,
        encroachment=encroachment,
        encroachment_matrix=encroachment_matrix,
        kpis=kpis,
        strategic_table=strategic,
        commercial_portfolio=portfolio,
        evolution_series=evolution_series,
        is_empty=False,
        message="",
    )


# def _build_strategic_table(
#     tx: pd.DataFrame,
#     ref_df: pd.DataFrame,
#     emp: pd.DataFrame,
# ) -> pd.DataFrame:
#     """Tableau stratégique BI par Zone_SA — 9 indicateurs décisionnels.

#     Colonnes produites:
#         Zone_SA, Total_POS_Ref, POS_Touches, POS_Non_Touches,
#         Taux_Couverture_pct, Volume_FCFA, Nb_Transactions,
#         Panier_Moyen_FCFA, Commerciaux_Actifs, Productivite_Moy_FCFA,
#         Nb_Empiétements
#     """
#     if tx.empty:
#         return pd.DataFrame()

#     work = tx.copy()
#     work_comm = _only_confirmed_commerciaux(tx)

#     if "Zone_SA_PDV" not in work.columns:
#         work["Zone_SA_PDV"] = work.get("Zone_SA", pd.NA)
#     if "Amount" not in work.columns:
#         work["Amount"] = 0.0

#     # Filtrer les Zone_SA invalides
#     _INVALID = {"none", "non renseigné", "non renseigne", "n/a", "", "null", "nan"}
#     work["Zone_SA_PDV"] = work["Zone_SA_PDV"].astype(str).str.strip()
#     work = work[~work["Zone_SA_PDV"].str.lower().isin(_INVALID)]

    
#     tx_agg = (
#         work_all.groupby("Zone_SA_PDV", dropna=False)
#         .agg(
#             Nb_Transactions=("Amount", "count"),
#             Volume_FCFA=("Amount", "sum"),
#             POS_Touches=("To_clean", "nunique"),
#         )
#         .reset_index()
#         .rename(columns={"Zone_SA_PDV": "Zone_SA"})
#     )

#     if not work_comm.empty:
#         comm_agg = (
#             work_comm.groupby("Zone_SA_PDV", dropna=False)["Commercial_MSISDN"]
#             .nunique()
#             .reset_index()
#             .rename(columns={"Zone_SA_PDV": "Zone_SA", "Commercial_MSISDN": "Commerciaux_Actifs"})
#         )
#         tx_agg = tx_agg.merge(comm_agg, on="Zone_SA", how="left")
#     else:
#         tx_agg["Commerciaux_Actifs"] = 0

#     tx_agg["Commerciaux_Actifs"] = tx_agg["Commerciaux_Actifs"].fillna(0).astype(int)
#     tx_agg["Panier_Moyen_FCFA"] = (
#         tx_agg["Volume_FCFA"] / tx_agg["Nb_Transactions"].replace(0, pd.NA)
#     ).round(0).fillna(0)
#     tx_agg["Productivite_Moy_FCFA"] = (
#         tx_agg["Volume_FCFA"] / tx_agg["Commerciaux_Actifs"].replace(0, pd.NA)
#     ).round(0).fillna(0)

#     # POS référentiel par Zone_SA
#     if not ref_df.empty and "sa_incharge" in ref_df.columns and "agent_msisdn" in ref_df.columns:
#         ref_agg = (
#             ref_df.copy()
#             .assign(sa_clean=ref_df["sa_incharge"].astype(str).str.strip())
#             .groupby("sa_clean")["agent_msisdn"]
#             .nunique()
#             .reset_index()
#             .rename(columns={"sa_clean": "Zone_SA", "agent_msisdn": "Total_POS_Ref"})
#         )
#         tx_agg = tx_agg.merge(ref_agg, on="Zone_SA", how="left")
#     else:
#         tx_agg["Total_POS_Ref"] = 0

#     tx_agg["Total_POS_Ref"] = tx_agg["Total_POS_Ref"].fillna(0).astype(int)
#     tx_agg["POS_Non_Touches"] = (tx_agg["Total_POS_Ref"] - tx_agg["POS_Touches"]).clip(lower=0)
#     tx_agg["Taux_Couverture_pct"] = (
#         tx_agg["POS_Touches"] / tx_agg["Total_POS_Ref"].replace(0, pd.NA) * 100
#     ).round(1).fillna(0.0)

#     # Empiètements par Zone PDV
#     if not emp.empty and "Zone PDV" in emp.columns:
#         emp_agg = (
#             emp.copy()
#             .assign(zone_pdv=emp["Zone PDV"].astype(str).str.strip())
#             .groupby("zone_pdv").size()
#             .reset_index(name="Nb_Empiétements")
#             .rename(columns={"zone_pdv": "Zone_SA"})
#         )
#         tx_agg = tx_agg.merge(emp_agg, on="Zone_SA", how="left")
#     else:
#         tx_agg["Nb_Empiétements"] = 0

#     tx_agg["Nb_Empiétements"] = tx_agg["Nb_Empiétements"].fillna(0).astype(int)

#     col_order = [
#         "Zone_SA", "Total_POS_Ref", "POS_Touches", "POS_Non_Touches",
#         "Taux_Couverture_pct", "Volume_FCFA", "Nb_Transactions",
#         "Panier_Moyen_FCFA", "Commerciaux_Actifs", "Productivite_Moy_FCFA",
#         "Nb_Empiétements",
#     ]
#     existing_cols = [c for c in col_order if c in tx_agg.columns]
#     return (
#         tx_agg[existing_cols]
#         .sort_values(["Volume_FCFA", "POS_Touches"], ascending=[False, False])
#         .reset_index(drop=True)
#     )


# def _build_encroachment_matrix(emp: pd.DataFrame) -> pd.DataFrame:
#     """Matrice croisée Zone_SA_Commercial × Zone_SA_PDV.

#     Chaque cellule = nombre de cas d'empiètement détectés entre ces deux zones.
#     Utile pour identifier les flux d'empiètement structurels.
#     """
#     if emp.empty:
#         return pd.DataFrame()

#     col_comm = next((c for c in ["Zone commerciale", "Zone_SA_Comm", "zone_sa_comm"] if c in emp.columns), None)
#     col_pdv = next((c for c in ["Zone PDV", "Zone_SA_PDV", "zone_sa_pdv"] if c in emp.columns), None)

#     if not col_comm or not col_pdv:
#         return pd.DataFrame()

#     _INVALID = {"none", "non renseigné", "non renseigne", "n/a", "", "null", "nan"}

#     work = emp.copy()
#     work["_sa_comm"] = work[col_comm].astype(str).str.strip()
#     work["_sa_pdv"] = work[col_pdv].astype(str).str.strip()
#     work = work[
#         ~work["_sa_comm"].str.lower().isin(_INVALID) &
#         ~work["_sa_pdv"].str.lower().isin(_INVALID)
#     ]

#     if work.empty:
#         return pd.DataFrame()

#     matrix = (
#         work.groupby(["_sa_comm", "_sa_pdv"])
#         .agg(nb_cas=("nb_transactions" if "nb_transactions" in work.columns else "_sa_comm", "count"))
#         .reset_index()
#         .rename(columns={"_sa_comm": "Zone_SA_Commercial", "_sa_pdv": "Zone_SA_PDV", "nb_cas": "Nb_Cas"})
#     )
#     return matrix.sort_values("Nb_Cas", ascending=False).reset_index(drop=True)


# def _build_commercial_portfolio(tx: pd.DataFrame) -> pd.DataFrame:
#     if tx.empty:
#         return pd.DataFrame()
#     work = tx.copy()
#     if "Commercial" not in work.columns:
#         work["Commercial"] = "Non renseigné"
#     if "Zone_SA_Comm" not in work.columns:
#         work["Zone_SA_Comm"] = work.get("Zone_SA", pd.NA)
#     if "Amount" not in work.columns:
#         work["Amount"] = 0.0

#     _INVALID = {"none", "non renseigné", "non renseigne", "nan"}
#     work["Commercial"] = work["Commercial"].astype(str).str.strip()
#     work = work[~work["Commercial"].str.lower().isin(_INVALID)]

#     result = (
#         work.groupby(["Commercial", "Zone_SA_Comm"], dropna=False)
#         .agg(
#             Transactions=("Amount", "count"),
#             Volume=("Amount", "sum"),
#             PDV_Touches=("To_clean", "nunique"),
#         )
#         .reset_index()
#         .rename(columns={"Zone_SA_Comm": "Zone_SA"})
#         .sort_values(["Volume", "Transactions"], ascending=[False, False])
#         .reset_index(drop=True)
#     )
#     result["Productivité_FCFA"] = (
#         result["Volume"] / result["PDV_Touches"].replace(0, pd.NA)
#     ).round(0).fillna(0)
#     return result



def _build_strategic_table(
    tx: pd.DataFrame,
    ref_df: pd.DataFrame,
    emp: pd.DataFrame,
) -> pd.DataFrame:
    """Tableau stratégique BI par Zone_SA — 9 indicateurs décisionnels.

    Commerciaux_Actifs = uniquement les MSISDN présents dans referentiel_commerciaux.
    """
    if tx is None or tx.empty:
        return pd.DataFrame()

    work = tx.copy()
    if "Zone_SA_PDV" not in work.columns:
        work["Zone_SA_PDV"] = work.get("Zone_SA", pd.NA)
    if "Amount" not in work.columns:
        work["Amount"] = 0.0
    if "To_clean" not in work.columns:
        return pd.DataFrame()

    _INVALID = {"none", "non renseigné", "non renseigne", "n/a", "", "null", "nan"}
    work["Zone_SA_PDV"] = work["Zone_SA_PDV"].astype(str).str.strip()
    work = work[~work["Zone_SA_PDV"].str.lower().isin(_INVALID)]

    # Agrégats volume / POS (toutes TX filtrées)
    tx_agg = (
        work.groupby("Zone_SA_PDV", dropna=False)
        .agg(
            Nb_Transactions=("Amount", "count"),
            Volume_FCFA=("Amount", "sum"),
            POS_Touches=("To_clean", "nunique"),
        )
        .reset_index()
        .rename(columns={"Zone_SA_PDV": "Zone_SA"})
    )

    # Commerciaux actifs = uniquement référentiel
    work_comm = _only_confirmed_commerciaux(work)
    if not work_comm.empty and "Commercial_MSISDN" in work_comm.columns:
        comm_agg = (
            work_comm.groupby("Zone_SA_PDV", dropna=False)["Commercial_MSISDN"]
            .nunique()
            .reset_index()
            .rename(columns={
                "Zone_SA_PDV": "Zone_SA",
                "Commercial_MSISDN": "Commerciaux_Actifs",
            })
        )
        tx_agg = tx_agg.merge(comm_agg, on="Zone_SA", how="left")
    else:
        tx_agg["Commerciaux_Actifs"] = 0

    tx_agg["Commerciaux_Actifs"] = tx_agg["Commerciaux_Actifs"].fillna(0).astype(int)

    tx_agg["Panier_Moyen_FCFA"] = (
        tx_agg["Volume_FCFA"] / tx_agg["Nb_Transactions"].replace(0, pd.NA)
    ).round(0).fillna(0)
    tx_agg["Productivite_Moy_FCFA"] = (
        tx_agg["Volume_FCFA"] / tx_agg["Commerciaux_Actifs"].replace(0, pd.NA)
    ).round(0).fillna(0)

    # POS référentiel par Zone_SA
    if not ref_df.empty and "agent_msisdn" in ref_df.columns:
        sa_col = next(
            (c for c in ["zone_sa", "sa_incharge", "Zone_SA"] if c in ref_df.columns),
            None,
        )
        if sa_col:
            ref_agg = (
                ref_df.copy()
                .assign(sa_clean=ref_df[sa_col].astype(str).str.strip())
                .groupby("sa_clean")["agent_msisdn"]
                .nunique()
                .reset_index()
                .rename(columns={"sa_clean": "Zone_SA", "agent_msisdn": "Total_POS_Ref"})
            )
            tx_agg = tx_agg.merge(ref_agg, on="Zone_SA", how="left")
        else:
            tx_agg["Total_POS_Ref"] = 0
    else:
        tx_agg["Total_POS_Ref"] = 0

    tx_agg["Total_POS_Ref"] = tx_agg["Total_POS_Ref"].fillna(0).astype(int)
    tx_agg["POS_Non_Touches"] = (tx_agg["Total_POS_Ref"] - tx_agg["POS_Touches"]).clip(lower=0)
    tx_agg["Taux_Couverture_pct"] = (
        tx_agg["POS_Touches"] / tx_agg["Total_POS_Ref"].replace(0, pd.NA) * 100
    ).round(1).fillna(0.0)

    # Empiètements par Zone PDV
    if not emp.empty:
        emp_col = next(
            (c for c in ["Zone PDV", "Zone_SA_PDV", "zone_sa_pdv"] if c in emp.columns),
            None,
        )
        if emp_col:
            emp_agg = (
                emp.copy()
                .assign(zone_pdv=emp[emp_col].astype(str).str.strip())
                .groupby("zone_pdv")
                .size()
                .reset_index(name="Nb_Empiétements")
                .rename(columns={"zone_pdv": "Zone_SA"})
            )
            tx_agg = tx_agg.merge(emp_agg, on="Zone_SA", how="left")
        else:
            tx_agg["Nb_Empiétements"] = 0
    else:
        tx_agg["Nb_Empiétements"] = 0

    tx_agg["Nb_Empiétements"] = tx_agg["Nb_Empiétements"].fillna(0).astype(int)

    col_order = [
        "Zone_SA", "Total_POS_Ref", "POS_Touches", "POS_Non_Touches",
        "Taux_Couverture_pct", "Volume_FCFA", "Nb_Transactions",
        "Panier_Moyen_FCFA", "Commerciaux_Actifs", "Productivite_Moy_FCFA",
        "Nb_Empiétements",
    ]
    existing_cols = [c for c in col_order if c in tx_agg.columns]
    return (
        tx_agg[existing_cols]
        .sort_values(["Volume_FCFA", "POS_Touches"], ascending=[False, False])
        .reset_index(drop=True)
    )


def _build_encroachment_matrix(emp: pd.DataFrame) -> pd.DataFrame:
    """Matrice croisée Zone_SA_Commercial × Zone_SA_PDV.

    get_empietement joint déjà referentiel_commerciaux → pas de faux acteurs.
    """
    if emp is None or emp.empty:
        return pd.DataFrame()

    col_comm = next(
        (c for c in ["Zone commerciale", "Zone_SA_Comm", "zone_sa_comm"] if c in emp.columns),
        None,
    )
    col_pdv = next(
        (c for c in ["Zone PDV", "Zone_SA_PDV", "zone_sa_pdv"] if c in emp.columns),
        None,
    )
    if not col_comm or not col_pdv:
        return pd.DataFrame()

    _INVALID = {"none", "non renseigné", "non renseigne", "n/a", "", "null", "nan"}

    work = emp.copy()
    work["_sa_comm"] = work[col_comm].astype(str).str.strip()
    work["_sa_pdv"] = work[col_pdv].astype(str).str.strip()
    work = work[
        ~work["_sa_comm"].str.lower().isin(_INVALID)
        & ~work["_sa_pdv"].str.lower().isin(_INVALID)
    ]
    if work.empty:
        return pd.DataFrame()

    count_col = "nb_transactions" if "nb_transactions" in work.columns else "_sa_comm"
    matrix = (
        work.groupby(["_sa_comm", "_sa_pdv"])
        .agg(Nb_Cas=(count_col, "count" if count_col == "_sa_comm" else "sum"))
        .reset_index()
        .rename(columns={
            "_sa_comm": "Zone_SA_Commercial",
            "_sa_pdv": "Zone_SA_PDV",
        })
    )
    return matrix.sort_values("Nb_Cas", ascending=False).reset_index(drop=True)


def _build_commercial_portfolio(tx: pd.DataFrame) -> pd.DataFrame:
    """Portefeuille par commercial — strictement referentiel_commerciaux."""
    work = _only_confirmed_commerciaux(tx)
    if work.empty:
        return pd.DataFrame()

    if "Zone_SA_Comm" not in work.columns:
        work["Zone_SA_Comm"] = work.get("Zone_SA", pd.NA)
    if "Amount" not in work.columns:
        work["Amount"] = 0.0
    if "To_clean" not in work.columns:
        return pd.DataFrame()
    if "Commercial_MSISDN" not in work.columns:
        work["Commercial_MSISDN"] = work.get("From_clean", pd.NA)

    result = (
        work.groupby(["Commercial", "Commercial_MSISDN", "Zone_SA_Comm"], dropna=False)
        .agg(
            Transactions=("Amount", "count"),
            Volume=("Amount", "sum"),
            PDV_Touches=("To_clean", "nunique"),
        )
        .reset_index()
        .rename(columns={"Zone_SA_Comm": "Zone_SA"})
        .sort_values(["Volume", "Transactions"], ascending=[False, False])
        .reset_index(drop=True)
    )
    result["Productivité_FCFA"] = (
        result["Volume"] / result["PDV_Touches"].replace(0, pd.NA)
    ).round(0).fillna(0)
    return result

def _empty_context(filters: ConqueteFilters, message: str) -> ConqueteContext:
    return ConqueteContext(
        filters=filters,
        transactions=pd.DataFrame(),
        encroachment=pd.DataFrame(),
        encroachment_matrix=pd.DataFrame(),
        kpis={},
        strategic_table=pd.DataFrame(),
        commercial_portfolio=pd.DataFrame(),
        evolution_series=pd.DataFrame(),
        is_empty=True,
        message=message,
    )

CAPILLARITE_CIBLE = 80


def _avg_daily_hours(tx_comm: pd.DataFrame) -> pd.DataFrame:
    if tx_comm.empty:
        return pd.DataFrame(columns=["Commercial_MSISDN", "Heure_debut_moy", "Heure_fin_moy"])

    work = tx_comm.copy()
    if "Hour" not in work.columns and "Date" in work.columns:
        work["Hour"] = pd.to_datetime(work["Date"], errors="coerce").dt.hour
    if "Date_only" not in work.columns and "Date" in work.columns:
        work["Date_only"] = pd.to_datetime(work["Date"], errors="coerce").dt.date
    if "Hour" not in work.columns or "Date_only" not in work.columns:
        return pd.DataFrame(columns=["Commercial_MSISDN", "Heure_debut_moy", "Heure_fin_moy"])

    daily = (
        work.groupby(["Commercial_MSISDN", "Date_only"])["Hour"]
        .agg(h_min="min", h_max="max")
        .reset_index()
    )
    return (
        daily.groupby("Commercial_MSISDN")
        .agg(Heure_debut_moy=("h_min", "mean"), Heure_fin_moy=("h_max", "mean"))
        .reset_index()
    )


def _fmt_hour(h) -> str:
    if h is None or (isinstance(h, float) and pd.isna(h)):
        return "—"
    try:
        h = float(h)
        return f"{int(h):02d}h{int(round((h % 1) * 60)):02d}"
    except Exception:
        return "—"


def _pos_ref_by_zone_sa(ref_df: pd.DataFrame) -> pd.Series:
    if ref_df is None or ref_df.empty:
        return pd.Series(dtype=float)
    sa_col = next((c for c in ["zone_sa", "sa_incharge"] if c in ref_df.columns), None)
    msisdn_col = next((c for c in ["agent_msisdn", "msisdn"] if c in ref_df.columns), None)
    if not sa_col or not msisdn_col:
        return pd.Series(dtype=float)
    return ref_df.groupby(ref_df[sa_col].astype(str).str.strip())[msisdn_col].nunique()


# def _build_portfolio_enriched(
#     assigned: pd.DataFrame,
#     tx_comm: pd.DataFrame,
#     ref_df: pd.DataFrame,
#     start_date: str | None = None,
#     end_date: str | None = None,
# ) -> pd.DataFrame:
#     if assigned.empty:
#         return pd.DataFrame()

#     # Zone SA principale
#     zone_map = {}
#     if not tx_comm.empty and "Zone_SA_Comm" in tx_comm.columns and "Commercial_MSISDN" in tx_comm.columns:
#         zone_map = (
#             tx_comm.groupby("Commercial_MSISDN")["Zone_SA_Comm"]
#             .agg(lambda s: s.mode().iat[0] if not s.mode().empty else "N/A")
#             .to_dict()
#         )

#     base = (
#         assigned.groupby(["Commercial", "Commercial_MSISDN"], dropna=False)
#         .agg(
#             POS_attribues=("To_clean", "nunique"),
#             Nb_HVC=("Category", lambda s: int((s == "HVC").sum())),
#             Nb_MVC=("Category", lambda s: int((s == "MVC").sum())),
#             Nb_LVC=("Category", lambda s: int((s == "LVC").sum())),
#             Nb_TX_attrib=("tx_count", "sum") if "tx_count" in assigned.columns else ("To_clean", "size"),
#         )
#         .reset_index()
#     )

#     # POS servis = attribués touchés au moins 1x sur la période
#     if not tx_comm.empty and "To_clean" in tx_comm.columns:
#         pos_set = set(assigned["To_clean"].astype(str))
#         servis = (
#             tx_comm[tx_comm["To_clean"].astype(str).isin(pos_set)]
#             .groupby("Commercial_MSISDN")["To_clean"]
#             .nunique()
#             .rename("POS_servis")
#             .reset_index()
#         )
#         base = base.merge(servis, on="Commercial_MSISDN", how="left")
#     else:
#         base["POS_servis"] = 0
#     base["POS_servis"] = base["POS_servis"].fillna(0).astype(int)

#     # Couverture = POS_servis / POS_attribues * 100
#     base["Taux_couverture_pct"] = (
#         base["POS_servis"] / base["POS_attribues"].replace(0, pd.NA) * 100
#     ).round(1).fillna(0.0)

#     # Capilarité en %
#     base["Capilarite_pct"] = (base["POS_attribues"] / CAPILLARITE_CIBLE * 100).round(1)

#     # Montant descendu
#     if not tx_comm.empty and "Amount" in tx_comm.columns:
#         vol = (
#             tx_comm.groupby("Commercial_MSISDN")["Amount"]
#             .sum()
#             .rename("Montant_descendu")
#             .reset_index()
#         )
#         base = base.merge(vol, on="Commercial_MSISDN", how="left")
#     else:
#         base["Montant_descendu"] = 0.0
#     base["Montant_descendu"] = pd.to_numeric(base["Montant_descendu"], errors="coerce").fillna(0)

#     # Reçu / Remonté
#     try:
#         from models.conquete_model import get_commercial_cash_flows, _load_master_caisse_msisdns
#         masters, caisses = _load_master_caisse_msisdns()
#         flows = get_commercial_cash_flows(
#             commercial_msisdns=base["Commercial_MSISDN"].astype(str).tolist(),
#             master_msisdns=masters,
#             caisse_msisdns=caisses,
#             start_date=start_date,
#             end_date=end_date,
#         )
#         base = base.merge(flows, on="Commercial_MSISDN", how="left")
#     except Exception:
#         base["Montant_recu"] = 0.0
#         base["Montant_remonte"] = 0.0

#     base["Montant_recu"] = pd.to_numeric(base.get("Montant_recu", 0), errors="coerce").fillna(0)
#     base["Montant_remonte"] = pd.to_numeric(base.get("Montant_remonte", 0), errors="coerce").fillna(0)

#     # Horaires
#     hours = _avg_daily_hours(tx_comm)
#     if not hours.empty:
#         base = base.merge(hours, on="Commercial_MSISDN", how="left")
#     else:
#         base["Heure_debut_moy"] = None
#         base["Heure_fin_moy"] = None
#     base["Heure_debut_moy"] = base["Heure_debut_moy"].apply(_fmt_hour)
#     base["Heure_fin_moy"] = base["Heure_fin_moy"].apply(_fmt_hour)

#     base["Zone_SA"] = base["Commercial_MSISDN"].map(zone_map).fillna("N/A")

#     cols = [
#         "Commercial", "Commercial_MSISDN", "Zone_SA",
#         "POS_attribues", "POS_servis", "Capilarite_pct", "Taux_couverture_pct",
#         "Nb_HVC", "Nb_MVC", "Nb_LVC",
#         "Montant_descendu", "Montant_recu", "Montant_remonte",
#         "Heure_debut_moy", "Heure_fin_moy", "Nb_TX_attrib",
#     ]
#     return (
#         base[[c for c in cols if c in base.columns]]
#         .sort_values("POS_attribues", ascending=False)
#         .reset_index(drop=True)
#     )

def _segment_bucket(seg) -> str:
    s = str(seg).strip().upper()
    if "HVC" in s or s.startswith("1"):
        return "HVC"
    if "MVC" in s or s.startswith("2"):
        return "MVC"
    if "LVC" in s or s.startswith("3"):
        return "LVC"
    return "Autres"

def _attach_segment_from_ref(assigned: pd.DataFrame, ref_df: pd.DataFrame) -> pd.DataFrame:
    """Segment HVC/MVC/LVC depuis referentiel_pos (indépendant du filtre Segment de la page)."""
    out = assigned.copy()
    if ref_df is None or ref_df.empty or "To_clean" not in out.columns:
        out["Category"] = out.get("Category", "Autres")
        return out

    msisdn_col = next((c for c in ["agent_msisdn", "msisdn"] if c in ref_df.columns), None)
    seg_col = next((c for c in ["segment_group", "Segment Group", "Segment"] if c in ref_df.columns), None)
    if not msisdn_col or not seg_col:
        out["Category"] = "Autres"
        return out

    ref = ref_df[[msisdn_col, seg_col]].copy()
    ref[msisdn_col] = ref[msisdn_col].apply(clean_phone)
    ref = ref.rename(columns={msisdn_col: "To_clean", seg_col: "_seg_ref"}).drop_duplicates("To_clean")
    out["To_clean"] = out["To_clean"].apply(clean_phone)
    out = out.drop(columns=["_seg_ref"], errors="ignore")
    out = out.merge(ref, on="To_clean", how="left")
    out["Category"] = out["_seg_ref"].apply(_segment_bucket)
    out = out.drop(columns=["_seg_ref"], errors="ignore")
    return out

# def _build_portfolio_enriched(
#     assigned: pd.DataFrame,
#     tx_comm: pd.DataFrame,
#     ref_df: pd.DataFrame,
#     start_date: str | None = None,
#     end_date: str | None = None,
# ) -> pd.DataFrame:
#     if assigned.empty:
#         return pd.DataFrame()

#     assigned = _attach_segment_from_ref(assigned, ref_df)

#     # ---------- 1 ligne = 1 commercial ----------
#     base = (
#         assigned.groupby(["Commercial", "Commercial_MSISDN"], dropna=False)
#         .agg(
#             POS_attribues=("To_clean", "nunique"),
#             Nb_HVC=("Category", lambda s: int((s == "HVC").sum())),
#             Nb_MVC=("Category", lambda s: int((s == "MVC").sum())),
#             Nb_LVC=("Category", lambda s: int((s == "LVC").sum())),
#             Nb_TX_attrib=("tx_count", "sum") if "tx_count" in assigned.columns else ("To_clean", "size"),
#             MSISDN_affiche=("MSISDN_affiche", "first")
#             if "MSISDN_affiche" in assigned.columns
#             else ("Commercial_MSISDN", "first"),
#         )
#         .reset_index()
#     )

#     # Zone SA (mode sur les POS attribués via ref)
#     zone_map = {}
#     if ref_df is not None and not ref_df.empty:
#         msisdn_col = next((c for c in ["agent_msisdn", "msisdn"] if c in ref_df.columns), None)
#         sa_col = next((c for c in ["zone_sa", "sa_incharge"] if c in ref_df.columns), None)
#         if msisdn_col and sa_col:
#             ref_sa = ref_df[[msisdn_col, sa_col]].copy()
#             ref_sa[msisdn_col] = ref_sa[msisdn_col].astype(str).str.strip()
#             ref_sa = ref_sa.rename(columns={msisdn_col: "To_clean", sa_col: "zone_sa"})
#             tmp = assigned.merge(ref_sa, on="To_clean", how="left")
#             zone_map = (
#                 tmp.groupby("Commercial_MSISDN")["zone_sa"]
#                 .agg(lambda s: s.dropna().mode().iat[0] if not s.dropna().mode().empty else "N/A")
#                 .to_dict()
#             )
#     base["Zone_SA"] = base["Commercial_MSISDN"].map(zone_map).fillna("N/A")

#     # ---------- Match TX ----------
#     # Clés réelles (exclure NAME::)
#     real_msisdns = {
#         m for m in base["Commercial_MSISDN"].astype(str)
#         if m and not str(m).startswith("NAME::") and m != "N/A"
#     }
#     name_keys = {
#         str(m).replace("NAME::", "").strip().upper()
#         for m in base["Commercial_MSISDN"].astype(str)
#         if str(m).startswith("NAME::")
#     }

#     tx = tx_comm.copy() if tx_comm is not None else pd.DataFrame()
#     if not tx.empty:
#         if "Commercial_MSISDN" not in tx.columns and "From_clean" in tx.columns:
#             tx["Commercial_MSISDN"] = tx["From_clean"]
#         tx["_from"] = tx.get("Commercial_MSISDN", tx.get("From_clean", pd.Series(dtype=str))).astype(str)
#         tx["_name"] = tx.get("Commercial", pd.Series(dtype=str)).astype(str).str.strip().str.upper()

#         # POS servis
#         pos_by_comm = (
#             assigned.groupby("Commercial_MSISDN")["To_clean"]
#             .apply(lambda s: set(s.astype(str)))
#             .to_dict()
#         )

#         servis_rows = []
#         for key, pos_set in pos_by_comm.items():
#             if str(key).startswith("NAME::"):
#                 nm = str(key).replace("NAME::", "").upper()
#                 sub = tx[tx["_name"] == nm]
#             else:
#                 sub = tx[tx["_from"] == str(key)]
#             if sub.empty or "To_clean" not in sub.columns:
#                 n_servis = 0
#                 vol = 0.0
#             else:
#                 n_servis = sub[sub["To_clean"].astype(str).isin(pos_set)]["To_clean"].nunique()
#                 vol = float(pd.to_numeric(sub.get("Amount", 0), errors="coerce").fillna(0).sum())
#             servis_rows.append({
#                 "Commercial_MSISDN": key,
#                 "POS_servis": n_servis,
#                 "Montant_descendu": vol,
#             })
#         servis_df = pd.DataFrame(servis_rows)
#         base = base.merge(servis_df, on="Commercial_MSISDN", how="left")
#     else:
#         base["POS_servis"] = 0
#         base["Montant_descendu"] = 0.0

#     base["POS_servis"] = base["POS_servis"].fillna(0).astype(int)
#     base["Montant_descendu"] = pd.to_numeric(base["Montant_descendu"], errors="coerce").fillna(0)

#     base["Taux_couverture_pct"] = (
#         base["POS_servis"] / base["POS_attribues"].replace(0, pd.NA) * 100
#     ).round(1).fillna(0.0)
#     base["Capilarite_pct"] = (base["POS_attribues"] / CAPILLARITE_CIBLE * 100).round(1)

#     # Reçu / Remonté (uniquement MSISDN réels)
#     try:
#         from models.conquete_model import get_commercial_cash_flows, _load_master_caisse_msisdns
#         masters, caisses = _load_master_caisse_msisdns()
#         real_list = [m for m in base["Commercial_MSISDN"].astype(str) if not str(m).startswith("NAME::")]
#         if real_list:
#             flows = get_commercial_cash_flows(
#                 commercial_msisdns=real_list,
#                 master_msisdns=masters,
#                 caisse_msisdns=caisses,
#                 start_date=start_date,
#                 end_date=end_date,
#             )
#             base = base.merge(flows, on="Commercial_MSISDN", how="left")
#     except Exception:
#         pass
#     base["Montant_recu"] = pd.to_numeric(base.get("Montant_recu", 0), errors="coerce").fillna(0)
#     base["Montant_remonte"] = pd.to_numeric(base.get("Montant_remonte", 0), errors="coerce").fillna(0)

#     # Horaires
#     hours = _avg_daily_hours(tx) if not tx.empty else pd.DataFrame()
#     if not hours.empty:
#         base = base.merge(hours, on="Commercial_MSISDN", how="left")
#     else:
#         base["Heure_debut_moy"] = None
#         base["Heure_fin_moy"] = None
#     base["Heure_debut_moy"] = base["Heure_debut_moy"].apply(_fmt_hour)
#     base["Heure_fin_moy"] = base["Heure_fin_moy"].apply(_fmt_hour)

#     # Affichage MSISDN
#     if "MSISDN_affiche" not in base.columns:
#         base["MSISDN_affiche"] = base["Commercial_MSISDN"].apply(
#             lambda x: "N/A" if str(x).startswith("NAME::") else x
#         )

#     cols = [
#         "Commercial", "MSISDN_affiche", "Zone_SA",
#         "POS_attribues", "POS_servis", "Capilarite_pct", "Taux_couverture_pct",
#         "Nb_HVC", "Nb_MVC", "Nb_LVC",
#         "Montant_descendu", "Montant_recu", "Montant_remonte",
#         "Heure_debut_moy", "Heure_fin_moy", "Nb_TX_attrib",
#     ]
#     out = base[[c for c in cols if c in base.columns]].copy()
#     out = out.rename(columns={"MSISDN_affiche": "MSISDN"})
#     # GARDE-FOU anti-doublons
#     out = out.drop_duplicates(subset=["Commercial", "MSISDN"], keep="first")
#     return out.sort_values("POS_attribues", ascending=False).reset_index(drop=True)

def _build_portfolio_enriched(
    assigned: pd.DataFrame,
    tx_comm: pd.DataFrame,
    ref_df: pd.DataFrame,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Enrichit le portefeuille commercial avec le fichier source unique, les ventes et les POS servis hors portefeuille."""
    if assigned.empty:
        return pd.DataFrame()

    tx = tx_comm.copy() if tx_comm is not None else pd.DataFrame()

    # Nettoyage et harmonisation des MSISDN
    if not tx.empty:
        if "Commercial_MSISDN" in tx.columns:
            tx["_from_clean"] = tx["Commercial_MSISDN"].apply(clean_phone)
        elif "From_clean" in tx.columns:
            tx["_from_clean"] = tx["From_clean"].apply(clean_phone)
        elif "from_msisdn" in tx.columns:
            tx["_from_clean"] = tx["from_msisdn"].apply(clean_phone)
        else:
            tx["_from_clean"] = ""

        tx["_name"] = tx.get("Commercial", tx.get("from_name", pd.Series(dtype=str))).astype(str).str.strip().str.upper()

    assigned["_comm_msisdn_clean"] = assigned["Commercial_MSISDN"].apply(clean_phone)

    # Base 1 ligne par commercial
    base = (
        assigned.groupby(["Commercial", "Commercial_MSISDN", "_comm_msisdn_clean"], dropna=False)
        .agg(
            POS_attribues=("To_clean", "nunique"),
            Nb_TX_attrib=("tx_count", "sum") if "tx_count" in assigned.columns else ("To_clean", "size"),
            MSISDN_affiche=("MSISDN_affiche", "first")
            if "MSISDN_affiche" in assigned.columns
            else ("Commercial_MSISDN", "first"),
        )
        .reset_index()
    )

    pos_by_comm = (
        assigned.groupby("_comm_msisdn_clean")["To_clean"]
        .apply(lambda s: set(s.astype(str)))
        .to_dict()
    )

    servis_rows = []
    src_col = "source_file" if "source_file" in tx.columns else ("Source_file" if "Source_file" in tx.columns else None)
    amt_col = "amount" if "amount" in tx.columns else ("Amount" if "Amount" in tx.columns else None)

    for _, row in base.iterrows():
        key_clean = row["_comm_msisdn_clean"]
        raw_key = str(row["Commercial_MSISDN"])
        is_name_match = raw_key.startswith("NAME::")

        # Isolation de toutes les transactions du commercial
        if is_name_match:
            nm = raw_key.replace("NAME::", "").upper()
            sub = tx[tx["_name"] == nm] if not tx.empty else pd.DataFrame()
        else:
            sub = tx[tx["_from_clean"] == key_clean] if not tx.empty else pd.DataFrame()

        pos_set = pos_by_comm.get(key_clean, set())

        # Fichier Source Unique (Le plus fréquent / Mode)
        if src_col and not sub.empty:
            src_counts = sub[src_col].dropna().value_counts()
            src_file_principal = src_counts.index[0] if not src_counts.empty else "Base SQL"
        else:
            src_file_principal = "N/A (Aucune TX)"

        if sub.empty:
            n_servis = 0
            n_servis_hors = 0
            vol = 0.0
            vol_hors_portefeuille = 0.0
            hvc, mvc, lvc = 0, 0, 0
        else:
            # 1. Transactions sur les POS attribués (En Portefeuille)
            sub_servis = sub[sub["To_clean"].astype(str).isin(pos_set)] if pos_set else sub

            # 2. Transactions sur les POS non attribués (Hors Portefeuille)
            sub_hors = sub[~sub["To_clean"].astype(str).isin(pos_set)] if pos_set else pd.DataFrame()

            # Calculs Hors Portefeuille (Nombre de POS et Vol. Financier)
            if not sub_hors.empty:
                n_servis_hors = sub_hors["To_clean"].nunique()
                vol_hors_portefeuille = float(pd.to_numeric(sub_hors[amt_col], errors="coerce").fillna(0).sum()) if amt_col else 0.0
            else:
                n_servis_hors = 0
                vol_hors_portefeuille = 0.0

            # Calculs En Portefeuille
            if sub_servis.empty:
                n_servis = 0
                vol = 0.0
                hvc, mvc, lvc = 0, 0, 0
            else:
                n_servis = sub_servis["To_clean"].nunique()
                vol_val = sub_servis[amt_col] if amt_col else 0
                vol = float(pd.to_numeric(vol_val, errors="coerce").fillna(0).sum())

                if "Segment_PDV" in sub_servis.columns:
                    sub_servis["Category_TX"] = sub_servis["Segment_PDV"].apply(_segment_bucket)
                    pdv_seg = sub_servis.groupby("To_clean")["Category_TX"].agg(
                        lambda s: s.mode().iat[0] if not s.mode().empty else "LVC"
                    )
                    hvc = int((pdv_seg == "HVC").sum())
                    mvc = int((pdv_seg == "MVC").sum())
                    lvc = int((pdv_seg == "LVC").sum())
                else:
                    hvc, mvc, lvc = 0, 0, n_servis

        servis_rows.append({
            "_comm_msisdn_clean": key_clean,
            "POS_servis": n_servis,
            "POS_servis_hors_portefeuille": n_servis_hors,  # <--- AJOUT NOMBRE POS HORS PORTEFEUILLE
            "Montant_descendu": vol,
            "Montant_descendu_hors_portefeuille": vol_hors_portefeuille,
            "Nb_HVC": hvc,
            "Nb_MVC": mvc,
            "Nb_LVC": lvc,
            "Source_donnees": src_file_principal
        })

    servis_df = pd.DataFrame(servis_rows).drop_duplicates(subset=["_comm_msisdn_clean"])
    base = base.merge(servis_df, on="_comm_msisdn_clean", how="left")

    base["POS_servis"] = base["POS_servis"].fillna(0).astype(int)
    base["POS_servis_hors_portefeuille"] = base["POS_servis_hors_portefeuille"].fillna(0).astype(int)
    base["Montant_descendu"] = pd.to_numeric(base["Montant_descendu"], errors="coerce").fillna(0)
    base["Montant_descendu_hors_portefeuille"] = pd.to_numeric(base["Montant_descendu_hors_portefeuille"], errors="coerce").fillna(0)
    base["Nb_HVC"] = base["Nb_HVC"].fillna(0).astype(int)
    base["Nb_MVC"] = base["Nb_MVC"].fillna(0).astype(int)
    base["Nb_LVC"] = base["Nb_LVC"].fillna(0).astype(int)

    base["Taux_couverture_pct"] = (
        base["POS_servis"] / base["POS_attribues"].replace(0, pd.NA) * 100
    ).round(1).fillna(0.0)
    base["Capilarite_pct"] = (CAPILLARITE_CIBLE  / base["POS_attribues"] * 100).round(1)

    # Récupération Cash Flow
    try:
        from models.conquete_model import get_commercial_cash_flows, _load_master_caisse_msisdns
        masters, caisses = _load_master_caisse_msisdns()
        real_list = [m for m in base["Commercial_MSISDN"].astype(str) if not str(m).startswith("NAME::")]
        if real_list:
            flows = get_commercial_cash_flows(
                commercial_msisdns=real_list,
                master_msisdns=masters,
                caisse_msisdns=caisses,
                start_date=start_date,
                end_date=end_date,
            )
            base = base.merge(flows, on="Commercial_MSISDN", how="left")
    except Exception:
        pass

    base["Montant_recu"] = pd.to_numeric(base.get("Montant_recu", 0), errors="coerce").fillna(0)
    base["Montant_remonte"] = pd.to_numeric(base.get("Montant_remonte", 0), errors="coerce").fillna(0)

    # Horaires
    hours = _avg_daily_hours(tx) if not tx.empty else pd.DataFrame()
    if not hours.empty and "Commercial_MSISDN" in hours.columns:
        hours["_comm_msisdn_clean"] = hours["Commercial_MSISDN"].apply(clean_phone)
        base = base.merge(hours[["_comm_msisdn_clean", "Heure_debut_moy", "Heure_fin_moy"]], on="_comm_msisdn_clean", how="left")
    else:
        base["Heure_debut_moy"] = None
        base["Heure_fin_moy"] = None

    base["Heure_debut_moy"] = base["Heure_debut_moy"].apply(_fmt_hour)
    base["Heure_fin_moy"] = base["Heure_fin_moy"].apply(_fmt_hour)

    if "MSISDN_affiche" not in base.columns:
        base["MSISDN_affiche"] = base["Commercial_MSISDN"].apply(
            lambda x: "N/A" if str(x).startswith("NAME::") else x
        )

    # Ordre final des colonnes
    cols = [
        "Commercial", "MSISDN_affiche", "Zone_SA", "Source_donnees",
        "POS_attribues", "POS_servis",  
        "Capilarite_pct", "Taux_couverture_pct",
        "Nb_HVC", "Nb_MVC", "Nb_LVC",
        "Montant_descendu", "POS_servis_hors_portefeuille", "Montant_descendu_hors_portefeuille",
        "Montant_recu", "Montant_remonte",
        "Heure_debut_moy", "Heure_fin_moy", "Nb_TX_attrib",
    ]
    out = base[[c for c in cols if c in base.columns]].copy()
    out = out.rename(columns={"MSISDN_affiche": "MSISDN"})
    out = out.drop_duplicates(subset=["Commercial", "MSISDN"], keep="first")
    return out.sort_values("POS_attribues", ascending=False).reset_index(drop=True)

