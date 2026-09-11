# app.py
import asyncio
import sys
import streamlit as st
from streamlit_option_menu import option_menu

st.set_page_config(
    page_title="Outil d'Analyse Interne",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Masquer visuellement le composant de navigation natif (Multi-page MPA)
st.html("""
    <style>
        [data-testid="stSidebarNav"] {
            display: none;
        }
    </style>
""")

# Correctif Python 3.13 / Windows pour Playwright + asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Auth & RBAC
from controllers.auth_controller import get_current_user, has_permission, restore_session
from models.user_model import init_user_table
from views.auth_view import render_login_page, render_user_bar

# Import des pages
from views.conquete_view import render_conquete_hub as show_conquete_territoire
from pages.dashboard import show_dashboard
from pages.listing_pos_oos import show_pos_oos_listing
from pages.performance import show_performance_hub
from pages.pos_analytic import show_gros_transferts
from pages.pos_from_commerciaux import show_pos_from_commerciaux
from pages.pos_non_touches import show_pos_non_touches
from pages.settings import show_settings
from pages.user_management import show_user_management
from pages.variations_hvc import render_oos_variation
from pages.oos import show_oos_hvc
from pages.relationship_map import show_relationship_map

# ── 1. Initialisation de la table Utilisateurs (si inexistante) ────────────────
init_user_table()

# ── 2. Écran de connexion (si non authentifié) ─────────────────────────────────
if not restore_session():
    render_login_page()
    st.stop()

# ── 3. Configuration du Menu et Filtrage RBAC ───────────────────────────────────
# Mapping complet des options, icônes et permissions associées
ALL_NAVIGATION_ITEMS = [
    {
        "label": "Dashboard",
        "icon": "house-fill",
        "permission": "dashboard",
        "handler": show_dashboard,
    },
    {
        "label": "Conquête de Territoire",
        "icon": "trophy",
        "permission": "conquete_territoire",
        "handler": show_conquete_territoire,
    },
    {
        "label": "Analyse Gros Transferts",
        "icon": "cash-stack",
        "permission": "gros_transferts",
        "handler": show_gros_transferts,
    },
    {
        "label": "Analyse des POS non Touche",
        "icon": "box-seam",
        "permission": "pos_non_touches",
        "handler": show_pos_non_touches,
    },
    {
        "label": "Performance Terrain",
        "icon": "graph-up-arrow",
        "permission": "performance",
        "handler": show_performance_hub,
    },
    {
        "label": "POS par Commercial",
        "icon": "people-fill",
        "permission": "pos_commercial",
        "handler": show_pos_from_commerciaux,
    },
    {
        "label": "OOS",
        "icon": "box-seam",
        "permission": "oos_analysis",
        "handler": show_oos_hvc,
    },
    {
        "label": "Cartographie relationnelle",
        "icon": "diagram-3",
        "permission": "relationship_map",
        "handler": show_relationship_map,
    },
    {
        "label": "Listing OOS",
        "icon": "box-seam",
        "permission": "oos_analysis",
        "handler": show_pos_oos_listing,
    },
    {
        "label": "Variation OOS HVC",
        "icon": "graph-up-arrow",
        "permission": "oos_analysis",
        "handler": render_oos_variation,
    },
    {
        "label": "Settings",
        "icon": "gear-fill",
        "permission": "settings",
        "handler": show_settings,
    },
    {
        "label": "Gestion utilisateurs",
        "icon": "person-gear",
        "permission": "user_management",
        "handler": show_user_management,
    },
]

# Filtrage des éléments autorisés selon le rôle de l'utilisateur connecté
allowed_items = [
    item for item in ALL_NAVIGATION_ITEMS if has_permission(item["permission"])
]

# Si l'utilisateur n'a accès à aucune page
if not allowed_items:
    st.error("Aucune page n'est accessible pour votre rôle actuel. Veuillez contacter un administrateur.")
    st.stop()

options_list = [item["label"] for item in allowed_items]
icons_list = [item["icon"] for item in allowed_items]

# ── 4. Barre de navigation latérale ─────────────────────────────────────────────
with st.sidebar:
    # Affiche le profil utilisateur connecté et le bouton Déconnexion
    render_user_bar()
    st.divider()

    selected_label = option_menu(
        menu_title="Menu Principal",
        options=options_list,
        icons=icons_list,
        menu_icon="menu-button-wide",
        default_index=0,
        orientation="vertical",
    )

# ── 5. Routage vers la vue sélectionnée ─────────────────────────────────────────
for item in allowed_items:
    if selected_label == item["label"]:
        item["handler"]()
        break
