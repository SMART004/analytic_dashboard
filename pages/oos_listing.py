# pages/oos_listing.py
import streamlit as st
import pandas as pd
import numpy as np
from utils.helpers import load_file, to_excel

def show_oos_listing():
    st.title("📦 Tirage Listing Out of Stock (OOS)")
    st.markdown("**HVC uniquement • Enrichissement Zones • Commercial fictif**")

    # Récupération du fichier Zones
    zones_df = st.session_state.get('zones_df')
    if zones_df is None:
        st.error("⚠️ Veuillez charger le fichier **Zones** dans Settings")
        st.stop()
    # ===================== FILTRE ZONE GLOBAL =====================
    st.sidebar.markdown("---")
    st.sidebar.subheader("Filtre Zone")

    zones = zones_df.copy()

    zone_list = ["Toutes"] + sorted(zones['ZONE NEW'].dropna().unique().tolist())

    selected_zone = st.sidebar.selectbox(
        "Zone",
        zone_list,
        key="oos_zone_filter"
    )

    selected_terr = "Toutes"
    if selected_zone != "Toutes" and 'TERRITORY CORRECT' in zones.columns:
        terr_list = ["Toutes"] + sorted(
            zones[zones['ZONE NEW'] == selected_zone]['TERRITORY CORRECT'].dropna().unique().tolist()
        )
        selected_terr = st.sidebar.selectbox(
            "Territory",
            terr_list,
            key="oos_terr_filter"
        )

    selected_isl = "Toutes"
    if selected_terr != "Toutes" and "ISL_Terr" in zones.columns:
        isl_list = ["Toutes"] + sorted(
            zones[zones['TERRITORY CORRECT'] == selected_terr]['ISL_Terr'].dropna().unique().tolist()
        )
        selected_isl = st.sidebar.selectbox(
            "Cluster",
            isl_list,
            key="oos_isl_filter"
        )

    selected_site = "Toutes"
    if selected_isl != "Toutes" and "SITENAME" in zones.columns:
        site_list = ["Toutes"] + sorted(
            zones[zones['ISL_Terr'] == selected_isl]['SITENAME'].dropna().unique().tolist()
        )
        selected_site = st.sidebar.selectbox(
            "Site",
            site_list,
            key="oos_site_filter"
        )

    uploaded_file = st.file_uploader(
        "Upload le fichier Listing Out of Stock (OOS)",
        type=["xlsx", "xls", "csv"],
        key="oos_file"
    )

    if uploaded_file:
        with st.spinner("Traitement du fichier OOS..."):
            df_oos = load_file(uploaded_file)
            df_oos.columns = [col.strip() for col in df_oos.columns]

            # Nettoyage Agent MSISDN
            if 'Agent MSISDN' in df_oos.columns:
                df_oos['Agent MSISDN'] = df_oos['Agent MSISDN'].astype(str).str.strip()
                df_oos['Numero du client'] = df_oos['Agent MSISDN']
            else:
                st.error("Colonne 'Agent MSISDN' non trouvée dans le fichier OOS.")
                st.stop()

            # ===================== FILTRE HVC =====================
            if 'segment_group' in df_oos.columns:
                df_oos = df_oos[df_oos['segment_group'].astype(str).str.upper() == '1-HVC'].copy()
                st.success(f"Filtré sur HVC uniquement → {len(df_oos)} lignes")
            else:
                st.warning("Colonne 'segment_group' non trouvée. Tous les enregistrements sont affichés.")

            # ===================== ENRICHISSEMENT ZONES =====================
            if 'SITENAME' in df_oos.columns and not zones_df.empty:
                zones_clean = zones_df[['SITENAME', 'ZONE NEW', 'ISL_Terr', 'TERRITORY CORRECT']].drop_duplicates()
                zones_clean = zones_clean.rename(columns={
                    'SITENAME': 'Locality',
                    'ISL_Terr': 'Cluster',
                    'TERRITORY CORRECT': 'Territory',
                    'ZONE NEW': 'Zone'
                })
                # Merge sur SITENAME (ou la colonne correspondante)
                df_oos = df_oos.merge(zones_clean, left_on='SITENAME', right_on='Locality', how='left')

            # ===================== COMMERCIAL (DONNÉES FICTIVES) =====================
            if len(df_oos) > 0:
                np.random.seed(42)
                fict_commercials = ["Commercial_A", "Commercial_B", "Commercial_C", 
                                  "Commercial_D", "Commercial_E", "Commercial_F"]
                df_oos['Commercial'] = np.random.choice(fict_commercials, size=len(df_oos))

            # ===================== CONSTRUCTION DU TABLEAU FINAL =====================
            final_columns = [
                'Numero du client', 'Nom du client', 'Locality', 'Cluster', 'Zone',
                'Segment group', 'Day target', 'OOS target', 'Float', 'Commercial'
            ]

            # Création du DataFrame final avec colonnes sécurisées
            final_df = pd.DataFrame()

            final_df['Numero du client'] = df_oos.get('Agent MSISDN', pd.Series(range(len(df_oos))))
            final_df['Nom du client'] = df_oos.get('Nom du client', "Non renseigné")
            final_df['Locality'] = df_oos.get('Locality', df_oos.get('SITENAME', "Non renseigné"))
            final_df['Cluster'] = df_oos.get('Cluster', df_oos.get('ISL_Terr', "Non renseigné"))
            final_df['Territory'] = df_oos.get('Territory', "Non renseigné")
            final_df['Zone'] = df_oos.get('Zone', "Non renseigné")
            final_df['Segment group'] = df_oos.get('segment_group', "1-HVC")

            # ===================== APPLICATION FILTRE =====================
            if selected_zone != "Toutes":
                final_df = final_df[final_df["Zone"] == selected_zone]

            if selected_terr != "Toutes" and "Territory" in final_df.columns:
                final_df = final_df[final_df["Territory"] == selected_terr]

            if selected_isl != "Toutes" and "Cluster" in final_df.columns:
                final_df = final_df[final_df["Cluster"] == selected_isl]

            if selected_site != "Toutes" and "Locality" in final_df.columns:
                final_df = final_df[final_df["Locality"] == selected_site]
            
            # Colonnes cibles avec valeurs par défaut si absentes
            final_df['Day target'] = pd.to_numeric(df_oos.get('Day target', pd.Series([0]*len(df_oos))),errors='coerce').fillna(0)
            final_df['OOS target'] = pd.to_numeric(df_oos.get('OOS target', pd.Series([0]*len(df_oos))), errors='coerce').fillna(0)
            final_df['Float'] = pd.to_numeric(df_oos.get('Float', pd.Series([0]*len(df_oos))), errors='coerce').fillna(0)
            final_df['Commercial'] = df_oos.get('Commercial', "Non assigné")

            # ===================== COULEURS =====================
            def color_row(row):
                styles = [''] * len(row)
                for i, col in enumerate(final_df.columns):
                    if col == 'Day target':
                        styles[i] = 'background-color: #C8E6C9; color: #2E7D32'   # Vert
                    elif col == 'OOS target':
                        styles[i] = 'background-color: #FFF9C4; color: #F57F17'   # Jaune
                    elif col == 'Float':
                        styles[i] = 'background-color: #FFCDD2; color: #C62828'   # Rouge
                return styles

            styled_df = final_df.style.apply(color_row, axis=1)

            # ===================== AFFICHAGE =====================
            st.subheader(f"📋 Listing OOS Final - {len(final_df)} clients HVC")
            st.dataframe(
                styled_df,
                use_container_width=True,
                height=650
            )

            # Statistiques
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Clients HVC", len(final_df))
            with col2:
                st.metric("Commerciaux", final_df['Commercial'].nunique())
            with col3:
                st.metric("Zones", final_df['Zone'].nunique())

            # Export
            st.subheader("📥 Export")
            col_e1, col_e2 = st.columns(2)
            excel_data = to_excel(final_df)
            col_e1.download_button(
                "Télécharger EXCEL", 
                excel_data, 
                "Listing_OOS_HVC.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            col_e2.download_button(
                "Télécharger CSV", 
                final_df.to_csv(index=False).encode('utf-8'),
                "Listing_OOS_HVC.csv", 
                "text/csv"
            )

    else:
        st.info("👆 Veuillez uploader votre fichier Listing Out of Stock.")