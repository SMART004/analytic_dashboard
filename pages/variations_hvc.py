"""
hvc_oos_variation.py
=====================
Module Streamlit : MultiIndex, KPIs globaux, recherche par site, Top 5 / Flop 5
(Plotly), export Excel par Territoire (mise en forme conditionnelle réelle) et
export Image fidèle au design du tableau (rendu 100% matplotlib, sans
dépendance à Chrome/Selenium/Playwright — donc fiable sur tout serveur, y
compris Streamlit Cloud / environnements sans navigateur).

Changements par rapport à la version précédente :
- L'export image ne dépend plus de `dataframe_image` + Chrome (source de
  plantages fréquents en production : "chrome executable not found" etc.).
  Un rendu maison en matplotlib reproduit exactement les mêmes seuils de
  couleur / flèches / en-têtes groupés que le tableau affiché à l'écran.
- Nouveau bouton d'export image de la vue courante (filtrée), en plus du zip
  image par territoire.
- Bug corrigé : le nom de fichier Excel n'était pas un f-string
  (`"...{old_label}..."` restait littéral au lieu d'être interpolé).
- Export Excel : les cellules contiennent désormais les valeurs numériques
  réelles (utile pour trier/filtrer dans Excel) + une mise en forme
  conditionnelle (couleurs) reproduisant les mêmes seuils que l'écran, plutôt
  que du texte brut avec emojis (qui n'apparaissait de toute façon pas dans
  l'ancien export Excel : `Styler.format()` avec une fonction Python n'est
  jamais traduit en contenu de cellule par `to_excel`, seules les valeurs
  numériques brutes étaient écrites).
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, date
from typing import List, Dict, Any, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

from utils.storage import list_files, get_file_bytes, upload_file

try:
    from utils.supabase import load_setting, save_file
except ImportError:
    pass

# --------------------------------------------------------------------------
# Constantes & Configuration
# --------------------------------------------------------------------------
STORAGE_FOLDER_DATA = "hvc_data_uploads"
STORAGE_FOLDER_ZONES = "hvc_zones"

DATA_SHEET_NAME = "Export"
SITE_COL = "SITENAME"
DAY_HVC_COL = "#day HVC"
OOS_PCT_COL = "%OOS HVC"

TARGET_TERRITORIES = {"EMANA", "NKOZOA_OLEMBE", "NKOLBONG", "ETOUDI"}

# Palette partagée par l'écran (emojis via st.dataframe) ET l'image (matplotlib)
COLOR_GREEN = "#16a34a"
COLOR_YELLOW = "#ca8a04"
COLOR_RED = "#dc2626"
COLOR_GRAY = "#6b7280"


# --------------------------------------------------------------------------
# Parsing & Fichiers
# --------------------------------------------------------------------------
def parse_file_timestamp(filename: str) -> Optional[datetime]:
    clean_fname = filename.split("__")[-1] if "__" in filename else filename

    match = re.search(r'(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})', clean_fname)
    if match:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}:{match.group(3)}:{match.group(4)}", "%Y-%m-%d %H:%M:%S")

    match = re.search(r'(\d{8})_(\d{6})', clean_fname)
    if match:
        return datetime.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S")

    match = re.search(r'(\d{4}-\d{2}-\d{2})', clean_fname)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d")

    return None


def save_data_upload(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    original_name = uploaded_file.name
    if not parse_file_timestamp(original_name):
        ts = datetime.now()
        ext = original_name.split(".")[-1] if "." in original_name else "xlsx"
        uploaded_file.name = f"data_{ts.strftime('%Y%m%d_%H%M%S')}.{ext}"

    success = upload_file(uploaded_file, STORAGE_FOLDER_DATA)
    return uploaded_file.name if success else ""


def save_zones_file(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    ext = uploaded_file.name.split(".")[-1] if "." in uploaded_file.name else "xlsx"
    uploaded_file.name = f"zones_reference.{ext}"
    success = upload_file(uploaded_file, STORAGE_FOLDER_ZONES)
    return uploaded_file.name if success else ""


def list_data_uploads() -> List[Dict[str, Any]]:
    file_list = list_files(STORAGE_FOLDER_DATA)
    items = []

    for item in file_list:
        fname = item.get("name", "") if isinstance(item, dict) else str(item)
        clean_fname = fname.split("__")[-1] if "__" in fname else fname

        if not (clean_fname.endswith(".xlsx") or clean_fname.endswith(".csv")):
            continue

        ts = parse_file_timestamp(clean_fname) or datetime.now()
        hour_str = f"{ts.hour}h"

        items.append({
            "name": fname,
            "clean_name": clean_fname,
            "timestamp": ts,
            "date": ts.date(),
            "time_str": hour_str
        })

    items.sort(key=lambda x: x["timestamp"])
    return items


# --------------------------------------------------------------------------
# Chargement Mappings & Données
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_dsm_mapping_from_settings() -> pd.DataFrame:
    df_dsm = None
    for key in ["dsm_mapping", "sites_etoudi", "commerciaux", "settings"]:
        res = load_setting(key)
        if res is not None and isinstance(res, pd.DataFrame) and not res.empty:
            df_dsm = res
            break

    if df_dsm is None:
        return pd.DataFrame()

    df_dsm = df_dsm.copy()
    df_dsm.columns = [str(c).strip() for c in df_dsm.columns]
    col_map = {c.lower(): c for c in df_dsm.columns}
    site_col, dsm_col = col_map.get("sitename", "sitename"), col_map.get("dsm_name", "dsm_name")

    if site_col not in df_dsm.columns or dsm_col not in df_dsm.columns:
        return pd.DataFrame()

    clean_dsm = df_dsm[[site_col, dsm_col]].dropna(subset=[site_col]).copy()
    clean_dsm["site_key"] = clean_dsm[site_col].astype(str).str.strip().str.upper()
    return clean_dsm[["site_key", dsm_col]].drop_duplicates(subset=["site_key"]).rename(columns={dsm_col: "dsm_name"})


@st.cache_data(show_spinner=False)
def load_zones_mapping_from_setting() -> pd.DataFrame:
    zones = load_setting("zones")
    if zones is None or zones.empty:
        return pd.DataFrame()

    zones = zones.copy()
    zones.columns = [str(c).strip() for c in zones.columns]
    col_map = {c.lower(): c for c in zones.columns}

    site_col_real = col_map.get("sitename", SITE_COL)
    terr_col_real = col_map.get("territoire correct") or col_map.get("territory correct") or "TERRITORY CORRECT"
    cluster_col_real = col_map.get("isl_terr") or "ISL_Terr"

    cols_to_keep = [site_col_real]
    if terr_col_real in zones.columns:
        cols_to_keep.append(terr_col_real)
    if cluster_col_real in zones.columns:
        cols_to_keep.append(cluster_col_real)

    zones_clean = zones[cols_to_keep].dropna(subset=[site_col_real]).copy()
    zones_clean["site_key"] = zones_clean[site_col_real].astype(str).str.strip().str.upper()

    rename_dict = {
        site_col_real: SITE_COL,
        terr_col_real: "TERRITORY",
        cluster_col_real: "Cluster"
    }

    return zones_clean.drop_duplicates(subset=["site_key"]).rename(columns=rename_dict)


@st.cache_data(show_spinner=False)
def load_site_level_data_bytes(file_bytes: bytes) -> pd.DataFrame:
    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=DATA_SHEET_NAME)
    df.columns = [str(c).strip() for c in df.columns]

    site_df = df[df[SITE_COL].notna() & (df[SITE_COL] != "Total")].drop_duplicates(subset=SITE_COL, keep="first").copy()
    day_hvc_col = DAY_HVC_COL if DAY_HVC_COL in site_df.columns else "#HVC"

    for col in [day_hvc_col, OOS_PCT_COL]:
        if col in site_df.columns:
            site_df[col] = pd.to_numeric(site_df[col], errors="coerce").fillna(0.0)

    if day_hvc_col != DAY_HVC_COL and day_hvc_col in site_df.columns:
        site_df = site_df.rename(columns={day_hvc_col: DAY_HVC_COL})

    site_df["site_key"] = site_df[SITE_COL].astype(str).str.strip().str.upper()
    return site_df[[SITE_COL, DAY_HVC_COL, OOS_PCT_COL, "site_key"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# MultiIndex & Graphiques Dynamiques
# --------------------------------------------------------------------------
def compute_multiindex_variation(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
    zones_df: pd.DataFrame,
    dsm_df: pd.DataFrame,
    old_label: str,
    new_label: str
) -> pd.DataFrame:

    merged = pd.merge(
        old_df[["site_key", SITE_COL, DAY_HVC_COL, OOS_PCT_COL]],
        new_df[["site_key", SITE_COL, DAY_HVC_COL, OOS_PCT_COL]],
        on="site_key",
        suffixes=("_old", "_new"),
        how="outer"
    ).fillna(0)

    merged[SITE_COL] = merged[f"{SITE_COL}_new"].replace("", 0)
    merged[SITE_COL] = merged[SITE_COL].where(merged[SITE_COL] != 0, merged[f"{SITE_COL}_old"])

    merged = merged.merge(zones_df[["site_key", "TERRITORY"]], on="site_key", how="left").fillna({"TERRITORY": "Non mappé"})
    merged = merged.merge(zones_df[["site_key", "Cluster"]], on="site_key", how="left").fillna({"Cluster": "Non mappé"})
    merged = merged.merge(dsm_df[["site_key", "dsm_name"]], on="site_key", how="left").fillna({"dsm_name": "Non attribué"})

    merged["Commercial"] = merged.apply(
        lambda r: r["dsm_name"] if str(r["Cluster"]).strip().upper() in TARGET_TERRITORIES else "N/A",
        axis=1
    )

    merged["%OOS_old"] = merged[f"{OOS_PCT_COL}_old"] * 100
    merged["%OOS_new"] = merged[f"{OOS_PCT_COL}_new"] * 100

    merged["Var #day HVC"] = merged[f"{DAY_HVC_COL}_new"] - merged[f"{DAY_HVC_COL}_old"]
    merged["Var %OOS (%)"] = merged["%OOS_new"] - merged["%OOS_old"]

    columns_tuples = [
        ("Informations", SITE_COL),
        ("Informations", "TERRITORY"),
        ("Informations", "Cluster"),
        ("Informations", "Commercial"),
        (f"🕒 {old_label}", "#day HVC"),
        (f"🕒 {old_label}", "%OOS HVC"),
        (f"🕒 {new_label}", "#day HVC"),
        (f"🕒 {new_label}", "%OOS HVC"),
        ("📊 Variations", "Δ #day HVC"),
        ("📊 Variations", "Δ %OOS (%)"),
    ]

    flat_data = pd.DataFrame({
        ("Informations", SITE_COL): merged[SITE_COL],
        ("Informations", "TERRITORY"): merged["TERRITORY"],
        ("Informations", "Cluster"): merged["Cluster"],
        ("Informations", "Commercial"): merged["Commercial"],
        (f"🕒 {old_label}", "#day HVC"): merged[f"{DAY_HVC_COL}_old"],
        (f"🕒 {old_label}", "%OOS HVC"): merged["%OOS_old"],
        (f"🕒 {new_label}", "#day HVC"): merged[f"{DAY_HVC_COL}_new"],
        (f"🕒 {new_label}", "%OOS HVC"): merged["%OOS_new"],
        ("📊 Variations", "Δ #day HVC"): merged["Var #day HVC"],
        ("📊 Variations", "Δ %OOS (%)"): merged["Var %OOS (%)"],
    })

    flat_data.columns = pd.MultiIndex.from_tuples(columns_tuples)
    return flat_data.sort_values(by=[("Informations", "Cluster"), ("📊 Variations", "Δ %OOS (%)")], ascending=[True, False]).reset_index(drop=True)


def render_plotly_top_flops(df: pd.DataFrame):
    """Génère deux graphiques en barres horizontales pour le Top 5 et Flop 5."""

    plot_df = pd.DataFrame({
        "SITENAME": df[("Informations", SITE_COL)],
        "Territoire": df[("Informations", "Cluster")],
        "Delta_OOS": df[("📊 Variations", "Δ %OOS (%)")]
    })

    plot_df = plot_df[plot_df["Delta_OOS"].abs() > 0.01]

    if plot_df.empty:
        st.info("Aucune variation significative à afficher sur le graphique.")
        return

    top_degrad = plot_df.sort_values("Delta_OOS", ascending=False).head(5)
    top_ameliog = plot_df.sort_values("Delta_OOS", ascending=True).head(5)

    g1, g2 = st.columns(2)

    plotly_config = {
        'displayModeBar': True,
        'toImageButtonOptions': {
            'format': 'png',
            'filename': 'hvc_variation_graph',
            'height': 400,
            'width': 700,
            'scale': 2
        }
    }

    with g1:
        st.markdown("##### 🔴 Top 5 Dégradations (%OOS ⬆️)")
        fig_deg = px.bar(
            top_degrad, x="Delta_OOS", y="SITENAME", orientation="h",
            text="Delta_OOS", color="Delta_OOS", color_continuous_scale="Reds",
            hover_data=["Territoire"]
        )
        fig_deg.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
        fig_deg.update_layout(
            yaxis=dict(autorange="reversed"), xaxis_title="Variation %OOS (%)",
            yaxis_title="", coloraxis_showscale=False, height=320,
            margin=dict(l=0, r=20, t=20, b=20)
        )
        st.plotly_chart(fig_deg, use_container_width=True, config=plotly_config)

    with g2:
        st.markdown("##### 🟢 Top 5 Améliorations (%OOS ⬇️)")
        fig_amel = px.bar(
            top_ameliog, x="Delta_OOS", y="SITENAME", orientation="h",
            text="Delta_OOS", color="Delta_OOS", color_continuous_scale="Greens_r",
            hover_data=["Territoire"]
        )
        fig_amel.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
        fig_amel.update_layout(
            yaxis=dict(autorange="reversed"), xaxis_title="Variation %OOS (%)",
            yaxis_title="", coloraxis_showscale=False, height=320,
            margin=dict(l=0, r=20, t=20, b=20)
        )
        st.plotly_chart(fig_amel, use_container_width=True, config=plotly_config)


# --------------------------------------------------------------------------
# Formatage des indicateurs (écran : emojis / image & excel : couleurs réelles)
# --------------------------------------------------------------------------
def _day_hvc_color(val: float) -> str:
    if pd.isna(val):
        return COLOR_GRAY
    if val < 0.8:
        return COLOR_RED
    if val < 1.0:
        return COLOR_YELLOW
    return COLOR_GREEN


def _oos_color(val: float) -> str:
    if pd.isna(val):
        return COLOR_GRAY
    if val < 20.0:
        return COLOR_GREEN
    if val <= 39.0:
        return COLOR_YELLOW
    return COLOR_RED


def fmt_day_hvc(val: float) -> str:
    """Indicateurs #day HVC : <0.8🔴, 0.8-1.0🟡, >=1.0🟢 (affichage écran)."""
    if pd.isna(val):
        return "-"
    if val < 0.8:
        return f"🔴 {val:.2f}"
    elif val < 1.0:
        return f"🟡 {val:.2f}"
    return f"🟢 {val:.2f}"


def fmt_oos_pct(val: float) -> str:
    """Indicateurs %OOS HVC : <20%🟢, 20%-39%🟡, >39%🔴 (affichage écran)."""
    if pd.isna(val):
        return "-"
    if val < 20.0:
        return f"🟢 {val:.2f}%"
    elif val <= 39.0:
        return f"🟡 {val:.2f}%"
    return f"🔴 {val:.2f}%"


def fmt_arrow_var(val: float, is_pct: bool = False) -> str:
    unit = "%" if is_pct else ""
    if pd.isna(val):
        return "-"
    if val > 0:
        return f"⬆️ +{val:.2f}{unit}"
    elif val < 0:
        return f"⬇️ {val:.2f}{unit}"
    return f"➡️ 0.00{unit}"


def get_formatted_styler(df: pd.DataFrame, old_label: str, new_label: str):
    """Styler utilisé pour l'affichage à l'écran (st.dataframe)."""
    format_dict = {
        (f"🕒 {old_label}", "#day HVC"): fmt_day_hvc,
        (f"🕒 {old_label}", "%OOS HVC"): fmt_oos_pct,
        (f"🕒 {new_label}", "#day HVC"): fmt_day_hvc,
        (f"🕒 {new_label}", "%OOS HVC"): fmt_oos_pct,
        ("📊 Variations", "Δ #day HVC"): lambda x: fmt_arrow_var(x, is_pct=False),
        ("📊 Variations", "Δ %OOS (%)"): lambda x: fmt_arrow_var(x, is_pct=True),
    }
    return df.style.format(format_dict)


# --------------------------------------------------------------------------
# EXPORT IMAGE — rendu maison matplotlib (fidèle au design, sans Chrome)
# --------------------------------------------------------------------------
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F0FF" "]+",
    flags=re.UNICODE,
)

_IMG_COL_WIDTHS = {
    SITE_COL: 2.6,
    "TERRITORY": 1.7,
    "Cluster": 1.5,
    "Commercial": 1.5,
    "#day HVC": 1.05,
    "%OOS HVC": 1.05,
    "Δ #day HVC": 1.2,
    "Δ %OOS (%)": 1.2,
}

_IMG_HEADER_BG = "#1f2937"
_IMG_SUBHEADER_BG = "#374151"
_IMG_HEADER_FG = "white"
_IMG_ROW_EVEN = "#ffffff"
_IMG_ROW_ODD = "#f3f4f6"
_IMG_BORDER = "#d1d5db"
_IMG_TEXT = "#111827"

# Nombre max de lignes dessinées dans UNE image. Au-delà, matplotlib doit
# allouer un canevas énorme (ex: 3000 lignes * 200 dpi -> des centaines de
# millions de pixels) qui provoque un MemoryError / bad allocation, surtout
# sous Windows. On limite donc la hauteur d'une image et on pagine le reste.
_MAX_ROWS_PER_IMAGE = 40
_MAX_FIG_HEIGHT_PX = 6000  # garde-fou supplémentaire, indépendant du nb de lignes


def _clean_label(s: str) -> str:
    """Retire les emojis des libellés pour l'image : les polices matplotlib
    par défaut ne les affichent pas en couleur (glyphes manquants -> carrés
    vides). La couleur du seuil est de toute façon déjà portée par la couleur
    du texte de la cellule (voir _day_hvc_color / _oos_color)."""
    return _EMOJI_RE.sub("", str(s)).strip()


def _var_text_and_color(val: float, is_pct: bool):
    unit = "%" if is_pct else ""
    if pd.isna(val):
        return "-", _IMG_TEXT
    if val > 0:
        return f"\u25B2 +{val:.2f}{unit}", COLOR_RED
    if val < 0:
        return f"\u25BC {val:.2f}{unit}", COLOR_GREEN
    return f"\u25AC 0.00{unit}", COLOR_GRAY


def _render_table_image_page(df: pd.DataFrame, old_label: str, new_label: str, title: str = "") -> bytes:
    """Dessine UNE page (au maximum _MAX_ROWS_PER_IMAGE lignes) du tableau en PNG."""
    old_key = f"🕒 {old_label}"
    new_key = f"🕒 {new_label}"
    var_key = "📊 Variations"

    ordered_cols = [
        ("Informations", SITE_COL),
        ("Informations", "TERRITORY"),
        ("Informations", "Cluster"),
        ("Informations", "Commercial"),
        (old_key, "#day HVC"),
        (old_key, "%OOS HVC"),
        (new_key, "#day HVC"),
        (new_key, "%OOS HVC"),
        (var_key, "Δ #day HVC"),
        (var_key, "Δ %OOS (%)"),
    ]
    cols = [c for c in ordered_cols if c in df.columns]
    if not cols or df.empty:
        fig, ax = plt.subplots(figsize=(6, 2))
        ax.text(0.5, 0.5, "Aucune donnée à exporter", ha="center", va="center")
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    widths = [_IMG_COL_WIDTHS.get(c[1], 1.0) for c in cols]
    total_w = sum(widths)
    n_rows = len(df)
    header_rows = 2

    fig_w = max(8, total_w * 0.62)
    fig_h = max(2.2, (n_rows + header_rows) * 0.34 + (0.6 if title else 0.2))

    # Garde-fou mémoire : quel que soit le nombre de lignes, on plafonne la
    # résolution du canevas pour ne jamais tenter une allocation démesurée.
    dpi = 200
    if fig_h * dpi > _MAX_FIG_HEIGHT_PX:
        dpi = max(60, int(_MAX_FIG_HEIGHT_PX / fig_h))

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    ax.set_xlim(0, total_w)
    ax.set_ylim(0, n_rows + header_rows)
    ax.invert_yaxis()
    ax.axis("off")

    if title:
        ax.text(total_w / 2, -0.55, title, ha="center", va="bottom",
                 fontsize=11, fontweight="bold", color=_IMG_TEXT)

    # -- en-tête groupée --
    x, i = 0.0, 0
    while i < len(cols):
        group = cols[i][0]
        span_w, j = 0.0, i
        while j < len(cols) and cols[j][0] == group:
            span_w += widths[j]
            j += 1
        ax.add_patch(Rectangle((x, 0), span_w, 1, facecolor=_IMG_HEADER_BG, edgecolor=_IMG_BORDER, linewidth=0.6))
        ax.text(x + span_w / 2, 0.5, _clean_label(group), ha="center", va="center",
                 fontsize=9.5, fontweight="bold", color=_IMG_HEADER_FG)
        x += span_w
        i = j

    # -- sous-en-tête --
    x = 0.0
    for (grp, sub), w in zip(cols, widths):
        ax.add_patch(Rectangle((x, 1), w, 1, facecolor=_IMG_SUBHEADER_BG, edgecolor=_IMG_BORDER, linewidth=0.6))
        ax.text(x + w / 2, 1.5, sub, ha="center", va="center", fontsize=8.5, color=_IMG_HEADER_FG)
        x += w

    # -- lignes de données --
    for r in range(n_rows):
        y = header_rows + r
        row_bg = _IMG_ROW_EVEN if r % 2 == 0 else _IMG_ROW_ODD
        x = 0.0
        for (grp, sub), w in zip(cols, widths):
            val = df.iloc[r][(grp, sub)]
            text_color = _IMG_TEXT
            ha = "center"

            if sub == SITE_COL:
                text, ha = str(val), "left"
            elif sub in ("TERRITORY", "Cluster", "Commercial"):
                text = str(val)
            elif sub == "#day HVC":
                text_color = _day_hvc_color(val)
                text = "-" if pd.isna(val) else f"\u25CF {val:.2f}"
            elif sub == "%OOS HVC":
                text_color = _oos_color(val)
                text = "-" if pd.isna(val) else f"\u25CF {val:.2f}%"
            elif sub.startswith("Δ"):
                text, text_color = _var_text_and_color(val, "%" in sub)
            else:
                text = "" if pd.isna(val) else str(val)

            ax.add_patch(Rectangle((x, y), w, 1, facecolor=row_bg, edgecolor=_IMG_BORDER, linewidth=0.5))
            tx = x + 0.08 if ha == "left" else x + w / 2
            bold = sub in ("#day HVC", "%OOS HVC") or sub.startswith("Δ")
            ax.text(tx, y + 0.5, text, ha=ha, va="center", fontsize=8.2,
                     color=text_color, fontweight="bold" if bold else "normal")
            x += w

    fig.tight_layout(pad=0.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _sort_by_abs_variation(df: pd.DataFrame) -> pd.DataFrame:
    var_col = ("📊 Variations", "Δ %OOS (%)")
    if var_col in df.columns:
        return df.reindex(df[var_col].abs().sort_values(ascending=False).index).reset_index(drop=True)
    return df.reset_index(drop=True)


def render_table_image(df: pd.DataFrame, old_label: str, new_label: str, title: str = "") -> bytes:
    """
    Dessine un PNG reproduisant fidèlement le design du tableau affiché à
    l'écran (mêmes en-têtes groupés, mêmes seuils de couleur, mêmes flèches).

    100% matplotlib : aucune dépendance à un navigateur.

    Sécurité mémoire : si le tableau contient plus de _MAX_ROWS_PER_IMAGE
    lignes, seules les variations les plus fortes (en valeur absolue) sont
    affichées, avec une mention dans le titre — pour un export exhaustif,
    utiliser le ZIP par territoire (paginé automatiquement) ou l'export Excel.
    """
    if len(df) > _MAX_ROWS_PER_IMAGE:
        df_page = _sort_by_abs_variation(df).head(_MAX_ROWS_PER_IMAGE)
        suffix = f" — Top {_MAX_ROWS_PER_IMAGE} variations les plus fortes sur {len(df)} sites (détail complet : Excel / ZIP)"
        title = f"{title}{suffix}" if title else suffix.lstrip(" — ")
        return _render_table_image_page(df_page, old_label, new_label, title)

    return _render_table_image_page(df, old_label, new_label, title)


def render_table_image_pages(df: pd.DataFrame, old_label: str, new_label: str, title_prefix: str = "") -> List[bytes]:
    """Comme render_table_image, mais renvoie une image par tranche de
    _MAX_ROWS_PER_IMAGE lignes au lieu de tronquer — utilisé pour les exports
    ZIP où l'on veut garder l'intégralité des sites."""
    if df.empty:
        return [_render_table_image_page(df, old_label, new_label, title_prefix)]

    df_sorted = _sort_by_abs_variation(df)
    n_pages = (len(df_sorted) + _MAX_ROWS_PER_IMAGE - 1) // _MAX_ROWS_PER_IMAGE
    pages = []
    for p in range(n_pages):
        chunk = df_sorted.iloc[p * _MAX_ROWS_PER_IMAGE:(p + 1) * _MAX_ROWS_PER_IMAGE]
        page_title = title_prefix
        if n_pages > 1:
            page_title = f"{title_prefix} (page {p + 1}/{n_pages})" if title_prefix else f"page {p + 1}/{n_pages}"
        pages.append(_render_table_image_page(chunk, old_label, new_label, page_title))
    return pages


def export_images_by_territories_zip(df: pd.DataFrame, old_label: str, new_label: str) -> Optional[bytes]:
    """Génère un PNG par Territoire (même design que le tableau) et les regroupe dans un zip."""
    if df.empty:
        st.warning("⚠️ Aucune donnée à exporter.")
        return None

    territory_col = find_territory_col(df)
    if territory_col is None:
        st.error("❌ La colonne 'TERRITORY' n'a pas été trouvée dans le tableau.")
        return None

    territories = sorted(df[territory_col].dropna().unique().tolist())
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for terr in territories:
            df_terr = df[df[territory_col] == terr].copy()
            if df_terr.empty:
                continue
            png_bytes = render_table_image(
                df_terr, old_label, new_label,
                title=f"{terr} — {old_label} → {new_label}",
            )
            clean_terr_name = re.sub(r'[^\w\-_\. ]', '_', str(terr))
            zip_file.writestr(f"variation_territoire_{clean_terr_name}.png", png_bytes)

    return zip_buffer.getvalue()


# --------------------------------------------------------------------------
# EXPORT EXCEL — valeurs numériques + mise en forme conditionnelle réelle
# --------------------------------------------------------------------------
TERRITORY_COL = ("Informations", "TERRITORY")
CLUSTER_COL = ("Informations", "Cluster")

_FILL_GREEN = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
_FILL_YELLOW = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
_FILL_RED = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
_FONT_GREEN = Font(color="006100")
_FONT_YELLOW = Font(color="9C6500")
_FONT_RED = Font(color="9C0006")


def find_territory_col(df: pd.DataFrame):
    for col in df.columns:
        if isinstance(col, tuple):
            if "TERRITORY" in col or "Territoire" in col:
                return col
        elif col in ["TERRITORY", "Territoire"]:
            return col
    return None


def _write_sheet_with_conditional_formatting(writer, df: pd.DataFrame, sheet_name: str,
                                               old_label: str, new_label: str) -> None:
    # pandas.to_excel ne sait pas écrire des colonnes MultiIndex sans colonne
    # d'index ("index=False") -> on construit la feuille nous-mêmes pour
    # garder le contrôle total sur les 2 lignes d'en-tête et la mise en forme.
    ws = writer.book.create_sheet(sheet_name)
    writer.sheets[sheet_name] = ws

    header_rows = 2
    n_rows = df.shape[0]
    cols = list(df.columns)

    header_font = Font(color="FFFFFF", bold=True)
    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    subheader_fill = PatternFill(start_color="374151", end_color="374151", fill_type="solid")

    # -- ligne 1 : groupes fusionnés --
    c_idx, i = 1, 0
    while i < len(cols):
        top = cols[i][0]
        j = i
        while j < len(cols) and cols[j][0] == top:
            j += 1
        span = j - i
        start_letter = get_column_letter(c_idx)
        end_letter = get_column_letter(c_idx + span - 1)
        if span > 1:
            ws.merge_cells(f"{start_letter}1:{end_letter}1")
        cell = ws.cell(row=1, column=c_idx, value=re.sub(r'[^\w\s\-#%().]', '', str(top)).strip())
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        c_idx += span
        i = j

    # -- ligne 2 : sous-en-têtes --
    for c_idx, (top, sub) in enumerate(cols, start=1):
        cell = ws.cell(row=2, column=c_idx, value=sub)
        cell.font = header_font
        cell.fill = subheader_fill
        cell.alignment = Alignment(horizontal="center")

    ws.freeze_panes = ws.cell(row=header_rows + 1, column=1)

    for c_idx, col in enumerate(cols, start=1):
        top, sub = col
        letter = get_column_letter(c_idx)
        ws.column_dimensions[letter].width = 30 if sub == SITE_COL else 16

        for r_idx in range(n_rows):
            excel_row = header_rows + 1 + r_idx
            val = df.iloc[r_idx][col]
            cell = ws.cell(row=excel_row, column=c_idx, value=(None if pd.isna(val) else val))
            if pd.isna(val):
                continue

            if sub == "#day HVC" and top.startswith("🕒"):
                cell.number_format = "0.00"
                cell.alignment = Alignment(horizontal="center")
                if val < 0.8:
                    cell.fill, cell.font = _FILL_RED, _FONT_RED
                elif val < 1.0:
                    cell.fill, cell.font = _FILL_YELLOW, _FONT_YELLOW
                else:
                    cell.fill, cell.font = _FILL_GREEN, _FONT_GREEN

            elif sub == "%OOS HVC" and top.startswith("🕒"):
                cell.number_format = '0.00"%"'
                cell.alignment = Alignment(horizontal="center")
                if val < 20.0:
                    cell.fill, cell.font = _FILL_GREEN, _FONT_GREEN
                elif val <= 39.0:
                    cell.fill, cell.font = _FILL_YELLOW, _FONT_YELLOW
                else:
                    cell.fill, cell.font = _FILL_RED, _FONT_RED

            elif str(sub).startswith("Δ"):
                cell.number_format = '+0.00;-0.00;0.00'
                cell.alignment = Alignment(horizontal="center")
                if val > 0:
                    cell.fill, cell.font = _FILL_RED, _FONT_RED
                elif val < 0:
                    cell.fill, cell.font = _FILL_GREEN, _FONT_GREEN


def export_df_to_excel_by_territory(df: pd.DataFrame, old_label: str, new_label: str) -> bytes:
    """
    Excel avec :
    - Onglet 1 : Tous les Territoires
    - Onglets suivants : un par Territoire (mise en forme conditionnelle identique aux seuils écran)
    """
    output = io.BytesIO()
    territory_col = find_territory_col(df)

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if "Sheet" in writer.book.sheetnames:
            del writer.book["Sheet"]
        _write_sheet_with_conditional_formatting(writer, df, "Tous les Territoires", old_label, new_label)

        if territory_col is not None:
            territories = sorted(df[territory_col].dropna().unique().tolist())
            for terr in territories:
                df_terr = df[df[territory_col] == terr].copy()
                if df_terr.empty:
                    continue
                clean_sheet_name = re.sub(r'[\\/*?:\[\]]', '_', str(terr))[:30]
                _write_sheet_with_conditional_formatting(writer, df_terr, clean_sheet_name, old_label, new_label)

    return output.getvalue()


# --------------------------------------------------------------------------
# Interface Streamlit
# --------------------------------------------------------------------------
def render_oos_variation() -> None:
    st.subheader("📊 Suivi des variations #day HVC & %OOS HVC")

    with st.expander("📥 Importer de nouveaux fichiers d'export Excel", expanded=False):
        uploaded_files = st.file_uploader(
            "Téléverser un ou plusieurs fichiers Excel (.xlsx, .csv)",
            type=["xlsx", "csv"],
            accept_multiple_files=True
        )
        if uploaded_files:
            saved_names = []
            for ufile in uploaded_files:
                name = save_data_upload(ufile)
                if name:
                    saved_names.append(name)
            if saved_names:
                st.success(f"{len(saved_names)} fichier(s) téléversé(s) avec succès !")
                st.cache_data.clear()
                st.rerun()

    zones_df = load_zones_mapping_from_setting()
    dsm_df = load_dsm_mapping_from_settings()

    if zones_df.empty:
        st.warning("Veuillez charger le fichier Zones dans les paramètres.")
        return

    uploads = list_data_uploads()
    if len(uploads) < 2:
        st.info("Au moins deux exports sont nécessaires pour calculer des variations.")
        return

    available_dates = sorted(list({u["date"] for u in uploads}), reverse=True)
    selected_date = st.selectbox("📅 Choisir une date :", available_dates, format_func=lambda d: d.strftime("%d/%m/%Y"))

    date_uploads = [u for u in uploads if u["date"] == selected_date]
    if len(date_uploads) < 2:
        st.warning(f"Seulement {len(date_uploads)} export(s) disponible(s) le {selected_date:%d/%m/%Y}.")
        return

    time_options = {f"{u['time_str']} — {u['clean_name']}": u for u in date_uploads}
    selected_times = st.multiselect(
        "🕒 Créneaux à comparer :",
        options=list(time_options.keys()),
        default=list(time_options.keys())[-2:]
    )

    if len(selected_times) < 2:
        st.info("Sélectionnez au moins deux créneaux horaires.")
        return

    selected_uploads = sorted([time_options[k] for k in selected_times], key=lambda x: x["timestamp"])
    pairs = [(selected_uploads[i], selected_uploads[i + 1]) for i in range(len(selected_uploads) - 1)]

    for idx, (older, newer) in enumerate(pairs):
        old_label, new_label = older["time_str"], newer["time_str"]

        st.markdown(f"### 🔄 Comparaison : **{old_label}** ➔ **{new_label}**")

        old_df = load_site_level_data_bytes(get_file_bytes(STORAGE_FOLDER_DATA, older["name"]))
        new_df = load_site_level_data_bytes(get_file_bytes(STORAGE_FOLDER_DATA, newer["name"]))

        multi_df = compute_multiindex_variation(old_df, new_df, zones_df, dsm_df, old_label, new_label)

        col_f1, col_f2, col_f3 = st.columns([2, 2, 2])

        with col_f1:
            territory_col = ("Informations", "TERRITORY")
            if territory_col in multi_df.columns:
                all_terrs = sorted(multi_df[territory_col].dropna().unique().tolist())
                selected_territories = st.multiselect(
                    "🌍 Filtre Territoire", options=all_terrs, default=all_terrs, key=f"territory_{idx}"
                )
            else:
                selected_territories = []

        with col_f2:
            cluster_col = ("Informations", "Cluster")
            if cluster_col in multi_df.columns:
                if territory_col in multi_df.columns and selected_territories:
                    sub_df = multi_df[multi_df[territory_col].isin(selected_territories)]
                    cluster_list = sorted(sub_df[cluster_col].dropna().unique().tolist())
                else:
                    cluster_list = sorted(multi_df[cluster_col].dropna().unique().tolist())

                selected_clusters = st.multiselect(
                    "📍 Filtre Cluster", options=cluster_list, default=cluster_list, key=f"cluster_{idx}"
                )
            else:
                selected_clusters = []

        with col_f3:
            search_query = st.text_input("🔍 Rechercher un site...", key=f"search_{idx}").strip()

        filtered_df = multi_df.copy()
        if territory_col in filtered_df.columns and selected_territories:
            filtered_df = filtered_df[filtered_df[territory_col].isin(selected_territories)]
        if cluster_col in filtered_df.columns and selected_clusters:
            filtered_df = filtered_df[filtered_df[cluster_col].isin(selected_clusters)]
        if search_query:
            site_col_tuple = ("Informations", SITE_COL)
            if site_col_tuple in filtered_df.columns:
                filtered_df = filtered_df[
                    filtered_df[site_col_tuple].astype(str).str.contains(search_query, case=False, na=False)
                ]

        tot_day_hvc_old = filtered_df[(f"🕒 {old_label}", "#day HVC")].sum()
        tot_day_hvc_new = filtered_df[(f"🕒 {new_label}", "#day HVC")].sum()
        avg_oos_old = filtered_df[(f"🕒 {old_label}", "%OOS HVC")].mean()
        avg_oos_new = filtered_df[(f"🕒 {new_label}", "%OOS HVC")].mean()

        k1, k2, k3 = st.columns(3)
        k1.metric("Nombre de sites", len(filtered_df))
        k2.metric("Moyenne #day HVC", f"{tot_day_hvc_new / max(1, len(filtered_df)):.2f}",
                   delta=f"{(tot_day_hvc_new - tot_day_hvc_old) / max(1, len(filtered_df)):+.2f}")
        k3.metric("%OOS HVC Moyen Global", f"{avg_oos_new:.2f}%",
                   delta=f"{avg_oos_new - avg_oos_old:+.2f}%", delta_color="inverse")

        render_plotly_top_flops(filtered_df)

        st.markdown("#### Export")
        c_exp1, c_exp2, c_exp3 = st.columns(3)

        with c_exp1:
            excel_bytes = export_df_to_excel_by_territory(filtered_df, old_label, new_label)
            st.download_button(
                label="📊 Excel (par Territoires)",
                data=excel_bytes,
                file_name=f"variation_hvc_oos_{old_label}_to_{new_label}_{selected_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"download_excel_territoires_{idx}"
            )

        with c_exp2:
            current_view_png = render_table_image(
                filtered_df, old_label, new_label,
                title=f"Variation %OOS HVC — {old_label} → {new_label}",
            )
            st.download_button(
                label="🖼️ Image PNG (vue actuelle)",
                data=current_view_png,
                file_name=f"variation_hvc_oos_{old_label}_to_{new_label}_{selected_date}.png",
                mime="image/png",
                key=f"download_png_current_{idx}"
            )

        with c_exp3:
            zip_bytes = export_images_by_territories_zip(filtered_df, old_label, new_label)
            if zip_bytes:
                st.download_button(
                    label="📸 Images PNG par Territoire (ZIP)",
                    data=zip_bytes,
                    file_name=f"captures_territoires_{old_label}_to_{new_label}.zip",
                    mime="application/zip",
                    key=f"download_zip_territoires_{idx}"
                )

        st.dataframe(
            get_formatted_styler(filtered_df, old_label, new_label),
            use_container_width=True,
            hide_index=True
        )

        st.divider()