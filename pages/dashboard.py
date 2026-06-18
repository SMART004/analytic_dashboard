# pages/dashboard.py
import streamlit as st
import plotly.express as px
import pandas as pd

from utils.helpers import clean_phone
from utils.storage import get_files_by_month, list_month_folders
from utils.supabase import load_setting

PERF_BUCKETS = ["performance-result-files"]
OOS_BUCKET = "listing-oos-result-files"


def _format_fcfa(value):
    try:
        value = float(value)
    except Exception:
        return "0 FCFA"

    if value == 0:
        return "0 FCFA"

    formatted = f"{int(value):,}".replace(",", " ")
    return f"{formatted} FCFA"


@st.cache_data(show_spinner=False)
def _load_latest_month_df(bucket):
    folders = list_month_folders(bucket=bucket)
    if not folders:
        return pd.DataFrame()

    latest_folder = sorted(folders, reverse=True)[0]
    df = get_files_by_month(bucket=bucket, selected_month=latest_folder, max_workers=4)
    return df if df is not None else pd.DataFrame()


def _normalize_transaction_df(df):
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()
    work.columns = [str(col).strip() for col in work.columns]

    if "Amount" in work.columns:
        work["Amount_num"] = pd.to_numeric(work["Amount"], errors="coerce").abs().fillna(0)
    else:
        work["Amount_num"] = 0

    for col in ["From", "To"]:
        if col in work.columns:
            work[f"{col}_clean"] = work[col].apply(clean_phone)
        else:
            work[f"{col}_clean"] = None

    if "Type" in work.columns:
        work["Type"] = work["Type"].astype(str).str.strip()
    else:
        work["Type"] = ""

    if "Balance" in work.columns:
        work["Balance_num"] = pd.to_numeric(work["Balance"], errors="coerce")
    else:
        work["Balance_num"] = pd.NA

    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce")

    if "Type" in work.columns:
        work["Type"] = work["Type"].astype(str).str.strip()
        type_map = {
            "cash out": "Cash out",
            "cash_out": "Cash out",
            "cash in": "Cash in",
            "cash_in": "Cash in",
            "transfer": "Transfer",
        }
        work["Type"] = work["Type"].str.lower().map(type_map).fillna(work["Type"])

    def _find_name_column(columns, candidates):
        for col in candidates:
            if col in columns:
                return col
        return None

    from_name_col = _find_name_column(work.columns, ["From Name", "From name", "From_Name", "from_name"])
    to_name_col = _find_name_column(work.columns, ["To Name", "To name", "To_Name", "to_name"])

    if from_name_col:
        work["From_name"] = work[from_name_col].astype(str).str.strip()
    else:
        work["From_name"] = pd.NA

    if to_name_col:
        work["To_name"] = work[to_name_col].astype(str).str.strip()
    else:
        work["To_name"] = pd.NA

    if "Nom_Ccial" in work.columns:
        work["Nom_Ccial"] = work["Nom_Ccial"].astype(str).str.strip().replace({"": pd.NA})
    else:
        work["Nom_Ccial"] = pd.NA

    same_name_cashout = (
        work["Type"].eq("Cash out") &
        work["From_name"].notna() &
        work["To_name"].notna() &
        work["From_name"].str.lower().eq(work["To_name"].str.lower())
    )
    work.loc[same_name_cashout & work["Nom_Ccial"].isna(), "Nom_Ccial"] = work.loc[
        same_name_cashout & work["Nom_Ccial"].isna(),
        "From_name"
    ]

    return work


def _compute_float_values(df):
    if df is None or df.empty:
        return 0, 0

    down_mask = (
        df["Type"].isin(["Transfer", "Cash out"]) &
        df["From_clean"].notna()
    )
    up_mask = (
        df["Type"].isin(["Transfer", "Cash in"]) &
        df["To_clean"].notna()
    )

    float_down = df.loc[down_mask, "Amount_num"].sum()
    float_up = df.loc[up_mask, "Amount_num"].sum()
    return float_down, float_up


def _find_zone_column(df):
    if df is None or df.empty:
        return None

    candidates = ["Zone", "Zone_Centre", "Zone_SA", "Zone_Territoire", "TERRITOIRE"]
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _build_top_commerciaux_df(perf_df):
    if perf_df is None or perf_df.empty or "Nom_Ccial" not in perf_df.columns:
        return pd.DataFrame()

    base = perf_df.copy()
    if "Amount_num" not in base.columns and "Amount" in base.columns:
        base = _normalize_transaction_df(base)

    rotation_series = _compute_commercial_rotation(base)

    if "Σ_FD" in base.columns:
        group_cols = ["Nom_Ccial"]
        agg = {
            "Σ_FD": "sum",
            "Nb_Jours": "sum",
            "TR_General": "first"
        }
        top = base.groupby(group_cols, dropna=False).agg(agg).reset_index()
        top = top.sort_values(by="Σ_FD", ascending=False)
        top["Montant Distribué"] = top["Σ_FD"]
    elif "Amount_num" in base.columns:
        top = base.groupby(["Nom_Ccial"], dropna=False).agg(
            Montant_Distribue=("Amount_num", "sum"),
            Nb_Jours=("Date", lambda x: x.dt.date.nunique() if x.notna().any() else 0)
        ).reset_index()
        top = top.sort_values(by="Montant_Distribue", ascending=False)
        top = top.rename(columns={"Montant_Distribue": "Montant Distribué"})
    else:
        return pd.DataFrame()

    top = top.merge(rotation_series.rename("TR_General"), on="Nom_Ccial", how="left")

    top = top.head(5)
    top["Taux de Rotation"] = pd.to_numeric(
        top["TR_General"].astype(str).str.replace("%", "", regex=False),
        errors="coerce"
    ).fillna(0.0)

    top["Performance"] = top["Montant Distribué"].apply(
        lambda x: "Excellent" if x >= 150_000_000 else ("Très bon" if x >= 80_000_000 else "Bon")
    )

    return top[["Nom_Ccial", "Montant Distribué", "Taux de Rotation", "Performance"]].rename(
        columns={"Nom_Ccial": "Commercial"}
    )


def _first_existing(columns, candidates):
    for col in candidates:
        if col in columns:
            return col
    return None


def _compute_commercial_rotation(df):
    if df is None or df.empty or "Nom_Ccial" not in df.columns:
        return pd.Series(dtype=float)

    work = df.copy()
    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
        work["Date_only"] = work["Date"].dt.date
    else:
        work["Date_only"] = pd.NA

    if "To_clean" not in work.columns and "To" in work.columns:
        work["To_clean"] = work["To"].apply(clean_phone)

    work = work[work["Type"].isin(["Transfer", "Cash in", "Cash out"])].copy()
    work = work[work["Date_only"].notna() & work["To_clean"].notna()]
    if work.empty:
        return pd.Series(dtype=float)

    grouped = work.groupby("Nom_Ccial", dropna=False).agg(
        Nb_Transactions=("Type", "count"),
        POS_Serve=("To_clean", "nunique"),
        Nb_Jours=("Date_only", "nunique")
    )
    grouped["TR_General"] = (
        grouped["Nb_Transactions"]
        / (grouped["POS_Serve"].replace(0, pd.NA) * grouped["Nb_Jours"].replace(0, pd.NA) * 3)
    ) * 100
    grouped["TR_General"] = grouped["TR_General"].fillna(0)
    return grouped["TR_General"]


def _load_consolidated_perf_df():
    buckets = PERF_BUCKETS
    if not buckets:
        return pd.DataFrame()

    folder = buckets[0]
    month_folders = list_month_folders(bucket=folder)
    if not month_folders:
        return pd.DataFrame()

    dfs = []
    for selected_month in month_folders:
        df = get_files_by_month(bucket=folder, selected_month=selected_month, max_workers=4)
        if df is not None and not df.empty:
            dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    consolidated = pd.concat(dfs, ignore_index=True)
    consolidated.columns = [str(col).strip() for col in consolidated.columns]

    dedup_cols = [col for col in consolidated.columns if col != "source_file"]
    consolidated = consolidated.drop_duplicates(subset=dedup_cols, keep="first").reset_index(drop=True)
    return consolidated


def _ensure_date_column(df):
    if df is None or df.empty:
        return df

    work = df.copy()
    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
    elif "Date_only" in work.columns:
        work["Date"] = pd.to_datetime(work["Date_only"], errors="coerce")
    return work


def _attach_zone_fields(df):
    # Les colonnes Zone_Centre, Zone_Territoire et Zone_SA sont fournies
    # par le fichier de configuration des commerciaux.
    if df is None or df.empty:
        return df

    work = df.copy()
    work["_row_id"] = range(len(work))
    if "Nom_Ccial" in work.columns:
        work["Nom_Ccial"] = work["Nom_Ccial"].astype(str).str.strip()
        work.loc[work["Nom_Ccial"].isin(["nan", "None", "", "<NA>"]), "Nom_Ccial"] = pd.NA

    comm_config = load_setting("commerciaux")
    if comm_config is None or comm_config.empty or "Nom_Ccial" not in comm_config.columns:
        for col in ["Zone_Centre", "Zone_Territoire", "Zone_SA"]:
            if col not in work.columns:
                work[col] = pd.NA
        work = work.drop(columns=["_row_id"], errors="ignore")
        return work

    comm = comm_config.copy()
    comm.columns = [str(col).strip() for col in comm.columns]
    comm["Nom_Ccial"] = comm["Nom_Ccial"].astype(str).str.strip()

    if "Ccial_MSISDN" in comm.columns:
        comm["Ccial_MSISDN"] = comm["Ccial_MSISDN"].apply(clean_phone)
    else:
        comm["Ccial_MSISDN"] = pd.NA

    for col in ["Zone_Centre", "Zone_Territoire", "Zone_SA"]:
        if col not in comm.columns:
            comm[col] = pd.NA

    comm = comm[["Nom_Ccial", "Ccial_MSISDN", "Zone_Centre", "Zone_Territoire", "Zone_SA"]].drop_duplicates(subset=["Nom_Ccial", "Ccial_MSISDN"])

    for col in ["Zone_Centre", "Zone_Territoire", "Zone_SA"]:
        if col not in work.columns:
            work[col] = pd.NA

    if "Nom_Ccial" in work.columns:
        merged = work.merge(comm.drop(columns=["Ccial_MSISDN"]), on="Nom_Ccial", how="left", suffixes=("", "_comm"))

        if ("From_clean" in work.columns or "To_clean" in work.columns) and merged[
            ["Zone_Centre", "Zone_Territoire", "Zone_SA"]
        ].isna().any(axis=1).any():
            fallback = work.copy()
            if "From_clean" not in fallback.columns or "To_clean" not in fallback.columns:
                for col in ["From", "To"]:
                    fallback[f"{col}_clean"] = fallback[col].apply(clean_phone) if col in fallback.columns else pd.NA

            fallback["Ccial_MSISDN"] = fallback["From_clean"].fillna(fallback["To_clean"])
            fallback_merge = fallback.merge(comm, on="Ccial_MSISDN", how="left", suffixes=("", "_comm"))

            for col in ["Zone_Centre", "Zone_Territoire", "Zone_SA", "Nom_Ccial"]:
                if f"{col}_comm" in fallback_merge.columns:
                    merged[col] = merged[col].combine_first(fallback_merge[f"{col}_comm"])
    else:
        if "From_clean" not in work.columns or "To_clean" not in work.columns:
            for col in ["From", "To"]:
                work[f"{col}_clean"] = work[col].apply(clean_phone) if col in work.columns else pd.NA

        work["Ccial_MSISDN"] = work["From_clean"].fillna(work["To_clean"])
        merged = work.merge(comm, on="Ccial_MSISDN", how="left", suffixes=("", "_comm"))
        if "Nom_Ccial" not in merged.columns:
            merged["Nom_Ccial"] = merged["Nom_Ccial_comm"]

    for col in ["Zone_Centre", "Zone_Territoire", "Zone_SA", "Nom_Ccial"]:
        if f"{col}_comm" in merged.columns:
            merged[col] = merged[col].combine_first(merged[f"{col}_comm"])
            merged.drop(columns=[f"{col}_comm"], inplace=True, errors="ignore")

    merged = merged.drop(columns=["_row_id"], errors="ignore")
    return merged


def _compute_unique_pos_served(df):
    if df is None or df.empty:
        return 0

    mask = (
        df["Type"].isin(["Transfer", "Cash out"]) &
        df["To_clean"].notna()
    )
    return int(df.loc[mask, "To_clean"].nunique())


def _prepare_master_pos_df(master_df):
    if master_df is None or master_df.empty:
        return pd.DataFrame()

    master = master_df.copy()
    master.columns = [str(col).strip() for col in master.columns]
    msisdn_col = _first_existing(master.columns, ["MSISDN", "Agent MSISDN", "Agent_MSISDN", "agent_msisdn"])
    if msisdn_col is None:
        return pd.DataFrame()

    master["POS_MSISDN"] = master[msisdn_col].apply(clean_phone)
    master = master[master["POS_MSISDN"].notna()].copy()

    territory_col = _first_existing(master.columns, ["Territory", "Zone_Territoire", "Zone", "Cluster", "Locality"])
    master["Territory"] = master[territory_col].astype(str).str.strip() if territory_col else pd.NA

    return master[["POS_MSISDN", "Territory"]].drop_duplicates(subset=["POS_MSISDN"])


def _load_master_pos_totals():
    master_ii = _prepare_master_pos_df(load_setting("maitre_pos"))
    master_iii = _prepare_master_pos_df(load_setting("maitre_pos_III"))

    if master_ii.empty and master_iii.empty:
        return pd.DataFrame()

    total = pd.concat([master_ii, master_iii], ignore_index=True).drop_duplicates(subset=["POS_MSISDN"])
    return total


def _count_master_pos_by_centre(center_label):
    master_ii = load_setting("maitre_pos")
    master_iii = load_setting("maitre_pos_III")

    def count_df(df, zone_name=None):
        if df is None or df.empty:
            return 0
        if "agent_msisdn" not in df.columns:
            return 0
        work = df.copy()
        work["agent_msisdn"] = work["agent_msisdn"].apply(clean_phone)
        work = work[work["agent_msisdn"].notna()]
        if zone_name:
            if "zone" in work.columns:
                return work[work["zone"].astype(str).str.strip().str.lower() == zone_name.lower()]["agent_msisdn"].nunique()
            return 0
        return work["agent_msisdn"].nunique()

    if center_label is None or str(center_label).strip().lower() == "toutes":
        return count_df(master_ii) + count_df(master_iii)

    centre_norm = str(center_label).strip().lower()
    if "centre iii" in centre_norm or "iii" in centre_norm:
        return count_df(master_iii, zone_name="centre iii")
    if "centre ii" in centre_norm or "ii" in centre_norm:
        return count_df(master_ii, zone_name="centre ii")

    return count_df(master_ii, zone_name=centre_norm) + count_df(master_iii, zone_name=centre_norm)


def _build_territory_performance_df(df):
    if df is None or df.empty:
        return pd.DataFrame()

    territory_column = "Zone_Territoire" if "Zone_Territoire" in df.columns else _find_zone_column(df)
    if territory_column is None:
        territory_column = "Zone"

    work = df.copy()
    work[territory_column] = work[territory_column].fillna("NON RENSEIGNÉ").astype(str)

    distributed = work[work["Type"].isin(["Transfer", "Cash out"])]
    if distributed.empty:
        return pd.DataFrame()

    summary = (
        distributed.groupby(territory_column, dropna=False)
        .agg(
            Montant_Distribué=("Amount_num", "sum"),
            POS_Servis_Uniques=("To_clean", lambda x: x.nunique())
        )
        .reset_index()
    )

    summary = summary.rename(columns={territory_column: "Zone"})

    master_totals = _load_master_pos_totals()
    totals = {}
    if not master_totals.empty and "Territory" in master_totals.columns:
        totals = (
            master_totals.groupby("Territory")["POS_MSISDN"]
            .nunique()
            .to_dict()
        )

    if not totals:
        comm_config = load_setting("commerciaux")
        if comm_config is not None and not comm_config.empty and "Zone_Territoire" in comm_config.columns:
            comm = comm_config.copy()
            comm["Zone_Territoire"] = comm["Zone_Territoire"].astype(str).str.strip().replace("", "NON RENSEIGNÉ")
            if "Ccial_MSISDN" in comm.columns:
                totals = comm.groupby("Zone_Territoire")["Ccial_MSISDN"].nunique().to_dict()
            else:
                totals = comm.groupby("Zone_Territoire")["Nom_Ccial"].nunique().to_dict()

    summary["Total_POS_Territoire"] = summary["Zone"].map(totals).fillna(0).astype(int)
    summary["Taux de couverture"] = summary.apply(
        lambda row: row["POS_Servis_Uniques"] / row["Total_POS_Territoire"]
        if row["Total_POS_Territoire"] > 0 else 0,
        axis=1
    )

    total_distributed = summary["Montant_Distribué"].sum()
    total_pos = summary["POS_Servis_Uniques"].sum()
    summary["Distribution_pct"] = summary["Montant_Distribué"] / total_distributed if total_distributed > 0 else 0
    summary["POS_pct"] = summary["POS_Servis_Uniques"] / total_pos if total_pos > 0 else 0
    summary["Ecart"] = summary["Distribution_pct"] - summary["POS_pct"]
    summary = summary.sort_values(by="Ecart", ascending=False)

    return summary


def _compute_transaction_type_summary(df):
    if df is None or df.empty or "Type" not in df.columns:
        return pd.DataFrame()

    work = df.copy()
    work["Type"] = work["Type"].astype(str).str.strip()
    summary = (
        work.groupby("Type", dropna=False)
        .size()
        .reset_index(name="Count")
    )
    summary["Type"] = summary["Type"].replace({"nan": "Autre", "None": "Autre"})
    summary.loc[summary["Type"].isin(["", "None", "nan"]), "Type"] = "Autre"
    return summary


def _style_top_table(df):
    if df is None or df.empty:
        return df

    styled = (
        df.copy()
        .pipe(lambda d: d.replace({None: pd.NA}))
        .style
        .format({
            "Montant Distribué": "{:,.0f}",
            "Taux de Rotation": "{:.1f}%"
        }, na_rep="N/A")
        .set_table_styles([
            {
                "selector": "th",
                "props": [
                    ("background-color", "black"),
                    ("color", "#f1c40f"),
                    ("font-weight", "bold"),
                    ("text-align", "center"),
                ],
            },
            {
                "selector": "td",
                "props": [
                    ("text-align", "center"),
                    ("border", "1px solid #ddd"),
                ],
            },
        ])
        .background_gradient(cmap="Blues", subset=["Montant Distribué"])
    )
    return styled


def show_dashboard():
    st.title("📊 Dashboard Général")
    st.markdown("### Vue d'ensemble des performances commerciales")

    perf_df = _load_consolidated_perf_df()
    if perf_df.empty:
        st.warning("Aucun fichier de performance disponible dans le bucket performance-result-files.")
        return

    perf_df = _ensure_date_column(perf_df)
    perf_df = _normalize_transaction_df(perf_df)
    perf_df = _attach_zone_fields(perf_df)
    tx_df = perf_df

    if tx_df.empty:
        st.warning("Impossible d'analyser les données de transaction. Vérifiez le format des fichiers de performance.")
        return

    available_dates = sorted(tx_df["Date"].dt.date.dropna().unique())
    if len(available_dates) >= 2:
        default_date_range = (available_dates[0], available_dates[-1])
    elif len(available_dates) == 1:
        default_date_range = available_dates[0]
    else:
        today = pd.Timestamp.today().date()
        default_date_range = (today, today)

    centre_options = sorted(tx_df["Zone_Centre"].fillna("NON RENSEIGNÉ").astype(str).unique())
    if "Centre II" in centre_options:
        default_centre = "Centre II"
    else:
        default_centre = centre_options[0] if centre_options else "Toutes"

    with st.sidebar:
        if available_dates:
            selected_dates = st.date_input(
                "Plage de dates",
                value=default_date_range,
                min_value=available_dates[0],
                max_value=available_dates[-1],
                key="dashboard_date_filter"
            )
        else:
            selected_dates = default_date_range

        if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
            start_date, end_date = selected_dates
        else:
            start_date = selected_dates
            end_date = selected_dates
        if not isinstance(start_date, pd.Timestamp):
            start_date = pd.to_datetime(start_date)
        if not isinstance(end_date, pd.Timestamp):
            end_date = pd.to_datetime(end_date)

        selected_centre = st.selectbox(
            "Centre",
            options=["Toutes"] + centre_options,
            index=(["Toutes"] + centre_options).index(default_centre)
            if default_centre in centre_options else 0,
            key="dashboard_centre_filter"
        )

        filtered_by_centre = tx_df.copy()
        if available_dates:
            filtered_by_centre = filtered_by_centre[
                (filtered_by_centre["Date"] >= pd.to_datetime(start_date)) &
                (filtered_by_centre["Date"] <= pd.to_datetime(end_date))
            ]

        if selected_centre != "Toutes":
            filtered_by_centre = filtered_by_centre[filtered_by_centre["Zone_Centre"] == selected_centre]

        territoire_options = sorted(
            filtered_by_centre["Zone_Territoire"].fillna("NON RENSEIGNÉ").astype(str).unique()
        )
        selected_territoire = st.selectbox(
            "Territoire",
            options=["Toutes"] + territoire_options,
            key="dashboard_territoire_filter"
        )

        filtered_by_territoire = filtered_by_centre.copy()
        if selected_territoire != "Toutes":
            filtered_by_territoire = filtered_by_territoire[filtered_by_territoire["Zone_Territoire"] == selected_territoire]

        commercial_options = sorted(filtered_by_territoire["Nom_Ccial"].dropna().astype(str).unique())
        selected_commercial = st.selectbox(
            "Commercial",
            options=["Tous"] + commercial_options,
            key="dashboard_commercial_filter"
        )

    filtered_df = filtered_by_territoire.copy()
    if selected_commercial != "Tous":
        filtered_df = filtered_df[filtered_df["Nom_Ccial"] == selected_commercial]

    if filtered_df.empty:
        st.warning("Aucun résultat trouvé pour la combinaison de filtres sélectionnée.")
        return

    distributed_amount = _compute_float_values(filtered_df)[0]
    unique_pos_served = _compute_unique_pos_served(filtered_df)
    total_pos_for_centre = _count_master_pos_by_centre(selected_centre)

    rotation_values = _compute_commercial_rotation(filtered_df)
    rotation = rotation_values.mean() if not rotation_values.empty else None
    if pd.isna(rotation):
        rotation = None
    if rotation is None and distributed_amount > 0:
        float_up = _compute_float_values(filtered_df)[1]
        rotation = float_up / distributed_amount

    pos_served_value = (
        f"{unique_pos_served:,}/{total_pos_for_centre:,}"
        if total_pos_for_centre > 0 else
        f"{unique_pos_served:,}/N/A"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(label="Montant Distribué", value=_format_fcfa(distributed_amount), delta=None)
    with col2:
        st.metric(label="POS servis uniques", value=pos_served_value, delta=None)
    with col3:
        st.metric(label="Rotation Moyenne", value=f"{rotation:.2f}" if rotation is not None else "N/A", delta=None)

    st.divider()

    st.subheader("🏆 Top Commerciaux")
    top5_df = _build_top_commerciaux_df(filtered_df)
    if top5_df.empty:
        st.warning("Aucun top commerciaux disponible dans la sélection actuelle.")
    else:
        st.dataframe(
            _style_top_table(top5_df),
            use_container_width=True,
            hide_index=True
        )

    st.divider()

    col_g1, col_g2 = st.columns(2)
    with col_g1:
        st.subheader("Répartition des types de transactions")
        type_summary = _compute_transaction_type_summary(filtered_df)
        if not type_summary.empty:
            fig_type = px.bar(
                type_summary,
                x="Type",
                y="Count",
                title="Répartition des types de transactions",
                labels={"Count": "Nombre", "Type": "Type de transaction"},
                height=420
            )
            fig_type.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig_type, use_container_width=True)
        else:
            st.warning("Aucune information de type de transaction disponible.")

    with col_g2:
        st.subheader("Distribution des montants de transactions")
        amount_df = filtered_df.copy()
        if "Amount_num" in amount_df.columns:
            amount_df = amount_df[amount_df["Amount_num"] > 0].copy()
        if not amount_df.empty:
            fig_hist = px.histogram(
                amount_df,
                x="Amount_num",
                nbins=50,
                title="Histogramme des montants de transactions",
                labels={"Amount_num": "Montant transaction (FCFA)"},
                height=420
            )
            fig_hist.update_layout(xaxis_title="Montant (FCFA)", yaxis_title="Nombre de transactions", bargap=0.05)
            st.plotly_chart(fig_hist, use_container_width=True)
        else:
            st.warning("Aucun montant de transaction positif disponible pour l'histogramme.")

    st.divider()
    st.subheader("POS servis vs Distribution par Territoire")
    territory_summary = _build_territory_performance_df(filtered_df)
    if territory_summary.empty:
        st.warning("Impossible de construire l'analyse territoriale avec la sélection actuelle.")
    else:
        display_summary = territory_summary.copy()
        display_summary["Taux de couverture"] = display_summary["Taux de couverture"].apply(
            lambda x: f"{x:.1%}" if pd.notna(x) else "N/A"
        )
        display_summary["Ecart"] = display_summary["Ecart"].apply(
            lambda x: f"{x:.1%}" if pd.notna(x) else "N/A"
        )

        fig_territory = px.bar(
            territory_summary,
            x="Zone",
            y="Montant_Distribué",
            color="Ecart",
            title="POS servis vs Distribution par Territoire",
            hover_data={
                "POS_Servis_Uniques": True,
                "Taux de couverture": ":.1%",
                "Total_POS_Territoire": True
            },
            height=450
        )
        fig_territory.update_layout(yaxis_title="Montant Distribué")
        st.plotly_chart(fig_territory, use_container_width=True)

        st.dataframe(
            display_summary[[
                "Zone",
                "Montant_Distribué",
                "POS_Servis_Uniques",
                "Taux de couverture",
                "Ecart"
            ]],
            use_container_width=True,
            hide_index=True
        )

    st.divider()