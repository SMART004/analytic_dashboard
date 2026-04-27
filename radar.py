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