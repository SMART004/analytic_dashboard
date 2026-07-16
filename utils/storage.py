# Storage file

import streamlit as st
import pandas as pd
import hashlib
import concurrent.futures
from io import BytesIO
from utils.helpers import load_file
from utils.supabase import supabase
import datetime
from utils.config_storage import (
    USE_LOCAL_STORAGE, DATA_PATH
)

# =====================================================
# HELPERS
# =====================================================

def file_hash(file):
    """
    Hash MD5 pour ÃƒÂ©viter les doublons exacts
    """
    # Lire le contenu brut
    content = file.read()
    file.seek(0)  # remettre le pointeur au dÃƒÂ©but pour ne pas bloquer l'upload
    return hashlib.md5(content).hexdigest()


def clean_filename(name):
    return name.replace(" ", "_").replace("/", "_")


# =====================================================
# CHECK DUPLICATE
# =====================================================

def file_already_exists(file, bucket=None):

    current_hash = file_hash(file)
    safe_name = clean_filename(file.name)

    # ==================================
    # LOCAL
    # ==================================
    if USE_LOCAL_STORAGE:

        for f in DATA_PATH.rglob("*"):

            if not f.is_file():
                continue

            if "__" not in f.name:
                continue

            existing_hash, existing_filename = f.name.split("__", 1)

            if (
                existing_hash == current_hash
                and existing_filename == safe_name
            ):
                return True

        return False

    # ==================================
    # SUPABASE
    # ==================================
    try:

        existing_files = supabase.storage.from_(bucket).list()

        for f in existing_files:

            existing_name = f.get("name", "")

            if "__" not in existing_name:
                continue

            existing_hash, existing_filename = existing_name.split("__", 1)

            if (
                existing_hash == current_hash
                and existing_filename == safe_name
            ):
                return True

        return False

    except Exception as e:

        st.error(str(e))
        return False

# =====================================================
# UPLOAD
# =====================================================

def upload_with_folder(files, bucket):
    """
    Upload plusieurs fichiers dans un dossier horodaté.
    Compatible Local + Supabase.
    """

    if not files:
        st.warning("Aucun fichier sélectionné")
        return None

    if not isinstance(files, list):
        files = [files]

    try:

        now = datetime.datetime.now()
        folder_name = now.strftime("%Y-%m-%d/%H-%M-%S")

        uploaded_files = []
        skipped_files = []

        for file in files:

            if file_already_exists(file, bucket):

                skipped_files.append(file.name)
                continue

            # ==========================
            # LOCAL
            # ==========================
            if USE_LOCAL_STORAGE:

                target_folder = (
                    DATA_PATH
                    / bucket
                    / folder_name
                )

                target_folder.mkdir(
                    parents=True,
                    exist_ok=True
                )

                filepath = target_folder / file.name

                with open(filepath, "wb") as f:
                    f.write(file.getvalue())

            # ==========================
            # SUPABASE
            # ==========================
            else:

                supabase.storage.from_(bucket).upload(
                    path=f"{folder_name}/{file.name}",
                    file=file.getvalue(),
                    file_options={
                        "content-type":
                        file.type or "application/octet-stream",
                        "upsert": "true"
                    }
                )

            uploaded_files.append(file.name)

        if uploaded_files:

            st.success(
                f"{len(uploaded_files)} fichier(s) uploadé(s)"
            )

        if skipped_files:

            st.warning(
                f"{len(skipped_files)} fichier(s) ignoré(s)"
            )

        return folder_name

    except Exception as e:

        st.error(
            f"Erreur upload : {str(e)}"
        )

        return None


def upload_file(uploaded_file, bucket):

    if uploaded_file is None:
        return False

    if file_already_exists(uploaded_file, bucket):
        st.warning(f"{uploaded_file.name} existe déjà")
        return False

    file_md5 = file_hash(uploaded_file)
    safe_name = clean_filename(uploaded_file.name)

    final_name = f"{file_md5}__{safe_name}"

    # ==================================
    # LOCAL
    # ==================================
    if USE_LOCAL_STORAGE:

        folder = DATA_PATH / bucket
        folder.mkdir(parents=True, exist_ok=True)

        filepath = folder / final_name

        with open(filepath, "wb") as f:
            f.write(uploaded_file.getvalue())

        return True

    # ==================================
    # SUPABASE
    # ==================================
    try:

        supabase.storage.from_(bucket).upload(
            final_name,
            uploaded_file.getvalue(),
            {
                "content-type": uploaded_file.type
            }
        )

        return True

    except Exception as e:

        st.error(str(e))
        return False

@st.cache_data(show_spinner=False)
def get_with_folder(bucket, file_path):

    try:

        # ==================================
        # LOCAL
        # ==================================
        if USE_LOCAL_STORAGE:

            local_file = DATA_PATH / bucket / file_path

            if not local_file.exists():

                st.error(
                    f"Fichier introuvable : {local_file}"
                )

                return None

            if local_file.suffix.lower() == ".csv":

                return pd.read_csv(local_file)

            return pd.read_excel(local_file)

        # ==================================
        # SUPABASE
        # ==================================
        file_bytes = (
            supabase.storage
            .from_(bucket)
            .download(file_path)
        )

        if file_path.lower().endswith(".csv"):

            return pd.read_csv(
                BytesIO(file_bytes)
            )

        return pd.read_excel(
            BytesIO(file_bytes)
        )

    except Exception as e:

        st.error(
            f"Erreur lecture fichier : {str(e)}"
        )

        return None

# =====================================================
# LOAD ALL FILES FROM BUCKET
# =====================================================
@st.cache_data(show_spinner=False, ttl=300)
def get_all_files(bucket: str) -> pd.DataFrame:
    """Charge tous les fichiers du bucket (local ou Supabase)."""
    all_dfs = []

    try:
        # ==================================
        # MODE LOCAL
        # ==================================
        if USE_LOCAL_STORAGE:
            bucket_folder = DATA_PATH / bucket

            if not bucket_folder.exists():
                st.warning(f"Dossier local non trouvé : {bucket_folder}")
                return pd.DataFrame()

            files = list(bucket_folder.rglob("*"))

            for file_path in files:
                if not file_path.is_file():
                    continue

                try:
                    # On crée un objet BytesIO avec .name pour compatibilité
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()

                    file_buffer = BytesIO(file_bytes)
                    file_buffer.name = file_path.name   # ← Important !

                    temp_df = load_file(file_buffer)

                    if temp_df is None or temp_df.empty:
                        continue

                    temp_df["source_file"] = file_path.name
                    temp_df["source_path"] = str(file_path)
                    all_dfs.append(temp_df)

                except Exception as e:
                    st.warning(f"Erreur lecture {file_path.name}: {e}")

        # ==================================
        # MODE SUPABASE
        # ==================================
        else:
            try:
                files = supabase.storage.from_(bucket).list()
            except Exception as e:
                st.error(f"Erreur listing Supabase : {e}")
                return pd.DataFrame()

            for f in files:
                file_name = f["name"]
                try:
                    downloaded = supabase.storage.from_(bucket).download(file_name)
                    file_buffer = BytesIO(downloaded)
                    file_buffer.name = file_name

                    temp_df = load_file(file_buffer)

                    if temp_df is None or temp_df.empty:
                        continue

                    temp_df["source_file"] = file_name
                    all_dfs.append(temp_df)

                except Exception as e:
                    st.warning(f"Erreur download {file_name}: {e}")

        # ==================================
        # FUSION FINALE
        # ==================================
        if not all_dfs:
            return pd.DataFrame()

        final_df = pd.concat(all_dfs, ignore_index=True)

        # Nettoyage colonnes
        final_df.columns = [str(c).strip() for c in final_df.columns]

        return final_df

    except Exception as e:
        st.error(f"Erreur générale get_all_files : {e}")
        return pd.DataFrame()
    

@st.cache_data(show_spinner=False)
def get_one_folder(bucket):

    try:

        all_dfs = []

        # ==================================
        # LOCAL
        # ==================================
        if USE_LOCAL_STORAGE:

            root = DATA_PATH / bucket

            if not root.exists():
                return pd.DataFrame()

            folders = [
                f.name
                for f in root.iterdir()
                if f.is_dir()
            ]

            if not folders:
                return pd.DataFrame()

            most_recent_folder = sorted(
                folders,
                reverse=True
            )[0]

            folder_path = (
                root
                / most_recent_folder
            )

            files = [
                f
                for f in folder_path.iterdir()
                if f.is_file()
            ]

            for file_path in files:

                try:

                    temp_df = load_file(
                        str(file_path)
                    )

                    if temp_df is None:
                        continue

                    temp_df["source_file"] = (
                        file_path.name
                    )

                    all_dfs.append(temp_df)

                except Exception as e:

                    st.warning(
                        f"Erreur {file_path.name}: {e}"
                    )

        # ==================================
        # SUPABASE
        # ==================================
        else:

            storage = supabase.storage.from_(bucket)

            folders = storage.list()

            valid_folders = [
                f["name"]
                for f in folders
            ]

            most_recent_folder = sorted(
                valid_folders,
                reverse=True
            )[0]

            folder_files = storage.list(
                path=most_recent_folder
            )

            for item in folder_files:

                file_path = (
                    f"{most_recent_folder}/"
                    f"{item['name']}"
                )

                downloaded = storage.download(
                    file_path
                )

                file_buffer = BytesIO(
                    downloaded
                )

                file_buffer.name = item["name"]

                temp_df = load_file(
                    file_buffer
                )

                if temp_df is None:
                    continue

                temp_df["source_file"] = (
                    item["name"]
                )

                all_dfs.append(temp_df)

        if not all_dfs:
            return pd.DataFrame()

        final_df = pd.concat(
            all_dfs,
            ignore_index=True
        )

        final_df.columns = [
            c.strip()
            for c in final_df.columns
        ]

        return final_df

    except Exception as e:

        st.error(
            f"Erreur : {str(e)}"
        )

        return pd.DataFrame()
    

################################## PERFORMANCE CC ###########
def extract_months_from_file(uploaded_file):
    """
    Retourne une liste d'objets datetime des mois prÃƒÂ©sents dans le fichier.
    Ãƒâ‚¬ adapter selon la structure de tes fichiers.
    """
    try:
        # Charger le fichier temporairement pour analyser les dates
        df = load_file(uploaded_file)  # ou pd.read_excel / pd.read_csv selon le cas

        if df is None or 'Date' not in df.columns:
            # Fallback : utiliser la date du jour
            return [datetime.date.today()]

        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        df = df.dropna(subset=['Date'])

        # Extraire les mois uniques
        unique_months = df['Date'].dt.to_period('M').unique()
        return [m.to_timestamp().date() for m in unique_months]

    except:
        # En cas d'erreur, on met le mois actuel
        return [datetime.date.today()]


def upload_file_by_month(uploaded_file, bucket):

    if uploaded_file is None:
        return False

    try:

        months = extract_months_from_file(
            uploaded_file
        )

        if not months:

            st.warning(
                f"Mois introuvable : "
                f"{uploaded_file.name}"
            )

            return False

        year = months[0].year

        month_numbers = sorted(
            [m.month for m in months]
        )

        if len(month_numbers) == 1:

            folder_name = (
                f"{year}-"
                f"{month_numbers[0]:02d}"
            )

        else:

            folder_name = (
                f"{year}-"
                + "-".join(
                    f"{m:02d}"
                    for m in month_numbers
                )
            )

        if file_already_exists(
            uploaded_file,
            bucket
        ):
            st.warning(
                f"{uploaded_file.name} existe déjà"
            )
            return False

        file_md5 = file_hash(
            uploaded_file
        )

        safe_name = clean_filename(
            uploaded_file.name
        )

        final_name = (
            f"{file_md5}__{safe_name}"
        )

        # ==========================
        # LOCAL
        # ==========================
        if USE_LOCAL_STORAGE:

            month_folder = (
                DATA_PATH
                / bucket
                / folder_name
            )

            month_folder.mkdir(
                parents=True,
                exist_ok=True
            )

            filepath = (
                month_folder
                / final_name
            )

            with open(filepath, "wb") as f:

                f.write(
                    uploaded_file.getvalue()
                )

        # ==========================
        # SUPABASE
        # ==========================
        else:

            final_path = (
                f"{folder_name}/"
                f"{final_name}"
            )

            supabase.storage.from_(bucket).upload(
                final_path,
                uploaded_file.getvalue(),
                {
                    "content-type":
                    uploaded_file.type
                    or "application/octet-stream"
                }
            )

        return True

    except Exception as e:

        st.error(
            f"Erreur upload : {str(e)}"
        )

        return False
    
@st.cache_data(ttl=120)
def list_month_folders(bucket=None):

    if USE_LOCAL_STORAGE:

        bucket_folder = DATA_PATH / bucket

        if not bucket_folder.exists():
            return []

        folders = sorted([
            f.name
            for f in bucket_folder.iterdir()
            if f.is_dir()
        ])

        return folders

    else:

        files = supabase.storage.from_(bucket).list()

        def is_likely_folder(name):

            if name.startswith("."):
                return False

            return "." not in name.split("/")[-1]

        return sorted([
            item["name"]
            for item in files
            if is_likely_folder(item["name"])
        ])
    
def get_month_files(bucket, selected_month):

    if USE_LOCAL_STORAGE:

        folder = DATA_PATH / bucket / selected_month

        if not folder.exists():
            return []

        return [
            {
                "name": file.name,
                "path": file
            }
            for file in folder.iterdir()
            if file.is_file()
        ]

    else:

        all_files = []

        offset = 0
        limit = 1000

        while True:

            files = supabase.storage.from_(bucket).list(
                path=selected_month,
                options={
                    "limit": limit,
                    "offset": offset
                }
            )

            if not files:
                break

            all_files.extend(files)

            offset += limit

            if len(files) < limit:
                break

        return all_files

def download_month_file(bucket, selected_month, file_info):

    if USE_LOCAL_STORAGE:

        file_path = file_info["path"]

        if file_path.suffix.lower() == ".csv":
            return pd.read_csv(file_path)

        return pd.read_excel(file_path)

    else:

        name = file_info["name"]

        full_path = f"{selected_month}/{name}"

        res = supabase.storage.from_(bucket).download(full_path)

        if full_path.lower().endswith(".csv"):
            return pd.read_csv(BytesIO(res))

        return pd.read_excel(BytesIO(res))

@st.cache_data(ttl=60)
def get_files_by_month(bucket, selected_month, max_workers=1):
    try:

        all_files = get_month_files(
            bucket,
            selected_month
        )

        if not all_files:

            st.warning(
                f"Aucun fichier dans {selected_month}"
            )
            return None

        st.info(f"📁 {len(all_files)} fichiers trouvés dans {selected_month}. Chargement en parallèle...")

        dfs = []
        errors = []

        def download_file(file_info):
            try:

                df = download_month_file(
                    bucket,
                    selected_month,
                    file_info
                )

                if df is None or df.empty:
                    return None

                df["source_file"] = file_info["name"]

                return df

            except Exception as e:

                return (
                    "ERROR",
                    file_info["name"],
                    str(e)
                )
            
        # Parallélisme avec ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(download_file, all_files))

        for result in results:
            if result is None:
                continue
            if isinstance(result, tuple) and result[0] == "ERROR":
                errors.append({
                    "file": result[1],
                    "error": result[2]
                })
            else:
                dfs.append(result)

        if dfs:
            final_df = pd.concat(dfs, ignore_index=True)
            final_df.columns = [c.strip() for c in final_df.columns]
            st.success(f"✅ {len(dfs)} fichiers chargés avec succès ({len(errors)} erreurs)")
            if errors:
                st.error(f"{len(errors)} fichiers en erreur")
                st.dataframe(
                    pd.DataFrame(errors),
                    use_container_width=True
                )
            return final_df
        else:
            st.error("Aucun fichier valide chargé")
            return None

    except Exception as e:
        st.error(f"Erreur générale : {e}")
        return None
