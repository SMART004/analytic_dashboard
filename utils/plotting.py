import plotly.express as px
import plotly.graph_objects as go

def create_distribution_chart(df):
    """Graphique Distribué vs Retourné"""
    fig = px.bar(
        df, x='Zone', y=['Distribué', 'Retourné'],
        title="Montant Distribué vs Retourné par Zone",
        barmode='group'
    )
    return fig

def create_oos_chart(df):
    """Graphique Haut OOS par zone"""
    fig = px.bar(df, x='Zone', y='Haut_OOS', title="Haut Rupture (OOS) par Zone")
    return fig

def create_gros_transferts_charts(result):
    """Crée des graphiques interprétables pour Gros Transferts"""
    if result.empty:
        return None, None

    # Graphique 1 : Répartition des montants
    fig1 = px.bar(
        result.head(10),
        x='Nom',
        y=['Transfer_reçu', 'Transfer_remis', 'Transfer envoyé au non exclu'],
        title="Top 10 - Transferts envoyés, remis et vers non exclus",
        barmode='group'
    )
    fig1.update_layout(height=500)

    # Graphique 2 : Camembert Transfer remis vs non exclu
    total_remis = result['Transfer_remis'].sum()
    total_non_exclu = result['Transfer envoyé au non exclu'].sum()

    fig2 = px.pie(
        values=[total_remis, total_non_exclu],
        names=['Transfer remis aux exclus', 'Transfer envoyé aux non exclus'],
        title="Répartition des Transferts envoyés"
    )
    fig2.update_layout(height=500)

    return fig1, fig2