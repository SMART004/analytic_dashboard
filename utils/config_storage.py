# utils/config_storage.py

from pathlib import Path

# =====================================================
# MODE DE STOCKAGE
# =====================================================

USE_LOCAL_STORAGE = True

# dossier racine local
LOCAL_STORAGE_PATH = Path("storage")

# settings
SETTINGS_PATH = LOCAL_STORAGE_PATH / "settings"

# données mensuelles
DATA_PATH = LOCAL_STORAGE_PATH / "data"