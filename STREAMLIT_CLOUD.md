# Deploiement Streamlit Community Cloud

## Parametres de l'application

- **Repository** : le depot Git contenant ce projet
- **Main file path** : `app.py`
- **Python version** : `3.12`

La commande de lancement par defaut de Streamlit Cloud suffit. Les dependances sont installees depuis `requirements.txt`.

## Secrets

Dans **App settings > Secrets**, ajouter le contenu de `.streamlit/secrets.toml.example` en remplacant les valeurs Turso.

`USE_LOCAL_STORAGE = false` active la base distante Turso pour la base metier, les comptes utilisateurs et les fichiers importes. Si cette cle est absente, l'application passe aussi automatiquement en mode distant lorsque `TURSO_DATABASE_URL` ou `LIBSQL_URL` est defini.

Les variables peuvent egalement etre fournies dans un fichier `.env` en local ou comme variables d'environnement dans un autre hebergeur. La priorite est : variables d'environnement, puis secrets Streamlit, puis valeurs par defaut.

## Donnees persistantes

Le stockage local `storage/` est ephemere sur Community Cloud. Le mode Turso est donc recommande en production : il conserve la base SQLite/libSQL, les utilisateurs et les fichiers de configuration/importes entre les redemarrages.
