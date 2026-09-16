# pages/pos_oos_listing.py
"""
Listing POS OOS.

Fonctionnement :
1. On importe un (ou plusieurs) fichier(s) "Listing OOS" contenant, entre
   autres colonnes, celles listées dans REQUIRED_COLS. Seules ces colonnes
   sont conservées, avec une correspondance de noms insensible à la casse,
   aux espaces et aux underscores (ex: "day_target" == "Day Target").
2. "Nom du POS" est retrouvé via le MSISDN, en cherchant dans le fichier
   Maître correspondant à la zone du POS :
   - Zone == "Centre II"  -> fichier Maître POS (Centre II)
   - sinon                -> fichier Maître POS (Centre III)
   via la colonne agent_msisdn -> full_name.
3. "Ccial en charge" est retrouvé via le MSISDN dans le fichier de mapping
   commercial (Settings), colonne HVC_MSISDN -> "Ccial en charge".
4. Les colonnes Day_Target (vert), OOS (jaune) et Float (rouge) sont
   surlignées sur toute la colonne, à l'écran comme dans l'export image.
5. L'export image se fait par Cluster (un ou plusieurs PNG par cluster,
   paginés automatiquement pour éviter tout MemoryError sur de gros clusters),
   regroupés dans un ZIP.
"""

import io
import re
import time
import zipfile
from typing import Optional

import numpy as np
import pandas as pd
import polars as pl
import streamlit as st

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from models.db import get_connection
from utils.helpers import clean_phone, to_excel
from utils.turso_storage import load_setting

# --------------------------------------------------------------------------
# ⚠️ À ADAPTER si besoin : clés de session utilisées par votre page Settings
# pour les fichiers Maître POS (Centre II / Centre III) et le mapping
# commercial. Renommez ces constantes pour qu'elles correspondent exactement
# aux clés que votre page Settings utilise (st.session_state[...]).
# --------------------------------------------------------------------------
SESSION_KEY_MASTER_II = "maitre_pos"        # Maître POS Centre II
SESSION_KEY_MASTER_III = "maitre_pos_III"   # Maître POS Centre III
SESSION_KEY_COMMERCIAL = "hvc_commercial"   # Mapping HVC_MSISDN -> Ccial en charge

_SESSION_SETTINGS_KEYS = {
    "zones": "zones_df",
    "maitre_pos": "pos_master_df",
    "maitre_pos_III": "pos_master_III_df",
    "hvc_commercial": "hvc_commercial_df",
}


@st.cache_data(ttl=300, show_spinner=False)
def _read_canonical_settings() -> dict[str, pd.DataFrame]:
    """Lit les référentiels canoniques créés par run_referentiel_ingestion."""
    conn = get_connection()
    try:
        zones = _read_database_query(
            """
            SELECT site_key, sitename AS "SITENAME", zone_new AS "ZONE NEW",
                   territory_correct AS "TERRITORY CORRECT",
                   isl_terr AS "ISL_Terr"
            FROM sites
            """,
            conn,
        ).to_pandas()
        master_pos = _read_database_query(
            """
            SELECT agent_msisdn, source_master, full_name, zone_centre AS zone,
                   zone_territoire AS territory, secteur_cluster AS cluster,
                   site_key, segment_group, day_target, oos_target
            FROM referentiel_pos
            WHERE source_master = 'maitre_pos'
            """,
            conn,
        ).to_pandas()
        master_pos_iii = _read_database_query(
            """
            SELECT agent_msisdn, source_master, full_name, zone_centre AS zone,
                   zone_territoire AS territory, secteur_cluster AS cluster,
                   site_key, segment_group, day_target, oos_target
            FROM referentiel_pos
            WHERE source_master = 'maitre_pos_III'
            """,
            conn,
        ).to_pandas()
        hvc_commercial = _read_database_query(
            """
            SELECT hvc_msisdn AS HVC_MSISDN,
                   ccial_en_charge AS "Ccial en charge"
            FROM hvc_commercial_mapping
            """,
            conn,
        ).to_pandas()
        return {
            "zones": zones,
            "maitre_pos": master_pos,
            "maitre_pos_III": master_pos_iii,
            "hvc_commercial": hvc_commercial,
        }
    finally:
        conn.close()


def _read_database_query(query: str, conn) -> pl.DataFrame:
    """Exécute une requête DB-API et matérialise directement le résultat en Polars."""
    return pl.read_database(query=query, connection=conn)


def _load_database_settings() -> dict[str, pd.DataFrame]:
    """Charge uniquement les tables canoniques, sans ingestion reseau implicite."""
    started_at = time.perf_counter()
    settings = _read_canonical_settings()
    st.session_state["listing_oos_database_load_seconds"] = round(
        time.perf_counter() - started_at, 2
    )
    return settings


@st.cache_data(ttl=300, show_spinner=False)
def _load_persisted_setting(folder_name: str) -> Optional[pd.DataFrame]:
    """Dernier repli : charge le fichier persistant depuis stored_files."""
    return load_setting(folder_name)


def _load_session_setting(
    folder_name: str,
    database_settings: Optional[dict[str, pd.DataFrame]] = None,
) -> Optional[pd.DataFrame]:
    """Priorise les tables Turso, puis session et fichier Settings en repli."""
    key = _SESSION_SETTINGS_KEYS.get(folder_name, folder_name)

    if database_settings is None:
        database_settings = _load_database_settings()
    database_value = database_settings.get(folder_name)
    if isinstance(database_value, pd.DataFrame) and not database_value.empty:
        st.session_state[key] = database_value
        return database_value

    value = st.session_state.get(key)
    if isinstance(value, pd.DataFrame):
        return value

    value = _load_persisted_setting(folder_name)
    if isinstance(value, pd.DataFrame):
        st.session_state[key] = value
        return value
    return None

REQUIRED_COLS = [
    "Day_Target", "Float", "OOS", "Last Trx Time",
    "MSISDN", "Locality", "Cluster", "Territory", "Zone", "Segment Group",
]

MAX_ROWS_PER_IMAGE = 25  # garde-fou mémoire : évite un MemoryError matplotlib sur de gros clusters


# ==========================================================================
# Utilitaires : normalisation des colonnes / MSISDN / mapping
# ==========================================================================
def _norm_key(s) -> str:
    """Clé de comparaison insensible à la casse, aux espaces et aux underscores."""
    return re.sub(r"[\s_]+", "", str(s)).strip().lower()


def _select_required_columns(df: pd.DataFrame, required_cols) -> pd.DataFrame:
    """Ne garde que les colonnes demandées, en retrouvant leur nom réel dans
    le fichier importé quelle que soit la casse/les espaces/les underscores."""
    lookup = {_norm_key(c): c for c in df.columns}
    rename_map, missing = {}, []
    for req in required_cols:
        real = lookup.get(_norm_key(req))
        if real is not None:
            rename_map[real] = req
        else:
            missing.append(req)
    df = df.rename(columns=rename_map)
    if missing:
        st.warning(f"⚠️ Colonnes absentes du fichier importé : {', '.join(missing)}")
    keep = [c for c in required_cols if c in df.columns]
    return df[keep].copy()


def _read_uploaded_file(file) -> pl.DataFrame:
    """Lit un fichier Listing OOS avec Polars avant conversion finale en Pandas."""
    content = file.getvalue()
    if file.name.lower().endswith(".csv"):
        return pl.read_csv(io.BytesIO(content), infer_schema_length=10000)
    return pl.read_excel(io.BytesIO(content), engine="calamine")


def _is_centre_ii(zone_value) -> bool:
    z = _norm_key(zone_value)
    return z in ("centreii", "centre2")


def _build_msisdn_map(master_df: Optional[pd.DataFrame], key_col_candidates, value_col_candidates) -> dict:
    """Construit un dict {MSISDN nettoyé: valeur} à partir d'un fichier maître,
    en retrouvant les colonnes clé/valeur indépendamment de la casse."""
    if master_df is None or master_df.empty:
        return {}
    lookup = {_norm_key(c): c for c in master_df.columns}
    key_col = next((lookup[_norm_key(c)] for c in key_col_candidates if _norm_key(c) in lookup), None)
    val_col = next((lookup[_norm_key(c)] for c in value_col_candidates if _norm_key(c) in lookup), None)
    if key_col is None or val_col is None:
        return {}
    tmp = master_df[[key_col, val_col]].dropna(subset=[key_col]).copy()
    tmp["_key"] = tmp[key_col].apply(clean_phone)
    tmp = tmp.dropna(subset=["_key"]).drop_duplicates(subset="_key", keep="last")
    return dict(zip(tmp["_key"], tmp[val_col]))


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
    "Day_Target": 1.1,
    "OOS": 1.0,
    "Float": 1.0,
    "Date dernière balance": 1.7,
}

_CELL_FONTSIZE = 7.6
_AVG_CHAR_WIDTH_FACTOR = 0.56  # largeur moyenne d'un caractère ≈ 0.56 * taille de police (points), pour DejaVu Sans
_CELL_PADDING_PT = 6.0


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

    widths = [_COL_WIDTHS.get(c, 1.3) for c in cols]
    total_w = sum(widths)
    n_rows = len(df)

    fig_w = max(9, total_w * 0.75)
    fig_h = max(2.0, (n_rows + 1) * 0.32 + (0.5 if title else 0.2))
    dpi = 200
    if fig_h * dpi > 6000:  # garde-fou mémoire, quel que soit le nb de lignes
        dpi = max(60, int(6000 / fig_h))

    # Points (pt) disponibles par unité de largeur de colonne, pour estimer
    # combien de caractères tiennent dans chaque cellule (1 pouce = 72 pt).
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
            tx = x + 0.08 if ha == "left" else x + w / 2
            # -1 caractère de marge quand aligné à gauche (padding gauche)
            max_chars = max(3, max_chars_per_col[c] - (1 if ha == "left" else 0))
            text = _fit_text(raw_text, max_chars)
            txt = ax.text(tx, y + 0.5, text, ha=ha, va="center", fontsize=_CELL_FONTSIZE, color=text_color)
            # Garde-fou : même si l'estimation de largeur est imparfaite, le
            # texte ne peut jamais déborder visuellement sur la cellule voisine.
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


# ==========================================================================
# Page principale
# ==========================================================================
def show_pos_oos_listing():
    total_started_at = time.perf_counter()
    progress = st.progress(0, text="Initialisation du Listing OOS...")
    phase_started_at = time.perf_counter()
    timings: dict[str, float] = {}

    def finish_phase(name: str, progress_value: int, message: str) -> None:
        timings[name] = round(time.perf_counter() - phase_started_at, 2)
        progress.progress(progress_value, text=message)

    st.title("Listing POS OOS")

    database_settings = _load_database_settings()
    finish_phase("Chargement Turso / référentiels", 20, "Référentiels chargés")

    phase_started_at = time.perf_counter()
    zones_df = _load_session_setting("zones", database_settings)
    if zones_df is None:
        progress.empty()
        st.error("Veuillez charger le fichier **Zones** dans Settings")
        st.stop()

    master_ii = _load_session_setting(SESSION_KEY_MASTER_II, database_settings)
    master_iii = _load_session_setting(SESSION_KEY_MASTER_III, database_settings)
    commercial_df = _load_session_setting(SESSION_KEY_COMMERCIAL, database_settings)
    finish_phase("Préparation des référentiels", 30, "Référentiels prêts")

    if master_ii is None and master_iii is None:
        st.warning("Aucun fichier Maître POS (Centre II / Centre III) trouvé dans Settings — 'Nom du POS' restera vide.")
    if commercial_df is None:
        st.warning("Fichier de mapping commercial ('commercial_pos') introuvable dans Settings — 'Ccial en charge' restera vide.")

    # ===================== IMPORT DU LISTING OOS =====================
    st.subheader("Importer le fichier Listing OOS")
    oos_files = st.file_uploader(
        "Fichier(s) Listing OOS (.xlsx, .xls, .csv)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pos_oos_listing_files",
    )
    if not oos_files:
        progress.progress(100, text="En attente du fichier Listing OOS")
        st.info("Chargez au moins un fichier pour afficher le listing.")
        st.stop()

    phase_started_at = time.perf_counter()
    raw_frames = [_read_uploaded_file(file) for file in oos_files]
    raw_df = pl.concat(raw_frames, how="diagonal_relaxed").to_pandas()
    finish_phase("Lecture des fichiers", 45, "Fichiers lus")

    phase_started_at = time.perf_counter()
    df_oos = _select_required_columns(raw_df, REQUIRED_COLS)
    if "MSISDN" not in df_oos.columns:
        st.error("La colonne MSISDN est introuvable dans le(s) fichier(s) importé(s).")
        st.stop()

    df_oos["MSISDN"] = df_oos["MSISDN"].astype(str).str.strip()
    df_oos["MSISDN_clean"] = df_oos["MSISDN"].apply(clean_phone)
    df_oos = df_oos.dropna(subset=["MSISDN_clean"]).drop_duplicates(subset="MSISDN_clean", keep="last")
    finish_phase("Nettoyage des données", 60, "Données nettoyées")

    # ===================== MAPPING NOM DU POS (Centre II / Centre III) ====
    phase_started_at = time.perf_counter()
    map_ii = _build_msisdn_map(master_ii, ["agent_msisdn", "Agent MSISDN"], ["full_name", "Full Name"])
    map_iii = _build_msisdn_map(master_iii, ["agent_msisdn", "Agent MSISDN"], ["full_name", "Full Name"])

    if "Zone" in df_oos.columns:
        is_ii = df_oos["Zone"].apply(_is_centre_ii)
    else:
        is_ii = pd.Series(False, index=df_oos.index)
    name_ii = df_oos["MSISDN_clean"].map(map_ii)
    name_iii = df_oos["MSISDN_clean"].map(map_iii)
    df_oos["Nom du POS"] = np.where(is_ii, name_ii, name_iii)
    df_oos["Nom du POS"] = df_oos["Nom du POS"].fillna("Non trouvé")

    # ===================== MAPPING CCIAL EN CHARGE =====================
    ccial_map = _build_msisdn_map(
        commercial_df,
        ["HVC_ MSISDN", "HVC_MSISDN", "HVC MSISDN"],
        ["Ccial en charge"],
    )
    df_oos["Ccial en charge"] = df_oos["MSISDN_clean"].map(ccial_map).fillna("Non attribué")
    finish_phase("Enrichissement des POS", 70, "Noms et commerciaux associés")

    # ===================== FILTRES =====================
    phase_started_at = time.perf_counter()
    st.sidebar.markdown("---")
    st.sidebar.subheader("Filtres")

    zones = zones_df.copy()
    zone_list = ["Toutes"] + sorted(zones['ZONE NEW'].dropna().unique().tolist())
    selected_zone = st.sidebar.selectbox("Zone", zone_list, key="oos_zone_filter")

    selected_terr = "Toutes"
    if selected_zone != "Toutes" and 'TERRITORY CORRECT' in zones.columns:
        terr_list = ["Toutes"] + sorted(
            zones[zones['ZONE NEW'] == selected_zone]['TERRITORY CORRECT'].dropna().unique().tolist()
        )
        selected_terr = st.sidebar.selectbox("Territory", terr_list, key="oos_terr_filter")

    selected_isl = "Toutes"
    if selected_terr != "Toutes" and "ISL_Terr" in zones.columns:
        isl_list = ["Toutes"] + sorted(
            zones[zones['TERRITORY CORRECT'] == selected_terr]['ISL_Terr'].dropna().unique().tolist()
        )
        selected_isl = st.sidebar.selectbox("Cluster", isl_list, key="oos_isl_filter")

    selected_site = "Toutes"
    if selected_isl != "Toutes" and "SITENAME" in zones.columns:
        site_list = ["Toutes"] + sorted(
            zones[zones['ISL_Terr'] == selected_isl]['SITENAME'].dropna().unique().tolist()
        )
        selected_site = st.sidebar.selectbox("Site", site_list, key="oos_site_filter")

    st.sidebar.markdown("---")
    segment_list = sorted(df_oos["Segment Group"].dropna().unique().tolist()) if "Segment Group" in df_oos.columns else []
    # Par défaut, on ne garde que les segments HVC (ex: "1-HVC"), mais
    # l'utilisateur peut élargir la sélection à d'autres segments si besoin.
    hvc_segments_default = [s for s in segment_list if "HVC" in str(s).upper()] or segment_list
    selected_segments = st.sidebar.multiselect(
        "🎯 Filtre Segment", segment_list, default=hvc_segments_default, key="oos_segment_filter"
    )

    st.sidebar.markdown("---")
    ccial_list = ["Toutes"] + sorted(df_oos['Ccial en charge'].dropna().unique().tolist())
    selected_ccial = st.sidebar.selectbox("Ccial", ccial_list, key="oos_ccial_filter")

    territory_list_full = sorted(df_oos["Territory"].dropna().unique().tolist()) if "Territory" in df_oos.columns else []
    selected_territories = st.sidebar.multiselect("🌍 Filtre Territoire", territory_list_full, default=territory_list_full, key="oos_territory_multi_filter")
    finish_phase("Préparation des filtres", 80, "Filtres prêts")

    # ===================== COLONNES FINALES =====================
    phase_started_at = time.perf_counter()
    display_df = pd.DataFrame()
    display_df['Numero du POS'] = df_oos.get('MSISDN')
    display_df['Nom du POS'] = df_oos.get('Nom du POS')
    display_df['Ccial en charge'] = df_oos.get('Ccial en charge')
    display_df['Locality'] = df_oos.get('Locality', "N/A")
    display_df['Cluster'] = df_oos.get('Cluster', "N/A")
    display_df['Territory'] = df_oos.get('Territory', "N/A")
    display_df['Zone'] = df_oos.get('Zone', "N/A")
    display_df['Segment group'] = df_oos.get('Segment Group', "N/A")
    display_df['Day_Target'] = pd.to_numeric(df_oos.get('Day_Target'), errors='coerce')
    display_df['OOS'] = pd.to_numeric(df_oos.get('OOS'), errors='coerce')
    display_df['Float'] = pd.to_numeric(df_oos.get('Float'), errors='coerce')

    if 'Last Trx Time' in df_oos.columns:
        last_trx = pd.to_datetime(df_oos['Last Trx Time'], errors='coerce')
        display_df['Date dernière balance'] = last_trx.dt.strftime('%d/%m/%Y %H:%M').fillna("N/A")
    else:
        display_df['Date dernière balance'] = "N/A"

    # ===================== APPLICATION DES FILTRES =====================
    if selected_zone != "Toutes":
        display_df = display_df[display_df["Zone"] == selected_zone]
    if selected_terr != "Toutes":
        display_df = display_df[display_df["Territory"] == selected_terr]
    if selected_isl != "Toutes":
        display_df = display_df[display_df["Cluster"] == selected_isl]
    if selected_site != "Toutes":
        display_df = display_df[display_df["Locality"] == selected_site]
    if selected_ccial != "Toutes":
            display_df = display_df[display_df["Ccial en charge"] == selected_ccial]
    if selected_segments:
        display_df = display_df[display_df["Segment group"].isin(selected_segments)]
    if selected_territories:
        display_df = display_df[display_df["Territory"].isin(selected_territories)]

    display_df['Day_Target'] = display_df['Day_Target'].fillna(0).astype(int)
    display_df['OOS'] = display_df['OOS'].fillna(0).astype(int)
    display_df['Float'] = display_df['Float'].fillna(0).astype(int)
    display_df = display_df.reset_index(drop=True)
    finish_phase("Application des filtres", 90, "Résultats calculés")

    # ===================== COULEURS (colonnes entières) =====================
    def color_row(row):
        styles = [''] * len(row)
        for i, col in enumerate(display_df.columns):
            if col == 'Day_Target':
                styles[i] = 'background-color: #C8E6C9; color: #2E7D32'
            elif col == 'OOS':
                styles[i] = 'background-color: #FFF9C4; color: #F57F17'
            elif col == 'Float':
                styles[i] = 'background-color: #FFCDD2; color: #C62828'
        return styles

    styled_df = display_df.style.apply(color_row, axis=1)

    # ===================== AFFICHAGE =====================
    st.subheader(f"{len(display_df)} POS en rupture")
    st.dataframe(styled_df, use_container_width=True, height=650)
    finish_phase("Affichage du tableau", 93, "Tableau affiché")

    # ===================== EXPORTS =====================
    phase_started_at = time.perf_counter()
    st.markdown("#### Export")
    c1, c2, c3 = st.columns(3)

    with c1:
        zip_bytes = _export_images_by_cluster_zip(display_df, cluster_col="Cluster")
        if zip_bytes:
            st.download_button(
                "📸 Images PNG par Cluster (ZIP)",
                data=zip_bytes,
                file_name="POS_OOS_par_cluster.zip",
                mime="application/zip",
            )
        else:
            st.caption("Aucune donnée à exporter en image.")

    with c2:
        excel_data = to_excel(display_df)
        st.download_button(
            "📊 Télécharger Excel",
            excel_data,
            "POS_OOS_Alert.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with c3:
        st.download_button(
            "📄 Télécharger CSV",
            display_df.to_csv(index=False).encode('utf-8'),
            "POS_OOS_Alert.csv",
            "text/csv",
        )
    finish_phase("Préparation des exports", 98, "Exports prêts")

    timings["Total"] = round(time.perf_counter() - total_started_at, 2)
    progress.progress(100, text=f"Terminé en {timings['Total']:.2f} s")
    if st.sidebar.checkbox("Afficher le diagnostic performance", key="oos_show_performance"):
        with st.expander("Diagnostic performance", expanded=True):
            st.dataframe(
                pd.DataFrame(
                    [{"Phase": name, "Durée (s)": duration} for name, duration in timings.items()]
                ),
                hide_index=True,
                use_container_width=True,
            )