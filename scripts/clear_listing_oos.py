import sqlite3
from models.db import get_connection

def clear_listing_oos(conn: sqlite3.Connection = None) -> int:
    """Vide entièrement la table listing_oos et libère l'espace."""
    should_close = False
    if conn is None:
        conn = get_connection()  # Utilisez votre fonction habituelle de connexion
        should_close = True

    try:
        cursor = conn.cursor()
        
        # 1. Compter le nombre de lignes avant suppression (pour info)
        cursor.execute("SELECT COUNT(*) FROM listing_oos")
        deleted_count = cursor.fetchone()[0]

        # 2. Supprimer toutes les données
        cursor.execute("DELETE FROM listing_oos")
        
        # 3. Réinitialiser la séquence AUTOINCREMENT (si existante)
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='listing_oos'")
        
        conn.commit()
        print(f"Table 'listing_oos' vidée avec succès ({deleted_count} lignes supprimées).")
        return deleted_count

    except Exception as e:
        conn.rollback()
        print(f"Erreur lors de la vidange de listing_oos : {e}")
        return 0
    finally:
        if should_close:
            conn.close()