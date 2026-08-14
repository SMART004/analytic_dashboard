# views/auth_view.py
import streamlit as st
from controllers.auth_controller import login, logout, get_current_user, has_permission

def render_login_page():
    st.title("🔒 Connexion à l'application")
    
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
        if st.sidebar.button("Déconnexion"):
            logout()