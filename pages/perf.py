# pages/cc_performance.py
import streamlit as st
import pandas as pd
import numpy as np
import os
from utils.helpers import clean_phone, to_excel
import plotly.graph_objects as go
import dataframe_image as dfi
from utils.storage import list_month_folders, get_files_by_month, upload_file_by_month
from utils.supabase import load_setting
import concurrent.futures

BUCKET_NAME = "performance-result-files"
CENTER_BUCKETS = {
    "centre_ii": "performance-result-files",
    "centre_iii": "performance-result-files",
}


def load_perf_folders(bucket, folders, max_workers=8):
    """
    Charge plusieurs dossiers en parallÃ¨le.
    """
    if not folders:
        return pd.DataFrame(), []

    dfs = []
    loaded_folders = []
    results = {}

    def load_single_folder(folder):
        try:
            df = get_files_by_month(
                bucket=bucket,
                selected_month=folder,
                max_workers=10   # ParallÃ©lisme interne par dossier
            )
            return folder, df
        except Exception as e:
            st.warning(f"Erreur chargement dossier {folder}: {e}")
            return folder, None

    # ParallÃ©lisme sur les dossiers
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_folder = {executor.submit(load_single_folder, folder): folder for folder in folders}

        for future in concurrent.futures.as_completed(future_to_folder):
            folder, df = future.result()
            if df is not None and not df.empty:
                df["source_folder"] = folder
                dfs.append(df)
                loaded_folders.append(folder)
                results[folder] = len(df)
            else:
                results[folder] = 0

    if not dfs:
        st.warning("Aucun fichier valide chargÃ© depuis les dossiers sÃ©lectionnÃ©s")
        return pd.DataFrame(), []

    # ConcatÃ©nation finale
    final_df = pd.concat(dfs, ignore_index=True)
    final_df.columns = [col.strip() for col in final_df.columns]

    # Rapport de chargement
    st.success(f"âœ… {len(loaded_folders)} dossier(s) chargÃ©s avec succÃ¨s")

    summary = pd.DataFrame({
        "Dossier": list(results.keys()),
        "Fichiers chargÃ©s": [results[f] for f in results]
    })
    st.dataframe(summary, use_container_width=True)

    return final_df, loaded_folders

def format_amount(x):
    if pd.isna(x) or x == 0:
        return "0"
    if x >= 1_000_000:
        return f"{x/1_000_000:.1f}M"
    elif x >= 1_000:
        return f"{x/1_000:.0f}K"
    else:
        return f"{int(x)}"

def style_perf(df):
    def parse_time(val):
        try:
            if pd.isna(val) or val == "N/A":
                return None
            return pd.to_datetime(str(val), format="%H:%M")
        except:
            return None

    def color_heure_debut(val):
        t = parse_time(val)
        if t is None:
            return ""

        minutes = t.hour * 60 + t.minute

        # 00:00 â†’ 07:30 = vert
        if minutes <= (7 * 60 + 30):
            return "background-color: #d8f3dc; color: black;"

        # 07:31 â†’ 07:59 = jaune
        elif minutes <= (7 * 60 + 59):
            return "background-color: #fff3bf; color: black;"

        # 08:00+ = rouge
        else:
            return "background-color: #ffd6d6; color: black;"

    def color_heure_fin(val):
        t = parse_time(val)
        if t is None:
            return ""

        minutes = t.hour * 60 + t.minute

        # 00:00 â†’ 14:59 = rouge
        if minutes < (15 * 60):
            return "background-color: #ffd6d6; color: black;"

        # 15:00 â†’ 16:59 = jaune
        elif minutes < (17 * 60):
            return "background-color: #fff3bf; color: black;"

        # 17:00+ = vert
        else:
            return "background-color: #d8f3dc; color: black;"

    def color_serve_20(val):
        try:
            v = float(val)

            if v < 10:
                return "background-color: #ffd6d6;"
            elif v < 20:
                return "background-color: #fff3bf;"
            else:
                return "background-color: #d8f3dc;"
        except:
            return ""

    def color_serve_5(val):
        try:
            v = float(val)

            if v < 2:
                return "background-color: #ffd6d6;"
            elif v < 5:
                return "background-color: #fff3bf;"
            else:
                return "background-color: #d8f3dc;"
        except:
            return ""

    def icon_sum_pos(val):
        try:
            v = float(val)

            if v < 20:
                return "❌ " + str(int(v))
            elif v < 40:
                return "⚠️ " + str(int(v))
            else:
                return "✅ " + str(int(v))
        except:
            return val

    def icon_tr(val):
        try:
            v = float(str(val).replace("%", "").strip())

            if 0 <= v <= 69:
                return f"❌ {v:.1f}%"
            elif 70 <= v <= 99:
                return f"⚠️ {v:.1f}%"
            elif v >= 100:
                return f"✅ {v:.1f}%"
            else:
                return f"{v:.1f}%"
        except:
            return val

    def triangle_new(val):
        try:
            v = float(val)

            # si au moins 1 nouveau client → triangle positif
            if v >= 1:
                return f"🟢 {int(v)}"

            # si 0 nouveau client → triangle négatif
            else:
                return f"🟠 {int(v)}"

        except:
            return val

    # ===================== transformation affichage =====================

    df = df.copy()

    # Σ_POS Serve → icônes
    df[("All segment", "Σ_POS Serve")] = df[
        ("All segment", "Σ_POS Serve")
    ].apply(icon_sum_pos)

    if ("TREND [14H→17H]", "New") in df.columns:
        df[("TREND [14H→17H]", "New")] = df[
            ("TREND [14H→17H]", "New")
        ].apply(triangle_new)

    # TR â†’ icÃ´nes
    for col in [
        ("HVC", "TR_HVC"),
        ("Others", "TR_Other"),
        ("All segment", "TR General")
    ]:
        if col in df.columns:
            df[col] = df[col].apply(icon_tr)

    # ===================== style =====================

    # ===================== STYLE =====================

    styled = (
        df.style

        # ===================== COLORS HEURES =====================
        .applymap(
            color_heure_debut,
            subset=[
                ("Dotations", "Heure"),
                ("Transactions (Transfert)", "Première")
            ]
        )

        .applymap(
            color_heure_fin,
            subset=[
                ("Transactions (Transfert)", "Dernière")
            ]
        )

        # ===================== TABLE STYLE =====================
        .set_table_styles([

            # HEADER GENERAL
            {
                "selector": "th",
                "props": [
                    ("background-color", "black"),
                    ("color", "#f1c40f"),
                    ("font-weight", "bold"),
                    ("text-align", "center"),
                    ("border", "1px solid #444"),
                ]
            },

            # CELLULES
            {
                "selector": "td",
                "props": [
                    ("text-align", "center"),
                    ("border", "1px solid #ddd"),
                    ("font-size", "11px"),
                    ("padding", "4px")
                ]
            },

            # ===================== DOTATIONS =====================
            {
                "selector": "th.col4, td.col4",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col5, td.col5",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # ===================== TRANSACTIONS =====================
            {
                "selector": "th.col6, td.col6",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col8, td.col8",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # ===================== HVC =====================
            {
                "selector": "th.col9, td.col9",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col11, td.col11",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # ===================== OTHERS =====================
            {
                "selector": "th.col12, td.col12",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col14, td.col14",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # ===================== ALL SEGMENT =====================
            {
                "selector": "th.col15, td.col15",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col17, td.col17",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # ===================== WORK PROGRESS =====================
            {
                "selector": "th.col18, td.col18",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col20, td.col20",
                "props": [("border-right", "4px solid #FFD966")]
            }

        ])
    )
    return styled

def get_hvc_list_from_master(master_df, center_key):
    if master_df is None:
        return []

    if center_key == "centre_iii":
        required_cols = ["agent_msisdn", "segment_group", "territory", "full_name"]
        missing = [col for col in required_cols if col not in master_df.columns]
        if missing:
            st.error(
                "Colonne(s) manquante(s) dans le fichier Maitre POS centre III: "
                + ", ".join(missing)
            )
            return []

        hvc_mask = master_df["segment_group"].astype(str).str.strip().str.upper().str.contains("HVC", na=False)
        return (
            master_df.loc[hvc_mask, "agent_msisdn"]
            .apply(clean_phone)
            .dropna()
            .astype(str)
            .tolist()
        )

    if "Segment Group" not in master_df.columns or "MSISDN" not in master_df.columns:
        st.error("Colonne(s) manquante(s) dans le fichier Maitre POS centre II: Segment Group, MSISDN")
        return []

    hvc_mask = master_df["Segment Group"].astype(str).str.strip().str.upper().str.contains("HVC", na=False)
    return (
        master_df.loc[hvc_mask, "MSISDN"]
        .apply(clean_phone)
        .dropna()
        .astype(str)
        .tolist()
    )


def compute_time_progress_work(df, hvc_list, global_excluded):
    windows = {
        "6h-9h50": (6 * 60, 9 * 60 + 50),
        "9h50-13h50": (9 * 60 + 50, 13 * 60 + 50),
        "13h50-17h50": (13 * 60 + 50, 17 * 60 + 50),
    }

    result = pd.DataFrame({"Nom_Ccial": df["Nom_Ccial"].dropna().unique()})
    work_df = df.copy()
    work_df["Time_Minutes"] = work_df["Date"].dt.hour * 60 + work_df["Date"].dt.minute

    for idx, (label, (start_minute, end_minute)) in enumerate(windows.items()):
        end_filter = (
            work_df["Time_Minutes"] <= end_minute
            if idx == len(windows) - 1
            else work_df["Time_Minutes"] < end_minute
        )

        window_base = work_df[
            (work_df["Type"] == "Transfer") &
            (work_df["Amount"] >= 10000) &
            (work_df["Time_Minutes"] >= start_minute) &
            end_filter &
            (~work_df["To_clean"].isin(global_excluded)) &
            (work_df["To_clean"].notna())
        ].copy()

        window_base["Progress_Segment"] = np.where(
            window_base["To_clean"].isin(hvc_list),
            "HVC",
            "OTHER"
        )

        grouped = (
            window_base
            .groupby(["Nom_Ccial", "Progress_Segment"])
            .agg(
                Serve=("To_clean", "nunique"),
                Nb_Trans=("To_clean", "size"),
                Nb_Jours=("Date_only", "nunique"),
            )
            .reset_index()
        )

        serve_col = f"HVC Serve {label}"
        tr_col = f"TR_HVC {label}"
        other_serve_col = f"Other Serve {label}"
        other_tr_col = f"TR_Other {label}"

        if grouped.empty:
            metrics = pd.DataFrame(columns=[
                "Nom_Ccial",
                serve_col,
                tr_col,
                other_serve_col,
                other_tr_col,
            ])
        else:
            grouped["TR"] = (
                (
                    grouped["Nb_Trans"]
                    / (grouped["Serve"].replace(0, np.nan) * grouped["Nb_Jours"].replace(0, np.nan) * 3)
                ) * 100
            ).fillna(0).round(1)

            metrics = grouped.pivot_table(
                index="Nom_Ccial",
                columns="Progress_Segment",
                values=["Serve", "TR"],
                fill_value=0,
                aggfunc="sum"
            )
            metrics.columns = [f"{a}_{b}" for a, b in metrics.columns]
            metrics = metrics.reset_index()
            metrics = metrics.rename(columns={
                "Serve_HVC": serve_col,
                "TR_HVC": tr_col,
                "Serve_OTHER": other_serve_col,
                "TR_OTHER": other_tr_col,
            })

        result = result.merge(metrics, on="Nom_Ccial", how="left")

        for col in [serve_col, tr_col, other_serve_col, other_tr_col]:
            if col not in result.columns:
                result[col] = 0
            result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0)

        result[serve_col] = result[serve_col].astype(int)
        result[other_serve_col] = result[other_serve_col].astype(int)
        result[tr_col] = result[tr_col].apply(lambda x: f"{x:.1f}%")
        result[other_tr_col] = result[other_tr_col].apply(lambda x: f"{x:.1f}%")

    return result


def _show_performance_center(center_label, center_key, master_df):
    st.subheader(center_label)
    
    comm_config = load_setting("commerciaux")
    exclusion_df = load_setting("caisses")
    exclusion_master = load_setting("masters")
    exclusion_cds = load_setting("cds")
    

    if comm_config is None:
        st.error("Veuillez charger le fichier **Configuration Commerciaux** dans Settings")
        return

    if master_df is None:
        st.error(f"Veuillez charger le fichier **{center_label}** dans Settings")
        return

    uploaded_key = f"{center_key}_files_uploaded"
    last_uploaded_key = f"{center_key}_last_uploaded_files"
    compile_mode_key = f"{center_key}_perf_compile_mode"
    compiled_folders_key = f"{center_key}_perf_compiled_folders"
    bucket_name = CENTER_BUCKETS.get(center_key, BUCKET_NAME)

    # ===============================
    # INIT SESSION STATE
    # ===============================
    if uploaded_key not in st.session_state:
        st.session_state[uploaded_key] = False

    if last_uploaded_key not in st.session_state:
        st.session_state[last_uploaded_key] = []

    if compile_mode_key not in st.session_state:
        st.session_state[compile_mode_key] = False

    if compiled_folders_key not in st.session_state:
        st.session_state[compiled_folders_key] = []

    # ===============================
    # UPLOAD UI
    # ===============================
    trans_files = st.file_uploader(
        "Upload les fichiers de transactions",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key=f"{center_key}_perf_trans"
    )

    # ===============================
    # DETECT NEW FILES
    # ===============================
    if trans_files:
        current_names = [f.name for f in trans_files]

        if current_names != st.session_state[last_uploaded_key]:
            st.session_state[uploaded_key] = False
            st.session_state[last_uploaded_key] = current_names

    # ===============================
    # UPLOAD LOGIC (SAFE)
    # ===============================
    if trans_files and not st.session_state[uploaded_key]:
        uploaded_count=0
        for file in trans_files:
            success = upload_file_by_month(
                bucket=bucket_name,
                uploaded_file=file
            )

            if success:
                uploaded_count += 1

        st.session_state[uploaded_key] = True
        get_files_by_month.clear()
        list_month_folders.clear()

        st.success(f"{uploaded_count} fichier(s) uploadÃ©(s) avec succÃ¨s")

        st.rerun()

    # ===============================
    # RESET BUTTON (OPTIONNEL)
    # ===============================
    if st.button("Reinitialiser les uploads", key=f"{center_key}_reset_uploads"):
        st.session_state[uploaded_key] = False
        st.session_state[last_uploaded_key] = []
        st.session_state[compile_mode_key] = False
        st.session_state[compiled_folders_key] = []
        st.info("Upload rÃ©initialisÃ©")

    with st.spinner("Analyse des performances en cours..."):
        # ===============================
        # SELECT DOSSIER
        # ===============================
        try:
            folders = list_month_folders(bucket=bucket_name)
        except Exception as e:
            st.error(f"Impossible de lire le bucket {bucket_name}: {e}")
            return

        if folders is None or len(folders) == 0:
            st.warning("Aucun dossier trouvÃ©")
            return

        selected_month = st.selectbox(
            "ðŸ“‚ Choisir le mois Ã  analyser",
            folders,
            index=len(folders) - 1,
            key=f"{center_key}_perf_default_folder"
        )

        compiled_defaults = [
            folder for folder in st.session_state[compiled_folders_key]
            if folder in folders
        ] or [selected_month]

        selected_compile_folders = st.multiselect(
            "Dossiers a compiler",
            folders,
            default=compiled_defaults,
            key=f"{center_key}_perf_compile_folders_selector"
        )

        col_compile, col_default = st.columns(2)
        with col_compile:
            if st.button("Compiler les dossiers", type="primary", key=f"{center_key}_compile_btn"):
                if selected_compile_folders:
                    st.session_state[compile_mode_key] = True
                    st.session_state[compiled_folders_key] = selected_compile_folders
                    st.rerun()
                else:
                    st.warning("Veuillez selectionner au moins un dossier a compiler.")

        with col_default:
            if st.button("Charger le dossier par defaut", key=f"{center_key}_default_folder_btn"):
                st.session_state[compile_mode_key] = False
                st.session_state[compiled_folders_key] = []
                st.rerun()

        # ===============================
        # LOAD DATA
        # ===============================
        if st.session_state[compile_mode_key]:
            folders_to_load = st.session_state[compiled_folders_key] or [selected_month]
            df, loaded_folders = load_perf_folders(
                bucket=bucket_name,
                folders=folders_to_load,
                max_workers=6
            )
            selected_month = " + ".join(loaded_folders)
        else:
            df = get_files_by_month(
                bucket=bucket_name,
                selected_month=selected_month,
                max_workers=12   # â† Ajuste entre 8 et 16 selon ta connexion
            )
            loaded_folders = [selected_month]

        if df is None or df.empty:
            st.warning("Aucun fichier de transactions trouvÃ©")
            return

        file_count = df['source_file'].nunique() if 'source_file' in df.columns else 0

        st.write(f"**Nombre de fichiers charges :** {file_count}")

        if st.session_state[compile_mode_key]:
            st.success(
                f"{len(df)} lignes chargees depuis {len(loaded_folders)} dossier(s): "
                f"{', '.join(loaded_folders)}"
            )
        else:
            st.success(f"{len(df)} lignes charges pour {selected_month}")

        # Nettoyage
        df['Date'] = pd.to_datetime(df.get('Date'), errors='coerce')
        df['Amount'] = pd.to_numeric(df.get('Amount'), errors='coerce').abs()

        if 'From' in df.columns:
            df['From_clean'] = df['From'].apply(clean_phone)
        if 'To' in df.columns:
            df['To_clean'] = df['To'].apply(clean_phone)

        df_full = df.copy()
        # =========================================================
        # ðŸ§¹ SUPPRESSION DES DOUBLONS (CRITIQUE)
        # =========================================================
        df = df.drop_duplicates(
            subset=['Date', 'From_clean', 'To_clean', 'Amount', 'Type'],
            keep='last'
        )

        df_full = df_full.drop_duplicates(
            subset=['Date', 'From_clean', 'To_clean', 'Amount', 'Type'],
            keep='last'
        )

        st.info(f"Apres deduplication : {len(df)} lignes")

        # ===================== FILTRE DATE =====================
        df['Date_only'] = df['Date'].dt.date
        df['Hour'] = df['Date'].dt.hour
        df_full['Date_only'] = df_full['Date'].dt.date

        min_date = df['Date_only'].min()
        max_date = df['Date_only'].max()

        # sÃ©curitÃ© si dates invalides
        if pd.isna(min_date) or pd.isna(max_date):
            st.error("Impossible de determiner les dates du fichier")
            return

        date_range = st.sidebar.date_input(
            f"Filtre Date ({selected_month})",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
            key=f"{center_key}_date_filter_{selected_month}"
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df['Date_only'] >= start_date) & (df['Date_only'] <= end_date)].copy()

            if start_date == end_date:
                st.sidebar.markdown("Filtre Heure")

                start_hour, end_hour = st.sidebar.slider(
                    "Plage horaire",
                    min_value=0,
                    max_value=23,
                    value=(0, 23),
                    key=f"{center_key}_hour_filter"
                )

                # application filtre heure
                df = df[
                    (df['Hour'] >= start_hour) &
                    (df['Hour'] <= end_hour)
                ].copy()
            else:
                if 'Hour' in df.columns:
                    df.drop(columns=['Hour'], inplace=True, errors='ignore')

                if 'Hour' in df_full.columns:
                    df_full.drop(columns=['Hour'], inplace=True, errors='ignore')


        # ===================== EXCLUSIONS GLOBAL COMPLET =====================
        commercial_numbers = comm_config['Ccial_MSISDN'].astype(str).str.strip().tolist()

        # Caisses (ancien exclusion_df)
        caisses = []
        if exclusion_df is not None and 'NUM' in exclusion_df.columns:
            caisses = exclusion_df['NUM'].apply(clean_phone).astype(str).tolist()

        # Masters (nouveau fichier Settings)
        masters_excl = []
        if exclusion_master is not None and 'NUM' in exclusion_master.columns:
            masters_excl = exclusion_master['NUM'].apply(clean_phone).astype(str).tolist()

        # CDS (nouveau fichier Settings)
        cds_excl = []
        if exclusion_cds is not None and 'NUM' in exclusion_cds.columns:
            cds_excl = exclusion_cds['NUM'].apply(clean_phone).astype(str).tolist()

        global_excluded = set(
            commercial_numbers +
            caisses +
            masters_excl +
            cds_excl
        )

        # ===================== IDENTIFICATION COMMERCIAUX =====================
        comm_config = comm_config.copy()
        comm_config['Ccial_MSISDN'] = comm_config['Ccial_MSISDN'].astype(str).str.strip()

        # On identifie le commercial uniquement via From (comme demandÃ©)
        df = df.merge(
            comm_config[['Ccial_MSISDN', 'Nom_Ccial', 'Zone_Territoire', 'Zone_SA', 'Zone_Centre']],
            left_on='From_clean',
            right_on='Ccial_MSISDN',
            how='left'
        )

        df = df[df['Nom_Ccial'].notna()].copy()
        df['Zone_Territoire'] = df['Zone_Territoire'].fillna("NON RENSEIGNÃ‰")
        df['Zone_SA'] = df['Zone_SA'].fillna("NON RENSEIGNÃ‰")
        df['Zone_Centre'] = df['Zone_Centre'].fillna("NON RENSEIGNÃ‰")
        df = df.groupby('source_file', group_keys=False).apply(
                lambda x: x[x['Nom_Ccial'] == x['Nom_Ccial'].value_counts().idxmax()]
            )

        if df.empty:
            st.error("Aucun commercial trouvÃ©")
            return

        # Zone_Centre
        if 'Zone_Centre' in comm_config.columns:
            df = df[df['Zone_Centre'] == center_label]
        else:
            st.sidebar.info("Colonne Zone_Centre non trouvÃ©e dans le fichier Commerciaux")

        # Filtre Zone_Territoire
        zone_list = ["Toutes"] + sorted(df['Zone_Territoire'].unique().tolist())
        selected_terr = st.sidebar.selectbox("Filtre Zone_Territoire", zone_list, key=f"{center_key}_zone_territoire")
        if selected_terr != "Toutes":
            df = df[df['Zone_Territoire'] == selected_terr]

        # Filtre Zone_SA
        zone_sa_list = ["Toutes"] + sorted(
            df['Zone_SA'].dropna().astype(str).unique().tolist()
        )
        selected_zone_sa = st.selectbox(
            "Filtrer All par Zone_SA",
            zone_sa_list,
            key=f"{center_key}_zone_sa_filter"
        )
        if selected_zone_sa != "Toutes":
            df = df[df['Zone_SA'] == selected_zone_sa]

        # ===================== DOTATION =====================
        # -----------------------------
        # Liste des numÃ©ros MASTER
        # -----------------------------
        master_numbers = set()
        if exclusion_master is not None and 'NUM' in exclusion_master.columns:
            master_numbers = set(
                exclusion_master['NUM']
                .apply(clean_phone)
                .astype(str)
            )

        # -----------------------------
        # Liste des numÃ©ros CAISSE
        # -----------------------------
        caisse_numbers = set()
        if exclusion_df is not None and 'NUM' in exclusion_df.columns:
            caisse_numbers = set(
                exclusion_df['NUM']
                .apply(clean_phone)
                .astype(str)
            )

        # -----------------------------
        # Transactions candidates
        # uniquement avant 11h
        # uniquement vers les commerciaux
        # -----------------------------
        dotation_trans = df_full[
            (df_full['Type'] == "Transfer") &
            (df_full['To_clean'].isin(df['From_clean'])) &
            (df_full['Date'].dt.hour < 11)
        ].copy()

        # identification du type de source
        def get_source_type(x):
            if x in master_numbers:
                return "MASTER"
            elif x in caisse_numbers:
                return "CAISSE"
            return None

        dotation_trans["Source_Type"] = dotation_trans["From_clean"].apply(get_source_type)

        # garder uniquement MASTER + CAISSE
        dotation_trans = dotation_trans[
            dotation_trans["Source_Type"].notna()
        ].copy()

        # rattacher le commercial
        dotation_trans = dotation_trans.merge(
            df[['source_file', 'Nom_Ccial']]
            .drop_duplicates(),
            on='source_file',
            how='left'
        )

        results = []

        # ===================================================
        # LOGIQUE :
        # - 1 seule transaction MASTER (la premiÃ¨re)
        # - 1 seule transaction CAISSE (la premiÃ¨re)
        # - max 2 transactions au total
        # ===================================================
        for (date, nom), group in dotation_trans.groupby(['Date_only', 'Nom_Ccial']):

            g = group.sort_values("Date")

            # premiÃ¨re transaction MASTER
            first_master = g[
                g["Source_Type"] == "MASTER"
            ].head(1)

            # premiÃ¨re transaction CAISSE
            first_caisse = g[
                g["Source_Type"] == "CAISSE"
            ].head(1)

            # concat des deux (max 2 lignes)
            final_tx = pd.concat([
                first_master,
                first_caisse
            ])

            montant = final_tx["Amount"].sum()

            heure = (
                final_tx["Date"].min().strftime("%H:%M")
                if not final_tx.empty else "N/A"
            )

            results.append({
                "Date_only": date,
                "Nom_Ccial": nom,
                "Heure_Dotation": heure,
                "Montant_Dotation": montant
            })

        dotation_group = pd.DataFrame(results)

        # sÃ©curitÃ© si vide
        if dotation_group.empty:
            dotation_group = pd.DataFrame(columns=[
                "Date_only",
                "Nom_Ccial",
                "Heure_Dotation",
                "Montant_Dotation"
            ])

        # ===================== CALCULS PRINCIPAUX =====================
        trans_df = df[
            (df['Type'] == "Transfer") &
            (df['From_clean'].isin(comm_config['Ccial_MSISDN'].astype(str)))
        ].copy()

        # def compute_perf(group):
        #     valid_trans = group[
        #         (~group['To_clean'].isin(global_excluded)) &
        #         (group['To_clean'].notna())
        #     ]

        #     filtered = valid_trans[valid_trans['Amount'] >= 10000]

        #     first_date = filtered['Date'].min()
        #     if pd.notna(first_date):
        #         premiere = first_date.strftime('%H:%M')
        #     else:
        #         premiere = 'N/A'

        #     last_date = filtered['Date'].max()
        #     if pd.notna(last_date):
        #         derniere = last_date.strftime('%H:%M')
        #     else:
        #         derniere = 'N/A'

        #     return pd.Series({
        #         # 'Date_only': group['Date_only'].iloc[0],
        #         'Zone_Centre': group['Zone_Centre'].iloc[0],
        #         'Zone_Territoire': group['Zone_Territoire'].iloc[0],
        #         'Zone_SA': group['Zone_SA'].iloc[0],
        #         'Premiere_Trans': premiere,
        #         'Derniere_Trans': derniere,
        #         'Nb_Transactions': filtered['Amount'].count()
        #     })

        trans_valid = trans_df[
            (~trans_df["To_clean"].isin(global_excluded)) &
            (trans_df["To_clean"].notna()) &
            (trans_df["Amount"] >= 10000)
        ].copy()
        first_trans = (
            trans_valid
            .groupby(["Date_only", "Nom_Ccial"], as_index=False)
            .agg(Premiere_Date=("Date", "min"))
        )

        first_trans["Premiere_Trans"] = (
            first_trans["Premiere_Date"]
            .dt.strftime("%H:%M")
        )

        last_trans = (
            trans_valid
            .groupby(["Date_only", "Nom_Ccial"], as_index=False)
            .agg(Derniere_Date=("Date", "max"))
        )

        last_trans["Derniere_Trans"] = (
            last_trans["Derniere_Date"]
            .dt.strftime("%H:%M")
        )

        nb_trans = (
            trans_valid
            .groupby(["Date_only", "Nom_Ccial"], as_index=False)
            .agg(
                Nb_Transactions=("Amount", "size"),
                Zone_Centre=("Zone_Centre", "first"),
                Zone_Territoire=("Zone_Territoire", "first"),
                Zone_SA=("Zone_SA", "first")
            )
        )

        perf = (
            nb_trans
            .merge(
                first_trans[
                    ["Date_only", "Nom_Ccial", "Premiere_Trans"]
                ],
                on=["Date_only", "Nom_Ccial"],
                how="left"
            )
            .merge(
                last_trans[
                    ["Date_only", "Nom_Ccial", "Derniere_Trans"]
                ],
                on=["Date_only", "Nom_Ccial"],
                how="left"
            )
        )
        # perf = (
        #     trans_df
        #     .groupby(['Date_only', 'Nom_Ccial'], as_index=False)
        #     .apply(compute_perf)
        #     .reset_index(drop=True)
        # )

        perf = perf.merge(dotation_group, on=['Date_only', 'Nom_Ccial'], how='left')
        perf['Montant_Dotation'] = perf['Montant_Dotation'].fillna(0)
        perf['Heure_Dotation'] = perf['Heure_Dotation'].fillna('N/A')
        perf[['Zone_Centre', 'Zone_Territoire', 'Zone_SA']] = perf[
            ['Zone_Centre', 'Zone_Territoire', 'Zone_SA']
        ].fillna("NON RENSEIGNÃ‰")


        # =========================================================
        # CONSOLIDATION PAR COMMERCIAL (UNE SEULE LIGNE PAR CCIAL)
        # =========================================================

        # =========================================================
        # IMPORTANT :
        # SUPPRIMER totalement l'ancien bloc :
        #
        # - groupby(source_file)
        # - bloc HVC / OTHERS / ALL SEGMENT ancien
        # - anciens calculs TR_HVC / TR_Other / TR_General
        #
        # et remplacer par CE BLOC UNIQUE
        # =========================================================

        # =========================================================
        # CALCUL HVC
        # =========================================================

        hvc_list = get_hvc_list_from_master(master_df, center_key)


       # =========================================================
        # BASE TRANSACTIONS (COMMUNES)
        # =========================================================
        base_trans = df[
            (df["Type"] == "Transfer") &
            (df["Amount"] >= 10000) &
            (~df["To_clean"].isin(global_excluded)) &
            (df["To_clean"].notna())
        ].copy()

        # =========================================================
        # SPLIT HVC / OTHERS
        # =========================================================
        base_trans["Segment"] = np.where(
            base_trans["To_clean"].isin(hvc_list),
            "HVC",
            "OTHER"
        )

        # =========================================================
        # CALCUL CENTRAL (ULTRA IMPORTANT)
        # FD + SERVE + COUNT CALCULÃ‰S ENSEMBLE
        # =========================================================
        def compute_segment_metrics(group):
            return pd.Series({
                "FD": group["Amount"].sum(),
                "Serve": group["To_clean"].nunique(),
                "Nb_Trans": len(group)
            })

        segment_group = (
            base_trans
            .groupby(
                ["Date_only", "Nom_Ccial", "Segment"],
                as_index=False
            )
            .agg(
                FD=("Amount", "sum"),
                Serve=("To_clean", "nunique"),
                Nb_Trans=("Amount", "size")
            )
        )

        # =========================================================
        # ðŸ”„ PIVOT
        # =========================================================
        st.write(segment_group.head())
        st.write(segment_group.columns.tolist())
        
        pivot_df = (
            segment_group
            .pivot_table(
                index=["Date_only", "Nom_Ccial"],
                columns="Segment",
                values=["FD", "Serve", "Nb_Trans"],
                fill_value=0
            )
        )

        pivot_df.columns = [f"{a}_{b}" for a, b in pivot_df.columns]
        pivot_df = pivot_df.reset_index()

        # =========================================================
        # ðŸ·ï¸ RENAME UNIQUE (UNE SEULE FOIS)
        # =========================================================
        pivot_df = pivot_df.rename(columns={
            "FD_HVC": "FD_HVC",
            "Serve_HVC": "HVC_Serve",
            "Nb_Trans_HVC": "Nb_Trans_HVC",

            "FD_OTHER": "FD_Others",
            "Serve_OTHER": "Other_Serve",
            "Nb_Trans_OTHER": "Nb_Trans_Other"
        })

        # =========================================================
        # ðŸ›¡ï¸ SÃ‰CURISATION COLONNES (APRÃˆS RENAME)
        # =========================================================
        expected_cols = [
            "FD_HVC", "HVC_Serve",
            "FD_Others", "Other_Serve",
            "Nb_Trans_HVC", "Nb_Trans_Other"
        ]

        for col in expected_cols:
            if col not in pivot_df.columns:
                pivot_df[col] = 0

        # =========================================================
        # ðŸ”— MERGE
        # =========================================================
        perf = perf.merge(
            pivot_df,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        # =========================================================
        # ðŸ§¹ SUPPRESSION DOUBLONS COLONNES (CRITIQUE)
        # =========================================================
        perf = perf.loc[:, ~perf.columns.duplicated()]

        # =========================================================
        # ðŸ§¹ FILLNA SAFE
        # =========================================================
        for col in expected_cols:
            perf[col] = (
                pd.to_numeric(
                    perf[col],
                    errors="coerce"
                )
                .fillna(0)
            )

        hvc_serve_period = (
            base_trans[base_trans["Segment"] == "HVC"]
            .groupby("Nom_Ccial")["To_clean"]
            .nunique()
        )
        other_serve_period = (
            base_trans[base_trans["Segment"] == "OTHER"]
            .groupby("Nom_Ccial")["To_clean"]
            .nunique()
        )

        perf["HVC_Serve"] = (
            perf["Nom_Ccial"].map(hvc_serve_period).fillna(0)
        )
        perf["Other_Serve"] = (
            perf["Nom_Ccial"].map(other_serve_period).fillna(0)
        )

        # =========================================================
        # âž• COLONNES GLOBALES
        # =========================================================
        perf["Σ_FD"] = perf["FD_HVC"] + perf["FD_Others"]

        all_segment_period = (
            base_trans
            .groupby("Nom_Ccial")
            .agg(**{"Σ_POS_Serve": ("To_clean", "nunique")})
            .reset_index()
        )

        perf = perf.merge(
            all_segment_period,
            on="Nom_Ccial",
            how="left"
        )
        perf["Σ_POS_Serve"] = pd.to_numeric(
            perf["Σ_POS_Serve"],
            errors="coerce"
        ).fillna(0)

        # =========================================================
        # ðŸ” CONTROLE QUALITÃ‰ (CRITIQUE)
        # =========================================================
        anomaly = perf[
            ((perf["FD_HVC"] == 0) & (perf["HVC_Serve"] > 0)) |
            ((perf["FD_HVC"] > 0) & (perf["HVC_Serve"] == 0))
        ]

        if not anomaly.empty:
            st.error("Incoherence detectée entre FD_HVC et HVC_Serve")
            st.dataframe(anomaly, height=300)

        time_progress = compute_time_progress_work(
            df=df,
            hvc_list=hvc_list,
            global_excluded=global_excluded
        )

        def time_to_minutes(val):
            try:
                if pd.isna(val) or val in ["N/A", "", None]:
                    return np.nan
                h, m = str(val).split(":")
                return int(h) * 60 + int(m)
            except:
                return np.nan


        def minutes_to_time(val):
            try:
                if pd.isna(val):
                    return "N/A"
                val = int(round(val))
                h = val // 60
                m = val % 60
                return f"{h:02d}:{m:02d}"
            except:
                return "N/A"


        # conversion heures â†’ minutes
        perf["Heure_Dotation_min"] = perf["Heure_Dotation"].apply(time_to_minutes)
        perf["Premiere_Trans_min"] = perf["Premiere_Trans"].apply(time_to_minutes)
        perf["Derniere_Trans_min"] = perf["Derniere_Trans"].apply(time_to_minutes)

        # conversion valeurs numÃ©riques
        numeric_cols = [
            "Montant_Dotation",
            "Nb_Transactions",
            "Nb_Trans_HVC",
            "Nb_Trans_Other",
            "FD_HVC",
            "HVC_Serve",
            "FD_Others",
            "Other_Serve",
            "Σ_FD",
            "Σ_POS_Serve",
        ]

        for col in numeric_cols:
            perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0)

        # nombre de jours rÃ©ellement filtrÃ©s
        nb_days = perf["Date_only"].nunique()

        if nb_days == 0:
            nb_days = 1

        required_tours_period = 3 * nb_days
        perf["Nb_Jours"] = perf["Date_only"]


        # consolidation finale
        perf_final = perf.groupby(
            ["Nom_Ccial", "Zone_SA", "Zone_Territoire", "Zone_Centre"],
            as_index=False
        ).agg({
            "Nb_Jours": lambda x: x[
                perf.loc[x.index, "Nb_Transactions"] > 0
            ].nunique(),
            "Heure_Dotation_min": "mean",
            "Premiere_Trans_min": "mean",
            "Derniere_Trans_min": "mean",

            "Montant_Dotation": "sum",
            "Nb_Transactions": "sum",
            "Nb_Trans_HVC":"sum",
            "Nb_Trans_Other":"sum",

            "FD_HVC": "sum",
            "HVC_Serve": "max",

            "FD_Others": "sum",
            "Other_Serve": "max",

            "Σ_FD": "sum",
            "Σ_POS_Serve": "max",
        })

               # =========================================================
        # TOUS LES COMMERCIAUX SETTINGS
        # =========================================================
        all_commerciaux = comm_config[
            ['Nom_Ccial', 'Zone_SA', 'Zone_Territoire', 'Zone_Centre']
        ].drop_duplicates().copy()

        all_commerciaux = all_commerciaux.fillna("NON RENSEIGNÃ‰")

        # =========================================================
        # FILTRE PAR CENTRE ACTIF (TAB)
        # =========================================================

        all_commerciaux = all_commerciaux[
            all_commerciaux['Zone_Centre'] == center_label
        ].copy()

        # Filtre Zone_Territoire
        if selected_terr != "Toutes":
            all_commerciaux = all_commerciaux[
                all_commerciaux['Zone_Territoire'] == selected_terr
            ].copy()

        # Filtre Zone_SA
        if selected_zone_sa != "Toutes":
            all_commerciaux = all_commerciaux[
                all_commerciaux['Zone_SA'] == selected_zone_sa
            ].copy()

        # =========================================================
        # MERGE FINAL SÃ‰CURISÃ‰
        # =========================================================
        merge_keys = ['Nom_Ccial', 'Zone_SA', 'Zone_Territoire', 'Zone_Centre']


        perf_final = all_commerciaux.merge(
            perf_final,
            on=merge_keys,
            how="left"
        )

        # Remplissage des valeurs manquantes pour les commerciaux sans activitÃ©
        numeric_cols = ["Nb_Jours", "Montant_Dotation", "Nb_Transactions",
                       "HVC_Serve", "Other_Serve", "Σ_POS_Serve", "FD_HVC", "FD_Others", "Σ_FD"]

        for col in numeric_cols:
            if col in perf_final.columns:
                perf_final[col] = pd.to_numeric(perf_final[col], errors='coerce').fillna(0).astype(int)

        perf_final = perf_final.merge(
            time_progress,
            on="Nom_Ccial",
            how="left"
        )

        # =========================================================
        # FILLNA COMMERCIAUX SANS ACTIVITE
        # =========================================================

        numeric_cols_fill = [
            "Nb_Jours",
            "Montant_Dotation",
            "Nb_Transactions",
            "Nb_Trans_HVC",
            "Nb_Trans_Other",
            "FD_HVC",
            "HVC_Serve",
            "FD_Others",
            "Other_Serve",
            "Σ_FD",
            "Σ_POS_Serve",
        ]

        for col in numeric_cols_fill:
            if col in perf_final.columns:
                perf_final[col] = (
                    pd.to_numeric(perf_final[col], errors="coerce")
                    .fillna(0).astype(int)
                )

        time_cols = [
            "Heure_Dotation",
            "Premiere_Trans",
            "Derniere_Trans"
        ]

        for col in time_cols:
            if col in perf_final.columns:
                perf_final[col] = perf_final[col].fillna("N/A")


        time_progress_labels = ["6h-9h50", "9h50-13h50", "13h50-17h50"]
        for label in time_progress_labels:
            serve_col = f"HVC Serve {label}"
            tr_col = f"TR_HVC {label}"
            other_serve_col = f"Other Serve {label}"
            other_tr_col = f"TR_Other {label}"
            display_col = f"TPW {label}"

            for col in [serve_col, tr_col, other_serve_col, other_tr_col]:
                if col not in perf_final.columns:
                    perf_final[col] = 0

            perf_final[serve_col] = pd.to_numeric(
                perf_final[serve_col],
                errors="coerce"
            ).fillna(0).astype(int)
            perf_final[tr_col] = (
                perf_final[tr_col]
                .fillna("0.0%")
                .astype(str)
            )
            perf_final[other_serve_col] = pd.to_numeric(
                perf_final[other_serve_col],
                errors="coerce"
            ).fillna(0).astype(int)
            perf_final[other_tr_col] = (
                perf_final[other_tr_col]
                .fillna("0.0%")
                .astype(str)
            )
            perf_final[display_col] = (
                perf_final[serve_col].astype(str)
                + " | "
                + perf_final[tr_col]
                + " | "
                + perf_final[other_serve_col].astype(str)
                + " | "
                + perf_final[other_tr_col]
            )

        # retour format heure
        perf_final["Heure_Dotation"] = perf_final["Heure_Dotation_min"].apply(minutes_to_time)
        perf_final["Premiere_Trans"] = perf_final["Premiere_Trans_min"].apply(minutes_to_time)
        perf_final["Derniere_Trans"] = perf_final["Derniere_Trans_min"].apply(minutes_to_time)
        perf_final = perf_final.rename(columns={
            "Date": "Nb_Jours"
        })

        # recalcul TR sur la pÃ©riode complÃ¨te
        perf_final["TR_HVC"] = (
            (
                perf_final['Nb_Trans_HVC']
                / (perf_final["HVC_Serve"].replace(0, np.nan) * perf_final["Nb_Jours"] * 3)
            ) * 100
        ).fillna(0).round(1)

        perf_final["TR_Other"] = (
            (
                perf_final['Nb_Trans_Other']
                / (perf_final["Other_Serve"].replace(0, np.nan) * perf_final["Nb_Jours"] * 3)
            ) * 100
        ).fillna(0).round(1)

        perf_final["TR_General"] = (
            (
                perf_final["Nb_Transactions"]
                / (perf_final["Σ_POS_Serve"].replace(0, np.nan) * perf_final["Nb_Jours"] * 3)
            ) * 100
        ).fillna(0).round(1)

        perf_final["Σ_FD_raw"] = perf_final["Σ_FD"]

        # formatage affichage
        for col in ["Montant_Dotation", "FD_HVC", "FD_Others", "Σ_FD"]:
            perf_final[col] = perf_final[col].apply(format_amount)

        perf_final["TR_HVC"] = perf_final["TR_HVC"].apply(lambda x: f"{x:.1f}%")
        perf_final["TR_Other"] = perf_final["TR_Other"].apply(lambda x: f"{x:.1f}%")
        perf_final["TR_General"] = perf_final["TR_General"].apply(lambda x: f"{x:.1f}%")
        perf_final['HVC_Serve'] = pd.to_numeric(perf_final['HVC_Serve'], errors='coerce').fillna(0).astype(int)
        perf_final['Other_Serve'] = pd.to_numeric(perf_final['Other_Serve'], errors='coerce').fillna(0).astype(int)
        perf_final['Σ_POS_Serve'] = pd.to_numeric(perf_final['Σ_POS_Serve'], errors='coerce').fillna(0).astype(int)
        # perf = perf.rename(columns={'Date_only': 'Date'})

        # dataset principal devient le consolidé
        perf = perf_final.copy()

        # ===================== AFFICHAGE =====================
        # ===================== MULTI-INDEX HEADER =====================
        columns = pd.MultiIndex.from_tuples([
            ("", "Zone_Centre"),
            ("", "Zone_SA"),
            ("", "Nom_Ccial"),
            ("", "Nb_Jours"),

            ("Dotations", "Heure"),
            ("Dotations", "Montant"),

            ("Transactions (Transfert)", "Première"),
            ("Transactions (Transfert)", "Dernière"),
            ("Transactions (Transfert)", "Σ trx"),

            ("HVC", "FD_HVC"),
            ("HVC", "HVC_Serve"),
            ("HVC", "TR_HVC"),

            ("Others", "FD_Others"),
            ("Others", "Other_Serve"),
            ("Others", "TR_Other"),

            ("All segment", "Σ_FD"),
            ("All segment", "Σ_POS Serve"),
            ("All segment", "TR General"),

            ("Work Progress (HVC Serve | TR_HVC | Other Serve | TR_Other)", "6h-9h50"),
            ("Work Progress (HVC Serve | TR_HVC | Other Serve | TR_Other)", "9h50-13h50"),
            ("Work Progress (HVC Serve | TR_HVC | Other Serve | TR_Other)", "13h50-17h50")
        ])

        def highlight_dotation(row):
            trx = row[("Transactions (Transfert)", "Σ trx")]
            serve = row[("All segment", "Σ_POS Serve")]

            if (
                pd.isna(trx)
                or trx == 0
                or pd.isna(serve)
                or serve == 0
            ):
                return ['background-color: red'] * len(row)

            return [''] * len(row)

        perf_display = perf.copy()

        perf_display = perf_display[[
            'Zone_Centre','Zone_SA', 'Nom_Ccial',
            'Nb_Jours',
            'Heure_Dotation', 'Montant_Dotation',
            'Premiere_Trans', 'Derniere_Trans', 'Nb_Transactions',
            'FD_HVC', 'HVC_Serve', 'TR_HVC',
            'FD_Others', 'Other_Serve', 'TR_Other',
            'Σ_FD', 'Σ_POS_Serve', 'TR_General',
            'TPW 6h-9h50', 'TPW 9h50-13h50', 'TPW 13h50-17h50',
        ]]

        perf_display.columns = columns

        st.subheader("Performance Detaillee des Commerciaux")
        styled_df = style_perf(perf_display)
        styled_df = styled_df.apply(highlight_dotation, axis=1)

        st.dataframe(styled_df, use_container_width=True, height=650)

        # ===================== TOP 10 COMMERCIAUX =====================
        st.subheader("Top 10 Commerciaux")

        # Filtre Zone_SA
        zone_sa_list = ["Toutes"] + sorted(
            perf['Zone_SA'].dropna().astype(str).unique().tolist()
        )

        selected_zone_sa = st.selectbox(
            "Filtrer par Zone_SA",
            zone_sa_list,
            key=f"{center_key}_top10_zone_sa"
        )

        top_perf = perf.copy()

        if selected_zone_sa != "Toutes":
            top_perf = top_perf[top_perf['Zone_SA'] == selected_zone_sa].copy()

        # ===================== PRÃ‰PARATION DES DONNÃ‰ES =====================
        # Conversions numÃ©riques
        top_perf['TR_General_num'] = (
            top_perf['TR_General'].astype(str).str.replace('%', '', regex=False)
        )
        top_perf['TR_General_num'] = pd.to_numeric(top_perf['TR_General_num'], errors='coerce').fillna(0)

        for col in ['HVC_Serve', 'Σ_FD_raw', 'Σ_POS_Serve', 'Nb_Jours']:
            if col in top_perf.columns:
                top_perf[col] = pd.to_numeric(top_perf[col], errors='coerce').fillna(0)

        # Heure de premiÃ¨re transaction (plus tÃ´t = mieux)
        top_perf['Premiere_Trans_min'] = top_perf['Premiere_Trans'].apply(time_to_minutes)
        top_perf['Premiere_Trans_min'] = top_perf['Premiere_Trans_min'].fillna(9999)

        # ===================== SCORE COMPOSITE Ã‰QUILIBRÃ‰ =====================
        # Normalisation + pondÃ©ration (tu peux ajuster les poids)

        # Normalisation (Min-Max) pour chaque critÃ¨re
        def normalize(series):
            min_val = series.min()
            max_val = series.max()
            return (series - min_val) / (max_val - min_val + 1e-8) if max_val > min_val else 0

        top_perf['Score_Jours'] = normalize(top_perf['Nb_Jours'])
        top_perf['Score_HVC'] = normalize(top_perf['HVC_Serve'])
        top_perf['Score_TR'] = normalize(top_perf['TR_General_num'])
        top_perf['Score_FD'] = normalize(top_perf['Σ_FD_raw'])
        top_perf['Score_Premiere'] = 1 - normalize(top_perf['Premiere_Trans_min'])  # inversÃ© (plus tÃ´t = mieux)

        # Score final pondÃ©rÃ© (ajuste les % selon ton besoin)
        top_perf['Score_Final'] = (
            top_perf['Score_Jours'] * 0.20 +    # 20% jours de travail
            top_perf['Score_HVC'] * 0.20 +      # 20% HVC_Serve
            top_perf['Score_TR'] * 0.20 +       # 20% Taux de Rotation
            top_perf['Score_FD'] * 0.20 +       # 20% Volume d'affaires
            top_perf['Score_Premiere'] * 0.20   # 20% Commence tÃ´t
        )

        # ===================== TRI & CLASSEMENT =====================
        top_perf = top_perf.sort_values(
            by='Score_Final',
            ascending=False
        ).head(10).copy()

        # Ajout du rang et mÃ©dailles
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        top_perf = top_perf.reset_index(drop=True)
        top_perf['Rang'] = top_perf.index + 1
        top_perf['Classement'] = top_perf['Rang'].apply(
            lambda x: f"{medals.get(x, '')} {x}"
        )

        # ===================== AFFICHAGE =====================
        top10_display = top_perf[[
            'Classement',
            'Nom_Ccial',
            'Zone_Centre',
            'Zone_SA',
            'Nb_Jours',
            'Premiere_Trans',
            'HVC_Serve',
            'Σ_FD',
            'Σ_POS_Serve',
            'TR_General',
            'Score_Final'
        ]].rename(columns={
            'Classement': '🥇 Rang',
            'Nom_Ccial': 'Commercial',
            'Zone_Centre': 'Zone_Centre',
            'Zone_SA': 'Zone_SA',
            'Nb_Jours': 'Nb_Jours',
            'Premiere_Trans': 'PremiÃ¨re Transaction',
            'HVC_Serve': 'HVC_Serve',
            'Σ_FD': 'Σ_FD',
            'Σ_POS_Serve': 'Σ_POS_Serve',
            'TR_General': 'TR_General',
            'Score_Final': 'Score Global'
        })

        # Optionnel : formater le score
        top10_display['Score Global'] = top10_display['Score Global'].round(3)

        st.dataframe(
            top10_display,
            use_container_width=True,
            height=450
        )

        # ===================== EXPORT IMAGE PAR BLOCS =====================

        if st.button("Capturer tableau complet en image", key=f"{center_key}_capture_perf_images"):
            # dossier temporaire
            export_folder = "exports_perf"
            os.makedirs(export_folder, exist_ok=True)

            # supprimer anciens fichiers
            for file in os.listdir(export_folder):
                file_path = os.path.join(export_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            # =========================================================
            # TABLEAU COMPLET (TOUTES LES LIGNES)
            # =========================================================

            full_table = perf_display.copy()

            # rÃ©appliquer le style
            styled_table = style_perf(full_table)
            styled_table = styled_table.apply(highlight_dotation, axis=1)

            # nom fichier
            file_path = os.path.join(export_folder, "Performance_Commerciaux_Complet.png")

            # export image
            dfi.export(
                styled_table,
                file_path,
                table_conversion="chrome"
            )

            # tÃ©lÃ©chargement direct
            with open(file_path, "rb") as f:
                st.download_button(
                    "ðŸ“¥ TÃ©lÃ©charger la capture complÃ¨te",
                    f,
                    "Performance_Commerciaux_Complet.png",
                    "image/png",
                    key=f"{center_key}_download_full_image"
                )
        col1, col2 = st.columns(2)
        excel_data = to_excel(perf)
        col1.download_button("Telecharger Excel", excel_data, f"Performance_Commerciaux_{center_key}.xlsx", key=f"{center_key}_download_excel")
        col2.download_button("Telecharger CSV", perf.to_csv(index=False).encode('utf-8'), f"Performance_Commerciaux_{center_key}.csv", key=f"{center_key}_download_csv")


def show_performance():
    st.title("Performance Commerciaux")

    center_ii_master_df = load_setting('maitre_pos')
    center_iii_master_df = load_setting('maitre_pos_III')

    tab_centre_ii, tab_centre_iii = st.tabs(["Centre II", "Centre III"])

    with tab_centre_ii:
        _show_performance_center(
            center_label="Centre II",
            center_key="centre_ii",
            master_df=center_ii_master_df
        )

    with tab_centre_iii:
        _show_performance_center(
            center_label="Centre III",
            center_key="centre_iii",
            master_df=center_iii_master_df
        )
