/**
 * cover-loader.js  [OPTIMISED]
 * Throttled, IntersectionObserver-based lazy-loader for album-art images.
 *
 * Images are marked up as:
 *   <img class="lazy-cover" data-src="/api/cover/...">
 *
 * After inserting HTML into the DOM, call:
 *   CoverLoader.observeAll(containerElement);
 *
 * Changes vs original:
 *  - load/error event listeners marked { passive: true } — safe because
 *    neither handler calls preventDefault().
 *  - Early-return guard in drain() avoids the function-call overhead
 *    when there is clearly nothing to process.
 */
const CoverLoader = (() => {
    const MAX_CONCURRENT = 3;
    let active = 0;
    const queue = [];

    function load(img) {
        active++;
        img.src = img.dataset.src;
        const done = () => { active--; drain(); };
        img.addEventListener('load',  done, { once: true, passive: true });
        img.addEventListener('error', done, { once: true, passive: true });
    }

    function drain() {
        // Early return avoids the while-condition evaluation when idle.
        if (!queue.length || active >= MAX_CONCURRENT) return;
        while (active < MAX_CONCURRENT && queue.length) load(queue.shift());
    }

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
         * @param {Element} [root=document]
         */
        observeAll(root = document) {
            root.querySelectorAll('img.lazy-cover[data-src]').forEach(img => observer.observe(img));
        },
    };
})();
