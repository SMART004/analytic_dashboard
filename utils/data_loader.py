from pathlib import Path
import pandas as pd
import streamlit as st

from utils.config_storage import DATA_PATH

@st.cache_data
def list_month_folders():

    return sorted([
        p.name
        for p in DATA_PATH.iterdir()
        if p.is_dir()
    ])

@st.cache_data
def get_files_by_month(selected_month):

    month_dir = DATA_PATH / selected_month

    files = list(month_dir.glob("*"))

    dfs = []

    for file_path in files:

        try:

            if file_path.suffix.lower() == ".csv":

                df = pd.read_csv(file_path)

            else:

                df = pd.read_excel(file_path)

            df["source_file"] = file_path.name

            dfs.append(df)

        except Exception as e:

            st.warning(f"{file_path.name} : {e}")

    if not dfs:
        return None

    return pd.concat(
        dfs,
        ignore_index=True
    )