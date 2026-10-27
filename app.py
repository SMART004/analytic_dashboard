# app.py
import asyncio
import os
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
        /* Design moderne pour tous les indicateurs st.spinner */
        [data-testid="stSpinner"] {
            background: rgba(248, 250, 252, 0.95) !important;
            backdrop-filter: blur(8px) !important;
            border: 1.5px solid rgba(59, 130, 246, 0.3) !important;
            border-radius: 12px !important;
            padding: 14px 22px !important;
            box-shadow: 0 8px 24px rgba(59, 130, 246, 0.12) !important;
            margin: 1rem 0 !important;
        }
        [data-testid="stSpinner"] > div {
            color: #1E40AF !important;
            font-weight: 600 !important;
            font-size: 0.95rem !important;
        }
        /* Barre lumineuse animée au sommet de l'écran lors du calcul/chargement */
        header[data-testid="stHeader"]::after {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 3.5px;
            background: linear-gradient(90deg, #3B82F6, #8B5CF6, #EC4899, #3B82F6);
            background-size: 200% 100%;
            z-index: 9999999;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.25s ease;
        }
        header[data-testid="stHeader"]:has([data-testid="stStatusWidget"])::after {
            opacity: 1;
            animation: topBarLoadingPulse 1.2s infinite linear;
        }
        @keyframes topBarLoadingPulse {
            0% { background-position: 200% 0; }
            100% { background-position: -200% 0; }
        }
    </style>
""")

# Optimisation UI pour l'application de bureau native
is_desktop = (
    os.getenv("DESKTOP_MODE") == "1"
    or st.query_params.get("desktop") == "1"
)
if is_desktop:
    st.html("""
        <style>
            /* Header transparent conservant le widget de chargement / état */
            header[data-testid="stHeader"] {
                background: transparent !important;
                height: 2.75rem !important;
                pointer-events: none;
                z-index: 99999 !important;
            }
            /* Masquer uniquement les boutons externes (Déployer, etc.) */
            header[data-testid="stHeader"] [data-testid="stToolbar"] > button,
            header[data-testid="stHeader"] [data-testid="stActionButton"] {
                display: none !important;
            }
            /* Conserver et styler le widget d'état / loader de Streamlit */
            header[data-testid="stHeader"] [data-testid="stStatusWidget"] {
                pointer-events: auto !important;
                visibility: visible !important;
                display: flex !important;
                align-items: center !important;
                gap: 6px !important;
                background: rgba(30, 58, 138, 0.95) !important;
                backdrop-filter: blur(10px) !important;
                color: #ffffff !important;
                border-radius: 9999px !important;
                padding: 4px 14px !important;
                box-shadow: 0 4px 14px rgba(30, 58, 138, 0.35) !important;
                border: 1px solid rgba(255, 255, 255, 0.25) !important;
                margin-right: 1.5rem !important;
                margin-top: 0.35rem !important;
                font-size: 0.82rem !important;
                font-weight: 600 !important;
            }
            header[data-testid="stHeader"] [data-testid="stStatusWidget"] * {
                color: #ffffff !important;
                fill: #ffffff !important;
            }
            .main .block-container {
                padding-top: 1.75rem !important;
            }
            footer {
                display: none !important;
            }
            /* Scrollbars fluides natives */
            ::-webkit-scrollbar {
                width: 8px;
                height: 8px;
            }
            ::-webkit-scrollbar-track {
                background: rgba(0, 0, 0, 0.04);
            }
            ::-webkit-scrollbar-thumb {
                background: rgba(100, 100, 100, 0.3);
                border-radius: 4px;
            }
            ::-webkit-scrollbar-thumb:hover {
                background: rgba(100, 100, 100, 0.5);
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
from views.sync_widget import render_sync_sidebar_widget

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

    # Widget Local-First et Synchronisation
    render_sync_sidebar_widget()

# ── 5. Routage vers la vue sélectionnée ─────────────────────────────────────────
for item in allowed_items:
    if selected_label == item["label"]:
        with st.spinner(f"Chargement des données ({item['label']})..."):
            item["handler"]()
        break
