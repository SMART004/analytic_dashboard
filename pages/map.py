import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from folium.plugins import HeatMap, MarkerCluster, Fullscreen
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import time

def show_pos_map():
    st.title("Carte des POS")

    pos_master = st.session_state.get('pos_master_df')
    if pos_master is None:
        st.error("Veuillez charger le **Fichier Maître POS** dans Settings")
        st.stop()

    df_map = pos_master.copy()
    df_map.columns = [col.strip() for col in df_map.columns]

    # ===================== GÉOCODAGE AUTOMATIQUE =====================
    lat_col = next((col for col in df_map.columns if col.lower() in ['latitude', 'lat']), None)
    lon_col = next((col for col in df_map.columns if col.lower() in ['longitude', 'lon', 'long']), None)

    if lat_col is None or lon_col is None:
        st.warning("Colonnes latitude/longitude non trouvées. Géocodage automatique en cours...")

        with st.spinner("Géocodage des sites en cours (cela peut prendre du temps)..."):
            geolocator = Nominatim(user_agent="float_tracker_cm")
            geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.2)

            def get_coords(site_name):
                try:
                    if pd.isna(site_name):
                        return None, None
                    location = geocode(f"{site_name}, Yaoundé, Cameroon")
                    if location:
                        return location.latitude, location.longitude
                    return None, None
                except:
                    return None, None

            # On utilise la colonne SITENAME ou Locality
            site_col = next((col for col in df_map.columns if col.upper() in ['SITENAME', 'LOCALITY', 'SITE']), 'SITENAME')

            if site_col in df_map.columns:
                df_map['latitude'], df_map['longitude'] = zip(*df_map[site_col].apply(get_coords))
                st.success("Géocodage terminé !")
            else:
                st.error("Impossible de trouver une colonne pour le nom du site (SITENAME ou Locality).")
                st.stop()

    # Nettoyage MSISDN
    if 'Agent MSISDN' in df_map.columns:
        df_map['MSISDN'] = df_map['Agent MSISDN'].astype(str).str.strip()
    elif 'MSISDN' in df_map.columns:
        df_map['MSISDN'] = df_map['MSISDN'].astype(str).str.strip()

    # ===================== FILTRES =====================
    st.sidebar.subheader("Filtres")
    zone_list = ["Toutes"]
    zone_col = next((col for col in df_map.columns if col.upper() in ['ZONE', 'ZONE NEW']), None)
    if zone_col:
        zone_list += sorted(df_map[zone_col].dropna().unique().tolist())

    selected_zone = st.sidebar.selectbox("Zone", zone_list, key="map_zone")

    if selected_zone != "Toutes" and zone_col:
        df_map = df_map[df_map[zone_col] == selected_zone]

    # ===================== CRÉATION DE LA CARTE =====================
    m = folium.Map(location=[3.8480, 11.5021], zoom_start=10, tiles="CartoDB positron")
    Fullscreen().add_to(m)

    # HeatMap
    heat_data = []
    for _, row in df_map.iterrows():
        if pd.notna(row.get('latitude')) and pd.notna(row.get('longitude')):
            heat_data.append([row['latitude'], row['longitude']])

    if heat_data:
        HeatMap(heat_data, radius=15, blur=10, max_zoom=13).add_to(m)

    # MarkerCluster
    marker_cluster = MarkerCluster(name="POS").add_to(m)

    for _, row in df_map.iterrows():
        if pd.notna(row.get('latitude')) and pd.notna(row.get('longitude')):
            popup_html = f"""
                <b>POS :</b> {row.get('MSISDN', 'N/A')}<br>
                <b>Nom :</b> {row.get('SA Name', 'N/A')}<br>
                <b>Zone :</b> {row.get('Zone', row.get('ZONE NEW', 'N/A'))}<br>
                <b>Day Target :</b> {row.get('Day_Target', 0):,.0f}<br>
                <b>OOS Target :</b> {row.get('OOS_Target', 0):,.0f}
            """

            folium.Marker(
                location=[row['latitude'], row['longitude']],
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=row.get('SA Name', row.get('MSISDN', 'POS')),
                icon=folium.Icon(color="blue", icon="map-marker", prefix="fa")
            ).add_to(marker_cluster)

    # Affichage de la carte
    st_folium(m, width="100%", height=700)

    # ===================== STATISTIQUES =====================
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total POS", len(df_map))
    with col2:
        st.metric("POS géolocalisés", len(heat_data))
    with col3:
        st.metric("Zones affichées", df_map.get('Zone', df_map.get('ZONE NEW', pd.Series())).nunique())

    st.caption("🔵 Points = POS | HeatMap = Densité des POS")
