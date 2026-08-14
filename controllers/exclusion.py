"""Authoritative internal-account exclusion rule.

Task 2 decision
---------------
The canonical exclusion universe is the SQLite ``exclusions_reference`` table,
populated during ingestion from the same sources previously read inline by the
pages: commerciaux, caisses, masters, CDS and POS relay-caisse.

The legacy helper in ``domain/reference.py`` was the closest correct version
because it was the only one that already modelled all five categories. The new
controller entry point keeps that business rule but reads from SQLite through
``models.reference_model`` so every controller can share one source of truth.
"""

from __future__ import annotations

import sqlite3
from typing import FrozenSet, Iterable, Optional

from models.reference_model import get_global_excluded_numbers as _read_exclusions
from utils.helpers import clean_phone

BASE_EXCLUSION_CATEGORIES: tuple[str, ...] = (
    "commercial",
    "caisse",
    "master",
    "cds",
)
POS_RELAY_CAISSE_CATEGORY = "pos_relay_caisse"


def exclusion_categories(include_pos_relay_caisse: bool = True) -> tuple[str, ...]:
    """Return the internal-account categories used for POS-served metrics."""
    if include_pos_relay_caisse:
        return (*BASE_EXCLUSION_CATEGORIES, POS_RELAY_CAISSE_CATEGORY)
    return BASE_EXCLUSION_CATEGORIES


def normalize_msisdn_set(values: Iterable[object]) -> FrozenSet[str]:
    """Normalize and freeze a collection of MSISDN-like values."""
    cleaned = {
        clean_phone(value)
        for value in values
        if value is not None and str(value).strip() != ""
    }
    return frozenset(number for number in cleaned if number and number != "None")


def get_excluded_msisdns(
    conn: Optional[sqlite3.Connection] = None,
    include_pos_relay_caisse: bool = True,
) -> FrozenSet[str]:
    """Return internal MSISDNs excluded from every POS-served computation.

    ``include_pos_relay_caisse`` defaults to ``True`` because relay-caisse POS
    behave as internal cash-desk actors in the existing reporting logic.
    """
    categories = list(exclusion_categories(include_pos_relay_caisse))
    return normalize_msisdn_set(_read_exclusions(conn=conn, categories=categories))


def is_excluded_msisdn(
    msisdn: object,
    excluded_msisdns: Iterable[str],
) -> bool:
    """Check one MSISDN against an already-loaded exclusion set."""
    cleaned = clean_phone(msisdn)
    return bool(cleaned and cleaned in set(excluded_msisdns))
