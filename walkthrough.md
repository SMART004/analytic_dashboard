# Walkthrough - Implémentation Tâche 1 : Infrastructure & Ingestion SQLite v1.1

## Modifications Apportées

### 1. Infrastructure Base de Données (`database/`)
- [NEW] [database/__init__.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/database/__init__.py) : Exportation du module de base de données.
- [NEW] [database/connection.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/database/connection.py) : Gestionnaire de connexions SQLite thread-safe configuré en mode **WAL** (`journal_mode=WAL`), `busy_timeout=10000`.
- [NEW] [database/schema.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/database/schema.py) : Script DDL complet implémentant le **Schéma SQLite v1.1** :
  - Clé primaire composite `(agent_msisdn, source_master)` sur `referentiel_pos`.
  - Snapshots d'ingestion sur `transactions` (`zone_sa_snapshot`, `territoire_snapshot`, `site_key_snapshot`).
  - Table canonique unique `sites(site_key)` avec clés étrangères.
  - Contrainte `CHECK` explicite sur `exclusions_reference.category`.
  - Table `hvc_commercial_mapping` rattachée par `hvc_msisdn` et `ccial_msisdn`.
  - Indexation optimisée (`idx_tx_date_only`, `idx_tx_hour`, `idx_tx_from_to`, etc.).

### 2. Service d'Ingestion & Reconstitution (`services/`)
- [NEW] [services/ingestion.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/services/ingestion.py) : Pipeline d'ingestion automatisé avec :
  - Ingestion & normalisation des sites (`sites_etoudi`, `zones`).
  - Ingestion & déduplication des PDV et commerciaux.
  - Consolidation des comptes internes exclus dans `exclusions_reference`.
  - Calcul et figeage des snapshots lors de l'ingestion des transactions.
  - Déduplication par hash de fichier (`file_hash`) et contrainte d'unicité `(tx_date, from_msisdn, to_msisdn, amount, tx_type)`.
- [NEW] [services/reference_service.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/services/reference_service.py) : Couche d'accès unifiée aux données de référence via requêtes SQLite directes.

---

## Résultats de l'Ingestion (en cours)

| Table SQLite v1.1 | Entrées Inérées / Normalisées | Remarques |
| :--- | :--- | :--- |
| **`sites`** | 232 sites | Normalisés avec `site_key` canonique |
| **`referentiel_pos`** | 14 236 agents/PDV | Gère Centre II et Centre III sans collision de clés |
| **`referentiel_commerciaux`** | 121 commerciaux | Indexés par `ccial_msisdn` |
| **`exclusions_reference`** | 268 comptes | Consolidés depuis commerciaux, caisses, masters, CDS, PR |
| **`transactions`** | En cours d'ingestion | Capture des snapshots `zone_sa_snapshot`, `territoire_snapshot` |
