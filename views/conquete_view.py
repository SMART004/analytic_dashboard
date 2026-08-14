"""views/conquete_view.py

Page Conquête de Territoire — Vue BI Mobile Money.

Structure (5 onglets):
  1. 📊 Synthèse & KPIs     — Métriques globales, ratio commerciaux actifs, alertes
  2. 🗺️ Empiètement        — Tableau détail + matrice Zone SA Commercial vs PDV
  3. 📈 Évolution          — Courbe quotidienne POS touchés / Volume / Commerciaux
  4. 🎯 Tableau Stratégique — Matrice de décision par Zone SA (9 KPIs)
  5. 👥 Portefeuille        — Productivité individuelle des commerciaux
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from controllers.conquete_controller import (
    ConqueteContext, ConqueteFilters,
    build_conquete_context, load_filter_options, _build_portfolio_enriched, _attach_segment_from_ref
)
from services.export_service import to_csv, to_excel


# ---------------------------------------------------------------------------
# Constantes visuelles
# ---------------------------------------------------------------------------
_SEUIL_COV_VERT  = 70.0    # % couverture — vert
_SEUIL_COV_ROUGE = 50.0    # % couverture — rouge
_SEUIL_EMP_ROUGE = 20.0    # % empiètement — rouge

# Libellés invalides à masquer dans tous les graphes et tableaux
_INVALID_LABELS = {"none", "non renseigné", "non renseigne", "n/a", "", "null", "nan",
                   "non renseigne", "NON RENSEIGNE"}


def _is_valid_label(val: Any) -> bool:
    """Retourne True si la valeur est un libellé non vide et valide."""
    return str(val).strip().lower() not in {s.lower() for s in _INVALID_LABELS}


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def render_conquete_hub() -> None:
    st.title("🏆 Conquête de Territoire")

    with st.expander("ℹ️ Source de données", expanded=False):
        st.info(
            "Conquête lit la table **transactions** (type = Transfer, montant ≥ 10 000 FCFA). "
            "Les KPIs sont calculés en croisant avec **referentiel_pos** (total POS) et "
            "**referentiel_commerciaux** (total commerciaux enregistrés)."
        )

    filters = render_conquete_filters()
    context = build_conquete_context(filters)

    tabs = st.tabs([
        "📊 Synthèse & KPIs",
        "🗺️ Empiètement",
        "📈 Évolution",
        "🎯 Tableau Stratégique",
        "👥 Portefeuille Commercial",
    ])

    with tabs[0]:
        _render_overview(context)
    with tabs[1]:
        _render_encroachment(context)
    with tabs[2]:
        _render_evolution(context)
    with tabs[3]:
        _render_strategic_table(context)
    with tabs[4]:
        _render_commercial_portfolio(context)


# ---------------------------------------------------------------------------
# Filtres sidebar
# ---------------------------------------------------------------------------

def render_conquete_filters() -> ConqueteFilters:
    options = load_filter_options()
    min_date = _parse_date(options.get("min_date"))
    max_date = _parse_date(options.get("max_date")) or min_date

    with st.sidebar:
        if min_date and max_date:
            selected_dates = st.date_input(
                "Période",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                key="conquete_date_filter",
            )
        else:
            selected_dates = None
        start_date, end_date = _date_range_to_strings(selected_dates)

        zone_centre   = st.selectbox("Centre",     ["Toutes"] + list(options.get("zone_centre", [])),    key="conquete_centre")
        zone_territoire = st.selectbox("Territoire", ["Toutes"] + list(options.get("zone_territoire", [])), key="conquete_territoire")
        zone_sa       = st.selectbox("Zone SA",    ["Toutes"] + list(options.get("zone_sa", [])),        key="conquete_zone_sa")
        segment_options = ["Tous"] + list(options.get("segment", []))
        seg_idx       = next((i for i, s in enumerate(segment_options) if str(s).strip().upper() == "1-HVC"), 0)
        segment       = st.selectbox("Segment",    segment_options, index=seg_idx,                     key="conquete_segment")
        type_empietement = st.selectbox("Empiètement", ["Tous", "Intra-centre", "Inter-centre"],          key="conquete_type_empietement")

    return ConqueteFilters(
        start_date=start_date,
        end_date=end_date,
        zone_centre=zone_centre,
        zone_territoire=zone_territoire,
        zone_sa=zone_sa,
        segment=segment,
        type_empietement=type_empietement,
    )


# ---------------------------------------------------------------------------
# Onglet 1 — Synthèse & KPIs
# ---------------------------------------------------------------------------

def _render_overview(context: ConqueteContext) -> None:
    if context.is_empty:
        st.warning(context.message)
        return

    kpis = context.kpis

    # ── Ligne 1 : KPIs principaux ──────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Transactions", f"{kpis.get('nb_transactions', 0):,}".replace(",", " "))
    c2.metric("Volume distribué", _format_fcfa(kpis.get("volume", 0)))
    c3.metric("Panier moyen / TX", _format_fcfa(kpis.get("panier_moyen", 0)))
    c4.metric(
        "POS touchés / Total",
        f"{kpis.get('touched_pos', 0):,} / {kpis.get('total_pos_ref', 0):,}".replace(",", " ")
    )
    taux_cov = kpis.get("taux_couverture", 0.0)
    delta_cov_color = "normal" if taux_cov >= _SEUIL_COV_VERT else "inverse"
    c5.metric("Taux de couverture", f"{taux_cov:.1f}%")

    # ── Ligne 2 : Commerciaux + Empiètement ───────────────────────────────
    c6, c7, c8, c9 = st.columns(4)
    touched = kpis.get("touched_commerciaux", 0)
    total_c = kpis.get("total_commerciaux_ref", 0)
    c6.metric(
        "Commerciaux actifs",
        f"{touched}",
        delta=f"sur {total_c} enregistrés" if total_c else None,
        delta_color="off",
        help="Commerciaux ayant effectué ≥1 Transfer ≥10 000 FCFA dans la période."
    )
    c7.metric("POS non touchés", f"{kpis.get('pos_non_touches', 0):,}".replace(",", " "))
    c8.metric("Empiètement Intra", f"{kpis.get('empietement_intra', 0):.1f}%")
    c9.metric("Empiètement Inter", f"{kpis.get('empietement_inter', 0):.1f}%")

    # ── Alertes BI ─────────────────────────────────────────────────────────
    st.markdown("---")

    alerts = []
    if taux_cov < _SEUIL_COV_ROUGE:
        alerts.append(f"⚠️ **Couverture critique** : {taux_cov:.1f}% — plus de la moitié des POS non touchés.")
    if kpis.get("empietement_intra", 0) + kpis.get("empietement_inter", 0) >= _SEUIL_EMP_ROUGE:
        alerts.append("⚠️ **Taux d'empiètement élevé** — des commerciaux opèrent hors de leur zone.")
    if total_c > 0 and touched / total_c < 0.5:
        alerts.append(f"⚠️ **Mobilisation faible** : seulement {touched}/{total_c} commerciaux actifs ({touched/total_c*100:.0f}%).")
    for a in alerts:
        st.warning(a)
    if not alerts:
        st.success("✅ Tous les indicateurs sont dans les normes pour la période sélectionnée.")

    # ── Tableau raw (top 100) ──────────────────────────────────────────────
    with st.expander("📋 Transactions brutes (top 100)", expanded=False):
        st.dataframe(context.transactions.head(100), use_container_width=True, hide_index=True)
        _render_exports("conquete_overview", context.transactions.head(500), {})


# ---------------------------------------------------------------------------
# Onglet 2 — Empiètement
# ---------------------------------------------------------------------------

def _render_encroachment(context: ConqueteContext) -> None:
    if context.is_empty:
        st.warning(context.message)
        return

    st.subheader("Détail des cas d'empiètement")
    df = context.encroachment
    if df.empty:
        st.info("✅ Aucun cas d'empiètement détecté sur la période.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        _render_exports("conquete_empietement", df, {})

    # ── Matrice d'empiètement ─────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🔥 Matrice d'empiètement par Zone SA")
    matrix = context.encroachment_matrix
    if matrix.empty:
        st.info("Aucune matrice disponible (données insuffisantes).")
    else:
        # Pivot pour heatmap — filtrer les Zone SA invalides
        try:
            m = matrix.copy()
            if "Zone_SA_Commercial" in m.columns:
                m = m[m["Zone_SA_Commercial"].apply(_is_valid_label)]
            if "Zone_SA_PDV" in m.columns:
                m = m[m["Zone_SA_PDV"].apply(_is_valid_label)]
            pivot = m.pivot_table(
                index="Zone_SA_Commercial",
                columns="Zone_SA_PDV",
                values="Nb_Cas",
                fill_value=0,
                aggfunc="sum",
            )
            fig = go.Figure(go.Heatmap(
                z=pivot.values.tolist(),
                x=pivot.columns.tolist(),
                y=pivot.index.tolist(),
                colorscale="YlOrRd",
                hoverongaps=False,
                text=pivot.values.tolist(),
                texttemplate="%{text}",
                showscale=True,
            ))
            fig.update_layout(
                title="Nombre de cas d'empiètement : Zone SA Commercial (axe Y) → Zone SA PDV (axe X)",
                xaxis_title="Zone SA du PDV visité",
                yaxis_title="Zone SA du Commercial",
                height=max(350, len(pivot.index) * 40),
                margin={"t": 60, "b": 40},
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            st.dataframe(matrix, use_container_width=True, hide_index=True)

        with st.expander("📋 Données brutes de la matrice"):
            st.dataframe(matrix, use_container_width=True, hide_index=True)
            _render_exports("conquete_matrice_empietement", matrix, {})


# ---------------------------------------------------------------------------
# Onglet 3 — Évolution
# ---------------------------------------------------------------------------

def _render_evolution(context: ConqueteContext) -> None:
    if context.is_empty:
        st.warning(context.message)
        return

    df = context.evolution_series
    if df.empty or "date" not in df.columns:
        st.info("Aucune série temporelle disponible pour cette sélection.")
        return

    st.subheader("📈 Évolution quotidienne des POS touchés & du volume")

    # ── Graphe POS touchés vs Commerciaux actifs ──────────────────────────
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(
        x=df["date"], y=df["pos_touches"],
        mode="lines+markers", name="POS touchés",
        line={"color": "#3b82f6", "width": 2},
        marker={"size": 5},
    ))
    fig1.add_trace(go.Scatter(
        x=df["date"], y=df["commerciaux_actifs"],
        mode="lines+markers", name="Commerciaux actifs",
        line={"color": "#10b981", "width": 2, "dash": "dot"},
        marker={"size": 5},
        yaxis="y2",
    ))
    fig1.update_layout(
        title="POS touchés et Commerciaux actifs par jour",
        xaxis_title="Date",
        yaxis={"title": "POS touchés", "side": "left"},
        yaxis2={"title": "Commerciaux actifs", "overlaying": "y", "side": "right", "showgrid": False},
        legend={"orientation": "h", "y": -0.2},
        height=380,
    )
    st.plotly_chart(fig1, use_container_width=True)

    # ── Graphe Volume distribué ────────────────────────────────────────────
    fig2 = go.Figure(go.Bar(
        x=df["date"], y=df["volume"],
        name="Volume (FCFA)",
        marker_color="#8b5cf6",
        opacity=0.85,
    ))
    fig2.update_layout(
        title="Volume distribué par jour (FCFA)",
        xaxis_title="Date",
        yaxis_title="Volume (FCFA)",
        height=300,
    )
    st.plotly_chart(fig2, use_container_width=True)

    # ── Graphe Nb Transactions ─────────────────────────────────────────────
    fig3 = px.area(
        df, x="date", y="nb_transactions",
        title="Nombre de transactions par jour",
        labels={"nb_transactions": "Transactions", "date": "Date"},
        color_discrete_sequence=["#f59e0b"],
        height=280,
    )
    st.plotly_chart(fig3, use_container_width=True)

    with st.expander("📋 Données brutes de l'évolution"):
        st.dataframe(df, use_container_width=True, hide_index=True)
        _render_exports("conquete_evolution", df, {})


# ---------------------------------------------------------------------------
# Onglet 4 — Tableau Stratégique BI
# ---------------------------------------------------------------------------

def _render_strategic_table(context: ConqueteContext) -> None:
    if context.is_empty:
        st.warning(context.message)
        return

    st.subheader("🎯 Tableau Stratégique par Zone SA")
    st.caption(
        "Vue décisionnelle par Zone SA — 9 indicateurs clés Mobile Money : "
        "couverture POS, volume, productivité commerciale, empiètement."
    )

    df = context.strategic_table
    if df.empty:
        st.info("Aucune donnée stratégique disponible.")
        return

    # Mise en forme conditionnelle
    def _style_coverage(val: Any) -> str:
        try:
            v = float(val)
            if v >= _SEUIL_COV_VERT:
                return "background-color:#d8f3dc;color:#155724;font-weight:bold"
            if v >= _SEUIL_COV_ROUGE:
                return "background-color:#fff3bf;color:#7d6608"
            return "background-color:#ffd6d6;color:#9C0006;font-weight:bold"
        except Exception:
            return ""

    def _style_emp(val: Any) -> str:
        try:
            v = int(val)
            if v >= 10:
                return "background-color:#ffd6d6;color:#9C0006;font-weight:bold"
            if v >= 3:
                return "background-color:#fff3bf"
            return ""
        except Exception:
            return ""

    # Formats lisibles
    disp = df.copy()
    if "Volume_FCFA" in disp.columns:
        disp["Volume_FCFA"] = disp["Volume_FCFA"].apply(lambda x: f"{int(x):,}".replace(",", " ") + " F")
    if "Panier_Moyen_FCFA" in disp.columns:
        disp["Panier_Moyen_FCFA"] = disp["Panier_Moyen_FCFA"].apply(lambda x: f"{int(x):,}".replace(",", " ") + " F")
    if "Productivite_Moy_FCFA" in disp.columns:
        disp["Productivite_Moy_FCFA"] = disp["Productivite_Moy_FCFA"].apply(lambda x: f"{int(x):,}".replace(",", " ") + " F")

    has_cov = "Taux_Couverture_pct" in df.columns
    has_emp = "Nb_Empiétements" in df.columns

    styled = disp.style
    if has_cov:
        styled = styled.applymap(_style_coverage, subset=["Taux_Couverture_pct"])
    if has_emp:
        styled = styled.applymap(_style_emp, subset=["Nb_Empiétements"])


    # Graphiques de synthèse stratégique
    col1, col2 = st.columns(2)
    with col1:
        if "Zone_SA" in df.columns and "Taux_Couverture_pct" in df.columns:
            # Filtrer les Zone_SA invalides
            fig_df = df[df["Zone_SA"].apply(_is_valid_label)].head(20)
            fig = px.bar(
                fig_df,
                x="Zone_SA", y="Taux_Couverture_pct",
                color="Taux_Couverture_pct",
                color_continuous_scale=["#ef4444", "#f59e0b", "#22c55e"],
                range_color=[0, 100],
                title="Taux de couverture par Zone SA (%)",
                labels={"Zone_SA": "", "Taux_Couverture_pct": "Couverture (%)"},
                height=350,
            )
            fig.add_hline(y=_SEUIL_COV_VERT, line_dash="dot", line_color="green",
                          annotation_text=f"Seuil vert ({_SEUIL_COV_VERT}%)")
            fig.add_hline(y=_SEUIL_COV_ROUGE, line_dash="dot", line_color="red",
                          annotation_text=f"Seuil rouge ({_SEUIL_COV_ROUGE}%)")
            fig.update_xaxes(tickangle=-30)
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        if "Zone_SA" in df.columns and "Productivite_Moy_FCFA" in df.columns:
            fig2_df = df[df["Zone_SA"].apply(_is_valid_label)].head(20)
            fig2 = px.bar(
                fig2_df.sort_values("Productivite_Moy_FCFA", ascending=True),
                x="Productivite_Moy_FCFA", y="Zone_SA",
                orientation="h",
                title="Productivité moyenne par commercial (FCFA/Zone SA)",
                labels={"Productivite_Moy_FCFA": "Volume/Commercial (FCFA)", "Zone_SA": ""},
                color="Productivite_Moy_FCFA",
                color_continuous_scale="Blues",
                height=350,
            )
            st.plotly_chart(fig2, use_container_width=True)

    # Filtrer le tableau affiché
    disp_df = disp[disp["Zone_SA"].apply(_is_valid_label)] if "Zone_SA" in disp.columns else disp
    st.dataframe(styled if not ("Zone_SA" in disp.columns) else
                 disp_df.style.pipe(lambda s: s.applymap(_style_coverage, subset=["Taux_Couverture_pct"]) if has_cov else s)
                             .pipe(lambda s: s.applymap(_style_emp, subset=["Nb_Empiétements"]) if has_emp else s),
                 use_container_width=True, hide_index=True, height=520)
    _render_exports("conquete_tableau_strategique", df[df["Zone_SA"].apply(_is_valid_label)] if "Zone_SA" in df.columns else df, {})


# ---------------------------------------------------------------------------
# Onglet 5 — Portefeuille Commercial
# ---------------------------------------------------------------------------

# def _render_commercial_portfolio(context: ConqueteContext) -> None:
#     if context.is_empty:
#         st.warning(context.message)
#         return

#     df = context.commercial_portfolio
#     if df.empty:
#         st.info("Aucun portefeuille commercial disponible.")
#         return

#     st.subheader("👥 Productivité des Commerciaux")

#     # Top 10 / Flop 10 par volume — filtrer les noms invalides
#     if "Commercial" in df.columns and "Volume" in df.columns:
#         by_comm = (
#             df[df["Commercial"].apply(_is_valid_label)]  # exclure non renseigné/None
#             .groupby("Commercial")
#             .agg(Volume=("Volume", "sum"), PDV_Touches=("PDV_Touches", "sum"),
#                  Transactions=("Transactions", "sum"))
#             .reset_index()
#             .sort_values("Volume", ascending=False)
#         )

#         col1, col2 = st.columns(2)
#         with col1:
#             top10 = by_comm.head(10)
#             fig = px.bar(
#                 top10.sort_values("Volume"),
#                 x="Volume", y="Commercial", orientation="h",
#                 title="🏆 Top 10 — Volume distribué (FCFA)",
#                 color="Volume", color_continuous_scale="Greens",
#                 height=350, labels={"Volume": "Volume (FCFA)", "Commercial": ""},
#             )
#             st.plotly_chart(fig, use_container_width=True)

#         with col2:
#             flop10 = by_comm.tail(10).sort_values("Volume")
#             fig2 = px.bar(
#                 flop10,
#                 x="Volume", y="Commercial", orientation="h",
#                 title="⚠️ Flop 10 — Volume distribué (FCFA)",
#                 color="Volume", color_continuous_scale="Reds",
#                 height=350, labels={"Volume": "Volume (FCFA)", "Commercial": ""},
#             )
#             st.plotly_chart(fig2, use_container_width=True)

#     # Filtrer le tableau des commerciaux
#     disp_df = df[df["Commercial"].apply(_is_valid_label)] if "Commercial" in df.columns else df
#     st.dataframe(disp_df, use_container_width=True, hide_index=True, height=480)
#     _render_exports("conquete_portefeuille", disp_df, {})

SESSION_KEY_MANUAL_MAP = "conquete_manual_pos_mapping"  # DataFrame ou None


def _render_mapping_controls(assigned_auto: pd.DataFrame) -> pd.DataFrame:
    """
    Retourne le DataFrame d'attribution à utiliser :
    - mapping manuel si chargé
    - sinon attribution automatique
    """
    st.markdown("#### Attribution POS ↔ Commercial")

    col_a, col_b, col_c = st.columns([2, 1, 1])

    with col_a:
        up = st.file_uploader(
            "Mapping manuel (Excel/CSV) — format à confirmer",
            type=["xlsx", "xls", "csv"],
            key="conquete_manual_map_upload",
            help="Sera appliqué à la place de l'attribution automatique (≥3 TX).",
        )
        if up is not None:
            try:
                if up.name.lower().endswith(".csv"):
                    manual = pd.read_csv(up)
                else:
                    manual = pd.read_excel(up)
                manual.columns = [str(c).strip() for c in manual.columns]
                st.session_state[SESSION_KEY_MANUAL_MAP] = manual
                st.success(f"Mapping manuel chargé : {len(manual)} lignes")
            except Exception as e:
                st.error(f"Erreur lecture mapping : {e}")

    with col_b:
        if st.button("🔄 Revenir à l'attribution auto", key="reset_manual_map"):
            st.session_state[SESSION_KEY_MANUAL_MAP] = None
            st.rerun()

    with col_c:
        # Export Excel du mapping automatique
        export_auto = assigned_auto.rename(columns={
            "Commercial": "Commercial",
            "Commercial_MSISDN": "MSISDN_Commercial",
            "To_clean": "MSISDN_POS",
            "tx_count": "Nb_Transactions",
            "Category": "Segment",
        })
        cols = [c for c in [
            "Commercial", "MSISDN_Commercial", "MSISDN_POS", "Segment", "Nb_Transactions"
        ] if c in export_auto.columns]
        st.download_button(
            "📥 Excel mapping auto",
            to_excel(export_auto[cols]),
            "mapping_auto_pos_commerciaux.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_auto_map",
        )

    manual = st.session_state.get(SESSION_KEY_MANUAL_MAP)
    if manual is not None and isinstance(manual, pd.DataFrame) and not manual.empty:
        st.info("Mode **mapping manuel** actif.")
        ref_comm = None
        try:
            from models.db import get_connection
            conn = get_connection()
            ref_comm = pd.read_sql_query(
                "SELECT ccial_msisdn, nom_ccial FROM referentiel_commerciaux",
                conn,
            )
            conn.close()
        except Exception:
            pass
        normalized = _normalize_manual_mapping(manual, ref_comm=ref_comm)
        if not normalized.empty:
            return normalized
        st.caption(
            f"Debug mapping | lignes={len(normalized)} | "
            f"col Numero trouvée | "
            f"MSISDN non-N/A={(normalized['MSISDN_affiche'] != 'N/A').sum()} | "
            f"exemples={normalized['MSISDN_affiche'].head(3).tolist()}"
        )

    return assigned_auto


# def _normalize_manual_mapping(df: pd.DataFrame) -> pd.DataFrame:
#     """
#     Normalise le fichier manuel vers le schéma assigned.
#     Colonnes attendues (flexibles) — tu confirmeras le format exact :
#       - MSISDN_Commercial / Ccial_MSISDN / Commercial_MSISDN
#       - MSISDN_POS / POS_MSISDN / To
#       - Commercial / Nom_Ccial (optionnel)
#       - Segment (optionnel)
#     """
#     from utils.helpers import clean_phone

#     col_comm_msisdn = next(
#         (c for c in df.columns if str(c).lower().replace(" ", "") in {
#             "msisdn_commercial", "ccial_msisdn", "commercial_msisdn", "from"
#         }),
#         None,
#     )
#     col_pos = next(
#         (c for c in df.columns if str(c).lower().replace(" ", "") in {
#             "msisdn_pos", "pos_msisdn", "to", "agent_msisdn"
#         }),
#         None,
#     )
#     col_name = next(
#         (c for c in df.columns if str(c).lower().replace(" ", "") in {
#             "commercial", "nom_ccial", "ccial_nom"
#         }),
#         None,
#     )
#     col_seg = next(
#         (c for c in df.columns if "segment" in str(c).lower()),
#         None,
#     )

#     if not col_comm_msisdn or not col_pos:
#         st.error("Mapping manuel : colonnes MSISDN commercial / MSISDN POS introuvables.")
#         return pd.DataFrame()

#     out = pd.DataFrame({
#         "Commercial_MSISDN": df[col_comm_msisdn].apply(clean_phone),
#         "To_clean": df[col_pos].apply(clean_phone),
#         "Commercial": df[col_name] if col_name else df[col_comm_msisdn],
#         "tx_count": 3,  # seuil respecté par construction manuelle
#         "Category": "Autres",
#     })
#     if col_seg:
#         seg = df[col_seg].astype(str).str.upper()
#         out.loc[seg.str.contains("HVC", na=False), "Category"] = "HVC"
#         out.loc[seg.str.contains("MVC", na=False), "Category"] = "MVC"
#         out.loc[seg.str.contains("LVC", na=False), "Category"] = "LVC"

#     out = out.dropna(subset=["Commercial_MSISDN", "To_clean"])
#     out = out.drop_duplicates(subset=["To_clean"], keep="first")
#     return out

# def _normalize_manual_mapping(df: pd.DataFrame, ref_comm: pd.DataFrame | None = None) -> pd.DataFrame:
#     """
#     Format fichier mapping :
#       AGENT MSISDN | ... | Commercial | Numero

#     - AGENT MSISDN = POS
#     - Commercial   = nom commercial (casse ignorée pour le match)
#     - Numero       = MSISDN commercial
#     - Segment      = toujours depuis referentiel_pos (plus tard via _attach_segment_from_ref)
#     """
#     from utils.helpers import clean_phone

#     df = df.copy()
#     df.columns = [str(c).strip() for c in df.columns]
#     # lookup sans espaces / underscore / casse
#     lookup = {
#         str(c).lower().replace(" ", "").replace("_", ""): c
#         for c in df.columns
#     }

#     def pick(*cands):
#         for c in cands:
#             k = c.lower().replace(" ", "").replace("_", "")
#             if k in lookup:
#                 return lookup[k]
#         return None

#     col_pos = pick("AGENT MSISDN", "Agent_MSISDN", "agent_msisdn", "POS_MSISDN", "MSISDN_POS")
#     col_name = pick("Commercial", "COMMERCIAL", "Nom_Ccial", "Ccial")
#     col_num = pick("Numero", "Numéro", "NUMERO", "MSISDN_Commercial", "Ccial_MSISDN")

#     if not col_pos:
#         st.error("Mapping : colonne 'AGENT MSISDN' introuvable.")
#         return pd.DataFrame()
#     if not col_name:
#         st.error("Mapping : colonne 'Commercial' introuvable.")
#         return pd.DataFrame()

#     out = pd.DataFrame({
#         "To_clean": df[col_pos].apply(clean_phone),
#         "Commercial": df[col_name].astype(str).str.strip(),
#     })

#     # Numero commercial
#     if col_num:
#         out["Commercial_MSISDN"] = df[col_num].apply(clean_phone)
#     else:
#         out["Commercial_MSISDN"] = None

#     # Nettoyage
#     out = out[out["To_clean"].notna() & (out["To_clean"].astype(str).str.len() > 0)]
#     out = out[out["Commercial"].notna() & ~out["Commercial"].str.lower().isin(
#         {"", "nan", "none", "n/a", "null"}
#     )]

#     # Si Numero vide → N/A (on garde le nom Commercial du fichier)
#     out["Commercial_MSISDN"] = out["Commercial_MSISDN"].apply(
#         lambda x: x if x and str(x).strip().lower() not in {"", "nan", "none", "none", "n/a"} else "N/A"
#     )

#     # Optionnel : si le numéro est dans le référentiel, on peut normaliser le nom
#     # mais la consigne est de garder le nom du fichier Commercial
#     if ref_comm is not None and not ref_comm.empty:
#         msisdn_c = next((c for c in ["ccial_msisdn", "Ccial_MSISDN"] if c in ref_comm.columns), None)
#         nom_c = next((c for c in ["nom_ccial", "Nom_Ccial"] if c in ref_comm.columns), None)
#         if msisdn_c:
#             known = set(
#                 ref_comm[msisdn_c].astype(str).map(clean_phone).dropna().tolist()
#             )
#             # Numéro présent dans le fichier mais PAS dans le référentiel → on laisse N/A
#             # si tu préfères garder le numéro fichier même hors référentiel, commente ce bloc :
#             mask_unknown = (
#                 (out["Commercial_MSISDN"] != "N/A")
#                 & ~out["Commercial_MSISDN"].isin(known)
#             )
#             # Consignes : "commerciaux qui n'auront de numero dans notre referentiel → N/A"
#             # Interprétation : si Numero n'est pas dans le référentiel, forcer N/A
#             out.loc[mask_unknown, "Commercial_MSISDN"] = "N/A"

#     out["tx_count"] = 3  # seuil respecté par construction manuelle
#     out["Category"] = "Autres"  # sera écrasé par _attach_segment_from_ref

#     # 1 POS → 1 commercial
#     out = out.drop_duplicates(subset=["To_clean"], keep="first")
#     return out.reset_index(drop=True)

def _normalize_manual_mapping(df: pd.DataFrame, ref_comm: pd.DataFrame | None = None) -> pd.DataFrame:
    from utils.helpers import clean_phone

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    lookup = {str(c).lower().replace(" ", "").replace("_", ""): c for c in df.columns}

    def pick(*cands):
        for c in cands:
            k = c.lower().replace(" ", "").replace("_", "")
            if k in lookup:
                return lookup[k]
        return None

    col_pos = pick("AGENT MSISDN", "Agent_MSISDN", "agent_msisdn", "POS_MSISDN")
    col_name = pick("Commercial", "COMMERCIAL", "Nom_Ccial")
    col_num = pick("Numero", "Numéro", "NUMERO", "MSISDN_Commercial", "Ccial_MSISDN")

    if not col_pos or not col_name:
        st.error("Mapping : colonnes 'AGENT MSISDN' et/ou 'Commercial' introuvables.")
        return pd.DataFrame()
    

    out = pd.DataFrame({
        "To_clean": df[col_pos].apply(clean_phone),
        "Commercial": df[col_name].astype(str).str.strip(),
    })

    # Numero fichier → clé interne (même hors référentiel)
    if col_num:
        out["Commercial_MSISDN"] = (
            df[col_num]
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

    # Clé de regroupement si pas de numéro
    out["Commercial_MSISDN"] = out["Commercial_MSISDN"].apply(
        lambda x: x if x and str(x).strip().lower() not in {"", "nan", "none", "n/a"} else None
    )
    # Si pas de numero → clé artificielle stable par nom
    out["Commercial_MSISDN"] = out.apply(
        lambda r: r["Commercial_MSISDN"]
        if r["Commercial_MSISDN"]
        else f"NAME::{str(r['Commercial']).strip().upper()}",
        axis=1,
    )

    # Affichage MSISDN : N/A si absent du référentiel
    known = set()
    if ref_comm is not None and not ref_comm.empty:
        msisdn_c = next((c for c in ["ccial_msisdn", "Ccial_MSISDN"] if c in ref_comm.columns), None)
        if msisdn_c:
            known = {
                clean_phone(x)
                for x in ref_comm[msisdn_c].tolist()
                if clean_phone(x)
            }

        # Affiche TOUJOURS le numero du fichier s'il existe
    out["MSISDN_affiche"] = out["Commercial_MSISDN"].apply(
        lambda x: "N/A" if (not x) or str(x).startswith("NAME::") else x
    )
    # Info seulement (ne masque plus le numero)
    out["Dans_referentiel"] = out["Commercial_MSISDN"].apply(
        lambda x: "Oui"
        if x and not str(x).startswith("NAME::") and (not known or x in known)
        else "Non"
    )

    out["tx_count"] = 3
    out["Category"] = "Autres"
    out = out.drop_duplicates(subset=["To_clean"], keep="first")
    return out.reset_index(drop=True)

# ---------------------------------------------------------------------------
# Onglet 5 — Portefeuille Commercial (Attribution intra-Zone SA + HVC/MVC/LVC)
# ---------------------------------------------------------------------------

def _render_commercial_portfolio(context: ConqueteContext) -> None:
    if context.is_empty:
        st.warning(context.message)
        return

    st.subheader("👥 Portefeuille Commercial & Attribution des POS")
    st.caption(
        "Attribution : commercial du **référentiel**, même Zone SA, ≥ 3 TX. "
        "Capilarité = POS attribués / 80. "
        "Reçu = Transfer Master/Caisse → Commercial. "
        "Remonté = Transfer Commercial → Master uniquement. "
        "Descendu = Transfer Commercial → POS."
    )

    df_tx = context.transactions.copy()
    if df_tx.empty:
        st.info("Aucune transaction disponible.")
        return

    required = ["Commercial", "Commercial_MSISDN", "To_clean"]
    if any(c not in df_tx.columns for c in required):
        st.warning(f"Colonnes manquantes : {[c for c in required if c not in df_tx.columns]}")
        return

    # Commerciaux confirmés uniquement
    df_tx = df_tx[
        df_tx["Commercial"].notna()
        & df_tx["Commercial"].astype(str).str.strip().ne("")
        & df_tx["Commercial"].apply(_is_valid_label)
    ].copy()
    if df_tx.empty:
        st.warning("Aucun commercial du référentiel dans les transactions.")
        return

    # Intra Zone SA
    # if "Zone_SA_Comm" in df_tx.columns and "Zone_SA_PDV" in df_tx.columns:
    #     df_tx = df_tx[
    #         df_tx["Zone_SA_Comm"].astype(str).str.strip().str.lower()
    #         == df_tx["Zone_SA_PDV"].astype(str).str.strip().str.lower()
    #     ]
    if df_tx.empty:
        st.warning("Aucune TX intra-Zone SA.")
        return

    group_cols = ["Commercial", "Commercial_MSISDN", "To_clean"]
    if "Segment_PDV" in df_tx.columns:
        group_cols.append("Segment_PDV")

    agg = {"tx_count": ("To_clean", "size")}
    if "Amount" in df_tx.columns:
        agg["volume_total"] = ("Amount", "sum")

    pairs = df_tx.groupby(group_cols, dropna=False).agg(**agg).reset_index()
    assignments = pairs[pairs["tx_count"] >= 3].copy()
    if assignments.empty:
        st.warning("Aucun POS avec ≥ 3 TX.")
        return

    assignments = assignments.sort_values(
        ["To_clean", "tx_count", "Commercial"], ascending=[True, False, True]
    )
    assigned = assignments.drop_duplicates(subset=["To_clean"], keep="first").copy()

        # Mapping manuel OU auto
    assigned = _render_mapping_controls(assigned)

    # if "Segment_PDV" in assigned.columns:
    #     seg = assigned["Segment_PDV"].astype(str).str.upper()
    #     assigned["Category"] = "Autres"
    #     assigned.loc[seg.str.contains("HVC", na=False), "Category"] = "HVC"
    #     assigned.loc[seg.str.contains("MVC", na=False), "Category"] = "MVC"
    #     assigned.loc[seg.str.contains("LVC", na=False), "Category"] = "LVC"
    # else:
    #     assigned["Category"] = "Autres"

    try:
        from models.pos_model import get_all_pos
        ref_df = get_all_pos()
    except Exception:
        ref_df = pd.DataFrame()

    # 2) Ensuite attacher les segments
    assigned = _attach_segment_from_ref(assigned, ref_df)

    enriched = _build_portfolio_enriched(
        assigned,
        df_tx,
        ref_df,
        start_date=context.filters.start_date,
        end_date=context.filters.end_date,
    )

    # KPIs
    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("Commerciaux", int(enriched["Commercial"].nunique()))
    m2.metric("POS attribués", int(enriched["POS_attribues"].sum()))
    m3.metric("Capilarité moy.", f"{enriched['Capilarite_pct'].mean():.1f}%")
    m4.metric("Couverture moy.", f"{enriched['Taux_couverture_pct'].mean():.1f}%")
    m5.metric("Total descendu", _format_fcfa(enriched["Montant_descendu"].sum()))
    m6.metric("Total reçu", _format_fcfa(enriched["Montant_recu"].sum()))
    m7.metric("Total remonté", _format_fcfa(enriched["Montant_remonte"].sum()))

    st.markdown("---")

    # Graphiques existants (Top 10 POS / segments) — optionnels, garder si déjà en place
    comparison = _build_monthly_portfolio_comparison(assigned, df_tx)
    _render_monthly_portfolio_comparison(comparison)

    col1, col2 = st.columns(2)
    with col1:
        top10 = enriched.head(10).sort_values("POS_attribues")
        fig1 = px.bar(
            top10, x="POS_attribues", y="Commercial", orientation="h",
            title="🏆 Top 10 — POS attribués",
            color="POS_attribues", color_continuous_scale="Viridis", height=360,
        )
        st.plotly_chart(fig1, use_container_width=True)
    with col2:
        melt = enriched.head(10).melt(
            id_vars=["Commercial"],
            value_vars=["Nb_HVC", "Nb_MVC", "Nb_LVC"],
            var_name="Segment", value_name="Nombre",
        )
        fig2 = px.bar(
            melt, x="Commercial", y="Nombre", color="Segment",
            title="HVC / MVC / LVC (Top 10)",
            color_discrete_map={"Nb_HVC": "#22c55e", "Nb_MVC": "#f59e0b", "Nb_LVC": "#ef4444"},
            barmode="stack", height=360,
        )
        fig2.update_xaxes(tickangle=-30)
        st.plotly_chart(fig2, use_container_width=True)

    # Tableau enrichi
    st.markdown("#### Portefeuille détaillé")
    disp = enriched.rename(columns={
        "MSISDN": "MSISDN",
        "POS_attribues": "POS attribués",
        "POS_servis": "POS servis",
        "Capilarite_pct": "Capilarité %",
        "Taux_couverture_pct": "Taux couverture %",
        "Montant_descendu": "Montant descendu",
        "POS_servis_hors_portefeuille": "POS servis (Hors Portefeuille)",
        "Montant_descendu_hors_portefeuille": "Montant descendu (Hors Portefeuille)",
        "Montant_recu": "Montant reçu",
        "Montant_remonte": "Montant remonté",
        "Heure_debut_moy": "Heure début moy.",
        "Heure_fin_moy": "Heure fin moy.",
        "Nb_TX_attrib": "TX attribuées",
        "Source_donnees": "Fichier Source",
    })
    for col in ["Montant descendu", "Montant reçu", "Montant remonté", "Montant descendu (Hors Portefeuille)"]:
        if col in disp.columns:
            disp[col] = disp[col].apply(_format_fcfa)

    st.dataframe(disp, use_container_width=True, hide_index=True, height=500)
    _render_exports("conquete_portefeuille_enrichi", enriched, {})

    with st.expander("📋 Détail POS attribués"):
        detail = assigned.rename(columns={
            "To_clean": "MSISDN POS",
            "tx_count": "Nb Transactions",
            "Category": "Segment",
        })
        cols = ["Commercial", "Commercial_MSISDN", "MSISDN POS", "Segment", "Nb Transactions"]
        st.dataframe(
            detail[[c for c in cols if c in detail.columns]],
            use_container_width=True,
            hide_index=True,
        )


# def _build_monthly_portfolio_comparison(assigned: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
#     if assigned.empty or tx.empty or "Date" not in tx.columns or "To_clean" not in tx.columns:
#         return pd.DataFrame()

#     work = tx.copy()
#     work["_date"] = pd.to_datetime(work["Date"], errors="coerce")
#     work = work[work["_date"].notna()].copy()
#     if work.empty:
#         return pd.DataFrame()

#     work["_month"] = work["_date"].dt.to_period("M")
#     work["_day"] = work["_date"].dt.day
#     month_cutoff = work.groupby("_month")["_day"].max()
#     if month_cutoff.empty or len(month_cutoff) < 2:
#         return pd.DataFrame()

#     common_day_limit = int(month_cutoff.min())
#     work = work[work["_day"] <= common_day_limit].copy()
#     if work.empty:
#         return pd.DataFrame()

#     assigned_work = assigned.copy()
#     assigned_work["_comm_key"] = assigned_work["Commercial_MSISDN"].astype(str)
#     assigned_work["_pos_key"] = assigned_work["To_clean"].astype(str)
#     pos_by_comm = (
#         assigned_work.groupby("_comm_key")["_pos_key"]
#         .apply(lambda values: set(values.dropna().astype(str)))
#         .to_dict()
#     )
#     all_portfolio_pos = set(assigned_work["_pos_key"].dropna().astype(str))
#     pos_attribues_total = int(assigned_work["_pos_key"].nunique())

#     work["_comm_key"] = work["Commercial_MSISDN"].astype(str) if "Commercial_MSISDN" in work.columns else ""
#     work["_pos_key"] = work["To_clean"].astype(str)
#     work["_amount"] = pd.to_numeric(work.get("Amount", 0), errors="coerce").fillna(0)
#     work["_in_portfolio"] = work.apply(
#         lambda row: row["_pos_key"] in pos_by_comm.get(row["_comm_key"], set()),
#         axis=1,
#     )
#     work["_outside_portfolio"] = ~work["_in_portfolio"] & ~work["_pos_key"].isin(all_portfolio_pos)

#     rows = []
#     for month, sub in work.groupby("_month", sort=True):
#         in_portfolio = sub[sub["_in_portfolio"]]
#         outside = sub[sub["_outside_portfolio"]]
#         pos_servis = int(in_portfolio["_pos_key"].nunique())
#         pos_hors = int(outside["_pos_key"].nunique())
#         montant_descendu = float(in_portfolio["_amount"].sum())
#         montant_hors = float(outside["_amount"].sum())
#         rows.append({
#             "Mois": month.to_timestamp().strftime("%Y-%m"),
#             "Jours compares": f"1-{common_day_limit}",
#             "POS servis portefeuille": pos_servis,
#             "POS servis hors portefeuille": pos_hors,
#             "POS servis total": pos_servis + pos_hors,
#             "Taux couverture (%)": round(pos_servis / pos_attribues_total * 100, 1) if pos_attribues_total else 0.0,
#             "Float descendu portefeuille": montant_descendu,
#             "Float descendu hors portefeuille": montant_hors,
#             "Float descendu total": montant_descendu + montant_hors,
#         })

#     return pd.DataFrame(rows)

# def _build_monthly_portfolio_comparison(assigned: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
#     if assigned.empty or tx.empty or "Date" not in tx.columns or "To_clean" not in tx.columns:
#         return pd.DataFrame()

#     work = tx.copy()
#     work["_date"] = pd.to_datetime(work["Date"], errors="coerce")
#     work = work[work["_date"].notna()].copy()
#     if work.empty:
#         return pd.DataFrame()

#     work["_month"] = work["_date"].dt.to_period("M")
#     work["_day"] = work["_date"].dt.day
#     month_cutoff = work.groupby("_month")["_day"].max()
#     if month_cutoff.empty or len(month_cutoff) < 2:
#         return pd.DataFrame()

#     # Alignement sur la fenêtre de jours commune (ex: 1 au 15 du mois)
#     common_day_limit = int(month_cutoff.min())
#     work = work[work["_day"] <= common_day_limit].copy()
#     if work.empty:
#         return pd.DataFrame()

#     assigned_work = assigned.copy()
#     assigned_work["_comm_key"] = assigned_work["Commercial_MSISDN"].astype(str)
#     assigned_work["_pos_key"] = assigned_work["To_clean"].astype(str)

#     # Cartographie des POS attribués par commercial
#     pos_by_comm = (
#         assigned_work.groupby("_comm_key")["_pos_key"]
#         .apply(lambda values: set(values.dropna().astype(str)))
#         .to_dict()
#     )
    
#     # Nombre de POS attribués par commercial
#     pos_attribues_by_comm = (
#         assigned_work.groupby(["Commercial", "_comm_key"])["_pos_key"]
#         .nunique()
#         .to_dict()
#     )
    
#     all_portfolio_pos = set(assigned_work["_pos_key"].dropna().astype(str))

#     work["_comm_key"] = work["Commercial_MSISDN"].astype(str) if "Commercial_MSISDN" in work.columns else ""
#     work["_pos_key"] = work["To_clean"].astype(str)
#     work["_amount"] = pd.to_numeric(work.get("Amount", 0), errors="coerce").fillna(0)
    
#     # Identification intra-portefeuille par commercial
#     work["_in_portfolio"] = work.apply(
#         lambda row: row["_pos_key"] in pos_by_comm.get(row["_comm_key"], set()),
#         axis=1,
#     )
#     work["_outside_portfolio"] = ~work["_in_portfolio"] & ~work["_pos_key"].isin(all_portfolio_pos)

#     rows = []
#     # Regroupement par Mois ET par Commercial
#     for (month, comm_key), sub in work.groupby(["_month", "_comm_key"], sort=True):
#         if not comm_key or comm_key not in pos_by_comm:
#             continue
            
#         # Nom du commercial
#         comm_name = sub["Commercial"].dropna().iloc[0] if "Commercial" in sub.columns and not sub["Commercial"].dropna().empty else comm_key
        
#         in_portfolio = sub[sub["_in_portfolio"]]
#         outside = sub[sub["_outside_portfolio"]]
        
#         pos_servis = int(in_portfolio["_pos_key"].nunique())
#         pos_hors = int(outside["_pos_key"].nunique())
#         montant_descendu = float(in_portfolio["_amount"].sum())
#         montant_hors = float(outside["_amount"].sum())
        
#         pos_attrib_comm = pos_attribues_by_comm.get((comm_name, comm_key), len(pos_by_comm.get(comm_key, set())))
#         taux_cov = round(pos_servis / pos_attrib_comm * 100, 1) if pos_attrib_comm > 0 else 0.0

#         rows.append({
#             "Mois": month.to_timestamp().strftime("%Y-%m"),
#             "Commercial": comm_name,
#             "Commercial_MSISDN": comm_key,
#             "Jours compares": f"1-{common_day_limit}",
#             "POS attribues": pos_attrib_comm,
#             "POS servis portefeuille": pos_servis,
#             "POS servis hors portefeuille": pos_hors,
#             "POS servis total": pos_servis + pos_hors,
#             "Taux couverture (%)": taux_cov,
#             "Float descendu portefeuille": montant_descendu,
#             "Float descendu hors portefeuille": montant_hors,
#             "Float descendu total": montant_descendu + montant_hors,
#         })

#     return pd.DataFrame(rows)

# def _build_monthly_portfolio_comparison(assigned: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
#     if assigned.empty or tx.empty or "Date" not in tx.columns or "To_clean" not in tx.columns:
#         return pd.DataFrame()

#     work = tx.copy()
#     work["_date"] = pd.to_datetime(work["Date"], errors="coerce")
#     work = work[work["_date"].notna()].copy()
#     if work.empty:
#         return pd.DataFrame()

#     work["_month"] = work["_date"].dt.to_period("M")
#     work["_day"] = work["_date"].dt.day
#     month_cutoff = work.groupby("_month")["_day"].max()
#     if month_cutoff.empty or len(month_cutoff) < 2:
#         return pd.DataFrame()

#     common_day_limit = int(month_cutoff.min())
#     work = work[work["_day"] <= common_day_limit].copy()
#     if work.empty:
#         return pd.DataFrame()

#     assigned_work = assigned.copy()
#     assigned_work["_comm_key"] = assigned_work["Commercial_MSISDN"].astype(str)
#     assigned_work["_pos_key"] = assigned_work["To_clean"].astype(str)

#     pos_by_comm = (
#         assigned_work.groupby("_comm_key")["_pos_key"]
#         .apply(lambda values: set(values.dropna().astype(str)))
#         .to_dict()
#     )
    
#     pos_attribues_by_comm = (
#         assigned_work.groupby(["Commercial", "_comm_key"])["_pos_key"]
#         .nunique()
#         .to_dict()
#     )
    
#     all_portfolio_pos = set(assigned_work["_pos_key"].dropna().astype(str))

#     work["_comm_key"] = work["Commercial_MSISDN"].astype(str) if "Commercial_MSISDN" in work.columns else ""
#     work["_pos_key"] = work["To_clean"].astype(str)
#     work["_amount"] = pd.to_numeric(work.get("Amount", 0), errors="coerce").fillna(0)
    
#     work["_in_portfolio"] = work.apply(
#         lambda row: row["_pos_key"] in pos_by_comm.get(row["_comm_key"], set()),
#         axis=1,
#     )
#     work["_outside_portfolio"] = ~work["_in_portfolio"] & ~work["_pos_key"].isin(all_portfolio_pos)

#     rows = []
#     for (month, comm_key), sub in work.groupby(["_month", "_comm_key"], sort=True):
#         if not comm_key or comm_key not in pos_by_comm:
#             continue
            
#         comm_name = sub["Commercial"].dropna().iloc[0] if "Commercial" in sub.columns and not sub["Commercial"].dropna().empty else comm_key
        
#         in_portfolio = sub[sub["_in_portfolio"]]
#         outside = sub[sub["_outside_portfolio"]]
        
#         pos_servis = int(in_portfolio["_pos_key"].nunique())
#         pos_hors = int(outside["_pos_key"].nunique())
#         montant_descendu = float(in_portfolio["_amount"].sum())
#         montant_hors = float(outside["_amount"].sum())
        
#         pos_attrib_comm = pos_attribues_by_comm.get((comm_name, comm_key), len(pos_by_comm.get(comm_key, set())))
#         taux_cov = round(pos_servis / pos_attrib_comm * 100, 1) if pos_attrib_comm > 0 else 0.0

#         rows.append({
#             "Mois": month.to_timestamp().strftime("%Y-%m"),
#             "Commercial": comm_name,
#             "Jours compares": f"1-{common_day_limit}",
#             "POS attribues": pos_attrib_comm,
#             "POS servis portefeuille": pos_servis,
#             "POS servis hors portefeuille": pos_hors,
#             "POS servis total": pos_servis + pos_hors,
#             "Taux couverture (%)": taux_cov,
#             "Float descendu portefeuille": montant_descendu,
#             "Float descendu hors portefeuille": montant_hors,
#             "Float descendu total": montant_descendu + montant_hors,
#         })

#     return pd.DataFrame(rows)

def _build_monthly_portfolio_comparison(
    assigned: pd.DataFrame, 
    tx: pd.DataFrame, 
    mapping_df: pd.DataFrame = None
) -> pd.DataFrame:
    if tx.empty or "Date" not in tx.columns or "To_clean" not in tx.columns:
        return pd.DataFrame()

    # --- 1. GESTION PRIORITAIRE DU MAPPING ---
    # Si le mapping existe, on l'utilise à la place du référentiel d'affectation
    if mapping_df is not None and not mapping_df.empty:
        assigned_work = mapping_df.copy()
    elif not assigned.empty:
        assigned_work = assigned.copy()
    else:
        return pd.DataFrame()

    # Normalisation des noms de colonnes dans le mapping (sécurité)
    # Cherche des équivalents si les noms exacts ne sont pas là
    col_map = {c.lower(): c for c in assigned_work.columns}
    
    # Détection dynamique du nom de commercial
    comm_col = next((col_map[c] for c in col_map if c in ["commercial", "agent", "nom_commercial", "commercial_name"]), "Commercial")
    # Détection dynamique du MSISDN commercial
    msisdn_col = next((col_map[c] for c in col_map if c in ["commercial_msisdn", "msisdn_agent", "msisdn_commercial", "msisdn"]), "Commercial_MSISDN")
    # Détection du POS / To_clean
    pos_col = next((col_map[c] for c in col_map if c in ["to_clean", "pos_msisdn", "msisdn_pos", "pos"]), "To_clean")

    # Nettoyage strict des clés (conversion en chaîne sans décimales .0)
    def _clean_key(series):
        return series.astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

    assigned_work["_comm_name"] = assigned_work[comm_col].astype(str).str.strip()
    assigned_work["_comm_key"] = _clean_key(assigned_work[msisdn_col])
    assigned_work["_pos_key"] = _clean_key(assigned_work[pos_col])

    # Mapping dicts
    pos_by_comm = (
        assigned_work.groupby("_comm_key")["_pos_key"]
        .apply(lambda v: set(v.dropna()))
        .to_dict()
    )
    
    pos_attribues_by_comm = (
        assigned_work.groupby(["_comm_name", "_comm_key"])["_pos_key"]
        .nunique()
        .to_dict()
    )
    
    # Table de correspondance MSISDN -> Nom du Commercial
    comm_name_map = (
        assigned_work.groupby("_comm_key")["_comm_name"]
        .last()
        .to_dict()
    )

    all_portfolio_pos = set(assigned_work["_pos_key"].dropna())

    # --- 2. TRAITEMENT DES TRANSACTIONS ---
    work = tx.copy()
    work["_date"] = pd.to_datetime(work["Date"], errors="coerce")
    work = work[work["_date"].notna()].copy()
    if work.empty:
        return pd.DataFrame()

    work["_month"] = work["_date"].dt.to_period("M")
    work["_day"] = work["_date"].dt.day
    month_cutoff = work.groupby("_month")["_day"].max()
    if month_cutoff.empty or len(month_cutoff) < 2:
        return pd.DataFrame()

    common_day_limit = int(month_cutoff.min())
    work = work[work["_day"] <= common_day_limit].copy()
    if work.empty:
        return pd.DataFrame()

    # Normalisation des clés dans transactions
    work["_comm_key"] = _clean_key(work["Commercial_MSISDN"]) if "Commercial_MSISDN" in work.columns else ""
    work["_pos_key"] = _clean_key(work["To_clean"])
    work["_amount"] = pd.to_numeric(work.get("Amount", 0), errors="coerce").fillna(0)

    # Réécriture du nom du commercial depuis le mapping
    work["_comm_name"] = work["_comm_key"].map(comm_name_map)
    
    # Fallback si le commercial n'est pas dans le mapping mais dans la TX
    if "Commercial" in work.columns:
        work["_comm_name"] = work["_comm_name"].fillna(work["Commercial"])

    work["_in_portfolio"] = work.apply(
        lambda row: row["_pos_key"] in pos_by_comm.get(row["_comm_key"], set()),
        axis=1,
    )
    work["_outside_portfolio"] = ~work["_in_portfolio"] & ~work["_pos_key"].isin(all_portfolio_pos)

    rows = []
    for (month, comm_key), sub in work.groupby(["_month", "_comm_key"], sort=True):
        if not comm_key or comm_key not in pos_by_comm:
            continue
            
        comm_name = sub["_comm_name"].dropna().iloc[0] if not sub["_comm_name"].dropna().empty else comm_key
        
        in_portfolio = sub[sub["_in_portfolio"]]
        outside = sub[sub["_outside_portfolio"]]
        
        pos_servis_p = int(in_portfolio["_pos_key"].nunique())
        pos_servis_hors = int(outside["_pos_key"].nunique())
        montant_p = float(in_portfolio["_amount"].sum())
        montant_hors = float(outside["_amount"].sum())
        
        pos_attrib_comm = pos_attribues_by_comm.get((comm_name, comm_key), len(pos_by_comm.get(comm_key, set())))
        taux_cov = round(pos_servis_p / pos_attrib_comm * 100, 1) if pos_attrib_comm > 0 else 0.0

        rows.append({
            "Mois": month.to_timestamp().strftime("%Y-%m"),
            "Commercial": comm_name,
            "Commercial_MSISDN": comm_key,
            "Jours compares": f"1-{common_day_limit}",
            "POS attribues": pos_attrib_comm,
            "POS servis portefeuille": pos_servis_p,
            "POS servis hors portefeuille": pos_servis_hors,
            "POS servis total": pos_servis_p + pos_servis_hors,
            "Taux couverture (%)": taux_cov,
            "Float descendu portefeuille": montant_p,
            "Float descendu hors portefeuille": montant_hors,
            "Float descendu total": montant_p + montant_hors,
        })

    return pd.DataFrame(rows)

# def _render_monthly_portfolio_comparison(df: pd.DataFrame) -> None:
#     st.markdown("#### Comparatif mensuel portefeuille")
#     if df.empty:
#         st.info("Selectionnez au moins deux mois dans le filtre de dates pour afficher le comparatif equilibre.")
#         return

#     day_window = df["Jours compares"].iloc[0] if "Jours compares" in df.columns else ""
#     if day_window:
#         st.caption(f"Comparaison equilibree sur les jours {day_window} de chaque mois selectionne.")

#     col1, col2 = st.columns(2)
#     with col1:
#         pos_melt = df.melt(
#             id_vars=["Mois"],
#             value_vars=["POS servis portefeuille", "POS servis hors portefeuille"],
#             var_name="Type",
#             value_name="POS servis",
#         )
#         fig_pos = px.bar(
#             pos_melt,
#             x="Mois",
#             y="POS servis",
#             color="Type",
#             barmode="group",
#             title="POS servis : portefeuille vs hors portefeuille",
#             color_discrete_map={
#                 "POS servis portefeuille": "#2563eb",
#                 "POS servis hors portefeuille": "#f59e0b",
#             },
#             height=360,
#         )
#         st.plotly_chart(fig_pos, use_container_width=True)

#     with col2:
#         fig_cov = px.line(
#             df,
#             x="Mois",
#             y="Taux couverture (%)",
#             markers=True,
#             title="Taux de couverture portefeuille",
#             height=360,
#         )
#         fig_cov.update_traces(line={"color": "#16a34a", "width": 3}, marker={"size": 8})
#         fig_cov.update_yaxes(range=[0, max(100, float(df["Taux couverture (%)"].max()) + 5)])
#         st.plotly_chart(fig_cov, use_container_width=True)

#     float_melt = df.melt(
#         id_vars=["Mois"],
#         value_vars=["Float descendu portefeuille", "Float descendu hors portefeuille"],
#         var_name="Type",
#         value_name="Float descendu",
#     )
#     fig_float = px.bar(
#         float_melt,
#         x="Mois",
#         y="Float descendu",
#         color="Type",
#         barmode="group",
#         title="Float descendu : portefeuille vs hors portefeuille",
#         color_discrete_map={
#             "Float descendu portefeuille": "#0f766e",
#             "Float descendu hors portefeuille": "#dc2626",
#         },
#         height=340,
#     )
#     st.plotly_chart(fig_float, use_container_width=True)

#     with st.expander("Donnees comparatives mensuelles"):
#         display = df.copy()
#         for col in ["Float descendu portefeuille", "Float descendu hors portefeuille", "Float descendu total"]:
#             display[col] = display[col].apply(_format_fcfa)
#         st.dataframe(display, use_container_width=True, hide_index=True)

# def _render_monthly_portfolio_comparison(df: pd.DataFrame) -> None:
#     st.markdown("#### 📊 Comparatif mensuel par commercial")
#     if df.empty:
#         st.info("Sélectionnez au moins deux mois dans le filtre de dates pour afficher le comparatif équilibré.")
#         return

#     day_window = df["Jours compares"].iloc[0] if "Jours compares" in df.columns else ""
#     if day_window:
#         st.caption(f"Comparaison équilibrée sur les jours {day_window} de chaque mois sélectionné.")

#     # Filtre par commercial
#     commerciaux_list = ["Tous les commerciaux"] + sorted(df["Commercial"].unique().tolist())
#     selected_comm = st.selectbox("Filtrer par commercial :", commerciaux_list, key="conquete_select_comm_portfolio")

#     df_view = df.copy()
#     if selected_comm != "Tous les commerciaux":
#         df_view = df_view[df_view["Commercial"] == selected_comm]
#     else:
#         # Agrégation globale si 'Tous' est sélectionné
#         df_view = df_view.groupby("Mois", as_index=False).agg({
#             "POS attribues": "sum",
#             "POS servis portefeuille": "sum",
#             "POS servis hors portefeuille": "sum",
#             "POS servis total": "sum",
#             "Float descendu portefeuille": "sum",
#             "Float descendu hors portefeuille": "sum",
#             "Float descendu total": "sum",
#         })
#         df_view["Taux couverture (%)"] = (
#             df_view["POS servis portefeuille"] / df_view["POS attribues"] * 100
#         ).round(1).fillna(0.0)

#     col1, col2 = st.columns(2)
#     with col1:
#         pos_melt = df_view.melt(
#             id_vars=["Mois"],
#             value_vars=["POS servis portefeuille", "POS servis hors portefeuille"],
#             var_name="Type",
#             value_name="POS servis",
#         )
#         fig_pos = px.bar(
#             pos_melt,
#             x="Mois",
#             y="POS servis",
#             color="Type",
#             barmode="group",
#             title=f"POS servis : Portefeuille vs Hors Portefeuille ({selected_comm})",
#             color_discrete_map={
#                 "POS servis portefeuille": "#2563eb",
#                 "POS servis hors portefeuille": "#f59e0b",
#             },
#             height=360,
#         )
#         st.plotly_chart(fig_pos, use_container_width=True)

#     with col2:
#         fig_cov = px.line(
#             df_view,
#             x="Mois",
#             y="Taux couverture (%)",
#             markers=True,
#             title="Évolution du taux de couverture (%)",
#             height=360,
#         )
#         fig_cov.update_traces(line={"color": "#16a34a", "width": 3}, marker={"size": 8})
#         fig_cov.update_yaxes(range=[0, max(100, float(df_view["Taux couverture (%)"].max() or 0) + 5)])
#         st.plotly_chart(fig_cov, use_container_width=True)

#     float_melt = df_view.melt(
#         id_vars=["Mois"],
#         value_vars=["Float descendu portefeuille", "Float descendu hors portefeuille"],
#         var_name="Type",
#         value_name="Float descendu",
#     )
#     fig_float = px.bar(
#         float_melt,
#         x="Mois",
#         y="Float descendu",
#         color="Type",
#         barmode="group",
#         title="Volume descendu (FCFA) : Portefeuille vs Hors Portefeuille",
#         color_discrete_map={
#             "Float descendu portefeuille": "#0f766e",
#             "Float descendu hors portefeuille": "#dc2626",
#         },
#         height=340,
#     )
#     st.plotly_chart(fig_float, use_container_width=True)

#     with st.expander("📋 Détail des données comparatives par commercial"):
#         display = df_view.copy()
#         for col in ["Float descendu portefeuille", "Float descendu hors portefeuille", "Float descendu total"]:
#             if col in display.columns:
#                 display[col] = display[col].apply(_format_fcfa)
#         st.dataframe(display, use_container_width=True, hide_index=True)

# def _render_monthly_portfolio_comparison(df: pd.DataFrame) -> None:
#     st.markdown("#### 📊 Comparatif d'Évolution de tous les Commerciaux")
#     if df.empty:
#         st.info("Sélectionnez au moins deux mois dans le filtre de dates pour afficher le comparatif.")
#         return

#     day_window = df["Jours compares"].iloc[0] if "Jours compares" in df.columns else ""
#     if day_window:
#         st.caption(f"Comparaison équilibrée basée sur les jours {day_window} de chaque mois.")

#     # 1. Graphe POS Servis en portefeuille (Comparaison côte à côte)
#     fig_pos = px.bar(
#         df,
#         x="Commercial",
#         y="POS servis portefeuille",
#         color="Mois",
#         barmode="group",
#         title="POS Portefeuille Servis par Commercial (Évolution mensuelle)",
#         height=380,
#         text_auto=True,
#     )
#     st.plotly_chart(fig_pos, use_container_width=True)

#     col1, col2 = st.columns(2)
#     with col1:
#         # 2. Graphe Taux de couverture (%) aligné
#         fig_cov = px.bar(
#             df,
#             x="Commercial",
#             y="Taux couverture (%)",
#             color="Mois",
#             barmode="group",
#             title="Taux de Couverture Portefeuille (%)",
#             height=360,
#             text_auto=".1f",
#         )
#         fig_cov.update_yaxes(range=[0, 100])
#         st.plotly_chart(fig_cov, use_container_width=True)

#     with col2:
#         # 3. Graphe Float descendu portefeuille aligné
#         fig_float = px.bar(
#             df,
#             x="Commercial",
#             y="Float descendu portefeuille",
#             color="Mois",
#             barmode="group",
#             title="Float Descendu Portefeuille (FCFA)",
#             height=360,
#         )
#         st.plotly_chart(fig_float, use_container_width=True)

#     # 4. Tableau comparatif aligné (Matrice Commerciaux x Mois)
#     st.markdown("##### 📋 Tableau Comparatif Global")
    
#     # Formatage propre du tableau pour affichage direct de l'ensemble des commerciaux
#     display_df = df.sort_values(by=["Commercial", "Mois"]).copy()
    
#     for col in ["Float descendu portefeuille", "Float descendu hors portefeuille", "Float descendu total"]:
#         if col in display_df.columns:
#             display_df[col] = display_df[col].apply(_format_fcfa)

#     st.dataframe(
#         display_df[[
#             "Commercial", 
#             "Mois", 
#             "POS attribues", 
#             "POS servis portefeuille", 
#             "Taux couverture (%)", 
#             "POS servis hors portefeuille",
#             "Float descendu portefeuille", 
#             "Float descendu hors portefeuille"
#         ]], 
#         use_container_width=True, 
#         hide_index=True
#     )

import io
import pandas as pd
import streamlit as st

def _render_monthly_portfolio_comparison(df: pd.DataFrame) -> None:
    st.markdown("#### 📊 Tableau de Bord Comparatif & Performance Commerciale")
    
    if df.empty or df["Mois"].nunique() < 2:
        st.info("Sélectionnez au moins deux mois complets dans vos données pour générer la comparaison BI.")
        return

    day_window = df["Jours compares"].iloc[0] if "Jours compares" in df.columns else ""
    if day_window:
        st.caption(f"ℹ️ Périmètre équitable : Analyse basée sur les jours **1 à {day_window.split('-')[-1]}** de chaque mois.")

    # --- 1. PRÉPARATION DES DONNÉES PIVOTÉES ---
    mois_dispos = sorted(df["Mois"].unique())
    m_base, m_recent = mois_dispos[0], mois_dispos[-1]

    df_base = df[df["Mois"] == m_base].set_index("Commercial")
    df_recent = df[df["Mois"] == m_recent].set_index("Commercial")

    commerciaux = sorted(list(set(df_base.index).union(set(df_recent.index))))

    raw_rows = []
    for c in commerciaux:
        pos_b = int(df_base.loc[c, "POS servis total"]) if c in df_base.index else 0
        pos_r = int(df_recent.loc[c, "POS servis total"]) if c in df_recent.index else 0
        diff_pos = pos_r - pos_b
        pct_pos = round((diff_pos / pos_b * 100), 1) if pos_b > 0 else 0.0

        cov_b = float(df_base.loc[c, "Taux couverture (%)"]) if c in df_base.index else 0.0
        cov_r = float(df_recent.loc[c, "Taux couverture (%)"]) if c in df_recent.index else 0.0
        diff_cov = round(cov_r - cov_b, 1)

        flt_b = float(df_base.loc[c, "Float descendu total"]) if c in df_base.index else 0.0
        flt_r = float(df_recent.loc[c, "Float descendu total"]) if c in df_recent.index else 0.0
        diff_flt = flt_r - flt_b
        pct_flt = round((diff_flt / flt_b * 100), 1) if flt_b > 0 else 0.0

        raw_rows.append({
            "Commercial": c,
            f"POS Total ({m_base})": pos_b,
            f"POS Total ({m_recent})": pos_r,
            "Évol. POS Total": diff_pos,
            "Évol. POS (%)": pct_pos,
            f"Couverture ({m_base})": cov_b,
            f"Couverture ({m_recent})": cov_r,
            "Évol. Couverture (%)": diff_cov,
            f"Float ({m_base})": flt_b,
            f"Float ({m_recent})": flt_r,
            "Évol. Float Total": diff_flt,
            "Évol. Float (%)": pct_flt,
        })

    bi_df = pd.DataFrame(raw_rows)

    # --- 2. HEADER BI : METRIC CARDS ---
    total_pos_base = bi_df[f"POS Total ({m_base})"].sum()
    total_pos_recent = bi_df[f"POS Total ({m_recent})"].sum()
    diff_pos_global = total_pos_recent - total_pos_base
    pct_pos_global = (diff_pos_global / total_pos_base * 100) if total_pos_base > 0 else 0

    total_flt_base = bi_df[f"Float ({m_base})"].sum()
    total_flt_recent = bi_df[f"Float ({m_recent})"].sum()
    diff_flt_global = total_flt_recent - total_flt_base
    pct_flt_global = (diff_flt_global / total_flt_base * 100) if total_flt_base > 0 else 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Commercials Actifs", len(commerciaux))
    col2.metric("Total POS Servis", f"{total_pos_recent:,}", f"{diff_pos_global:+} ({pct_pos_global:+.1f}%)")
    col3.metric("Float Total Injecté", f"{total_flt_recent/1e6:.1f} M FCFA", f"{diff_flt_global/1e6:+.1f} M FCFA ({pct_flt_global:+.1f}%)")
    col4.metric("Période Comparée", f"{m_recent} vs {m_base}")

    st.divider()

    # --- 3. MISE EN FORME "STYLE BI" AVEC CONDITIONAL FORMATTING ---
    def style_performance(val):
        """Applique une couleur dynamique sur les cellules de variation."""
        if isinstance(val, (int, float)):
            if val > 0:
                return 'background-color: #d1fae5; color: #065f46; font-weight: bold;'  # Vert soft
            elif val < 0:
                return 'background-color: #fee2e2; color: #991b1b; font-weight: bold;'  # Rouge soft
        return ''

    # Formatage de présentation
    styled_df = (
        bi_df.style
        .map(style_performance, subset=["Évol. POS Total", "Évol. POS (%)", "Évol. Couverture (%)", "Évol. Float (%)"])
        .format({
            f"POS Total ({m_base})": "{:,.0f}",
            f"POS Total ({m_recent})": "{:,.0f}",
            "Évol. POS Total": "{:+,.0f}",
            "Évol. POS (%)": "{:+.1f}%",
            f"Couverture ({m_base})": "{:.1f}%",
            f"Couverture ({m_recent})": "{:.1f}%",
            "Évol. Couverture (%)": "{:+.1f}%",
            f"Float ({m_base})": lambda x: _format_fcfa(x),
            f"Float ({m_recent})": lambda x: _format_fcfa(x),
            "Évol. Float Total": lambda x: _format_fcfa(x),
            "Évol. Float (%)": "{:+.1f}%",
        })
    )

    # Entête de section + Bouton d'export alignés
    head_col1, head_col2 = st.columns([3, 1])
    with head_col1:
        st.markdown("##### 📋 Matrice d'Évolution Mensuelle")
    with head_col2:
        # --- 4. EXPORT EXCEL PROFESSIONNEL (XLSXWRITER) ---
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
            bi_df.to_excel(writer, sheet_name="Comparatif_BI", index=False)
            
            # Formatage natif Excel avec le moteur XlsxWriter
            workbook  = writer.book
            worksheet = writer.sheets["Comparatif_BI"]
            
            # Formats d'en-tête et de données
            header_format = workbook.add_format({
                'bold': True, 'text_wrap': True, 'valign': 'vcenter',
                'fg_color': '#1E293B', 'font_color': '#FFFFFF', 'border': 1
            })
            currency_format = workbook.add_format({'num_format': '#,##0 "FCFA"', 'border': 1})
            percent_format = workbook.add_format({'num_format': '+0.0%;-0.0%;0.0%', 'border': 1})
            number_format = workbook.add_format({'num_format': '#,##0', 'border': 1})
            
            # Application de la ligne d'en-tête
            for col_num, value in enumerate(bi_df.columns.values):
                worksheet.write(0, col_num, value, header_format)
                worksheet.set_column(col_num, col_num, 18) # Largeur automatique
                
        st.download_button(
            label="📥 Exporter en Excel",
            data=excel_buffer.getvalue(),
            file_name=f"comparatif_bi_commerciaux_{m_base}_vs_{m_recent}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    # Affichage du tableau interactif BI
    st.dataframe(styled_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _render_exports(prefix: str, df: pd.DataFrame, styles: dict[str, Any]) -> None:
    if df.empty:
        return
    col_csv, col_xlsx = st.columns(2)
    col_csv.download_button("📄 CSV", to_csv(df), f"{prefix}.csv", "text/csv", key=f"{prefix}_csv")
    col_xlsx.download_button(
        "📊 Excel", to_excel(df, styles=styles, sheet_name="Conquête"),
        f"{prefix}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{prefix}_xlsx",
    )


def _format_fcfa(value: Any) -> str:
    try:
        amount = float(value)
    except Exception:
        amount = 0.0
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
