# Implementation Plan — Revision OOS & Classement Commerciaux dans Streamlit `oos_view`

Mise à jour majeure de la vue `oos_view` pour intégrer l'évaluation de la résolution OOS (sans jointure transactions), le tableau de classement des commerciaux avec score de performance et graphique Plotly Top 10, l'enrichissement des métriques POS fréquemment en rupture (OOS Frequently), le filtre de comportement (OOS Chroniques vs Occasionnels), et l'ajout d'un filtre temporel par plage de dates.

---

## User Review Required

> [!IMPORTANT]
> - **Règle de résolution OOS** : L'évaluation de la résolution d'un POS s'effectue **exclusivement** via les captures `listing_oos`. Un POS est considéré comme **Corrigé / Réarmé** au cours d'une journée s'il apparaît dans au moins une capture mais ne figure plus dans la dernière capture de cette même journée.
> - **Formule du Score Commercial (/100)** :
>   - `Poids Correction (70 pts max)` = `(POS Corrigés / POS Uniques OOS) * 70`
>   - `Bonus Stabilité (30 pts max)` = `MAX(0, 30 - (Fréquence Moyenne OOS par POS * 10))`
>   - `Score Final (/100)` = `Poids Correction + Bonus Stabilité`
> - **Filtres de date & comportement** : Un sélecteur de plage de dates (Date de début et Date de fin) est ajouté dans `oos_view`, permettant d'analyser l'historique sur n'importe quelle période sélectionnée.

---

## Open Questions

- Aucune question bloquante. La formule et les règles métiers demandées sont clairement spécifiées et testées.

---

## Proposed Changes

### Controllers & Business Logic (`controllers/oos_controller.py`)

#### [MODIFY] [oos_controller.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/controllers/oos_controller.py)
- Ajouter le support du filtrage par plage de dates (`date_start`, `date_end`) dans `OosHvcFilters`.
- Implémenter la fonction `compute_commercial_ranking(df_oos: pd.DataFrame)` :
  - Calcule pour chaque commercial : `POS Uniques OOS`, `Cumul Apparitions OOS`, `Fréquence Moy.`, `POS Corrigés`, `Taux Résolution (%)`, `Score Final (/100)` et `Rang`.
- Implémenter / mettre à jour `compute_frequently_oos_metrics(df_oos: pd.DataFrame, behavior_filter: str)` :
  - Calcul à l'échelle quotidienne du nombre de snapshots par jour.
  - Métriques : `%OOS Moyen`, `Durée Moyenne OOS (Heures)`, `Float Moyen OOS`, `Site`, `Zone_SA`, `Commercial Attribué`, `Nombre d'apparitions (Jour)`, et `Statut` ("Chronique" vs "Occasionnel").
  - Prise en compte du filtre de comportement : `Tous`, `OOS Chroniques / Jour par Jour` (>= 2 jours d'OOS), `OOS Occasionnels / Parfois` (1 jour d'OOS).

---

### Models Tier (`models/oos_model.py`)

#### [MODIFY] [oos_model.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/models/oos_model.py)
- Mettre à jour `get_oos_listing(...)` et `get_oos_filter_options(...)` pour prendre en compte le filtrage par plage de dates (`date_start` et `date_end`) sur `listing_oos.snapshot_date`.

---

### Views & User Interface (`views/oos_view.py`)

#### [MODIFY] [oos_view.py](file:///c:/Users/marc.siewe/Documents/analytic_dashboard/views/oos_view.py)
- **Filtre de Dates** : Ajouter un widget `st.date_input` pour sélectionner la plage de dates (par défaut du min au max disponible ou dernier jour).
- **Nouvelle Section : Classement des Commerciaux** :
  - Graphique Plotly à barres horizontales affichant le Top 10 des commerciaux par Score Final, avec code couleur (Vert >= 80, Orange 50-79, Rouge < 50).
  - Tableau interactif Streamlit avec les 8 colonnes requises (`Rang`, `Commercial`, `POS Uniques OOS`, `Cumul Apparitions OOS`, `Fréquence Moy.`, `POS Corrigés`, `Taux Résolution (%)`, `Score Final (/100)`).
- **Mise à jour de la section "OOS Frequently"** :
  - Ajout du widget de segmentation par comportement : `Tous`, `OOS Chroniques / Jour par Jour`, `OOS Occasionnels / Parfois`.
  - Tableau enrichi avec les 10 colonnes requises (`Nom POS`, `Numéro POS`, `Site`, `Zone_SA`, `Commercial Attribué`, `Nombre d'apparitions (Jour)`, `%OOS Moyen`, `Durée Moyenne OOS (Heures)`, `Float Moyen OOS`, `Statut`).
  - Boutons d'export (CSV, Excel).

---

## Verification Plan

### Automated Tests
- Exécuter un script Python d'intégration pour valider :
  1. Le calcul exact des POS corrigés sans jointure transactions.
  2. La formule du Score Commercial (Poids Correction + Bonus Stabilité).
  3. L'évaluation quotidienne des snapshots et du %OOS moyen.
  4. La bonne classification "Chronique" vs "Occasionnel".

### Manual Verification
- Lancer le serveur Streamlit via `python -m streamlit run app.py` ou `run_command` pour s'assurer du rendu sans erreur.
- Vérifier l'interactivité des filtres de date, filtres géographiques et filtre de comportement.
- Vérifier les graphiques Plotly et l'export des tableaux Excel/CSV.
