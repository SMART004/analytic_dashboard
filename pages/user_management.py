"""Administration des comptes et des rôles."""
import sqlite3

import streamlit as st

from app_config.settings import ROLE_ADMIN, ROLE_MANAGER, ROLE_VIEWER
from controllers.auth_controller import get_current_user, hash_password, has_permission
from models.user_model import (
    create_user,
    delete_user,
    init_user_table,
    list_users,
    update_user_password,
    update_user_role,
)

ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_VIEWER)
ROLE_LABELS = {
    ROLE_ADMIN: "Administrateur",
    ROLE_MANAGER: "Manager",
    ROLE_VIEWER: "Lecteur",
}


def _admin_count(users: list[dict]) -> int:
    return sum(user["role"] == ROLE_ADMIN for user in users)


def _can_remove_admin(users: list[dict], user: dict) -> bool:
    return user["role"] != ROLE_ADMIN or _admin_count(users) > 1


def show_user_management() -> None:
    """Affiche la gestion des utilisateurs, réservée aux administrateurs."""
    if not has_permission("user_management"):
        st.error("Accès réservé aux administrateurs.")
        return

    init_user_table()
    current_user = get_current_user() or {}
    users = list_users()

    st.title("Gestion des utilisateurs")
    st.caption("Créez les comptes et contrôlez les accès aux pages de l'application.")

    with st.expander("Créer un utilisateur", expanded=not users):
        with st.form("create_user_form", clear_on_submit=True):
            username = st.text_input("Nom d'utilisateur", key="new_username")
            password = st.text_input("Mot de passe", type="password", key="new_password")
            role = st.selectbox(
                "Rôle",
                ROLES,
                format_func=lambda value: ROLE_LABELS[value],
                key="new_role",
            )
            submitted = st.form_submit_button("Créer le compte", type="primary")

        if submitted:
            clean_username = username.strip()
            if not clean_username or not password:
                st.error("Le nom d'utilisateur et le mot de passe sont obligatoires.")
            elif len(password) < 8:
                st.error("Le mot de passe doit contenir au moins 8 caractères.")
            else:
                try:
                    create_user(clean_username, hash_password(password), role)
                except sqlite3.IntegrityError:
                    st.error("Ce nom d'utilisateur existe déjà.")
                else:
                    st.success(f"Le compte « {clean_username} » a été créé.")
                    st.rerun()

    st.subheader(f"Comptes ({len(users)})")
    if not users:
        st.info("Aucun utilisateur n'est encore enregistré.")
        return

    for user in users:
        is_current = user["username"] == current_user.get("username")
        label = f"{user['username']} · {ROLE_LABELS.get(user['role'], user['role'])}"
        with st.expander(label, expanded=False):
            st.caption(f"Créé le : {user['created_at'] or 'date inconnue'}")
            role_key = f"role_{user['id']}"
            with st.form(f"edit_user_{user['id']}"):
                selected_role = st.selectbox(
                    "Rôle",
                    ROLES,
                    index=ROLES.index(user["role"]) if user["role"] in ROLES else 0,
                    format_func=lambda value: ROLE_LABELS[value],
                    key=role_key,
                )
                new_password = st.text_input(
                    "Nouveau mot de passe (facultatif)",
                    type="password",
                    key=f"password_{user['id']}",
                )
                save = st.form_submit_button("Enregistrer les changements", type="primary")

            if save:
                if selected_role != user["role"] and not _can_remove_admin(users, user):
                    st.error("Impossible de retirer le rôle du dernier administrateur.")
                elif is_current and selected_role != ROLE_ADMIN:
                    st.error("Vous ne pouvez pas retirer votre propre rôle administrateur.")
                elif new_password and len(new_password) < 8:
                    st.error("Le nouveau mot de passe doit contenir au moins 8 caractères.")
                else:
                    if selected_role != user["role"]:
                        update_user_role(user["id"], selected_role)
                    if new_password:
                        update_user_password(user["id"], hash_password(new_password))
                    st.success("Utilisateur mis à jour.")
                    st.rerun()

            if is_current:
                st.info("Compte actuellement connecté : sa suppression est désactivée.")
            elif st.button("Supprimer ce compte", key=f"delete_{user['id']}", type="secondary"):
                if not _can_remove_admin(users, user):
                    st.error("Impossible de supprimer le dernier administrateur.")
                else:
                    delete_user(user["id"])
                    st.success("Utilisateur supprimé.")
                    st.rerun()
