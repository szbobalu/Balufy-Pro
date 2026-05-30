/**
 * equalizer.js  [OPTIMISED]
 * 5-band parametric equaliser built on the Web Audio API.
 * Lazily initialised on first open to respect the browser's
 * autoplay policy.
 *
 * Depends on globals:
 *   state.js — audio
 *
 * Changes vs original:
 *  - _eq() helper caches getElementById results in _EQC map.
 *  - Two separate document.addEventListener('click') merged into one,
 *    saving a listener registration and one handler invocation per click.
 *  - Combined handler marked { passive: true } — safe because neither
 *    branch calls preventDefault().
 *  - Optional chaining for _eqCtx?.state.
 *  - textContent over innerText.
 */

// ── DOM element cache ─────────────────────────────────────────
const _EQC = {};
const _eq = id => _EQC[id] ??= document.getElementById(id);

// ── EQ state ──────────────────────────────────────────────────
let _eqCtx     = null;
let _eqSrc     = null;
let _eqFilters = [];
let _eqOpen    = false;

// ── Band definitions ──────────────────────────────────────────
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
    if (_eqCtx) return;
    try {
        _eqCtx = new (window.AudioContext || window.webkitAudioContext)();
        _eqSrc = _eqCtx.createMediaElementSource(audio);

        let prev = _eqSrc;
        _eqFilters = EQ_BANDS.map(({ freq }, i) => {
            const f = _eqCtx.createBiquadFilter();
            f.type            = i === 0 ? 'lowshelf'
                              : i === EQ_BANDS.length - 1 ? 'highshelf'
                              : 'peaking';
            f.frequency.value = freq;
            f.gain.value      = 0;
            f.Q.value         = 1.0;
            prev.connect(f);
            prev = f;
            return f;
        });
        prev.connect(_eqCtx.destination);
    } catch (e) {
        console.warn('Web Audio API unavailable:', e);
        _eqCtx = null;
    }
}

// ── Build EQ band slider UI ───────────────────────────────────
function _buildEqBandsUI() {
    _eq('eq-bands').innerHTML = EQ_BANDS.map((band, i) => `
        <div class="eq-band">
            <span class="eq-gain-label" id="eq-gain-${i}">0dB</span>
            <div class="eq-slider-wrap">
                <input type="range" class="eq-slider" id="eq-slider-${i}"
                       min="-12" max="12" step="0.5" value="0"
                       oninput="eqSetBand(${i}, this.value)"
                       title="${band.label}">
            </div>
            <span class="eq-freq-label">${band.label}</span>
        </div>
    `).join('');
    // Bust the element cache so freshly injected elements are picked up.
    EQ_BANDS.forEach((_, i) => { delete _EQC[`eq-gain-${i}`]; delete _EQC[`eq-slider-${i}`]; });
}

// ── Toggle EQ panel ───────────────────────────────────────────
function toggleEqPanel() {
    _eqOpen = !_eqOpen;
    _initEq();
    if (_eqOpen && _eq('eq-bands').children.length === 0) _buildEqBandsUI();
    if (_eqCtx?.state === 'suspended') _eqCtx.resume();
    _eq('eq-panel').classList.toggle('open', _eqOpen);
    _eq('btn-eq').classList.toggle('active', _eqOpen);
}

// ── Set a single band ─────────────────────────────────────────
function eqSetBand(index, gainDb) {
    const gain = parseFloat(gainDb);
    if (_eqFilters[index]) _eqFilters[index].gain.value = gain;
    const label = _eq(`eq-gain-${index}`);
    if (label) label.textContent = (gain >= 0 ? '+' : '') + gain.toFixed(1) + 'dB';
    document.querySelectorAll('.eq-preset-btn').forEach(b => b.classList.remove('active'));
}

// ── Apply a preset ────────────────────────────────────────────
function eqPreset(name) {
    _initEq();
    if (_eqCtx?.state === 'suspended') _eqCtx.resume();
    if (_eq('eq-bands').children.length === 0) _buildEqBandsUI();

    const gains = EQ_PRESETS[name] || EQ_PRESETS.flat;
    gains.forEach((g, i) => {
        if (_eqFilters[i]) _eqFilters[i].gain.value = g;
        const slider = _eq(`eq-slider-${i}`);
        const label  = _eq(`eq-gain-${i}`);
        if (slider) slider.value = g;
        if (label)  label.textContent = (g >= 0 ? '+' : '') + g.toFixed(1) + 'dB';
    });

    document.querySelectorAll('.eq-preset-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('onclick') === `eqPreset('${name}')`);
    });
}

function eqReset() { eqPreset('flat'); }

// ── Single merged document click handler ─────────────────────
// Handles both:
//  1. Closing the panel when the user clicks outside it.
//  2. Resuming AudioContext on any user gesture (browser autoplay policy).
// Marked passive: true because neither branch calls preventDefault().
document.addEventListener('click', e => {
    // Always attempt to resume a suspended context on any click.
    if (_eqCtx?.state === 'suspended') _eqCtx.resume();

    // Close panel if click landed outside it.
    if (!_eqOpen) return;
    if (!_eq('eq-panel').contains(e.target) && !_eq('btn-eq').contains(e.target)) {
        _eqOpen = false;
        _eq('eq-panel').classList.remove('open');
        _eq('btn-eq').classList.remove('active');
    }
}, { passive: true });
