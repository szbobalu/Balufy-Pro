/**
 * player.js  [OPTIMISED]
 * Core playback engine: queue management, transport controls,
 * scrubber, volume, ambient colour extraction, and stream quality.
 *
 * Depends on globals:
 *   state.js       — audio, queue, cursor, shuffleMode, repeatMode,
 *                    streamQuality, likedPaths
 *   utils.js       — fmtTime, showToast
 *   cover-loader.js
 *   prefetch-manager.js
 *   listen-tracker.js
 *   lyrics.js      — fetchLyrics, syncLyrics
 *   equalizer.js   — _eqCtx
 *
 * Changes vs original:
 *  - _p() helper caches every getElementById result in _PC map.
 *    ontimeupdate fired 4-5×/sec — zero getElementById calls now.
 *  - ontimeupdate wrapped in requestAnimationFrame (_ticking flag)
 *    so the handler body runs at most once per display frame (~60fps)
 *    instead of every browser timeupdate tick.
 *  - updatePlayingRow tracks _activeRow directly (O(1)) instead of
 *    querySelectorAll('.track-row') scanning the full DOM (O(N)).
 *  - togglePlay uses _activeRow directly instead of querySelectorAll.
 *  - innerText → textContent everywhere (avoids forced layout recalc).
 *  - Optional chaining for _eqCtx?.state checks.
 */

// ── DOM element cache ─────────────────────────────────────────
const _PC = {};
/** Return cached element; look up and store on first access. */
const _p = id => _PC[id] ??= document.getElementById(id);

// ── Currently highlighted track row ──────────────────────────
// Tracked so we can do O(1) class toggles instead of DOM scans.
let _activeRow = null;

// ── Stream URL builder ────────────────────────────────────────
function buildStreamUrl(path, seekSecs) {
    let url = `/api/stream/${encodeURIComponent(path)}`;
    const params = [];
    if (streamQuality !== 'original') params.push(`quality=${streamQuality}`);
    if (seekSecs > 0) params.push(`seek=${Math.floor(seekSecs)}`);
    return params.length ? `${url}?${params.join('&')}` : url;
}

// ── Start playback of queue[cursor] ──────────────────────────
function startPlayback() {
    if (cursor < 0 || cursor >= queue.length) return;
    const track = queue[cursor];

    ListenTracker.start(track.path);

    const cachedUrl = (streamQuality === 'original')
        ? PrefetchManager.getUrl(track.path)
        : null;
    audio.src = cachedUrl || buildStreamUrl(track.path, 0);

    _p('p-title').textContent  = track.title;
    _p('p-artist').textContent = track.artist;
    _p('tot-time').textContent = track.duration_fmt;

    const artBox = _p('p-art');
    artBox.innerHTML = track.has_cover
        ? `<img src="/api/cover/${encodeURIComponent(track.path)}" loading="eager">`
        : '<span class="material-symbols-outlined" style="margin:14px;color:#333">music_note</span>';
    artBox.classList.add('is-playing');

    if (track.has_cover) {
        extractAmbientColor(`/api/cover/${encodeURIComponent(track.path)}`)
            .then(({ r, g, b }) => applyAmbientColor(r, g, b));
    } else {
        clearAmbientColor();
    }

    audio.play();
    if (typeof _eqCtx !== 'undefined' && _eqCtx?.state === 'suspended') {
        _eqCtx.resume();
    }
    _p('play-icon').textContent = 'pause';

    updatePlayingRow();
    updatePlayerHeart();
    fetchLyrics(track.title, track.artist);

    PrefetchManager.prefetchUpcoming(2);
}

// ── Highlight the currently playing row ──────────────────────
// Clears the previous reference (O(1)) then queries only the new row.
function updatePlayingRow() {
    if (_activeRow) {
        _activeRow.classList.remove('playing', 'paused');
        _activeRow = null;
    }
    if (cursor < 0 || !queue[cursor]) return;
    const path = encodeURIComponent(queue[cursor].path);
    _activeRow = document.querySelector(`.track-row[data-path="${path}"]`);
    if (_activeRow) _activeRow.classList.add('playing');
}

// ── Transport controls ────────────────────────────────────────
function togglePlay() {
    if (!audio.src) return;
    if (audio.paused) {
        audio.play();
        _p('play-icon').textContent = 'pause';
        _p('p-art').classList.add('is-playing');
        _activeRow?.classList.remove('paused');        // O(1) — no querySelectorAll
    } else {
        audio.pause();
        _p('play-icon').textContent = 'play_arrow';
        _p('p-art').classList.remove('is-playing');
        _activeRow?.classList.add('paused');           // O(1)
    }
}

function playNext() {
    if (!queue.length) return;
    if (repeatMode === 'one') { audio.currentTime = 0; audio.play(); return; }
    cursor = (cursor + 1) % queue.length;
    startPlayback();
}

function playPrev() {
    if (!queue.length) return;
    if (audio.currentTime > 3) { audio.currentTime = 0; return; }
    cursor = (cursor - 1 + queue.length) % queue.length;
    startPlayback();
}

// ── Shuffle ───────────────────────────────────────────────────
function toggleShuffle() {
    shuffleMode = !shuffleMode;
    updateShuffleUI();
    if (shuffleMode && queue.length) buildShuffleQueue(cursor);
    showToast(shuffleMode ? '⇄ Shuffle on' : '⇄ Shuffle off');
}

function updateShuffleUI() {
    _p('btn-shuffle').classList.toggle('active', shuffleMode);
}

function buildShuffleQueue(keepIdx) {
    const current = queue[keepIdx];
    const rest = queue.filter((_, i) => i !== keepIdx);
    for (let i = rest.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [rest[i], rest[j]] = [rest[j], rest[i]];
    }
    queue  = [current, ...rest];
    cursor = 0;
}

// ── Repeat ────────────────────────────────────────────────────
function toggleRepeat() {
    if (!repeatMode)               repeatMode = 'all';
    else if (repeatMode === 'all') repeatMode = 'one';
    else                           repeatMode = false;
    updateRepeatUI();
    const labels = { false: '↺ Repeat off', all: '↺ Repeat all', one: '↺ Repeat one' };
    showToast(labels[repeatMode]);
}

function updateRepeatUI() {
    const btn = _p('btn-repeat');
    btn.classList.toggle('active', !!repeatMode);
    btn.querySelector('span').textContent = repeatMode === 'one' ? 'repeat_one' : 'repeat';
}

// ── Audio element event handlers ─────────────────────────────
// rAF throttle: the browser fires timeupdate 4-5×/sec; we only
// need to update the UI once per animation frame (~16ms / 60fps).
let _ticking = false;
function _onTimeUpdate() {
    _ticking = false;
    if (!audio.duration) return;
    const ct  = audio.currentTime;
    const dur = audio.duration;
    _p('progress-fill').style.width = `${(ct / dur) * 100}%`;
    _p('cur-time').textContent = fmtTime(ct);
    syncLyrics(ct);
}

audio.ontimeupdate = () => {
    if (_ticking) return;
    _ticking = true;
    requestAnimationFrame(_onTimeUpdate);
};

audio.onended = () => {
    if (repeatMode === 'one') { audio.currentTime = 0; audio.play(); return; }
    if (cursor < queue.length - 1 || repeatMode === 'all') playNext();
};

// ── Scrubber ──────────────────────────────────────────────────
function seek(e) {
    const rect = _p('progress-bar').getBoundingClientRect();
    const pos  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    audio.currentTime = pos * audio.duration;
}

function scrubHover(e) {
    if (!audio.duration) return;
    const bar  = _p('progress-bar');
    const rect = bar.getBoundingClientRect();
    const pos  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    bar.style.setProperty('--hover-pct', `${pos * 100}%`);
    bar.setAttribute('data-hover-time', fmtTime(pos * audio.duration));
}

function clearScrubHover() {
    _p('progress-bar').removeAttribute('data-hover-time');
}

// ── Volume ────────────────────────────────────────────────────
function setVolume(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const vol  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    audio.volume = vol;
    _p('vol-fill').style.width = `${vol * 100}%`;
}

// ── Ambient colour extraction ─────────────────────────────────
/**
 * Sample the album art at 48×48 and find the most vivid non-greyscale
 * colour to use as an ambient accent throughout the UI.
 * @param {string} imgSrc
 * @returns {Promise<{r:number, g:number, b:number}>}
 */
function extractAmbientColor(imgSrc) {
    return new Promise(resolve => {
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = () => {
            const S = 48;
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = S;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, S, S);
            const { data } = ctx.getImageData(0, 0, S, S);

            const candidates = [];
            for (let i = 0; i < data.length; i += 4) {
                const r = data[i], g = data[i + 1], b = data[i + 2];
                const luma = (r * 299 + g * 587 + b * 114) / 1000;
                if (luma < 28 || luma > 228) continue;
                const max = Math.max(r, g, b), min = Math.min(r, g, b);
                const sat = max > 0 ? (max - min) / max : 0;
                if (sat < 0.18) continue;
                candidates.push({ r, g, b, score: sat * (luma / 160) });
            }

            if (!candidates.length) { resolve({ r: 70, g: 70, b: 70 }); return; }
            candidates.sort((a, b) => b.score - a.score);
            const top = candidates.slice(0, Math.max(1, (candidates.length * 0.2) | 0));
            const sum = top.reduce(
                (acc, c) => ({ r: acc.r + c.r, g: acc.g + c.g, b: acc.b + c.b }),
                { r: 0, g: 0, b: 0 }
            );
            resolve({
                r: (sum.r / top.length) | 0,
                g: (sum.g / top.length) | 0,
                b: (sum.b / top.length) | 0,
            });
        };
        img.onerror = () => resolve({ r: 70, g: 70, b: 70 });
        img.src = imgSrc;
    });
}

function applyAmbientColor(r, g, b) {
    const root = document.documentElement;
    root.style.setProperty('--amb-r', r);
    root.style.setProperty('--amb-g', g);
    root.style.setProperty('--amb-b', b);
    _p('ambient-layer').style.background = `
        radial-gradient(ellipse 90% 45% at 50% 108%,  rgba(${r},${g},${b},0.20) 0%, transparent 70%),
        radial-gradient(ellipse 55% 55% at 8%  48%,   rgba(${r},${g},${b},0.07) 0%, transparent 65%)
    `;
}

function clearAmbientColor() {
    const root = document.documentElement;
    root.style.setProperty('--amb-r', 0);
    root.style.setProperty('--amb-g', 0);
    root.style.setProperty('--amb-b', 0);
    _p('ambient-layer').style.background = 'transparent';
}

// ── Quality / bitrate ─────────────────────────────────────────
function onQualityChange(val) {
    streamQuality = val;
    if (!audio.src || cursor < 0 || !queue[cursor]) return;
    const wasPlaying = !audio.paused;
    audio.src = buildStreamUrl(queue[cursor].path, 0);
    if (wasPlaying) { audio.play(); _p('play-icon').textContent = 'pause'; }
    showToast(`🎛 ${val === 'original' ? 'Original quality' : val + ' kbps'}`);
}
