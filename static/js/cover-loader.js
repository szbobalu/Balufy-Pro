/**
 * cover-loader.js
 * Throttled, IntersectionObserver-based lazy-loader for album-art images.
 *
 * Images are marked up as:
 *   <img class="lazy-cover" data-src="/api/cover/...">
 *
 * After inserting HTML into the DOM, call:
 *   CoverLoader.observeAll(containerElement);
 *
 * The loader starts requests 300 px before the image enters the viewport
 * and caps concurrent in-flight fetches at MAX_CONCURRENT, preventing
 * burst 429s from the /api/cover endpoint.
 */
const CoverLoader = (() => {
    const MAX_CONCURRENT = 3;   // max parallel cover fetches
    let active = 0;
    const queue = [];

    function load(img) {
        active++;
        img.src = img.dataset.src;
        const done = () => { active--; drain(); };
        img.addEventListener('load',  done, { once: true });
        img.addEventListener('error', done, { once: true });
    }

    function drain() {
        while (active < MAX_CONCURRENT && queue.length) load(queue.shift());
    }

    // Start loading 300 px before the image enters the viewport
    // so there is no visible pop-in on normal scroll speed.
    const observer = new IntersectionObserver(entries => {
        entries.forEach(e => {
            if (!e.isIntersecting) return;
            observer.unobserve(e.target);
            if (e.target.dataset.src) { queue.push(e.target); drain(); }
        });
    }, { rootMargin: '300px' });

    return {
        /**
         * Observe every img.lazy-cover[data-src] inside root.
         * Call this after injecting HTML into the DOM.
         * @param {Element} [root=document]
         */
        observeAll(root = document) {
            root.querySelectorAll('img.lazy-cover[data-src]').forEach(img => observer.observe(img));
        },
    };
})();
