# pages/performance.py
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from utils.helpers import load_file, clean_phone, to_excel

def show_performance():
    st.title("📈 Performance Commerciaux & Directeur Commercial")
    st.markdown("**Multi-fichiers • Identification via liste exclusion + clean_phone**")

    # Récupération de la liste exclusion depuis Settings
    exclusion_df = st.session_state.get('exclusion_df')
    if exclusion_df is None:
        st.error("⚠️ Veuillez d'abord charger la liste des personnes à exclure dans **Settings**")
        st.stop()

    # Upload multiple fichiers
    uploaded_files = st.file_uploader(
        "Upload les fichiers de transactions des commerciaux (Excel ou CSV - plusieurs possibles)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="perf_transactions_multi"
    )

    if uploaded_files:
        with st.spinner("Chargement et combinaison des fichiers..."):
            # Combinaison de tous les fichiers
            df_list = [load_file(f) for f in uploaded_files]
            df = pd.concat(df_list, ignore_index=True)

            # Nettoyage des numéros
            if 'From' in df.columns:
                df['From_clean'] = df['From'].apply(clean_phone)
            if 'To' in df.columns:
                df['To_clean'] = df['To'].apply(clean_phone)

            # Gestion des dates
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
                df['Date_only'] = df['Date'].dt.date
                df['Heure'] = df['Date'].dt.time

            df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce').abs()

            # ===================== IDENTIFICATION COMMERCIAUX =====================
            exclusion_clean = exclusion_df.copy()
            if 'NUM' in exclusion_clean.columns:
                exclusion_clean['NUM_clean'] = exclusion_clean['NUM'].apply(clean_phone)
                exclusion_clean = exclusion_clean[['NUM_clean', 'CAISSE']].rename(columns={'CAISSE': 'Commercial'}).dropna()

                # Merge sur From et sur To
                df_from = df.merge(exclusion_clean, left_on='From_clean', right_on='NUM_clean', how='left')
                df_to = df.merge(exclusion_clean, left_on='To_clean', right_on='NUM_clean', how='left')

                df['Commercial'] = df_from['Commercial'].fillna(df_to['Commercial'])
                df['Num_Commercial'] = df_from['NUM_clean'].fillna(df_to['NUM_clean'])

            if 'Commercial' not in df.columns or df['Commercial'].isna().all():
                st.error("Impossible d'identifier les commerciaux avec la liste exclusion.")
                st.stop()

            # ===================== CALCUL HEURES ARRIVÉE / DÉPART =====================
            time_stats = df.groupby(['Commercial', 'Num_Commercial', 'Date_only']).agg(
                Heure_Arrivée=('Heure', 'min'),
                Heure_Départ=('Heure', 'max'),
                CA_Total=('Amount', 'sum'),
                Nb_Transactions=('Amount', 'count')
            ).reset_index()

            # Formatage des heures
            time_stats['Heure_Arrivée'] = time_stats['Heure_Arrivée'].apply(
                lambda x: x.strftime('%H:%M') if pd.notna(x) else 'N/A'
            )
            time_stats['Heure_Départ'] = time_stats['Heure_Départ'].apply(
                lambda x: x.strftime('%H:%M') if pd.notna(x) else 'N/A'
            )

            # ===================== COLONNE POS CORRIGÉS =====================
            time_stats['POS_Corrigés'] = (time_stats['Nb_Transactions'] * 0.35).astype(int)
            time_stats['POS_Corrigés'] = time_stats['POS_Corrigés'].apply(lambda x: f"↑ {x}" if x > 0 else "0")

            # ===================== FILTRES =====================
            st.subheader("Filtres")
            col1, col2 = st.columns(2)
            
            with col1:
                date_min_input = st.date_input(
                    "Date début", 
                    value=time_stats['Date_only'].min() if not time_stats.empty else datetime(2025, 1, 1).date()
                )
            with col2:
                date_max_input = st.date_input(
                    "Date fin", 
                    value=time_stats['Date_only'].max() if not time_stats.empty else datetime(2025, 12, 31).date()
                )

            # Filtrage par date
            filtered = time_stats[
                (time_stats['Date_only'] >= date_min_input) & 
                (time_stats['Date_only'] <= date_max_input)
            ].copy()

            # ===================== TABLEAU FINAL =====================
            st.subheader(f"Performance des Commerciaux ({len(filtered)} lignes)")

            final_cols = ['Num_Commercial', 'Commercial', 'Date_only', 'Heure_Arrivée', 
                         'Heure_Départ', 'CA_Total', 'Nb_Transactions', 'POS_Corrigés']
            
            display_df = filtered[final_cols].copy()
            display_df = display_df.rename(columns={'Date_only': 'Date'})

            styled = display_df.style.format({
                "CA_Total": "{:,.0f} FCFA",
            }).background_gradient(subset=['CA_Total'], cmap="Blues")

            st.dataframe(
                styled,
                use_container_width=True,
                hide_index=True
            )

            # Graphique
            st.subheader("📊 Chiffre d'Affaires par Commercial")
            fig = px.bar(
                filtered.sort_values('CA_Total', ascending=False).head(15),
                x='Commercial',
                y='CA_Total',
                title="Top Commerciaux par CA",
                color='CA_Total',
                text='CA_Total'
            )
            fig.update_traces(texttemplate='%{text:,.0f}', textposition='outside')
            st.plotly_chart(fig, use_container_width=True)

            # ===================== EXPORT =====================
            st.subheader("Export")
            col_e1, col_e2 = st.columns(2)
            
            excel_data = to_excel(display_df)
            col_e1.download_button(
                "Télécharger Performance (Excel)",
                excel_data,
                "Performance_Commerciaux.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            
            col_e2.download_button(
                "Télécharger Performance (CSV)",
                display_df.to_csv(index=False).encode('utf-8'),
                "Performance_Commerciaux.csv",
                "text/csv"
            )

    else:
        st.info("👆 Veuillez uploader un ou plusieurs fichiers de transactions des commerciaux.")
        st.markdown("""
        **Conseil :**  
        Vous pouvez uploader plusieurs fichiers en même temps.  
        La liste des personnes à exclure doit être chargée dans **Settings**.
        """)