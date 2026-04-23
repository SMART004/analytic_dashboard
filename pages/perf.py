# pages/cc_performance.py
import streamlit as st
import pandas as pd
import os
import zipfile
from utils.helpers import load_file, clean_phone, to_excel
import plotly.graph_objects as go
import dataframe_image as dfi

def format_amount(x):
    if pd.isna(x) or x == 0:
        return "0"
    if x >= 1_000_000:
        return f"{x/1_000_000:.1f}M"
    elif x >= 1_000:
        return f"{x/1_000:.0f}K"
    else:
        return f"{int(x)}"
    
def style_perf(df):
    return (
        df.style
        .set_table_styles([
            # ==============================
            # STYLE GLOBAL
            # ==============================
            {
                "selector": "th",
                "props": [
                    ("background-color", "black"),
                    ("color", "gold"),
                    ("font-weight", "bold"),
                    ("text-align", "center"),
                    ("border", "1px solid #666")
                ]
            },
            {
                "selector": "td",
                "props": [
                    ("text-align", "center"),
                    ("border", "1px solid #999")
                ]
            },

            # -------------------
            # DOTATIONS
            # col3 -> Heure
            # col4 -> Montant
            # -------------------
            {
                "selector": "th.col3, td.col3",
                "props": [
                    ("border-left", "4px solid #FFD966")
                ]
            },
            {
                "selector": "th.col4, td.col4",
                "props": [
                    ("border-right", "4px solid #FFD966")
                ]
            },

            # -------------------
            # HVC
            # col8 -> FD_HVC
            # col10 -> TR_HVC
            # -------------------
            {
                "selector": "th.col8, td.col8",
                "props": [
                    ("border-left", "4px solid #FFD966")
                ]
            },
            {
                "selector": "th.col10, td.col10",
                "props": [
                    ("border-right", "4px solid #FFD966")
                ]
            },

            # -------------------
            # OTHERS
            # col11 -> FD_Others
            # col13 -> TR_Other
            # -------------------
            {
                "selector": "th.col11, td.col11",
                "props": [
                    ("border-left", "4px solid #FFD966")
                ]
            },
            {
                "selector": "th.col13, td.col13",
                "props": [
                    ("border-right", "4px solid #FFD966")
                ]
            },

            # -------------------
            # ALL SEGMENT
            # col14 -> Σ_FD
            # col16 -> TR General
            # -------------------
            {
                "selector": "th.col14, td.col14",
                "props": [
                    ("border-left", "4px solid #FFD966")
                ]
            },
            {
                "selector": "th.col16, td.col16",
                "props": [
                    ("border-right", "4px solid #FFD966")
                ]
            },

            # -------------------
            # TREND
            # col17 -> POS_serve
            # col18 -> New
            # -------------------
            {
                "selector": "th.col17, td.col17",
                "props": [
                    ("border-left", "4px solid #FFD966")
                ]
            },
            {
                "selector": "th.col18, td.col18",
                "props": [
                    ("border-right", "4px solid #FFD966")
                ]
            },
        ])
        .set_properties(**{
            "font-size": "13px",
            "text-align": "center"
        })
    )

def show_performance():
    st.title("📈 Performance Commerciaux")

    comm_config = st.session_state.get('commercial_config_df')
    exclusion_df = st.session_state.get('exclusion_df')
    exclusion_master = st.session_state.get('exclusion_master')
    exclusion_cds = st.session_state.get('exclusion_cds')
    master_df = st.session_state.get('pos_master_df')
    # dotation_numbers = st.session_state.get('dotation_numbers', [])

    if comm_config is None:
        st.error("Veuillez charger le fichier **Configuration Commerciaux** dans Settings")
        st.stop()

    # Configuration vitesse de rotation
    st.sidebar.subheader("Configuration Rotation")
    rotation_speed = st.sidebar.number_input("Vitesse de rotation", min_value=1, value=1, step=1)
    required_tours = 3 * rotation_speed   # ex: si vitesse=2 → 6 tours

    trans_files = st.file_uploader(
        "Upload les fichiers de transactions (plusieurs possibles)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="perf_trans"
    )

    if not trans_files:
        st.info("Veuillez uploader les fichiers de transactions.")
        return

    with st.spinner("Analyse des performances en cours..."):
        df_list = []
        for f in trans_files:
            temp = load_file(f)
            temp['source_file'] = f.name
            df_list.append(temp)

        df = pd.concat(df_list, ignore_index=True)
        df.columns = [col.strip() for col in df.columns]

        # Nettoyage
        df['Date'] = pd.to_datetime(df.get('Date'), errors='coerce')
        df['Amount'] = pd.to_numeric(df.get('Amount'), errors='coerce').abs()

        if 'From' in df.columns:
            df['From_clean'] = df['From'].apply(clean_phone)
        if 'To' in df.columns:
            df['To_clean'] = df['To'].apply(clean_phone)

        df_full = df.copy()

        # ===================== FILTRE DATE =====================
        df['Date_only'] = df['Date'].dt.date
        df_full['Date_only'] = df_full['Date'].dt.date

        min_date = df['Date_only'].min()
        max_date = df['Date_only'].max()

        date_range = st.sidebar.date_input(
            "Filtre Date",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df['Date_only'] >= start_date) & (df['Date_only'] <= end_date)]

        # # ===================== SELECTION JOUR =====================
        # available_dates = sorted(df['Date_only'].dropna().unique())

        # selected_date = st.selectbox(
        #     "📅 Choisir une date",
        #     available_dates
        # )

        # # Filtrer sur UNE seule date
        # df = df[df['Date_only'] == selected_date]
        
        # ===================== EXCLUSIONS GLOBAL COMPLET =====================
        commercial_numbers = comm_config['Ccial_MSISDN'].astype(str).str.strip().tolist()

        # Caisses (ancien exclusion_df)
        caisses = []
        if exclusion_df is not None and 'NUM' in exclusion_df.columns:
            caisses = exclusion_df['NUM'].apply(clean_phone).astype(str).tolist()

        # Masters (nouveau fichier Settings)
        masters_excl = []
        if exclusion_master is not None and 'NUM' in exclusion_master.columns:
            masters_excl = exclusion_master['NUM'].apply(clean_phone).astype(str).tolist()

        # CDS (nouveau fichier Settings)
        cds_excl = []
        if exclusion_cds is not None and 'NUM' in exclusion_cds.columns:
            cds_excl = exclusion_cds['NUM'].apply(clean_phone).astype(str).tolist()

        global_excluded = set(
            commercial_numbers +
            caisses +
            masters_excl +
            cds_excl
        )

        # ===================== IDENTIFICATION COMMERCIAUX =====================
        comm_config = comm_config.copy()
        comm_config['Ccial_MSISDN'] = comm_config['Ccial_MSISDN'].astype(str).str.strip()

        # On identifie le commercial uniquement via From (comme demandé)
        df = df.merge(
            comm_config[['Ccial_MSISDN', 'Nom_Ccial', 'Zone_Territoire', 'Zone_SA']],
            left_on='From_clean',
            right_on='Ccial_MSISDN',
            how='left'
        )

        df = df[df['Nom_Ccial'].notna()].copy()
        df = df.groupby('source_file', group_keys=False).apply(
                lambda x: x[x['Nom_Ccial'] == x['Nom_Ccial'].value_counts().idxmax()]
            )

        if df.empty:
            st.error("Aucun commercial trouvé")
            st.stop()

        # Filtre Zone_Territoire
        zone_list = ["Toutes"] + sorted(df['Zone_Territoire'].dropna().unique().tolist())
        selected_terr = st.sidebar.selectbox("Filtre Zone_Territoire", zone_list)
        if selected_terr != "Toutes":
            df = df[df['Zone_Territoire'] == selected_terr]

        # ===================== DOTATION =====================
        # -----------------------------
        # Liste des numéros MASTER
        # -----------------------------
        master_numbers = set()
        if exclusion_master is not None and 'NUM' in exclusion_master.columns:
            master_numbers = set(
                exclusion_master['NUM']
                .apply(clean_phone)
                .astype(str)
            )

        # -----------------------------
        # Liste des numéros CAISSE
        # -----------------------------
        caisse_numbers = set()
        if exclusion_df is not None and 'NUM' in exclusion_df.columns:
            caisse_numbers = set(
                exclusion_df['NUM']
                .apply(clean_phone)
                .astype(str)
            )

        # -----------------------------
        # Transactions candidates
        # uniquement avant 11h
        # uniquement vers les commerciaux
        # -----------------------------
        dotation_trans = df_full[
            (df_full['Type'] == "Transfer") &
            (df_full['To_clean'].isin(df['From_clean'])) &
            (df_full['Date'].dt.hour < 11)
        ].copy()

        # identification du type de source
        def get_source_type(x):
            if x in master_numbers:
                return "MASTER"
            elif x in caisse_numbers:
                return "CAISSE"
            return None

        dotation_trans["Source_Type"] = dotation_trans["From_clean"].apply(get_source_type)

        # garder uniquement MASTER + CAISSE
        dotation_trans = dotation_trans[
            dotation_trans["Source_Type"].notna()
        ].copy()

        # rattacher le commercial
        dotation_trans = dotation_trans.merge(
            df[['source_file', 'Nom_Ccial']]
            .drop_duplicates(),
            on='source_file',
            how='left'
        )

        results = []

        # ===================================================
        # LOGIQUE :
        # - 1 seule transaction MASTER (la première)
        # - 1 seule transaction CAISSE (la première)
        # - max 2 transactions au total
        # ===================================================
        for (date, nom), group in dotation_trans.groupby(['Date_only', 'Nom_Ccial']):

            g = group.sort_values("Date")

            # première transaction MASTER
            first_master = g[
                g["Source_Type"] == "MASTER"
            ].head(1)

            # première transaction CAISSE
            first_caisse = g[
                g["Source_Type"] == "CAISSE"
            ].head(1)

            # concat des deux (max 2 lignes)
            final_tx = pd.concat([
                first_master,
                first_caisse
            ])

            montant = final_tx["Amount"].sum()

            heure = (
                final_tx["Date"].min().strftime("%H:%M")
                if not final_tx.empty else "N/A"
            )

            results.append({
                "Date_only": date,
                "Nom_Ccial": nom,
                "Heure_Dotation": heure,
                "Montant_Dotation": montant
            })

        dotation_group = pd.DataFrame(results)

        # sécurité si vide
        if dotation_group.empty:
            dotation_group = pd.DataFrame(columns=[
                "Date_only",
                "Nom_Ccial",
                "Heure_Dotation",
                "Montant_Dotation"
            ])

        # ===================== CALCULS PRINCIPAUX =====================
        trans_df = df[
            (df['Type'] == "Transfer") & 
            (df['From_clean'].isin(comm_config['Ccial_MSISDN'].astype(str)))
        ].copy()

        def compute_perf(group):
            valid_trans = group[
                (~group['To_clean'].isin(global_excluded)) &
                (group['To_clean'].notna())
            ]

            filtered = valid_trans[valid_trans['Amount'] >= 10000]

            first_date = filtered['Date'].min()
            if pd.notna(first_date):
                premiere = first_date.strftime('%H:%M')
            else:
                premiere = 'N/A'

            last_date = filtered['Date'].max()
            if pd.notna(last_date):
                derniere = last_date.strftime('%H:%M')
            else:
                derniere = 'N/A'

            return pd.Series({
                # 'Date_only': group['Date_only'].iloc[0],
                'Zone_Territoire': group['Zone_Territoire'].iloc[0],
                'Zone_SA': group['Zone_SA'].iloc[0],
                'Premiere_Trans': premiere,
                'Derniere_Trans': derniere,
                'Nb_Transactions': filtered['Amount'].count()
            })

        perf = trans_df.groupby(['Date_only', 'Nom_Ccial']).apply(compute_perf).reset_index()

        perf = perf.merge(dotation_group, on=['Date_only', 'Nom_Ccial'], how='left')
        perf['Montant_Dotation'] = perf['Montant_Dotation'].fillna(0)
        perf['Heure_Dotation'] = perf['Heure_Dotation'].fillna('N/A')

        # ===================== HVC =====================
        hvc_trans = df[
            (df['Type'] == "Transfer") &
            (df['From_clean'].isin(commercial_numbers)) &
            (~df['To_clean'].isin(global_excluded)) &
            (df['To_clean'].notna()) &
            (df['Amount'] >= 10000)
        ].copy()

        if master_df is not None and 'Segment Group' in master_df.columns:
            hvc_msidsn = master_df[master_df['Segment Group'].astype(str).str.strip() == "1-HVC"]['MSISDN'].astype(str).str.strip().tolist()
            hvc_trans = hvc_trans[hvc_trans['To_clean'].isin(hvc_msidsn)]

        hvc_group = hvc_trans.groupby(['Date_only', 'Nom_Ccial']).agg(
            FD_HVC=('Amount', 'sum'),
            HVC_Serve=('To_clean', 'nunique'),
            Nb_Trans_HVC=('Amount', 'count')
        ).reset_index()

        perf = perf.merge(
            hvc_group,
            on=['Date_only','Nom_Ccial'],
            how='left'
        ).fillna(0)

        # Tour de rotation HVC
        perf['TR_HVC'] = ((perf['Nb_Trans_HVC'] / (perf['HVC_Serve'] * required_tours)) * 100).round(1)
        perf['TR_HVC'] = perf['TR_HVC'].fillna(0)
        
        # ===================== OTHERS (MVC + LVC + Inconnus) =====================
        # Clients inconnus (non dans Master et non dans exclusion) sont considérés comme LVC

        other_trans = df[
            (df['Type'] == "Transfer") &
            (df['From_clean'].isin(commercial_numbers)) &
            (~df['To_clean'].isin(global_excluded)) &
            (df['To_clean'].notna()) &
            (df['Amount'] >= 10000)
        ].copy()

        # Exclure uniquement HVC
        if master_df is not None:
            hvc_list = master_df[
                master_df['Segment Group'].astype(str).str.strip() == "1-HVC"
            ]['MSISDN'].astype(str).str.strip().tolist()

            other_trans = other_trans[
                ~other_trans['To_clean'].isin(hvc_list)
            ]

        other_group = other_trans.groupby(['Date_only','Nom_Ccial']).agg(
            FD_Others=('Amount', 'sum'),
            Other_Serve=('To_clean', 'nunique'),
            Nb_Trans_Other=('Amount', 'count')
        ).reset_index()

        perf = perf.merge(other_group,
            on=['Date_only','Nom_Ccial'],
            how='left').fillna(0)

        perf['TR_Other'] = ((perf['Nb_Trans_Other'] / (perf['Other_Serve'] * required_tours)) * 100).round(1)
        perf['TR_Other'] = perf['TR_Other'].fillna(0)

        # ===================== ALL SEGMENT =====================
        perf['Σ_FD'] = perf['FD_HVC'] + perf['FD_Others']
        perf['Σ_POS_Serve'] = perf['HVC_Serve'] + perf['Other_Serve']
        perf['TR_General'] = ((perf['Nb_Transactions'] / ((perf['HVC_Serve'] + perf['Other_Serve'])* required_tours)) * 100).round(1)

        # On supprime les colonnes de calcul intermédiaire pour ne pas les afficher
        columns_to_drop = ['Nb_Trans_HVC', 'Nb_Trans_Other']
        perf = perf.drop(columns=[col for col in columns_to_drop if col in perf.columns], errors='ignore')

        perf['Alerte_Dotation'] = perf['Montant_Dotation'] > 4_000_000
        perf['Montant_Dotation_raw'] = perf['Montant_Dotation']

        # ===================== FORMATAGE =====================
        for col in ['Montant_Dotation', 'FD_HVC', 'FD_Others', 'Σ_FD']:
            perf[col] = perf[col].apply(format_amount)

        perf['TR_HVC'] = perf['TR_HVC'].apply(lambda x: f"{x:.1f}%")
        perf['TR_Other'] = perf['TR_Other'].apply(lambda x: f"{x:.1f}%")
        perf['TR_General'] = perf['TR_General'].apply(lambda x: f"{x:.1f}%")

        # ===================== TREND [14h-17h] =====================
        df['Hour'] = df['Date'].dt.hour
        trend_df = df[
            (df['Type']=="Transfer") &
            (df['From_clean'].isin(commercial_numbers)) &
            (df['Amount'] >= 10000)
        ].copy()

        trend_14_17 = trend_df[
            (trend_df['Hour'] >= 14) &
            (trend_df['Hour'] < 17) &
            (~trend_df['To_clean'].isin(global_excluded)) &
            (trend_df['To_clean'].notna())
        ]

        trend_6_14 = trend_df[
            (trend_df['Hour'] >= 6) &
            (trend_df['Hour'] <= 13) &
            (~trend_df['To_clean'].isin(global_excluded)) &
            (trend_df['To_clean'].notna())
        ]

        pos_serve_14_17 = trend_14_17.groupby(['Date_only','Nom_Ccial'])['To_clean']\
        .nunique().reset_index(name='POS_serve')

        new_clients = trend_14_17[
            ~trend_14_17['To_clean'].isin(trend_6_14['To_clean'])
        ]

        new_14_17 = new_clients.groupby(['Date_only','Nom_Ccial'])['To_clean']\
            .nunique().reset_index(name='New')

        trend = pos_serve_14_17.merge(new_14_17, 
            on=['Date_only','Nom_Ccial'], how='left')

        perf = perf.merge(trend, on=['Date_only','Nom_Ccial'], how='left')

        perf['POS_serve'] = pd.to_numeric(perf['POS_serve'], errors='coerce').fillna(0).astype(int)
        perf['New'] = pd.to_numeric(perf['New'], errors='coerce').fillna(0).astype(int)
        perf['HVC_Serve'] = pd.to_numeric(perf['HVC_Serve'], errors='coerce').fillna(0).astype(int)
        perf['Other_Serve'] = pd.to_numeric(perf['Other_Serve'], errors='coerce').fillna(0).astype(int)
        perf['Σ_POS_Serve'] = pd.to_numeric(perf['Σ_POS_Serve'], errors='coerce').fillna(0).astype(int)
        perf = perf.rename(columns={'Date_only': 'Date'})
        
        # ===================== AFFICHAGE =====================
        # ===================== MULTI-INDEX HEADER =====================
        columns = pd.MultiIndex.from_tuples([
            ("", "Date"),
            ("", "Zone_SA"),
            ("", "Nom_Ccial"),

            ("Dotations", "Heure"),
            ("Dotations", "Montant"),

            ("Transactions (Transfert)", "Première"),
            ("Transactions (Transfert)", "Dernière"),
            ("Transactions (Transfert)", "Σ trx"),

            ("HVC", "FD_HVC"),
            ("HVC", "HVC_Serve"),
            ("HVC", "TR_HVC"),

            ("Others", "FD_Others"),
            ("Others", "Other_Serve"),
            ("Others", "TR_Other"),

            ("All segment", "Σ_FD"),
            ("All segment", "Σ_POS Serve"),
            ("All segment", "TR General"),

            ("TREND [14H→17H]", "POS_serve"),
            ("TREND [14H→17H]", "New")
        ])

        # def highlight_dotation(row):
        #     if row[("Dotations", "Montant")] and row.name is not None:
        #         val = perf.loc[row.name, 'Montant_Dotation_raw']
        #         if val > 4_000_000:
        #             return ['background-color: red'] * len(row)
        #     return [''] * len(row)
        
        perf_display = perf.copy()

        perf_display = perf_display[[
            'Date',
            'Zone_SA', 'Nom_Ccial',
            'Heure_Dotation', 'Montant_Dotation',
            'Premiere_Trans', 'Derniere_Trans', 'Nb_Transactions',
            'FD_HVC', 'HVC_Serve', 'TR_HVC',
            'FD_Others', 'Other_Serve', 'TR_Other',
            'Σ_FD', 'Σ_POS_Serve', 'TR_General',
            'POS_serve', 'New'
        ]]

        perf_display.columns = columns

        st.subheader("Performance Détaillée des Commerciaux")
        styled_df = style_perf(perf_display)
        # styled_df = styled_df.apply(highlight_dotation, axis=1)

        st.dataframe(styled_df, use_container_width=True, height=650)

        # ===================== RADAR CHARTS =====================
        st.subheader("📡 Radar Performance Commercial")

        # --------------------------------------------------
        # FILTRE Zone_SA (une seule)
        # --------------------------------------------------
        zone_sa_list = sorted(
            perf['Zone_SA'].dropna().unique().tolist()
        )

        selected_zone_sa = st.selectbox(
            "Choisir une Zone_SA",
            zone_sa_list,
            key="radar_zone_sa"
        )

        perf_radar = perf[
            perf['Zone_SA'] == selected_zone_sa
        ].copy()

        # --------------------------------------------------
        # FILTRE Commercial (un seul)
        # --------------------------------------------------
        commercial_list = sorted(
            perf_radar['Nom_Ccial'].dropna().unique().tolist()
        )

        selected_commercial = st.selectbox(
            "Choisir un Commercial",
            commercial_list,
            key="radar_commercial"
        )

        perf_radar = perf_radar[
            perf_radar['Nom_Ccial'] == selected_commercial
        ].copy()

        # --------------------------------------------------
        # Fonction conversion montant formaté -> numérique
        # --------------------------------------------------
        def clean_amount(x):
            if pd.isna(x):
                return 0

            if isinstance(x, str):
                x = x.strip()

                # ex: 3.5M -> 3500000
                if "M" in x:
                    try:
                        return float(x.replace("M", "").replace(",", "").strip()) * 1_000_000
                    except:
                        return 0

                # ex: 850K -> 850000
                if "K" in x:
                    try:
                        return float(x.replace("K", "").replace(",", "").strip()) * 1_000
                    except:
                        return 0

                try:
                    return float(x.replace(",", ""))
                except:
                    return 0

            return x

        # --------------------------------------------------
        # Si données disponibles
        # --------------------------------------------------
        if not perf_radar.empty:

            # ==================================================
            # RADAR 1 : FINANCIER
            # ==================================================
            financial_data = {
                "Dotation": perf_radar["Montant_Dotation"].apply(clean_amount).sum(),
                "FD_HVC": perf_radar["FD_HVC"].apply(clean_amount).sum(),
                "FD_Others": perf_radar["FD_Others"].apply(clean_amount).sum(),
                "Σ_FD": perf_radar["Σ_FD"].apply(clean_amount).sum(),
            }

            financial_categories = list(financial_data.keys())
            financial_values = list(financial_data.values())

            # fermeture radar
            financial_categories += financial_categories[:1]
            financial_values += financial_values[:1]

            fig_financial = go.Figure()

            fig_financial.add_trace(go.Scatterpolar(
                r=financial_values,
                theta=financial_categories,
                fill='toself',
                name=selected_commercial
            ))

            fig_financial.update_layout(
                title=f"Radar Financier — {selected_commercial}",
                polar=dict(
                    radialaxis=dict(
                        visible=True
                    )
                ),
                showlegend=False
            )

            st.plotly_chart(fig_financial, use_container_width=True)

            # ==================================================
            # RADAR 2 : ACTIVITÉ
            # ==================================================
            activity_data = {
                "HVC_Serve": pd.to_numeric(
                    perf_radar["HVC_Serve"],
                    errors="coerce"
                ).fillna(0).sum(),

                "Other_Serve": pd.to_numeric(
                    perf_radar["Other_Serve"],
                    errors="coerce"
                ).fillna(0).sum(),

                "Σ_POS_Serve": pd.to_numeric(
                    perf_radar["Σ_POS_Serve"],
                    errors="coerce"
                ).fillna(0).sum(),

                "POS_serve": pd.to_numeric(
                    perf_radar["POS_serve"],
                    errors="coerce"
                ).fillna(0).sum(),

                "New": pd.to_numeric(
                    perf_radar["New"],
                    errors="coerce"
                ).fillna(0).sum(),

                "Nb_Transactions": pd.to_numeric(
                    perf_radar["Nb_Transactions"],
                    errors="coerce"
                ).fillna(0).sum(),
            }

            activity_categories = list(activity_data.keys())
            activity_values = list(activity_data.values())

            # fermeture radar
            activity_categories += activity_categories[:1]
            activity_values += activity_values[:1]

            fig_activity = go.Figure()

            fig_activity.add_trace(go.Scatterpolar(
                r=activity_values,
                theta=activity_categories,
                fill='toself',
                name=selected_commercial
            ))

            fig_activity.update_layout(
                title=f"Radar Activité — {selected_commercial}",
                polar=dict(
                    radialaxis=dict(
                        visible=True
                    )
                ),
                showlegend=False
            )

            st.plotly_chart(fig_activity, use_container_width=True)

        else:
            st.warning("Aucune donnée disponible pour ce commercial.")

        # ===================== DEBUG DOTATION =====================
        # st.subheader("🔍 Analyse des dotations élevées (>4M)")

        # alert_commerciaux = perf[perf['Montant_Dotation_raw'] > 4_000_000]['Nom_Ccial'].unique()

        # if len(alert_commerciaux) > 0:

        #     selected_ccial = st.selectbox(
        #         "Choisir un commercial",
        #         alert_commerciaux
        #     )

        #     selected_date = st.selectbox(
        #         "Choisir une date",
        #         perf[perf['Nom_Ccial'] == selected_ccial]['Date'].unique()
        #     )

        #     debug_df = dotation_trans[
        #         (dotation_trans['Nom_Ccial'] == selected_ccial) &
        #         (dotation_trans['Date_only'] == selected_date)
        #     ].sort_values('Date')

        #     debug_df['Type_Source'] = debug_df['From_clean'].apply(
        #         lambda x: "MASTER" if x in masters_excl else "CAISSE"
        #     )

        #     st.write("### 📊 Transactions de dotation")
        #     st.dataframe(debug_df)

        #     st.write("### 💰 Somme totale")
        #     st.write(debug_df['Amount'].sum())

        #     st.write("### 🔎 Répartition")
        #     st.write(debug_df.groupby('Type_Source')['Amount'].sum())

        #     st.write("### 🔢 Nombre de transactions")
        #     st.write(len(debug_df))

        # else:
        #     st.success("✅ Aucun dépassement de dotation")

        # Export
        # ===================== EXPORT IMAGE PAR BLOCS =====================

        if st.button("📸 Capturer tableau en images (20 lignes par image)"):

            # dossier temporaire
            export_folder = "exports_perf"
            os.makedirs(export_folder, exist_ok=True)

            # supprimer anciens fichiers
            for file in os.listdir(export_folder):
                file_path = os.path.join(export_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            # nombre de lignes par image
            chunk_size = 20

            # ici on utilise perf_display (pas styled_df)
            total_rows = len(perf_display)

            for i in range(0, total_rows, chunk_size):
                chunk = perf_display.iloc[i:i + chunk_size].copy()

                # réappliquer le style sur chaque bloc
                styled_chunk = style_perf(chunk)

                file_name = f"performance_part_{(i // chunk_size) + 1}.png"
                file_path = os.path.join(export_folder, file_name)

                dfi.export(
                    styled_chunk,
                    file_path,
                    table_conversion="chrome"
                )

            # créer zip
            zip_path = os.path.join(export_folder, "Performance_Commerciaux.zip")

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
                    "Performance_Commerciaux.zip",
                    "application/zip"
                )
        col1, col2 = st.columns(2)
        excel_data = to_excel(perf)
        col1.download_button("Télécharger Excel", excel_data, "Performance_Commerciaux.xlsx")
        col2.download_button("Télécharger CSV", perf.to_csv(index=False).encode('utf-8'), "Performance_Commerciaux.csv")