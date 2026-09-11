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
from typing import Any, Optional

import pandas as pd
import streamlit as st

from controllers.performance_controller import (
    WORK_PROGRESS_WINDOWS,
    PerformanceContext,
    PerformanceFilters,
    build_performance_context,
    load_filter_options,
)
from ingestion.upload_sync import (
    Segment,
    list_available_folders,
    sync_segment_to_sqlite,
    upload_segment_files,
)
from services.export_service import to_csv, to_excel, to_grouped_zip, to_image

WORK_PROGRESS_LABELS = list(WORK_PROGRESS_WINDOWS.keys())

_SEGMENT_LABELS = {
    "commercial": "Commerciaux",
    "pr_caisse": "POS relais & Caisses",
    "cds": "CDS",
}

_CDS_DISPLAY_ONLY_COLUMNS = ["FD_Commercial", "Ccial_Serve"]


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


def _render_segment_tab(segment: Segment) -> None:
    _render_upload_sync_section(segment)
    st.divider()

    filters = render_performance_filters(segment)
    context = _build_performance_context_cached(filters)
    render_performance(context)


@st.cache_data(show_spinner=False)
def _load_filter_options_cached(segment: Segment) -> dict[str, Any]:
    return load_filter_options(segment)


@st.cache_resource(show_spinner=False, ttl=120)
def _build_performance_context_cached(filters: PerformanceFilters) -> PerformanceContext:
    return build_performance_context(filters)


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

    if segment == "commercial":
        with col_a:
            zone_centre = st.selectbox("Centre", ["Toutes", "Centre II", "Centre III"], key=f"{prefix}_centre")
        with col_b:
            zone_territoire = st.selectbox(
                "Territoire", ["Toutes"] + list(options.get("zone_territoire", [])), key=f"{prefix}_territoire"
            )
        with col_c:
            zone_sa = st.selectbox("Zone_SA", ["Toutes"] + list(options.get("zone_sa", [])), key=f"{prefix}_zone_sa")

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
    )


# ---------------------------------------------------------------------------
# Rendu d'un segment
# ---------------------------------------------------------------------------

def render_performance(context: PerformanceContext) -> None:
    st.subheader(f"Performance {_SEGMENT_LABELS.get(context.segment, context.segment)}")

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

    display_with_total = _append_total_row(display_df, context.segment)

    st.dataframe(display_with_total, use_container_width=True, hide_index=True, height=600)

    # Top 10 : commercial + pr_caisse uniquement (context.top_commerciaux est
    # vide pour cds par construction, jamais calculé côté controller).
    if not context.top_commerciaux.empty:
        st.markdown("#### Top 10")
        st.dataframe(_build_top10_display(context), use_container_width=True, hide_index=True)

    st.markdown("#### Export")
    export_df = _prepare_export_df(display_with_total, context.segment)
    _render_exports(f"performance_{context.segment}", export_df, styles, group_col)


# ---------------------------------------------------------------------------
# Construction des tableaux d'affichage par segment
# ---------------------------------------------------------------------------

def _build_display(context: PerformanceContext) -> tuple[pd.DataFrame, dict[str, Any], Optional[str]]:
    segment = context.segment
    if segment == "commercial":
        return _build_commercial_display(context.table), _commercial_styles(), "Zone_Centre"
    if segment == "pr_caisse":
        return _build_pr_caisse_display(context.table, type_point=context.filters.type_point), _pr_caisse_styles(), "Territoire"
    if segment == "cds":
        return _build_cds_display(context.table), _cds_styles(), None
    raise ValueError(f"Segment inconnu: {segment}")


def _build_commercial_display(table: pd.DataFrame) -> pd.DataFrame:
    df = table.copy()
    df["Heure_Dotation"] = _col(df, "Heure_Dotation_min", float("nan")).apply(_minutes_to_time)
    df["Premiere_Trans"] = _col(df, "Premiere_Trans_min", float("nan")).apply(_minutes_to_time)
    df["Derniere_Trans"] = _col(df, "Derniere_Trans_min", float("nan")).apply(_minutes_to_time)

    display = pd.DataFrame({
        "Zone_Centre": _col(df, "Zone_Centre", ""),
        "Zone_SA": _col(df, "Zone_SA", ""),
        "Commercial": _col(df, "Actor_Nom", ""),
        "Nb_Jours": _to_int(_col(df, "Nb_Jours", 0)),
        "Heure_Dotation": df["Heure_Dotation"],
        "Dotation_Montant": _to_num(_col(df, "Dotation_Montant", 0)),
        "Premiere_Trans": df["Premiere_Trans"],
        "Derniere_Trans": df["Derniere_Trans"],
        "Nb_Transactions": _to_int(_col(df, "Nb_Transactions", 0)),
        "FD_HVC": _to_num(_col(df, "FD_HVC", 0)),
        "HVC_Serve": _to_int(_col(df, "HVC_Serve", 0)),
        "TR_HVC (%)": _to_num(_col(df, "TR_HVC", 0)),
        "FD_Others": _to_num(_col(df, "FD_Others", 0)),
        "Other_Serve": _to_int(_col(df, "Other_Serve", 0)),
        "TR_Other (%)": _to_num(_col(df, "TR_Other", 0)),
        "Sigma_FD": _to_num(_col(df, "Sigma_FD", 0)),
        "Sigma_POS_Serve": _to_int(_col(df, "Sigma_POS_Serve", 0)),
        "TR_General (%)": _to_num(_col(df, "TR_General", 0)),
    })

    for label in WORK_PROGRESS_LABELS:
        serve_col, tr_col = f"HVC Serve {label}", f"TR_HVC {label}"
        other_serve_col, other_tr_col = f"Other Serve {label}", f"TR_Other {label}"
        display[f"TPW {label}"] = (
            _to_int(_col(df, serve_col, 0)).astype(str) + " | "
            + _to_num(_col(df, tr_col, 0)).round(1).astype(str) + "% | "
            + _to_int(_col(df, other_serve_col, 0)).astype(str) + " | "
            + _to_num(_col(df, other_tr_col, 0)).round(1).astype(str) + "%"
        )

    return display.sort_values(["Zone_Centre", "Zone_SA", "Commercial"]).reset_index(drop=True)


def _build_pr_caisse_display(table: pd.DataFrame, type_point: str = "Tous") -> pd.DataFrame:
    df = table.copy()
    display = pd.DataFrame({
        "Territoire": _col(df, "Territoire", ""),
        "Localisation": _col(df, "Localisation", ""),
        "Nom": _col(df, "Actor_Nom", ""),
        "Type": _col(df, "Type_Point", ""),
        "Nb_Jours": _to_int(_col(df, "Nb_Jours", 0)),
        "FD_HVC": _to_num(_col(df, "FD_HVC", 0)),
        "HVC_Serve": _to_int(_col(df, "HVC_Serve", 0)),
        "FD_Others": _to_num(_col(df, "FD_Others", 0)),
        "Other_Serve": _to_int(_col(df, "Other_Serve", 0)),
        "Sigma_FD": _to_num(_col(df, "Sigma_FD", 0)),
        "Sigma_POS_Serve": _to_int(_col(df, "Sigma_POS_Serve", 0)),
        "POS_serve [14h-17h]": _to_int(_col(df, "POS_serve", 0)),
        "Nouveaux [14h-17h]": _to_int(_col(df, "New", 0)),
        "Dotation_Nom": _col(df, "Dotation_Nom", ""),
        "Dotation_Montant": _to_num(_col(df, "Dotation_Montant", 0)),
    })

    # Work Progress (3 fenetres, Serve uniquement, sans TR — decision actee).
    # Renseigne uniquement pour les lignes "Caisses" (le controller ne le
    # calcule que sur Type_Point == "Caisses") ; "0 | 0" pour Point Relais.
    for label in WORK_PROGRESS_LABELS:
        serve_col = f"HVC Serve {label}"
        other_serve_col = f"Other Serve {label}"
        display[f"TPW {label}"] = (
            "HVC:" + _to_int(_col(df, serve_col, 0)).astype(str)
            + " | Other:" + _to_int(_col(df, other_serve_col, 0)).astype(str)
        )

    if type_point == "Caisses" or (not df.empty and "Type_Point" in df.columns and (df["Type_Point"] == "Caisses").all()):
        display = display.drop(columns=["Dotation_Nom", "Dotation_Montant"], errors="ignore")

    return display.sort_values(["Type", "Territoire", "Nom"]).reset_index(drop=True)


def _build_cds_display(table: pd.DataFrame) -> pd.DataFrame:
    df = table.copy()
    quota_pct = _col(df, "Taux_Couverture_Quota", None)
    quota_str = quota_pct.apply(lambda v: f"{v:.1f}%" if pd.notna(v) else "N/A")

    display = pd.DataFrame({
        "CDS": _col(df, "Actor_Nom", ""),
        "Nb_Jours": _to_int(_col(df, "Nb_Jours", 0)),
        "FD_HVC": _to_num(_col(df, "FD_HVC", 0)),
        "HVC_Serve": _to_int(_col(df, "HVC_Serve", 0)),
        "FD_Commercial": _to_num(_col(df, "FD_Commercial", 0)),
        "Ccial_Serve": _to_int(_col(df, "Ccial_Serve", 0)),
        "Nb_HVC_Attribue": _to_int(_col(df, "nb_hvc_attribue", 0)),
        "Taux_Couverture_Quota": quota_str,
        # POS_serve/Nouveaux [14h-17h] retires : plus calcules pour CDS
        # cote controller (Bloquant 1), les afficher serait trompeur (0 partout).
        "Dotation_Nom": _col(df, "Dotation_Nom", ""),
        "Dotation_Montant": _to_num(_col(df, "Dotation_Montant", 0)),
    })
    return display.sort_values(["CDS"]).reset_index(drop=True)


def _prepare_export_df(df: pd.DataFrame, segment: str) -> pd.DataFrame:
    if segment == "cds":
        return df.drop(columns=_CDS_DISPLAY_ONLY_COLUMNS, errors="ignore")
    return df


def _build_top10_display(context: PerformanceContext) -> pd.DataFrame:
    df = context.top_commerciaux.copy()
    cols = [c for c in ["Actor_Nom", "Nb_Jours", "HVC_Serve", "TR_General", "Sigma_FD", "Score_Global"] if c in df.columns]
    display = df[cols].rename(columns={
        "Actor_Nom": "Nom",
        "TR_General": "TR_General (%)",
        "Sigma_FD": "Sigma_FD (montant)",
    })
    display.insert(0, "Rang", range(1, len(display) + 1))
    return display


# ---------------------------------------------------------------------------
# Ligne TOTAL
# ---------------------------------------------------------------------------

def _append_total_row(display_df: pd.DataFrame, segment: str) -> pd.DataFrame:
    if display_df.empty:
        return display_df

    label_col = display_df.columns[0]
    total: dict[str, Any] = {col: "" for col in display_df.columns}
    total[label_col] = "TOTAL"

    for col in display_df.select_dtypes(include="number").columns:
        total[col] = display_df[col].sum()

    if segment == "cds" and "HVC_Serve" in total and "Nb_HVC_Attribue" in total:
        total_serve, total_quota = total["HVC_Serve"], total["Nb_HVC_Attribue"]
        total["Taux_Couverture_Quota"] = f"{(total_serve / total_quota * 100):.1f}%" if total_quota else "N/A"

    return pd.concat([display_df, pd.DataFrame([total])], ignore_index=True)


# ---------------------------------------------------------------------------
# Styles par segment
# ---------------------------------------------------------------------------

def _total_row_match(row: pd.Series) -> bool:
    return str(row.iloc[0]).strip().upper() == "TOTAL"


def _inactive_row_style(serve_col: str):
    """Ligne rouge clair si l'acteur n'a servi aucun POS (hors ligne TOTAL)."""

    def _style(row: pd.Series) -> dict[str, Any]:
        if _total_row_match(row):
            return {}
        try:
            if float(row.get(serve_col, 0)) == 0:
                return {"fill": "#ffcccc"}
        except Exception:
            pass
        return {}

    return _style


def _time_str_to_minutes(value: Any) -> Optional[int]:
    try:
        if value in (None, "N/A", ""):
            return None
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _heure_debut_rule(value: Any, row: pd.Series, col: Any) -> Optional[dict[str, Any]]:
    if col not in ("Heure_Dotation", "Premiere_Trans"):
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
    if col != "Derniere_Trans":
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
        "formats": {
            "Dotation_Montant": "#,##0",
            "FD_HVC": "#,##0",
            "FD_Others": "#,##0",
            "Sigma_FD": "#,##0",
        },
        "rules": [
            _heure_debut_rule,
            _heure_fin_rule,
            {"columns": ["TR_HVC (%)", "TR_Other (%)", "TR_General (%)"], "op": "<", "value": 70, "style": {"fill": "#ffd6d6"}},
            {"columns": ["TR_HVC (%)", "TR_Other (%)", "TR_General (%)"], "op": "between", "value": (70, 99.999), "style": {"fill": "#fff3bf"}},
            {"columns": ["TR_HVC (%)", "TR_Other (%)", "TR_General (%)"], "op": ">=", "value": 100, "style": {"fill": "#d8f3dc"}},
            {"columns": ["Sigma_POS_Serve"], "op": "<", "value": 20, "style": {"fill": "#ffd6d6"}},
            {"columns": ["Sigma_POS_Serve"], "op": "between", "value": (20, 39), "style": {"fill": "#fff3bf"}},
            {"columns": ["Sigma_POS_Serve"], "op": ">=", "value": 40, "style": {"fill": "#d8f3dc"}},
        ],
    }


def _pr_caisse_styles() -> dict[str, Any]:
    return {
        "sheet_name": "Performance PR & Caisses",
        "total_row_match": _total_row_match,
        "row_styles": _inactive_row_style("Sigma_POS_Serve"),
        "formats": {
            "FD_HVC": "#,##0",
            "FD_Others": "#,##0",
            "Sigma_FD": "#,##0",
            "Dotation_Montant": "#,##0",
        },
    }


def _cds_styles() -> dict[str, Any]:
    return {
        "sheet_name": "Performance CDS",
        "total_row_match": _total_row_match,
        "row_styles": _inactive_row_style("HVC_Serve"),
        "formats": {
            "FD_HVC": "#,##0",
            "Dotation_Montant": "#,##0",
        },
    }


# ---------------------------------------------------------------------------
# Export — services/export_service.py exclusivement
# ---------------------------------------------------------------------------

def _render_exports(prefix: str, df: pd.DataFrame, styles: dict[str, Any], group_col: Optional[str]) -> None:
    has_group_export = bool(group_col) and group_col in df.columns
    # Colonnes : CSV | Excel | Image | ZIP (si groupe)
    cols = st.columns(4 if has_group_export else 3)

    cols[0].download_button("CSV", to_csv(df), f"{prefix}.csv", "text/csv", key=f"{prefix}_csv")

    cols[1].download_button(
        "Excel",
        to_excel(df, styles=styles, sheet_name=styles.get("sheet_name", "Performance")),
        f"{prefix}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{prefix}_xlsx",
    )

    # Export image PNG — isolé derrière un bouton pour ne pas bloquer le chargement
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
