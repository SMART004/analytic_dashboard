import streamlit as st
from utils.helpers import load_file

def show_settings():
    st.title("⚙️ Settings - Fichiers Importants")

    st.markdown("**Chargez ici les fichiers qui seront utilisés dans toute l’application**")

    col1, col2 = st.columns(2)

    with col1:
        excl_file = st.file_uploader("Liste des personnes à EXCLURE (Gros Transferts)", 
                                     type=["xlsx","xls","csv"], key="excl")
        if excl_file:
            df = load_file(excl_file)
            st.session_state.exclusion_df = df
            st.success(f"Liste exclusion chargée ({len(df)} lignes)")

        zones_file = st.file_uploader("Fichier Zones (ZONE, TERRITORY CORRECT, ISL_TERR, SITENAME)", 
                                      type=["xlsx","xls","csv"], key="zones")
        if zones_file:
            df = load_file(zones_file)
            st.session_state.zones_df = df
            st.success(f"Fichier Zones chargé ({len(df)} lignes)")

    with col2:
        client_file = st.file_uploader("Fichier Mapping Clients (pour OOS)", 
                                       type=["xlsx","xls","csv"], key="clients")
        if client_file:
            df = load_file(client_file)
            st.session_state.client_mapping_df = df
            st.success(f"Mapping clients chargé ({len(df)} lignes)")

    st.info("Ces fichiers sont maintenant disponibles dans toutes les pages.")