import streamlit as st
import plotly.express as px
import pandas as pd
import numpy as np
import os
import zipfile
import dataframe_image as dfi
from utils.helpers import clean_phone, to_excel
from utils.storage import list_month_folders, get_files_by_month, upload_file_by_month
from utils.supabase import load_setting

BUCKET_NAME = "performance-cds-result-files"


def load_perf_folders(bucket, folders):
    dfs = []
    loaded_folders = []

    for folder in folders:
        folder_df = get_files_by_month(bucket=bucket, selected_month=folder)
        if folder_df is None or folder_df.empty:
            continue

        folder_df["source_folder"] = folder
        dfs.append(folder_df)
        loaded_folders.append(folder)

    if not dfs:
        return pd.DataFrame(), []

    return pd.concat(dfs, ignore_index=True), loaded_folders


def format_amount(x):
    if pd.isna(x) or x == 0:
        return "0"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}K"
    return f"{int(x)}"


def _standardize_transaction_type(df):
    if df is None or df.empty or "Type" not in df.columns:
        return df

    df = df.copy()
    df["Type"] = df["Type"].astype(str).str.strip()
    type_map = {
        "cash out": "Cash out",
        "cash_out": "Cash out",
        "cash in": "Cash in",
        "cash_in": "Cash in",
        "transfer": "Transfer",
    }
    df["Type"] = df["Type"].str.lower().map(type_map).fillna(df["Type"])
    return df


def _extract_name_columns(df):
    if df is None or df.empty:
        return df

    df = df.copy()
    from_cols = ["From Name", "From name", "From_Name", "from_name"]
    to_cols = ["To Name", "To name", "To_Name", "to_name"]

    df["From_name"] = pd.NA
    df["To_name"] = pd.NA

    for col in from_cols:
        if col in df.columns:
            df["From_name"] = df[col].astype(str).str.strip()
            break

    for col in to_cols:
        if col in df.columns:
            df["To_name"] = df[col].astype(str).str.strip()
            break

    return df


def _match_cds_by_name(df, cds_config):
    if df is None or df.empty or cds_config is None or cds_config.empty:
        return df

    df = df.copy()
    if "CDS" not in cds_config.columns:
        return df

    name_map = (
        cds_config["CDS"]
        .astype(str)
        .str.strip()
        .str.lower()
        .replace({"": pd.NA})
    )
    name_map = cds_config.loc[name_map.notna(), ["CDS", "NUM_clean"]].copy()
    name_map["CDS"] = name_map["CDS"].astype(str).str.strip().str.lower()
    lookup = name_map.set_index("CDS")["NUM_clean"].to_dict()

    same_name_cash_out = (
        df["Type"].eq("Cash out") &
        df["From_name"].notna() &
        df["To_name"].notna() &
        df["From_name"].str.lower().eq(df["To_name"].str.lower())
    )

    if same_name_cash_out.any():
        names = df.loc[same_name_cash_out, "From_name"].str.strip().str.lower()
        df.loc[same_name_cash_out, "NUM_clean"] = names.map(lookup)

    return df


def style_perf(df):

    def highlight_inactive(row):

        # colonne multi-index
        try:
            val = row[("Nb_Jours", "HVC_Serve")]
        except:
            return [""] * len(row)

        # ne pas colorer TOTAL
        is_total = str(row.iloc[0]).upper() == "TOTAL"

        if not is_total and pd.notna(val) and float(val) == 0:
            return ["background-color: #ffcccc"] * len(row)

        return [""] * len(row)

    def highlight_total(row):
        is_total = str(row.iloc[0]).upper() == "TOTAL"

        return [
            "font-weight: bold; background-color: #fff3bf;"
            if is_total else ""
            for _ in row
        ]

    return (
        df.style

        # IMPORTANT :
        # inactive AVANT total
        .apply(highlight_inactive, axis=1)

        .apply(highlight_total, axis=1)

        .set_table_styles([
            {
                "selector": "th",
                "props": [
                    ("background-color", "black"),
                    ("color", "#f1c40f"),
                    ("font-weight", "bold"),
                    ("text-align", "center"),
                    ("border", "1px solid #444"),
                ],
            },
            {
                "selector": "td",
                "props": [
                    ("text-align", "center"),
                    ("border", "1px solid #ddd"),
                ],
            },
        ])
    )

def extract_numbers(config_df, column):
    if config_df is None or column not in config_df.columns:
        return []
    return config_df[column].apply(clean_phone).dropna().astype(str).tolist()


def prepare_Cds_config(cds_df):
    required_cols = ["NUM", "CDS"]
    missing = [col for col in required_cols if col not in cds_df.columns]
    if missing:
        st.error("Colonne(s) manquante(s) dans CDS: " + ", ".join(missing))
        st.stop()

    config = cds_df[required_cols].copy()
    config["NUM_clean"] = config["NUM"].apply(clean_phone)
    config["CDS"] = config["CDS"].fillna("").astype(str).str.strip()
    
    config = config[config["NUM_clean"].notna()].drop_duplicates("NUM_clean")
    return config


def get_hvc_numbers(master_df):
    """Retourne les numéros HVC à partir du fichier master."""
    if master_df is None or master_df.empty:
        st.warning("Fichier master_df vide ou non chargé.")
        return set()

    df = master_df.copy()

    # ====================== NETTOYAGE ======================
    # Colonne MSISDN
    msisdn_col = None
    for col in ["msisdn", "agent_msisdn", "MSISDN", "Agent MSISDN"]:
        if col in df.columns:
            msisdn_col = col
            break

    if not msisdn_col:
        st.error("Aucune colonne MSISDN trouvée dans master_df")
        return set()

    df["MSISDN_clean"] = df[msisdn_col].apply(clean_phone)

    # Colonne Segment Group
    segment_col = None
    for col in ["segment_group", "Segment Group", "Segment_Group", "segment"]:
        if col in df.columns:
            segment_col = col
            break

    if not segment_col:
        st.warning("Aucune colonne segment_group trouvée. Aucun HVC filtré.")
        return set()

    # Extraction des HVC
    hvc_mask = df[segment_col].astype(str).str.strip().str.contains("1-HVC", case=False, na=False)

    hvc_numbers = set(
        df[hvc_mask]["MSISDN_clean"].dropna().astype(str)
    )

    st.info(f"HVC trouvés dans master : {len(hvc_numbers)}")

    return hvc_numbers


def add_total_row(df, include_trend, include_dotation):
    if df.empty:
        return df

    work = df.copy()
    work["Dotation_Montant"] = pd.to_numeric(work["Dotation_Montant_raw"], errors="coerce").fillna(0)
    work["FD_HVC"] = pd.to_numeric(work["FD_HVC_raw"], errors="coerce").fillna(0)

    total = {
        "CDS": "",
        "Nb_Jours": pd.to_numeric(work["Nb_Jours"], errors="coerce").sum(),
        "FD_HVC": pd.to_numeric(work["FD_HVC_raw"], errors="coerce").sum(),
        "HVC_Serve": pd.to_numeric(work["HVC_Serve"], errors="coerce").sum(),
    }

    if include_trend:
        total["POS_serve"] = pd.to_numeric(work["POS_serve"], errors="coerce").sum()
        total["New"] = pd.to_numeric(work["New"], errors="coerce").sum()
    if include_dotation:
        total["Dotation_Nom"]= ""
        total["Dotation_Montant"]= pd.to_numeric(
            work["Dotation_Montant"],
            errors="coerce"
        ).sum()

    out = pd.concat([work, pd.DataFrame([total])], ignore_index=True)

    for col in ["Dotation_Montant","FD_HVC"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).apply(format_amount)

    for col in ["Nb_Jours", "HVC_Serve", "POS_serve", "New"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)

    return out


def build_display(df, include_trend, include_dotation):
    cols = [
        "CDS",
        "Nb_Jours",

        "FD_HVC",
        "HVC_Serve",
    ]
    headers = [
        ("", "Nom"),
        ("", "Nb_Jours"),

        ("HVC", "FD_HVC"),
        ("HVC", "HVC_Serve"),
    ]

    if include_trend:
        cols += ["POS_serve", "New"]
        headers += [("TREND [14H->17H]", "POS_serve"), ("TREND [14H->17H]", "New")]
    if include_dotation:
        cols += ["Dotation_Nom", "Dotation_Montant"]
        headers += [("Dotations", "Nom"), ("Dotations", "Montant")]

    display = df[cols].copy()
    display.columns = pd.MultiIndex.from_tuples(headers)
    return display


def show_table(title, perf_df, include_trend, include_dotation):
    if perf_df.empty:
        st.info(f"Aucune donnee pour {title}.")
        return

    st.subheader(title)
    display = build_display(add_total_row(perf_df, include_trend, include_dotation), include_trend,include_dotation)
    st.dataframe(style_perf(display), use_container_width=True, height=650)


def show_performance_cds():
    st.title("Performance CDS")

    comm_config = load_setting("commerciaux")
    exclusion_df = load_setting("caisses")
    master_df = load_setting("maitre_pos")
    exclusion_master = load_setting("masters")
    exclusion_cds = load_setting("cds")
    pos_relay_caisse_df = load_setting("pos_relay_caisse")

    if exclusion_cds is None:
        st.error("Veuillez charger le fichier CDS dans Settings")
        st.stop()

    cds_config = prepare_Cds_config(exclusion_cds)
    if cds_config.empty:
        st.error("Le fichier CDS ne contient aucun NUM exploitable")
        st.stop()

    if "cds_files_uploaded" not in st.session_state:
        st.session_state.cds_files_uploaded = False
    if "cds_last_uploaded_files" not in st.session_state:
        st.session_state.cds_last_uploaded_files = []
    if "cds_compile_mode" not in st.session_state:
        st.session_state.cds_compile_mode = False
    if "cds_compiled_folders" not in st.session_state:
        st.session_state.cds_compiled_folders = []

    trans_files = st.file_uploader(
        "Upload les fichiers de transactions cds",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="cds_perf_trans",
    )

    if trans_files:
        current_names = [f.name for f in trans_files]
        if current_names != st.session_state.cds_last_uploaded_files:
            st.session_state.cds_files_uploaded = False
            st.session_state.cds_last_uploaded_files = current_names

    if trans_files and not st.session_state.cds_files_uploaded:
        uploaded_count = 0
        for file in trans_files:
            if upload_file_by_month(bucket=BUCKET_NAME, uploaded_file=file):
                uploaded_count += 1

        st.session_state.cds_files_uploaded = True
        get_files_by_month.clear()
        list_month_folders.clear()
        st.success(f"{uploaded_count} fichier(s) uploade(s) avec succes")
        st.rerun()

    if st.button("Reinitialiser les uploads"):
        st.session_state.cds_files_uploaded = False
        st.session_state.cds_last_uploaded_files = []
        st.session_state.cds_compile_mode = False
        st.session_state.cds_compiled_folders = []
        st.info("Upload reinitialise")

    with st.spinner("Analyse des performances CDS en cours..."):
        folders = list_month_folders(bucket=BUCKET_NAME)
        if folders is None or len(folders) == 0:
            st.warning("Aucun dossier trouve")
            st.stop()

        selected_month = st.selectbox(
            "Choisir le mois a analyser",
            folders,
            index=len(folders) - 1,
            key="cds_default_folder",
        )

        compiled_defaults = [
            folder for folder in st.session_state.cds_compiled_folders
            if folder in folders
        ] or [selected_month]

        selected_compile_folders = st.multiselect(
            "Dossiers a compiler",
            folders,
            default=compiled_defaults,
            key="cds_compile_folders_selector",
        )

        col_compile, col_default = st.columns(2)
        with col_compile:
            if st.button("Compiler les dossiers", type="primary", key="cds_compile_btn"):
                if selected_compile_folders:
                    st.session_state.cds_compile_mode = True
                    st.session_state.cds_compiled_folders = selected_compile_folders
                    st.rerun()
                else:
                    st.warning("Veuillez selectionner au moins un dossier a compiler.")

        with col_default:
            if st.button("Charger le dossier par defaut", key="cds_default_btn"):
                st.session_state.cds_compile_mode = False
                st.session_state.cds_compiled_folders = []
                st.rerun()

        if st.session_state.cds_compile_mode:
            folders_to_load = st.session_state.cds_compiled_folders or [selected_month]
            df, loaded_folders = load_perf_folders(bucket=BUCKET_NAME, folders=folders_to_load)
            selected_month_label = " + ".join(loaded_folders)
        else:
            df = get_files_by_month(bucket=BUCKET_NAME, selected_month=selected_month)
            loaded_folders = [selected_month]
            selected_month_label = selected_month

        if df is None or df.empty:
            st.warning("Aucun fichier de transactions trouve")
            st.stop()

        st.success(
            f"{len(df)} lignes chargees depuis {len(loaded_folders)} dossier(s): "
            f"{', '.join(loaded_folders)}"
        )

        required_transaction_cols = ["Date", "Amount", "Type", "From", "To"]
        missing_transaction_cols = [col for col in required_transaction_cols if col not in df.columns]
        if missing_transaction_cols:
            st.error("Colonne(s) manquante(s) dans les transactions: " + ", ".join(missing_transaction_cols))
            st.stop()

        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").abs()
        df["From_clean"] = df["From"].apply(clean_phone)
        df["To_clean"] = df["To"].apply(clean_phone)
        df = _standardize_transaction_type(df)
        df = _extract_name_columns(df)
        df = df.dropna(subset=["Date"])

        df = df.drop_duplicates(
            subset=["Date", "From_clean", "To_clean", "Amount", "Type"],
            keep="last",
        )
        st.info(f"Apres deduplication : {len(df)} lignes")

        df["Date_only"] = df["Date"].dt.date
        df["Hour"] = df["Date"].dt.hour


        min_date = df["Date_only"].min()
        max_date = df["Date_only"].max()
        if pd.isna(min_date) or pd.isna(max_date):
            st.error("Impossible de determiner les dates du fichier")
            st.stop()

        date_range = st.sidebar.date_input(
            f"Filtre Date ({selected_month_label})",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
            key=f"cds_date_filter_{selected_month_label}",
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df["Date_only"] >= start_date) & (df["Date_only"] <= end_date)].copy()


        filtered_config = cds_config.copy()
        

        commercial_numbers = extract_numbers(comm_config, "Ccial_MSISDN")
        caisse_numbers = extract_numbers(exclusion_df, "NUM")
        master_numbers = extract_numbers(exclusion_master, "NUM")
        pr_caisse_numbers = extract_numbers(pos_relay_caisse_df, "MSISDN_PR")
        cds_numbers = cds_config["NUM_clean"].dropna().astype(str).tolist()

        global_excluded = set(
            commercial_numbers
            + caisse_numbers
            + master_numbers
            + cds_numbers
            + pr_caisse_numbers
        )

        # =========================
        # MERGE DES TRANSACTIONS
        # =========================
        
        all_trans = df.copy()

        cds_numbers = set(filtered_config["NUM_clean"].dropna().astype(str).tolist())
        df["From_clean_str"] = df["From_clean"].astype(str)
        df["To_clean_str"] = df["To_clean"].astype(str)
        df["NUM_clean"] = df["From_clean_str"].where(
            df["From_clean_str"].isin(cds_numbers),
            df["To_clean_str"].where(df["To_clean_str"].isin(cds_numbers))
        )

        df = _match_cds_by_name(df, filtered_config)
        df = df[df["NUM_clean"].notna()].copy()
        df = df.merge(
            filtered_config,
            on="NUM_clean",
            how="left",
        )

        # garder uniquement les transactions provenant
        # des CDS connus
        df = df[df["NUM_clean"].notna()].copy()

        st.write(f"Transactions filtrées pour les CDS connus : {len(df)} lignes")

        # Graphique de répartition des types de transactions
        type_summary = (
            df["Type"]
            .astype(str)
            .str.strip()
            .replace({"nan": "Autre", "None": "Autre", "": "Autre"})
            .value_counts()
            .reset_index(name="Count")
            .rename(columns={"index": "Type"})
        )

        if not type_summary.empty:
            st.subheader("Répartition des types de transactions")
            fig_type = px.bar(
                type_summary,
                x="Type",
                y="Count",
                title="Répartition des types de transactions",
                labels={"Type": "Type", "Count": "Nombre de transactions"},
                text="Count",
                height=420,
            )
            fig_type.update_layout(xaxis_tickangle=-45, yaxis_title="Nombre de transactions")
            fig_type.update_traces(textposition="outside")
            st.plotly_chart(fig_type, use_container_width=True)

        hvc_numbers = get_hvc_numbers(master_df)
        keys = [
            "NUM_clean",
            "CDS",
        ]

        fd_trans = df[
            (df["Type"] == "Transfer")
            & (df["Amount"] >= 10000)
            & (df["To_clean"].notna())
            & (~df["To_clean"].isin(global_excluded))
        ].copy()

        st.write(fd_trans)

        # Garder uniquement les lignes où To_clean est dans les HVC
        fd_trans = fd_trans[
            fd_trans["To_clean"].isin(hvc_numbers)
        ].copy()
        
        # Optionnel : ajouter la colonne Segment si tu en as besoin plus tard
        fd_trans["Segment"] = "HVC"

        if fd_trans.empty:
            st.warning("Aucune transaction valide apres exclusions.")

        # fd_trans["Segment"] = np.where(fd_trans["To_clean"].isin(hvc_numbers), "HVC")
    

        # =====================================================
        # BASE COMPLETE = TOUS LES CDS DU SETTINGS
        # =====================================================
        base_perf = filtered_config[keys].drop_duplicates().copy()

        # =====================================================
        # DOTATIONS (Masters -> CDS)
        # =====================================================

        master_ref = exclusion_master[
            ["NUM", "MASTER"]
        ].copy()

        master_ref["NUM"] = (
            master_ref["NUM"]
            .astype(str)
            .str.strip()
        )

        dotation_trans = all_trans[
            all_trans["Type"] == "Transfer"
        ].copy()

        # Identification MASTER
        dotation_trans = dotation_trans.merge(
            master_ref,
            left_on="From_clean",
            right_on="NUM",
            how="left"
        )

        dotation_trans["Dotation_Nom"] = dotation_trans["MASTER"]

        # Garder uniquement Master
        dotation_trans = dotation_trans[
            dotation_trans["Dotation_Nom"].notna()
        ].copy()

        # CDS identifié sur TO
        dotation_trans = dotation_trans.merge(
            filtered_config[keys],
            left_on="To_clean",
            right_on="NUM_clean",
            how="inner"
        )

        # Dotateur principal
        if not dotation_trans.empty:

            dotation_freq = (
                dotation_trans
                .groupby(keys + ["Dotation_Nom"])
                .size()
                .reset_index(name="Nb")
            )

            idx = (
                dotation_freq
                .groupby(keys)["Nb"]
                .idxmax()
            )

            dotation_name = (
                dotation_freq
                .loc[idx]
                .drop(columns="Nb")
            )

            dotation_amount = (
                dotation_trans
                .groupby(keys)["Amount"]
                .sum()
                .reset_index(name="Dotation_Montant")
            )

        else:

            dotation_name = pd.DataFrame(
                columns=keys + ["Dotation_Nom"]
            )

            dotation_amount = pd.DataFrame(
                columns=keys + ["Dotation_Montant"]
            )

        # =====================================================
        # CONSTRUCTION PERF
        # =====================================================
        perf = base_perf.copy()

        if fd_trans.empty:

            perf = base_perf.copy()

            for col in [
                "Nb_Jours",
                "FD_HVC",
                "Serve_HVC",
            ]:
                perf[col] = 0
        else:
            segment_metrics = (
                fd_trans
                .groupby(keys + ["Segment"], dropna=False)
                .agg(
                    FD=("Amount", "sum"),
                    Serve=("To_clean", "nunique")
                )
                .reset_index()
            )

            pivot_df = segment_metrics.pivot_table(
                index=keys,
                columns="Segment",
                values=["FD", "Serve"],
                fill_value=0,
                aggfunc="sum",
            )
            pivot_df.columns = [f"{a}_{b}" for a, b in pivot_df.columns]
            pivot_df = pivot_df.reset_index()

            for col in ["FD_HVC", "Serve_HVC"]:
                if col not in pivot_df.columns:
                    pivot_df[col] = 0

            days = (
                fd_trans.groupby(keys, dropna=False)["Date_only"]
                .nunique()
                .reset_index(name="Nb_Jours")
            )

            perf_calc = pivot_df.merge(days, on=keys, how="left")

            # =====================================================
            # ON REINTEGRE TOUS LES PR/CAISSES
            # =====================================================
            perf = base_perf.merge(
                perf_calc,
                on=keys,
                how="left"
            )
            perf = perf.merge(
                dotation_name,
                on=keys,
                how="left"
            )

            perf = perf.merge(
                dotation_amount,
                on=keys,
                how="left"
            )

            perf = perf.rename(columns={
                "Serve_HVC": "HVC_Serve",
            })

            required_cols = [
                "FD_HVC",
                "HVC_Serve",
            ]

            for col in required_cols:
                if col not in perf.columns:
                    perf[col] = 0

            perf = perf.loc[:, ~perf.columns.duplicated()]

            # =====================================================
            # FILLNA GLOBAL
            # =====================================================
            numeric_fill_cols = [
                "Nb_Jours",
                "FD_HVC",
                "HVC_Serve",
            ]

            for col in numeric_fill_cols:
                if col in perf.columns:
                    # Conversion sécurisée
                    perf[col] = pd.to_numeric(perf[col], errors="coerce")
                    # Remplissage des NaN
                    perf[col] = perf[col].fillna(0)
                else:
                    perf[col] = 0

        trend_base = fd_trans.copy()
        trend_14_17 = trend_base[(trend_base["Hour"] >= 14) & (trend_base["Hour"] < 17)].copy()
        trend_6_14 = trend_base[(trend_base["Hour"] >= 6) & (trend_base["Hour"] <= 13)].copy()

        pos_group = (
            trend_14_17.groupby("NUM_clean")["To_clean"]
            .nunique()
            .reset_index(name="POS_serve")
        )

        morning_clients = (
            trend_6_14.groupby("NUM_clean")["To_clean"]
            .agg(lambda values: set(values.dropna()))
            .to_dict()
        )
        afternoon_clients = trend_14_17[["NUM_clean", "To_clean"]].dropna().drop_duplicates()
        new_clients = afternoon_clients[
            afternoon_clients.apply(
                lambda row: row["To_clean"] not in morning_clients.get(
                    row["NUM_clean"],
                    set()
                ),
                axis=1,
            )
        ].copy()

        # =========================
        # SECURISATION SI VIDE
        # =========================
        if new_clients.empty or "NUM_clean" not in new_clients.columns:
            new_group = pd.DataFrame(columns=["NUM_clean", "New"])
        else:
            new_group = (
                new_clients.groupby("NUM_clean")["To_clean"]
                .nunique()
                .reset_index(name="New")
            )

        trend_final = pos_group.merge(
            new_group,
            on="NUM_clean",
            how="left"
        )

        perf = perf.merge(trend_final, on="NUM_clean", how="left")
        for col in ["POS_serve", "New"]:
            if col not in perf.columns:
                perf[col] = 0
            perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0)

        numeric_cols = [
                "Nb_Jours",
                "FD_HVC",
                "HVC_Serve", "POS_serve", "New", "Dotation_Montant"
        ]
        for col in numeric_cols:
            if col in perf.columns:
                # Conversion sécurisée
                perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0)
            else:
                perf[col] = 0

        perf["Dotation_Montant_raw"] = perf["Dotation_Montant"]
        perf["FD_HVC_raw"] = perf["FD_HVC"]

        perf = perf.sort_values(["CDS"]).reset_index(drop=True)

        export_perf = perf.copy()
        for col in ["FD_HVC"]:
            perf[col] = perf[col].apply(format_amount)
        for col in ["Nb_Jours", "HVC_Serve", "POS_serve", "New"]:
            perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0).astype(int)

        show_table(
            "Performance Detaillee des CDS",
            perf.copy(),
            include_trend=True,
            include_dotation=True
        )

        if st.button("Capturer tableau en images", key="cds_capture_images"):
            export_folder = "exports_cds_perf"
            os.makedirs(export_folder, exist_ok=True)
            for file in os.listdir(export_folder):
                file_path = os.path.join(export_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            image_files = []
            tables = []
            cds_table = perf.copy()
            if not cds_table.empty:
                tables.append(("cds", build_display(add_total_row(cds_table, True, True), True, True)))
            
            for name, table in tables:
                file_name = f"performance_{name}.png"
                file_path = os.path.join(export_folder, file_name)
                dfi.export(style_perf(table), file_path, table_conversion="chrome")
                image_files.append(file_name)

            zip_path = os.path.join(export_folder, "Performance_CDS.zip")
            with zipfile.ZipFile(zip_path, "w") as zipf:
                for file_name in image_files:
                    zipf.write(os.path.join(export_folder, file_name), arcname=file_name)

            with open(zip_path, "rb") as f:
                st.download_button(
                    "Telecharger les captures (ZIP)",
                    f,
                    "Performance_CDS.zip",
                    "application/zip",
                )

        col1, col2 = st.columns(2)
        excel_data = to_excel(export_perf)
        col1.download_button("Telecharger Excel", excel_data, "Performance_CDS.xlsx")
        col2.download_button(
            "Telecharger CSV",
            export_perf.to_csv(index=False).encode("utf-8"),
            "Performance_CDS.csv",
        )
