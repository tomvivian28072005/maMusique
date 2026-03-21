// ── Clom API Local (offline mobile) ───────────────────────
// Intercepte les appels clomFetch() et les traite localement via SQLite.
// Chaque fonction retourne un objet Response-like : { ok, status, json() }

function localResponse(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return data; },
    async text() { return JSON.stringify(data); }
  };
}

// ── Résoudre les URLs de fichiers locaux ──────────────────
// Sur mobile, les fichiers audio/cover sont dans Capacitor Filesystem.
// On convertit les chemins DB (/downloads/xxx.mp3) en URLs natives.

async function resolveTrackUrls(track) {
  if (!track) return track;
  const t = { ...track };
  // Renommer pt_added_at → added_at si présent (playlist tracks)
  if (t.pt_added_at) {
    t.added_at = t.pt_added_at;
    delete t.pt_added_at;
  }
  // Convertir les chemins en URLs Capacitor natives
  if (t.file_path) t.file_path = await readLocalFileAsUrl(t.file_path);
  if (t.cover_path) {
    const cleanCover = t.cover_path.split('?')[0]; // Enlever le ?t=timestamp
    t.cover_path = await readLocalFileAsUrl(cleanCover);
  }
  // Defaults
  t.play_count = t.play_count || 0;
  t.volume_coeff = t.volume_coeff != null ? t.volume_coeff : 1.0;
  t.cover_zoom = t.cover_zoom != null ? t.cover_zoom : 1.0;
  t.cover_offset_x = t.cover_offset_x != null ? t.cover_offset_x : 0.0;
  t.cover_offset_y = t.cover_offset_y != null ? t.cover_offset_y : 0.0;
  return t;
}

async function resolvePlaylistUrls(pl) {
  if (!pl) return pl;
  const p = { ...pl };
  if (p.cover_path) {
    const cleanCover = p.cover_path.split('?')[0];
    p.cover_path = await readLocalFileAsUrl(cleanCover);
  }
  p.cover_zoom = p.cover_zoom != null ? p.cover_zoom : 1.0;
  p.cover_offset_x = p.cover_offset_x != null ? p.cover_offset_x : 0.0;
  p.cover_offset_y = p.cover_offset_y != null ? p.cover_offset_y : 0.0;
  return p;
}

// ── Router principal ──────────────────────────────────────
// Parse le path + method et dispatch vers la bonne fonction locale.

async function handleLocalApi(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const url = new URL(path, 'http://local');
  const pathname = url.pathname;
  const params = url.searchParams;

  // Parse le body si présent
  let body = null;
  if (options.body) {
    if (typeof options.body === 'string') {
      try { body = JSON.parse(options.body); } catch { body = options.body; }
    } else if (options.body instanceof FormData) {
      body = options.body; // FormData pour les uploads
    }
  }

  try {
    // ── GET /api/version ──
    if (method === 'GET' && pathname === '/api/version') {
      return localResponse({ version: MOBILE_APP_VERSION });
    }

    // ── GET /api/tracks ──
    if (method === 'GET' && pathname === '/api/tracks') {
      const tracks = await localGetTracks(params.get('search'), params.get('sort_by') || 'added_at');
      const resolved = [];
      for (const t of tracks) {
        try { resolved.push(await resolveTrackUrls(t)); } catch(e) { resolved.push(t); }
      }
      return localResponse(resolved);
    }

    // ── DELETE /api/tracks/{id} ──
    const deleteTrackMatch = pathname.match(/^\/api\/tracks\/(\d+)$/);
    if (method === 'DELETE' && deleteTrackMatch) {
      await localDeleteTrack(parseInt(deleteTrackMatch[1]));
      return localResponse({ message: 'Track deleted.' });
    }

    // ── PATCH /api/tracks/{id} ──
    const patchTrackMatch = pathname.match(/^\/api\/tracks\/(\d+)$/);
    if (method === 'PATCH' && patchTrackMatch) {
      const track = await localUpdateTrack(parseInt(patchTrackMatch[1]), body);
      if (!track) return localResponse({ detail: 'Track not found' }, 404);
      return localResponse({
        message: 'Track updated.',
        title: track.title,
        artist: track.artist,
        volume_coeff: track.volume_coeff,
        start_time: track.start_time,
        end_time: track.end_time,
        cover_zoom: track.cover_zoom,
        cover_offset_x: track.cover_offset_x,
        cover_offset_y: track.cover_offset_y
      });
    }

    // ── POST /api/tracks/{id}/cover ──
    const trackCoverMatch = pathname.match(/^\/api\/tracks\/(\d+)\/cover$/);
    if (method === 'POST' && trackCoverMatch && body instanceof FormData) {
      const file = body.get('file');
      const coverPath = await localUploadTrackCover(parseInt(trackCoverMatch[1]), file);
      const resolvedPath = await readLocalFileAsUrl(coverPath.split('?')[0]);
      return localResponse({ cover_path: resolvedPath });
    }

    // ── POST /api/tracks/{id}/played ──
    const playedMatch = pathname.match(/^\/api\/tracks\/(\d+)\/played$/);
    if (method === 'POST' && playedMatch) {
      const count = await localIncrementPlayCount(parseInt(playedMatch[1]));
      return localResponse({ play_count: count });
    }

    // ── POST /api/tracks/{id}/toggle-favorite ──
    const favMatch = pathname.match(/^\/api\/tracks\/(\d+)\/toggle-favorite$/);
    if (method === 'POST' && favMatch) {
      const result = await localToggleFavorite(parseInt(favMatch[1]));
      return localResponse(result);
    }

    // ── GET /api/tracks/{id}/playlists ──
    const trackPlMatch = pathname.match(/^\/api\/tracks\/(\d+)\/playlists$/);
    if (method === 'GET' && trackPlMatch) {
      const ids = await localGetTrackPlaylists(parseInt(trackPlMatch[1]));
      return localResponse({ playlist_ids: ids });
    }

    // ── GET /api/favorites ──
    if (method === 'GET' && pathname === '/api/favorites') {
      return localResponse(await localGetFavorites());
    }

    // ── GET /api/playlists ──
    if (method === 'GET' && pathname === '/api/playlists') {
      const playlists = await localGetPlaylists();
      const resolved = [];
      for (const p of playlists) {
        try { resolved.push(await resolvePlaylistUrls(p)); } catch(e) { resolved.push(p); }
      }
      return localResponse(resolved);
    }

    // ── POST /api/playlists ──
    if (method === 'POST' && pathname === '/api/playlists') {
      return localResponse(await localCreatePlaylist(body.name));
    }

    // ── POST /api/playlists/reorder ──
    if (method === 'POST' && pathname === '/api/playlists/reorder') {
      await localReorderPlaylists(body.playlist_ids);
      return localResponse({ message: 'OK' });
    }

    // ── PATCH /api/playlists/{id} ──
    const patchPlMatch = pathname.match(/^\/api\/playlists\/(\d+)$/);
    if (method === 'PATCH' && patchPlMatch) {
      const pl = await localUpdatePlaylist(parseInt(patchPlMatch[1]), body);
      if (!pl) return localResponse({ detail: 'Playlist not found' }, 404);
      const resolved = await resolvePlaylistUrls(pl);
      return localResponse({
        id: resolved.id, name: resolved.name, cover_path: resolved.cover_path,
        cover_zoom: resolved.cover_zoom, cover_offset_x: resolved.cover_offset_x,
        cover_offset_y: resolved.cover_offset_y
      });
    }

    // ── DELETE /api/playlists/{id} ──
    const deletePlMatch = pathname.match(/^\/api\/playlists\/(\d+)$/);
    if (method === 'DELETE' && deletePlMatch) {
      const ok = await localDeletePlaylist(parseInt(deletePlMatch[1]));
      if (!ok) return localResponse({ detail: 'Cannot delete' }, 400);
      return localResponse({ message: 'Playlist supprimée.' });
    }

    // ── POST /api/playlists/{id}/cover ──
    const plCoverMatch = pathname.match(/^\/api\/playlists\/(\d+)\/cover$/);
    if (method === 'POST' && plCoverMatch && body instanceof FormData) {
      const file = body.get('file');
      const coverPath = await localUploadPlaylistCover(parseInt(plCoverMatch[1]), file);
      const resolvedPath = await readLocalFileAsUrl(coverPath.split('?')[0]);
      return localResponse({ cover_path: resolvedPath });
    }

    // ── GET /api/playlists/{id}/tracks ──
    const plTracksMatch = pathname.match(/^\/api\/playlists\/(\d+)\/tracks$/);
    if (method === 'GET' && plTracksMatch) {
      const tracks = await localGetPlaylistTracks(parseInt(plTracksMatch[1]));
      const resolved = [];
      for (const t of tracks) {
        try { resolved.push(await resolveTrackUrls(t)); } catch(e) { resolved.push(t); }
      }
      return localResponse(resolved);
    }

    // ── POST /api/playlists/{id}/tracks ──
    if (method === 'POST' && plTracksMatch) {
      await localAddTrackToPlaylist(parseInt(plTracksMatch[1]), body.track_id);
      return localResponse({ message: 'Morceau ajouté à la playlist.' });
    }

    // ── DELETE /api/playlists/{id}/tracks/{tid} ──
    const plTrackDelMatch = pathname.match(/^\/api\/playlists\/(\d+)\/tracks\/(\d+)$/);
    if (method === 'DELETE' && plTrackDelMatch) {
      await localRemoveTrackFromPlaylist(parseInt(plTrackDelMatch[1]), parseInt(plTrackDelMatch[2]));
      return localResponse({ message: 'Morceau retiré de la playlist.' });
    }

    // ── GET /api/playlists/{id}/duration ──
    const durationMatch = pathname.match(/^\/api\/playlists\/(\d+)\/duration$/);
    if (method === 'GET' && durationMatch) {
      return localResponse(await localGetPlaylistDuration(parseInt(durationMatch[1])));
    }

    // ── GET /api/sync/manifest ──
    if (method === 'GET' && pathname === '/api/sync/manifest') {
      return localResponse(await getLocalManifest());
    }

    // ── GET /api/sync/changes ──
    if (method === 'GET' && pathname === '/api/sync/changes') {
      const since = parseFloat(params.get('since') || '0');
      const excludeDevice = params.get('device_id');
      let changes = await getLocalChanges(since);
      if (excludeDevice) changes = changes.filter(c => c.device_id !== excludeDevice);
      return localResponse({ device_id: _deviceId, changes });
    }

    // ── Download / Import / Search (non supporté offline) ──
    if (pathname.startsWith('/api/download') || pathname.startsWith('/api/search-download') ||
        pathname.startsWith('/api/import')) {
      return localResponse({
        message: 'Cette fonctionnalité nécessite une connexion au PC. Télécharge tes morceaux sur le PC puis synchronise.',
        status: 'unavailable'
      }, 501);
    }

    // ── Network info / Update / Shutdown (non applicable) ──
    if (pathname === '/api/network-info' || pathname === '/api/update' ||
        pathname === '/api/shutdown' || pathname === '/api/cancel-shutdown') {
      return localResponse({ message: 'Not applicable on mobile' }, 200);
    }

    // ── Fallback ──
    console.warn('[Clom] Unhandled local API:', method, pathname);
    return localResponse({ detail: 'Not implemented locally' }, 501);

  } catch (err) {
    console.error('[Clom] Local API error:', method, pathname, err, err.stack);
    return localResponse({ detail: err.message, _debug_path: pathname, _debug_stack: String(err.stack || '') }, 500);
  }
}
