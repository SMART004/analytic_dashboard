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
from pages.pos_caisse_perf import show_performance_pos_caisse
from pages.settings import show_settings
from pages.pos_oos import show_pos_oos_listing
from pages.pos_from_night import show_pos_nuit
from pages.pos_from_commerciaux import show_pos_from_commerciaux

# Menu latéral
with st.sidebar:
    selected = option_menu(
        menu_title="Menu Principal",
        options=["Dashboard", "Analyse Gros Transferts", "Analyse des POS non Touche", "Performance Commerciaux", "Performance POS relais & Caisses", "POS par Commercial", "Listing OOS", "Settings"],
        icons=["house-fill", "cash-stack", "box-seam", "graph-up-arrow", "graph-up-arrow", "people-fill", "box-seam", "gear-fill"],
        menu_icon="menu-button-wide",
        default_index=0,
        orientation="vertical"
    )
            

# Affichage de la page sélectionnée
if selected == "Dashboard":
    show_dashboard()
elif selected == "Analyse Gros Transferts":
    show_gros_transferts()
elif selected == "Analyse des POS non Touche":
    show_pos_nuit()
elif selected == "Performance Commerciaux":
    show_performance()
elif selected == "Performance POS relais & Caisses":
    show_performance_pos_caisse()
elif selected == "POS par Commercial":
    show_pos_from_commerciaux()
elif selected == "Listing OOS":
    show_pos_oos_listing()
elif selected == "Settings":
    show_settings()
# elif selected == "Carte":
#     show_pos_map()
