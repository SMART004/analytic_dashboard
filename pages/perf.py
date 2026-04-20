# pages/cc_performance.py
import streamlit as st
import pandas as pd
from st_aggrid import GridOptionsBuilder, AgGrid, GridUpdateMode, ColumnsAutoSizeMode
from utils.helpers import load_file, clean_phone, to_excel
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
    return df.style.set_table_styles([
        {
            "selector": "th",
            "props": [
                ("background-color", "black"),
                ("color", "gold"),
                ("text-align", "center"),
                ("font-weight", "bold"),
                ("border", "1px solid white")
            ]
        },
        {
            "selector": "td",
            "props": [
                ("text-align", "center"),
                ("border", "1px solid #444")
            ]
        }
    ])
    

def show_performance():
    st.title("📈 Performance Commerciaux")

    comm_config = st.session_state.get('commercial_config_df')
    exclusion_df = st.session_state.get('exclusion_df')
    master_df = st.session_state.get('pos_master_df')
    dotation_numbers = st.session_state.get('dotation_numbers', [])

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
        df_list = [load_file(f) for f in trans_files]
        df = pd.concat(df_list, ignore_index=True)
        df.columns = [col.strip() for col in df.columns]

        # Nettoyage
        df['Date'] = pd.to_datetime(df.get('Date'), errors='coerce')
        df['Amount'] = pd.to_numeric(df.get('Amount'), errors='coerce').abs()

        if 'From' in df.columns:
            df['From_clean'] = df['From'].apply(clean_phone)
        if 'To' in df.columns:
            df['To_clean'] = df['To'].apply(clean_phone)

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

        
        df_to = df.merge(
            comm_config[['Ccial_MSISDN', 'Nom_Ccial']],
            left_on='To_clean',
            right_on='Ccial_MSISDN',
            how='left',
            suffixes=('', '_to')
        )

        df['Nom_Ccial'] = df['Nom_Ccial'].fillna(df_to['Nom_Ccial_to'])
        df = df[df['Nom_Ccial'].notna()].copy()

        if df.empty:
            st.error("Aucun commercial trouvé")
            st.stop()

        # Filtre Zone_Territoire
        zone_list = ["Toutes"] + sorted(df['Zone_Territoire'].dropna().unique().tolist())
        selected_terr = st.sidebar.selectbox("Filtre Zone_Territoire", zone_list)
        if selected_terr != "Toutes":
            df = df[df['Zone_Territoire'] == selected_terr]

        # ===================== DOTATION =====================
        source_numbers = set(dotation_numbers)

        if exclusion_df is not None and 'NUM' in exclusion_df.columns:
            source_numbers.update(exclusion_df['NUM'].apply(clean_phone).astype(str))

        dotation_trans = df[
            (df['Type'] == "Transfer") &
            (df['From_clean'].isin(source_numbers))
        ]

        if not dotation_trans.empty:
            dotation_group = dotation_trans.groupby('Nom_Ccial').agg(
                Heure_Dotation=('Date', lambda x: x.min().strftime('%H:%M') if not x.empty else 'N/A')
            ).reset_index()

            first_tx = dotation_trans.sort_values('Date').drop_duplicates(
                ['From_clean', 'Nom_Ccial']
            )
            montant_group = first_tx.groupby('Nom_Ccial')['Amount'].sum().reset_index(name='Montant_Dotation')

            dotation_group = dotation_group.merge(montant_group, on='Nom_Ccial', how='left')
        else:
            dotation_group = pd.DataFrame(columns=['Nom_Ccial', 'Heure_Dotation', 'Montant_Dotation'])

        # ===================== CALCULS PRINCIPAUX =====================
        trans_df = df[
            (df['Type'] == "Transfer") & 
            (df['From_clean'].isin(comm_config['Ccial_MSISDN'].astype(str)))
        ].copy()

        def compute_perf(group):
            filtered = group[group['Amount'] > 10000]

            premiere = (
                filtered['Date'].min().strftime('%H:%M')
                if not filtered.empty else 'N/A'
            )

            derniere = (
                group['Date'].max().strftime('%H:%M')
                if not group.empty else 'N/A'
            )

            return pd.Series({
                'Zone_Territoire': group['Zone_Territoire'].iloc[0] if 'Zone_Territoire' in group else None,
                'Zone_SA': group['Zone_SA'].iloc[0] if 'Zone_SA' in group else None,
                'Premiere_Trans': premiere,
                'Derniere_Trans': derniere,
                'Nb_Transactions': group['Amount'].count()
            })

        perf = trans_df.groupby('Nom_Ccial').apply(compute_perf).reset_index()

        perf = perf.merge(dotation_group, on='Nom_Ccial', how='left')
        perf['Montant_Dotation'] = perf['Montant_Dotation'].fillna(0)
        perf['Heure_Dotation'] = perf['Heure_Dotation'].fillna('N/A')

        # ===================== HVC =====================
        hvc_trans = df[
            (df['Type'] == "Transfer") & 
            (df['From_clean'].isin(comm_config['Ccial_MSISDN'].astype(str)))
        ].copy()
        if master_df is not None and 'Segment Group' in master_df.columns:
            hvc_msidsn = master_df[master_df['Segment Group'].astype(str).str.strip() == "1-HVC"]['MSISDN'].astype(str).str.strip().tolist()
            hvc_trans = hvc_trans[hvc_trans['To_clean'].isin(hvc_msidsn)]

        hvc_group = hvc_trans.groupby('Nom_Ccial').agg(
            FD_HVC=('Amount', 'sum'),
            HVC_Serve=('To_clean', 'nunique'),
            Nb_Trans_HVC=('Amount', 'count')
        ).reset_index()

        perf = perf.merge(hvc_group, on='Nom_Ccial', how='left').fillna(0)

        # Tour de rotation HVC
        perf['TR_HVC'] = ((perf['Nb_Trans_HVC'] / (perf['HVC_Serve'] * required_tours)) * 100).round(1)
        perf['TR_HVC'] = perf['TR_HVC'].fillna(0)

        # ===================== OTHERS (MVC + LVC + Inconnus) =====================
        # Clients inconnus (non dans Master et non dans exclusion) sont considérés comme LVC
        other_trans = df[
            (df['Type'] == "Transfer") & 
            (df['From_clean'].isin(comm_config['Ccial_MSISDN'].astype(str)))
        ].copy()

        if master_df is not None and 'Segment Group' in master_df.columns:
            known_hvc = master_df[master_df['Segment Group'].astype(str).str.strip() != "1-HVC"]['MSISDN'].astype(str).str.strip().tolist()
            other_trans = other_trans[other_trans['To_clean'].isin(known_hvc)]

        # Clients inconnus (non dans exclusion et non dans master) → inclus dans Others comme LVC
        if exclusion_df is not None:
            known_caisses = exclusion_df['NUM'].apply(clean_phone).astype(str).tolist()
            unknown_clients = other_trans[~other_trans['To_clean'].isin(known_caisses)]
            other_trans = pd.concat([other_trans, unknown_clients]).drop_duplicates()

        other_group = other_trans.groupby('Nom_Ccial').agg(
            FD_Others=('Amount', 'sum'),
            Other_Serve=('To_clean', 'nunique'),
            Nb_Trans_Other=('Amount', 'count')
        ).reset_index()

        perf = perf.merge(other_group, on='Nom_Ccial', how='left').fillna(0)

        perf['TR_Other'] = ((perf['Nb_Trans_Other'] / (perf['Other_Serve'] * required_tours)) * 100).round(1)
        perf['TR_Other'] = perf['TR_Other'].fillna(0)

        # ===================== ALL SEGMENT =====================
        perf['Σ_FD'] = perf['FD_HVC'] + perf['FD_Others']
        perf['Σ_POS_Serve'] = perf['HVC_Serve'] + perf['Other_Serve']
        perf['TR_General'] = ((perf['Nb_Transactions'] / ((perf['HVC_Serve'] + perf['Other_Serve'])* required_tours)) * 100).round(1)

        # On supprime les colonnes de calcul intermédiaire pour ne pas les afficher
        columns_to_drop = ['Nb_Trans_HVC', 'Nb_Trans_Other']
        perf = perf.drop(columns=[col for col in columns_to_drop if col in perf.columns], errors='ignore')

        # ===================== FORMATAGE =====================
        for col in ['Montant_Dotation', 'FD_HVC', 'FD_Others', 'Σ_FD']:
            perf[col] = perf[col].apply(format_amount)

        perf['TR_HVC'] = perf['TR_HVC'].apply(lambda x: f"{x:.1f}%")
        perf['TR_Other'] = perf['TR_Other'].apply(lambda x: f"{x:.1f}%")
        perf['TR_General'] = perf['TR_General'].apply(lambda x: f"{x:.1f}%")

        # ===================== TREND [14h-17h] =====================
        df['Hour'] = df['Date'].dt.hour
        trend_14_17 = df[(df['Hour'] >= 14) & (df['Hour'] <= 17)]
        trend_6_14 = df[(df['Hour'] >= 6) & (df['Hour'] <= 13)]

        pos_serve_14_17 = trend_14_17.groupby('Nom_Ccial')['To_clean'].nunique().reset_index(name='POS_serve')
        new_clients = trend_14_17[~trend_14_17['To_clean'].isin(trend_6_14['To_clean'])]
        new_14_17 = new_clients.groupby('Nom_Ccial')['To_clean'].nunique().reset_index(name='New')

        trend = pos_serve_14_17.merge(new_14_17, on='Nom_Ccial', how='left').fillna(0)
        perf = perf.merge(trend, on='Nom_Ccial', how='left').fillna(0)
        
        
        # ===================== AFFICHAGE =====================
        # ===================== MULTI-INDEX HEADER =====================
        columns = pd.MultiIndex.from_tuples([
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

        perf_display = perf.copy()

        perf_display = perf_display[[
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

        st.dataframe(styled_df, use_container_width=True, height=650)

        # Export
        if st.button("📸 Capturer tableau en image"):
            dfi.export(styled_df, "performance.png", table_conversion="chrome")

            with open("performance.png", "rb") as f:
                st.download_button(
                    "Télécharger image",
                    f,
                    "Performance_Commerciaux.png",
                    "image/png"
                )
        col1, col2 = st.columns(2)
        excel_data = to_excel(perf)
        col1.download_button("Télécharger Excel", excel_data, "Performance_Commerciaux.xlsx")
        col2.download_button("Télécharger CSV", perf.to_csv(index=False).encode('utf-8'), "Performance_Commerciaux.csv")