# pages/gros_transferts.py
import streamlit as st
import pandas as pd
from utils.helpers import load_file, clean_phone, find_amount_column, to_excel
from utils.plotting import create_gros_transferts_charts
from utils.supabase import load_setting
from domain.reference import get_global_excluded_numbers


def _collect_phone_numbers(df, columns):
    if df is None or df.empty:
        return set()

    phone_numbers = set()
    for col in columns:
        if col in df.columns:
            phone_numbers.update(
                df[col].apply(clean_phone).dropna().astype(str).tolist()
            )
    return phone_numbers


def _load_excluded_to_phones():
    return get_global_excluded_numbers(
        commerciaux=load_setting("commerciaux"),
        caisses=load_setting("caisses"),
        masters=load_setting("masters"),
        cds=load_setting("cds"),
        pos_relay_caisse=load_setting("pos_relay_caisse"),
    )


def show_gros_transferts():
    st.title("Analyse des Gros Transferts")

    # Récupération de la liste d'exclusion depuis Settings
    exclusion_df = load_setting("caisses")
    if exclusion_df is None:
        st.error("Veuillez charger le fichier **D'exclusion** dans Settings")
        st.stop()

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
        st.subheader("Étape 1 – Fichiers Caisses")

        col1, _ = st.columns(2)
        with col1:
            caisse_files = st.file_uploader(
                "Fichiers transactions CAISSES (Excel ou CSV)",
                type=["xlsx", "xls", "csv"],
                accept_multiple_files=True,
                key="caisses_gt"
            )

        if st.button("Identifier les personnes recherchées", type="primary"):
            if not caisse_files:
                st.error("Veuillez uploader au moins un fichier de transactions caisses")
                st.stop()

            with st.spinner("Analyse des transactions caisses..."):
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

                exclusion_set = _load_excluded_to_phones()

                # Personnes ciblées
                targeted = total_caisses[
                    (total_caisses['Total_reçu_caisses'] >= seuil) &
                    (~total_caisses['phone_to'].isin(exclusion_set))
                ].copy()

                if targeted.empty:
                    st.warning("Aucune personne ne dépasse le seuil après exclusion.")
                    st.stop()

                if 'To name' in transfers_caisses.columns:
                    names = transfers_caisses.groupby('phone_to')['To name'].first().reset_index()
                    targeted = targeted.merge(names, on='phone_to', how='left')
                else:
                    targeted['To name'] = "Nom non trouvé"

                targeted = targeted.rename(columns={
                    'phone_to': 'Numéro du POS',
                    'To name': 'Nom du POS',
                    'Total_reçu_caisses': 'Total reçu des caisses'
                })

                # Réorganisation : Numéro → Nom → Total reçu des caisses
                targeted = targeted[['Numéro du POS', 'Nom du POS', 'Total reçu des caisses']]

                st.session_state.gt_targeted = targeted
                st.session_state.gt_targeted_phones = set(targeted['Numéro du POS'])
                st.session_state.gt_exclusion_set = exclusion_set
                st.session_state.gt_amount_col = amount_col

                st.success(f"{len(targeted)} POS identifiés")
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

                excluded_to_phones = _load_excluded_to_phones()
                if excluded_to_phones:
                    df_persons = df_persons[~df_persons['phone_to'].isin(excluded_to_phones)].copy()

                targeted_phones = st.session_state.gt_targeted_phones
                exclusion_set = st.session_state.gt_exclusion_set

                # Calculs
                cash_in = df_persons[(df_persons['Type'] == "Cash in") & 
                                    df_persons['phone_from'].isin(targeted_phones)].groupby('phone_from')['Amount_num'].sum()

                cash_out = df_persons[(df_persons['Type'] == "Cash out") & 
                                     df_persons['phone_to'].isin(targeted_phones)].groupby('phone_to')['Amount_num'].sum()

                transfer_remis = df_persons[
                    (df_persons['Type'] == "Transfer") &
                    (df_persons['phone_from'].isin(targeted_phones)) &
                    (df_persons['phone_to'].isin(exclusion_set))
                ].groupby('phone_from')['Amount_num'].sum()

                # Calcul direct du Transfer envoyé au non exclu
                transfer_envoye_non_exclu = df_persons[
                    (df_persons['Type'] == "Transfer") &
                    (df_persons['phone_from'].isin(targeted_phones)) &
                    (~df_persons['phone_to'].isin(exclusion_set))
                ].groupby('phone_from')['Amount_num'].sum()

                # Construction du résultat final
                result = st.session_state.gt_targeted.set_index('Numéro du POS').copy()
                result['Cash_In'] = cash_in.reindex(result.index).fillna(0)
                result['Cash_Out'] = cash_out.reindex(result.index).fillna(0)
                result['Float up'] = transfer_remis.reindex(result.index).fillna(0)
                result['Float down'] = transfer_envoye_non_exclu.reindex(result.index).fillna(0)

                result = result.reset_index()

                # ===================== COLORATION CONDITIONNELLE SANS COLONNE % =====================
                st.subheader("📊 Résultat Final")

                def highlight_low_cashin(row):
                    try:
                        ratio = row['Cash_In'] / row['Total reçu des caisses']
                        if ratio <= 0.20:
                            return ['background-color: #FFCDD2'] * len(row)   # Rouge
                    except:
                        pass
                    return [''] * len(row)

                styled_result = result.style.format({
                    col: '{:,.0f}' for col in ['Total reçu des caisses', 'Cash_In', 'Cash_Out', 
                                              'Float up', 'Float down']
                }).apply(highlight_low_cashin, axis=1)

                st.dataframe(styled_result, use_container_width=True, height=700)

                # Graphiques (correction du nom de colonne)
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

        if st.button("🔄 Nouvelle analyse Gros Transferts"):
            for key in ['gt_targeted', 'gt_targeted_phones', 'gt_exclusion_set', 'gt_amount_col']:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()
