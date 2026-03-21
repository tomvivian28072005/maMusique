from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session, relationship
from datetime import datetime
import time as _time
import json as _json
import uuid as _uuid

DATABASE_URL = "sqlite:///./music.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "tracks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    artist = Column(String, nullable=False, default="Unknown")
    file_path = Column(String, nullable=False, unique=True)
    youtube_url = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    play_count = Column(Integer, default=0)
    volume_coeff = Column(Float, default=1.0)
    start_time = Column(Float, nullable=True)  # seconds, None = from beginning
    end_time = Column(Float, nullable=True)     # seconds, None = until end
    cover_path = Column(String, nullable=True)  # path to cover image
    cover_zoom = Column(Float, default=1.0)     # zoom level for cover
    cover_offset_x = Column(Float, default=0.0) # horizontal offset (-1 to 1)
    cover_offset_y = Column(Float, default=0.0) # vertical offset (-1 to 1)
    stereo_balance = Column(Float, default=0.0) # -1.0 (full left) to 1.0 (full right)
    # MusicBrainz metadata
    mbid = Column(String, nullable=True)           # MusicBrainz recording ID
    mbid_release = Column(String, nullable=True)   # MusicBrainz release ID (for covers)
    album = Column(String, nullable=True)          # Album name
    duration = Column(Float, nullable=True)        # Duration in seconds
    # Download status: 'downloaded', 'pending', 'downloading', 'known'
    download_status = Column(String, default="downloaded")
    removed_at = Column(Float, nullable=True)      # timestamp when removed from all playlists


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    cover_path = Column(String, nullable=True)
    cover_zoom = Column(Float, default=1.0)
    cover_offset_x = Column(Float, default=0.0)
    cover_offset_y = Column(Float, default=0.0)
    is_default = Column(Integer, default=0)  # 1 for "Perso", not deletable
    position = Column(Integer, default=999)  # ordering, lower = higher
    created_at = Column(DateTime, default=datetime.utcnow)

    entries = relationship("PlaylistTrack", back_populates="playlist", cascade="all, delete-orphan")


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"

    id = Column(Integer, primary_key=True, index=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False)
    track_id = Column(Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)

    playlist = relationship("Playlist", back_populates="entries")
    track = relationship("Track")

    __table_args__ = (
        UniqueConstraint("playlist_id", "track_id", name="uq_playlist_track"),
    )


# ── Sync — Journal de modifications ───────────────────────────

class ChangeLog(Base):
    __tablename__ = "change_log"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, nullable=False)        # UUID de l'appareil source
    entity_type = Column(String, nullable=False)       # "track", "playlist", "playlist_track"
    entity_id = Column(Integer, nullable=False)        # ID de l'entité modifiée
    action = Column(String, nullable=False)            # "create", "update", "delete"
    data = Column(String, nullable=True)               # JSON snapshot (pour create/update)
    timestamp = Column(Float, nullable=False)          # Unix timestamp précis


class ListenHistory(Base):
    __tablename__ = "listen_history"

    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    listened_at = Column(DateTime, default=datetime.utcnow)
    duration_seconds = Column(Float, default=0)
    device_id = Column(String, nullable=True)  # UUID de l'appareil source

    track = relationship("Track")


class SyncDevice(Base):
    __tablename__ = "sync_devices"

    id = Column(String, primary_key=True)              # UUID unique par appareil
    name = Column(String, nullable=False)              # "PC de Tom", "Téléphone"
    last_sync = Column(Float, nullable=True)           # Dernier sync réussi (unix timestamp)
    created_at = Column(DateTime, default=datetime.utcnow)


# Device ID persistant pour ce PC (généré une fois, stocké en DB)
_device_id_cache = None

def get_device_id(db: Session) -> str:
    global _device_id_cache
    if _device_id_cache:
        return _device_id_cache
    device = db.query(SyncDevice).filter(SyncDevice.name == "__local__").first()
    if not device:
        device = SyncDevice(id=str(_uuid.uuid4()), name="__local__")
        db.add(device)
        db.commit()
    _device_id_cache = device.id
    return device.id


def record_change(db: Session, entity_type: str, entity_id: int, action: str, data: dict = None):
    """Enregistre une modification dans le journal de sync."""
    device_id = get_device_id(db)
    entry = ChangeLog(
        device_id=device_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        data=_json.dumps(data, default=str) if data else None,
        timestamp=_time.time()
    )
    db.add(entry)
    db.commit()


def get_changes_since(db: Session, since: float, exclude_device: str = None) -> list[dict]:
    """Récupère les changements depuis un timestamp, en excluant un device."""
    query = db.query(ChangeLog).filter(ChangeLog.timestamp > since)
    if exclude_device:
        query = query.filter(ChangeLog.device_id != exclude_device)
    entries = query.order_by(ChangeLog.timestamp.asc()).all()
    return [{
        "id": e.id, "device_id": e.device_id, "entity_type": e.entity_type,
        "entity_id": e.entity_id, "action": e.action,
        "data": _json.loads(e.data) if e.data else None, "timestamp": e.timestamp
    } for e in entries]


def cleanup_changelog(db: Session):
    """Supprime les entrées du journal déjà synchronisées par tous les appareils."""
    devices = db.query(SyncDevice).filter(SyncDevice.name != "__local__").all()
    if not devices:
        return 0  # Pas d'appareils distants, on garde tout
    min_sync = min(d.last_sync for d in devices if d.last_sync is not None)
    if min_sync is None:
        return 0
    count = db.query(ChangeLog).filter(ChangeLog.timestamp <= min_sync).delete()
    db.commit()
    return count


def track_to_dict(track: "Track") -> dict:
    """Sérialise un Track en dict pour le changelog."""
    return {
        "id": track.id, "title": track.title, "artist": track.artist,
        "file_path": track.file_path, "youtube_url": track.youtube_url,
        "added_at": str(track.added_at), "play_count": track.play_count or 0,
        "volume_coeff": track.volume_coeff or 1.0,
        "start_time": track.start_time, "end_time": track.end_time,
        "cover_path": track.cover_path, "cover_zoom": track.cover_zoom or 1.0,
        "cover_offset_x": track.cover_offset_x or 0.0, "cover_offset_y": track.cover_offset_y or 0.0,
        "stereo_balance": track.stereo_balance or 0.0,
        "mbid": track.mbid, "mbid_release": track.mbid_release,
        "album": track.album, "duration": track.duration,
        "download_status": track.download_status or "downloaded",
        "removed_at": track.removed_at,
    }


def listen_history_to_dict(lh: "ListenHistory") -> dict:
    return {
        "id": lh.id, "track_id": lh.track_id,
        "listened_at": str(lh.listened_at), "duration_seconds": lh.duration_seconds,
        "device_id": lh.device_id,
    }


def playlist_to_dict(playlist: "Playlist") -> dict:
    """Sérialise une Playlist en dict pour le changelog."""
    return {
        "id": playlist.id, "name": playlist.name, "cover_path": playlist.cover_path,
        "cover_zoom": playlist.cover_zoom or 1.0,
        "cover_offset_x": playlist.cover_offset_x or 0.0, "cover_offset_y": playlist.cover_offset_y or 0.0,
        "is_default": playlist.is_default, "position": playlist.position,
        "created_at": str(playlist.created_at),
    }


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migration for existing DBs (SQLite doesn't support ADD COLUMN IF NOT EXISTS)
    with engine.connect() as conn:
        for sql in [
            "ALTER TABLE tracks ADD COLUMN play_count INTEGER DEFAULT 0",
            "ALTER TABLE tracks ADD COLUMN volume_coeff REAL DEFAULT 1.0",
            "ALTER TABLE tracks ADD COLUMN start_time REAL",
            "ALTER TABLE tracks ADD COLUMN end_time REAL",
            "ALTER TABLE tracks ADD COLUMN cover_path TEXT",
            "ALTER TABLE tracks ADD COLUMN cover_zoom REAL DEFAULT 1.0",
            "ALTER TABLE tracks ADD COLUMN cover_offset_x REAL DEFAULT 0.0",
            "ALTER TABLE tracks ADD COLUMN cover_offset_y REAL DEFAULT 0.0",
            "ALTER TABLE tracks ADD COLUMN stereo_balance REAL DEFAULT 0.0",
            "ALTER TABLE tracks ADD COLUMN mbid TEXT",
            "ALTER TABLE tracks ADD COLUMN mbid_release TEXT",
            "ALTER TABLE tracks ADD COLUMN album TEXT",
            "ALTER TABLE tracks ADD COLUMN duration REAL",
            "ALTER TABLE tracks ADD COLUMN download_status TEXT DEFAULT 'downloaded'",
            "ALTER TABLE tracks ADD COLUMN removed_at REAL",
            "ALTER TABLE playlists ADD COLUMN position INTEGER DEFAULT 999",
            "ALTER TABLE playlists ADD COLUMN cover_zoom REAL DEFAULT 1.0",
            "ALTER TABLE playlists ADD COLUMN cover_offset_x REAL DEFAULT 0.0",
            "ALTER TABLE playlists ADD COLUMN cover_offset_y REAL DEFAULT 0.0",
        ]:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass
    # Listen history migration
    for sql in [
        "ALTER TABLE listen_history ADD COLUMN device_id TEXT",
    ]:
        try:
            conn.execute(text(sql))
            conn.commit()
        except Exception:
            pass
    # Create default playlists if they don't exist
    db = SessionLocal()
    try:
        # Coup de coeur playlist
        fav = db.query(Playlist).filter(Playlist.name == "Coup de cœur", Playlist.is_default == 1).first()
        if not fav:
            fav = Playlist(name="Coup de cœur", is_default=1, position=0)
            db.add(fav)
            db.commit()
            db.refresh(fav)
            # Add all existing tracks to Coup de cœur
            all_tracks = db.query(Track).all()
            for t in all_tracks:
                existing = db.query(PlaylistTrack).filter(
                    PlaylistTrack.playlist_id == fav.id,
                    PlaylistTrack.track_id == t.id
                ).first()
                if not existing:
                    db.add(PlaylistTrack(playlist_id=fav.id, track_id=t.id))
            db.commit()
        elif fav.position is None or fav.position != 0:
            fav.position = 0
            db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Track CRUD ─────────────────────────────────────────────────

def add_track(db: Session, title: str, artist: str, file_path: str, youtube_url: str = None) -> Track:
    track = Track(title=title, artist=artist, file_path=file_path, youtube_url=youtube_url)
    db.add(track)
    db.commit()
    db.refresh(track)
    return track


def get_tracks(db: Session, search: str = None, sort_by: str = "added_at") -> list[Track]:
    query = db.query(Track)

    if search:
        pattern = f"%{search}%"
        query = query.filter(
            Track.title.ilike(pattern) | Track.artist.ilike(pattern)
        )

    sort_map = {
        "added_at": Track.added_at.desc(),
        "title": Track.title.asc(),
        "artist": Track.artist.asc(),
    }
    order = sort_map.get(sort_by, Track.added_at.desc())
    query = query.order_by(order)

    return query.all()


def delete_track(db: Session, track_id: int) -> bool:
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        return False
    db.delete(track)
    db.commit()
    return True


# ── Playlist CRUD ──────────────────────────────────────────────

def get_playlists(db: Session) -> list[Playlist]:
    return db.query(Playlist).order_by(Playlist.position.asc(), Playlist.created_at.asc()).all()


def create_playlist(db: Session, name: str) -> Playlist:
    playlist = Playlist(name=name)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return playlist


def update_playlist(db: Session, playlist_id: int, name: str = None, cover_path: str = None,
                    cover_zoom: float = None, cover_offset_x: float = None, cover_offset_y: float = None) -> Playlist | None:
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        return None
    if name is not None:
        playlist.name = name.strip()
    if cover_path is not None:
        playlist.cover_path = cover_path
    if cover_zoom is not None:
        playlist.cover_zoom = max(0.5, min(5.0, cover_zoom))
    if cover_offset_x is not None:
        playlist.cover_offset_x = max(-1.0, min(1.0, cover_offset_x))
    if cover_offset_y is not None:
        playlist.cover_offset_y = max(-1.0, min(1.0, cover_offset_y))
    db.commit()
    db.refresh(playlist)
    return playlist


def delete_playlist(db: Session, playlist_id: int) -> bool:
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist or (playlist.is_default and playlist.name == "Coup de cœur"):
        return False
    db.delete(playlist)
    db.commit()
    return True


def add_track_to_playlist(db: Session, playlist_id: int, track_id: int) -> PlaylistTrack | None:
    existing = db.query(PlaylistTrack).filter(
        PlaylistTrack.playlist_id == playlist_id,
        PlaylistTrack.track_id == track_id
    ).first()
    if existing:
        return existing
    entry = PlaylistTrack(playlist_id=playlist_id, track_id=track_id)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def remove_track_from_playlist(db: Session, playlist_id: int, track_id: int) -> bool:
    entry = db.query(PlaylistTrack).filter(
        PlaylistTrack.playlist_id == playlist_id,
        PlaylistTrack.track_id == track_id
    ).first()
    if not entry:
        return False
    db.delete(entry)
    db.commit()
    return True


def get_playlist_tracks(db: Session, playlist_id: int) -> list[dict]:
    entries = (
        db.query(PlaylistTrack)
        .filter(PlaylistTrack.playlist_id == playlist_id)
        .order_by(PlaylistTrack.added_at.desc())
        .all()
    )
    result = []
    for e in entries:
        if e.track:
            result.append({
                "id": e.track.id,
                "title": e.track.title,
                "artist": e.track.artist,
                "file_path": e.track.file_path,
                "youtube_url": e.track.youtube_url,
                "added_at": e.added_at,  # date d'ajout dans la playlist
                "library_added_at": e.track.added_at, # date d'ajout dans la bibliothèque
                "play_count": e.track.play_count or 0,
                "volume_coeff": e.track.volume_coeff if e.track.volume_coeff is not None else 1.0,
                "start_time": e.track.start_time,
                "end_time": e.track.end_time,
                "cover_path": e.track.cover_path,
                "cover_zoom": e.track.cover_zoom if e.track.cover_zoom is not None else 1.0,
                "cover_offset_x": e.track.cover_offset_x if e.track.cover_offset_x is not None else 0.0,
                "cover_offset_y": e.track.cover_offset_y if e.track.cover_offset_y is not None else 0.0,
                "stereo_balance": e.track.stereo_balance if e.track.stereo_balance is not None else 0.0,
            })
    return result
