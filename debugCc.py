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