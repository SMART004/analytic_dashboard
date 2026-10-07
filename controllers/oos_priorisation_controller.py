import pandas as pd
import numpy as np
from datetime import datetime
import io
import zipfile
from typing import Dict, Any

from models.oos_model import get_all_pos, get_hvc_commercial_mapping

def process_float_priorisation(
    df: pd.DataFrame, 
    col_msisdn: str, 
    col_name: str, 
    col_status: str, 
    col_balance: str, 
    col_float_req: str, 
    col_avg_hourly: str, 
    threshold_proche: int = 24, 
    threshold_surveiller: int = 72
) -> Dict[str, Any]:
    
    df = df.copy()
    
    # 1. Clean msisdn
    df[col_msisdn] = df[col_msisdn].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
    
    # Ensure numeric types
    for col in [col_balance, col_float_req, col_avg_hourly]:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
        
    # 2. Calculate heures_avant_rupture
    def calc_hours(row):
        avg_h = row[col_avg_hourly]
        bal = row[col_balance]
        if avg_h < 0:
            if avg_h == 0:
                return float('inf')
            return bal / abs(avg_h)
        return float('inf')
        
    df['heures_avant_rupture_num'] = df.apply(calc_hours, axis=1)
    
    # 3. Classification
    def classify(row):
        if row[col_status] == 'Below' or row[col_balance] < row[col_float_req]:
            return 'RUPTURE'
        h = row['heures_avant_rupture_num']
        if h <= threshold_proche:
            return 'RUPTURE_PROCHE'
        if h <= threshold_surveiller:
            return 'A_SURVEILLER'
        return 'STABLE'
        
    df['classification'] = df.apply(classify, axis=1)
    
    # 4. Format heures_avant_rupture string
    df['heures_avant_rupture'] = df['heures_avant_rupture_num'].apply(
        lambda x: "Illimité" if x == float('inf') else round(x, 1)
    )
    
    # 5. Join Referentiel POS
    ref_pos = get_all_pos()
    msisdn_ref = next((c for c in ["msisdn", "agent_msisdn"] if c in ref_pos.columns), None)
    
    df_non_joints = pd.DataFrame()
    if msisdn_ref and ref_pos is not None and not ref_pos.empty:
        ref_pos[msisdn_ref] = ref_pos[msisdn_ref].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
        ref_pos_clean = ref_pos.drop_duplicates(subset=[msisdn_ref], keep='last')
        
        # We need segment, zone, territory, cluster, sitename
        # Look for the columns in ref_pos
        def find_col(possible_names):
            return next((c for c in possible_names if c in ref_pos_clean.columns), None)
            
        c_seg = find_col(['segment', 'segment_group'])
        c_zone = find_col(['zone', 'Zone'])
        c_terr = find_col(['territory', 'Territoire', 'territoire'])
        c_clust = find_col(['cluster', 'Cluster'])
        c_site = find_col(['sitename', 'site', 'Nom_Site', 'nom_site'])
        
        cols_to_extract = [msisdn_ref]
        rename_dict = {}
        if c_seg: cols_to_extract.append(c_seg); rename_dict[c_seg] = 'segment'
        if c_zone: cols_to_extract.append(c_zone); rename_dict[c_zone] = 'zone'
        if c_terr: cols_to_extract.append(c_terr); rename_dict[c_terr] = 'territoire'
        if c_clust: cols_to_extract.append(c_clust); rename_dict[c_clust] = 'cluster'
        if c_site: cols_to_extract.append(c_site); rename_dict[c_site] = 'sitename'
        
        ref_subset = ref_pos_clean[cols_to_extract].rename(columns=rename_dict)
        
        # Merge
        merged = df.merge(ref_subset, left_on=col_msisdn, right_on=msisdn_ref, how='left')
        
        df_non_joints = merged[merged['segment'].isna()][df.columns]
    else:
        merged = df.copy()
        for c in ['segment', 'zone', 'territoire', 'cluster', 'sitename']:
            merged[c] = 'N/A'
        df_non_joints = df.copy()

    # 6. FILTRE OBLIGATOIRE: HVC
    if 'segment' in merged.columns:
        merged['segment'] = merged['segment'].fillna('N/A')
        merged = merged[merged['segment'].astype(str).str.upper().str.contains('HVC')]
    else:
        # If no segment column found, we can't filter.
        merged['segment'] = 'N/A'
        
    # 7. Join commercial en charge
    mapping = get_hvc_commercial_mapping()
    if mapping is not None and not mapping.empty and 'hvc_msisdn' in mapping.columns and 'commercial' in mapping.columns:
        mapping['hvc_msisdn'] = mapping['hvc_msisdn'].astype(str).str.strip()
        map_clean = mapping[['hvc_msisdn', 'commercial']].drop_duplicates(subset=['hvc_msisdn'], keep='last')
        merged = merged.merge(map_clean, left_on=col_msisdn, right_on='hvc_msisdn', how='left')
        merged['commercial'] = merged['commercial'].fillna('Non attribué')
    else:
        merged['commercial'] = 'Non attribué'
        
    # 8. Define recommended action
    def action(row):
        cls = row['classification']
        if cls == 'RUPTURE': return 'Réapprovisionnement immédiat'
        if cls == 'RUPTURE_PROCHE': return 'Réapprovisionnement sous 24h'
        if cls == 'A_SURVEILLER': return 'Contacter agent / Prévoir réappro'
        return 'Aucune action requise'
    
    if not merged.empty:
        merged['action_recommandee'] = merged.apply(action, axis=1)
    else:
        merged['action_recommandee'] = []
        
    # 9. Sort by urgency
    cat_type = pd.CategoricalDtype(categories=['RUPTURE', 'RUPTURE_PROCHE', 'A_SURVEILLER', 'STABLE'], ordered=True)
    if not merged.empty:
        merged['classification'] = merged['classification'].astype(cat_type)
        merged = merged.sort_values(['classification', 'heures_avant_rupture_num'], ascending=[True, True])
    
    # 10. Format final output
    final_cols = {
        col_msisdn: 'MSISDN',
        col_name: 'Agent Name',
        'segment': 'Segment',
        'zone': 'Zone',
        'territoire': 'Territoire',
        'cluster': 'Cluster',
        'sitename': 'Sitename',
        col_status: 'Status',
        col_balance: 'Balance',
        col_float_req: 'Float Required',
        col_avg_hourly: 'Average Hourly Float',
        'heures_avant_rupture': 'Heures Avant Rupture',
        'classification': 'Classification',
        'commercial': 'Commercial En Charge',
        'action_recommandee': 'Action Recommandée'
    }
    
    for k in final_cols.keys():
        if k not in merged.columns:
            merged[k] = 'N/A'
            
    df_final = merged[list(final_cols.keys())].rename(columns=final_cols)
    
    # 11. Synthesis
    if not df_final.empty:
        syn_class = df_final['Classification'].value_counts().reset_index()
        syn_class.columns = ['Classification', "Nombre d'agents"]
        
        df_deficit = df_final[pd.to_numeric(df_final['Balance'], errors='coerce') < pd.to_numeric(df_final['Float Required'], errors='coerce')]
        vol_reappro = (pd.to_numeric(df_deficit['Float Required'], errors='coerce') - pd.to_numeric(df_deficit['Balance'], errors='coerce')).sum()
        
        syn_comm = df_final['Commercial En Charge'].value_counts().reset_index()
        syn_comm.columns = ['Commercial', "Nombre d'agents"]
    else:
        syn_class = pd.DataFrame(columns=['Classification', "Nombre d'agents"])
        vol_reappro = 0
        syn_comm = pd.DataFrame(columns=['Commercial', "Nombre d'agents"])
        
    return {
        'df_final': df_final,
        'df_non_joints': df_non_joints,
        'syn_class': syn_class,
        'vol_reappro': vol_reappro,
        'syn_comm': syn_comm
    }

def create_excel_report(result: Dict[str, Any], snapshot_date: str) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_final = result['df_final'].copy()
        
        # Format the dataframe a bit if needed
        df_final.to_excel(writer, sheet_name='Priorisation HVC', index=False)
        
        # Synthese
        syn_class = result['syn_class'].copy()
        syn_comm = result['syn_comm'].copy()
        
        syn_class.to_excel(writer, sheet_name='Synthèse', index=False, startrow=1)
        worksheet = writer.sheets['Synthèse']
        worksheet.write(0, 0, f"Snapshot: {snapshot_date}")
        worksheet.write(0, 3, "Volume total à réapprovisionner:")
        worksheet.write(0, 4, result['vol_reappro'])
        
        syn_comm.to_excel(writer, sheet_name='Synthèse', index=False, startrow=len(syn_class)+4)
        worksheet.write(len(syn_class)+3, 0, "Répartition par commercial")
        
        # Non joints
        if not result['df_non_joints'].empty:
            result['df_non_joints'].to_excel(writer, sheet_name='Non joints', index=False)
        else:
            pd.DataFrame({'Message': ['Tous les MSISDN ont été joints.']}).to_excel(writer, sheet_name='Non joints', index=False)
            
    return output.getvalue()
