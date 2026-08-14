# scripts/create_admin.py
"""
Script CLI d'initialisation des utilisateurs de départ dans la base SQLite.
Permet de modifier un compte 'Admin'

lancer avec python scripts/update_user.py
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


def update_user_password(username: str, new_password: str) -> bool:
    """Met à jour uniquement le mot de passe d'un utilisateur existant."""
    init_user_table()  # S'assure que la table existe
    
    clean_username = username.strip()
    if not clean_username or not new_password.strip():
        print("⚠️ Le nom d'utilisateur et le mot de passe ne peuvent pas être vides.")
        return False

    hashed_pw = hash_password(new_password.strip())

    conn = sqlite3.connect(DEFAULT_DB_FILENAME)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE users
            SET password_hash = ?
            WHERE username = ?;
            """,
            (hashed_pw, clean_username),
        )
        conn.commit()

        if cursor.rowcount == 0:
            print(f"❌ Utilisateur '{clean_username}' non trouvé.")
            return False

        print(f"✅ Le mot de passe de '{clean_username}' a été mis à jour avec succès !")
        return True

    except Exception as exc:
        print(f"❌ Erreur lors de la mise à jour du mot de passe pour '{clean_username}' : {exc}")
        return False
    finally:
        conn.close()

def reset_password_cli():
    print("=" * 60)
    print(" 🔑 RÉINITIALISATION DU MOT DE PASSE UTILISATEUR")
    print("=" * 60)

    username = input("\nNom d'utilisateur à modifier : ").strip()
    while not username:
        print("⚠️ Le nom d'utilisateur est obligatoire.")
        username = input("Nom d'utilisateur à modifier : ").strip()

    # Demande du nouveau mot de passe avec confirmation
    new_pass = getpass.getpass("Nouveau mot de passe : ").strip()
    while not new_pass:
        print("⚠️ Le mot de passe ne peut pas être vide.")
        new_pass = getpass.getpass("Nouveau mot de passe : ").strip()

    confirm_pass = getpass.getpass("Confirmez le nouveau mot de passe : ").strip()
    if new_pass != confirm_pass:
        print("❌ Les mots de passe ne correspondent pas. Opération annulée.")
        return

    # Exécution de la mise à jour
    success = update_user_password(username, new_pass)
    if success:
        print("\n🎉 Réinitialisation terminée avec succès !")


if __name__ == "__main__":
    reset_password_cli()