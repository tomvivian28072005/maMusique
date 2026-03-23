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
      cover_offset_y REAL DEFAULT 0.0,
      stereo_balance REAL DEFAULT 0.0
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
    CREATE TABLE IF NOT EXISTS listen_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      track_id INTEGER NOT NULL,
      listened_at TEXT DEFAULT (datetime('now')),
      duration_seconds REAL DEFAULT 0,
      FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
    );
    PRAGMA foreign_keys = ON;
  `;
  await _db.execute({ database: DB_NAME, statements: schema });

  // Migrations for existing DBs
  for (const sql of [
    "ALTER TABLE tracks ADD COLUMN stereo_balance REAL DEFAULT 0.0",
    "ALTER TABLE tracks ADD COLUMN duration REAL",
    "ALTER TABLE listen_history ADD COLUMN device_id TEXT",
  ]) {
    try { await _db.execute({ database: DB_NAME, statements: sql }); } catch (e) { /* already exists */ }
  }

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

  // Reprendre le heartbeat si on connaît un PC
  resumeHeartbeatIfKnown();
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

// ── Change Log ────────────────────────────────────────────

async function recordLocalChange(entityType, entityId, action, data = null) {
  await dbRun(
    "INSERT INTO change_log (device_id, entity_type, entity_id, action, data, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
    [_deviceId, entityType, entityId, action, data ? JSON.stringify(data) : null, Date.now() / 1000]
  );
}

function localTrackToDict(t) {
  return {
    id: t.id, title: t.title, artist: t.artist,
    file_path: t.file_path, youtube_url: t.youtube_url,
    added_at: t.added_at, play_count: t.play_count || 0,
    volume_coeff: t.volume_coeff || 1.0,
    start_time: t.start_time, end_time: t.end_time,
    cover_path: t.cover_path, cover_zoom: t.cover_zoom || 1.0,
    cover_offset_x: t.cover_offset_x || 0.0, cover_offset_y: t.cover_offset_y || 0.0,
    stereo_balance: t.stereo_balance || 0.0,
  };
}

function localPlaylistToDict(p) {
  return {
    id: p.id, name: p.name, cover_path: p.cover_path,
    cover_zoom: p.cover_zoom || 1.0,
    cover_offset_x: p.cover_offset_x || 0.0, cover_offset_y: p.cover_offset_y || 0.0,
    is_default: p.is_default ? 1 : 0, position: p.position,
    created_at: p.created_at,
  };
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
    'title': 'LOWER(title) ASC',
    'artist': 'LOWER(artist) ASC',
  };
  sql += ` ORDER BY ${sortMap[sortBy] || 'added_at DESC'}`;
  return await dbQuery(sql, params);
}

async function localDeleteTrack(id) {
  const track = (await dbQuery("SELECT file_path, cover_path FROM tracks WHERE id = ?", [id]))[0];
  if (track) {
    await deleteLocalFile(track.file_path);
    if (track.cover_path) await deleteLocalFile(track.cover_path.split('?')[0]);
  }
  await dbRun("DELETE FROM tracks WHERE id = ?", [id]);
  await recordLocalChange("track", id, "delete");
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
  if (body.stereo_balance !== undefined) {
    sets.push("stereo_balance = ?");
    params.push(Math.max(-1, Math.min(1, body.stereo_balance)));
  }
  if (body.clear_cover) {
    if (track.cover_path) await deleteLocalFile(track.cover_path.split('?')[0]);
    sets.push("cover_path = NULL");
  }

  if (sets.length > 0) {
    params.push(id);
    await dbRun(`UPDATE tracks SET ${sets.join(', ')} WHERE id = ?`, params);
  }

  const updated = (await dbQuery("SELECT * FROM tracks WHERE id = ?", [id]))[0];
  if (updated) await recordLocalChange("track", id, "update", localTrackToDict(updated));
  return updated;
}

async function localUploadTrackCover(id, file) {
  const ext = file.name.split('.').pop().toLowerCase();
  const fileName = `track_${id}.${ext}`;
  const filePath = `/covers/${fileName}`;
  await saveLocalFile(filePath, file);
  const coverPath = `${filePath}?t=${Date.now()}`;
  await dbRun("UPDATE tracks SET cover_path = ? WHERE id = ?", [coverPath, id]);
  const updated = (await dbQuery("SELECT * FROM tracks WHERE id = ?", [id]))[0];
  if (updated) await recordLocalChange("track", id, "update", localTrackToDict(updated));
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
    await recordLocalChange("playlist_track", existing.id, "delete", { playlist_id: fav.id, track_id: id });
    return { favorite: false };
  } else {
    const res = await dbRun("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)", [fav.id, id]);
    await recordLocalChange("playlist_track", res.lastId, "create", { playlist_id: fav.id, track_id: id });
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
  const pl = (await dbQuery("SELECT * FROM playlists WHERE id = ?", [res.lastId]))[0];
  if (pl) await recordLocalChange("playlist", res.lastId, "create", localPlaylistToDict(pl));
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

  const updated = (await dbQuery("SELECT * FROM playlists WHERE id = ?", [id]))[0];
  if (updated) await recordLocalChange("playlist", id, "update", localPlaylistToDict(updated));
  return updated;
}

async function localDeletePlaylist(id) {
  const pl = (await dbQuery("SELECT is_default, name FROM playlists WHERE id = ?", [id]))[0];
  if (!pl) return false;
  if (pl.is_default && pl.name === "Coup de cœur") return false;
  await dbRun("DELETE FROM playlists WHERE id = ?", [id]);
  await recordLocalChange("playlist", id, "delete");
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
  const updated = (await dbQuery("SELECT * FROM playlists WHERE id = ?", [id]))[0];
  if (updated) await recordLocalChange("playlist", id, "update", localPlaylistToDict(updated));
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
  const res = await dbRun("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)", [playlistId, trackId]);
  await recordLocalChange("playlist_track", res.lastId, "create", { playlist_id: playlistId, track_id: trackId });
}

async function localRemoveTrackFromPlaylist(playlistId, trackId) {
  const existing = (await dbQuery("SELECT id FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", [playlistId, trackId]))[0];
  await dbRun("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", [playlistId, trackId]);
  if (existing) await recordLocalChange("playlist_track", existing.id, "delete", { playlist_id: playlistId, track_id: trackId });
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

// ── Sync Engine ──────────────────────────────────────────

let _syncInProgress = false;

async function getLocalChanges(since = 0) {
  const rows = await dbQuery(
    "SELECT * FROM change_log WHERE timestamp > ? ORDER BY timestamp ASC",
    [since]
  );
  return rows.map(r => ({
    id: r.id, device_id: r.device_id, entity_type: r.entity_type,
    entity_id: r.entity_id, action: r.action,
    data: r.data ? JSON.parse(r.data) : null, timestamp: r.timestamp
  }));
}

async function getLocalManifest() {
  const tracks = await dbQuery("SELECT * FROM tracks");
  const playlists = await dbQuery("SELECT * FROM playlists");
  const pts = await dbQuery("SELECT playlist_id, track_id FROM playlist_tracks");
  const lh = await dbQuery("SELECT * FROM listen_history");
  return {
    device_id: _deviceId,
    tracks: tracks.map(localTrackToDict),
    playlists: playlists.map(localPlaylistToDict),
    playlist_tracks: pts,
    listen_history: lh.map(h => ({
      id: h.id, track_id: h.track_id, listened_at: h.listened_at,
      duration_seconds: h.duration_seconds, device_id: h.device_id
    }))
  };
}

async function applyRemoteChanges(changes) {
  let applied = 0;
  for (const change of changes) {
    try {
      const { entity_type, entity_id, action, data } = change;

      if (entity_type === "track") {
        if (action === "create" && data) {
          const existing = (await dbQuery("SELECT id FROM tracks WHERE id = ?", [entity_id]))[0];
          if (!existing) {
            await dbRun(
              `INSERT INTO tracks (id, title, artist, file_path, youtube_url, added_at, play_count,
               volume_coeff, start_time, end_time, cover_path, cover_zoom, cover_offset_x, cover_offset_y, stereo_balance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
              [entity_id, data.title, data.artist, data.file_path, data.youtube_url || null,
               data.added_at || new Date().toISOString(), data.play_count || 0,
               data.volume_coeff || 1.0, data.start_time || null, data.end_time || null,
               data.cover_path || null, data.cover_zoom || 1.0,
               data.cover_offset_x || 0.0, data.cover_offset_y || 0.0, data.stereo_balance || 0.0]
            );
            applied++;
          }
        } else if (action === "update" && data) {
          const existing = (await dbQuery("SELECT id FROM tracks WHERE id = ?", [entity_id]))[0];
          if (existing) {
            const sets = [];
            const params = [];
            for (const key of ["title", "artist", "volume_coeff", "start_time", "end_time",
                               "cover_path", "cover_zoom", "cover_offset_x", "cover_offset_y", "stereo_balance"]) {
              if (data[key] !== undefined) { sets.push(`${key} = ?`); params.push(data[key]); }
            }
            if (sets.length > 0) {
              params.push(entity_id);
              await dbRun(`UPDATE tracks SET ${sets.join(', ')} WHERE id = ?`, params);
              applied++;
            }
          }
        } else if (action === "delete") {
          const track = (await dbQuery("SELECT file_path, cover_path FROM tracks WHERE id = ?", [entity_id]))[0];
          if (track) {
            await deleteLocalFile(track.file_path);
            if (track.cover_path) await deleteLocalFile(track.cover_path.split('?')[0]);
            await dbRun("DELETE FROM tracks WHERE id = ?", [entity_id]);
            applied++;
          }
        }
      } else if (entity_type === "playlist") {
        if (action === "create" && data) {
          const existing = (await dbQuery("SELECT id FROM playlists WHERE id = ?", [entity_id]))[0];
          if (!existing) {
            await dbRun(
              `INSERT INTO playlists (id, name, cover_path, cover_zoom, cover_offset_x, cover_offset_y,
               is_default, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
              [entity_id, data.name, data.cover_path || null, data.cover_zoom || 1.0,
               data.cover_offset_x || 0.0, data.cover_offset_y || 0.0,
               data.is_default || 0, data.position || 999, data.created_at || new Date().toISOString()]
            );
            applied++;
          }
        } else if (action === "update" && data) {
          const existing = (await dbQuery("SELECT id FROM playlists WHERE id = ?", [entity_id]))[0];
          if (existing) {
            const sets = [];
            const params = [];
            for (const key of ["name", "cover_path", "cover_zoom", "cover_offset_x", "cover_offset_y", "position"]) {
              if (data[key] !== undefined) { sets.push(`${key} = ?`); params.push(data[key]); }
            }
            if (sets.length > 0) {
              params.push(entity_id);
              await dbRun(`UPDATE playlists SET ${sets.join(', ')} WHERE id = ?`, params);
              applied++;
            }
          }
        } else if (action === "delete") {
          const pl = (await dbQuery("SELECT is_default, name FROM playlists WHERE id = ?", [entity_id]))[0];
          if (pl && !(pl.is_default && pl.name === "Coup de cœur")) {
            await dbRun("DELETE FROM playlists WHERE id = ?", [entity_id]);
            applied++;
          }
        }
      } else if (entity_type === "playlist_track") {
        if (action === "create" && data) {
          const existing = (await dbQuery(
            "SELECT id FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            [data.playlist_id, data.track_id]
          ))[0];
          if (!existing) {
            await dbRun("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)",
              [data.playlist_id, data.track_id]);
            applied++;
          }
        } else if (action === "delete" && data) {
          await dbRun("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            [data.playlist_id, data.track_id]);
          applied++;
        }
      } else if (entity_type === "listen_history") {
        if (action === "create" && data) {
          // Dédup par track_id + listened_at + device_id
          const srcDevice = data.device_id || change.device_id;
          const existing = (await dbQuery(
            "SELECT id FROM listen_history WHERE track_id = ? AND listened_at = ? AND device_id = ?",
            [data.track_id, data.listened_at, srcDevice]
          ))[0];
          if (!existing) {
            await dbRun(
              "INSERT INTO listen_history (track_id, listened_at, duration_seconds, device_id) VALUES (?, ?, ?, ?)",
              [data.track_id, data.listened_at, data.duration_seconds || 0, srcDevice]
            );
            applied++;
          }
        }
      }
    } catch (err) {
      console.warn('[Clom Sync] Error applying change:', change, err);
    }
  }
  return applied;
}

let _syncAbortController = null;
let _heartbeatInterval = null;
let _lastKnownServer = null;

// Heartbeat: le téléphone s'annonce au PC toutes les 30s
function startDeviceHeartbeat(serverUrl) {
  _lastKnownServer = serverUrl;
  if (_heartbeatInterval) clearInterval(_heartbeatInterval);
  const sendHeartbeat = async () => {
    try {
      const trackCount = await dbGet("SELECT COUNT(*) as c FROM tracks");
      await fetch(`${serverUrl}/api/devices/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: _deviceId,
          name: 'Téléphone',
          device_type: 'mobile',
          track_count: trackCount?.[0]?.c || 0,
        })
      });
    } catch(e) { /* PC éteint, pas grave */ }
  };
  sendHeartbeat();
  _heartbeatInterval = setInterval(sendHeartbeat, 30000);
}

// Appelé au démarrage pour reprendre le heartbeat si on connaît un serveur
async function resumeHeartbeatIfKnown() {
  try {
    const devices = await dbGet("SELECT * FROM sync_devices WHERE name != '__local__' ORDER BY last_sync DESC LIMIT 1");
    if (devices && devices.length > 0) {
      startDeviceHeartbeat(devices[0].name);
    }
  } catch(e) { /* pas de serveur connu */ }
}

async function syncWithServer(serverUrl, onProgress) {
  if (_syncInProgress) throw new Error('Sync déjà en cours');
  _syncInProgress = true;
  _syncAbortController = new AbortController();
  const signal = _syncAbortController.signal;

  try {
    const report = (step, detail) => onProgress && onProgress(step, detail);
    const checkCancel = () => { if (signal.aborted) throw new Error('Synchronisation annulée'); };

    // 1. Récupérer le last_sync avec ce serveur
    report('init', 'Connexion au serveur...');
    const devices = await dbQuery("SELECT * FROM sync_devices WHERE name != '__local__'");
    const knownServer = devices.find(d => d.name === serverUrl);
    const lastSync = knownServer ? (knownServer.last_sync || 0) : 0;

    // 2. Récupérer les changements du serveur depuis notre dernier sync
    checkCancel();
    report('download', 'Récupération des changements...');
    const serverRes = await fetch(`${serverUrl}/api/sync/changes?since=${lastSync}&device_id=${_deviceId}`, { signal });
    if (!serverRes.ok) throw new Error('Serveur inaccessible');
    const serverData = await serverRes.json();
    const remoteDeviceId = serverData.device_id;
    const remoteChanges = serverData.changes;

    // 3. Récupérer nos changements locaux depuis le dernier sync
    const localChanges = await getLocalChanges(lastSync);

    // 4. Envoyer nos changements au serveur
    checkCancel();
    if (localChanges.length > 0) {
      report('upload', `Envoi de ${localChanges.length} changement(s)...`);
      const applyRes = await fetch(`${serverUrl}/api/sync/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: _deviceId,
          device_name: 'Mobile',
          changes: localChanges
        }),
        signal
      });
      const applyData = await applyRes.json();
      report('upload', `${applyData.applied} changement(s) appliqué(s) sur le serveur`);
    }

    // 5. Appliquer les changements du serveur localement
    if (remoteChanges.length > 0) {
      report('apply', `Application de ${remoteChanges.length} changement(s)...`);
      const applied = await applyRemoteChanges(remoteChanges);
      report('apply', `${applied} changement(s) appliqué(s) localement`);
    }

    // 6. Transférer les fichiers manquants (MP3 + covers)
    checkCancel();
    report('files', 'Vérification des fichiers...');
    const manifest = await getLocalManifest();
    const serverManifestRes = await fetch(`${serverUrl}/api/sync/manifest`, { signal });
    const serverManifest = await serverManifestRes.json();

    // Trouver les tracks qui existent sur le serveur mais pas localement
    const localTrackIds = new Set(manifest.tracks.map(t => t.id));
    const missingTracks = serverManifest.tracks.filter(t => !localTrackIds.has(t.id));

    // Trouver les tracks qu'on a mais dont le fichier manque
    const localTracksWithFiles = await dbQuery("SELECT id, file_path FROM tracks");
    const tracksNeedingFile = [];
    for (const t of localTracksWithFiles) {
      const exists = await localFileExists(t.file_path);
      if (!exists) tracksNeedingFile.push(t);
    }

    const toDownload = [...missingTracks.map(t => t.id), ...tracksNeedingFile.map(t => t.id)];
    const uniqueToDownload = [...new Set(toDownload)];

    if (uniqueToDownload.length > 0) {
      for (let i = 0; i < uniqueToDownload.length; i++) {
        const trackId = uniqueToDownload[i];
        checkCancel();
        report('files', `Téléchargement ${i + 1}/${uniqueToDownload.length}...`);

        try {
          // Télécharger le fichier audio
          const fileRes = await fetch(`${serverUrl}/api/sync/tracks/${trackId}/file`, { signal });
          if (!fileRes.ok) { console.warn(`[Sync] File ${trackId} not found on server`); continue; }

          const blob = await fileRes.blob();
          const serverTrack = serverManifest.tracks.find(t => t.id === trackId);
          if (!serverTrack) continue;

          // Sauvegarder le fichier localement
          const fileName = serverTrack.file_path.split('/').pop();
          const localPath = `/downloads/${fileName}`;
          await saveLocalFile(localPath, blob);

          // Créer/mettre à jour l'entrée DB si elle n'existe pas
          const existingLocal = (await dbQuery("SELECT id FROM tracks WHERE id = ?", [trackId]))[0];
          if (!existingLocal) {
            await dbRun(
              `INSERT INTO tracks (id, title, artist, file_path, youtube_url, added_at, play_count,
               volume_coeff, start_time, end_time, cover_path, cover_zoom, cover_offset_x, cover_offset_y, stereo_balance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
              [trackId, serverTrack.title, serverTrack.artist, localPath,
               serverTrack.youtube_url || null, serverTrack.added_at || new Date().toISOString(),
               serverTrack.play_count || 0, serverTrack.volume_coeff || 1.0,
               serverTrack.start_time || null, serverTrack.end_time || null,
               serverTrack.cover_path || null, serverTrack.cover_zoom || 1.0,
               serverTrack.cover_offset_x || 0.0, serverTrack.cover_offset_y || 0.0,
               serverTrack.stereo_balance || 0.0]
            );
          } else {
            await dbRun("UPDATE tracks SET file_path = ? WHERE id = ?", [localPath, trackId]);
          }

          // Télécharger la cover si elle existe
          if (serverTrack.cover_path) {
            try {
              const coverName = serverTrack.cover_path.split('?')[0].split('/').pop();
              const coverRes = await fetch(`${serverUrl}${serverTrack.cover_path.split('?')[0]}`);
              if (coverRes.ok) {
                const coverBlob = await coverRes.blob();
                const localCoverPath = `/covers/${coverName}`;
                await saveLocalFile(localCoverPath, coverBlob);
                await dbRun("UPDATE tracks SET cover_path = ? WHERE id = ?", [`${localCoverPath}?t=${Date.now()}`, trackId]);
              }
            } catch (e) { console.warn('[Sync] Cover download failed for track', trackId, e); }
          }
        } catch (err) {
          console.warn('[Sync] Failed to download track', trackId, err);
        }
      }
    }

    // Télécharger les covers de playlists manquantes
    for (const remotePl of serverManifest.playlists) {
      if (remotePl.cover_path) {
        const localPl = (await dbQuery("SELECT cover_path FROM playlists WHERE id = ?", [remotePl.id]))[0];
        if (localPl && !localPl.cover_path) {
          try {
            const coverName = remotePl.cover_path.split('?')[0].split('/').pop();
            const coverRes = await fetch(`${serverUrl}${remotePl.cover_path.split('?')[0]}`);
            if (coverRes.ok) {
              const coverBlob = await coverRes.blob();
              const localCoverPath = `/covers/${coverName}`;
              await saveLocalFile(localCoverPath, coverBlob);
              await dbRun("UPDATE playlists SET cover_path = ? WHERE id = ?", [`${localCoverPath}?t=${Date.now()}`, remotePl.id]);
            }
          } catch (e) { console.warn('[Sync] Playlist cover download failed', remotePl.id, e); }
        }
      }
    }

    // 6b. Envoyer au serveur les fichiers que le serveur n'a pas
    const serverTrackIds = new Set(serverManifest.tracks.map(t => t.id));
    const localOnlyTracks = manifest.tracks.filter(t => !serverTrackIds.has(t.id));
    // Aussi les tracks qu'on a en commun mais dont le serveur n'a pas le fichier
    const serverTracksNoFile = serverManifest.tracks.filter(t => {
      const localT = manifest.tracks.find(lt => lt.id === t.id);
      return localT && !t.has_file;
    });
    const tracksToUpload = [...localOnlyTracks, ...serverTracksNoFile];

    if (tracksToUpload.length > 0) {
      for (let i = 0; i < tracksToUpload.length; i++) {
        const t = tracksToUpload[i];
        checkCancel();
        report('files', `Envoi ${i + 1}/${tracksToUpload.length}...`);
        try {
          // Lire le fichier audio local
          const audioExists = await localFileExists(t.file_path);
          if (audioExists) {
            const audioUrl = await readLocalFileAsUrl(t.file_path);
            const audioRes = await fetch(audioUrl);
            if (audioRes.ok) {
              const audioBlob = await audioRes.blob();
              const fileName = t.file_path.split('/').pop();
              const formData = new FormData();
              formData.append('file', audioBlob, fileName);
              await fetch(`${serverUrl}/api/sync/tracks/${t.id}/upload`, {
                method: 'POST', body: formData, signal
              });
            }
          }
          // Envoyer la cover si elle existe
          if (t.cover_path) {
            const coverClean = t.cover_path.split('?')[0];
            const coverExists = await localFileExists(coverClean);
            if (coverExists) {
              const coverUrl = await readLocalFileAsUrl(coverClean);
              const coverRes = await fetch(coverUrl);
              if (coverRes.ok) {
                const coverBlob = await coverRes.blob();
                const coverName = coverClean.split('/').pop();
                const coverForm = new FormData();
                coverForm.append('file', coverBlob, coverName);
                await fetch(`${serverUrl}/api/sync/covers/upload?track_id=${t.id}`, {
                  method: 'POST', body: coverForm, signal
                });
              }
            }
          }
        } catch (e) { console.warn('[Sync] Upload track failed', t.id, e); }
      }
    }

    // 7. Sync des playlist_tracks manquants
    const localPts = new Set((await dbQuery("SELECT playlist_id, track_id FROM playlist_tracks"))
      .map(pt => `${pt.playlist_id}-${pt.track_id}`));
    for (const pt of serverManifest.playlist_tracks) {
      const key = `${pt.playlist_id}-${pt.track_id}`;
      if (!localPts.has(key)) {
        const plExists = (await dbQuery("SELECT id FROM playlists WHERE id = ?", [pt.playlist_id]))[0];
        const trExists = (await dbQuery("SELECT id FROM tracks WHERE id = ?", [pt.track_id]))[0];
        if (plExists && trExists) {
          await dbRun("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)",
            [pt.playlist_id, pt.track_id]);
        }
      }
    }

    // 8. Sync des listen_history manquants
    if (serverManifest.listen_history && serverManifest.listen_history.length > 0) {
      report('files', 'Synchronisation des statistiques...');
      const localLh = new Set((await dbQuery("SELECT track_id, listened_at, device_id FROM listen_history"))
        .map(h => `${h.track_id}-${h.listened_at}-${h.device_id}`));
      for (const rh of serverManifest.listen_history) {
        const key = `${rh.track_id}-${rh.listened_at}-${rh.device_id}`;
        if (!localLh.has(key)) {
          const trExists = (await dbQuery("SELECT id FROM tracks WHERE id = ?", [rh.track_id]))[0];
          if (trExists) {
            await dbRun(
              "INSERT INTO listen_history (track_id, listened_at, duration_seconds, device_id) VALUES (?, ?, ?, ?)",
              [rh.track_id, rh.listened_at, rh.duration_seconds || 0, rh.device_id]
            );
          }
        }
      }
    }

    // 9. Marquer la sync comme terminée
    report('complete', 'Finalisation...');
    await fetch(`${serverUrl}/api/sync/complete?device_id=${_deviceId}`, { method: 'POST' });

    // Mettre à jour le last_sync local
    const now = Date.now() / 1000;
    if (knownServer) {
      await dbRun("UPDATE sync_devices SET last_sync = ? WHERE id = ?", [now, knownServer.id]);
    } else {
      await dbRun("INSERT INTO sync_devices (id, name, last_sync) VALUES (?, ?, ?)",
        [remoteDeviceId, serverUrl, now]);
    }

    // Nettoyer le change_log local (garder seulement ce qui est après now)
    await dbRun("DELETE FROM change_log WHERE timestamp <= ?", [now]);

    // Démarrer le heartbeat vers ce serveur
    startDeviceHeartbeat(serverUrl);

    report('done', 'Synchronisation terminée !');
    return {
      sent: localChanges.length,
      received: remoteChanges.length,
      filesDownloaded: uniqueToDownload.length
    };

  } finally {
    _syncInProgress = false;
  }
}

async function localFileExists(path) {
  if (!path) return false;
  const { Filesystem } = Capacitor.Plugins;
  const cleanPath = path.startsWith('/') ? path.substring(1) : path;
  try {
    await Filesystem.stat({ path: `Clom/${cleanPath}`, directory: 'EXTERNAL' });
    return true;
  } catch {
    return false;
  }
}

// ── Mobile Download (NewPipe Extractor) ──────────────────

async function mobileDownloadFromUrl(youtubeUrl, onProgress) {
  const report = (step, detail) => onProgress && onProgress(step, detail);

  // 1. Extraction + téléchargement côté natif Java (évite le CORS)
  report('extract', 'Extraction...');
  const { YtExtractor } = Capacitor.Plugins;
  report('download', 'Téléchargement...');
  const info = await YtExtractor.download({ url: youtubeUrl });

  // 2. Nettoyer titre/artiste
  let title = info.title || 'Unknown';
  let artist = info.artist || 'Unknown';
  const sep = title.match(/^(.+?)\s*[-–—]\s*(.+)$/);
  if (sep) { artist = sep[1].trim(); title = sep[2].trim(); }

  // 3. Insérer en DB (les fichiers sont déjà sauvés par le plugin Java)
  report('db', 'Ajout à la bibliothèque...');
  const coverPath = info.coverPath || null;
  const res = await dbRun(
    `INSERT INTO tracks (title, artist, file_path, youtube_url, cover_path) VALUES (?, ?, ?, ?, ?)`,
    [title, artist, info.filePath, youtubeUrl, coverPath]
  );

  const track = (await dbQuery("SELECT * FROM tracks WHERE id = ?", [res.lastId]))[0];
  if (track) await recordLocalChange("track", res.lastId, "create", localTrackToDict(track));

  report('done', 'Terminé !');

  return { id: res.lastId, title, artist };
}

async function mobileSearchAndDownload(searchTitle, searchArtist, onProgress) {
  const report = (step, detail) => onProgress && onProgress(step, detail);

  // 1. Rechercher sur YouTube
  report('search', 'Recherche sur YouTube...');
  const { YtExtractor } = Capacitor.Plugins;
  const query = searchArtist ? `${searchTitle} ${searchArtist}` : searchTitle;
  const searchResult = await YtExtractor.search({ query });

  let results = searchResult.results;
  if (typeof results === 'string') results = JSON.parse(results);
  if (!results || results.length === 0) throw new Error('Aucun résultat trouvé');

  // Score results to find the best match (prefer official, matching artist/title)
  const titleLower = searchTitle.toLowerCase();
  const artistLower = (searchArtist || '').toLowerCase();
  const scored = results.map(r => {
    let score = 0;
    const rt = (r.title || '').toLowerCase();
    const ra = (r.artist || r.uploaderName || '').toLowerCase();
    // Title match
    if (rt.includes(titleLower) || titleLower.includes(rt)) score += 3;
    // Artist match
    if (artistLower && (ra.includes(artistLower) || artistLower.includes(ra))) score += 3;
    // Penalize covers, remixes, live versions
    if (/cover|remix|live|karaoke|instrumental|parody/i.test(rt)) score -= 2;
    // Prefer "official" in title
    if (/official/i.test(rt)) score += 1;
    // Prefer reasonable duration (2-8 min for music)
    if (r.duration && r.duration >= 120 && r.duration <= 480) score += 1;
    return { ...r, _score: score };
  });
  scored.sort((a, b) => b._score - a._score);
  const best = scored[0];
  if (!best || !best.url) throw new Error('Aucun résultat valide');

  report('extract', `Trouvé : ${best.title}`);

  // 2. Télécharger via URL
  return await mobileDownloadFromUrl(best.url, onProgress);
}

// ── Cover Art (MusicBrainz / Cover Art Archive) ──────────

async function fetchHighResCover(title, artist, trackId) {
  if (!title || !trackId) return null;
  try {
    // Clean title: remove "(Official Video)", "[HD]", etc.
    const cleanTitle = title.replace(/\s*[\(\[].*?[\)\]]\s*/g, '').trim();
    const cleanArtist = (artist || '').replace(/\s*[\(\[].*?[\)\]]\s*/g, '').replace(/ - Topic$/i, '').trim();

    // Try multiple queries: strict first, then loose
    const queries = [];
    if (cleanArtist) {
      queries.push(`recording:"${cleanTitle}" AND artist:"${cleanArtist}"`);
      queries.push(`${cleanTitle} ${cleanArtist}`);
    }
    queries.push(cleanTitle);

    for (const q of queries) {
      const mbRes = await fetch(`https://musicbrainz.org/ws/2/recording?query=${encodeURIComponent(q)}&fmt=json&limit=5`, {
        headers: { 'User-Agent': 'Clom/1.0 (github.com/tomvivian28072005/maMusique)' }
      });
      if (!mbRes.ok) { console.log('[Cover] MB search failed:', mbRes.status); continue; }
      const mbData = await mbRes.json();
      const recordings = mbData.recordings || [];
      console.log(`[Cover] MB query "${q}" → ${recordings.length} results`);

      for (const rec of recordings) {
        const releases = rec.releases || [];

        for (const rel of releases) {
          if (!rel.id) continue;
          try {
            const caaRes = await fetch(`https://coverartarchive.org/release/${rel.id}`, {
              headers: { 'User-Agent': 'Clom/1.0' }
            });
            if (!caaRes.ok) continue;
            const caaData = await caaRes.json();
            const front = (caaData.images || []).find(img => img.front);
            if (front && front.thumbnails && front.thumbnails['1200']) {
              console.log(`[Cover] Found 1200px cover from "${rel.title}" (${rel.id})`);
              return front.thumbnails['1200'];
            }
            if (front && front.image) {
              console.log(`[Cover] Found full-size cover from "${rel.title}" (${rel.id})`);
              return front.image;
            }
          } catch(e) { continue; }
        }
      }
    }
    console.log('[Cover] No cover found for:', cleanTitle, cleanArtist);
    return null;
  } catch(e) {
    console.log('[Cover] MusicBrainz error:', e);
    return null;
  }
}

async function upgradeTrackCover(trackId, title, artist) {
  console.log(`[Cover] Attempting upgrade for track ${trackId}: "${title}" by "${artist}"`);
  const coverUrl = await fetchHighResCover(title, artist, trackId);
  if (!coverUrl) { console.log('[Cover] No HD cover URL found, keeping YouTube thumbnail'); return false; }
  try {
    const { Filesystem } = Capacitor.Plugins;
    // Download the image
    console.log('[Cover] Downloading HD cover from:', coverUrl.substring(0, 80));
    const response = await fetch(coverUrl);
    if (!response.ok) { console.log('[Cover] Download failed:', response.status); return false; }
    const blob = await response.blob();
    console.log(`[Cover] Downloaded ${(blob.size / 1024).toFixed(0)} KB`);

    // Convert to base64
    const reader = new FileReader();
    const base64 = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });

    // Save to filesystem
    const fileName = `cover_${trackId}_hd.jpg`;
    await Filesystem.writeFile({
      path: `Clom/covers/${fileName}`,
      data: base64,
      directory: 'EXTERNAL',
      recursive: true
    });

    // Update DB — overwrite YouTube thumbnail
    const coverPath = `covers/${fileName}?t=${Date.now()}`;
    await dbRun("UPDATE tracks SET cover_path = ? WHERE id = ?", [coverPath, trackId]);
    await recordLocalChange("track", trackId, "update", { cover_path: coverPath });

    console.log(`[Cover] ✓ Upgraded cover for track ${trackId} (${(blob.size / 1024).toFixed(0)} KB)`);
    // Notify UI to refresh cover if this track is currently playing
    window.dispatchEvent(new CustomEvent('cover-upgraded', { detail: { trackId, coverPath } }));
    return true;
  } catch(e) {
    console.log('[Cover] Upgrade error:', e.message || e);
    return false;
  }
}

// ── Listen History (statistiques) ─────────────────────────

async function recordListenSession(trackId, durationSeconds) {
  if (!trackId || durationSeconds < 5) return; // Ignorer les écoutes < 5s
  const listenedAt = new Date().toISOString().replace('T', ' ').split('.')[0];
  const dur = Math.round(durationSeconds);
  const res = await dbRun(
    "INSERT INTO listen_history (track_id, listened_at, duration_seconds, device_id) VALUES (?, ?, ?, ?)",
    [trackId, listenedAt, dur, _deviceId]
  );
  // Log pour sync
  await recordLocalChange('listen_history', res.lastId || 0, 'create', {
    track_id: trackId, listened_at: listenedAt, duration_seconds: dur, device_id: _deviceId
  });
}

async function getListenStats() {
  // Temps d'écoute total (secondes)
  const totalRow = (await dbQuery("SELECT COALESCE(SUM(duration_seconds), 0) as total FROM listen_history"))[0];
  const totalSeconds = totalRow ? totalRow.total : 0;

  // Temps d'écoute par jour (7 derniers jours)
  const daily = await dbQuery(`
    SELECT date(listened_at) as day, SUM(duration_seconds) as total
    FROM listen_history
    WHERE listened_at >= datetime('now', '-7 days')
    GROUP BY date(listened_at)
    ORDER BY day ASC
  `);

  // Top morceaux par nombre d'écoutes
  const topByCount = await dbQuery(`
    SELECT t.id, t.title, t.artist, t.cover_path, COUNT(*) as listen_count
    FROM listen_history h JOIN tracks t ON h.track_id = t.id
    GROUP BY t.id ORDER BY listen_count DESC LIMIT 10
  `);

  // Top morceaux par temps d'écoute
  const topByTime = await dbQuery(`
    SELECT t.id, t.title, t.artist, t.cover_path, SUM(h.duration_seconds) as total_time
    FROM listen_history h JOIN tracks t ON h.track_id = t.id
    GROUP BY t.id ORDER BY total_time DESC LIMIT 10
  `);

  // Nombre total de morceaux
  const trackCountRow = (await dbQuery("SELECT COUNT(*) as cnt FROM tracks"))[0];
  const trackCount = trackCountRow ? trackCountRow.cnt : 0;

  // Nombre total d'écoutes
  const listenCountRow = (await dbQuery("SELECT COUNT(*) as cnt FROM listen_history"))[0];
  const listenCount = listenCountRow ? listenCountRow.cnt : 0;

  // Top artistes par temps d'écoute
  const topArtists = await dbQuery(`
    SELECT t.artist, COUNT(*) as listen_count, SUM(h.duration_seconds) as total_time
    FROM listen_history h JOIN tracks t ON h.track_id = t.id
    WHERE t.artist IS NOT NULL AND t.artist != '' AND LOWER(t.artist) != 'unknown'
    GROUP BY LOWER(t.artist) ORDER BY total_time DESC LIMIT 8
  `);

  // Heures préférées d'écoute (distribution par heure de la journée)
  const hourlyRaw = await dbQuery(`
    SELECT CAST(strftime('%H', listened_at) AS INTEGER) as hour, SUM(duration_seconds) as total
    FROM listen_history
    GROUP BY hour ORDER BY hour ASC
  `);
  const hourly = Array.from({length: 24}, (_, i) => {
    const row = hourlyRaw.find(r => r.hour === i);
    return { hour: i, total: row ? row.total : 0 };
  });

  // Streak (jours consécutifs d'écoute)
  const allDays = await dbQuery(`
    SELECT DISTINCT date(listened_at) as day FROM listen_history ORDER BY day DESC
  `);
  let streak = 0;
  if (allDays.length > 0) {
    const today = new Date().toISOString().split('T')[0];
    const yesterday = new Date(Date.now() - 86400000).toISOString().split('T')[0];
    // Streak starts from today or yesterday
    if (allDays[0].day === today || allDays[0].day === yesterday) {
      streak = 1;
      for (let i = 1; i < allDays.length; i++) {
        const prev = new Date(allDays[i-1].day);
        const curr = new Date(allDays[i].day);
        const diff = (prev - curr) / 86400000;
        if (diff === 1) streak++;
        else break;
      }
    }
  }

  // Temps d'écoute par jour (30 derniers jours pour le graphe étendu)
  const daily30 = await dbQuery(`
    SELECT date(listened_at) as day, SUM(duration_seconds) as total
    FROM listen_history
    WHERE listened_at >= datetime('now', '-30 days')
    GROUP BY date(listened_at)
    ORDER BY day ASC
  `);

  // Moyenne par jour (sur les jours où on a écouté)
  const daysWithListens = allDays.length;
  const avgPerDay = daysWithListens > 0 ? totalSeconds / daysWithListens : 0;

  // Meilleur jour de la semaine (0=Dim, 1=Lun, ..., 6=Sam)
  const bestDayRaw = await dbQuery(`
    SELECT CAST(strftime('%w', listened_at) AS INTEGER) as dow, SUM(duration_seconds) as total
    FROM listen_history
    GROUP BY dow ORDER BY total DESC LIMIT 1
  `);
  const dayNames = ['Dimanche','Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi'];
  const bestDayOfWeek = bestDayRaw.length > 0 ? { name: dayNames[bestDayRaw[0].dow], seconds: bestDayRaw[0].total } : null;

  // Session la plus longue
  const longestRow = (await dbQuery("SELECT MAX(duration_seconds) as longest FROM listen_history"))[0];
  const longestSession = longestRow ? longestRow.longest || 0 : 0;

  // Première écoute
  const firstRow = (await dbQuery("SELECT MIN(listened_at) as first_listen FROM listen_history"))[0];
  const firstListen = firstRow ? firstRow.first_listen : null;

  // Découvertes récentes (morceaux écoutés pour la 1ère fois cette semaine)
  const recentDiscoveries = await dbQuery(`
    SELECT t.id, t.title, t.artist, t.cover_path, MIN(h.listened_at) as first_listen
    FROM listen_history h JOIN tracks t ON h.track_id = t.id
    GROUP BY t.id
    HAVING first_listen >= datetime('now', '-7 days')
    ORDER BY first_listen DESC LIMIT 8
  `);

  // Distribution par jour de la semaine (pour graphe complet)
  const weekdayRaw = await dbQuery(`
    SELECT CAST(strftime('%w', listened_at) AS INTEGER) as dow, SUM(duration_seconds) as total
    FROM listen_history GROUP BY dow ORDER BY dow ASC
  `);
  const weekday = Array.from({length: 7}, (_, i) => {
    const row = weekdayRaw.find(r => r.dow === i);
    return { dow: i, name: ['Dim','Lun','Mar','Mer','Jeu','Ven','Sam'][i], total: row ? row.total : 0 };
  });

  // Resolve cover URLs for display
  const resolveCover = async (items) => {
    for (const item of items) {
      if (item.cover_path) {
        const clean = item.cover_path.split('?')[0];
        item.cover_path = await readLocalFileAsUrl(clean);
      }
    }
  };
  await resolveCover(topByCount);
  await resolveCover(topByTime);
  await resolveCover(recentDiscoveries);

  // All-time daily data for timeline graph
  const dailyAll = await dbQuery(`
    SELECT date(listened_at) as day, SUM(duration_seconds) as total
    FROM listen_history
    GROUP BY date(listened_at)
    ORDER BY day ASC
  `);

  return {
    totalSeconds, daily, daily30, dailyAll, topByCount, topByTime, topArtists, hourly, streak,
    trackCount, listenCount, avgPerDay, bestDayOfWeek, longestSession, firstListen,
    recentDiscoveries, weekday
  };
}

// ── Export pour utilisation dans api-local.js ──────────────
// (tout est global car c'est du vanilla JS inline)
