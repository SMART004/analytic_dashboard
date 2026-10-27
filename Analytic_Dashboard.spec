# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

added_files = [
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/app.py', '.'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/config.py', '.'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/models/schema.sql', 'models'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/models', 'models'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/views', 'views'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/controllers', 'controllers'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/services', 'services'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/utils', 'utils'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/pages', 'pages'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/app_config', 'app_config'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/ingestion', 'ingestion'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/domain', 'domain'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/scripts', 'scripts'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/.venv/Lib/site-packages/streamlit', 'streamlit'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/.venv/Lib/site-packages/streamlit_option_menu/frontend/dist', 'streamlit_option_menu/frontend/dist'),
        ('C:/Users/marc.siewe/Documents/analytic_dashboard/.venv/Lib/site-packages/_polars_runtime_compat', '_polars_runtime_compat'),
        ('C:/Users/marc.siewe/AppData/Local/Programs/Python/Python313/Lib/site-packages/streamlit-1.55.0.dist-info', 'streamlit-1.55.0.dist-info'),
        ('C:/Users/marc.siewe/AppData/Local/Programs/Python/Python313/Lib/site-packages/streamlit_option_menu-0.4.0.dist-info', 'streamlit_option_menu-0.4.0.dist-info'),
        ('C:/Users/marc.siewe/AppData/Local/Programs/Python/Python313/Lib/site-packages/altair-6.0.0.dist-info', 'altair-6.0.0.dist-info')
]

hidden_modules = ['streamlit', 'streamlit.web.cli', 'streamlit.web.bootstrap', 'streamlit.runtime', 'streamlit.runtime.scriptrunner.magic_funcs', 'streamlit.runtime.scriptrunner.magic', 'streamlit_option_menu', 'altair', 'polars', '_polars_runtime_compat', '_polars_runtime_compat._polars_runtime', 'fastexcel', 'pyarrow', 'plotly', 'openpyxl', 'xlsxwriter', 'bcrypt', 'libsql', 'sqlite3', 'pandas', 'numpy', 'sklearn', 'dotenv', 'webview', 'pythonnet', 'clr', 'bottle', 'proxy_tools']

a = Analysis(
    ['desktop_app.py'],
    pathex=['C:/Users/marc.siewe/Documents/analytic_dashboard'],
    binaries=[],
    datas=added_files,
    hiddenimports=hidden_modules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib.tests', 'numpy.tests'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Analytic_Dashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True, # Console visible pour logs de démarrage, peut être masquée avec False
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Analytic_Dashboard',
)
