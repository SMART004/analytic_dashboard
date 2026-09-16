# utils/config_storage.py

from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()


def get_config_value(name: str, default: str | None = None) -> str | None:
	"""Lit une configuration depuis l'environnement ou les secrets Streamlit."""
	value = os.getenv(name)
	if value is not None:
		return value

	try:
		import streamlit as st
		value = st.secrets.get(name)
	except (ImportError, FileNotFoundError, KeyError, RuntimeError):
		value = None

	return str(value) if value is not None else default


def _env_bool(name: str, default: bool) -> bool:
	return (get_config_value(name, str(default)) or str(default)).strip().lower() in {
		"1", "true", "yes", "on"
	}


_has_remote_database = bool(
	get_config_value("LIBSQL_URL") or get_config_value("TURSO_DATABASE_URL")
)

# SQLite local par défaut en développement ; Turso est sélectionné
# automatiquement lorsque ses secrets sont présents sur Streamlit Cloud.
USE_LOCAL_STORAGE = _env_bool("USE_LOCAL_STORAGE", not _has_remote_database)

# dossier racine local
LOCAL_STORAGE_PATH = Path("storage")

# settings
SETTINGS_PATH = LOCAL_STORAGE_PATH / "settings"

# données mensuelles
DATA_PATH = LOCAL_STORAGE_PATH / "data"