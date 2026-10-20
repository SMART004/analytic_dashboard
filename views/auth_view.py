# views/auth_view.py
import time
import streamlit as st
from controllers.auth_controller import login, logout, get_current_user, has_permission

def render_login_page():
    st.title("🔒 Connexion à l'application")

    # Affiche un avertissement si l'utilisateur a été déconnecté pour expiration (>24h)
    expired_msg = st.session_state.pop("session_expired_message", None)
    if expired_msg:
        st.warning(expired_msg)

    with st.form("login_form"):
        username = st.text_input("Nom d'utilisateur")
        password = st.text_input("Mot de passe", type="password")
        submit = st.form_submit_button("Se connecter")
        
        if submit:
            if login(username, password):
                st.success("Connexion réussie !")
                st.rerun()
            else:
                st.error("Nom d'utilisateur ou mot de passe incorrect.")

def render_user_bar():
    """Affiche une barre d'information utilisateur dans la barre latérale."""
    user = get_current_user()
    if user:
        st.sidebar.markdown(f"👤 **{user['username']}** (`{user['role']}`)")
        expires_at = st.session_state.get("session_expires_at")
        if expires_at:
            remaining_mins = int((float(expires_at) - time.time()) / 60)
            if remaining_mins > 60:
                h = remaining_mins // 60
                m = remaining_mins % 60
                st.sidebar.caption(f"⏱️ Session active : reste {h}h{m:02d}")
            elif remaining_mins > 0:
                st.sidebar.caption(f"⏱️ Session active : reste {remaining_mins} min")
            else:
                st.sidebar.caption("⏱️ Session expirée")
        if st.sidebar.button("Déconnexion"):
            logout()