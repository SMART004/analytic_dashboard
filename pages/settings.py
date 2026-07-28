# Settings page - Configurations
import streamlit as st
from utils.supabase import handle_upload

def show_settings():
    st.title("⚙️ Settings - Configurations")
    st.markdown("**Chargez ici tous les fichiers et configurations de l'application**")

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
        "Commerciaux", 
        "Caisses", 
        "Zones", 
        "Maître POS", 
        "Maitre POS centre III",
        "Masters",
        "CDS",
        "POS relai & Caisses",
        "Sites Etoudi",
        "HVC Commercial"
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
    # ===================== ONGLET CDS =====================
    with tab5:
        st.subheader("Fichier Maitre POS centre III")
        
        handle_upload(
            tab_title="Fichier Maitre POS centre III",
            uploader_label="Upload Fichier Maitre POS centre III",
            uploader_key="maitre_III_file_uploader", 
            session_key="pos_master_III_df",
            folder_name="maitre_pos_III"
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
    
    # ===================== ONGLET POS RELAIS & CAISSES =====================
    with tab8:
        st.subheader("Fichier Configuration POS Relais & Caisses")
        st.markdown("Colonnes attendues : `MSISDN_PR`, `TERRITOIRE`,`Localisation`, `Nom du point de relais`")
        
        handle_upload(
            tab_title="Fichier POS Relais & Caisses",
            uploader_label="Upload Fichier POS Relais & Caisses",
            uploader_key="pos_relay_caisse_file_uploader", 
            session_key="pos_relay_caisse_df",
            folder_name="pos_relay_caisse"
        )

    # ===================== ONGLET SITES ETOUDI =====================
    with tab9:
        st.subheader("Fichier Configuration SITES ETOUDI")
        st.markdown("Colonnes attendues : `dsm_name`, `sitename`, `quartier")
        
        handle_upload(
            tab_title="Fichier sites etoudi",
            uploader_label="Upload Fichier sites etoudi",
            uploader_key="sites_etoudi_file_uploader", 
            session_key="sites_etoudi_df",
            folder_name="sites_etoudi"
        )
    
    # ===================== ONGLET HVC COMMERCIAL =====================
    with tab10:
        st.subheader("Fichier Configuration HVC COMMERCIAL")
        st.markdown("Colonnes attendues : `Ccial en charge`, `HVC_msisdn`")
        
        handle_upload(
            tab_title="Fichier hvc commercial",
            uploader_label="Upload Fichier hvc commercial",
            uploader_key="hvc_commercial_file_uploader", 
            session_key="hvc_commercial_df",
            folder_name="hvc_commercial"
        )

    st.divider()
    st.info("Tous les fichiers et configurations chargés ici sont disponibles dans les autres pages.")