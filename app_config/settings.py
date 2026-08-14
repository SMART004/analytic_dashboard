# config/settings.py
"""
Business configuration and externalized thresholds.
Allows updating threshold parameters without code modification or redeployment.
Previously held in root config.py (DEFAULT_SEUIL, ZONE_COLUMNS).
"""

# ─── Financial / Transaction Thresholds ────────────────────────────────────────
MIN_TRANSFER_AMOUNT: float = 10_000    # FCFA — seuil minimum pour comptage POS servi
                                        # et filtrage conquête (5 fichiers utilisaient
                                        # 10 000 en dur; centralisé ici)

# ─── Performance Rating Bands ──────────────────────────────────────────────────
EXCELLENT_PERF_THRESHOLD: float = 150_000_000   # FCFA
VERY_GOOD_PERF_THRESHOLD: float = 80_000_000    # FCFA
DEFAULT_SEUIL: float = 40_000_000               # ancien seuil de config.py racine

# ─── Column Names Expected in Source Files ─────────────────────────────────────
ZONE_COLUMNS = ["ZONE", "TERRITORY CORRECT", "ISL_TERR", "SITENAME"]  # ancien config.py

# ─── Hourly Trend Windows (pages de performance uniquement) ────────────────────
AFTERNOON_TREND_START_HOUR: int = 14   # [14h, 17h[  → TREND [14H→17H]
AFTERNOON_TREND_END_HOUR:   int = 17
MORNING_TREND_START_HOUR:   int = 6    # [6h, 14h]   → TREND matin
MORNING_TREND_END_HOUR:     int = 14

# ─── Coverage / Capillarity Targets ────────────────────────────────────────────
DEFAULT_CAPILLARITY_TARGET: int = 80   # objectif PDV servis par commercial
COVERAGE_GREEN_THRESHOLD:   float = 80  # %
COVERAGE_YELLOW_THRESHOLD:  float = 60  # %

# ─── Database ──────────────────────────────────────────────────────────────────
DEFAULT_DB_FILENAME: str = "dashboard.db"


# ─── Roles & Permissions (RBAC) ────────────────────────────────────────────────
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_VIEWER = "viewer"

# Association des rôles aux pages/actions autorisées
PERMISSIONS = {
    ROLE_ADMIN: ["dashboard", "performance", "settings", "oos_analysis", "conquete_territoire", "gros_transferts", "pos_non_touches", "pos_commercial"],
    ROLE_MANAGER: ["dashboard", "performance", "oos_analysis"],
    ROLE_VIEWER: ["dashboard", "oos_analysis"],
}