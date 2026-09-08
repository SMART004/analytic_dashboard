from __future__ import annotations

from collections import defaultdict
from datetime import date
from math import hypot, sqrt

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from models.db import get_cached_connection


NODE_COLORS = {
    "POS": "#2457d6",
    "Commercial": "#f0b429",
    "CDS": "#8b5cf6",
    "Caisse": "#ef6c57",
    "Point Relais": "#26a69a",
    "Autre": "#8b949e",
}


@st.cache_data(ttl=300, show_spinner=False)
def load_relationship_data() -> pl.DataFrame:
    """Charge les transactions et les référentiels déjà consolidés par l'ingestion."""
    conn = get_cached_connection()
    query = """
        SELECT t.date_only, t.tx_type, t.amount, t.from_msisdn, t.to_msisdn,
               t.from_name, t.to_name, t.territoire_snapshot, t.site_key_snapshot,
               p_from.full_name AS from_pos_name, p_from.zone_territoire AS from_territory,
               p_from.segment_group AS from_segment, p_to.full_name AS to_pos_name,
               p_to.zone_territoire AS to_territory, p_to.segment_group AS to_segment,
               c_from.nom_ccial AS from_commercial, c_to.nom_ccial AS to_commercial,
               pr_from.nom AS from_point_name, pr_from.type_point AS from_point_type,
               pr_to.nom AS to_point_name, pr_to.type_point AS to_point_type,
               cr_from.nom_cds AS from_cds, cr_to.nom_cds AS to_cds,
               CASE
                   WHEN pr_from.type_point IS NOT NULL THEN pr_from.type_point
                   WHEN c_from.ccial_msisdn IS NOT NULL THEN 'Commercial'
                   WHEN cr_from.cds_msisdn IS NOT NULL THEN 'CDS'
                   WHEN p_from.agent_msisdn IS NOT NULL THEN 'POS'
                   ELSE 'Autre'
               END AS from_entity_type,
               CASE
                   WHEN pr_to.type_point IS NOT NULL THEN pr_to.type_point
                   WHEN c_to.ccial_msisdn IS NOT NULL THEN 'Commercial'
                   WHEN cr_to.cds_msisdn IS NOT NULL THEN 'CDS'
                   WHEN p_to.agent_msisdn IS NOT NULL THEN 'POS'
                   ELSE 'Autre'
               END AS to_entity_type
        FROM transactions t
        LEFT JOIN referentiel_pos p_from ON p_from.agent_msisdn = t.from_msisdn
        LEFT JOIN referentiel_pos p_to ON p_to.agent_msisdn = t.to_msisdn
        LEFT JOIN referentiel_commerciaux c_from ON c_from.ccial_msisdn = t.from_msisdn
        LEFT JOIN referentiel_commerciaux c_to ON c_to.ccial_msisdn = t.to_msisdn
        LEFT JOIN point_relay_referentiel pr_from ON pr_from.msisdn_pr = t.from_msisdn
        LEFT JOIN point_relay_referentiel pr_to ON pr_to.msisdn_pr = t.to_msisdn
        LEFT JOIN cds_referentiel cr_from ON cr_from.cds_msisdn = t.from_msisdn
        LEFT JOIN cds_referentiel cr_to ON cr_to.cds_msisdn = t.to_msisdn
        WHERE t.tx_type = 'Transfer'
    """
    cursor = conn.execute(query)
    rows = cursor.fetchall()
    columns = [item[0] for item in cursor.description]
    if not rows:
        return pl.DataFrame({column: pl.Series(column, [], dtype=pl.Utf8) for column in columns})

    text_columns = [
        "date_only", "tx_type", "from_msisdn", "to_msisdn", "from_name", "to_name",
        "territoire_snapshot", "site_key_snapshot", "from_pos_name", "from_territory",
        "from_segment", "to_pos_name", "to_territory", "to_segment", "from_commercial",
        "to_commercial", "from_point_name", "from_point_type", "to_point_name",
        "to_point_type", "from_cds", "to_cds", "from_entity_type", "to_entity_type",
    ]
    values = [dict(zip(columns, row)) for row in rows]
    frame = {}
    for column in columns:
        column_values = [item.get(column) for item in values]
        if column == "amount":
            frame[column] = pl.Series(column, [float(value) if value is not None else None for value in column_values], dtype=pl.Float64)
        elif column in text_columns:
            frame[column] = pl.Series(column, [str(value) if value is not None else None for value in column_values], dtype=pl.Utf8)
        else:
            frame[column] = pl.Series(column, column_values)
    return pl.DataFrame(frame)

def _entity(frame: dict, prefix: str, phone: str | None) -> tuple[str, str, str]:
    if not phone:
        return "", "", ""
    kind = frame.get(f"{prefix}_entity_type") or "Autre"
    name = frame.get(f"{prefix}_pos_name") or frame.get(f"{prefix}_name") or phone
    if kind == "Commercial" and frame.get(f"{prefix}_commercial"):
        name = frame[f"{prefix}_commercial"]
    elif kind == "CDS" and frame.get(f"{prefix}_cds"):
        name = frame[f"{prefix}_cds"]
    elif kind in {"Caisses", "Point Relais"} and frame.get(f"{prefix}_point_name"):
        name = frame[f"{prefix}_point_name"]
    return phone, str(name), kind


def _build_graph(data: pl.DataFrame, max_nodes: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    if data.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    node_map: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    for row in data.iter_rows(named=True):
        source = _entity(row, "from", row.get("from_msisdn"))
        target = _entity(row, "to", row.get("to_msisdn"))
        if not source[0] or not target[0] or source[0] == target[0]:
            continue
        for phone, name, kind in (source, target):
            node_map.setdefault(phone, {"id": phone, "name": name, "kind": kind,
                                        "territory": row.get("from_territory" if phone == source[0] else "to_territory"),
                                        "segment": row.get("from_segment" if phone == source[0] else "to_segment"),
                                        "degree": 0, "volume": 0.0})
        key = (source[0], target[0])
        item = edges.setdefault(key, {"source": source[0], "target": target[0], "tx_type": "Transfer", "count": 0, "amount": 0.0})
        item["count"] += 1
        item["amount"] += float(row.get("amount") or 0)
        node_map[source[0]]["degree"] += 1
        node_map[target[0]]["degree"] += 1
        node_map[source[0]]["volume"] += float(row.get("amount") or 0)
        node_map[target[0]]["volume"] += float(row.get("amount") or 0)
    ranked = sorted(node_map.values(), key=lambda item: (item["degree"], item["volume"]), reverse=True)
    if len(ranked) > max_nodes:
        allowed = {item["id"] for item in ranked[:max_nodes]}
        ranked = [item for item in ranked if item["id"] in allowed]
        edges = {key: item for key, item in edges.items() if item["source"] in allowed and item["target"] in allowed}
    return (
        pl.DataFrame(ranked, infer_schema_length=None),
        pl.DataFrame(list(edges.values()), infer_schema_length=None) if edges else pl.DataFrame(),
    )

def _spring_positions(nodes: pl.DataFrame, edges: pl.DataFrame, iterations: int = 80) -> dict[str, tuple[float, float]]:
    """Calcule un placement force-directed déterministe sans dépendance graphique externe."""
    node_ids = [row["id"] for row in nodes.iter_rows(named=True)]
    positions = {node_id: (4.0 * ((index * 0.6180339887) % 1.0) - 2.0,
                           4.0 * ((index * 0.3819660113) % 1.0) - 2.0)
                 for index, node_id in enumerate(node_ids)}
    links = [(edge["source"], edge["target"], max(1.0, sqrt(edge["count"])))
             for edge in edges.iter_rows(named=True)]
    if not links:
        return positions
    area = max(24.0, len(node_ids) * 1.8)
    ideal_distance = sqrt(area / max(len(node_ids), 1)) * 1.7
    for _ in range(iterations):
        forces = {node_id: [0.0, 0.0] for node_id in node_ids}
        for index, first in enumerate(node_ids):
            for second in node_ids[index + 1:]:
                dx = positions[first][0] - positions[second][0]
                dy = positions[first][1] - positions[second][1]
                distance = max(hypot(dx, dy), 0.05)
                force = (ideal_distance * ideal_distance) / distance
                fx, fy = force * dx / distance, force * dy / distance
                forces[first][0] += fx
                forces[first][1] += fy
                forces[second][0] -= fx
                forces[second][1] -= fy
        for source, target, weight in links:
            dx = positions[target][0] - positions[source][0]
            dy = positions[target][1] - positions[source][1]
            distance = max(hypot(dx, dy), 0.05)
            force = (distance * distance / ideal_distance) * 0.12 * weight
            fx, fy = force * dx / distance, force * dy / distance
            forces[source][0] += fx
            forces[source][1] += fy
            forces[target][0] -= fx
            forces[target][1] -= fy
        temperature = max(0.04, 0.35 * (1.0 - _ / iterations))
        for node_id, (fx, fy) in forces.items():
            magnitude = max(hypot(fx, fy), 0.05)
            x, y = positions[node_id]
            positions[node_id] = (x + fx / magnitude * min(magnitude, temperature),
                                  y + fy / magnitude * min(magnitude, temperature))
    return positions


def _figure(nodes: pl.DataFrame, edges: pl.DataFrame) -> go.Figure:
    positions = _spring_positions(nodes, edges)
    groups = defaultdict(list)
    for node in nodes.iter_rows(named=True):
        groups[node["kind"]].append(node["id"])
    fig = go.Figure()
    for edge in edges.iter_rows(named=True):
        x0, y0 = positions[edge["source"]]
        x1, y1 = positions[edge["target"]]
        fig.add_trace(go.Scatter(x=[x0, x1, None], y=[y0, y1, None], mode="lines",
                     line={"width": max(0.8, min(5, edge["count"] ** 0.5)), "color": "#c8d0da"},
                                 hoverinfo="text", text=f"{edge['tx_type']} | {edge['count']} transaction(s) | {edge['amount']:,.0f} FCFA",
                                 showlegend=False))
    for kind in sorted(groups):
        rows = [row for row in nodes.iter_rows(named=True) if row["kind"] == kind]
        fig.add_trace(go.Scatter(
            x=[positions[row["id"]][0] for row in rows], y=[positions[row["id"]][1] for row in rows],
            mode="markers",
                marker={"size": [max(7, min(34, 7 + sqrt(row["degree"]) * 3.2)) for row in rows],
                    "color": NODE_COLORS.get(kind, NODE_COLORS["Autre"]), "line": {"width": 1, "color": "white"}},
            name=kind, customdata=[[row["name"], row["id"], row.get("territory") or "", row.get("segment") or ""] for row in rows],
            hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>Territoire: %{customdata[2]}<br>Segment: %{customdata[3]}<extra></extra>"))
    fig.update_layout(height=760, template="plotly_white", hovermode="closest", showlegend=True,
                      xaxis={"visible": False}, yaxis={"visible": False}, margin={"l": 0, "r": 0, "t": 10, "b": 0})
    return fig


def show_relationship_map() -> None:
    st.title("Cartographie relationnelle")
    st.caption("Flux Transfer uniquement | layout force-directed | la taille indique le nombre de relations.")
    try:
        data = load_relationship_data()
    except Exception as exc:
        st.error(f"La cartographie nécessite une base ingérée: {exc}")
        return
    if data.is_empty():
        st.info("Aucune transaction disponible. Lancez l'ingestion pour construire la cartographie.")
        return

    data = data.with_columns(pl.col("date_only").str.to_date(strict=False).alias("_date"))
    min_date = data.get_column("_date").min() or date.today()
    max_date = data.get_column("_date").max() or date.today()
    with st.sidebar:
        st.subheader("Filtres de la cartographie")
        territory_values = sorted(data.select(pl.coalesce([pl.col("territoire_snapshot"), pl.lit("Non renseigné")])).to_series().unique().to_list())
        segment_values = sorted(set(data.get_column("from_segment").drop_nulls().to_list()) | set(data.get_column("to_segment").drop_nulls().to_list()))
        territories = st.multiselect("Territoire", territory_values)
        segments = st.multiselect("Segment", segment_values)
        dates = st.date_input("Période", value=(min_date, max_date), min_value=min_date, max_value=max_date)
        max_nodes = st.slider("Nombre maximum de nœuds", 20, 500, 180, 10)
    filtered = data
    if territories:
        filtered = filtered.filter(pl.col("territoire_snapshot").fill_null("Non renseigné").is_in(territories))
    if segments:
        filtered = filtered.filter(pl.col("from_segment").is_in(segments) | pl.col("to_segment").is_in(segments))
    if isinstance(dates, tuple) and len(dates) == 2:
        filtered = filtered.filter(pl.col("_date").is_between(dates[0], dates[1]))
    nodes, edges = _build_graph(filtered, max_nodes)
    if nodes.is_empty():
        st.warning("Aucune relation ne correspond aux filtres sélectionnés.")
        return
    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Acteurs", nodes.height)
    metric_col2.metric("Relations", edges.height)
    metric_col3.metric("Transactions", filtered.height)
    st.plotly_chart(_figure(nodes, edges), use_container_width=True, config={"displaylogo": False})
    with st.expander("Détails des relations"):
        st.dataframe(edges.sort("amount", descending=True), use_container_width=True, hide_index=True)