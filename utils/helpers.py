import pandas as pd
from io import BytesIO

def load_file(uploaded_file):
    """Charge un fichier Excel ou CSV"""
    if uploaded_file.name.lower().endswith('.csv'):
        return pd.read_csv(uploaded_file)
    else:
        return pd.read_excel(uploaded_file)

def clean_phone(x):
    """Nettoie les numéros de téléphone"""
    if pd.isna(x):
        return None
    s = str(x).strip()
    s = s.replace("FRI:", "").replace("/MSISDN", "").strip()
    return s if s else None

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