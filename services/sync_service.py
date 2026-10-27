# services/sync_service.py
"""
Service de synchronisation pour l'architecture Local-First de l'application de bureau native.
Permet d'orchestrer la synchronisation bidirectionnelle et résiliente des données :
- Fichiers de configuration & paramétrages (stored_files)
- Comptes utilisateurs et rôles RBAC (users)
- Référentiels métiers (sites, POS, commerciaux, exclusions, CDS, etc.)
- Invalidation dynamique des caches Streamlit
- Historisation complète et horodatage des opérations
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from utils.config_storage import LOCAL_STORAGE_PATH, SETTINGS_PATH
from utils.network import get_network_status

logger = logging.getLogger(__name__)

SYNC_METADATA_FILE = SETTINGS_PATH / "sync_metadata.json"


def _ensure_settings_dir() -> None:
    SETTINGS_PATH.mkdir(parents=True, exist_ok=True)


def get_last_sync_info() -> dict[str, Any]:
    """Retourne les métadonnées de la dernière synchronisation."""
    _ensure_settings_dir()
    if not SYNC_METADATA_FILE.exists():
        return {
            "last_sync_time": None,
            "formatted_time": "Jamais synchronisé",
            "status": "NEVER",
            "message": "Aucune synchronisation n'a encore été effectuée.",
            "details": {},
        }

    try:
        with open(SYNC_METADATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            iso_time = data.get("last_sync_time")
            if iso_time:
                try:
                    dt = datetime.fromisoformat(iso_time)
                    data["formatted_time"] = dt.strftime("%d/%m/%Y à %H:%M:%S")
                except ValueError:
                    data["formatted_time"] = iso_time
            else:
                data["formatted_time"] = "Inconnue"
            return data
    except Exception as exc:
        logger.warning("Erreur lecture sync_metadata.json: %s", exc)
        return {
            "last_sync_time": None,
            "formatted_time": "Inconnue",
            "status": "ERROR",
            "message": f"Erreur lecture métadonnées: {exc}",
            "details": {},
        }


def record_sync_event(
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Enregistre le résultat d'une synchronisation."""
    _ensure_settings_dir()
    record = {
        "last_sync_time": datetime.now().isoformat(),
        "status": status,
        "message": message,
        "details": details or {},
    }
    try:
        with open(SYNC_METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.error("Impossible d'enregistrer sync_metadata.json: %s", exc)


def _parse_timestamp(ts: Any) -> float:
    """Convertit n'importe quel format de timestamp en float pour comparaison fiable."""
    if not ts:
        return 0.0
    ts_str = str(ts).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(ts_str.replace("Z", ""), fmt).timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "")).timestamp()
    except Exception:
        return 0.0


def _sync_stored_files(remote_conn, local_conn: sqlite3.Connection) -> dict[str, int]:
    """
    Synchronise les fichiers stockés (stored_files) entre Turso et SQLite local.
    RÈGLE SETTINGS : Les fichiers de configuration (settings) sont remplacés par la
    version la plus récente (Last-Write-Wins). Si local est plus récent que Turso,
    Turso est mis à jour et les anciens fichiers du dossier sont supprimés.
    Si Turso est plus récent, le local est mis à jour.
    """
    from utils.turso_storage import _ensure_table
    _ensure_table(local_conn)
    _ensure_table(remote_conn)

    pulled_count = 0
    pushed_count = 0

    # 1. Charger les fichiers locaux de configuration (settings)
    local_rows = local_conn.execute(
        "SELECT bucket, file_path, file_name, content, content_type, content_hash, created_at FROM stored_files WHERE bucket = 'settings'"
    ).fetchall()
    local_files = {(r[0], r[1]): {
        "bucket": r[0], "file_path": r[1], "file_name": r[2],
        "content": r[3], "content_type": r[4], "content_hash": r[5],
        "created_at": str(r[6] or "")
    } for r in local_rows}

    # 2. Charger les fichiers distants de configuration (Turso)
    try:
        remote_cursor = remote_conn.cursor()
        remote_rows = remote_cursor.execute(
            "SELECT bucket, file_path, file_name, content, content_type, content_hash, created_at FROM stored_files WHERE bucket = 'settings'"
        ).fetchall()
        remote_files = {(r[0], r[1]): {
            "bucket": r[0], "file_path": r[1], "file_name": r[2],
            "content": r[3], "content_type": r[4], "content_hash": r[5],
            "created_at": str(r[6] or "")
        } for r in remote_rows}
    except Exception as exc:
        logger.warning("Impossible de lire stored_files sur Turso distant: %s", exc)
        remote_files = {}

    # 3. Synchronisation par dossier pour le bucket 'settings'
    all_keys = set(local_files.keys()).union(set(remote_files.keys()))
    settings_folders = {k[1].split("/")[0] for k in all_keys if k[0] == "settings" and "/" in k[1]}

    for folder in settings_folders:
        prefix = f"{folder}/"
        folder_locals = {k[1]: v for k, v in local_files.items() if k[0] == "settings" and k[1].startswith(prefix)}
        folder_remotes = {k[1]: v for k, v in remote_files.items() if k[0] == "settings" and k[1].startswith(prefix)}

        best_local = max(folder_locals.values(), key=lambda x: _parse_timestamp(x["created_at"])) if folder_locals else None
        best_remote = max(folder_remotes.values(), key=lambda x: _parse_timestamp(x["created_at"])) if folder_remotes else None

        t_local = _parse_timestamp(best_local["created_at"]) if best_local else 0.0
        t_remote = _parse_timestamp(best_remote["created_at"]) if best_remote else 0.0

        if best_local and (not best_remote or t_local >= t_remote):
            # Local est plus récent ou égal -> PUSH vers Turso (remplace l'ancien fichier de settings dans Turso)
            # Nettoyer les anciens fichiers obsolètes locaux de ce dossier s'il y en a plusieurs
            if len(folder_locals) > 1:
                local_conn.execute(
                    "DELETE FROM stored_files WHERE bucket = 'settings' AND file_path LIKE ? AND file_path != ?",
                    (f"{prefix}%", best_local["file_path"]),
                )
                local_conn.commit()

            folder_disk = LOCAL_STORAGE_PATH / "settings" / folder
            if folder_disk.exists() and folder_disk.is_dir():
                for f in folder_disk.glob("*"):
                    if f.is_file() and f.name != best_local["file_name"]:
                        try:
                            f.unlink()
                        except Exception:
                            pass

            # Remplacement dans Turso si hash différent, ou absent, ou anciens fichiers présents
            if not best_remote or best_local["content_hash"] != best_remote["content_hash"] or len(folder_remotes) > 1:
                try:
                    remote_conn.execute("DELETE FROM stored_files WHERE bucket = 'settings' AND file_path LIKE ?", (f"{prefix}%",))
                    remote_conn.execute(
                        """
                        INSERT INTO stored_files (bucket, file_path, file_name, content, content_type, content_hash, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("settings", best_local["file_path"], best_local["file_name"], bytes(best_local["content"]), best_local["content_type"], best_local["content_hash"], best_local["created_at"])
                    )
                    remote_conn.commit()
                    pushed_count += 1
                    logger.info("PUSH settings vers Turso : %s (remplacement par le plus récent)", best_local["file_path"])
                except Exception as p_err:
                    logger.warning("Échec push setting %s: %s", best_local["file_path"], p_err)
        elif best_remote:
            # Distant est plus récent que local -> PULL vers local (remplace l'ancien paramétrage local)
            # Nettoyer les doublons distants éventuels sur Turso
            if len(folder_remotes) > 1:
                try:
                    remote_conn.execute(
                        "DELETE FROM stored_files WHERE bucket = 'settings' AND file_path LIKE ? AND file_path != ?",
                        (f"{prefix}%", best_remote["file_path"]),
                    )
                    remote_conn.commit()
                except Exception:
                    pass

            if not best_local or best_remote["content_hash"] != best_local["content_hash"] or len(folder_locals) > 1:
                try:
                    local_conn.execute("DELETE FROM stored_files WHERE bucket = 'settings' AND file_path LIKE ?", (f"{prefix}%",))
                    local_conn.execute(
                        """
                        INSERT INTO stored_files (bucket, file_path, file_name, content, content_type, content_hash, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("settings", best_remote["file_path"], best_remote["file_name"], bytes(best_remote["content"]), best_remote["content_type"], best_remote["content_hash"], best_remote["created_at"])
                    )
                    local_conn.commit()
                    # Nettoyer et réécrire sur disque
                    folder_disk = LOCAL_STORAGE_PATH / "settings" / folder
                    if folder_disk.exists() and folder_disk.is_dir():
                        for f in folder_disk.glob("*"):
                            if f.is_file():
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                    dest_file = LOCAL_STORAGE_PATH / "settings" / best_remote["file_path"]
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    dest_file.write_bytes(bytes(best_remote["content"]))
                    pulled_count += 1
                    logger.info("PULL settings depuis Turso : %s (remplacement par le plus récent)", best_remote["file_path"])
                except Exception as pull_err:
                    logger.warning("Échec pull setting %s: %s", best_remote["file_path"], pull_err)

    local_conn.commit()
    try:
        remote_conn.commit()
    except Exception:
        pass

    return {"pulled": pulled_count, "pushed": pushed_count}


def _sync_sites(remote_conn, local_conn: sqlite3.Connection, progress_callback: Callable[[str], None] | None = None) -> int:
    """Synchronise la table canonique des sites vers Turso pour satisfaire les contraintes de clés étrangères (FK)."""
    synced = 0
    try:
        if progress_callback:
            progress_callback("Vérification des sites référentiels sur Turso...")
        raw_rows = local_conn.execute(
            "SELECT site_key, sitename, zone_new, territory_correct, isl_terr, quartier, dsm_name FROM sites"
        ).fetchall()
        rows = [tuple(r) for r in raw_rows]
        if rows:
            batch_size = 300
            for b_idx in range(0, len(rows), batch_size):
                chunk = rows[b_idx : b_idx + batch_size]
                remote_conn.cursor().executemany(
                    """
                    INSERT OR IGNORE INTO sites (site_key, sitename, zone_new, territory_correct, isl_terr, quartier, dsm_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    chunk,
                )
                remote_conn.commit()
            synced = len(rows)
    except Exception as exc:
        logger.warning("Synchronisation de la table sites vers Turso ignorée: %s", exc)
    return synced


def _sync_referentiels_to_turso(
    remote_conn,
    local_conn: sqlite3.Connection,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Synchronise l'ensemble des référentiels (sites, commerciaux, POS) vers Turso."""
    counts = {"sites": 0, "commerciaux": 0, "referentiel_pos": 0}
    try:
        # 1. Sites canoniques
        counts["sites"] = _sync_sites(remote_conn, local_conn, progress_callback)

        # 2. Commerciaux
        if progress_callback:
            progress_callback("Vérification des commerciaux sur Turso...")
        raw_comm = local_conn.execute(
            "SELECT ccial_msisdn, nom_ccial, zone_centre, zone_territoire, zone_sa FROM referentiel_commerciaux"
        ).fetchall()
        if raw_comm:
            remote_conn.cursor().executemany(
                """
                INSERT INTO referentiel_commerciaux (ccial_msisdn, nom_ccial, zone_centre, zone_territoire, zone_sa)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ccial_msisdn) DO UPDATE SET
                    nom_ccial=excluded.nom_ccial,
                    zone_centre=excluded.zone_centre,
                    zone_territoire=excluded.zone_territoire,
                    zone_sa=excluded.zone_sa
                """,
                [tuple(r) for r in raw_comm],
            )
            remote_conn.commit()
            counts["commerciaux"] = len(raw_comm)

        # 3. Référentiel POS
        if progress_callback:
            progress_callback("Vérification du référentiel POS sur Turso...")
        raw_pos = local_conn.execute(
            """
            SELECT agent_msisdn, source_master, full_name, zone_centre, zone_territoire,
                   zone_sa, secteur_cluster, site_key, segment_group, day_target, oos_target
            FROM referentiel_pos
            """
        ).fetchall()
        if raw_pos:
            batch_size = 500
            for b_idx in range(0, len(raw_pos), batch_size):
                chunk = [tuple(r) for r in raw_pos[b_idx : b_idx + batch_size]]
                remote_conn.cursor().executemany(
                    """
                    INSERT INTO referentiel_pos (
                        agent_msisdn, source_master, full_name, zone_centre, zone_territoire,
                        zone_sa, secteur_cluster, site_key, segment_group, day_target, oos_target
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_msisdn, source_master) DO UPDATE SET
                        full_name=excluded.full_name,
                        zone_centre=excluded.zone_centre,
                        zone_territoire=excluded.zone_territoire,
                        zone_sa=excluded.zone_sa,
                        secteur_cluster=excluded.secteur_cluster,
                        site_key=excluded.site_key,
                        segment_group=excluded.segment_group,
                        day_target=excluded.day_target,
                        oos_target=excluded.oos_target
                    """,
                    chunk,
                )
            remote_conn.commit()
            counts["referentiel_pos"] = len(raw_pos)

    except Exception as exc:
        logger.warning("Synchronisation des référentiels vers Turso partielle: %s", exc)

    return counts


def _sync_listing_oos(remote_conn, local_conn: sqlite3.Connection, progress_callback: Callable[[str], None] | None = None, max_dates_per_run: int = 20) -> dict[str, int]:
    """
    Ajoute les enregistrements listing_oos dans Turso et dans SQLite sans jamais remplacer
    les données existantes (INSERT OR IGNORE).
    Priorise les snapshots les plus récents.
    """
    pushed = 0
    pulled = 0
    try:
        local_dates = set(r[0] for r in local_conn.execute("SELECT DISTINCT snapshot_date FROM listing_oos").fetchall())
        remote_dates = set(r[0] for r in remote_conn.cursor().execute("SELECT DISTINCT snapshot_date FROM listing_oos").fetchall())

        dates_to_push = sorted(local_dates - remote_dates, reverse=True)
        dates_to_pull = sorted(remote_dates - local_dates, reverse=True)

        target_push = dates_to_push[:max_dates_per_run] if max_dates_per_run else dates_to_push
        total_push_dates = len(target_push)
        for i, s_date in enumerate(target_push):
            if progress_callback:
                progress_callback(f"OOS : Ajout vers Turso snapshot {s_date} ({i+1}/{total_push_dates})...")
            raw_rows = local_conn.execute(
                """
                SELECT msisdn, day_target, float_amount, oos_pct, is_oos, last_trx_time,
                       site_key, cluster, territory, zone, segment_group, snapshot_date
                FROM listing_oos WHERE snapshot_date = ?
                """,
                (s_date,),
            ).fetchall()
            rows = [tuple(r) for r in raw_rows]
            if rows:
                batch_size = 2000
                for b_idx in range(0, len(rows), batch_size):
                    chunk = rows[b_idx : b_idx + batch_size]
                    remote_conn.cursor().executemany(
                        """
                        INSERT OR IGNORE INTO listing_oos (
                            msisdn, day_target, float_amount, oos_pct, is_oos, last_trx_time,
                            site_key, cluster, territory, zone, segment_group, snapshot_date
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        chunk,
                    )
                remote_conn.commit()
                pushed += len(rows)

        target_pull = dates_to_pull[:max_dates_per_run] if max_dates_per_run else dates_to_pull
        total_pull_dates = len(target_pull)
        for i, s_date in enumerate(target_pull):
            if progress_callback:
                progress_callback(f"OOS : Rapatriement snapshot {s_date} ({i+1}/{total_pull_dates})...")
            raw_rows = remote_conn.cursor().execute(
                """
                SELECT msisdn, day_target, float_amount, oos_pct, is_oos, last_trx_time,
                       site_key, cluster, territory, zone, segment_group, snapshot_date
                FROM listing_oos WHERE snapshot_date = ?
                """,
                (s_date,),
            ).fetchall()
            rows = [tuple(r) for r in raw_rows]
            if rows:
                batch_size = 2000
                for b_idx in range(0, len(rows), batch_size):
                    chunk = rows[b_idx : b_idx + batch_size]
                    local_conn.executemany(
                        """
                        INSERT OR IGNORE INTO listing_oos (
                            msisdn, day_target, float_amount, oos_pct, is_oos, last_trx_time,
                            site_key, cluster, territory, zone, segment_group, snapshot_date
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        chunk,
                    )
                local_conn.commit()
                pulled += len(rows)

    except Exception as exc:
        logger.error("Erreur _sync_listing_oos: %s", exc)

    return {"pushed": pushed, "pulled": pulled}


def _sync_transactions(remote_conn, local_conn: sqlite3.Connection, progress_callback: Callable[[str], None] | None = None, max_dates_per_run: int = 15) -> dict[str, int]:
    """
    Ajoute les transactions dans Turso et dans SQLite sans jamais remplacer
    les données existantes (INSERT OR IGNORE).
    Synchronise en priorité les dates les plus récentes.
    """
    pushed = 0
    pulled = 0
    try:
        local_dates = set(r[0] for r in local_conn.execute("SELECT DISTINCT date_only FROM transactions").fetchall())
        remote_dates = set(r[0] for r in remote_conn.cursor().execute("SELECT DISTINCT date_only FROM transactions").fetchall())

        dates_to_push = sorted(local_dates - remote_dates, reverse=True)
        dates_to_pull = sorted(remote_dates - local_dates, reverse=True)

        target_push = dates_to_push[:max_dates_per_run] if max_dates_per_run else dates_to_push
        for i, d_str in enumerate(target_push):
            if progress_callback:
                progress_callback(f"Transactions : Envoi vers Turso {d_str} ({i+1}/{len(target_push)})...")
            raw_rows = local_conn.execute(
                """
                SELECT tx_date, date_only, hour, tx_type, amount,
                       from_msisdn, to_msisdn, from_name, to_name, balance,
                       zone_sa_snapshot, territoire_snapshot, site_key_snapshot, source_file, file_hash
                FROM transactions WHERE date_only = ?
                """,
                (d_str,),
            ).fetchall()
            rows = [tuple(r) for r in raw_rows]
            if rows:
                batch_size = 2000
                for b_idx in range(0, len(rows), batch_size):
                    chunk = rows[b_idx : b_idx + batch_size]
                    remote_conn.cursor().executemany(
                        """
                        INSERT OR IGNORE INTO transactions (
                            tx_date, date_only, hour, tx_type, amount,
                            from_msisdn, to_msisdn, from_name, to_name, balance,
                            zone_sa_snapshot, territoire_snapshot, site_key_snapshot, source_file, file_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        chunk,
                    )
                remote_conn.commit()
                pushed += len(rows)

        target_pull = dates_to_pull[:max_dates_per_run] if max_dates_per_run else dates_to_pull
        for i, d_str in enumerate(target_pull):
            if progress_callback:
                progress_callback(f"Transactions : Rapatriement {d_str} ({i+1}/{len(target_pull)})...")
            raw_rows = remote_conn.cursor().execute(
                """
                SELECT tx_date, date_only, hour, tx_type, amount,
                       from_msisdn, to_msisdn, from_name, to_name, balance,
                       zone_sa_snapshot, territoire_snapshot, site_key_snapshot, source_file, file_hash
                FROM transactions WHERE date_only = ?
                """,
                (d_str,),
            ).fetchall()
            rows = [tuple(r) for r in raw_rows]
            if rows:
                batch_size = 2000
                for b_idx in range(0, len(rows), batch_size):
                    chunk = rows[b_idx : b_idx + batch_size]
                    local_conn.executemany(
                        """
                        INSERT OR IGNORE INTO transactions (
                            tx_date, date_only, hour, tx_type, amount,
                            from_msisdn, to_msisdn, from_name, to_name, balance,
                            zone_sa_snapshot, territoire_snapshot, site_key_snapshot, source_file, file_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        chunk,
                    )
                local_conn.commit()
                pulled += len(rows)

    except Exception as exc:
        logger.error("Erreur _sync_transactions: %s", exc)

    return {"pushed": pushed, "pulled": pulled}


def _sync_hvc_variations(remote_conn, local_conn: sqlite3.Connection, progress_callback: Callable[[str], None] | None = None, max_stamps_per_run: int = 30) -> dict[str, int]:
    """
    Ajoute les variations HVC dans Turso et dans SQLite sans jamais remplacer
    les données existantes (INSERT OR IGNORE).
    """
    pushed = 0
    pulled = 0
    try:
        local_stamps = set(r[0] for r in local_conn.execute("SELECT DISTINCT snapshot_timestamp FROM hvc_variations").fetchall())
        remote_stamps = set(r[0] for r in remote_conn.cursor().execute("SELECT DISTINCT snapshot_timestamp FROM hvc_variations").fetchall())

        stamps_to_push = sorted(local_stamps - remote_stamps, reverse=True)[:max_stamps_per_run]
        stamps_to_pull = sorted(remote_stamps - local_stamps, reverse=True)[:max_stamps_per_run]

        for s_stamp in stamps_to_push:
            if progress_callback:
                progress_callback(f"HVC : Ajout vers Turso relevé {s_stamp}...")
            raw_rows = local_conn.execute(
                "SELECT site_key, day_hvc, oos_pct, snapshot_timestamp, source_file FROM hvc_variations WHERE snapshot_timestamp = ?",
                (s_stamp,),
            ).fetchall()
            rows = [tuple(r) for r in raw_rows]
            if rows:
                remote_conn.cursor().executemany(
                    "INSERT OR IGNORE INTO hvc_variations (site_key, day_hvc, oos_pct, snapshot_timestamp, source_file) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                remote_conn.commit()
                pushed += len(rows)

        for s_stamp in stamps_to_pull:
            if progress_callback:
                progress_callback(f"HVC : Rapatriement relevé {s_stamp}...")
            raw_rows = remote_conn.cursor().execute(
                "SELECT site_key, day_hvc, oos_pct, snapshot_timestamp, source_file FROM hvc_variations WHERE snapshot_timestamp = ?",
                (s_stamp,),
            ).fetchall()
            rows = [tuple(r) for r in raw_rows]
            if rows:
                local_conn.executemany(
                    "INSERT OR IGNORE INTO hvc_variations (site_key, day_hvc, oos_pct, snapshot_timestamp, source_file) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                local_conn.commit()
                pulled += len(rows)
    except Exception as exc:
        logger.warning("Erreur _sync_hvc_variations: %s", exc)

    return {"pushed": pushed, "pulled": pulled}


def _sync_users(remote_conn, local_conn: sqlite3.Connection) -> int:
    """
    Synchronise la table users depuis Turso vers SQLite local pour garantir
    que les comptes et permissions sont utilisables même 100% hors-ligne.
    """
    synced_users = 0
    try:
        remote_cursor = remote_conn.cursor()
        remote_users = remote_cursor.execute(
            "SELECT username, password_hash, role, created_at FROM users"
        ).fetchall()
        for u in remote_users:
            local_conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    role = excluded.role
                """,
                (u[0], u[1], u[2], u[3]),
            )
            synced_users += 1
        local_conn.commit()
    except Exception as exc:
        logger.warning("Synchronisation de la table users ignorée: %s", exc)

    return synced_users


def run_full_synchronization(
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """
    Exécute la synchronisation globale bidirectionnelle pour l'application en mode Local-First :
    1. Paramétrages (settings) : Remplacement dans Turso par la version la plus récente (Last-Write-Wins).
    2. Comptes utilisateurs (users).
    3. Ingestion des référentiels métiers dans SQLite.
    4. Listing OOS : Ajout dans Turso sans remplacement (INSERT OR IGNORE).
    5. Transactions : Ajout dans Turso sans remplacement (INSERT OR IGNORE).
    6. Rafraîchissement des caches Streamlit.
    """
    t_start = time.perf_counter()

    if progress_callback:
        progress_callback(5, "Vérification de la connectivité réseau...")

    net_status = get_network_status(force_refresh=True)
    if not net_status["online"]:
        msg = (
            "Mode Hors Ligne : Aucune connexion Internet disponible. "
            "Vos données locales SQLite restent 100% utilisables en toute autonomie."
        )
        record_sync_event(
            status="OFFLINE",
            message=msg,
            details={"network": net_status},
        )
        return {
            "success": False,
            "status": "OFFLINE",
            "message": msg,
            "details": {},
        }

    from models.db import get_connection, get_remote_connection

    remote_conn = get_remote_connection()
    if remote_conn is None:
        msg = (
            "Serveur distant inaccessible : la base distante Turso ne répond pas. "
            "L'application continue de fonctionner sur la base locale SQLite."
        )
        record_sync_event(
            status="UNREACHABLE",
            message=msg,
            details={"network": net_status},
        )
        return {
            "success": False,
            "status": "UNREACHABLE",
            "message": msg,
            "details": {},
        }

    details: dict[str, Any] = {}

    try:
        with get_connection() as local_conn:
            # Étape 1 : Paramétrages (Settings) - Règle du plus récent (Last-Write-Wins)
            if progress_callback:
                progress_callback(15, "Synchronisation des paramétrages (Settings)...")
            files_sync = _sync_stored_files(remote_conn, local_conn)
            details["stored_files"] = files_sync

            # Étape 2 : Utilisateurs
            if progress_callback:
                progress_callback(30, "Synchronisation des comptes utilisateurs...")
            users_count = _sync_users(remote_conn, local_conn)
            details["users_synced"] = users_count

            # Étape 3 : Ingestion des référentiels métiers vers SQLite local
            if progress_callback:
                progress_callback(45, "Ingestion des référentiels métiers locaux...")
            from ingestion.ingest import run_referentiel_ingestion
            referentiels_count = run_referentiel_ingestion(
                conn=local_conn,
                progress_callback=lambda pct, text: progress_callback(
                    45 + int(pct * 0.15), f"Référentiels : {text}"
                ) if progress_callback else None,
            )
            details["referentiels"] = referentiels_count

            # Étape 3.5 : Synchronisation des référentiels vers Turso (sites, commerciaux, POS)
            if progress_callback:
                progress_callback(60, "Synchronisation des référentiels vers Turso...")
            ref_turso_counts = _sync_referentiels_to_turso(
                remote_conn, local_conn,
                progress_callback=lambda msg: progress_callback(62, msg) if progress_callback else None
            )
            details["referentiels_turso"] = ref_turso_counts

            # Étape 4 : Listing OOS - Ajout sans remplacement
            if progress_callback:
                progress_callback(65, "Synchronisation incrémentale Listing OOS...")
            oos_sync = _sync_listing_oos(
                remote_conn, local_conn,
                progress_callback=lambda msg: progress_callback(70, msg) if progress_callback else None
            )
            details["listing_oos"] = oos_sync

            # Étape 5 : Transactions - Ajout sans remplacement
            if progress_callback:
                progress_callback(78, "Synchronisation incrémentale Transactions...")
            tx_sync = _sync_transactions(
                remote_conn, local_conn,
                progress_callback=lambda msg: progress_callback(82, msg) if progress_callback else None
            )
            details["transactions"] = tx_sync

            # Étape 6 : Variations HVC - Ajout sans remplacement
            if progress_callback:
                progress_callback(88, "Synchronisation incrémentale Variations HVC...")
            hvc_sync = _sync_hvc_variations(
                remote_conn, local_conn,
                progress_callback=lambda msg: progress_callback(90, msg) if progress_callback else None
            )
            details["hvc_variations"] = hvc_sync

        # Étape 7 : Invalidation des caches Streamlit
        if progress_callback:
            progress_callback(95, "Rafraîchissement des caches de visualisation...")
        try:
            import streamlit as st
            st.cache_data.clear()
        except Exception:
            pass

        try:
            from models.db import get_cached_connection
            get_cached_connection.clear()
        except Exception:
            pass

        duration = round(time.perf_counter() - t_start, 2)
        details["duration_seconds"] = duration

        summary_parts = []
        if files_sync.get("pushed", 0) > 0:
            summary_parts.append(f"{files_sync['pushed']} paramètre(s) mis à jour sur Turso")
        if files_sync.get("pulled", 0) > 0:
            summary_parts.append(f"{files_sync['pulled']} paramètre(s) rapatriés")
        if oos_sync.get("pushed", 0) > 0:
            summary_parts.append(f"{oos_sync['pushed']} OOS ajoutés à Turso")
        if tx_sync.get("pushed", 0) > 0:
            summary_parts.append(f"{tx_sync['pushed']} transactions ajoutées à Turso")
        if hvc_sync.get("pushed", 0) > 0:
            summary_parts.append(f"{hvc_sync['pushed']} HVC ajoutés à Turso")

        summary_str = ", ".join(summary_parts) if summary_parts else "Toutes les données sont déjà synchronisées"
        success_msg = f"Synchronisation réussie en {duration}s ! ({summary_str})"

        record_sync_event(
            status="SUCCESS",
            message=success_msg,
            details=details,
        )

        if progress_callback:
            progress_callback(100, "Synchronisation terminée avec succès !")

        return {
            "success": True,
            "status": "SUCCESS",
            "message": success_msg,
            "details": details,
        }

    except Exception as exc:
        logger.error("Erreur durant la synchronisation: %s", exc, exc_info=True)
        err_msg = f"Échec de synchronisation : {exc}"
        record_sync_event(
            status="FAILED",
            message=err_msg,
            details={"error": str(exc)},
        )
        return {
            "success": False,
            "status": "FAILED",
            "message": err_msg,
            "details": {"error": str(exc)},
        }
    finally:
        try:
            remote_conn.close()
        except Exception:
            pass
