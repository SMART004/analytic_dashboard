import streamlit as st
import pandas as pd
import numpy as np
from utils.helpers import clean_phone, to_excel
from utils.supabase import load_setting

BUCKET_NAME = "pos-night-result-files"
BUCKET_NAME_COMMERCIAL_COVERAGE = "pos-commercial-coverage-files"


def normalize_numeric_str(x):
    """Convertit un nombre potentiellement flottant (ex: 237653364482.0) en chaîne entière propre."""
    try:
        return str(int(float(x)))
    except (ValueError, TypeError):
        return str(x).strip()


# =====================================================================
# HELPER PARTAGÉ (utilisé par les deux onglets)
# =====================================================================
def read_uploaded_files(files):
    all_df = []

    for file in files:
        try:
            if file.name.endswith(".csv"):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)

            df["source_file"] = file.name
            all_df.append(df)

        except Exception as e:
            st.error(f"Erreur lecture {file.name}: {e}")

    if not all_df:
        return None

    final_df = pd.concat(all_df, ignore_index=True)
    final_df.columns = [str(c).strip() for c in final_df.columns]
    return final_df


def show_pos_nuit():
    st.title("🌙 POS de Nuit & Couverture")

    master_df = load_setting("maitre_pos")
    comm_config = load_setting("commerciaux")
    exclusion_df = load_setting("caisses")
    exclusion_master = load_setting("masters")
    exclusion_cds = load_setting("cds")

    tab_nuit, tab_couverture = st.tabs(
        ["🌙 POS de Nuit", "🧑‍💼 POS Non Touchés & Classement"]
    )

    with tab_nuit:
        _show_pos_nuit_tab(
            master_df, comm_config, exclusion_df, exclusion_master, exclusion_cds
        )

    with tab_couverture:
        _show_couverture_commerciale_tab(comm_config)


# =====================================================================
# ONGLET 1 : POS DE NUIT
# =====================================================================
def _show_pos_nuit_tab(master_df, comm_config, exclusion_df, exclusion_master, exclusion_cds):

    if master_df is None:
        st.warning("⚠️ Veuillez charger le fichier Master POS dans Settings pour utiliser l'onglet POS de Nuit.")
        return

    trans_files = st.file_uploader(
        "Upload fichiers transactions",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pos_nuit_files"
    )

    if not trans_files:
        st.info("Veuillez uploader les fichiers de transactions")
        return

    with st.spinner("Analyse POS de nuit en cours..."):

        df = read_uploaded_files(trans_files)

        if df is None or df.empty:
            st.warning("Aucun fichier valide")
            return

        st.success(f"{len(df)} lignes chargées")

        df["Date"] = pd.to_datetime(df.get("Date"), errors="coerce")
        df["Amount"] = pd.to_numeric(df.get("Amount"), errors="coerce").fillna(0).abs()

        if "Balance" in df.columns:
            df["Balance"] = pd.to_numeric(df.get("Balance"), errors="coerce").fillna(0)
        else:
            df["Balance"] = 0

        if "From" in df.columns:
            df["From_clean"] = df["From"].apply(normalize_numeric_str).apply(clean_phone)
        if "To" in df.columns:
            df["To_clean"] = df["To"].apply(normalize_numeric_str).apply(clean_phone)

        # 🧹 SUPPRESSION DES DOUBLONS
        df = df.drop_duplicates(
            subset=['Date', 'From_clean', 'To_clean', 'Amount', 'Type'],
            keep='last'
        )

        st.info(f"🧹 Après déduplication : {len(df)} lignes")

        df["Date_only"] = df["Date"].dt.date
        df["Hour"] = df["Date"].dt.hour

        min_date = df["Date_only"].min()
        max_date = df["Date_only"].max()

        date_range = st.sidebar.date_input(
            "Période (POS de Nuit)",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
            key="date_range_nuit"
        )

        if len(date_range) == 2:
            start_date, end_date = date_range
            df = df[(df["Date_only"] >= start_date) & (df["Date_only"] <= end_date)]

        # Master POS
        master_df = master_df.copy()
        master_df["MSISDN"] = master_df["MSISDN"].astype(str).apply(normalize_numeric_str).apply(clean_phone)

        # Exclusions globales
        excluded_numbers = set()

        if comm_config is not None and "Ccial_MSISDN" in comm_config.columns:
            excluded_numbers.update(
                comm_config["Ccial_MSISDN"].astype(str).apply(normalize_numeric_str).apply(clean_phone).tolist()
            )

        for ex in [exclusion_df, exclusion_master, exclusion_cds]:
            if ex is not None and "NUM" in ex.columns:
                excluded_numbers.update(
                    ex["NUM"].astype(str).apply(normalize_numeric_str).apply(clean_phone).tolist()
                )

        # Identification POS via master
        df_from = df.merge(
            master_df[["MSISDN", "Territory", "Zone", "Locality", "Segment Group"]],
            left_on="From_clean",
            right_on="MSISDN",
            how="left"
        )

        df_to = df.merge(
            master_df[["MSISDN", "Territory", "Zone", "Locality", "Segment Group"]],
            left_on="To_clean",
            right_on="MSISDN",
            how="left",
            suffixes=("", "_to")
        )

        df["POS_MSISDN"] = df_from["MSISDN"].fillna(df_to["MSISDN"])
        df["Territory"] = df_from["Territory"].fillna(df_to["Territory"])
        df["Zone"] = df_from["Zone"].fillna(df_to["Zone"])
        df["Locality"] = df_from["Locality"].fillna(df_to["Locality"])
        df["Segment"] = df_from["Segment Group"].fillna(df_to["Segment Group"])

        if "From Name" in df.columns and "To Name" in df.columns:
            df["POS NAME"] = df["From Name"].fillna(df["To Name"])
        elif "Name" in df.columns:
            df["POS NAME"] = df["Name"]
        else:
            df["POS NAME"] = df["POS_MSISDN"]

        df = df[df["POS_MSISDN"].notna()].copy()

        # Exclusion des comptes administratifs
        df = df[
            (~df["To_clean"].isin(excluded_numbers)) &
            (~df["From_clean"].isin(excluded_numbers)) &
            (df["To_clean"].notna()) &
            (df["From_clean"].notna())
        ].copy()

        # Filtres horaires
        night_df = df[(df["Hour"] >= 19) | (df["Hour"] <= 5)].copy()
        day_df = df[(df["Hour"] > 5) & (df["Hour"] < 19)].copy()

        # Filtre Segment
        segment_list = ["Tous"] + sorted(
            night_df["Segment"].dropna().astype(str).unique().tolist()
        )
        selected_segment = st.sidebar.selectbox(
            "Filtre Segment (POS de Nuit)", segment_list, key="segment_nuit"
        )
        if selected_segment != "Tous":
            night_df = night_df[night_df["Segment"].astype(str) == selected_segment]
            day_df = day_df[day_df["Segment"].astype(str) == selected_segment]

        # Filtres Zone
        zone_list = ["Toutes"] + sorted(
            night_df["Zone"].dropna().astype(str).unique().tolist()
        )
        selected_zone = st.sidebar.selectbox(
            "Filtre Zone (POS de Nuit)", zone_list, key="zone_nuit"
        )
        if selected_zone != "Toutes":
            night_df = night_df[night_df["Zone"] == selected_zone]
            day_df = day_df[day_df["Zone"] == selected_zone]

        # Filtres Territoire
        terr_list = ["Toutes"] + sorted(
            night_df["Territory"].dropna().astype(str).unique().tolist()
        )
        selected_terr = st.sidebar.selectbox(
            "Filtre Territory (POS de Nuit)", terr_list, key="terr_nuit"
        )
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
        st.dataframe(result_df, use_container_width=True, height=500)

        # Unique POS Nuit
        unique_night_pos = night_df.groupby("POS_MSISDN").agg({
            "POS NAME": "first",
            "Territory": "first",
            "Zone": "first",
            "Locality": "first",
            "Segment": "first",
            "Date_only": "nunique",
            "source_file": "nunique",
            "Amount": ["count", "sum"]
        }).reset_index()

        unique_night_pos.columns = [
            "Téléphone", "Nom POS", "Territory", "Zone", "Locality", "Segment",
            "Nb nuits actives", "Nb fichiers", "Nb transactions nuit", "Montant total nuit"
        ]

        day_pos_list = set(day_df["POS_MSISDN"].unique())
        unique_night_pos["Statut"] = unique_night_pos["Téléphone"].apply(
            lambda x: "⚠️ Nuit + Jour" if x in day_pos_list else "✅ Nuit uniquement"
        )

        st.write(f"### Total POS de nuit uniques : {unique_night_pos['Téléphone'].nunique()}")
        st.dataframe(unique_night_pos, use_container_width=True, height=500)

        # Exports
        col_ex1, col_ex2 = st.columns(2)
        with col_ex1:
            st.download_button(
                "📥 Excel Liste POS de Nuit",
                to_excel(unique_night_pos),
                "Liste_POS_de_Nuit.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_night_unique_excel"
            )
        with col_ex2:
            st.download_button(
                "📥 CSV Liste POS de Nuit",
                unique_night_pos.to_csv(index=False).encode("utf-8"),
                "Liste_POS_de_Nuit.csv",
                "text/csv",
                key="dl_night_unique_csv"
            )


# =====================================================================
# ONGLET 2 : POS NON TOUCHÉS PAR UN COMMERCIAL & CLASSEMENT
# =====================================================================
def _show_couverture_commerciale_tab(comm_config):
    st.header("🧑‍💼 POS Non Touchés par les Commerciaux")
    st.caption("Importez d'un côté les transactions commerciales et de l'autre le fichier contenant les POS non servis.")

    col_up1, col_up2 = st.columns(2)

    with col_up1:
        comm_files = st.file_uploader(
            "1️⃣ Transactions Commerciales",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
            key="commercial_trans_files"
        )

    with col_up2:
        untouched_files = st.file_uploader(
            "2️⃣ Fichier POS Non Servis (colonne 'pos non servis')",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
            key="untouched_pos_files"
        )

    if not comm_files or not untouched_files:
        st.info("Veuillez charger les deux fichiers ci-dessus pour afficher l'analyse.")
        return

    with st.spinner("Analyse des POS non touchés en cours..."):

        df_comm = read_uploaded_files(comm_files)
        df_untouched = read_uploaded_files(untouched_files)

        if df_comm is None or df_comm.empty or df_untouched is None or df_untouched.empty:
            st.warning("Un ou plusieurs fichiers importés sont valides mais vides.")
            return

        # =====================================================================
        # 1. TRAITEMENT FICHIER POS NON SERVIS
        # =====================================================================
        cols_untouched_lower = {str(c).strip().lower(): c for c in df_untouched.columns}
        pos_non_servi_col = cols_untouched_lower.get("pos non servis") or cols_untouched_lower.get("pos non servi")

        if not pos_non_servi_col:
            st.error("❌ La colonne **'pos non servis'** est introuvable dans le fichier chargé en zone 2.")
            return

        df_untouched["POS_NON_TOUCH_MSISDN"] = df_untouched[pos_non_servi_col].apply(normalize_numeric_str).apply(clean_phone)
        untouched_df = df_untouched[
            df_untouched["POS_NON_TOUCH_MSISDN"].notna() &
            (df_untouched["POS_NON_TOUCH_MSISDN"] != "") &
            (df_untouched["POS_NON_TOUCH_MSISDN"] != "none")
        ].copy()

        if untouched_df.empty:
            st.warning("Aucun POS non servi valide trouvé.")
            return

        # =====================================================================
        # 2. TRAITEMENT TRANSACTIONS COMMERCIALES
        # From = commercial, To = POS visité
        # Filtre : Type contient "transfer", Amount >= 10000
        # =====================================================================
        amount_col = next((c for c in df_comm.columns if c.lower() in ["amount", "montant"]), "Amount")
        df_comm["Amount"] = pd.to_numeric(df_comm.get(amount_col, 0), errors="coerce").fillna(0).abs()
        df_comm["Date"] = pd.to_datetime(df_comm.get("Date"), errors="coerce")

        df_comm["From_clean"] = df_comm["From"].apply(normalize_numeric_str).apply(clean_phone) if "From" in df_comm.columns else ""
        df_comm["To_clean"] = df_comm["To"].apply(normalize_numeric_str).apply(clean_phone) if "To" in df_comm.columns else ""

        # Récupération de la liste des commerciaux
        comm_tmp = comm_config.copy() if comm_config is not None and "Ccial_MSISDN" in comm_config.columns else pd.DataFrame()
        comm_name_map = {}
        comm_numbers = set()

        if not comm_tmp.empty:
            comm_tmp["Ccial_MSISDN_clean"] = comm_tmp["Ccial_MSISDN"].apply(normalize_numeric_str).apply(clean_phone)
            comm_numbers = set(comm_tmp["Ccial_MSISDN_clean"].dropna().tolist())

            name_c = next((c for c in ["Ccial_Nom", "Nom_Ccial", "Ccial_Name", "Nom", "Commercial"] if c in comm_tmp.columns), None)
            if name_c:
                comm_name_map = dict(zip(comm_tmp["Ccial_MSISDN_clean"], comm_tmp[name_c]))

        # Filtrage des transactions valides
        if "Type" in df_comm.columns:
            type_mask = df_comm["Type"].astype(str).str.strip().str.lower().str.contains("transfer|trf|trans", regex=True)
        else:
            type_mask = True

        if comm_numbers:
            from_mask = df_comm["From_clean"].isin(comm_numbers)
        else:
            from_mask = True

        comm_valid = df_comm[
            from_mask &
            type_mask &
            (df_comm["Amount"] >= 10000) &
            (df_comm["To_clean"] != "")
        ].copy()

        # Cartographie POS -> Commercial & Montant (on garde le commercial qui a le plus transféré vers ce POS)
        if not comm_valid.empty:
            comm_agg = comm_valid.groupby(["To_clean", "From_clean"])["Amount"].sum().reset_index()
            comm_agg = comm_agg.sort_values("Amount", ascending=False).drop_duplicates(subset="To_clean", keep="first")
            pos_to_comm_map = comm_agg.set_index("To_clean")["From_clean"].to_dict()
            pos_to_amount_map = comm_agg.set_index("To_clean")["Amount"].to_dict()
        else:
            pos_to_comm_map = {}
            pos_to_amount_map = {}

        untouched_df["Commercial_MSISDN"] = untouched_df["POS_NON_TOUCH_MSISDN"].map(pos_to_comm_map).fillna("Inconnu")
        untouched_df["Montant"] = untouched_df["POS_NON_TOUCH_MSISDN"].map(pos_to_amount_map).fillna(0)

        # =====================================================================
        # 2bis. Détection colonne Site Name (variantes possibles)
        # =====================================================================
        site_name_aliases = ["site name", "sitename", "nom pos", "pos name", "nom du pos"]
        site_col_found = next((cols_untouched_lower[a] for a in site_name_aliases if a in cols_untouched_lower), None)
        if site_col_found:
            cols_untouched_lower["site name"] = site_col_found

        # Normalisation des colonnes géographiques + site name
        for col_name in ["Zone", "territoire", "Locality", "segment_group", "sitename"]:
            matched_col = cols_untouched_lower.get(col_name.lower())
            if matched_col and matched_col in untouched_df.columns:
                untouched_df[col_name] = untouched_df[matched_col]
            elif col_name not in untouched_df.columns:
                untouched_df[col_name] = "N/A"

        # Cartographie Téléphone POS -> Nom du site (issue du fichier POS non servis)
        site_name_map = {}
        if "sitename" in untouched_df.columns:
            site_name_map = dict(
                zip(untouched_df["POS_NON_TOUCH_MSISDN"], untouched_df["sitename"])
            )

        # =====================================================================
        # DEBUG : Diagnostic du Mapping POS
        # =====================================================================
        with st.expander("🔍 DEBUG : Diagnostic du Mapping POS"):
            match_count = (untouched_df["Commercial_MSISDN"] != "Inconnu").sum()
            st.write(f"**Nombre de POS non servis ayant un commercial associé :** {match_count} / {len(untouched_df)}")
            if not site_col_found:
                st.write("⚠️ Aucune colonne 'Site Name' détectée dans le fichier POS non servis.")

        # Filtres Sidebar (POS non servis)
        st.sidebar.markdown("---")
        st.sidebar.subheader("🎯 Filtres Couverture")

        if "Zone" in untouched_df.columns and untouched_df["Zone"].nunique() > 1:
            zone_list = ["Toutes"] + sorted(untouched_df["Zone"].dropna().astype(str).unique().tolist())
            selected_zone = st.sidebar.selectbox("Zone", zone_list, key="zone_couv")
            if selected_zone != "Toutes":
                untouched_df = untouched_df[untouched_df["Zone"] == selected_zone]

        if "territoire" in untouched_df.columns and untouched_df["territoire"].nunique() > 1:
            terr_list = ["Toutes"] + sorted(untouched_df["territoire"].dropna().astype(str).unique().tolist())
            selected_terr = st.sidebar.selectbox("territoire", terr_list, key="terr_couv")
            if selected_terr != "Toutes":
                untouched_df = untouched_df[untouched_df["territoire"] == selected_terr]

        # =====================================================================
        # 3. LISTE UNIQUE DES POS NON TOUCHÉS
        # =====================================================================
        st.subheader("🚫 Liste Unique des POS Non Touchés")

        unique_pos_df = untouched_df.groupby("POS_NON_TOUCH_MSISDN").agg({
            "territoire": "first",
            "Zone": "first",
            "Locality": "first",
            "segment_group": "first",
            "sitename": "first",
            "Commercial_MSISDN": "first",
            "Montant": "sum"
        }).reset_index()

        unique_pos_df["Montant"] = unique_pos_df["Montant"].round(0).astype(int)

        unique_pos_df["Nom Commercial"] = unique_pos_df["Commercial_MSISDN"].map(comm_name_map).fillna(unique_pos_df["Commercial_MSISDN"])

        unique_pos_df = unique_pos_df.rename(columns={
            "POS_NON_TOUCH_MSISDN": "Téléphone POS",
            "Commercial_MSISDN": "MSISDN Commercial",
            "Nom Commercial": "Commercial Associé",
            "Montant": "Montant Total"
        })

        st.write(f"**Total POS non touchés uniques :** `{len(unique_pos_df)}`")
        st.dataframe(unique_pos_df, use_container_width=True, height=400)

        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "📊 Exporter Liste POS Non Touchés (EXCEL)",
                to_excel(unique_pos_df),
                "POS_Non_Touches.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_untouched_excel"
            )
        with c2:
            st.download_button(
                "📄 Exporter Liste POS Non Touchés (CSV)",
                unique_pos_df.to_csv(index=False).encode("utf-8"),
                "POS_Non_Touches.csv",
                "text/csv",
                key="dl_untouched_csv"
            )

        # =====================================================================
        # 4. CLASSEMENT DES COMMERCIAUX PAR POS NON TOUCHÉS & MONTANT
        # =====================================================================
        st.markdown("---")
        st.subheader("🏆 Classement des Commerciaux (POS Non Touchés & Montants)")

        ranking = unique_pos_df.groupby("MSISDN Commercial").agg(
            Total_POS_Non_Touches=("Téléphone POS", "nunique"),
            Montant_Total=("Montant Total", "sum")
        ).reset_index().rename(columns={"MSISDN Commercial": "Commercial_MSISDN"})

        ranking["Montant_Total"] = ranking["Montant_Total"].round(0).astype(int)

        # Exclut "Inconnu"
        ranking = ranking[
            ranking["Commercial_MSISDN"].astype(str).str.strip().str.lower() != "inconnu"
        ]
        ranking = ranking[ranking["Commercial_MSISDN"].notna()]

        ranking["Nom Commercial"] = ranking["Commercial_MSISDN"].map(comm_name_map).fillna(ranking["Commercial_MSISDN"])

        # Territoire principal par commercial (le plus fréquent parmi ses POS couverts)
        comm_territory_map = (
            untouched_df[untouched_df["Commercial_MSISDN"] != "Inconnu"]
            .groupby("Commercial_MSISDN")["territoire"]
            .agg(lambda s: s.mode().iat[0] if not s.mode().empty else "N/A")
            .to_dict()
        )
        ranking["territoire"] = ranking["Commercial_MSISDN"].map(comm_territory_map).fillna("N/A")

        if "segment_group" in untouched_df.columns and untouched_df["segment_group"].nunique() > 1:
            segment_pivot = untouched_df.groupby(["Commercial_MSISDN", "segment_group"])["POS_NON_TOUCH_MSISDN"].nunique().unstack(fill_value=0)
            ranking = ranking.merge(segment_pivot, on="Commercial_MSISDN", how="left").fillna(0)

        # Filtre Territoire (Commerciaux) dans la sidebar
        st.sidebar.markdown("---")
        st.sidebar.subheader("🎯 Filtre Classement Commerciaux")
        if ranking["territoire"].nunique() > 1:
            ranking_terr_list = ["Toutes"] + sorted(ranking["territoire"].dropna().astype(str).unique().tolist())
            selected_ranking_terr = st.sidebar.selectbox("Territoire (Commerciaux)", ranking_terr_list, key="terr_ranking")
            if selected_ranking_terr != "Toutes":
                ranking = ranking[ranking["territoire"] == selected_ranking_terr]

        cols_order = ["Commercial_MSISDN", "Nom Commercial", "territoire", "Total_POS_Non_Touches", "Montant_Total"]
        other_cols = [c for c in ranking.columns if c not in cols_order]
        ranking = ranking[cols_order + other_cols]

        ranking = ranking.sort_values("Total_POS_Non_Touches", ascending=False).reset_index(drop=True)
        ranking.insert(0, "Rang", range(1, len(ranking) + 1))

        st.dataframe(
            ranking.style.format({"Montant_Total": "{:,.0f}"}),
            use_container_width=True,
            height=450
        )

        col_r1, col_r2 = st.columns(2)
        with col_r1:
            st.download_button(
                "📊 Exporter Classement Commerciaux (EXCEL)",
                to_excel(ranking),
                "Classement_Commerciaux_POS_Non_Touches.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_rank_excel"
            )
        with col_r2:
            st.download_button(
                "📄 Exporter Classement Commerciaux (CSV)",
                ranking.to_csv(index=False).encode("utf-8"),
                "Classement_Commerciaux_POS_Non_Touches.csv",
                "text/csv",
                key="dl_rank_csv"
            )

        # =====================================================================
        # 5. TOP 3 DES MEILLEURS SITES (UNIQUES)
        # Basé sur le nombre de POS non servis par site (même logique que le
        # classement des commerciaux) et sur le montant donné à ces POS non
        # servis (même source que "Montant Total" du classement).
        # =====================================================================
        st.markdown("---")
        st.subheader("🥇 Top 3 des Meilleurs Sites")

        if "sitename" in unique_pos_df.columns and unique_pos_df["sitename"].notna().any():
            # On exclut les POS non servis non rattachés à un commercial identifié
            # ("Inconnu"), exactement comme pour le classement des commerciaux.
            unique_pos_df_known = unique_pos_df[
                unique_pos_df["MSISDN Commercial"].astype(str).str.strip().str.lower() != "inconnu"
            ]

            if unique_pos_df_known.empty:
                st.info("Aucun POS non servi n'est rattaché à un commercial identifié pour établir le top des sites.")
            else:
                top_sites = unique_pos_df_known.groupby("sitename").agg(
                    Nb_POS_Non_Touches=("Téléphone POS", "nunique"),
                    Montant_Total=("Montant Total", "sum")
                ).reset_index()

                top_sites["Montant_Total"] = top_sites["Montant_Total"].round(0).astype(int)
                top_sites = top_sites.sort_values(
                    ["Nb_POS_Non_Touches", "Montant_Total"], ascending=[False, False]
                ).head(3).reset_index(drop=True)
                top_sites.insert(0, "Rang", range(1, len(top_sites) + 1))

                st.dataframe(
                    top_sites.rename(columns={
                        "sitename": "Nom du Site",
                        "Nb_POS_Non_Touches": "Nb POS Non Servis",
                        "Montant_Total": "Montant Total"
                    }).style.format({"Montant Total": "{:,.0f}"}),
                    use_container_width=True,
                    height=160
                )
        else:
            st.info("Aucune colonne 'Site Name' exploitable pour établir le top des sites.")

        # =====================================================================
        # 6. TENDANCE JOUR PAR JOUR DE POS NON SERVIS TOUCHÉS PAR COMMERCIAL
        # Période fixe de comparaison : du 15 au 25 juillet
        # On mesure si le RYTHME (nb de nouveaux POS uniques touchés par jour)
        # accélère ou ralentit sur la période, via la pente d'une régression
        # linéaire sur les nouveaux POS touchés chaque jour.
        # =====================================================================
        st.markdown("---")
        st.subheader("📈 Tendance de Couverture des POS Non Servis (15 → 25 Juillet)")

        if not comm_valid.empty and comm_valid["Date"].notna().any():
            years_in_data = comm_valid["Date"].dt.year.dropna()
            ref_year = int(years_in_data.mode().iat[0]) if not years_in_data.empty else pd.Timestamp.now().year

            period_start = pd.Timestamp(year=ref_year, month=7, day=15).date()
            period_end = pd.Timestamp(year=ref_year, month=7, day=25).date()
            nb_days = (period_end - period_start).days + 1  # 11 jours

            untouched_pos_set = set(unique_pos_df["Téléphone POS"].tolist())
            comm_valid_untouched = comm_valid[comm_valid["To_clean"].isin(untouched_pos_set)].copy()
            comm_valid_untouched["Jour"] = comm_valid_untouched["Date"].dt.date

            period_df = comm_valid_untouched[
                (comm_valid_untouched["Jour"] >= period_start) &
                (comm_valid_untouched["Jour"] <= period_end)
            ]

            if period_df.empty:
                st.info(f"Aucune transaction vers un POS non servi entre le {period_start.strftime('%d/%m/%Y')} et le {period_end.strftime('%d/%m/%Y')}.")
            else:
                # Un même POS ne doit compter qu'UNE SEULE FOIS sur toute la période :
                # on garde uniquement le 1er jour où chaque commercial a touché chaque
                # POS non servi (peu importe s'il le retouche les jours suivants).
                first_touch = (
                    period_df.sort_values("Date")
                    .drop_duplicates(subset=["From_clean", "To_clean"], keep="first")
                    [["From_clean", "To_clean", "Jour"]]
                    .rename(columns={"To_clean": "POS_MSISDN", "Jour": "Jour"})
                )

                # Grille complète : tous les commerciaux actifs x tous les jours de la
                # période (même les jours à 0 nouveau POS), pour que la régression
                # reflète correctement le rythme réel jour après jour.
                all_days = pd.date_range(period_start, period_end, freq="D").date
                commercials_actifs = first_touch["From_clean"].unique()

                grid = pd.MultiIndex.from_product(
                    [commercials_actifs, all_days], names=["From_clean", "Jour"]
                ).to_frame(index=False)

                daily_new = (
                    first_touch.groupby(["From_clean", "Jour"])
                    .size()
                    .reset_index(name="Nouveaux_POS_Jour")
                )

                daily_full = grid.merge(daily_new, on=["From_clean", "Jour"], how="left")
                daily_full["Nouveaux_POS_Jour"] = daily_full["Nouveaux_POS_Jour"].fillna(0).astype(int)
                daily_full = daily_full.sort_values(["From_clean", "Jour"])
                daily_full["Jour_Index"] = daily_full.groupby("From_clean").cumcount() + 1  # 1 -> 11
                daily_full["Cumul_POS_Uniques"] = daily_full.groupby("From_clean")["Nouveaux_POS_Jour"].cumsum()

                # Pente (régression linéaire) du rythme quotidien par commercial
                def calc_pente(group):
                    x = group["Jour_Index"].values.astype(float)
                    y = group["Nouveaux_POS_Jour"].values.astype(float)
                    if len(x) < 2 or np.all(y == y[0]):
                        return 0.0
                    slope, _ = np.polyfit(x, y, 1)
                    return round(float(slope), 3)

                pente_df = (
                    daily_full.groupby("From_clean")
                    .apply(calc_pente)
                    .reset_index(name="Pente")
                )

                def classer_tendance(p):
                    if p > 0.05:
                        return "🔼 Accélération"
                    elif p < -0.05:
                        return "🔽 Ralentissement"
                    else:
                        return "➡️ Stable"

                pente_df["Tendance"] = pente_df["Pente"].apply(classer_tendance)

                total_unique = (
                    first_touch.groupby("From_clean")["POS_MSISDN"]
                    .nunique()
                    .reset_index(name="Total_POS_Uniques_Periode")
                )

                tendance_df = pente_df.merge(total_unique, on="From_clean", how="left")
                tendance_df = tendance_df.rename(columns={"From_clean": "Commercial_MSISDN"})
                tendance_df["Nom Commercial"] = tendance_df["Commercial_MSISDN"].map(comm_name_map).fillna(tendance_df["Commercial_MSISDN"])

                # Classement : les plus fortes accélérations en premier
                tendance_df = tendance_df.sort_values(
                    ["Pente", "Total_POS_Uniques_Periode"], ascending=[False, False]
                ).reset_index(drop=True)
                tendance_df.insert(0, "Rang", range(1, len(tendance_df) + 1))

                tendance_df = tendance_df[
                    ["Rang", "Nom Commercial", "Commercial_MSISDN", "Total_POS_Uniques_Periode", "Pente", "Tendance"]
                ]

                st.caption(
                    f"Sur {nb_days} jours (du {period_start.strftime('%d/%m/%Y')} au {period_end.strftime('%d/%m/%Y')}) : "
                    "la pente mesure l'évolution du nombre de NOUVEAUX POS non servis touchés chaque jour "
                    "(chaque POS ne compte qu'une seule fois sur la période). Pente positive = le commercial "
                    "touche de plus en plus de nouveaux POS au fil des jours ; pente négative = il ralentit."
                )
                st.dataframe(tendance_df, use_container_width=True, height=400)

                # Détail jour par jour (audit / vérification de la progression)
                with st.expander("📅 Détail jour par jour (nouveaux POS uniques touchés)"):
                    daily_detail = daily_full.copy()
                    daily_detail["Nom Commercial"] = daily_detail["From_clean"].map(comm_name_map).fillna(daily_detail["From_clean"])
                    daily_detail = daily_detail[
                        ["Nom Commercial", "From_clean", "Jour", "Jour_Index", "Nouveaux_POS_Jour", "Cumul_POS_Uniques"]
                    ]
                    st.dataframe(daily_detail, use_container_width=True, height=350)

                st.download_button(
                    "📥 Exporter Tendance (EXCEL)",
                    to_excel(tendance_df),
                    "Tendance_POS_Non_Servis.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_tendance_excel"
                )
        else:
            st.info("Aucune date exploitable dans le fichier de transactions commerciales pour calculer la tendance.")