# utils/network.py
"""
Module de détection réseau et de statut de connectivité pour le mode Local-First.
Conçu pour être non-bloquant (< 1.0s timeout) et avec mise en cache TTL pour
ne jamais ralentir les interactions de l'interface Streamlit.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from utils.config_storage import LOCAL_STORAGE_PATH, USE_LOCAL_STORAGE, get_config_value

logger = logging.getLogger(__name__)

# Cache en mémoire de l'état réseau (durée de validité en secondes)
_NETWORK_CACHE_TTL_SECONDS = 20.0
_cached_status: dict[str, Any] | None = None
_cached_time: float = 0.0

DEFAULT_PROBE_HOSTS = [
    ("1.1.1.1", 53),   # Cloudflare DNS
    ("8.8.8.8", 53),   # Google DNS
]


def check_internet_connectivity(timeout: float = 1.0) -> tuple[bool, float | None]:
    """
    Vérifie rapidement si une connexion Internet est active en ouvrant un socket TCP.
    Retourne (est_connecté, latence_en_ms).
    """
    for host, port in DEFAULT_PROBE_HOSTS:
        t0 = time.perf_counter()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            return True, latency_ms
        except (socket.timeout, OSError):
            continue
        except Exception as exc:
            logger.debug("Test socket vers %s:%s échoué : %s", host, port, exc)
            continue
    return False, None


def check_remote_host_reachability(timeout: float = 1.5) -> tuple[bool, str | None]:
    """
    Vérifie si l'hôte distant Turso / LibSQL est joignable si configuré.
    """
    url = get_config_value("LIBSQL_URL") or get_config_value("TURSO_DATABASE_URL")
    if not url:
        return False, "Aucun serveur distant configuré"

    try:
        # Extraire le hostname
        if "://" in url:
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme in ("https", "libsql", "wss") else 80)
        else:
            host = url.split(":")[0]
            port = 443

        if not host:
            return False, "Hôte distant invalide"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        return True, host
    except (socket.timeout, OSError) as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def get_network_status(force_refresh: bool = False) -> dict[str, Any]:
    """
    Retourne le statut réseau et l'état local-first avec cache TTL.
    """
    global _cached_status, _cached_time

    now = time.time()
    if not force_refresh and _cached_status is not None and (now - _cached_time < _NETWORK_CACHE_TTL_SECONDS):
        return _cached_status

    is_online, latency = check_internet_connectivity(timeout=1.0)
    remote_url = get_config_value("LIBSQL_URL") or get_config_value("TURSO_DATABASE_URL")
    remote_configured = bool(remote_url)
    remote_reachable = False
    remote_host = None

    if remote_configured and is_online:
        remote_reachable, remote_host = check_remote_host_reachability(timeout=1.0)

    db_path = LOCAL_STORAGE_PATH / "dashboard.db"
    db_size_mb = 0.0
    db_exists = db_path.exists()
    if db_exists:
        try:
            db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
        except OSError:
            db_size_mb = 0.0

    status = {
        "online": is_online,
        "status_label": "En ligne" if is_online else "Hors ligne (Mode Local)",
        "status_color": "green" if is_online else "red",
        "latency_ms": latency,
        "is_local_first": True,
        "use_local_storage": USE_LOCAL_STORAGE,
        "remote_configured": remote_configured,
        "remote_reachable": remote_reachable,
        "remote_host": remote_host,
        "db_path": str(db_path),
        "db_exists": db_exists,
        "db_size_mb": db_size_mb,
        "checked_at": datetime.now().strftime("%H:%M:%S"),
    }

    _cached_status = status
    _cached_time = now
    return status


def is_online(force_refresh: bool = False) -> bool:
    """Raccourci booléen rapide pour vérifier si l'appareil est connecté à Internet."""
    return get_network_status(force_refresh=force_refresh)["online"]


def refresh_network_cache() -> dict[str, Any]:
    """Force le rafraîchissement immédiat du cache réseau et retourne l'état."""
    return get_network_status(force_refresh=True)


def check_local_db_integrity() -> tuple[bool, str]:
    """Exécute un PRAGMA quick_check sur la base SQLite locale pour s'assurer de sa cohérence."""
    db_path = LOCAL_STORAGE_PATH / "dashboard.db"
    if not db_path.exists():
        return False, "Base de données locale introuvable."
    try:
        import sqlite3
        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
            cursor = conn.cursor()
            res = cursor.execute("PRAGMA quick_check;").fetchone()
            if res and res[0].lower() == "ok":
                return True, "Base locale saine (PRAGMA quick_check: OK)"
            return False, f"Avertissement intégrité: {res[0] if res else 'inconnu'}"
    except Exception as exc:
        return False, f"Erreur vérification intégrité: {exc}"

