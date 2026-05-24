/**
 * equalizer.js
 * 5-band parametric equaliser built on the Web Audio API.
 * Lazily initialised on first open to respect the browser's
 * autoplay policy.
 *
 * Depends on globals:
 *   state.js — audio
 */

// ── EQ state ──────────────────────────────────────────────────
let _eqCtx     = null;
let _eqSrc     = null;
let _eqFilters = [];
let _eqOpen    = false;

// ── Band definitions ──────────────────────────────────────────
// Each entry: { freq (Hz), label }
const EQ_BANDS = [
    { freq: 60,    label: '60Hz'  },
    { freq: 250,   label: '250Hz' },
    { freq: 1000,  label: '1kHz'  },
    { freq: 4000,  label: '4kHz'  },
    { freq: 16000, label: '16kHz' },
];

// ── Preset gain values (dB) in EQ_BANDS order ────────────────
const EQ_PRESETS = {
    flat:       [  0,   0,   0,   0,   0 ],
    bass:       [  8,   5,   0,  -1,  -1 ],
    vocal:      [ -2,   0,   4,   3,   1 ],
    treble:     [ -1,  -1,   1,   4,   7 ],
    electronic: [  5,   3,  -1,   2,   4 ],
};

// ── Lazy AudioContext initialisation ──────────────────────────
function _initEq() {
    if (_eqCtx) return; // already set up
    try {
        _eqCtx = new (window.AudioContext || window.webkitAudioContext)();
        _eqSrc = _eqCtx.createMediaElementSource(audio);

        // Build biquad filter chain: source → f0 → f1 → … → destination
        let prev = _eqSrc;
        _eqFilters = EQ_BANDS.map(({ freq }, i) => {
            const f = _eqCtx.createBiquadFilter();
            f.type            = (i === 0) ? 'lowshelf'
                              : (i === EQ_BANDS.length - 1) ? 'highshelf'
                              : 'peaking';
            f.frequency.value = freq;
            f.gain.value      = 0;
            f.Q.value         = 1.0;
            prev.connect(f);
            prev = f;
            return f;
        });
        prev.connect(_eqCtx.destination);
    } catch(e) {
        console.warn('Web Audio API unavailable:', e);
        _eqCtx = null;
    }
}

// ── Build EQ band slider UI ───────────────────────────────────
function _buildEqBandsUI() {
    const container = document.getElementById('eq-bands');
    container.innerHTML = EQ_BANDS.map((band, i) => `
        <div class="eq-band">
            <span class="eq-gain-label" id="eq-gain-${i}">0dB</span>
            <div class="eq-slider-wrap">
                <input type="range"
                       class="eq-slider"
                       id="eq-slider-${i}"
                       min="-12" max="12" step="0.5" value="0"
                       oninput="eqSetBand(${i}, this.value)"
                       title="${band.label}">
            </div>
            <span class="eq-freq-label">${band.label}</span>
        </div>
    `).join('');
}

// ── Toggle EQ panel ───────────────────────────────────────────
function toggleEqPanel() {
    _eqOpen = !_eqOpen;
    _initEq();
    if (_eqOpen && document.getElementById('eq-bands').children.length === 0) {
        _buildEqBandsUI();
    }
    // Resume AudioContext if suspended (browser autoplay policy)
    if (_eqCtx && _eqCtx.state === 'suspended') _eqCtx.resume();
    document.getElementById('eq-panel').classList.toggle('open', _eqOpen);
    document.getElementById('btn-eq').classList.toggle('active', _eqOpen);
}

// ── Set a single band ─────────────────────────────────────────
function eqSetBand(index, gainDb) {
    const gain = parseFloat(gainDb);
    if (_eqFilters[index]) _eqFilters[index].gain.value = gain;
    const label = document.getElementById(`eq-gain-${index}`);
    if (label) label.textContent = (gain >= 0 ? '+' : '') + gain.toFixed(1) + 'dB';
    // Deactivate preset pill since user is now customising
    document.querySelectorAll('.eq-preset-btn').forEach(b => b.classList.remove('active'));
}

// ── Apply a preset ────────────────────────────────────────────
function eqPreset(name) {
    _initEq();
    if (_eqCtx && _eqCtx.state === 'suspended') _eqCtx.resume();

    // Build UI if not yet built (panel might not have been opened yet)
    if (document.getElementById('eq-bands').children.length === 0) _buildEqBandsUI();

    const gains = EQ_PRESETS[name] || EQ_PRESETS.flat;
    gains.forEach((g, i) => {
        if (_eqFilters[i]) _eqFilters[i].gain.value = g;
        const slider = document.getElementById(`eq-slider-${i}`);
        const label  = document.getElementById(`eq-gain-${i}`);
        if (slider) slider.value = g;
        if (label)  label.textContent = (g >= 0 ? '+' : '') + g.toFixed(1) + 'dB';
    });

    document.querySelectorAll('.eq-preset-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('onclick') === `eqPreset('${name}')`);
    });
}

function eqReset() { eqPreset('flat'); }

// ── Close panel when clicking outside ────────────────────────
document.addEventListener('click', e => {
    if (!_eqOpen) return;
    const panel = document.getElementById('eq-panel');
    const btn   = document.getElementById('btn-eq');
    if (!panel.contains(e.target) && !btn.contains(e.target)) {
        _eqOpen = false;
        panel.classList.remove('open');
        btn.classList.remove('active');
    }
});

// ── Resume AudioContext on any user interaction ───────────────
// Required by browsers that suspend AudioContext until a gesture.
document.addEventListener('click', () => {
    if (_eqCtx && _eqCtx.state === 'suspended') _eqCtx.resume();
}, { once: false });
