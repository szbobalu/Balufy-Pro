"""Balufy Pro — Personal Music Streaming Server
Run: python serve.py  |  Default password: password (override with BALUFY_PASSWORD)
"""

import collections, hashlib, json as json_mod, logging, os, re, secrets
import shutil, sqlite3, subprocess, threading, time, urllib.error
import urllib.parse, urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import Flask, render_template, session, redirect, url_for, request, jsonify, Response, abort, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from mutagen.flac import FLAC
    from mutagen import File as MutagenFile
except ImportError:
    raise SystemExit("Missing dependency — run: pip install mutagen flask")


# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("BALUFY_SECRET", secrets.token_hex(32))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")

# ── Core config ───────────────────────────────────────────────────────────────

AUDIO_DIR  = Path(os.environ.get("BALUFY_AUDIO_DIR", "/mnt/data/audio/"))
PASSWORD   = os.environ.get("BALUFY_PASSWORD", "1234")
PWD_HASH   = hashlib.sha256(PASSWORD.encode()).hexdigest()
SUPPORTED  = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aac", ".opus"}
MIME_MAP   = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
              ".ogg": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac", ".opus": "audio/opus"}
FFMPEG_BIN = shutil.which("ffmpeg")

LIBRARY_CACHE = {"data": None, "last_updated": 0}
COVER_CACHE: dict = {}

# ── DB paths & helpers ───────────────────────────────────────────────────────

LYRICS_DB        = Path(os.environ.get("BALUFY_LYRICS_DB", "lyrics.db"))
HABITS_DB        = Path(os.environ.get("BALUFY_HABITS_DB", "habits.db"))
LYRICS_CACHE_TTL = int(os.environ.get("BALUFY_LYRICS_TTL", 30 * 24 * 3600))
LYRICS_NF_TTL    = 7 * 24 * 3600
PREDICT_CACHE_SIZE = int(os.environ.get("BALUFY_PREDICT_SIZE", "8"))

@contextmanager
def open_db(path: Path):
    db = sqlite3.connect(path)
    try:
        yield db
    finally:
        db.close()

def lyrics_db() -> sqlite3.Connection:
    db = sqlite3.connect(LYRICS_DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS lyrics (
            key TEXT PRIMARY KEY, track_name TEXT NOT NULL, artist_name TEXT NOT NULL,
            synced_lyrics TEXT, plain_lyrics TEXT,
            not_found INTEGER NOT NULL DEFAULT 0, cached_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lyrics_cached_at ON lyrics(cached_at);
        CREATE TABLE IF NOT EXISTS liked_songs (path TEXT PRIMARY KEY, liked_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS listen_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
            played_at INTEGER NOT NULL, play_secs INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_lh_path ON listen_history(path);
        CREATE INDEX IF NOT EXISTS idx_lh_ts   ON listen_history(played_at);
    """)
    db.commit()
    return db

def habits_db() -> sqlite3.Connection:
    db = sqlite3.connect(HABITS_DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
            event_type TEXT NOT NULL, payload TEXT NOT NULL, ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ue_ts     ON user_events(ts);
        CREATE INDEX IF NOT EXISTS idx_ue_client ON user_events(client_id);
        CREATE TABLE IF NOT EXISTS transition_counts (
            from_path TEXT NOT NULL, to_path TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (from_path, to_path)
        );
        CREATE TABLE IF NOT EXISTS global_plays (path TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 1);
    """)
    db.commit()
    return db

def lyrics_key(track: str, artist: str) -> str:
    return f"{track.lower().strip()}|||{artist.lower().strip()}"

# ── Logging ──────────────────────────────────────────────────────────────────

def _make_logger(name: str, path: Path, level=logging.INFO) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(level)
    lg.propagate = False
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(fh)
    return lg

visit_logger = _make_logger("balufy.visits", Path(os.environ.get("BALUFY_LOG", "visitors.log")))
ids_logger   = _make_logger("balufy.ids",    Path(os.environ.get("BALUFY_IDS_LOG", "ids.log")), logging.WARNING)
ids_log_path = Path(os.environ.get("BALUFY_IDS_LOG", "ids.log"))

def log_visit(note: str = ""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    visit_logger.info("%s | %s | %s %s | %s%s",
        ts, request.remote_addr or "unknown",
        request.method, request.full_path.rstrip("?"),
        request.headers.get("User-Agent", "-"),
        f" | {note}" if note else "")

def ids_log(ip: str, reason: str):
    ids_logger.warning("%s | %-15s | %s",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), ip, reason)

# ── Rate limiting & IDS ───────────────────────────────────────────────────────

RL_GLOBAL  = int(os.environ.get("BALUFY_RL_GLOBAL",  "70"))
RL_API     = int(os.environ.get("BALUFY_RL_API",     "70"))
RL_STREAM  = int(os.environ.get("BALUFY_RL_STREAM",  "40"))
RL_LOGIN   = int(os.environ.get("BALUFY_RL_LOGIN",    "5"))
BAN_THRESH = int(os.environ.get("BALUFY_BAN_THRESH", "10"))
BAN_DUR    = int(os.environ.get("BALUFY_BAN_DURATION","120"))
IDS_404    = int(os.environ.get("BALUFY_IDS_404",    "20"))
IDS_ERR    = int(os.environ.get("BALUFY_IDS_ERR",    "30"))

ALLOW_IPS = set(filter(None, os.environ.get("BALUFY_ALLOW_IPS", "127.0.0.1").split(",")))
DENY_IPS  = set(filter(None, os.environ.get("BALUFY_DENY_IPS",  "").split(",")))

_DD = lambda: collections.defaultdict(collections.deque)
rl_global, rl_api, rl_login, rl_stream = _DD(), _DD(), _DD(), _DD()
ids_404_dq, ids_errs_dq = _DD(), _DD()
ban_table: dict[str, tuple[float, int]] = {}

rl_lock = threading.Lock()
ids_lock = threading.Lock()
ban_lock = threading.Lock()

SCANNER_UAS = frozenset(("nikto","sqlmap","nmap","masscan","zgrab","zmap","python-requests",
    "go-http-client","libwww-perl","dirbuster","gobuster","wfuzz","ffuf","feroxbuster",
    "nuclei","hydra","medusa","burpsuite","burp suite","havij","acunetix","nessus",
    "openvas","appscan","w3af","skipfish","arachni","vega","webscarab"))
TRAVERSAL = ("../","..\\","%2e%2e","%2E%2E","/..","\\..","/etc/passwd",
             "/etc/shadow","/proc/self","\\windows\\","/win.ini")

def sw_add(dq: collections.deque, window: float = 60.0) -> int:
    now = time.monotonic()
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    dq.append(now)
    return len(dq)

def is_banned(ip: str) -> bool:
    with ban_lock:
        entry = ban_table.get(ip)
        if not entry: return False
        unban_at, count = entry
        if time.time() < unban_at: return True
        ban_table[ip] = (0.0, count)
        return False

def record_violation(ip: str, reason: str):
    ids_log(ip, f"VIOLATION {reason}")
    with ban_lock:
        unban_at, count = ban_table.get(ip, (0.0, 0))
        count += 1
        if count >= BAN_THRESH:
            dur = min(BAN_DUR * (2 ** ((count - BAN_THRESH) // 2)), 86400)
            unban_at = time.time() + dur
            ids_log(ip, f"BANNED {dur}s (total_violations={count})")
        ban_table[ip] = (unban_at, count)

@app.before_request
def ddos_ids_guard():
    ip, path = request.remote_addr or "unknown", request.path
    if ip in DENY_IPS:
        ids_log(ip, f"STATIC_DENY {path}"); abort(403)
    if ip in ALLOW_IPS: return
    if is_banned(ip): abort(429)

    ua = request.headers.get("User-Agent", "")
    decoded = urllib.parse.unquote_plus(request.full_path)
    if any(p in decoded for p in TRAVERSAL):
        record_violation(ip, f"PATH_TRAVERSAL {request.full_path[:120]}"); abort(400)
    if any(t in ua.lower() for t in SCANNER_UAS):
        record_violation(ip, f"SCANNER_UA {ua[:100]}")
        if is_banned(ip): abort(403)
    if not ua and path not in ("/favicon.ico",) and not path.startswith("/static/"):
        record_violation(ip, f"NO_UA {path}")

    with rl_lock:
        checks = [
            (path == "/login" and request.method == "POST", rl_login[ip], RL_LOGIN,  "RL_LOGIN_EXCEEDED"),
            (path.startswith("/api/stream/"),               rl_stream[ip], RL_STREAM, "RL_STREAM_EXCEEDED"),
            (path.startswith("/api/"),                      rl_api[ip],    RL_API,    "RL_API_EXCEEDED"),
            (True,                                          rl_global[ip], RL_GLOBAL, "RL_GLOBAL_EXCEEDED"),
        ]
        for cond, dq, limit, label in checks:
            if cond and sw_add(dq) > limit:
                record_violation(ip, label); abort(429)

@app.after_request
def after_request_hooks(response):
    code, ip = response.status_code, request.remote_addr or "unknown"

    # IDS monitoring
    if ip not in ALLOW_IPS and code != 206:
        with ids_lock:
            if code == 404 and sw_add(ids_404_dq[ip]) >= IDS_404:
                record_violation(ip, "IDS_404_FLOOD")
            elif 400 <= code < 500 and code != 429 and sw_add(ids_errs_dq[ip]) >= IDS_ERR:
                record_violation(ip, f"IDS_ERROR_FLOOD (status={code})")

    # Visit logging
    if code not in (206, 301, 302, 304):
        log_visit(f"status={code}")

    # Security headers
    response.headers.update({
        "X-Content-Type-Options":         "nosniff",
        "X-Frame-Options":                "DENY",
        "Strict-Transport-Security":      "max-age=31536000; includeSubDomains",
        "Content-Security-Policy":        (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; "
            "form-action 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; upgrade-insecure-requests;"
        ),
        "Permissions-Policy":             "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
        "Cross-Origin-Resource-Policy":   "same-origin",
        "Referrer-Policy":                "strict-origin-when-cross-origin",
        "Server":                         "nginx",
    })
    return response

@app.errorhandler(429)
def handle_429(e):
    r = jsonify({"error": "Too many requests — slow down."})
    r.status_code = 429
    r.headers["Retry-After"] = "60"
    return r

def _cleanup_worker():
    while True:
        time.sleep(600)
        cutoff, now = time.monotonic() - 120, time.time()
        with rl_lock:
            for dct in (rl_global, rl_api, rl_login, rl_stream):
                for dq in dct.values():
                    while dq and dq[0] < cutoff: dq.popleft()
        with ids_lock:
            for dct in (ids_404_dq, ids_errs_dq):
                for dq in dct.values():
                    while dq and dq[0] < cutoff: dq.popleft()
        with ban_lock:
            for ip in [ip for ip, (u, c) in ban_table.items() if u < now and c < BAN_THRESH]:
                del ban_table[ip]

threading.Thread(target=_cleanup_worker, daemon=True, name="protection-gc").start()

# ── CSRF ─────────────────────────────────────────────────────────────────────

def generate_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]

app.jinja_env.globals["csrf_token"] = generate_csrf_token

@app.before_request
def csrf_protect():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"): return
    if request.path in {"/login", "/favicon.ico"}: return
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not secrets.compare_digest(session.get("csrf_token", ""), token):
        abort(403)

# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ── Path / metadata helpers ───────────────────────────────────────────────────

def secure_path(rel: str) -> Optional[Path]:
    try:
        p = (AUDIO_DIR / rel).resolve()
        p.relative_to(AUDIO_DIR.resolve())
        return p
    except Exception:
        return None

def fmt_duration(s: int) -> str:
    m, s = divmod(int(s), 60)
    return f"{m}:{s:02d}"

def get_track_info(filepath: Path) -> dict:
    rel = str(filepath.relative_to(AUDIO_DIR))
    info = {"path": rel, "title": filepath.stem, "artist": "Unknown Artist",
            "album": "Unknown Album", "duration": 0, "duration_fmt": "0:00",
            "track_number": 0, "has_cover": False, "ext": filepath.suffix.lower(), "bitrate": 0}
    try:
        ext = filepath.suffix.lower()
        if ext == ".flac":
            audio = FLAC(filepath)
            def tag(k, d=""): v = audio.get(k, [d]); return str(v[0]).strip() if v else d
            info.update(title=tag("title") or filepath.stem, artist=tag("artist") or "Unknown Artist",
                        album=tag("album") or "Unknown Album")
            try: info["track_number"] = int(str(tag("tracknumber", "0")).split("/")[0])
            except ValueError: pass
            if audio.info:
                info.update(duration=int(audio.info.length), duration_fmt=fmt_duration(audio.info.length))
                try:
                    if audio.info.length > 0:
                        info["bitrate"] = int(filepath.stat().st_size * 8 / audio.info.length / 1000)
                except Exception: pass
            info["has_cover"] = bool(audio.pictures)
        else:
            easy = MutagenFile(filepath, easy=True)
            if easy and easy.tags:
                def etag(k, d=""): v = easy.tags.get(k, [d]); return str(v[0]).strip() if v else d
                info.update(title=etag("title") or filepath.stem, artist=etag("artist") or "Unknown Artist",
                            album=etag("album") or "Unknown Album")
            if easy and easy.info:
                info.update(duration=int(easy.info.length), duration_fmt=fmt_duration(easy.info.length))
                try:
                    if hasattr(easy.info, "bitrate") and easy.info.bitrate:
                        info["bitrate"] = easy.info.bitrate // 1000
                except Exception: pass
            full = MutagenFile(filepath)
            if full:
                if hasattr(full, "pictures"): info["has_cover"] = bool(full.pictures)
                elif full.tags: info["has_cover"] = any(k.startswith(("APIC", "covr")) for k in full.tags)
    except Exception: pass
    return info

def scan_library_cached(force=False) -> dict:
    now = time.time()
    if LIBRARY_CACHE["data"] and not force and (now - LIBRARY_CACHE["last_updated"] < 300):
        return LIBRARY_CACHE["data"]
    if not AUDIO_DIR.exists():
        return {"albums": [], "tracks": []}

    albums, tracks = [], []
    for item in sorted(AUDIO_DIR.iterdir(), key=lambda x: x.name.lower()):
        if item.is_dir():
            atracks = sorted(
                [get_track_info(f) for f in sorted(item.iterdir(), key=lambda x: x.name.lower())
                 if f.is_file() and f.suffix.lower() in SUPPORTED],
                key=lambda t: (t["track_number"] or 999, t["title"])
            )
            if not atracks: continue
            first = atracks[0]
            albums.append({
                "folder":      item.name,
                "name":        first["album"]  if first["album"]  != "Unknown Album"  else item.name,
                "artist":      first["artist"] if first["artist"] != "Unknown Artist" else "Various Artists",
                "cover_path":  next((t["path"] for t in atracks if t["has_cover"]), None),
                "tracks":      atracks,
                "track_count": len(atracks),
            })
        elif item.is_file() and item.suffix.lower() in SUPPORTED:
            tracks.append(get_track_info(item))

    LIBRARY_CACHE.update(data={"albums": albums, "tracks": tracks}, last_updated=now)
    start_lyrics_prefetch(LIBRARY_CACHE["data"])
    return LIBRARY_CACHE["data"]

def extract_cover(filepath: Path) -> Optional[tuple[bytes, str]]:
    s = str(filepath)
    if s in COVER_CACHE: return COVER_CACHE[s]
    try:
        audio = MutagenFile(filepath)
        if not audio: return None
        data, mime = None, "image/jpeg"
        if hasattr(audio, "pictures") and audio.pictures:
            p = audio.pictures[0]; data, mime = p.data, p.mime or "image/jpeg"
        elif audio.tags:
            for k in audio.tags:
                if k.startswith("APIC"): data = audio.tags[k].data; break
                if k == "covr":
                    imgs = audio.tags[k]
                    if imgs: data = bytes(imgs[0]); break
        if data: COVER_CACHE[s] = (data, mime); return data, mime
    except Exception: pass
    return None

# ── Lyrics helpers ─────────────────────────────────────────────────────────────

def clean_title(t: str) -> str:
    return re.sub(r'\s*[\(\[].*?[\)\]]', '', t).strip()

def _fetch_lrclib(track: str, artist: str) -> tuple[Optional[str], Optional[str], int]:
    """Returns (synced, plain, not_found_flag). Raises on non-404 HTTP errors."""
    params = urllib.parse.urlencode({"track_name": track, "artist_name": artist})
    req    = urllib.request.Request(f"https://lrclib.net/api/get?{params}", headers={"Lrclib-Client": "Balufy Pro"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json_mod.loads(resp.read().decode())
        synced = data.get("syncedLyrics") or None
        plain  = data.get("plainLyrics")  or None
        return synced, plain, 0 if (synced or plain) else 1
    except urllib.error.HTTPError as exc:
        if exc.code == 404: return None, None, 1
        raise
    except Exception: raise

def _upsert_lyrics(db, key, track, artist, synced, plain, nf, ts=None):
    db.execute("""
        INSERT INTO lyrics (key, track_name, artist_name, synced_lyrics, plain_lyrics, not_found, cached_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET
            synced_lyrics=excluded.synced_lyrics, plain_lyrics=excluded.plain_lyrics,
            not_found=excluded.not_found, cached_at=excluded.cached_at
    """, (key, track, artist, synced, plain, nf, ts or int(time.time())))

# ── Lyrics prefetch ───────────────────────────────────────────────────────────

prefetch_lock  = threading.Lock()
prefetch_state = {"total": 0, "cached": 0, "running": False}

def prefetch_lyrics_bg(tracks: list):
    now, todo = int(time.time()), []
    with open_db(LYRICS_DB) as db:
        for t in tracks:
            title, artist = clean_title(t.get("title", "")), t.get("artist", "")
            if not title or not artist or artist == "Unknown Artist": continue
            key = lyrics_key(title, artist)
            row = db.execute("SELECT not_found, cached_at FROM lyrics WHERE key=?", (key,)).fetchone()
            if row:
                nf, cached_at = row
                if now - cached_at < (LYRICS_NF_TTL if nf else LYRICS_CACHE_TTL): continue
            todo.append((title, artist))

    with prefetch_lock:
        prefetch_state.update(total=len(tracks), cached=len(tracks)-len(todo), running=True)

    for title, artist in todo:
        try:
            synced, plain, nf = _fetch_lrclib(title, artist)
        except Exception:
            time.sleep(2); continue

        key = lyrics_key(title, artist)
        with open_db(LYRICS_DB) as db:
            _upsert_lyrics(db, key, title, artist, synced, plain, nf)
            db.commit()

        with prefetch_lock:
            prefetch_state["cached"] += 1
        time.sleep(0.5)

    with prefetch_lock:
        prefetch_state["running"] = False

def start_lyrics_prefetch(library: dict):
    all_tracks = list(library.get("tracks", []))
    for alb in library.get("albums", []): all_tracks.extend(alb.get("tracks", []))
    threading.Thread(target=prefetch_lyrics_bg, args=(all_tracks,), daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    ico = Path(__file__).parent / "favicon.ico"
    return send_file(ico, mimetype="image/x-icon", max_age=86400) if ico.is_file() else abort(404)

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"): return redirect(url_for("index"))
    generate_csrf_token()
    error = None
    if request.method == "POST":
        if hashlib.sha256(request.form.get("password", "").encode()).hexdigest() == PWD_HASH:
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
    sw_js = r"""
const CACHE_NAME='balufy-v1',COVER_RE=/\/api\/cover\//,STATIC_RE=/\/static\//,
      LIBRARY_URL='/api/library',COVER_TTL=7*24*3600*1000;
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(
    caches.keys().then(k=>Promise.all(k.filter(n=>n!==CACHE_NAME).map(n=>caches.delete(n))))
    .then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
    const url=event.request.url;
    if(COVER_RE.test(url))  return event.respondWith(cacheFirstTTL(event.request,COVER_TTL));
    if(STATIC_RE.test(url)) return event.respondWith(cacheFirst(event.request));
    if(url.includes(LIBRARY_URL)) return event.respondWith(staleWhileRevalidate(event.request));
});
async function cacheFirst(req){
    const c=await caches.match(req);if(c)return c;
    const r=await fetch(req);if(r.ok)(await caches.open(CACHE_NAME)).put(req,r.clone());return r;
}
async function cacheFirstTTL(req,ttl){
    const cache=await caches.open(CACHE_NAME),c=await cache.match(req);
    if(c&&Date.now()-new Date(c.headers.get('date')||0)<ttl)return c;
    try{const r=await fetch(req);if(r.ok)cache.put(req,r.clone());return r;}
    catch(_){return c||new Response('',{status:503});}
}
async function staleWhileRevalidate(req){
    const cache=await caches.open(CACHE_NAME),c=await cache.match(req);
    const f=fetch(req).then(r=>{if(r.ok)cache.put(req,r.clone());return r;}).catch(()=>null);
    return c||await f||new Response('{}');
}""".strip()
    return Response(sw_js, 200, headers={"Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-cache"})

@app.route("/")
@login_required
def index():
    return render_template("index.html", ffmpeg_available=bool(FFMPEG_BIN))

@app.route("/api/library")
@login_required
def api_library():
    data = scan_library_cached()
    etag = f'"{hashlib.md5(str(LIBRARY_CACHE["last_updated"]).encode()).hexdigest()[:16]}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)
    resp = jsonify(data)
    resp.headers.update(ETag=etag, **{"Cache-Control": "private, max-age=60, stale-while-revalidate=300"})
    return resp

@app.route("/api/refresh")
@login_required
def api_refresh():
    return jsonify(scan_library_cached(force=True))

@app.route("/api/cover/<path:filepath>")
@login_required
def api_cover(filepath):
    safe = secure_path(filepath)
    if not safe or not safe.is_file(): abort(404)
    mtime = int(safe.stat().st_mtime)
    etag  = f'"{hashlib.md5(f"{filepath}:{mtime}".encode()).hexdigest()[:16]}"'
    if request.headers.get("If-None-Match") == etag: return Response(status=304)
    result = extract_cover(safe)
    if not result: abort(404)
    data, mime = result
    return Response(data, 200, headers={
        "Content-Type": mime, "ETag": etag, "Content-Length": str(len(data)),
        "Cache-Control": "private, max-age=604800, stale-while-revalidate=86400",
    })

CHUNK_FIRST, CHUNK = 16 * 1024, 128 * 1024

@app.route("/api/stream/<path:filepath>")
@login_required
def api_stream(filepath):
    safe = secure_path(filepath)
    if not safe or not safe.is_file(): abort(404)

    quality = request.args.get("quality", "original")
    seek_s  = max(0, int(request.args.get("seek", 0) or 0))

    if quality in ("320", "192", "128") and FFMPEG_BIN:
        cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error"]
        if seek_s: cmd += ["-ss", str(seek_s)]
        cmd += ["-i", str(safe), "-vn", "-c:a", "libmp3lame", "-b:a", f"{quality}k", "-f", "mp3", "pipe:1"]
        def ffmpeg_gen():
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while chunk := proc.stdout.read(CHUNK): yield chunk
            finally:
                try: proc.kill(); proc.wait()
                except Exception: pass
        return Response(ffmpeg_gen(), 200, headers={
            "Content-Type": "audio/mpeg", "Accept-Ranges": "none",
            "Cache-Control": "no-cache", "X-Transcoded": quality,
        })

    mime, size = MIME_MAP.get(safe.suffix.lower(), "audio/mpeg"), safe.stat().st_size
    base = {"Content-Type": mime, "Accept-Ranges": "bytes", "Cache-Control": "no-cache"}

    range_hdr = request.headers.get("Range")
    if range_hdr:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if not m: abort(416)
        start = int(m.group(1))
        end   = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        if start > end or start >= size: abort(416)
        length = end - start + 1
        def range_gen():
            with open(safe, "rb") as fh:
                fh.seek(start)
                rem, first = length, True
                while rem > 0:
                    chunk = fh.read(min(CHUNK_FIRST if first else CHUNK, rem))
                    if not chunk: break
                    rem -= len(chunk); first = False; yield chunk
        return Response(range_gen(), 206, headers={**base,
            "Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)})

    def full_gen():
        with open(safe, "rb") as fh:
            first = True
            while chunk := fh.read(CHUNK_FIRST if first else CHUNK):
                first = False; yield chunk
    return Response(full_gen(), 200, headers={**base, "Content-Length": str(size)})

@app.route("/api/lyrics/cache-status")
@login_required
def api_lyrics_cache_status():
    with prefetch_lock:
        return jsonify(dict(prefetch_state))

@app.route("/api/liked", methods=["GET"])
@login_required
def api_liked_get():
    with open_db(LYRICS_DB) as db:
        rows = db.execute("SELECT path FROM liked_songs ORDER BY liked_at DESC").fetchall()
    return jsonify({"paths": [r[0] for r in rows]})

@app.route("/api/liked/<path:filepath>", methods=["POST", "DELETE"])
@login_required
def api_liked_toggle(filepath):
    safe = secure_path(filepath)
    if not safe or not safe.is_file(): abort(404)
    rel = str(safe.relative_to(AUDIO_DIR.resolve()))
    with open_db(LYRICS_DB) as db:
        if request.method == "POST":
            db.execute("INSERT OR REPLACE INTO liked_songs (path, liked_at) VALUES (?,?)", (rel, int(time.time())))
        else:
            db.execute("DELETE FROM liked_songs WHERE path=?", (rel,))
        db.commit()
    return jsonify({"liked": request.method == "POST", "path": rel})

@app.route("/api/lyrics")
@login_required
def api_lyrics():
    track_name  = request.args.get("track_name",  "").strip()
    artist_name = request.args.get("artist_name", "").strip()
    if not track_name or not artist_name:
        return jsonify({"error": "missing params"}), 400

    key, now = lyrics_key(track_name, artist_name), int(time.time())
    with open_db(LYRICS_DB) as db:
        row = db.execute(
            "SELECT synced_lyrics, plain_lyrics, not_found, cached_at FROM lyrics WHERE key=?", (key,)
        ).fetchone()
        if row:
            synced, plain, nf, cached_at = row
            if now - cached_at < (LYRICS_NF_TTL if nf else LYRICS_CACHE_TTL):
                return (jsonify({"error": "not_found"}), 404) if nf else jsonify({"syncedLyrics": synced, "plainLyrics": plain})

        try:
            synced, plain, nf = _fetch_lrclib(track_name, artist_name)
        except Exception:
            return jsonify({"error": "api_error"}), 502

        _upsert_lyrics(db, key, track_name, artist_name, synced, plain, nf, now)
        db.commit()

    return (jsonify({"error": "not_found"}), 404) if nf else jsonify({"syncedLyrics": synced, "plainLyrics": plain})

@app.route("/api/listen-event", methods=["POST"])
@login_required
def api_listen_event():
    data = request.get_json(silent=True) or {}
    path, play_secs = str(data.get("path", "")).strip(), max(0, int(data.get("play_secs", 0) or 0))
    if not path: return jsonify({"error": "missing path"}), 400
    safe = secure_path(path)
    if not safe or not safe.is_file(): return jsonify({"error": "not found"}), 404
    rel = str(safe.relative_to(AUDIO_DIR.resolve()))
    with open_db(LYRICS_DB) as db:
        db.execute("INSERT INTO listen_history (path, played_at, play_secs) VALUES (?,?,?)",
                   (rel, int(time.time()), play_secs))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/today-hits")
@login_required
def api_today_hits():
    import random as rand
    rng = rand.Random()
    lib = scan_library_cached()
    all_tracks = list(lib.get("tracks", [])) + [t for a in lib.get("albums", []) for t in a.get("tracks", [])]
    if not all_tracks:
        return jsonify({"tracks": [], "generated_at": int(time.time())})

    now       = int(time.time())
    week_ago  = now - 7  * 24 * 3600
    month_ago = now - 30 * 24 * 3600
    dur_map   = {t["path"]: max(1, t.get("duration", 180)) for t in all_tracks}

    with open_db(LYRICS_DB) as db:
        rows = db.execute("SELECT path, played_at, play_secs FROM listen_history WHERE played_at>?",
                          (month_ago,)).fetchall()

    raw: dict[str, float] = {}
    for path, played_at, play_secs in rows:
        dur  = dur_map.get(path, 180)
        base = 3.0 if played_at >= week_ago else 1.0
        base += 2.0 if play_secs >= dur * 0.75 else (0.5 if play_secs >= 60 else 0)
        raw[path] = raw.get(path, 0.0) + base

    track_map = {t["path"]: t for t in all_tracks}
    scored = sorted(
        [(track_map[p], s * rng.uniform(0.70, 1.30)) for p, s in raw.items() if p in track_map],
        key=lambda x: x[1], reverse=True
    )
    never   = [t for t in all_tracks if t["path"] not in raw]
    rng.shuffle(never)

    known_q, disc_q = 23, 7
    playlist = [t for t, _ in scored[:known_q]] + never[:disc_q]
    if len(playlist) < 10:
        extras = [t for t in all_tracks if t not in playlist]
        rng.shuffle(extras)
        playlist += extras[:30 - len(playlist)]
    if len(playlist) > 3:
        top3, rest = playlist[:3], playlist[3:]
        rng.shuffle(rest)
        playlist = top3 + rest

    return jsonify({"tracks": playlist[:30], "generated_at": now})

@app.route("/api/predict", methods=["POST"])
@login_required
def api_predict():
    data, now = request.get_json(silent=True) or {}, int(time.time())
    events, client_id = data.get("events", []), data.get("client_id", "unknown")
    current_track = data.get("current_track")

    with open_db(HABITS_DB) as db:
        for ev in events:
            etype, edata = ev.get("type", ""), ev.get("data", {})
            db.execute("INSERT INTO user_events (client_id, event_type, payload, ts) VALUES (?,?,?,?)",
                       (client_id, etype, json_mod.dumps(edata), ev.get("ts", now)))
            if etype == "play":
                curr = edata.get("track", "")
                if curr:
                    db.execute("INSERT INTO global_plays (path, count) VALUES (?,1) ON CONFLICT(path) DO UPDATE SET count=count+1", (curr,))
                prev = edata.get("previous_track", "")
                if prev and curr:
                    db.execute("INSERT INTO transition_counts (from_path, to_path, count) VALUES (?,?,1) ON CONFLICT(from_path, to_path) DO UPDATE SET count=count+1", (prev, curr))
        db.commit()

        predictions = []
        if current_track:
            rows = db.execute("SELECT to_path, count FROM transition_counts WHERE from_path=? ORDER BY count DESC LIMIT ?",
                              (current_track, PREDICT_CACHE_SIZE)).fetchall()
            predictions = [{"path": p, "score": c} for p, c in rows]

        needed = PREDICT_CACHE_SIZE - len(predictions)
        if needed > 0:
            existing = {p["path"] for p in predictions}
            rows = db.execute("SELECT path, count FROM global_plays ORDER BY count DESC LIMIT ?",
                              (needed + len(predictions),)).fetchall()
            for path, cnt in rows:
                if path not in existing:
                    predictions.append({"path": path, "score": cnt})
                    if len(predictions) >= PREDICT_CACHE_SIZE: break

    return jsonify({"predictions": predictions[:PREDICT_CACHE_SIZE]})

@app.route("/api/admin/bans")
@login_required
def api_admin_bans():
    now = time.time()
    with ban_lock:
        snapshot = {ip: {"banned": u > now, "unban_in_seconds": max(0, int(u - now)), "violations": c}
                    for ip, (u, c) in ban_table.items()}
    return jsonify({"bans": snapshot, "total_tracked": len(snapshot),
                    "active_bans": sum(1 for v in snapshot.values() if v["banned"]),
                    "ids_log": str(ids_log_path.resolve())})

@app.route("/api/admin/bans/<path:ip_addr>", methods=["DELETE"])
@login_required
def api_admin_unban(ip_addr: str):
    with ban_lock:
        if ip_addr in ban_table:
            del ban_table[ip_addr]
            ids_log(ip_addr, "MANUALLY_UNBANNED")
            return jsonify({"ok": True, "ip": ip_addr})
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    AUDIO_DIR.mkdir(exist_ok=True)
    lyrics_db().close()
    habits_db().close()
    print("=" * 52)
    print("  Balufy Pro — Personal Music Server")
    scan_library_cached(force=True)
    print(f"  Audio dir : {AUDIO_DIR.resolve()}")
    print(f"  Lyrics DB : {LYRICS_DB.resolve()}")
    print(f"  Habits DB : {HABITS_DB.resolve()}")
    print(f"  IDS log   : {ids_log_path.resolve()}")
    print(f"  FFmpeg    : {FFMPEG_BIN or 'not found (quality selector disabled)'}")
    print(f"  Rate limits: global={RL_GLOBAL}/min  api={RL_API}/min  stream={RL_STREAM}/min  login={RL_LOGIN}/min")
    print(f"  Ban policy : threshold={BAN_THRESH} violations  base={BAN_DUR}s (exponential)")
    print("=" * 52)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
