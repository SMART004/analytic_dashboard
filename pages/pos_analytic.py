# pages/gros_transferts.py
import streamlit as st
import pandas as pd
from utils.helpers import load_file, clean_phone, find_amount_column, to_excel
from utils.plotting import create_gros_transferts_charts

def show_gros_transferts():
    st.title("🔍 Analyse des Gros Transferts (≥ 40M)")
    st.markdown("**Transfer_reçu = somme sur From (Transfer envoyé)**")

    # Récupération de la liste d'exclusion depuis Settings
    exclusion_df = st.session_state.get('exclusion_df')

    # ===================== SESSION STATE =====================
    if 'gt_targeted' not in st.session_state:
        st.session_state.gt_targeted = None
        st.session_state.gt_targeted_phones = None
        st.session_state.gt_exclusion_set = None
        st.session_state.gt_amount_col = None

    seuil = st.number_input("Seuil minimum reçu des caisses (FCFA)", 
                           value=40_000_000, 
                           step=1_000_000)

    # ===================== ÉTAPE 1 : Identification =====================
    if st.session_state.gt_targeted is None:
        st.subheader("Étape 1 – Fichiers Caisses + Liste Exclusion")

        col1, col2 = st.columns(2)
        with col1:
            caisse_files = st.file_uploader(
                "Fichiers transactions CAISSES (Excel ou CSV)",
                type=["xlsx", "xls", "csv"],
                accept_multiple_files=True,
                key="caisses_gt"
            )

        # with col2:
        #     excl_file = st.file_uploader(
        #         "Fichier liste EXCLUSION (colonne NUM)",
        #         type=["xlsx", "xls", "csv"],
        #         key="excl_gt"
        #     )

        if st.button("Identifier les personnes recherchées", type="primary"):
            if not caisse_files or not exclusion_df:
                st.error("Veuillez uploader les fichiers caisses et exclusion")
                st.stop()

            with st.spinner("Analyse des transactions caisses..."):
                # Chargement caisses
                df_list = [load_file(f) for f in caisse_files]
                df_caisses = pd.concat(df_list, ignore_index=True)

                amount_col = find_amount_column(df_caisses)
                if not amount_col:
                    st.error(f"Colonne montant non trouvée. Colonnes disponibles : {list(df_caisses.columns)}")
                    st.stop()

                st.info(f"Colonne montant détectée : **{amount_col}**")

                df_caisses['Amount_num'] = pd.to_numeric(df_caisses[amount_col], errors='coerce').abs()
                df_caisses['phone_to'] = df_caisses.get('To', pd.Series()).apply(clean_phone)

                transfers_caisses = df_caisses[df_caisses['Type'] == "Transfer"].copy()

                total_caisses = (
                    transfers_caisses.groupby('phone_to')['Amount_num']
                    .sum()
                    .reset_index(name='Total_reçu_caisses')
                )

                # Chargement exclusion
                df_excl = load_file(exclusion_df)
                if 'NUM' not in df_excl.columns:
                    st.error("Colonne 'NUM' manquante dans le fichier exclusion")
                    st.stop()

                df_excl['NUM_clean'] = df_excl['NUM'].apply(clean_phone)
                exclusion_set = set(df_excl['NUM_clean'].dropna().astype(str))

                # Personnes ciblées
                targeted = total_caisses[
                    (total_caisses['Total_reçu_caisses'] >= seuil) &
                    (~total_caisses['phone_to'].isin(exclusion_set))
                ].copy()

                if 'To name' in transfers_caisses.columns:
                    names = transfers_caisses.groupby('phone_to')['To name'].first().reset_index()
                    targeted = targeted.merge(names, on='phone_to', how='left')
                else:
                    targeted['To name'] = "Nom non trouvé"

                targeted = targeted.rename(columns={
                    'phone_to': 'Numéro',
                    'To name': 'Nom',
                    'Total_reçu_caisses': 'Total reçu des caisses'
                })

                # Sauvegarde en session
                st.session_state.gt_targeted = targeted
                st.session_state.gt_targeted_phones = set(targeted['Numéro'])
                st.session_state.gt_exclusion_set = exclusion_set
                st.session_state.gt_amount_col = amount_col

                st.success(f"{len(targeted)} personnes recherchées identifiées")
                st.rerun()

    # ===================== ÉTAPE 2 : Calculs & Graphiques =====================
    else:
        st.subheader("📋 Personnes Recherchées")
        st.dataframe(
            st.session_state.gt_targeted.style.format({'Total reçu des caisses': '{:,.0f}'}),
            use_container_width=True,
            height=400
        )

        st.subheader("Étape 2 – Transactions des personnes recherchées")
        persons_files = st.file_uploader(
            "Fichiers des transactions des personnes (Excel ou CSV)",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
            key="persons_gt"
        )

        if st.button("Calculer tous les montants", type="primary") and persons_files:
            with st.spinner("Calcul des montants en cours..."):
                df_list_p = [load_file(f) for f in persons_files]
                df_persons = pd.concat(df_list_p, ignore_index=True)

                amount_col = st.session_state.gt_amount_col
                if amount_col in df_persons.columns:
                    df_persons['Amount_num'] = pd.to_numeric(df_persons[amount_col], errors='coerce').abs()
                else:
                    numeric_cols = df_persons.select_dtypes(include=['number']).columns
                    df_persons['Amount_num'] = pd.to_numeric(df_persons[numeric_cols[0]], errors='coerce').abs() if len(numeric_cols) > 0 else 0

                df_persons['phone_from'] = df_persons.get('From', pd.Series()).apply(clean_phone)
                df_persons['phone_to'] = df_persons.get('To', pd.Series()).apply(clean_phone)

                targeted_phones = st.session_state.gt_targeted_phones
                exclusion_set = st.session_state.gt_exclusion_set

                # Calculs
                cash_in = df_persons[(df_persons['Type'] == "Cash in") & 
                                    df_persons['phone_from'].isin(targeted_phones)].groupby('phone_from')['Amount_num'].sum()

                cash_out = df_persons[(df_persons['Type'] == "Cash out") & 
                                     df_persons['phone_to'].isin(targeted_phones)].groupby('phone_to')['Amount_num'].sum()

                transfer_recu = df_persons[(df_persons['Type'] == "Transfer") & 
                                          df_persons['phone_from'].isin(targeted_phones)].groupby('phone_from')['Amount_num'].sum()

                transfer_remis = df_persons[
                    (df_persons['Type'] == "Transfer") &
                    (df_persons['phone_from'].isin(targeted_phones)) &
                    (df_persons['phone_to'].isin(exclusion_set))
                ].groupby('phone_from')['Amount_num'].sum()

                transfer_envoye_non_exclu = transfer_recu - transfer_remis.reindex(transfer_recu.index, fill_value=0)

                # Construction du résultat final
                result = st.session_state.gt_targeted.set_index('Numéro').copy()
                result['Cash_In'] = cash_in.reindex(result.index).fillna(0)
                result['Cash_Out_reçu'] = cash_out.reindex(result.index).fillna(0)
                result['Transfer_reçu'] = transfer_recu.reindex(result.index).fillna(0)
                result['Transfer_remis'] = transfer_remis.reindex(result.index).fillna(0)
                result['Transfer envoyé au non exclu'] = transfer_envoye_non_exclu.reindex(result.index).fillna(0)

                result = result.reset_index()

                # ===================== AFFICHAGE =====================
                st.subheader("📊 Résultat Final")
                numeric_cols = result.select_dtypes(include=['number']).columns.tolist()

                styled_result = result.style.format({col: '{:,.0f}' for col in numeric_cols})
                st.dataframe(styled_result, use_container_width=True, height=700)

                # Graphiques d'interprétation
                st.subheader("📈 Interprétation Graphique")
                fig1, fig2 = create_gros_transferts_charts(result)

                if fig1 and fig2:
                    col_g1, col_g2 = st.columns(2)
                    with col_g1:
                        st.plotly_chart(fig1, use_container_width=True)
                    with col_g2:
                        st.plotly_chart(fig2, use_container_width=True)

                # ===================== EXPORT =====================
                st.subheader("Export des résultats")
                col_exp1, col_exp2 = st.columns(2)

                excel_data = to_excel(result)
                col_exp1.download_button(
                    label="Télécharger en EXCEL (.xlsx)",
                    data=excel_data,
                    file_name="Analyse_Gros_Transferts.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

                col_exp2.download_button(
                    label="Télécharger en CSV",
                    data=result.to_csv(index=False).encode('utf-8'),
                    file_name="Analyse_Gros_Transferts.csv",
                    mime="text/csv"
                )

        # Bouton de réinitialisation
        if st.button("🔄 Nouvelle analyse Gros Transferts"):
            for key in ['gt_targeted', 'gt_targeted_phones', 'gt_exclusion_set', 'gt_amount_col']:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()