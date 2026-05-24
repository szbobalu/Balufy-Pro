/**
 * state.js
 * Shared mutable state and the CSRF helper.
 * Must be loaded before every other Balufy JS module.
 */

// ── Library data (populated on window.onload) ─────────────────
let library = { albums: [], tracks: [] };

// ── Playback queue & cursor ───────────────────────────────────
let queue  = [];
let cursor = -1;

// ── Playback mode flags ───────────────────────────────────────
let shuffleMode = false;
let repeatMode  = false; // false | 'one' | 'all'

// ── Per-view track caches (used by play-all / shuffle) ────────
let currentAlbumTracks  = [];
let currentArtistTracks = [];
let currentHitsTracks   = [];

// ── Liked song paths ──────────────────────────────────────────
let likedPaths = new Set();

// ── Stream quality setting ────────────────────────────────────
let streamQuality = 'original';

// ── Active tab identifier ─────────────────────────────────────
let currentTab = 'library';

// ── CSRF helper ───────────────────────────────────────────────
function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]').content;
}

// ── Audio element (DOM must exist; scripts are end-of-body) ───
const audio = document.getElementById('main-audio');
