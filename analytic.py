# ===================== SUPABASE IMPORTS =====================
from utils.supabase import (
    upload_result_file,
    load_all_result_files
)


def show_gros_transferts():
    st.title("Analyse des Gros Transferts")

    # =====================================================
    # BUCKET CONFIG
    # =====================================================
    BUCKET_NAME = "pos-bad-result-files"

    # Deux dossiers dans le bucket :
    # pos-bad-result-files/
    # ├── caisses/
    # └── persons/

    CAISSE_FOLDER = "caisses"
    PERSONS_FOLDER = "persons"

    # =====================================================
    # EXCLUSION SETTINGS
    # =====================================================
    exclusion_df = st.session_state.get("exclusion_df")

    if exclusion_df is None:
        st.error("Veuillez charger le fichier d'exclusion dans Settings")
        st.stop()

    # =====================================================
    # SESSION STATE
    # =====================================================
    if "gt_targeted" not in st.session_state:
        st.session_state.gt_targeted = None
        st.session_state.gt_targeted_phones = None
        st.session_state.gt_exclusion_set = None
        st.session_state.gt_amount_col = None

    seuil = st.number_input(
        "Seuil minimum reçu des caisses (FCFA)",
        value=40_000_000,
        step=1_000_000
    )

    # =====================================================
    # ETAPE 1 : FICHIERS CAISSES
    # =====================================================
    if st.session_state.gt_targeted is None:

        st.subheader("Étape 1 – Fichiers Transactions CAISSES")

        uploaded_caisse_files = st.file_uploader(
            "Uploader les fichiers CAISSES",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
            key="gt_caisses_upload"
        )

        # =========================================
        # Upload vers Supabase
        # =========================================
        if uploaded_caisse_files:
            for file in uploaded_caisse_files:
                upload_result_file(
                    bucket_name=BUCKET_NAME,
                    uploaded_file=file,
                    folder_name=CAISSE_FOLDER
                )

            st.success("Fichiers CAISSES uploadés avec succès")
            st.rerun()

        # =========================================
        # Chargement depuis Supabase
        # =========================================
        df_caisses = load_all_result_files(
            bucket_name=BUCKET_NAME,
            folder_name=CAISSE_FOLDER
        )

        if df_caisses is None or df_caisses.empty:
            st.info("Aucun fichier CAISSES trouvé dans Supabase")
            return

        st.success(f"{len(df_caisses)} lignes CAISSES chargées depuis Supabase")

        if st.button("Identifier les personnes recherchées", type="primary"):

            with st.spinner("Analyse des transactions caisses..."):

                amount_col = find_amount_column(df_caisses)

                if not amount_col:
                    st.error(
                        f"Colonne montant non trouvée. Colonnes disponibles : {list(df_caisses.columns)}"
                    )
                    st.stop()

                st.info(f"Colonne montant détectée : {amount_col}")

                df_caisses["Amount_num"] = pd.to_numeric(
                    df_caisses[amount_col],
                    errors="coerce"
                ).abs()

                df_caisses["phone_to"] = df_caisses.get(
                    "To",
                    pd.Series()
                ).apply(clean_phone)

                transfers_caisses = df_caisses[
                    df_caisses["Type"] == "Transfer"
                ].copy()

                total_caisses = (
                    transfers_caisses
                    .groupby("phone_to")["Amount_num"]
                    .sum()
                    .reset_index(name="Total_reçu_caisses")
                )

                # =========================================
                # EXCLUSION
                # =========================================
                df_excl = exclusion_df.copy()

                if "NUM" not in df_excl.columns:
                    st.error("Colonne NUM manquante dans exclusion")
                    st.stop()

                df_excl["NUM_clean"] = df_excl["NUM"].apply(clean_phone)
                exclusion_set = set(
                    df_excl["NUM_clean"]
                    .dropna()
                    .astype(str)
                )

                # =========================================
                # PERSONNES CIBLEES
                # =========================================
                targeted = total_caisses[
                    (total_caisses["Total_reçu_caisses"] >= seuil) &
                    (~total_caisses["phone_to"].isin(exclusion_set))
                ].copy()

                if targeted.empty:
                    st.warning("Aucune personne ne dépasse le seuil")
                    st.stop()

                if "To name" in transfers_caisses.columns:
                    names = (
                        transfers_caisses
                        .groupby("phone_to")["To name"]
                        .first()
                        .reset_index()
                    )

                    targeted = targeted.merge(
                        names,
                        on="phone_to",
                        how="left"
                    )
                else:
                    targeted["To name"] = "Nom non trouvé"

                targeted = targeted.rename(columns={
                    "phone_to": "Numéro du POS",
                    "To name": "Nom du POS",
                    "Total_reçu_caisses": "Total reçu des caisses"
                })

                targeted = targeted[[
                    "Numéro du POS",
                    "Nom du POS",
                    "Total reçu des caisses"
                ]]

                st.session_state.gt_targeted = targeted
                st.session_state.gt_targeted_phones = set(
                    targeted["Numéro du POS"]
                )
                st.session_state.gt_exclusion_set = exclusion_set
                st.session_state.gt_amount_col = amount_col

                st.success(f"{len(targeted)} POS identifiés")
                st.rerun()

    # =====================================================
    # ETAPE 2 : TRANSACTIONS DES POS
    # =====================================================
    else:

        st.subheader("📋 Personnes Recherchées")

        st.dataframe(
            st.session_state.gt_targeted.style.format({
                "Total reçu des caisses": "{:,.0f}"
            }),
            use_container_width=True,
            height=400
        )

        st.subheader("Étape 2 – Transactions des POS ciblés")

        uploaded_person_files = st.file_uploader(
            "Uploader les fichiers transactions POS",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
            key="gt_persons_upload"
        )

        # =========================================
        # Upload vers Supabase
        # =========================================
        if uploaded_person_files:
            for file in uploaded_person_files:
                upload_result_file(
                    bucket_name=BUCKET_NAME,
                    uploaded_file=file,
                    folder_name=PERSONS_FOLDER
                )

            st.success("Fichiers POS uploadés avec succès")
            st.rerun()

        # =========================================
        # Chargement depuis Supabase
        # =========================================
        df_persons = load_all_result_files(
            bucket_name=BUCKET_NAME,
            folder_name=PERSONS_FOLDER
        )

        if df_persons is None or df_persons.empty:
            st.info("Aucun fichier POS trouvé dans Supabase")
            return

        st.success(f"{len(df_persons)} lignes POS chargées depuis Supabase")

        if st.button("Calculer tous les montants", type="primary"):

            with st.spinner("Calcul des montants en cours..."):

                amount_col = st.session_state.gt_amount_col

                if amount_col in df_persons.columns:
                    df_persons["Amount_num"] = pd.to_numeric(
                        df_persons[amount_col],
                        errors="coerce"
                    ).abs()
                else:
                    numeric_cols = df_persons.select_dtypes(
                        include=["number"]
                    ).columns

                    if len(numeric_cols) > 0:
                        df_persons["Amount_num"] = pd.to_numeric(
                            df_persons[numeric_cols[0]],
                            errors="coerce"
                        ).abs()
                    else:
                        df_persons["Amount_num"] = 0

                df_persons["phone_from"] = df_persons.get(
                    "From",
                    pd.Series()
                ).apply(clean_phone)

                df_persons["phone_to"] = df_persons.get(
                    "To",
                    pd.Series()
                ).apply(clean_phone)

                targeted_phones = st.session_state.gt_targeted_phones
                exclusion_set = st.session_state.gt_exclusion_set

                # =========================================
                # CALCULS
                # =========================================
                cash_in = df_persons[
                    (df_persons["Type"] == "Cash in") &
                    (df_persons["phone_from"].isin(targeted_phones))
                ].groupby("phone_from")["Amount_num"].sum()

                cash_out = df_persons[
                    (df_persons["Type"] == "Cash out") &
                    (df_persons["phone_to"].isin(targeted_phones))
                ].groupby("phone_to")["Amount_num"].sum()

                transfer_remis = df_persons[
                    (df_persons["Type"] == "Transfer") &
                    (df_persons["phone_from"].isin(targeted_phones)) &
                    (df_persons["phone_to"].isin(exclusion_set))
                ].groupby("phone_from")["Amount_num"].sum()

                transfer_envoye_non_exclu = df_persons[
                    (df_persons["Type"] == "Transfer") &
                    (df_persons["phone_from"].isin(targeted_phones)) &
                    (~df_persons["phone_to"].isin(exclusion_set))
                ].groupby("phone_from")["Amount_num"].sum()

                # =========================================
                # RESULTAT FINAL
                # =========================================
                result = (
                    st.session_state.gt_targeted
                    .set_index("Numéro du POS")
                    .copy()
                )

                result["Cash_In"] = cash_in.reindex(result.index).fillna(0)
                result["Cash_Out"] = cash_out.reindex(result.index).fillna(0)
                result["Float up"] = transfer_remis.reindex(result.index).fillna(0)
                result["Float down"] = transfer_envoye_non_exclu.reindex(result.index).fillna(0)

                result = result.reset_index()

                st.subheader("📊 Résultat Final")
                st.dataframe(result, use_container_width=True, height=700)