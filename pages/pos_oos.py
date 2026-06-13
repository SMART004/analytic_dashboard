# pages/pos_oos_listing.py
import streamlit as st
import pandas as pd
from utils.helpers import clean_phone, to_excel
import dataframe_image as dfi
import os
import zipfile
from utils.storage import upload_with_folder, get_one_folder
from utils.supabase import supabase

BUCKET_NAME = "listing-oos-result-files"

def show_pos_oos_listing():
    st.title("Listing POS OOS")

    # Récupération du fichier Maître POS
    pos_master = st.session_state.get('pos_master_df')
    if pos_master is None:
        st.error("Veuillez charger le **Fichier Maître POS** dans Settings")
        st.stop()
    
    zones_df = st.session_state.get('zones_df')
    if zones_df is None:
        st.error("Veuillez charger le fichier **Zones** dans Settings")
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
    # ===============================
    # INIT SESSION STATE
    # ===============================
    if "files_uploaded" not in st.session_state:
        st.session_state.files_uploaded = False

    if "last_uploaded_files" not in st.session_state:
        st.session_state.last_uploaded_files = []

    # Upload multiple fichiers de transactions
    transaction_files = st.file_uploader(
        "Upload les fichiers de transactions (plusieurs possibles)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pos_transactions"
    )

    # ===============================
    # DETECT NEW FILES
    # ===============================
    if transaction_files:
        current_names = [f.name for f in transaction_files]

        if current_names != st.session_state.last_uploaded_files:
            st.session_state.files_uploaded = False
            st.session_state.last_uploaded_files = current_names
            
    

    # ===============================
    # UPLOAD LOGIC (SAFE)
    # ===============================
    if transaction_files and not st.session_state.files_uploaded:
            result = upload_with_folder(
                supabase=supabase, 
                files=transaction_files,
                bucket=BUCKET_NAME
            )
            
            if result:
                get_one_folder.clear()
                st.session_state.files_uploaded = True
                st.rerun()

    # ===============================
    # RESET BUTTON (OPTIONNEL)
    # ===============================
    if st.button("🔄 Réinitialiser les uploads"):
        st.session_state.files_uploaded = False
        st.session_state.last_uploaded_files = []
        st.info("Upload réinitialisé")

    with st.spinner("Identification des POS présents dans les transactions..."):
        # Combinaison des fichiers transactions
        df_trans = get_one_folder(
            bucket=BUCKET_NAME
        )

        if df_trans is None or df_trans.empty:
            st.warning("Aucun fichier de transactions trouvé")
            st.stop()

        # Nettoyage des numéros From et To
        if 'From' in df_trans.columns:
            df_trans['From_clean'] = df_trans['From'].apply(clean_phone)
        if 'To' in df_trans.columns:
            df_trans['To_clean'] = df_trans['To'].apply(clean_phone)
        
        
        # =========================================================
        # 🧹 SUPPRESSION DES DOUBLONS (CRITIQUE)
        # =========================================================
        df_trans = df_trans.drop_duplicates(
            subset=['Date', 'From_clean', 'To_clean', 'Amount', 'Type'],
            keep='last'
        )

        st.info(f"🧹 Après déduplication : {len(df_trans)} lignes")

        # Création d'une colonne MSISDN unique à partir de From ou To
        df_trans['MSISDN'] = df_trans['To_clean'].fillna(df_trans['From_clean'])

        # Suppression des lignes sans MSISDN valide
        df_trans = df_trans[df_trans['MSISDN'].notna()].copy()

        # ===================== FILTRE DE DATE =====================
        if 'Date' in df_trans.columns:
            df_trans['Date'] = pd.to_datetime(df_trans['Date'], errors='coerce')

            st.subheader("Filtre de Date")
            col_d1, col_d2 = st.columns(2)

            min_date = df_trans['Date'].min().date() if not df_trans['Date'].isna().all() else None
            max_date = df_trans['Date'].max().date() if not df_trans['Date'].isna().all() else None

            with col_d1:
                date_debut = st.date_input("Date début", value=min_date)
            with col_d2:
                date_fin = st.date_input("Date fin", value=max_date)

            # Application du filtre date
            df_trans = df_trans[
                (df_trans['Date'].dt.date >= date_debut) &
                (df_trans['Date'].dt.date <= date_fin)
            ].copy()

        # ===================== DERNIÈRE BALANCE =====================
        if 'Balance' not in df_trans.columns:
            st.error("Colonne 'Balance' non trouvée dans les fichiers de transactions.")
            st.stop()

        df_trans['Balance'] = pd.to_numeric(df_trans['Balance'], errors='coerce')

        # Trier pour garder la dernière transaction par MSISDN
        if 'Date' in df_trans.columns:
            df_trans['Date'] = pd.to_datetime(df_trans['Date'], errors='coerce')
            df_trans = df_trans.sort_values(['MSISDN', 'Date'], ascending=[True, False])
        else:
            df_trans = df_trans.sort_values('MSISDN')

        latest_balance = df_trans.drop_duplicates(subset=['MSISDN'], keep='first')[['MSISDN', 'Balance', 'Date']]
        latest_balance = latest_balance.rename(columns={'Balance': 'Float'})

        # ===================== RÉCUPÉRATION NOM DU CLIENT =====================
        name_col = None
        for col in ['To Name', 'From Name', 'To_Name', 'From_Name', 'Name']:
            if col in df_trans.columns:
                name_col = col
                break

        if name_col:
            client_names = df_trans.groupby('MSISDN')[name_col].first().reset_index()
            client_names = client_names.rename(columns={name_col: 'Nom du POS'})
        else:
            client_names = pd.DataFrame(columns=['MSISDN', 'Nom du client'])

        # ===================== FUSION AVEC FICHIER MAÎTRE =====================
        pos_master.columns = [col.strip() for col in pos_master.columns]

        if 'MSISDN' in pos_master.columns:
            pos_master['MSISDN'] = pos_master['MSISDN'].astype(str).str.strip()
        elif 'Agent MSISDN' in pos_master.columns:
            pos_master['MSISDN'] = pos_master['Agent MSISDN'].astype(str).str.strip()

        # Inner join → seulement les POS présents dans les transactions
        final_df = pos_master.merge(latest_balance, on='MSISDN', how='inner')
        final_df = final_df.merge(client_names, on='MSISDN', how='left')

        final_df['Float'] = final_df['Float'].fillna(0)
        final_df['Nom du POS'] = final_df.get('Nom du POS', "Non trouvé")

        # Filtre : Float < OOS_Target
        final_df = final_df[final_df['Float'] < final_df['OOS_Target']].copy()

        # ===================== FILTRE HVC =====================
        if 'Segment Group' in final_df.columns:
            final_df = final_df[final_df['Segment Group'].astype(str).str.upper() == '1-HVC'].copy()
            st.success(f"Filtré sur HVC uniquement → {len(final_df)} lignes")
        else:
            st.warning("Colonne 'Segment Group' non trouvée. Tous les enregistrements sont affichés.")

        # ===================== COLONNES FINALES =====================
        final_df = final_df.rename(columns={
            'Segment Group': 'Segment group',
            'Day_Target': 'Day Target',
            'OOS_Target': 'OOS Target'
        })

        display_df = pd.DataFrame()
        display_df['Numero du POS'] = final_df['MSISDN']
        display_df['Nom du POS'] = final_df['Nom du POS']
        display_df['Locality'] = final_df.get('Locality', final_df.get('SA Name', "N/A"))
        display_df['Cluster'] = final_df.get('Cluster', "N/A")
        display_df['Territory'] = final_df.get('Territory', "Non renseigné")
        display_df['Zone'] = final_df.get('Zone', "N/A")
        display_df['Segment group'] = final_df.get('Segment group', "N/A")

        # ===================== APPLICATION FILTRE =====================
        if selected_zone != "Toutes":
            display_df = display_df[display_df["Zone"] == selected_zone]

        if selected_terr != "Toutes" and "Territory" in final_df.columns:
            display_df = display_df[display_df["Territory"] == selected_terr]

        if selected_isl != "Toutes" and "Cluster" in final_df.columns:
            display_df = display_df[display_df["Cluster"] == selected_isl]

        if selected_site != "Toutes" and "Locality" in final_df.columns:
            display_df = display_df[display_df["Locality"] == selected_site]
        
        # Conversion en entiers
        display_df['Day Target'] = pd.to_numeric(final_df.get('Day Target', 0), errors='coerce').fillna(0).astype(int)
        display_df['OOS Target'] = pd.to_numeric(final_df.get('OOS Target', 0), errors='coerce').fillna(0).astype(int)
        display_df['Float'] = pd.to_numeric(final_df.get('Float', 0), errors='coerce').fillna(0).astype(int)
        # ===================== FORMATAGE DATE =====================
        if 'Date' in final_df.columns:
            display_df['Date dernière balance'] = final_df['Date'].dt.strftime('%d/%m/%Y %H:%M')
        else:
            display_df['Date dernière balance'] = "N/A"

        # ===================== COULEURS =====================
        def color_row(row):
            styles = [''] * len(row)
            for i, col in enumerate(display_df.columns):
                if col == 'Day Target':
                    styles[i] = 'background-color: #C8E6C9; color: #2E7D32'
                elif col == 'OOS Target':
                    styles[i] = 'background-color: #FFF9C4; color: #F57F17'
                elif col == 'Float':
                    styles[i] = 'background-color: #FFCDD2; color: #C62828'
            return styles

        styled_df = display_df.style.apply(color_row, axis=1)

        # ===================== AFFICHAGE =====================
        st.subheader(f"{len(display_df)} POS en rupture")

        st.dataframe(
            styled_df,
            use_container_width=True,
            height=650
        )

        # Export
        # ===================== EXPORT IMAGE PAR BLOCS =====================

        if st.button("📸 Capturer tableau en images (20 lignes par image)"):

            # dossier temporaire
            export_folder = "exports_pos_oos"
            os.makedirs(export_folder, exist_ok=True)

            # supprimer anciens fichiers
            for file in os.listdir(export_folder):
                file_path = os.path.join(export_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            # nombre de lignes par image
            chunk_size = 20

            # ici on utilise perf_display (pas styled_df)
            total_rows = len(display_df)

            for i in range(0, total_rows, chunk_size):
                chunk = display_df.iloc[i:i + chunk_size].copy()

                # réappliquer le style sur chaque bloc
                styled_chunk = styled_df(chunk)

                file_name = f"pos_oos_part_{(i // chunk_size) + 1}.png"
                file_path = os.path.join(export_folder, file_name)

                dfi.export(
                    styled_chunk,
                    file_path,
                    table_conversion="chrome"
                )

            # créer zip
            zip_path = os.path.join(export_folder, "POS_OOS.zip")

            with zipfile.ZipFile(zip_path, "w") as zipf:
                for file in os.listdir(export_folder):
                    if file.endswith(".png"):
                        zipf.write(
                            os.path.join(export_folder, file),
                            arcname=file
                        )

            # bouton téléchargement ZIP
            with open(zip_path, "rb") as f:
                st.download_button(
                    "📥 Télécharger toutes les captures (ZIP)",
                    f,
                    "POS_OOS.zip",
                    "application/zip"
                )
        col_e1, col_e2 = st.columns(2)
        excel_data = to_excel(display_df)
        col_e1.download_button("Télécharger EXCEL", excel_data, "POS_OOS_Alert.xlsx",
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        col_e2.download_button("Télécharger CSV", display_df.to_csv(index=False).encode('utf-8'),
                                "POS_OOS_Alert.csv", "text/csv")