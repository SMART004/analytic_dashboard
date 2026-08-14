from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

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


@dataclass
class PosNonTouchesContext:
    filters: PosNonTouchesFilters
    kpis: dict[str, Any]
    detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_zone_sa: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_territory: pd.DataFrame = field(default_factory=pd.DataFrame)
    by_commercial: pd.DataFrame = field(default_factory=pd.DataFrame)
    inactivity_buckets: pd.DataFrame = field(default_factory=pd.DataFrame)
    export_styles: dict[str, Any] = field(default_factory=dict)
    is_empty: bool = False
    message: str = ""


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
            "error": str(exc),
        }


def build_pos_non_touches_context(filters: PosNonTouchesFilters) -> PosNonTouchesContext:
    try:
        total_pos = get_total_pos_count(
            segment=_clean_filter(filters.segment, all_labels=("Tous", "Toutes")),
            site=_clean_filter(filters.site, all_labels=("Tous", "Toutes")),
            zone_sa=_clean_filter(filters.zone_sa),
            zone=_clean_filter(filters.zone),
            territory=_clean_filter(filters.territory),
        )
        detail = get_pos_non_touches_detail(
            start_date=filters.start_date,
            end_date=filters.end_date,
            segment=_clean_filter(filters.segment, all_labels=("Tous", "Toutes")),
            site=_clean_filter(filters.site, all_labels=("Tous", "Toutes")),
            zone_sa=_clean_filter(filters.zone_sa),
            zone=_clean_filter(filters.zone),
            territory=_clean_filter(filters.territory),
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
    work = df.copy()
    end_ts = pd.to_datetime(end_date, errors="coerce") if end_date else pd.Timestamp.today().normalize()
    last = pd.to_datetime(work.get("Derniere date d'intervention"), errors="coerce")
    work["Delai inactivite (jours)"] = (end_ts - last).dt.days
    work["Delai inactivite (jours)"] = work["Delai inactivite (jours)"].where(work["Delai inactivite (jours)"] >= 0)
    return work


def _build_kpis(df: pd.DataFrame, total_pos: int) -> dict[str, Any]:
    total_non_touches = int(df["Numero du POS"].nunique()) if not df.empty and "Numero du POS" in df.columns else 0
    hvc_mask = df["Commercial attribue"].notna() & (df["Commercial attribue"].astype(str).str.strip() != "") if not df.empty else pd.Series(dtype=bool)
    hvc_non_touches = int(df.loc[hvc_mask, "Numero du POS"].nunique()) if not df.empty else 0
    commerciaux_impactes = int(df.loc[hvc_mask, "Commercial attribue"].nunique()) if not df.empty else 0
    avg_inactivity = float(df["Delai inactivite (jours)"].dropna().mean()) if not df.empty and "Delai inactivite (jours)" in df.columns else 0.0

    return {
        "total_pos": total_pos,
        "total_non_touches": total_non_touches,
        "taux_non_touches": round(total_non_touches / total_pos * 100, 1) if total_pos else 0.0,
        "hvc_non_touches": hvc_non_touches,
        "commerciaux_impactes": commerciaux_impactes,
        "delai_moyen_inactivite": round(avg_inactivity, 1) if pd.notna(avg_inactivity) else 0.0,
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
    if df.empty or column not in df.columns:
        return pd.DataFrame(columns=[output_label, "POS non touches"])
    work = df.copy()
    work[column] = work[column].fillna("Non renseigne").astype(str).str.strip()
    work = work[~work[column].str.lower().isin({"", "nan", "none", "null"})]
    return (
        work.groupby(column)["Numero du POS"]
        .nunique()
        .reset_index(name="POS non touches")
        .rename(columns={column: output_label})
        .sort_values("POS non touches", ascending=False)
        .reset_index(drop=True)
    )


def _inactivity_buckets(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "Delai inactivite (jours)" not in df.columns:
        return pd.DataFrame(columns=["Delai", "POS non touches"])
    work = df[df["Delai inactivite (jours)"].notna()].copy()
    if work.empty:
        return pd.DataFrame(columns=["Delai", "POS non touches"])
    bins = [-1, 7, 14, 30, 10_000]
    labels = ["0-7 jours", "8-14 jours", "15-30 jours", "30+ jours"]
    work["Delai"] = pd.cut(work["Delai inactivite (jours)"], bins=bins, labels=labels)
    return (
        work.groupby("Delai", observed=False)["Numero du POS"]
        .nunique()
        .reset_index(name="POS non touches")
    )


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
