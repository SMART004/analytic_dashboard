import pandas as pd

from utils.helpers import clean_phone


ZONE_COLUMNS = ["Zone_Centre", "Zone_Territoire", "Zone_SA"]
POS_ZONE_COLUMNS = ["Zone_Centre", "Zone_Territoire", "Zone_SA", "Secteur"]
COMMERCIAL_ID_COL = "Ccial_MSISDN"
COMMERCIAL_NAME_COL = "Nom_Ccial"
POS_ID_COL = "agent_msisdn"


def collect_clean_numbers(df, columns):
    """Collect normalized phone numbers from the first matching columns."""
    if df is None or df.empty:
        return set()

    work = df.copy()
    work.columns = [str(col).strip() for col in work.columns]

    numbers = set()
    for col in columns:
        if col in work.columns:
            values = work[col].apply(clean_phone).dropna().astype(str)
            numbers.update(value for value in values if value)
    return numbers


def normalize_commercial_reference(comm_df):
    """Normalize the commercial reference once for all transaction pages."""
    if comm_df is None or comm_df.empty:
        return pd.DataFrame(
            columns=[COMMERCIAL_ID_COL, COMMERCIAL_NAME_COL, *ZONE_COLUMNS]
        )

    comm = comm_df.copy()
    comm.columns = [str(col).strip() for col in comm.columns]

    if COMMERCIAL_ID_COL not in comm.columns:
        comm[COMMERCIAL_ID_COL] = pd.NA
    if COMMERCIAL_NAME_COL not in comm.columns:
        comm[COMMERCIAL_NAME_COL] = pd.NA

    comm[COMMERCIAL_ID_COL] = comm[COMMERCIAL_ID_COL].apply(clean_phone)
    comm[COMMERCIAL_NAME_COL] = (
        comm[COMMERCIAL_NAME_COL].astype(str).str.strip().replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}
        )
    )

    for col in ZONE_COLUMNS:
        if col not in comm.columns:
            comm[col] = pd.NA
        comm[col] = (
            comm[col].astype(str).str.strip().replace(
                {"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}
            )
        )

    return (
        comm[[COMMERCIAL_ID_COL, COMMERCIAL_NAME_COL, *ZONE_COLUMNS]]
        .dropna(subset=[COMMERCIAL_ID_COL])
        .drop_duplicates(subset=[COMMERCIAL_ID_COL], keep="first")
        .reset_index(drop=True)
    )


def _first_existing(columns, candidates):
    for col in candidates:
        if col in columns:
            return col
    return None


def normalize_pos_reference(pos_df):
    """Normalize the PDV/agent reference with a stable zone hierarchy."""
    if pos_df is None or pos_df.empty:
        return pd.DataFrame(columns=[POS_ID_COL, *POS_ZONE_COLUMNS])

    pos = pos_df.copy()
    pos.columns = [str(col).strip() for col in pos.columns]

    id_col = _first_existing(pos.columns, ["agent_msisdn", "MSISDN", "Agent MSISDN", "Agent_MSISDN"])
    if id_col is None:
        return pd.DataFrame(columns=[POS_ID_COL, *POS_ZONE_COLUMNS])

    normalized = pd.DataFrame()
    normalized[POS_ID_COL] = pos[id_col].apply(clean_phone)

    candidates = {
        "Zone_Centre": ["Zone_Centre", "zone", "Zone", "Centre_Maitre"],
        "Zone_Territoire": ["Zone_Territoire", "territory", "Territory", "TERRITOIRE"],
        "Zone_SA": ["Zone_SA", "sa_incharge", "zone_sa", "Zone_SA_Normalisee"],
        "Secteur": ["Secteur", "Cluster", "cluster", "Locality", "locality", "quartier"],
    }
    for out_col, source_cols in candidates.items():
        source_col = _first_existing(pos.columns, source_cols)
        normalized[out_col] = (
            pos[source_col].astype(str).str.strip().replace(
                {"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}
            )
            if source_col
            else pd.NA
        )

    return (
        normalized.dropna(subset=[POS_ID_COL])
        .drop_duplicates(subset=[POS_ID_COL], keep="first")
        .reset_index(drop=True)
    )


def get_global_excluded_numbers(
    commerciaux=None,
    caisses=None,
    masters=None,
    cds=None,
    pos_relay_caisse=None,
    include_pos_relay_caisse=True,
):
    """
    One financial-reporting exclusion set.

    These are internal actors and must not be counted as served PDV when they
    appear as the transaction counterparty.
    """
    excluded = set()
    excluded.update(collect_clean_numbers(commerciaux, [COMMERCIAL_ID_COL, "Commercial_MSISDN", "NUM"]))
    excluded.update(collect_clean_numbers(caisses, ["NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]))
    excluded.update(collect_clean_numbers(masters, ["NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]))
    excluded.update(collect_clean_numbers(cds, ["NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"]))

    if include_pos_relay_caisse:
        excluded.update(
            collect_clean_numbers(
                pos_relay_caisse,
                ["MSISDN_PR", "NUM", "MSISDN", "Ccial_MSISDN", "Commercial_MSISDN"],
            )
        )

    return {number for number in excluded if number and number != "None"}


def ensure_transaction_phone_columns(df):
    """Ensure From_clean and To_clean exist with the same normalization."""
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()
    work.columns = [str(col).strip() for col in work.columns]
    work["From_clean"] = work["From"].apply(clean_phone) if "From" in work.columns else pd.NA
    work["To_clean"] = work["To"].apply(clean_phone) if "To" in work.columns else pd.NA
    return work


def attach_commercial_reference(df, comm_df, side="From_clean"):
    """
    Attach commercial identity and zones to transactions.

    Field-force transactions use From_clean as the commercial actor. Other
    pages can pass side="To_clean" explicitly if their flow requires it.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    work = ensure_transaction_phone_columns(df)
    comm = normalize_commercial_reference(comm_df)

    for col in [COMMERCIAL_NAME_COL, *ZONE_COLUMNS]:
        if col not in work.columns:
            work[col] = pd.NA

    if comm.empty or side not in work.columns:
        return work

    merge_cols = [COMMERCIAL_ID_COL, COMMERCIAL_NAME_COL, *ZONE_COLUMNS]
    merged = work.merge(
        comm[merge_cols],
        left_on=side,
        right_on=COMMERCIAL_ID_COL,
        how="left",
        suffixes=("", "_ref"),
    )

    for col in [COMMERCIAL_NAME_COL, *ZONE_COLUMNS]:
        ref_col = f"{col}_ref"
        if ref_col in merged.columns:
            merged[col] = merged[col].combine_first(merged[ref_col])
            merged.drop(columns=[ref_col], inplace=True, errors="ignore")

    return merged


def attach_pos_reference(df, pos_df, side="To_clean", suffix="_pos"):
    """Attach PDV/agent zone fields to transactions from a normalized reference."""
    if df is None or df.empty:
        return pd.DataFrame()

    work = ensure_transaction_phone_columns(df)
    pos = normalize_pos_reference(pos_df)
    if pos.empty or side not in work.columns:
        return work

    rename = {
        "Zone_Centre": f"Zone_Centre{suffix}",
        "Zone_Territoire": f"Zone_Territoire{suffix}",
        "Zone_SA": f"Zone_SA{suffix}",
        "Secteur": f"Secteur{suffix}",
    }

    return work.merge(
        pos[[POS_ID_COL, *POS_ZONE_COLUMNS]].rename(columns=rename),
        left_on=side,
        right_on=POS_ID_COL,
        how="left",
    )


def resolve_transaction_zone(df, comm_df=None, side="From_clean"):
    """Return transactions enriched with the commercial zone hierarchy."""
    return attach_commercial_reference(df, comm_df, side=side)


def resolve_agent_zone(agent_number, pos_df=None, comm_df=None):
    """Resolve centre/territory/zone/sector for one agent or commercial number."""
    number = clean_phone(agent_number)
    if not number:
        return {}

    comm = normalize_commercial_reference(comm_df)
    if not comm.empty:
        match = comm[comm[COMMERCIAL_ID_COL] == number]
        if not match.empty:
            row = match.iloc[0]
            return {
                "type": "commercial",
                "Zone_Centre": row.get("Zone_Centre"),
                "Zone_Territoire": row.get("Zone_Territoire"),
                "Zone_SA": row.get("Zone_SA"),
                "Secteur": pd.NA,
                "Nom_Ccial": row.get(COMMERCIAL_NAME_COL),
            }

    pos = normalize_pos_reference(pos_df)
    if not pos.empty:
        match = pos[pos[POS_ID_COL] == number]
        if not match.empty:
            row = match.iloc[0]
            return {
                "type": "pdv",
                "Zone_Centre": row.get("Zone_Centre"),
                "Zone_Territoire": row.get("Zone_Territoire"),
                "Zone_SA": row.get("Zone_SA"),
                "Secteur": row.get("Secteur"),
            }

    return {}


def is_served_pos_series(df, excluded_numbers=None, min_amount=10000, tx_types=("Transfer",)):
    """
    Boolean mask for financial-reporting PDV served counts.

    The served PDV is always the To_clean counterparty after internal-account
    exclusion.
    """
    if df is None or df.empty:
        return pd.Series(dtype=bool)

    work = ensure_transaction_phone_columns(df)
    excluded_numbers = excluded_numbers or set()
    amount = pd.to_numeric(work.get("Amount", 0), errors="coerce").abs().fillna(0)
    tx_type = work.get("Type", pd.Series(index=work.index, dtype=object)).astype(str).str.strip()

    return (
        tx_type.isin(tx_types)
        & (amount >= min_amount)
        & work["To_clean"].notna()
        & (~work["To_clean"].isin(excluded_numbers))
    )
