"""
Script de build — Crée le serveur Python packagé pour Electron.
Usage: python build.py
"""
import subprocess
import shutil
import sys
import zipfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist" / "python-server"

# URL ffmpeg essentials (gyan.dev) — ~100 Mo au lieu de 400 Mo pour full
FFMPEG_ESSENTIALS_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_CACHE = ROOT / "dist" / "ffmpeg-essentials.zip"

NODE_EXE = Path(r"C:\jeu + application\utilitaire\programation\node.exe")


def download_ffmpeg_essentials():
    """Télécharge ffmpeg essentials si pas déjà en cache."""
    if FFMPEG_CACHE.exists():
        print(f"  ffmpeg essentials déjà en cache ({FFMPEG_CACHE.stat().st_size // 1024 // 1024} Mo)")
        return FFMPEG_CACHE

    print(f"  Téléchargement ffmpeg essentials...")
    FFMPEG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(FFMPEG_ESSENTIALS_URL, FFMPEG_CACHE)
    print(f"  Téléchargé ({FFMPEG_CACHE.stat().st_size // 1024 // 1024} Mo)")
    return FFMPEG_CACHE


def extract_ffmpeg_exe(zip_path, dest_dir):
    """Extrait uniquement ffmpeg.exe du zip (pas ffprobe, pas ffplay)."""
    with zipfile.ZipFile(zip_path, 'r') as z:
        for member in z.namelist():
            if member.endswith('/ffmpeg.exe'):
                # Extraire dans un dossier temp puis déplacer
                data = z.read(member)
                out = dest_dir / "ffmpeg.exe"
                out.write_bytes(data)
                print(f"  ffmpeg.exe extrait ({len(data) // 1024 // 1024} Mo)")
                return
    print("  ATTENTION: ffmpeg.exe introuvable dans le zip!")


def main():
    print("=== Build Clom (serveur Python) ===\n")

    # 1. PyInstaller
    print("[1/4] PyInstaller...")
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--noconsole",
        "--name", "Clom",
        "--icon", "NONE",
        "--distpath", str(ROOT / "dist" / "_pyinstaller"),
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

    # Déplacer le résultat PyInstaller vers dist/python-server/
    pyinstaller_out = ROOT / "dist" / "_pyinstaller" / "Clom"
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.move(str(pyinstaller_out), str(DIST))
    temp_dir = ROOT / "dist" / "_pyinstaller"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    # Renommer launcher.exe → Clom.exe
    launcher = DIST / "launcher.exe"
    final_exe = DIST / "Clom.exe"
    if launcher.exists() and not final_exe.exists():
        launcher.rename(final_exe)

    # Copier les fichiers data de _internal/ vers la racine du dist
    internal = DIST / "_internal"
    for name in ("index.html", "database.py", "logo.svg"):
        src = internal / name
        if src.exists():
            shutil.copy2(src, DIST / name)
            print(f"  {name} copié vers racine")

    # 2. Outils externes dans bin/
    print("[2/4] Copie des outils externes dans bin/...")
    bin_dir = DIST / "bin"
    bin_dir.mkdir(exist_ok=True)

    # yt-dlp
    ytdlp_src = ROOT / "bin_standalone" / "yt-dlp.exe"
    if not ytdlp_src.exists():
        ytdlp_src = ROOT / "venv" / "Scripts" / "yt-dlp.exe"
    if ytdlp_src.exists():
        shutil.copy2(ytdlp_src, bin_dir / "yt-dlp.exe")
        print(f"  yt-dlp.exe copié ({ytdlp_src.stat().st_size // 1024 // 1024} Mo)")

    # ffmpeg essentials (téléchargé automatiquement, sans ffprobe)
    print("  ffmpeg essentials...")
    zip_path = download_ffmpeg_essentials()
    extract_ffmpeg_exe(zip_path, bin_dir)

    # node.exe
    if NODE_EXE.exists():
        shutil.copy2(NODE_EXE, bin_dir / "node.exe")
        print(f"  node.exe copié ({NODE_EXE.stat().st_size // 1024 // 1024} Mo)")

    # 3. Nettoyage
    print("[3/4] Nettoyage et préparation...")
    (DIST / "downloads").mkdir(exist_ok=True)
    (DIST / "covers").mkdir(exist_ok=True)

    for f in ("music.db", "Clom.log", "cookies.txt"):
        p = DIST / f
        if p.exists():
            p.unlink()
            print(f"  {f} supprimé du build")

    # 4. Résumé
    total_size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n[4/4] Build serveur Python terminé !")
    print(f"  Dossier : {DIST}")
    print(f"  Taille totale : {total_size // 1024 // 1024} Mo")
    print(f"\n  Pour construire l'installeur Electron :")
    print(f"  npx electron-builder --win")


if __name__ == "__main__":
    main()
