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

BUCKET_NAME = "performance-pos-caisse-result-files"


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


def _match_pr_by_name(df, pr_config):
    if df is None or df.empty or pr_config is None or pr_config.empty:
        return df

    df = df.copy()
    if "Nom du point de relais" not in pr_config.columns:
        return df

    name_map = (
        pr_config["Nom du point de relais"]
        .astype(str)
        .str.strip()
        .str.lower()
        .replace({"": pd.NA})
    )
    name_map = pr_config.loc[name_map.notna(), ["Nom du point de relais", "MSISDN_PR_clean"]].copy()
    name_map["Nom du point de relais"] = name_map["Nom du point de relais"].astype(str).str.strip().str.lower()
    lookup = name_map.set_index("Nom du point de relais")["MSISDN_PR_clean"].to_dict()

    same_name_cash_out = (
        df["Type"].eq("Cash out") &
        df["From_name"].notna() &
        df["To_name"].notna() &
        df["From_name"].str.lower().eq(df["To_name"].str.lower())
    )

    if same_name_cash_out.any():
        names = df.loc[same_name_cash_out, "From_name"].str.strip().str.lower()
        df.loc[same_name_cash_out, "MSISDN_PR_clean"] = names.map(lookup)

    return df


def style_perf(df):

    def highlight_inactive(row):

        # colonne multi-index
        try:
            val = row[("All segment", "Σ_POS_Serve")]
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


def prepare_point_relay_caisse_config(point_relay_caisse_df):
    required_cols = ["MSISDN_PR", "TERRITOIRE", "Localisation", "Nom du point de relais"]
    missing = [col for col in required_cols if col not in point_relay_caisse_df.columns]
    if missing:
        st.error("Colonne(s) manquante(s) dans Point Relais & Caisses: " + ", ".join(missing))
        st.stop()

    config = point_relay_caisse_df[required_cols].copy()
    config["MSISDN_PR_clean"] = config["MSISDN_PR"].apply(clean_phone)
    config["Nom du point de relais"] = config["Nom du point de relais"].fillna("").astype(str).str.strip()
    config["TERRITOIRE"] = config["TERRITOIRE"].fillna("NON RENSEIGNE").astype(str).str.strip()
    config["Localisation"] = config["Localisation"].fillna("NON RENSEIGNE").astype(str).str.strip()
    config["Type_Point"] = np.where(
        config["Nom du point de relais"].str.upper().str.startswith("CAISSE"),
        "Caisses",
        "Point Relais",
    )
    config = config[config["MSISDN_PR_clean"].notna()].drop_duplicates("MSISDN_PR_clean")
    return config


def get_hvc_numbers(master_df):
    if master_df is None:
        st.error("Le fichier Master POS est manquant dans les configurations.")
        st.stop()
        
    required = ["segment_group", "agent_msisdn"]
    missing = [col for col in required if col not in master_df.columns]
    if missing:
        st.error(f"Le fichier Master POS ne contient pas les colonnes requises : {', '.join(missing)}")
        st.stop() # Bloque l'exécution pour vous forcer à corriger le fichier

    return set(
        master_df[
            master_df["segment_group"].astype(str).str.strip().eq("1-HVC")
        ]["agent_msisdn"].apply(clean_phone).dropna().astype(str)
    )

def get_mvc_lvc_numbers(master_df):
    if master_df is None:
        st.error("Le fichier Master POS est manquant dans les configurations.")
        st.stop()

    required = ["segment_group", "agent_msisdn"]
    missing = [col for col in required if col not in master_df.columns]
    if missing:
        st.error(f"Le fichier Master POS ne contient pas les colonnes requises : {', '.join(missing)}")
        st.stop()

    # On filtre sur les valeurs exactes de vos segments MVC et LVC
    # (Ajustez les textes exacts "2-MVC" ou "3-LVC" selon vos données réelles)
    valid_segments = ["2-MVC", "3-LVC", "MVC", "LVC"] 
    
    filtered_df = master_df[
        master_df["segment_group"].astype(str).str.strip().isin(valid_segments)
    ]
    return set(filtered_df["agent_msisdn"].apply(clean_phone).dropna().astype(str))


def add_total_row(df, include_trend, include_dotation):
    if df.empty:
        return df

    work = df.copy()
    work["Dotation_Montant"] = pd.to_numeric(work["Dotation_Montant_raw"], errors="coerce").fillna(0)
    work["FD_HVC"] = pd.to_numeric(work["FD_HVC_raw"], errors="coerce").fillna(0)
    work["FD_Others"] = pd.to_numeric(work["FD_Others_raw"], errors="coerce").fillna(0)
    work["Σ_FD"] = pd.to_numeric(work["Σ_FD_raw"], errors="coerce").fillna(0)
    # work["Cash_In_HVC"] = pd.to_numeric(work["Cash_In_HVC_raw"], errors="coerce").fillna(0)
    # work["Cash_Out_HVC"] = pd.to_numeric(work["Cash_Out_HVC_raw"], errors="coerce").fillna(0)
    # work["Cash_In_Others"] = pd.to_numeric(work["Cash_In_Others_raw"], errors="coerce").fillna(0)
    # work["Cash_Out_Others"] = pd.to_numeric(work["Cash_Out_Others_raw"], errors="coerce").fillna(0)
    # work["Σ_Cash_In"] = pd.to_numeric(work["Σ_Cash_In_raw"], errors="coerce").fillna(0)
    # work["Σ_Cash_Out"] = pd.to_numeric(work["Σ_Cash_Out_raw"], errors="coerce").fillna(0)

    total = {
        "TERRITOIRE": "TOTAL",
        "Localisation": "",
        "Nom du point de relais": "",
        "Type_Point": "",
        "Nb_Jours": pd.to_numeric(work["Nb_Jours"], errors="coerce").sum(),
        # "Cash_In_HVC": pd.to_numeric(work["Cash_In_HVC_raw"], errors="coerce").sum(),
        # "Cash_In_Serve_HVC": pd.to_numeric(work["Cash_In_Serve_HVC"], errors="coerce").sum(),
        # "Cash_Out_HVC": pd.to_numeric(work["Cash_Out_HVC_raw"], errors="coerce").sum(),
        # "Cash_Out_Serve_HVC": pd.to_numeric(work["Cash_Out_Serve_HVC"], errors="coerce").sum(),
        # "Cash_In_Others": pd.to_numeric(work["Cash_In_Others_raw"], errors="coerce").sum(),
        # "Cash_In_Serve_Others": pd.to_numeric(work["Cash_In_Serve_Others"], errors="coerce").sum(),
        # "Cash_Out_Others": pd.to_numeric(work["Cash_Out_Others_raw"], errors="coerce").sum(),
        # "Cash_Out_Serve_Others": pd.to_numeric(work["Cash_Out_Serve_Others"], errors="coerce").sum(),
        "FD_HVC": pd.to_numeric(work["FD_HVC_raw"], errors="coerce").sum(),
        "HVC_Serve": pd.to_numeric(work["HVC_Serve"], errors="coerce").sum(),
        "FD_Others": pd.to_numeric(work["FD_Others_raw"], errors="coerce").sum(),
        "Other_Serve": pd.to_numeric(work["Other_Serve"], errors="coerce").sum(),
        # "Σ_Cash_In": pd.to_numeric(work["Σ_Cash_In_raw"], errors="coerce").sum(),
        # "Σ_Cash_Out": pd.to_numeric(work["Σ_Cash_Out_raw"], errors="coerce").sum(),
        # "Σ_Cash_In_Serve": pd.to_numeric(work["Σ_Cash_In_Serve"], errors="coerce").sum(),
        # "Σ_Cash_Out_Serve": pd.to_numeric(work["Σ_Cash_Out_Serve"], errors="coerce").sum(),
        "Σ_FD": pd.to_numeric(work["Σ_FD_raw"], errors="coerce").sum(),
        "Σ_POS_Serve": pd.to_numeric(work["Σ_POS_Serve"], errors="coerce").sum(),
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

    for col in ["Dotation_Montant","FD_HVC", "FD_Others", "Σ_FD"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).apply(format_amount)

    for col in ["Nb_Jours", "HVC_Serve", "Other_Serve", "Σ_POS_Serve", "POS_serve", "New"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)

    return out


def build_display(df, include_trend, include_dotation):
    cols = [
        "TERRITOIRE",
        "Localisation",
        "Nom du point de relais",
        "Type_Point",
        "Nb_Jours",

        "FD_HVC",
        "HVC_Serve",
        # "Cash_In_HVC",
        # "Cash_In_Serve_HVC",
        # "Cash_Out_HVC",
        # "Cash_Out_Serve_HVC",

        "FD_Others",
        "Other_Serve",
        # "Cash_In_Others",
        # "Cash_In_Serve_Others",
        # "Cash_Out_Others",
        # "Cash_Out_Serve_Others",

        "Σ_FD",
        "Σ_POS_Serve",
        # "Σ_Cash_In",
        # "Σ_Cash_In_Serve",
        # "Σ_Cash_Out",
        # "Σ_Cash_Out_Serve",
    ]
    headers = [
        ("", "Territoire"),
        ("", "Localisation"),
        ("", "Nom"),
        ("", "Type"),
        ("", "Nb_Jours"),

        ("HVC", "FD_HVC"),
        ("HVC", "HVC_Serve"),
        # ("HVC", "Cash_In_HVC"),
        # ("HVC", "Cash_In_Serve_HVC"),
        # ("HVC", "Cash_Out_HVC"),
        # ("HVC", "Cash_Out_Serve_HVC"),

        ("Others", "FD_Others"),
        ("Others", "Other_Serve"),
        # ("Others", "Cash_In_Others"),
        # ("Others", "Cash_In_Serve_Others"),
        # ("Others", "Cash_Out_Others"),
        # ("Others", "Cash_Out_Serve_Others"),

        ("All segment", "Σ_FD"),
        ("All segment", "Σ_POS_Serve"),
        # ("All segment", "Σ_Cash_In"),
        # ("All segment", "Σ_Cash_In_Serve"),
        # ("All segment", "Σ_Cash_Out"),
        # ("All segment", "Σ_Cash_Out_Serve"),
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


def show_performance_pos_caisse():
    st.title("Performance PR & Caisses")

    comm_config = load_setting("commerciaux")
    exclusion_df = load_setting("caisses")
    master_df = load_setting("maitre_pos")
    exclusion_master = load_setting("masters")
    exclusion_cds = load_setting("cds")
    pos_relay_caisse_df = load_setting("pos_relay_caisse")

    if pos_relay_caisse_df is None:
        st.error("Veuillez charger le fichier Point Relais & Caisses dans Settings")
        st.stop()

    pr_config = prepare_point_relay_caisse_config(pos_relay_caisse_df)
    if pr_config.empty:
        st.error("Le fichier Point Relais & Caisses ne contient aucun MSISDN exploitable")
        st.stop()

    if "pos_caisse_files_uploaded" not in st.session_state:
        st.session_state.pos_caisse_files_uploaded = False
    if "pos_caisse_last_uploaded_files" not in st.session_state:
        st.session_state.pos_caisse_last_uploaded_files = []
    if "pos_caisse_compile_mode" not in st.session_state:
        st.session_state.pos_caisse_compile_mode = False
    if "pos_caisse_compiled_folders" not in st.session_state:
        st.session_state.pos_caisse_compiled_folders = []

    trans_files = st.file_uploader(
        "Upload les fichiers de transactions PR/Caisses",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pr_caisse_perf_trans",
    )

    if trans_files:
        current_names = [f.name for f in trans_files]
        if current_names != st.session_state.pos_caisse_last_uploaded_files:
            st.session_state.pos_caisse_files_uploaded = False
            st.session_state.pos_caisse_last_uploaded_files = current_names

    if trans_files and not st.session_state.pos_caisse_files_uploaded:
        uploaded_count = 0
        for file in trans_files:
            if upload_file_by_month(bucket=BUCKET_NAME, uploaded_file=file):
                uploaded_count += 1

        st.session_state.pos_caisse_files_uploaded = True
        get_files_by_month.clear()
        list_month_folders.clear()
        st.success(f"{uploaded_count} fichier(s) uploade(s) avec succes")
        st.rerun()

    if st.button("Reinitialiser les uploads"):
        st.session_state.pos_caisse_files_uploaded = False
        st.session_state.pos_caisse_last_uploaded_files = []
        st.session_state.pos_caisse_compile_mode = False
        st.session_state.pos_caisse_compiled_folders = []
        st.info("Upload reinitialise")

    with st.spinner("Analyse des performances PR/Caisses en cours..."):
        folders = list_month_folders(bucket=BUCKET_NAME)
        if folders is None or len(folders) == 0:
            st.warning("Aucun dossier trouve")
            st.stop()

        selected_month = st.selectbox(
            "Choisir le mois a analyser",
            folders,
            index=len(folders) - 1,
            key="pr_caisse_default_folder",
        )

        compiled_defaults = [
            folder for folder in st.session_state.pos_caisse_compiled_folders
            if folder in folders
        ] or [selected_month]

        selected_compile_folders = st.multiselect(
            "Dossiers a compiler",
            folders,
            default=compiled_defaults,
            key="pr_caisse_compile_folders_selector",
        )

        col_compile, col_default = st.columns(2)
        with col_compile:
            if st.button("Compiler les dossiers", type="primary", key="pr_caisse_compile_btn"):
                if selected_compile_folders:
                    st.session_state.pos_caisse_compile_mode = True
                    st.session_state.pos_caisse_compiled_folders = selected_compile_folders
                    st.rerun()
                else:
                    st.warning("Veuillez selectionner au moins un dossier a compiler.")

        with col_default:
            if st.button("Charger le dossier par defaut", key="pr_caisse_default_btn"):
                st.session_state.pos_caisse_compile_mode = False
                st.session_state.pos_caisse_compiled_folders = []
                st.rerun()

        if st.session_state.pos_caisse_compile_mode:
            folders_to_load = st.session_state.pos_caisse_compiled_folders or [selected_month]
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
            key=f"pr_caisse_date_filter_{selected_month_label}",
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df["Date_only"] >= start_date) & (df["Date_only"] <= end_date)].copy()

        type_options = ["Tous", "Point Relais", "Caisses"]
        selected_type = st.sidebar.selectbox("Filtre Type", type_options, key="pr_caisse_type_filter")

        territoire_options = ["Tous"] + sorted(pr_config["TERRITOIRE"].dropna().astype(str).unique().tolist())
        selected_territoire = st.sidebar.selectbox(
            "Filtre Territoire",
            territoire_options,
            key="pr_caisse_territoire_filter",
        )

        filtered_config = pr_config.copy()
        if selected_type != "Tous":
            filtered_config = filtered_config[filtered_config["Type_Point"] == selected_type].copy()
        if selected_territoire != "Tous":
            filtered_config = filtered_config[filtered_config["TERRITOIRE"] == selected_territoire].copy()

        if filtered_config.empty:
            st.warning("Aucun PR/Caisse ne correspond aux filtres.")
            st.stop()

        commercial_numbers = extract_numbers(comm_config, "Ccial_MSISDN")
        caisse_numbers = extract_numbers(exclusion_df, "NUM")
        master_numbers = extract_numbers(exclusion_master, "NUM")
        cds_numbers = extract_numbers(exclusion_cds, "NUM")
        pr_caisse_numbers = pr_config["MSISDN_PR_clean"].dropna().astype(str).tolist()

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

        pr_numbers = set(filtered_config["MSISDN_PR_clean"].dropna().astype(str).tolist())
        df["From_clean_str"] = df["From_clean"].astype(str)
        df["To_clean_str"] = df["To_clean"].astype(str)
        df["MSISDN_PR_clean"] = df["From_clean_str"].where(
            df["From_clean_str"].isin(pr_numbers),
            df["To_clean_str"].where(df["To_clean_str"].isin(pr_numbers))
        )

        df = _match_pr_by_name(df, filtered_config)
        df = df[df["MSISDN_PR_clean"].notna()].copy()
        df = df.merge(
            filtered_config,
            on="MSISDN_PR_clean",
            how="left",
        )

        # garder uniquement les transactions provenant
        # des PR/Caisses connus
        df = df[df["MSISDN_PR_clean"].notna()].copy()

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
        mvc_lvc_numbers = get_mvc_lvc_numbers(master_df)

        keys = [
            "MSISDN_PR_clean",
            "TERRITOIRE",
            "Localisation",
            "Nom du point de relais",
            "Type_Point"
        ]

        fd_trans = df[
            (df["Type"] == "Transfer")
            & (df["Amount"] >= 10000)
            & (df["To_clean"].notna())
            & (~df["To_clean"].isin(global_excluded))
        ].copy()

        # Attribution stricte du segment
        def assign_segment(phone_number):
            if phone_number in hvc_numbers:
                return "HVC"
            elif phone_number in mvc_lvc_numbers:
                return "OTHER" # Concerne désormais STRICTEMENT les MVC et LVC
            else:
                return "UNKNOWN" # Pour les clients sans segment ou hors cible

        fd_trans["Segment"] = fd_trans["To_clean"].apply(assign_segment)

        # IMPORTANT : On ne garde dans le rapport que les segments valides (HVC et OTHER)
        # On exclut les "UNKNOWN" pour ne pas polluer les résultats
        fd_trans = fd_trans[fd_trans["Segment"].isin(["HVC", "OTHER"])].copy()

        if fd_trans.empty:
            st.warning("Aucune transaction valide apres exclusions.")

        fd_trans["Segment"] = np.where(fd_trans["To_clean"].isin(hvc_numbers), "HVC", "OTHER")
        
        # cash_in_trans = df[
        #     (df["Type"] == "Cash in")
        #     & (df["Amount"] >= 10000)
        #     & (df["To_clean"].notna())
        #     & (~df["To_clean"].isin(global_excluded))
        # ].copy()

        # # cash_in_trans = cash_in_trans.merge(
        # #     filtered_config,
        # #     left_on="From_clean",
        # #     right_on="MSISDN_PR_clean",
        # #     how="inner"
        # # )
        # cash_in_trans["Segment"] = np.where(
        #     cash_in_trans["To_clean"].isin(hvc_numbers),
        #     "HVC",
        #     "OTHER"
        # )

        # cash_out_trans = all_trans[
        #     (all_trans["Type"] == "Cash out")
        #     & (all_trans["Amount"] >= 10000)
        #     & (all_trans["From_clean"].notna())
        #     & (~all_trans["From_clean"].isin(global_excluded))
        # ].copy()

        # cash_out_trans = cash_out_trans.merge(
        #     filtered_config,
        #     left_on="To_clean",
        #     right_on="MSISDN_PR_clean",
        #     how="inner"
        # )
        # cash_out_trans["Segment"] = np.where(
        #     cash_out_trans["From_clean"].isin(hvc_numbers),
        #     "HVC",
        #     "OTHER"
        # )

        # cash_in_serve = (
        #     cash_in_trans
        #     .groupby(keys + ["Segment"])
        #     ["To_clean"]
        #     .nunique()
        #     .reset_index(name="Cash_In_Serve")
        # )

        # cash_out_serve = (
        #     cash_out_trans
        #     .groupby(keys + ["Segment"])
        #     ["From_clean"]
        #     .nunique()
        #     .reset_index(name="Cash_Out_Serve")
        # )
    

        # =====================================================
        # BASE COMPLETE = TOUS LES PR/CAISSES DU SETTINGS
        # =====================================================
        base_perf = filtered_config[keys].drop_duplicates().copy()

        # =====================================================
        # DOTATIONS (COMMERCIAL/CDS -> PR)
        # =====================================================

        comm_ref = comm_config[
            ["Ccial_MSISDN", "Nom_Ccial"]
        ].copy()

        comm_ref["Ccial_MSISDN"] = (
            comm_ref["Ccial_MSISDN"]
            .astype(str)
            .str.strip()
        )

        cds_ref = exclusion_cds[
            ["NUM", "CDS"]
        ].copy()

        cds_ref["NUM"] = (
            cds_ref["NUM"]
            .astype(str)
            .str.strip()
        )

        dotation_trans = all_trans[
            all_trans["Type"] == "Transfer"
        ].copy()

        # Identification Commercial
        dotation_trans = dotation_trans.merge(
            comm_ref,
            left_on="From_clean",
            right_on="Ccial_MSISDN",
            how="left"
        )

        # Identification CDS
        dotation_trans = dotation_trans.merge(
            cds_ref,
            left_on="From_clean",
            right_on="NUM",
            how="left"
        )

        dotation_trans["Dotation_Nom"] = (
            dotation_trans["Nom_Ccial"]
            .fillna(dotation_trans["CDS"])
        )

        # Garder uniquement Commercial ou CDS
        dotation_trans = dotation_trans[
            dotation_trans["Dotation_Nom"].notna()
        ].copy()

        # PR identifié sur TO
        dotation_trans = dotation_trans.merge(
            filtered_config[keys],
            left_on="To_clean",
            right_on="MSISDN_PR_clean",
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


        if fd_trans.empty:

            perf = base_perf.copy()

            for col in [
                "Nb_Jours",
                # "Cash_In_HVC",
                # "Cash_In_OTHER",
                # "Cash_Out_HVC",
                # "Cash_Out_OTHER",
                # "Cash_In_Serve_HVC",
                # "Cash_In_Serve_OTHER",
                # "Cash_Out_Serve_HVC",
                # "Cash_Out_Serve_OTHER",
                "FD_HVC",
                "Serve_HVC",
                "FD_OTHER",
                "Serve_OTHER",
                "Σ_FD",
                # "Σ_Cash_In",
                # "Σ_Cash_Out",
                # "Σ_Cash_In_Serve",
                # "Σ_Cash_Out_Serve",
                "Σ_POS_Serve",
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

            # cash_in_metrics = (
            #     cash_in_trans
            #     .groupby(keys + ["Segment"], dropna=False)
            #     .agg(
            #         Cash_In=("Amount", "sum")
            #     )
            #     .reset_index()
            # )

            # cash_out_metrics = (
            #     cash_out_trans
            #     .groupby(keys + ["Segment"], dropna=False)
            #     .agg(
            #         Cash_Out=("Amount", "sum")
            #     )
            #     .reset_index()
            # )
            # cash_metrics = cash_in_metrics.merge(
            #     cash_out_metrics,
            #     on=keys + ["Segment"],
            #     how="outer"
            # ).fillna(0)

            # cash_pivot = cash_metrics.pivot_table(
            #     index=keys,
            #     columns="Segment",
            #     values=[
            #         "Cash_In",
            #         "Cash_Out"
            #     ],
            #     fill_value=0,
            #     aggfunc="sum"
            # )

            # cash_pivot.columns = [
            #     f"{a}_{b}"
            #     for a, b in cash_pivot.columns
            # ]

            # cash_pivot = cash_pivot.reset_index()

            # cash_serve = cash_in_serve.merge(
            #     cash_out_serve,
            #     on=keys + ["Segment"],
            #     how="outer"
            # ).fillna(0)

            # cash_serve_pivot = cash_serve.pivot_table(
            #     index=keys,
            #     columns="Segment",
            #     values=[
            #         "Cash_In_Serve",
            #         "Cash_Out_Serve"
            #     ],
            #     fill_value=0,
            #     aggfunc="sum"
            # )

            # cash_serve_pivot.columns = [
            #     f"{a}_{b}"
            #     for a,b in cash_serve_pivot.columns
            # ]

            # cash_serve_pivot = cash_serve_pivot.reset_index()

            # pivot_df = pivot_df.merge(
            #     cash_pivot,
            #     on=keys,
            #     how="left"
            # )
            # pivot_df = pivot_df.merge(
            #     cash_serve_pivot,
            #     on=keys,
            #     how="left"
            # )

            for col in ["FD_HVC", "Serve_HVC", "FD_OTHER", "Serve_OTHER"]:
                if col not in pivot_df.columns:
                    pivot_df[col] = 0

            days = (
                fd_trans.groupby(keys, dropna=False)["Date_only"]
                .nunique()
                .reset_index(name="Nb_Jours")
            )

            # perf = pivot_df.merge(days, on=keys, how="left")
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
                "FD_OTHER": "FD_Others",
                "Serve_OTHER": "Other_Serve",

                # "Cash_In_OTHER": "Cash_In_Others",
                # "Cash_Out_OTHER": "Cash_Out_Others",

                # "Cash_In_Serve_OTHER": "Cash_In_Serve_Others",
                # "Cash_Out_Serve_OTHER": "Cash_Out_Serve_Others",
            })

            required_cols = [
                "FD_HVC",
                "FD_Others",
                "HVC_Serve",
                "Other_Serve",

                # "Cash_In_HVC",
                # "Cash_In_Others",

                # "Cash_Out_HVC",
                # "Cash_Out_Others",

                # "Cash_In_Serve_HVC",
                # "Cash_In_Serve_Others",

                # "Cash_Out_Serve_HVC",
                # "Cash_Out_Serve_Others",
            ]

            for col in required_cols:
                if col not in perf.columns:
                    perf[col] = 0

            perf = perf.loc[:, ~perf.columns.duplicated()]

            perf["Σ_FD"] = perf["FD_HVC"] + perf["FD_Others"]
            # perf["Σ_Cash_In"] = perf["Cash_In_HVC"] + perf["Cash_In_Others"]
            # perf["Σ_Cash_Out"] = perf["Cash_Out_HVC"] + perf["Cash_Out_Others"]
            # perf["Σ_Cash_In_Serve"] = perf["Cash_In_Serve_HVC"] + perf["Cash_In_Serve_Others"]

            # perf["Σ_Cash_Out_Serve"] = perf["Cash_Out_Serve_HVC"] + perf["Cash_Out_Serve_Others"]
            perf["Σ_POS_Serve"] = perf["HVC_Serve"] + perf["Other_Serve"]

            # =====================================================
            # FILLNA GLOBAL
            # =====================================================
            numeric_fill_cols = [
                "Nb_Jours",
                # "Cash_In_HVC",
                # "Cash_In_Serve_HVC",
                # "Cash_Out_HVC",
                # "Cash_Out_Serve_HVC",
                # "Cash_In_Others",
                # "Cash_In_Serve_Others",
                # "Cash_Out_Others",
                # "Cash_Out_Serve_Others",
                "FD_HVC",
                "HVC_Serve",
                "FD_Others",
                "Other_Serve",
                "Σ_FD",
                # "Σ_Cash_In",
                # "Σ_Cash_Out",
                # "Σ_Cash_In_Serve",
                # "Σ_Cash_Out_Serve",
                "Σ_POS_Serve",
            ]

            for col in numeric_fill_cols:
                if col in perf.columns:
                    # Conversion sécurisée
                    perf[col] = pd.to_numeric(perf[col], errors="coerce")
                    # Remplissage des NaN
                    perf[col] = perf[col].fillna(0)
                else:
                    perf[col] = 0

        trend_base = fd_trans[fd_trans["Type_Point"] == "Caisses"].copy()
        if trend_base.empty:
            trend_final = pd.DataFrame(columns=["MSISDN_PR_clean", "POS_serve", "New"])
        else:
            trend_14_17 = trend_base[(trend_base["Hour"] >= 14) & (trend_base["Hour"] < 17)].copy()
            trend_6_14 = trend_base[(trend_base["Hour"] >= 6) & (trend_base["Hour"] <= 13)].copy()

            pos_group = (
                trend_14_17.groupby("MSISDN_PR_clean")["To_clean"]
                .nunique()
                .reset_index(name="POS_serve")
            )

            morning_clients = (
                trend_6_14.groupby("MSISDN_PR_clean")["To_clean"]
                .agg(lambda values: set(values.dropna()))
                .to_dict()
            )
            afternoon_clients = trend_14_17[["MSISDN_PR_clean", "To_clean"]].dropna().drop_duplicates()
            new_clients = afternoon_clients[
                afternoon_clients.apply(
                    lambda row: row["To_clean"] not in morning_clients.get(
                        row["MSISDN_PR_clean"],
                        set()
                    ),
                    axis=1,
                )
            ].copy()

            # =========================
            # SECURISATION SI VIDE
            # =========================
            if new_clients.empty or "MSISDN_PR_clean" not in new_clients.columns:
                new_group = pd.DataFrame(columns=["MSISDN_PR_clean", "New"])
            else:
                new_group = (
                    new_clients.groupby("MSISDN_PR_clean")["To_clean"]
                    .nunique()
                    .reset_index(name="New")
                )

            trend_final = pos_group.merge(
                new_group,
                on="MSISDN_PR_clean",
                how="left"
            )

        perf = perf.merge(trend_final, on="MSISDN_PR_clean", how="left")
        for col in ["POS_serve", "New"]:
            if col not in perf.columns:
                perf[col] = 0
            perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0)

        numeric_cols = [
                "Nb_Jours",
                # "Cash_In_HVC",
                # "Cash_In_Serve_HVC",
                # "Cash_Out_HVC",
                # "Cash_Out_Serve_HVC",
                # "Cash_In_Others",
                # "Cash_In_Serve_Others",
                # "Cash_Out_Others",
                # "Cash_Out_Serve_Others",
                "FD_HVC",
                "HVC_Serve",
                "FD_Others",
                "Other_Serve",
                "Σ_FD",
                # "Σ_Cash_In",
                # "Σ_Cash_Out",
                # "Σ_Cash_In_Serve",
                # "Σ_Cash_Out_Serve",
                "Σ_POS_Serve",
        ]
        for col in numeric_cols:
            perf[col] = pd.to_numeric(perf.get(col, 0), errors="coerce").fillna(0)

        perf["Dotation_Montant_raw"] = perf["Dotation_Montant"]
        perf["FD_HVC_raw"] = perf["FD_HVC"]
        perf["FD_Others_raw"] = perf["FD_Others"]
        perf["Σ_FD_raw"] = perf["Σ_FD"]
        # perf["Cash_In_HVC_raw"] = perf["Cash_In_HVC"]
        # perf["Cash_Out_HVC_raw"] = perf["Cash_Out_HVC"]
        # perf["Cash_In_Others_raw"] = perf["Cash_In_Others"]
        # perf["Cash_Out_Others_raw"] = perf["Cash_Out_Others"]
        # perf["Σ_Cash_In_raw"] = perf["Σ_Cash_In"]
        # perf["Σ_Cash_Out_raw"] = perf["Σ_Cash_Out"]

        perf = perf.sort_values(["Type_Point", "TERRITOIRE", "Nom du point de relais"]).reset_index(drop=True)

        export_perf = perf.copy()
        for col in ["FD_HVC", "FD_Others", "Σ_FD"]:
            perf[col] = perf[col].apply(format_amount)
        for col in ["Nb_Jours", "HVC_Serve", "Other_Serve", "Σ_POS_Serve", "POS_serve", "New"]:
            perf[col] = pd.to_numeric(perf[col], errors="coerce").fillna(0).astype(int)

        if selected_type in ["Tous", "Point Relais"]:
            show_table(
                "Performance Detaillee des Point Relais",
                perf[perf["Type_Point"] == "Point Relais"].copy(),
                include_trend=False,
                include_dotation=True
            )

        if selected_type in ["Tous", "Caisses"]:
            show_table(
                "Performance Detaillee des Caisses",
                perf[perf["Type_Point"] == "Caisses"].copy(),
                include_trend=True,
                include_dotation=False
            )

        if st.button("Capturer tableau en images", key="pr_caisse_capture_images"):
            export_folder = "exports_pr_caisse_perf"
            os.makedirs(export_folder, exist_ok=True)
            for file in os.listdir(export_folder):
                file_path = os.path.join(export_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            image_files = []
            tables = []
            if selected_type in ["Tous", "Point Relais"]:
                pr_table = perf[perf["Type_Point"] == "Point Relais"].copy()
                if not pr_table.empty:
                    tables.append(("pr_relais", build_display(add_total_row(pr_table, False, True), False, True)))
            if selected_type in ["Tous", "Caisses"]:
                caisse_table = perf[perf["Type_Point"] == "Caisses"].copy()
                if not caisse_table.empty:
                    tables.append(("caisses", build_display(add_total_row(caisse_table, True, False), True, False)))

            for name, table in tables:
                file_name = f"performance_{name}.png"
                file_path = os.path.join(export_folder, file_name)
                dfi.export(style_perf(table), file_path, table_conversion="chrome")
                image_files.append(file_name)

            zip_path = os.path.join(export_folder, "Performance_PR_Caisses.zip")
            with zipfile.ZipFile(zip_path, "w") as zipf:
                for file_name in image_files:
                    zipf.write(os.path.join(export_folder, file_name), arcname=file_name)

            with open(zip_path, "rb") as f:
                st.download_button(
                    "Telecharger les captures (ZIP)",
                    f,
                    "Performance_PR_Caisses.zip",
                    "application/zip",
                )

        col1, col2 = st.columns(2)
        excel_data = to_excel(export_perf)
        col1.download_button("Telecharger Excel", excel_data, "Performance_PR_Caisses.xlsx")
        col2.download_button(
            "Telecharger CSV",
            export_perf.to_csv(index=False).encode("utf-8"),
            "Performance_PR_Caisses.csv",
        )
