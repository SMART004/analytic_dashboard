import streamlit as st
import pandas as pd
import hashlib
from io import BytesIO
from utils.helpers import load_file
from utils.supabase import supabase
import datetime


# =====================================================
# HELPERS
# =====================================================

def file_hash(file):
    """
    Hash MD5 pour éviter les doublons exacts
    """
    # Lire le contenu brut
    content = file.read()
    file.seek(0)  # remettre le pointeur au début pour ne pas bloquer l'upload
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

    try:
        existing_files = supabase.storage.from_(bucket).list()
    except Exception as e:
        st.error(f"Erreur lors de la récupération des fichiers : {str(e)}")
        return False

    if not existing_files:
        return False

    current_hash = file_hash(file)

    for f in existing_files:
        existing_name = f.get("name", "")

        # convention:
        # hash__filename.xlsx
        if "__" in existing_name:
            existing_hash, existing_filename = existing_name.split("__", 1)
            safe_name = clean_filename(file.name)

            if existing_hash == current_hash and existing_filename == safe_name:
                return True

    return False


# =====================================================
# UPLOAD
# =====================================================

def upload_with_folder(supabase, files, bucket):
    """
    Upload plusieurs fichiers dans UN SEUL dossier horodaté.
    """
    if not files:
        st.warning("Aucun fichier sélectionné")
        return False

    # Si un seul fichier est passé (pas dans une liste), on le transforme en liste
    if not isinstance(files, list):
        files = [files]

    try:
        storage = supabase.storage.from_(bucket)
        
        # =====================================================
        # Création d'UN SEUL dossier pour tout le batch
        # =====================================================
        now = datetime.datetime.now()
        folder_name = now.strftime("%Y-%m-%d_%H-%M-%S")   # ex: 2025-05-04_14-35-22
        uploaded_files = []
        skipped_files = []

        for file in files:
            # Vérification individuelle par fichier
            if file_already_exists(supabase, file, bucket):
                st.warning(f"⚠️ Le fichier '{file.name}' existe déjà.")
                skipped_files.append(file.name)
                continue

            file_bytes = file.getvalue()
            file_path = f"{folder_name}/{file.name}"

            # Upload
            storage.upload(
                path=file_path,
                file=file_bytes,
                file_options={
                    "content-type": file.type or "application/octet-stream",
                    "upsert": "true"
                }
            )
            uploaded_files.append(file.name)

        # Messages de synthèse
        if uploaded_files:
            st.success(f"""
                ✅ **{len(uploaded_files)} fichier(s)** uploadé(s) avec succès !
                📁 Dossier créé : **{folder_name}**
            """)
        
        if skipped_files:
            st.warning(f"{len(skipped_files)} fichier(s) ignoré(s) car déjà existant(s).")

        return folder_name if uploaded_files else None

    except Exception as e:
        st.error(f"Erreur lors de l'upload : {str(e)}")
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
    
@st.cache_data(show_spinner=False)
def get_one_folder(bucket):
    """
    Charge TOUS les fichiers du DOSSIER le plus récent 
    (dossier créé avec le format YYYY-MM-DD_HH-MM-SS)
    """
    try:
        # Récupérer tous les fichiers à la racine
        files_list = supabase.storage.from_(bucket).list()

        if not files_list:
            st.info("Aucun fichier ou dossier trouvé dans le bucket")
            return pd.DataFrame()

        files_df = pd.DataFrame(files_list)

        # Identifier les dossiers (ceux qui contiennent un '/')
        files_df['is_folder'] = files_df['name'].str.contains('/')
        files_df['folder_name'] = files_df['name'].apply(
            lambda x: x.split('/')[0] if '/' in x else None
        )

        # Garder uniquement les dossiers
        folders = files_df[files_df['folder_name'].notna()]['folder_name'].unique()

        if len(folders) == 0:
            st.warning("Aucun dossier trouvé. Chargement en mode fichier unique.")
            # Fallback : charger le fichier le plus récent à la racine
            return _load_most_recent_single_file(supabase, bucket)

        # Trier les dossiers par nom (le plus récent en premier grâce au timestamp)
        sorted_folders = sorted(folders, reverse=True)
        most_recent_folder = sorted_folders[0]

        st.info(f"📁 Dossier chargé : **{most_recent_folder}** (le plus récent)")

        # Lister les fichiers dans ce dossier
        folder_files = supabase.storage.from_(bucket).list(path=most_recent_folder)

        if not folder_files:
            st.warning(f"Aucun fichier trouvé dans le dossier {most_recent_folder}")
            return pd.DataFrame()

        all_dfs = []

        for f in folder_files:
            file_path = f"{most_recent_folder}/{f['name']}"

            downloaded = supabase.storage.from_(bucket).download(file_path)
            if not downloaded:
                continue

            file_buffer = BytesIO(downloaded)
            file_buffer.name = f['name']

            temp_df = load_file(file_buffer)

            if temp_df is not None and not temp_df.empty:
                temp_df.columns = [c.strip() for c in temp_df.columns]
                temp_df["source_file"] = f['name']
                all_dfs.append(temp_df)

        if not all_dfs:
            st.warning("Aucun fichier valide dans le dossier")
            return pd.DataFrame()

        final_df = pd.concat(all_dfs, ignore_index=True)
        
        st.success(f"""
            ✅ Dossier chargé avec succès : 
            **{len(final_df)} lignes** | **{len(all_dfs)} fichiers**
        """)

        return final_df

    except Exception as e:
        st.error(f"Erreur lors du chargement du dossier : {str(e)}")
        return pd.DataFrame()


# Fonction helper (fallback)
def _load_most_recent_single_file(supabase, bucket):
    """Fallback si aucun dossier n'est trouvé"""
    try:
        files = supabase.storage.from_(bucket).list()
        if not files:
            return pd.DataFrame()
        
        # Prendre le fichier le plus récent
        most_recent = max(files, key=lambda x: x.get('created_at', ''))
        file_name = most_recent['name']
        
        downloaded = supabase.storage.from_(bucket).download(file_name)
        file_buffer = BytesIO(downloaded)
        file_buffer.name = file_name
        
        df = load_file(file_buffer)
        if df is not None:
            df.columns = [c.strip() for c in df.columns]
            df["source_file"] = file_name
        return df
    except:
        return pd.DataFrame()
    
################################## PERFORMANCE CC ###########
def extract_month_year(file):
    try:
        # lecture rapide (optimisée)
        if file.name.endswith(".csv"):
            df = pd.read_csv(file, nrows=200)
        else:
            df = pd.read_excel(file, nrows=200)

        if 'Date' not in df.columns:
            return None

        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')

        min_date = df['Date'].min()

        if pd.isna(min_date):
            return None

        return min_date.strftime("%Y-%m")

    except Exception as e:
        st.error(f"Erreur lecture fichier pour détection date: {e}")
        return None
    

def upload_file_by_month(supabase, uploaded_file, bucket):
    """
    Upload avec organisation par mois + anti doublon
    """

    if uploaded_file is None:
        return False

    # 🔍 détecter dossier mois
    month_folder = extract_month_year(uploaded_file)

    if not month_folder:
        st.warning(f"Impossible de détecter le mois pour {uploaded_file.name}")
        return False

    # 🔐 anti doublon (dans tout le bucket)
    if file_already_exists(supabase, uploaded_file, bucket):
        st.warning(f"⚠️ '{uploaded_file.name}' existe déjà.")
        return False

    try:
        file_md5 = file_hash(uploaded_file)
        safe_name = clean_filename(uploaded_file.name)

        # 📁 chemin avec dossier
        final_path = f"{month_folder}/{file_md5}__{safe_name}"

        supabase.storage.from_(bucket).upload(
            final_path,
            uploaded_file.getvalue(),
            {"content-type": uploaded_file.type}
        )

        return True

    except Exception as e:
        st.error(f"Erreur upload Supabase : {str(e)}")
        return False
    
def list_month_folders(supabase, bucket):
    files = supabase.storage.from_(bucket).list()

    folders = set()

    for f in files:
        name = f.get("name", "")

        if "/" in name:
            folder = name.split("/")[0]
            folders.add(folder)

    return sorted(list(folders))

def get_files_by_month(supabase, bucket, selected_month):
    files = supabase.storage.from_(bucket).list()

    dfs = []

    for f in files:
        name = f.get("name", "")

        if not name.startswith(selected_month):
            continue

        file_path = name

        try:
            data = supabase.storage.from_(bucket).download(file_path)

            if file_path.endswith(".csv"):
                df = pd.read_csv(BytesIO(data))
            else:
                df = pd.read_excel(BytesIO(data))

            df["source_file"] = file_path
            dfs.append(df)

        except Exception as e:
            st.warning(f"Erreur lecture {file_path}: {e}")

    if dfs:
        return pd.concat(dfs, ignore_index=True)

    return pd.DataFrame()