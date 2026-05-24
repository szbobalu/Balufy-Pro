/**
 * app.js
 * Application entry point.
 * Bootstraps data fetching, wires up keyboard shortcuts, and
 * registers page-lifecycle handlers.
 *
 * Must be loaded last — all other modules must already be present.
 *
 * Depends on all other Balufy JS modules.
 */

// ── Bootstrap ─────────────────────────────────────────────────
window.onload = async () => {
    try {
        const [libResp, likedResp] = await Promise.all([
            fetch('/api/library'),
            fetch('/api/liked'),
        ]);
        library = await libResp.json();
        const likedData = await likedResp.json();
        likedPaths = new Set(likedData.paths || []);

        renderLibrary();
        renderArtists();
        updateLikedCount();
    } catch (e) {
        console.error('Load failed', e);
    }

    pollCacheProgress();
};

// ── Page lifecycle ────────────────────────────────────────────
window.addEventListener('beforeunload', () => {
    ListenTracker.stop();
    PrefetchManager.cleanup();
});

// ── Keyboard shortcuts ────────────────────────────────────────
document.addEventListener('keydown', e => {
    // Don't capture shortcuts while the user is typing
    if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;

    if (e.key === ' ') {
        e.preventDefault();
        togglePlay();
        showToast(audio.paused ? '⏸ Paused' : '▶ Playing');
    } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        playNext();
        showToast('⏭ Next');
    } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        playPrev();
        showToast('⏮ Prev');
    } else if (e.key === 's' || e.key === 'S') {
        toggleShuffle();
    } else if (e.key === 'r' || e.key === 'R') {
        toggleRepeat();
    } else if (e.key === 'l' || e.key === 'L') {
        toggleLyrics();
        showToast(lyricsOpen ? '🎤 Lyrics on' : '🎤 Lyrics off');
    } else if (e.key === 'h' || e.key === 'H') {
        toggleLikeCurrentTrack();
    }
});
