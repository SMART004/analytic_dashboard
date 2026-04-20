import streamlit as st
from utils.helpers import load_file
import pandas as pd

def show_settings():
    st.title("⚙️ Settings - Configurations")
    st.markdown("**Chargez ici tous les fichiers et configurations de l'application**")

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Commerciaux", 
        "Caisses", 
        "Zones", 
        "Maître POS", 
        "Numéros de Dotation"
    ])

    # ===================== ONGLET COMMERCIAUX =====================
    with tab1:
        st.subheader("Fichier Configuration Commerciaux")
        st.markdown("Colonnes attendues : `Zone_Territoire`, `Zone_SA`, `Nom_Ccial`, `Ccial_MSISDN`")
        
        comm_file = st.file_uploader(
            "Upload Fichier Commerciaux",
            type=["xlsx", "xls", "csv"],
            key="comm_config"
        )
        if comm_file:
            df_comm = load_file(comm_file)
            st.session_state.commercial_config_df = df_comm
            st.success(f"Fichier Commerciaux chargé ({len(df_comm)} lignes)")
            st.dataframe(df_comm.head(10), use_container_width=True)

    # ===================== ONGLET CAISSES =====================
    with tab2:
        st.subheader("Fichier Configuration Caisses")
        st.markdown("Colonnes attendues : `NUM`, `CAISSE`")
        
        caisse_file = st.file_uploader(
            "Upload Fichier Caisses (Liste d'exclusion)",
            type=["xlsx", "xls", "csv"],
            key="caisse_config"
        )
        if caisse_file:
            df_caisse = load_file(caisse_file)
            st.session_state.exclusion_df = df_caisse
            st.success(f"Fichier Caisses chargé ({len(df_caisse)} lignes)")
            st.dataframe(df_caisse.head(10), use_container_width=True)

    # ===================== ONGLET ZONES =====================
    with tab3:
        st.subheader("Fichier Zones Hiérarchique")
        st.markdown("Colonnes attendues : `ZONE NEW`, `TERRITORY CORRECT`, `ISL_Terr`, `SITENAME`")
        
        zones_file = st.file_uploader(
            "Upload Fichier Zones",
            type=["xlsx", "xls", "csv"],
            key="zones_config"
        )
        if zones_file:
            df_zones = load_file(zones_file)
            st.session_state.zones_df = df_zones
            st.success(f"Fichier Zones chargé ({len(df_zones)} lignes)")
            st.dataframe(df_zones.head(10), use_container_width=True)

    # ===================== ONGLET MAÎTRE POS =====================
    with tab4:
        st.subheader("Fichier Maître POS")
        st.markdown("Contient les informations des POS (Day_Target, OOS_Target, etc.)")
        
        pos_file = st.file_uploader(
            "Upload Fichier Maître POS",
            type=["xlsx", "xls", "csv"],
            key="pos_master_config"
        )
        if pos_file:
            df_pos = load_file(pos_file)
            st.session_state.pos_master_df = df_pos
            st.success(f"Fichier Maître POS chargé ({len(df_pos)} lignes)")
            st.dataframe(df_pos.head(10), use_container_width=True)

    # ===================== ONGLET NUMÉROS DE DOTATION =====================
    with tab5:
        st.subheader("Configuration des Numéros de Dotation")
        st.markdown("""
        Ajoutez ici les numéros qui servent à **doter** les commerciaux.  
        Ces numéros seront utilisés pour calculer l'**Heure de dotation** et le **Montant de dotation**.
        """)

        # Input pour ajouter des numéros manuellement
        new_number = st.text_input("Ajouter un numéro de dotation (MSISDN)", placeholder="2376XXXXXXXX")
        
        col_add, col_clear = st.columns([3, 1])
        with col_add:
            if st.button("Ajouter ce numéro"):
                if new_number:
                    if 'dotation_numbers' not in st.session_state:
                        st.session_state.dotation_numbers = []
                    if new_number not in st.session_state.dotation_numbers:
                        st.session_state.dotation_numbers.append(new_number)
                        st.success(f"Numéro {new_number} ajouté")
                    else:
                        st.warning("Ce numéro est déjà dans la liste")
                else:
                    st.warning("Veuillez entrer un numéro")

        with col_clear:
            if st.button("Effacer tout"):
                st.session_state.dotation_numbers = []
                st.success("Liste effacée")

        # Affichage de la liste actuelle
        if 'dotation_numbers' in st.session_state and st.session_state.dotation_numbers:
            st.write("**Numéros de dotation configurés :**")
            dotation_df = pd.DataFrame({"Numéro de Dotation": st.session_state.dotation_numbers})
            st.dataframe(dotation_df, use_container_width=True)
        else:
            st.info("Aucun numéro de dotation configuré pour le moment.")

        st.caption("Ces numéros seront utilisés dans la page Performance pour calculer l'heure et le montant de dotation.")

    st.divider()
    st.info("Tous les fichiers et configurations chargés ici sont disponibles dans les autres pages.")