# app.py
import streamlit as st
from streamlit_option_menu import option_menu

st.set_page_config(
    page_title="Outil d'Analyse Interne",
    layout="wide"
)

# Import des pages
from pages.dashboard import show_dashboard
from pages.pos_analytic import show_gros_transferts
from pages.perf import show_performance
from pages.settings import show_settings
from pages.pos_oos import show_pos_oos_listing
from pages.pos_from_night import show_pos_nuit

# Menu latéral
with st.sidebar:
    selected = option_menu(
        menu_title="Menu Principal",
        options=["Dashboard", "Analyse Gros Transferts", "Analyse des POS non Touche", "Performance Commerciaux", "Listing OOS", "Settings"],
        icons=["house-fill", "cash-stack", "box-seam", "graph-up-arrow", "box-seam", "gear-fill"],
        menu_icon="menu-button-wide",
        default_index=0,
        orientation="vertical"
    )

    # # ===================== FILTRE ZONE GLOBAL =====================
    # if st.session_state.get('zones_df') is not None and selected != "Settings":
    #     st.sidebar.markdown("---")
    #     st.sidebar.subheader("Filtre Zone Global")

    #     zones = st.session_state.zones_df
    #     zone_list = ["Toutes"] + sorted(zones['ZONE NEW'].dropna().unique().tolist())
        
    #     selected_zone = st.sidebar.selectbox(
    #         "ZONE", 
    #         zone_list, 
    #         key="global_zone_filter"
    #     )
        
    #     selected_terr = None
    #     if selected_zone != "Toutes" and 'TERRITORY CORRECT' in zones.columns:
    #         terr_list = ["Toutes"] + sorted(
    #             zones[zones['ZONE NEW'] == selected_zone]['TERRITORY CORRECT'].dropna().unique().tolist()
    #         )
    #         selected_terr = st.sidebar.selectbox(
    #             "TERRITORY", 
    #             terr_list, 
    #             key="global_terr_filter"
    #         )
    #     selected_isl = None
    #     if selected_terr is not None and selected_terr != "Toutes" and "ISL_Terr" in zones.columns:
    #         isl_list = ["Toutes"] + sorted(
    #             zones[zones['TERRITORY CORRECT'] == selected_terr]['ISL_Terr'].dropna().unique().tolist()
    #         )
    #         selected_isl = st.sidebar.selectbox(
    #             "Cluster", 
    #             isl_list, 
    #             key="global_isl_filter"
    #         )

    #     selected_site = None
    #     if selected_isl is not None and selected_isl != "Toutes" and "SITENAME" in zones.columns:
    #         site_list = ["Toutes"] + sorted(
    #             zones[zones['ISL_Terr'] == selected_isl]['SITENAME'].dropna().unique().tolist()
    #         )
    #         selected_site = st.sidebar.selectbox(
    #             "SITENAME",
    #             site_list,
    #             key="global_site_filter"
    #         )
            

# Affichage de la page sélectionnée
if selected == "Dashboard":
    show_dashboard()
elif selected == "Analyse Gros Transferts":
    show_gros_transferts()
elif selected == "Analyse des POS non Touche":
    show_pos_nuit()
elif selected == "Performance Commerciaux":
    show_performance()
elif selected == "Listing OOS":
    show_pos_oos_listing()
elif selected == "Settings":
    show_settings()
# elif selected == "Carte":
#     show_pos_map()