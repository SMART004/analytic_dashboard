"""Streamlit rendering for the dashboard page."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from controllers.dashboard_controller import DashboardContext, DashboardFilters
from services.export_service import to_csv, to_excel, to_image


def render_dashboard_filters(options: dict[str, Any]) -> DashboardFilters:
    min_date = _parse_date(options.get("min_date"))
    max_date = _parse_date(options.get("max_date")) or min_date

    with st.sidebar:
        if min_date and max_date:
            selected_dates = st.date_input(
                "Plage de dates",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                key="dashboard_date_filter",
            )
        else:
            selected_dates = None

        start_date, end_date = _date_range_to_strings(selected_dates)

        centres = ["Toutes"] + list(options.get("centres", []))
        centre = st.selectbox("Centre", centres, key="dashboard_centre_filter")

        territoires = ["Toutes"] + list(options.get("territoires", []))
        territoire = st.selectbox("Territoire", territoires, key="dashboard_territoire_filter")

        commerciaux = ["Tous"] + list(options.get("commerciaux", []))
        commercial = st.selectbox("Commercial", commerciaux, key="dashboard_commercial_filter")

        segments = ["Tous"] + list(options.get("segments", []))
        seg_idx = next((i for i, s in enumerate(segments) if str(s).strip().upper() == "1-HVC"), 0)
        segment = st.selectbox("Segment", segments, index=seg_idx, key="dashboard_segment_filter")

    return DashboardFilters(
        start_date=start_date,
        end_date=end_date,
        centre=centre,
        territoire=territoire,
        commercial=commercial,
        segment=segment,
    )


def render_dashboard(context: DashboardContext) -> None:
    st.title("Dashboard General")

    if context.is_empty:
        st.warning(context.message)
        return

    _render_kpis(context.kpis)
    st.divider()

    left, right = st.columns([1.15, 0.85])
    with left:
        _render_non_touched_by_cluster(context)
    with right:
        _render_oos_radial(context.kpis.get("oos_rate", 0.0))

    st.divider()
    _render_cluster_ranking(context)
    st.divider()
    _render_territory_section(context)


def _render_kpis(kpis: dict[str, Any]) -> None:
    cols = st.columns(5)
    cols[0].metric("Montant distribue", _format_fcfa(kpis.get("distributed_amount", 0)))
    cols[1].metric("POS servis", f"{kpis.get('touched_pos', 0):,}/{kpis.get('total_pos', 0):,}".replace(",", " "))
    cols[2].metric("Couverture", f"{kpis.get('coverage_rate', 0):.1f}%")
    cols[3].metric("POS non touches", f"{kpis.get('not_touched_pos', 0):,}".replace(",", " "))
    rotation = kpis.get("avg_rotation")
    cols[4].metric("Rotation moyenne", f"{rotation:.2f}" if rotation is not None else "N/A")

    sub = st.columns(2)
    sub[0].metric("OOS actuel", f"{kpis.get('oos_rate', 0):.1f}%")
    sub[1].metric("Transfer", _format_fcfa(kpis.get("transfer_volume", 0)))


def _render_non_touched_by_cluster(context: DashboardContext) -> None:
    st.subheader("POS non touches par cluster")
    df = context.top_commerciaux
    if df.empty:
        st.info("Aucune donnee disponible.")
        return

    # Filtrer les libellés invalides
    _INVALID = {"NON RENSEIGNE", "None", "N/A", "nan", "", "null"}
    chart_df = df.rename(columns={"Cluster": "Cluster", "POS_Non_Touches": "POS non touches"})
    chart_df = chart_df[~chart_df["Cluster"].astype(str).str.strip().isin(_INVALID)].copy()
    if chart_df.empty:
        st.info("Aucune donnee disponible.")
        return
    fig = px.bar(chart_df, x="Cluster", y="POS non touches", color="POS non touches", height=320)
    fig.update_layout(xaxis_title="", yaxis_title="POS non touches", showlegend=False)
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(chart_df, use_container_width=True, hide_index=True)
    _render_exports("dashboard_pos_non_touches_cluster", chart_df, context.export_styles)


def _render_oos_radial(oos_rate: float) -> None:
    st.subheader("Niveau OOS")
    try:
        from views.oos_view import _build_oos_gauge
        fig = _build_oos_gauge(float(oos_rate))
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        st.metric("OOS actuel", f"{float(oos_rate):.1f}%")


def _render_cluster_ranking(context: DashboardContext) -> None:
    st.subheader("Classement clusters")
    df = context.site_cluster_ranking
    if df.empty:
        st.info("Aucun classement disponible.")
        return

    # Filtrer les libellés invalides
    _INVALID = {"NON RENSEIGNE", "None", "N/A", "nan", "", "null"}
    df = df[~df["Groupe"].astype(str).str.strip().isin(_INVALID)].copy()
    if df.empty:
        st.info("Aucun classement disponible.")
        return

    fig = px.bar(df, x="Groupe", y="Score", color="Score", height=280)
    fig.update_layout(xaxis_title="", yaxis_title="Score")
    st.plotly_chart(fig, use_container_width=True)
    if "Taux OOS (%)" in df.columns:
        from views.oos_view import _oos_cell_color
        st.dataframe(df.style.applymap(_oos_cell_color, subset=["Taux OOS (%)"]), use_container_width=True, hide_index=True)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
    _render_exports("dashboard_classement_clusters", df, context.export_styles)


def _render_territory_section(context: DashboardContext) -> None:
    st.subheader("Couverture, montant distribue et OOS par territoire")
    df = context.territory_summary
    if df.empty:
        st.info("Aucune synthese territoriale disponible.")
        return

    # Filtrer les libellés invalides
    _INVALID = {"NON RENSEIGNE", "None", "N/A", "nan", "", "null"}
    chart_df = df.copy()
    if "Territoire" in chart_df.columns:
        chart_df = chart_df[~chart_df["Territoire"].astype(str).str.strip().isin(_INVALID)].copy()

    if "Taux de couverture" in chart_df.columns:
        chart_df["Couverture (%)"] = chart_df["Taux de couverture"]
    if "Taux OOS (%)" not in chart_df.columns and not context.oos_summary.empty:
        if "Territoire" in context.oos_summary.columns:
            chart_df = chart_df.merge(context.oos_summary[["Territoire", "Taux OOS (%)"]], on="Territoire", how="left")
    if "Taux OOS (%)" not in chart_df.columns:
        chart_df["Taux OOS (%)"] = 0.0

    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=chart_df["Territoire"],
            y=chart_df["Montant_Distribue"],
            name="Montant distribué",
            yaxis="y",
            marker_color="#4c78a8",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=chart_df["Territoire"],
            y=chart_df["Couverture (%)"],
            mode="lines+markers",
            name="Couverture (%)",
            yaxis="y2",
            line=dict(color="#2ca02c", width=3),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=chart_df["Territoire"],
            y=chart_df["Taux OOS (%)"],
            mode="lines+markers",
            name="OOS (%)",
            yaxis="y2",
            line=dict(color="#d62728", width=3, dash="dash"),
        )
    )
    fig.update_layout(
        title="Couverture, montant distribué et OOS par territoire",
        yaxis=dict(title="Montant distribué"),
        yaxis2=dict(title="Couverture / OOS (%)", overlaying="y", side="right"),
        height=320,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    display_cols = [
        col for col in [
            "Territoire",
            "Montant_Distribue",
            "POS_Servis_Uniques",
            "Total_POS_Territoire",
            "Taux de couverture",
            "Taux OOS (%)",
            "Ecart",
        ] if col in df.columns
    ]
    # Filtrer aussi la table affichée
    disp_df = df[display_cols].copy()
    if "Territoire" in disp_df.columns:
        disp_df = disp_df[~disp_df["Territoire"].astype(str).str.strip().isin(_INVALID)]
    if "Taux OOS (%)" in disp_df.columns:
        from views.oos_view import _oos_cell_color
        st.dataframe(disp_df.style.applymap(_oos_cell_color, subset=["Taux OOS (%)"]), use_container_width=True, hide_index=True)
    else:
        st.dataframe(disp_df, use_container_width=True, hide_index=True)
    _render_exports("dashboard_territoires", disp_df, context.export_styles)


def _render_exports(prefix: str, df: pd.DataFrame, styles: dict[str, Any]) -> None:
    col_csv, col_xlsx, col_png = st.columns(3)
    col_csv.download_button(
        "CSV",
        to_csv(df),
        f"{prefix}.csv",
        "text/csv",
        key=f"{prefix}_csv",
    )
    col_xlsx.download_button(
        "Excel",
        to_excel(df, styles=styles, sheet_name="Dashboard"),
        f"{prefix}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{prefix}_xlsx",
    )
    col_png.download_button(
        "Image",
        to_image(df, styles=styles, title=prefix),
        f"{prefix}.png",
        "image/png",
        key=f"{prefix}_png",
    )


def _format_fcfa(value: Any) -> str:
    try:
        amount = float(value)
    except Exception:
        amount = 0.0
    abs_amount = abs(amount)
    if abs_amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:,.1f} Md FCFA".replace(",", " ")
    if abs_amount >= 1_000_000:
        return f"{amount / 1_000_000:,.1f} M FCFA".replace(",", " ")
    if abs_amount >= 1_000:
        return f"{amount / 1_000:,.1f} k FCFA".replace(",", " ")
    return f"{int(amount):,} FCFA".replace(",", " ")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _date_range_to_strings(selected_dates: Any) -> tuple[str | None, str | None]:
    if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
        start, end = selected_dates
    elif selected_dates:
        start = end = selected_dates
    else:
        return None, None
    return pd.to_datetime(start).date().isoformat(), pd.to_datetime(end).date().isoformat()
