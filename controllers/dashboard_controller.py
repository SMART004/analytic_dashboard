"""Dashboard controller.

The dashboard is intentionally compact: a few global KPIs, a commercial ranking
and a site/territory ranking. SQL filtering is delegated to models before KPI
calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from controllers.exclusion import get_excluded_msisdns
from controllers.kpis import (
    kpi_avg_rotation,
    kpi_cash_in,
    kpi_cash_out,
    kpi_coverage_rate,
    kpi_distributed_amount,
    kpi_oos_by_group,
    kpi_oos_rate,
    kpi_pos_not_touched,
    kpi_pos_touched,
    kpi_site_cluster_ranking,
    kpi_territory_summary,
    kpi_top_commerciaux,
    kpi_transfer_volume,
)
from models.oos_model import get_oos_listing
from models.pos_model import get_all_pos
from models.transactions_model import get_dashboard_filter_options, get_transactions


@dataclass(frozen=True)
class DashboardFilters:
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    centre: str = "Toutes"
    territoire: str = "Toutes"
    commercial: str = "Tous"
    segment: str = "Tous"


@dataclass(frozen=True)
class DashboardContext:
    filters: DashboardFilters
    kpis: dict[str, Any]
    top_commerciaux: pd.DataFrame
    territory_summary: pd.DataFrame
    site_cluster_ranking: pd.DataFrame
    oos_summary: pd.DataFrame
    export_styles: dict[str, Any]
    is_empty: bool = False
    message: str = ""


def load_filter_options() -> dict[str, Any]:
    """Return dashboard filter options for the view."""
    try:
        return get_dashboard_filter_options()
    except Exception as exc:
        return {
            "min_date": None,
            "max_date": None,
            "centres": [],
            "territoires": [],
            "commerciaux": [],
            "segments": [],
            "error": str(exc),
        }


def build_dashboard_context(filters: DashboardFilters) -> DashboardContext:
    """Build all dashboard-ready data for the selected filters."""
    try:
        excluded = get_excluded_msisdns()
        tx_df = get_transactions(
            start_date=filters.start_date,
            end_date=filters.end_date,
            zone_centre=filters.centre,
            territory=filters.territoire,
            commercial=filters.commercial,
            segment=filters.segment,
        )
        pos_df = get_all_pos(
            zone_centre=filters.centre,
            territory=filters.territoire if filters.territoire != "Toutes" else None,
            segment=filters.segment if filters.segment != "Tous" else None,
        )
        oos_df = _safe_oos_listing(filters)
    except Exception as exc:
        return _empty_context(filters, f"Impossible de charger les donnees SQLite du dashboard: {exc}")

    if tx_df.empty:
        return _empty_context(filters, "Aucune transaction trouvee pour les filtres selectionnes.")

    tx_df = tx_df[tx_df["Type"].str.lower().str.replace("_", " ", regex=False).isin({"transfer"})].copy() if "Type" in tx_df.columns else tx_df.copy()
    total_pos = _distinct_pos_count(pos_df)
    touched_pos = kpi_pos_touched(tx_df, excluded=excluded)
    coverage_rate = kpi_coverage_rate(touched_pos, total_pos)

    tx_df = _normalize_dashboard_territories(tx_df)
    total_pos_by_territory = _total_pos_by_territory(pos_df)
    territory_summary = kpi_territory_summary(
        tx_df,
        total_pos_by_territory=total_pos_by_territory,
        territory_col="Zone_Territoire",
    )
    if not territory_summary.empty:
        territory_summary = territory_summary.rename(columns={"Zone": "Territoire"})
        if not oos_df.empty and "territory" in oos_df.columns:
            oos_df_work = oos_df.copy()
            oos_df_work["Territoire"] = oos_df_work["territory"].astype(str).str.strip().map(_display_territory)
            oos_terr = (
                oos_df_work.groupby("Territoire")["msisdn"]
                .nunique()
                .reset_index(name="Nb_OOS")
            )
            territory_summary = territory_summary.merge(oos_terr, on="Territoire", how="left")
            territory_summary["Nb_OOS"] = territory_summary["Nb_OOS"].fillna(0).astype(int)
            territory_summary["Taux OOS (%)"] = territory_summary.apply(
                lambda row: round(row["Nb_OOS"] / row["Total_POS_Territoire"] * 100, 1)
                if row["Total_POS_Territoire"] > 0 else 0.0, axis=1
            )
        else:
            territory_summary["Taux OOS (%)"] = 0.0

    ranking_base = _build_cluster_coverage_summary(tx_df, pos_df, oos_df=oos_df)
    site_cluster_ranking = kpi_site_cluster_ranking(
        ranking_base,
        oos_df=oos_df,
        group_col="Cluster",
        top_n=10,
        ascending=False,
    )
    oos_summary = ranking_base[["Cluster", "Total_POS_Territoire", "Nb_OOS", "Taux OOS (%)"]].rename(columns={"Cluster": "Groupe"}) if (not ranking_base.empty and "Nb_OOS" in ranking_base.columns) else pd.DataFrame()

    transfer_volume = kpi_transfer_volume(tx_df)
    nb_oos_global = oos_df["msisdn"].nunique() if (not oos_df.empty and "msisdn" in oos_df.columns) else len(oos_df)
    kpis = {
        "distributed_amount": transfer_volume,
        "cash_in": 0.0,
        "cash_out": 0.0,
        "transfer_volume": transfer_volume,
        "touched_pos": touched_pos,
        "total_pos": total_pos,
        "not_touched_pos": kpi_pos_not_touched(total_pos, touched_pos),
        "coverage_rate": coverage_rate,
        "avg_rotation": kpi_avg_rotation(tx_df),
        "oos_rate": round(nb_oos_global / total_pos * 100, 1) if (not oos_df.empty and total_pos > 0) else 0.0,
    }

    return DashboardContext(
        filters=filters,
        kpis=kpis,
        top_commerciaux=_build_non_touched_by_cluster(pos_df, tx_df),
        territory_summary=territory_summary,
        site_cluster_ranking=site_cluster_ranking,
        oos_summary=oos_summary,
        export_styles=_dashboard_export_styles(),
    )


def _safe_oos_listing(filters: DashboardFilters) -> pd.DataFrame:
    """Charge le dernier snapshot OOS en appliquant tous les filtres actifs."""
    try:
        oos_df = get_oos_listing(
            zone=filters.centre if filters.centre not in ("Toutes", "Tous", "") else None,
            territory=filters.territoire if filters.territoire not in ("Toutes", "Tous", "") else None,
            segment_group=filters.segment if filters.segment not in ("Tous", "Toutes", "") else None,
        )
    except Exception:
        return pd.DataFrame()
    if oos_df.empty:
        return oos_df
    if "snapshot_date" in oos_df.columns:
        latest = oos_df["snapshot_date"].dropna().max()
        if pd.notna(latest):
            oos_df = oos_df[oos_df["snapshot_date"] == latest].copy()
    return oos_df


def _distinct_pos_count(pos_df: pd.DataFrame) -> int:
    if pos_df.empty or "agent_msisdn" not in pos_df.columns:
        return 0
    return int(pos_df["agent_msisdn"].dropna().nunique())


def _total_pos_by_territory(pos_df: pd.DataFrame) -> dict[str, int]:
    if pos_df.empty or "territory" not in pos_df.columns or "agent_msisdn" not in pos_df.columns:
        return {}
    _INVALID = {"non renseigne", "non renseigné", "none", "n/a", "", "null", "nan"}
    work = pos_df.copy()
    work["territory"] = work["territory"].astype(str).str.strip().map(_display_territory)
    work = work[~work["territory"].str.lower().isin(_INVALID)]
    return work.groupby("territory")["agent_msisdn"].nunique().to_dict()


def _normalize_dashboard_territories(tx_df: pd.DataFrame) -> pd.DataFrame:
    if tx_df.empty or "Zone_Territoire" not in tx_df.columns:
        return tx_df
    _INVALID = {"non renseigne", "non renseigné", "none", "n/a", "", "null", "nan"}
    work = tx_df.copy()
    work["Zone_Territoire"] = work["Zone_Territoire"].astype(str).str.strip().map(_display_territory)
    # Remplacer les libellés invalides par NaN (seront exclus du groupby)
    work["Zone_Territoire"] = work["Zone_Territoire"].where(
        ~work["Zone_Territoire"].str.lower().isin(_INVALID), other=pd.NA
    )
    return work


def _build_cluster_coverage_summary(
    tx_df: pd.DataFrame,
    pos_df: pd.DataFrame,
    oos_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if tx_df.empty or pos_df.empty or "agent_msisdn" not in pos_df.columns:
        return pd.DataFrame()

    pos = pos_df.copy()
    pos["Cluster"] = _cluster_label(pos)  # NaN pour les invalides
    # Ne garder que les POS avec un cluster valide
    pos_valid = pos.dropna(subset=["Cluster"])
    totals = pos_valid.groupby("Cluster")["agent_msisdn"].nunique().rename("Total_POS_Territoire")

    work = tx_df.copy()
    if "Cluster" not in work.columns or work["Cluster"].isna().all():
        work = work.merge(
            pos_valid[["agent_msisdn", "Cluster"]].dropna(),
            left_on="To_clean",
            right_on="agent_msisdn",
            how="left",
        )
    # Ne pas remplacer NaN par "NON RENSEIGNE" : on exclut les lignes sans cluster valide
    work["Type_norm"] = work.get("Type", pd.Series(index=work.index, dtype="object")).astype("string").str.strip().str.lower().str.replace("_", " ", regex=False)

    dist = work[work["Type_norm"].isin({"transfer"})].dropna(subset=["Cluster"]).copy()
    if dist.empty:
        return pd.DataFrame()

    summary = (
        dist.groupby("Cluster", dropna=True)
        .agg(
            Montant_Distribue=("Amount_num" if "Amount_num" in dist.columns else "Amount", "sum"),
            POS_Servis_Uniques=("To_clean", "nunique"),
        )
        .reset_index()
    )
    summary["Total_POS_Territoire"] = summary["Cluster"].map(totals).fillna(0).astype(int)
    summary["Taux de couverture"] = summary.apply(
        lambda row: kpi_coverage_rate(row["POS_Servis_Uniques"], row["Total_POS_Territoire"]),
        axis=1,
    )

    if oos_df is not None and not oos_df.empty:
        oos_df_work = oos_df.copy()
        cluster_col_oos = "cluster" if "cluster" in oos_df_work.columns else ("Cluster" if "Cluster" in oos_df_work.columns else None)
        if cluster_col_oos:
            oos_df_work["Cluster"] = oos_df_work[cluster_col_oos].astype(str).str.strip()
            oos_counts = oos_df_work.groupby("Cluster")["msisdn"].nunique().rename("Nb_OOS")
            summary["Nb_OOS"] = summary["Cluster"].map(oos_counts).fillna(0).astype(int)
            summary["Taux OOS (%)"] = summary.apply(
                lambda row: round(row["Nb_OOS"] / row["Total_POS_Territoire"] * 100, 1)
                if row["Total_POS_Territoire"] > 0 else 0.0,
                axis=1,
            )
        else:
            summary["Nb_OOS"] = 0
            summary["Taux OOS (%)"] = 0.0
    else:
        summary["Nb_OOS"] = 0
        summary["Taux OOS (%)"] = 0.0

    return summary


def _build_non_touched_by_cluster(pos_df: pd.DataFrame, tx_df: pd.DataFrame) -> pd.DataFrame:
    if pos_df.empty:
        return pd.DataFrame(columns=["Cluster", "POS_Non_Touches"])
    pos = pos_df.copy()
    pos["Cluster"] = _cluster_label(pos)
    # Ne garder que les POS avec un cluster valide
    pos = pos.dropna(subset=["Cluster"])
    total_by_cluster = pos.groupby("Cluster")["agent_msisdn"].nunique().rename("Total_POS").reset_index()
    if tx_df.empty:
        return total_by_cluster.rename(columns={"Cluster": "Cluster", "Total_POS": "POS_Non_Touches"})

    touched = tx_df.copy()
    touched["Type_norm"] = touched.get("Type", pd.Series(index=touched.index, dtype="object")).astype("string").str.strip().str.lower().str.replace("_", " ", regex=False)
    touched = touched[touched["Type_norm"].isin({"transfer"})].copy()
    if "To_clean" in touched.columns:
        served = touched.groupby("To_clean").size().index.tolist()
    else:
        served = []
    touched_by_cluster = pos[pos["agent_msisdn"].isin(served)].groupby("Cluster")["agent_msisdn"].nunique().rename("POS_Touches").reset_index()
    summary = total_by_cluster.merge(touched_by_cluster, on="Cluster", how="left")
    summary["POS_Touches"] = summary["POS_Touches"].fillna(0).astype(int)
    summary["POS_Non_Touches"] = (summary["Total_POS"] - summary["POS_Touches"]).clip(lower=0)
    return summary[["Cluster", "POS_Non_Touches"]].sort_values("POS_Non_Touches", ascending=False)


def _cluster_label(pos_df: pd.DataFrame) -> pd.Series:
    _INVALID = {"non renseigne", "non renseigné", "none", "n/a", "", "null", "nan"}
    for col in ["quartier", "site_key", "territory"]:
        if col in pos_df.columns:
            values = pos_df[col].fillna("").astype(str).str.strip()
            # Filtrer les valeurs invalides — ne pas créer un groupe "NON RENSEIGNE"
            valid_mask = ~values.str.lower().isin(_INVALID) & values.ne("")
            if valid_mask.any():
                # Remplacer les invalides par NaN (seront exclus lors du groupby)
                return values.where(valid_mask, other=pd.NA)
    return pd.Series(pd.NA, index=pos_df.index, dtype="object")


def _display_territory(value: object) -> str:
    text = str(value).strip()
    if not text:
        return "NON RENSEIGNE"
    upper = text.upper()
    if upper.startswith("YAOUNDE "):
        return text[8:].strip() or text
    return text


def _dashboard_export_styles() -> dict[str, Any]:
    return {
        "column_styles": {
            "Montant Distribue": {"align": "center"},
            "Montant_Distribue": {"align": "center"},
            "Couverture (%)": {"align": "center"},
            "Taux OOS (%)": {"align": "center"},
        },
        "formats": {
            "Montant Distribue": '#,##0',
            "Montant_Distribue": '#,##0',
            "Couverture (%)": '0.0',
            "Taux OOS (%)": '0.0',
        },
        "rules": [
            {"columns": ["Couverture (%)", "Taux de couverture"], "op": ">=", "value": 80, "style": {"fill": "#C6EFCE", "font_color": "#006100"}},
            {"columns": ["Couverture (%)", "Taux de couverture"], "op": "<", "value": 60, "style": {"fill": "#FFC7CE", "font_color": "#9C0006"}},
            {"columns": ["Taux OOS (%)"], "op": ">=", "value": 40, "style": {"fill": "#FFC7CE", "font_color": "#9C0006"}},
        ],
    }


def _empty_context(filters: DashboardFilters, message: str) -> DashboardContext:
    return DashboardContext(
        filters=filters,
        kpis={},
        top_commerciaux=pd.DataFrame(),
        territory_summary=pd.DataFrame(),
        site_cluster_ranking=pd.DataFrame(),
        oos_summary=pd.DataFrame(),
        export_styles=_dashboard_export_styles(),
        is_empty=True,
        message=message,
    )
