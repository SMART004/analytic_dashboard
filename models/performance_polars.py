"""Metrics shared by the three performance segments, computed with Polars."""

from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd
import polars as pl

from models.db import get_connection
from utils.helpers import clean_phone


def _tx_frame(transactions: pd.DataFrame) -> pl.DataFrame:
    if transactions.empty:
        return pl.DataFrame()
    return pl.from_pandas(transactions, include_index=False).with_columns([
        pl.col("Actor_MSISDN").cast(pl.Utf8, strict=False)
            .map_elements(clean_phone, return_dtype=pl.Utf8),
        pl.col("To_clean").cast(pl.Utf8, strict=False)
            .map_elements(clean_phone, return_dtype=pl.Utf8),
        pl.col("Date_only").cast(pl.Utf8, strict=False),
        pl.col("Amount").cast(pl.Float64, strict=False).fill_null(0),
        pl.col("Hour").cast(pl.Int64, strict=False).fill_null(0),
        pl.col("Date").cast(pl.Datetime, strict=False),
    ]).filter(pl.col("Amount") >= 10_000)


def _clean_key_columns(frame: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """Apply the same phone canonicalization to SQL and transaction keys."""
    expressions = []
    for column in columns:
        if column in frame.columns:
            expressions.append(
                pl.col(column).cast(pl.Utf8, strict=False)
                .map_elements(clean_phone, return_dtype=pl.Utf8)
                .alias(column)
            )
    return frame.with_columns(expressions) if expressions else frame


def _segment_frame(transactions: pd.DataFrame, hvc_set: set[str], others_set: set[str], split: bool) -> pl.DataFrame:
    work = _tx_frame(transactions)
    if work.is_empty():
        return work
    return work.with_columns(
        pl.when(pl.col("To_clean").is_in(list(hvc_set))).then(pl.lit("HVC"))
        .when((not split) | pl.col("To_clean").is_in(list(others_set))).then(pl.lit("OTHER"))
        .otherwise(pl.lit("UNKNOWN")).alias("Segment")
    ).filter(pl.col("Segment") != "UNKNOWN")


def compute_hvc_others_polars(
    transactions: pd.DataFrame, hvc_set: set[str], others_set: set[str], split: bool
) -> pd.DataFrame:
    work = _segment_frame(transactions, hvc_set, others_set, split)
    if work.is_empty():
        return pd.DataFrame(columns=["Actor_MSISDN", "Nb_Jours", "FD_HVC", "HVC_Serve", "Nb_Trans_HVC", "FD_Others", "Other_Serve", "Nb_Trans_Other"])
    days = work.group_by("Actor_MSISDN").agg(pl.col("Date_only").n_unique().alias("Nb_Jours"))
    metrics = work.group_by(["Actor_MSISDN", "Segment"]).agg([
        pl.col("Amount").sum().alias("FD"),
        pl.col("To_clean").n_unique().alias("Serve"),
        pl.len().alias("Nb_Trans"),
    ])
    result = days
    for segment, prefix in (("HVC", "HVC"), ("OTHER", "Other")):
        values = metrics.filter(pl.col("Segment") == segment).select([
            "Actor_MSISDN", pl.col("FD").alias("FD_Others" if prefix == "Other" else f"FD_{prefix}"),
            pl.col("Serve").alias(f"{prefix}_Serve"), pl.col("Nb_Trans").alias(f"Nb_Trans_{prefix}"),
        ])
        result = result.join(values, on="Actor_MSISDN", how="left")
    return result.fill_null(0).to_pandas()


# def compute_pr_portfolio_metrics(
#     transactions: pd.DataFrame,
#     hvc_set: set[str],
#     others_set: set[str],
#     start_date: Optional[str],
#     end_date: Optional[str],
#     conn: sqlite3.Connection,
# ) -> pd.DataFrame:
#     """Compute PR KPIs from the assigned portfolio and expose out-of-portfolio activity."""
#     work = _tx_frame(transactions)
#     if work.is_empty():
#         return pd.DataFrame(columns=["Actor_MSISDN", "Nb_Jours", "FD_HVC", "HVC_Serve", "Nb_Trans_HVC", "FD_Others", "Other_Serve", "Nb_Trans_Other", "Hors_Portefeuille_POS", "FD_Hors_Portefeuille", "POS_Touches", "POS_Attribues", "Taux_Couverture", "Taux_Persistance"])
#     mapping = pl.read_database(
#         """
#         SELECT numero_du_point AS Actor_MSISDN,
#              numero_pos AS Portfolio_POS_Key,
#              1 AS Portfolio_Marker
#         FROM pos_point_relais
#         WHERE numero_pos IS NOT NULL AND TRIM(numero_pos) <> '0'
#         """,
#         conn,
#     ).with_columns([
#         pl.col("Actor_MSISDN").cast(pl.Utf8, strict=False).str.strip_chars(),
#         pl.col("Portfolio_POS_Key").cast(pl.Utf8, strict=False).str.strip_chars(),
#         pl.col("Portfolio_Marker").cast(pl.Int8),
#     ]).unique()
#     work = work.join(
#         mapping,
#         left_on=["Actor_MSISDN", "To_clean"],
#         right_on=["Actor_MSISDN", "Portfolio_POS_Key"],
#         how="left",
#     ).with_columns(
#         pl.col("Portfolio_Marker").is_not_null().alias("In_Portefeuille")
#     )
#     portfolio = work.filter(pl.col("In_Portefeuille"))
#     outside = work.filter(~pl.col("In_Portefeuille"))
#     metrics = compute_hvc_others_polars(portfolio.to_pandas(), hvc_set, others_set, split=True)
#     result = pl.from_pandas(metrics) if not metrics.empty else pl.DataFrame({"Actor_MSISDN": work.get_column("Actor_MSISDN").unique()})
#     outside_metrics = outside.group_by("Actor_MSISDN").agg([
#         pl.col("To_clean").n_unique().alias("Hors_Portefeuille_POS"),
#         pl.col("Amount").sum().round(0).cast(pl.Int64).alias("FD_Hors_Portefeuille"),
#     ])
#     result = result.join(outside_metrics, on="Actor_MSISDN", how="left").fill_null(0)
#     assigned = mapping.group_by("Actor_MSISDN").agg(pl.col("Portfolio_POS_Key").n_unique().alias("POS_Attribues"))
#     touched = portfolio.group_by("Actor_MSISDN").agg(pl.col("To_clean").n_unique().alias("POS_Touches"))
#     result = result.join(touched, on="Actor_MSISDN", how="left").join(assigned, on="Actor_MSISDN", how="left").fill_null(0)
#     period_days = max((pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1, 1) if start_date and end_date else 1
#     if period_days > 1:
#         persistence = portfolio.group_by(["Actor_MSISDN", "To_clean"]).agg(pl.col("Date_only").n_unique().alias("active_days")).with_columns(
#             (pl.col("active_days") / period_days * 100).alias("rate")
#         ).group_by("Actor_MSISDN").agg(pl.col("rate").mean().round(1).alias("Taux_Persistance"))
#     else:
#         persistence = pl.DataFrame({"Actor_MSISDN": result.get_column("Actor_MSISDN"), "Taux_Persistance": [None] * result.height})
#     return result.join(persistence, on="Actor_MSISDN", how="left").with_columns([
#         (pl.col("FD_HVC") + pl.col("FD_Others") + pl.col("FD_Hors_Portefeuille")).round(0).cast(pl.Int64).alias("Sigma_FD"),
#         (pl.col("HVC_Serve") + pl.col("Other_Serve") + pl.col("Hors_Portefeuille_POS")).cast(pl.Int64).alias("Sigma_POS_Serve"),
#         pl.when(pl.col("POS_Attribues") > 0).then((pl.col("POS_Touches") / pl.col("POS_Attribues") * 100).round(1)).otherwise(None).alias("Taux_Couverture"),
#     ]).to_pandas()

def compute_pr_portfolio_metrics(
    transactions: pd.DataFrame,
    hvc_set: set[str],
    others_set: set[str],
    start_date: Optional[str],
    end_date: Optional[str],
    conn: sqlite3.Connection,
) -> pd.DataFrame:
    """Compute PR KPIs from the assigned portfolio and expose out-of-portfolio activity."""
    work = _tx_frame(transactions)

    # 1. Gestion DataFrame vide
    empty_cols = [
        "Actor_MSISDN",
        "Nb_Jours",
        "FD_HVC",
        "HVC_Serve",
        "Nb_Trans_HVC",
        "FD_Others",
        "Other_Serve",
        "Nb_Trans_Other",
        "Hors_Portefeuille_POS",
        "FD_Hors_Portefeuille",
        "POS_Touches",
        "POS_Attribues",
        "Taux_Couverture",
        "Taux_Persistance",
        "Sigma_FD",
        "Sigma_POS_Serve",
    ]
    if work.is_empty():
        return pd.DataFrame(columns=empty_cols)

    # Cast systématique de la clé de jointure dans work
    work = work.with_columns(
        [
            pl.col("Actor_MSISDN").cast(pl.Utf8, strict=False).str.strip_chars(),
            pl.col("To_clean").cast(pl.Utf8, strict=False).str.strip_chars(),
        ]
    )

    # 2. Lecture du mapping SQL
    mapping = (
        pl.read_database(
            """
        SELECT numero_du_point AS Actor_MSISDN,
               numero_pos AS Portfolio_POS_Key,
               1 AS Portfolio_Marker
        FROM pos_point_relais
        WHERE numero_pos IS NOT NULL AND TRIM(numero_pos) <> '0'
        """,
            conn,
        )
        .pipe(_clean_key_columns, ["Actor_MSISDN", "Portfolio_POS_Key"])
        .with_columns(pl.col("Portfolio_Marker").cast(pl.Int8))
        .unique()
    )

    # 3. Jointure pour identifier le Portefeuille vs Hors-Portefeuille
    types = pl.read_database(
        "SELECT msisdn_pr AS Actor_MSISDN, type_point FROM point_relay_referentiel", conn
    ).pipe(_clean_key_columns, ["Actor_MSISDN"])

    work = work.join(
        mapping,
        left_on=["Actor_MSISDN", "To_clean"],
        right_on=["Actor_MSISDN", "Portfolio_POS_Key"],
        how="left",
    ).join(
        types, on="Actor_MSISDN", how="left"
    ).with_columns(
        (pl.col("Portfolio_Marker").is_not_null() | (pl.col("type_point") == "Caisses")).alias("In_Portefeuille")
    )

    portfolio = work.filter(pl.col("In_Portefeuille"))
    outside = work.filter(~pl.col("In_Portefeuille"))

    # 4. Calcul des métriques HVC / Others
    # Pour un PR, tous les POS du portefeuille qui ne sont pas HVC sont
    # comptabilises en Others, comme dans le calcul CDS. La source de verite
    # reste la liste des POS effectivement touches dans le portefeuille.
    metrics = compute_hvc_others_polars(
        portfolio.to_pandas(), hvc_set, set(), split=False
    )

    if not metrics.empty:
        result = pl.from_pandas(metrics).with_columns(
            pl.col("Actor_MSISDN").cast(pl.Utf8).str.strip_chars()
        )
    else:
        result = pl.DataFrame(
            {
                "Actor_MSISDN": work.get_column("Actor_MSISDN")
                .unique()
                .cast(pl.Utf8)
                .str.strip_chars()
            }
        )

    # 5. Métriques Hors-Portefeuille
    outside_metrics = outside.group_by("Actor_MSISDN").agg(
        [
            pl.col("To_clean").n_unique().alias("Hors_Portefeuille_POS"),
            pl.col("Amount").sum().round(0).cast(pl.Int64).alias("FD_Hors_Portefeuille"),
        ]
    )
    result = result.join(outside_metrics, on="Actor_MSISDN", how="left")

    # 6. Attribués & Touchés
    assigned = mapping.group_by("Actor_MSISDN").agg(
        pl.col("Portfolio_POS_Key").n_unique().alias("POS_Attribues")
    )
    touched = portfolio.group_by("Actor_MSISDN").agg(
        pl.col("To_clean").n_unique().alias("POS_Touches")
    )

    result = result.join(touched, on="Actor_MSISDN", how="left").join(
        assigned, on="Actor_MSISDN", how="left"
    )

    # 7. Calcul Taux de Persistance
    period_days = (
        max((pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1, 1)
        if start_date and end_date
        else 1
    )

    if period_days > 1 and not portfolio.is_empty():
        persistence = (
            portfolio.group_by(["Actor_MSISDN", "To_clean"])
            .agg(pl.col("Date_only").n_unique().alias("active_days"))
            .with_columns((pl.col("active_days") / period_days * 100).alias("rate"))
            .group_by("Actor_MSISDN")
            .agg(pl.col("rate").mean().round(1).alias("Taux_Persistance"))
        )
    else:
        persistence = pl.DataFrame(
            {
                "Actor_MSISDN": result.get_column("Actor_MSISDN"),
                "Taux_Persistance": pl.Series([None] * result.height, dtype=pl.Float64),
            }
        )

    result = result.join(persistence, on="Actor_MSISDN", how="left")

    required_cols = {
        "FD_HVC": pl.Float64,
        "FD_Others": pl.Float64,
        "FD_Hors_Portefeuille": pl.Int64,
        "HVC_Serve": pl.Int64,
        "Other_Serve": pl.Int64,
        "Hors_Portefeuille_POS": pl.Int64,
        "POS_Attribues": pl.Int64,
        "POS_Touches": pl.Int64,
        "Taux_Persistance": pl.Float64,
    }

    missing_exprs = [
        pl.lit(0).cast(dtype).alias(col)
        for col, dtype in required_cols.items()
        if col not in result.columns
    ]

    if missing_exprs:
        result = result.with_columns(missing_exprs)

    return (
        result.with_columns(
            [
                (
                    pl.col("FD_HVC").fill_null(0)
                    + pl.col("FD_Others").fill_null(0)
                    + pl.col("FD_Hors_Portefeuille").fill_null(0)
                )
                .round(0)
                .cast(pl.Int64)
                .alias("Sigma_FD"),
                (
                    pl.col("HVC_Serve").fill_null(0)
                    + pl.col("Other_Serve").fill_null(0)
                    + pl.col("Hors_Portefeuille_POS").fill_null(0)
                )
                .cast(pl.Int64)
                .alias("Sigma_POS_Serve"),
                pl.when(pl.col("POS_Attribues").fill_null(0) > 0)
                .then(
                    (
                        pl.col("POS_Touches").fill_null(0)
                        / pl.col("POS_Attribues")
                        * 100
                    ).round(1)
                )
                .otherwise(None)
                .alias("Taux_Couverture"),
            ]
        )
        .fill_null(0)
        .to_pandas()
    )

def compute_daily_activity_polars(transactions: pd.DataFrame, is_single_day: bool) -> pd.DataFrame:
    work = _tx_frame(transactions)
    if work.is_empty():
        return pd.DataFrame(columns=["Actor_MSISDN", "Nb_Transactions", "Premiere_Trans_min", "Derniere_Trans_min", "Nb_Jours_Actifs"])
    work = work.with_columns((pl.col("Hour") * 60 + pl.col("Date").dt.minute()).alias("Minutes"))
    per_day = work.group_by(["Actor_MSISDN", "Date_only"]).agg([
        pl.len().alias("Nb"), pl.col("Minutes").min().alias("Premiere"), pl.col("Minutes").max().alias("Derniere")
    ])
    return per_day.group_by("Actor_MSISDN").agg([
        pl.col("Nb").sum().alias("Nb_Transactions"),
        (pl.col("Premiere").min() if is_single_day else pl.col("Premiere").mean()).alias("Premiere_Trans_min"),
        (pl.col("Derniere").max() if is_single_day else pl.col("Derniere").mean()).alias("Derniere_Trans_min"),
        pl.col("Date_only").n_unique().alias("Nb_Jours_Actifs"),
    ]).to_pandas()


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "Actor_MSISDN",
            "POS_Touches",
            "POS_Attribues",
            "Taux_Couverture",
            "Taux_Persistance",
        ]
    )


def _assigned_pos(segment: str, conn: sqlite3.Connection) -> pl.DataFrame:
    if segment == "pr_caisse":
        mapping = _clean_key_columns(
            pl.read_database(
                "SELECT numero_du_point AS Actor_MSISDN, numero_pos AS POS FROM pos_point_relais",
                conn,
            ),
            ["Actor_MSISDN", "POS"],
        ).filter(pl.col("POS").is_not_null() & (pl.col("POS") != "0"))
        return mapping.group_by("Actor_MSISDN").agg(
            pl.col("POS").n_unique().alias("POS_Attribues")
        )
    if segment == "commercial":
        mapping = _clean_key_columns(
            pl.read_database(
                "SELECT ccial_msisdn AS Actor_MSISDN, hvc_msisdn AS POS FROM hvc_commercial_mapping WHERE ccial_msisdn IS NOT NULL",
                conn,
            ),
            ["Actor_MSISDN", "POS"],
        )
        return mapping.group_by("Actor_MSISDN").agg(pl.col("POS").n_unique().alias("POS_Attribues"))
    mapping = _clean_key_columns(
        pl.read_database(
            """
            SELECT c.cds_msisdn AS Actor_MSISDN, a.hvc_msisdn AS POS
            FROM hvc_cds_assignments a
            INNER JOIN cds_referentiel c ON c.nom_cds = a.cds_nom
            """,
            conn,
        ),
        ["Actor_MSISDN", "POS"],
    )
    return mapping.group_by("Actor_MSISDN").agg(pl.col("POS").n_unique().alias("POS_Attribues"))


def compute_coverage_persistence(
    transactions: pd.DataFrame,
    segment: str,
    start_date: Optional[str],
    end_date: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """Return coverage and multi-day persistence for every active actor.

    Persistence is the mean ratio of active days per touched POS over the
    selected period. It is intentionally null for a one-day period.
    """
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        assigned = _assigned_pos(segment, connection)
        if transactions.empty:
            return assigned.to_pandas() if not assigned.is_empty() else _empty()

        required = {"Actor_MSISDN", "To_clean", "Date_only"}
        if not required.issubset(transactions.columns):
            return _empty()

        work = pl.from_pandas(transactions[list(required)], include_index=False).with_columns([
            pl.col("Actor_MSISDN").cast(pl.Utf8, strict=False),
            pl.col("To_clean").cast(pl.Utf8, strict=False),
            pl.col("Date_only").cast(pl.Utf8, strict=False),
        ]).drop_nulls(["Actor_MSISDN", "To_clean", "Date_only"]).unique()
        touched = work.group_by("Actor_MSISDN").agg(
            pl.col("To_clean").n_unique().alias("POS_Touches")
        )

        period_days = 1
        if start_date and end_date:
            period_days = max((pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1, 1)

        if period_days > 1:
            persistence = (
                work.group_by(["Actor_MSISDN", "To_clean"])
                .agg(pl.col("Date_only").n_unique().alias("active_days"))
                .with_columns((pl.col("active_days") / period_days * 100).alias("pos_persistence"))
                .group_by("Actor_MSISDN")
                .agg(pl.col("pos_persistence").mean().round(1).alias("Taux_Persistance"))
            )
        else:
            persistence = touched.select([
                "Actor_MSISDN",
                pl.lit(None, dtype=pl.Float64).alias("Taux_Persistance"),
            ])

        result = touched.join(persistence, on="Actor_MSISDN", how="left")
        if not assigned.is_empty():
            result = result.join(assigned, on="Actor_MSISDN", how="left")
        else:
            result = result.with_columns(pl.lit(0).alias("POS_Attribues"))

        return result.with_columns(
            pl.when(pl.col("POS_Attribues") > 0)
            .then((pl.col("POS_Touches") / pl.col("POS_Attribues") * 100).round(1))
            .otherwise(None)
            .alias("Taux_Couverture")
        ).to_pandas()
    finally:
        if own_connection:
            connection.close()