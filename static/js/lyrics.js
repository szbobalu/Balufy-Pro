/**
 * lyrics.js
 * Lyrics panel: toggle, fetching from /api/lyrics (backed by LRCLIB),
 * LRC parsing, synced/plain rendering, real-time line highlighting,
 * click-to-seek, and lyrics-prefetch progress pill.
 *
 * Depends on globals:
 *   state.js  — audio (for currentTime in seekToLyric)
 *   utils.js  — escHtml
 */

// ── Module state ──────────────────────────────────────────────
let lyricsData     = [];    // parsed LRC lines: [{time, text}, …]
let lyricsOpen     = false;
let activeLyricIdx = -1;
let lyricsFetched  = '';    // "title|||artist" key to avoid re-fetching

// ── Panel toggle ──────────────────────────────────────────────
function toggleLyrics() {
    lyricsOpen = !lyricsOpen;
    document.getElementById('lyrics-panel').classList.toggle('open', lyricsOpen);
    document.getElementById('btn-lyrics').classList.toggle('active', lyricsOpen);
}

// ── Fetch lyrics from server (which proxies LRCLIB + SQLite cache) ─
async function fetchLyrics(title, artist) {
    const key = `${title}|||${artist}`;
    if (key === lyricsFetched) return;
    lyricsFetched  = key;
    lyricsData     = [];
    activeLyricIdx = -1;

    const scroll = document.getElementById('lyrics-scroll');
    scroll.innerHTML = `<div class="lyrics-loading"><span class="material-symbols-outlined">sync</span> Loading lyrics…</div>`;
    document.getElementById('lyrics-source').innerHTML = '';

    // Strip parenthetical / bracketed suffixes from the title before querying
    const cleanTitle = title.replace(/\s*[\(\[].*?[\)\]]/g, '').trim();

    try {
        const params = new URLSearchParams({ track_name: cleanTitle, artist_name: artist });
        const res    = await fetch(`/api/lyrics?${params}`);
        if (res.status === 404) throw new Error('not_found');
        if (!res.ok)            throw new Error('api_error');
        const data = await res.json();

        document.getElementById('lyrics-source').innerHTML =
            `Lyrics · <a href="https://lrclib.net" target="_blank" rel="noopener">LRCLIB</a>`;

        if (data.syncedLyrics) {
            lyricsData = parseLRC(data.syncedLyrics);
            renderSyncedLyrics();
        } else if (data.plainLyrics) {
            renderPlainLyrics(data.plainLyrics);
        } else {
            throw new Error('empty');
        }
    } catch (e) {
        lyricsData = [];
        const msg = (e.message === 'not_found' || e.message === 'empty')
            ? 'No lyrics found for this track'
            : 'Could not reach lyrics service';
        scroll.innerHTML = `<div class="lyrics-status">${msg}</div>`;
        document.getElementById('lyrics-source').innerHTML = '';
    }
}

// ── LRC parser ────────────────────────────────────────────────
/**
 * Parse an LRC string into an array of {time, text} objects,
 * sorted by ascending time.
 * @param {string} lrc
 * @returns {{time: number, text: string}[]}
 */
function parseLRC(lrc) {
    const re = /\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)/;
    return lrc.split('\n')
        .map(line => {
            const m = line.match(re);
            if (!m) return null;
            const time = parseInt(m[1]) * 60 + parseInt(m[2]) + parseInt(m[3].padEnd(3, '0')) / 1000;
            return { time, text: m[4].trim() };
        })
        .filter(Boolean)
        .sort((a, b) => a.time - b.time);
}

// ── Renderers ─────────────────────────────────────────────────
function renderSyncedLyrics() {
    const scroll  = document.getElementById('lyrics-scroll');
    const spacer  = '<div style="height:42%"></div>';
    scroll.innerHTML = spacer +
        lyricsData.map((l, i) => {
            if (l.text === '') return `<div class="lyric-line blank-line" data-idx="${i}"></div>`;
            return `<div class="lyric-line" data-idx="${i}" onclick="seekToLyric(${i})">${escHtml(l.text)}</div>`;
        }).join('') + spacer;
}

function renderPlainLyrics(text) {
    const scroll = document.getElementById('lyrics-scroll');
    const spacer = '<div style="height:20%"></div>';
    scroll.innerHTML = spacer +
        text.split('\n').map(line =>
            `<div class="lyric-line">${line ? escHtml(line) : '<span style="opacity:0.25">·</span>'}</div>`
        ).join('') + spacer;
}

// ── Real-time sync (called every timeupdate) ──────────────────
/**
 * Highlight the lyric line matching currentTime, scroll it into
 * the centre of the panel, and dim surrounding lines.
 * @param {number} currentTime – audio.currentTime
 */
function syncLyrics(currentTime) {
    if (!lyricsData.length) return;
    let idx = -1;
    for (let i = 0; i < lyricsData.length; i++) {
        if (lyricsData[i].time <= currentTime + 0.25) idx = i;
        else break;
    }
    if (idx === activeLyricIdx) return;
    activeLyricIdx = idx;

    const scroll = document.getElementById('lyrics-scroll');
    scroll.querySelectorAll('.lyric-line[data-idx]').forEach(el => {
        const i = parseInt(el.dataset.idx);
        el.classList.remove('active', 'near');
        if (i === idx)                   el.classList.add('active');
        else if (Math.abs(i - idx) <= 2) el.classList.add('near');
    });

    if (idx >= 0) {
        const activeLine = scroll.querySelector(`.lyric-line[data-idx="${idx}"]`);
        if (activeLine) {
            const top = activeLine.offsetTop - scroll.clientHeight / 2 + activeLine.clientHeight / 2;
            scroll.scrollTo({ top, behavior: 'smooth' });
        }
    }
}

// ── Click-to-seek ─────────────────────────────────────────────
function seekToLyric(idx) {
    if (lyricsData[idx] !== undefined) audio.currentTime = lyricsData[idx].time;
}

// ── Lyrics prefetch progress pill ────────────────────────────
let _pollTimer = null;

/**
 * Poll /api/lyrics/cache-status and update the progress pill
 * in the nav bar. Reschedules itself at an interval that depends
 * on whether a background cache run is still active.
 */
async function pollCacheProgress() {
    try {
        const res  = await fetch('/api/lyrics/cache-status');
        const data = await res.json();
        const el   = document.getElementById('cache-progress');
        const bar  = document.getElementById('cp-bar');
        const txt  = document.getElementById('cp-text');

        if (data.total > 0 && (data.running || data.cached < data.total)) {
            const pct = Math.round((data.cached / data.total) * 100);
            el.classList.add('visible');
            bar.style.width = pct + '%';
            txt.textContent = `${data.cached}/${data.total}`;
        } else {
            el.classList.remove('visible');
        }

        clearTimeout(_pollTimer);
        _pollTimer = setTimeout(pollCacheProgress, data.running ? 3000 : 15000);
    } catch(e) {
        clearTimeout(_pollTimer);
        _pollTimer = setTimeout(pollCacheProgress, 30000);
    }
}
