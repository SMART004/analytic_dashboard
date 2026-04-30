import streamlit as st
import pandas as pd
import hashlib
from io import BytesIO
from utils.helpers import load_file
from utils.supabase import supabase


# =====================================================
# HELPERS
# =====================================================

def file_hash(file):
    """
    Hash MD5 pour éviter les doublons exacts
    """
    content = file.getvalue()
    return hashlib.md5(content).hexdigest()


def clean_filename(name):
    return name.replace(" ", "_").replace("/", "_")


# =====================================================
# CHECK DUPLICATE
# =====================================================

def file_already_exists(supabase, file, bucket):
    """
    Vérifie si un fichier identique existe déjà
    (nom + hash)
    """

    existing_files = supabase.storage.from_(bucket).list()

    if not existing_files:
        return False

    current_hash = file_hash(file)

    for f in existing_files:
        existing_name = f.get("name", "")

        # convention:
        # hash__filename.xlsx
        if "__" in existing_name:
            existing_hash = existing_name.split("__")[0]

            if existing_hash == current_hash:
                return True

    return False


# =====================================================
# UPLOAD
# =====================================================

def upload_with_folder(supabase, file, folder_name, bucket):
    if file is None:
        return False

    if file_already_exists(supabase, file, bucket):
        st.warning(
            f"⚠️ Le fichier '{file.name}' existe déjà."
        )
        return False
    
    try:
        file_bytes = file.getvalue()
        storage = supabase.storage.from_(bucket)

        # chemin final
        file_path = f"{folder_name}/{file.name}"

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



def upload_file(supabase, uploaded_file, bucket):
    """
    Upload avec protection anti doublon
    """

    if uploaded_file is None:
        return False

    if file_already_exists(supabase, uploaded_file, bucket):
        st.warning(
            f"⚠️ Le fichier '{uploaded_file.name}' existe déjà."
        )
        return False

    try:
        file_md5 = file_hash(uploaded_file)
        safe_name = clean_filename(uploaded_file.name)

        final_name = f"{file_md5}__{safe_name}"

        supabase.storage.from_(bucket).upload(
            final_name,
            uploaded_file.getvalue(),
            {"content-type": uploaded_file.type}
        )

        return True

    except Exception as e:
        st.error(
            f"Erreur upload Supabase : {str(e)}"
        )
        return False

@st.cache_data(show_spinner=False)
def get_with_folder(bucket,file_path):
    """
    Télécharge le fichier depuis Supabase et retourne un DataFrame
    """
    try:
        file_bytes = supabase.storage.from_(bucket).download(file_path)

        if file_path.endswith(".csv"):
            return pd.read_csv(BytesIO(file_bytes))
        else:
            return pd.read_excel(BytesIO(file_bytes))

    except Exception as e:
        st.error(f"Erreur lecture Supabase : {str(e)}")
        return None

# =====================================================
# LOAD ALL FILES FROM BUCKET
# =====================================================
@st.cache_data(show_spinner=False)
def get_all_files(bucket):
    """
    Charge TOUS les fichiers du bucket
    puis les combine pour le traitement
    """

    try:
        files = supabase.storage.from_(bucket).list()

        if not files:
            st.info("Aucun fichier trouvé")
            return pd.DataFrame()

        all_dfs = []

        for f in files:
            file_name = f["name"]

            downloaded = supabase.storage.from_(bucket).download(file_name)

            if not downloaded:
                continue

            file_buffer = BytesIO(downloaded)

            file_buffer.name = file_name

            temp_df = load_file(file_buffer)

            temp_df["source_file"] = file_name

            all_dfs.append(temp_df)

        if not all_dfs:
            return pd.DataFrame()

        final_df = pd.concat(
            all_dfs,
            ignore_index=True
        )

        final_df.columns = [
            c.strip() for c in final_df.columns
        ]

        return final_df

    except Exception as e:
        st.error(
            f"Erreur chargement fichiers : {str(e)}"
        )
        return pd.DataFrame()