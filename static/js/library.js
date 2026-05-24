/**
 * library.js
 * Renders all views (Library, Album detail, Artists, Artist detail,
 * Liked songs, Today's Hits) and handles tab / navigation switching.
 *
 * Depends on globals:
 *   state.js        — library, queue, cursor, shuffleMode, likedPaths,
 *                     currentAlbumTracks, currentArtistTracks, currentHitsTracks,
 *                     currentTab, csrfToken
 *   utils.js        — escHtml
 *   cover-loader.js — CoverLoader
 *   prefetch-manager.js — PrefetchManager
 *   player.js       — startPlayback, buildShuffleQueue, updateShuffleUI,
 *                     updatePlayerHeart
 */

// ── Top-level library view ────────────────────────────────────
function renderLibrary() {
    const albumEl = document.getElementById('album-container');
    const trackEl = document.getElementById('loose-track-container');

    albumEl.innerHTML = library.albums.map((alb, i) => `
        <div class="card" onclick="openAlbum(${i})">
            <div class="card-art">
                ${alb.cover_path
                    ? `<img class="lazy-cover" data-src="/api/cover/${encodeURIComponent(alb.cover_path)}">`
                    : '<span class="material-symbols-outlined">album</span>'}
                <div class="card-play-overlay">
                    <button onclick="event.stopPropagation(); openAlbumAndPlay(${i}, false)">
                        <span class="material-symbols-outlined">play_arrow</span>
                    </button>
                </div>
            </div>
            <div class="card-info">
                <div class="title">${escHtml(alb.name)}</div>
                <div class="meta">${escHtml(alb.artist)}</div>
            </div>
        </div>
    `).join('');

    // Loose tracks use the track's own track_number (useSequential = false)
    trackEl.innerHTML = library.tracks.map((t, i) =>
        renderTrackRow(t, i, library.tracks, false)
    ).join('');
    applyRowStagger(trackEl);
    attachHoverPrefetch(trackEl, library.tracks);

    CoverLoader.observeAll(albumEl);
}

// ── Track row builder ─────────────────────────────────────────
/**
 * Render a single <div class="track-row"> string.
 *
 * @param {object}  t             – track data object
 * @param {number}  i             – index within the rendered list
 * @param {Array}   contextQueue  – the queue that will be active on click
 * @param {boolean} useSequential – true  → always show i+1 as the row number
 *                                  false → show t.track_number (or i+1 fallback)
 */
function renderTrackRow(t, i, contextQueue, useSequential) {
    const isLiked    = likedPaths.has(t.path);
    const displayNum = useSequential ? (i + 1) : (t.track_number || i + 1);
    const encodedCtx = JSON.stringify(contextQueue).replace(/'/g, "&apos;");
    const prefetched = PrefetchManager.isCached(t.path) ? ' style="color:#4caf50"' : '';
    return `
        <div class="track-row" data-path="${encodeURIComponent(t.path)}"
             onclick='playFromContext("${encodeURIComponent(JSON.stringify(t.path))}", ${encodedCtx})'>
            <div class="t-num">
                <span class="row-num"${prefetched}>${displayNum}</span>
                <div class="eq-bars">
                    <span></span><span></span><span></span>
                </div>
            </div>
            <div class="t-info">
                <div class="track-name">${escHtml(t.title)}</div>
                <div class="track-artist">${escHtml(t.artist)}</div>
            </div>
            <div class="t-dur">${t.duration_fmt}${t.bitrate ? `<div style="font-size:0.65rem;margin-top:1px;opacity:0.45">${t.bitrate}k</div>` : ''}</div>
            <div class="t-like">
                <button class="like-btn${isLiked ? ' liked' : ''}"
                        data-path="${t.path}"
                        onclick="event.stopPropagation(); toggleLike('${t.path.replace(/'/g,"\\'")}', this)"
                        title="${isLiked ? 'Unlike' : 'Like'}">
                    <span class="material-symbols-outlined">${isLiked ? 'favorite' : 'favorite_border'}</span>
                </button>
            </div>
        </div>
    `;
}

// ── Row animation helpers ─────────────────────────────────────
function applyRowStagger(container) {
    container.querySelectorAll('.track-row').forEach((row, i) => {
        row.style.animationDelay = `${i * 35}ms`;
    });
}

/**
 * Attach mouseenter / mouseleave hover-prefetch listeners to every
 * track row inside container, using tracks as the data source.
 */
function attachHoverPrefetch(container, tracks) {
    const byPath = new Map(tracks.map(t => [encodeURIComponent(t.path), t]));
    container.querySelectorAll('.track-row').forEach(row => {
        const t = byPath.get(row.dataset.path);
        if (!t) return;
        row.addEventListener('mouseenter', () => PrefetchManager.onHoverStart(t));
        row.addEventListener('mouseleave', () => PrefetchManager.onHoverEnd(t));
    });
}

// ── Play from an arbitrary context queue ─────────────────────
function playFromContext(encodedPath, contextArray) {
    const targetPath = JSON.parse(decodeURIComponent(encodedPath));
    queue  = [...contextArray];
    cursor = queue.findIndex(track => track.path === targetPath);
    if (shuffleMode) buildShuffleQueue(cursor);
    startPlayback();
}

// ── Album view ────────────────────────────────────────────────
function openAlbum(index, autoplay = false, doShuffle = false) {
    const alb = library.albums[index];

    _hideAllViews();
    document.getElementById('view-album').classList.add('active');
    document.getElementById('main-scroll').scrollTop = 0;

    document.getElementById('detail-title').innerText  = alb.name;
    document.getElementById('detail-artist').innerText = alb.artist;

    const artBox = document.getElementById('detail-art-box');
    artBox.innerHTML = alb.cover_path
        ? `<img class="lazy-cover" data-src="/api/cover/${encodeURIComponent(alb.cover_path)}">`
        : '<span class="material-symbols-outlined" style="font-size: 5rem; color: #222; margin: 60px;">album</span>';

    currentAlbumTracks = alb.tracks;

    const container = document.getElementById('album-track-container');
    container.innerHTML = alb.tracks.map((t, i) => renderTrackRow(t, i, alb.tracks, false)).join('');
    applyRowStagger(container);
    attachHoverPrefetch(container, alb.tracks);

    CoverLoader.observeAll(document.getElementById('view-album'));

    if (autoplay) {
        queue = [...alb.tracks];
        if (doShuffle) {
            shuffleMode = true;
            updateShuffleUI();
            buildShuffleQueue(0);
        } else {
            cursor = 0;
        }
        startPlayback();
    }
}

function openAlbumAndPlay(index, doShuffle) { openAlbum(index, true, doShuffle); }

function playAlbum(doShuffle) {
    if (!currentAlbumTracks.length) return;
    queue = [...currentAlbumTracks];
    if (doShuffle) {
        shuffleMode = true; updateShuffleUI(); buildShuffleQueue(0);
    } else { cursor = 0; }
    startPlayback();
}

// ── Artists view ──────────────────────────────────────────────
function renderArtists() {
    // Aggregate all tracks from library and group by artist name
    const allTracks = [
        ...library.tracks,
        ...library.albums.flatMap(a => a.tracks),
    ];

    const artistMap = new Map(); // name → { tracks, coverPath }
    for (const t of allTracks) {
        const name = t.artist || 'Unknown Artist';
        if (!artistMap.has(name)) artistMap.set(name, { tracks: [], coverPath: null });
        const entry = artistMap.get(name);
        entry.tracks.push(t);
        if (!entry.coverPath && t.has_cover) entry.coverPath = t.path;
    }

    // Sort alphabetically (case-insensitive)
    const sorted = [...artistMap.entries()].sort((a, b) =>
        a[0].localeCompare(b[0], undefined, { sensitivity: 'base' })
    );

    const grid = document.getElementById('artist-grid-container');
    grid.innerHTML = sorted.map(([name, data], i) => `
        <div class="artist-card" onclick="openArtist(${i})">
            <div class="artist-avatar">
                ${data.coverPath
                    ? `<img class="lazy-cover" data-src="/api/cover/${encodeURIComponent(data.coverPath)}"
                           style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`
                    : '<span class="material-symbols-outlined">person</span>'}
            </div>
            <div class="artist-card-text">
                <div class="artist-name">${escHtml(name)}</div>
                <div class="artist-meta">${data.tracks.length} track${data.tracks.length !== 1 ? 's' : ''}</div>
            </div>
        </div>
    `).join('');

    // Store for openArtist() lookups by index
    window._sortedArtists = sorted;

    CoverLoader.observeAll(grid);
}

function openArtist(index) {
    const [name, data] = window._sortedArtists[index];
    currentArtistTracks = data.tracks;

    _hideAllViews();
    document.getElementById('view-artist').classList.add('active');
    document.getElementById('main-scroll').scrollTop = 0;
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-artists').classList.add('active');
    currentTab = 'artists';

    document.getElementById('artist-detail-name').innerText = name;
    document.getElementById('artist-detail-meta').innerText =
        `${data.tracks.length} track${data.tracks.length !== 1 ? 's' : ''}`;

    const artBox = document.getElementById('artist-detail-art-box');
    if (data.coverPath) {
        artBox.innerHTML = `<img class="lazy-cover" data-src="/api/cover/${encodeURIComponent(data.coverPath)}"
                                style="width:100%;height:100%;object-fit:cover;">`;
    } else {
        artBox.innerHTML = `<span class="material-symbols-outlined"
                                 style="font-size:5rem;color:#2a2a2a;">person</span>`;
    }

    // Artist context: sequential 1-based numbering (useSequential = true)
    const container = document.getElementById('artist-track-container');
    container.innerHTML = data.tracks.map((t, i) =>
        renderTrackRow(t, i, data.tracks, true)
    ).join('');
    applyRowStagger(container);
    attachHoverPrefetch(container, data.tracks);

    CoverLoader.observeAll(document.getElementById('view-artist'));
}

function showArtists() {
    _hideAllViews();
    document.getElementById('view-artists').classList.add('active');
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-artists').classList.add('active');
    currentTab = 'artists';
    document.getElementById('main-scroll').scrollTop = 0;
}

function playArtist(doShuffle) {
    if (!currentArtistTracks.length) return;
    queue = [...currentArtistTracks];
    if (doShuffle) {
        shuffleMode = true; updateShuffleUI(); buildShuffleQueue(0);
    } else { cursor = 0; }
    startPlayback();
}

// ── Today's Hits ──────────────────────────────────────────────
let _hitsLoaded = false;

async function loadTodayHits(force = false) {
    if (_hitsLoaded && !force) return;
    _hitsLoaded = true;

    const btn = document.getElementById('hits-regen-btn');
    btn.classList.add('spinning');
    btn.disabled = true;

    const container = document.getElementById('hits-track-container');
    container.innerHTML = `<div class="hits-empty">
        <span class="material-symbols-outlined">auto_awesome</span>
        <p>Generating your playlist…</p></div>`;

    try {
        const res  = await fetch('/api/today-hits');
        const data = await res.json();

        currentHitsTracks = data.tracks || [];

        if (!currentHitsTracks.length) {
            container.innerHTML = `<div class="hits-empty">
                <span class="material-symbols-outlined">music_note</span>
                <p>Start listening to some tracks and come back — your personalised playlist will build up over time!</p>
            </div>`;
            document.getElementById('hits-meta').textContent = 'No listening history yet';
        } else {
            // Sequential numbering in hits context (useSequential = true)
            container.innerHTML = currentHitsTracks.map((t, i) =>
                renderTrackRow(t, i, currentHitsTracks, true)
            ).join('');
            applyRowStagger(container);
            attachHoverPrefetch(container, currentHitsTracks);

            const ts = data.generated_at
                ? new Date(data.generated_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                : '';
            document.getElementById('hits-meta').textContent =
                `${currentHitsTracks.length} tracks · generated ${ts ? 'at ' + ts : 'just now'}`;
        }
    } catch(e) {
        container.innerHTML = `<div class="hits-empty">
            <span class="material-symbols-outlined">error</span>
            <p>Could not generate playlist. Please try again.</p></div>`;
    } finally {
        btn.classList.remove('spinning');
        btn.disabled = false;
    }
}

function playHits(doShuffle) {
    if (!currentHitsTracks.length) return;
    queue = [...currentHitsTracks];
    if (doShuffle) {
        shuffleMode = true; updateShuffleUI(); buildShuffleQueue(0);
    } else { cursor = 0; }
    startPlayback();
}

// ── Tab & view switching ──────────────────────────────────────
function _hideAllViews() {
    ['view-library','view-album','view-liked','view-artists','view-artist','view-hits']
        .forEach(id => document.getElementById(id).classList.remove('active'));
}

function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById(`tab-${tab}`).classList.add('active');

    _hideAllViews();
    if (tab === 'library') {
        document.getElementById('view-library').classList.add('active');
    } else if (tab === 'artists') {
        document.getElementById('view-artists').classList.add('active');
    } else if (tab === 'hits') {
        document.getElementById('view-hits').classList.add('active');
        loadTodayHits();
    } else if (tab === 'liked') {
        document.getElementById('view-liked').classList.add('active');
        renderLiked();
    }

    document.getElementById('main-scroll').scrollTop = 0;
}

function showLibrary() {
    _hideAllViews();
    document.getElementById('view-library').classList.add('active');
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-library').classList.add('active');
    currentTab = 'library';
    document.getElementById('main-scroll').scrollTop = 0;
}

// ── Liked songs ───────────────────────────────────────────────
async function toggleLike(path, btnEl) {
    const isLiked = likedPaths.has(path);
    const method  = isLiked ? 'DELETE' : 'POST';
    try {
        const res = await fetch(`/api/liked/${encodeURIComponent(path)}`, {
            method,
            headers: { 'X-CSRF-Token': csrfToken() },
        });
        if (!res.ok) return;
        if (isLiked) likedPaths.delete(path);
        else         likedPaths.add(path);

        document.querySelectorAll(`.like-btn[data-path="${CSS.escape(path)}"]`).forEach(btn => {
            btn.classList.toggle('liked', !isLiked);
            btn.querySelector('span').innerText = isLiked ? 'favorite_border' : 'favorite';
            btn.title = isLiked ? 'Like' : 'Unlike';
        });
        updatePlayerHeart();
        updateLikedCount();
        if (currentTab === 'liked') renderLiked();
    } catch(e) { console.error('Like toggle failed', e); }
}

async function toggleLikeCurrentTrack() {
    if (cursor < 0 || !queue[cursor]) return;
    await toggleLike(queue[cursor].path, null);
}

function updatePlayerHeart() {
    if (cursor < 0 || !queue[cursor]) return;
    const isLiked = likedPaths.has(queue[cursor].path);
    document.getElementById('btn-like').classList.toggle('liked', isLiked);
    document.getElementById('like-icon').innerText = isLiked ? 'favorite' : 'favorite_border';
}

function updateLikedCount() {
    const badge = document.getElementById('liked-count');
    badge.textContent = likedPaths.size > 0 ? likedPaths.size : '';
}

function renderLiked() {
    const container = document.getElementById('liked-track-container');
    const actions   = document.getElementById('liked-play-actions');

    if (likedPaths.size === 0) {
        actions.style.display = 'none';
        container.innerHTML = `
            <div class="liked-empty">
                <span class="material-symbols-outlined">favorite</span>
                <p>No liked songs yet.<br>Press the heart on any track to save it here.</p>
            </div>`;
        return;
    }

    const allTracks = [
        ...library.tracks,
        ...library.albums.flatMap(a => a.tracks),
    ];
    const likedTracks = [...likedPaths]
        .map(p => allTracks.find(t => t.path === p))
        .filter(Boolean);

    actions.style.display = likedTracks.length ? 'flex' : 'none';
    // Liked context: sequential 1-based numbering (useSequential = true)
    container.innerHTML = likedTracks.map((t, i) =>
        renderTrackRow(t, i, likedTracks, true)
    ).join('');
    applyRowStagger(container);
    attachHoverPrefetch(container, likedTracks);
}

function playLiked(doShuffle) {
    const allTracks = [
        ...library.tracks,
        ...library.albums.flatMap(a => a.tracks),
    ];
    const likedTracks = [...likedPaths]
        .map(p => allTracks.find(t => t.path === p))
        .filter(Boolean);
    if (!likedTracks.length) return;
    queue = [...likedTracks];
    if (doShuffle) {
        shuffleMode = true; updateShuffleUI(); buildShuffleQueue(0);
    } else { cursor = 0; }
    startPlayback();
}
