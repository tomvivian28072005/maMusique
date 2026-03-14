"""
Script de build — Crée le dossier distribuable + installeur maMusique.
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
    print("[1/5] PyInstaller...")
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
    internal = DIST / "_internal"
    for name in ("index.html", "database.py", "logo.svg"):
        src = internal / name
        if src.exists():
            shutil.copy2(src, DIST / name)
            print(f"  {name} copié vers racine")

    # 2. Copier les outils externes dans bin/
    print("[2/5] Copie des outils externes dans bin/...")
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

    # 3. Créer les dossiers vides + nettoyer les fichiers perso
    print("[3/5] Nettoyage et préparation...")
    (DIST / "downloads").mkdir(exist_ok=True)
    (DIST / "covers").mkdir(exist_ok=True)

    # Supprimer les fichiers qui ne doivent pas être distribués
    for f in ("music.db", "maMusique.log", "cookies.txt"):
        p = DIST / f
        if p.exists():
            p.unlink()
            print(f"  {f} supprimé du build")

    # 4. Créer l'installeur Inno Setup
    print("[4/5] Création de l'installeur...")
    iss_path = ROOT / "installer.iss"
    if not iss_path.exists():
        print("  ATTENTION: installer.iss introuvable, installeur non créé")
    else:
        iscc = Path(r"C:\Users\tomvi\AppData\Local\Programs\Inno Setup 6\ISCC.exe")
        if not iscc.exists():
            print("  ATTENTION: Inno Setup non installé, installeur non créé")
            print("  Installe-le via: winget install JRSoftware.InnoSetup")
        else:
            subprocess.run([str(iscc), str(iss_path)], check=True)
            print("  Installeur créé !")

    # 5. Résumé
    total_size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n[5/5] Build terminé !")
    print(f"  Dossier : {DIST}")
    print(f"  Taille totale : {total_size // 1024 // 1024} Mo")
    installer = ROOT / "dist" / "maMusique-setup.exe"
    if installer.exists():
        print(f"  Installeur : {installer} ({installer.stat().st_size // 1024 // 1024} Mo)")


if __name__ == "__main__":
    main()
