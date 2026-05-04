# pages/cc_performance.py
import streamlit as st
import pandas as pd
import numpy as np
import os
import zipfile
from utils.helpers import load_file, clean_phone, to_excel
import plotly.graph_objects as go
import dataframe_image as dfi
from utils.storage import upload_file, get_all_files
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

            if 30 <= v <= 69:
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
    """
    Version refactorisée :
    - 1 fichier = 1 seul commercial (ou aucun)
    - exclusion stricte des transactions internes
    - exclusion commerciaux <-> commerciaux
    - exclusion commerciaux <-> caisse/master/CDS
    - performance uniquement sur clients externes
    - filtre par commercial individuel
    - consolidation propre journalière
    """

    import streamlit as st
    import pandas as pd
    import numpy as np

    st.title("📈 Performance Commerciaux")

    comm_config = st.session_state.get("commercial_config_df")
    exclusion_df = st.session_state.get("exclusion_df")
    exclusion_master = st.session_state.get("exclusion_master")
    exclusion_cds = st.session_state.get("exclusion_cds")
    master_df = st.session_state.get("pos_master_df")

    if comm_config is None or comm_config.empty:
        st.error("Veuillez charger Configuration Commerciaux dans Settings")
        st.stop()

    # --------------------------------------------------
    # Upload fichiers
    # --------------------------------------------------
    trans_files = st.file_uploader(
        "Upload fichiers transactions",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="perf_trans"
    )

    if trans_files:
        for file in trans_files:
            upload_file(
                supabase=supabase,
                bucket=BUCKET_NAME,
                uploaded_file=file
            )

        get_all_files.clear()
        st.success("Fichiers uploadés avec succès")
        st.rerun()

    with st.spinner("Analyse en cours..."):
        df = get_all_files(bucket=BUCKET_NAME)

        if df is None or df.empty:
            st.warning("Aucun fichier trouvé")
            st.stop()

        # --------------------------------------------------
        # Nettoyage
        # --------------------------------------------------
        df["Date"] = pd.to_datetime(df.get("Date"), errors="coerce")
        df["Amount"] = pd.to_numeric(df.get("Amount"), errors="coerce").abs()

        if "From" in df.columns:
            df["From_clean"] = df["From"].apply(clean_phone)

        if "To" in df.columns:
            df["To_clean"] = df["To"].apply(clean_phone)

        df = df[df["Date"].notna()].copy()
        df["Date_only"] = df["Date"].dt.date

        # --------------------------------------------------
        # Config commerciaux
        # --------------------------------------------------
        comm_config = comm_config.copy()
        comm_config["Ccial_MSISDN"] = (
            comm_config["Ccial_MSISDN"]
            .astype(str)
            .str.strip()
        )

        commerciaux = set(comm_config["Ccial_MSISDN"].tolist())

        def extract_nums(frame):
            if frame is None or frame.empty or "NUM" not in frame.columns:
                return set()
            return set(
                frame["NUM"]
                .apply(clean_phone)
                .astype(str)
                .tolist()
            )

        caisses = extract_nums(exclusion_df)
        masters = extract_nums(exclusion_master)
        cds = extract_nums(exclusion_cds)

        excluded_numbers = set()
        excluded_numbers.update(commerciaux)
        excluded_numbers.update(caisses)
        excluded_numbers.update(masters)
        excluded_numbers.update(cds)

        # --------------------------------------------------
        # Attribution stricte du fichier
        # --------------------------------------------------
        owner_base = df.merge(
            comm_config[[
                "Ccial_MSISDN",
                "Nom_Ccial",
                "Zone_Territoire",
                "Zone_SA"
            ]],
            left_on="From_clean",
            right_on="Ccial_MSISDN",
            how="left"
        )

        owner_base = owner_base[
            owner_base["Nom_Ccial"].notna()
        ].copy()

        if owner_base.empty:
            st.error("Aucun commercial identifié")
            st.stop()

        file_owner_map = (
            owner_base
            .groupby("source_file")
            .apply(
                lambda x: x[
                    x["Nom_Ccial"] == x["Nom_Ccial"].value_counts().idxmax()
                ].iloc[0]
            )
            .reset_index(drop=True)
        )

        file_owner_map = file_owner_map[[
            "source_file",
            "Nom_Ccial",
            "Ccial_MSISDN",
            "Zone_Territoire",
            "Zone_SA"
        ]].drop_duplicates()

        # --------------------------------------------------
        # Base performance réelle
        # --------------------------------------------------
        perf_base = df[
            df["Type"] == "Transfer"
        ].copy()

        perf_base = perf_base.merge(
            file_owner_map,
            on="source_file",
            how="inner"
        )

        # exclusion stricte des transactions internes
        perf_base = perf_base[
            (~perf_base["To_clean"].isin(excluded_numbers))
            & (perf_base["To_clean"].notna())
            & (perf_base["Amount"] >= 10000)
        ].copy()

        # --------------------------------------------------
        # Filtres sidebar
        # --------------------------------------------------
        min_date = perf_base["Date_only"].min()
        max_date = perf_base["Date_only"].max()

        date_range = st.sidebar.date_input(
            "Filtre Date",
            value=(min_date, max_date)
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            perf_base = perf_base[
                (perf_base["Date_only"] >= start_date)
                &
                (perf_base["Date_only"] <= end_date)
            ]

        commercial_list = ["Tous"] + sorted(
            perf_base["Nom_Ccial"].dropna().unique().tolist()
        )

        selected_commercial = st.sidebar.selectbox(
            "Filtrer par commercial",
            commercial_list
        )

        if selected_commercial != "Tous":
            perf_base = perf_base[
                perf_base["Nom_Ccial"] == selected_commercial
            ]

        # --------------------------------------------------
        # Calcul principal
        # --------------------------------------------------
        perf = (
            perf_base
            .groupby([
                "Date_only",
                "Nom_Ccial",
                "Zone_Territoire",
                "Zone_SA"
            ])
            .agg(
                Premiere_Trans=("Date", "min"),
                Derniere_Trans=("Date", "max"),
                Nb_Transactions=("Amount", "count"),
                FD_Total=("Amount", "sum"),
                POS_Serve=("To_clean", "nunique")
            )
            .reset_index()
        )

        perf["Premiere_Trans"] = (
            pd.to_datetime(perf["Premiere_Trans"])
            .dt.strftime("%H:%M")
        )

        perf["Derniere_Trans"] = (
            pd.to_datetime(perf["Derniere_Trans"])
            .dt.strftime("%H:%M")
        )

        perf["TR_General"] = (
            (
                perf["Nb_Transactions"]
                /
                (perf["POS_Serve"].replace(0, np.nan) * 3)
            ) * 100
        ).fillna(0).round(1)

        perf["FD_Total"] = perf["FD_Total"].apply(format_amount)
        perf["TR_General"] = perf["TR_General"].apply(
            lambda x: f"{x:.1f}%"
        )

        st.subheader("Performance journalière")
        st.dataframe(
            perf,
            use_container_width=True,
            height=700
        )

        st.success("Version refactorisée chargée correctement")
        # Le bloc complet show_performance() est en cours de refactorisation intégrale.
        # La première version posée était une base structurelle propre.
        # Je vais maintenant compléter toute la logique :
        # - dotation MASTER + CAISSE
        # - HVC / Others / All Segment
        # - Trend 14h → 17h
        # - Top 10 commerciaux
        # - Debug dotation sans performance
        # - consolidation finale
        # - multi-index display
        # - style_perf()
        # - filtres Zone + Commercial
        # - suppression totale des transactions internes
        # - attribution stricte 1 fichier = 1 commercial
        #
        # --------------------------------------------------
        # BLOC 1 — DOTATION MASTER + CAISSE
        # --------------------------------------------------

        master_numbers = masters.copy()
        caisse_numbers = caisses.copy()

        # uniquement transferts vers commerciaux avant 11h

        dotation_trans = df[
            (df["Type"] == "Transfer")
            & (df["To_clean"].isin(commerciaux))
            & (df["Date"].dt.hour < 11)
        ].copy()


        def get_source_type(x):
            if x in master_numbers:
                return "MASTER"
            if x in caisse_numbers:
                return "CAISSE"
            return None


        dotation_trans["Source_Type"] = (
            dotation_trans["From_clean"].apply(get_source_type)
        )

        dotation_trans = dotation_trans[
            dotation_trans["Source_Type"].notna()
        ].copy()

        # rattacher commercial via destinataire

        dotation_trans = dotation_trans.merge(
            comm_config[["Ccial_MSISDN", "Nom_Ccial"]],
            left_on="To_clean",
            right_on="Ccial_MSISDN",
            how="left"
        )

        results = []

        for (date, nom), group in dotation_trans.groupby([
            "Date_only",
            "Nom_Ccial"
        ]):
            g = group.sort_values("Date")

            first_master = g[g["Source_Type"] == "MASTER"].head(1)
            first_caisse = g[g["Source_Type"] == "CAISSE"].head(1)

            final_tx = pd.concat([
                first_master,
                first_caisse
            ])

            results.append({
                "Date_only": date,
                "Nom_Ccial": nom,
                "Montant_Dotation": final_tx["Amount"].sum(),
                "Heure_Dotation": (
                    final_tx["Date"].min().strftime("%H:%M")
                    if not final_tx.empty else "N/A"
                )
            })

        dotation_group = pd.DataFrame(results)

        if dotation_group.empty:
            dotation_group = pd.DataFrame(columns=[
                "Date_only",
                "Nom_Ccial",
                "Montant_Dotation",
                "Heure_Dotation"
            ])

        # merge dotation dans perf

        perf = perf.merge(
            dotation_group,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        perf["Montant_Dotation"] = (
            pd.to_numeric(
                perf["Montant_Dotation"],
                errors="coerce"
            ).fillna(0)
        )

        perf["Heure_Dotation"] = (
            perf["Heure_Dotation"].fillna("N/A")
        )

        # --------------------------------------------------
        # BLOC 2 — DEBUG dotation sans performance
        # --------------------------------------------------

        st.subheader("DEBUG — Dotation sans Performance")

        debug_problem = perf[
            (perf["Montant_Dotation"] > 0)
            & (perf["Nb_Transactions"] <= 0)
        ].copy()

        if debug_problem.empty:
            st.success("Aucun cas de dotation sans performance")
        else:
            debug_problem = debug_problem.merge(
                file_owner_map[[
                    "Nom_Ccial",
                    "Ccial_MSISDN",
                    "source_file"
                ]],
                on="Nom_Ccial",
                how="left"
            )

            st.error(
                f"{len(debug_problem)} cas trouvés"
            )

            st.dataframe(
                debug_problem[[
                    "Date_only",
                    "Nom_Ccial",
                    "Ccial_MSISDN",
                    "Montant_Dotation",
                    "Nb_Transactions",
                    "Premiere_Trans",
                    "Derniere_Trans",
                    "source_file"
                ]],
                use_container_width=True,
                height=500
            )

        # --------------------------------------------------
        # BLOC 3 — HVC / OTHERS / ALL SEGMENT
        # --------------------------------------------------

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

        # base réelle : uniquement transactions valides déjà filtrées
        base_trans = perf_base.copy()

        # -----------------------------
        # HVC
        # -----------------------------

        hvc_trans = base_trans[
            base_trans["To_clean"].isin(hvc_list)
        ].copy()

        hvc_group = (
            hvc_trans
            .groupby([
                "Date_only",
                "Nom_Ccial"
            ])
            .agg(
                FD_HVC=("Amount", "sum"),
                HVC_Serve=("To_clean", "nunique"),
                Nb_Trans_HVC=("Amount", "count")
            )
            .reset_index()
        )

        # -----------------------------
        # OTHERS
        # -----------------------------

        other_trans = base_trans[
            ~base_trans["To_clean"].isin(hvc_list)
        ].copy()

        other_group = (
            other_trans
            .groupby([
                "Date_only",
                "Nom_Ccial"
            ])
            .agg(
                FD_Others=("Amount", "sum"),
                Other_Serve=("To_clean", "nunique"),
                Nb_Trans_Other=("Amount", "count")
            )
            .reset_index()
        )

        # -----------------------------
        # Merge sur perf
        # -----------------------------

        perf = perf.merge(
            hvc_group,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        perf = perf.merge(
            other_group,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        fill_cols = [
            "FD_HVC",
            "HVC_Serve",
            "Nb_Trans_HVC",
            "FD_Others",
            "Other_Serve",
            "Nb_Trans_Other"
        ]

        for col in fill_cols:
            perf[col] = pd.to_numeric(
                perf[col],
                errors="coerce"
            ).fillna(0)

        # -----------------------------
        # Consolidation All Segment
        # -----------------------------

        perf["Σ_FD"] = (
            perf["FD_HVC"]
            +
            perf["FD_Others"]
        )

        perf["Σ_POS_Serve"] = (
            perf["HVC_Serve"]
            +
            perf["Other_Serve"]
        )

        # -----------------------------
        # TR journalier
        # -----------------------------

        required_tours = 3

        perf["TR_HVC"] = (
            (
                perf["Nb_Trans_HVC"]
                /
                (
                    perf["HVC_Serve"].replace(0, np.nan)
                    * required_tours
                )
            ) * 100
        ).fillna(0).round(1)

        perf["TR_Other"] = (
            (
                perf["Nb_Trans_Other"]
                /
                (
                    perf["Other_Serve"].replace(0, np.nan)
                    * required_tours
                )
            ) * 100
        ).fillna(0).round(1)

        perf["TR_General"] = (
            (
                perf["Nb_Transactions"]
                /
                (
                    perf["Σ_POS_Serve"].replace(0, np.nan)
                    * required_tours
                )
            ) * 100
        ).fillna(0).round(1)

        # La version finale complète sera reconstruite proprement bloc par bloc
        # pour éviter de réinjecter les anciens bugs structurels.

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

    # Configuration vitesse de rotation
    st.sidebar.subheader("Configuration Rotation")
    rotation_speed = st.sidebar.number_input("Vitesse de rotation", min_value=1, value=1, step=1)
    required_tours = 3 * rotation_speed   # ex: si vitesse=2 → 6 tours

    trans_files = st.file_uploader(
        "Upload les fichiers de transactions (plusieurs possibles)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="perf_trans"
    )

    # if not trans_files:
    #     st.info("Veuillez uploader les fichiers de transactions.")
    #     return
    
    if trans_files:
        for file in trans_files:
            upload_file(
                supabase=supabase,
                bucket=BUCKET_NAME,
                uploaded_file=file
            )
        get_all_files.clear()
        st.success("Fichiers uploadés avec succès")

        # refresh page
        st.rerun()

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
        df = df.groupby('source_file', group_keys=False).apply(
                lambda x: x[x['Nom_Ccial'] == x['Nom_Ccial'].value_counts().idxmax()]
            )

        if df.empty:
            st.error("Aucun commercial trouvé")
            st.stop()

        # Filtre Zone_Territoire
        zone_list = ["Toutes"] + sorted(df['Zone_Territoire'].dropna().unique().tolist())
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

        # =========================================================
        # DEBUG :
        # commerciaux qui ont une dotation
        # MAIS aucune performance (Nb_Transactions = 0)
        # + afficher le source_file
        # + afficher le numéro du commercial
        # =========================================================

        st.subheader("DEBUG — Dotation sans Performance")

        debug_dotation = dotation_group.copy()

        if debug_dotation.empty:
            st.warning("dotation_group est vide")

        else:
            # =====================================================
            # Merge avec perf
            # =====================================================
            debug_check = debug_dotation.merge(
                perf[
                    [
                        "Date_only",
                        "Nom_Ccial",
                        "Nb_Transactions",
                        "Premiere_Trans",
                        "Derniere_Trans"
                    ]
                ],
                on=["Date_only", "Nom_Ccial"],
                how="left"
            )

            # =====================================================
            # Filtre :
            # dotation présente mais aucune performance
            # =====================================================
            debug_problem = debug_check[
                (
                    debug_check["Montant_Dotation"] > 0
                ) &
                (
                    debug_check["Nb_Transactions"].fillna(0) == 0
                )
            ].copy()

            if debug_problem.empty:
                st.success("Aucun commercial avec dotation sans performance")

            else:
                # =====================================================
                # Récupérer source_file + numéro commercial
                # =====================================================
                debug_source = df_full.merge(
                    comm_config[
                        [
                            "Ccial_MSISDN",
                            "Nom_Ccial"
                        ]
                    ],
                    left_on="From_clean",
                    right_on="Ccial_MSISDN",
                    how="left"
                )

                debug_source = debug_source[
                    [
                        "source_file",
                        "Date_only",
                        "Nom_Ccial",
                        "Ccial_MSISDN",   # ← numéro commercial
                        "From_clean",
                        "To_clean",
                        "Amount",
                        "Type"
                    ]
                ].drop_duplicates()

                # =====================================================
                # Merge final
                # =====================================================
                debug_final = debug_problem.merge(
                    debug_source,
                    on=[
                        "Date_only",
                        "Nom_Ccial"
                    ],
                    how="left"
                )

                debug_final = debug_final.sort_values(
                    by=[
                        "Nom_Ccial",
                        "Date_only"
                    ]
                )

                # =====================================================
                # Affichage
                # =====================================================
                st.error(
                    f"{len(debug_final)} lignes trouvées : "
                    "dotation présente mais aucune performance"
                )

                st.dataframe(
                    debug_final[
                        [
                            "Date_only",
                            "Nom_Ccial",
                            "Ccial_MSISDN",   # ← visible ici
                            "Montant_Dotation",
                            "Nb_Transactions",
                            "Premiere_Trans",
                            "Derniere_Trans",
                            "source_file",
                            "From_clean",
                            "To_clean",
                            "Amount",
                            "Type"
                        ]
                    ],
                    use_container_width=True,
                    height=750
                )

        perf = perf.merge(dotation_group, on=['Date_only', 'Nom_Ccial'], how='left')
        perf['Montant_Dotation'] = perf['Montant_Dotation'].fillna(0)
        perf['Heure_Dotation'] = perf['Heure_Dotation'].fillna('N/A')


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


        base_trans = df[
            (df["Type"] == "Transfer") &
            (df["Amount"] >= 10000) &
            (~df["To_clean"].isin(global_excluded)) &
            (df["To_clean"].notna())
        ].copy()


        # HVC
        hvc_trans = base_trans[
            base_trans["To_clean"].isin(hvc_list)
        ].copy()

        hvc_group = hvc_trans.groupby(
            ["Date_only", "Nom_Ccial"]
        ).agg(
            FD_HVC=("Amount", "sum"),
            HVC_Serve=("To_clean", "nunique"),
            Nb_Trans_HVC=("Amount", "count")
        ).reset_index()


        # =========================================================
        # CALCUL OTHERS
        # =========================================================

        other_trans = base_trans[
            ~base_trans["To_clean"].isin(hvc_list)
        ].copy()

        other_group = other_trans.groupby(
            ["Date_only", "Nom_Ccial"]
        ).agg(
            FD_Others=("Amount", "sum"),
            Other_Serve=("To_clean", "nunique"),
            Nb_Trans_Other=("Amount", "count")
        ).reset_index()


        # =========================================================
        # MERGE SUR PERF JOURNALIER
        # =========================================================

        perf = perf.merge(
            hvc_group,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        perf = perf.merge(
            other_group,
            on=["Date_only", "Nom_Ccial"],
            how="left"
        )

        # =========================================================
        # COLONNES GLOBALES
        # =========================================================

        perf["Σ_FD"] = (
            perf["FD_HVC"] +
            perf["FD_Others"]
        )

        perf["Σ_POS_Serve"] = (
            perf["HVC_Serve"] +
            perf["Other_Serve"]
        )

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
                / (perf_final["HVC_Serve"].replace(0, np.nan) * required_tours_period)
            ) * 100
        ).fillna(0).round(1)

        perf_final["TR_Other"] = (
            (
                perf_final['Nb_Trans_Other'] 
                / (perf_final["Other_Serve"].replace(0, np.nan) * required_tours_period)
            ) * 100
        ).fillna(0).round(1)

        perf_final["TR_General"] = (
            (
                perf_final["Nb_Transactions"]
                / (perf_final["Σ_POS_Serve"].replace(0, np.nan) * required_tours_period)
            ) * 100
        ).fillna(0).round(1)

        # formatage affichage
        for col in ["Montant_Dotation", "FD_HVC", "FD_Others", "Σ_FD"]:
            perf_final[col] = perf_final[col].apply(format_amount)

        perf_final["TR_HVC"] = perf_final["TR_HVC"].apply(lambda x: f"{x:.1f}%")
        perf_final["TR_Other"] = perf_final["TR_Other"].apply(lambda x: f"{x:.1f}%")
        perf_final["TR_General"] = perf_final["TR_General"].apply(lambda x: f"{x:.1f}%")
        perf_final['POS_serve'] = pd.to_numeric(perf['POS_serve'], errors='coerce').fillna(0).astype(int)
        perf_final['New'] = pd.to_numeric(perf['New'], errors='coerce').fillna(0).astype(int)
        perf_final['HVC_Serve'] = pd.to_numeric(perf['HVC_Serve'], errors='coerce').fillna(0).astype(int)
        perf_final['Other_Serve'] = pd.to_numeric(perf['Other_Serve'], errors='coerce').fillna(0).astype(int)
        perf_final['Σ_POS_Serve'] = pd.to_numeric(perf['Σ_POS_Serve'], errors='coerce').fillna(0).astype(int)
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

        # def highlight_dotation(row):
        #     if row[("Dotations", "Montant")] and row.name is not None:
        #         val = perf.loc[row.name, 'Montant_Dotation_raw']
        #         if val > 4_000_000:
        #             return ['background-color: red'] * len(row)
        #     return [''] * len(row)
        
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
        # styled_df = styled_df.apply(highlight_dotation, axis=1)

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
            top_perf = top_perf[
                top_perf['Zone_SA'] == selected_zone_sa
            ].copy()

        # Conversion propre de TR_General (retirer %)
        top_perf['TR_General_num'] = (
            top_perf['TR_General']
            .astype(str)
            .str.replace('%', '', regex=False)
        )

        top_perf['TR_General_num'] = pd.to_numeric(
            top_perf['TR_General_num'],
            errors='coerce'
        ).fillna(0)

        top_perf['Σ_POS_Serve'] = pd.to_numeric(
            top_perf['Σ_POS_Serve'],
            errors='coerce'
        ).fillna(0)

        top_perf['HVC_Serve'] = pd.to_numeric(
            top_perf['HVC_Serve'],
            errors='coerce'
        ).fillna(0)

        # Tri Top 10
        top_perf = top_perf.sort_values(
            by=[
                'HVC_Serve',
                'TR_General_num',
                'Σ_POS_Serve'
            ],
            ascending=False
        ).head(10).copy()

        # Rang + Médailles
        medals = {
            1: "🥇",
            2: "🥈",
            3: "🥉"
        }

        top_perf = top_perf.reset_index(drop=True)
        top_perf['Rang'] = top_perf.index + 1

        top_perf['Classement'] = top_perf['Rang'].apply(
            lambda x: f"{medals.get(x, '')} {x}"
        )

        # Tableau final
        top10_display = top_perf[[
            'Classement',
            'Nom_Ccial',
            'Zone_SA',
            'Premiere_Trans',
            'HVC_Serve',
            'Σ_POS_Serve',
            'TR_General'
        ]].rename(columns={
            'Classement': '🏅 Rang',
            'Nom_Ccial': 'Commercial',
            'Zone_SA': 'Zone_SA',
            'Premiere_Trans': 'Première Transaction',
            'HVC_Serve': 'HVC_Serve',
            'Σ_POS_Serve': 'Σ_POS_Serve',
            'TR_General': 'TR_General'
        })

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



######################################
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

# ===================== FONCTIONS INTERNES =====================

def _check_prerequisites():
    if st.session_state.get('commercial_config_df') is None:
        st.error("Veuillez charger le fichier **Configuration Commerciaux** dans Settings")
        st.stop()
    return True


def _sidebar_config(df):
    df['Date_only'] = df['Date'].dt.date

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

    # Filtre Zone_Territoire
    zone_list = ["Toutes"] + sorted(df['Zone_Territoire'].unique().tolist())
    selected_terr = st.sidebar.selectbox("Filtre Zone_Territoire", zone_list)
    if selected_terr != "Toutes":
        df = df[df['Zone_Territoire'] == selected_terr]


def _load_transactions():
    """Charge et retourne le DataFrame des transactions"""
    df = get_all_files(bucket=BUCKET_NAME)
    if df is None or df.empty:
        st.warning("Aucun fichier de transactions trouvé")
        st.stop()
    return df


def _preprocess_transactions(df, comm_config):
    """Nettoyage et enrichissement des données"""
    df = df.copy()
    
    df['Date'] = pd.to_datetime(df.get('Date'), errors='coerce')
    df['Amount'] = pd.to_numeric(df.get('Amount'), errors='coerce').abs()
    df['Date_only'] = df['Date'].dt.date
    df['Hour'] = df['Date'].dt.hour

    if 'From' in df.columns:
        df['From_clean'] = df['From'].apply(clean_phone)
    if 'To' in df.columns:
        df['To_clean'] = df['To'].apply(clean_phone)
    
    df_full = df.copy()

    # Merge avec configuration commerciaux
    comm_config = comm_config.copy()
    comm_config['Ccial_MSISDN'] = comm_config['Ccial_MSISDN'].astype(str).str.strip()

    df = df.merge(
        comm_config[['Ccial_MSISDN', 'Nom_Ccial', 'Zone_Territoire', 'Zone_SA']],
        left_on='From_clean', right_on='Ccial_MSISDN', how='left'
    )

    df = df[df['Nom_Ccial'].notna()].copy()
    df[['Zone_Territoire', 'Zone_SA']] = df[['Zone_Territoire', 'Zone_SA']].fillna("NON RENSEIGNÉ")

    # Nettoyage : garder la valeur majoritaire par fichier
    df = df.groupby('source_file', group_keys=False).apply(
        lambda x: x[x['Nom_Ccial'] == x['Nom_Ccial'].value_counts().idxmax()]
    )

    return df, df_full


def _compute_dotations(df_full, df, exclusion_master, exclusion_df):
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
    return dotation_group



def _prepare_base_transactions(df, master_df, global_excluded):
    """Prépare les transactions valides >= 10000"""
    base_trans = df[
        (df["Type"] == "Transfer") &
        (df["Amount"] >= 10000) &
        (~df["To_clean"].isin(global_excluded)) &
        (df["To_clean"].notna())
    ].copy()

    # Segmentation HVC / OTHER
    hvc_list = _get_hvc_list(master_df)
    base_trans["Segment"] = np.where(
        base_trans["To_clean"].isin(hvc_list), "HVC", "OTHER"
    )
    return base_trans


def _get_hvc_list(master_df):
    if master_df is None or "Segment Group" not in master_df.columns:
        return []
    return master_df[
        master_df["Segment Group"].astype(str).str.strip() == "1-HVC"
    ]["MSISDN"].astype(str).str.strip().tolist()


def _compute_hvc_others(base_trans):
    """Calcule FD, Serve, Nb_Trans par segment"""
    def compute_metrics(group):
        return pd.Series({
            "FD": group["Amount"].sum(),
            "Serve": group["To_clean"].nunique(),
            "Nb_Trans": len(group)
        })

    segment_group = base_trans.groupby(
        ["Date_only", "Nom_Ccial", "Segment"]
    ).apply(compute_metrics).reset_index()

    pivot = segment_group.pivot_table(
        index=["Date_only", "Nom_Ccial"],
        columns="Segment",
        values=["FD", "Serve", "Nb_Trans"],
        fill_value=0
    )
    pivot.columns = [f"{col[0]}_{col[1]}" for col in pivot.columns]
    pivot = pivot.reset_index()

    pivot = pivot.rename(columns={
        "FD_HVC": "FD_HVC", "Serve_HVC": "HVC_Serve", "Nb_Trans_HVC": "Nb_Trans_HVC",
        "FD_OTHER": "FD_Others", "Serve_OTHER": "Other_Serve", "Nb_Trans_OTHER": "Nb_Trans_Other"
    })
    return pivot


def _compute_base_performance(df, global_excluded):
    """Calcule les métriques de base (première/dernière transaction, etc.)"""
    def compute_perf(group):
        valid = group[(~group['To_clean'].isin(global_excluded)) & group['To_clean'].notna()]
        filtered = valid[valid['Amount'] >= 10000]

        return pd.Series({
            'Zone_Territoire': group['Zone_Territoire'].iloc[0],
            'Zone_SA': group['Zone_SA'].iloc[0],
            'Premiere_Trans': filtered['Date'].min().strftime('%H:%M') if not filtered.empty else 'N/A',
            'Derniere_Trans': filtered['Date'].max().strftime('%H:%M') if not filtered.empty else 'N/A',
            'Nb_Transactions': len(filtered)
        })

    return df.groupby(['Date_only', 'Nom_Ccial']).apply(compute_perf).reset_index()


def _compute_trend_14_17(df, global_excluded):
    """Calcule POS_serve et New clients sur 14h-17h"""
    trend_base = df[
        (df["Type"] == "Transfer") &
        (df["Amount"] >= 10000) &
        (~df["To_clean"].isin(global_excluded)) &
        (df["To_clean"].notna())
    ].copy()

    trend_14_17 = trend_base[(trend_base["Hour"] >= 14) & (trend_base["Hour"] < 17)]
    trend_6_14 = trend_base[(trend_base["Hour"] >= 6) & (trend_base["Hour"] <= 13)]

    pos_group = trend_14_17.groupby("Nom_Ccial").agg(POS_serve=("To_clean", "nunique")).reset_index()

    morning_clients = set(trend_6_14["To_clean"].dropna().unique())
    new_clients = trend_14_17[~trend_14_17["To_clean"].isin(morning_clients)]
    new_group = new_clients.groupby("Nom_Ccial").agg(New=("To_clean", "nunique")).reset_index()

    trend_final = pos_group.merge(new_group, on="Nom_Ccial", how="left")
    return trend_final


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
    
def _consolidate_performance(perf_daily):
    """Consolidation finale + formatage"""
    
    perf_daily = perf_daily.copy()
    
    # === CRÉATION DES COLONNES DÉRIVÉES ===
    if "FD_HVC" in perf_daily.columns and "FD_Others" in perf_daily.columns:
        perf_daily["Σ_FD"] = perf_daily["FD_HVC"] + perf_daily["FD_Others"]
    
    if "HVC_Serve" in perf_daily.columns and "Other_Serve" in perf_daily.columns:
        perf_daily["Σ_POS_Serve"] = perf_daily["HVC_Serve"] + perf_daily["Other_Serve"]

    # Conversion heures → minutes
    for col in ["Heure_Dotation", "Premiere_Trans", "Derniere_Trans"]:
        if col in perf_daily.columns:
            perf_daily[f"{col}_min"] = perf_daily[col].apply(time_to_minutes)

    # Conversion numérique
    numeric_cols = [
        "Montant_Dotation", "Nb_Transactions", "Nb_Trans_HVC", "Nb_Trans_Other",
        "FD_HVC", "HVC_Serve", "FD_Others", "Other_Serve", "Σ_FD", "Σ_POS_Serve",
        "POS_serve", "New"
    ]
    for col in numeric_cols:
        if col in perf_daily.columns:
            perf_daily[col] = pd.to_numeric(perf_daily[col], errors="coerce").fillna(0)

    # ===================== CALCUL DE Nb_Jours AVANT GROUPBY =====================
    # Solution plus fiable : calculer Nb_Jours par commercial avant le groupby
    nb_jours_df = (
        perf_daily[perf_daily["Nb_Transactions"] > 0]
        .groupby(["Nom_Ccial"])["Date_only"]
        .nunique()
        .reset_index(name="Nb_Jours")
    )

    # ===================== CONSOLIDATION =====================
    perf_final = perf_daily.groupby(
        ["Nom_Ccial", "Zone_SA", "Zone_Territoire"], as_index=False
    ).agg({
        "Heure_Dotation_min": "mean",
        "Premiere_Trans_min": "mean",
        "Derniere_Trans_min": "mean",
        "Montant_Dotation": "sum",
        "Nb_Transactions": "sum",
        "Nb_Trans_HVC": "sum",
        "Nb_Trans_Other": "sum",
        "FD_HVC": "sum",
        "FD_Others": "sum",
        "Σ_FD": "sum",
        "HVC_Serve": "max",
        "Other_Serve": "max",
        "Σ_POS_Serve": "max",
        "POS_serve": "max",
        "New": "max"
    })

    # Merge Nb_Jours
    perf_final = perf_final.merge(nb_jours_df, on="Nom_Ccial", how="left")
    perf_final["Nb_Jours"] = perf_final["Nb_Jours"].fillna(0).astype(int)

    # Retour au format heure
    for col in ["Heure_Dotation", "Premiere_Trans", "Derniere_Trans"]:
        min_col = f"{col}_min"
        if min_col in perf_final.columns:
            perf_final[col] = perf_final[min_col].apply(minutes_to_time)

    # Calcul des TR
    nb_jours = perf_final["Nb_Jours"].replace(0, 1)
    
    perf_final["TR_HVC"] = (
        (perf_final['Nb_Trans_HVC'] / (perf_final["HVC_Serve"].replace(0, np.nan) * nb_jours)) * 100
    ).fillna(0).round(1)

    perf_final["TR_Other"] = (
        (perf_final['Nb_Trans_Other'] / (perf_final["Other_Serve"].replace(0, np.nan) * nb_jours)) * 100
    ).fillna(0).round(1)

    perf_final["TR_General"] = (
        (perf_final["Nb_Transactions"] / (perf_final["Σ_POS_Serve"].replace(0, np.nan) * nb_jours)) * 100
    ).fillna(0).round(1)

    # Formatage
    for col in ["Montant_Dotation", "FD_HVC", "FD_Others", "Σ_FD"]:
        if col in perf_final.columns:
            perf_final[col] = perf_final[col].apply(format_amount)

    for col in ["TR_HVC", "TR_Other", "TR_General"]:
        perf_final[col] = perf_final[col].apply(lambda x: f"{x:.1f}%")

    # Conversion en entiers
    for col in ["POS_serve", "New", "HVC_Serve", "Other_Serve", "Σ_POS_Serve", "Nb_Jours"]:
        if col in perf_final.columns:
            perf_final[col] = pd.to_numeric(perf_final[col], errors="coerce").fillna(0).astype(int)

    return perf_final

def highlight_dotation(row, perf):
        if row[("Nb_Transactions")] and row.name is not None:
            val = perf.loc[row.name, 'Nb_Transactions']
            if val == 0:
                return ['background-color: red'] * len(row)
        return [''] * len(row)


def _display_top_10(perf):
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
        top_perf = top_perf[
            top_perf['Zone_SA'] == selected_zone_sa
        ].copy()

    # Conversion propre de TR_General (retirer %)
    top_perf['TR_General_num'] = (
        top_perf['TR_General']
        .astype(str)
        .str.replace('%', '', regex=False)
    )

    top_perf['TR_General_num'] = pd.to_numeric(
        top_perf['TR_General_num'],
        errors='coerce'
    ).fillna(0)

    top_perf['Σ_POS_Serve'] = pd.to_numeric(
        top_perf['Σ_POS_Serve'],
        errors='coerce'
    ).fillna(0)

    top_perf['HVC_Serve'] = pd.to_numeric(
        top_perf['HVC_Serve'],
        errors='coerce'
    ).fillna(0)

    # Tri Top 10
    top_perf = top_perf.sort_values(
        by=[
            'HVC_Serve',
            'TR_General_num',
            'Σ_POS_Serve'
        ],
        ascending=False
    ).head(10).copy()

    # Rang + Médailles
    medals = {
        1: "🥇",
        2: "🥈",
        3: "🥉"
    }

    top_perf = top_perf.reset_index(drop=True)
    top_perf['Rang'] = top_perf.index + 1

    top_perf['Classement'] = top_perf['Rang'].apply(
        lambda x: f"{medals.get(x, '')} {x}"
    )

    # Tableau final
    top10_display = top_perf[[
        'Classement',
        'Nom_Ccial',
        'Zone_SA',
        'Premiere_Trans',
        'HVC_Serve',
        'Σ_POS_Serve',
        'TR_General'
    ]].rename(columns={
        'Classement': '🏅 Rang',
        'Nom_Ccial': 'Commercial',
        'Zone_SA': 'Zone_SA',
        'Premiere_Trans': 'Première Transaction',
        'HVC_Serve': 'HVC_Serve',
        'Σ_POS_Serve': 'Σ_POS_Serve',
        'TR_General': 'TR_General'
    })

    st.dataframe(
        top10_display,
        use_container_width=True,
        height=450
    )


def _add_export_buttons(perf, perf_displqy):
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
        total_rows = len(perf_displqy)

        for i in range(0, total_rows, chunk_size):
            chunk = perf_displqy.iloc[i:i + chunk_size].copy()

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
    col1.download_button("Télécharger Excel", to_excel(perf), "Performance_Commerciaux.xlsx")
    col2.download_button("Télécharger CSV", perf.to_csv(index=False).encode('utf-8'), "Performance_Commerciaux.csv")


# ===================== FONCTION PRINCIPALE =====================

def show_performance():
    st.title("📈 Performance Commerciaux")

    _check_prerequisites()

    comm_config = st.session_state.commercial_config_df
    exclusion_df = st.session_state.get('exclusion_df')
    exclusion_master = st.session_state.get('exclusion_master')
    exclusion_cds = st.session_state.get('exclusion_cds')
    master_df = st.session_state.get('pos_master_df')

    # Upload logic (à garder tel quel ou légèrement refactorisé)
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
        df_raw = _load_transactions()
        df, df_full = _preprocess_transactions(df_raw, comm_config)

        _sidebar_config(df)

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

        # Calculs
        base_trans = _prepare_base_transactions(df, master_df, global_excluded)
        hvc_others = _compute_hvc_others(base_trans)
        base_perf = _compute_base_performance(df, global_excluded)
        trend = _compute_trend_14_17(df, global_excluded)
        dotation_group = _compute_dotations(df_full, df, exclusion_master, exclusion_df)

        # Merge
        perf_daily = base_perf.merge(hvc_others, on=['Date_only', 'Nom_Ccial'], how='left')
        perf_daily = perf_daily.merge(dotation_group, on=['Date_only', 'Nom_Ccial'], how='left')
        perf_daily = perf_daily.merge(trend, on='Nom_Ccial', how='left')

        if "FD_HVC" in perf_daily.columns and "FD_Others" in perf_daily.columns:
            perf_daily["Σ_FD"] = perf_daily["FD_HVC"] + perf_daily["FD_Others"]
        if "HVC_Serve" in perf_daily.columns and "Other_Serve" in perf_daily.columns:
            perf_daily["Σ_POS_Serve"] = perf_daily["HVC_Serve"] + perf_daily["Other_Serve"]

        # Consolidation finale
        perf_final = _consolidate_performance(perf_daily)

        # Affichage
        # _display_main_table(perf_final)
        _display_top_10(perf_final)

        st.subheader("Performance Détaillée des Commerciaux")
        # Création du MultiIndex pour les colonnes (à adapter selon tes besoins)
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
        
        perf_display = perf_final.copy()

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
        styled_df = style_perf(perf_final)
        styled_df = styled_df.apply(highlight_dotation, axis=1)
        st.dataframe(styled_df, use_container_width=True, height=650)

        _add_export_buttons(perf_final, perf_displqy=perf_display)