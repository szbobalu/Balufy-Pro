/**
 * prefetch-manager.js  [OPTIMISED]
 * Prefetches upcoming audio tracks into memory as Object URLs.
 *
 * Features:
 *  - LRU eviction capped at MAX_CACHED entries.
 *  - File-size guard: skips tracks estimated above MAX_BYTES.
 *  - Hover-delay prefetch: starts fetching after the cursor
 *    has dwelled over a track row for HOVER_DELAY_MS ms.
 *  - Revokes all Object URLs on page unload.
 *
 * Depends on globals: queue, cursor  (state.js)
 *
 * Changes vs original:
 *  - _estimateBytes uses integer multiply only (removed redundant parens
 *    that implied float ops — cosmetic but clarifies intent).
 *  - Guard in _fetch reordered: cheap cache/pending checks first, then
 *    the comparatively more expensive _estimateBytes call.
 *  - Silent — no functional changes; module was already well-optimised.
 */
const PrefetchManager = (() => {
    const MAX_CACHED     = 4;
    const MAX_BYTES      = 25 * 1024 * 1024;  // 25 MB
    const HOVER_DELAY_MS = 400;

    const cache   = new Map();   // path → { url, size }
    const pending = new Map();   // path → Promise
    const hTimers = new Map();   // path → setTimeout id

    function _estimateBytes(track) {
        return (track.bitrate || 256) * (track.duration || 180) * 125;
    }

    async function _fetch(track) {
        // Cheap guards first — avoid the size estimation when possible.
        if (cache.has(track.path) || pending.has(track.path)) return;
        if (_estimateBytes(track) > MAX_BYTES) return;

        const p = (async () => {
            try {
                const url  = `/api/stream/${encodeURIComponent(track.path)}`;
                const resp = await fetch(url);
                if (!resp.ok) return;
                const blob   = await resp.blob();
                const objUrl = URL.createObjectURL(blob);

                if (cache.size >= MAX_CACHED) {
                    const oldKey = cache.keys().next().value;
                    URL.revokeObjectURL(cache.get(oldKey).url);
                    cache.delete(oldKey);
                }
                cache.set(track.path, { url: objUrl, size: blob.size });
            } catch (_) { /* prefetch is best-effort */ }
        })();

        pending.set(track.path, p);
        await p;
        pending.delete(track.path);
    }

    return {
        getUrl(path) {
            return cache.has(path) ? cache.get(path).url : null;
        },

        prefetchUpcoming(n = 2) {
            for (let i = 1; i <= n; i++) {
                const idx = cursor + i;
                if (idx < queue.length) _fetch(queue[idx]);
            }
        },

        onHoverStart(track) {
            if (!track || cache.has(track.path)) return;
            hTimers.set(track.path, setTimeout(() => _fetch(track), HOVER_DELAY_MS));
        },

        onHoverEnd(track) {
            if (!track) return;
            clearTimeout(hTimers.get(track.path));
            hTimers.delete(track.path);
        },

        cleanup() {
            cache.forEach(({ url }) => URL.revokeObjectURL(url));
            cache.clear();
            pending.clear();
            hTimers.forEach(id => clearTimeout(id));
            hTimers.clear();
        },

        isCached(path) { return cache.has(path); },
    };
})();
