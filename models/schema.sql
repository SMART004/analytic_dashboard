-- models/schema.sql
-- Schéma SQLite v1.1 — Dashboard Analytics Mobile Money

-- 0. Fichiers de configuration et de transactions stockés dans libSQL/Turso
CREATE TABLE IF NOT EXISTS stored_files (
    bucket TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    content BLOB NOT NULL,
    content_type TEXT,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bucket, file_path)
);

CREATE INDEX IF NOT EXISTS idx_stored_files_bucket ON stored_files(bucket);

-- 1. RÉFÉRENTIEL DES SITES (table canonique)
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

-- 2. RÉFÉRENTIEL DES PDV / AGENTS
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

-- 3. RÉFÉRENTIEL DES COMMERCIAUX
CREATE TABLE IF NOT EXISTS referentiel_commerciaux (
    ccial_msisdn TEXT PRIMARY KEY,
    nom_ccial TEXT NOT NULL,
    zone_centre TEXT,
    zone_territoire TEXT,
    zone_sa TEXT
);

-- 4. EXCLUSIONS (COMPTES INTERNES)
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

-- 5. TRANSACTIONS MOBILE MONEY
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

-- 6. LISTING OOS
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

-- 7. VARIATIONS HVC
CREATE TABLE IF NOT EXISTS hvc_variations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_key TEXT NOT NULL REFERENCES sites(site_key),
    day_hvc REAL DEFAULT 0,
    oos_pct REAL DEFAULT 0,
    snapshot_timestamp TEXT NOT NULL,
    source_file TEXT,
    UNIQUE(site_key, snapshot_timestamp)
);

-- 8. MAPPING HVC → COMMERCIAL
CREATE TABLE IF NOT EXISTS hvc_commercial_mapping (
    hvc_msisdn TEXT PRIMARY KEY,
    ccial_msisdn TEXT,
    ccial_en_charge TEXT
);

-- 9. QUOTA HVC ATTRIBUE PAR CDS (fichier settings 'hvc_cds' : NUM_HVC / NOM_CDS)
-- Un HVC est assigne a un seul CDS. Le quota nb_HVC d'un CDS se calcule a la
-- volee via COUNT(*) sur cette table (jamais stocke en dur, pour eviter toute
-- desynchronisation quand le fichier settings est remplace).
CREATE TABLE IF NOT EXISTS hvc_cds_assignments (
    hvc_msisdn TEXT PRIMARY KEY,        -- NUM_HVC nettoye (clean_phone)
    cds_nom TEXT NOT NULL,              -- NOM_CDS tel quel (trim)
    UNIQUE(hvc_msisdn)
);

-- 10. REFERENTIEL CDS (fichier settings 'cds' : NUM / CDS)
-- Distinct de exclusions_reference : celle-ci ne sert que la regle
-- d'exclusion globale et ne garantit pas un nom fiable par entite.
CREATE TABLE IF NOT EXISTS cds_referentiel (
    cds_msisdn TEXT PRIMARY KEY,
    nom_cds TEXT NOT NULL
);

-- 11. REFERENTIEL POINT RELAIS & CAISSES (fichier settings 'pos_relay_caisse')
-- type_point est derive a l'ingestion depuis le prefixe "CAISSE" du nom
-- (meme regle que l'ancienne prepare_point_relay_caisse_config).
CREATE TABLE IF NOT EXISTS point_relay_referentiel (
    msisdn_pr TEXT PRIMARY KEY,
    nom TEXT NOT NULL,
    territoire TEXT,
    localisation TEXT,
    type_point TEXT NOT NULL CHECK (type_point IN ('Point Relais', 'Caisses'))
);

-- INDEX
CREATE INDEX IF NOT EXISTS idx_tx_date_only ON transactions(date_only);
CREATE INDEX IF NOT EXISTS idx_tx_hour ON transactions(hour);
CREATE INDEX IF NOT EXISTS idx_tx_from_to ON transactions(from_msisdn, to_msisdn);
CREATE INDEX IF NOT EXISTS idx_tx_to_date ON transactions(to_msisdn, date_only, tx_date);
CREATE INDEX IF NOT EXISTS idx_tx_to_txdate_id ON transactions(to_msisdn, tx_date, id);
CREATE INDEX IF NOT EXISTS idx_tx_type_amount ON transactions(tx_type, amount);
CREATE INDEX IF NOT EXISTS idx_tx_site_snapshot ON transactions(site_key_snapshot);
CREATE INDEX IF NOT EXISTS idx_pos_zone_sa ON referentiel_pos(zone_sa);
CREATE INDEX IF NOT EXISTS idx_pos_site_key ON referentiel_pos(site_key);
CREATE INDEX IF NOT EXISTS idx_commerciaux_zone_sa ON referentiel_commerciaux(zone_sa);
CREATE INDEX IF NOT EXISTS idx_exclusions_msisdn ON exclusions_reference(msisdn);
CREATE INDEX IF NOT EXISTS idx_oos_site_key ON listing_oos(site_key);
CREATE INDEX IF NOT EXISTS idx_hvc_site_key ON hvc_variations(site_key);
CREATE INDEX IF NOT EXISTS idx_hvc_cds_nom ON hvc_cds_assignments(cds_nom);
CREATE INDEX IF NOT EXISTS idx_pr_territoire ON point_relay_referentiel(territoire);
CREATE INDEX IF NOT EXISTS idx_pr_type_point ON point_relay_referentiel(type_point);
