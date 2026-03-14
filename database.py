from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session, relationship
from datetime import datetime

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
    # Create default playlists if they don't exist
    db = SessionLocal()
    try:
        perso = db.query(Playlist).filter(Playlist.name == "Perso", Playlist.is_default == 1).first()
        if not perso:
            db.add(Playlist(name="Perso", is_default=1, position=1))
            db.commit()
        elif perso.position is None or perso.position == 999:
            perso.position = 1
            db.commit()
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
        playlist.cover_zoom = max(0.5, min(3.0, cover_zoom))
    if cover_offset_x is not None:
        playlist.cover_offset_x = max(-1.0, min(1.0, cover_offset_x))
    if cover_offset_y is not None:
        playlist.cover_offset_y = max(-1.0, min(1.0, cover_offset_y))
    db.commit()
    db.refresh(playlist)
    return playlist


def delete_playlist(db: Session, playlist_id: int) -> bool:
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist or playlist.is_default:
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
            })
    return result
