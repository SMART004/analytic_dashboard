# views/sync_widget.py
"""
Composant d'interface utilisateur pour le mode Local-First.
Fournit le widget de statut et de synchronisation dans la barre latérale,
ainsi que le panneau d'administration complet dans les paramètres.
"""

from __future__ import annotations

import streamlit as st
from utils.network import get_network_status, refresh_network_cache, check_local_db_integrity
from services.sync_service import get_last_sync_info, run_full_synchronization


def render_sync_sidebar_widget() -> None:
    """
    Affiche le statut réseau, la base locale et le bouton de synchronisation
    dans la barre latérale de l'application de bureau native.
    """
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🖥️ Mode Bureau Local-First")

    net_info = get_network_status(force_refresh=False)
    sync_info = get_last_sync_info()

    # Badge de statut réseau
    if net_info["online"]:
        ping = f" ({net_info['latency_ms']} ms)" if net_info["latency_ms"] else ""
        st.sidebar.success(f"🟢 **En ligne**{ping}")
    else:
        st.sidebar.warning("🟠 **Mode Hors ligne** (Local-First)")

    # Information sur la base locale
    db_size = net_info.get("db_size_mb", 0.0)
    st.sidebar.caption(f"💾 **Base Locale** : SQLite (`{db_size} Mo`)")

    # Information sur la dernière synchronisation
    formatted_sync = sync_info.get("formatted_time", "Jamais synchronisé")
    sync_status = sync_info.get("status", "NEVER")
    status_icon = "✅" if sync_status == "SUCCESS" else ("⚠️" if sync_status == "OFFLINE" else "ℹ️")
    st.sidebar.caption(f"🔄 **Dernière synchro** : {status_icon} {formatted_sync}")

    # Bouton d'action de synchronisation
    col1, col2 = st.sidebar.columns([3, 1])
    with col1:
        sync_clicked = st.button("🔄 Synchroniser", key="btn_sidebar_sync", use_container_width=True)
    with col2:
        refresh_clicked = st.button("⚡", key="btn_sidebar_refresh_net", help="Tester le réseau", use_container_width=True)

    if refresh_clicked:
        refresh_network_cache()
        st.rerun()

    if sync_clicked:
        progress_bar = st.sidebar.progress(0, text="Démarrage de la synchronisation...")

        def _on_progress(pct: int, msg: str):
            progress_bar.progress(pct, text=msg)

        res = run_full_synchronization(progress_callback=_on_progress)
        progress_bar.empty()

        if res["success"]:
            st.sidebar.success(res["message"])
            st.toast("Synchronisation réussie !", icon="✅")
            st.rerun()
        else:
            if res.get("status") == "OFFLINE":
                st.sidebar.info(res["message"])
                st.toast("Mode Hors-ligne : vos données locales sont préservées.", icon="ℹ️")
            else:
                st.sidebar.error(res["message"])
                st.toast(res["message"], icon="⚠️")


def render_sync_full_panel() -> None:
    """
    Affiche le panneau complet d'administration du mode Local-First
    pour la page des paramètres (pages/settings.py).
    """
    st.subheader("🌐 Architecture Local-First & Synchronisation Cloud")
    st.markdown(
        """
        Cette application fonctionne en architecture **Local-First** : toutes vos consultations,
        filtres et visualisations s'exécutent directement sur votre base **SQLite locale**
        (`storage/dashboard.db`) pour des performances instantanées et un fonctionnement 100% autonome
        sans connexion Internet.
        """
    )

    # Ligne de diagnostics
    col_net, col_db, col_sync = st.columns(3)

    net_info = get_network_status(force_refresh=False)
    sync_info = get_last_sync_info()

    with col_net:
        st.markdown("#### 📡 Connectivité")
        if net_info["online"]:
            st.success(f"🟢 **Connecté à Internet** ({net_info['latency_ms']} ms)")
        else:
            st.error("🔴 **Non connecté à Internet**")

        remote_host = net_info.get("remote_host") or "Non configuré"
        if net_info.get("remote_reachable"):
            st.success(f"☁️ Hôte Turso joignable : `{remote_host}`")
        elif net_info.get("remote_configured"):
            st.warning(f"☁️ Hôte Turso injoignable : `{remote_host}`")
        else:
            st.info("☁️ Aucune base distante configurée (Mode 100% Local)")

    with col_db:
        st.markdown("#### 💾 Base Locale SQLite")
        db_path = net_info.get("db_path", "storage/dashboard.db")
        db_size = net_info.get("db_size_mb", 0.0)
        st.info(f"📁 Fichier : `{db_path}`")
        st.metric("Taille disque", f"{db_size} Mo")

        is_healthy, health_msg = check_local_db_integrity()
        if is_healthy:
            st.success(f"🛡️ {health_msg}")
        else:
            st.warning(f"⚠️ {health_msg}")

    with col_sync:
        st.markdown("#### 🔄 Dernière Synchronisation")
        st.metric("Date & Heure", sync_info.get("formatted_time", "Jamais"))
        status = sync_info.get("status", "NEVER")
        if status == "SUCCESS":
            st.success("Statut : Succès")
        elif status == "OFFLINE":
            st.warning("Statut : Annulée (Hors ligne)")
        elif status == "FAILED":
            st.error("Statut : Échec")
        else:
            st.info("Statut : En attente")

    st.divider()

    # Actions de synchronisation
    st.markdown("### ⚡ Actions de Synchronisation")
    btn_col1, btn_col2, btn_col3 = st.columns([2, 2, 2])

    with btn_col1:
        if st.button("🚀 Lancer Synchronisation Complète", use_container_width=True, type="primary"):
            pbar = st.progress(0, text="Initialisation de la synchronisation...")

            def _update_pbar(pct: int, msg: str):
                pbar.progress(pct, text=msg)

            res = run_full_synchronization(progress_callback=_update_pbar)
            pbar.empty()

            if res["success"]:
                st.success(res["message"])
                st.toast("Synchronisation terminée !", icon="✅")
                st.rerun()
            else:
                if res.get("status") == "OFFLINE":
                    st.info(res["message"])
                else:
                    st.error(res["message"])

    with btn_col2:
        if st.button("📡 Tester la Connectivité Réseau", use_container_width=True):
            fresh_status = refresh_network_cache()
            st.success(f"Statut actualisé à {fresh_status['checked_at']}.")
            st.rerun()

    with btn_col3:
        if st.button("🧹 Vider les Caches Streamlit", use_container_width=True):
            st.cache_data.clear()
            try:
                from models.db import get_cached_connection
                get_cached_connection.clear()
            except Exception:
                pass
            st.success("Tous les caches mémoire ont été réinitialisés.")
            st.toast("Caches vidés avec succès", icon="🧹")

    st.divider()

    # Volumétrie des tables locales
    st.markdown("### 📊 Statistiques de la Base Locale")
    try:
        from models.db import get_connection
        tables_to_check = [
            ("referentiel_pos", "PDV / Agents référencés"),
            ("sites", "Sites géographiques"),
            ("referentiel_commerciaux", "Commerciaux terrain"),
            ("listing_oos", "Snapshots Ruptures OOS"),
            ("users", "Comptes Utilisateurs"),
            ("stored_files", "Fichiers de paramétrage"),
            ("transactions", "Transactions enregistrées"),
        ]
        with get_connection() as conn:
            counts = {}
            for t_name, label in tables_to_check:
                try:
                    c = conn.execute(f"SELECT count(*) FROM {t_name}").fetchone()[0]
                    counts[label] = c
                except Exception:
                    counts[label] = 0

        metric_cols = st.columns(4)
        col_idx = 0
        for label, count in counts.items():
            metric_cols[col_idx % 4].metric(label, f"{count:,}".replace(",", " "))
            col_idx += 1
    except Exception as exc:
        st.caption(f"Impossible de charger les statistiques : {exc}")

    # Historique détaillé de la dernière synchro
    details = sync_info.get("details", {})
    if details:
        with st.expander("📄 Détails techniques de la dernière synchronisation", expanded=False):
            st.json(details)
