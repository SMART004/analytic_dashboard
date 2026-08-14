# scripts/create_admin.py
"""
Script CLI d'initialisation des utilisateurs de départ dans la base SQLite.
Permet d'insérer un compte 'Admin' et un compte 'Viewer' de manière sécurisée.

lancer avec python scripts/create_admin.py
"""

from __future__ import annotations

import getpass
import os
import sqlite3
import sys

# Ajouter le répertoire racine au PYTHONPATH pour importer nos modules internes
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bcrypt

from app_config.settings import DEFAULT_DB_FILENAME, ROLE_ADMIN, ROLE_VIEWER
from models.user_model import init_user_table


def hash_password(password: str) -> str:
    """Hache le mot de passe avec bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_user(username: str, password: str, role: str) -> bool:
    """Insère ou met à jour un utilisateur dans la table SQLite 'users'."""
    init_user_table()  # S'assure que la table existe
    hashed_pw = hash_password(password)

    conn = sqlite3.connect(DEFAULT_DB_FILENAME)
    try:
        cursor = conn.cursor()
        # Insertion ou mise à jour en cas d'existence
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, role)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                role = excluded.role;
        """,
            (username.strip(), hashed_pw, role.strip()),
        )
        conn.commit()
        return True
    except Exception as exc:
        print(f"❌ Erreur lors de la création de l'utilisateur '{username}' : {exc}")
        return False
    finally:
        conn.close()


def main():
    print("=" * 60)
    print(" 🛠️  INITIALISATION DES UTILISATEURS (ADMIN & VIEWER)")
    print("=" * 60)

    # 1. Création de l'Administrateur
    print("\n--- 1. Compte Administrateur (Access total) ---")
    admin_user = input("Nom d'utilisateur Admin [défaut: admin] : ").strip() or "admin"
    admin_pass = getpass.getpass("Mot de passe Admin : ").strip()
    
    while not admin_pass:
        print("⚠️ Le mot de passe ne peut pas être vide.")
        admin_pass = getpass.getpass("Mot de passe Admin : ").strip()

    if create_user(admin_user, admin_pass, ROLE_ADMIN):
        print(f"✅ Compte Admin '{admin_user}' créé/mis à jour avec succès !")

    # 2. Création de l'utilisateur Viewer
    print("\n--- 2. Compte Lecteur / Viewer (Accès restreint) ---")
    viewer_user = input("Nom d'utilisateur Viewer [défaut: viewer] : ").strip() or "viewer"
    viewer_pass = getpass.getpass("Mot de passe Viewer : ").strip()
    
    while not viewer_pass:
        print("⚠️ Le mot de passe ne peut pas être vide.")
        viewer_pass = getpass.getpass("Mot de passe Viewer : ").strip()

    if create_user(viewer_user, viewer_pass, ROLE_VIEWER):
        print(f"✅ Compte Viewer '{viewer_user}' créé/mis à jour avec succès !")

    print("\n🎉 Terminé ! Vous pouvez maintenant utiliser ces identifiants pour vous connecter.")


if __name__ == "__main__":
    main()