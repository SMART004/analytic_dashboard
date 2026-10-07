import pandas as pd

def build_oos_hvc_analysis(assigned: pd.DataFrame, tx: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    try:
        from models.oos_model import get_oos_listing
        df_oos = get_oos_listing(date_start=start_date, date_end=end_date)
    except Exception:
        return pd.DataFrame()

    if df_oos.empty or assigned.empty:
        return pd.DataFrame()

    # Filter assigned for HVC only
    hvc_assigned = assigned[assigned.get("Category", "") == "HVC"].copy()
    if hvc_assigned.empty:
        return pd.DataFrame()

    from utils.helpers import clean_phone
    hvc_msisdns = set(hvc_assigned["To_clean"].dropna().apply(clean_phone).unique())
    
    df_oos["msisdn_clean"] = df_oos["msisdn"].apply(clean_phone)
    df_oos = df_oos[df_oos["msisdn_clean"].isin(hvc_msisdns)].copy()

    if df_oos.empty:
        return pd.DataFrame()

    s_date = pd.to_datetime(start_date) if start_date else pd.to_datetime(df_oos["snapshot_date"].min())
    e_date = pd.to_datetime(end_date) if end_date else pd.to_datetime(df_oos["snapshot_date"].max())
    if pd.isna(s_date) or pd.isna(e_date):
        return pd.DataFrame()
        
    date_range = pd.date_range(s_date, e_date)
    
    tx_dates = set()
    if not tx.empty and "To_clean" in tx.columns and "Date" in tx.columns:
        tx_hvc = tx[tx["To_clean"].isin(hvc_msisdns)].copy()
        tx_hvc["_dt"] = pd.to_datetime(tx_hvc["Date"]).dt.date
        for _, row in tx_hvc.iterrows():
            tx_dates.add((str(row["To_clean"]), row["_dt"]))

    df_oos["_dt"] = pd.to_datetime(df_oos["snapshot_date"]).dt.date
    df_oos["is_oos"] = pd.to_numeric(df_oos["is_oos"], errors="coerce").fillna(0).astype(int)
    
    oos_map = {}
    for _, row in df_oos.iterrows():
        oos_map[(str(row["msisdn_clean"]), row["_dt"])] = row["is_oos"]

    pos_oos_days = {}
    pos_total_days = len(date_range)
    
    for msisdn in hvc_msisdns:
        days_oos = 0
        last_state = 0
        for d in date_range:
            dt = d.date()
            if (msisdn, dt) in tx_dates:
                current_state = 0
            elif (msisdn, dt) in oos_map:
                current_state = oos_map[(msisdn, dt)]
            else:
                current_state = last_state
                
            if current_state == 1:
                days_oos += 1
            last_state = current_state
            
        pos_oos_days[msisdn] = days_oos

    hvc_assigned["jours_oos"] = hvc_assigned["To_clean"].map(pos_oos_days).fillna(0)
    hvc_assigned["jours_total"] = pos_total_days
    
    try:
        from models.pos_model import get_all_pos, get_all_sites
        ref_pos = get_all_pos()
        sites = get_all_sites()
        if not ref_pos.empty:
            ref_pos["msisdn_clean"] = ref_pos["agent_msisdn"].apply(clean_phone)
            if not sites.empty:
                ref_pos = ref_pos.merge(sites[["site_key", "sitename"]], on="site_key", how="left")
            site_col = "sitename" if "sitename" in ref_pos.columns else "site_key"
            site_map = ref_pos.drop_duplicates("msisdn_clean").set_index("msisdn_clean")[site_col]
            hvc_assigned["Site"] = hvc_assigned["To_clean"].map(site_map).fillna("Inconnu")
        else:
            hvc_assigned["Site"] = "Inconnu"
    except:
        hvc_assigned["Site"] = "Inconnu"

    agg_df = hvc_assigned.groupby(["Commercial", "Site"]).agg(
        Nb_HVC=("To_clean", "nunique"),
        Jours_OOS=("jours_oos", "sum"),
        Jours_Total=("jours_total", "sum")
    ).reset_index()
    
    agg_df["% OOS HVC (Sévérité)"] = (agg_df["Jours_OOS"] / agg_df["Jours_Total"] * 100).round(1).fillna(0)
    return agg_df.sort_values("% OOS HVC (Sévérité)", ascending=False)
