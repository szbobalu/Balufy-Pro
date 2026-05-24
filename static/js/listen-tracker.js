/**
 * listen-tracker.js
 * Records how long the user actually listened to each track
 * and reports it to /api/listen-event for the habit model.
 *
 * Uses keepalive:true so the beacon survives tab-close.
 * Silently ignores plays shorter than 5 seconds (accidental clicks).
 *
 * Depends on globals: csrfToken  (state.js)
 */
const ListenTracker = (() => {
    let _path  = null;
    let _start = null;

    function _report() {
        if (!_path || !_start) return;
        const secs = Math.round((Date.now() - _start) / 1000);
        if (secs < 5) { _path = null; _start = null; return; } // skip accidental clicks

        const body = JSON.stringify({ path: _path, play_secs: secs });
        fetch('/api/listen-event', {
            method:    'POST',
            headers:   { 'Content-Type': 'application/json',
                         'X-CSRF-Token': csrfToken() },
            body,
            keepalive: true,   // survives page navigation / close
        }).catch(() => {});

        _path  = null;
        _start = null;
    }

    return {
        /** Call when a new track starts playing. */
        start(path) { _report(); _path = path; _start = Date.now(); },
        /** Call when playback stops (beforeunload, skip, etc.). */
        stop()      { _report(); },
    };
})();
