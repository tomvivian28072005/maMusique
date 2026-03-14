"""
Script de build — Crée le dossier distribuable maMusique.
Usage: python build.py
"""
import subprocess
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist" / "maMusique"

# Chemins des outils externes (à adapter si besoin)
FFMPEG_DIR = Path(r"C:\Users\tomvi\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin")
NODE_EXE = Path(r"C:\jeu + application\utilitaire\programation\node.exe")

def main():
    print("=== Build maMusique ===\n")

    # 1. PyInstaller
    print("[1/4] PyInstaller...")
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name", "maMusique",
        "--icon", "NONE",  # TODO: ajouter une icône .ico plus tard
        "--add-data", "index.html;.",
        "--add-data", "database.py;.",
        "--add-data", "logo.svg;.",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "aiofiles",
        "--hidden-import", "sqlalchemy.dialects.sqlite",
        "launcher.py",
    ], check=True)

    # PyInstaller crée dist/maMusique/launcher.exe, on renomme
    launcher = DIST / "launcher.exe"
    final_exe = DIST / "maMusique.exe"
    if launcher.exists() and not final_exe.exists():
        launcher.rename(final_exe)

    # Copier les fichiers data de _internal/ vers la racine du dist
    # (main.py les cherche relativement au cwd = dossier du .exe)
    internal = DIST / "_internal"
    for name in ("index.html", "database.py", "logo.svg"):
        src = internal / name
        if src.exists():
            shutil.copy2(src, DIST / name)
            print(f"  {name} copié vers racine")

    # 2. Copier les outils externes dans bin/
    print("[2/4] Copie des outils externes dans bin/...")
    bin_dir = DIST / "bin"
    bin_dir.mkdir(exist_ok=True)

    # yt-dlp
    ytdlp_src = ROOT / "venv" / "Scripts" / "yt-dlp.exe"
    if ytdlp_src.exists():
        shutil.copy2(ytdlp_src, bin_dir / "yt-dlp.exe")
        print(f"  yt-dlp.exe copié")

    # ffmpeg + ffprobe
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        src = FFMPEG_DIR / name
        if src.exists():
            shutil.copy2(src, bin_dir / name)
            print(f"  {name} copié ({src.stat().st_size // 1024 // 1024} Mo)")
        else:
            print(f"  ATTENTION: {name} introuvable à {src}")

    # node.exe
    if NODE_EXE.exists():
        shutil.copy2(NODE_EXE, bin_dir / "node.exe")
        print(f"  node.exe copié")

    # 3. Créer les dossiers vides nécessaires
    print("[3/4] Création des dossiers...")
    (DIST / "downloads").mkdir(exist_ok=True)
    (DIST / "covers").mkdir(exist_ok=True)

    # 4. Résumé
    total_size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n[4/4] Build terminé !")
    print(f"  Dossier : {DIST}")
    print(f"  Taille totale : {total_size // 1024 // 1024} Mo")
    print(f"\nPour tester : lance {final_exe}")


if __name__ == "__main__":
    main()
