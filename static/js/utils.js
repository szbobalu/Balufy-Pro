/**
 * utils.js
 * Small, stateless helper functions used across all modules.
 */

/**
 * Escape HTML special characters so user data can be safely
 * inserted with innerHTML.
 * @param {*} s
 * @returns {string}
 */
function escHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/**
 * Format a duration in seconds as M:SS.
 * @param {number} secs
 * @returns {string}
 */
function fmtTime(secs) {
    const s = Math.floor(secs);
    return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, '0')}`;
}

// ── Keyboard shortcut / action toast ─────────────────────────
let _toastTimer;

/**
 * Show a brief notification toast at the bottom of the screen.
 * @param {string} msg
 */
function showToast(msg) {
    const el = document.getElementById('kbd-toast');
    el.innerText = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove('show'), 1500);
}
