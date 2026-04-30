import streamlit as st
import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv
import os
from utils.helpers import load_file
from io import BytesIO

load_dotenv()

# =========================================================
# CONFIG SUPABASE
# =========================================================

SUPABSE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

supabase: Client = create_client(SUPABSE_URL, SUPABASE_KEY)

# Bucket déjà créé dans Supabase Storage
BUCKET_NAME = "settings-files"


# =========================================================
# HELPERS
# =========================================================


def upload_to_supabase(file, folder_name):
    try:
        file_bytes = file.getvalue()
        storage = supabase.storage.from_(BUCKET_NAME)

        # chemin final
        file_path = f"{folder_name}/{file.name}"

        # =====================================================
        # 1. Vérifier uniquement les fichiers du dossier ciblé
        # =====================================================
        existing_files = storage.list(folder_name)

        files_to_delete = []

        if existing_files:
            for old_file in existing_files:
                old_name = old_file.get("name")

                if old_name:
                    old_full_path = f"{folder_name}/{old_name}"
                    files_to_delete.append(old_full_path)

        # =====================================================
        # 2. Supprimer uniquement les anciens fichiers
        #    de CE dossier
        # =====================================================
        if files_to_delete:
            storage.remove(files_to_delete)

            st.info(
                f"{len(files_to_delete)} ancien(s) fichier(s) supprimé(s) dans '{folder_name}'"
            )

        # =====================================================
        # 3. Upload du nouveau fichier
        # =====================================================
        storage.upload(
            path=file_path,
            file=file_bytes,
            file_options={
                "content-type": file.type,
                "upsert": "true"
            }
        )

        st.success(
            f"Nouveau fichier uploadé dans '{folder_name}' avec succès"
        )

        return file_path

    except Exception as e:
        st.error(f"Erreur upload Supabase : {str(e)}")
        return None

@st.cache_data(show_spinner=False)
def get_file_from_supabase(file_path):
    """
    Télécharge le fichier depuis Supabase et retourne un DataFrame
    """
    try:
        file_bytes = supabase.storage.from_(BUCKET_NAME).download(file_path)

        if file_path.endswith(".csv"):
            return pd.read_csv(BytesIO(file_bytes))
        else:
            return pd.read_excel(BytesIO(file_bytes))

    except Exception as e:
        st.error(f"Erreur lecture Supabase : {str(e)}")
        return None


def handle_upload(
    tab_title,
    uploader_label,
    uploader_key,   # clé widget
    session_key,    # clé dataframe
    folder_name
):
    
    st.subheader(tab_title)

    # =====================================================
    # 1. RECHERCHE AUTOMATIQUE DU FICHIER DANS SUPABASE
    # =====================================================
    storage = supabase.storage.from_(BUCKET_NAME)
    existing_files = storage.list(folder_name)

    if existing_files:
        get_file_from_supabase.clear()
        
        latest_file = existing_files[0]["name"]
        file_path = f"{folder_name}/{latest_file}"

        if session_key not in st.session_state:
            df_saved = get_file_from_supabase(file_path)

            if df_saved is not None:
                st.session_state[session_key] = df_saved
                st.session_state[f"{session_key}_path"] = file_path

    # =====================================================
    # 2. AFFICHAGE DU DATAFRAME SAUVEGARDÉ
    # =====================================================
    if session_key in st.session_state:
        df_existing = st.session_state[session_key]

        st.success(
            f"Fichier déjà enregistré ({len(df_existing)} lignes)"
        )

        st.write(
            f"📁 Source : {st.session_state.get(f'{session_key}_path', 'Supabase')}"
        )

        st.dataframe(
            df_existing.head(10),
            use_container_width=True
        )

        st.divider()

    uploaded_file = st.file_uploader(
        uploader_label,
        type=["xlsx", "xls", "csv"],
        key=uploader_key
    )

    if uploaded_file:

        # upload Supabase
        file_path = upload_to_supabase(
            uploaded_file,
            folder_name
        )

        if file_path:
            df = load_file(uploaded_file)

            # stockage dataframe
            st.session_state[session_key] = df

            # stockage path Supabase
            st.session_state[f"{session_key}_path"] = file_path

            st.success(
                f"{tab_title} chargé avec succès ({len(df)} lignes)"
            )

            st.write(f"📁 Stocké dans Supabase : {file_path}")

            st.dataframe(
                df.head(10),
                use_container_width=True
            )