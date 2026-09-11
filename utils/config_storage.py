# utils/config_storage.py

from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
	return os.getenv(name, str(default)).strip().lower() in {
		"1", "true", "yes", "on"
	}


# true en développement : SQLite local dans storage/dashboard.db.
# false en production : base distante Turso via TURSO_DATABASE_URL/TURSO_AUTH_TOKEN.
USE_LOCAL_STORAGE = _env_bool("USE_LOCAL_STORAGE", True)

# dossier racine local
LOCAL_STORAGE_PATH = Path("storage")

# settings
SETTINGS_PATH = LOCAL_STORAGE_PATH / "settings"

# données mensuelles
DATA_PATH = LOCAL_STORAGE_PATH / "data"