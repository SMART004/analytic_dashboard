# pages/cc_performance.py
import streamlit as st
import pandas as pd
import numpy as np
import os
import zipfile
from utils.helpers import load_file, clean_phone, to_excel
import plotly.graph_objects as go
import dataframe_image as dfi
from utils.storage import upload_file, get_all_files, file_hash
from utils.supabase import supabase

BUCKET_NAME = "performance-result-files"

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

        # 00:00 → 07:30 = vert
        if minutes <= (7 * 60 + 30):
            return "background-color: #d8f3dc; color: black;"

        # 07:31 → 07:59 = jaune
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

        # 00:00 → 14:59 = rouge
        if minutes < (15 * 60):
            return "background-color: #ffd6d6; color: black;"

        # 15:00 → 16:59 = jaune
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

    df[("TREND [14H→17H]","New")]=df[
        ("TREND [14H→17H]","New")
    ].apply(triangle_new)

    # TR → icônes
    for col in [
        ("HVC", "TR_HVC"),
        ("Others", "TR_Other"),
        ("All segment", "TR General")
    ]:
        df[col] = df[col].apply(icon_tr)

    # ===================== style =====================

    styled = (
        df.style
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
        .applymap(
            color_serve_20,
            subset=[
                ("HVC", "HVC_Serve"),
                ("Others", "Other_Serve")
            ]
        )
        .applymap(
            color_serve_5,
            subset=[
                ("TREND [14H→17H]", "POS_serve")
            ]
        )
        .set_table_styles([
            {
                "selector": "th",
                "props": [
                    ("background-color", "black"),
                    ("color", "#f1c40f"),
                    ("font-weight", "bold"),
                    ("text-align", "center"),
                    ("border", "1px solid #444")
                ]
            },
            # Dotations
            {
                "selector": "th.col3, td.col3",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col4, td.col4",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # HVC
            {
                "selector": "th.col8, td.col8",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col10, td.col10",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # Others
            {
                "selector": "th.col11, td.col11",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col13, td.col13",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # All segment
            {
                "selector": "th.col14, td.col14",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col16, td.col16",
                "props": [("border-right", "4px solid #FFD966")]
            },

            # Trend
            {
                "selector": "th.col17, td.col17",
                "props": [("border-left", "4px solid #FFD966")]
            },
            {
                "selector": "th.col18, td.col18",
                "props": [("border-right", "4px solid #FFD966")]
            },
            {
                "selector": "td",
                "props": [
                    ("text-align", "center"),
                    ("border", "1px solid #ddd")
                ]
            }
        ])
    )

    return styled

def show_performance():
    st.title("📈 Performance Commerciaux")

    comm_config = st.session_state.get('commercial_config_df')
    exclusion_df = st.session_state.get('exclusion_df')
    exclusion_master = st.session_state.get('exclusion_master')
    exclusion_cds = st.session_state.get('exclusion_cds')
    master_df = st.session_state.get('pos_master_df')

    if comm_config is None:
        st.error("Veuillez charger le fichier **Configuration Commerciaux** dans Settings")
        st.stop()

    # ===============================
    # INIT SESSION STATE
    # ===============================
    if "files_uploaded" not in st.session_state:
        st.session_state.files_uploaded = False

    if "last_uploaded_files" not in st.session_state:
        st.session_state.last_uploaded_files = []

    # ===============================
    # UPLOAD UI
    # ===============================
    trans_files = st.file_uploader(
        "Upload les fichiers de transactions",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="perf_trans"
    )

    # ===============================
    # DETECT NEW FILES
    # ===============================
    if trans_files:
        current_names = [f.name for f in trans_files]

        if current_names != st.session_state.last_uploaded_files:
            st.session_state.files_uploaded = False
            st.session_state.last_uploaded_files = current_names

    # ===============================
    # UPLOAD LOGIC (SAFE)
    # ===============================
    if trans_files and not st.session_state.files_uploaded:

        seen_hashes = set()
        uploaded_count = 0

        for file in trans_files:

            file_md5 = file_hash(file)

            # éviter doublon dans le même batch
            if file_md5 in seen_hashes:
                continue

            seen_hashes.add(file_md5)

            success = upload_file(
                supabase=supabase,
                bucket=BUCKET_NAME,
                uploaded_file=file
            )

            if success:
                uploaded_count += 1

        st.session_state.files_uploaded = True
        get_all_files.clear()

        st.success(f"{uploaded_count} fichier(s) uploadé(s) avec succès")

        st.rerun()

    # ===============================
    # RESET BUTTON (OPTIONNEL)
    # ===============================
    if st.button("🔄 Réinitialiser les uploads"):
        st.session_state.files_uploaded = False
        st.session_state.last_uploaded_files = []
        st.info("Upload réinitialisé")

    with st.spinner("Analyse des performances en cours..."):
        df = get_all_files(
            bucket=BUCKET_NAME
        )

        if df is None or df.empty:
            st.warning("Aucun fichier de transactions trouvé")
            st.stop()

        st.success(f"{len(df)} lignes chargées")

        # Nettoyage
        df['Date'] = pd.to_datetime(df.get('Date'), errors='coerce')
        df['Amount'] = pd.to_numeric(df.get('Amount'), errors='coerce').abs()

        if 'From' in df.columns:
            df['From_clean'] = df['From'].apply(clean_phone)
        if 'To' in df.columns:
            df['To_clean'] = df['To'].apply(clean_phone)

        df_full = df.copy()
        # =========================================================
        # 🧹 SUPPRESSION DES DOUBLONS (CRITIQUE)
        # =========================================================
        df = df.drop_duplicates(
            subset=['Date', 'From_clean', 'To_clean', 'Amount', 'Type'],
            keep='last'
        )

        df_full = df_full.drop_duplicates(
            subset=['Date', 'From_clean', 'To_clean', 'Amount', 'Type'],
            keep='last'
        )

        st.info(f"🧹 Après déduplication : {len(df)} lignes")
        
        # ===================== FILTRE DATE =====================
        df['Date_only'] = df['Date'].dt.date
        df_full['Date_only'] = df_full['Date'].dt.date

        min_date = df['Date_only'].min()
        max_date = df['Date_only'].max()

        date_range = st.sidebar.date_input(
            "Filtre Date",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df['Date_only'] >= start_date) & (df['Date_only'] <= end_date)]

        
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

        # On identifie le commercial uniquement via From (comme demandé)
        df = df.merge(
            comm_config[['Ccial_MSISDN', 'Nom_Ccial', 'Zone_Territoire', 'Zone_SA']],
            left_on='From_clean',
            right_on='Ccial_MSISDN',
            how='left'
        )

        df = df[df['Nom_Ccial'].notna()].copy()
        df['Zone_Territoire'] = df['Zone_Territoire'].fillna("NON RENSEIGNÉ")
        df['Zone_SA'] = df['Zone_SA'].fillna("NON RENSEIGNÉ")

        df = df.groupby('source_file', group_keys=False).apply(
                lambda x: x[x['Nom_Ccial'] == x['Nom_Ccial'].value_counts().idxmax()]
            )

        if df.empty:
            st.error("Aucun commercial trouvé")
            st.stop()

        # Filtre Zone_Territoire
        zone_list = ["Toutes"] + sorted(df['Zone_Territoire'].unique().tolist())
        selected_terr = st.sidebar.selectbox("Filtre Zone_Territoire", zone_list)
        if selected_terr != "Toutes":
            df = df[df['Zone_Territoire'] == selected_terr]

        # ===================== DOTATION =====================
        # -----------------------------
        # Liste des numéros MASTER
        # -----------------------------
        master_numbers = set()
        if exclusion_master is not None and 'NUM' in exclusion_master.columns:
            master_numbers = set(
                exclusion_master['NUM']
                .apply(clean_phone)
                .astype(str)
            )

        # -----------------------------
        # Liste des numéros CAISSE
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
        # - 1 seule transaction MASTER (la première)
        # - 1 seule transaction CAISSE (la première)
        # - max 2 transactions au total
        # ===================================================
        for (date, nom), group in dotation_trans.groupby(['Date_only', 'Nom_Ccial']):

            g = group.sort_values("Date")

            # première transaction MASTER
            first_master = g[
                g["Source_Type"] == "MASTER"
            ].head(1)

            # première transaction CAISSE
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

        # sécurité si vide
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

        def compute_perf(group):
            valid_trans = group[
                (~group['To_clean'].isin(global_excluded)) &
                (group['To_clean'].notna())
            ]

            filtered = valid_trans[valid_trans['Amount'] >= 10000]

            first_date = filtered['Date'].min()
            if pd.notna(first_date):
                premiere = first_date.strftime('%H:%M')
            else:
                premiere = 'N/A'

            last_date = filtered['Date'].max()
            if pd.notna(last_date):
                derniere = last_date.strftime('%H:%M')
            else:
                derniere = 'N/A'

            return pd.Series({
                # 'Date_only': group['Date_only'].iloc[0],
                'Zone_Territoire': group['Zone_Territoire'].iloc[0],
                'Zone_SA': group['Zone_SA'].iloc[0],
                'Premiere_Trans': premiere,
                'Derniere_Trans': derniere,
                'Nb_Transactions': filtered['Amount'].count()
            })

        perf = trans_df.groupby(['Date_only', 'Nom_Ccial']).apply(compute_perf).reset_index()

        perf = perf.merge(dotation_group, on=['Date_only', 'Nom_Ccial'], how='left')
        perf['Montant_Dotation'] = perf['Montant_Dotation'].fillna(0)
        perf['Heure_Dotation'] = perf['Heure_Dotation'].fillna('N/A')
        perf[['Zone_Territoire', 'Zone_SA']] = perf[
            ['Zone_Territoire', 'Zone_SA']
        ].fillna("NON RENSEIGNÉ")


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

        hvc_list = []

        if master_df is not None and "Segment Group" in master_df.columns:
            hvc_list = master_df[
                master_df["Segment Group"]
                .astype(str)
                .str.strip() == "1-HVC"
            ]["MSISDN"].astype(str).str.strip().tolist()


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
        # LISTE HVC
        # =========================================================
        hvc_list = []

        if master_df is not None and "Segment Group" in master_df.columns:
            hvc_list = (
                master_df[
                    master_df["Segment Group"]
                    .astype(str)
                    .str.strip() == "1-HVC"
                ]["MSISDN"]
                .astype(str)
                .str.strip()
                .tolist()
            )

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
        # FD + SERVE + COUNT CALCULÉS ENSEMBLE
        # =========================================================
        def compute_segment_metrics(group):
            return pd.Series({
                "FD": group["Amount"].sum(),
                "Serve": group["To_clean"].nunique(),
                "Nb_Trans": len(group)
            })

        segment_group = (
            base_trans
            .groupby(["Date_only", "Nom_Ccial", "Segment"])
            .apply(compute_segment_metrics)
            .reset_index()
        )

        # =========================================================
        # PIVOT POUR AVOIR HVC / OTHER EN COLONNES
        # =========================================================
        pivot_df = segment_group.pivot_table(
            index=["Date_only", "Nom_Ccial"],
            columns="Segment",
            values=["FD", "Serve", "Nb_Trans"],
            fill_value=0
        )

        pivot_df.columns = [
            f"{col[0]}_{col[1]}" for col in pivot_df.columns
        ]

        pivot_df = pivot_df.reset_index()

        # =========================================================
        # RENOMMAGE FINAL
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
        # MERGE AVEC PERF
        # =========================================================
        perf = perf.merge(
            pivot_df,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        # =========================================================
        # REMPLISSAGE FINAL (SAFE)
        # =========================================================
        cols_fill = [
            "FD_HVC", "HVC_Serve", "Nb_Trans_HVC",
            "FD_Others", "Other_Serve", "Nb_Trans_Other"
        ]

        for col in cols_fill:
            perf[col] = perf[col].fillna(0)

        # =========================================================
        # COLONNES GLOBALES
        # =========================================================
        perf["Σ_FD"] = perf["FD_HVC"] + perf["FD_Others"]
        perf["Σ_POS_Serve"] = perf["HVC_Serve"] + perf["Other_Serve"]

        # =========================================================
        # 🔍 CONTROLE QUALITÉ (CRITIQUE)
        # =========================================================
        anomaly = perf[
            ((perf["FD_HVC"] == 0) & (perf["HVC_Serve"] > 0)) |
            ((perf["FD_HVC"] > 0) & (perf["HVC_Serve"] == 0))
        ]

        if not anomaly.empty:
            st.error("🚨 Incohérence détectée entre FD_HVC et HVC_Serve")
            st.dataframe(anomaly, height=300)

        # =========================================================
        # TREND [14H → 17H] VERSION CORRECTE
        # =========================================================

        df["Hour"] = df["Date"].dt.hour

        trend_base = df[
            (df["Type"] == "Transfer") &
            (df["Amount"] >= 10000) &
            (~df["To_clean"].isin(global_excluded)) &
            (df["To_clean"].notna())
        ].copy()

        # période 14h → 17h
        trend_14_17 = trend_base[
            (trend_base["Hour"] >= 14) &
            (trend_base["Hour"] < 17)
        ].copy()

        # période 6h → 13h
        trend_6_14 = trend_base[
            (trend_base["Hour"] >= 6) &
            (trend_base["Hour"] <= 13)
        ].copy()


        # =========================================================
        # POS SERVE UNIQUES (14h → 17h)
        # =========================================================

        pos_group = trend_14_17.groupby(
            ["Nom_Ccial"]
        ).agg(
            POS_serve=("To_clean", "nunique")
        ).reset_index()


        # =========================================================
        # NEW CLIENTS UNIQUES
        # client vu à 14h-17h mais pas vu entre 6h-13h
        # =========================================================

        morning_clients = set(
            trend_6_14["To_clean"].dropna().unique()
        )

        new_clients = trend_14_17[
            ~trend_14_17["To_clean"].isin(morning_clients)
        ].copy()

        new_group = new_clients.groupby(
            ["Nom_Ccial"]
        ).agg(
            New=("To_clean", "nunique")
        ).reset_index()


        # =========================================================
        # MERGE TREND FINAL
        # =========================================================

        trend_final = pos_group.merge(
            new_group,
            on="Nom_Ccial",
            how="left"
        )

        perf = perf.merge(
            trend_final,
            on="Nom_Ccial",
            how="left"
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


        # conversion heures → minutes
        perf["Heure_Dotation_min"] = perf["Heure_Dotation"].apply(time_to_minutes)
        perf["Premiere_Trans_min"] = perf["Premiere_Trans"].apply(time_to_minutes)
        perf["Derniere_Trans_min"] = perf["Derniere_Trans"].apply(time_to_minutes)

        # conversion valeurs numériques
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
            "POS_serve",
            "New"
        ]

        for col in numeric_cols:
            perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0)

        # nombre de jours réellement filtrés
        nb_days = perf["Date_only"].nunique()

        if nb_days == 0:
            nb_days = 1

        required_tours_period = 3 * nb_days
        perf["Nb_Jours"] = perf["Date_only"]

        # consolidation finale
        perf_final = perf.groupby(
            ["Nom_Ccial", "Zone_SA", "Zone_Territoire"],
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

            "POS_serve": "max",
            "New": "max"
        })

        # retour format heure
        perf_final["Heure_Dotation"] = perf_final["Heure_Dotation_min"].apply(minutes_to_time)
        perf_final["Premiere_Trans"] = perf_final["Premiere_Trans_min"].apply(minutes_to_time)
        perf_final["Derniere_Trans"] = perf_final["Derniere_Trans_min"].apply(minutes_to_time)
        perf_final = perf_final.rename(columns={
            "Date": "Nb_Jours"
        })

        # recalcul TR sur la période complète
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
        perf_final['POS_serve'] = pd.to_numeric(perf_final['POS_serve'], errors='coerce').fillna(0).astype(int)
        perf_final['New'] = pd.to_numeric(perf_final['New'], errors='coerce').fillna(0).astype(int)
        perf_final['HVC_Serve'] = pd.to_numeric(perf_final['HVC_Serve'], errors='coerce').fillna(0).astype(int)
        perf_final['Other_Serve'] = pd.to_numeric(perf_final['Other_Serve'], errors='coerce').fillna(0).astype(int)
        perf_final['Σ_POS_Serve'] = pd.to_numeric(perf_final['Σ_POS_Serve'], errors='coerce').fillna(0).astype(int)
        # perf = perf.rename(columns={'Date_only': 'Date'})

        # dataset principal devient le consolidé
        perf = perf_final.copy()
        
        # ===================== AFFICHAGE =====================
        # ===================== MULTI-INDEX HEADER =====================
        columns = pd.MultiIndex.from_tuples([
            ("", "Nb_Jours"),
            ("", "Zone_SA"),
            ("", "Nom_Ccial"),

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

            ("TREND [14H→17H]", "POS_serve"),
            ("TREND [14H→17H]", "New")
        ])

        def highlight_dotation(row):
            val = row[("Transactions (Transfert)", "Σ trx")]

            if pd.notna(val) and val == 0:
                return ['background-color: red'] * len(row)

            return [''] * len(row)
        
        perf_display = perf.copy()

        perf_display = perf_display[[
            'Nb_Jours',
            'Zone_SA', 'Nom_Ccial',
            'Heure_Dotation', 'Montant_Dotation',
            'Premiere_Trans', 'Derniere_Trans', 'Nb_Transactions',
            'FD_HVC', 'HVC_Serve', 'TR_HVC',
            'FD_Others', 'Other_Serve', 'TR_Other',
            'Σ_FD', 'Σ_POS_Serve', 'TR_General',
            'POS_serve', 'New'
        ]]

        perf_display.columns = columns

        st.subheader("Performance Détaillée des Commerciaux")
        styled_df = style_perf(perf_display)
        styled_df = styled_df.apply(highlight_dotation, axis=1)

        st.dataframe(styled_df, use_container_width=True, height=650)

        # ===================== TOP 10 COMMERCIAUX =====================
        st.subheader("🏆 Top 10 Commerciaux")

        # Filtre Zone_SA
        zone_sa_list = ["Toutes"] + sorted(
            perf['Zone_SA'].dropna().astype(str).unique().tolist()
        )

        selected_zone_sa = st.selectbox(
            "Filtrer par Zone_SA",
            zone_sa_list,
            key="top10_zone_sa"
        )

        top_perf = perf.copy()

        if selected_zone_sa != "Toutes":
            top_perf = top_perf[top_perf['Zone_SA'] == selected_zone_sa].copy()

        # ===================== PRÉPARATION DES DONNÉES =====================
        # Conversions numériques
        top_perf['TR_General_num'] = (
            top_perf['TR_General'].astype(str).str.replace('%', '', regex=False)
        )
        top_perf['TR_General_num'] = pd.to_numeric(top_perf['TR_General_num'], errors='coerce').fillna(0)

        for col in ['HVC_Serve', 'Σ_FD_raw', 'Σ_POS_Serve', 'Nb_Jours']:
            if col in top_perf.columns:
                top_perf[col] = pd.to_numeric(top_perf[col], errors='coerce').fillna(0)

        # Heure de première transaction (plus tôt = mieux)
        top_perf['Premiere_Trans_min'] = top_perf['Premiere_Trans'].apply(time_to_minutes)
        top_perf['Premiere_Trans_min'] = top_perf['Premiere_Trans_min'].fillna(9999)

        # ===================== SCORE COMPOSITE ÉQUILIBRÉ =====================
        # Normalisation + pondération (tu peux ajuster les poids)

        # Normalisation (Min-Max) pour chaque critère
        def normalize(series):
            min_val = series.min()
            max_val = series.max()
            return (series - min_val) / (max_val - min_val + 1e-8) if max_val > min_val else 0

        top_perf['Score_Jours'] = normalize(top_perf['Nb_Jours'])
        top_perf['Score_HVC'] = normalize(top_perf['HVC_Serve'])
        top_perf['Score_TR'] = normalize(top_perf['TR_General_num'])
        top_perf['Score_FD'] = normalize(top_perf['Σ_FD_raw'])
        top_perf['Score_Premiere'] = 1 - normalize(top_perf['Premiere_Trans_min'])  # inversé (plus tôt = mieux)

        # Score final pondéré (ajuste les % selon ton besoin)
        top_perf['Score_Final'] = (
            top_perf['Score_Jours'] * 0.20 +    # 20% jours de travail
            top_perf['Score_HVC'] * 0.20 +      # 20% HVC_Serve
            top_perf['Score_TR'] * 0.20 +       # 20% Taux de Rotation
            top_perf['Score_FD'] * 0.20 +       # 20% Volume d'affaires
            top_perf['Score_Premiere'] * 0.20   # 20% Commence tôt
        )

        # ===================== TRI & CLASSEMENT =====================
        top_perf = top_perf.sort_values(
            by='Score_Final',
            ascending=False
        ).head(10).copy()

        # Ajout du rang et médailles
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
            'Zone_SA',
            'Nb_Jours',
            'Premiere_Trans',
            'HVC_Serve',
            'Σ_FD',
            'Σ_POS_Serve',
            'TR_General',
            'Score_Final'
        ]].rename(columns={
            'Classement': '🏅 Rang',
            'Nom_Ccial': 'Commercial',
            'Zone_SA': 'Zone_SA',
            'Nb_Jours': 'Nb_Jours',
            'Premiere_Trans': 'Première Transaction',
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

        if st.button("📸 Capturer tableau en images (20 lignes par image)"):

            # dossier temporaire
            export_folder = "exports_perf"
            os.makedirs(export_folder, exist_ok=True)

            # supprimer anciens fichiers
            for file in os.listdir(export_folder):
                file_path = os.path.join(export_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            # nombre de lignes par image
            chunk_size = 20

            # ici on utilise perf_display (pas styled_df)
            total_rows = len(perf_display)

            for i in range(0, total_rows, chunk_size):
                chunk = perf_display.iloc[i:i + chunk_size].copy()

                # réappliquer le style sur chaque bloc
                styled_chunk = style_perf(chunk)

                file_name = f"performance_part_{(i // chunk_size) + 1}.png"
                file_path = os.path.join(export_folder, file_name)

                dfi.export(
                    styled_chunk,
                    file_path,
                    table_conversion="chrome"
                )

            # créer zip
            zip_path = os.path.join(export_folder, "Performance_Commerciaux.zip")

            with zipfile.ZipFile(zip_path, "w") as zipf:
                for file in os.listdir(export_folder):
                    if file.endswith(".png"):
                        zipf.write(
                            os.path.join(export_folder, file),
                            arcname=file
                        )

            # bouton téléchargement ZIP
            with open(zip_path, "rb") as f:
                st.download_button(
                    "📥 Télécharger toutes les captures (ZIP)",
                    f,
                    "Performance_Commerciaux.zip",
                    "application/zip"
                )
        col1, col2 = st.columns(2)
        excel_data = to_excel(perf)
        col1.download_button("Télécharger Excel", excel_data, "Performance_Commerciaux.xlsx")
        col2.download_button("Télécharger CSV", perf.to_csv(index=False).encode('utf-8'), "Performance_Commerciaux.csv")