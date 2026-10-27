# build_executable.py
"""
Script de compilation et d'empaquetage de l'Application de Bureau Native (.exe).
Utilise PyInstaller pour générer une distribution autonome distribuable (standalone)
prête à être partagée avec d'autres utilisateurs sans installation de Python.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
BUILD_DIR = BASE_DIR / "build"
OUTPUT_NAME = "Analytic_Dashboard"


def get_site_packages_dir() -> Path:
    venv_site = BASE_DIR / ".venv" / "Lib" / "site-packages"
    if venv_site.exists():
        return venv_site
    import site
    return Path(site.getsitepackages()[0])


def generate_spec_file() -> Path:
    site_packages = get_site_packages_dir()
    streamlit_static = site_packages / "streamlit" / "static"
    option_menu_dist = site_packages / "streamlit_option_menu" / "frontend" / "dist"

    spec_path = BASE_DIR / f"{OUTPUT_NAME}.spec"

    datas = [
        (str(BASE_DIR / "app.py"), "."),
        (str(BASE_DIR / "config.py"), "."),
        (str(BASE_DIR / "models" / "schema.sql"), "models"),
        (str(BASE_DIR / "models"), "models"),
        (str(BASE_DIR / "views"), "views"),
        (str(BASE_DIR / "controllers"), "controllers"),
        (str(BASE_DIR / "services"), "services"),
        (str(BASE_DIR / "utils"), "utils"),
        (str(BASE_DIR / "pages"), "pages"),
        (str(BASE_DIR / "app_config"), "app_config"),
        (str(BASE_DIR / "ingestion"), "ingestion"),
        (str(BASE_DIR / "domain"), "domain"),
        (str(BASE_DIR / "scripts"), "scripts"),
    ]

    streamlit_pkg = site_packages / "streamlit"
    if streamlit_pkg.exists():
        datas.append((str(streamlit_pkg), "streamlit"))
    elif streamlit_static.exists():
        datas.append((str(streamlit_static), "streamlit/static"))
    if option_menu_dist.exists():
        datas.append((str(option_menu_dist), "streamlit_option_menu/frontend/dist"))

    polars_compat = site_packages / "_polars_runtime_compat"
    if polars_compat.exists():
        datas.append((str(polars_compat), "_polars_runtime_compat"))

    from PyInstaller.utils.hooks import copy_metadata, collect_data_files, collect_submodules
    for pkg in ["streamlit", "streamlit_option_menu", "altair", "pywebview"]:
        try:
            datas.extend(copy_metadata(pkg))
        except Exception:
            pass

    try:
        datas.extend(collect_data_files("webview"))
    except Exception:
        pass

    datas_str = ",\n        ".join(f"('{src.replace(chr(92), '/')}', '{dst.replace(chr(92), '/')}')" for src, dst in datas)

    hidden_imports = [
        "streamlit",
        "streamlit.web.cli",
        "streamlit.web.bootstrap",
        "streamlit.runtime",
        "streamlit.runtime.scriptrunner.magic_funcs",
        "streamlit.runtime.scriptrunner.magic",
        "streamlit_option_menu",
        "altair",
        "polars",
        "_polars_runtime_compat",
        "_polars_runtime_compat._polars_runtime",
        "fastexcel",
        "pyarrow",
        "plotly",
        "openpyxl",
        "xlsxwriter",
        "bcrypt",
        "libsql",
        "sqlite3",
        "pandas",
        "numpy",
        "sklearn",
        "dotenv",
        "webview",
        "pythonnet",
        "clr",
        "bottle",
        "proxy_tools",
    ]
    hidden_imports_str = ", ".join(f"'{imp}'" for imp in hidden_imports)

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

added_files = [
        {datas_str}
]

hidden_modules = [{hidden_imports_str}]

a = Analysis(
    ['desktop_app.py'],
    pathex=['{str(BASE_DIR).replace(chr(92), '/')}'],
    binaries=[],
    datas=added_files,
    hiddenimports=hidden_modules,
    hookspath=[],
    hooksconfig={{}},
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
    name='{OUTPUT_NAME}',
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
    name='{OUTPUT_NAME}',
)
"""

    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(spec_content)

    print("[*] Fichier spec genere : " + str(spec_path))
    return spec_path


def run_build() -> int:
    spec_path = generate_spec_file()

    venv_pyinstaller = BASE_DIR / ".venv" / "Scripts" / "pyinstaller.exe"
    pyinstaller_cmd = str(venv_pyinstaller) if venv_pyinstaller.exists() else "pyinstaller"

    print("=" * 65)
    print("[*] Compilation de l'executable avec PyInstaller...")
    print("=" * 65)

    cmd = [pyinstaller_cmd, "--clean", "--noconfirm", str(spec_path)]
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print(f"[!] Echec de la compilation (code {result.returncode})")
        return result.returncode

    dist_app = DIST_DIR / OUTPUT_NAME
    print(f"[+] Executable compile avec succes dans : {dist_app}")

    # Copie du dossier storage de base et du .env vers le dossier dist pour distribution
    storage_src = BASE_DIR / "storage"
    storage_dst = dist_app / "storage"
    if storage_src.exists() and not storage_dst.exists():
        print("[*] Copie des donnees locales (storage/) dans le paquet distribuable...")
        shutil.copytree(str(storage_src), str(storage_dst), dirs_exist_ok=True)

    env_src = BASE_DIR / ".env"
    if env_src.exists():
        shutil.copy(str(env_src), str(dist_app / ".env"))

    streamlit_cfg = BASE_DIR / ".streamlit"
    if streamlit_cfg.exists():
        shutil.copytree(str(streamlit_cfg), str(dist_app / ".streamlit"), dirs_exist_ok=True)
        shutil.copytree(str(streamlit_cfg), str(dist_app / "_internal" / ".streamlit"), dirs_exist_ok=True)

    # Creation d'un lanceur rapide .bat dans le dossier de distribution
    bat_content = f"@echo off\r\ncd /d \"%~dp0\"\r\nstart \"\" \"{OUTPUT_NAME}.exe\"\r\n"
    with open(dist_app / f"Lancer_{OUTPUT_NAME}.bat", "w", encoding="utf-8") as f:
        f.write(bat_content)

    print("=" * 65)
    print("[+] Paquet distribuable pret !")
    print(f"   Dossier : {dist_app}")
    print(f"   Executable : {dist_app / (OUTPUT_NAME + '.exe')}")
    print("   Vous pouvez compresser ce dossier en .ZIP pour le partager avec d'autres utilisateurs.")
    print("=" * 65)

    return 0


if __name__ == "__main__":
    sys.exit(run_build())
