# Implementation Plan - Restructuration & Migration SQLite v1.1

Ce document valide la **Tâche 0** après révision intégrale du diagnostic sur `dashboard.py` et `conquete_territoire.py`, justification/correction des affirmations du diagnostic précédent par les lignes de code exactes, et adoption officielle du **Schéma SQLite v1.1**.

---

## 1. Diagnostic Complémentaire (`dashboard.py` & `conquete_territoire.py`)

### A. Analyse de [dashboard.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/dashboard.py)

#### 1. Logique de rattachement aux zones
- **Rattachement par Commercial** : Dans [_attach_zone_fields](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/dashboard.py#L278-L299), le dashboard enrichit les transactions à partir du fichier `commerciaux` via la fonction `attach_commercial_reference(work, comm_config, side="From_clean")`. Les champs `Zone_Centre`, `Zone_Territoire` et `Zone_SA` de la transaction sont issus exclusivement du commercial émetteur (`From_clean`).
- **Comptage des PDV servis uniques** : Dans [_compute_unique_pos_served](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/dashboard.py#L301-L314), le système applique `get_global_excluded_numbers` et `is_served_pos_series` pour filtrer les comptes internes et compter les numéros `To_clean` distincts.
- **Référentiel POS par Territoire** : Dans [_build_territory_performance_df](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/dashboard.py#L376-L435), le total des POS d'un territoire est calculé en fusionnant `maitre_pos` et `maitre_pos_III` via `_load_master_pos_totals()`.

---

### B. Analyse de [conquete_territoire.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py)

#### 1. Logique de rattachement double (PDV et Commercial)
- Dans [enrichir_transactions](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L233-L305), chaque transaction est enrichie sur **deux axes indépendants** :
  - **Axe PDV (`To_clean`)** : Rattaché aux référentiels PDV (`maitre_pos` + `maitre_pos_III`). Génère `Zone_SA_PDV`, `Centre_PDV`, `Territoire_PDV`, `Quartier_PDV`, `Sitename_PDV`, `Segment_PDV`.
  - **Axe Commercial (`From_clean`)** : Rattaché au référentiel `commerciaux`. Génère `Zone_SA_Comm`, `Centre_Comm`, `Territoire_Comm`, `Commercial`.

#### 2. Filtres de qualification des transactions
- Aux lignes [1579-1584](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L1579-L1584), les transactions sont strictement filtrées sur :
  $$\text{Type} = \text{"TRANSFER"} \quad \text{ET} \quad \text{Amount} > 10\,000$$

#### 3. Logique de détection d'empiètement
- Dans [detecter_empietement](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L704-L762) :
  - L'empiètement est strictement **intra-centre** : `(df["Centre_Comm"] == df["Centre_PDV"])`. Les transactions inter-centres sont exclues.
  - Il y a empiètement si le commercial intervient dans une zone différente de la zone du PDV : `(df["Zone_SA_Comm"] != df["Zone_SA_PDV"])`.
- Dans [detecter_pdv_multi_visites](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L795-L841) :
  - Identifie les PDV (`To_clean`) qui reçoivent des transactions d'au moins 2 commerciaux issus de `Zone_SA_Comm` distinctes.

#### 4. Gestion des portefeuilles commerciaux et cas particuliers
- **Normalisation des zones** : Via `_normalize_zone_sa` ([L47-L58](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L47-L58)) avec un dictionnaire `ZONE_MAPPING` (`WD` $\rightarrow$ `WILLY DISTRIBUTION`, `FLASH` $\rightarrow$ `ETS FLASH SERVICES`, `PASCAL` $\rightarrow$ `PASCAL SARL`, etc.).
- **Cas spécial Centre III & PASCAL SARL** :
  - Défini aux lignes [41](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L41) et [224-225](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L224-L225). Pour le Centre III, la zone `PASCAL SARL` est incluse même en l'absence de PDV explicitement rattachés dans le fichier POS.
- **Construction du portefeuille commercial** ([L900-L962](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L900-L962)) :
  1. Correspondance par nom/prénom (`Ccial en charge` dans le fichier PDV vs `Nom_Ccial`).
  2. Fallback par `Zone_SA_Normalisee`.
  3. Fallback PASCAL SARL pour le Centre III.
  4. Fallback ultime de déduction par les transactions réelles (`deduit = True`) notamment pour le cas `ETS FLASH SERVICES`.

---

## 2. Vérification et Citation des Lignes de Code Exactes

| Affirmation du diagnostic initial | Ligne(s) exacte(s) dans le code source | Statut / Correction |
| :--- | :--- | :--- |
| **Seuil "10 000 FCFA"** | • [domain/reference.py:260](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/domain/reference.py#L260) : `min_amount=10000`<br>• [pages/conquete_territoire.py:1582](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/conquete_territoire.py#L1582) : `(tx_enrichi_full["Amount"] > 10000)`<br>• [pages/perf.py:416](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/perf.py#L416) : `(work_df["Amount"] >= 10000)`<br>• [pages/pos_from_night.py:407](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/pos_from_night.py#L407) : `(df_comm["Amount"] >= 10000)`<br>• [pages/pr_caisse_perf.py:639](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/pr_caisse_perf.py#L639) : `(df["Amount"] >= 10000)`<br>• [pages/cds_perf.py:563](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/cds_perf.py#L563) : `(df["Amount"] >= 10000)` | **Confirmé** : Le filtre $\ge 10\,000$ (ou $> 10\,000$) est codé en dur dans 6 fichiers. |
| **Fenêtre horaire "14h-17h"** | • [pages/pr_caisse_perf.py:1065](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/pr_caisse_perf.py#L1065) : `trend_14_17 = trend_base[(trend_base["Hour"] >= 14) & (trend_base["Hour"] < 17)]`<br>• [pages/cds_perf.py:771](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/cds_perf.py#L771) : `trend_14_17 = trend_base[(trend_base["Hour"] >= 14) & (trend_base["Hour"] < 17)]`<br>• [pages/perf.py:211-213](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/perf.py#L211-L213) : `("TREND [14H→17H]", "New")` | **Confirmé** : La fenêtre $[14\text{h}, 17\text{h}[$ est utilisée pour calculer la tendance après-midi des caisses et CDS. |
| **Fichier `domain/reference.py::get_global_excluded_numbers()`** | • [domain/reference.py:113-142](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/domain/reference.py#L113-L142)<br>• [pages/dashboard.py:12-13](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/pages/dashboard.py#L12-L13) | **Correction requise** : La fonction existe bien dans `domain/reference.py`, mais elle **n'est pas canonique à l'échelle du projet** car 5 pages (`perf.py`, `cds_perf.py`, `pr_caisse_perf.py`, `pos_from_commerciaux.py`, `pos_from_night.py`) recalculent leurs exclusions séparément inline. |

---

## 3. Validation Officielle du Schéma SQLite v1.1

Le **Schéma v1.1** est officiellement validé et servira de base exclusive pour les migrations et le refactoring :

```sql
-- =====================================================================
-- 1. RÉFÉRENTIEL DES SITES (table canonique)
-- =====================================================================
CREATE TABLE IF NOT EXISTS sites (
    site_key TEXT PRIMARY KEY,          -- Nom de site normalisé (upper/trim), clé canonique
    sitename TEXT NOT NULL,             -- Nom d'affichage tel qu'il apparaît dans les fichiers
    zone_new TEXT,
    territory_correct TEXT,
    isl_terr TEXT,
    quartier TEXT,                      -- issu de sites_etoudi
    dsm_name TEXT,                      -- issu de sites_etoudi
    UNIQUE(sitename)
);

-- =====================================================================
-- 2. RÉFÉRENTIEL DES PDV / AGENTS
-- =====================================================================
CREATE TABLE IF NOT EXISTS referentiel_pos (
    agent_msisdn TEXT NOT NULL,
    source_master TEXT NOT NULL,        -- 'maitre_pos' ou 'maitre_pos_III'
    full_name TEXT,
    zone_centre TEXT,
    zone_territoire TEXT,
    zone_sa TEXT,
    secteur_cluster TEXT,
    site_key TEXT REFERENCES sites(site_key),
    segment_group TEXT,
    day_target REAL DEFAULT 0,
    oos_target REAL DEFAULT 0,
    PRIMARY KEY (agent_msisdn, source_master)
);

-- =====================================================================
-- 3. RÉFÉRENTIEL DES COMMERCIAUX
-- =====================================================================
CREATE TABLE IF NOT EXISTS referentiel_commerciaux (
    ccial_msisdn TEXT PRIMARY KEY,
    nom_ccial TEXT NOT NULL,
    zone_centre TEXT,
    zone_territoire TEXT,
    zone_sa TEXT
);

-- =====================================================================
-- 4. EXCLUSIONS (COMPTES INTERNES)
-- =====================================================================
CREATE TABLE IF NOT EXISTS exclusions_reference (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msisdn TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN 
        ('commercial', 'caisse', 'master', 'cds', 'pos_relay_caisse')),
    label TEXT,
    territoire TEXT,
    localisation TEXT,
    UNIQUE(msisdn, category)
);

-- =====================================================================
-- 5. TRANSACTIONS MOBILE MONEY
-- =====================================================================
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_date TEXT NOT NULL,
    date_only TEXT NOT NULL,
    hour INTEGER NOT NULL,
    tx_type TEXT NOT NULL,
    amount REAL NOT NULL,
    from_msisdn TEXT,
    to_msisdn TEXT,
    from_name TEXT,
    to_name TEXT,
    balance REAL,
    zone_sa_snapshot TEXT,
    territoire_snapshot TEXT,
    site_key_snapshot TEXT REFERENCES sites(site_key),
    source_file TEXT,
    file_hash TEXT NOT NULL,
    UNIQUE(tx_date, from_msisdn, to_msisdn, amount, tx_type)
);

-- =====================================================================
-- 6. LISTING OOS
-- =====================================================================
CREATE TABLE IF NOT EXISTS listing_oos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msisdn TEXT NOT NULL,
    day_target REAL,
    float_amount REAL,
    oos_pct REAL,
    is_oos INTEGER DEFAULT 0,
    last_trx_time TEXT,
    site_key TEXT REFERENCES sites(site_key),
    cluster TEXT,
    territory TEXT,
    zone TEXT,
    segment_group TEXT,
    snapshot_date TEXT NOT NULL,
    UNIQUE(msisdn, snapshot_date)
);

-- =====================================================================
-- 7. VARIATIONS HVC
-- =====================================================================
CREATE TABLE IF NOT EXISTS hvc_variations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_key TEXT NOT NULL REFERENCES sites(site_key),
    day_hvc REAL DEFAULT 0,
    oos_pct REAL DEFAULT 0,
    snapshot_timestamp TEXT NOT NULL,
    source_file TEXT,
    UNIQUE(site_key, snapshot_timestamp)
);

-- =====================================================================
-- 8. MAPPING HVC → COMMERCIAL
-- =====================================================================
CREATE TABLE IF NOT EXISTS hvc_commercial_mapping (
    hvc_msisdn TEXT PRIMARY KEY,
    ccial_msisdn TEXT NOT NULL REFERENCES referentiel_commerciaux(ccial_msisdn),
    ccial_en_charge TEXT
);
```

---

## 4. Plan d'Exécution des Prochaines Tâches (1 à 5)

### Tâche 1 : Infrastructure & Ingestion SQLite
- Créer `database/schema.py` avec le DDL SQLite v1.1 complet.
- Créer `database/connection.py` pour la gestion des connexions thread-safe et WAL mode.
- Créer `services/ingestion.py` pour ingérer les fichiers Parquet/CSV dans SQLite avec calcul des snapshots (`zone_sa_snapshot`, `territoire_snapshot`, `site_key_snapshot`).

### Tâche 2 : Unification de la Couche Référentiel
- Déplacer l'accès données vers `services/reference_service.py`.
- Centraliser `get_global_excluded_numbers()` lisant directement depuis `exclusions_reference`.

### Tâche 3 : Refactoring des Pages Métier
- Adapter `pages/dashboard.py` et `pages/conquete_territoire.py` pour interroger SQLite au lieu de retraiter des DataFrames bruts volumineux en mémoire.
- Conserver les calculs d'empiètement intra-centre et les règles de portefeuille commercial (notamment pour `PASCAL SARL` et `ETS FLASH SERVICES`).

### Tâche 4 : Verification & Tests
- Vérification des requêtes SQLite et des KPIs par rapport aux résultats pandas existants.

---

## Plan de Vérification

### Tests Automatisés & Scripts
- Script de validation de schéma SQLite (`python database/schema.py`).
- Tests de cohérence sur les comptages uniques et montants cumulés.

### Vérification Manuelle
- Lancement de l'application Streamlit (`streamlit run app.py`) et validation visuelle des tableaux de bord (Dashboard, Conquête de Territoire, Perf, CDS, Caisses).
