# controllers/auth_controller.py
import bcrypt
import streamlit as st
from typing import Optional, Dict, Any
from models.user_model import get_user_by_username
from models.user_model import create_auth_session, delete_auth_session, get_user_by_session_token
from app_config.settings import PERMISSIONS, ROLE_VIEWER

def hash_password(password: str) -> str:
    """Hache un mot de passe avec bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
    """Vérifie un mot de passe contre son hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))

def login(username: str, password: str) -> bool:
    """Authentifie l'utilisateur et initialise la session."""
    user = get_user_by_username(username)
    if user and check_password(password, user["password_hash"]):
        st.session_state["authenticated"] = True
        st.session_state["user"] = {
            "username": user["username"],
            "role": user["role"]
        }
        st.query_params["auth"] = create_auth_session(user["username"])
        return True
    return False

def logout() -> None:
    """Déconnecte l'utilisateur."""
    st.session_state["authenticated"] = False
    st.session_state["user"] = None
    delete_auth_session(st.query_params.get("auth", ""))
    st.query_params.pop("auth", None)
    st.rerun()


def restore_session() -> bool:
    """Restaure l'authentification après un redémarrage du processus Streamlit."""
    if st.session_state.get("authenticated", False):
        return True
    user = get_user_by_session_token(st.query_params.get("auth", ""))
    if not user:
        return False
    st.session_state["authenticated"] = True
    st.session_state["user"] = {
        "username": user["username"],
        "role": user["role"],
    }
    return True

def get_current_user() -> Optional[Dict[str, Any]]:
    """Retourne l'utilisateur courant stocké en session."""
    return st.session_state.get("user")

def has_permission(feature: str) -> bool:
    """Vérifie si l'utilisateur courant a la permission pour une fonctionnalité."""
    user = get_current_user()
    if not user:
        return False
    user_role = user.get("role", ROLE_VIEWER)
    allowed_features = PERMISSIONS.get(user_role, [])
    return feature in allowed_features