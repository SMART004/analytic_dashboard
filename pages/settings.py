import streamlit as st
from utils.helpers import load_file
import pandas as pd
from utils.supabase import handle_upload

def show_settings():
    st.title("⚙️ Settings - Configurations")
    st.markdown("**Chargez ici tous les fichiers et configurations de l'application**")

    tab1, tab2, tab3, tab4, tab6, tab7 = st.tabs([
        "Commerciaux", 
        "Caisses", 
        "Zones", 
        "Maître POS", 
        "Masters",
        "CDS"
    ])

    # ===================== ONGLET COMMERCIAUX =====================
    with tab1:
        st.subheader("Fichier Configuration Commerciaux")
        st.markdown("Colonnes attendues : `Zone_Territoire`, `Zone_SA`, `Nom_Ccial`, `Ccial_MSISDN`")
        
        handle_upload(
            tab_title="Fichier Commerciaux",
            uploader_label="Upload Fichier Commerciaux",
            uploader_key="comm_file_uploader", 
            session_key="commercial_config_df",
            folder_name="commerciaux"
        )

    # ===================== ONGLET CAISSES =====================
    with tab2:
        st.subheader("Fichier Configuration Caisses")
        st.markdown("Colonnes attendues : `NUM`, `CAISSE`")
        
        handle_upload(
            tab_title="Fichier Caisses",
            uploader_label="Upload Fichier Caisses",
            uploader_key="caisse_file_uploader", 
            session_key="exclusion_df",
            folder_name="caisses"
        )

    # ===================== ONGLET ZONES =====================
    with tab3:
        st.subheader("Fichier Zones Hiérarchique")
        st.markdown("Colonnes attendues : `ZONE NEW`, `TERRITORY CORRECT`, `ISL_Terr`, `SITENAME`")
         
        handle_upload(
            tab_title="Fichier Zones",
            uploader_label="Upload Fichier Zones",
            uploader_key="zone_file_uploader", 
            session_key="zones_df",
            folder_name="zones"
        )

    # ===================== ONGLET MAÎTRE POS =====================
    with tab4:
        st.subheader("Fichier Maître POS")
        st.markdown("Contient les informations des POS (Day_Target, OOS_Target, etc.)")
        
        handle_upload(
            tab_title="Fichier Maître POS",
            uploader_label="Upload Fichier Maître POS",
            uploader_key="maitre_file_uploader", 
            session_key="pos_master_df",
            folder_name="maitre_pos"
        )

    
    # ===================== ONGLET Masters =====================
    with tab6:
        st.subheader("Fichier Configuration Master")
        st.markdown("Colonnes attendues : `NUM`, `MASTER`")
        
        handle_upload(
            tab_title="Fichier Masters",
            uploader_label="Upload Fichier MASTER",
            uploader_key="master_file_uploader", 
            session_key="exclusion_master",
            folder_name="masters"
        )
    
    # ===================== ONGLET CDS =====================
    with tab7:
        st.subheader("Fichier Configuration CDS")
        st.markdown("Colonnes attendues : `NUM`, `CDS`")
        
        handle_upload(
            tab_title="Fichier CDS",
            uploader_label="Upload Fichier CDS",
            uploader_key="cds_file_uploader", 
            session_key="exclusion_cds",
            folder_name="cds"
        )

    st.divider()
    st.info("Tous les fichiers et configurations chargés ici sont disponibles dans les autres pages.")