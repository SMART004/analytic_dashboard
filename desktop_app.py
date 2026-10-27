# desktop_app.py
"""
Lanceur d'Application de Bureau Native (Local-First) pour Windows avec pywebview.
Demarre le serveur analytique local en arriere-plan et ouvre une fenetre
native autonome via pywebview (Edge WebView2 integre).

Architecture a deux processus :
1. Processus Maitre (GUI) : Detecte un port libre, lance le sous-processus serveur,
   attend la reponse locale du serveur Streamlit, ouvre la fenetre native
   autonome via pywebview, et assure l'extinction propre du serveur a la fermeture.
2. Sous-processus Serveur : Execute le moteur Streamlit et l'acces SQLite local.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

# Configuration encodage console Windows anti-crash (cp1252 / utf-8)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Resolution des repertoires selon le mode (executable compile vs script Python)
if getattr(sys, "frozen", False):
    APP_DIR = Path(getattr(sys, "_MEIPASS", sys.executable)).resolve()
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
    BASE_DIR = APP_DIR

# S'assurer que APP_DIR est dans sys.path pour tous les modules
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

DEFAULT_PORT = 8501
WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 920
APP_TITLE = "Tableau de Bord Analytique - Mode Bureau Local-First"


def find_available_port(starting_port: int = DEFAULT_PORT) -> int:
    """Trouve un port TCP local libre."""
    port = starting_port
    while port < starting_port + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex(("127.0.0.1", port))
            if result != 0:
                return port
        port += 1
    return starting_port


def wait_for_server(url: str, timeout: float = 75.0) -> bool:
    """Attend que le serveur Streamlit soit pret a repondre."""
    health_url = f"{url}/_stcore/health"
    start_time = time.time()
    last_dot = start_time
    while time.time() - start_time < timeout:
        try:
            with urlopen(health_url, timeout=1.5) as response:
                if response.status == 200:
                    print()
                    return True
        except Exception:
            if time.time() - last_dot >= 2.0:
                print(".", end="", flush=True)
                last_dot = time.time()
            time.sleep(0.5)
    print()
    return False


def run_server_mode(port: int) -> int:
    """Mode serveur interne : lance le serveur Streamlit."""
    os.environ["DESKTOP_MODE"] = "1"
    os.environ["USE_LOCAL_STORAGE"] = "true"
    os.environ["POLARS_SKIP_CPU_CHECK"] = "1"

    from streamlit.web import cli as stcli

    app_path = str(APP_DIR / "app.py")
    sys.argv = [
        "streamlit",
        "run",
        app_path,
        f"--server.port={port}",
        "--server.address=127.0.0.1",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
    ]
    return stcli.main()


def run_gui_master() -> int:
    """Mode maitre : demarre le serveur en arriere-plan et la fenetre pywebview."""
    print("=" * 65)
    print(f"[*] Demarrage de {APP_TITLE}")
    print("    Mode Local-First (Base SQLite locale active)")
    print("=" * 65)

    # Verification de chemin UNC (partage reseau \\server\...)
    if str(BASE_DIR).startswith("\\\\"):
        print("[!] ATTENTION : Vous executez l'application depuis un partage reseau.")
        print("    Pour eviter les lenteurs et blocages de base de donnees, veuillez")
        print("    COPIER ce dossier sur votre disque local (ex: Bureau ou C:\\Applications).")
        print("=" * 65)

    os.environ["DESKTOP_MODE"] = "1"
    os.environ["USE_LOCAL_STORAGE"] = "true"
    os.environ["POLARS_SKIP_CPU_CHECK"] = "1"

    port = find_available_port(DEFAULT_PORT)
    app_url = f"http://127.0.0.1:{port}/?desktop=1"

    # Fichier de journalisation du serveur local
    log_file_path = BASE_DIR / "server.log"

    # Construction de la commande pour lancer le serveur
    if getattr(sys, "frozen", False):
        server_cmd = [sys.executable, "--server", f"--port={port}"]
    else:
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        python_exe = str(venv_python) if venv_python.exists() else sys.executable
        server_cmd = [python_exe, str(BASE_DIR / "desktop_app.py"), "--server", f"--port={port}"]

    print(f"[*] Demarrage du serveur analytique sur le port {port}...")
    log_file = open(log_file_path, "w", encoding="utf-8", errors="replace")

    server_process = subprocess.Popen(
        server_cmd,
        cwd=str(BASE_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    try:
        print("[*] Initialisation du moteur en cours (veuillez patienter)...")
        if not wait_for_server(f"http://127.0.0.1:{port}", timeout=75.0):
            print("[!] Erreur : Le serveur local n'a pas repondu dans le delai imparti.")
            print(f"[!] Consultez le fichier de log pour plus de details : {log_file_path}")
            server_process.terminate()
            input("\nAppuyez sur Entree pour quitter...")
            return 1

        print(f"[+] Serveur local pret sur http://127.0.0.1:{port} !")

        # Tentative d'ouverture via pywebview (fenetre native autonome)
        opened_native = False
        try:
            import webview
            # Activer le support des telechargements (SaveFileDialog Windows natif)
            webview.settings["ALLOW_DOWNLOADS"] = True
            print("[*] Ouverture de la fenetre de bureau native via pywebview...")
            window = webview.create_window(
                title=APP_TITLE,
                url=app_url,
                width=WINDOW_WIDTH,
                height=WINDOW_HEIGHT,
                min_size=(960, 640),
                confirm_close=False,
            )
            print("=" * 65)
            print("[+] Application de bureau active. Fermez la fenetre pour quitter.")
            print(f"    URL d'acces direct : http://127.0.0.1:{port}")
            print("=" * 65)
            webview.start()
            opened_native = True
        except Exception as webview_err:
            print(f"[-] Pywebview indisponible ({webview_err}), bascule sur navigateur...")

        if not opened_native:
            # Fallback navigateur standard
            import webbrowser
            print(f"[*] Ouverture de votre navigateur sur {app_url}...")
            webbrowser.open(app_url)
            print("=" * 65)
            print("[+] Application active dans votre navigateur.")
            print(f"    URL : http://127.0.0.1:{port}")
            print("    Ne fermez pas cette fenetre terminal tant que vous utilisez l'application.")
            print("=" * 65)
            input("\nAppuyez sur Entree dans ce terminal lorsque vous souhaitez fermer l'application...")

    except KeyboardInterrupt:
        print("\n[*] Arret demande par l'utilisateur...")
    finally:
        print("[*] Fermeture du serveur local...")
        try:
            server_process.terminate()
            server_process.wait(timeout=3.0)
        except Exception:
            server_process.kill()
        try:
            log_file.close()
        except Exception:
            pass
        print("[+] Application fermee proprement.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--server", action="store_true", help="Lancer en mode serveur Streamlit")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port d'ecoute du serveur")
    args, unknown = parser.parse_known_args()

    if args.server:
        return run_server_mode(args.port)
    else:
        return run_gui_master()


if __name__ == "__main__":
    sys.exit(main())
