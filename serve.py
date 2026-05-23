""" Balufy Pro — Personal Music Streaming Server Run: python serve.py 
Default password: password (override with BALUFY_PASSWORD env var) """

import os
import re
import json as json_mod
import hashlib
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import logging
import collections
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
from typing import Optional

from flask import (
    Flask, render_template, session, redirect, url_for,
    request, jsonify, Response, abort, send_file
)
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from mutagen.flac import FLAC
    from mutagen import File as MutagenFile
except ImportError:
    raise SystemExit("Missing dependency — run: pip install mutagen flask")

# ── Config & Globals ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("BALUFY_SECRET", secrets.token_hex(32))

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_SECURE   = True,
    SESSION_COOKIE_HTTPONLY = True,
    SESSION_COOKIE_SAMESITE = "Strict",
)

# ── Visitor logging ───────────────────────────────────────────────────────────

LOG_FILE = Path(os.environ.get("BALUFY_LOG", "visitors.log"))

_visit_logger = logging.getLogger("balufy.visits")
_visit_logger.setLevel(logging.INFO)
_visit_logger.propagate = False
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(message)s"))
_visit_logger.addHandler(_fh)

def log_visit(note: str = ""):
    """Append one line to visitors.log: timestamp | ip | method path | ua | note"""
    ip  = request.remote_addr or "unknown"
    ua  = request.headers.get("User-Agent", "-")
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _visit_logger.info("%s | %s | %s %s | %s%s",
        ts, ip, request.method, request.full_path.rstrip("?"), ua,
        f" | {note}" if note else ""
    )

# Path configuration for music storage.
AUDIO_DIR   = Path("/mnt/data/audio/")
PASSWORD    = os.environ.get("BALUFY_PASSWORD", "ChooseAPasswordPls")
PWD_HASH    = hashlib.sha256(PASSWORD.encode()).hexdigest()
SUPPORTED   = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aac", ".opus"}
MIME_MAP    = {
    ".flac": "audio/flac",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".ogg":  "audio/ogg",
    ".wav":  "audio/wav",
    ".aac":  "audio/aac",
    ".opus": "audio/opus",
}

FFMPEG_BIN = shutil.which("ffmpeg")

LIBRARY_CACHE = {"data": None, "last_updated": 0}
COVER_CACHE = {}

# ── Lyrics DB ─────────────────────────────────────────────────────────────────

LYRICS_DB          = Path(os.environ.get("BALUFY_LYRICS_DB", "lyrics.db"))
LYRICS_CACHE_TTL   = int(os.environ.get("BALUFY_LYRICS_TTL", 30 * 24 * 3600))
LYRICS_NF_TTL      = 7 * 24 * 3600

def _lyrics_db() -> sqlite3.Connection:
    """Open (and auto-init) the lyrics SQLite database."""
    db = sqlite3.connect(LYRICS_DB)
    db.execute("""
        CREATE TABLE IF NOT EXISTS lyrics (
            key           TEXT PRIMARY KEY,
            track_name    TEXT NOT NULL,
            artist_name   TEXT NOT NULL,
            synced_lyrics TEXT,
            plain_lyrics  TEXT,
            not_found     INTEGER NOT NULL DEFAULT 0,
            cached_at     INTEGER NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_lyrics_cached_at ON lyrics(cached_at)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS liked_songs (
            path      TEXT PRIMARY KEY,
            liked_at  INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS listen_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            path      TEXT    NOT NULL,
            played_at INTEGER NOT NULL,
            play_secs INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_lh_path ON listen_history(path)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lh_ts   ON listen_history(played_at)")
    db.commit()
    return db

def _lyrics_key(track: str, artist: str) -> str:
    """Normalised cache key: lower-cased, stripped, pipe-separated."""
    return f"{track.lower().strip()}|||{artist.lower().strip()}"

# ── Habits DB (server-side prediction) ────────────────────────────────────────

HABITS_DB          = Path(os.environ.get("BALUFY_HABITS_DB", "habits.db"))
PREDICT_CACHE_SIZE = int(os.environ.get("BALUFY_PREDICT_SIZE", "8"))

def _habits_db() -> sqlite3.Connection:
    """Open (and auto-init) the user habits database."""
    db = sqlite3.connect(HABITS_DB)
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            payload     TEXT NOT NULL,
            ts          INTEGER NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_ue_ts ON user_events(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ue_client ON user_events(client_id)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS transition_counts (
            from_path TEXT NOT NULL,
            to_path   TEXT NOT NULL,
            count     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (from_path, to_path)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS global_plays (
            path  TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 1
        )
    """)
    db.commit()
    return db

# ── Background lyrics prefetch ─────────────────────────────────────────────────

_prefetch_lock  = threading.Lock()
_prefetch_state = {"total": 0, "cached": 0, "running": False}

def _clean_title(title: str) -> str:
    """Strip parenthetical suffixes — mirrors the JS cleanTitle logic."""
    return re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip()

def _prefetch_lyrics_bg(tracks: list) -> None:
    """
    Daemon thread: pre-warm the lyrics cache for every track in the library.
    """
    global _prefetch_state
    now = int(time.time())

    todo: list[tuple[str, str]] = []
    db = _lyrics_db()
    try:
        for t in tracks:
            title  = _clean_title(t.get("title", ""))
            artist = t.get("artist", "")
            if not title or not artist or artist == "Unknown Artist":
                continue
            key = _lyrics_key(title, artist)
            row = db.execute(
                "SELECT not_found, cached_at FROM lyrics WHERE key=?", (key,)
            ).fetchone()
            if row:
                nf, cached_at = row
                ttl = LYRICS_NF_TTL if nf else LYRICS_CACHE_TTL
                if now - cached_at < ttl:
                    continue
            todo.append((title, artist))
    finally:
        db.close()

    already_cached = len(tracks) - len(todo)
    with _prefetch_lock:
        _prefetch_state = {
            "total":   len(tracks),
            "cached":  already_cached,
            "running": True,
        }

    for title, artist in todo:
        params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
        url    = f"https://lrclib.net/api/get?{params}"
        synced = plain = None
        nf_flag = 0

        try:
            req = urllib.request.Request(url, headers={"Lrclib-Client": "Balufy Pro"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data   = json_mod.loads(resp.read().decode())
            synced  = data.get("syncedLyrics") or None
            plain   = data.get("plainLyrics")  or None
            nf_flag = 0 if (synced or plain) else 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                nf_flag = 1
            else:
                time.sleep(2)
                continue
        except Exception:
            time.sleep(2)
            continue

        key = _lyrics_key(title, artist)
        ts  = int(time.time())
        db  = _lyrics_db()
        try:
            db.execute("""
                INSERT INTO lyrics
                    (key, track_name, artist_name, synced_lyrics, plain_lyrics,
                     not_found, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    synced_lyrics = excluded.synced_lyrics,
                    plain_lyrics  = excluded.plain_lyrics,
                    not_found     = excluded.not_found,
                    cached_at     = excluded.cached_at
            """, (key, title, artist, synced, plain, nf_flag, ts))
            db.commit()
        finally:
            db.close()

        with _prefetch_lock:
            _prefetch_state["cached"] += 1

        time.sleep(0.5)

    with _prefetch_lock:
        _prefetch_state["running"] = False


def _start_lyrics_prefetch(library: dict) -> None:
    """Collect every track from the library dict and kick off the prefetch daemon."""
    all_tracks: list = list(library.get("tracks", []))
    for alb in library.get("albums", []):
        all_tracks.extend(alb.get("tracks", []))
    threading.Thread(
        target=_prefetch_lyrics_bg, args=(all_tracks,), daemon=True
    ).start()

# ── Helpers ───────────────────────────────────────────────────────────────────

def secure_path(rel: str) -> Optional[Path]:
    """Resolve a relative path and ensure it stays inside AUDIO_DIR."""
    try:
        resolved = (AUDIO_DIR / rel).resolve()
        resolved.relative_to(AUDIO_DIR.resolve())
        return resolved
    except (ValueError, Exception):
        return None

def fmt_duration(seconds: int) -> str:
    """Format seconds into M:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

def get_track_info(filepath: Path) -> dict:
    """Extract metadata using mutagen."""
    rel = str(filepath.relative_to(AUDIO_DIR))
    info = {
        "path":         rel,
        "title":        filepath.stem,
        "artist":       "Unknown Artist",
        "album":        "Unknown Album",
        "duration":     0,
        "duration_fmt": "0:00",
        "track_number": 0,
        "has_cover":    False,
        "ext":          filepath.suffix.lower(),
        "bitrate":      0,
    }
    try:
        ext = filepath.suffix.lower()
        if ext == ".flac":
            audio = FLAC(filepath)
            def tag(k, default=""):
                v = audio.get(k, [default])
                return str(v[0]).strip() if v else default
            info["title"]  = tag("title")  or filepath.stem
            info["artist"] = tag("artist") or "Unknown Artist"
            info["album"]  = tag("album")  or "Unknown Album"
            tn = tag("tracknumber", "0")
            try:
                info["track_number"] = int(str(tn).split("/")[0])
            except ValueError:
                pass
            if audio.info:
                info["duration"]     = int(audio.info.length)
                info["duration_fmt"] = fmt_duration(audio.info.length)
                try:
                    if audio.info.length > 0:
                        info["bitrate"] = int(
                            filepath.stat().st_size * 8 / audio.info.length / 1000
                        )
                except Exception:
                    pass
            info["has_cover"] = bool(audio.pictures)
        else:
            easy = MutagenFile(filepath, easy=True)
            if easy and easy.tags:
                def etag(k, default=""):
                    v = easy.tags.get(k, [default])
                    return str(v[0]).strip() if v else default
                info["title"]  = etag("title")  or filepath.stem
                info["artist"] = etag("artist") or "Unknown Artist"
                info["album"]  = etag("album")  or "Unknown Album"
            if easy and easy.info:
                info["duration"]     = int(easy.info.length)
                info["duration_fmt"] = fmt_duration(easy.info.length)
                try:
                    if hasattr(easy.info, "bitrate") and easy.info.bitrate:
                        info["bitrate"] = easy.info.bitrate // 1000
                except Exception:
                    pass
            
            full = MutagenFile(filepath)
            if full:
                if hasattr(full, "pictures"):
                    info["has_cover"] = bool(full.pictures)
                elif full.tags:
                    info["has_cover"] = any(
                        k.startswith(("APIC", "covr")) for k in full.tags.keys()
                    )
    except Exception:
        pass
    return info

def scan_library_cached(force=False) -> dict:
    """Scan disk and cache results to prevent redundant I/O."""
    now = time.time()
    if LIBRARY_CACHE["data"] and not force and (now - LIBRARY_CACHE["last_updated"] < 300):
        return LIBRARY_CACHE["data"]

    if not AUDIO_DIR.exists():
        return {"albums": [], "tracks": []}

    albums, tracks = [], []
    for item in sorted(AUDIO_DIR.iterdir(), key=lambda x: x.name.lower()):
        if item.is_dir():
            album_tracks = [
                get_track_info(f)
                for f in sorted(item.iterdir(), key=lambda x: x.name.lower())
                if f.is_file() and f.suffix.lower() in SUPPORTED
            ]
            if not album_tracks:
                continue
            album_tracks.sort(key=lambda t: (t["track_number"] or 999, t["title"]))
            cover_path = next((t["path"] for t in album_tracks if t["has_cover"]), None)
            first = album_tracks[0]
            album_name  = first["album"]  if first["album"]  != "Unknown Album"  else item.name
            artist_name = first["artist"] if first["artist"] != "Unknown Artist" else "Various Artists"
            albums.append({
                "folder":      item.name,
                "name":        album_name,
                "artist":      artist_name,
                "cover_path":  cover_path,
                "tracks":      album_tracks,
                "track_count": len(album_tracks),
            })
        elif item.is_file() and item.suffix.lower() in SUPPORTED:
            tracks.append(get_track_info(item))

    LIBRARY_CACHE["data"] = {"albums": albums, "tracks": tracks}
    LIBRARY_CACHE["last_updated"] = now
    result = LIBRARY_CACHE["data"]
    _start_lyrics_prefetch(result)
    return result

def extract_cover(filepath: Path) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime_type) from audio tags or None."""
    path_str = str(filepath)
    if path_str in COVER_CACHE:
        return COVER_CACHE[path_str]

    try:
        audio = MutagenFile(filepath)
        if not audio:
            return None
        
        data, mime = None, "image/jpeg"
        if hasattr(audio, "pictures") and audio.pictures:
            p = audio.pictures[0]
            data, mime = p.data, p.mime or "image/jpeg"
        elif audio.tags:
            for k in audio.tags.keys():
                if k.startswith("APIC"):
                    data = audio.tags[k].data
                    break
                if k == "covr":
                    imgs = audio.tags[k]
                    if imgs:
                        data = bytes(imgs[0])
                        break
        
        if data:
            COVER_CACHE[path_str] = (data, mime)
            return data, mime
    except Exception:
        pass
    return None

# ── DDoS / IDS Protection ────────────────────────────────────────────────────
#
# All limits are tunable via environment variables — defaults are conservative
# enough for a single-user personal server.  Whitelisted IPs (127.0.0.1 by
# default) skip every check so local playback is never throttled.
#
# Environment knobs:
#   BALUFY_ALLOW_IPS   comma-separated IPs that bypass all checks  (default: 127.0.0.1)
#   BALUFY_DENY_IPS    comma-separated IPs that are always blocked  (default: "")
#   BALUFY_RL_GLOBAL   max requests/min per IP across all endpoints (default: 120)
#   BALUFY_RL_API      max requests/min per IP on /api/* routes     (default:  60)
#   BALUFY_RL_STREAM   max requests/min per IP on /api/stream/*     (default:  20)
#   BALUFY_RL_LOGIN    max POST attempts/min per IP on /login        (default:   5)
#   BALUFY_BAN_THRESH  violations before a temp-ban is applied       (default:  10)
#   BALUFY_BAN_DURATION base ban duration in seconds                 (default: 600)
#   BALUFY_IDS_404     404 responses/min before flagging an IP       (default:  20)
#   BALUFY_IDS_ERR     4xx responses/min before flagging an IP       (default:  30)
#   BALUFY_IDS_LOG     path for the IDS/ban event log file           (default: ids.log)

RL_GLOBAL_LIMIT   = int(os.environ.get("BALUFY_RL_GLOBAL",   "70"))
RL_API_LIMIT      = int(os.environ.get("BALUFY_RL_API",       "70"))
RL_STREAM_LIMIT   = int(os.environ.get("BALUFY_RL_STREAM",    "40"))
RL_LOGIN_LIMIT    = int(os.environ.get("BALUFY_RL_LOGIN",      "5"))
BAN_THRESHOLD     = int(os.environ.get("BALUFY_BAN_THRESH",   "10"))
BAN_DURATION      = int(os.environ.get("BALUFY_BAN_DURATION", "120"))
IDS_404_THRESHOLD = int(os.environ.get("BALUFY_IDS_404",      "20"))
IDS_ERROR_THRESH  = int(os.environ.get("BALUFY_IDS_ERR",      "30"))

STATIC_IPS_ALLOW = set(filter(
    None, os.environ.get("BALUFY_ALLOW_IPS", "127.0.0.1").split(",")
))
STATIC_IPS_DENY  = set(filter(
    None, os.environ.get("BALUFY_DENY_IPS",  "").split(",")
))

# Per-bucket sliding-window deques: { ip → deque[monotonic_timestamp] }
_rl_global : dict[str, collections.deque] = collections.defaultdict(collections.deque)
_rl_api    : dict[str, collections.deque] = collections.defaultdict(collections.deque)
_rl_login  : dict[str, collections.deque] = collections.defaultdict(collections.deque)
_rl_stream : dict[str, collections.deque] = collections.defaultdict(collections.deque)

# IDS anomaly counters: { ip → deque[monotonic_timestamp] }
_ids_404   : dict[str, collections.deque] = collections.defaultdict(collections.deque)
_ids_errs  : dict[str, collections.deque] = collections.defaultdict(collections.deque)

# Ban table: { ip → (unban_unix_timestamp, cumulative_violation_count) }
_ban_table : dict[str, tuple[float, int]] = {}

_rl_lock  = threading.Lock()
_ids_lock = threading.Lock()
_ban_lock = threading.Lock()

# ── IDS logger ────────────────────────────────────────────────────────────────

_ids_logger = logging.getLogger("balufy.ids")
_ids_logger.setLevel(logging.WARNING)
_ids_logger.propagate = False
_ids_log_path = Path(os.environ.get("BALUFY_IDS_LOG", "ids.log"))
_ids_fh = logging.FileHandler(_ids_log_path, encoding="utf-8")
_ids_fh.setFormatter(logging.Formatter("%(message)s"))
_ids_logger.addHandler(_ids_fh)

def _ids_log(ip: str, reason: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _ids_logger.warning("%s | %-15s | %s", ts, ip, reason)

# ── Sliding-window helpers ────────────────────────────────────────────────────

def _sw_add(dq: collections.deque, window: float = 60.0) -> int:
    """Append current monotonic time, prune stale entries, return new count."""
    now    = time.monotonic()
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    dq.append(now)
    return len(dq)

# ── Ban helpers ───────────────────────────────────────────────────────────────

def _is_banned(ip: str) -> bool:
    """Return True if ip is currently under an active ban."""
    with _ban_lock:
        entry = _ban_table.get(ip)
        if entry is None:
            return False
        unban_at, count = entry
        if time.time() < unban_at:
            return True
        # Ban expired — preserve violation count for escalation, clear active ban
        _ban_table[ip] = (0.0, count)
        return False

def _record_violation(ip: str, reason: str) -> None:
    """
    Log a violation.  Once cumulative violations reach BAN_THRESHOLD, apply an
    escalating exponential back-off ban (doubles each successive ban, capped at 24 h).
    """
    _ids_log(ip, f"VIOLATION {reason}")
    with _ban_lock:
        unban_at, count = _ban_table.get(ip, (0.0, 0))
        count += 1
        if count >= BAN_THRESHOLD:
            tier       = (count - BAN_THRESHOLD) // 2
            duration   = min(BAN_DURATION * (2 ** tier), 86400)
            unban_at   = time.time() + duration
            _ids_log(ip, f"BANNED {duration}s  (total_violations={count})")
        _ban_table[ip] = (unban_at, count)

# ── Intrusion-detection heuristics ───────────────────────────────────────────

# Known scanning / attack tool fingerprints (case-insensitive substring match)
_SCANNER_UA_FRAGMENTS = frozenset((
    "nikto", "sqlmap", "nmap", "masscan", "zgrab", "zmap",
    "python-requests", "go-http-client", "libwww-perl",
    "dirbuster", "gobuster", "wfuzz", "ffuf", "feroxbuster",
    "nuclei", "hydra", "medusa", "burpsuite", "burp suite",
    "havij", "acunetix", "nessus", "openvas", "appscan",
    "w3af", "skipfish", "arachni", "vega", "webscarab",
))

# Path patterns that are never legitimate for this app
_TRAVERSAL_INDICATORS = (
    "../", "..\\", "%2e%2e", "%2E%2E",
    "/..", "\\..", "/etc/passwd", "/etc/shadow",
    "/proc/self", "\\windows\\", "/win.ini",
)

def _ua_is_scanner(ua: str) -> bool:
    low = ua.lower()
    return any(tok in low for tok in _SCANNER_UA_FRAGMENTS)

def _path_is_traversal(raw_path: str) -> bool:
    decoded = urllib.parse.unquote_plus(raw_path)
    return any(p in decoded for p in _TRAVERSAL_INDICATORS)

# ── before_request guard (runs BEFORE csrf_protect) ──────────────────────────

@app.before_request
def ddos_ids_guard():
    """
    Gate every inbound request through four layers:
      1. Static deny / allow lists
      2. Active ban table check
      3. IDS heuristics (path traversal, scanner UA, missing UA)
      4. Per-endpoint sliding-window rate limiting
    """
    ip   = request.remote_addr or "unknown"
    path = request.path

    # Layer 1 — static lists (fastest exit)
    if ip in STATIC_IPS_DENY:
        _ids_log(ip, f"STATIC_DENY {path}")
        abort(403)
    if ip in STATIC_IPS_ALLOW:
        return  # whitelisted — skip everything

    # Layer 2 — active ban
    if _is_banned(ip):
        abort(429)

    ua = request.headers.get("User-Agent", "")

    # Layer 3a — path traversal
    if _path_is_traversal(request.full_path):
        _record_violation(ip, f"PATH_TRAVERSAL {request.full_path[:120]}")
        abort(400)

    # Layer 3b — scanner/tool user-agent
    if _ua_is_scanner(ua):
        _record_violation(ip, f"SCANNER_UA {ua[:100]}")
        if _is_banned(ip):
            abort(403)

    # Layer 3c — completely missing UA on non-static endpoints
    if not ua and path not in ("/favicon.ico",) and not path.startswith("/static/"):
        _record_violation(ip, f"NO_UA {path}")

    # Layer 4 — rate limiting
    with _rl_lock:
        # Login brute-force (tightest limit)
        if path == "/login" and request.method == "POST":
            if _sw_add(_rl_login[ip]) > RL_LOGIN_LIMIT:
                _record_violation(ip, f"RL_LOGIN_EXCEEDED")
                abort(429)

        # Streaming (per-connection churn guard)
        if path.startswith("/api/stream/"):
            if _sw_add(_rl_stream[ip]) > RL_STREAM_LIMIT:
                _record_violation(ip, f"RL_STREAM_EXCEEDED")
                abort(429)

        # Generic API
        if path.startswith("/api/"):
            if _sw_add(_rl_api[ip]) > RL_API_LIMIT:
                _record_violation(ip, f"RL_API_EXCEEDED")
                abort(429)

        # Global catch-all (DDoS / flood guard)
        if _sw_add(_rl_global[ip]) > RL_GLOBAL_LIMIT:
            _record_violation(ip, f"RL_GLOBAL_EXCEEDED")
            abort(429)


# ── after_request anomaly monitor ────────────────────────────────────────────

@app.after_request
def ids_response_monitor(response):
    """
    Track per-IP 404 and 4xx rates after each response.
    Spikes indicate directory bruteforcing or fuzzing.
    """
    ip = request.remote_addr or "unknown"
    if ip in STATIC_IPS_ALLOW or response.status_code == 206:
        return response

    code = response.status_code
    with _ids_lock:
        if code == 404:
            if _sw_add(_ids_404[ip]) >= IDS_404_THRESHOLD:
                _record_violation(ip, f"IDS_404_FLOOD")
        elif 400 <= code < 500 and code != 429:
            if _sw_add(_ids_errs[ip]) >= IDS_ERROR_THRESH:
                _record_violation(ip, f"IDS_ERROR_FLOOD (status={code})")

    return response


# ── 429 error handler with Retry-After ───────────────────────────────────────

@app.errorhandler(429)
def handle_too_many_requests(e):
    resp = jsonify({"error": "Too many requests — slow down."})
    resp.status_code = 429
    resp.headers["Retry-After"] = "60"
    return resp


# ── Auth decorator (defined early — used by admin endpoints below) ────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Admin: ban management endpoints ──────────────────────────────────────────

@app.route("/api/admin/bans")
@login_required
def api_admin_bans():
    """
    GET /api/admin/bans
    Returns the current ban table: active bans, violation counts, and
    per-IP rate-limit snapshot.  Useful for monitoring from the UI.
    """
    now = time.time()
    with _ban_lock:
        snapshot = {
            ip: {
                "banned":           unban_at > now,
                "unban_in_seconds": max(0, int(unban_at - now)),
                "violations":       count,
            }
            for ip, (unban_at, count) in _ban_table.items()
        }
    return jsonify({
        "bans":            snapshot,
        "total_tracked":   len(snapshot),
        "active_bans":     sum(1 for v in snapshot.values() if v["banned"]),
        "ids_log":         str(_ids_log_path.resolve()),
    })


@app.route("/api/admin/bans/<path:ip_addr>", methods=["DELETE"])
@login_required
def api_admin_unban(ip_addr: str):
    """
    DELETE /api/admin/bans/<ip>
    Manually lift a ban and reset violation count for an IP.
    """
    with _ban_lock:
        if ip_addr in _ban_table:
            del _ban_table[ip_addr]
            _ids_log(ip_addr, "MANUALLY_UNBANNED")
            return jsonify({"ok": True, "ip": ip_addr})
    return jsonify({"error": "not found"}), 404


# ── Periodic memory cleanup (runs every 10 min) ───────────────────────────────

def _protection_cleanup() -> None:
    """
    Daemon thread: prune stale sliding-window entries and expired ban records
    to prevent unbounded memory growth over long uptimes.
    """
    while True:
        time.sleep(600)
        cutoff = time.monotonic() - 120   # keep 2-min history max
        now    = time.time()

        with _rl_lock:
            for dct in (_rl_global, _rl_api, _rl_login, _rl_stream):
                for dq in dct.values():
                    while dq and dq[0] < cutoff:
                        dq.popleft()

        with _ids_lock:
            for dct in (_ids_404, _ids_errs):
                for dq in dct.values():
                    while dq and dq[0] < cutoff:
                        dq.popleft()

        with _ban_lock:
            # Remove entries where ban expired AND violations are below threshold
            stale = [
                ip for ip, (unban_at, count) in _ban_table.items()
                if unban_at < now and count < BAN_THRESHOLD
            ]
            for ip in stale:
                del _ban_table[ip]

threading.Thread(target=_protection_cleanup, daemon=True, name="protection-gc").start()


# ── CSRF Protection ───────────────────────────────────────────────────────────

def generate_csrf_token() -> str:
    """Generate and store a per-session CSRF token (idempotent)."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]

app.jinja_env.globals["csrf_token"] = generate_csrf_token

CSRF_EXEMPT_PATHS = {"/login", "/favicon.ico"}

@app.before_request
def csrf_protect():
    """
    Reject state-changing requests that lack a valid CSRF token.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if request.path in CSRF_EXEMPT_PATHS:
        return
    expected = session.get("_csrf_token", "")
    token = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("_csrf_token", "")
    )
    if not expected or not secrets.compare_digest(expected, token):
        abort(403)

# ── Request hooks ────────────────────────────────────────────────────────────

@app.after_request
def log_after(response):
    if response.status_code not in (206, 301, 302, 304):
        log_visit(f"status={response.status_code}")
    return response

@app.after_request
def add_security_headers(response):
    """
    Apply OWASP-recommended security headers to every response.
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "upgrade-insecure-requests;"
    )
    response.headers["Server"] = "nginx"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), "
        "payment=(), usb=(), interest-cohort=()"
    )
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers.remove("Server") if "Server" in response.headers else None
    response.headers["Server"] = "nginx"
    response.headers.pop("X-Powered-By", None)
    response.headers["X-Powered-By"] = "Express"

    return response



# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    ico = Path(__file__).parent / "favicon.ico"
    if not ico.is_file():
        abort(404)
    return send_file(ico, mimetype="image/x-icon", max_age=86400)

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    generate_csrf_token()
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if hashlib.sha256(pwd.encode()).hexdigest() == PWD_HASH:
            session["authenticated"] = True
            session.permanent = False
            log_visit("LOGIN=success")
            return redirect(url_for("index"))
        log_visit("LOGIN=failed")
        error = "Incorrect password. Please try again."
    return render_template("login.html", error=error)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    resp.headers["Clear-Site-Data"] = '"cookies", "storage", "cache"'
    return resp

@app.route("/sw.js")
def service_worker():
    """
    Service Worker: caches cover art (cache-first, 7-day TTL) and
    static font assets (cache-first, permanent).  Library JSON is served
    stale-while-revalidate so the page never blocks on a slow network.
    """
    sw_js = r"""
const CACHE_NAME  = 'balufy-v1';
const COVER_RE    = /\/api\/cover\//;
const STATIC_RE   = /\/static\//;
const LIBRARY_URL = '/api/library';
const COVER_TTL   = 7 * 24 * 3600 * 1000;  // 7 days in ms

self.addEventListener('install',  () => self.skipWaiting());
self.addEventListener('activate', e  => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    const { request } = event;
    const url = request.url;

    // Cover art – cache-first, TTL-checked
    if (COVER_RE.test(url)) {
        event.respondWith(cacheFirstWithTTL(request, COVER_TTL));
        return;
    }
    // Static fonts / CSS – cache-first forever (filenames are stable)
    if (STATIC_RE.test(url)) {
        event.respondWith(cacheFirst(request));
        return;
    }
    // Library JSON – network-first with cached fallback (stale-while-revalidate)
    if (url.includes(LIBRARY_URL)) {
        event.respondWith(staleWhileRevalidate(request));
        return;
    }
});

async function cacheFirst(request) {
    const cached = await caches.match(request);
    if (cached) return cached;
    const resp = await fetch(request);
    if (resp.ok) {
        const cache = await caches.open(CACHE_NAME);
        cache.put(request, resp.clone());
    }
    return resp;
}

async function cacheFirstWithTTL(request, ttlMs) {
    const cache  = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) {
        const age = Date.now() - new Date(cached.headers.get('date') || 0).getTime();
        if (age < ttlMs) return cached;
    }
    try {
        const resp = await fetch(request);
        if (resp.ok) cache.put(request, resp.clone());
        return resp;
    } catch (_) {
        return cached || new Response('', { status: 503 });
    }
}

async function staleWhileRevalidate(request) {
    const cache  = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    const fetchP = fetch(request).then(resp => {
        if (resp.ok) cache.put(request, resp.clone());
        return resp;
    }).catch(() => null);
    return cached || await fetchP || new Response('{}');
}
""".strip()
    return Response(sw_js, 200, headers={
        "Content-Type":  "application/javascript; charset=utf-8",
        "Cache-Control": "no-cache",
    })


@app.route("/")
@login_required
def index():
    return render_template("index.html", ffmpeg_available=bool(FFMPEG_BIN))

@app.route("/api/library")
@login_required
def api_library():
    """Returns the library structure in JSON format, with ETag for conditional GETs."""
    data = scan_library_cached()
    etag = f'"{hashlib.md5(str(LIBRARY_CACHE["last_updated"]).encode()).hexdigest()[:16]}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)
    resp = jsonify(data)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=60, stale-while-revalidate=300"
    return resp

@app.route("/api/refresh")
@login_required
def api_refresh():
    """Manually forces a library re-scan."""
    return jsonify(scan_library_cached(force=True))

@app.route("/api/cover/<path:filepath>")
@login_required
def api_cover(filepath):
    """Serves the cover image extracted from a track's metadata."""
    safe = secure_path(filepath)
    if not safe or not safe.is_file():
        abort(404)
    # ETag = hex of file mtime so the browser can cache aggressively and revalidate cheaply
    mtime   = int(safe.stat().st_mtime)
    etag    = f'"{hashlib.md5(f"{filepath}:{mtime}".encode()).hexdigest()[:16]}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)
    result = extract_cover(safe)
    if not result:
        abort(404)
    data, mime = result
    return Response(
        data, 200,
        headers={
            "Content-Type":   mime,
            "ETag":           etag,
            "Cache-Control":  "private, max-age=604800, stale-while-revalidate=86400",
            "Content-Length": str(len(data)),
        }
    )

STREAM_CHUNK_FIRST = 16 * 1024
STREAM_CHUNK       = 128 * 1024

@app.route("/api/stream/<path:filepath>")
@login_required
def api_stream(filepath):
    """
    Range-aware streaming with a fast-start first chunk.
    """
    safe = secure_path(filepath)
    if not safe or not safe.is_file():
        abort(404)

    quality = request.args.get("quality", "original")
    seek_s  = max(0, int(request.args.get("seek", 0) or 0))

    if quality != "original" and FFMPEG_BIN and quality in ("320", "192", "128"):
        cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error"]
        if seek_s > 0:
            cmd += ["-ss", str(seek_s)]
        cmd += [
            "-i", str(safe),
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", f"{quality}k",
            "-f", "mp3",
            "pipe:1",
        ]

        def _ffmpeg_gen():
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            try:
                while True:
                    chunk = proc.stdout.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass

        return Response(
            _ffmpeg_gen(), 200,
            headers={
                "Content-Type":  "audio/mpeg",
                "Accept-Ranges": "none",
                "Cache-Control": "no-cache",
                "X-Transcoded":  quality,
            },
        )

    mime      = MIME_MAP.get(safe.suffix.lower(), "audio/mpeg")
    file_size = safe.stat().st_size
    base_headers = {
        "Content-Type":  mime,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }

    range_header = request.headers.get("Range")
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not m:
            abort(416)
        start  = int(m.group(1))
        end    = int(m.group(2)) if m.group(2) else file_size - 1
        end    = min(end, file_size - 1)
        if start > end or start >= file_size:
            abort(416)
        length = end - start + 1

        def _range_gen():
            with open(safe, "rb") as fh:
                fh.seek(start)
                remaining = length
                first_chunk = True
                while remaining > 0:
                    size  = STREAM_CHUNK_FIRST if first_chunk else STREAM_CHUNK
                    chunk = fh.read(min(size, remaining))
                    if not chunk:
                        break
                    remaining  -= len(chunk)
                    first_chunk = False
                    yield chunk

        return Response(
            _range_gen(), 206,
            headers={
                **base_headers,
                "Content-Range":  f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
            }
        )

    def _full_gen():
        with open(safe, "rb") as fh:
            first_chunk = True
            while True:
                size  = STREAM_CHUNK_FIRST if first_chunk else STREAM_CHUNK
                chunk = fh.read(size)
                if not chunk:
                    break
                first_chunk = False
                yield chunk

    return Response(
        _full_gen(), 200,
        headers={**base_headers, "Content-Length": str(file_size)}
    )


@app.route("/api/lyrics/cache-status")
@login_required
def api_lyrics_cache_status():
    """Returns prefetch progress: {total, cached, running}."""
    with _prefetch_lock:
        return jsonify(dict(_prefetch_state))


# ── Liked songs ───────────────────────────────────────────────────────────────

@app.route("/api/liked", methods=["GET"])
@login_required
def api_liked_get():
    """Return all liked track paths as a JSON list."""
    with _lyrics_db() as db:
        rows = db.execute(
            "SELECT path FROM liked_songs ORDER BY liked_at DESC"
        ).fetchall()
    return jsonify({"paths": [r[0] for r in rows]})


@app.route("/api/liked/<path:filepath>", methods=["POST", "DELETE"])
@login_required
def api_liked_toggle(filepath):
    """Like (POST) or unlike (DELETE) a track."""
    safe = secure_path(filepath)
    if not safe or not safe.is_file():
        abort(404)

    rel = str(safe.relative_to(AUDIO_DIR.resolve()))

    with _lyrics_db() as db:
        if request.method == "POST":
            db.execute(
                "INSERT OR REPLACE INTO liked_songs (path, liked_at) VALUES (?, ?)",
                (rel, int(time.time()))
            )
            db.commit()
            return jsonify({"liked": True, "path": rel})
        else:
            db.execute("DELETE FROM liked_songs WHERE path = ?", (rel,))
            db.commit()
            return jsonify({"liked": False, "path": rel})

@app.route("/api/lyrics")
@login_required
def api_lyrics():
    """
    Lyrics proxy with SQLite cache.
    """
    track_name  = request.args.get("track_name",  "").strip()
    artist_name = request.args.get("artist_name", "").strip()
    if not track_name or not artist_name:
        return jsonify({"error": "missing params"}), 400

    key = _lyrics_key(track_name, artist_name)
    now = int(time.time())

    with _lyrics_db() as db:
        row = db.execute(
            "SELECT synced_lyrics, plain_lyrics, not_found, cached_at "
            "FROM lyrics WHERE key = ?",
            (key,)
        ).fetchone()

        if row:
            synced, plain, not_found, cached_at = row
            ttl = LYRICS_NF_TTL if not_found else LYRICS_CACHE_TTL
            if now - cached_at < ttl:
                if not_found:
                    return jsonify({"error": "not_found"}), 404
                return jsonify({"syncedLyrics": synced, "plainLyrics": plain})

        params = urllib.parse.urlencode({
            "track_name":  track_name,
            "artist_name": artist_name,
        })
        url = f"https://lrclib.net/api/get?{params}"
        synced = plain = None
        not_found_flag = 0

        try:
            req = urllib.request.Request(
                url, headers={"Lrclib-Client": "Balufy Pro"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json_mod.loads(resp.read().decode())
            synced         = data.get("syncedLyrics") or None
            plain          = data.get("plainLyrics")  or None
            not_found_flag = 0 if (synced or plain) else 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                not_found_flag = 1
            else:
                return jsonify({"error": "api_error"}), 502
        except Exception:
            return jsonify({"error": "api_error"}), 502

        db.execute("""
            INSERT INTO lyrics
                (key, track_name, artist_name, synced_lyrics, plain_lyrics,
                 not_found, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                synced_lyrics = excluded.synced_lyrics,
                plain_lyrics  = excluded.plain_lyrics,
                not_found     = excluded.not_found,
                cached_at     = excluded.cached_at
        """, (key, track_name, artist_name, synced, plain, not_found_flag, now))
        db.commit()

    if not_found_flag:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"syncedLyrics": synced, "plainLyrics": plain})


# ── Listen history ────────────────────────────────────────────────────────────

@app.route("/api/listen-event", methods=["POST"])
@login_required
def api_listen_event():
    """
    Record a completed (or partial) track play for habit tracking.
    """
    data      = request.get_json(silent=True) or {}
    path      = str(data.get("path", "")).strip()
    play_secs = max(0, int(data.get("play_secs", 0) or 0))

    if not path:
        return jsonify({"error": "missing path"}), 400

    safe = secure_path(path)
    if not safe or not safe.is_file():
        return jsonify({"error": "not found"}), 404

    rel = str(safe.relative_to(AUDIO_DIR.resolve()))

    with _lyrics_db() as db:
        db.execute(
            "INSERT INTO listen_history (path, played_at, play_secs) VALUES (?, ?, ?)",
            (rel, int(time.time()), play_secs),
        )
        db.commit()

    return jsonify({"ok": True})


# ── Today's Hits ──────────────────────────────────────────────────────────────

@app.route("/api/today-hits")
@login_required
def api_today_hits():
    """
    Generate a personalised ~30-track playlist from listening habits.
    """
    import random as _random
    rng = _random.Random()

    lib       = scan_library_cached()
    all_tracks: list[dict] = list(lib.get("tracks", []))
    for alb in lib.get("albums", []):
        all_tracks.extend(alb.get("tracks", []))

    if not all_tracks:
        return jsonify({"tracks": [], "generated_at": int(time.time())})

    now       = int(time.time())
    week_ago  = now - 7  * 24 * 3600
    month_ago = now - 30 * 24 * 3600

    with _lyrics_db() as db:
        rows = db.execute(
            "SELECT path, played_at, play_secs "
            "FROM listen_history WHERE played_at > ?",
            (month_ago,),
        ).fetchall()

    duration_map = {t["path"]: max(1, t.get("duration", 180)) for t in all_tracks}

    raw_scores: dict[str, float] = {}
    for path, played_at, play_secs in rows:
        dur  = duration_map.get(path, 180)
        base = 3.0 if played_at >= week_ago else 1.0
        if   play_secs >= dur * 0.75: base += 2.0
        elif play_secs >= 60:          base += 0.5
        raw_scores[path] = raw_scores.get(path, 0.0) + base

    played_paths = set(raw_scores)
    track_map    = {t["path"]: t for t in all_tracks}

    scored: list[tuple[dict, float]] = []
    for path, score in raw_scores.items():
        if path in track_map:
            jitter = rng.uniform(0.70, 1.30)
            scored.append((track_map[path], score * jitter))

    scored.sort(key=lambda x: x[1], reverse=True)

    never_played = [t for t in all_tracks if t["path"] not in played_paths]
    rng.shuffle(never_played)

    target      = 30
    known_quota = max(0, target - target // 4)
    disc_quota  = target - known_quota

    playlist: list[dict] = [t for t, _ in scored[:known_quota]]
    playlist += never_played[:disc_quota]

    if len(playlist) < 10:
        extras = [t for t in all_tracks if t not in playlist]
        rng.shuffle(extras)
        playlist += extras[: target - len(playlist)]

    if len(playlist) > 3:
        top3 = playlist[:3]
        rest = playlist[3:]
        rng.shuffle(rest)
        playlist = top3 + rest

    return jsonify({"tracks": playlist[:target], "generated_at": now})


# ── Prediction endpoint ───────────────────────────────────────────────────────

@app.route("/api/predict", methods=["POST"])
@login_required
def api_predict():
    """
    Receive client-side events and current context.
    Returns a list of tracks the user is most likely to play next,
    so the browser can pre-fetch them.
    """
    data = request.get_json(silent=True) or {}

    events = data.get("events", [])
    client_id = data.get("client_id", "unknown")
    db_habits = _habits_db()
    now = int(time.time())

    # 1. Process incoming events
    for ev in events:
        etype = ev.get("type", "")
        payload = json_mod.dumps(ev.get("data", {}))
        db_habits.execute(
            "INSERT INTO user_events (client_id, event_type, payload, ts) VALUES (?,?,?,?)",
            (client_id, etype, payload, ev.get("ts", now))
        )

        # Update transition counts when a 'play' event arrives
        if etype == "play" and "previous_track" in ev.get("data", {}):
            prev = ev["data"]["previous_track"]
            curr = ev["data"].get("track", "")
            if prev and curr:
                db_habits.execute("""
                    INSERT INTO transition_counts (from_path, to_path, count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(from_path, to_path) DO UPDATE SET count = count + 1
                """, (prev, curr))

        # Update global plays
        if etype == "play":
            curr = ev["data"].get("track", "")
            if curr:
                db_habits.execute("""
                    INSERT INTO global_plays (path, count)
                    VALUES (?, 1)
                    ON CONFLICT(path) DO UPDATE SET count = count + 1
                """, (curr,))

    db_habits.commit()

    # 2. Build predictions
    predictions = []

    current_track = data.get("current_track")
    if current_track:
        rows = db_habits.execute("""
            SELECT to_path, count FROM transition_counts
            WHERE from_path = ? ORDER BY count DESC LIMIT ?
        """, (current_track, PREDICT_CACHE_SIZE)).fetchall()
        for path, cnt in rows:
            predictions.append({"path": path, "score": cnt})

    # Fill up with globally popular tracks
    needed = PREDICT_CACHE_SIZE - len(predictions)
    if needed > 0:
        existing = {p["path"] for p in predictions}
        rows = db_habits.execute("""
            SELECT path, count FROM global_plays
            ORDER BY count DESC LIMIT ?
        """, (needed + len(predictions),)).fetchall()
        for path, cnt in rows:
            if path not in existing:
                predictions.append({"path": path, "score": cnt})
                if len(predictions) >= PREDICT_CACHE_SIZE:
                    break

    db_habits.close()
    return jsonify({"predictions": predictions[:PREDICT_CACHE_SIZE]})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AUDIO_DIR.mkdir(exist_ok=True)
    
    _lyrics_db().close()
    _habits_db().close()
    print("=" * 52)
    print("  Balufy Pro — Personal Music Server")
    print("  Warming library cache…")
    scan_library_cached(force=True)
    print(f"  Audio dir : {AUDIO_DIR.resolve()}")
    print(f"  Lyrics DB : {LYRICS_DB.resolve()}")
    print(f"  Habits DB : {HABITS_DB.resolve()}")
    print(f"  IDS log   : {_ids_log_path.resolve()}")
    print(f"  FFmpeg    : {FFMPEG_BIN or 'not found (quality selector disabled)'}")
    print(f"  Rate limits: global={RL_GLOBAL_LIMIT}/min  api={RL_API_LIMIT}/min  "
          f"stream={RL_STREAM_LIMIT}/min  login={RL_LOGIN_LIMIT}/min")
    print(f"  Ban policy: threshold={BAN_THRESHOLD} violations  base={BAN_DURATION}s (exponential)")
    print("=" * 52)
    
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
