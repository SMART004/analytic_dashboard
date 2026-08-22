from __future__ import annotations

from datetime import date
from typing import Any, Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from controllers.pos_non_touches_controller import (
    PosNonTouchesContext,
    PosNonTouchesFilters,
    build_pos_non_touches_context,
    load_filter_options,
)
from services.export_service import to_csv, to_excel, to_image


def render_pos_non_touches_page() -> None:
    st.title("Analyse des POS non touches")

    with st.expander("Source et logique metier", expanded=False):
        st.info(
            "La page croise referentiel_pos, transactions et hvc_commercial_mapping. "
            "Un POS non touche est un POS du referentiel sans transaction recue sur la periode selectionnee. "
            "La derniere intervention est calculee sur tout l'historique disponible."
        )

    options = load_filter_options()
    if options.get("error"):
        st.warning(f"Options de filtres incompletes : {options['error']}")

    filters = _render_filters(options)
    with st.spinner("Chargement de l'analyse POS non touches..."):
        context = build_pos_non_touches_context(filters)

    _render_kpis(context)

    if context.is_empty:
        st.warning(context.message)
        return

    left, middle, right = st.columns([1.1, 1.1, 1.2])
    with left:
        _render_bar(context.by_zone_sa.head(12), x="POS non touches", y="Zone_SA", title="Repartition par Zone_SA")
    with middle:
        _render_bar(context.by_territory.head(12), x="POS non touches", y="Territoire", title="Top territoires")
    with right:
        _render_bar(context.by_commercial.head(12), x="POS non touches", y="Commercial", title="HVC non touches par commercial")

    lower_left, lower_right = st.columns([1, 1])
    with lower_left:
        _render_inactivity(context)
    with lower_right:
        _render_matrix(context)

    st.subheader("Tableau detaille")
    display = _display_table(context.detail)
    st.dataframe(
        display.style.apply(_highlight_inactivity, axis=1),
        use_container_width=True,
        hide_index=True,
        height=520,
    )
    _render_exports(display, context.export_styles)


def _render_filters(options: dict[str, Any]) -> PosNonTouchesFilters:
    min_date = _parse_date(options.get("min_date"))
    max_date = _parse_date(options.get("max_date")) or min_date

    with st.sidebar:
        st.subheader("Filtres POS non touches")
        if min_date and max_date:
            selected_dates = st.date_input(
                "Periode",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                key="pnt_date_filter",
            )
        else:
            selected_dates = None

        start_date, end_date = _date_range_to_strings(selected_dates)
        segment_options = ["Tous"] + list(options.get("segments", []))
        segment = st.selectbox("Segment", segment_options, key="pnt_segment")
        site = st.selectbox("Site", ["Tous"] + list(options.get("sites", [])), key="pnt_site")
        zone_sa = st.selectbox("Zone SA", ["Toutes"] + list(options.get("zone_sa", [])), key="pnt_zone_sa")
        zone = st.selectbox("Zone", ["Toutes"] + list(options.get("zones", [])), key="pnt_zone")
        territory = st.selectbox("Territoire", ["Toutes"] + list(options.get("territories", [])), key="pnt_territory")
        cluster = st.selectbox("Cluster", ["Tous"] + list(options.get("clusters", [])), key="pnt_cluster")

    return PosNonTouchesFilters(
        start_date=start_date,
        end_date=end_date,
        segment=segment,
        site=site,
        zone_sa=zone_sa,
        zone=zone,
        territory=territory,
        cluster=cluster,
    )


def _render_kpis(context: PosNonTouchesContext) -> None:
    kpis = context.kpis
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total POS Non Touches", _fmt_int(kpis.get("total_non_touches", 0)))
    c2.metric("Taux de POS Non Touches", f"{kpis.get('taux_non_touches', 0.0):.1f}%")
    c3.metric("POS HVC Non Touches", _fmt_int(kpis.get("hvc_non_touches", 0)))
    c4.metric("Commerciaux Impactes", _fmt_int(kpis.get("commerciaux_impactes", 0)))
    c5.metric("Delai Moyen d'Inactivite", f"{kpis.get('delai_moyen_inactivite', 0.0):.1f} jours")
    st.caption(f"Parc filtre : {_fmt_int(kpis.get('total_pos', 0))} POS")


def _render_bar(df: pd.DataFrame, x: str, y: str, title: str) -> None:
    st.subheader(title)
    if df.empty:
        st.info("Aucune donnee.")
        return
    fig = px.bar(df.sort_values(x, ascending=True), x=x, y=y, orientation="h", text=x, height=360)
    fig.update_traces(textposition="outside", marker_color="#2f6f73")
    fig.update_layout(
        xaxis_title="",
        yaxis_title="",
        showlegend=False,
        margin=dict(l=8, r=24, t=12, b=8),
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_inactivity(context: PosNonTouchesContext) -> None:
    st.subheader("Delai d'inactivite")
    df = context.inactivity_buckets
    if df.empty:
        st.info("Aucune derniere intervention connue.")
        return
    fig = px.bar(df, x="Delai", y="POS non touches", text="POS non touches", height=320)
    fig.update_traces(textposition="outside", marker_color="#9a6b30")
    fig.update_layout(xaxis_title="", yaxis_title="", showlegend=False, margin=dict(l=8, r=24, t=12, b=8))
    st.plotly_chart(fig, use_container_width=True)


def _render_matrix(context: PosNonTouchesContext) -> None:
    st.subheader("Matrice Zone SA / Territoire")
    df = context.detail
    if df.empty:
        st.info("Aucune donnee.")
        return
    matrix = (
        df.pivot_table(
            index="Zone_SA",
            columns="Territoire",
            values="Numero du POS",
            aggfunc="nunique",
            fill_value=0,
        )
        .reset_index()
    )
    st.dataframe(matrix, use_container_width=True, hide_index=True, height=320)


def _display_table(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Nom du POS",
        "Numero du POS",
        "Site",
        "Territoire",
        "Zone_SA",
        "Zone",
        "Cluster",
        "MSISDN intervenant",
        "Nom intervenant",
        "Type intervenant",
        "Zone_SA intervenant",
        "Derniere date d'intervention",
        "Commercial attribue",
        "Delai inactivite (jours)",
    ]
    available = [col for col in columns if col in df.columns]
    return df[available].copy()


def _highlight_inactivity(row: pd.Series) -> list[str]:
    value = pd.to_numeric(row.get("Delai inactivite (jours)"), errors="coerce")
    if pd.isna(value):
        return ["" for _ in row]
    if value >= 30:
        return ["background-color: #ffebee; color: #9f1239;" for _ in row]
    if value >= 14:
        return ["background-color: #fff7ed; color: #9a3412;" for _ in row]
    return ["" for _ in row]


def _render_exports(df: pd.DataFrame, styles: dict[str, Any]) -> None:
    st.subheader("Exports")
    c1, c2, c3 = st.columns(3)
    c1.download_button(
        "CSV",
        to_csv(df),
        "analyse_pos_non_touches.csv",
        "text/csv",
        key="pnt_export_csv",
    )
    c2.download_button(
        "Excel",
        to_excel(df, styles, sheet_name="POS non touches"),
        "analyse_pos_non_touches.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="pnt_export_excel",
    )
    c3.download_button(
        "Image",
        to_image(df, styles, title="Analyse des POS non touches"),
        "analyse_pos_non_touches.png",
        "image/png",
        key="pnt_export_image",
    )


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _date_range_to_strings(value: Any) -> tuple[Optional[str], Optional[str]]:
    if isinstance(value, tuple) and len(value) == 2:
        return value[0].isoformat(), value[1].isoformat()
    if isinstance(value, list) and len(value) == 2:
        return value[0].isoformat(), value[1].isoformat()
    if isinstance(value, date):
        return value.isoformat(), value.isoformat()
    return None, None


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return "0"
