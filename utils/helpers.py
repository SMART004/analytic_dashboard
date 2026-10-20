import pandas as pd
from io import BytesIO

def load_file(uploaded_file):
    """Charge un fichier Excel ou CSV"""
    if uploaded_file.name.lower().endswith('.csv'):
        return pd.read_csv(uploaded_file)
    else:
        return pd.read_excel(uploaded_file)

def clean_phone(x):
    """Nettoie les numéros de téléphone et normalise au format standard 2376XXXXXXXX (Cameroun)."""
    if pd.isna(x) or x is None:
        return None
    if isinstance(x, float):
        import numpy as np
        if np.isnan(x):
            return None
        s = f"{int(x)}" if x.is_integer() else str(x)
    else:
        s = str(x).strip()
        if s.endswith(".0"):
            s = s[:-2]

    # Supprimer les préfixes/suffixes opérateur fréquents
    s = s.replace("FRI:", "").replace("/MSISDN", "").replace("/MM", "").strip()

    # Extraire uniquement les chiffres
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None

    # Normalisation Cameroun : 9 chiffres débutant par 6 -> 2376XXXXXXXX
    if len(digits) == 9 and digits.startswith("6"):
        return "237" + digits
    elif digits.startswith("00237"):
        return digits[2:]
    return digits


def find_amount_column(df):
    """Détecte automatiquement la colonne montant"""
    possible = ['Amount', 'Amount Currency', 'amount', 'Montant', 'AMOUNT', 'Value']
    for col in possible:
        if col in df.columns:
            return col
    for col in df.columns:
        if isinstance(col, str) and "Amount" in col:
            return col
    return None

def to_excel(df):
    """Convertit un DataFrame en bytes Excel"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Analyse')
    return output.getvalue()