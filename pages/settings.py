# Settings page - Configurations
import streamlit as st
from utils.turso_storage import handle_upload
from ingestion.ingest import run_referentiel_ingestion, update_pos_targets_from_file
from utils.turso_storage import load_setting


def _handle_reference_upload(**kwargs):
    uploaded = handle_upload(**kwargs)
    if uploaded:
        with st.spinner("Synchronisation SQLite en cours..."):
            try:
                run_referentiel_ingestion()
            except Exception as exc:
                st.error(f"Echec de la synchronisation SQLite : {exc}")
            else:
                st.cache_data.clear()
                st.success("Synchronisation SQLite effectuee.")
    return uploaded


def show_settings():
    st.title("⚙️ Settings - Configurations")
    st.markdown("**Chargez ici tous les fichiers et configurations de l'application**")

    (tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12,
    ) = st.tabs([
        "Commerciaux",
        "Caisses",
        "Zones",
        "Maître POS",
        "Maitre POS centre III",
        "Masters",
        "CDS",
        "POS relai & Caisses",
        "Sites Etoudi",
        "HVC Commercial",
        "HVC CDS (Quota)",
        "Cibles POS (OOS / Day Target)",   # ← nouveau
    ])

    # ===================== ONGLET COMMERCIAUX =====================
    with tab1:
        st.subheader("Fichier Configuration Commerciaux")
        st.markdown("Colonnes attendues : `Zone_Territoire`, `Zone_SA`, `Nom_Ccial`, `Ccial_MSISDN`")

        _handle_reference_upload(
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

        _handle_reference_upload(
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

        _handle_reference_upload(
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

        _handle_reference_upload(
            tab_title="Fichier Maître POS",
            uploader_label="Upload Fichier Maître POS",
            uploader_key="maitre_file_uploader",
            session_key="pos_master_df",
            folder_name="maitre_pos"
        )

    # ===================== ONGLET MAITRE POS CENTRE III =====================
    with tab5:
        st.subheader("Fichier Maitre POS centre III")

        _handle_reference_upload(
            tab_title="Fichier Maitre POS centre III",
            uploader_label="Upload Fichier Maitre POS centre III",
            uploader_key="maitre_III_file_uploader",
            session_key="pos_master_III_df",
            folder_name="maitre_pos_III"
        )

    # ===================== ONGLET MASTERS =====================
    with tab6:
        st.subheader("Fichier Configuration Master")
        st.markdown("Colonnes attendues : `NUM`, `MASTER`")

        _handle_reference_upload(
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

        _handle_reference_upload(
            tab_title="Fichier CDS",
            uploader_label="Upload Fichier CDS",
            uploader_key="cds_file_uploader",
            session_key="exclusion_cds",
            folder_name="cds"
        )

    # ===================== ONGLET POS RELAIS & CAISSES =====================
    with tab8:
        st.subheader("Fichier Configuration POS Relais & Caisses")
        st.markdown("Colonnes attendues : `MSISDN_PR`, `TERRITOIRE`, `Localisation`, `Nom du point de relais`")

        _handle_reference_upload(
            tab_title="Fichier POS Relais & Caisses",
            uploader_label="Upload Fichier POS Relais & Caisses",
            uploader_key="pos_relay_caisse_file_uploader",
            session_key="pos_relay_caisse_df",
            folder_name="pos_relay_caisse"
        )

    # ===================== ONGLET SITES ETOUDI =====================
    with tab9:
        st.subheader("Fichier Configuration SITES ETOUDI")
        st.markdown("Colonnes attendues : `dsm_name`, `sitename`, `quartier`")

        _handle_reference_upload(
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

        _handle_reference_upload(
            tab_title="Fichier hvc commercial",
            uploader_label="Upload Fichier hvc commercial",
            uploader_key="hvc_commercial_file_uploader",
            session_key="hvc_commercial_df",
            folder_name="hvc_commercial"
        )

    # ===================== ONGLET HVC CDS (QUOTA) =====================
    # Onglet manquant identifie en Tache 5 : sans lui, le quota HVC/CDS
    # (colonnes NUM_HVC / NOM_CDS) n'a jamais pu etre uploade, meme si
    # l'ingestion (ingest_hvc_cds_assignments) existait deja cote backend.
    with tab11:
        st.subheader("Fichier Quota HVC par CDS")
        st.markdown(
            "Colonnes attendues : `NUM_HVC`, `NOM_CDS` — assigne chaque HVC a "
            "un seul CDS. Le quota affiche sur la page Performance CDS "
            "(colonne Nb_HVC_Attribue) est recalcule automatiquement a partir "
            "de ce fichier."
        )

        _handle_reference_upload(
            tab_title="Fichier quota HVC CDS",
            uploader_label="Upload Fichier quota HVC CDS",
            uploader_key="hvc_cds_file_uploader",
            session_key="hvc_cds_df",
            folder_name="hvc_cds"
        )

    with tab12:
        st.subheader("Mise à jour oos_target & day_target")
        st.markdown(
            """
            Uploadez un fichier Excel/CSV contenant au minimum :
            - **MSISDN** (variantes : `MSISDN`, `agent_msisdn`, `Agent MSISDN`, `NUM`…)
            - **oos_target** et/ou **day_target**  
              (variantes : `OOS_target`, `OOS target`, `Day_Target`, `Day Target`…)

            Les valeurs sont mises à jour dans `referentiel_pos` par correspondance MSISDN.
            """
        )

        handle_upload(
            tab_title="Fichier cibles POS",
            uploader_label="Upload Fichier oos_target / day_target",
            uploader_key="pos_targets_file_uploader",
            session_key="pos_targets_df",
            folder_name="pos_targets",
        )

        if st.button("🔄 Mettre à jour referentiel_pos", type="primary", key="update_pos_targets_btn"):
            with st.spinner("Mise à jour en cours…"):
                try:
                    df = load_setting("pos_targets")  # folder_name de handle_upload
                    if df is None or df.empty:
                        st.warning(
                            "Aucun fichier chargé. "
                            "Uploadez d'abord un fichier dans cet onglet."
                        )
                    else:
                        result = update_pos_targets_from_file(df)

                        st.success(
                            f"Fichier : **{result['rows_file']}** lignes | "
                            f"Mises à jour : **{result['updated']}** | "
                            f"Non trouvés dans referentiel_pos : **{result['not_found']}**"
                        )

                        if result["not_found"] > 0:
                            st.info(
                                f"{result['not_found']} MSISDN du fichier "
                                "n'existent pas dans referentiel_pos "
                                "(format de numéro ou POS absent du master)."
                            )

                        # POS encore à 0 après update
                        try:
                            from models.db import get_connection
                            import pandas as pd
                            conn = get_connection()
                            zero_count = pd.read_sql_query(
                                """
                                SELECT COUNT(*) AS nb
                                FROM referentiel_pos
                                WHERE oos_target IS NULL OR oos_target = 0
                                """,
                                conn,
                            )["nb"].iloc[0]
                            conn.close()
                            st.caption(
                                f"POS encore avec oos_target = 0 : **{zero_count}**"
                            )
                        except Exception:
                            pass

                        st.cache_data.clear()

                except Exception as e:
                    st.error(f"Échec : {e}")

    st.divider()

    # ===================== SYNCHRONISATION VERS SQLITE =====================
    # Correctif : avant ce bouton, aucun fichier uploade ici n'etait jamais
    # pousse vers SQLite (seules les transactions des buckets de performance
    # l'etaient, via ingestion/upload_sync.py). Les pages qui lisent
    # exclusivement SQLite (models/*.py) restaient donc a vide meme apres
    # upload reussi d'un fichier settings. Idempotent : peut etre rappele
    # sans risque de duplication.
    st.subheader("Synchronisation vers la base")
    st.caption(
        "Les fichiers uploades ci-dessus sont stockes, mais les pages de "
        "l'application (Dashboard, Performance, Conquete, OOS...) lisent la "
        "base SQLite, pas les fichiers directement. Cliquez ci-dessous apres "
        "chaque upload pour que les changements soient pris en compte."
    )

    if st.button("🔄 Synchroniser les référentiels vers SQLite", type="primary", key="settings_sync_btn"):
        with st.spinner("Synchronisation en cours..."):
            try:
                counts = run_referentiel_ingestion()
            except Exception as exc:
                st.error(f"Echec de la synchronisation : {exc}")
            else:
                st.success("Référentiels synchronisés avec succès.")
                cols = st.columns(4)
                labels = [
                    ("Sites", "sites"),
                    ("POS", "referentiel_pos"),
                    ("Commerciaux", "referentiel_commerciaux"),
                    ("Exclusions", "exclusions_reference"),
                    ("CDS", "cds_referentiel"),
                    ("Points Relais/Caisses", "point_relay_referentiel"),
                    ("Mapping HVC->Commercial", "hvc_commercial_mapping"),
                    ("Quota HVC->CDS", "hvc_cds_assignments"),
                ]
                for i, (label, key) in enumerate(labels):
                    cols[i % 4].metric(label, counts.get(key, 0))
                st.cache_data.clear()

    st.info(
        "Tous les fichiers et configurations chargés ici sont disponibles "
        "dans les autres pages après synchronisation."
    )
