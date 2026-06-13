import os
import zipfile
from io import BytesIO

import dataframe_image as dfi
import pandas as pd
import streamlit as st

from utils.helpers import clean_phone, load_file
from utils.supabase import load_setting


def _first_existing(columns, candidates):
    for col in candidates:
        if col in columns:
            return col
    return None


def _first_existing_case_insensitive(columns, candidates):
    normalized_columns = {str(col).strip().lower(): col for col in columns}
    for candidate in candidates:
        found = normalized_columns.get(str(candidate).strip().lower())
        if found is not None:
            return found
    return None


def _clean_pos_name(name, fallback):
    if pd.isna(name):
        return fallback

    value = str(name).strip()
    if not value or value.lower() in ["nan", "none", "null", "n/a"]:
        return fallback

    return value


def _best_pos_name(names, fallback):
    fallback = str(fallback)
    for name in names:
        clean_name = _clean_pos_name(name, fallback)
        if clean_name != fallback:
            return clean_name
    return fallback


def _safe_export_name(value, max_length=31):
    cleaned = "".join(
        char if char.isalnum() or char in [" ", "_", "-"] else "_"
        for char in str(value).strip()
    )
    cleaned = "_".join(cleaned.split())
    if not cleaned:
        cleaned = "Commercial"
    return cleaned[:max_length]


def _read_uploaded_files(files):
    frames = []

    for file in files:
        try:
            df = load_file(file)
            df["source_file"] = file.name
            frames.append(df)
        except Exception as exc:
            st.error(f"Erreur lecture {file.name}: {exc}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df.columns = [str(col).strip() for col in df.columns]
    return df


def _prepare_master(master_df):
    master = master_df.copy()
    master.columns = [str(col).strip() for col in master.columns]

    msisdn_col = _first_existing(master.columns, ["MSISDN", "Agent MSISDN", "Agent_MSISDN", "agent_msisdn"])
    if msisdn_col is None:
        st.error("Colonne MSISDN introuvable dans le fichier Maitre POS.")
        st.stop()

    master["POS_MSISDN"] = master[msisdn_col].apply(clean_phone)
    master = master[master["POS_MSISDN"].notna()].copy()

    field_candidates = {
        "Locality": ["Locality", "SA Name", "sitename"],
        "Cluster": ["Cluster"],
        "Territory": ["Territory"],
        "Zone": ["Zone"],
        "Segment Group": ["Segment Group","segment_group","Segment_Group"],
    }

    normalized = pd.DataFrame({"POS_MSISDN": master["POS_MSISDN"]})
    for output_col, candidates in field_candidates.items():
        source_col = _first_existing(master.columns, candidates)
        normalized[output_col] = master[source_col] if source_col else "N/A"

    keep_cols = [
        "POS_MSISDN",
        "Locality",
        "Cluster",
        "Territory",
        "Zone",
        "Segment Group",
    ]
    return normalized[keep_cols].drop_duplicates(subset=["POS_MSISDN"])


def _prepare_commercials(comm_config):
    comm = comm_config.copy()
    comm.columns = [str(col).strip() for col in comm.columns]

    required = ["Ccial_MSISDN", "Nom_Ccial"]
    missing = [col for col in required if col not in comm.columns]
    if missing:
        st.error(f"Colonne(s) manquante(s) dans Configuration Commerciaux: {', '.join(missing)}")
        st.stop()

    comm["Commercial_MSISDN"] = comm["Ccial_MSISDN"].apply(clean_phone)
    comm = comm[comm["Commercial_MSISDN"].notna()].copy()

    for col in ["Zone_Territoire", "Zone_SA"]:
        if col not in comm.columns:
            comm[col] = "N/A"

    return comm[
        ["Commercial_MSISDN", "Nom_Ccial", "Zone_Territoire", "Zone_SA"]
    ].drop_duplicates(subset=["Commercial_MSISDN"])


def _style_assignments(df):
    def color_count(value):
        try:
            if int(value) >= 10:
                return "background-color: #d8f3dc; color: #1b4332;"
            return "background-color: #fff3bf; color: #5f4700;"
        except Exception:
            return ""

    return (
        df.style
        .applymap(color_count, subset=["Nb transactions"])
        .set_table_styles([
            {
                "selector": "th",
                "props": [
                    ("background-color", "#111827"),
                    ("color", "#f9fafb"),
                    ("font-weight", "bold"),
                    ("text-align", "center"),
                ],
            },
            {
                "selector": "td",
                "props": [
                    ("text-align", "center"),
                    ("border", "1px solid #e5e7eb"),
                ],
            },
        ])
    )


def _export_images_by_commercial(display_df):
    if display_df.empty:
        st.warning("Aucune donnee a capturer.")
        return

    export_folder = "exports_pos_from_commerciaux"
    os.makedirs(export_folder, exist_ok=True)

    for file_name in os.listdir(export_folder):
        file_path = os.path.join(export_folder, file_name)
        if os.path.isfile(file_path):
            os.remove(file_path)

    for commercial, group in display_df.groupby("Commercial", dropna=False):
        commercial_name = _safe_export_name(commercial, max_length=80)
        export_df = group.copy()
        file_name = f"{commercial_name}.png"
        file_path = os.path.join(export_folder, file_name)
        dfi.export(_style_assignments(export_df), file_path, table_conversion="chrome")

    zip_path = os.path.join(export_folder, "POS_attribues_commerciaux.zip")
    with zipfile.ZipFile(zip_path, "w") as zip_file:
        for file_name in os.listdir(export_folder):
            if file_name.endswith(".png"):
                zip_file.write(os.path.join(export_folder, file_name), arcname=file_name)

    with open(zip_path, "rb") as zip_file:
        st.download_button(
            "Telecharger images par commercial (ZIP)",
            zip_file,
            "POS_attribues_commerciaux.zip",
            "application/zip",
        )


def _export_excel_by_commercial(display_df):
    output = BytesIO()
    used_sheet_names = set()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for commercial, group in display_df.groupby("Commercial", dropna=False):
            base_sheet_name = _safe_export_name(commercial, max_length=31)
            sheet_name = base_sheet_name
            suffix = 1
            while sheet_name in used_sheet_names:
                suffix += 1
                sheet_name = f"{base_sheet_name[:28]}_{suffix}"
            used_sheet_names.add(sheet_name)

            export_df = group.copy()
            export_df.to_excel(writer, index=False, sheet_name=sheet_name)

    return output.getvalue()


def _export_csv_zip_by_commercial(display_df):
    output = BytesIO()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for commercial, group in display_df.groupby("Commercial", dropna=False):
            commercial_name = _safe_export_name(commercial, max_length=80)
            export_df = group.copy()
            zip_file.writestr(
                f"{commercial_name}.csv",
                export_df.to_csv(index=False).encode("utf-8-sig"),
            )

    return output.getvalue()


def show_pos_from_commerciaux():
    st.title("POS attribues aux commerciaux")

    master_III_df = load_setting("maitre_pos_III")
    comm_config = load_setting("commerciaux")
    master_df = load_setting("maitre_pos")

    if master_df is None:
        st.error("Veuillez charger le fichier Maitre POS dans Settings.")
        st.stop()

    if comm_config is None:
        st.error("Veuillez charger le fichier Configuration Commerciaux dans Settings.")
        st.stop()

    trans_files = st.file_uploader(
        "Upload fichiers de transactions des commerciaux",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pos_from_commerciaux_files",
    )

    if not trans_files:
        st.info("Veuillez uploader les fichiers de transactions.")
        st.stop()

    with st.spinner("Identification des POS attribues aux commerciaux..."):
        trans_df = _read_uploaded_files(trans_files)
        if trans_df.empty:
            st.warning("Aucun fichier valide.")
            st.stop()

        required_cols = ["From", "To"]
        missing = [col for col in required_cols if col not in trans_df.columns]
        if missing:
            st.error(f"Colonne(s) manquante(s) dans les transactions: {', '.join(missing)}")
            st.stop()

        trans_df["From_clean"] = trans_df["From"].apply(clean_phone)
        trans_df["To_clean"] = trans_df["To"].apply(clean_phone)

        if "Date" in trans_df.columns:
            trans_df["Date"] = pd.to_datetime(trans_df["Date"], errors="coerce")
            trans_df["Date_only"] = trans_df["Date"].dt.date

        if "Amount" in trans_df.columns:
            trans_df["Amount"] = pd.to_numeric(trans_df["Amount"], errors="coerce").fillna(0).abs()
        else:
            trans_df["Amount"] = 0

        duplicate_subset = [
            col for col in ["Date", "From_clean", "To_clean", "Amount", "Type"]
            if col in trans_df.columns
        ]
        if duplicate_subset:
            trans_df = trans_df.drop_duplicates(subset=duplicate_subset, keep="last")

        master = _prepare_master(master_df)
        commercials = _prepare_commercials(comm_config)

        trans_df = trans_df.merge(
            commercials,
            left_on="From_clean",
            right_on="Commercial_MSISDN",
            how="inner",
        )

        trans_df = trans_df.merge(
            master,
            left_on="To_clean",
            right_on="POS_MSISDN",
            how="inner",
        )

        to_name_col = _first_existing_case_insensitive(
            trans_df.columns,
            [
                "To Name",
                "To name",
                "To_Name",
                "to_name",
                "Receiver Name",
                "Receiver_Name",
                "Recipient Name",
                "Recipient_Name",
            ],
        )
        trans_df["Nom POS"] = (
            trans_df.apply(
                lambda row: _clean_pos_name(row[to_name_col], row["POS_MSISDN"]),
                axis=1,
            )
            if to_name_col else trans_df["POS_MSISDN"]
        )

        if trans_df.empty:
            st.warning("Aucune transaction Commercial -> POS trouvee avec les fichiers charges.")
            st.stop()

        if "Date_only" in trans_df.columns and trans_df["Date_only"].notna().any():
            min_date = trans_df["Date_only"].min()
            max_date = trans_df["Date_only"].max()
            date_range = st.sidebar.date_input(
                "Periode",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                key="pos_from_commerciaux_date",
            )
            if len(date_range) == 2:
                start_date, end_date = date_range
                trans_df = trans_df[
                    (trans_df["Date_only"] >= start_date) &
                    (trans_df["Date_only"] <= end_date)
                ].copy()

        segment_list = ["Tous"] + sorted(
            trans_df["Segment Group"].dropna().astype(str).unique().tolist()
        )
        selected_segment = st.sidebar.selectbox(
            "Filtre Segment",
            segment_list,
            key="pos_from_commerciaux_segment",
        )
        if selected_segment != "Tous":
            trans_df = trans_df[trans_df["Segment Group"].astype(str) == selected_segment].copy()

        commercial_list = ["Tous"] + sorted(
            trans_df["Nom_Ccial"].dropna().astype(str).unique().tolist()
        )
        selected_commercial = st.sidebar.selectbox(
            "Filtre Commercial",
            commercial_list,
            key="pos_from_commerciaux_commercial",
        )
        if selected_commercial != "Tous":
            trans_df = trans_df[trans_df["Nom_Ccial"].astype(str) == selected_commercial].copy()

        pos_names = (
            trans_df.groupby("POS_MSISDN")["Nom POS"]
            .apply(lambda values: _best_pos_name(values, values.name))
            .reset_index()
        )

        grouped = trans_df.groupby(
            [
                "Nom_Ccial",
                "Commercial_MSISDN",
                "Zone_Territoire",
                "Zone_SA",
                "POS_MSISDN",
                "Locality",
                "Cluster",
                "Territory",
                "Zone",
                "Segment Group",
            ],
            dropna=False,
        ).agg(
            **{
                "Nb transactions": ("POS_MSISDN", "size"),
                "Nb jours": ("Date_only", "nunique") if "Date_only" in trans_df.columns else ("source_file", "nunique"),
            }
        ).reset_index()

        grouped = grouped.merge(pos_names, on="POS_MSISDN", how="left")

        assignments = grouped[grouped["Nb transactions"] >= 3].copy()
        assignments = assignments.sort_values(
            ["POS_MSISDN", "Nb transactions", "Nom_Ccial"],
            ascending=[True, False, True],
        )
        assignments = assignments.drop_duplicates(subset=["POS_MSISDN"], keep="first")
        assignments = assignments.sort_values(
            ["Nom_Ccial", "Nb transactions"],
            ascending=[True, False],
        )

    if assignments.empty:
        st.warning("Aucun POS ne respecte le seuil de 3 transactions minimum.")
        st.stop()

    display_df = assignments.rename(columns={
        "Nom_Ccial": "Commercial",
        "Commercial_MSISDN": "Numero commercial",
        "POS_MSISDN": "Numero POS",
    })

    st.metric("POS attribues", display_df["Numero POS"].nunique())
    st.metric("Commerciaux identifies", display_df["Commercial"].nunique())

    summary_df = display_df.groupby(
        ["Commercial"],
        dropna=False,
    ).agg(
        **{
            "POS attribues": ("Numero POS", "nunique"),
            "Transactions": ("Nb transactions", "sum"),
        }
    ).reset_index().sort_values("POS attribues", ascending=False)

    final_columns = [
        "Commercial",
        "Numero POS",
        "Nom POS",
        "Locality",
        "Segment Group",
        "Nb transactions",
    ]
    display_df = display_df[[col for col in final_columns if col in display_df.columns]]

    st.subheader("Resume par commercial")
    st.dataframe(summary_df, use_container_width=True, height=300)

    st.subheader("Liste des POS attribues")
    st.dataframe(_style_assignments(display_df), use_container_width=True, height=650)

    if st.button("Capturer les tableaux par commercial"):
        _export_images_by_commercial(display_df)

    col_excel, col_csv = st.columns(2)
    col_excel.download_button(
        "Telecharger Excel par commercial",
        _export_excel_by_commercial(display_df),
        "POS_attribues_par_commercial.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    col_csv.download_button(
        "Telecharger CSV par commercial",
        _export_csv_zip_by_commercial(display_df),
        "POS_attribues_par_commercial_csv.zip",
        "application/zip",
    )
