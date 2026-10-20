# controllers/auth_controller.py
import bcrypt
import time
from datetime import datetime, timezone
import streamlit as st
from typing import Optional, Dict, Any
from models.user_model import (
    get_user_by_username,
    create_auth_session,
    delete_auth_session,
    get_user_by_session_token,
    clean_expired_sessions,
)
from app_config.settings import PERMISSIONS, ROLE_VIEWER, AUTH_SESSION_DURATION_HOURS

SESSION_TIMEOUT_SECONDS: float = AUTH_SESSION_DURATION_HOURS * 3600.0


def hash_password(password: str) -> str:
    """Hache un mot de passe avec bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def check_password(password: str, hashed_password: str) -> bool:
    """Vérifie un mot de passe contre son hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))


def login(username: str, password: str) -> bool:
    """Authentifie l'utilisateur et initialise la session avec un délai de 24h."""
    clean_expired_sessions()
    user = get_user_by_username(username)
    if user and check_password(password, user["password_hash"]):
        now_ts = time.time()
        st.session_state["authenticated"] = True
        st.session_state["user"] = {
            "username": user["username"],
            "role": user["role"]
        }
        st.session_state["session_expires_at"] = now_ts + SESSION_TIMEOUT_SECONDS
        st.session_state["login_time"] = now_ts

        # Jeton persistant (24h)
        token = create_auth_session(user["username"], hours=AUTH_SESSION_DURATION_HOURS)
        st.query_params["auth"] = token
        return True
    return False


def logout(expired: bool = False) -> None:
    """Déconnecte l'utilisateur et révoque sa session."""
    token = st.query_params.get("auth", "")
    if token:
        delete_auth_session(token)
        st.query_params.pop("auth", None)

    st.session_state["authenticated"] = False
    st.session_state["user"] = None
    st.session_state.pop("session_expires_at", None)
    st.session_state.pop("login_time", None)

    if expired:
        st.session_state["session_expired_message"] = (
            "⚠️ Votre session a expiré après 24 heures. Veuillez vous reconnecter."
        )

    st.rerun()


def restore_session() -> bool:
    """Restaure l'authentification et vérifie si le délai de 24h est dépassé."""
    # 1. Vérification session mémoire Streamlit
    if st.session_state.get("authenticated", False):
        expires_at = st.session_state.get("session_expires_at")
        if expires_at is not None and time.time() > float(expires_at):
            logout(expired=True)
            return False
        return True

    # 2. Vérification session persistante (query params / jeton SQLite)
    auth_token = st.query_params.get("auth", "")
    if not auth_token:
        return False

    user = get_user_by_session_token(auth_token)
    if not user:
        # Jeton introuvable ou déjà expiré dans la table auth_sessions
        logout(expired=True)
        return False

    # Calculer le temps restant par rapport à l'heure d'expiration du jeton
    expires_at_iso = user.get("expires_at")
    remaining_seconds = SESSION_TIMEOUT_SECONDS
    if expires_at_iso:
        try:
            exp_dt = datetime.fromisoformat(expires_at_iso)
            now_dt = datetime.now(timezone.utc)
            diff = (exp_dt - now_dt).total_seconds()
            if diff <= 0:
                logout(expired=True)
                return False
            remaining_seconds = min(diff, SESSION_TIMEOUT_SECONDS)
        except Exception:
            remaining_seconds = SESSION_TIMEOUT_SECONDS

    st.session_state["authenticated"] = True
    st.session_state["user"] = {
        "username": user["username"],
        "role": user["role"],
    }
    st.session_state["session_expires_at"] = time.time() + remaining_seconds
    st.session_state["login_time"] = time.time() - (SESSION_TIMEOUT_SECONDS - remaining_seconds)
    return True


def get_current_user() -> Optional[Dict[str, Any]]:
    """Retourne l'utilisateur courant stocké en session après vérification du délai."""
    if not restore_session():
        return None
    return st.session_state.get("user")


def has_permission(feature: str) -> bool:
    """Vérifie si l'utilisateur courant a la permission pour une fonctionnalité."""
    user = get_current_user()
    if not user:
        return False
    user_role = user.get("role", ROLE_VIEWER)
    allowed_features = PERMISSIONS.get(user_role, [])
    return feature in allowed_features