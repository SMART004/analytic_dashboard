"""Performance view (Tache 5.4).

Rendu commun pour les trois segments de performance (Commercial, PR & Caisses,
CDS), paramétré par ce que controllers.performance_controller expose dans
PerformanceContext. Aucune logique métier ici : uniquement mise en forme,
disposition, et export.

Décision de conception : filtres EN LIGNE (pas en sidebar)
------------------------------------------------------------
Avec 3 segments dans des onglets Streamlit, mettre les 3 jeux de filtres en
st.sidebar les empilerait tous simultanément (les onglets Streamlit ne
masquent pas le contenu sidebar selon l'onglet actif). Pour rester dans la
contrainte "pages pas surchargées" posée dès le début du refactor, les
filtres sont donc rendus en ligne, en haut de chaque onglet.

Décisions reprises du controller (Tâche 5.3), à ne jamais réinventer ici :
- Le slider heure n'est proposé QUE pour commercial/pr_caisse, et seulement
  si une seule date est sélectionnée. Il n'apparaît jamais pour cds — pas
  seulement inactif côté backend, absent de l'écran.
- Le Top 10 n'est rendu que pour commercial et pr_caisse (context.
  top_commerciaux est vide pour cds par construction).
- Les 3 styles visuels restent distincts (décision Q2) : tableau riche pour
  commercial (couleurs horaires, seuils TR), tableau simple avec ligne
  inactive rouge pour pr_caisse et cds.
- La ligne TOTAL du quota CDS n'est jamais une moyenne des taux déjà
  arrondis : HVC_Serve et Nb_HVC_Attribue sont resommés séparément, une
  seule division à la fin.

Correctif (AttributeError 'int' object has no attribute 'fillna') :
---------------------------------------------------------------------
Quand context.table ne contient pas une colonne attendue (ex: le controller
est passé par la branche "aucune transaction sur la periode" et n'a peuplé
que les colonnes de base), `df.get("col", 0)` renvoie l'entier littéral 0
au lieu d'une Series. `_to_int`/`_to_num` plantaient alors sur `.fillna`
puisqu'un entier n'a pas cette méthode. Toutes les colonnes potentiellement
absentes passent désormais par `_col(df, name, default)`, qui garantit une
Series de la bonne longueur (alignée sur df.index) même quand la colonne
n'existe pas — la vue ne doit jamais supposer qu'une colonne du controller
est systématiquement présente.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
import hashlib
from typing import Any, Optional

import numpy as np
import pandas as pd
import streamlit as st

from controllers.performance_controller import (
    WORK_PROGRESS_WINDOWS,
    PerformanceContext,
    PerformanceFilters,
    build_performance_context,
    build_unserved_pos_export,
    count_working_days_6j,
    load_filter_options,
    normalize_commercial_mapping,
)
from ingestion.upload_sync import (
    Segment,
    list_available_folders,
    sync_segment_to_sqlite,
    upload_segment_files,
)
from services.export_service import to_csv, to_excel, to_excel_sheets, to_grouped_zip, to_image

WORK_PROGRESS_LABELS = list(WORK_PROGRESS_WINDOWS.keys())

_SEGMENT_LABELS = {
    "commercial": "Commerciaux",
    "pr_caisse": "POS relais & Caisses",
    "cds": "CDS",
}

_CDS_DISPLAY_ONLY_COLUMNS = []


# ---------------------------------------------------------------------------
# Page principale (3 onglets)
# ---------------------------------------------------------------------------

def render_performance_hub() -> None:
    """Point d'entrée unique — remplace pages/performance.py (3 pages séparées)."""
    st.title("Performance Terrain")

    tab_commercial, tab_pr_caisse, tab_cds = st.tabs(
        [_SEGMENT_LABELS["commercial"], _SEGMENT_LABELS["pr_caisse"], _SEGMENT_LABELS["cds"]]
    )

    with tab_commercial:
        _render_segment_tab("commercial")
    with tab_pr_caisse:
        _render_segment_tab("pr_caisse")
    with tab_cds:
        _render_segment_tab("cds")


SESSION_KEY_COMM_MAPPING = "perf_commercial_manual_mapping"
SESSION_KEY_COMM_MAPPING_HASH = "perf_commercial_manual_mapping_hash"


def _render_commercial_mapping_section() -> None:
    """Permet d'importer un fichier d'attribution POS <-> Commercial pour le calcul du taux de couverture."""
    manual_df = st.session_state.get(SESSION_KEY_COMM_MAPPING)
    is_active = manual_df is not None and isinstance(manual_df, pd.DataFrame) and not manual_df.empty

    expander_title = "📁 Attribution POS ↔ Commercial (Optionnel — Couverture)"
    if is_active:
        expander_title += f" [ACTIF : {len(manual_df)} POS]"

    with st.expander(expander_title, expanded=is_active):
        st.markdown(
            "Importez ici un fichier Excel/CSV d'attribution des POS aux commerciaux (colonnes reconnues : "
            "`AGENT MSISDN` / `POS`, `Commercial` / `Nom_Ccial`, et/ou `Numero` / `MSISDN_Commercial`). "
            "**L'import n'est pas obligatoire** : si aucun fichier n'est chargé, l'attribution se fait automatiquement "
            "sur la base des transactions (≥ 3 transactions)."
        )

        col_up, col_btn = st.columns([3, 1])
        with col_up:
            uploaded = st.file_uploader(
                "Importer le fichier de mapping POS ↔ Commercial",
                type=["xlsx", "xls", "csv"],
                key="perf_comm_manual_mapping_file",
                help="Associe chaque POS à son commercial pour calculer précisément POS attribués, POS touchés et Taux de couverture.",
            )
            if uploaded is not None:
                content_bytes = uploaded.getvalue()
                current_hash = hashlib.md5(content_bytes).hexdigest()
                if st.session_state.get(SESSION_KEY_COMM_MAPPING_HASH) != current_hash:
                    try:
                        if uploaded.name.lower().endswith(".csv"):
                            raw_df = pd.read_csv(BytesIO(content_bytes))
                        else:
                            raw_df = pd.read_excel(BytesIO(content_bytes))
                        normalized = normalize_commercial_mapping(raw_df)
                        if not normalized.empty:
                            st.session_state[SESSION_KEY_COMM_MAPPING] = normalized
                            st.session_state[SESSION_KEY_COMM_MAPPING_HASH] = current_hash
                            _build_performance_context_cached.clear()
                            st.success(
                                f"✅ Mapping manuel chargé : {len(normalized)} POS attribués à "
                                f"{normalized['Commercial'].nunique()} commerciaux."
                            )
                            st.rerun()
                        else:
                            st.error(
                                "Colonnes introuvables dans le fichier. Veuillez vérifier qu'il contient au moins "
                                "une colonne pour les POS (ex: 'AGENT MSISDN') et une pour le commercial (ex: 'Commercial')."
                            )
                    except Exception as exc:
                        st.error(f"Erreur de lecture du fichier de mapping : {exc}")

        with col_btn:
            st.write("")
            st.write("")
            if is_active:
                if st.button("🔄 Réinitialiser (Mode Auto)", key="btn_reset_perf_comm_mapping"):
                    st.session_state[SESSION_KEY_COMM_MAPPING] = None
                    st.session_state[SESSION_KEY_COMM_MAPPING_HASH] = None
                    _build_performance_context_cached.clear()
                    st.rerun()

        if is_active:
            st.info(
                f"ℹ️ **Mode mapping manuel actif** : {len(manual_df)} attributions chargées. "
                "Le taux de couverture est calculé sur ce portefeuille."
            )
        else:
            st.caption("ℹ️ **Mode automatique actif** : Attribution automatique calculée sur les transactions (≥ 3 TX).")


def _render_segment_tab(segment: Segment) -> None:
    _render_upload_sync_section(segment)
    st.divider()

    if segment == "commercial":
        _render_commercial_mapping_section()
        st.divider()

    filters = render_performance_filters(segment)
    context = _build_performance_context_cached(filters)
    render_performance(context)


@st.cache_data(show_spinner=False)
def _load_filter_options_cached(segment: Segment) -> dict[str, Any]:
    return load_filter_options(segment)


@st.cache_resource(show_spinner=False, ttl=120)
def _build_performance_context_cached(filters: PerformanceFilters) -> PerformanceContext:
    manual_mapping = st.session_state.get(SESSION_KEY_COMM_MAPPING) if filters.segment == "commercial" else None
    return build_performance_context(filters, manual_mapping=manual_mapping)


@st.cache_data(show_spinner=False)
def _list_available_folders_cached(segment: Segment) -> list[str]:
    return list_available_folders(segment)


# ---------------------------------------------------------------------------
# Module d'Upload & Synchronisation
# ---------------------------------------------------------------------------

def _render_upload_sync_section(segment: Segment) -> None:
    """Rend la section expandable d'upload et de synchronisation SQLite."""
    prefix = f"sync_{segment}"

    with st.expander(f"📥 Données & Ingestion - {_SEGMENT_LABELS.get(segment, segment)}", expanded=False):
        col_upload, col_sync = st.columns(2)

        with col_upload:
            st.markdown("##### 1. Téléverser des fichiers")
            uploaded_files = st.file_uploader(
                "Fichiers de transactions",
                type=["csv", "xlsx", "xls"],
                accept_multiple_files=True,
                key=f"{prefix}_uploader",
            )
            if st.button("Uploader vers Storage", key=f"{prefix}_btn_upload"):
                if uploaded_files:
                    with st.spinner("Téléversement vers Turso..."):
                        nb_uploaded = upload_segment_files(segment, uploaded_files)
                        st.success(f"✅ {nb_uploaded} fichier(s) téléversé(s) avec succès.")
                        st.cache_data.clear()
                        st.rerun()
                else:
                    st.warning("Veuillez sélectionner au moins un fichier.")

        with col_sync:
            st.markdown("##### 2. Synchroniser vers SQLite")
            available_folders = _list_available_folders_cached(segment)

            if not available_folders:
                st.info("Aucun dossier/mois disponible dans le bucket.")
            else:
                selected_folders = st.multiselect(
                    "Sélectionner les dossiers à ingérer",
                    options=available_folders,
                    default=available_folders,
                    key=f"{prefix}_folders_select",
                )

                if st.button("Lancer la synchronisation", key=f"{prefix}_btn_sync"):
                    if selected_folders:
                        with st.spinner("Ingestion des fichiers dans SQLite..."):
                            res = sync_segment_to_sqlite(segment, selected_folders)
                            if res.get("fichiers_en_erreur", 0):
                                st.warning(
                                    f"Synchronisation partielle : {res['fichiers_en_erreur']} "
                                    "fichier(s) n'ont pas pu être téléchargé(s). "
                                    "Relancez la synchronisation pour reprendre. "
                                    f"{res['lignes_inserees']} nouvelle(s) ligne(s) insérée(s)."
                                )
                            else:
                                st.success(
                                    f"✅ Synchronisation terminée : {res['lignes_inserees']} "
                                    f"nouvelle(s) ligne(s) insérée(s) sur {res['lignes_chargees']} "
                                    f"lues ({res['fichiers']} fichier(s))."
                                )
                            st.cache_data.clear()
                            st.rerun()
                    else:
                        st.warning("Veuillez sélectionner au moins un dossier.")


# ---------------------------------------------------------------------------
# Filtres (en ligne, cf. décision de conception en tête de fichier)
# ---------------------------------------------------------------------------

def render_performance_filters(segment: str) -> PerformanceFilters:
    options = _load_filter_options_cached(segment)
    min_date = _parse_date(options.get("min_date"))
    max_date = _parse_date(options.get("max_date")) or min_date
    prefix = f"perf_{segment}"

    col_date, col_a, col_b, col_c = st.columns(4)

    with col_date:
        if min_date and max_date:
            selected_dates = st.date_input(
                "Période",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                key=f"{prefix}_date_filter",
            )
        else:
            selected_dates = None
    start_date, end_date = _date_range_to_strings(selected_dates)

    hour_start = hour_end = None
    zone_centre, zone_territoire, zone_sa = "Toutes", "Toutes", "Toutes"
    type_point, territoire = "Tous", "Toutes"

    tr_mode = "actifs"
    if segment == "commercial":
        with col_a:
            zone_centre = st.selectbox("Centre", ["Toutes", "Centre II", "Centre III"], key=f"{prefix}_centre")
        with col_b:
            zone_territoire = st.selectbox(
                "Territoire", ["Toutes"] + list(options.get("zone_territoire", [])), key=f"{prefix}_territoire"
            )
        with col_c:
            zone_sa = st.selectbox("Zone_SA", ["Toutes"] + list(options.get("zone_sa", [])), key=f"{prefix}_zone_sa")

        st.write("")
        col_mode, col_mode_info = st.columns([5, 4])
        with col_mode:
            tr_mode = st.radio(
                "Mode de calcul du Taux de Rotation (TR) :",
                options=["actifs", "ouvres_26j"],
                format_func=lambda m: "🏃 Jours réels travaillés (actifs)" if m == "actifs" else "📅 Approche calendaire 6j/7 (standard 26j)",
                horizontal=True,
                key=f"{prefix}_tr_mode",
                help=(
                    "• Jours réels travaillés : Dénominateur basé sur les jours effectifs où le commercial a eu au moins une transaction.\n"
                    "• Approche calendaire 6j/7 : Dénominateur basé sur la norme de 26 jours ouvrés (dimanches exclus, 6j/7) sur le mois."
                ),
            )
        with col_mode_info:
            nb_ouvres_calc = count_working_days_6j(start_date, end_date)
            if tr_mode == "ouvres_26j":
                st.info(f"📅 **Norme 6j/7 active** : Cible fixée sur **{nb_ouvres_calc} jours ouvrés** (dimanches exclus) pour tous les commerciaux.")
            else:
                st.caption("🏃 **Jours actifs** : Cible ajustée selon les jours d'activité réels de chaque commercial.")

    elif segment == "pr_caisse":
        with col_a:
            type_point = st.selectbox("Type", ["Tous", "Point Relais", "Caisses"], key=f"{prefix}_type_point")
        with col_b:
            territoire = st.selectbox(
                "Territoire", ["Toutes"] + list(options.get("territoire", [])), key=f"{prefix}_territoire_pr"
            )

    # Slider heure : commercial + pr_caisse uniquement, et seulement si une
    # seule date est sélectionnée (même règle que l'ancien perf.py). Absent
    # de l'écran pour "cds" — pas seulement inactif côté backend.
    if segment in ("commercial", "pr_caisse") and start_date and end_date and start_date == end_date:
        hour_start, hour_end = st.slider(
            "Plage horaire", min_value=0, max_value=23, value=(0, 23), key=f"{prefix}_hour_filter"
        )

    mapping_hash = st.session_state.get(SESSION_KEY_COMM_MAPPING_HASH) if segment == "commercial" else None

    return PerformanceFilters(
        segment=segment,
        start_date=start_date,
        end_date=end_date,
        hour_start=hour_start,
        hour_end=hour_end,
        zone_centre=zone_centre,
        zone_territoire=zone_territoire,
        zone_sa=zone_sa,
        type_point=type_point,
        territoire=territoire,
        mapping_hash=mapping_hash,
        tr_mode=tr_mode,
    )


# ---------------------------------------------------------------------------
# Rendu d'un segment
# ---------------------------------------------------------------------------

def render_performance(context: PerformanceContext) -> None:
    st.subheader(f"Performance {_SEGMENT_LABELS.get(context.segment, context.segment)}")

    if context.segment == "commercial":
        tr_mode = getattr(context.filters, "tr_mode", "actifs")
        if tr_mode == "ouvres_26j":
            nb_ouvres = count_working_days_6j(context.filters.start_date, context.filters.end_date)
            st.caption(f"📅 *Approche calendaire active : Taux de Rotation calculé sur une base de **{nb_ouvres} jours ouvrés** (6j/7, dimanches exclus).*")
        else:
            st.caption("🏃 *Approche jours actifs active : Taux de Rotation calculé sur les **jours réels d'activité** de chaque commercial.*")

    if context.is_empty:
        st.warning(context.message)
        return

    if context.message:
        # Ex: "Aucune transaction sur la periode." — le controller peut
        # avoir un message informatif meme quand la table n'est pas vide
        # (un acteur par ligne, tout a zero). On l'affiche sans bloquer
        # l'affichage du tableau.
        st.info(context.message)

    display_df, styles, group_col = _build_display(context)
    if display_df.empty:
        st.info("Aucune donnée à afficher pour ces filtres.")
        return

    display_with_total = _append_total_row(display_df, context.segment, context=context)

    display_table = display_with_total
    if context.segment == "commercial":
        dsm_df_screen, cov_wallet_screen, cov_no_wallet_screen = _split_commercial_views(display_with_total)
        col_view, _ = st.columns([4, 1])
        with col_view:
            selected_view = st.radio(
                "Vue affichée à l'écran :",
                options=["complete", "dsm_activity", "cov_wallet", "cov_no_wallet"],
                format_func=lambda v: {
                    "complete": "Vue Complète (Toutes colonnes)",
                    "dsm_activity": "📊 DSM Activity",
                    "cov_wallet": "🎯 Couverture - Wallet",
                    "cov_no_wallet": "🎯 Couverture - No Wallet",
                }[v],
                horizontal=True,
                key=f"perf_comm_view_mode",
            )
        if selected_view == "dsm_activity":
            display_table = dsm_df_screen
        elif selected_view == "cov_wallet":
            display_table = cov_wallet_screen
        elif selected_view == "cov_no_wallet":
            display_table = cov_no_wallet_screen

    elif context.segment == "pr_caisse":
        dsm_df_screen, cov_wallet_screen, cov_no_wallet_screen = _split_pr_caisse_views(display_with_total)
        col_view, _ = st.columns([4, 1])
        with col_view:
            selected_view = st.radio(
                "Vue affichée à l'écran :",
                options=["complete", "dsm_activity", "cov_wallet", "cov_no_wallet"],
                format_func=lambda v: {
                    "complete": "Vue Complète (Toutes colonnes)",
                    "dsm_activity": "📊 DSM Activity",
                    "cov_wallet": "🎯 Couverture - Wallet",
                    "cov_no_wallet": "🎯 Couverture - No Wallet",
                }[v],
                horizontal=True,
                key=f"perf_pr_view_mode",
            )
        if selected_view == "dsm_activity":
            display_table = dsm_df_screen
        elif selected_view == "cov_wallet":
            display_table = cov_wallet_screen
        elif selected_view == "cov_no_wallet":
            display_table = cov_no_wallet_screen

    if context.segment == "cds":
        st.dataframe(_style_cds_display(display_table), use_container_width=True, hide_index=True, height=600)
    elif context.segment == "commercial":
        st.dataframe(_style_commercial_display(display_table), use_container_width=True, hide_index=True, height=600)
    elif context.segment == "pr_caisse":
        st.dataframe(_style_pr_caisse_display(display_table), use_container_width=True, hide_index=True, height=600)
    else:
        st.dataframe(_style_persistence(display_table), use_container_width=True, hide_index=True, height=600)

    # Top 10 : commercial + pr_caisse uniquement (context.top_commerciaux est
    # vide pour cds par construction, jamais calculé côté controller).
    if not context.top_commerciaux.empty:
        st.markdown("#### Top 10")
        st.dataframe(_build_top10_display(context), use_container_width=True, hide_index=True)

    # Espace d'analyse automatisée et synthèse terrain
    _render_performance_insights(context, display_with_total)

    st.markdown("#### Export")
    export_df = _prepare_export_df(display_with_total, context.segment)
    if context.segment == "commercial":
        filter_sig = f"{context.filters.start_date}_{context.filters.end_date}_{context.filters.zone_centre}_{context.filters.zone_territoire}_{context.filters.zone_sa}_{getattr(context.filters, 'tr_mode', 'actifs')}"
        _render_commercial_exports(f"performance_{context.segment}", export_df, styles, group_col, filter_sig)
    elif context.segment == "pr_caisse":
        filter_sig = f"{context.filters.start_date}_{context.filters.end_date}_{context.filters.type_point}_{context.filters.territoire}"
        _render_pr_caisse_exports(f"performance_{context.segment}", export_df, styles, group_col, filter_sig)
    else:
        _render_exports(f"performance_{context.segment}", export_df, styles, group_col)

    if context.segment == "commercial":
        st.markdown("---")
        st.markdown("##### 📍 Export des POS non servis (Portefeuille & Hors portefeuille)")
        st.caption(
            "Exporte au format Excel les **POS non attribués et non servis** ainsi que les **POS attribués mais non servis** "
            "par les commerciaux selon la durée et les filtres appliqués."
        )
        filter_sig = f"{context.filters.start_date}_{context.filters.end_date}_{context.filters.zone_centre}_{context.filters.zone_territoire}_{context.filters.zone_sa}"
        data_key = f"unserved_pos_excel_{filter_sig}"

        col_gen, col_dl = st.columns([1, 2])
        with col_gen:
            if st.button("📊 Préparer l'export des POS non servis", key=f"btn_gen_unserved_{filter_sig}"):
                with st.spinner("Génération du classeur Excel des POS non servis..."):
                    manual_mapping = st.session_state.get(SESSION_KEY_COMM_MAPPING)
                    st.session_state[data_key] = build_unserved_pos_export(context.filters, manual_mapping=manual_mapping)
        with col_dl:
            if st.session_state.get(data_key):
                st.download_button(
                    label="⬇️ Télécharger POS non servis [.xlsx]",
                    data=st.session_state[data_key],
                    file_name=f"POS_non_servis_{context.filters.start_date or 'debut'}_{context.filters.end_date or 'fin'}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"btn_dl_unserved_{filter_sig}",
                )



# ---------------------------------------------------------------------------
# Construction des tableaux d'affichage par segment
# ---------------------------------------------------------------------------

def _build_display(context: PerformanceContext) -> tuple[pd.DataFrame, dict[str, Any], Optional[Any]]:
    segment = context.segment
    if segment == "commercial":
        return _build_commercial_display(context.table), _commercial_styles(), ("Commercial", "Zone_Centre")
    if segment == "pr_caisse":
        return _build_pr_caisse_display(context.table, type_point=context.filters.type_point), _pr_caisse_styles(), ("Acteur", "Territoire")
    if segment == "cds":
        return _build_cds_display(context.table), _cds_styles(), None
    raise ValueError(f"Segment inconnu: {segment}")


def _build_commercial_display(table: pd.DataFrame) -> pd.DataFrame:
    df = table.copy()
    df["Heure_Dotation"] = _col(df, "Heure_Dotation_min", float("nan")).apply(_minutes_to_time)
    df["Premiere_Trans"] = _col(df, "Premiere_Trans_min", float("nan")).apply(_minutes_to_time)
    df["Derniere_Trans"] = _col(df, "Derniere_Trans_min", float("nan")).apply(_minutes_to_time)

    data = {
        # Commercial
        ("Commercial", "Zone_Centre"): _col(df, "Zone_Centre", ""),
        ("Commercial", "Zone_SA"): _col(df, "Zone_SA", ""),
        ("Commercial", "Nom"): _col(df, "Actor_Nom", ""),
        ("Commercial", "Numero"): _col(df, "Actor_MSISDN", "").astype(str),

        # Temps & Activité
        ("Temps & Activité", "Nb_Jours"): _to_int(_col(df, "Nb_Jours", 0)),
        ("Temps & Activité", "1ere_Trans"): df["Premiere_Trans"],
        ("Temps & Activité", "Dern_Trans"): df["Derniere_Trans"],
        ("Temps & Activité", "Nb_Trans"): _to_int(_col(df, "Nb_Transactions", 0)),

        # Dotation & Trésorerie
        ("Dotation & Trésorerie", "Heure"): df["Heure_Dotation"],
        ("Dotation & Trésorerie", "Montant"): _to_int(_col(df, "Dotation_Montant", 0)),
        ("Dotation & Trésorerie", "FU"): _to_int(_col(df, "FU", 0)),

        # HVC_Wallet
        ("HVC_Wallet", "Served"): _to_int(_col(df, "HVC_Wallet", 0)),
        ("HVC_Wallet", "FD"): _to_int(_col(df, "FD_HVC_Wallet", 0)),
        ("HVC_Wallet", "TR_(%)"): _to_num(_col(df, "TR_HVC_Wallet", 0)),

        # HVC_No_Wallet
        ("HVC_No_Wallet", "Served"): _to_int(_col(df, "HVC_No_Wallet", 0)),
        ("HVC_No_Wallet", "FD"): _to_int(_col(df, "FD_HVC_No_Wallet", 0)),
        ("HVC_No_Wallet", "TR_(%)"): _to_num(_col(df, "TR_HVC_No_Wallet", 0)),

        # HVC (Total)
        ("HVC", "Served"): _to_int(_col(df, "HVC_Total_Served", _col(df, "Sigma_HVC_Serve", 0))),
        ("HVC", "FD"): _to_int(_col(df, "FD_HVC_Total", _col(df, "FD_HVC", 0))),
        ("HVC", "TR_(%)"): _to_num(_col(df, "TR_HVC_General", 0)),

        # Others_Wallet
        ("Others_Wallet", "Served"): _to_int(_col(df, "Others_Wallet", 0)),
        ("Others_Wallet", "FD"): _to_int(_col(df, "FD_Others_Wallet", 0)),
        ("Others_Wallet", "TR_(%)"): _to_num(_col(df, "TR_Others_Wallet", 0)),

        # Others_No_Wallet
        ("Others_No_Wallet", "Served"): _to_int(_col(df, "Others_No_Wallet", 0)),
        ("Others_No_Wallet", "FD"): _to_int(_col(df, "FD_Others_No_Wallet", 0)),
        ("Others_No_Wallet", "TR_(%)"): _to_num(_col(df, "TR_Others_No_Wallet", 0)),

        # Others (Total)
        ("Others", "Served"): _to_int(_col(df, "Others_Total_Served", _col(df, "Sigma_Others_Serve", 0))),
        ("Others", "FD"): _to_int(_col(df, "FD_Others_Total", _col(df, "FD_Others", 0))),
        ("Others", "TR_(%)"): _to_num(_col(df, "TR_Others_General", 0)),

        # Synthèse Ventes & POS
        ("Synthèse Ventes & POS", "Somme_POS_Serve"): _to_int(_col(df, "Sigma_POS_Serve", 0)),
        ("Synthèse Ventes & POS", "Somme_FD"): _to_int(_col(df, "Sigma_FD", 0)),
        ("Synthèse Ventes & POS", "TR_General_(%)"): _to_num(_col(df, "TR_General", 0)),

        # Objectifs & Couverture
        ("Objectifs & Couverture", "HVC_Attrib"): _to_int(_col(df, "POS_Attribues_HVC", 0)),
        ("Objectifs & Couverture", "Others_Attrib"): _to_int(_col(df, "POS_Attribues_Others", 0)),
        ("Objectifs & Couverture", "Tot_Attrib"): _to_int(_col(df, "POS_Attribues", 0)),
        ("Objectifs & Couverture", "POS_Touches"): _to_int(_col(df, "POS_Touches", 0)),
        ("Objectifs & Couverture", "Couv_HVC_(%)"): _to_num(_col(df, "Taux_Couverture_HVC", 0)),
        ("Objectifs & Couverture", "Couv_Others_(%)"): _to_num(_col(df, "Taux_Couverture_Others", 0)),
        ("Objectifs & Couverture", "Couv_Tot_(%)"): _to_num(_col(df, "Taux_Couverture", 0)),
        ("Objectifs & Couverture", "Persistance_(%)"): pd.to_numeric(_col(df, "Taux_Persistance", float("nan")), errors="coerce"),
    }

    for label in WORK_PROGRESS_LABELS:
        serve_col, tr_col = f"HVC Serve {label}", f"TR_HVC {label}"
        other_serve_col, other_tr_col = f"Other Serve {label}", f"TR_Other {label}"
        data[("Work Progress", label)] = (
            _to_int(_col(df, serve_col, 0)).astype(str) + " | "
            + _to_num(_col(df, tr_col, 0)).round(1).astype(str) + "% | "
            + _to_int(_col(df, other_serve_col, 0)).astype(str) + " | "
            + _to_num(_col(df, other_tr_col, 0)).round(1).astype(str) + "%"
        )

    multi_df = pd.DataFrame(data)
    multi_df.columns = pd.MultiIndex.from_tuples(multi_df.columns)
    return multi_df.sort_values([("Commercial", "Zone_Centre"), ("Commercial", "Zone_SA"), ("Commercial", "Nom")]).reset_index(drop=True)


def _build_pr_caisse_display(table: pd.DataFrame, type_point: str = "Tous") -> pd.DataFrame:
    df = table.copy()
    data = {
        # Acteur
        ("Acteur", "Type"): _col(df, "Type_Point", ""),
        ("Acteur", "Territoire"): _col(df, "Territoire", ""),
        ("Acteur", "Localisation"): _col(df, "Localisation", ""),
        ("Acteur", "Nom"): _col(df, "Actor_Nom", ""),

        # Temps
        ("Temps", "Nb_Jours"): _to_int(_col(df, "Nb_Jours", 0)),

        # Dotation & Trésorerie
        ("Dotation & Trésorerie", "Nom"): _col(df, "Dotation_Nom", ""),
        ("Dotation & Trésorerie", "Montant"): _to_int(_col(df, "Dotation_Montant", 0)),
        ("Dotation & Trésorerie", "Float_Up"): _to_int(_col(df, "FU", 0)),

        # HVC_Wallet
        ("HVC_Wallet", "Served"): _to_int(_col(df, "HVC_Wallet", _col(df, "HVC_Serve", 0))),
        ("HVC_Wallet", "FD"): _to_int(_col(df, "FD_HVC_Wallet", _col(df, "FD_HVC", 0))),

        # HVC_No_Wallet
        ("HVC_No_Wallet", "Served"): _to_int(_col(df, "HVC_No_Wallet", 0)),
        ("HVC_No_Wallet", "FD"): _to_int(_col(df, "FD_HVC_No_Wallet", 0)),

        # HVC (Total)
        ("HVC", "Served"): _to_int(_col(df, "HVC_Total_Served", _col(df, "HVC_Serve", 0))),
        ("HVC", "FD"): _to_int(_col(df, "FD_HVC_Total", _col(df, "FD_HVC", 0))),

        # Others_Wallet
        ("Others_Wallet", "Served"): _to_int(_col(df, "Others_Wallet", _col(df, "Other_Serve", 0))),
        ("Others_Wallet", "FD"): _to_int(_col(df, "FD_Others_Wallet", _col(df, "FD_Others", 0))),

        # Others_No_Wallet
        ("Others_No_Wallet", "Served"): _to_int(_col(df, "Others_No_Wallet", 0)),
        ("Others_No_Wallet", "FD"): _to_int(_col(df, "FD_Others_No_Wallet", 0)),

        # Others (Total)
        ("Others", "Served"): _to_int(_col(df, "Others_Total_Served", _col(df, "Other_Serve", 0))),
        ("Others", "FD"): _to_int(_col(df, "FD_Others_Total", _col(df, "FD_Others", 0))),

        # Synthèse POS & Finances
        ("Synthèse POS & Finances", "Somme_POS_Serve"): _to_int(_col(df, "Sigma_POS_Serve", 0)),
        ("Synthèse POS & Finances", "Somme_FD"): _to_int(_col(df, "Sigma_FD", 0)),

        # Objectifs & Couverture
        ("Objectifs & Couverture", "POS_Attribues"): _to_int(_col(df, "POS_Attribues", 0)),
        ("Objectifs & Couverture", "POS_Touches"): _to_int(_col(df, "POS_Touches", 0)),
        ("Objectifs & Couverture", "Taux_Couverture_(%)"): _to_num(_col(df, "Taux_Couverture", 0.0)),
        ("Objectifs & Couverture", "Taux_Persistance_(%)"): pd.to_numeric(_col(df, "Taux_Persistance", float("nan")), errors="coerce"),
    }

    multi_df = pd.DataFrame(data)
    multi_df.columns = pd.MultiIndex.from_tuples(multi_df.columns)

    if type_point == "Caisses" or (not df.empty and "Type_Point" in df.columns and (df["Type_Point"] == "Caisses").all()):
        cols_to_drop = [
            ("Dotation & Trésorerie", "Nom"), ("Dotation & Trésorerie", "Montant"),
            ("Objectifs & Couverture", "POS_Attribues"), ("Objectifs & Couverture", "POS_Touches"),
            ("Objectifs & Couverture", "Taux_Couverture_(%)"), ("Objectifs & Couverture", "Taux_Persistance_(%)"),
        ]
        multi_df = multi_df.drop(columns=[c for c in cols_to_drop if c in multi_df.columns], errors="ignore")

    return multi_df.sort_values([("Acteur", "Type"), ("Acteur", "Territoire"), ("Acteur", "Nom")]).reset_index(drop=True)


def _build_cds_display(table: pd.DataFrame) -> pd.DataFrame:
    df = table.copy()
    data = {
        ("CDS", "Nom"): _col(df, "Actor_Nom", ""),
        ("CDS", "Numero"): _col(df, "Actor_MSISDN", "").astype(str),
        ("Temps", "Nb_Jours"): _to_int(_col(df, "Nb_Jours", 0)),
        ("HVC_Wallet", "Served"): _to_int(_col(df, "HVC_Wallet_Served", 0)),
        ("HVC_Wallet", "FD"): _to_int(_col(df, "HVC_Wallet_FD", 0)),
        ("HVC_No_Wallet", "Served"): _to_int(_col(df, "HVC_No_Wallet_Served", 0)),
        ("HVC_No_Wallet", "FD"): _to_int(_col(df, "HVC_No_Wallet_FD", 0)),
        ("Others", "Served"): _to_int(_col(df, "Others_Served", 0)),
        ("Others", "FD"): _to_int(_col(df, "Others_FD", 0)),
        ("Synthèse POS", "Somme_POS_Serve"): _to_int(_col(df, "Somme_POS_Serve", 0)),
        ("Commerciaux", "Served"): _to_int(_col(df, "Ccial_Serve", 0)),
        ("Commerciaux", "FD"): _to_int(_col(df, "FD_Commercial", 0)),
        ("Finances", "Somme_FD"): _to_int(_col(df, "Somme_FD", 0)),
        ("Objectifs & Suivi", "Nb_HVC_Attribue"): _to_int(_col(df, "Nb_HVC_Attribue", 0)),
        ("Objectifs & Suivi", "Taux_Couverture_(%)"): _to_num(_col(df, "Taux_Couverture_Quota", 0.0)),
        ("Dotation", "Nom"): _col(df, "Dotation_Nom", ""),
        ("Dotation", "Montant"): _to_int(_col(df, "Dotation_Montant", 0)),
    }
    multi_df = pd.DataFrame(data)
    multi_df.columns = pd.MultiIndex.from_tuples(multi_df.columns)
    return multi_df.sort_values([("CDS", "Nom")]).reset_index(drop=True)


def _prepare_export_df(df: pd.DataFrame, segment: str) -> pd.DataFrame:
    if segment == "cds":
        return df.drop(columns=_CDS_DISPLAY_ONLY_COLUMNS, errors="ignore")
    if segment == "pr_caisse":
        non_total = df[~df.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
        if not non_total.empty and ("Acteur", "Type") in non_total.columns and (non_total[("Acteur", "Type")] == "Caisses").all():
            cols_to_drop = [
                ("Dotation & Trésorerie", "Nom"), ("Dotation & Trésorerie", "Montant"),
                ("Objectifs & Couverture", "POS_Attribues"), ("Objectifs & Couverture", "POS_Touches"),
                ("Objectifs & Couverture", "Taux_Couverture_(%)"), ("Objectifs & Couverture", "Taux_Persistance_(%)"),
            ]
            return df.drop(columns=[c for c in cols_to_drop if c in df.columns], errors="ignore")
    return df


def _build_top10_display(context: PerformanceContext) -> pd.DataFrame:
    df = context.top_commerciaux.copy()
    cols = [c for c in ["Actor_Nom", "Nb_Jours", "HVC_Serve", "TR_General", "Sigma_FD", "Score_Global"] if c in df.columns]
    display = df[cols].rename(columns={
        "Actor_Nom": "Nom",
        "TR_General": "TR_General (%)",
        "Sigma_FD": "Σ_FD (montant)",
    })
    display.insert(0, "Rang", range(1, len(display) + 1))
    return display


# ---------------------------------------------------------------------------
# Ligne TOTAL
# ---------------------------------------------------------------------------

def _append_total_row(
    display_df: pd.DataFrame, 
    segment: str, 
    context: Optional[PerformanceContext] = None
) -> pd.DataFrame:
    if display_df.empty:
        return display_df

    if segment == "cds":
        total_dict = {col: "" for col in display_df.columns}
        total_dict[("CDS", "Nom")] = "TOTAL"

        filters = getattr(context, "filters", None)
        if filters and filters.start_date and filters.end_date:
            try:
                d_start = pd.to_datetime(filters.start_date).date()
                d_end = pd.to_datetime(filters.end_date).date()
                nb_jours_total = max((d_end - d_start).days + 1, 1)
            except Exception:
                nb_jours_total = 1
        else:
            nb_jours_total = 1
        total_dict[("Temps", "Nb_Jours")] = int(nb_jours_total)

        for col in [
            ("HVC_Wallet", "Served"),
            ("HVC_Wallet", "FD"),
            ("HVC_No_Wallet", "Served"),
            ("HVC_No_Wallet", "FD"),
            ("Others", "Served"),
            ("Others", "FD"),
            ("Synthèse POS", "Somme_POS_Serve"),
            ("Commerciaux", "Served"),
            ("Commerciaux", "FD"),
            ("Finances", "Somme_FD"),
            ("Objectifs & Suivi", "Nb_HVC_Attribue"),
            ("Dotation", "Montant"),
        ]:
            if col in display_df.columns:
                total_dict[col] = int(pd.to_numeric(display_df[col], errors="coerce").fillna(0).sum())

        hvc_wallet_served_tot = total_dict.get(("HVC_Wallet", "Served"), 0)
        hvc_attribue_tot = total_dict.get(("Objectifs & Suivi", "Nb_HVC_Attribue"), 0)
        if hvc_attribue_tot > 0:
            total_dict[("Objectifs & Suivi", "Taux_Couverture_(%)")] = round(hvc_wallet_served_tot / hvc_attribue_tot * 100, 1)
        else:
            total_dict[("Objectifs & Suivi", "Taux_Couverture_(%)")] = 0.0

        total_row_df = pd.DataFrame([total_dict], columns=display_df.columns)
        return pd.concat([display_df, total_row_df], ignore_index=True)

    if segment == "commercial":
        label_col = ("Commercial", "Zone_Centre")
        total_dict = {col: "" for col in display_df.columns}
        total_dict[label_col] = "TOTAL"

        num_sum_cols = [
            ("Dotation & Trésorerie", "Montant"),
            ("Dotation & Trésorerie", "FU"),
            ("Temps & Activité", "Nb_Trans"),
            ("HVC_Wallet", "Served"),
            ("HVC_Wallet", "FD"),
            ("HVC_No_Wallet", "Served"),
            ("HVC_No_Wallet", "FD"),
            ("HVC", "Served"),
            ("HVC", "FD"),
            ("Others_Wallet", "Served"),
            ("Others_Wallet", "FD"),
            ("Others_No_Wallet", "Served"),
            ("Others_No_Wallet", "FD"),
            ("Others", "Served"),
            ("Others", "FD"),
            ("Synthèse Ventes & POS", "Somme_POS_Serve"),
            ("Synthèse Ventes & POS", "Somme_FD"),
            ("Objectifs & Couverture", "HVC_Attrib"),
            ("Objectifs & Couverture", "Others_Attrib"),
            ("Objectifs & Couverture", "Tot_Attrib"),
            ("Objectifs & Couverture", "POS_Touches"),
        ]
        for col in num_sum_cols:
            if col in display_df.columns:
                total_dict[col] = int(pd.to_numeric(display_df[col], errors="coerce").fillna(0).sum())

        for hour_col in [("Dotation & Trésorerie", "Heure"), ("Temps & Activité", "1ere_Trans"), ("Temps & Activité", "Dern_Trans")]:
            if hour_col in display_df.columns:
                valid_mins = display_df[hour_col].apply(_time_str_to_minutes).dropna()
                total_dict[hour_col] = _minutes_to_time(valid_mins.mean()) if not valid_mins.empty else "N/A"

        ctx_filters = getattr(context, "filters", None) if context is not None else None
        is_ouvres_mode = (
            ctx_filters is not None
            and getattr(ctx_filters, "tr_mode", "actifs") == "ouvres_26j"
        )
        if is_ouvres_mode:
            nb_ouvres = (
                context.table["Nb_Jours_Ouvres"].iloc[0]
                if (context is not None and not context.table.empty and "Nb_Jours_Ouvres" in context.table.columns and not context.table["Nb_Jours_Ouvres"].empty)
                else count_working_days_6j(getattr(ctx_filters, "start_date", None), getattr(ctx_filters, "end_date", None))
            )
            if ("Temps & Activité", "Nb_Jours") in display_df.columns:
                total_dict[("Temps & Activité", "Nb_Jours")] = int(nb_ouvres)
        else:
            if ("Temps & Activité", "Nb_Jours") in display_df.columns:
                total_dict[("Temps & Activité", "Nb_Jours")] = int(pd.to_numeric(display_df[("Temps & Activité", "Nb_Jours")], errors="coerce").fillna(0).max())

        table = context.table if (context is not None and not context.table.empty) else None
        if table is not None:
            if is_ouvres_mode:
                nb_jours = pd.Series(float(nb_ouvres), index=table.index, dtype=float)
            else:
                nb_jours = pd.to_numeric(_col(table, "Nb_Jours_Actifs", _col(table, "Nb_Jours", 1)), errors="coerce").replace(0, np.nan).fillna(1)

            def _calc_tr_table(nb_trans_col: str, serve_col: str) -> float:
                if nb_trans_col not in table.columns or serve_col not in table.columns:
                    return 0.0
                nb_trans = pd.to_numeric(table[nb_trans_col], errors="coerce").fillna(0).sum()
                serve = pd.to_numeric(table[serve_col], errors="coerce").fillna(0)
                cible = (serve * nb_jours * 3).sum()
                return round((nb_trans / cible * 100), 1) if cible > 0 else 0.0

            if ("HVC_Wallet", "TR_(%)") in display_df.columns:
                total_dict[("HVC_Wallet", "TR_(%)")] = _calc_tr_table("Nb_Trans_HVC_Wallet", "HVC_Wallet")
            if ("HVC_No_Wallet", "TR_(%)") in display_df.columns:
                total_dict[("HVC_No_Wallet", "TR_(%)")] = _calc_tr_table("Nb_Trans_HVC_No_Wallet", "HVC_No_Wallet")
            if ("HVC", "TR_(%)") in display_df.columns:
                tr_val = _calc_tr_table("Nb_Trans_HVC", "Sigma_HVC_Serve")
                if tr_val == 0.0:
                    nb_trans_hvc = (
                        pd.to_numeric(_col(table, "Nb_Trans_HVC_Wallet", 0), errors="coerce").fillna(0).sum()
                        + pd.to_numeric(_col(table, "Nb_Trans_HVC_No_Wallet", 0), errors="coerce").fillna(0).sum()
                    )
                    serve_hvc = pd.to_numeric(_col(table, "Sigma_HVC_Serve", 0), errors="coerce").fillna(0)
                    cible_hvc = (serve_hvc * nb_jours * 3).sum()
                    tr_val = round((nb_trans_hvc / cible_hvc * 100), 1) if cible_hvc > 0 else 0.0
                total_dict[("HVC", "TR_(%)")] = tr_val

            if ("Others_Wallet", "TR_(%)") in display_df.columns:
                total_dict[("Others_Wallet", "TR_(%)")] = _calc_tr_table("Nb_Trans_Others_Wallet", "Others_Wallet")
            if ("Others_No_Wallet", "TR_(%)") in display_df.columns:
                total_dict[("Others_No_Wallet", "TR_(%)")] = _calc_tr_table("Nb_Trans_Others_No_Wallet", "Others_No_Wallet")
            if ("Others", "TR_(%)") in display_df.columns:
                tr_val_oth = _calc_tr_table("Nb_Trans_Other", "Sigma_Others_Serve")
                if tr_val_oth == 0.0:
                    nb_trans_oth = (
                        pd.to_numeric(_col(table, "Nb_Trans_Others_Wallet", 0), errors="coerce").fillna(0).sum()
                        + pd.to_numeric(_col(table, "Nb_Trans_Others_No_Wallet", 0), errors="coerce").fillna(0).sum()
                    )
                    serve_oth = pd.to_numeric(_col(table, "Sigma_Others_Serve", 0), errors="coerce").fillna(0)
                    cible_oth = (serve_oth * nb_jours * 3).sum()
                    tr_val_oth = round((nb_trans_oth / cible_oth * 100), 1) if cible_oth > 0 else 0.0
                total_dict[("Others", "TR_(%)")] = tr_val_oth

            if ("Synthèse Ventes & POS", "TR_General_(%)") in display_df.columns:
                tr_gen_val = _calc_tr_table("Nb_Transactions", "Sigma_POS_Serve")
                if tr_gen_val == 0.0:
                    nb_trans_gen = (
                        pd.to_numeric(_col(table, "Nb_Trans_HVC_Wallet", 0), errors="coerce").fillna(0).sum()
                        + pd.to_numeric(_col(table, "Nb_Trans_HVC_No_Wallet", 0), errors="coerce").fillna(0).sum()
                        + pd.to_numeric(_col(table, "Nb_Trans_Others_Wallet", 0), errors="coerce").fillna(0).sum()
                        + pd.to_numeric(_col(table, "Nb_Trans_Others_No_Wallet", 0), errors="coerce").fillna(0).sum()
                    )
                    serve_all = pd.to_numeric(_col(table, "Sigma_POS_Serve", 0), errors="coerce").fillna(0)
                    cible_all = (serve_all * nb_jours * 3).sum()
                    tr_gen_val = round((nb_trans_gen / cible_all * 100), 1) if cible_all > 0 else 0.0
                total_dict[("Synthèse Ventes & POS", "TR_General_(%)")] = tr_gen_val

            for label in WORK_PROGRESS_LABELS:
                wp_col = ("Work Progress", label)
                if wp_col in display_df.columns:
                    serve_col, tr_col = f"HVC Serve {label}", f"TR_HVC {label}"
                    other_serve_col, other_tr_col = f"Other Serve {label}", f"TR_Other {label}"
                    hvc_s = pd.to_numeric(_col(table, serve_col, 0), errors="coerce").fillna(0)
                    other_s = pd.to_numeric(_col(table, other_serve_col, 0), errors="coerce").fillna(0)
                    tot_hvc = int(hvc_s.sum())
                    tot_other = int(other_s.sum())

                    tr_hvc_s = pd.to_numeric(_col(table, tr_col, 0), errors="coerce").fillna(0)
                    tr_other_s = pd.to_numeric(_col(table, other_tr_col, 0), errors="coerce").fillna(0)

                    cible_hvc_w = (hvc_s * nb_jours * 3).sum()
                    trans_hvc_w = (tr_hvc_s * hvc_s * nb_jours * 3 / 100).sum()
                    tr_hvc_tot = round(trans_hvc_w / cible_hvc_w * 100, 1) if cible_hvc_w > 0 else 0.0

                    cible_other_w = (other_s * nb_jours * 3).sum()
                    trans_other_w = (tr_other_s * other_s * nb_jours * 3 / 100).sum()
                    tr_other_tot = round(trans_other_w / cible_other_w * 100, 1) if cible_other_w > 0 else 0.0

                    total_dict[wp_col] = f"{tot_hvc} | {tr_hvc_tot:.1f}% | {tot_other} | {tr_other_tot:.1f}%"

        tot_attr = total_dict.get(("Objectifs & Couverture", "Tot_Attrib"), 0)
        tot_touch = total_dict.get(("Objectifs & Couverture", "POS_Touches"), 0)
        if ("Objectifs & Couverture", "Couv_Tot_(%)") in display_df.columns:
            total_dict[("Objectifs & Couverture", "Couv_Tot_(%)")] = round(tot_touch / tot_attr * 100, 1) if tot_attr > 0 else 0.0

        hvc_attr = total_dict.get(("Objectifs & Couverture", "HVC_Attrib"), 0)
        hvc_served_wallet = total_dict.get(("HVC_Wallet", "Served"), 0)
        if ("Objectifs & Couverture", "Couv_HVC_(%)") in display_df.columns:
            total_dict[("Objectifs & Couverture", "Couv_HVC_(%)")] = round(hvc_served_wallet / hvc_attr * 100, 1) if hvc_attr > 0 else 0.0

        oth_attr = total_dict.get(("Objectifs & Couverture", "Others_Attrib"), 0)
        oth_served_wallet = total_dict.get(("Others_Wallet", "Served"), 0)
        if ("Objectifs & Couverture", "Couv_Others_(%)") in display_df.columns:
            total_dict[("Objectifs & Couverture", "Couv_Others_(%)")] = round(oth_served_wallet / oth_attr * 100, 1) if oth_attr > 0 else 0.0

        if ("Objectifs & Couverture", "Persistance_(%)") in display_df.columns:
            total_dict[("Objectifs & Couverture", "Persistance_(%)")] = None

        total_row_df = pd.DataFrame([total_dict], columns=display_df.columns).astype(display_df.dtypes.to_dict(), errors="ignore")
        return pd.concat([display_df, total_row_df], ignore_index=True)

    if segment == "pr_caisse":
        label_col = ("Acteur", "Type")
        total_dict = {col: ("" if display_df[col].dtype == object else None) for col in display_df.columns}
        total_dict[label_col] = "TOTAL"

        filters = getattr(context, "filters", None)
        if filters and filters.start_date and filters.end_date:
            try:
                d_start = pd.to_datetime(filters.start_date).date()
                d_end = pd.to_datetime(filters.end_date).date()
                nb_jours_total = max((d_end - d_start).days + 1, 1)
            except Exception:
                nb_jours_total = 1
        else:
            nb_jours_total = 1
        if ("Temps", "Nb_Jours") in display_df.columns:
            total_dict[("Temps", "Nb_Jours")] = int(nb_jours_total)

        for col in [
            ("Dotation & Trésorerie", "Montant"),
            ("Dotation & Trésorerie", "Float_Up"),
            ("HVC_Wallet", "Served"),
            ("HVC_Wallet", "FD"),
            ("HVC_No_Wallet", "Served"),
            ("HVC_No_Wallet", "FD"),
            ("HVC", "Served"),
            ("HVC", "FD"),
            ("Others_Wallet", "Served"),
            ("Others_Wallet", "FD"),
            ("Others_No_Wallet", "Served"),
            ("Others_No_Wallet", "FD"),
            ("Others", "Served"),
            ("Others", "FD"),
            ("Synthèse POS & Finances", "Somme_POS_Serve"),
            ("Synthèse POS & Finances", "Somme_FD"),
            ("Objectifs & Couverture", "POS_Attribues"),
            ("Objectifs & Couverture", "POS_Touches"),
        ]:
            if col in display_df.columns:
                total_dict[col] = int(pd.to_numeric(display_df[col], errors="coerce").fillna(0).sum())

        # Couverture strictement restreinte aux Point Relais (et non Caisses)
        if ("Objectifs & Couverture", "POS_Attribues") in display_df.columns and ("Objectifs & Couverture", "POS_Touches") in display_df.columns:
            is_pr = display_df[("Acteur", "Type")].astype(str).str.lower().str.contains("point relais")
            pr_rows = display_df[is_pr]
            if not pr_rows.empty:
                pr_attrib = pd.to_numeric(pr_rows[("Objectifs & Couverture", "POS_Attribues")], errors="coerce").fillna(0).sum()
                pr_touches = pd.to_numeric(pr_rows[("Objectifs & Couverture", "POS_Touches")], errors="coerce").fillna(0).sum()
                if pr_attrib > 0:
                    total_dict[("Objectifs & Couverture", "Taux_Couverture_(%)")] = round(pr_touches / pr_attrib * 100, 1)
                else:
                    total_dict[("Objectifs & Couverture", "Taux_Couverture_(%)")] = 0.0
            else:
                total_dict[("Objectifs & Couverture", "Taux_Couverture_(%)")] = 0.0

        if ("Objectifs & Couverture", "Taux_Persistance_(%)") in display_df.columns:
            total_dict[("Objectifs & Couverture", "Taux_Persistance_(%)")] = None

        total_row_df = pd.DataFrame([total_dict], columns=display_df.columns).astype(display_df.dtypes.to_dict(), errors="ignore")
        return pd.concat([display_df, total_row_df], ignore_index=True)

    label_col = display_df.columns[0]
    total: dict[str, Any] = {col: "" for col in display_df.columns}
    total[label_col] = "TOTAL"
    for col in display_df.select_dtypes(include="number").columns:
        total[col] = display_df[col].sum()
    return pd.concat([display_df, pd.DataFrame([total])], ignore_index=True)


# ---------------------------------------------------------------------------
# Styles par segment
# ---------------------------------------------------------------------------

def _total_row_match(row: pd.Series) -> bool:
    return str(row.iloc[0]).strip().upper() == "TOTAL"


def _inactive_commercial_style(row: pd.Series) -> dict[str, Any]:
    if _total_row_match(row):
        return {}
    try:
        served = float(row.get(("Synthèse Ventes & POS", "Somme_POS_Serve"), row.get(("Commercial", "Somme_POS_Serve"), 0)))
        tx = float(row.get(("Temps & Activité", "Nb_Trans"), 0))
        if served == 0 and tx == 0:
            return {"fill": "#ffcccc"}
    except Exception:
        pass
    return {}


def _inactive_pr_caisse_style(row: pd.Series) -> dict[str, Any]:
    if _total_row_match(row):
        return {}
    try:
        served = float(row.get(("Synthèse POS & Finances", "Somme_POS_Serve"), row.get(("HVC", "Served"), 0)))
        if served == 0:
            return {"fill": "#ffcccc"}
    except Exception:
        pass
    return {}


def _time_str_to_minutes(value: Any) -> Optional[int]:
    try:
        if value in (None, "N/A", ""):
            return None
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _heure_debut_rule(value: Any, row: pd.Series, col: Any) -> Optional[dict[str, Any]]:
    col_str = col[-1] if isinstance(col, tuple) else str(col)
    if col_str not in ("Heure_Dotation", "Premiere_Trans", "H_Dotation", "1ere_Trans", "Heure"):
        return None
    if _total_row_match(row):
        return None
    minutes = _time_str_to_minutes(value)
    if minutes is None:
        return None
    if minutes <= 7 * 60 + 30:
        return {"fill": "#d8f3dc"}
    if minutes <= 7 * 60 + 59:
        return {"fill": "#fff3bf"}
    return {"fill": "#ffd6d6"}


def _heure_fin_rule(value: Any, row: pd.Series, col: Any) -> Optional[dict[str, Any]]:
    col_str = col[-1] if isinstance(col, tuple) else str(col)
    if col_str not in ("Derniere_Trans", "Dern_Trans"):
        return None
    if _total_row_match(row):
        return None
    minutes = _time_str_to_minutes(value)
    if minutes is None:
        return None
    if minutes < 15 * 60:
        return {"fill": "#ffd6d6"}
    if minutes < 17 * 60:
        return {"fill": "#fff3bf"}
    return {"fill": "#d8f3dc"}


def _commercial_styles() -> dict[str, Any]:
    return {
        "sheet_name": "Performance Commerciaux",
        "total_row_match": _total_row_match,
        "row_styles": _inactive_commercial_style,
        "formats": {
            ("Dotation & Trésorerie", "Montant"): "#,##0",
            ("Dotation & Trésorerie", "FU"): "#,##0",
            ("Temps & Activité", "Nb_Jours"): "#,##0",
            ("Temps & Activité", "Nb_Trans"): "#,##0",
            ("HVC_Wallet", "Served"): "#,##0",
            ("HVC_Wallet", "FD"): "#,##0",
            ("HVC_Wallet", "TR_(%)"): '0.0"%"',
            ("HVC_No_Wallet", "Served"): "#,##0",
            ("HVC_No_Wallet", "FD"): "#,##0",
            ("HVC_No_Wallet", "TR_(%)"): '0.0"%"',
            ("HVC", "Served"): "#,##0",
            ("HVC", "FD"): "#,##0",
            ("HVC", "TR_(%)"): '0.0"%"',
            ("Others_Wallet", "Served"): "#,##0",
            ("Others_Wallet", "FD"): "#,##0",
            ("Others_Wallet", "TR_(%)"): '0.0"%"',
            ("Others_No_Wallet", "Served"): "#,##0",
            ("Others_No_Wallet", "FD"): "#,##0",
            ("Others_No_Wallet", "TR_(%)"): '0.0"%"',
            ("Others", "Served"): "#,##0",
            ("Others", "FD"): "#,##0",
            ("Others", "TR_(%)"): '0.0"%"',
            ("Synthèse Ventes & POS", "Somme_POS_Serve"): "#,##0",
            ("Synthèse Ventes & POS", "Somme_FD"): "#,##0",
            ("Synthèse Ventes & POS", "TR_General_(%)"): '0.0"%"',
            ("Synthèse Wallet", "Total_Served"): "#,##0",
            ("Synthèse Wallet", "Total_FD"): "#,##0",
            ("Synthèse No_Wallet", "Total_Served"): "#,##0",
            ("Synthèse No_Wallet", "Total_FD"): "#,##0",
            ("Objectifs & Couverture", "HVC_Attrib"): "#,##0",
            ("Objectifs & Couverture", "Others_Attrib"): "#,##0",
            ("Objectifs & Couverture", "Tot_Attrib"): "#,##0",
            ("Objectifs & Couverture", "POS_Touches"): "#,##0",
            ("Objectifs & Couverture", "Couv_HVC_(%)"): '0.0"%"',
            ("Objectifs & Couverture", "Couv_Others_(%)"): '0.0"%"',
            ("Objectifs & Couverture", "Couv_Tot_(%)"): '0.0"%"',
            ("Objectifs & Couverture", "Persistance_(%)"): '0.0"%"',
        },
        "rules": [
            _heure_debut_rule,
            _heure_fin_rule,
            {
                "columns": [
                    ("HVC_Wallet", "TR_(%)"), ("HVC_No_Wallet", "TR_(%)"), ("HVC", "TR_(%)"),
                    ("Others_Wallet", "TR_(%)"), ("Others_No_Wallet", "TR_(%)"), ("Others", "TR_(%)"),
                    ("Synthèse Ventes & POS", "TR_General_(%)"),
                ],
                "op": "<",
                "value": 70,
                "style": {"fill": "#ffd6d6"},
            },
            {
                "columns": [
                    ("HVC_Wallet", "TR_(%)"), ("HVC_No_Wallet", "TR_(%)"), ("HVC", "TR_(%)"),
                    ("Others_Wallet", "TR_(%)"), ("Others_No_Wallet", "TR_(%)"), ("Others", "TR_(%)"),
                    ("Synthèse Ventes & POS", "TR_General_(%)"),
                ],
                "op": "between",
                "value": (70, 99.999),
                "style": {"fill": "#fff3bf"},
            },
            {
                "columns": [
                    ("HVC_Wallet", "TR_(%)"), ("HVC_No_Wallet", "TR_(%)"), ("HVC", "TR_(%)"),
                    ("Others_Wallet", "TR_(%)"), ("Others_No_Wallet", "TR_(%)"), ("Others", "TR_(%)"),
                    ("Synthèse Ventes & POS", "TR_General_(%)"),
                ],
                "op": ">=",
                "value": 100,
                "style": {"fill": "#d8f3dc"},
            },
            {"columns": [("Synthèse Ventes & POS", "Somme_POS_Serve")], "op": "<", "value": 20, "style": {"fill": "#ffd6d6"}},
            {"columns": [("Synthèse Ventes & POS", "Somme_POS_Serve")], "op": "between", "value": (20, 39), "style": {"fill": "#fff3bf"}},
            {"columns": [("Synthèse Ventes & POS", "Somme_POS_Serve")], "op": ">=", "value": 40, "style": {"fill": "#d8f3dc"}},
            {
                "columns": [
                    ("Objectifs & Couverture", "Couv_HVC_(%)"),
                    ("Objectifs & Couverture", "Couv_Others_(%)"),
                    ("Objectifs & Couverture", "Couv_Tot_(%)"),
                ],
                "op": "<",
                "value": 70,
                "style": {"fill": "#ffd6d6"},
            },
            {
                "columns": [
                    ("Objectifs & Couverture", "Couv_HVC_(%)"),
                    ("Objectifs & Couverture", "Couv_Others_(%)"),
                    ("Objectifs & Couverture", "Couv_Tot_(%)"),
                ],
                "op": "between",
                "value": (70, 89.999),
                "style": {"fill": "#fff3bf"},
            },
            {
                "columns": [
                    ("Objectifs & Couverture", "Couv_HVC_(%)"),
                    ("Objectifs & Couverture", "Couv_Others_(%)"),
                    ("Objectifs & Couverture", "Couv_Tot_(%)"),
                ],
                "op": ">=",
                "value": 90,
                "style": {"fill": "#d8f3dc"},
            },
        ],
    }


def _pr_caisse_styles() -> dict[str, Any]:
    return {
        "sheet_name": "Performance PR & Caisses",
        "total_row_match": _total_row_match,
        "row_styles": _inactive_pr_caisse_style,
        "formats": {
            ("Temps", "Nb_Jours"): "#,##0",
            ("Dotation & Trésorerie", "Montant"): "#,##0",
            ("Dotation & Trésorerie", "Float_Up"): "#,##0",
            ("HVC_Wallet", "Served"): "#,##0",
            ("HVC_Wallet", "FD"): "#,##0",
            ("HVC_No_Wallet", "Served"): "#,##0",
            ("HVC_No_Wallet", "FD"): "#,##0",
            ("HVC", "Served"): "#,##0",
            ("HVC", "FD"): "#,##0",
            ("Others_Wallet", "Served"): "#,##0",
            ("Others_Wallet", "FD"): "#,##0",
            ("Others_No_Wallet", "Served"): "#,##0",
            ("Others_No_Wallet", "FD"): "#,##0",
            ("Others", "Served"): "#,##0",
            ("Others", "FD"): "#,##0",
            ("Synthèse POS & Finances", "Somme_POS_Serve"): "#,##0",
            ("Synthèse POS & Finances", "Somme_FD"): "#,##0",
            ("Synthèse Wallet", "Total_Served"): "#,##0",
            ("Synthèse Wallet", "Total_FD"): "#,##0",
            ("Synthèse No_Wallet", "Total_Served"): "#,##0",
            ("Synthèse No_Wallet", "Total_FD"): "#,##0",
            ("Objectifs & Couverture", "POS_Attribues"): "#,##0",
            ("Objectifs & Couverture", "POS_Touches"): "#,##0",
            ("Objectifs & Couverture", "Taux_Couverture_(%)"): '0.0"%"',
            ("Objectifs & Couverture", "Taux_Persistance_(%)"): '0.0"%"',
        },
    }


def _cds_styles() -> dict[str, Any]:
    def _inactive_cds(row: pd.Series) -> dict[str, Any]:
        if str(row.iloc[0]).strip().upper() == "TOTAL":
            return {}
        try:
            served = float(row.get(("Synthèse POS", "Somme_POS_Serve"), row.get(("HVC_Wallet", "Served"), 0)))
            ccial_served = float(row.get(("Commerciaux", "Served"), 0))
            if served == 0 and ccial_served == 0:
                return {"fill": "#ffcccc"}
        except Exception:
            pass
        return {}

    return {
        "sheet_name": "Performance CDS",
        "total_row_match": lambda row: str(row.iloc[0]).strip().upper() == "TOTAL",
        "row_styles": _inactive_cds,
        "formats": {
            ("HVC_Wallet", "FD"): "#,##0",
            ("HVC_No_Wallet", "FD"): "#,##0",
            ("Others", "FD"): "#,##0",
            ("Commerciaux", "FD"): "#,##0",
            ("Finances", "Somme_FD"): "#,##0",
            ("Dotation", "Montant"): "#,##0",
            ("Objectifs & Suivi", "Taux_Couverture_(%)"): '0.0"%"',
        },
        "display_formats": {
            ("HVC_Wallet", "FD"): lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) and v != "" else "-",
            ("HVC_No_Wallet", "FD"): lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) and v != "" else "-",
            ("Others", "FD"): lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) and v != "" else "-",
            ("Commerciaux", "FD"): lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) and v != "" else "-",
            ("Finances", "Somme_FD"): lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) and v != "" else "-",
            ("Dotation", "Montant"): lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) and v != "" else "-",
            ("Objectifs & Suivi", "Taux_Couverture_(%)"): lambda v: f"{float(v):.1f}%" if pd.notna(v) and v != "" and v != "N/A" else "N/A",
        },
    }


def _style_cds_display(df: pd.DataFrame):
    """Formatage riche et stylisation du tableau MultiIndex CDS pour Streamlit."""
    if df.empty:
        return df

    fmt = {
        ("HVC_Wallet", "FD"): "{:,.0f}",
        ("HVC_No_Wallet", "FD"): "{:,.0f}",
        ("Others", "FD"): "{:,.0f}",
        ("Commerciaux", "FD"): "{:,.0f}",
        ("Finances", "Somme_FD"): "{:,.0f}",
        ("Dotation", "Montant"): "{:,.0f}",
        ("Objectifs & Suivi", "Taux_Couverture_(%)"): lambda v: f"{float(v):.1f}%" if pd.notna(v) and v != "" and v != "N/A" else "N/A",
    }

    def highlight_inactive(row):
        if str(row.get(("CDS", "Nom"), "")).strip().upper() == "TOTAL":
            return [""] * len(row)
        try:
            pos_served = float(row.get(("Synthèse POS", "Somme_POS_Serve"), 0))
            ccial_served = float(row.get(("Commerciaux", "Served"), 0))
            if pos_served == 0 and ccial_served == 0:
                return ["background-color: #ffd6d6"] * len(row)
        except Exception:
            pass
        return [""] * len(row)

    return df.style.format(fmt).apply(highlight_inactive, axis=1)


def _style_commercial_display(df: pd.DataFrame):
    """Formatage riche et stylisation du tableau MultiIndex Commercial pour Streamlit."""
    if df.empty:
        return df

    int_cols = [
        ("Dotation & Trésorerie", "Montant"),
        ("Dotation & Trésorerie", "FU"),
        ("Temps & Activité", "Nb_Jours"),
        ("Temps & Activité", "Nb_Trans"),
        ("HVC_Wallet", "Served"),
        ("HVC_Wallet", "FD"),
        ("HVC_No_Wallet", "Served"),
        ("HVC_No_Wallet", "FD"),
        ("HVC", "Served"),
        ("HVC", "FD"),
        ("Others_Wallet", "Served"),
        ("Others_Wallet", "FD"),
        ("Others_No_Wallet", "Served"),
        ("Others_No_Wallet", "FD"),
        ("Others", "Served"),
        ("Others", "FD"),
        ("Synthèse Ventes & POS", "Somme_POS_Serve"),
        ("Synthèse Ventes & POS", "Somme_FD"),
        ("Synthèse Wallet", "Total_Served"),
        ("Synthèse Wallet", "Total_FD"),
        ("Synthèse No_Wallet", "Total_Served"),
        ("Synthèse No_Wallet", "Total_FD"),
        ("Objectifs & Couverture", "HVC_Attrib"),
        ("Objectifs & Couverture", "Others_Attrib"),
        ("Objectifs & Couverture", "Tot_Attrib"),
        ("Objectifs & Couverture", "POS_Touches"),
    ]
    pct_cols = [
        ("HVC_Wallet", "TR_(%)"),
        ("HVC_No_Wallet", "TR_(%)"),
        ("HVC", "TR_(%)"),
        ("Others_Wallet", "TR_(%)"),
        ("Others_No_Wallet", "TR_(%)"),
        ("Others", "TR_(%)"),
        ("Synthèse Ventes & POS", "TR_General_(%)"),
        ("Objectifs & Couverture", "Couv_HVC_(%)"),
        ("Objectifs & Couverture", "Couv_Others_(%)"),
        ("Objectifs & Couverture", "Couv_Tot_(%)"),
        ("Objectifs & Couverture", "Persistance_(%)"),
    ]

    fmt = {}
    for c in int_cols:
        if c in df.columns:
            fmt[c] = "{:,.0f}"
    for c in pct_cols:
        if c in df.columns:
            fmt[c] = lambda v: f"{float(v):.1f}%" if pd.notna(v) and v != "" and v != "N/A" else "-"

    def highlight_inactive(row):
        if str(row.iloc[0]).strip().upper() == "TOTAL":
            return [""] * len(row)
        try:
            served = float(row.get(("Synthèse Ventes & POS", "Somme_POS_Serve"), 0))
            trans = float(row.get(("Temps & Activité", "Nb_Trans"), 0))
            if served == 0 and trans == 0:
                return ["background-color: #ffd6d6"] * len(row)
        except Exception:
            pass
        return [""] * len(row)

    styled = df.style.format(fmt).apply(highlight_inactive, axis=1)
    pers_col = ("Objectifs & Couverture", "Persistance_(%)")
    if pers_col in df.columns:
        def color_pers(val):
            if pd.isna(val) or val == "" or val == "N/A":
                return ""
            try:
                v = float(val)
                if v >= 70:
                    return "background-color: #d8f3dc; color: #1b4332;"
                if v >= 40:
                    return "background-color: #fff3bf; color: #5f4700;"
                return "background-color: #ffd6d6; color: #7f1d1d;"
            except Exception:
                return ""
        if hasattr(styled, "map"):
            styled = styled.map(color_pers, subset=[pers_col])
        else:
            styled = styled.applymap(color_pers, subset=[pers_col])
    return styled


def _style_pr_caisse_display(df: pd.DataFrame):
    """Formatage riche et stylisation du tableau MultiIndex PR & Caisses pour Streamlit."""
    if df.empty:
        return df

    int_cols = [
        ("Temps", "Nb_Jours"),
        ("Dotation & Trésorerie", "Montant"),
        ("Dotation & Trésorerie", "Float_Up"),
        ("HVC_Wallet", "Served"),
        ("HVC_Wallet", "FD"),
        ("HVC_No_Wallet", "Served"),
        ("HVC_No_Wallet", "FD"),
        ("HVC", "Served"),
        ("HVC", "FD"),
        ("Others_Wallet", "Served"),
        ("Others_Wallet", "FD"),
        ("Others_No_Wallet", "Served"),
        ("Others_No_Wallet", "FD"),
        ("Others", "Served"),
        ("Others", "FD"),
        ("Synthèse POS & Finances", "Somme_POS_Serve"),
        ("Synthèse POS & Finances", "Somme_FD"),
        ("Synthèse Wallet", "Total_Served"),
        ("Synthèse Wallet", "Total_FD"),
        ("Synthèse No_Wallet", "Total_Served"),
        ("Synthèse No_Wallet", "Total_FD"),
        ("Objectifs & Couverture", "POS_Attribues"),
        ("Objectifs & Couverture", "POS_Touches"),
    ]
    pct_cols = [
        ("Objectifs & Couverture", "Taux_Couverture_(%)"),
        ("Objectifs & Couverture", "Taux_Persistance_(%)"),
    ]

    fmt = {}
    for c in int_cols:
        if c in df.columns:
            fmt[c] = "{:,.0f}"
    for c in pct_cols:
        if c in df.columns:
            fmt[c] = lambda v: f"{float(v):.1f}%" if pd.notna(v) and v != "" and v != "N/A" else "-"

    def highlight_inactive(row):
        if str(row.iloc[0]).strip().upper() == "TOTAL":
            return [""] * len(row)
        try:
            served = float(row.get(("Synthèse POS & Finances", "Somme_POS_Serve"), 0))
            if served == 0:
                return ["background-color: #ffd6d6"] * len(row)
        except Exception:
            pass
        return [""] * len(row)

    styled = df.style.format(fmt).apply(highlight_inactive, axis=1)
    pers_col = ("Objectifs & Couverture", "Taux_Persistance_(%)")
    if pers_col in df.columns:
        def color_pers(val):
            if pd.isna(val) or val == "" or val == "N/A":
                return ""
            try:
                v = float(val)
                if v >= 70:
                    return "background-color: #d8f3dc; color: #1b4332;"
                if v >= 40:
                    return "background-color: #fff3bf; color: #5f4700;"
                return "background-color: #ffd6d6; color: #7f1d1d;"
            except Exception:
                return ""
        if hasattr(styled, "map"):
            styled = styled.map(color_pers, subset=[pers_col])
        else:
            styled = styled.applymap(color_pers, subset=[pers_col])
    return styled


def _render_performance_insights(context: PerformanceContext, display_df: pd.DataFrame) -> None:
    """Espace d'analyse automatisée et synthèse terrain sous chaque tableau de bord."""
    if display_df.empty:
        return

    segment = context.segment
    with st.expander("📝 Synthèse analytique & Insights terrain", expanded=True):
        if segment == "cds":
            _render_cds_insights(display_df, context)
        elif segment == "commercial":
            _render_commercial_insights(display_df, context)
        elif segment == "pr_caisse":
            _render_pr_caisse_insights(display_df, context)


def _render_cds_insights(display_df: pd.DataFrame, context: PerformanceContext) -> None:
    df = display_df[display_df[("CDS", "Nom")].astype(str).str.upper() != "TOTAL"]
    if df.empty:
        st.info("Aucun CDS à analyser.")
        return

    total_cds = len(df)
    active_mask = (
        (pd.to_numeric(df[("Synthèse POS", "Somme_POS_Serve")], errors="coerce").fillna(0) > 0)
        | (pd.to_numeric(df[("Commerciaux", "Served")], errors="coerce").fillna(0) > 0)
        | (pd.to_numeric(df[("Finances", "Somme_FD")], errors="coerce").fillna(0) > 0)
    )
    nb_active = int(active_mask.sum())
    nb_inactive = total_cds - nb_active
    pct_active = (nb_active / total_cds * 100) if total_cds > 0 else 0.0

    tot_hvc_w_served = int(pd.to_numeric(df[("HVC_Wallet", "Served")], errors="coerce").fillna(0).sum())
    tot_hvc_quota = int(pd.to_numeric(df[("Objectifs & Suivi", "Nb_HVC_Attribue")], errors="coerce").fillna(0).sum())
    global_cov = (tot_hvc_w_served / tot_hvc_quota * 100) if tot_hvc_quota > 0 else 0.0

    with_quota = df[pd.to_numeric(df[("Objectifs & Suivi", "Nb_HVC_Attribue")], errors="coerce").fillna(0) > 0]
    best_text = ""
    if not with_quota.empty:
        best_row = with_quota.sort_values(
            [("Objectifs & Suivi", "Taux_Couverture_(%)"), ("HVC_Wallet", "Served")],
            ascending=False,
        ).iloc[0]
        best_nom = best_row[("CDS", "Nom")]
        best_taux = float(best_row[("Objectifs & Suivi", "Taux_Couverture_(%)")])
        best_served = int(best_row[("HVC_Wallet", "Served")])
        best_quota = int(best_row[("Objectifs & Suivi", "Nb_HVC_Attribue")])
        best_text = f"  - 🌟 **Meilleur performeur** : **{best_nom}** avec **{best_taux:.1f}%** de couverture ({best_served}/{best_quota} HVC servis)."

    tot_fd = int(pd.to_numeric(df[("Finances", "Somme_FD")], errors="coerce").fillna(0).sum())
    fd_w = int(pd.to_numeric(df[("HVC_Wallet", "FD")], errors="coerce").fillna(0).sum())
    fd_nw = int(pd.to_numeric(df[("HVC_No_Wallet", "FD")], errors="coerce").fillna(0).sum())
    fd_oth = int(pd.to_numeric(df[("Others", "FD")], errors="coerce").fillna(0).sum())
    fd_ccial = int(pd.to_numeric(df[("Commerciaux", "FD")], errors="coerce").fillna(0).sum())

    pct_w = (fd_w / tot_fd * 100) if tot_fd > 0 else 0.0
    pct_nw = (fd_nw / tot_fd * 100) if tot_fd > 0 else 0.0
    pct_oth = (fd_oth / tot_fd * 100) if tot_fd > 0 else 0.0
    pct_ccial = (fd_ccial / tot_fd * 100) if tot_fd > 0 else 0.0

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### 👥 Activité & Efficacité de Couverture")
        st.markdown(
            f"• **Activité globale** : **{nb_active}/{total_cds} CDS actifs** ({pct_active:.1f}%) sur la période ({nb_inactive} inactif(s)).\n\n"
            f"• **Efficacité portefeuille HVC_Wallet** : Niveau global d'atteinte de **{global_cov:.1f}%** ({tot_hvc_w_served:,} servis sur {tot_hvc_quota:,} attribués).\n"
            + best_text
        )
    with col2:
        st.markdown("##### 💰 Ventilation Financière (FD Distribué)")
        st.markdown(
            f"• **Montant global distribué** : **{tot_fd:,.0f} FCFA** :\n"
            f"  - 🟢 **HVC_Wallet (Portefeuille prioritaire)** : {fd_w:,.0f} FCFA ({pct_w:.1f}%)\n"
            f"  - 🔵 **HVC_No_Wallet (Hors portefeuille)** : {fd_nw:,.0f} FCFA ({pct_nw:.1f}%)\n"
            f"  - ⚪ **Others (Points hors HVC)** : {fd_oth:,.0f} FCFA ({pct_oth:.1f}%)\n"
            f"  - 🔄 **Commerciaux (Réapprovisionnement)** : {fd_ccial:,.0f} FCFA ({pct_ccial:.1f}%)"
        )

    st.markdown("##### ⚠️ Alertes Terrain & Anomalies")
    dot_no_act = df[
        (pd.to_numeric(df[("Dotation", "Montant")], errors="coerce").fillna(0) > 0)
        & (pd.to_numeric(df[("Synthèse POS", "Somme_POS_Serve")], errors="coerce").fillna(0) == 0)
        & (pd.to_numeric(df[("Commerciaux", "Served")], errors="coerce").fillna(0) == 0)
    ]
    exclusive_hp = df[
        (pd.to_numeric(df[("HVC_Wallet", "Served")], errors="coerce").fillna(0) == 0)
        & (
            (pd.to_numeric(df[("HVC_No_Wallet", "Served")], errors="coerce").fillna(0) > 0)
            | (pd.to_numeric(df[("Others", "Served")], errors="coerce").fillna(0) > 0)
        )
    ]
    zero_cov = df[
        (pd.to_numeric(df[("Objectifs & Suivi", "Nb_HVC_Attribue")], errors="coerce").fillna(0) > 0)
        & (pd.to_numeric(df[("Objectifs & Suivi", "Taux_Couverture_(%)")], errors="coerce").fillna(0) == 0.0)
    ]

    alerts = []
    if not dot_no_act.empty:
        noms_dot = ", ".join([f"**{r[('CDS', 'Nom')]}** ({int(r[('Dotation', 'Montant')]):,} FCFA)" for _, r in dot_no_act.iterrows()])
        alerts.append(f"🚨 **Dotation sans activité** : {noms_dot} ont reçu une dotation sans enregistrer d'activité de distribution.")
    if not exclusive_hp.empty:
        noms_hp = ", ".join([f"**{nom}**" for nom in exclusive_hp[("CDS", "Nom")].tolist()])
        alerts.append(f"⚠️ **Distribution hors-portefeuille exclusive** : {noms_hp} n'ont servi aucun client de leur portefeuille HVC mais opèrent hors-portefeuille.")
    if not zero_cov.empty:
        noms_zero = ", ".join([f"**{nom}**" for nom in zero_cov[("CDS", "Nom")].tolist()])
        alerts.append(f"ℹ️ **Couverture nulle** : {noms_zero} enregistrent 0.0% de couverture malgré un portefeuille HVC attribué.")

    if alerts:
        for alert in alerts:
            st.warning(alert)
    else:
        st.success("✅ Aucun signalement critique détecté : alignement opérationnel et portefeuille conforme.")


def _render_commercial_insights(display_df: pd.DataFrame, context: PerformanceContext) -> None:
    df = display_df[~display_df.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
    if df.empty:
        st.info("Aucun commercial à analyser.")
        return

    total_comm = len(df)
    active_mask = (
        (pd.to_numeric(df.get(("Temps & Activité", "Nb_Trans"), 0), errors="coerce").fillna(0) > 0)
        | (pd.to_numeric(df.get(("Synthèse Ventes & POS", "Somme_POS_Serve"), 0), errors="coerce").fillna(0) > 0)
    )
    nb_active = int(active_mask.sum())
    nb_inactive = total_comm - nb_active
    pct_active = (nb_active / total_comm * 100) if total_comm > 0 else 0.0

    tot_attrib = int(pd.to_numeric(df.get(("Objectifs & Couverture", "Tot_Attrib"), 0), errors="coerce").fillna(0).sum())
    tot_touch = int(pd.to_numeric(df.get(("Objectifs & Couverture", "POS_Touches"), 0), errors="coerce").fillna(0).sum())
    global_cov = (tot_touch / tot_attrib * 100) if tot_attrib > 0 else 0.0

    best_text = ""
    with_quota = df[pd.to_numeric(df.get(("Objectifs & Couverture", "Tot_Attrib"), 0), errors="coerce").fillna(0) > 0]
    if not with_quota.empty:
        best_row = with_quota.sort_values(
            [("Objectifs & Couverture", "Couv_Tot_(%)"), ("Objectifs & Couverture", "POS_Touches")], ascending=False
        ).iloc[0]
        best_nom = best_row.get(("Commercial", "Nom"), "")
        best_taux = float(best_row.get(("Objectifs & Couverture", "Couv_Tot_(%)"), 0))
        best_touch = int(best_row.get(("Objectifs & Couverture", "POS_Touches"), 0))
        best_attr = int(best_row.get(("Objectifs & Couverture", "Tot_Attrib"), 0))
        best_text = f"  - 🌟 **Meilleur performeur** : **{best_nom}** avec **{best_taux:.1f}%** de couverture ({best_touch}/{best_attr} POS servis)."

    tot_fd = int(pd.to_numeric(df.get(("Synthèse Ventes & POS", "Somme_FD"), 0), errors="coerce").fillna(0).sum())
    fd_w = int(
        pd.to_numeric(df.get(("HVC_Wallet", "FD"), 0), errors="coerce").fillna(0).sum()
        + pd.to_numeric(df.get(("Others_Wallet", "FD"), 0), errors="coerce").fillna(0).sum()
    )
    fd_nw = int(
        pd.to_numeric(df.get(("HVC_No_Wallet", "FD"), 0), errors="coerce").fillna(0).sum()
        + pd.to_numeric(df.get(("Others_No_Wallet", "FD"), 0), errors="coerce").fillna(0).sum()
    )
    pct_w = (fd_w / tot_fd * 100) if tot_fd > 0 else 0.0
    pct_nw = (fd_nw / tot_fd * 100) if tot_fd > 0 else 0.0

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### 👥 Activité & Couverture Commerciaux")
        st.markdown(
            f"• **Activité globale** : **{nb_active}/{total_comm} commerciaux actifs** ({pct_active:.1f}%) sur la période ({nb_inactive} inactif(s)).\n\n"
            f"• **Couverture portefeuille globale** : **{global_cov:.1f}%** ({tot_touch:,} touchés sur {tot_attrib:,} attribués).\n"
            + best_text
        )
    with col2:
        st.markdown("##### 💰 Ventilation Portefeuille vs Hors Portefeuille")
        st.markdown(
            f"• **Volume total distribué** : **{tot_fd:,.0f} FCFA** :\n"
            f"  - 🟢 **Portefeuille (Wallet)** : {fd_w:,.0f} FCFA ({pct_w:.1f}%)\n"
            f"  - 🔵 **Hors Portefeuille (No Wallet / Conquête)** : {fd_nw:,.0f} FCFA ({pct_nw:.1f}%)"
        )

    st.markdown("##### ⚠️ Alertes Terrain & Anomalies")
    dot_no_tx = df[
        (pd.to_numeric(df.get(("Dotation & Trésorerie", "Montant"), 0), errors="coerce").fillna(0) > 0)
        & (pd.to_numeric(df.get(("Temps & Activité", "Nb_Trans"), 0), errors="coerce").fillna(0) == 0)
    ]
    alerts = []
    if not dot_no_tx.empty:
        noms_dot = ", ".join([f"**{r.get(('Commercial', 'Nom'), '')}** ({int(r.get(('Dotation & Trésorerie', 'Montant'), 0)):,} FCFA)" for _, r in dot_no_tx.iterrows()])
        alerts.append(f"🚨 **Dotation sans transaction** : {noms_dot} ont reçu une dotation sans enregistrer de transaction.")

    if alerts:
        for alert in alerts:
            st.warning(alert)
    else:
        st.success("✅ Aucun signalement critique détecté : activité conforme.")


def _render_pr_caisse_insights(display_df: pd.DataFrame, context: PerformanceContext) -> None:
    df = display_df[~display_df.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
    if df.empty:
        st.info("Aucun point relais / caisse à analyser.")
        return

    total_pts = len(df)
    active_mask = (
        (pd.to_numeric(df.get(("Synthèse POS & Finances", "Somme_POS_Serve"), 0), errors="coerce").fillna(0) > 0)
        | (pd.to_numeric(df.get(("Synthèse POS & Finances", "Somme_FD"), 0), errors="coerce").fillna(0) > 0)
    )
    nb_active = int(active_mask.sum())
    nb_inactive = total_pts - nb_active
    pct_active = (nb_active / total_pts * 100) if total_pts > 0 else 0.0

    tot_fd = int(pd.to_numeric(df.get(("Synthèse POS & Finances", "Somme_FD"), 0), errors="coerce").fillna(0).sum())
    fd_hvc = int(pd.to_numeric(df.get(("HVC", "FD"), 0), errors="coerce").fillna(0).sum())
    fd_oth = int(pd.to_numeric(df.get(("Others", "FD"), 0), errors="coerce").fillna(0).sum())
    fd_hp = int(
        pd.to_numeric(df.get(("HVC_No_Wallet", "FD"), 0), errors="coerce").fillna(0).sum()
        + pd.to_numeric(df.get(("Others_No_Wallet", "FD"), 0), errors="coerce").fillna(0).sum()
    )

    is_pr = df[("Acteur", "Type")].astype(str).str.lower().str.contains("point relais") if ("Acteur", "Type") in df.columns else pd.Series(False, index=df.index)
    pr_df = df[is_pr]
    pr_cov_text = ""
    if not pr_df.empty and ("Objectifs & Couverture", "POS_Attribues") in pr_df.columns:
        tot_attr_pr = int(pd.to_numeric(pr_df[("Objectifs & Couverture", "POS_Attribues")], errors="coerce").fillna(0).sum())
        tot_touch_pr = int(pd.to_numeric(pr_df[("Objectifs & Couverture", "POS_Touches")], errors="coerce").fillna(0).sum())
        pr_cov = (tot_touch_pr / tot_attr_pr * 100) if tot_attr_pr > 0 else 0.0
        pr_cov_text = f"\n\n• **Couverture Portefeuille Point Relais** : **{pr_cov:.1f}%** ({tot_touch_pr:,} touchés sur {tot_attr_pr:,} attribués)."

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### 👥 Activité Réseau Points Relais & Caisses")
        st.markdown(
            f"• **Activité globale** : **{nb_active}/{total_pts} acteurs actifs** ({pct_active:.1f}%) sur la période ({nb_inactive} inactif(s))."
            + pr_cov_text
        )
    with col2:
        st.markdown("##### 💰 Ventilation Financière")
        st.markdown(
            f"• **Volume total distribué** : **{tot_fd:,.0f} FCFA** :\n"
            f"  - 🟢 **Segment HVC** : {fd_hvc:,.0f} FCFA\n"
            f"  - ⚪ **Segment Others (MVC/LVC)** : {fd_oth:,.0f} FCFA\n"
            f"  - 🔵 **Hors Portefeuille** : {fd_hp:,.0f} FCFA"
        )

    dot_no_act = df[
        (pd.to_numeric(df.get(("Dotation & Trésorerie", "Montant"), 0), errors="coerce").fillna(0) > 0)
        & (pd.to_numeric(df.get(("Synthèse POS & Finances", "Somme_POS_Serve"), 0), errors="coerce").fillna(0) == 0)
    ]
    if not dot_no_act.empty:
        st.markdown("##### ⚠️ Alertes Terrain")
        noms = ", ".join([f"**{r.get(('Acteur', 'Nom'), '')}** ({int(r.get(('Dotation & Trésorerie', 'Montant'), 0)):,} FCFA)" for _, r in dot_no_act.iterrows()])
        st.warning(f"🚨 **Dotation sans distribution** : {noms} ont reçu une dotation sans POS servi.")


def _style_persistence(df: pd.DataFrame):
    """Colorise la persistance: vert >=70, jaune 40-69, rouge <40."""
    column = "Persistance (%)" if "Persistance (%)" in df.columns else "Taux_Persistance (%)"
    if column not in df.columns:
        return df

    def color(value: Any) -> str:
        if pd.isna(value) or value == "" or value == "N/A":
            return ""
        try:
            val = float(value)
        except (ValueError, TypeError):
            return ""
            
        if val >= 70:
            return "background-color: #d8f3dc; color: #1b4332;"
        if val >= 40:
            return "background-color: #fff3bf; color: #5f4700;"
        return "background-color: #ffd6d6; color: #7f1d1d;"

    def format_pct(value: Any) -> str:
        if pd.isna(value) or value == "" or value == "N/A":
            return ""
        try:
            return f"{float(value):.1f}%"
        except (ValueError, TypeError):
            return str(value)

    # Use map if pandas version allows, otherwise applymap
    if hasattr(df.style, 'map'):
        return df.style.map(color, subset=[column]).format({column: format_pct})
    return df.style.applymap(color, subset=[column]).format({column: format_pct})


# ---------------------------------------------------------------------------
# Export — services/export_service.py exclusivement
# ---------------------------------------------------------------------------

def _split_commercial_views(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Sépare le tableau commercial en trois sections strictes MultiIndex :
    1. DSM Activity : Activité générale, dotation, FU, heures, transactions, HVC (Served, FD, TR), Others (Served, FD, TR), Synthèse Ventes & POS et fenêtres TPW.
    2. Couverture - Wallet : Commercial, Objectifs & Couverture, HVC_Wallet, Others_Wallet, Synthèse Wallet.
    3. Couverture - No Wallet : Commercial, HVC_No_Wallet, Others_No_Wallet, Synthèse No_Wallet.
    """
    # 1. DSM Activity
    dsm_base_cols = [
        ("Commercial", "Zone_Centre"), ("Commercial", "Zone_SA"), ("Commercial", "Nom"), ("Commercial", "Numero"),
        ("Temps & Activité", "Nb_Jours"), ("Temps & Activité", "1ere_Trans"), ("Temps & Activité", "Dern_Trans"), ("Temps & Activité", "Nb_Trans"),
        ("Dotation & Trésorerie", "Heure"), ("Dotation & Trésorerie", "Montant"), ("Dotation & Trésorerie", "FU"),
        ("HVC", "Served"), ("HVC", "FD"), ("HVC", "TR_(%)"),
        ("Others", "Served"), ("Others", "FD"), ("Others", "TR_(%)"),
        ("Synthèse Ventes & POS", "Somme_POS_Serve"), ("Synthèse Ventes & POS", "Somme_FD"), ("Synthèse Ventes & POS", "TR_General_(%)"),
    ]
    tpw_cols = [c for c in df.columns if isinstance(c, tuple) and c[0] == "Work Progress"]
    dsm_cols = [c for c in dsm_base_cols if c in df.columns] + tpw_cols
    dsm_df = df[dsm_cols].copy()

    # 2. Couverture - Wallet
    cov_wallet_cols = [
        ("Commercial", "Zone_Centre"), ("Commercial", "Zone_SA"), ("Commercial", "Nom"), ("Commercial", "Numero"),
        ("Objectifs & Couverture", "HVC_Attrib"), ("Objectifs & Couverture", "Others_Attrib"), ("Objectifs & Couverture", "Tot_Attrib"),
        ("Objectifs & Couverture", "POS_Touches"),
        ("Objectifs & Couverture", "Couv_HVC_(%)"), ("Objectifs & Couverture", "Couv_Others_(%)"), ("Objectifs & Couverture", "Couv_Tot_(%)"),
        ("Objectifs & Couverture", "Persistance_(%)"),
        ("HVC_Wallet", "Served"), ("HVC_Wallet", "FD"), ("HVC_Wallet", "TR_(%)"),
        ("Others_Wallet", "Served"), ("Others_Wallet", "FD"), ("Others_Wallet", "TR_(%)"),
    ]
    cov_wallet_df = df[[c for c in cov_wallet_cols if c in df.columns]].copy()
    hvc_w_s = pd.to_numeric(cov_wallet_df.get(("HVC_Wallet", "Served"), 0), errors="coerce").fillna(0)
    oth_w_s = pd.to_numeric(cov_wallet_df.get(("Others_Wallet", "Served"), 0), errors="coerce").fillna(0)
    hvc_w_fd = pd.to_numeric(cov_wallet_df.get(("HVC_Wallet", "FD"), 0), errors="coerce").fillna(0)
    oth_w_fd = pd.to_numeric(cov_wallet_df.get(("Others_Wallet", "FD"), 0), errors="coerce").fillna(0)
    cov_wallet_df[("Synthèse Wallet", "Total_Served")] = (hvc_w_s + oth_w_s).astype(int)
    cov_wallet_df[("Synthèse Wallet", "Total_FD")] = (hvc_w_fd + oth_w_fd).astype(int)

    # 3. Couverture - No Wallet
    cov_no_wallet_cols = [
        ("Commercial", "Zone_Centre"), ("Commercial", "Zone_SA"), ("Commercial", "Nom"), ("Commercial", "Numero"),
        ("HVC_No_Wallet", "Served"), ("HVC_No_Wallet", "FD"), ("HVC_No_Wallet", "TR_(%)"),
        ("Others_No_Wallet", "Served"), ("Others_No_Wallet", "FD"), ("Others_No_Wallet", "TR_(%)"),
    ]
    cov_no_wallet_df = df[[c for c in cov_no_wallet_cols if c in df.columns]].copy()
    hvc_nw_s = pd.to_numeric(cov_no_wallet_df.get(("HVC_No_Wallet", "Served"), 0), errors="coerce").fillna(0)
    oth_nw_s = pd.to_numeric(cov_no_wallet_df.get(("Others_No_Wallet", "Served"), 0), errors="coerce").fillna(0)
    hvc_nw_fd = pd.to_numeric(cov_no_wallet_df.get(("HVC_No_Wallet", "FD"), 0), errors="coerce").fillna(0)
    oth_nw_fd = pd.to_numeric(cov_no_wallet_df.get(("Others_No_Wallet", "FD"), 0), errors="coerce").fillna(0)
    cov_no_wallet_df[("Synthèse No_Wallet", "Total_Served")] = (hvc_nw_s + oth_nw_s).astype(int)
    cov_no_wallet_df[("Synthèse No_Wallet", "Total_FD")] = (hvc_nw_fd + oth_nw_fd).astype(int)

    return dsm_df, cov_wallet_df, cov_no_wallet_df


def _split_pr_caisse_views(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Sépare le tableau PR & Caisses en 3 sous-vues MultiIndex :
    1. DSM Activity : Acteur, Temps, Dotation & Trésorerie, HVC (Served, FD), Others (Served, FD), Synthèse POS & Finances.
    2. Couverture - Wallet : Acteur, Objectifs & Couverture, HVC_Wallet, Others_Wallet, Synthèse Wallet.
    3. Couverture - No Wallet : Acteur, HVC_No_Wallet, Others_No_Wallet, Synthèse No_Wallet.
    """
    # 1. DSM Activity
    dsm_cols = [
        ("Acteur", "Type"), ("Acteur", "Territoire"), ("Acteur", "Localisation"), ("Acteur", "Nom"),
        ("Temps", "Nb_Jours"),
        ("Dotation & Trésorerie", "Nom"), ("Dotation & Trésorerie", "Montant"), ("Dotation & Trésorerie", "Float_Up"),
        ("HVC", "Served"), ("HVC", "FD"),
        ("Others", "Served"), ("Others", "FD"),
        ("Synthèse POS & Finances", "Somme_POS_Serve"), ("Synthèse POS & Finances", "Somme_FD"),
    ]
    dsm_df = df[[c for c in dsm_cols if c in df.columns]].copy()

    # 2. Couverture - Wallet
    cov_wallet_cols = [
        ("Acteur", "Type"), ("Acteur", "Territoire"), ("Acteur", "Localisation"), ("Acteur", "Nom"),
        ("Objectifs & Couverture", "POS_Attribues"), ("Objectifs & Couverture", "POS_Touches"),
        ("Objectifs & Couverture", "Taux_Couverture_(%)"), ("Objectifs & Couverture", "Taux_Persistance_(%)"),
        ("HVC_Wallet", "Served"), ("HVC_Wallet", "FD"),
        ("Others_Wallet", "Served"), ("Others_Wallet", "FD"),
    ]
    cov_wallet_df = df[[c for c in cov_wallet_cols if c in df.columns]].copy()
    hvc_w_s = pd.to_numeric(cov_wallet_df.get(("HVC_Wallet", "Served"), 0), errors="coerce").fillna(0)
    oth_w_s = pd.to_numeric(cov_wallet_df.get(("Others_Wallet", "Served"), 0), errors="coerce").fillna(0)
    hvc_w_fd = pd.to_numeric(cov_wallet_df.get(("HVC_Wallet", "FD"), 0), errors="coerce").fillna(0)
    oth_w_fd = pd.to_numeric(cov_wallet_df.get(("Others_Wallet", "FD"), 0), errors="coerce").fillna(0)
    cov_wallet_df[("Synthèse Wallet", "Total_Served")] = (hvc_w_s + oth_w_s).astype(int)
    cov_wallet_df[("Synthèse Wallet", "Total_FD")] = (hvc_w_fd + oth_w_fd).astype(int)

    # 3. Couverture - No Wallet
    cov_no_wallet_cols = [
        ("Acteur", "Type"), ("Acteur", "Territoire"), ("Acteur", "Localisation"), ("Acteur", "Nom"),
        ("HVC_No_Wallet", "Served"), ("HVC_No_Wallet", "FD"),
        ("Others_No_Wallet", "Served"), ("Others_No_Wallet", "FD"),
    ]
    cov_no_wallet_df = df[[c for c in cov_no_wallet_cols if c in df.columns]].copy()
    hvc_nw_s = pd.to_numeric(cov_no_wallet_df.get(("HVC_No_Wallet", "Served"), 0), errors="coerce").fillna(0)
    oth_nw_s = pd.to_numeric(cov_no_wallet_df.get(("Others_No_Wallet", "Served"), 0), errors="coerce").fillna(0)
    hvc_nw_fd = pd.to_numeric(cov_no_wallet_df.get(("HVC_No_Wallet", "FD"), 0), errors="coerce").fillna(0)
    oth_nw_fd = pd.to_numeric(cov_no_wallet_df.get(("Others_No_Wallet", "FD"), 0), errors="coerce").fillna(0)
    cov_no_wallet_df[("Synthèse No_Wallet", "Total_Served")] = (hvc_nw_s + oth_nw_s).astype(int)
    cov_no_wallet_df[("Synthèse No_Wallet", "Total_FD")] = (hvc_nw_fd + oth_nw_fd).astype(int)

    return dsm_df, cov_wallet_df, cov_no_wallet_df


def _render_sub_export(
    prefix: str,
    df: pd.DataFrame,
    styles: dict[str, Any],
    group_col: Optional[Any],
    title: str,
    key_suffix: str,
) -> None:
    has_group = bool(group_col) and group_col in df.columns
    cols = st.columns(4 if has_group else 3)

    cols[0].download_button(
        "CSV",
        to_csv(df),
        f"{prefix}.csv",
        "text/csv",
        key=f"{prefix}_csv_{key_suffix}",
    )

    cols[1].download_button(
        "Excel",
        to_excel(df, styles=styles, sheet_name=styles.get("sheet_name", title)),
        f"{prefix}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{prefix}_xlsx_{key_suffix}",
    )

    with cols[2]:
        img_key = f"{prefix}_img_{key_suffix}"
        if st.button(f"🖼️ Générer l'image ({title})", key=f"btn_img_{key_suffix}"):
            with st.spinner(f"Génération de l'image {title}..."):
                st.session_state[img_key] = to_image(df, styles=styles, title=title)
        if st.session_state.get(img_key):
            st.download_button(
                f"⬇️ Télécharger l'image ({title})",
                st.session_state[img_key],
                f"{prefix}.png",
                "image/png",
                key=f"dl_img_{key_suffix}",
            )

    if has_group:
        with cols[3]:
            zip_key = f"{prefix}_zip_{key_suffix}"
            if st.button(f"📦 Générer ZIP ({title})", key=f"btn_zip_{key_suffix}"):
                with st.spinner(f"Génération du ZIP {title}..."):
                    groupable = df[df[group_col].astype(str) != ""]
                    groupable = groupable[~groupable.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
                    st.session_state[zip_key] = to_grouped_zip(
                        groupable,
                        group_col=group_col,
                        export="image",
                        styles=styles,
                        filename_prefix=prefix,
                    )
            if st.session_state.get(zip_key):
                st.download_button(
                    f"⬇️ Télécharger le ZIP ({title})",
                    st.session_state[zip_key],
                    f"{prefix}_par_groupe.zip",
                    "application/zip",
                    key=f"dl_zip_{key_suffix}",
                )


def _render_commercial_exports(
    prefix: str,
    full_df: pd.DataFrame,
    styles: dict[str, Any],
    group_col: Optional[Any],
    filter_sig: str,
) -> None:
    dsm_df, cov_wallet_df, cov_no_wallet_df = _split_commercial_views(full_df)
    has_group = bool(group_col) and group_col in full_df.columns

    st.markdown("##### 1. 📊 Section DSM Activity (Activité Opérationnelle & Ventes Générales)")
    st.caption("Contient l'activité opérationnelle, dotation, float-up, transactions, volumes ventes globaux, POS servis globaux, rotations globales et fenêtres TPW. Exclut portefeuille et couverture.")
    dsm_styles = {**styles, "sheet_name": "DSM Activity"}
    _render_sub_export(
        prefix=f"{prefix}_dsm_activity",
        df=dsm_df,
        styles=dsm_styles,
        group_col=group_col if has_group else None,
        title="DSM Activity",
        key_suffix=f"dsm_{filter_sig}",
    )

    st.write("")
    st.markdown("##### 2. 🎯 Section Taux de Couverture (Portefeuille & Hors Portefeuille)")
    st.caption("Comprend les deux volets distincts : Couverture - Wallet (Portefeuille Attribué) et Couverture - No Wallet (Hors Portefeuille / Conquête & Dépannage).")

    col_excel, col_img_w, col_img_nw = st.columns([1.5, 1.5, 1.5])
    with col_excel:
        cov_sheets = {
            "Couverture - Wallet": cov_wallet_df,
            "Couverture - No Wallet": cov_no_wallet_df,
        }
        excel_cov_bytes = to_excel_sheets(cov_sheets, styles=styles)
        st.download_button(
            label="📊 Télécharger Excel Couverture [.xlsx] (2 Feuilles)",
            data=excel_cov_bytes,
            file_name=f"{prefix}_taux_couverture.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{prefix}_btn_excel_cov_{filter_sig}",
        )

    with col_img_w:
        img_w_key = f"{prefix}_img_wallet_{filter_sig}"
        if st.button("🖼️ Générer l'image (Wallet - Portefeuille)", key=f"btn_img_wallet_{filter_sig}"):
            with st.spinner("Génération de l'image (Wallet)..."):
                w_styles = {**styles, "sheet_name": "Couverture - Wallet"}
                st.session_state[img_w_key] = to_image(cov_wallet_df, styles=w_styles, title="Couverture - Wallet")
        if st.session_state.get(img_w_key):
            st.download_button(
                "⬇️ Télécharger l'image (Wallet)",
                st.session_state[img_w_key],
                f"{prefix}_couverture_wallet.png",
                "image/png",
                key=f"dl_img_wallet_{filter_sig}",
            )

    with col_img_nw:
        img_nw_key = f"{prefix}_img_nowallet_{filter_sig}"
        if st.button("🖼️ Générer l'image (No Wallet - Hors Portefeuille)", key=f"btn_img_nowallet_{filter_sig}"):
            with st.spinner("Génération de l'image (No Wallet)..."):
                nw_styles = {**styles, "sheet_name": "Couverture - No Wallet"}
                st.session_state[img_nw_key] = to_image(cov_no_wallet_df, styles=nw_styles, title="Couverture - No Wallet")
        if st.session_state.get(img_nw_key):
            st.download_button(
                "⬇️ Télécharger l'image (No Wallet)",
                st.session_state[img_nw_key],
                f"{prefix}_couverture_nowallet.png",
                "image/png",
                key=f"dl_img_nowallet_{filter_sig}",
            )

    col_csv_w, col_csv_nw = st.columns([1.5, 1.5])
    with col_csv_w:
        st.download_button(
            "CSV (Wallet - Portefeuille)",
            to_csv(cov_wallet_df),
            f"{prefix}_couverture_wallet.csv",
            "text/csv",
            key=f"{prefix}_csv_wallet_{filter_sig}",
        )
    with col_csv_nw:
        st.download_button(
            "CSV (No Wallet - Hors Portefeuille)",
            to_csv(cov_no_wallet_df),
            f"{prefix}_couverture_nowallet.csv",
            "text/csv",
            key=f"{prefix}_csv_nowallet_{filter_sig}",
        )

    if has_group:
        col_zip_w, col_zip_nw = st.columns([1.5, 1.5])
        with col_zip_w:
            zip_w_key = f"{prefix}_zip_wallet_{filter_sig}"
            if st.button("📦 Générer ZIP (Wallet par centre)", key=f"btn_zip_wallet_{filter_sig}"):
                with st.spinner("Génération du ZIP (Wallet)..."):
                    groupable_w = cov_wallet_df[cov_wallet_df[group_col].astype(str) != ""]
                    groupable_w = groupable_w[~groupable_w.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
                    st.session_state[zip_w_key] = to_grouped_zip(
                        groupable_w,
                        group_col=group_col,
                        export="image",
                        styles={**styles, "sheet_name": "Couverture - Wallet"},
                        filename_prefix=f"{prefix}_wallet",
                    )
            if st.session_state.get(zip_w_key):
                st.download_button(
                    "⬇️ Télécharger ZIP (Wallet)",
                    st.session_state[zip_w_key],
                    f"{prefix}_wallet_par_groupe.zip",
                    "application/zip",
                    key=f"dl_zip_wallet_{filter_sig}",
                )
        with col_zip_nw:
            zip_nw_key = f"{prefix}_zip_nowallet_{filter_sig}"
            if st.button("📦 Générer ZIP (No Wallet par centre)", key=f"btn_zip_nowallet_{filter_sig}"):
                with st.spinner("Génération du ZIP (No Wallet)..."):
                    groupable_nw = cov_no_wallet_df[cov_no_wallet_df[group_col].astype(str) != ""]
                    groupable_nw = groupable_nw[~groupable_nw.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
                    st.session_state[zip_nw_key] = to_grouped_zip(
                        groupable_nw,
                        group_col=group_col,
                        export="image",
                        styles={**styles, "sheet_name": "Couverture - No Wallet"},
                        filename_prefix=f"{prefix}_nowallet",
                    )
            if st.session_state.get(zip_nw_key):
                st.download_button(
                    "⬇️ Télécharger ZIP (No Wallet)",
                    st.session_state[zip_nw_key],
                    f"{prefix}_nowallet_par_groupe.zip",
                    "application/zip",
                    key=f"dl_zip_nowallet_{filter_sig}",
                )

    st.write("")
    st.markdown("##### 3. 📑 Classeur Excel Complet (3 Feuilles : DSM Activity + Couverture Wallet + Couverture No Wallet)")
    st.caption("Exporte un classeur Excel unique réunissant les trois sections dans trois feuilles distinctes.")
    col_full, _ = st.columns([2, 2])
    with col_full:
        sheets_all = {
            "DSM Activity": dsm_df,
            "Couverture - Wallet": cov_wallet_df,
            "Couverture - No Wallet": cov_no_wallet_df,
        }
        excel_all_bytes = to_excel_sheets(sheets_all, styles=styles)
        st.download_button(
            label="📑 Télécharger Excel Complet [.xlsx] (3 Feuilles)",
            data=excel_all_bytes,
            file_name=f"{prefix}_complet_3feuilles.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{prefix}_btn_excel_combined_{filter_sig}",
        )


def _render_pr_caisse_exports(
    prefix: str,
    full_df: pd.DataFrame,
    styles: dict[str, Any],
    group_col: Optional[Any],
    filter_sig: str,
) -> None:
    dsm_df, cov_wallet_df, cov_no_wallet_df = _split_pr_caisse_views(full_df)
    has_group = bool(group_col) and group_col in full_df.columns

    st.markdown("##### 1. 📊 Section DSM Activity (Activité Générale & Distribution)")
    st.caption("Contient l'activité générale, dotation, float-up, HVC total, Others total et synthèse globale.")
    dsm_styles = {**styles, "sheet_name": "DSM Activity"}
    _render_sub_export(
        prefix=f"{prefix}_dsm_activity",
        df=dsm_df,
        styles=dsm_styles,
        group_col=group_col if has_group else None,
        title="DSM Activity",
        key_suffix=f"dsm_{filter_sig}",
    )

    st.write("")
    st.markdown("##### 2. 🎯 Section Taux de Couverture (Portefeuille Point Relais & Hors Portefeuille)")
    st.caption("Comprend les deux volets distincts : Couverture - Wallet (Portefeuille Attribué PR) et Couverture - No Wallet (Hors Portefeuille / Vente Libre).")

    col_excel, col_img_w, col_img_nw = st.columns([1.5, 1.5, 1.5])
    with col_excel:
        cov_sheets = {
            "Couverture - Wallet": cov_wallet_df,
            "Couverture - No Wallet": cov_no_wallet_df,
        }
        excel_cov_bytes = to_excel_sheets(cov_sheets, styles=styles)
        st.download_button(
            label="📊 Télécharger Excel Couverture [.xlsx] (2 Feuilles)",
            data=excel_cov_bytes,
            file_name=f"{prefix}_taux_couverture.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{prefix}_btn_excel_cov_{filter_sig}",
        )

    with col_img_w:
        img_w_key = f"{prefix}_img_wallet_{filter_sig}"
        if st.button("🖼️ Générer l'image (Wallet - Portefeuille)", key=f"btn_img_wallet_{filter_sig}"):
            with st.spinner("Génération de l'image (Wallet)..."):
                w_styles = {**styles, "sheet_name": "Couverture - Wallet"}
                st.session_state[img_w_key] = to_image(cov_wallet_df, styles=w_styles, title="Couverture - Wallet")
        if st.session_state.get(img_w_key):
            st.download_button(
                "⬇️ Télécharger l'image (Wallet)",
                st.session_state[img_w_key],
                f"{prefix}_couverture_wallet.png",
                "image/png",
                key=f"dl_img_wallet_{filter_sig}",
            )

    with col_img_nw:
        img_nw_key = f"{prefix}_img_nowallet_{filter_sig}"
        if st.button("🖼️ Générer l'image (No Wallet - Hors Portefeuille)", key=f"btn_img_nowallet_{filter_sig}"):
            with st.spinner("Génération de l'image (No Wallet)..."):
                nw_styles = {**styles, "sheet_name": "Couverture - No Wallet"}
                st.session_state[img_nw_key] = to_image(cov_no_wallet_df, styles=nw_styles, title="Couverture - No Wallet")
        if st.session_state.get(img_nw_key):
            st.download_button(
                "⬇️ Télécharger l'image (No Wallet)",
                st.session_state[img_nw_key],
                f"{prefix}_couverture_nowallet.png",
                "image/png",
                key=f"dl_img_nowallet_{filter_sig}",
            )

    col_csv_w, col_csv_nw = st.columns([1.5, 1.5])
    with col_csv_w:
        st.download_button(
            "CSV (Wallet - Portefeuille)",
            to_csv(cov_wallet_df),
            f"{prefix}_couverture_wallet.csv",
            "text/csv",
            key=f"{prefix}_csv_wallet_{filter_sig}",
        )
    with col_csv_nw:
        st.download_button(
            "CSV (No Wallet - Hors Portefeuille)",
            to_csv(cov_no_wallet_df),
            f"{prefix}_couverture_nowallet.csv",
            "text/csv",
            key=f"{prefix}_csv_nowallet_{filter_sig}",
        )

    if has_group:
        col_zip_w, col_zip_nw = st.columns([1.5, 1.5])
        with col_zip_w:
            zip_w_key = f"{prefix}_zip_wallet_{filter_sig}"
            if st.button("📦 Générer ZIP (Wallet par territoire)", key=f"btn_zip_wallet_{filter_sig}"):
                with st.spinner("Génération du ZIP (Wallet)..."):
                    groupable_w = cov_wallet_df[cov_wallet_df[group_col].astype(str) != ""]
                    groupable_w = groupable_w[~groupable_w.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
                    st.session_state[zip_w_key] = to_grouped_zip(
                        groupable_w,
                        group_col=group_col,
                        export="image",
                        styles={**styles, "sheet_name": "Couverture - Wallet"},
                        filename_prefix=f"{prefix}_wallet",
                    )
            if st.session_state.get(zip_w_key):
                st.download_button(
                    "⬇️ Télécharger ZIP (Wallet)",
                    st.session_state[zip_w_key],
                    f"{prefix}_wallet_par_territoire.zip",
                    "application/zip",
                    key=f"dl_zip_wallet_{filter_sig}",
                )
        with col_zip_nw:
            zip_nw_key = f"{prefix}_zip_nowallet_{filter_sig}"
            if st.button("📦 Générer ZIP (No Wallet par territoire)", key=f"btn_zip_nowallet_{filter_sig}"):
                with st.spinner("Génération du ZIP (No Wallet)..."):
                    groupable_nw = cov_no_wallet_df[cov_no_wallet_df[group_col].astype(str) != ""]
                    groupable_nw = groupable_nw[~groupable_nw.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
                    st.session_state[zip_nw_key] = to_grouped_zip(
                        groupable_nw,
                        group_col=group_col,
                        export="image",
                        styles={**styles, "sheet_name": "Couverture - No Wallet"},
                        filename_prefix=f"{prefix}_nowallet",
                    )
            if st.session_state.get(zip_nw_key):
                st.download_button(
                    "⬇️ Télécharger ZIP (No Wallet)",
                    st.session_state[zip_nw_key],
                    f"{prefix}_nowallet_par_territoire.zip",
                    "application/zip",
                    key=f"dl_zip_nowallet_{filter_sig}",
                )

    st.write("")
    st.markdown("##### 3. 📑 Classeur Excel Complet (3 Feuilles : DSM Activity + Couverture Wallet + Couverture No Wallet)")
    st.caption("Exporte un classeur Excel unique réunissant les trois sections dans trois feuilles distinctes.")
    col_full, _ = st.columns([2, 2])
    with col_full:
        sheets_all = {
            "DSM Activity": dsm_df,
            "Couverture - Wallet": cov_wallet_df,
            "Couverture - No Wallet": cov_no_wallet_df,
        }
        excel_all_bytes = to_excel_sheets(sheets_all, styles=styles)
        st.download_button(
            label="📑 Télécharger Excel Complet [.xlsx] (3 Feuilles)",
            data=excel_all_bytes,
            file_name=f"{prefix}_complet_3feuilles.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{prefix}_btn_excel_combined_{filter_sig}",
        )


def _render_exports(prefix: str, df: pd.DataFrame, styles: dict[str, Any], group_col: Optional[Any]) -> None:
    has_group_export = bool(group_col) and group_col in df.columns
    cols = st.columns(4 if has_group_export else 3)

    cols[0].download_button("CSV", to_csv(df), f"{prefix}.csv", "text/csv", key=f"{prefix}_csv")

    cols[1].download_button(
        "Excel",
        to_excel(df, styles=styles, sheet_name=styles.get("sheet_name", "Performance")),
        f"{prefix}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{prefix}_xlsx",
    )

    with cols[2]:
        img_key = f"{prefix}_image_bytes"
        if st.button("🖼️ Générer l'image", key=f"{prefix}_btn_gen_img"):
            with st.spinner("Génération de l'image..."):
                st.session_state[img_key] = to_image(df, styles=styles, title=prefix)
        if st.session_state.get(img_key):
            st.download_button(
                "⬇️ Télécharger l'image",
                st.session_state[img_key],
                f"{prefix}.png",
                "image/png",
                key=f"{prefix}_png",
            )

    if has_group_export:
        with cols[3]:
            zip_key = f"{prefix}_zip_bytes"
            if st.button("🖼️ Générer ZIP par groupe", key=f"{prefix}_btn_gen_zip"):
                with st.spinner("Génération du ZIP..."):
                    groupable = df[df[group_col].astype(str) != ""]
                    groupable = groupable[~groupable.iloc[:, 0].astype(str).str.upper().eq("TOTAL")]
                    st.session_state[zip_key] = to_grouped_zip(
                        groupable, group_col=group_col,
                        export="image", styles=styles,
                        filename_prefix=prefix
                    )
            if st.session_state.get(zip_key):
                st.download_button(
                    "⬇️ Télécharger le ZIP",
                    st.session_state[zip_key],
                    f"{prefix}_par_groupe.zip",
                    "application/zip",
                    key=f"{prefix}_zip",
                )


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _col(df: pd.DataFrame, name: str, default: Any = 0) -> pd.Series:
    """
    Retourne df[name] si la colonne existe, sinon une Series remplie de
    `default` alignee sur df.index (meme longueur). C'est le correctif
    central : df.get(name, default) renvoie le litteral `default` (un
    int/str/None), PAS une Series, quand la colonne est absente — et
    plantait donc tout appel a .fillna()/.apply() en aval. Toute lecture de
    colonne potentiellement absente du controller doit passer par cette
    fonction, jamais par df.get(...) direct ou df[...] direct.
    """
    if name in df.columns:
        return df[name]
    return pd.Series(default, index=df.index)


def _minutes_to_time(value: Any) -> str:
    if pd.isna(value):
        return "N/A"
    try:
        total = int(round(float(value)))
    except Exception:
        return "N/A"
    h, m = divmod(total, 60)
    return f"{h:02d}:{m:02d}"


def _to_int(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0).astype(int)


def _to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _date_range_to_strings(selected_dates: Any) -> tuple[Optional[str], Optional[str]]:
    if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
        start, end = selected_dates
    elif selected_dates:
        start = end = selected_dates
    else:
        return None, None
    return pd.to_datetime(start).date().isoformat(), pd.to_datetime(end).date().isoformat()
