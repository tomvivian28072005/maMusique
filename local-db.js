// ── Clom Local DB (Capacitor SQLite) ──────────────────────
// Miroir du schéma Python database.py, pour fonctionnement offline sur mobile.

const DB_NAME = 'clom_music';

let _db = null;       // CapacitorSQLite ref
let _deviceId = null;  // UUID local

// ── Init ──────────────────────────────────────────────────

async function initLocalDB() {
  const { CapacitorSQLite } = Capacitor.Plugins;
  _db = CapacitorSQLite;

  // Créer / ouvrir la DB
  await _db.createConnection({ database: DB_NAME, version: 1, encrypted: false, mode: 'no-encryption' });
  await _db.open({ database: DB_NAME });

  // Créer les tables
  const schema = `
    CREATE TABLE IF NOT EXISTS tracks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      artist TEXT NOT NULL DEFAULT 'Unknown',
      file_path TEXT NOT NULL UNIQUE,
      youtube_url TEXT,
      added_at TEXT DEFAULT (datetime('now')),
      play_count INTEGER DEFAULT 0,
      volume_coeff REAL DEFAULT 1.0,
      start_time REAL,
      end_time REAL,
      cover_path TEXT,
      cover_zoom REAL DEFAULT 1.0,
      cover_offset_x REAL DEFAULT 0.0,
      cover_offset_y REAL DEFAULT 0.0
    );
    CREATE TABLE IF NOT EXISTS playlists (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      cover_path TEXT,
      cover_zoom REAL DEFAULT 1.0,
      cover_offset_x REAL DEFAULT 0.0,
      cover_offset_y REAL DEFAULT 0.0,
      is_default INTEGER DEFAULT 0,
      position INTEGER DEFAULT 999,
      created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS playlist_tracks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      playlist_id INTEGER NOT NULL,
      track_id INTEGER NOT NULL,
      added_at TEXT DEFAULT (datetime('now')),
      UNIQUE(playlist_id, track_id),
      FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
      FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS change_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL,
      entity_type TEXT NOT NULL,
      entity_id INTEGER NOT NULL,
      action TEXT NOT NULL,
      data TEXT,
      timestamp REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sync_devices (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      last_sync REAL,
      created_at TEXT DEFAULT (datetime('now'))
    );
    PRAGMA foreign_keys = ON;
  `;
  await _db.execute({ database: DB_NAME, statements: schema });

  // Créer la playlist "Coup de cœur" par défaut si elle n'existe pas
  const fav = await dbQuery("SELECT id FROM playlists WHERE name = 'Coup de cœur' AND is_default = 1");
  if (fav.length === 0) {
    await dbRun("INSERT INTO playlists (name, is_default, position) VALUES ('Coup de cœur', 1, 0)");
  }

  // Device ID local
  const dev = await dbQuery("SELECT id FROM sync_devices WHERE name = '__local__'");
  if (dev.length === 0) {
    _deviceId = crypto.randomUUID();
    await dbRun("INSERT INTO sync_devices (id, name) VALUES (?, '__local__')", [_deviceId]);
  } else {
    _deviceId = dev[0].id;
  }

  console.log('[Clom] Local DB ready, device:', _deviceId);
}

// ── Helpers SQL ───────────────────────────────────────────

async function dbQuery(sql, params = []) {
  const res = await _db.query({ database: DB_NAME, statement: sql, values: params });
  return res.values || [];
}

async function dbRun(sql, params = []) {
  const res = await _db.run({ database: DB_NAME, statement: sql, values: params });
  return res.changes || { changes: 0, lastId: 0 };
}

// ── Tracks CRUD ───────────────────────────────────────────

async function localGetTracks(search, sortBy) {
  let sql = "SELECT * FROM tracks";
  const params = [];
  if (search) {
    sql += " WHERE title LIKE ? OR artist LIKE ?";
    params.push(`%${search}%`, `%${search}%`);
  }
  const sortMap = {
    'added_at': 'added_at DESC',
    'title': 'title ASC COLLATE NOCASE',
    'artist': 'artist ASC COLLATE NOCASE',
  };
  sql += ` ORDER BY ${sortMap[sortBy] || 'added_at DESC'}`;
  return await dbQuery(sql, params);
}

async function localDeleteTrack(id) {
  // Supprimer le fichier audio et la cover
  const track = (await dbQuery("SELECT file_path, cover_path FROM tracks WHERE id = ?", [id]))[0];
  if (track) {
    await deleteLocalFile(track.file_path);
    if (track.cover_path) await deleteLocalFile(track.cover_path.split('?')[0]);
  }
  await dbRun("DELETE FROM tracks WHERE id = ?", [id]);
}

async function localUpdateTrack(id, body) {
  const track = (await dbQuery("SELECT * FROM tracks WHERE id = ?", [id]))[0];
  if (!track) return null;

  const sets = [];
  const params = [];

  if (body.title !== undefined) { sets.push("title = ?"); params.push(body.title.trim()); }
  if (body.artist !== undefined) { sets.push("artist = ?"); params.push(body.artist.trim()); }
  if (body.volume_coeff !== undefined) {
    sets.push("volume_coeff = ?");
    params.push(Math.max(0.5, Math.min(5.0, body.volume_coeff)));
  }
  if (body.start_time !== undefined) { sets.push("start_time = ?"); params.push(Math.max(0, body.start_time)); }
  if (body.end_time !== undefined) { sets.push("end_time = ?"); params.push(Math.max(0, body.end_time)); }
  if (body.clear_start_time) { sets.push("start_time = NULL"); }
  if (body.clear_end_time) { sets.push("end_time = NULL"); }
  if (body.cover_zoom !== undefined) {
    sets.push("cover_zoom = ?");
    params.push(Math.max(0.5, Math.min(3.0, body.cover_zoom)));
  }
  if (body.cover_offset_x !== undefined) {
    sets.push("cover_offset_x = ?");
    params.push(Math.max(-1, Math.min(1, body.cover_offset_x)));
  }
  if (body.cover_offset_y !== undefined) {
    sets.push("cover_offset_y = ?");
    params.push(Math.max(-1, Math.min(1, body.cover_offset_y)));
  }
  if (body.clear_cover) {
    if (track.cover_path) await deleteLocalFile(track.cover_path.split('?')[0]);
    sets.push("cover_path = NULL");
  }

  if (sets.length > 0) {
    params.push(id);
    await dbRun(`UPDATE tracks SET ${sets.join(', ')} WHERE id = ?`, params);
  }

  return (await dbQuery("SELECT * FROM tracks WHERE id = ?", [id]))[0];
}

async function localUploadTrackCover(id, file) {
  const ext = file.name.split('.').pop().toLowerCase();
  const fileName = `track_${id}.${ext}`;
  const filePath = `/covers/${fileName}`;
  await saveLocalFile(filePath, file);
  const coverPath = `${filePath}?t=${Date.now()}`;
  await dbRun("UPDATE tracks SET cover_path = ? WHERE id = ?", [coverPath, id]);
  return coverPath;
}

async function localIncrementPlayCount(id) {
  await dbRun("UPDATE tracks SET play_count = COALESCE(play_count, 0) + 1 WHERE id = ?", [id]);
  const t = (await dbQuery("SELECT play_count FROM tracks WHERE id = ?", [id]))[0];
  return t ? t.play_count : 1;
}

async function localToggleFavorite(id) {
  const fav = (await dbQuery("SELECT id FROM playlists WHERE is_default = 1 LIMIT 1"))[0];
  if (!fav) return { favorite: false };
  const existing = (await dbQuery(
    "SELECT id FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
    [fav.id, id]
  ))[0];
  if (existing) {
    await dbRun("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", [fav.id, id]);
    return { favorite: false };
  } else {
    await dbRun("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)", [fav.id, id]);
    return { favorite: true };
  }
}

async function localGetTrackPlaylists(id) {
  const rows = await dbQuery("SELECT playlist_id FROM playlist_tracks WHERE track_id = ?", [id]);
  return rows.map(r => r.playlist_id);
}

// ── Playlists CRUD ────────────────────────────────────────

async function localGetPlaylists() {
  const playlists = await dbQuery("SELECT * FROM playlists ORDER BY position ASC, created_at ASC");
  // Ajouter track_count
  for (const p of playlists) {
    const cnt = (await dbQuery("SELECT COUNT(*) as c FROM playlist_tracks WHERE playlist_id = ?", [p.id]))[0];
    p.track_count = cnt ? cnt.c : 0;
    p.is_default = !!p.is_default;
  }
  return playlists;
}

async function localCreatePlaylist(name) {
  const res = await dbRun("INSERT INTO playlists (name) VALUES (?)", [name.trim()]);
  return { id: res.lastId, name: name.trim() };
}

async function localUpdatePlaylist(id, body) {
  const pl = (await dbQuery("SELECT * FROM playlists WHERE id = ?", [id]))[0];
  if (!pl) return null;

  const sets = [];
  const params = [];

  if (body.name !== undefined) { sets.push("name = ?"); params.push(body.name.trim()); }
  if (body.cover_zoom !== undefined) {
    sets.push("cover_zoom = ?");
    params.push(Math.max(0.5, Math.min(3.0, body.cover_zoom)));
  }
  if (body.cover_offset_x !== undefined) {
    sets.push("cover_offset_x = ?");
    params.push(Math.max(-1, Math.min(1, body.cover_offset_x)));
  }
  if (body.cover_offset_y !== undefined) {
    sets.push("cover_offset_y = ?");
    params.push(Math.max(-1, Math.min(1, body.cover_offset_y)));
  }
  if (body.clear_cover) {
    if (pl.cover_path) await deleteLocalFile(pl.cover_path.split('?')[0]);
    sets.push("cover_path = NULL");
  }

  if (sets.length > 0) {
    params.push(id);
    await dbRun(`UPDATE playlists SET ${sets.join(', ')} WHERE id = ?`, params);
  }

  return (await dbQuery("SELECT * FROM playlists WHERE id = ?", [id]))[0];
}

async function localDeletePlaylist(id) {
  const pl = (await dbQuery("SELECT is_default, name FROM playlists WHERE id = ?", [id]))[0];
  if (!pl) return false;
  if (pl.is_default && pl.name === "Coup de cœur") return false;
  await dbRun("DELETE FROM playlists WHERE id = ?", [id]);
  return true;
}

async function localReorderPlaylists(playlistIds) {
  for (let i = 0; i < playlistIds.length; i++) {
    await dbRun("UPDATE playlists SET position = ? WHERE id = ?", [i, playlistIds[i]]);
  }
}

async function localUploadPlaylistCover(id, file) {
  const ext = file.name.split('.').pop().toLowerCase();
  const fileName = `playlist_${id}.${ext}`;
  const filePath = `/covers/${fileName}`;
  await saveLocalFile(filePath, file);
  const coverPath = `${filePath}?t=${Date.now()}`;
  await dbRun("UPDATE playlists SET cover_path = ? WHERE id = ?", [coverPath, id]);
  return coverPath;
}

// ── Playlist Tracks ───────────────────────────────────────

async function localGetPlaylistTracks(playlistId) {
  return await dbQuery(`
    SELECT t.*, pt.added_at as pt_added_at, t.added_at as library_added_at
    FROM playlist_tracks pt
    JOIN tracks t ON t.id = pt.track_id
    WHERE pt.playlist_id = ?
    ORDER BY pt.added_at DESC
  `, [playlistId]);
}

async function localAddTrackToPlaylist(playlistId, trackId) {
  await dbRun("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)", [playlistId, trackId]);
}

async function localRemoveTrackFromPlaylist(playlistId, trackId) {
  await dbRun("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", [playlistId, trackId]);
}

// ── Favorites ─────────────────────────────────────────────

async function localGetFavorites() {
  const fav = (await dbQuery("SELECT id FROM playlists WHERE is_default = 1 LIMIT 1"))[0];
  if (!fav) return { playlist_id: null, track_ids: [] };
  const rows = await dbQuery("SELECT track_id FROM playlist_tracks WHERE playlist_id = ?", [fav.id]);
  return { playlist_id: fav.id, track_ids: rows.map(r => r.track_id) };
}

// ── Duration ──────────────────────────────────────────────

async function localGetPlaylistDuration(playlistId) {
  // On ne peut pas calculer la durée exacte sans décoder les MP3
  // Pour l'instant on retourne 0 (le frontend gère le cas gracieusement)
  return { total_seconds: 0 };
}

// ── Filesystem helpers (Capacitor) ────────────────────────

async function saveLocalFile(path, fileOrBlob) {
  const { Filesystem } = Capacitor.Plugins;
  // Lire le fichier en base64
  const base64 = await new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result.split(',')[1]);
    reader.readAsDataURL(fileOrBlob);
  });
  // Déterminer le bon chemin dans le dossier Clom
  const cleanPath = path.startsWith('/') ? path.substring(1) : path;
  await Filesystem.writeFile({
    path: `Clom/${cleanPath}`,
    data: base64,
    directory: 'EXTERNAL',
    recursive: true
  });
}

async function readLocalFileAsUrl(path) {
  if (!path) return path;
  const { Filesystem } = Capacitor.Plugins;
  const cleanPath = path.startsWith('/') ? path.substring(1) : path;
  try {
    const result = await Filesystem.getUri({
      path: `Clom/${cleanPath}`,
      directory: 'EXTERNAL'
    });
    return Capacitor.convertFileSrc(result.uri);
  } catch {
    return path;
  }
}

async function deleteLocalFile(path) {
  if (!path) return;
  const { Filesystem } = Capacitor.Plugins;
  const cleanPath = path.startsWith('/') ? path.substring(1) : path;
  try {
    await Filesystem.deleteFile({
      path: `Clom/${cleanPath}`,
      directory: 'EXTERNAL'
    });
  } catch {
    // Fichier n'existe pas, on ignore
  }
}

// ── Export pour utilisation dans api-local.js ──────────────
// (tout est global car c'est du vanilla JS inline)
