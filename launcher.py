"""
Clom — Launcher
Démarre le serveur FastAPI et ouvre le navigateur automatiquement.
"""
import sys
import os
import threading
import webbrowser
import time

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
URL = f"http://localhost:{PORT}"


def open_browser():
    """Attend que le serveur soit prêt puis ouvre le navigateur."""
    import urllib.request
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(URL, timeout=1)
            webbrowser.open(URL)
            return
        except Exception:
            pass


if __name__ == "__main__":
    # Ajouter le répertoire de base au path pour que "import main" fonctionne
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    # Ouvrir le navigateur dans un thread séparé
    threading.Thread(target=open_browser, daemon=True).start()

    # Lancer uvicorn — import direct de l'app pour compatibilité PyInstaller
    from main import app
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
