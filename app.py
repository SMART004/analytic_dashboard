# app.py
import streamlit as st
from streamlit_option_menu import option_menu

st.set_page_config(
    page_title="Outil d'Analyse Interne",
    layout="wide"
)

import sys
import asyncio

# Correctif Python 3.13 / Windows pour Playwright + asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Import des pages
from pages.dashboard import show_dashboard
from pages.pos_analytic import show_gros_transferts
from pages.perf import show_performance
from pages.pr_caisse_perf import show_performance_pos_caisse
from pages.cds_perf import show_performance_cds
from pages.settings import show_settings
from pages.listing_pos_oos import show_pos_oos_listing
from pages.pos_from_night import show_pos_nuit
from pages.pos_from_commerciaux import show_pos_from_commerciaux
from pages.conquete_territoire import show_conquete_territoire
from pages.variations_hvc import render_oos_variation

# Menu latéral
with st.sidebar:
    selected = option_menu(
        menu_title="Menu Principal",
        options=["Dashboard", "Conquête de Territoire", "Analyse Gros Transferts", "Analyse des POS non Touche", "Performance Commerciaux", "Performance POS relais & Caisses", "Performance CDS","POS par Commercial", "Listing OOS", "Variation OOS HVC","Settings"],
        icons=["house-fill", "trophy", "cash-stack", "box-seam", "graph-up-arrow", "graph-up-arrow", "graph-up-arrow", "people-fill", "box-seam", "graph-up-arrow", "gear-fill"],
        menu_icon="menu-button-wide",
        default_index=0,
        orientation="vertical"
    )
            

# Affichage de la page sélectionnée
if selected == "Dashboard":
    show_dashboard()
if selected == "Conquête de Territoire":
    show_conquete_territoire()
elif selected == "Analyse Gros Transferts":
    show_gros_transferts()
elif selected == "Analyse des POS non Touche":
    show_pos_nuit()
elif selected == "Performance Commerciaux":
    show_performance()
elif selected == "Performance POS relais & Caisses":
    show_performance_pos_caisse()
elif selected == "Performance CDS":
    show_performance_cds()
elif selected == "POS par Commercial":
    show_pos_from_commerciaux()
elif selected == "Listing OOS":
    show_pos_oos_listing()
elif selected == "Variation OOS HVC":
    render_oos_variation()
elif selected == "Settings":
    show_settings()
# elif selected == "Carte":
#     show_pos_map()
