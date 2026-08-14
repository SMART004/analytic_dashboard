import streamlit as st
import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv
import os
import hashlib
from io import BytesIO
from utils.config_storage import USE_LOCAL_STORAGE, SETTINGS_PATH

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

# =====================================================
# SAVE FILE
# =====================================================

def save_file(file, folder_name):

    if USE_LOCAL_STORAGE:

        folder = SETTINGS_PATH / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        # supprimer anciens fichiers
        for old_file in folder.iterdir():
            if old_file.is_file():
                old_file.unlink()

        filepath = folder / file.name

        with open(filepath, "wb") as f:
            f.write(file.getbuffer())

        get_file_from_supabase.clear()
        return str(filepath)

    else:

        saved_path = upload_to_supabase(file, folder_name)
        get_file_from_supabase.clear()
        return saved_path


def load_setting(folder_name):

    if USE_LOCAL_STORAGE:

        folder = SETTINGS_PATH / folder_name

        if not folder.exists():
            return None

        files = list(folder.glob("*"))

        if not files:
            return None

        file_path = files[0]

        if file_path.suffix.lower() == ".csv":
            return pd.read_csv(file_path)

        return pd.read_excel(file_path)

    else:

        files = supabase.storage.from_(BUCKET_NAME).list(folder_name)

        if not files:
            return None

        file_name = files[0]["name"]

        return get_file_from_supabase(
            f"{folder_name}/{file_name}"
        )

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
    uploader_key,
    session_key,
    folder_name
):

    uploaded_file = st.file_uploader(
        uploader_label,
        key=uploader_key,
        type=["xlsx", "xls", "csv"]
    )

    uploaded = False

    if uploaded_file:
        upload_sig = hashlib.md5(uploaded_file.getvalue()).hexdigest()
        upload_state_key = f"{uploader_key}_last_uploaded_sig"

        if st.session_state.get(upload_state_key) != upload_sig:
            save_file(
                uploaded_file,
                folder_name
            )
            st.session_state[upload_state_key] = upload_sig
            uploaded = True

        st.session_state[session_key] = load_setting(
            folder_name
        )

        st.success("Fichier chargé")

    # rechargement automatique
    elif session_key not in st.session_state:

        df = load_setting(folder_name)

        if df is not None:
            st.session_state[session_key] = df

    return uploaded
