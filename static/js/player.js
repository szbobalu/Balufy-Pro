/**
 * player.js
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
 *   lyrics.js      — fetchLyrics, syncLyrics (called via audio events)
 *   equalizer.js   — _eqCtx (resumed on play)
 */

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

    // Report previous track listen duration
    ListenTracker.start(track.path);

    // Use prefetched Blob URL when available (original quality only);
    // fall back to the stream endpoint for transcoded or un-cached tracks.
    const cachedUrl = (streamQuality === 'original')
        ? PrefetchManager.getUrl(track.path)
        : null;
    audio.src = cachedUrl || buildStreamUrl(track.path, 0);

    document.getElementById('p-title').innerText  = track.title;
    document.getElementById('p-artist').innerText = track.artist;
    document.getElementById('tot-time').innerText = track.duration_fmt;

    // Player art — single on-demand request, bypasses CoverLoader intentionally.
    const artBox = document.getElementById('p-art');
    artBox.innerHTML = track.has_cover
        ? `<img src="/api/cover/${encodeURIComponent(track.path)}" loading="eager">`
        : '<span class="material-symbols-outlined" style="margin: 14px; color: #333;">music_note</span>';
    artBox.classList.add('is-playing');

    if (track.has_cover) {
        extractAmbientColor(`/api/cover/${encodeURIComponent(track.path)}`)
            .then(({ r, g, b }) => applyAmbientColor(r, g, b));
    } else {
        clearAmbientColor();
    }

    audio.play();
    if (typeof _eqCtx !== 'undefined' && _eqCtx && _eqCtx.state === 'suspended') {
        _eqCtx.resume();
    }
    document.getElementById('play-icon').innerText = 'pause';

    updatePlayingRow();
    updatePlayerHeart();
    fetchLyrics(track.title, track.artist);

    // Kick off prefetch for the next couple of tracks in the queue
    PrefetchManager.prefetchUpcoming(2);
}

// ── Highlight the currently playing row in every track list ──
function updatePlayingRow() {
    document.querySelectorAll('.track-row').forEach(row => {
        row.classList.remove('playing', 'paused');
    });
    if (cursor < 0 || !queue[cursor]) return;
    const path   = encodeURIComponent(queue[cursor].path);
    const active = document.querySelector(`.track-row[data-path="${path}"]`);
    if (active) active.classList.add('playing');
}

// ── Transport controls ────────────────────────────────────────
function togglePlay() {
    if (!audio.src) return;
    if (audio.paused) {
        audio.play();
        document.getElementById('play-icon').innerText = 'pause';
        document.getElementById('p-art').classList.add('is-playing');
        document.querySelectorAll('.track-row.playing').forEach(r => r.classList.remove('paused'));
    } else {
        audio.pause();
        document.getElementById('play-icon').innerText = 'play_arrow';
        document.getElementById('p-art').classList.remove('is-playing');
        document.querySelectorAll('.track-row.playing').forEach(r => r.classList.add('paused'));
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
    document.getElementById('btn-shuffle').classList.toggle('active', shuffleMode);
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
    if (!repeatMode)            repeatMode = 'all';
    else if (repeatMode === 'all') repeatMode = 'one';
    else                        repeatMode = false;
    updateRepeatUI();
    const labels = { false: '↺ Repeat off', all: '↺ Repeat all', one: '↺ Repeat one' };
    showToast(labels[repeatMode]);
}

function updateRepeatUI() {
    const btn = document.getElementById('btn-repeat');
    btn.classList.toggle('active', !!repeatMode);
    btn.querySelector('span').innerText = repeatMode === 'one' ? 'repeat_one' : 'repeat';
}

// ── Audio element event handlers ─────────────────────────────
audio.ontimeupdate = () => {
    if (!audio.duration) return;
    const pct = (audio.currentTime / audio.duration) * 100;
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('cur-time').innerText = fmtTime(audio.currentTime);
    syncLyrics(audio.currentTime);
};

audio.onended = () => {
    if (repeatMode === 'one') { audio.currentTime = 0; audio.play(); return; }
    if (cursor < queue.length - 1 || repeatMode === 'all') playNext();
};

// ── Scrubber ──────────────────────────────────────────────────
function seek(e) {
    const rect = document.getElementById('progress-bar').getBoundingClientRect();
    const pos  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    audio.currentTime = pos * audio.duration;
}

function scrubHover(e) {
    if (!audio.duration) return;
    const bar  = document.getElementById('progress-bar');
    const rect = bar.getBoundingClientRect();
    const pos  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    bar.style.setProperty('--hover-pct', (pos * 100) + '%');
    bar.setAttribute('data-hover-time', fmtTime(pos * audio.duration));
}

function clearScrubHover() {
    document.getElementById('progress-bar').removeAttribute('data-hover-time');
}

// ── Volume ────────────────────────────────────────────────────
function setVolume(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const vol  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    audio.volume = vol;
    document.getElementById('vol-fill').style.width = (vol * 100) + '%';
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
                const r = data[i], g = data[i+1], b = data[i+2];
                const luma = (r * 299 + g * 587 + b * 114) / 1000;
                if (luma < 28 || luma > 228) continue;
                const max = Math.max(r, g, b), min = Math.min(r, g, b);
                const sat = max > 0 ? (max - min) / max : 0;
                if (sat < 0.18) continue;
                candidates.push({ r, g, b, score: sat * (luma / 160) });
            }

            if (!candidates.length) { resolve({ r: 70, g: 70, b: 70 }); return; }
            candidates.sort((a, b) => b.score - a.score);
            const top = candidates.slice(0, Math.max(1, Math.floor(candidates.length * 0.2)));
            const sum = top.reduce((acc, c) => ({ r: acc.r+c.r, g: acc.g+c.g, b: acc.b+c.b }), { r:0, g:0, b:0 });
            resolve({
                r: Math.round(sum.r / top.length),
                g: Math.round(sum.g / top.length),
                b: Math.round(sum.b / top.length)
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
    document.getElementById('ambient-layer').style.background = `
        radial-gradient(ellipse 90% 45% at 50% 108%,  rgba(${r},${g},${b},0.20) 0%, transparent 70%),
        radial-gradient(ellipse 55% 55% at 8%  48%,   rgba(${r},${g},${b},0.07) 0%, transparent 65%)
    `;
}

function clearAmbientColor() {
    document.documentElement.style.setProperty('--amb-r', 0);
    document.documentElement.style.setProperty('--amb-g', 0);
    document.documentElement.style.setProperty('--amb-b', 0);
    document.getElementById('ambient-layer').style.background = 'transparent';
}

// ── Quality / bitrate ─────────────────────────────────────────
function onQualityChange(val) {
    streamQuality = val;
    if (!audio.src || cursor < 0 || !queue[cursor]) return;
    const wasPlaying = !audio.paused;
    // Prefetch cache is original-only; transcoded paths must always stream
    audio.src = buildStreamUrl(queue[cursor].path, 0);
    if (wasPlaying) { audio.play(); document.getElementById('play-icon').innerText = 'pause'; }
    showToast(`🎛 ${val === 'original' ? 'Original quality' : val + ' kbps'}`);
}
