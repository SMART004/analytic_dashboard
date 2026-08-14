"""Pure KPI functions used by controllers.

These functions intentionally know nothing about Streamlit or SQL. Models fetch
and pre-filter data; controllers pass DataFrames or scalar inputs here.
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from app_config.settings import (
    AFTERNOON_TREND_END_HOUR,
    AFTERNOON_TREND_START_HOUR,
    EXCELLENT_PERF_THRESHOLD,
    MIN_TRANSFER_AMOUNT,
    VERY_GOOD_PERF_THRESHOLD,
)

TRANSFER_TYPES = frozenset({"transfer"})
CASH_IN_TYPES = frozenset({"cash in", "cashin"})
CASH_OUT_TYPES = frozenset({"cash out", "cashout"})
DISTRIBUTION_TYPES = TRANSFER_TYPES | CASH_OUT_TYPES


def _empty_series(df: pd.DataFrame, dtype: object = object) -> pd.Series:
    return pd.Series(index=df.index, dtype=dtype)


def _first_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _text_series(df: pd.DataFrame, candidates: Sequence[str]) -> pd.Series:
    col = _first_col(df, candidates)
    if col is None:
        return _empty_series(df, object)
    return df[col].astype("string").str.strip()


def _normalized_type_series(df: pd.DataFrame) -> pd.Series:
    tx_type = _text_series(df, ["Type", "tx_type", "type", "Transaction_Type"])
    return tx_type.str.lower().str.replace("_", " ", regex=False)


def _amount_series(df: pd.DataFrame) -> pd.Series:
    col = _first_col(df, ["Amount_num", "Amount", "amount", "Montant"])
    if col is None:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").abs().fillna(0.0)


def _to_msisdn_series(df: pd.DataFrame) -> pd.Series:
    return _text_series(df, ["To_clean", "to_msisdn", "To", "phone_to", "PDV", "msisdn"])


def _commercial_series(df: pd.DataFrame) -> pd.Series:
    return _text_series(df, ["Nom_Ccial", "Commercial", "nom_ccial", "Nom commercial"])


def _date_series(df: pd.DataFrame) -> pd.Series:
    col = _first_col(df, ["Date", "tx_date", "date", "Date_only", "snapshot_date"])
    if col is None:
        return pd.Series(pd.NaT, index=df.index)
    return pd.to_datetime(df[col], errors="coerce")


def _hour_series(df: pd.DataFrame) -> pd.Series:
    col = _first_col(df, ["Hour", "hour"])
    if col is not None:
        return pd.to_numeric(df[col], errors="coerce")
    dates = _date_series(df)
    return dates.dt.hour


def _cluster_series(df: pd.DataFrame) -> pd.Series:
    return _text_series(df, ["cluster", "Cluster", "secteur_cluster", "Secteur", "site_key", "Site"])


def _site_series(df: pd.DataFrame) -> pd.Series:
    return _text_series(df, ["site_key", "Site", "Sitename_PDV", "sitename"])


def _is_oos_series(df: pd.DataFrame) -> pd.Series:
    col = _first_col(df, ["is_oos", "Is_OOS", "OOS"])
    if col is not None:
        return pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int).astype(bool)
    pct_col = _first_col(df, ["oos_pct", "OOS_PCT", "Taux OOS (%)"])
    if pct_col is not None:
        return pd.to_numeric(df[pct_col], errors="coerce").fillna(0) > 0
    return pd.Series(False, index=df.index)


def _served_transfer_mask(
    df: pd.DataFrame,
    excluded: Iterable[str] = frozenset(),
    min_amount: float = MIN_TRANSFER_AMOUNT,
) -> pd.Series:
    """Mask for POS-served Transfer rows."""
    excluded_set = frozenset(str(value) for value in excluded if value)
    to_msisdn = _to_msisdn_series(df)
    return (
        _normalized_type_series(df).isin(TRANSFER_TYPES)
        & (_amount_series(df) >= min_amount)
        & to_msisdn.notna()
        & (to_msisdn != "")
        & ~to_msisdn.isin(excluded_set)
    )


def served_pos_set(
    df: pd.DataFrame,
    excluded: Iterable[str] = frozenset(),
    ref_pos_set: Optional[Iterable[str]] = None,
    min_amount: float = MIN_TRANSFER_AMOUNT,
) -> FrozenSet[str]:
    """Return distinct POS served by valid Transfer rows."""
    if df.empty:
        return frozenset()
    to_msisdn = _to_msisdn_series(df)
    mask = _served_transfer_mask(df, excluded=excluded, min_amount=min_amount)
    if ref_pos_set is not None:
        ref = frozenset(str(value) for value in ref_pos_set if value)
        mask &= to_msisdn.isin(ref)
    return frozenset(to_msisdn[mask].dropna().astype(str))


def kpi_coverage_rate(touched: int, total: int) -> float:
    """Network coverage rate: served POS divided by total POS."""
    return round((touched / total) * 100, 1) if total > 0 else 0.0


def kpi_pos_touched(
    df: pd.DataFrame,
    ref_pos_set: Optional[Iterable[str]] = None,
    excluded: Iterable[str] = frozenset(),
    min_amount: float = MIN_TRANSFER_AMOUNT,
) -> int:
    """Count distinct served POS after the canonical Transfer/exclusion filter."""
    return len(served_pos_set(df, excluded=excluded, ref_pos_set=ref_pos_set, min_amount=min_amount))


def kpi_distinct_pos_touched(
    df: pd.DataFrame,
    ref_pos_set: Optional[Iterable[str]] = None,
) -> int:
    """Count distinct POS in the recipient column, optionally restricted to a reference universe."""
    if df.empty:
        return 0
    values = _to_msisdn_series(df).dropna().astype(str)
    values = values[values != ""]
    if ref_pos_set is not None:
        ref = frozenset(str(value) for value in ref_pos_set if value)
        values = values[values.isin(ref)]
    return int(values.nunique())


def kpi_pos_not_touched(total_pos: int, touched: int) -> int:
    """Count POS not touched in the selected universe."""
    return max(int(total_pos) - int(touched), 0)


def kpi_new_pos(current_touched: Iterable[str], previous_touched: Iterable[str]) -> int:
    """New POS: present in current period and absent from previous period."""
    return len(frozenset(current_touched) - frozenset(previous_touched))


def kpi_lost_pos(current_touched: Iterable[str], previous_touched: Iterable[str]) -> int:
    """Lost POS: present in previous period and absent from current period."""
    return len(frozenset(previous_touched) - frozenset(current_touched))


def kpi_avg_pos_per_day(
    df: pd.DataFrame,
    touched_set: Optional[Iterable[str]] = None,
) -> float:
    """Average daily number of distinct served POS."""
    if df.empty:
        return 0.0
    dates = _date_series(df)
    to_msisdn = _to_msisdn_series(df)
    work = pd.DataFrame({"date": dates.dt.date, "pos": to_msisdn}).dropna()
    if touched_set is not None:
        work = work[work["pos"].isin(frozenset(touched_set))]
    if work.empty:
        return 0.0
    return round(float(work.groupby("date")["pos"].nunique().mean()), 1)


def kpi_distributed_amount(df: pd.DataFrame) -> float:
    """Distributed amount: Transfer plus Cash out volume."""
    if df.empty:
        return 0.0
    return float(_amount_series(df)[_normalized_type_series(df).isin(DISTRIBUTION_TYPES)].sum())


def kpi_cash_in(df: pd.DataFrame) -> float:
    """Total Cash In volume."""
    if df.empty:
        return 0.0
    return float(_amount_series(df)[_normalized_type_series(df).isin(CASH_IN_TYPES)].sum())


def kpi_cash_out(df: pd.DataFrame) -> float:
    """Total Cash Out volume."""
    if df.empty:
        return 0.0
    return float(_amount_series(df)[_normalized_type_series(df).isin(CASH_OUT_TYPES)].sum())


def kpi_transfer_volume(df: pd.DataFrame) -> float:
    """Total Transfer volume."""
    if df.empty:
        return 0.0
    return float(_amount_series(df)[_normalized_type_series(df).isin(TRANSFER_TYPES)].sum())


def kpi_avg_rotation(df: pd.DataFrame) -> Optional[float]:
    """Average commercial rotation: transactions / (served POS * days * 3)."""
    if df.empty:
        return None
    commercial = _commercial_series(df)
    if commercial.dropna().empty:
        return None
    work = pd.DataFrame(
        {
            "commercial": commercial.fillna("NON RENSEIGNE"),
            "date": _date_series(df).dt.date,
            "pos": _to_msisdn_series(df),
            "tx": 1,
        }
    ).dropna(subset=["date"])
    if work.empty:
        return None
    grouped = work.groupby("commercial", dropna=False).agg(
        nb_transactions=("tx", "sum"),
        pos_served=("pos", "nunique"),
        nb_days=("date", "nunique"),
    )
    denominator = grouped["pos_served"].replace(0, np.nan) * grouped["nb_days"].replace(0, np.nan) * 3
    rates = (grouped["nb_transactions"] / denominator) * 100
    mean_rate = rates.mean()
    return round(float(mean_rate), 2) if pd.notna(mean_rate) else None


def kpi_encroachment_rate(df_encroachment: pd.DataFrame, df_all_transactions: pd.DataFrame) -> float:
    """Encroachment rate: out-of-portfolio POS divided by total touched POS."""
    rates = kpi_encroachment_rates_by_type(df_encroachment, df_all_transactions)
    return round(rates.get("Intra-centre", 0.0) + rates.get("Inter-centre", 0.0), 1)


def kpi_encroachment_rates_by_type(
    df_encroachment: pd.DataFrame,
    df_all_transactions: Optional[pd.DataFrame] = None,
) -> dict[str, float]:
    """Return separate rates for intra-centre and inter-centre encroachment."""
    if df_encroachment is None or df_encroachment.empty:
        return {"Intra-centre": 0.0, "Inter-centre": 0.0}

    total_touched = 0
    if df_all_transactions is not None and not df_all_transactions.empty:
        total_touched = kpi_distinct_pos_touched(df_all_transactions)

    pdv_col = _first_col(df_encroachment, ["PDV", "To_clean", "to_msisdn"])
    type_col = _first_col(df_encroachment, ["Type_Empietement", "Type_Empietement", "Type_empietement"])
    rates = {"Intra-centre": 0.0, "Inter-centre": 0.0}
    if pdv_col is None:
        return rates

    if total_touched <= 0:
        total_touched = int(df_encroachment[pdv_col].nunique()) if pdv_col in df_encroachment.columns else 0

    for kind in rates:
        subset = df_encroachment[df_encroachment.get(type_col, pd.Series(index=df_encroachment.index, dtype="object")) == kind]
        unique_pdv = int(subset[pdv_col].nunique()) if pdv_col in subset.columns else 0
        if total_touched > 0:
            rates[kind] = round((unique_pdv / total_touched) * 100, 1)
    return rates


def kpi_perf_rating(amount: float) -> str:
    """Performance label according to configured thresholds."""
    if amount >= EXCELLENT_PERF_THRESHOLD:
        return "Excellent"
    if amount >= VERY_GOOD_PERF_THRESHOLD:
        return "Tres bon"
    return "Bon"


def kpi_top_commerciaux(
    df: pd.DataFrame,
    excluded: Iterable[str] = frozenset(),
    top_n: int = 5,
    min_amount: float = MIN_TRANSFER_AMOUNT,
) -> pd.DataFrame:
    """Top commerciaux by valid distributed Transfer amount."""
    if df.empty:
        return pd.DataFrame(columns=["Commercial", "Montant Distribue", "Performance"])
    mask = _served_transfer_mask(df, excluded=excluded, min_amount=min_amount)
    base = pd.DataFrame(
        {
            "Commercial": _commercial_series(df).fillna("NON RENSEIGNE"),
            "amount": _amount_series(df),
        }
    )[mask]
    if base.empty:
        return pd.DataFrame(columns=["Commercial", "Montant Distribue", "Performance"])
    result = (
        base.groupby("Commercial", dropna=False)["amount"]
        .sum()
        .reset_index(name="Montant Distribue")
        .sort_values("Montant Distribue", ascending=False)
        .head(top_n)
    )
    result["Performance"] = result["Montant Distribue"].apply(kpi_perf_rating)
    return result[["Commercial", "Montant Distribue", "Performance"]]


def kpi_territory_summary(
    df: pd.DataFrame,
    total_pos_by_territory: Mapping[str, int],
    territory_col: str = "Zone_Territoire",
) -> pd.DataFrame:
    """Summarize distribution, served POS and coverage by territory."""
    if df.empty:
        return pd.DataFrame()
    territory_used = territory_col if territory_col in df.columns else _first_col(df, ["Zone_SA", "territory", "zone"])
    if territory_used is None:
        return pd.DataFrame()
    work = pd.DataFrame(
        {
            "Zone": df[territory_used].astype("string").fillna("NON RENSEIGNE"),
            "type": _normalized_type_series(df),
            "amount": _amount_series(df),
            "pos": _to_msisdn_series(df),
        }
    )
    dist = work[work["type"].isin(DISTRIBUTION_TYPES)]
    summary = (
        dist.groupby("Zone", dropna=False)
        .agg(Montant_Distribue=("amount", "sum"), POS_Servis_Uniques=("pos", "nunique"))
        .reset_index()
    )
    summary["Total_POS_Territoire"] = summary["Zone"].map(total_pos_by_territory).fillna(0).astype(int)
    summary["Taux de couverture"] = summary.apply(
        lambda row: kpi_coverage_rate(row["POS_Servis_Uniques"], row["Total_POS_Territoire"]),
        axis=1,
    )
    total_dist = summary["Montant_Distribue"].sum()
    total_pos = summary["POS_Servis_Uniques"].sum()
    summary["Distribution_pct"] = summary["Montant_Distribue"] / total_dist if total_dist > 0 else 0.0
    summary["POS_pct"] = summary["POS_Servis_Uniques"] / total_pos if total_pos > 0 else 0.0
    summary["Ecart"] = summary["Distribution_pct"] - summary["POS_pct"]
    return summary.sort_values("Ecart", ascending=False).reset_index(drop=True)


def kpi_oos_rate(df_oos: pd.DataFrame) -> float:
    """Current OOS rate: OOS POS divided by total POS in the snapshot."""
    if df_oos.empty:
        return 0.0
    total = len(df_oos)
    return round((int(_is_oos_series(df_oos).sum()) / total) * 100, 1) if total > 0 else 0.0


def kpi_oos_by_cluster(df_oos: pd.DataFrame) -> pd.DataFrame:
    """OOS summary by cluster/site."""
    if df_oos.empty:
        return pd.DataFrame(columns=["Cluster", "Total", "En_OOS", "Taux OOS (%)"])
    cluster = _cluster_series(df_oos).fillna("NON RENSEIGNE")
    work = pd.DataFrame({"Cluster": cluster, "is_oos": _is_oos_series(df_oos)})
    summary = (
        work.groupby("Cluster", dropna=False)
        .agg(Total=("is_oos", "count"), En_OOS=("is_oos", "sum"))
        .reset_index()
    )
    summary["Taux OOS (%)"] = (summary["En_OOS"] / summary["Total"] * 100).round(1)
    return summary.sort_values("Taux OOS (%)", ascending=False).reset_index(drop=True)


def kpi_oos_variation(
    current_oos: pd.DataFrame,
    previous_oos: pd.DataFrame,
    group_col: str = "cluster",
) -> pd.DataFrame:
    """OOS rate variation between two snapshots, grouped by cluster/site."""
    current = kpi_oos_by_group(current_oos, group_col=group_col).rename(columns={"Taux OOS (%)": "OOS courant (%)"})
    previous = kpi_oos_by_group(previous_oos, group_col=group_col).rename(columns={"Taux OOS (%)": "OOS precedent (%)"})
    if current.empty and previous.empty:
        return pd.DataFrame()
    result = current.merge(previous, on="Groupe", how="outer").fillna(0)
    result["Variation OOS (pts)"] = (result["OOS courant (%)"] - result["OOS precedent (%)"]).round(1)
    return result.sort_values("Variation OOS (pts)", ascending=False).reset_index(drop=True)


def kpi_oos_by_group(df_oos: pd.DataFrame, group_col: str = "cluster") -> pd.DataFrame:
    """OOS summary by a requested column — libellés invalides exclus."""
    if df_oos.empty:
        return pd.DataFrame(columns=["Groupe", "Total", "En_OOS", "Taux OOS (%)"])
    _INVALID = {"non renseigne", "non renseigné", "none", "n/a", "", "null", "nan"}
    if group_col in df_oos.columns:
        group = df_oos[group_col].astype("string").fillna("")
    elif group_col == "site":
        group = _site_series(df_oos).fillna("")
    else:
        group = _cluster_series(df_oos).fillna("")
    work = pd.DataFrame({"Groupe": group, "is_oos": _is_oos_series(df_oos)})
    # Exclure les groupes invalides
    work = work[~work["Groupe"].str.strip().str.lower().isin(_INVALID)]
    if work.empty:
        return pd.DataFrame(columns=["Groupe", "Total", "En_OOS", "Taux OOS (%)"])
    summary = (
        work.groupby("Groupe", dropna=True)
        .agg(Total=("is_oos", "count"), En_OOS=("is_oos", "sum"))
        .reset_index()
    )
    summary["Taux OOS (%)"] = (summary["En_OOS"] / summary["Total"] * 100).round(1)
    return summary.sort_values("Taux OOS (%)", ascending=False).reset_index(drop=True)


def kpi_day_hvc(df_hvc: pd.DataFrame) -> pd.DataFrame:
    """Latest Day HVC values by site."""
    if df_hvc.empty or "day_hvc" not in df_hvc.columns:
        return pd.DataFrame(columns=["site_key", "day_hvc", "oos_pct"])
    work = df_hvc.copy()
    if "snapshot_timestamp" in work.columns:
        work["snapshot_timestamp"] = pd.to_datetime(work["snapshot_timestamp"], errors="coerce")
        work = work.sort_values("snapshot_timestamp")
    site_col = _first_col(work, ["site_key", "Site", "sitename"])
    subset = [site_col] if site_col else None
    if subset:
        work = work.drop_duplicates(subset=subset, keep="last")
    columns = [col for col in [site_col, "day_hvc", "oos_pct"] if col in work.columns]
    result = work[columns].reset_index(drop=True)
    if site_col and site_col != "site_key":
        result = result.rename(columns={site_col: "site_key"})
    return result


def kpi_site_cluster_ranking(
    coverage_df: pd.DataFrame,
    oos_df: Optional[pd.DataFrame] = None,
    group_col: str = "Cluster",
    top_n: int = 5,
    ascending: bool = False,
) -> pd.DataFrame:
    """Rank sites/clusters using coverage first and OOS as a penalty."""
    if coverage_df.empty:
        return pd.DataFrame()
    group_used = group_col if group_col in coverage_df.columns else _first_col(coverage_df, ["Cluster", "Zone", "site_key", "Site"])
    if group_used is None:
        return pd.DataFrame()
    coverage_col = _first_col(coverage_df, ["Taux de couverture", "coverage_rate", "Coverage (%)"])
    touched_col = _first_col(coverage_df, ["POS_Servis_Uniques", "served_pos", "POS servis"])
    total_col = _first_col(coverage_df, ["Total_POS_Territoire", "total_pos", "Total"])
    work = pd.DataFrame({"Groupe": coverage_df[group_used].astype("string").fillna("NON RENSEIGNE")})
    if coverage_col:
        work["Couverture (%)"] = pd.to_numeric(coverage_df[coverage_col], errors="coerce").fillna(0)
    elif touched_col and total_col:
        touched = pd.to_numeric(coverage_df[touched_col], errors="coerce").fillna(0)
        total = pd.to_numeric(coverage_df[total_col], errors="coerce").fillna(0)
        work["Couverture (%)"] = np.where(total > 0, (touched / total) * 100, 0).round(1)
    else:
        work["Couverture (%)"] = 0.0
    if "Taux OOS (%)" in coverage_df.columns:
        work["Taux OOS (%)"] = pd.to_numeric(coverage_df["Taux OOS (%)"], errors="coerce").fillna(0.0)
    elif oos_df is not None and not oos_df.empty:
        oos_summary = kpi_oos_by_group(oos_df, group_col=group_col).rename(columns={"Groupe": "Groupe"})
        work = work.merge(oos_summary[["Groupe", "Taux OOS (%)"]], on="Groupe", how="left")
    else:
        work["Taux OOS (%)"] = 0.0
    work["Taux OOS (%)"] = work["Taux OOS (%)"].fillna(0.0)
    work["Score"] = (work["Couverture (%)"] - work["Taux OOS (%)"]).round(1)
    return work.sort_values("Score", ascending=ascending).head(top_n).reset_index(drop=True)


def kpi_afternoon_trend(
    df: pd.DataFrame,
    excluded: Iterable[str] = frozenset(),
    hour_start: int = AFTERNOON_TREND_START_HOUR,
    hour_end: int = AFTERNOON_TREND_END_HOUR,
) -> pd.DataFrame:
    """Distinct served POS by commercial in the performance-only hour window."""
    if df.empty:
        return pd.DataFrame(columns=["Nom_Ccial", "POS_serve", "Nouveaux"])
    hours = _hour_series(df)
    mask = _served_transfer_mask(df, excluded=excluded) & (hours >= hour_start) & (hours < hour_end)
    window = pd.DataFrame(
        {
            "Nom_Ccial": _commercial_series(df).fillna("NON RENSEIGNE"),
            "To_clean": _to_msisdn_series(df),
        }
    )[mask]
    if window.empty:
        return pd.DataFrame(columns=["Nom_Ccial", "POS_serve", "Nouveaux"])
    result = (
        window.groupby("Nom_Ccial", dropna=False)
        .agg(POS_serve=("To_clean", "nunique"), Nouveaux=("To_clean", lambda values: len(set(values.dropna()))))
        .reset_index()
    )
    return result
