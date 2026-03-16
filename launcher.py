"""
Clom — Launcher
Démarre le serveur FastAPI et ouvre le navigateur automatiquement.
Écoute sur 0.0.0.0 pour permettre l'accès depuis le réseau local (téléphone).
"""
import sys
import os
import threading
import webbrowser
import time
import socket

# Quand empaquété avec PyInstaller, les fichiers sont dans _MEIPASS
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    os.chdir(BASE_DIR)
    # --noconsole met sys.stdout/stderr à None, ce qui crash uvicorn
    # Rediriger vers le fichier log
    _log = open(os.path.join(BASE_DIR, "Clom.log"), "a", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    os.chdir(BASE_DIR)

PORT = 9000
HOST = "0.0.0.0"
URL = f"http://localhost:{PORT}"


def get_local_ip():
    """Récupère l'IP locale du PC sur le réseau Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ensure_firewall_rule():
    """Crée une règle pare-feu Windows pour autoriser le port 9000 (une seule fois)."""
    import subprocess
    try:
        # Vérifie si la règle existe déjà
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Clom"],
            capture_output=True, text=True, creationflags=0x08000000
        )
        if "Clom" in result.stdout:
            return  # Règle déjà présente
        # Crée la règle
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             "name=Clom", "dir=in", "action=allow", "protocol=TCP", f"localport={PORT}"],
            capture_output=True, creationflags=0x08000000
        )
        print(f"  Règle pare-feu créée pour le port {PORT}.")
    except Exception as e:
        print(f"  Impossible de configurer le pare-feu : {e}")


def open_browser():
    """Attend que le serveur soit prêt puis ouvre le navigateur."""
    import urllib.request
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(URL, timeout=1)
            webbrowser.open(URL)
            local_ip = get_local_ip()
            if local_ip != "127.0.0.1":
                print(f"\n  Accès mobile : http://{local_ip}:{PORT}")
                print(f"  Scanne le QR code dans l'app pour te connecter depuis ton téléphone.\n")
            return
        except Exception:
            pass


if __name__ == "__main__":
    # Ajouter le répertoire de base au path pour que "import main" fonctionne
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    # Configurer le pare-feu Windows au premier lancement
    ensure_firewall_rule()

    # Ouvrir le navigateur dans un thread séparé
    threading.Thread(target=open_browser, daemon=True).start()

    # Lancer uvicorn — écoute sur 0.0.0.0 pour accès LAN
    from main import app
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
