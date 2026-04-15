# pages/dashboard.py
import streamlit as st
import plotly.express as px
import pandas as pd

def show_dashboard():
    st.title("📊 Dashboard Général")
    st.markdown("### Vue d'ensemble des activités")

    # KPIs principaux
    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric(
            label="Montant Distribué",
            value="1 245 678 000 FCFA",
            delta="↑ 12.4%"
        )

    with col2:
        st.metric(
            label="Montant Retourné",
            value="892 450 000 FCFA",
            delta="↓ 8.2%"
        )

    with col3:
        st.metric(
            label="Rotation Moyenne",
            value="1.39",
            delta="↑ 0.15"
        )

    st.divider()

    # Top 5 Commerciaux
    st.subheader("🏆 Top 5 Commerciaux")
    top5_data = {
        "Commercial": ["Jean Dupont", "Marie Kamga", "Paul Biya", "Sophie Nji", "Alain Talla"],
        "Montant Distribué (M FCFA)": [245, 198, 167, 142, 131],
        "Taux de Retour (%)": [68, 72, 65, 78, 81],
        "Performance": ["Excellent", "Très bon", "Bon", "Très bon", "Bon"]
    }
    top5_df = pd.DataFrame(top5_data)

    st.dataframe(
        top5_df.style.background_gradient(cmap="Blues").format({
            "Montant Distribué (M FCFA)": "{:,.0f}",
            "Taux de Retour (%)": "{:.1f}%"
        }),
        use_container_width=True,
        hide_index=True
    )

    st.divider()

    # Graphiques
    col_g1, col_g2 = st.columns(2)

    with col_g1:
        st.subheader("Distribué vs Retourné par Zone")
        zone_data = pd.DataFrame({
            "Zone": ["Zone Nord", "Zone Centre", "Zone Littoral", "Zone Ouest"],
            "Distribué": [450, 320, 280, 195],
            "Retourné": [310, 210, 195, 140]
        })
        fig1 = px.bar(
            zone_data,
            x="Zone",
            y=["Distribué", "Retourné"],
            title="Montant Distribué vs Retourné par Zone",
            barmode="group",
            height=450
        )
        st.plotly_chart(fig1, use_container_width=True)

    with col_g2:
        st.subheader("Haut Rupture (OOS) par Zone")
        oos_data = pd.DataFrame({
            "Zone": ["Zone Nord", "Zone Centre", "Zone Littoral", "Zone Ouest"],
            "Haut OOS (%)": [45, 28, 67, 52]
        })
        fig2 = px.bar(
            oos_data,
            x="Zone",
            y="Haut OOS (%)",
            title="Taux de Haut Rupture par Zone",
            color="Haut OOS (%)",
            height=450
        )
        st.plotly_chart(fig2, use_container_width=True)

    # Note informative
    st.info("💡 Les données affichées ici sont des exemples. Elles seront connectées aux vrais fichiers une fois les pages Performance et OOS développées.")