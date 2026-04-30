import streamlit as st
import pandas as pd
from utils.helpers import load_file, clean_phone, to_excel
import plotly.express as px
from utils.storage import upload_file, get_all_files
from utils.supabase import supabase

BUCKET_NAME = "pos-night-result-files"

def show_pos_nuit():
    st.title("🌙 POS de Nuit")

    master_df = st.session_state.get("pos_master_df")
    comm_config = st.session_state.get("commercial_config_df")
    exclusion_df = st.session_state.get("exclusion_df")
    exclusion_master = st.session_state.get("exclusion_master")
    exclusion_cds = st.session_state.get("exclusion_cds")

    if master_df is None:
        st.error("Veuillez charger le fichier Master POS dans Settings")
        st.stop()

    trans_files = st.file_uploader(
        "Upload fichiers transactions",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pos_nuit_files"
    )

    # if not trans_files:
    #     st.info("Veuillez uploader les fichiers de transactions")
    #     return

    if trans_files:
        for file in trans_files:
            upload_file(
                supabase=supabase,
                bucket=BUCKET_NAME,
                uploaded_file=file
            )
        get_all_files.clear()
        st.success("Fichiers uploadés avec succès")

        # refresh page
        st.rerun()

    with st.spinner("Analyse POS de nuit en cours..."):
        df = get_all_files(
            bucket=BUCKET_NAME
        )

        if df is None or df.empty:
            st.warning("Aucun fichier de transactions trouvé")
            st.stop()

        st.success(f"{len(df)} lignes chargées")

        df["Date"] = pd.to_datetime(df.get("Date"), errors="coerce")
        df["Amount"] = pd.to_numeric(df.get("Amount"), errors="coerce").fillna(0).abs()

        if "Balance" in df.columns:
            df["Balance"] = pd.to_numeric(df.get("Balance"), errors="coerce").fillna(0)
        else:
            df["Balance"] = 0

        if "From" in df.columns:
            df["From_clean"] = df["From"].apply(clean_phone)
        if "To" in df.columns:
            df["To_clean"] = df["To"].apply(clean_phone)

        df["Date_only"] = df["Date"].dt.date
        df["Hour"] = df["Date"].dt.hour

        min_date = df["Date_only"].min()
        max_date = df["Date_only"].max()

        date_range = st.sidebar.date_input(
            "Période",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df["Date_only"] >= start_date) & (df["Date_only"] <= end_date)]

        # master POS
        master_df = master_df.copy()
        master_df["MSISDN"] = master_df["MSISDN"].astype(str).apply(clean_phone)

        # exclusions globales
        excluded_numbers = set()

        if comm_config is not None and "Ccial_MSISDN" in comm_config.columns:
            excluded_numbers.update(
                comm_config["Ccial_MSISDN"].astype(str).apply(clean_phone).tolist()
            )

        for ex in [exclusion_df, exclusion_master, exclusion_cds]:
            if ex is not None and "NUM" in ex.columns:
                excluded_numbers.update(
                    ex["NUM"].astype(str).apply(clean_phone).tolist()
                )

        # Identification POS via master (sur From et To)
        df_from = df.merge(
            master_df[[
                "MSISDN",
                "Territory",
                "Zone",
                "Locality",
                "Segment Group"
            ]],
            left_on="From_clean",
            right_on="MSISDN",
            how="left"
        )

        df_to = df.merge(
            master_df[[
                "MSISDN",
                "Territory",
                "Zone",
                "Locality",
                "Segment Group"
            ]],
            left_on="To_clean",
            right_on="MSISDN",
            how="left",
            suffixes=("", "_to")
        )

        # POS identifié si From OU To appartient au master
        df["POS_MSISDN"] = df_from["MSISDN"].fillna(df_to["MSISDN"])
        df["Territory"] = df_from["Territory"].fillna(df_to["Territory"])
        df["Zone"] = df_from["Zone"].fillna(df_to["Zone"])
        df["Locality"] = df_from["Locality"].fillna(df_to["Locality"])
        df["Segment"] = df_from["Segment Group"].fillna(df_to["Segment Group"])

        # Le nom du POS vient uniquement du fichier transaction
        # on privilégie le nom lié à From sinon celui de To
        if "From Name" in df.columns and "To Name" in df.columns:
            df["POS NAME"] = df["From Name"].fillna(df["To Name"])
        elif "Name" in df.columns:
            df["POS NAME"] = df["Name"]
        else:
            df["POS NAME"] = df["POS_MSISDN"]

        df = df[df["POS_MSISDN"].notna()].copy()

        # exclusion des transactions vers/depuis commerciaux / masters / caisses / cds
        df = df[
            (~df["To_clean"].isin(excluded_numbers)) &
            (~df["From_clean"].isin(excluded_numbers)) &
            (df["To_clean"].notna()) &
            (df["From_clean"].notna())
        ].copy()

        # filtres horaires
        night_df = df[
            (df["Hour"] >= 19) | (df["Hour"] <= 5)
        ].copy()

        day_df = df[
            (df["Hour"] > 5) & (df["Hour"] < 19)
        ].copy()
        
        #Filtre Segment
        segment_list = ["Tous"] + sorted(
            night_df["Segment"].dropna().astype(str).unique().tolist()
        )

        selected_segment = st.sidebar.selectbox(
            "Filtre Segment",
            segment_list
        )

        if selected_segment != "Tous":
            night_df = night_df[
                night_df["Segment"].astype(str) == selected_segment
            ]

            day_df = day_df[
                day_df["Segment"].astype(str) == selected_segment
            ]

        # filtres zone
        zone_list = ["Toutes"] + sorted(
            night_df["Zone"].dropna().astype(str).unique().tolist()
        )
        selected_zone = st.sidebar.selectbox("Filtre Zone", zone_list)

        if selected_zone != "Toutes":
            night_df = night_df[night_df["Zone"] == selected_zone]
            day_df = day_df[day_df["Zone"] == selected_zone]

        # filtres territoire
        terr_list = ["Toutes"] + sorted(
            night_df["Territory"].dropna().astype(str).unique().tolist()
        )
        selected_terr = st.sidebar.selectbox("Filtre Territory", terr_list)

        if selected_terr != "Toutes":
            night_df = night_df[night_df["Territory"] == selected_terr]
            day_df = day_df[day_df["Territory"] == selected_terr]

        results = []

        for msisdn, group in night_df.groupby("POS_MSISDN"):
            g = group.sort_values("Date")
            last_tx = g.iloc[-1]

            has_day_activity = msisdn in day_df["POS_MSISDN"].unique()
            status = "⚠️ Nuit + Jour" if has_day_activity else "✅ Nuit uniquement"

            results.append({
                "Date": g["Date_only"].max(),
                "Téléphone": msisdn,
                "Nom POS": g["POS NAME"].iloc[0],
                "Territory": g["Territory"].iloc[0],
                "Zone": g["Zone"].iloc[0],
                "Locality": g["Locality"].iloc[0],
                "Segment": g["Segment"].iloc[0],
                "Première transaction": g["Date"].min().strftime("%H:%M"),
                "Dernière transaction": g["Date"].max().strftime("%H:%M"),
                "Cash In": g.loc[g["Type"] == "Cash in", "Amount"].sum(),
                "Transfer": g.loc[g["Type"] == "Transfer", "Amount"].sum(),
                "Cash Out": g.loc[g["Type"] == "Cash out", "Amount"].sum(),
                "External Payment": g.loc[g["Type"] == "External payment", "Amount"].sum(),
                "Montant Total": g["Amount"].sum(),
                "Dernière Balance": last_tx.get("Balance", 0),
                "Type dernière transaction": last_tx.get("Type", "N/A"),
                "Statut": status
            })

        result_df = pd.DataFrame(results)

        if result_df.empty:
            st.warning("Aucun POS de nuit trouvé")
            return

        st.subheader("📊 Liste complète POS de Nuit")
        st.dataframe(result_df, use_container_width=True, height=650)

        # ===================== LISTE UNIQUE DES POS DE NUIT =====================
        st.subheader("🌙 Liste Unique des POS de Nuit")

        # uniquement les transactions de nuit
        unique_night_pos = night_df.groupby("POS_MSISDN").agg({
            "POS NAME": "first",
            "Territory": "first",
            "Zone": "first",
            "Locality": "first",
            "Segment": "first",
            "Date_only": "nunique",     # nombre de nuits distinctes
            "source_file": "nunique",   # nombre de fichiers
            "Amount": ["count", "sum"]  # nb transactions + montant total
        }).reset_index()

        # nettoyage colonnes multi-index
        unique_night_pos.columns = [
            "Téléphone",
            "Nom POS",
            "Territory",
            "Zone",
            "Locality",
            "Segment",
            "Nb nuits actives",
            "Nb fichiers",
            "Nb transactions nuit",
            "Montant total nuit"
        ]

        # vérifier si le POS travaille aussi en journée
        day_pos_list = set(day_df["POS_MSISDN"].unique())

        unique_night_pos["Statut"] = unique_night_pos["Téléphone"].apply(
            lambda x: "⚠️ Nuit + Jour" if x in day_pos_list else "✅ Nuit uniquement"
        )

        # indicateur de régularité nocturne
        def regularite_nuit(row):
            if row["Nb nuits actives"] >= 5:
                return "🟢 Très régulier la nuit"
            elif row["Nb nuits actives"] >= 3:
                return "🟡 Régulier la nuit"
            else:
                return "⚪ Occasionnel la nuit"

        unique_night_pos["Régularité Nuit"] = unique_night_pos.apply(
            regularite_nuit,
            axis=1
        )

        # tri par importance
        unique_night_pos = unique_night_pos.sort_values(
            ["Nb nuits actives", "Nb transactions nuit", "Montant total nuit"],
            ascending=False
        )

        st.write(
            f"### Total POS de nuit uniques : {unique_night_pos['Téléphone'].nunique()}"
        )

        st.dataframe(
            unique_night_pos,
            use_container_width=True,
            height=650
        )

        # export complet
        st.download_button(
            "📥 Télécharger Liste POS de Nuit",
            unique_night_pos.to_csv(index=False).encode("utf-8"),
            "Liste_POS_de_Nuit.csv",
            "text/csv"
        )

        # ===================== LISTE UNIQUE DES POS =====================
        st.subheader("📌 Liste Unique des POS identifiés")

        # fréquence d’apparition du POS dans les fichiers
        unique_pos = df.groupby("POS_MSISDN").agg({
            "POS NAME": "first",
            "Territory": "first",
            "Zone": "first",
            "Segment":"first",
            "Locality": "first",
            "Date_only": "nunique",   # nombre de jours distincts
            "source_file": "nunique", # nombre de fichiers distincts
            "Amount": "count"         # nombre total de transactions
        }).reset_index()

        unique_pos = unique_pos.rename(columns={
            "POS_MSISDN": "Téléphone",
            "POS NAME": "Nom POS",
            "Segment":"Segment",
            "Date_only": "Nb jours actifs",
            "source_file": "Nb fichiers",
            "Amount": "Nb transactions"
        })

        # signalement POS réguliers
        def pos_regularite(row):
            if row["Nb jours actifs"] >= 5:
                return "🟢 Très régulier"
            elif row["Nb jours actifs"] >= 3:
                return "🟡 Régulier"
            else:
                return "⚪ Occasionnel"

        unique_pos["Régularité"] = unique_pos.apply(pos_regularite, axis=1)

        # tri : les plus réguliers en haut
        unique_pos = unique_pos.sort_values(
            ["Nb jours actifs", "Nb transactions"],
            ascending=False
        )

        st.write(f"### Total POS uniques : {unique_pos['Téléphone'].nunique()}")

        st.dataframe(
            unique_pos,
            use_container_width=True,
            height=600
        )

        # ===================== TOP POS LES PLUS RÉGULIERS =====================
        st.subheader("🏆 TOP POS les plus réguliers")

        top_regular = unique_pos.head(20)

        st.dataframe(
            top_regular,
            use_container_width=True
        )

        # ===================== GRAPHE ACTIVITÉ DE NUIT PAR TERRITORY =====================
        st.subheader("📊 Activité de Nuit par Territory")

        # compter le nombre d’activités nocturnes par territoire
        territory_night = night_df.groupby("Territory").agg({
            "Amount": "count"
        }).reset_index()

        territory_night = territory_night.rename(columns={
            "Amount": "Nb_Activites_Nuit"
        })

        # tri décroissant
        territory_night = territory_night.sort_values(
            "Nb_Activites_Nuit",
            ascending=False
        )

        # affichage tableau résumé
        st.dataframe(
            territory_night,
            use_container_width=True,
            height=400
        )

        # graphique bar chart
        fig = px.bar(
            territory_night,
            x="Territory",
            y="Nb_Activites_Nuit",
            text="Nb_Activites_Nuit",
            title="Nombre d'activités de nuit par Territory"
        )

        fig.update_layout(
            xaxis_title="Territory",
            yaxis_title="Nombre d'activités de nuit",
            xaxis_tickangle=-45,
            height=550
        )

        st.plotly_chart(fig, use_container_width=True)

        # export CSV
        st.download_button(
            "📥 Télécharger Liste POS uniques",
            unique_pos.to_csv(index=False).encode("utf-8"),
            "Liste_POS_uniques.csv",
            "text/csv"
        )

        st.download_button(
            "📥 Télécharger Excel",
            result_df.to_csv(index=False).encode("utf-8"),
            "POS_de_Nuit.csv",
            "text/csv"
        )
