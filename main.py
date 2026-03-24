import os
import re
import csv
import json
import logging
import asyncio
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

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, UploadFile, File, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import case
from sqlalchemy.orm import Session

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1

import musicbrainzngs
musicbrainzngs.set_useragent("Clom", "0.1", "https://github.com/tomvivian28072005/maMusique")

from database import (
    init_db, get_db, add_track, get_tracks, delete_track, Track,
    Playlist, PlaylistTrack, get_playlists, create_playlist, update_playlist, delete_playlist,
    add_track_to_playlist, remove_track_from_playlist, get_playlist_tracks,
    record_change, track_to_dict, playlist_to_dict, listen_history_to_dict,
    get_changes_since, get_device_id, cleanup_changelog, SyncDevice, ChangeLog,
    ListenHistory, SessionLocal,
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


async def cleanup_orphan_tracks():
    """Supprime les fichiers des morceaux retirés de toutes les playlists depuis > 3 jours."""
    CLEANUP_DELAY = 3 * 24 * 3600  # 3 jours
    while True:
        await asyncio.sleep(3600)  # Vérifier toutes les heures
        try:
            db = SessionLocal()
            cutoff = time.time() - CLEANUP_DELAY
            orphans = db.query(Track).filter(
                Track.removed_at != None,
                Track.removed_at < cutoff,
            ).all()
            for track in orphans:
                # Supprimer le fichier audio
                if track.file_path:
                    fpath = str(_BASE / track.file_path.lstrip("/"))
                    if os.path.exists(fpath):
                        os.remove(fpath)
                        logger.info(f"Nettoyage: supprimé {fpath}")
                # Supprimer la cover si elle existe
                if track.cover_path:
                    cpath = str(_BASE / track.cover_path.lstrip("/"))
                    if os.path.exists(cpath):
                        os.remove(cpath)
                # Garder l'entrée en DB (métadonnées) mais marquer comme non-téléchargé
                track.download_status = "known"
                track.file_path = ""
            if orphans:
                db.commit()
                logger.info(f"Nettoyage: {len(orphans)} morceau(x) nettoyé(s)")
            db.close()
        except Exception as e:
            logger.warning(f"Erreur nettoyage: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Lancer le job de nettoyage en arrière-plan
    cleanup_task = asyncio.create_task(cleanup_orphan_tracks())
    yield
    cleanup_task.cancel()


APP_VERSION = "0.1.17.1"

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
    stereo_balance: Optional[float] = None
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
    stereo_balance: float = 0.0
    download_status: str = "downloaded"
    album: Optional[str] = None
    mbid: Optional[str] = None
    mbid_release: Optional[str] = None
    duration: Optional[float] = None

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


def fetch_and_save_cover(db: Session, track_id: int, title: str, artist: str):
    """Cherche une pochette HD sur MusicBrainz/Cover Art Archive et la sauvegarde."""
    import urllib.request
    try:
        # Rechercher sur MusicBrainz
        query = f"{title} AND artist:{artist}" if artist else title
        result = musicbrainzngs.search_recordings(query=query, limit=5)
        recordings = result.get("recording-list", [])
        if not recordings:
            logger.info(f"MusicBrainz: aucun résultat pour '{title}' - '{artist}'")
            return None

        import re
        compilation_re = re.compile(r"best of|greatest|compilation|award|hits|antholog", re.IGNORECASE)
        title_lower = title.lower().strip()

        # Trouver un release avec une cover, en préférant single/album du morceau
        for rec in recordings:
            releases = rec.get("release-list", [])
            # Trier : exact match titre > contient titre > le reste ; compilations en dernier
            def _score(r):
                rt = (r.get("title") or "").lower()
                s = 0
                if rt == title_lower: s += 3
                elif title_lower in rt or rt in title_lower: s += 2
                if compilation_re.search(rt): s -= 5
                return s
            releases = sorted(releases, key=_score, reverse=True)

            for release in releases:
                mbid = release.get("id", "")
                if not mbid:
                    continue
                try:
                    img_data = musicbrainzngs.get_image_list(mbid)
                    images = img_data.get("images", [])
                    if not images:
                        continue
                    front = next((img for img in images if img.get("front")), images[0])
                    cover_url = front.get("thumbnails", {}).get("1200",
                                front.get("thumbnails", {}).get("large",
                                front.get("image", "")))
                    if not cover_url:
                        continue

                    # Télécharger l'image
                    req = urllib.request.Request(cover_url, headers={"User-Agent": "Clom/0.1"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        img_bytes = resp.read()

                    # Sauvegarder dans covers/
                    ext = ".jpg"
                    content_type = resp.headers.get("Content-Type", "")
                    if "png" in content_type:
                        ext = ".png"
                    cover_filename = f"track_{track_id}{ext}"
                    cover_path = COVERS_DIR / cover_filename
                    cover_path.write_bytes(img_bytes)

                    # Mettre à jour la DB
                    rel_cover = f"/covers/{cover_filename}"
                    track = db.query(Track).filter(Track.id == track_id).first()
                    if track:
                        track.cover_path = rel_cover
                        db.commit()
                        record_change(db, "track", track.id, "update", track_to_dict(track))
                    logger.info(f"Cover HD sauvegardée: {rel_cover} (depuis {mbid})")
                    return rel_cover
                except Exception as e:
                    logger.debug(f"Cover Art Archive skip {mbid}: {e}")
                    continue
        logger.info(f"MusicBrainz: aucune cover trouvée pour '{title}'")
        return None
    except Exception as e:
        logger.warning(f"fetch_and_save_cover error: {e}")
        return None


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


def normalize_audio(filepath: str) -> float:
    """Normalise le volume d'un fichier MP3 via ffmpeg loudnorm (2-pass).
    Retourne le coefficient de volume calculé (1.0 = pas de changement)."""
    ffmpeg = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    if not os.path.exists(ffmpeg):
        logger.warning("ffmpeg non trouvé, normalisation ignorée")
        return 1.0
    try:
        # Pass 1 : mesurer le volume
        measure_cmd = [
            ffmpeg, "-i", filepath, "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"
        ]
        result = subprocess.run(measure_cmd, capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
        # Extraire les données JSON de la sortie stderr
        stderr = result.stderr
        json_start = stderr.rfind("{")
        json_end = stderr.rfind("}") + 1
        if json_start < 0 or json_end <= json_start:
            logger.warning("Impossible de parser la sortie loudnorm")
            return 1.0
        stats = json.loads(stderr[json_start:json_end])
        input_i = float(stats.get("input_i", -14))
        # Si le volume est déjà proche de -14 LUFS, pas besoin de normaliser
        if abs(input_i - (-14)) < 1.5:
            logger.info(f"Volume OK ({input_i:.1f} LUFS), pas de normalisation nécessaire")
            return 1.0
        # Pass 2 : normaliser
        temp_path = filepath + ".norm.mp3"
        norm_cmd = [
            ffmpeg, "-y", "-i", filepath, "-af",
            f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={stats['input_i']}:measured_LRA={stats['input_lra']}:measured_TP={stats['input_tp']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true",
            "-ar", "44100", "-ab", "192k", temp_path
        ]
        result2 = subprocess.run(norm_cmd, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
        if result2.returncode == 0 and os.path.exists(temp_path):
            os.replace(temp_path, filepath)
            logger.info(f"Audio normalisé: {input_i:.1f} → -14 LUFS")
            return 1.0
        else:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            logger.warning(f"Normalisation échouée (rc={result2.returncode})")
            return 1.0
    except Exception as e:
        logger.warning(f"Erreur normalisation: {e}")
        return 1.0


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

    # Normaliser le volume (-14 LUFS)
    normalize_audio(filepath)

    rel_path = "/downloads/" + Path(filepath).name
    track = add_track(db, title=final_title, artist=final_artist,
                      file_path=rel_path, youtube_url=url)
    record_change(db, "track", track.id, "create", track_to_dict(track))

    # Chercher une pochette HD automatiquement (MusicBrainz)
    cover_found = None
    try:
        cover_found = fetch_and_save_cover(db, track.id, final_title, final_artist)
    except Exception as e:
        logger.warning(f"Auto cover fetch failed: {e}")

    # Fallback : utiliser la thumbnail YouTube si MusicBrainz n'a rien trouvé
    if not cover_found:
        yt_thumb = info.get("thumbnail")
        if yt_thumb:
            try:
                import urllib.request as _ur
                req = _ur.Request(yt_thumb, headers={"User-Agent": "Clom/0.1"})
                with _ur.urlopen(req, timeout=10) as resp:
                    img_bytes = resp.read()
                cover_filename = f"track_{track.id}.jpg"
                cover_path = COVERS_DIR / cover_filename
                cover_path.write_bytes(img_bytes)
                track.cover_path = f"/covers/{cover_filename}"
                db.commit()
                record_change(db, "track", track.id, "update", track_to_dict(track))
                logger.info(f"Cover YouTube fallback: /covers/{cover_filename}")
            except Exception as e:
                logger.warning(f"YouTube thumbnail fallback failed: {e}")

    active_downloads[url] = {"status": "done", "track_id": track.id, "_ts": time.time()}
    return {"track_id": track.id, "title": final_title, "artist": final_artist}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_path = Path("index.html")
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/manifest.json")
async def serve_manifest():
    p = Path("manifest.json")
    if p.exists():
        return FileResponse(str(p), media_type="application/manifest+json")
    raise HTTPException(status_code=404)


@app.get("/api/stats")
async def api_stats(db: Session = Depends(get_db)):
    """Statistiques d'écoute pour le desktop."""
    from sqlalchemy import func as sqf
    tracks = db.query(Track).all()
    total_tracks = len(tracks)
    total_plays = sum(t.play_count or 0 for t in tracks)
    total_duration = sum((t.play_count or 0) * (t.duration or 180) for t in tracks)
    # Top tracks
    top = sorted([t for t in tracks if (t.play_count or 0) > 0], key=lambda t: t.play_count or 0, reverse=True)[:10]
    top_tracks = [{"title": t.title, "artist": t.artist, "play_count": t.play_count, "cover_path": t.cover_path} for t in top]
    # Top artists
    artist_plays = {}
    for t in tracks:
        if t.play_count and t.play_count > 0:
            artist_plays[t.artist] = artist_plays.get(t.artist, 0) + t.play_count
    top_artists = sorted(artist_plays.items(), key=lambda x: x[1], reverse=True)[:10]
    top_artists = [{"artist": a, "play_count": c} for a, c in top_artists]
    # Oldest track
    first_added = min((t.added_at for t in tracks if t.added_at), default=None)
    return {
        "total_tracks": total_tracks,
        "total_plays": total_plays,
        "total_duration_seconds": total_duration,
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "first_added": str(first_added) if first_added else None,
    }


@app.get("/api/version")
async def api_version():
    import socket
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "PC"
    return {"version": APP_VERSION, "name": hostname}


@app.get("/api/network-info")
async def api_network_info():
    """Retourne l'IP locale et un QR code base64 pour l'accès mobile."""
    import socket, io, base64
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    url = f"http://{local_ip}:9000"
    deep_link = f"clom://sync/{local_ip}:9000"
    qr_b64 = None
    qr_deeplink_b64 = None
    try:
        import qrcode
        from qrcode.image.pil import PilImage

        # QR for deep link (opens the Clom app)
        qr_dl = qrcode.QRCode(version=1, box_size=8, border=2)
        qr_dl.add_data(deep_link)
        qr_dl.make(fit=True)
        img_dl = qr_dl.make_image(fill_color="white", back_color="#050508", image_factory=PilImage)
        buf_dl = io.BytesIO()
        img_dl.save(buf_dl, format="PNG")
        qr_deeplink_b64 = base64.b64encode(buf_dl.getvalue()).decode()

        # QR for web URL (fallback)
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="white", back_color="#050508", image_factory=PilImage)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass

    return {"local_ip": local_ip, "url": url, "qr_base64": qr_b64, "qr_deeplink_base64": qr_deeplink_b64, "deep_link": deep_link}


@app.get("/api/discover")
async def api_discover_devices():
    """Scan le réseau local pour trouver d'autres instances Clom."""
    import socket, asyncio, json
    from urllib.request import urlopen
    from urllib.error import URLError
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return {"devices": [], "local_ip": "127.0.0.1"}

    subnet = ".".join(local_ip.split(".")[:3])
    devices = []
    loop = asyncio.get_event_loop()

    def check_host(ip):
        if ip == local_ip:
            return None
        try:
            resp = urlopen(f"http://{ip}:9000/api/version", timeout=1.2)
            data = json.loads(resp.read())
            if "version" in data:
                name = data.get("name", ip)
                if not name or name == ip:
                    try:
                        name = socket.gethostbyaddr(ip)[0].split(".")[0]
                    except Exception:
                        pass
                return {"ip": ip, "url": f"http://{ip}:9000", "version": data["version"], "name": name}
        except Exception:
            return None

    tasks = [loop.run_in_executor(None, check_host, f"{subnet}.{i}") for i in range(1, 255)]
    results = await asyncio.gather(*tasks)
    devices = [r for r in results if r is not None]

    return {"devices": devices, "local_ip": local_ip}


_shutdown_timer = None
_server_start_time = time.time()

@app.post("/api/shutdown")
async def api_shutdown():
    """Shutdown différé — annulé si la page se recharge dans les 3 secondes."""
    global _shutdown_timer
    if not getattr(sys, 'frozen', False):
        return {"status": "ignored (dev mode)"}
    # Ignorer les shutdown pendant les 10 premières secondes (pages zombies après MAJ)
    if time.time() - _server_start_time < 10:
        logger.info("Shutdown ignored (server just started, likely stale page)")
        return {"status": "ignored (grace period)"}
    if _shutdown_timer is not None:
        _shutdown_timer.cancel()
    logger.info("Shutdown requested by client (3s delay)")
    def _do_shutdown():
        import signal
        logger.info("Shutdown confirmed (no reload detected)")
        os.kill(os.getpid(), signal.SIGTERM)
    _shutdown_timer = threading.Timer(3.0, _do_shutdown)
    _shutdown_timer.daemon = True
    _shutdown_timer.start()
    return {"status": "shutting down in 3s"}

@app.post("/api/cancel-shutdown")
async def api_cancel_shutdown():
    """Annule un shutdown en cours (appelé au chargement de la page)."""
    global _shutdown_timer
    if _shutdown_timer is not None:
        _shutdown_timer.cancel()
        _shutdown_timer = None
        logger.info("Shutdown cancelled (page reloaded)")
        return {"status": "cancelled"}
    return {"status": "no pending shutdown"}


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

    # 3. Créer un script batch qui attend la fin du serveur, installe, relance
    app_exe = os.path.join(os.path.dirname(sys.executable), "Clom.exe") if getattr(sys, 'frozen', False) else ""
    bat_path = os.path.join(tmp_dir, "clom_update.bat")
    with open(bat_path, "w") as f:
        f.write(f'@echo off\n')
        f.write(f'timeout /t 5 /nobreak >nul\n')
        f.write(f'start /wait "" "{setup_path}" /VERYSILENT /FORCECLOSEAPPLICATIONS /SUPPRESSMSGBOXES /SP-\n')
        f.write(f'timeout /t 2 /nobreak >nul\n')
        f.write(f'start "" "{app_exe}"\n')
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
        db.refresh(track)
        record_change(db, "track", track.id, "update", track_to_dict(track))

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
            stereo_balance=t.stereo_balance if t.stereo_balance is not None else 0.0,
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

    record_change(db, "track", track_id, "delete")
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
        track.volume_coeff = max(0.5, min(5.0, req.volume_coeff))
    if req.stereo_balance is not None:
        track.stereo_balance = max(-1.0, min(1.0, req.stereo_balance))
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
    db.refresh(track)
    record_change(db, "track", track.id, "update", track_to_dict(track))
    return {"message": "Track updated.", "title": track.title, "artist": track.artist,
            "volume_coeff": track.volume_coeff, "start_time": track.start_time, "end_time": track.end_time,
            "cover_zoom": track.cover_zoom, "cover_offset_x": track.cover_offset_x, "cover_offset_y": track.cover_offset_y,
            "stereo_balance": track.stereo_balance}


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
    db.refresh(track)
    record_change(db, "track", track.id, "update", track_to_dict(track))
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
        record_change(db, "playlist_track", existing.id, "delete", {"playlist_id": fav_playlist.id, "track_id": track_id})
        db.delete(existing)
        db.commit()
        return {"favorite": False}
    else:
        entry = PlaylistTrack(playlist_id=fav_playlist.id, track_id=track_id)
        db.add(entry)
        db.commit()
        db.refresh(entry)
        record_change(db, "playlist_track", entry.id, "create", {"playlist_id": fav_playlist.id, "track_id": track_id})
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
    for pid in req.playlist_ids:
        pl = db.query(Playlist).filter(Playlist.id == pid).first()
        if pl:
            record_change(db, "playlist", pl.id, "update", playlist_to_dict(pl))
    return {"message": "OK"}


@app.post("/api/playlists")
async def api_create_playlist(req: PlaylistCreateRequest, db: Session = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom ne peut pas être vide.")
    playlist = create_playlist(db, name)
    record_change(db, "playlist", playlist.id, "create", playlist_to_dict(playlist))
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
    record_change(db, "playlist", playlist.id, "update", playlist_to_dict(playlist))
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
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    record_change(db, "playlist", playlist_id, "update", playlist_to_dict(playlist))
    return {"cover_path": cover_url}


@app.delete("/api/playlists/{playlist_id}")
async def api_delete_playlist(playlist_id: int, db: Session = Depends(get_db)):
    record_change(db, "playlist", playlist_id, "delete")
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
            "stereo_balance": t.get("stereo_balance", 0.0),
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
    entry = add_track_to_playlist(db, playlist_id, req.track_id)
    record_change(db, "playlist_track", entry.id, "create", {"playlist_id": playlist_id, "track_id": req.track_id})
    # Reset removed_at si le morceau était marqué pour suppression
    if track.removed_at is not None:
        track.removed_at = None
        db.commit()
    return {"message": "Morceau ajouté à la playlist."}


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
async def api_remove_track_from_playlist(playlist_id: int, track_id: int, db: Session = Depends(get_db)):
    entry = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id, PlaylistTrack.track_id == track_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Morceau non trouvé dans la playlist.")
    record_change(db, "playlist_track", entry.id, "delete", {"playlist_id": playlist_id, "track_id": track_id})
    remove_track_from_playlist(db, playlist_id, track_id)
    # Vérifier si le morceau est encore dans au moins une playlist
    remaining = db.query(PlaylistTrack).filter(PlaylistTrack.track_id == track_id).count()
    if remaining == 0:
        track = db.query(Track).filter(Track.id == track_id).first()
        if track:
            track.removed_at = time.time()
            db.commit()
    return {"message": "Morceau retiré de la playlist."}


@app.get("/api/tracks/{track_id}/playlists")
async def api_track_playlists(track_id: int, db: Session = Depends(get_db)):
    """Return list of playlist IDs that contain this track."""
    entries = db.query(PlaylistTrack.playlist_id).filter(PlaylistTrack.track_id == track_id).all()
    return {"playlist_ids": [e[0] for e in entries]}


@app.get("/api/playlists/{playlist_id}/duration")
async def api_playlist_duration(playlist_id: int, db: Session = Depends(get_db)):
    """Calculate total duration of a playlist, respecting start_time/end_time trim."""
    tracks = get_playlist_tracks(db, playlist_id)
    total = 0.0
    for t in tracks:
        fp = Path(t["file_path"].lstrip("/"))
        try:
            if fp.exists():
                audio = MP3(str(fp))
                length = audio.info.length
                start = t.get("start_time") or 0
                end = t.get("end_time") or length
                total += max(0, min(end, length) - start)
        except Exception:
            pass
    return {"total_seconds": total}


@app.get("/api/playlists/{playlist_id}/export")
async def api_export_playlist(playlist_id: int, db: Session = Depends(get_db)):
    """Export playlist as a zip: folder with CSV + MP3 files."""
    import zipfile, tempfile, shutil

    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist introuvable.")

    tracks = get_playlist_tracks(db, playlist_id)
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', playlist.name).strip() or "playlist"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()

    try:
        with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
            # CSV info
            csv_buf = StringIO()
            writer = csv.writer(csv_buf, delimiter='\t')
            writer.writerow(["Title", "Artist", "Play Count", "Volume Coeff", "Start Time", "End Time"])
            for t in tracks:
                writer.writerow([
                    t["title"], t["artist"], t.get("play_count", 0),
                    t.get("volume_coeff", 1.0), t.get("start_time", ""), t.get("end_time", "")
                ])
            zf.writestr(f"{safe_name}/info.csv", csv_buf.getvalue())

            # MP3 files
            for t in tracks:
                fp = Path(t["file_path"].lstrip("/"))
                if fp.exists():
                    zf.write(str(fp), f"{safe_name}/{fp.name}")

        bg = BackgroundTasks()
        bg.add_task(os.unlink, tmp.name)
        return FileResponse(tmp.name, filename=f"{safe_name}.zip", media_type="application/zip",
                          background=bg)
    except Exception as e:
        os.unlink(tmp.name)
        raise HTTPException(status_code=500, detail=str(e))


# ── Sync ─────────────────────────────────────────────────────────────────────


class SyncApplyRequest(BaseModel):
    device_id: str
    device_name: str
    changes: list[dict]


@app.get("/api/sync/changes")
async def api_sync_changes(since: float = 0, device_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Retourne les changements depuis un timestamp, en excluant un device."""
    changes = get_changes_since(db, since, exclude_device=device_id)
    local_device_id = get_device_id(db)
    return {"device_id": local_device_id, "changes": changes}


@app.post("/api/sync/apply")
async def api_sync_apply(req: SyncApplyRequest, db: Session = Depends(get_db)):
    """Applique les changements reçus d'un autre appareil."""
    # Enregistrer/mettre à jour le device distant
    remote = db.query(SyncDevice).filter(SyncDevice.id == req.device_id).first()
    if not remote:
        remote = SyncDevice(id=req.device_id, name=req.device_name)
        db.add(remote)
        db.commit()
    elif remote.name != req.device_name:
        remote.name = req.device_name
        db.commit()

    applied = 0
    errors = []

    for change in req.changes:
        try:
            entity_type = change["entity_type"]
            entity_id = change["entity_id"]
            action = change["action"]
            data = change.get("data")

            if entity_type == "track":
                if action == "create" and data:
                    existing = db.query(Track).filter(Track.id == entity_id).first()
                    if not existing:
                        track = Track(
                            id=entity_id, title=data["title"], artist=data["artist"],
                            file_path=data["file_path"], youtube_url=data.get("youtube_url"),
                            play_count=data.get("play_count", 0),
                            volume_coeff=data.get("volume_coeff", 1.0),
                            start_time=data.get("start_time"), end_time=data.get("end_time"),
                            cover_path=data.get("cover_path"),
                            cover_zoom=data.get("cover_zoom", 1.0),
                            cover_offset_x=data.get("cover_offset_x", 0.0),
                            cover_offset_y=data.get("cover_offset_y", 0.0),
                        )
                        db.add(track)
                        db.commit()
                        applied += 1
                elif action == "update" and data:
                    track = db.query(Track).filter(Track.id == entity_id).first()
                    if track:
                        for key in ("title", "artist", "file_path", "youtube_url", "play_count",
                                    "volume_coeff", "start_time", "end_time", "cover_path",
                                    "cover_zoom", "cover_offset_x", "cover_offset_y"):
                            if key in data:
                                setattr(track, key, data[key])
                        db.commit()
                        applied += 1
                elif action == "delete":
                    track = db.query(Track).filter(Track.id == entity_id).first()
                    if track:
                        db.delete(track)
                        db.commit()
                        applied += 1

            elif entity_type == "playlist":
                if action == "create" and data:
                    existing = db.query(Playlist).filter(Playlist.id == entity_id).first()
                    if not existing:
                        pl = Playlist(
                            id=entity_id, name=data["name"],
                            cover_path=data.get("cover_path"),
                            cover_zoom=data.get("cover_zoom", 1.0),
                            cover_offset_x=data.get("cover_offset_x", 0.0),
                            cover_offset_y=data.get("cover_offset_y", 0.0),
                            is_default=data.get("is_default", 0),
                            position=data.get("position", 999),
                        )
                        db.add(pl)
                        db.commit()
                        applied += 1
                elif action == "update" and data:
                    pl = db.query(Playlist).filter(Playlist.id == entity_id).first()
                    if pl:
                        for key in ("name", "cover_path", "cover_zoom", "cover_offset_x",
                                    "cover_offset_y", "position"):
                            if key in data:
                                setattr(pl, key, data[key])
                        db.commit()
                        applied += 1
                elif action == "delete":
                    pl = db.query(Playlist).filter(Playlist.id == entity_id).first()
                    if pl and not (pl.is_default and pl.name == "Coup de cœur"):
                        db.delete(pl)
                        db.commit()
                        applied += 1

            elif entity_type == "playlist_track":
                if action == "create" and data:
                    pid, tid = data["playlist_id"], data["track_id"]
                    existing = db.query(PlaylistTrack).filter(
                        PlaylistTrack.playlist_id == pid, PlaylistTrack.track_id == tid
                    ).first()
                    if not existing:
                        db.add(PlaylistTrack(playlist_id=pid, track_id=tid))
                        db.commit()
                        applied += 1
                elif action == "delete" and data:
                    pid, tid = data["playlist_id"], data["track_id"]
                    entry = db.query(PlaylistTrack).filter(
                        PlaylistTrack.playlist_id == pid, PlaylistTrack.track_id == tid
                    ).first()
                    if entry:
                        db.delete(entry)
                        db.commit()
                        applied += 1

            elif entity_type == "listen_history":
                if action == "create" and data:
                    # Dédup par track_id + listened_at + device_id
                    listened_at = data.get("listened_at")
                    src_device = data.get("device_id", change.get("device_id"))
                    existing = db.query(ListenHistory).filter(
                        ListenHistory.track_id == data["track_id"],
                        ListenHistory.listened_at == listened_at,
                        ListenHistory.device_id == src_device,
                    ).first()
                    if not existing:
                        lh = ListenHistory(
                            track_id=data["track_id"],
                            listened_at=listened_at,
                            duration_seconds=data.get("duration_seconds", 0),
                            device_id=src_device,
                        )
                        db.add(lh)
                        db.commit()
                        applied += 1

        except Exception as e:
            errors.append({"change": change, "error": str(e)})

    return {"applied": applied, "errors": errors}


@app.post("/api/sync/complete")
async def api_sync_complete(device_id: str, db: Session = Depends(get_db)):
    """Marque la sync comme terminée pour un device. Nettoie le journal."""
    device = db.query(SyncDevice).filter(SyncDevice.id == device_id).first()
    if device:
        device.last_sync = time.time()
        db.commit()
    cleaned = cleanup_changelog(db)
    return {"cleaned": cleaned}


@app.post("/api/sync/execute")
async def api_sync_execute(request: Request, db: Session = Depends(get_db)):
    """Exécute une sync bidirectionnelle PC-to-PC avec un serveur distant."""
    body = await request.json()
    remote_url = body.get("url", "").rstrip("/")
    if not remote_url:
        raise HTTPException(status_code=400, detail="URL manquante")

    import urllib.request, shutil

    local_device_id = get_device_id(db)

    # 1. Récupérer les devices connus pour last_sync
    remote_device = db.query(SyncDevice).filter(SyncDevice.name == remote_url).first()
    last_sync = remote_device.last_sync if remote_device else 0

    # 2. Récupérer les changements du serveur distant
    try:
        with urllib.request.urlopen(f"{remote_url}/api/sync/changes?since={last_sync}&device_id={local_device_id}", timeout=10) as r:
            remote_data = json.loads(r.read())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Impossible de joindre le serveur : {e}")

    remote_device_id = remote_data["device_id"]
    remote_changes = remote_data["changes"]

    # 3. Envoyer nos changements locaux au serveur distant
    local_changes = get_changes_since(db, last_sync, exclude_device=remote_device_id)
    sent = 0
    if local_changes:
        payload = json.dumps({
            "device_id": local_device_id,
            "device_name": "PC",
            "changes": local_changes,
        }).encode()
        req_obj = urllib.request.Request(
            f"{remote_url}/api/sync/apply",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_obj, timeout=30) as r:
            result = json.loads(r.read())
            sent = result.get("applied", 0)

    # 4. Appliquer les changements distants localement
    received = 0
    if remote_changes:
        for change in remote_changes:
            try:
                entity_type = change["entity_type"]
                entity_id = change["entity_id"]
                action = change["action"]
                data = change.get("data")
                if entity_type == "track":
                    if action == "create" and data:
                        if not db.query(Track).filter(Track.id == entity_id).first():
                            track = Track(
                                id=entity_id, title=data["title"], artist=data["artist"],
                                file_path=data["file_path"], youtube_url=data.get("youtube_url"),
                                play_count=data.get("play_count", 0),
                                volume_coeff=data.get("volume_coeff", 1.0),
                                start_time=data.get("start_time"), end_time=data.get("end_time"),
                                cover_path=data.get("cover_path"),
                                cover_zoom=data.get("cover_zoom", 1.0),
                                cover_offset_x=data.get("cover_offset_x", 0.0),
                                cover_offset_y=data.get("cover_offset_y", 0.0),
                                stereo_balance=data.get("stereo_balance", 0.0),
                            )
                            db.add(track)
                            db.commit()
                            received += 1
                    elif action == "update" and data:
                        track = db.query(Track).filter(Track.id == entity_id).first()
                        if track:
                            for key in ("title", "artist", "file_path", "youtube_url", "play_count",
                                        "volume_coeff", "start_time", "end_time", "cover_path",
                                        "cover_zoom", "cover_offset_x", "cover_offset_y", "stereo_balance"):
                                if key in data:
                                    setattr(track, key, data[key])
                            db.commit()
                            received += 1
                    elif action == "delete":
                        track = db.query(Track).filter(Track.id == entity_id).first()
                        if track:
                            db.delete(track)
                            db.commit()
                            received += 1
                elif entity_type == "playlist":
                    if action == "create" and data:
                        if not db.query(Playlist).filter(Playlist.id == entity_id).first():
                            pl = Playlist(id=entity_id, name=data["name"], cover_path=data.get("cover_path"),
                                cover_zoom=data.get("cover_zoom", 1.0), cover_offset_x=data.get("cover_offset_x", 0.0),
                                cover_offset_y=data.get("cover_offset_y", 0.0), is_default=data.get("is_default", 0),
                                position=data.get("position", 999))
                            db.add(pl)
                            db.commit()
                            received += 1
                    elif action == "update" and data:
                        pl = db.query(Playlist).filter(Playlist.id == entity_id).first()
                        if pl:
                            for key in ("name", "cover_path", "cover_zoom", "cover_offset_x", "cover_offset_y", "position"):
                                if key in data:
                                    setattr(pl, key, data[key])
                            db.commit()
                            received += 1
                    elif action == "delete":
                        pl = db.query(Playlist).filter(Playlist.id == entity_id).first()
                        if pl and not (pl.is_default and pl.name == "Coup de cœur"):
                            db.delete(pl)
                            db.commit()
                            received += 1
                elif entity_type == "playlist_track":
                    if action == "create" and data:
                        pid, tid = data["playlist_id"], data["track_id"]
                        if not db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == pid, PlaylistTrack.track_id == tid).first():
                            db.add(PlaylistTrack(playlist_id=pid, track_id=tid))
                            db.commit()
                            received += 1
                    elif action == "delete" and data:
                        pid, tid = data["playlist_id"], data["track_id"]
                        entry = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == pid, PlaylistTrack.track_id == tid).first()
                        if entry:
                            db.delete(entry)
                            db.commit()
                            received += 1
            except Exception as e:
                logger.warning(f"[Sync] Erreur application changement: {e}")

    # 5. Transférer les fichiers manquants (bidirectionnel)
    with urllib.request.urlopen(f"{remote_url}/api/sync/manifest", timeout=10) as r:
        remote_manifest = json.loads(r.read())

    local_tracks = {t.id: t for t in db.query(Track).all()}
    remote_track_map = {t["id"]: t for t in remote_manifest["tracks"]}

    files_downloaded = 0

    # Fichiers qu'on n'a pas localement mais qui existent sur le distant
    for rt in remote_manifest["tracks"]:
        tid = rt["id"]
        local_t = local_tracks.get(tid)
        if local_t and local_t.file_path:
            local_file = Path(local_t.file_path.lstrip("/"))
            if local_file.exists():
                continue
        elif not local_t:
            continue  # pas de track en DB, on skip (devrait avoir été créé par les changes)

        # Télécharger le fichier
        try:
            file_url = f"{remote_url}/api/sync/tracks/{tid}/file"
            file_name = Path(rt["file_path"]).name if rt.get("file_path") else f"{tid}.mp3"
            local_path = Path("downloads") / file_name
            local_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(file_url, str(local_path))
            if local_t:
                local_t.file_path = f"/downloads/{file_name}"
                db.commit()
            files_downloaded += 1
        except Exception as e:
            logger.warning(f"[Sync] Fichier {tid}: {e}")

    # Télécharger les covers manquantes
    for rt in remote_manifest["tracks"]:
        if rt.get("cover_path"):
            local_t = local_tracks.get(rt["id"])
            if local_t and local_t.cover_path:
                cover_file = Path(local_t.cover_path.split("?")[0].lstrip("/"))
                if cover_file.exists():
                    continue
            if local_t:
                try:
                    cover_name = rt["cover_path"].split("?")[0].split("/")[-1]
                    cover_url = f"{remote_url}{rt['cover_path'].split('?')[0]}"
                    local_cover = Path("covers") / cover_name
                    local_cover.parent.mkdir(parents=True, exist_ok=True)
                    urllib.request.urlretrieve(cover_url, str(local_cover))
                    local_t.cover_path = f"/covers/{cover_name}?t={int(time.time())}"
                    db.commit()
                except Exception as e:
                    logger.warning(f"[Sync] Cover {rt['id']}: {e}")

    # 6. Compléter la sync
    try:
        req_obj = urllib.request.Request(
            f"{remote_url}/api/sync/complete?device_id={local_device_id}",
            method="POST",
        )
        urllib.request.urlopen(req_obj, timeout=10)
    except Exception:
        pass

    # Mettre à jour last_sync
    now = time.time()
    if remote_device:
        remote_device.last_sync = now
    else:
        remote_device = SyncDevice(id=remote_device_id, name=remote_url, last_sync=now)
        db.add(remote_device)
    db.commit()
    cleanup_changelog(db)

    return {"sent": sent, "received": received, "files_downloaded": files_downloaded}


@app.post("/api/sync/preview")
async def api_sync_preview(request: Request, db: Session = Depends(get_db)):
    """Compare la bibliothèque locale avec celle d'un appareil distant et retourne un aperçu."""
    body = await request.json()
    remote_tracks = body.get("tracks", [])
    remote_playlists = body.get("playlists", [])

    local_tracks = {t.id: t for t in db.query(Track).all()}
    local_track_ids = set(local_tracks.keys())
    remote_track_ids = set(t["id"] for t in remote_tracks)

    to_send = []  # tracks on local but not remote
    to_receive = []  # tracks on remote but not local
    to_update = []  # tracks on both but modified

    for tid in local_track_ids - remote_track_ids:
        t = local_tracks[tid]
        to_send.append({"id": t.id, "title": t.title, "artist": t.artist})

    for rt in remote_tracks:
        if rt["id"] not in local_track_ids:
            to_receive.append({"id": rt["id"], "title": rt["title"], "artist": rt["artist"]})

    return {
        "to_send": to_send,
        "to_receive": to_receive,
        "send_count": len(to_send),
        "receive_count": len(to_receive),
        "local_total": len(local_tracks),
        "remote_total": len(remote_tracks),
    }


# ── Registered devices (in-memory) ──
_registered_devices = {}  # device_id -> {name, ip, last_seen, device_type}


@app.post("/api/devices/register")
async def api_device_register(request: Request):
    """Un appareil s'enregistre pour être visible sur le réseau."""
    body = await request.json()
    device_id = body.get("device_id", "")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id manquant")
    _registered_devices[device_id] = {
        "name": body.get("name", "Appareil"),
        "ip": request.client.host if request.client else "unknown",
        "last_seen": time.time(),
        "device_type": body.get("device_type", "mobile"),
        "track_count": body.get("track_count", 0),
    }
    # Cleanup stale (>5min)
    now = time.time()
    for k in list(_registered_devices.keys()):
        if now - _registered_devices[k]["last_seen"] > 300:
            del _registered_devices[k]
    return {"ok": True}


@app.get("/api/devices")
async def api_devices_list():
    """Liste les appareils enregistrés (actifs)."""
    now = time.time()
    active = [
        {"device_id": k, **v}
        for k, v in _registered_devices.items()
        if now - v["last_seen"] < 300
    ]
    return {"devices": active}


# Pending sync requests (in-memory, cleared on restart)
_pending_sync_requests = {}  # request_id -> {from_ip, from_name, preview, timestamp, status, target_device_id}


@app.post("/api/sync/request")
async def api_sync_request(request: Request):
    """Enregistre une demande de sync. target_device_id optionnel pour cibler un appareil."""
    body = await request.json()
    import uuid
    req_id = str(uuid.uuid4())[:8]
    _pending_sync_requests[req_id] = {
        "from_ip": request.client.host if request.client else "unknown",
        "from_name": body.get("name", "Appareil inconnu"),
        "preview": body.get("preview", {}),
        "timestamp": time.time(),
        "status": "pending",  # pending | accepted | rejected
        "target_device_id": body.get("target_device_id"),
    }
    # Cleanup old requests (>5min)
    now = time.time()
    for k in list(_pending_sync_requests.keys()):
        if now - _pending_sync_requests[k]["timestamp"] > 300:
            del _pending_sync_requests[k]
    return {"request_id": req_id}


@app.get("/api/sync/pending")
async def api_sync_pending(device_id: str = None):
    """Retourne les demandes de sync en attente, filtrées par device_id si fourni."""
    pending = []
    for k, v in _pending_sync_requests.items():
        if v["status"] != "pending":
            continue
        # Si device_id fourni, ne montrer que les demandes ciblées pour cet appareil
        if device_id and v.get("target_device_id") and v["target_device_id"] != device_id:
            continue
        pending.append({"id": k, **v})
    return {"requests": pending}


@app.post("/api/sync/accept/{request_id}")
async def api_sync_accept(request_id: str):
    """Accepte une demande de sync."""
    if request_id in _pending_sync_requests:
        _pending_sync_requests[request_id]["status"] = "accepted"
        return {"status": "accepted"}
    raise HTTPException(status_code=404, detail="Demande introuvable")


@app.post("/api/sync/reject/{request_id}")
async def api_sync_reject(request_id: str):
    """Refuse une demande de sync."""
    if request_id in _pending_sync_requests:
        _pending_sync_requests[request_id]["status"] = "rejected"
        return {"status": "rejected"}
    raise HTTPException(status_code=404, detail="Demande introuvable")


@app.get("/api/sync/request/{request_id}/status")
async def api_sync_request_status(request_id: str):
    """Vérifie le statut d'une demande de sync."""
    if request_id in _pending_sync_requests:
        return {"status": _pending_sync_requests[request_id]["status"]}
    raise HTTPException(status_code=404, detail="Demande introuvable")


@app.get("/api/sync/manifest")
async def api_sync_manifest(db: Session = Depends(get_db)):
    """Retourne un résumé de la bibliothèque pour comparaison rapide."""
    tracks = db.query(Track).all()
    playlists = get_playlists(db)
    track_dicts = []
    for t in tracks:
        d = track_to_dict(t)
        # Indiquer si le fichier audio existe physiquement
        d["has_file"] = bool(t.file_path and Path(t.file_path.lstrip("/")).exists())
        track_dicts.append(d)
    return {
        "device_id": get_device_id(db),
        "tracks": track_dicts,
        "playlists": [playlist_to_dict(p) for p in playlists],
        "playlist_tracks": [
            {"playlist_id": pt.playlist_id, "track_id": pt.track_id}
            for pt in db.query(PlaylistTrack).all()
        ],
        "listen_history": [listen_history_to_dict(lh) for lh in db.query(ListenHistory).all()],
    }


@app.get("/api/sync/tracks/{track_id}/file")
async def api_sync_track_file(track_id: int, db: Session = Depends(get_db)):
    """Envoie le fichier audio d'un morceau pour transfert LAN."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found.")
    file_abs = Path(track.file_path.lstrip("/"))
    if not file_abs.exists():
        raise HTTPException(status_code=404, detail="Fichier audio introuvable.")
    return FileResponse(str(file_abs), media_type="audio/mpeg", filename=file_abs.name)


@app.post("/api/sync/tracks/{track_id}/upload")
async def api_sync_track_upload(track_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Reçoit un fichier audio envoyé depuis un autre appareil (mobile → PC)."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found.")

    # Sauvegarder le fichier dans downloads/
    ext = Path(file.filename).suffix or ".mp3"
    safe_name = f"{track.artist} - {track.title}".replace("/", "-").replace("\\", "-")[:100]
    filename = f"{safe_name}{ext}"
    dest = DOWNLOADS_DIR / filename
    # Éviter les doublons
    counter = 1
    while dest.exists():
        dest = DOWNLOADS_DIR / f"{safe_name} ({counter}){ext}"
        counter += 1

    content = await file.read()
    dest.write_bytes(content)

    track.file_path = f"/downloads/{dest.name}"
    db.commit()
    record_change(db, "track", track.id, "update", track_to_dict(track))

    logger.info(f"[Sync Upload] Track {track_id} reçue: {dest.name} ({len(content) // 1024} Ko)")
    return {"ok": True, "file_path": track.file_path}


@app.post("/api/sync/covers/upload")
async def api_sync_cover_upload(track_id: int | None = None, playlist_id: int | None = None,
                                file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Reçoit une cover envoyée depuis un autre appareil."""
    if not track_id and not playlist_id:
        raise HTTPException(status_code=400, detail="track_id ou playlist_id requis")

    ext = Path(file.filename).suffix or ".jpg"
    content = await file.read()

    if track_id:
        track = db.query(Track).filter(Track.id == track_id).first()
        if not track:
            raise HTTPException(status_code=404, detail="Track not found.")
        cover_name = f"track_{track_id}{ext}"
        cover_path = COVERS_DIR / cover_name
        cover_path.write_bytes(content)
        track.cover_path = f"/covers/{cover_name}?t={int(time.time())}"
        db.commit()
        record_change(db, "track", track.id, "update", track_to_dict(track))
    elif playlist_id:
        pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
        if not pl:
            raise HTTPException(status_code=404, detail="Playlist not found.")
        cover_name = f"playlist_{playlist_id}{ext}"
        cover_path = COVERS_DIR / cover_name
        cover_path.write_bytes(content)
        pl.cover_path = f"/covers/{cover_name}?t={int(time.time())}"
        db.commit()
        record_change(db, "playlist", pl.id, "update", playlist_to_dict(pl))

    return {"ok": True}


# ── CSV Import (Deezer, Spotify, etc.) ───────────────────────────────────────

def normalize_csv_columns(rows: list[dict]) -> list[dict]:
    """Normalise les noms de colonnes CSV (Deezer/Spotify/générique) vers un format canonique."""
    COLUMN_MAP = {
        # Title
        "track name": "Track name",
        "titre": "Track name",
        "track_name": "Track name",
        "song name": "Track name",
        "song": "Track name",
        "title": "Track name",
        "name": "Track name",
        # Artist
        "artist name": "Artist name",
        "artist name(s)": "Artist name",
        "artiste": "Artist name",
        "artist_name": "Artist name",
        "artist": "Artist name",
        "artists": "Artist name",
        # Album
        "album name": "Album name",
        "album": "Album name",
        "album_name": "Album name",
        # Playlist
        "playlist name": "Playlist name",
        "playlist": "Playlist name",
        "playlist_name": "Playlist name",
    }
    normalized = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            canonical = COLUMN_MAP.get(key.strip().lower(), key.strip())
            new_row[canonical] = value
        normalized.append(new_row)
    return normalized


def preprocess_csv_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Retire les lignes sans titre et déduplique par titre+artiste+playlist. Retourne (rows_nettoyés, nb_retirés)."""
    seen = set()
    clean = []
    for row in rows:
        title = (row.get("Track name") or "").strip()
        if not title:
            continue
        artist = (row.get("Artist name") or "").strip()
        playlist = (row.get("Playlist name") or "").strip()
        key = (title.lower(), artist.lower(), playlist.lower())
        if key in seen:
            continue
        seen.add(key)
        clean.append(row)
    return clean, len(rows) - len(clean)


import_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": 0,
    "current": "",
    "log": [],
    "failed_rows": [],
    "stopped_early": False,
    "stop_requested": False,
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
    import_status["failed_rows"] = []
    import_status["stopped_early"] = False
    import_status["stop_requested"] = False

    consecutive_failures = 0
    playlist_cache: dict[str, int] = {}

    def get_or_create_playlist(name: str) -> int:
        if name in playlist_cache:
            return playlist_cache[name]
        existing = db.query(Playlist).filter(Playlist.name == name).first()
        if existing:
            playlist_cache[name] = existing.id
            return existing.id
        pl = create_playlist(db, name)
        record_change(db, "playlist", pl.id, "create", playlist_to_dict(pl))
        playlist_cache[name] = pl.id
        return pl.id

    for i, row in enumerate(rows):
        if import_status["stop_requested"]:
            import_status["stopped_early"] = True
            remaining = rows[i:]
            import_status["failed_rows"].extend(remaining)
            import_status["log"].append(f"⛔ Import arrêté par l'utilisateur. {len(remaining)} morceaux restants.")
            break

        title = (row.get("Track name") or "").strip()
        artist = (row.get("Artist name") or "").strip()
        playlist_name = (row.get("Playlist name") or "").strip()

        if not title:
            import_status["done"] += 1
            continue

        import_status["current"] = f"{title} - {artist}"
        log_msg = f"[{i+1}/{len(rows)}] {title} - {artist}"

        try:
            existing_track = db.query(Track).filter(
                Track.title.ilike(title),
                Track.artist.ilike(artist)
            ).first()

            if existing_track:
                track_id = existing_track.id
                import_status["log"].append(f"{log_msg} → déjà en base")
            else:
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

                clean_name = f"{sanitize_filename(artist)} - {sanitize_filename(title)}.mp3"
                final_path = DOWNLOADS_DIR / clean_name

                if final_path.exists():
                    filepath = str(final_path)
                else:
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

                    yt_name = f"{sanitize_filename(info.get('uploader') or 'Unknown')} - {sanitize_filename(info.get('title') or 'Unknown')}.mp3"
                    yt_path = DOWNLOADS_DIR / yt_name
                    if not yt_path.exists():
                        new_files = set(DOWNLOADS_DIR.glob("*.mp3")) - existing_before
                        if not new_files:
                            raise FileNotFoundError("MP3 introuvable")
                        yt_path = next(iter(new_files))

                    os.rename(str(yt_path), str(final_path))
                    filepath = str(final_path)

                inject_metadata(filepath, title, artist)

                rel_path = "/downloads/" + final_path.name
                existing_by_path = db.query(Track).filter(Track.file_path == rel_path).first()
                if existing_by_path:
                    track_id = existing_by_path.id
                else:
                    track = add_track(db, title=title, artist=artist,
                                      file_path=rel_path, youtube_url=video_url)
                    record_change(db, "track", track.id, "create", track_to_dict(track))
                    track_id = track.id

                import_status["log"].append(f"{log_msg} → OK")

            # Add to playlist (map favorite playlist names to "Coup de cœur")
            if playlist_name:
                _fav_names = {"favorite tracks", "liked songs", "loved tracks", "titres likés", "coups de cœur", "coup de coeur", "coup de cœur"}
                if playlist_name.lower().strip() in _fav_names:
                    fav_pl = db.query(Playlist).filter(Playlist.name == "Coup de cœur", Playlist.is_default == 1).first()
                    if fav_pl:
                        pl_id = fav_pl.id
                    else:
                        pl_id = get_or_create_playlist(playlist_name)
                else:
                    pl_id = get_or_create_playlist(playlist_name)
                pt_entry = add_track_to_playlist(db, pl_id, track_id)
                if pt_entry:
                    record_change(db, "playlist_track", pt_entry.id, "create", {"playlist_id": pl_id, "track_id": track_id})

            consecutive_failures = 0

        except Exception as e:
            import_status["errors"] += 1
            import_status["log"].append(f"{log_msg} → ERREUR: {str(e)[:120]}")
            import_status["failed_rows"].append(row)
            consecutive_failures += 1

            if consecutive_failures >= 10:
                import_status["stopped_early"] = True
                remaining = rows[i + 1:]
                import_status["failed_rows"].extend(remaining)
                import_status["log"].append(f"⛔ Import arrêté : {consecutive_failures} échecs consécutifs. {len(remaining)} morceaux restants.")
                break

        import_status["done"] += 1

        if i < len(rows) - 1:
            time.sleep(2)

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
    text = content.decode("utf-8-sig")
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

    # Normaliser les colonnes (Deezer/Spotify/générique) puis dédupliquer
    rows = normalize_csv_columns(rows)
    rows, removed = preprocess_csv_rows(rows)

    if not rows:
        raise HTTPException(status_code=400, detail="Aucun morceau valide trouvé dans le CSV.")

    thread = threading.Thread(target=import_worker, args=(rows,), daemon=True)
    thread.start()

    msg = f"Import démarré : {len(rows)} morceaux à traiter."
    if removed > 0:
        msg += f" ({removed} doublons/lignes vides retirés)"
    return {"message": msg}


@app.get("/api/import/status")
async def api_import_status():
    with _status_lock:
        result = import_status.copy()
    # N'envoyer les failed_rows complètes que quand l'import est terminé
    if result["running"]:
        result["failed_count"] = len(result.get("failed_rows", []))
        result.pop("failed_rows", None)
    return result


@app.post("/api/import/stop")
async def api_import_stop():
    if not import_status["running"]:
        return {"message": "Aucun import en cours"}
    import_status["stop_requested"] = True
    return {"message": "Arrêt demandé"}


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

            track = add_track(db, title=final_title, artist=final_artist, file_path=rel_path, youtube_url=None)
            record_change(db, "track", track.id, "create", track_to_dict(track))
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


# ── MusicBrainz search ──────────────────────────────────────

@app.get("/api/search/musicbrainz")
async def api_search_musicbrainz(q: str, limit: int = 15):
    """Recherche dans MusicBrainz (titre, artiste, album)."""
    if len(q) < 2:
        return {"results": []}
    try:
        result = musicbrainzngs.search_recordings(query=q, limit=limit)
        recordings = result.get("recording-list", [])
        out = []
        for rec in recordings:
            artist = ""
            if "artist-credit" in rec:
                parts = rec["artist-credit"]
                artist = "".join(
                    p["artist"]["name"] if isinstance(p, dict) and "artist" in p else str(p)
                    for p in parts
                )
            release = ""
            mbid_release = ""
            if "release-list" in rec and rec["release-list"]:
                release = rec["release-list"][0].get("title", "")
                mbid_release = rec["release-list"][0].get("id", "")
            out.append({
                "mbid": rec.get("id", ""),
                "title": rec.get("title", ""),
                "artist": artist,
                "album": release,
                "mbid_release": mbid_release,
                "duration": int(rec["length"]) // 1000 if rec.get("length") else None,
                "score": int(rec.get("ext:score", 0)),
            })
        return {"results": out}
    except Exception as e:
        logger.warning("MusicBrainz search error: %s", e)
        return {"results": [], "error": str(e)}


# ── YouTube search + audio stream ────────────────────────────

@app.get("/api/search/youtube")
async def api_search_youtube(q: str, limit: int = 5):
    """Recherche YouTube via yt-dlp (ytsearch)."""
    if len(q) < 2:
        return {"results": []}
    try:
        cmd = [YTDLP_BIN, "--dump-json", "--no-download", "--flat-playlist",
               f"ytsearch{limit}:{q}"]
        proc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: subprocess.run(cmd, capture_output=True, text=True,
                                         timeout=15, creationflags=_NO_WINDOW))
        results = []
        for line in proc.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                vid = data.get("id", "")
                thumb = data.get("thumbnail", "")
                if not thumb and vid:
                    thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                results.append({
                    "id": vid,
                    "title": data.get("title", ""),
                    "artist": data.get("channel", data.get("uploader", "")),
                    "duration": data.get("duration"),
                    "thumbnail": thumb,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                })
            except json.JSONDecodeError:
                continue
        return {"results": results}
    except Exception as e:
        logger.warning("YouTube search error: %s", e)
        return {"results": [], "error": str(e)}


@app.get("/api/stream/youtube")
async def api_stream_youtube(video_id: str):
    """Extrait l'URL audio directe d'une vidéo YouTube pour pré-écoute."""
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id manquant")
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        cmd = [YTDLP_BIN, "-f", "bestaudio", "-g", "--no-playlist", url]
        proc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: subprocess.run(cmd, capture_output=True, text=True,
                                         timeout=15, creationflags=_NO_WINDOW))
        audio_url = proc.stdout.strip()
        if not audio_url:
            raise HTTPException(status_code=404, detail="Impossible d'extraire l'audio")
        return {"audio_url": audio_url, "video_id": video_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("YouTube stream error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── Cover Art (MusicBrainz Cover Art Archive) ────────────────

@app.get("/api/cover/musicbrainz/{release_mbid}")
async def api_cover_musicbrainz(release_mbid: str):
    """Récupère l'URL de la pochette HD depuis Cover Art Archive."""
    try:
        data = musicbrainzngs.get_image_list(release_mbid)
        images = data.get("images", [])
        if not images:
            return {"cover_url": None}
        # Prendre la première image front, sinon la première dispo
        front = next((img for img in images if img.get("front")), images[0])
        # Retourner l'URL en grande taille (1200px) ou originale
        thumb_large = front.get("thumbnails", {}).get("1200", front.get("thumbnails", {}).get("large", ""))
        return {
            "cover_url": thumb_large or front.get("image", ""),
            "thumbnails": front.get("thumbnails", {}),
        }
    except Exception as e:
        logger.warning("Cover Art Archive error for %s: %s", release_mbid, e)
        return {"cover_url": None}
