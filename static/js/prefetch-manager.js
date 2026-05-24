/**
 * prefetch-manager.js
 * Prefetches upcoming audio tracks into memory as Object URLs,
 * so playback starts instantly without a network round-trip.
 *
 * Features:
 *  - LRU eviction capped at MAX_CACHED entries.
 *  - File-size guard: skips tracks estimated above MAX_BYTES.
 *  - Hover-delay prefetch: starts fetching after the cursor
 *    has dwelled over a track row for HOVER_DELAY_MS ms.
 *  - Revokes all Object URLs on page unload.
 *
 * Depends on globals: queue, cursor  (state.js)
 */
const PrefetchManager = (() => {
    const MAX_CACHED     = 4;              // max tracks kept in memory
    const MAX_BYTES      = 25 * 1024*1024; // 25 MB — skip anything larger
    const HOVER_DELAY_MS = 400;            // cursor dwell time before fetch

    const cache   = new Map();   // path → { url: ObjectURL, size: number }
    const pending = new Map();   // path → Promise
    const hTimers = new Map();   // path → setTimeout id

    function _estimateBytes(track) {
        // bitrate (kbps) × duration (s) × 125  ≈  file size in bytes
        return (track.bitrate || 256) * (track.duration || 180) * 125;
    }

    async function _fetch(track) {
        if (cache.has(track.path) || pending.has(track.path)) return;
        if (_estimateBytes(track) > MAX_BYTES) return; // too large

        const p = (async () => {
            try {
                // Only prefetch original-quality streams;
                // transcoded FFmpeg pipes aren't cacheable.
                const url  = `/api/stream/${encodeURIComponent(track.path)}`;
                const resp = await fetch(url);
                if (!resp.ok) return;
                const blob   = await resp.blob();
                const objUrl = URL.createObjectURL(blob);

                // Evict the oldest entry if over limit
                if (cache.size >= MAX_CACHED) {
                    const oldKey = cache.keys().next().value;
                    URL.revokeObjectURL(cache.get(oldKey).url);
                    cache.delete(oldKey);
                }
                cache.set(track.path, { url: objUrl, size: blob.size });
            } catch (_) { /* silent — prefetch is best-effort */ }
        })();

        pending.set(track.path, p);
        await p;
        pending.delete(track.path);
    }

    return {
        /** Return cached ObjectURL for path, or null. */
        getUrl(path) {
            return cache.has(path) ? cache.get(path).url : null;
        },

        /** Prefetch the next N tracks ahead of the current cursor. */
        prefetchUpcoming(n = 2) {
            for (let i = 1; i <= n; i++) {
                const idx = cursor + i;
                if (idx < queue.length) _fetch(queue[idx]);
            }
        },

        /** Start hover-delay timer for a track row. */
        onHoverStart(track) {
            if (!track || cache.has(track.path)) return;
            const id = setTimeout(() => _fetch(track), HOVER_DELAY_MS);
            hTimers.set(track.path, id);
        },

        /** Cancel hover timer if cursor left before delay fired. */
        onHoverEnd(track) {
            if (!track) return;
            clearTimeout(hTimers.get(track.path));
            hTimers.delete(track.path);
        },

        /** Revoke all ObjectURLs on page unload to free memory. */
        cleanup() {
            cache.forEach(({ url }) => URL.revokeObjectURL(url));
            cache.clear();
            pending.clear();
            hTimers.forEach(id => clearTimeout(id));
            hTimers.clear();
        },

        /** Check whether a track path is already in cache. */
        isCached(path) { return cache.has(path); },
    };
})();
