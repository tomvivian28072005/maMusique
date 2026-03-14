import os
import re
import csv
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from io import StringIO

# Logging : console + fichier rotatif
logger = logging.getLogger("Clom")
logger.setLevel(logging.INFO)
_console_h = logging.StreamHandler()
_console_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
_file_h = logging.FileHandler("Clom.log", encoding="utf-8")
_file_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_console_h)
logger.addHandler(_file_h)

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import case
from sqlalchemy.orm import Session

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1

from database import (
    init_db, get_db, add_track, get_tracks, delete_track, Track,
    Playlist, PlaylistTrack, get_playlists, create_playlist, update_playlist, delete_playlist,
    add_track_to_playlist, remove_track_from_playlist, get_playlist_tracks,
)

# Répertoire de base (compatible PyInstaller)
import sys
_BASE = Path(os.path.dirname(sys.executable)) if getattr(sys, 'frozen', False) else Path(__file__).parent

# Outils externes — cherche d'abord dans bin/ local, sinon chemins de dev
_BIN_DIR = _BASE / "bin"

def _find_bin(name, dev_fallback):
    """Cherche un exécutable dans bin/ puis dans le PATH, sinon fallback dev."""
    local = _BIN_DIR / name
    if local.exists():
        return str(local)
    # Chercher dans le PATH système
    import shutil
    found = shutil.which(name)
    if found:
        return found
    return dev_fallback

YTDLP_BIN = _find_bin("yt-dlp.exe", str(_BASE / "venv" / "Scripts" / "yt-dlp.exe"))
FFMPEG_DIR = str(_BIN_DIR) if (_BIN_DIR / "ffmpeg.exe").exists() else r"C:\Users\tomvi\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin"
NODE_PATH = _find_bin("node.exe", r"C:\jeu + application\utilitaire\programation\node.exe")
_cookies_path = _BASE / "cookies.txt"
COOKIES_FILE = str(_cookies_path) if _cookies_path.exists() else None

# Empêcher l'ouverture d'une fenêtre console pour les sous-processus (yt-dlp, ffmpeg…)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def _ytdlp_base_args(*, use_js=True):
    """Arguments communs à toutes les commandes yt-dlp."""
    args = [YTDLP_BIN, "--ffmpeg-location", FFMPEG_DIR]
    if use_js:
        args += ["--js-runtimes", f"node:{NODE_PATH}", "--remote-components", "ejs:github"]
    if COOKIES_FILE:
        args += ["--cookies", COOKIES_FILE]
    return args

logger.info(f"Base dir: {_BASE}")
logger.info(f"yt-dlp:   {YTDLP_BIN} (exists: {Path(YTDLP_BIN).exists()})")
logger.info(f"ffmpeg:   {FFMPEG_DIR} (exists: {Path(FFMPEG_DIR, 'ffmpeg.exe').exists()})")
logger.info(f"node:     {NODE_PATH} (exists: {Path(NODE_PATH).exists()})")
logger.info(f"cookies:  {COOKIES_FILE or 'non trouvé (ignoré)'}")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
COVERS_DIR = Path("covers")
COVERS_DIR.mkdir(exist_ok=True)

# Lock pour protéger les dicts de statut partagés entre threads
_status_lock = threading.Lock()

# Track active downloads: url -> status/progress
active_downloads: dict[str, dict] = {}

# Track search-based downloads: key -> status/progress
search_downloads: dict[str, dict] = {}

# Track replacements: track_id -> status/progress
active_replacements: dict[int, dict] = {}


def _cleanup_status_dicts():
    """Supprime les entrées terminées (done/error) après 1h."""
    while True:
        time.sleep(3600)
        now = time.time()
        with _status_lock:
            for d in (active_downloads, search_downloads):
                expired = [k for k, v in d.items()
                           if v.get("status") in ("done", "error") and now - v.get("_ts", now) > 3600]
                for k in expired:
                    del d[k]
            expired_r = [k for k, v in active_replacements.items()
                         if v.get("status") in ("done", "error") and now - v.get("_ts", now) > 3600]
            for k in expired_r:
                del active_replacements[k]

threading.Thread(target=_cleanup_status_dicts, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


APP_VERSION = "0.1.0"

app = FastAPI(title="Clom", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")
app.mount("/covers", StaticFiles(directory="covers"), name="covers")


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str


class SearchDownloadRequest(BaseModel):
    title: str
    artist: str = ""


class RenameRequest(BaseModel):
    title: Optional[str] = None
    artist: Optional[str] = None
    youtube_url: Optional[str] = None
    volume_coeff: Optional[float] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    clear_start_time: bool = False
    clear_end_time: bool = False
    cover_zoom: Optional[float] = None
    cover_offset_x: Optional[float] = None
    cover_offset_y: Optional[float] = None
    clear_cover: bool = False


class PlaylistCreateRequest(BaseModel):
    name: str


class PlaylistUpdateRequest(BaseModel):
    name: Optional[str] = None
    cover_zoom: Optional[float] = None
    cover_offset_x: Optional[float] = None
    cover_offset_y: Optional[float] = None
    clear_cover: bool = False


class PlaylistAddTrackRequest(BaseModel):
    track_id: int


class TrackResponse(BaseModel):
    id: int
    title: str
    artist: str
    file_path: str
    youtube_url: Optional[str]
    added_at: str
    play_count: int = 0
    volume_coeff: float = 1.0
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    cover_path: Optional[str] = None
    cover_zoom: float = 1.0
    cover_offset_x: float = 0.0
    cover_offset_y: float = 0.0

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def extract_metadata(filepath: str) -> tuple[str, str]:
    """Extract title and artist from MP3 tags, fallback to filename."""
    try:
        audio = MP3(filepath, ID3=ID3)
        title = str(audio.tags.get("TIT2", "")).strip() or None
        artist = str(audio.tags.get("TPE1", "")).strip() or None
        return title, artist
    except Exception as e:
        logger.debug("Impossible de lire les tags ID3 de %s: %s", filepath, e)
        return None, None


def inject_metadata(filepath: str, title: str, artist: str):
    """Write title and artist into MP3 ID3 tags."""
    try:
        try:
            tags = ID3(filepath)
        except Exception:
            tags = ID3()  # Fichier sans tags existants
        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags.save(filepath)
    except Exception as e:
        logger.warning("Échec injection métadonnées pour %s: %s", filepath, e)


def split_artist_title_from_filename(filename: str) -> tuple[str, str]:
    stem = Path(filename).stem.strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        artist = artist.strip() or "Unknown"
        title = title.strip() or stem
        return title, artist
    return stem or "Titre inconnu", "Unknown"


def unique_download_filename(base_name: str) -> str:
    safe_name = sanitize_filename(base_name)
    stem = Path(safe_name).stem
    ext = Path(safe_name).suffix.lower() or ".mp3"
    candidate = f"{stem}{ext}"
    idx = 1
    while (DOWNLOADS_DIR / candidate).exists():
        candidate = f"{stem}_{idx}{ext}"
        idx += 1
    return candidate


def clean_youtube_url(url: str) -> str:
    """Nettoie l'URL YouTube pour ne garder que la vidéo (supprime list, start_radio, etc.)."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    
    # Handle youtu.be links
    if parsed.netloc == 'youtu.be':
        # The video ID is in the path
        video_id = parsed.path.lstrip('/')
        return f"https://www.youtube.com/watch?v={video_id}"
        
    params = parse_qs(parsed.query)
    # Ne garder que le paramètre 'v' (ID de la vidéo)
    clean_params = {}
    if "v" in params:
        clean_params["v"] = params["v"][0]
    else:
        # If no 'v' and not youtu.be, just return original but stripped of other junk if possible
        return url.strip()
        
    clean_query = urlencode(clean_params)
    return urlunparse(parsed._replace(query=clean_query))


def download_audio(url: str, db: Session) -> dict:
    """Download audio from YouTube URL using yt-dlp CLI subprocess."""
    url = clean_youtube_url(url)
    active_downloads[url] = {"status": "downloading", "progress": 0}

    output_template = str(DOWNLOADS_DIR / "%(uploader)s - %(title)s.%(ext)s")

    # Étape 1 : Récupérer les métadonnées (titre, artiste) via --dump-json
    meta_extra = ["--dump-json", "--no-download", "--no-playlist", url]
    meta_result = subprocess.run(_ytdlp_base_args() + meta_extra, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
    if meta_result.returncode != 0 or not meta_result.stdout.strip():
        logger.warning(f"yt-dlp meta failed (rc={meta_result.returncode}), retrying without JS flags...")
        meta_result = subprocess.run(_ytdlp_base_args(use_js=False) + meta_extra, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
    if meta_result.returncode != 0:
        raise RuntimeError(meta_result.stderr.strip().split("\n")[-1])

    info = json.loads(meta_result.stdout)

    active_downloads[url]["progress"] = 10

    # Étape 2 : Télécharger + convertir en MP3 via CLI
    dl_extra = [
        "--format", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192",
        "-o", output_template,
        "--no-overwrites",
        "--no-playlist",
        url,
    ]
    existing_before = set(DOWNLOADS_DIR.glob("*.mp3"))
    dl_result = subprocess.run(_ytdlp_base_args() + dl_extra, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
    if dl_result.returncode != 0:
        logger.warning(f"yt-dlp download failed (rc={dl_result.returncode}), retrying without JS flags...")
        dl_result = subprocess.run(_ytdlp_base_args(use_js=False) + dl_extra, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
    if dl_result.returncode != 0:
        raise RuntimeError(dl_result.stderr.strip().split("\n")[-1])

    active_downloads[url]["status"] = "processing"
    active_downloads[url]["progress"] = 90

    # Trouver le fichier MP3 résultant
    uploader = sanitize_filename(info.get("uploader") or "Unknown")
    title_raw = sanitize_filename(info.get("title") or "Unknown")
    expected_name = f"{uploader} - {title_raw}.mp3"
    filepath = str(DOWNLOADS_DIR / expected_name)

    if not os.path.exists(filepath):
        new_files = set(DOWNLOADS_DIR.glob("*.mp3")) - existing_before
        if new_files:
            filepath = str(next(iter(new_files)))
        else:
            raise FileNotFoundError("Fichier MP3 introuvable après téléchargement.")

    # Métadonnées ID3
    tag_title, tag_artist = extract_metadata(filepath)
    final_title = tag_title or info.get("title") or title_raw
    final_artist = tag_artist or info.get("uploader") or uploader
    inject_metadata(filepath, final_title, final_artist)

    rel_path = "/downloads/" + Path(filepath).name
    track = add_track(db, title=final_title, artist=final_artist,
                      file_path=rel_path, youtube_url=url)

    active_downloads[url] = {"status": "done", "track_id": track.id, "_ts": time.time()}
    return {"track_id": track.id, "title": final_title, "artist": final_artist}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_path = Path("index.html")
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/version")
async def api_version():
    return {"version": APP_VERSION}


@app.post("/api/shutdown")
async def api_shutdown():
    """Arrête le serveur proprement (appelé quand l'utilisateur ferme l'onglet)."""
    if not getattr(sys, 'frozen', False):
        return {"status": "ignored (dev mode)"}
    import signal
    logger.info("Shutdown requested by client")
    os.kill(os.getpid(), signal.SIGTERM)
    return {"status": "shutting down"}


@app.post("/api/update")
async def api_update():
    """Télécharge la dernière release et lance l'installeur silencieusement."""
    import tempfile
    import signal
    import urllib.request

    # 1. Récupérer l'URL du .exe depuis GitHub
    logger.info("Update: fetching latest release info...")
    req = urllib.request.Request(
        "https://api.github.com/repos/tomvivian28072005/maMusique/releases/latest",
        headers={"User-Agent": "Clom-updater"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        release = json.loads(resp.read())

    assets = release.get("assets", [])
    setup_asset = next((a for a in assets if a["name"].endswith("-setup.exe")), None)
    if not setup_asset:
        raise HTTPException(status_code=404, detail="Installeur introuvable dans la release.")

    download_url = setup_asset["browser_download_url"]
    logger.info(f"Update: downloading {download_url}...")

    # 2. Télécharger dans un fichier temporaire
    tmp_dir = tempfile.gettempdir()
    setup_path = os.path.join(tmp_dir, setup_asset["name"])
    urllib.request.urlretrieve(download_url, setup_path)
    logger.info(f"Update: downloaded to {setup_path}")

    # 3. Créer un script batch qui attend, lance l'installeur, puis nettoie
    bat_path = os.path.join(tmp_dir, "clom_update.bat")
    with open(bat_path, "w") as f:
        f.write(f'@echo off\n')
        f.write(f'timeout /t 3 /nobreak >nul\n')
        f.write(f'start "" "{setup_path}" /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS\n')
        f.write(f'del "%~f0"\n')

    # 4. Lancer le script batch détaché
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        ["cmd", "/c", bat_path],
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW
    )
    logger.info("Update: batch script launched, shutting down...")

    # 5. Arrêter le serveur
    os.kill(os.getpid(), signal.SIGTERM)
    return {"status": "updating"}


@app.post("/api/download")
async def api_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = clean_youtube_url(req.url.strip())
    if not url or "v=" not in url:
        raise HTTPException(status_code=400, detail="URL YouTube invalide.")

    if url in active_downloads and active_downloads[url].get("status") not in ("done", "error"):
        return {"message": "Download already in progress.", "status": active_downloads[url]}

    def run_download():
        # Crée une nouvelle session DB dans la tâche de fond (la session de la requête est déjà fermée)
        from database import SessionLocal
        db = SessionLocal()
        try:
            download_audio(url, db)
        except Exception as e:
            active_downloads[url] = {"status": "error", "message": str(e), "_ts": time.time()}
        finally:
            db.close()

    background_tasks.add_task(run_download)
    active_downloads[url] = {"status": "queued", "progress": 0}
    return {"message": "Download started.", "url": url}


@app.get("/api/download/status")
async def api_download_status(url: str):
    clean_url = clean_youtube_url(url)
    with _status_lock:
        status = active_downloads.get(clean_url, {"status": "unknown"}).copy()
    return status


@app.get("/api/tracks/{track_id}/replace-status")
async def api_track_replacement_status(track_id: int):
    with _status_lock:
        status = active_replacements.get(track_id, {"status": "none"}).copy()
    return status


def replace_track_worker(track_id: int, url: str):
    """Background task to download new audio and replace existing track file."""
    url = clean_youtube_url(url)
    from database import SessionLocal, Track
    db = SessionLocal()
    try:
        active_replacements[track_id] = {"status": "downloading", "progress": 10}
        
        # Step 1: Get info
        meta_extra = ["--dump-json", "--no-download", "--no-playlist", url]
        meta_result = subprocess.run(_ytdlp_base_args() + meta_extra, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
        if meta_result.returncode != 0 or not meta_result.stdout.strip():
            meta_result = subprocess.run(_ytdlp_base_args(use_js=False) + meta_extra, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
        if meta_result.returncode != 0:
            raise RuntimeError(f"Metadata error: {meta_result.stderr.strip()}")

        info = json.loads(meta_result.stdout)
        active_replacements[track_id]["progress"] = 30

        # Step 2: Download
        temp_name = f"replace_{track_id}_{int(time.time())}.%(ext)s"
        output_template = str(DOWNLOADS_DIR / temp_name)

        dl_extra = [
            "--format", "bestaudio/best",
            "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192",
            "-o", output_template,
            "--no-playlist",
            url
        ]
        dl_result = subprocess.run(_ytdlp_base_args() + dl_extra, capture_output=True, text=True, timeout=1800, creationflags=_NO_WINDOW)
        if dl_result.returncode != 0:
            dl_result = subprocess.run(_ytdlp_base_args(use_js=False) + dl_extra, capture_output=True, text=True, timeout=1800, creationflags=_NO_WINDOW)
        if dl_result.returncode != 0:
            raise RuntimeError(f"Download error: {dl_result.stderr.strip() or 'Unknown error'}")
            
        active_replacements[track_id]["status"] = "processing"
        active_replacements[track_id]["progress"] = 80
        
        # Find the new file
        mp3_files = sorted(DOWNLOADS_DIR.glob(f"replace_{track_id}_*.mp3"), key=os.path.getmtime, reverse=True)
        if not mp3_files:
            raise FileNotFoundError("Downloaded MP3 not found.")
        new_file_abs = mp3_files[0]
        
        # Step 3: Update DB and delete old file
        track = db.query(Track).filter(Track.id == track_id).first()
        if not track:
            new_file_abs.unlink()
            raise RuntimeError("Track not found in database anymore.")
            
        old_file_rel = track.file_path.lstrip("/")
        old_file_abs = (Path(os.getcwd()) / old_file_rel).absolute()
        
        # Final filename based on track info + unique part to avoid Windows file locks
        timestamp = int(time.time())
        final_name = f"{sanitize_filename(track.artist)} - {sanitize_filename(track.title)}_{timestamp}.mp3"
        final_path_abs = (DOWNLOADS_DIR / final_name).absolute()
        
        # rename downloaded file to final name
        os.rename(str(new_file_abs), str(final_path_abs))
        
        # Update track info
        track.file_path = "/downloads/" + final_path_abs.name
        track.youtube_url = url
        
        # Inject metadata to the new file
        inject_metadata(str(final_path_abs), track.title, track.artist)
        
        db.commit()
        
        # Try to delete the old file AFTER DB commit
        if old_file_abs.exists() and old_file_abs != final_path_abs:
            try:
                old_file_abs.unlink()
            except OSError as e:
                logger.warning("Impossible de supprimer l'ancien fichier %s: %s", old_file_abs, e)
                
        active_replacements[track_id] = {"status": "done", "progress": 100, "_ts": time.time()}
        
    except Exception as e:
        active_replacements[track_id] = {"status": "error", "message": str(e), "_ts": time.time()}
    finally:
        db.close()


@app.post("/api/search-download")
async def api_search_download(req: SearchDownloadRequest, background_tasks: BackgroundTasks):
    title = req.title.strip()
    artist = req.artist.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Le titre est requis.")
    key = f"{title}||{artist}".lower()
    if key in search_downloads and search_downloads[key].get("status") not in ("done", "error"):
        return {"message": "Recherche déjà en cours.", "status": search_downloads[key]}

    def run_search_download():
        from database import SessionLocal
        db = SessionLocal()
        try:
            search_downloads[key] = {"status": "searching", "progress": 10}
            search_query = f"ytsearch1:{title} {artist}" if artist else f"ytsearch1:{title}"
            extra_args = ["--dump-json", "--no-download", "--no-playlist", search_query]
            meta_cmd = _ytdlp_base_args() + extra_args
            logger.info(f"yt-dlp search cmd: {meta_cmd}")
            meta_result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
            logger.info(f"yt-dlp search rc={meta_result.returncode} stdout_len={len(meta_result.stdout)} stderr_len={len(meta_result.stderr)}")
            # Fallback : retry sans --js-runtimes / --remote-components
            if meta_result.returncode != 0 or not meta_result.stdout.strip():
                logger.warning(f"yt-dlp search failed (rc={meta_result.returncode}), retrying without JS flags...")
                if meta_result.stderr:
                    logger.warning(f"yt-dlp stderr: {meta_result.stderr.strip()}")
                meta_cmd2 = _ytdlp_base_args(use_js=False) + extra_args
                meta_result = subprocess.run(meta_cmd2, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
                logger.info(f"yt-dlp fallback rc={meta_result.returncode} stdout_len={len(meta_result.stdout)}")
            if meta_result.returncode != 0 or not meta_result.stdout.strip():
                err = meta_result.stderr.strip().split("\n")[-1] if meta_result.stderr.strip() else "Aucun résultat"
                logger.error(f"yt-dlp search final stderr: {meta_result.stderr}")
                raise RuntimeError(f"Recherche échouée : {err}")

            info = json.loads(meta_result.stdout)
            video_url = info.get("webpage_url") or info.get("url", "")

            search_downloads[key] = {"status": "downloading", "progress": 40}

            clean_artist = sanitize_filename(artist or info.get("uploader") or "Unknown")
            clean_title = sanitize_filename(title)
            clean_name = f"{clean_artist} - {clean_title}.mp3"
            final_path = DOWNLOADS_DIR / clean_name

            if not final_path.exists():
                output_template = str(DOWNLOADS_DIR / "%(uploader)s - %(title)s.%(ext)s")
                dl_extra = [
                    "--format", "bestaudio/best",
                    "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192",
                    "-o", output_template,
                    "--no-overwrites",
                    "--no-playlist",
                    video_url,
                ]
                existing_before = set(DOWNLOADS_DIR.glob("*.mp3"))
                dl_cmd = _ytdlp_base_args() + dl_extra
                dl_result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
                if dl_result.returncode != 0:
                    logger.warning(f"yt-dlp download failed (rc={dl_result.returncode}), retrying without JS flags...")
                    dl_cmd2 = _ytdlp_base_args(use_js=False) + dl_extra
                    dl_result = subprocess.run(dl_cmd2, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
                if dl_result.returncode != 0:
                    raise RuntimeError(f"Téléchargement échoué : {dl_result.stderr.strip().split(chr(10))[-1]}")

                yt_name = f"{sanitize_filename(info.get('uploader') or 'Unknown')} - {sanitize_filename(info.get('title') or 'Unknown')}.mp3"
                yt_path = DOWNLOADS_DIR / yt_name
                if not yt_path.exists():
                    new_files = set(DOWNLOADS_DIR.glob("*.mp3")) - existing_before
                    if not new_files:
                        raise FileNotFoundError("MP3 introuvable")
                    yt_path = next(iter(new_files))
                os.rename(str(yt_path), str(final_path))

            search_downloads[key] = {"status": "processing", "progress": 85}

            final_title = title or info.get("title") or "Unknown"
            final_artist = artist or info.get("uploader") or "Unknown"
            inject_metadata(str(final_path), final_title, final_artist)

            rel_path = "/downloads/" + final_path.name
            existing = db.query(Track).filter(Track.file_path == rel_path).first()
            if existing:
                track_id = existing.id
            else:
                track = add_track(db, title=final_title, artist=final_artist,
                                  file_path=rel_path, youtube_url=video_url)
                track_id = track.id

            search_downloads[key] = {"status": "done", "progress": 100, "track_id": track_id,
                                     "title": final_title, "artist": final_artist, "_ts": time.time()}
        except Exception as e:
            logger.error(f"search-download failed for '{title} {artist}': {e}")
            search_downloads[key] = {"status": "error", "message": str(e), "_ts": time.time()}
        finally:
            db.close()

    background_tasks.add_task(run_search_download)
    search_downloads[key] = {"status": "queued", "progress": 0}
    return {"message": "Recherche lancée.", "key": key}


@app.get("/api/search-download/status")
async def api_search_download_status(key: str):
    with _status_lock:
        status = search_downloads.get(key, {"status": "unknown"}).copy()
    return status


@app.get("/api/tracks", response_model=list[TrackResponse])
async def api_get_tracks(
    search: Optional[str] = None,
    sort_by: Optional[str] = "added_at",
    db: Session = Depends(get_db),
):
    tracks = get_tracks(db, search=search, sort_by=sort_by)
    return [
        TrackResponse(
            id=t.id,
            title=t.title,
            artist=t.artist,
            file_path=t.file_path,
            youtube_url=t.youtube_url,
            added_at=t.added_at.isoformat(),
            play_count=t.play_count or 0,
            volume_coeff=t.volume_coeff if t.volume_coeff is not None else 1.0,
            start_time=t.start_time,
            end_time=t.end_time,
            cover_path=t.cover_path,
            cover_zoom=t.cover_zoom if t.cover_zoom is not None else 1.0,
            cover_offset_x=t.cover_offset_x if t.cover_offset_x is not None else 0.0,
            cover_offset_y=t.cover_offset_y if t.cover_offset_y is not None else 0.0,
        )
        for t in tracks
    ]


@app.delete("/api/tracks/{track_id}")
async def api_delete_track(track_id: int, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found.")

    # Delete the physical file
    try:
        file_abs = Path(track.file_path.lstrip("/"))
        if file_abs.exists():
            file_abs.unlink()
    except OSError as e:
        logger.warning("Impossible de supprimer le fichier %s: %s", track.file_path, e)

    delete_track(db, track_id)
    return {"message": "Track deleted."}


@app.patch("/api/tracks/{track_id}")
async def api_rename_track(track_id: int, req: RenameRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found.")

    if isinstance(req.title, str):
        track.title = req.title.strip()
    if isinstance(req.artist, str):
        track.artist = req.artist.strip()
    
    if req.youtube_url and req.youtube_url.strip():
        url = clean_youtube_url(req.youtube_url.strip())
        # Check if it looks like a valid youtube watch URL after cleaning
        if "v=" in url or "youtu.be/" in url:
            background_tasks.add_task(replace_track_worker, track_id, url)

    if req.volume_coeff is not None:
        track.volume_coeff = max(0.5, min(2.0, req.volume_coeff))
    if req.clear_start_time:
        track.start_time = None
    elif req.start_time is not None:
        track.start_time = max(0, req.start_time)
    if req.clear_end_time:
        track.end_time = None
    elif req.end_time is not None:
        track.end_time = max(0, req.end_time)
    if req.clear_cover:
        track.cover_path = None
        track.cover_zoom = 1.0
        track.cover_offset_x = 0.0
        track.cover_offset_y = 0.0
    else:
        if req.cover_zoom is not None:
            track.cover_zoom = max(0.5, min(3.0, req.cover_zoom))
        if req.cover_offset_x is not None:
            track.cover_offset_x = max(-1.0, min(1.0, req.cover_offset_x))
        if req.cover_offset_y is not None:
            track.cover_offset_y = max(-1.0, min(1.0, req.cover_offset_y))

    # Update ID3 tags in the MP3 file
    try:
        file_abs = Path(track.file_path.lstrip("/"))
        if file_abs.exists():
            inject_metadata(str(file_abs), track.title, track.artist)
    except Exception as e:
        logger.warning("Échec mise à jour métadonnées pour track %d: %s", track_id, e)

    db.commit()
    return {"message": "Track updated.", "title": track.title, "artist": track.artist,
            "volume_coeff": track.volume_coeff, "start_time": track.start_time, "end_time": track.end_time,
            "cover_zoom": track.cover_zoom, "cover_offset_x": track.cover_offset_x, "cover_offset_y": track.cover_offset_y}


@app.post("/api/tracks/{track_id}/cover")
async def api_upload_track_cover(track_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found.")
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
        raise HTTPException(status_code=400, detail="Format non supporté (jpg, png, webp, mp4).")
    # Remove old cover files with different extensions
    for old_ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
        old_file = COVERS_DIR / f"track_{track_id}{old_ext}"
        if old_file.exists() and old_ext != ext:
            old_file.unlink(missing_ok=True)
    cover_name = f"track_{track_id}{ext}"
    cover_file = COVERS_DIR / cover_name
    content = await file.read()
    with open(cover_file, "wb") as f:
        f.write(content)
    cover_url = f"/covers/{cover_name}?t={int(time.time())}"
    track.cover_path = cover_url
    db.commit()
    return {"cover_path": cover_url}


@app.post("/api/tracks/{track_id}/played")
async def api_track_played(track_id: int, db: Session = Depends(get_db)):
    rows = db.query(Track).filter(Track.id == track_id).update(
        {Track.play_count: case((Track.play_count == None, 1), else_=Track.play_count + 1)},
        synchronize_session="fetch"
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Track not found.")
    db.commit()
    track = db.query(Track).filter(Track.id == track_id).first()
    return {"play_count": track.play_count}


@app.post("/api/tracks/{track_id}/toggle-favorite")
async def api_toggle_favorite(track_id: int, db: Session = Depends(get_db)):
    fav_playlist = db.query(Playlist).filter(Playlist.name == "Coup de cœur", Playlist.is_default == 1).first()
    if not fav_playlist:
        raise HTTPException(status_code=404, detail="Playlist Coup de cœur introuvable.")
    existing = db.query(PlaylistTrack).filter(
        PlaylistTrack.playlist_id == fav_playlist.id,
        PlaylistTrack.track_id == track_id
    ).first()
    if existing:
        db.delete(existing)
        db.commit()
        return {"favorite": False}
    else:
        db.add(PlaylistTrack(playlist_id=fav_playlist.id, track_id=track_id))
        db.commit()
        return {"favorite": True}


@app.get("/api/favorites")
async def api_get_favorites(db: Session = Depends(get_db)):
    fav_playlist = db.query(Playlist).filter(Playlist.name == "Coup de cœur", Playlist.is_default == 1).first()
    if not fav_playlist:
        return {"playlist_id": None, "track_ids": []}
    entries = db.query(PlaylistTrack.track_id).filter(PlaylistTrack.playlist_id == fav_playlist.id).all()
    return {"playlist_id": fav_playlist.id, "track_ids": [e[0] for e in entries]}


# ── Playlist Routes ──────────────────────────────────────────────────────────

@app.get("/api/playlists")
async def api_get_playlists(db: Session = Depends(get_db)):
    playlists = get_playlists(db)
    result = []
    for p in playlists:
        count = len(p.entries)
        result.append({
            "id": p.id,
            "name": p.name,
            "cover_path": p.cover_path,
            "cover_zoom": p.cover_zoom if p.cover_zoom is not None else 1.0,
            "cover_offset_x": p.cover_offset_x if p.cover_offset_x is not None else 0.0,
            "cover_offset_y": p.cover_offset_y if p.cover_offset_y is not None else 0.0,
            "is_default": bool(p.is_default),
            "track_count": count,
            "position": p.position if p.position is not None else 999,
            "created_at": p.created_at.isoformat(),
        })
    return result


class PlaylistReorderRequest(BaseModel):
    playlist_ids: list[int]


@app.post("/api/playlists/reorder")
async def api_reorder_playlists(req: PlaylistReorderRequest, db: Session = Depends(get_db)):
    for i, pid in enumerate(req.playlist_ids):
        pl = db.query(Playlist).filter(Playlist.id == pid).first()
        if pl:
            pl.position = i
    db.commit()
    return {"message": "OK"}


@app.post("/api/playlists")
async def api_create_playlist(req: PlaylistCreateRequest, db: Session = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom ne peut pas être vide.")
    playlist = create_playlist(db, name)
    return {"id": playlist.id, "name": playlist.name}


@app.patch("/api/playlists/{playlist_id}")
async def api_update_playlist(playlist_id: int, req: PlaylistUpdateRequest, db: Session = Depends(get_db)):
    if req.clear_cover:
        pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
        if pl and pl.cover_path:
            cover_file = Path(pl.cover_path.lstrip("/"))
            if cover_file.exists():
                cover_file.unlink()
        playlist = update_playlist(db, playlist_id, name=req.name, cover_path="")
        if playlist:
            playlist.cover_path = None
            playlist.cover_zoom = 1.0
            playlist.cover_offset_x = 0.0
            playlist.cover_offset_y = 0.0
            db.commit()
            db.refresh(playlist)
    else:
        playlist = update_playlist(db, playlist_id, name=req.name,
                                   cover_zoom=req.cover_zoom, cover_offset_x=req.cover_offset_x,
                                   cover_offset_y=req.cover_offset_y)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist introuvable.")
    return {"id": playlist.id, "name": playlist.name, "cover_path": playlist.cover_path,
            "cover_zoom": playlist.cover_zoom, "cover_offset_x": playlist.cover_offset_x,
            "cover_offset_y": playlist.cover_offset_y}


@app.post("/api/playlists/{playlist_id}/cover")
async def api_upload_playlist_cover(playlist_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist introuvable.")
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
        raise HTTPException(status_code=400, detail="Format non supporté (jpg, png, webp, mp4).")
    # Remove old cover files with different extensions
    for old_ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
        old_file = COVERS_DIR / f"playlist_{playlist_id}{old_ext}"
        if old_file.exists() and old_ext != ext:
            old_file.unlink(missing_ok=True)
    cover_name = f"playlist_{playlist_id}{ext}"
    cover_file = COVERS_DIR / cover_name
    content = await file.read()
    with open(cover_file, "wb") as f:
        f.write(content)
    cover_url = f"/covers/{cover_name}?t={int(time.time())}"
    update_playlist(db, playlist_id, cover_path=cover_url)
    return {"cover_path": cover_url}


@app.delete("/api/playlists/{playlist_id}")
async def api_delete_playlist(playlist_id: int, db: Session = Depends(get_db)):
    success = delete_playlist(db, playlist_id)
    if not success:
        raise HTTPException(status_code=400, detail="Impossible de supprimer cette playlist.")
    return {"message": "Playlist supprimée."}


@app.get("/api/playlists/{playlist_id}/tracks")
async def api_get_playlist_tracks(playlist_id: int, db: Session = Depends(get_db)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist introuvable.")
    tracks = get_playlist_tracks(db, playlist_id)
    return [
        {
            "id": t["id"],
            "title": t["title"],
            "artist": t["artist"],
            "file_path": t["file_path"],
            "youtube_url": t["youtube_url"],
            "added_at": t["added_at"].isoformat(),
            "library_added_at": t["library_added_at"].isoformat() if t.get("library_added_at") else None,
            "play_count": t.get("play_count", 0),
            "volume_coeff": t.get("volume_coeff", 1.0),
            "start_time": t.get("start_time"),
            "end_time": t.get("end_time"),
            "cover_path": t.get("cover_path"),
            "cover_zoom": t.get("cover_zoom", 1.0),
            "cover_offset_x": t.get("cover_offset_x", 0.0),
            "cover_offset_y": t.get("cover_offset_y", 0.0),
        }
        for t in tracks
    ]


@app.post("/api/playlists/{playlist_id}/tracks")
async def api_add_track_to_playlist(playlist_id: int, req: PlaylistAddTrackRequest, db: Session = Depends(get_db)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist introuvable.")
    track = db.query(Track).filter(Track.id == req.track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Morceau introuvable.")
    add_track_to_playlist(db, playlist_id, req.track_id)
    return {"message": "Morceau ajouté à la playlist."}


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
async def api_remove_track_from_playlist(playlist_id: int, track_id: int, db: Session = Depends(get_db)):
    success = remove_track_from_playlist(db, playlist_id, track_id)
    if not success:
        raise HTTPException(status_code=404, detail="Morceau non trouvé dans la playlist.")
    return {"message": "Morceau retiré de la playlist."}


# ── Deezer CSV Import ────────────────────────────────────────────────────────

import_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": 0,
    "current": "",
    "log": [],  # last N messages
}


def import_worker(rows: list[dict]):
    """Background thread that downloads each track and assigns to playlists."""
    from database import SessionLocal
    db = SessionLocal()

    import_status["running"] = True
    import_status["total"] = len(rows)
    import_status["done"] = 0
    import_status["errors"] = 0
    import_status["log"] = []

    # Group rows by playlist
    playlist_cache: dict[str, int] = {}  # name -> id

    def get_or_create_playlist(name: str) -> int:
        if name in playlist_cache:
            return playlist_cache[name]
        existing = db.query(Playlist).filter(Playlist.name == name).first()
        if existing:
            playlist_cache[name] = existing.id
            return existing.id
        pl = create_playlist(db, name)
        playlist_cache[name] = pl.id
        return pl.id

    for i, row in enumerate(rows):
        title = (row.get("Track name") or row.get("titre") or "").strip()
        artist = (row.get("Artist name") or row.get("artiste") or "").strip()
        playlist_name = (row.get("Playlist name") or row.get("playlist") or "").strip()

        if not title:
            import_status["done"] += 1
            continue

        import_status["current"] = f"{title} - {artist}"
        log_msg = f"[{i+1}/{len(rows)}] {title} - {artist}"

        try:
            # Check if track already exists in DB (by title+artist match)
            existing_track = db.query(Track).filter(
                Track.title.ilike(title),
                Track.artist.ilike(artist)
            ).first()

            if existing_track:
                track_id = existing_track.id
                import_status["log"].append(f"{log_msg} → déjà en base")
            else:
                # Search YouTube: "title artist"
                search_query = f"ytsearch1:{title} {artist}"
                meta_extra = ["--dump-json", "--no-download", "--no-playlist", search_query]
                meta_result = subprocess.run(_ytdlp_base_args() + meta_extra, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
                if meta_result.returncode != 0 or not meta_result.stdout.strip():
                    meta_result = subprocess.run(_ytdlp_base_args(use_js=False) + meta_extra, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
                if meta_result.returncode != 0 or not meta_result.stdout.strip():
                    err_detail = meta_result.stderr.strip().split(chr(10))[-1] if meta_result.stderr.strip() else "no output"
                    raise RuntimeError(f"yt-dlp search failed: {err_detail}")

                info = json.loads(meta_result.stdout)
                video_url = info.get("webpage_url") or info.get("url", "")

                # Use clean Deezer title/artist for the final filename
                clean_name = f"{sanitize_filename(artist)} - {sanitize_filename(title)}.mp3"
                final_path = DOWNLOADS_DIR / clean_name

                # Skip download if file already exists on disk
                if final_path.exists():
                    filepath = str(final_path)
                else:
                    # Download audio (use yt-dlp naming, then rename)
                    output_template = str(DOWNLOADS_DIR / "%(uploader)s - %(title)s.%(ext)s")

                    dl_extra = [
                        "--format", "bestaudio/best",
                        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192",
                        "-o", output_template,
                        "--no-overwrites",
                        "--no-playlist",
                        video_url,
                    ]
                    existing_before = set(DOWNLOADS_DIR.glob("*.mp3"))
                    dl_result = subprocess.run(_ytdlp_base_args() + dl_extra, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
                    if dl_result.returncode != 0:
                        dl_result = subprocess.run(_ytdlp_base_args(use_js=False) + dl_extra, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
                    if dl_result.returncode != 0:
                        raise RuntimeError(f"Download failed: {dl_result.stderr.strip().split(chr(10))[-1]}")

                    # Find the downloaded MP3 (yt-dlp names it with uploader - title)
                    yt_name = f"{sanitize_filename(info.get('uploader') or 'Unknown')} - {sanitize_filename(info.get('title') or 'Unknown')}.mp3"
                    yt_path = DOWNLOADS_DIR / yt_name
                    if not yt_path.exists():
                        new_files = set(DOWNLOADS_DIR.glob("*.mp3")) - existing_before
                        if not new_files:
                            raise FileNotFoundError("MP3 introuvable")
                        yt_path = next(iter(new_files))

                    # Rename to clean Deezer name
                    os.rename(str(yt_path), str(final_path))
                    filepath = str(final_path)

                # Inject metadata with the Deezer title/artist
                inject_metadata(filepath, title, artist)

                rel_path = "/downloads/" + final_path.name
                # Check if file_path already exists in DB
                existing_by_path = db.query(Track).filter(Track.file_path == rel_path).first()
                if existing_by_path:
                    track_id = existing_by_path.id
                else:
                    track = add_track(db, title=title, artist=artist,
                                      file_path=rel_path, youtube_url=video_url)
                    track_id = track.id

                import_status["log"].append(f"{log_msg} → OK")

            # Add to playlist
            if playlist_name:
                pl_id = get_or_create_playlist(playlist_name)
                add_track_to_playlist(db, pl_id, track_id)

        except Exception as e:
            import_status["errors"] += 1
            import_status["log"].append(f"{log_msg} → ERREUR: {str(e)[:120]}")

        import_status["done"] += 1

        # Delay between tracks to avoid YouTube rate-limiting
        if i < len(rows) - 1:
            time.sleep(2)

        # Keep only last 50 log entries
        if len(import_status["log"]) > 50:
            import_status["log"] = import_status["log"][-50:]

    db.close()
    import_status["running"] = False
    import_status["current"] = "Terminé !"


@app.post("/api/import/deezer")
async def api_import_deezer(file: UploadFile = File(...)):
    if import_status["running"]:
        raise HTTPException(status_code=400, detail="Un import est déjà en cours.")

    content = await file.read()
    text = content.decode("utf-8-sig")  # handles BOM
    # Auto-detect delimiter (tab, semicolon, or comma)
    first_line = text.split("\n", 1)[0]
    if "\t" in first_line:
        delimiter = "\t"
    elif ";" in first_line:
        delimiter = ";"
    else:
        delimiter = ","
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)
    rows = list(reader)

    if not rows:
        raise HTTPException(status_code=400, detail="Fichier CSV vide.")

    # Launch in background thread (not BackgroundTasks, as this is long-running)
    thread = threading.Thread(target=import_worker, args=(rows,), daemon=True)
    thread.start()

    return {"message": f"Import démarré : {len(rows)} morceaux à traiter."}


@app.get("/api/import/status")
async def api_import_status():
    with _status_lock:
        return import_status.copy()


@app.post("/api/import/folder-audio")
async def api_import_folder_audio(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    allowed_ext = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
    added = 0
    skipped = 0
    errors = 0

    for uploaded in files:
        original_name = Path(uploaded.filename or "").name
        ext = Path(original_name).suffix.lower()

        if not original_name or ext not in allowed_ext:
            skipped += 1
            continue

        try:
            final_filename = unique_download_filename(original_name)
            dest_path = DOWNLOADS_DIR / final_filename
            content = await uploaded.read()
            if not content:
                skipped += 1
                continue

            with open(dest_path, "wb") as out:
                out.write(content)

            rel_path = "/downloads/" + final_filename

            existing = db.query(Track).filter(Track.file_path == rel_path).first()
            if existing:
                skipped += 1
                continue

            tag_title, tag_artist = extract_metadata(str(dest_path))
            fallback_title, fallback_artist = split_artist_title_from_filename(original_name)
            final_title = (tag_title or fallback_title or "Titre inconnu").strip()
            final_artist = (tag_artist or fallback_artist or "Unknown").strip()

            add_track(db, title=final_title, artist=final_artist, file_path=rel_path, youtube_url=None)
            added += 1
        except Exception as e:
            logger.warning("Échec import fichier %s: %s", uploaded.filename, e)
            errors += 1

    return {
        "message": "Import terminé.",
        "added": added,
        "skipped": skipped,
        "errors": errors,
        "total": len(files),
    }
