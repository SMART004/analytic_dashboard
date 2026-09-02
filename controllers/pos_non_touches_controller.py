from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Optional
import numpy as np
import pandas as pd
import polars as pl
import streamlit as st

from models.pos_non_touches_model import (
    get_pos_non_touches_detail,
    get_pos_non_touches_filter_options,
    get_total_pos_count,
)


@dataclass(frozen=True)
class PosNonTouchesFilters:
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    segment: str = "Tous"
    site: str = "Tous"
    zone_sa: str = "Toutes"
    zone: str = "Toutes"
    territory: str = "Toutes"
    cluster: str = "Tous"


@dataclass
class PosNonTouchesContext:
    filters: PosNonTouchesFilters
    kpis: dict[str, Any]
    detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_zone_sa: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_territory: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_cluster: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_commercial: pd.DataFrame = field(default_factory=pd.DataFrame)
    inactivity_buckets: pd.DataFrame = field(default_factory=pd.DataFrame)
    export_styles: dict[str, Any] = field(default_factory=dict)
    is_empty: bool = False
    message: str = ""


@st.cache_data(ttl=300, show_spinner=False)  # 5 minutes — options quasi-statiques
def load_filter_options() -> dict[str, Any]:
    try:
        return get_pos_non_touches_filter_options()
    except Exception as exc:
        return {
            "min_date": None,
            "max_date": None,
            "segments": [],
            "sites": [],
            "zone_sa": [],
            "zones": [],
            "territories": [],
            "clusters": [],
            "error": str(exc),
        }


@st.cache_data(ttl=120, show_spinner=False)  # 2 minutes — PosNonTouchesFilters est frozen=True (hashable)
def build_pos_non_touches_context(filters: PosNonTouchesFilters) -> PosNonTouchesContext:
    try:
        total_pos = get_total_pos_count(
            segment=_clean_filter(filters.segment, all_labels=("Tous", "Toutes")),
            site=_clean_filter(filters.site, all_labels=("Tous", "Toutes")),
            zone_sa=_clean_filter(filters.zone_sa),
            zone=_clean_filter(filters.zone),
            territory=_clean_filter(filters.territory),
            cluster=_clean_filter(filters.cluster, all_labels=("Tous", "Toutes")),
        )
        detail = get_pos_non_touches_detail(
            start_date=filters.start_date,
            end_date=filters.end_date,
            segment=_clean_filter(filters.segment, all_labels=("Tous", "Toutes")),
            site=_clean_filter(filters.site, all_labels=("Tous", "Toutes")),
            zone_sa=_clean_filter(filters.zone_sa),
            zone=_clean_filter(filters.zone),
            territory=_clean_filter(filters.territory),
            cluster=_clean_filter(filters.cluster, all_labels=("Tous", "Toutes")),
        )
    except Exception as exc:
        return PosNonTouchesContext(
            filters=filters,
            kpis=_empty_kpis(),
            is_empty=True,
            message=f"Erreur chargement POS non touches : {exc}",
        )

    detail = _add_inactivity_days(detail, filters.end_date)
    kpis = _build_kpis(detail, total_pos)

    if detail.empty:
        return PosNonTouchesContext(
            filters=filters,
            kpis=kpis,
            detail=detail,
            export_styles=_export_styles(),
            is_empty=True,
            message="Aucun POS non touche pour ces filtres.",
        )

    return PosNonTouchesContext(
        filters=filters,
        kpis=kpis,
        detail=detail,
        by_zone_sa=_count_by(detail, "Zone_SA", "Zone_SA"),
        by_territory=_count_by(detail, "Territoire", "Territoire"),
        by_cluster=_count_by(detail, "Cluster", "Cluster"),
        by_commercial=_count_by(detail[detail["Commercial attribue"].notna()], "Commercial attribue", "Commercial"),
        inactivity_buckets=_inactivity_buckets(detail),
        export_styles=_export_styles(),
    )


def _clean_filter(value: Optional[str], all_labels: tuple[str, ...] = ("Toutes", "Tous")) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    if not value or value in all_labels:
        return None
    return value


def _add_inactivity_days(df: pd.DataFrame, end_date: Optional[str]) -> pd.DataFrame:
    if df.empty:
        return df
    end_ts = pd.to_datetime(end_date).date() if end_date else datetime.now().date()
    lf = pl.from_pandas(df).lazy()
    
    date_col = "Derniere date d'intervention"
    if date_col in df.columns:
        lf = lf.with_columns(
            pl.col(date_col).str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False).alias("_last_dt")
        ).with_columns(
            (pl.lit(end_ts).cast(pl.Date) - pl.col("_last_dt").cast(pl.Date)).dt.total_days().alias("Delai inactivite (jours)")
        ).with_columns(
            pl.when(pl.col("Delai inactivite (jours)") >= 0)
            .then(pl.col("Delai inactivite (jours)"))
            .otherwise(None)
            .alias("Delai inactivite (jours)")
        ).drop(["_last_dt"])
    return lf.collect().to_pandas()


def _build_kpis(df: pd.DataFrame, total_pos: int) -> dict[str, Any]:
    if df.empty:
        return _empty_kpis()
    
    lf = pl.from_pandas(df).lazy()
    
    total_non_touches = df["Numero du POS"].nunique() if "Numero du POS" in df.columns else 0
    
    # HVC mask
    hvc_non_touches = 0
    commerciaux_impactes = 0
    if "Commercial attribue" in df.columns and "Numero du POS" in df.columns:
        hvc_lf = lf.filter(
            pl.col("Commercial attribue").is_not_null() &
            (pl.col("Commercial attribue").cast(pl.Utf8).str.strip_chars() != "")
        )
        hvc_agg = hvc_lf.select([
            pl.col("Numero du POS").n_unique().alias("hvc_nt"),
            pl.col("Commercial attribue").n_unique().alias("comm_imp")
        ]).collect()
        if len(hvc_agg) > 0:
            hvc_non_touches = int(hvc_agg["hvc_nt"][0])
            commerciaux_impactes = int(hvc_agg["comm_imp"][0])

    avg_inactivity = float(df["Delai inactivite (jours)"].dropna().mean()) if "Delai inactivite (jours)" in df.columns and len(df["Delai inactivite (jours)"].dropna()) > 0 else 0.0

    return {
        "total_pos": total_pos,
        "total_non_touches": total_non_touches,
        "taux_non_touches": round(total_non_touches / total_pos * 100, 1) if total_pos else 0.0,
        "hvc_non_touches": hvc_non_touches,
        "commerciaux_impactes": commerciaux_impactes,
        "delai_moyen_inactivite": round(avg_inactivity, 1) if not np.isnan(avg_inactivity) else 0.0,
    }


def _empty_kpis() -> dict[str, Any]:
    return {
        "total_pos": 0,
        "total_non_touches": 0,
        "taux_non_touches": 0.0,
        "hvc_non_touches": 0,
        "commerciaux_impactes": 0,
        "delai_moyen_inactivite": 0.0,
    }


def _count_by(df: pd.DataFrame, column: str, output_label: str) -> pd.DataFrame:
    if df.empty or column not in df.columns or "Numero du POS" not in df.columns:
        return pd.DataFrame(columns=[output_label, "POS non touches"])
    
    lf = pl.from_pandas(df).lazy()
    res = lf.with_columns(
        pl.col(column).fill_null("Non renseigne").cast(pl.Utf8).str.strip_chars().alias("_grp_col")
    ).filter(
        ~pl.col("_grp_col").str.to_lowercase().is_in(["", "nan", "none", "null"])
    ).group_by("_grp_col").agg(
        pl.col("Numero du POS").n_unique().alias("POS non touches")
    ).rename({"_grp_col": output_label}).sort("POS non touches", descending=True).collect()
    
    return res.to_pandas()


def _inactivity_buckets(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "Delai inactivite (jours)" not in df.columns or "Numero du POS" not in df.columns:
        return pd.DataFrame(columns=["Delai", "POS non touches"])
    
    lf = pl.from_pandas(df).lazy()
    lf = lf.filter(pl.col("Delai inactivite (jours)").is_not_null())
    
    res = lf.with_columns(
        pl.when(pl.col("Delai inactivite (jours)") <= 7).then(pl.lit("0-7 jours"))
        .when(pl.col("Delai inactivite (jours)") <= 14).then(pl.lit("8-14 jours"))
        .when(pl.col("Delai inactivite (jours)") <= 30).then(pl.lit("15-30 jours"))
        .otherwise(pl.lit("30+ jours"))
        .alias("Delai")
    ).group_by("Delai").agg(
        pl.col("Numero du POS").n_unique().alias("POS non touches")
    ).collect()
    
    # Ordonnancement correct des tranches
    order_map = {"0-7 jours": 0, "8-14 jours": 1, "15-30 jours": 2, "30+ jours": 3}
    df_res = res.to_pandas()
    if not df_res.empty:
        df_res["_order"] = df_res["Delai"].map(order_map).fillna(99)
        df_res = df_res.sort_values("_order").drop(columns=["_order"]).reset_index(drop=True)
    return df_res


def _export_styles() -> dict[str, Any]:
    return {
        "header_fill": "#22313f",
        "subheader_fill": "#34515e",
        "row_even_fill": "#ffffff",
        "row_odd_fill": "#f5f7fa",
        "column_styles": {
            "Commercial attribue": {"fill": "#e8f5e9", "font_color": "#1b5e20"},
            "Delai inactivite (jours)": {"fill": "#fff3e0", "font_color": "#e65100"},
        },
        "rules": [
            {
                "columns": ["Delai inactivite (jours)"],
                "op": ">=",
                "value": 30,
                "style": {"fill": "#ffcdd2", "font_color": "#b71c1c", "bold": True},
            }
        ],
        "formats": {"Delai inactivite (jours)": "0"},
    }
