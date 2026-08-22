"""views/oos_hvc_view.py

Page fusionnee OOS & Variations HVC.

Structure :
  Barre de statut globale (KPIs transverses, fraicheur des donnees)
  Filtres communs (zone, territoire, cluster, SA, heure)
  Onglet 1 — Listing OOS
    - Filtre supplementaire : snapshot_date, segment_group
    - Couverture OOS par cluster (tableau)
    - Tableau de listing colore par ligne
    - Export CSV / Excel / Image / ZIP par cluster
  Onglet 2 — Variations HVC
    - Filtres supplementaires : snapshot T1 / T2
    - Graphe de progression (series temporelle, barres Day HVC + ligne %OOS)
    - KPIs de variation (delta T1->T2)
    - Top 10 / Flop 10 en barres horizontales (sites en Y, valeurs en X)
    - Tableau complet de comparaison
    - Export CSV / Excel
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from datetime import datetime

import io
import re
import zipfile


import matplotlib

from models.pos_model import get_all_pos
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


from controllers.oos_controller import (
    OosHvcContext, OosHvcFilters, OosKpis,
    OosListingContext, HvcVariationContext,
    build_oos_hvc_context, load_filter_options,
    build_hvc_variation_only, _load_hvc_snapshot,
    build_hvc_frequently_oos,
    enrich_oos_listing_with_deduced_commercials,
    read_tx_files,
    extract_hvc_low_balance_from_tx,
    inject_into_listing_oos,
    get_oos_full_dataset,
    compute_commercial_ranking,
    compute_frequently_oos_metrics
)
from ingestion.upload_sync import sync_oos_to_sqlite, sync_hvc_variation_to_sqlite
from services.export_service import to_csv, to_excel, to_grouped_zip, to_image
from utils.helpers_hvc_oos_variation import (
    compute_multiindex_variation,
    get_formatted_styler,
    render_plotly_top_flops,
    export_df_to_excel_by_territory,
    render_table_image,
    export_images_by_territories_zip,
    export_images_by_clusters_zip,
    load_zones_mapping_from_setting,
    load_dsm_mapping_from_settings,
)
from scripts.clear_listing_oos import clear_listing_oos

# Seuils visuels — coherents avec variations_hvc.py original
_OOS_SEUIL_VERT = 20.0      # % OOS < 20 -> ok
_OOS_SEUIL_JAUNE = 40.0     # % OOS 20-40 -> surveillance
_DAY_HVC_VERT = 1.0
_DAY_HVC_JAUNE = 0.8

# ==========================================================================
# Rendu image — même design que le tableau à l'écran (colonnes colorées),
# rendu 100% matplotlib (pas de dépendance à Chrome), avec pagination pour
# éviter tout MemoryError sur de grands tableaux.
# ==========================================================================
_HEADER_BG = "#374151"
_HEADER_FG = "white"
_ROW_EVEN = "#ffffff"
_ROW_ODD = "#f3f4f6"
_BORDER = "#d1d5db"
_TEXT = "#111827"

_COL_BG = {
    "Day_Target": "#C8E6C9",   # vert
    "OOS": "#FFF9C4",          # jaune
    "Float": "#FFCDD2",        # rouge
}
_COL_FG = {
    "Day_Target": "#2E7D32",
    "OOS": "#F57F17",
    "Float": "#C62828",
}
_COL_WIDTHS = {
    "Numero du POS": 1.7,
    "Nom du POS": 3.2,
    "Ccial en charge": 2.2,
    "Locality": 1.9,
    "Cluster": 1.4,
    "Territory": 1.7,
    "Zone": 1.2,
    "Segment group": 1.3,
    "Rupture": 1.1,
    "Statut": 1.3,
    "Historique": 1.8,
    "Day_Target": 1.1,
    "OOS": 1.0,
    "Float": 1.0,
    "Date dernière balance": 1.7,
}

_CELL_FONTSIZE = 7.6
_AVG_CHAR_WIDTH_FACTOR = 0.56  # largeur moyenne d'un caractère ≈ 0.56 * taille de police (points), pour DejaVu Sans
_CELL_PADDING_PT = 6.0
MAX_ROWS_PER_IMAGE = 25  # garde-fou mémoire : évite un MemoryError matplotlib sur de gros clusters


def _fit_text(text: str, max_chars: int) -> str:
    """Tronque le texte avec une ellipse s'il dépasse le nombre de caractères
    estimé pour tenir dans la cellule."""
    text = str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"


def _render_pos_table_page(df: pd.DataFrame, title: str = "") -> bytes:
    cols = list(df.columns)
    if df.empty or not cols:
        fig, ax = plt.subplots(figsize=(6, 2))
        ax.text(0.5, 0.5, "Aucune donnée à exporter", ha="center", va="center")
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    # Calcul de la largeur dynamique de la colonne Historique selon le nb max de snapshots
    if "Historique" in cols and not df.empty and "Historique" in df.columns:
        max_syms = df["Historique"].dropna().apply(lambda v: len(str(v).split())).max()
        max_syms = max(1, int(max_syms))
        # 0.20 par carré + 0.04 de gap, minimum 1.0, plafond 8.0
        hist_width = min(8.0, max(1.0, max_syms * 0.24))
        widths = [hist_width if c == "Historique" else _COL_WIDTHS.get(c, 1.3) for c in cols]
    else:
        widths = [_COL_WIDTHS.get(c, 1.3) for c in cols]
    total_w = sum(widths)
    n_rows = len(df)

    fig_w = max(9, total_w * 0.75)
    fig_h = max(2.0, (n_rows + 1) * 0.32 + (0.5 if title else 0.2))
    dpi = 200
    if fig_h * dpi > 6000:  # garde-fou mémoire, quel que soit le nb de lignes
        dpi = max(60, int(6000 / fig_h))

    pts_per_unit = (fig_w / total_w) * 72
    max_chars_per_col = {
        c: max(3, int((w * pts_per_unit - _CELL_PADDING_PT) / (_CELL_FONTSIZE * _AVG_CHAR_WIDTH_FACTOR)))
        for c, w in zip(cols, widths)
    }

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    ax.set_xlim(0, total_w)
    ax.set_ylim(0, n_rows + 1)
    ax.invert_yaxis()
    ax.axis("off")

    if title:
        ax.text(total_w / 2, -0.4, title, ha="center", va="bottom",
                 fontsize=11, fontweight="bold", color=_TEXT)

    # en-tête
    x = 0.0
    for c, w in zip(cols, widths):
        rect = Rectangle((x, 0), w, 1, facecolor=_HEADER_BG, edgecolor=_BORDER, linewidth=0.6)
        ax.add_patch(rect)
        txt = ax.text(x + w / 2, 0.5, _fit_text(c, max_chars_per_col[c]), ha="center", va="center",
                       fontsize=8.3, fontweight="bold", color=_HEADER_FG)
        txt.set_clip_path(rect)
        x += w

    # données
    for r in range(n_rows):
        y = 1 + r
        x = 0.0
        row_bg_default = _ROW_EVEN if r % 2 == 0 else _ROW_ODD
        for c, w in zip(cols, widths):
            val = df.iloc[r][c]
            cell_bg = _COL_BG.get(c, row_bg_default)
            text_color = _COL_FG.get(c, _TEXT)
            raw_text = "-" if pd.isna(val) else str(val)
            ha = "left" if c in ("Nom du POS", "Locality", "Ccial en charge") else "center"

            rect = Rectangle((x, y), w, 1, facecolor=cell_bg, edgecolor=_BORDER, linewidth=0.5)
            ax.add_patch(rect)

            if str(c) == "Historique":
                symbols = raw_text.split()
                if symbols:
                    n_sym = len(symbols)
                    box_w = min(0.22, (w * 0.7) / max(1, n_sym))
                    box_h = 0.45
                    gap = 0.05
                    total_boxes_w = n_sym * box_w + (n_sym - 1) * gap
                    start_x = x + (w - total_boxes_w) / 2.0
                    by = y + (1.0 - box_h) / 2.0
                    for idx_sym, sym in enumerate(symbols):
                        bx = start_x + idx_sym * (box_w + gap)
                        color = "#e74c3c" if "🟥" in sym else ("#2ecc71" if "🟩" in sym else "#d1d5db")
                        patch = Rectangle((bx, by), box_w, box_h, facecolor=color, edgecolor="#ffffff", linewidth=0.6)
                        ax.add_patch(patch)
            else:
                tx = x + 0.08 if ha == "left" else x + w / 2
                max_chars = max(3, max_chars_per_col[c] - (1 if ha == "left" else 0))
                text = _fit_text(raw_text, max_chars)
                txt = ax.text(tx, y + 0.5, text, ha=ha, va="center", fontsize=_CELL_FONTSIZE, color=text_color)
                txt.set_clip_path(rect)

            x += w

    fig.tight_layout(pad=0.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _render_pos_table_pages(df: pd.DataFrame, title_prefix: str) -> list:
    if df.empty:
        return [_render_pos_table_page(df, title_prefix)]
    n_pages = (len(df) + MAX_ROWS_PER_IMAGE - 1) // MAX_ROWS_PER_IMAGE
    pages = []
    for p in range(n_pages):
        chunk = df.iloc[p * MAX_ROWS_PER_IMAGE:(p + 1) * MAX_ROWS_PER_IMAGE]
        t = f"{title_prefix} (page {p + 1}/{n_pages})" if n_pages > 1 else title_prefix
        pages.append(_render_pos_table_page(chunk, t))
    return pages


# ---------------------------------------------------------------------------
# Point d'entree
# ---------------------------------------------------------------------------

def render_oos_hvc_hub() -> None:
    st.title("OOS & Variations HVC")

    options = load_filter_options()
    filters, tab_listing, tab_variation, tab_tx, tab_upload = _render_layout(options)

    context = build_oos_hvc_context(filters)

    # KPI
    _render_global_status_bar(context.global_kpis)

    # Graphiques globaux (hors tabs)
    _render_global_dashboard(context.global_kpis, context.variation.progression)

    st.markdown("---")
    render_reset_oos_button()

    st.markdown("---")

    # Onglets
    with tab_listing:
        _render_listing_tab(context.listing, filters)

    with tab_variation:
        _render_variation_tab(context.variation, filters, options)

    with tab_tx:
        _render_tx_to_oos_tab()

    with tab_upload:
        _render_upload_section()

def _render_tx_to_oos_tab() -> None:
    """Onglet : upload transactions → HVC balance < oos_target → listing OOS."""
    st.markdown("### Transactions → Listing OOS (HVC sous cible)")
    st.caption(
        "Les fichiers ne sont **pas** stockés. "
        "On extrait les HVC dont la dernière balance (From) est < oos_target, "
        "puis on les injecte dans le listing OOS avec le snapshot de l’heure d’upload."
    )

    files = st.file_uploader(
        "Fichiers de transactions (.xlsx, .xls, .csv)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="tx_to_oos_upload",
    )

    if not files:
        st.info("Chargez un ou plusieurs fichiers de transactions.")
        return

    if st.button("Analyser & injecter dans Listing OOS", type="primary", key="tx_to_oos_run"):
        with st.spinner("Traitement en mémoire…"):
            try:
                tx_df = read_tx_files(files)
                if tx_df.empty:
                    st.warning("Aucun fichier valide.")
                    return

                ref_df = get_all_pos()
                if ref_df is None or ref_df.empty:
                    st.error("Référentiel POS vide. Chargez-le dans Settings.")
                    return

                pos_oos = extract_hvc_low_balance_from_tx(tx_df, ref_df)

                if pos_oos.empty:
                    st.warning("Aucun HVC avec Balance < oos_target trouvé.")
                    return

                st.success(f"{len(pos_oos)} HVC sous cible identifiés.")
                st.dataframe(pos_oos, use_container_width=True, height=350)

                snapshot_ts = datetime.now()
                n = inject_into_listing_oos(pos_oos, snapshot_ts=snapshot_ts)

                st.success(
                    f"{n} POS ajoutés au listing OOS "
                    f"(snapshot : {snapshot_ts.strftime('%Y-%m-%d %H:%M')})."
                )
                st.info("Les fichiers transactions n’ont pas été enregistrés.")

            except Exception as e:
                st.error(f"Erreur : {e}")

def render_reset_oos_button():
    st.warning("⚠️ Zone de danger")
    
    # Bouton avec confirmation pour éviter les fausses manipulations
    if st.button("🗑️ Vider tout l'historique OOS", key="btn_clear_oos"):
        st.session_state["confirm_clear_oos"] = True

    if st.session_state.get("confirm_clear_oos", False):
        st.error("Êtes-vous sûr de vouloir supprimer TOUTES les données de la table listing_oos ? Cette action est irréversible.")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Oui, tout vider", key="btn_confirm_yes"):
                count = clear_listing_oos()
                st.success(f"La table a été vidée ({count} enregistrements supprimés).")
                st.session_state["confirm_clear_oos"] = False
                st.rerun()
                
        with col2:
            if st.button("❌ Annuler", key="btn_confirm_no"):
                st.session_state["confirm_clear_oos"] = False
                st.rerun()

# ---------------------------------------------------------------------------
# Layout et filtres
# ---------------------------------------------------------------------------

def _render_layout(options: dict):
    """Filtres communs en haut, puis onglets."""
    st.markdown("### Filtres")

    min_d_str = options.get("min_date")
    max_d_str = options.get("max_date")
    try:
        min_d = datetime.strptime(min_d_str, "%Y-%m-%d").date() if min_d_str else datetime.now().date()
        max_d = datetime.strptime(max_d_str, "%Y-%m-%d").date() if max_d_str else datetime.now().date()
    except Exception:
        min_d = datetime.now().date()
        max_d = datetime.now().date()

    c0, c1, c2, c3, c4, c5, c6 = st.columns([1.8, 1.2, 1.2, 1.2, 1.2, 1.2, 1.4])

    with c0:
        dates_selected = st.date_input(
            "Plage de dates",
            value=(min_d, max_d),
            key="ohv_dates",
        )
        if isinstance(dates_selected, (tuple, list)) and len(dates_selected) == 2:
            date_start = dates_selected[0].strftime("%Y-%m-%d")
            date_end = dates_selected[1].strftime("%Y-%m-%d")
        elif isinstance(dates_selected, (tuple, list)) and len(dates_selected) == 1:
            date_start = dates_selected[0].strftime("%Y-%m-%d")
            date_end = dates_selected[0].strftime("%Y-%m-%d")
        else:
            date_start = min_d_str
            date_end = max_d_str

    with c1:
        zone = st.selectbox("Zone", ["Toutes"] + options.get("zone", []), key="ohv_zone")
    with c2:
        territory = st.selectbox("Territoire", ["Toutes"] + options.get("territory", []),
                                  key="ohv_territory")
    with c3:
        cluster = st.selectbox("Cluster", ["Tous"] + options.get("cluster", []), key="ohv_cluster")
    with c4:
        zone_sa = st.selectbox("Zone SA", ["Toutes"] + options.get("zone_sa", []), key="ohv_zone_sa")
    with c5:
        segment_opts = ["Tous"] + options.get("segment_group", [])
        segment_group = st.selectbox("Segment", segment_opts, index=0, key="ohv_segment")
    with c6:
        hour_range = st.slider("Plage horaire", 0, 23, (0, 23), key="ohv_hour")

    tab_listing, tab_variation, tab_tx, tab_upload = st.tabs([
        "Listing OOS",
        "Variations HVC",
        "Transactions → OOS",
        "Upload / Sync",
    ])

    filters = OosHvcFilters(
        zone=zone, territory=territory, cluster=cluster, zone_sa=zone_sa,
        segment_group=segment_group,
        date_start=date_start, date_end=date_end,
        hour_min=hour_range[0], hour_max=hour_range[1],
    )
    return filters, tab_listing, tab_variation, tab_tx, tab_upload


# ---------------------------------------------------------------------------
# Barre de statut globale
# ---------------------------------------------------------------------------

def _render_global_dashboard(kpis: OosKpis, progression_df: Optional[pd.DataFrame] = None) -> None:
    """
    Affiche les graphiques globaux au-dessus des tabs.
    """

    col1, col2 = st.columns([2, 1])

    with col1:

        fig = _build_progression_chart(
            progression_df=progression_df,
            current_oos=kpis.pct_oos_global,
            previous_oos=kpis.pct_oos_previous,
        )

        st.plotly_chart(
            fig,
            use_container_width=True
        )

    with col2:

        gauge = _build_oos_gauge(
            kpis.pct_oos_global
        )

        st.plotly_chart(
            gauge,
            use_container_width=True
        )

def _render_global_status_bar(kpis: OosKpis) -> None:
    """Bande de KPIs transverses, independante des filtres d'onglet."""
    fresh = f"Snapshot : {kpis.latest_snapshot or 'N/A'} — {kpis.latest_nb_lignes:,} POS"
    st.caption(f"🕐 {fresh}")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total POS referentiel", f"{kpis.total_pos_referentiel:,}")
    c2.metric("POS en rupture", f"{kpis.nb_pos_oos:,}")
    c3.metric(
        "% OOS global",
        f"{kpis.pct_oos_global:.1f}%",
        delta=None,
        delta_color="inverse",
    )
    c4.metric("HVC en rupture", f"{kpis.nb_hvc_oos:,}")
    c5.metric("Day HVC moyen", f"{kpis.day_hvc_moyen:.2f}")
    c6.metric("Freq. OOS moy.", f"{kpis.frequence_oos:.1f} snapshots")


# ---------------------------------------------------------------------------
# Composants UI — Classement Commerciaux & POS Fréquemment en Rupture
# ---------------------------------------------------------------------------

def _build_commercial_ranking_chart(df_ranking: pd.DataFrame) -> go.Figure:
    """Graphique à barres horizontales du Top 10 des commerciaux par Score Final.
    Badges / codes couleurs:
    - Vert: Score >= 80 (#27ae60)
    - Orange: Score 50 - 79.9 (#f39c12)
    - Rouge: Score < 50 (#e74c3c)
    """
    if df_ranking.empty or "Score Final (/100)" not in df_ranking.columns:
        return go.Figure()

    top10 = df_ranking.head(10).iloc[::-1]  # Inverser pour affichage descendant dans Plotly
    commerciaux = top10["Commercial"].tolist()
    scores = top10["Score Final (/100)"].tolist()

    bar_colors = []
    for s in scores:
        if s >= 80:
            bar_colors.append("#27ae60")  # Vert
        elif s >= 50:
            bar_colors.append("#f39c12")  # Orange
        else:
            bar_colors.append("#e74c3c")  # Rouge

    fig = go.Figure(go.Bar(
        x=scores,
        y=commerciaux,
        orientation="h",
        marker=dict(color=bar_colors),
        text=[f"<b>{s:.2f} pts</b>" for s in scores],
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>Score Final : %{x:.2f} / 100<extra></extra>",
    ))

    fig.update_layout(
        title="<b>Top 10 Commerciaux par Score Final (/100)</b>",
        xaxis=dict(title="Score Final (/100)", range=[0, 110]),
        yaxis=dict(title=""),
        height=380,
        margin=dict(l=20, r=40, t=50, b=30),
        template="plotly_white",
        showlegend=False,
    )
    return fig


def _render_commercial_ranking_section(df_full: pd.DataFrame) -> None:
    st.markdown("### 🏆 Classement des Commerciaux")
    st.caption(
        "Le **Score Final (/100)** évalue la performance du commercial selon son **Taux de Résolution OOS** "
        "(70 pts max) et sa **Fréquence Moyenne d'OOS par POS** (Bonus Stabilité 30 pts max)."
    )

    df_ranking = compute_commercial_ranking(df_full)

    if df_ranking.empty:
        st.info("Aucune donnée disponible pour établir le classement des commerciaux.")
        return

    c_chart, c_table = st.columns([1.2, 1.8])

    with c_chart:
        fig_ranking = _build_commercial_ranking_chart(df_ranking)
        st.plotly_chart(fig_ranking, use_container_width=True)
        st.caption("🟢 **Vert (>= 80)**: Excellent | 🟠 **Orange (50-79)**: Moyen | 🔴 **Rouge (< 50)**: À améliorer")

    with c_table:
        st.markdown("#### Table du Classement")

        def _style_score(val):
            try:
                v = float(val)
                if v >= 80:
                    return "background-color: #d4edda; color: #155724; font-weight: bold;"
                elif v >= 50:
                    return "background-color: #fff3cd; color: #856404; font-weight: bold;"
                else:
                    return "background-color: #f8d7da; color: #721c24; font-weight: bold;"
            except Exception:
                return ""

        styled = df_ranking.style.applymap(_style_score, subset=["Score Final (/100)"])
        st.dataframe(styled, use_container_width=True, hide_index=True, height=350)

        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "📄 CSV Ranking",
                to_csv(df_ranking),
                "classement_commerciaux_oos.csv",
                "text/csv",
                key="btn_exp_ranking_csv",
            )
        with c2:
            st.download_button(
                "📊 Excel Ranking",
                to_excel(df_ranking, sheet_name="Ranking Commerciaux"),
                "classement_commerciaux_oos.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="btn_exp_ranking_xlsx",
            )


def _render_frequently_oos_section(df_full: pd.DataFrame) -> None:
    st.markdown("---")
    st.markdown("### 🚨 POS Fréquemment en Rupture (OOS Frequently)")
    st.caption(
        "Suivi des POS en rupture fréquente avec segmentation par comportement (%OOS Moyen, Durée Moyenne OOS, Float Moyen et Site de rattachement)."
    )

    c1, _ = st.columns([2.5, 1.5])
    with c1:
        behavior_filter = st.selectbox(
            "Filtre de Comportement OOS (Slicing)",
            [
                "Tous",
                "OOS Chroniques",
                "OOS Récurrents",
                "OOS Nouveaux",
                "OOS Occasionnels",
            ],
            key="oos_freq_slicing_select",
            help="Chronique = Rupture lourde ou persistante (>= 3 snapshots consécutifs ou >= 35% du temps). Récurrent = Ruptures répétées sur plusieurs jours ou >= 1.8 fois/jour. Nouveau = 1ère apparition sur le dernier snapshot. Occasionnel = Rupture ponctuelle isolée."
        )

    df_freq = compute_frequently_oos_metrics(df_full, behavior_filter=behavior_filter)

    if df_freq.empty:
        st.info("Aucun POS trouvé pour ce filtre de comportement.")
        return

    st.caption(f"📊 **{len(df_freq):,} POS** listés.")

    def _style_freq(row):
        styles = [""] * len(row)
        for i, col in enumerate(row.index):
            if col == "Statut":
                val = str(row[col])
                if val == "Chronique":
                    styles[i] = "background-color: #f8d7da; color: #721c24; font-weight: bold;"
                elif val == "Récurrent":
                    styles[i] = "background-color: #ffe8cc; color: #d9480f; font-weight: bold;"
                elif val == "Nouveau":
                    styles[i] = "background-color: #d0ebff; color: #1864ab; font-weight: bold;"
                else:
                    styles[i] = "background-color: #fff3cd; color: #856404;"
            elif col == "%OOS Moyen":
                try:
                    v = float(row[col])
                    if v >= 40:
                        styles[i] = "background-color: #ffd6d6; color: #9C0006; font-weight: bold;"
                    elif v >= 20:
                        styles[i] = "background-color: #fff3bf; color: #7d6608;"
                except Exception:
                    pass
        return styles

    styled = df_freq.style.apply(_style_freq, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True, height=450)

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "📄 CSV OOS Frequently",
            to_csv(df_freq),
            "pos_frequently_oos.csv",
            "text/csv",
            key="btn_exp_freq_csv",
        )
    with c2:
        st.download_button(
            "📊 Excel OOS Frequently",
            to_excel(df_freq, sheet_name="POS Frequently OOS"),
            "pos_frequently_oos.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="btn_exp_freq_xlsx",
        )


# ---------------------------------------------------------------------------
# Onglet Listing OOS
# ---------------------------------------------------------------------------

def _render_listing_tab(listing: OosListingContext, base_filters: OosHvcFilters) -> None:
    # Charge le dataset complet pour la plage temporelle sélectionnée
    df_full = get_oos_full_dataset(base_filters)

    # Section 1 : Classement des Commerciaux (Nouvelle Section demandée)
    _render_commercial_ranking_section(df_full)

    # Section 2 : POS Fréquemment en Rupture avec Métriques Avancées & Slicing
    _render_frequently_oos_section(df_full)

    st.markdown("---")
    st.markdown("### 📸 Détail du Snapshot Instantané & Couverture")

    # Filtres supplementaires propres a cet onglet
    c1, c2, c3 = st.columns(3)
    with c1:
        snapshot_date = st.text_input(
            "Snapshot spécifique (YYYY-MM-DD HH:MM:SS)", value="", key="oos_snapshot",
            help="Laisser vide pour le dernier snapshot disponible."
        )
    with c2:
        segment_options = ["Tous", "1-HVC", "2-MVC", "3-LVC", "4-Others"]
        segment_group = st.selectbox("Segment (listing uniquement)", segment_options, index=0, key="oos_segment")
    with c3:
        status_filter = st.selectbox(
            "Filtrer par Statut",
            ["Tous", "Chronique", "Récurrent", "Nouveau", "Occasionnel"],
            index=0,
            key="oos_status_filter",
            help="Filtrer le listing des POS par gravité de rupture."
        )

    if listing.is_empty:
        st.warning(listing.message or "Aucune donnée OOS pour ce snapshot.")
        return

    # Couverture par cluster
    if not listing.par_cluster.empty:
        st.markdown("#### Couverture OOS par cluster")
        cov_display = listing.par_cluster.rename(columns={
            "secteur_cluster": "Cluster",
            "nb_oos": "POS en rupture",
            "nb_total": "Total referentiel",
            "taux_oos_pct": "Taux OOS (%)",
        })
        st.dataframe(
            cov_display.style.applymap(_oos_cell_color, subset=["Taux OOS (%)"]),
            use_container_width=True, hide_index=True,
        )

    st.markdown("#### Détail des POS en rupture")
    display = _build_oos_display(listing.table)
    if status_filter != "Tous" and "Statut" in display.columns:
        display = display[display["Statut"] == status_filter].reset_index(drop=True)

    # Affichage du récapitulatif dynamique des statuts
    if not display.empty and "Statut" in display.columns:
        counts = display["Statut"].value_counts().to_dict()
        st.caption(
            f"📊 **Affichés : {len(display):,} POS** | "
            f"🔴 **{counts.get('Chronique', 0):,} Chroniques** | "
            f"🟠 **{counts.get('Récurrent', 0):,} Récurrents** | "
            f"🔵 **{counts.get('Nouveau', 0):,} Nouveaux** | "
            f"🟡 **{counts.get('Occasionnel', 0):,} Occasionnels**"
        )

    styled = display.style.apply(_oos_column_color, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True, height=650)

    st.markdown("#### Export")
    styles = _oos_styles()
    group_col = "Cluster" if "Cluster" in display.columns else None
    _render_exports_oos(display, styles, group_col)

    # Deduction du commercial pour les POS Non attribués
    _render_deduced_commercials_section(display)

    # ------------------------------------------------------------------
    # HVC Fréquemment en OOS (alerte chronique)
    # ------------------------------------------------------------------
    _render_hvc_frequently_oos(base_filters)


def _render_deduced_commercials_section(display: pd.DataFrame) -> None:
    """Affiche le tableau d'attribution des commerciaux enrichi avec la déduction SQL
    (nombre de transactions max avec tie-break sur la date) pour les POS marqués 'Non attribué'."""
    st.markdown("---")
    st.markdown("#### 🤝 Commerciaux & Zone SA Déduits (POS Non Attribués)")
    st.caption(
        "Recherche SQL exécutée sur les MSISDNs 'Non attribué' dans le listing OOS pour déterminer "
        "l'intervenant le plus fréquent (mode / nombre de transactions max) avec tie-break sur la date la plus récente."
    )

    if display.empty:
        st.info("Aucune donnée OOS à analyser.")
        return

    # Utilisation de la fonction enrich_oos_listing_with_deduced_commercials du controller
    enriched_df = enrich_oos_listing_with_deduced_commercials(display)

    # Construction du tableau spécifique avec les colonnes enrichies
    deduced_df = pd.DataFrame()
    deduced_df["Numero du POS"] = enriched_df.get("Numero du POS", enriched_df.get("msisdn", ""))
    deduced_df["Nom du POS"] = enriched_df.get("Nom du POS", enriched_df.get("full_name", ""))
    
    # --- Nouvelles Colonnes Ajoutées ---
    deduced_df["MSISDN Intervenant"] = enriched_df["MSISDN Intervenant"].fillna("-")
    deduced_df["Nom du commercial / Intervenant"] = (
        enriched_df["Commercial (via transactions)"]
        .fillna(enriched_df.get("Ccial en charge", "Non attribué"))
    )
    deduced_df["Type Intervenant"] = enriched_df["Type Intervenant"].fillna("N/A")
    deduced_df["Zone_SA du commercial"] = enriched_df["Zone_SA (déduit)"].fillna("N/A")

    # Masque des POS déduits via transactions
    is_deduced_mask = enriched_df["Commercial (via transactions)"].notna() & (
        enriched_df["Commercial (via transactions)"].astype(str).str.strip() != ""
    )
    deduced_df["Statut"] = is_deduced_mask.map(
        {True: "Déduit via transactions", False: "Initialement attribué"}
    )

    c_opt, c_btn = st.columns([3, 1])
    with c_opt:
        only_unassigned = st.checkbox(
            "Afficher uniquement les POS déduits (initialement 'Non attribué')",
            value=True,
            key="chk_only_deduced_pos",
        )

    if only_unassigned:
        final_table = deduced_df[is_deduced_mask].reset_index(drop=True)
    else:
        final_table = deduced_df.reset_index(drop=True)

    if final_table.empty:
        if only_unassigned:
            st.info("Aucun POS 'Non attribué' n'a pu être associé à un intervenant via l'historique des transactions.")
        else:
            st.info("Aucune donnée disponible.")
    else:
        st.dataframe(final_table, use_container_width=True, hide_index=True)

    with c_btn:
        excel_bytes = to_excel(final_table, sheet_name="Commerciaux Déduits")
        st.download_button(
            label="📊 Export Excel Unique",
            data=excel_bytes,
            file_name="pos_oos_commerciaux_deduits.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="btn_export_deduced_excel",
        )

def _build_oos_display(df: pd.DataFrame) -> pd.DataFrame:
    """Construit le tableau avec le même design que pos_oos_listing.py."""
    display = pd.DataFrame()

    display["Numero du POS"] = df.get("msisdn", df.get("MSISDN", ""))
    display["Nom du POS"] = df.get("full_name", df.get("Nom du POS", "Non trouvé"))
    display["Ccial en charge"] = df.get("commercial", df.get("Ccial en charge", "Non attribué"))
    display["Locality"] = df.get("sitename", df.get("locality", df.get("Locality", df.get("site_key", "N/A"))))
    display["Cluster"] = df.get("cluster", df.get("Cluster", "N/A"))
    display["Territory"] = df.get("territory", df.get("Territory", "N/A"))
    display["Zone"] = df.get("zone", df.get("Zone", "N/A"))
    display["Segment group"] = df.get("segment_group", df.get("Segment Group", "N/A"))

    # Nouvelles colonnes demandées: Rupture, Statut, Historique (carrés d'évolution)
    display["Rupture"] = pd.to_numeric(df.get("Rupture", df.get("appearances", 1)), errors="coerce").fillna(1).astype(int)
    display["Statut"] = df.get("Statut", "Occasionnel")
    display["Historique"] = df.get("Historique", "🟥")

    display["Day_Target"] = pd.to_numeric(df.get("day_target", df.get("Day_Target", 0)), errors="coerce").fillna(0).astype(int)
    display["OOS"] = pd.to_numeric(df.get("oos_pct", df.get("OOS", 0)), errors="coerce").fillna(0).astype(int)
    display["Float"] = pd.to_numeric(df.get("float_amount", df.get("Float", 0)), errors="coerce").fillna(0).astype(int)

    # Recherche dynamique de la colonne contenant la date
    date_col = next((c for c in df.columns if c.strip().lower() in ["last_trx_time", "last trx time", "date dernière balance", "tx_date", "created_at"]), None)

    if date_col:
        # Conversion flexible avec format mixte et support des formats FR
        last_dt = pd.to_datetime(df[date_col], errors="coerce", format="mixed", dayfirst=True)
        display["Date dernière balance"] = last_dt.dt.strftime("%d/%m/%Y %H:%M").fillna("N/A")
    else:
        display["Date dernière balance"] = "N/A"

    return display.reset_index(drop=True)

def _oos_column_color(row: pd.Series) -> list[str]:
    styles = [""] * len(row)
    for i, col in enumerate(row.index):
        if col == "Day_Target":
            styles[i] = "background-color: #C8E6C9; color: #2E7D32"
        elif col == "OOS":
            styles[i] = "background-color: #FFF9C4; color: #F57F17"
        elif col == "Float":
            styles[i] = "background-color: #FFCDD2; color: #C62828"
        elif col == "Statut":
            val = str(row[col])
            if val == "Chronique":
                styles[i] = "background-color: #f8d7da; color: #721c24; font-weight: bold;"
            elif val == "Récurrent":
                styles[i] = "background-color: #ffe8cc; color: #d9480f; font-weight: bold;"
            elif val == "Nouveau":
                styles[i] = "background-color: #d0ebff; color: #1864ab; font-weight: bold;"
            else:
                styles[i] = "background-color: #fff3cd; color: #856404;"
        elif col == "Rupture":
            styles[i] = "font-weight: bold;"
    return styles


def _oos_cell_color(val: Any) -> str:
    try:
        v = float(val)
        if v >= _OOS_SEUIL_JAUNE:
            return "background-color: #ffd6d6"
        if v >= _OOS_SEUIL_VERT:
            return "background-color: #fff3bf"
        return "background-color: #d8f3dc"
    except Exception:
        return ""


def _oos_styles() -> dict:
    return {
        "sheet_name": "Listing OOS",
        "formats": {"Float": "#,##0", "Day Target": "#,##0"},
        "rules": [
            {"columns": ["OOS"], "op": ">=", "value": _OOS_SEUIL_JAUNE, "style": {"fill": "#ffd6d6"}},
            {"columns": ["OOS"], "op": "between", "value": (_OOS_SEUIL_VERT, _OOS_SEUIL_JAUNE - 0.01), "style": {"fill": "#fff3bf"}},
            {"columns": ["OOS"], "op": "<", "value": _OOS_SEUIL_VERT, "style": {"fill": "#d8f3dc"}},
        ],
    }


def _render_hvc_frequently_oos(filters: OosHvcFilters) -> None:
    """Affiche la liste des HVC en OOS chronique (plusieurs snapshots consécutifs).

    Avertissement BI : un HVC présent dans listing_oos sur plusieurs snapshots
    successifs est un signal fort de défaillance structurelle (float insuffisant,
    approvisionnement manquant, désengagement du POS). Cette liste permet au
    chef de zone d'agir en priorité.
    """
    with st.expander("🚨 HVC en rupture fréquente (chroniquement OOS)", expanded=False):
        min_snap = st.slider(
            "Nombre minimum de snapshots en OOS", 1, 10, 2,
            key="oos_hvc_min_snap",
            help="Un HVC qui apparaît en rupture sur X snapshots consécutifs ou plus est considéré chroniquement OOS.",
        )
        df = build_hvc_frequently_oos(filters, min_snapshots=min_snap)
        if df.empty:
            st.info(
                f"Aucun HVC en rupture sur {min_snap}+ snapshots pour ces filtres. "
                "Le parc HVC est en bonne santé sur la période analysée."
            )
            return

        st.caption(
            f"📊 **{len(df)} HVC** en rupture sur au moins **{min_snap} snapshot(s)** — "
            f"OOS moyen : **{df['pct_oos_moy'].mean():.1f}%**"
        )

        # Renommage pour affichage
        display = df.rename(columns={
            "msisdn":            "MSISDN HVC",
            "territory":         "Territoire",
            "cluster":           "Cluster",
            "segment_group":     "Segment",
            "nb_snapshots_oos":  "Nb snapshots en OOS",
            "pct_oos_moy":       "% OOS moyen",
            "last_snapshot_date": "Dernier snapshot OOS",
        })

        def _color_oos(val: Any) -> str:
            try:
                v = float(val)
                if v >= _OOS_SEUIL_JAUNE:
                    return "background-color:#ffd6d6;color:#9C0006;font-weight:bold"
                if v >= _OOS_SEUIL_VERT:
                    return "background-color:#fff3bf;color:#7d6608"
                return "background-color:#d8f3dc;color:#155724"
            except Exception:
                return ""

        def _color_snaps(val: Any) -> str:
            try:
                v = int(val)
                if v >= 5:
                    return "background-color:#ffd6d6;color:#9C0006;font-weight:bold"
                if v >= 3:
                    return "background-color:#fff3bf"
                return ""
            except Exception:
                return ""

        styled = (
            display.style
            .applymap(_color_oos, subset=["% OOS moyen"])
            .applymap(_color_snaps, subset=["Nb snapshots en OOS"])
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Export rapide
        col_csv, col_xlsx = st.columns(2)
        col_csv.download_button(
            "📄 CSV", to_csv(display), "hvc_frequently_oos.csv",
            "text/csv", key="hvc_freq_oos_csv"
        )
        col_xlsx.download_button(
            "📊 Excel", to_excel(display, sheet_name="HVC Freq OOS"),
            "hvc_frequently_oos.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="hvc_freq_oos_xlsx"
        )

def _export_images_by_cluster_zip(df: pd.DataFrame, cluster_col: str = "Cluster") -> Optional[bytes]:
    if df.empty or cluster_col not in df.columns:
        return None
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for cluster in sorted(df[cluster_col].dropna().unique().tolist()):
            df_c = df[df[cluster_col] == cluster]
            if df_c.empty:
                continue
            pages = _render_pos_table_pages(df_c, f"POS OOS — {cluster}")
            clean_name = re.sub(r'[^\w\-_\. ]', '_', str(cluster))
            for i, png in enumerate(pages, start=1):
                suffix = f"_p{i}" if len(pages) > 1 else ""
                zf.writestr(f"pos_oos_{clean_name}{suffix}.png", png)
    return zip_buffer.getvalue()


def _render_exports_oos(df: pd.DataFrame, styles: dict, group_col: Optional[str]) -> None:
    has_group = bool(group_col) and group_col in df.columns
    cols = st.columns(3 if has_group else 2)
    cols[0].download_button("CSV", to_csv(df), "oos_listing.csv", "text/csv", key="oos_csv")
    cols[1].download_button(
        "Excel", to_excel(df, styles=styles, sheet_name="Listing OOS"),
        "oos_listing.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="oos_xlsx",
    )
    # cols[2].download_button(
    #     "Image", to_image(df, styles=styles, title="Listing OOS"),
    #     "oos_listing.png", "image/png", key="oos_png",
    # )
    if has_group:
        zip_file = _export_images_by_cluster_zip(df, cluster_col=group_col)
        if zip_file:
            cols[2].download_button(
                "ZIP par cluster", zip_file, "oos_listing_by_cluster.zip", "application/zip",
                key="oos_zip",
            )


# ---------------------------------------------------------------------------
# Onglet Variations HVC
# ---------------------------------------------------------------------------

def _render_variation_tab(
    variation: HvcVariationContext,
    base_filters: OosHvcFilters,
    options: dict,
) -> None:
    """Onglet Variations HVC — design identique à hvc_oos_variation.py."""

    snapshots = variation.available_snapshots
    if len(snapshots) < 2:
        st.info("Chargez au moins deux snapshots pour comparer (onglet Upload / Sync).")
        return

    # ---------- Sélection T1 / T2 ----------
    c1, c2 = st.columns(2)
    with c1:
        t1 = st.selectbox("Snapshot T1 (référence)", snapshots, index=0, key="hvc_t1")
    with c2:
        t2 = st.selectbox(
            "Snapshot T2 (cible)",
            snapshots,
            index=len(snapshots) - 1,
            key="hvc_t2",
        )

    if t1 == t2:
        st.warning("Les deux snapshots doivent être différents.")
        return

    # Labels courts (ex: 10h, 14h)
    def _short_label(ts: str) -> str:
        try:
            dt = pd.to_datetime(ts)
            return f"{dt.hour}h"
        except Exception:
            return ts[-5:] if len(ts) > 5 else ts

    old_label = _short_label(t1)
    new_label = _short_label(t2)

    st.markdown(f"### 🔄 Comparaison : **{old_label}** ➔ **{new_label}**")

    # ---------- Chargement des deux snapshots ----------
    try:
        df_t1 = _load_hvc_snapshot(t1, base_filters)
        df_t2 = _load_hvc_snapshot(t2, base_filters)
    except Exception as e:
        st.error(f"Erreur chargement snapshots : {e}")
        return

    if df_t1.empty or df_t2.empty:
        st.warning("Données insuffisantes pour un des deux snapshots.")
        return

    # Adapter au format attendu par compute_multiindex_variation
    def _to_original_format(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "site_key" not in out.columns and "SITENAME" in out.columns:
            out["site_key"] = out["SITENAME"].astype(str).str.strip().str.upper()
        if "SITENAME" not in out.columns and "site_key" in out.columns:
            out["SITENAME"] = out["site_key"]
        # Colonnes attendues
        rename = {}
        if "day_hvc" in out.columns:
            rename["day_hvc"] = "#day HVC"
        if "oos_pct" in out.columns:
            # oos_pct est souvent une fraction 0-1
            out["oos_pct"] = pd.to_numeric(out["oos_pct"], errors="coerce").fillna(0)
            out["oos_pct"] = out["oos_pct"].apply(lambda v: v if v > 1 else v)  # déjà en fraction ou %
            rename["oos_pct"] = "%OOS HVC"
        out = out.rename(columns=rename)
        keep = [c for c in ["site_key", "SITENAME", "#day HVC", "%OOS HVC"] if c in out.columns]
        return out[keep].drop_duplicates(subset=["site_key"], keep="last")

    old_df = _to_original_format(df_t1)
    new_df = _to_original_format(df_t2)

    # Zones + DSM (comme l'original)
    zones_df = load_zones_mapping_from_setting()
    dsm_df = load_dsm_mapping_from_settings()

    if zones_df.empty:
        st.warning("Fichier Zones manquant dans Settings — les territoires/clusters seront incomplets.")

    multi_df = compute_multiindex_variation(
        old_df, new_df, zones_df, dsm_df, old_label, new_label
    )

    if multi_df.empty:
        st.info("Aucun site commun entre les deux snapshots.")
        return

    # ---------- Filtres ----------
    col_f1, col_f2, col_f3 = st.columns(3)

    territory_col = ("Informations", "TERRITORY")
    cluster_col = ("Informations", "Cluster")
    site_col = ("Informations", "SITENAME")

    with col_f1:
        all_terrs = sorted(multi_df[territory_col].dropna().unique().tolist()) if territory_col in multi_df.columns else []
        selected_terrs = st.multiselect(
            "🌍 Filtre Territoire",
            options=all_terrs,
            default=all_terrs,
            key="var_territory",
        )

    with col_f2:
        if territory_col in multi_df.columns and selected_terrs:
            sub = multi_df[multi_df[territory_col].isin(selected_terrs)]
            cluster_list = sorted(sub[cluster_col].dropna().unique().tolist())
        else:
            cluster_list = sorted(multi_df[cluster_col].dropna().unique().tolist()) if cluster_col in multi_df.columns else []
        selected_clusters = st.multiselect(
            "📍 Filtre Cluster",
            options=cluster_list,
            default=cluster_list,
            key="var_cluster",
        )

    with col_f3:
        search_query = st.text_input("🔍 Rechercher un site...", key="var_search").strip()

    # Application des filtres
    filtered_df = multi_df.copy()
    if territory_col in filtered_df.columns and selected_terrs:
        filtered_df = filtered_df[filtered_df[territory_col].isin(selected_terrs)]
    if cluster_col in filtered_df.columns and selected_clusters:
        filtered_df = filtered_df[filtered_df[cluster_col].isin(selected_clusters)]
    if search_query and site_col in filtered_df.columns:
        filtered_df = filtered_df[
            filtered_df[site_col].astype(str).str.contains(search_query, case=False, na=False)
        ]

    # ---------- KPIs ----------
    tot_day_old = filtered_df[(f"🕒 {old_label}", "#day HVC")].sum()
    tot_day_new = filtered_df[(f"🕒 {new_label}", "#day HVC")].sum()
    avg_oos_old = filtered_df[(f"🕒 {old_label}", "%OOS HVC")].mean()
    avg_oos_new = filtered_df[(f"🕒 {new_label}", "%OOS HVC")].mean()
    n_sites = len(filtered_df)

    k1, k2, k3 = st.columns(3)
    k1.metric("Nombre de sites", n_sites)
    k2.metric(
        "Moyenne #day HVC",
        f"{tot_day_new / max(1, n_sites):.2f}",
        delta=f"{(tot_day_new - tot_day_old) / max(1, n_sites):+.2f}",
    )
    k3.metric(
        "%OOS HVC Moyen Global",
        f"{avg_oos_new:.2f}%",
        delta=f"{avg_oos_new - avg_oos_old:+.2f}%",
        delta_color="inverse",
    )

    # ---------- Top 5 / Flop 5 ----------
    render_plotly_top_flops(filtered_df)

    # ---------- Tableau MultiIndex formaté ----------
    st.dataframe(
        get_formatted_styler(filtered_df, old_label, new_label),
        use_container_width=True,
        hide_index=True,
        height=520,
    )

    # ---------- Exports ----------
    st.markdown("#### Export")
    c_exp1, c_exp2 = st.columns(2)

    with c_exp1:
        excel_bytes = export_df_to_excel_by_territory(filtered_df, old_label, new_label)
        st.download_button(
            label="📊 Excel (par Territoires)",
            data=excel_bytes,
            file_name=f"variation_hvc_oos_{old_label}_to_{new_label}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="hvc_excel_terr",
        )

    # with c_exp2:
    #     png_bytes = render_table_image(
    #         filtered_df,
    #         old_label,
    #         new_label,
    #         title=f"Variation %OOS HVC — {old_label} → {new_label}",
    #     )
    #     st.download_button(
    #         label="🖼️ Image PNG (vue actuelle)",
    #         data=png_bytes,
    #         file_name=f"variation_hvc_oos_{old_label}_to_{new_label}.png",
    #         mime="image/png",
    #         key="hvc_png_current",
    #     )

    with c_exp2:
        zip_cluster_bytes = export_images_by_clusters_zip(filtered_df, old_label, new_label)
        if zip_cluster_bytes:
            st.download_button(
                label="📸 Images PNG par Cluster (ZIP)",
                data=zip_cluster_bytes,
                file_name=f"captures_clusters_{old_label}_to_{new_label}.zip",
                mime="application/zip",
                key="hvc_zip_cluster",
            )
        else:
            st.caption("Aucune donnée par cluster.")

    # with c_exp3:
    #     zip_bytes = export_images_by_territories_zip(filtered_df, old_label, new_label)
    #     if zip_bytes:
    #         st.download_button(
    #             label="📸 Images PNG par Territoire (ZIP)",
    #             data=zip_bytes,
    #             file_name=f"captures_territoires_{old_label}_to_{new_label}.zip",
    #             mime="application/zip",
    #             key="hvc_zip_terr",
    #         )

def _render_variation_kpis(kpis: dict) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Day HVC T1", f"{kpis.get('day_hvc_t1', 0):.2f}")
    c2.metric(
        "Day HVC T2",
        f"{kpis.get('day_hvc_t2', 0):.2f}",
        delta=f"{kpis.get('delta_day_hvc_global', 0):+.2f}",
    )
    c3.metric("% OOS T1", f"{kpis.get('oos_t1_pct', 0):.1f}%")
    c4.metric(
        "% OOS T2",
        f"{kpis.get('oos_t2_pct', 0):.1f}%",
        delta=f"{kpis.get('oos_t2_pct', 0) - kpis.get('oos_t1_pct', 0):+.1f} pp",
        delta_color="inverse",
    )
    c5.metric(
        "Sites ameliores / deteriores",
        f"{kpis.get('sites_ameliores', 0)} / {kpis.get('sites_deteriores', 0)}",
    )


# ---------------------------------------------------------------------------
# Graphes Plotly
# ---------------------------------------------------------------------------

def _build_progression_chart(
    progression_df: Optional[pd.DataFrame] = None,
    current_oos: float = 0.0,
    previous_oos: float = 0.0,
) -> go.Figure:
    fig = go.Figure()
    hours_labels = [f"{h:02d}h" for h in range(24)]

    has_real_data = (
        progression_df is not None
        and not progression_df.empty
        and "date" in progression_df.columns
        and "hour" in progression_df.columns
        and "oos_pct" in progression_df.columns
    )

    if has_real_data:
        dates = sorted([d for d in progression_df["date"].dropna().unique() if str(d).strip()])
        latest_date = dates[-1] if dates else None
        previous_date = dates[-2] if len(dates) >= 2 else None

        j_data = progression_df[progression_df["date"] == latest_date] if latest_date else pd.DataFrame()
        j1_data = progression_df[progression_df["date"] == previous_date] if previous_date else pd.DataFrame()

        j_map = j_data.groupby("hour")["oos_pct"].last().to_dict() if not j_data.empty else {}
        j1_map = j1_data.groupby("hour")["oos_pct"].last().to_dict() if not j1_data.empty else {}

        y_j = [j_map.get(h) for h in range(24)]
        y_j1 = [j1_map.get(h) for h in range(24)]

        # Si une seule date : utiliser previous_oos comme point de reference J-1
        if previous_date is None and previous_oos is not None:
            # Place previous_oos sur les heures ou J a une valeur (reference plate)
            y_j1 = [previous_oos if y_j[h] is not None else None for h in range(24)]
            label_j1 = f"J-1 (ref KPI {previous_oos:.1f}%)"
        else:
            label_j1 = f"J-1 ({previous_date})" if previous_date else "J-1"

        label_j = f"J ({latest_date})" if latest_date else "J"

        fig.add_trace(go.Scatter(
            x=hours_labels, y=y_j1, name=label_j1,
            mode="lines+markers",
            line=dict(color="#95a5a6", width=2, dash="dash"),
            marker=dict(size=6), connectgaps=True,
            hovertemplate="%{x}<br>J-1: %{y:.1f}%<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=hours_labels, y=y_j, name=label_j,
            mode="lines+markers",
            line=dict(color="#e74c3c", width=2.5),
            marker=dict(size=7), connectgaps=True,
            hovertemplate="%{x}<br>J: %{y:.1f}%<extra></extra>",
        ))

    elif current_oos or previous_oos:
        # Fallback : 2 points KPI (pas de serie horaire)
        now_h = datetime.now().hour
        y_j = [None] * 24
        y_j1 = [None] * 24
        y_j[now_h] = current_oos
        y_j1[now_h] = previous_oos

        fig.add_trace(go.Scatter(
            x=hours_labels, y=y_j1, name=f"J-1 ({previous_oos:.1f}%)",
            mode="markers", marker=dict(size=10, color="#95a5a6"),
        ))
        fig.add_trace(go.Scatter(
            x=hours_labels, y=y_j, name=f"J ({current_oos:.1f}%)",
            mode="markers", marker=dict(size=12, color="#e74c3c"),
        ))
        fig.add_annotation(
            text="Historique horaire insuffisant — affichage des KPIs courant / précédent",
            xref="paper", yref="paper", x=0.5, y=0.95, showarrow=False,
            font=dict(size=11, color="#7f8c8d"),
        )
    else:
        fig.add_annotation(
            text="Pas encore assez d'historique de snapshots horodatés pour tracer l'évolution horaire.",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=13, color="#7f8c8d"),
        )

    fig.update_layout(
        title="Progression OOS (J vs J-1) par heure",
        height=330,
        margin=dict(l=20, r=20, t=60, b=20),
        yaxis_title="Taux OOS (%)",
        xaxis_title="Heures de la journée",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def _build_oos_gauge(current_oos: float) -> go.Figure:
    """
    Gauge radial moderne inspiré de Power BI.
    """

    if current_oos <= 20:
        value_color = "#2ecc71"
    elif current_oos < 30:
        value_color = "#f39c12"
    else:
        value_color = "#e74c3c"

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",

            value=current_oos,

            number={
                "suffix": "%",
                "font": {
                    "size": 64,          # << beaucoup plus gros
                    "color": value_color,
                    "family": "Arial Black",
                },
            },

            title={
                "text": "<b>OOS ACTUEL</b>",
                "font": {
                    "size": 24,
                },
            },

            gauge={

                "shape": "angular",

                "axis": {
                    "range": [0, 100],
                    "tickwidth": 2,
                    "tickcolor": "#777",
                    "tickfont": {"size": 14},
                },

                "bar": {
                    "color": "#2c3e50",
                    "thickness": 0.18,
                },

                "bgcolor": "white",

                "borderwidth": 0,

                "steps": [

                    {
                        "range": [0, 20],
                        "color": "#2ecc71",
                    },

                    {
                        "range": [20, 30],
                        "color": "#f4d03f",
                    },

                    {
                        "range": [30, 100],
                        "color": "#e74c3c",
                    },

                ],

                "threshold": {
                    "line": {
                        "color": "#2c3e50",
                        "width": 6,
                    },
                    "thickness": 0.85,
                    "value": current_oos,
                },

            },
        )
    )

    fig.update_layout(

        height=360,

        margin=dict(
            l=20,
            r=20,
            t=60,
            b=10,
        ),

        paper_bgcolor="white",
        plot_bgcolor="white",

        font=dict(
            family="Arial",
        ),

    )

    return fig

def _build_ranking_chart(
    df: pd.DataFrame,
    metric: str = "delta_day_hvc",
    title: str = "Top 10",
    color: str = "#4CAF50",
) -> go.Figure:
    """Barres horizontales : sites en Y, Δ Day HVC en X."""
    if df.empty or metric not in df.columns:
        return go.Figure()

    df_sorted = df.sort_values(metric, ascending=True).tail(10)
    sites = df_sorted["site_key"].astype(str).tolist()
    values = pd.to_numeric(df_sorted[metric], errors="coerce").fillna(0).tolist()
    text_labels = [f"{v:+.2f}" for v in values]

    bar_colors = [
        "#4CAF50" if v > 0 else ("#F44336" if v < 0 else "#FFC107")
        for v in values
    ]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=sites,
            orientation="h",
            marker_color=bar_colors,
            text=text_labels,
            textposition="inside",
            insidetextanchor="middle",
            hovertemplate="%{y}<br>Δ Day HVC : %{x:+.2f}<extra></extra>",
        )
    )

    fig.update_layout(
        title=title,
        xaxis_title="Δ Day HVC",
        yaxis_title="",
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        margin=dict(l=10, r=20, t=50, b=30),
        height=400,
        showlegend=False,
    )
    fig.add_vline(x=0, line_width=1.2, line_color="#333")

    return fig

# ---------------------------------------------------------------------------
# Construction du tableau de variation
# ---------------------------------------------------------------------------

def _build_variation_display(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return pd.DataFrame({
        "Site": df.get("site_key", ""),
        "Day HVC T1": pd.to_numeric(df.get("day_hvc_t1", 0), errors="coerce").fillna(0).round(2),
        "Day HVC T2": pd.to_numeric(df.get("day_hvc_t2", 0), errors="coerce").fillna(0).round(2),
        "Δ Day HVC": pd.to_numeric(df.get("delta_day_hvc", 0), errors="coerce").fillna(0).round(2),
        "OOS T1 (%)": pd.to_numeric(df.get("oos_pct_t1", 0), errors="coerce").fillna(0).round(1),
        "OOS T2 (%)": pd.to_numeric(df.get("oos_pct_t2", 0), errors="coerce").fillna(0).round(1),
        "Δ OOS (pp)": pd.to_numeric(df.get("delta_oos_pct", 0), errors="coerce").fillna(0).round(1),
    }).sort_values("Δ Day HVC", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Styles tableau variation
# ---------------------------------------------------------------------------

def _day_hvc_cell_color(val: Any) -> str:
    try:
        v = float(val)
        if v >= _DAY_HVC_VERT:
            return "background-color: #d8f3dc"
        if v >= _DAY_HVC_JAUNE:
            return "background-color: #fff3bf"
        return "background-color: #ffd6d6"
    except Exception:
        return ""


def _delta_cell_color(val: Any) -> str:
    try:
        v = float(val)
        if v > 0:
            return "background-color: #d8f3dc"
        if v < 0:
            return "background-color: #ffd6d6"
        return ""
    except Exception:
        return ""


def _variation_styles() -> dict:
    return {
        "sheet_name": "Variations HVC",
        "formats": {
            "Day HVC T1": "0.00", "Day HVC T2": "0.00", "Δ Day HVC": "0.00",
            "OOS T1 (%)": "0.0", "OOS T2 (%)": "0.0", "Δ OOS (pp)": "0.0",
        },
        "rules": [
            {"columns": ["Day HVC T1", "Day HVC T2"], "op": ">=", "value": _DAY_HVC_VERT, "style": {"fill": "#d8f3dc"}},
            {"columns": ["Day HVC T1", "Day HVC T2"], "op": "between",
             "value": (_DAY_HVC_JAUNE, _DAY_HVC_VERT - 0.001), "style": {"fill": "#fff3bf"}},
            {"columns": ["Day HVC T1", "Day HVC T2"], "op": "<", "value": _DAY_HVC_JAUNE, "style": {"fill": "#ffd6d6"}},
            {"columns": ["Δ Day HVC"], "op": ">", "value": 0, "style": {"fill": "#d8f3dc"}},
            {"columns": ["Δ Day HVC"], "op": "<", "value": 0, "style": {"fill": "#ffd6d6"}},
        ],
    }


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _render_upload_section() -> None:
    st.subheader("Upload OOS")
    st.caption("Snapshot instantane des POS en rupture.")
    oos_file = st.file_uploader("Fichier OOS (.xlsx / .csv)", type=["xlsx", "csv"],
                                 key="oos_upload_file", accept_multiple_files=False)
    if oos_file and st.button("Ingerer OOS", key="oos_ingest_btn"):
        with st.spinner("Ingestion OOS..."):
            result = sync_oos_to_sqlite(oos_file)
        if result.get("error"):
            st.error(f"Erreur : {result['error']}")
        else:
            st.success(f"{result.get('lignes_inserees', 0)} lignes | snapshot : {result.get('snapshot_date')}")

    st.markdown("---")
    st.subheader("Upload HVC Variations")
    st.caption("Chaque fichier constitue un snapshot horodate. Minimum 2 pour comparer.")
    hvc_files = st.file_uploader("Fichier(s) HVC (.xlsx / .csv)", type=["xlsx", "csv"],
                                   key="hvc_upload_files", accept_multiple_files=True)
    if hvc_files and st.button("Ingerer HVC", key="hvc_ingest_btn"):
        with st.spinner(f"Ingestion de {len(hvc_files)} fichier(s)..."):
            results = [sync_hvc_variation_to_sqlite(f) for f in hvc_files]
        errors = [r.get("error") for r in results if r.get("error")]
        inserted = sum(r.get("lignes_inserees", 0) for r in results)
        if errors:
            st.error(f"Erreurs : {'; '.join(str(e) for e in errors)}")
        if inserted:
            st.success(f"{inserted} lignes inserees sur {len(hvc_files)} fichier(s).")