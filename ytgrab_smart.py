#!/usr/bin/env python3
"""YTGrab Smart Discovery - Stark Industries media acquisition & smart search system.
Run:  python ytgrab_smart.py  ->  opens http://127.0.0.1:8765 in your browser.
Needs Python 3.8+, yt-dlp (auto-installed) and FFmpeg.  Built by Hari - Stark Industries."""
import json, os, re, shutil, subprocess, sys, threading, time, uuid, webbrowser
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("Installing yt-dlp ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    import yt_dlp
from yt_dlp.utils import DownloadCancelled

PORT = int(os.environ.get("YTGRAB_PORT", 8765))
DOWNLOADS = Path.home() / "Downloads"
DEFAULT_TPL = "%(title)s [%(id)s].%(ext)s"
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".flac", ".wav", ".ogg", ".aac", ".mov"}
JOBS, OPTS, LOCK, SEM = [], {}, threading.Lock(), threading.Semaphore(3)  # 3 parallel downloads

OFFICIAL_HINTS = (
    "vevo", "topic", "official", "sony music", "think music", "saregama", "lahari",
    "t-series", "aditya music", "junglee", "zee music", "sun tv", "ayngaran", "eros",
    "wunderbar", "anirudh", "rahman", "records", "studios"
)

NOISE_WORDS = re.compile(
    r"\b(official|full|video|audio|lyric|lyrical|song|songs|hd|4k|8k|hq|uhd|remastered|remaster|"
    r"visualizer|music|ost|bgm|soundtrack|theme|jukebox|album|movie|film|tamil|english|hindi|"
    r"telugu|malayalam|kannada|version|with|lyrics|video song|audio song|full video)\b",
    re.I,
)


def find_job(jid):
    return next((j for j in JOBS if j["id"] == jid), None)


# ---------- player / file helpers ----------
def find_vlc():
    p = shutil.which("vlc")
    if p:
        return p
    cands = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands.append(Path(base) / "VideoLAN" / "VLC" / "vlc.exe")
                cands.append(Path(base) / "Programs" / "VideoLAN" / "VLC" / "vlc.exe")
    elif sys.platform == "darwin":
        cands.append(Path("/Applications/VLC.app/Contents/MacOS/VLC"))
    for c in cands:
        if c.exists():
            return str(c)
    return None


def open_path(p):
    if sys.platform.startswith("win"):
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def play_files(files, prefer_vlc=True):
    vlc = find_vlc() if prefer_vlc else None
    if vlc:
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform.startswith("win"):
            kw["creationflags"] = 0x00000008 | 0x00000200
        subprocess.Popen([vlc, *files], **kw)
        return "VLC"
    open_path(files[0])
    return "system player"


def reveal(path):
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(Path(path).resolve())])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


def recent_media(folder, since, vid_id=""):
    out = []
    root = Path(folder)
    if not root.exists():
        return []
    for pat in ("*", "*/*"):
        for p in root.glob(pat):
            try:
                if p.is_file() and p.suffix.lower() in MEDIA_EXT and not p.name.endswith(".part"):
                    if p.stat().st_mtime >= since - 3 or (vid_id and f"[{vid_id}]" in p.name):
                        out.append(p)
            except OSError:
                pass
    return [str(p) for p in sorted(out, key=lambda x: x.stat().st_mtime)]


# ---------- smart search & deduplication ----------
def canonical_song_key(title, query=""):
    """Normalize a YouTube video title into a clean canonical song key to detect duplicates."""
    s = (title or "").lower()
    # Remove bracketed/parenthesized metadata e.g. (Official Video), [4K]
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
    # Remove query words (e.g. movie or artist name) if other words exist
    q_tokens = [re.escape(w.lower()) for w in re.findall(r"\w+", query or "") if len(w) > 2]
    # Split by common separators (- | : //) and remove parts that are just the movie/artist or noise
    parts = [p.strip() for p in re.split(r"[\|\-:•–—/]+", s) if p.strip()]
    cleaned_parts = []
    for p in parts:
        p_clean = NOISE_WORDS.sub(" ", p)
        if q_tokens:
            p_clean = re.sub(r"\b(" + "|".join(q_tokens) + r")\b", " ", p_clean)
        p_clean = re.sub(r"[^a-z0-9\u0B80-\u0BFF]+", " ", p_clean).strip()
        if len(p_clean) >= 3:
            cleaned_parts.append(p_clean)
    base = cleaned_parts[0] if cleaned_parts else s
    base = NOISE_WORDS.sub(" ", base)
    base = re.sub(r"[^a-z0-9\u0B80-\u0BFF]+", " ", base)
    tokens = [t for t in base.split() if len(t) > 1]
    return " ".join(tokens[:5]) or re.sub(r"\W+", "", (title or "").lower())[:20]


def is_similar_song(k1, k2):
    if not k1 or not k2:
        return False
    if k1 == k2 or k1 in k2 or k2 in k1:
        return True
    return SequenceMatcher(None, k1, k2).ratio() >= 0.78


def score_entry(e):
    """Score a search result so official channels and high-view uploads win deduplication."""
    ch = (e.get("uploader") or e.get("channel") or "").lower()
    title = (e.get("title") or "").lower()
    views = e.get("view_count") or 0
    score = min(views / 100_000, 50)
    if any(h in ch for h in OFFICIAL_HINTS):
        score += 40
    if "official" in title:
        score += 15
    if any(bad in title for bad in ("8d", "slowed", "reverb", "karaoke", "cover", "status", "whatsapp", "reaction", "shorts")):
        score -= 35
    return score


def smart_search(query, category="movie", lang="orig", cookies="", proxy="", limit=22):
    q = (query or "").strip()
    if not q:
        return []
    lang_map = {"ta": "Tamil", "en": "English", "hi": "Hindi", "te": "Telugu", "ml": "Malayalam"}
    lang_word = lang_map.get(lang, "")

    if category == "movie":
        search_q = f"{q} {lang_word} movie songs".strip()
    elif category == "artist":
        search_q = f"{q} {lang_word} top songs official".strip()
    elif category == "mood":
        search_q = f"{q} {lang_word} songs".strip()
    elif category == "playlist":
        search_q = f"{q} {lang_word} full album jukebox songs".strip()
    else:
        search_q = f"{q} {lang_word}".strip()

    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "web", "tv"]}},
    }
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    elif Path("cookies.txt").exists():
        opts["cookiefile"] = "cookies.txt"
    if proxy:
        opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{limit}:{search_q}", download=False)

    raw_entries = (res or {}).get("entries") or []
    candidates = []
    for e in raw_entries:
        if not e or not e.get("id"):
            continue
        dur = e.get("duration") or 0
        # Filter out <45s shorts/teasers; for individual song searches, skip >14m jukeboxes
        if category != "playlist" and dur and (dur < 45 or dur > 840):
            continue
        thumbs = e.get("thumbnails") or []
        thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg")
        url = e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        key = canonical_song_key(e.get("title") or "", q)
        candidates.append({
            "id": e["id"],
            "url": url,
            "title": e.get("title") or e["id"],
            "uploader": e.get("uploader") or e.get("channel") or "YouTube",
            "duration": dur,
            "views": e.get("view_count"),
            "thumb": thumb,
            "key": key,
            "score": score_entry(e),
        })

    # Sort by quality/official score first so the best version of each song is kept
    candidates.sort(key=lambda x: x["score"], reverse=True)
    unique = []
    kept_keys = []
    for c in candidates:
        if any(is_similar_song(c["key"], k) for k in kept_keys):
            continue
        kept_keys.append(c["key"])
        unique.append(c)

    return unique


# ---------- yt-dlp download ----------
def build_opts(o, job):
    folder = Path(o.get("folder") or DOWNLOADS).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    job["folder"] = str(folder)
    tpl = o.get("template") or DEFAULT_TPL
    if o.get("playlist") and tpl == DEFAULT_TPL:
        tpl = "%(playlist_title|Playlist)s/%(playlist_index|0)02d - " + DEFAULT_TPL
    pp, opts = [], {
        "outtmpl": str(folder / tpl), "windowsfilenames": True, "noplaylist": not o.get("playlist"),
        "quiet": True, "no_warnings": True, "noprogress": True, "retries": 10, "fragment_retries": 10,
        "concurrent_fragment_downloads": 4, "continuedl": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "web", "tv"]}},
    }
    if not o.get("playlist"):
        opts["playlist_items"] = "1"
    elif o.get("items"):
        opts["playlist_items"] = o["items"]
    if o.get("ratelimit"):
        opts["ratelimit"] = int(float(o["ratelimit"]) * 1024 * 1024)
    if o.get("cookies"):
        opts["cookiesfrombrowser"] = (o["cookies"],)
    elif Path("cookies.txt").exists():
        opts["cookiefile"] = "cookies.txt"
    if o.get("proxy"):
        opts["proxy"] = o["proxy"]

    alang = (o.get("alang") or "orig").strip()
    # Language-aware audio selector with automatic fallback to default best audio
    ba_sel = f"ba[language^={alang}]/ba" if alang and alang != "orig" else "ba"

    if o["mode"] == "video":
        h = int(o.get("height") or 0)
        if h:
            opts["format"] = f"bv*[height<={h}]+{ba_sel}/b[height<={h}]/bv*+ba/b"
        else:
            opts["format"] = f"bv*+{ba_sel}/b/bv*+ba"
        opts["merge_output_format"] = o.get("container", "mkv")
        if o.get("container") == "mp4":
            opts["format_sort"] = ["res", "ext:mp4:m4a"]
    else:
        opts["format"] = f"{ba_sel}/b"
        codec = o.get("aformat", "mp3")
        if codec != "best":
            pp.append({"key": "FFmpegExtractAudio", "preferredcodec": codec,
                       "preferredquality": o.get("abitrate", "192") if codec in ("mp3", "m4a", "opus") else None})

    if o.get("sb"):
        pp.append({"key": "SponsorBlock", "categories": ["sponsor", "selfpromo", "interaction"], "when": "after_filter"})
        pp.append({"key": "ModifyChapters", "remove_sponsor_segments": ["sponsor", "selfpromo", "interaction"]})
    if o.get("subs"):
        opts.update(writesubtitles=True, writeautomaticsub=bool(o.get("autosubs")),
                    subtitleslangs=[s.strip() for s in (o.get("sublangs") or "en").split(",") if s.strip()])
        if o.get("subembed") and o["mode"] == "video":
            pp.append({"key": "FFmpegEmbedSubtitle"})
    if o.get("meta"):
        pp.append({"key": "FFmpegMetadata", "add_chapters": bool(o.get("chapters", True)), "add_metadata": True})
    if o.get("thumb"):
        opts["writethumbnail"] = True
        pp.append({"key": "EmbedThumbnail"})
    opts["postprocessors"] = pp

    def add_file(p):
        if p and isinstance(p, str) and Path(p).suffix.lower() in MEDIA_EXT and p not in job["files"]:
            job["files"].append(p)

    def hook(d):
        if job["cancel"]:
            raise DownloadCancelled()
        info = d.get("info_dict") or {}
        if info.get("id") and not job.get("vid_id"):
            job["vid_id"] = info["id"]
        if info.get("title"):
            job["title"] = info["title"]
        if info.get("thumbnail") and not job["thumb"]:
            job["thumb"] = info["thumbnail"]
        if info.get("playlist_index"):
            job["part"] = f"{info['playlist_index']}/{info.get('n_entries') or '?'}"
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            job.update(status="downloading", speed=d.get("speed") or 0, eta=d.get("eta"),
                       percent=(d.get("downloaded_bytes", 0) / tot * 100) if tot else 0)
        elif d["status"] == "finished":
            job["percent"] = 100

    def pphook(d):
        if d["status"] == "started":
            job.update(status="processing", speed=0, eta=None)
        elif d["status"] == "finished":
            info = d.get("info_dict") or {}
            add_file(info.get("filepath") or info.get("_filename"))

    def final_file(d):
        if isinstance(d, str):
            add_file(d)
        elif isinstance(d, dict):
            add_file(d.get("filepath") or (d.get("info_dict") or {}).get("filepath"))

    opts["progress_hooks"], opts["postprocessor_hooks"], opts["post_hooks"] = [hook], [pphook], [final_file]
    return opts


def run_job(job, o):
    with SEM:
        if job["cancel"]:
            job["status"] = "cancelled"
            return
        try:
            job["status"] = "downloading"
            job["started"] = time.time()
            with yt_dlp.YoutubeDL(build_opts(o, job)) as ydl:
                ydl.download([job["url"]])
            job["files"] = [f for f in job["files"] if os.path.exists(f)]
            if not job["files"]:
                job["files"] = recent_media(job["folder"], job["started"], job.get("vid_id", ""))
            job.update(status="done", percent=100, speed=0, eta=None)
        except DownloadCancelled:
            job["status"] = "cancelled"
        except Exception as e:
            job.update(status="cancelled" if job["cancel"] else "error",
                       error=str(e).replace("ERROR: ", "")[:400])


def get_info(url, cookies="", proxy="", playlist=False):
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
        "noplaylist": not playlist,
        "extractor_args": {"youtube": {"player_client": ["ios", "web", "tv"]}},
    }
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    elif Path("cookies.txt").exists():
        opts["cookiefile"] = "cookies.txt"
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        i = ydl.extract_info(url, download=False)
    heights = sorted({f["height"] for f in i.get("formats", []) if f.get("height")}, reverse=True)
    langs = sorted({f.get("language") for f in i.get("formats", []) if f.get("language")})
    entries = i.get("entries") or []
    is_pl = i.get("_type") in ("playlist", "multi_video")
    return {
        "title": i.get("title"),
        "uploader": i.get("uploader") or i.get("channel"),
        "duration": i.get("duration"),
        "thumb": i.get("thumbnail") or (entries[0].get("thumbnail") if entries and isinstance(entries[0], dict) else None),
        "views": i.get("view_count"),
        "is_playlist": is_pl,
        "count": len(entries) if is_pl else 1,
        "heights": heights,
        "langs": langs,
    }


def config():
    try:
        free = round(shutil.disk_usage(DOWNLOADS if DOWNLOADS.exists() else Path.home()).free / 1024 ** 3, 1)
    except OSError:
        free = None
    return {
        "folder": str(DOWNLOADS),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "version": yt_dlp.version.__version__,
        "vlc": bool(find_vlc()),
        "disk_free": free,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.host_ok():
            return self.reply({"error": "forbidden"}, 403)
        if self.path == "/":
            self.reply(PAGE, ctype="text/html")
        elif self.path == "/api/state":
            with LOCK:
                jobs = [dict(j, files=list(j["files"])) for j in JOBS]
            self.reply({"jobs": jobs, "config": config()})
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.host_ok() or (origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")):
            return self.reply({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/search":
                results = smart_search(
                    data.get("query", ""),
                    data.get("category", "movie"),
                    data.get("lang", "orig"),
                    data.get("cookies", ""),
                    data.get("proxy", ""),
                )
                self.reply({"results": results})
            elif self.path == "/api/info":
                self.reply(get_info(data["url"], data.get("cookies", ""), data.get("proxy", ""), bool(data.get("playlist"))))
            elif self.path == "/api/download":
                ids = []
                for it in data["items"]:
                    url = (it.get("url") or "").strip()
                    if not url:
                        continue
                    o = dict(data["opts"], playlist=bool(it.get("playlist")))
                    job = {"id": uuid.uuid4().hex[:8], "url": url, "title": it.get("title") or url,
                           "thumb": it.get("thumb") or "", "status": "queued",
                           "percent": 0, "speed": 0, "eta": None, "error": "", "folder": "", "part": "",
                           "mode": o["mode"], "alang": o.get("alang", "orig"), "playlist": o["playlist"],
                           "cancel": False, "added": time.time(), "started": 0, "vid_id": "", "files": []}
                    with LOCK:
                        JOBS.insert(0, job)
                    OPTS[job["id"]] = o
                    ids.append(job["id"])
                    threading.Thread(target=run_job, args=(job, o), daemon=True).start()
                self.reply({"ids": ids})
            elif self.path == "/api/cancel":
                j = find_job(data["id"])
                if j:
                    j["cancel"] = True
                self.reply({"ok": True})
            elif self.path == "/api/retry":
                j = find_job(data["id"])
                if j and j["status"] in ("error", "cancelled") and j["id"] in OPTS:
                    j.update(cancel=False, status="queued", error="", percent=0, files=[], part="")
                    threading.Thread(target=run_job, args=(j, OPTS[j["id"]]), daemon=True).start()
                self.reply({"ok": True})
            elif self.path == "/api/play":
                j = find_job(data.get("id"))
                files = [f for f in (j["files"] if j else []) if os.path.exists(f)]
                if not files and j:
                    files = recent_media(j.get("folder") or DOWNLOADS, j.get("started") or j.get("added", 0), j.get("vid_id", ""))
                    j["files"] = files
                if not files:
                    raise RuntimeError("File not found - it may have been moved or deleted.")
                self.reply({"player": play_files(files, bool(data.get("vlc", True)))})
            elif self.path == "/api/reveal":
                j = find_job(data.get("id"))
                files = [f for f in (j["files"] if j else []) if os.path.exists(f)]
                if not files and j:
                    files = recent_media(j.get("folder") or DOWNLOADS, j.get("started") or j.get("added", 0), j.get("vid_id", ""))
                if files:
                    reveal(files[0])
                else:
                    p = Path((j and j["folder"]) or DOWNLOADS).expanduser()
                    p.mkdir(parents=True, exist_ok=True)
                    open_path(str(p))
                self.reply({"ok": True})
            elif self.path == "/api/open":
                p = Path(data.get("folder") or DOWNLOADS).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                open_path(str(p))
                self.reply({"ok": True})
            elif self.path == "/api/clear":
                with LOCK:
                    JOBS[:] = [j for j in JOBS if j["status"] in ("queued", "downloading", "processing")]
                self.reply({"ok": True})
            elif self.path == "/api/update":
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], capture_output=True, text=True)
                self.reply({"ok": r.returncode == 0, "msg": "Updated. Restart YTGrab to use the new version." if r.returncode == 0 else r.stderr[-300:]})
            else:
                self.reply({"error": "not found"}, 404)
        except Exception as e:
            self.reply({"error": str(e).replace("ERROR: ", "")[:400]}, 400)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YTGrab Smart Discovery // Stark Industries</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#04070d;--ink:#e6f6ff;--mute:#86a0b6;--line:rgba(140,220,255,.2);--acc:#37d7ff;--acc2:#2f7bff;--hot:#ff3d4f;--gold:#ffc24a;--ok:#35f0a5;--bad:#ff5468;--accink:#00131c;--glass:linear-gradient(135deg,rgba(255,255,255,.14),rgba(255,255,255,.03))}
:root[data-t=mark]{--acc:#ffc24a;--acc2:#ff3d4f;--hot:#ff7a1a;--gold:#fff0b3;--line:rgba(255,190,110,.22);--ink:#fff3e4;--mute:#bba48c;--accink:#1a0805}
:root[data-t=ultron]{--acc:#ff2a54;--acc2:#9b2fff;--hot:#ff003c;--gold:#ff8c42;--line:rgba(255,70,110,.24);--ink:#ffeaf0;--mute:#b88699;--accink:#190006}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;min-height:100vh;color:var(--ink);background:var(--bg);font:14px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;overflow-x:hidden}
.hud,h1,h2.t,.tile b{font-family:"Orbitron","Bahnschrift","Segoe UI",sans-serif}
.bg{position:fixed;inset:0;z-index:-3;overflow:hidden;background:radial-gradient(1100px 700px at 85% -10%,color-mix(in srgb,var(--acc2) 24%,transparent),transparent),radial-gradient(900px 600px at -10% 110%,color-mix(in srgb,var(--hot) 20%,transparent),var(--bg))}
.blob{position:absolute;border-radius:50%;filter:blur(80px);opacity:.5;animation:drift 24s ease-in-out infinite alternate}
.b1{width:420px;height:420px;background:var(--acc);left:6%;top:10%}.b2{width:500px;height:500px;background:var(--acc2);right:4%;top:32%;animation-duration:30s}.b3{width:360px;height:360px;background:var(--hot);left:42%;bottom:-10%;opacity:.38;animation-duration:27s}
@keyframes drift{to{transform:translate(110px,-70px) scale(1.25)}}
.grid{position:fixed;inset:0;z-index:-2;background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);background-size:52px 52px;-webkit-mask-image:radial-gradient(ellipse at 50% 25%,#000 5%,transparent 72%);mask-image:radial-gradient(ellipse at 50% 25%,#000 5%,transparent 72%);opacity:.4}
.hring{position:fixed;right:-160px;top:90px;width:640px;height:640px;z-index:-2;opacity:.16;animation:spin 90s linear infinite}
.scan{position:fixed;inset:0;z-index:70;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(255,255,255,.022) 0 1px,transparent 1px 3px);mix-blend-mode:overlay}
@keyframes spin{to{transform:rotate(360deg)}}
/* liquid glass */
.glass{position:relative;border-radius:22px;background:var(--glass);border:1px solid rgba(255,255,255,.17);-webkit-backdrop-filter:blur(26px) saturate(180%);backdrop-filter:blur(26px) saturate(180%);box-shadow:0 14px 44px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.38),inset 0 -1px 0 rgba(255,255,255,.06),inset 0 0 46px rgba(255,255,255,.035)}
.glass>*{position:relative;z-index:1}
.glass::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;background:radial-gradient(440px circle at var(--mx,50%) var(--my,0%),rgba(255,255,255,.17),transparent 45%)}
.glass::after{content:"";position:absolute;inset:8px;border-radius:15px;pointer-events:none;opacity:.7;background:
linear-gradient(var(--acc),var(--acc)) top left/14px 1.5px no-repeat,linear-gradient(var(--acc),var(--acc)) top left/1.5px 14px no-repeat,
linear-gradient(var(--acc),var(--acc)) top right/14px 1.5px no-repeat,linear-gradient(var(--acc),var(--acc)) top right/1.5px 14px no-repeat,
linear-gradient(var(--acc),var(--acc)) bottom left/14px 1.5px no-repeat,linear-gradient(var(--acc),var(--acc)) bottom left/1.5px 14px no-repeat,
linear-gradient(var(--acc),var(--acc)) bottom right/14px 1.5px no-repeat,linear-gradient(var(--acc),var(--acc)) bottom right/1.5px 14px no-repeat}
.bar::after,.tile::after,.banner::after,.dossier::after{display:none}
.bar{display:flex;align-items:center;gap:12px;padding:10px 18px;margin:14px 28px 0;position:sticky;top:12px;z-index:10;flex-wrap:wrap}
h1{font-size:20px;margin:0;font-weight:900;letter-spacing:.2em;background:linear-gradient(90deg,#fff,var(--acc));-webkit-background-clip:text;background-clip:text;color:transparent}
h1 small{display:block;font-size:9px;font-weight:500;letter-spacing:.32em;color:var(--mute);-webkit-text-fill-color:var(--mute)}
.sp{flex:1}.mute{color:var(--mute)}.sm{font-size:12px}.gold{color:var(--gold)}
.reactor{width:44px;height:44px;flex:none;filter:drop-shadow(0 0 8px var(--acc))}.reactor.big{width:120px;height:120px;margin:0 auto 12px}
.reactor svg{width:100%;height:100%;overflow:visible}.sa{stroke:var(--acc)}.sb{stroke:var(--acc2)}
.spin{transform-box:fill-box;transform-origin:center;animation:spin 9s linear infinite}.spin.r{animation-duration:14s;animation-direction:reverse}
.wrap{max-width:1360px;margin:auto;padding:18px 28px 30px}
.banner{display:flex;align-items:center;gap:10px;padding:12px 18px;margin-bottom:14px;flex-wrap:wrap}
.hud{letter-spacing:.14em;font-size:12px;text-transform:uppercase}
.chip{display:inline-flex;align-items:center;gap:7px;font:600 10px "Orbitron",sans-serif;letter-spacing:.14em;padding:5px 11px;border-radius:99px;border:1px solid var(--line);background:rgba(255,255,255,.05)}
.chip::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--bad);box-shadow:0 0 8px var(--bad)}.chip.ok::before{background:var(--ok);box-shadow:0 0 8px var(--ok);animation:pulse 2s infinite}
@keyframes pulse{50%{opacity:.35}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.tile{padding:13px 16px;border-radius:18px}.tile b{display:block;font-size:22px;letter-spacing:.04em;color:var(--acc);text-shadow:0 0 14px color-mix(in srgb,var(--acc) 55%,transparent)}.tile span{font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--mute)}
main{display:grid;grid-template-columns:minmax(360px,490px) 1fr;gap:18px;align-items:start}
@media(max-width:920px){main{grid-template-columns:1fr}.wrap{padding:14px}.bar{margin:10px 12px 0}}
.card{padding:22px}.card h2{font-size:13px;margin:0 0 12px;letter-spacing:.22em;text-transform:uppercase;color:var(--acc)}
textarea,input,select{width:100%;background:rgba(0,10,25,.45);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:10px 12px;font:inherit;transition:.2s}
textarea{min-height:74px;resize:vertical}select option{background:#0b1220;color:#e6f6ff}
label{display:block;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);margin:13px 0 5px}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
textarea:focus,input:focus,select:focus{outline:0;border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent),0 0 18px color-mix(in srgb,var(--acc) 35%,transparent)}
button{font:inherit;cursor:pointer;color:var(--ink);border:1px solid rgba(255,255,255,.2);background:linear-gradient(180deg,rgba(255,255,255,.17),rgba(255,255,255,.04));border-radius:12px;padding:9px 14px;box-shadow:inset 0 1px 0 rgba(255,255,255,.32);transition:.18s}
button:hover{border-color:var(--acc);transform:translateY(-1px);box-shadow:inset 0 1px 0 rgba(255,255,255,.4),0 0 18px color-mix(in srgb,var(--acc) 45%,transparent)}button:active{transform:none}
button.pri{border-color:transparent;color:var(--accink);font-weight:800;letter-spacing:.14em;text-transform:uppercase;background:linear-gradient(120deg,var(--acc2),var(--acc),var(--gold),var(--acc),var(--acc2));background-size:200% 100%;animation:flow 7s linear infinite}
@keyframes flow{to{background-position:200% 0}}
button.big{width:100%;padding:14px;margin-top:18px;font-size:13px}button.ic{padding:5px 11px;font-size:12px;border-radius:10px}
button.play{background:linear-gradient(120deg,color-mix(in srgb,var(--ok) 70%,#000),var(--ok));color:#00170d;border-color:transparent;font-weight:700}
.row{display:flex;gap:10px}.row>*{flex:1}
.seg{display:flex;border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:12px;background:rgba(0,10,25,.35)}
.seg button{flex:1;border:0;border-radius:0;background:transparent;box-shadow:none;letter-spacing:.06em;font-size:12.5px;padding:8px 6px}.seg button:hover{transform:none}
.seg button.on{background:linear-gradient(120deg,var(--acc2),var(--acc));color:var(--accink);font-weight:700}
/* Quick Preset Pills */
.pills{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.pill-btn{font-size:11px;padding:4px 10px;border-radius:99px;background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--mute)}
.pill-btn:hover{color:var(--ink);border-color:var(--acc)}
/* Deduplicated Song Checklist */
.slist{max-height:330px;overflow-y:auto;margin-top:12px;border:1px solid var(--line);border-radius:14px;background:rgba(0,8,20,.55);padding:6px}
.sitem{display:flex;align-items:center;gap:10px;padding:8px;border-bottom:1px solid rgba(255,255,255,.06);border-radius:10px;cursor:pointer;transition:.15s}
.sitem:last-child{border-bottom:0}
.sitem:hover{background:rgba(255,255,255,.06)}
.sitem input{width:18px;height:18px;accent-color:var(--acc);flex:none;cursor:pointer}
.sitem img{width:76px;height:44px;object-fit:cover;border-radius:7px;flex:none;background:#000}
.sinfo{flex:1;min-width:0}
.sinfo b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sinfo span{font-size:11px;color:var(--mute)}
.chk{display:flex;align-items:center;gap:11px;margin:9px 0;font-size:13px;letter-spacing:0;text-transform:none;color:var(--ink);cursor:pointer}
.chk input{appearance:none;-webkit-appearance:none;width:38px;height:22px;flex:none;padding:0;border-radius:99px;background:rgba(255,255,255,.1);position:relative;cursor:pointer;transition:.2s}
.chk input::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;opacity:.85;transition:.2s}
.chk input:checked{background:linear-gradient(120deg,var(--acc2),var(--acc));border-color:var(--acc);box-shadow:0 0 12px color-mix(in srgb,var(--acc) 50%,transparent)}.chk input:checked::after{left:18px}
details{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}summary{cursor:pointer;color:var(--mute);letter-spacing:.14em;text-transform:uppercase;font-size:11px}
.prev{display:flex;gap:12px;margin-top:12px;padding:10px;border:1px solid var(--line);border-radius:14px;background:rgba(0,10,25,.4);align-items:center}
.prev img{width:120px;height:68px;object-fit:cover;border-radius:8px;background:#0003;flex:none}.prev b{display:block}
.job{display:flex;gap:14px;align-items:center;padding:13px 0;border-bottom:1px solid var(--line)}.job:last-child{border:0}
.job img{width:112px;height:63px;object-fit:cover;border-radius:10px;background:rgba(255,255,255,.06);flex:none;box-shadow:0 4px 14px rgba(0,0,0,.4)}
.jb{flex:1;min-width:0}.jt{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar2{height:6px;background:rgba(255,255,255,.1);border-radius:4px;margin:8px 0 5px;overflow:hidden}.bar2 i{display:block;height:100%;background:linear-gradient(90deg,var(--acc2),var(--acc));border-radius:4px;box-shadow:0 0 10px var(--acc);transition:width .4s}
.done .bar2 i{background:var(--ok);box-shadow:0 0 10px var(--ok)}.error .bar2 i{background:var(--bad);box-shadow:none}
.processing .bar2 i{background:repeating-linear-gradient(45deg,var(--acc) 0 8px,transparent 8px 16px);animation:mv 1s linear infinite}@keyframes mv{to{background-position:16px 0}}
.ring{--p:0;width:50px;height:50px;border-radius:50%;flex:none;display:grid;place-items:center;position:relative;font:700 11px "Orbitron",sans-serif;background:conic-gradient(var(--c,var(--acc)) calc(var(--p)*1%),rgba(255,255,255,.1) 0);filter:drop-shadow(0 0 6px var(--c,var(--acc)))}
.ring::before{content:"";position:absolute;inset:4px;border-radius:50%;background:#0a1220}.ring span{position:relative}
.acts{display:flex;gap:7px;margin-top:7px;flex-wrap:wrap}.err{color:var(--bad);font-size:12px}
.warn{background:rgba(245,158,11,.14);border:1px solid #f59e0b;border-radius:12px;padding:9px 12px;margin-bottom:14px;font-size:13px}
.empty{padding:44px 10px;text-align:center;color:var(--mute)}.empty .reactor{opacity:.55}
.dossier{display:flex;align-items:center;gap:14px;padding:14px 20px;margin-top:18px;flex-wrap:wrap}
.dossier .links{display:flex;gap:10px;flex-wrap:wrap}
.dossier a{display:inline-flex;align-items:center;gap:8px;padding:6px 13px;border-radius:99px;border:1px solid var(--line);background:rgba(255,255,255,.05);color:var(--ink);text-decoration:none;font-size:12px;transition:.18s}
.dossier a:hover{border-color:var(--acc);box-shadow:0 0 14px color-mix(in srgb,var(--acc) 40%,transparent);transform:translateY(-1px)}
.dossier a b{font:700 10px "Orbitron",sans-serif;color:var(--acc);letter-spacing:.1em}
.modal{position:fixed;inset:0;z-index:60;display:grid;place-items:center;background:rgba(2,6,12,.55);-webkit-backdrop-filter:blur(9px);backdrop-filter:blur(9px);opacity:0;pointer-events:none;transition:.25s;padding:16px}
.modal.on{opacity:1;pointer-events:auto}.box{width:min(470px,100%);padding:28px;transform:scale(.95);transition:.25s}.modal.on .box{transform:none}
.box h2{margin:0 0 6px;font-size:18px;letter-spacing:.1em}.box p{margin:6px 0}
code#mu{display:block;word-break:break-all;padding:9px 11px;border-radius:10px;background:rgba(0,0,0,.38);font-size:12px;color:var(--acc);margin:8px 0 16px;max-height:64px;overflow:auto}
.about{text-align:center}.ct{display:flex;align-items:center;gap:13px;padding:11px 14px;margin-top:10px;border:1px solid var(--line);border-radius:14px;color:var(--ink);text-decoration:none;background:rgba(255,255,255,.05);transition:.18s;text-align:left}
.ct:hover{border-color:var(--acc);transform:translateX(4px);box-shadow:0 0 18px color-mix(in srgb,var(--acc) 40%,transparent)}
.ct i{font-style:normal;width:32px;height:32px;display:grid;place-items:center;border-radius:50%;background:linear-gradient(135deg,var(--acc2),var(--acc));color:var(--accink);font:800 12px "Orbitron",sans-serif;flex:none}.ct small{display:block;color:var(--mute);font-size:10px;letter-spacing:.16em;text-transform:uppercase}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(12px);padding:11px 20px;border-radius:14px;z-index:80;opacity:0;transition:.25s;pointer-events:none;background:rgba(10,18,32,.85);border:1px solid var(--acc);box-shadow:0 0 24px color-mix(in srgb,var(--acc) 40%,transparent);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}
#toast.on{opacity:1;transform:translateX(-50%)}
footer{text-align:center;padding:14px 0 22px;color:var(--mute);font-size:11px;letter-spacing:.22em;text-transform:uppercase}footer a{color:var(--acc);cursor:pointer;text-decoration:none}
#boot{position:fixed;inset:0;z-index:100;background:radial-gradient(circle at 50% 40%,#0a1a2c,#02050a);display:grid;place-content:center;justify-items:center;gap:6px;transition:opacity .7s;cursor:pointer}#boot.off{opacity:0;pointer-events:none}
#bt{font:13px/1.9 Consolas,"Cascadia Mono",monospace;color:var(--acc);margin:6px 0;min-height:140px;text-shadow:0 0 10px var(--acc)}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body>
<div class="bg"><div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
<svg class="hring" viewBox="0 0 200 200" fill="none" stroke="currentColor" style="color:var(--acc)"><circle cx="100" cy="100" r="96" stroke-width=".6" stroke-dasharray="2 4"/><circle cx="100" cy="100" r="84" stroke-width="2" stroke-dasharray="40 12 4 12"/><circle cx="100" cy="100" r="66" stroke-width=".8"/><circle cx="100" cy="100" r="50" stroke-width="3" stroke-dasharray="20 8"/></svg></div>
<div class="grid"></div><div class="scan"></div>

<div id="boot"><div class="reactor big"></div><pre id="bt"></pre><div class="sm mute">click to skip</div></div>

<header class="glass bar"><div class="reactor"></div>
<h1>YTGRAB<small>STARK INDUSTRIES // SMART DISCOVERY</small></h1><span class="chip ok" id="online">J.A.R.V.I.S ONLINE</span><div class="sp"></div>
<span class="mute sm" id="ver"></span>
<button class="ic" id="voice">Voice: OFF</button><button class="ic" id="theme">Theme</button><button class="ic" id="upd">Update yt-dlp</button><button class="ic" id="aboutb">About</button></header>

<div class="wrap">
<div class="glass banner"><span class="hud" id="greet"></span><div class="sp"></div><span class="chip" id="cf">FFMPEG</span><span class="chip" id="cv">VLC</span></div>
<div class="stats">
<div class="glass tile"><b id="sa">0</b><span>Active</span></div><div class="glass tile"><b id="sq">0</b><span>Queued</span></div>
<div class="glass tile"><b id="sd">0</b><span>Completed</span></div><div class="glass tile"><b id="ss">0 KB/s</b><span>Throughput</span></div>
<div class="glass tile"><b id="sf">--</b><span>Disk free</span></div></div>

<main>
<section class="glass card">
  <h2>Acquisition Console</h2>
  <div id="warn"></div>

  <!-- Tab Switcher: Smart Search vs Direct Links -->
  <div class="seg" id="inmode" style="margin-top:0;margin-bottom:14px">
    <button data-in="search" class="on">🔍 Smart Movie / Artist Search</button>
    <button data-in="url">🔗 Paste Direct Links</button>
  </div>

  <!-- SMART SEARCH PANEL -->
  <div id="search-pane">
    <label style="margin-top:0">Search Category</label>
    <div class="seg" id="scat" style="margin-top:4px">
      <button data-c="movie" class="on">🎬 Movie</button>
      <button data-c="artist">🎤 Artist</button>
      <button data-c="mood">🌙 Mood / Vibe</button>
      <button data-c="playlist">💿 Album / Jukebox</button>
    </div>

    <label>Movie Name, Artist, or Vibe</label>
    <div class="row">
      <input id="sq-in" placeholder="e.g. Vinnaithaandi Varuvaayaa, Taylor Swift, Tamil Melody..." spellcheck="false">
      <button class="pri" style="flex:none" id="sbtn">Search</button>
    </div>

    <div class="pills" id="presets">
      <button class="pill-btn" data-q="Vinnaithaandi Varuvaayaa" data-c="movie" data-l="ta">VTV (Movie)</button>
      <button class="pill-btn" data-q="Tamil Melody Hits" data-c="mood" data-l="ta">Tamil Melody</button>
      <button class="pill-btn" data-q="Tamil Sad Songs" data-c="mood" data-l="ta">Tamil Sad Songs</button>
      <button class="pill-btn" data-q="Taylor Swift" data-c="artist" data-l="en">Taylor Swift</button>
      <button class="pill-btn" data-q="Sia" data-c="artist" data-l="en">Sia</button>
      <button class="pill-btn" data-q="A.R. Rahman Hits" data-c="artist" data-l="ta">A.R. Rahman</button>
    </div>

    <!-- Deduplicated Results Checklist -->
    <div id="sres-wrap" hidden>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:14px">
        <span class="sm gold" id="sres-count">0 unique songs found</span>
        <div style="display:flex;gap:6px">
          <button class="ic" id="sel-all">All</button>
          <button class="ic" id="sel-top5">Top 5</button>
          <button class="ic" id="sel-none">None</button>
        </div>
      </div>
      <div class="slist" id="sres-list"></div>
    </div>
  </div>

  <!-- DIRECT URL PANEL -->
  <div id="url-pane" hidden>
    <label style="margin-top:0">Video or playlist links (one per line)</label>
    <textarea id="urls" placeholder="https://www.youtube.com/watch?v=...   (Ctrl+Enter to add)" spellcheck="false"></textarea>
    <div class="row" style="margin-top:8px"><button id="fetch">Preview first link</button><button id="paste">Paste from clipboard</button></div>
    <div id="prev"></div>
    <label>When a link is or belongs to a playlist</label>
    <select id="plmode"><option value="ask">Ask me every time</option><option value="single">Always just the single video</option><option value="playlist">Always the whole playlist</option></select>
  </div>

  <!-- SHARED FORMAT, LANGUAGE & QUALITY SETTINGS -->
  <div class="seg" id="mode"><button data-m="video" class="on">🎬 Video</button><button data-m="audio">🎵 Audio Only</button></div>

  <div class="row">
    <div>
      <label>Audio / Track Language</label>
      <select id="alang">
        <option value="orig" selected>Original / Default Audio</option>
        <option value="ta">Tamil (தமிழ்)</option>
        <option value="en">English</option>
        <option value="hi">Hindi</option>
        <option value="te">Telugu</option>
        <option value="ml">Malayalam</option>
      </select>
    </div>
  </div>

  <div id="vopts"><div class="row"><div><label>Max resolution</label><select id="height"><option value="0">Best available</option><option>2160</option><option>1440</option><option selected>1080</option><option>720</option><option>480</option><option>360</option></select></div>
  <div><label>Container</label><select id="container"><option value="mkv">MKV (most compatible merge)</option><option value="mp4">MP4</option><option value="webm">WebM</option></select></div></div></div>

  <div id="aopts" hidden><div class="row"><div><label>Format</label><select id="aformat"><option value="mp3">MP3</option><option value="m4a">M4A</option><option value="opus">Opus</option><option value="flac">FLAC</option><option value="wav">WAV</option><option value="best">Best original (no convert)</option></select></div>
  <div><label>Bitrate (kbps)</label><select id="abitrate"><option>320</option><option selected>192</option><option>128</option></select></div></div></div>

  <label>Save to</label><div class="row"><input id="folder"><button style="flex:none" id="openf">Open</button></div>

  <div style="margin-top:12px"><label class="chk" style="margin:0"><input type="checkbox" id="meta" checked> Embed metadata and chapters</label>
  <label class="chk" style="margin:0"><input type="checkbox" id="thumb" checked> Embed thumbnail as cover art</label>
  <label class="chk" style="margin:0"><input type="checkbox" id="sb"> Skip sponsor segments (SponsorBlock)</label>
  <label class="chk" style="margin:0"><input type="checkbox" id="subs"> Download subtitles</label></div>
  <div id="subo" hidden><div class="row"><input id="sublangs" value="en,ta" placeholder="en,es,ta"><label class="chk" style="margin:0"><input type="checkbox" id="autosubs"> Auto-generated</label></div>
  <label class="chk"><input type="checkbox" id="subembed"> Embed into video file</label></div>

  <details><summary>Advanced</summary>
  <label>Play button opens</label><select id="player"><option value="vlc">VLC if installed, else system player</option><option value="sys">System default player</option></select>
  <label>Playlist items (e.g. 1-5,8)</label><input id="items">
  <label>Speed limit (MB/s, blank = unlimited)</label><input id="ratelimit" type="number" min="0" step="0.5">
  <label>Use cookies from browser (fixes YouTube bot / sign-in error)</label><select id="cookies"><option value="">None (or auto cookies.txt)</option><option>firefox</option><option>chrome</option><option>edge</option><option>brave</option><option>opera</option></select>
  <label>Proxy</label><input id="proxy" placeholder="http://host:port">
  <label>Filename template</label><input id="template" value="%(title)s [%(id)s].%(ext)s"></details>

  <button class="pri big" id="go">Initiate Download</button>
</section>

<section class="glass card"><div style="display:flex;align-items:center;margin-bottom:6px"><h2 style="margin:0">Queue</h2><div class="sp"></div><button class="ic" id="clear">Clear finished</button></div>
<div id="jobs"></div></section></main>

<div class="glass dossier">
  <div class="reactor" style="width:34px;height:34px"></div>
  <div><div class="hud gold" style="font-size:10px">Stark Industries // Lead Architect</div><b>Built by Hari</b></div>
  <div class="sp"></div>
  <div class="links">
    <a href="mailto:hariprasath14@gmail.com"><b>MAIL</b> hariprasath14@gmail.com</a>
    <a href="tel:+************" data-copy="**********"><b>MOB</b> +91 ***** *****</a>
    <a href="https://instagram.com/shades_of_ultron" target="_blank" rel="noopener"><b>IG</b> @shades_of_ultron</a>
  </div>
</div>
<footer>Built by Hari &middot; Stark Industries &middot; <a id="aboutf">Contact Dossier</a></footer></div>

<div class="modal" id="modal"><div class="glass box"><h2 class="t">Playlist detected</h2><p class="mute" id="mprompt">This link contains a playlist:</p><code id="mu"></code>
<p style="margin-bottom:14px">What should I download, sir?</p>
<div class="row"><button class="pri" id="m1">Just single video</button><button id="m2">Whole playlist</button></div>
<label class="chk" id="mleft" style="margin-top:16px"><input type="checkbox" id="mall"> Same answer for the other <b id="mallt" style="margin:0 4px"></b> playlist link(s)</label>
<button class="ic" id="mx" style="margin-top:6px">Cancel</button></div></div>

<div class="modal" id="about"><div class="glass box about"><div class="reactor big"></div><div class="hud gold">Stark Industries</div><h2 class="t" style="margin-top:8px">Built by Hari</h2>
<p class="mute sm">YTGrab &middot; media acquisition system &middot; powered by yt-dlp &amp; FFmpeg</p>
<a class="ct" href="mailto:hariprasath14@gmail.com"><i>@</i><div><small>Email</small>hariprasath14@gmail.com</div></a>
<a class="ct" href="tel:+91**********"><i>Ph</i><div><small>Mobile</small>+91 ***** *****</div></a>
<a class="ct" href="https://instagram.com/shades_of_ultron" target="_blank" rel="noopener"><i>IG</i><div><small>Instagram</small>@shades_of_ultron</div></a>
<p class="mute sm" style="margin-top:16px">Please download only content you have the right to save.</p>
<button class="ic" id="ax" style="margin-top:6px">Close</button></div></div>
<div id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let mode='video',inMode='search',scat='movie',cfg={},prev={},lastSig='',voice=localStorage.yv==='1',previewKind={},searchResults=[];
const REACTOR='<svg viewBox="0 0 100 100"><defs><radialGradient id="rg"><stop offset="0" style="stop-color:#fff"/><stop offset=".4" style="stop-color:var(--acc)"/><stop offset="1" style="stop-color:transparent"/></radialGradient></defs><circle class="sa" cx="50" cy="50" r="47" fill="none" stroke-width="1.5" opacity=".6"/><g class="spin"><circle class="sa" cx="50" cy="50" r="40" fill="none" stroke-width="5" stroke-dasharray="14 8"/></g><g class="spin r"><circle class="sb" cx="50" cy="50" r="31" fill="none" stroke-width="2" stroke-dasharray="3 5"/></g><circle class="sa" cx="50" cy="50" r="23" fill="none" stroke-width="3"/><polygon class="sa" points="50,35 63,42.5 63,57.5 50,65 37,57.5 37,42.5" fill="none" stroke-width="1.5"/><circle cx="50" cy="50" r="14" fill="url(#rg)"/></svg>';
document.querySelectorAll('.reactor').forEach(e=>e.innerHTML=REACTOR);
const post=(u,d)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||'Request failed');return j});
let tt;const toast=m=>{const t=$('toast');t.textContent=m;t.classList.add('on');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('on'),3200)};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dur=s=>{if(s==null||!s)return'';s=Math.round(s);const h=s/3600|0,m=(s%3600)/60|0,x=s%60;return(h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')};
const mb=b=>b>1048576?(b/1048576).toFixed(1)+' MB/s':(b/1024).toFixed(0)+' KB/s';
const fmtViews=v=>!v?'':v>=1e6?(v/1e6).toFixed(1)+'M views':v>=1e3?(v/1e3).toFixed(0)+'K views':v+' views';
const F=['height','container','aformat','abitrate','alang','folder','plmode','meta','thumb','sb','subs','sublangs','autosubs','subembed','items','ratelimit','cookies','proxy','template','player'];
const val=id=>{const e=$(id);return e.type==='checkbox'?e.checked:e.value};
function opts(){const o={mode};F.forEach(k=>o[k]=val(k));o.vlc=o.player==='vlc';return o}
function save(){localStorage.setItem('ytgrab_smart',JSON.stringify(opts()))}
function load(){try{const s=JSON.parse(localStorage.getItem('ytgrab_smart')||'{}');if(s.mode)setMode(s.mode);F.forEach(k=>{if(s[k]===undefined||k==='folder'&&!s[k])return;const e=$(k);if(!e)return;e.type==='checkbox'?e.checked=s[k]:e.value=s[k]})}catch(e){}$('subo').hidden=!$('subs').checked}

function setMode(m){mode=m;document.querySelectorAll('#mode button').forEach(b=>b.classList.toggle('on',b.dataset.m===m));$('vopts').hidden=m!=='video';$('aopts').hidden=m!=='audio'}
document.querySelectorAll('#mode button').forEach(b=>b.onclick=()=>{setMode(b.dataset.m);save()});

/* Input Mode: Smart Search vs Direct URL */
document.querySelectorAll('#inmode button').forEach(b=>b.onclick=()=>{
  inMode=b.dataset.in;
  document.querySelectorAll('#inmode button').forEach(x=>x.classList.toggle('on',x.dataset.in===inMode));
  $('search-pane').hidden=inMode!=='search';
  $('url-pane').hidden=inMode!=='url';
});

/* Search Category Selector */
document.querySelectorAll('#scat button').forEach(b=>b.onclick=()=>{
  scat=b.dataset.c;
  document.querySelectorAll('#scat button').forEach(x=>x.classList.toggle('on',x.dataset.c===scat));
});

/* Quick Preset Pills */
document.querySelectorAll('#presets button').forEach(b=>b.onclick=()=>{
  $('sq-in').value=b.dataset.q;
  scat=b.dataset.c;
  document.querySelectorAll('#scat button').forEach(x=>x.classList.toggle('on',x.dataset.c===scat));
  if(b.dataset.l)$('alang').value=b.dataset.l;
  runSmartSearch();
});

/* Execute Smart Deduplicated Search */
async function runSmartSearch(){
  const q=$('sq-in').value.trim();
  if(!q)return toast('Type a movie name, artist, or mood first, sir.');
  $('sres-wrap').hidden=false;
  $('sres-count').textContent='J.A.R.V.I.S. scanning & removing duplicate uploads...';
  $('sres-list').innerHTML='<div class="mute sm" style="padding:18px;text-align:center">Searching YouTube & filtering duplicate songs...</div>';
  try{
    const r=await post('/api/search',{query:q,category:scat,lang:val('alang'),cookies:val('cookies'),proxy:val('proxy')});
    searchResults=r.results||[];
    $('sres-count').textContent=`${searchResults.length} unique track(s) found (duplicates removed)`;
    if(!searchResults.length){
      $('sres-list').innerHTML='<div class="mute sm" style="padding:18px;text-align:center">No matching tracks found.</div>';
      return;
    }
    say(`Found ${searchResults.length} unique tracks for ${q}, sir. Select the ones you wish to acquire.`);
    $('sres-list').innerHTML=searchResults.map((it,idx)=>`
      <label class="sitem">
        <input type="checkbox" class="schk" data-idx="${idx}" ${idx<3?'checked':''}>
        <img src="${esc(it.thumb)}" referrerpolicy="no-referrer">
        <div class="sinfo">
          <b title="${esc(it.title)}">${esc(it.title)}</b>
          <span>${esc(it.uploader)}${it.duration?' &middot; '+dur(it.duration):''}${it.views?' &middot; '+fmtViews(it.views):''}</span>
        </div>
      </label>
    `).join('');
  }catch(e){
    $('sres-count').textContent='Search error';
    $('sres-list').innerHTML=`<div class="err" style="padding:12px">${esc(e.message)}</div>`;
  }
}
$('sbtn').onclick=runSmartSearch;
$('sq-in').addEventListener('keydown',e=>{if(e.key==='Enter')runSmartSearch()});
$('sel-all').onclick=()=>document.querySelectorAll('.schk').forEach(c=>c.checked=true);
$('sel-none').onclick=()=>document.querySelectorAll('.schk').forEach(c=>c.checked=false);
$('sel-top5').onclick=()=>document.querySelectorAll('.schk').forEach((c,i)=>c.checked=i<5);

document.querySelectorAll('input,select').forEach(e=>e.addEventListener('change',()=>{$('subo').hidden=!$('subs').checked;save()}));
/* theme + voice */
const themes=['arc','mark','ultron'],tnames={arc:'Arc',mark:'Mark III',ultron:'Ultron'};
document.documentElement.dataset.t=localStorage.ytt||'arc';
const tlabel=()=>$('theme').textContent='Theme: '+(tnames[document.documentElement.dataset.t]||'Arc');tlabel();
$('theme').onclick=()=>{const d=document.documentElement;d.dataset.t=themes[(themes.indexOf(d.dataset.t)+1)%themes.length];localStorage.ytt=d.dataset.t;tlabel()};
const vlabel=()=>$('voice').textContent='Voice: '+(voice?'ON':'OFF');vlabel();
const say=t=>{if(voice&&'speechSynthesis'in window){const u=new SpeechSynthesisUtterance(t);u.pitch=.85;speechSynthesis.cancel();speechSynthesis.speak(u)}};
$('voice').onclick=()=>{voice=!voice;localStorage.yv=voice?'1':'0';vlabel();say('J.A.R.V.I.S. voice enabled, sir.')};
function beep(){try{const a=new (window.AudioContext||window.webkitAudioContext)();[880,1320].forEach((f,i)=>{const o=a.createOscillator(),g=a.createGain(),t=a.currentTime+i*.12;o.type='sine';o.frequency.value=f;g.gain.setValueAtTime(.0001,t);g.gain.exponentialRampToValueAtTime(.12,t+.02);g.gain.exponentialRampToValueAtTime(.0001,t+.25);o.connect(g);g.connect(a.destination);o.start(t);o.stop(t+.3)})}catch(e){}}
const hr=new Date().getHours();$('greet').textContent=(hr<12?'Good morning':hr<17?'Good afternoon':'Good evening')+', sir. All systems nominal.';
/* boot */
(function(){const b=$('boot');if(sessionStorage.getItem('yb')){b.remove();return}sessionStorage.setItem('yb','1');
const L=['> J.A.R.V.I.S. // SMART DISCOVERY BOOT','> LOADING yt-dlp & DEDUPLICATION ENGINE ... OK','> LINKING FFMPEG MULTI-LANGUAGE PIPELINE .. OK','> STARK INDUSTRIES MEDIA SYSTEM ONLINE','> '+$('greet').textContent];
let i=0;const end=()=>{const x=$('boot');if(!x)return;x.classList.add('off');setTimeout(()=>x.remove(),800)};
const t=setInterval(()=>{if(i<L.length)$('bt').textContent+=L[i++]+'\n';else{clearInterval(t);setTimeout(end,600)}},380);
b.onclick=()=>{clearInterval(t);end()}})();
/* glass spotlight */
let raf=0;document.addEventListener('pointermove',e=>{if(raf)return;raf=requestAnimationFrame(()=>{raf=0;document.querySelectorAll('.glass').forEach(g=>{const r=g.getBoundingClientRect();g.style.setProperty('--mx',(e.clientX-r.left)+'px');g.style.setProperty('--my',(e.clientY-r.top)+'px')})})});
/* link kinds */
function kind(u){if(previewKind[u])return previewKind[u];try{const x=new URL(u);const list=x.searchParams.get('list');const v=x.searchParams.get('v')||(x.hostname.includes('youtu.be')&&x.pathname.length>1)||/\/(shorts|live)\//.test(x.pathname);
if(list&&v)return'both';if(list||/\/(playlist|sets|album)\b/i.test(x.pathname))return'playlist';return'single'}catch(e){return'single'}}
const links=()=>$('urls').value.split('\n').map(s=>s.trim()).filter(Boolean);
function ask(u,left,k){return new Promise(res=>{$('mu').textContent=u;$('mprompt').textContent=k==='playlist'?'This is a playlist link:':'This link is a video that belongs to a playlist:';$('mleft').hidden=left<1;$('mallt').textContent=left;$('mall').checked=false;$('modal').classList.add('on');say('Playlist detected, sir. Shall I download the single video or the whole playlist?');
const fin=c=>{$('modal').classList.remove('on');res({c,all:$('mall').checked})};$('m1').onclick=()=>fin(false);$('m2').onclick=()=>fin(true);$('mx').onclick=()=>fin(null);window.__askCancel=()=>fin(null)})}
const closeAbout=()=>$('about').classList.remove('on');
$('aboutb').onclick=$('aboutf').onclick=()=>$('about').classList.add('on');$('ax').onclick=closeAbout;
$('about').onclick=e=>{if(e.target===$('about'))closeAbout()};
document.querySelectorAll('[data-copy]').forEach(a=>a.addEventListener('click',()=>{navigator.clipboard?.writeText(a.dataset.copy);toast('Copied '+a.dataset.copy)}));
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeAbout();if($('modal').classList.contains('on')&&window.__askCancel)window.__askCancel()}});
$('paste').onclick=async()=>{try{const t=await navigator.clipboard.readText();$('urls').value=($('urls').value.trim()+'\n'+t).trim()}catch(e){toast('Clipboard blocked: paste with Ctrl+V')}};
$('fetch').onclick=async()=>{const u=links()[0];if(!u)return toast('Paste a link first');const k=kind(u);
$('prev').innerHTML='<div class="prev mute">Scanning target...</div>';
try{const i=await post('/api/info',{url:u,cookies:val('cookies'),proxy:val('proxy'),playlist:k==='playlist'});
if(i.is_playlist&&k==='single')previewKind[u]='playlist';
const isPl=k!=='single'||i.is_playlist;
$('prev').innerHTML=`<div class="prev">${i.thumb?`<img src="${esc(i.thumb)}" referrerpolicy="no-referrer">`:''}<div><b>${esc(i.title)}</b><span class="mute sm">${esc(i.uploader||'')}${i.is_playlist?` - playlist, ${i.count} videos`:` - ${dur(i.duration)}`}</span>${i.heights.length?`<div class="sm mute">Up to ${i.heights[0]}p available</div>`:''}${i.langs&&i.langs.length?`<div class="sm mute">Audio tracks: ${esc(i.langs.join(', '))}</div>`:''}${isPl?'<div class="sm gold">Playlist detected - J.A.R.V.I.S. will ask whether to download single video or playlist</div>':''}</div></div>`}
catch(e){$('prev').innerHTML=`<div class="prev err">${esc(e.message)}</div>`}};

$('go').onclick=async()=>{
  if('Notification'in window&&Notification.permission==='default')Notification.requestPermission();
  const items=[];
  if(inMode==='search'){
    const chks=[...document.querySelectorAll('.schk:checked')];
    if(!chks.length)return toast('Search for a movie/artist and check at least one song first, sir.');
    chks.forEach(c=>{
      const it=searchResults[Number(c.dataset.idx)];
      if(it)items.push({url:it.url,title:it.title,thumb:it.thumb,playlist:false});
    });
  }else{
    const urls=links();if(!urls.length)return toast('Paste at least one link');
    let sticky=null;
    const plLeft=idx=>urls.slice(idx+1).filter(x=>kind(x)!=='single').length;
    for(let i=0;i<urls.length;i++){const u=urls[i],k=kind(u);let pl=false;
    if(k!=='single'){const m=$('plmode').value;
    if(m==='single')pl=false;else if(m==='playlist')pl=true;else if(sticky!==null)pl=sticky;
    else{const r=await ask(u,plLeft(i),k);if(r.c===null)return;pl=r.c;if(r.all)sticky=pl}}
    items.push({url:u,playlist:pl})}
  }
  try{
    await post('/api/download',{items,opts:opts()});
    if(inMode==='url'){$('urls').value='';$('prev').innerHTML=''}
    toast(items.length+' track(s) added to the queue');
    refresh();
  }catch(e){toast(e.message)}
};
$('urls').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))$('go').click()});
$('openf').onclick=()=>post('/api/open',{folder:$('folder').value}).catch(e=>toast(e.message));
$('clear').onclick=()=>post('/api/clear').then(refresh);
$('upd').onclick=async()=>{toast('Updating yt-dlp...');try{toast((await post('/api/update')).msg)}catch(e){toast(e.message)}};
$('jobs').onclick=e=>{const b=e.target.closest('button');if(!b)return;const id=b.dataset.id,a=b.dataset.a;
if(a==='play')post('/api/play',{id,vlc:$('player').value==='vlc'}).then(r=>toast('Playing in '+r.player)).catch(x=>toast(x.message));
else post('/api/'+a,{id}).then(refresh).catch(x=>toast(x.message))};
const label=j=>({queued:'Waiting...',downloading:`${j.percent.toFixed(1)}%${j.speed?' - '+mb(j.speed):''}${j.eta!=null?' - '+dur(j.eta)+' left':''}`,processing:'Converting and merging...',done:'Finished',error:'Failed',cancelled:'Cancelled'}[j.status]);
function render(jobs){const sig=JSON.stringify(jobs);if(sig===lastSig)return;lastSig=sig;
$('jobs').innerHTML=jobs.length?jobs.map(j=>{const live=['queued','downloading','processing'].includes(j.status);
const p=j.status==='processing'||j.status==='done'?100:Math.round(j.percent);const col=j.status==='done'?'var(--ok)':(j.status==='error'||j.status==='cancelled')?'var(--bad)':'var(--acc)';const n=j.files.length;
const langTag=j.alang&&j.alang!=='orig'?' ['+j.alang.toUpperCase()+']':'';
return`<div class="job ${j.status}">${j.thumb?`<img src="${esc(j.thumb)}" referrerpolicy="no-referrer">`:'<img>'}<div class="jb"><div class="jt" title="${esc(j.title)}">${esc(j.title)}</div>
<div class="bar2"><i style="width:${p}%"></i></div><div class="sm mute">${j.part?'Item '+j.part+' - ':''}${label(j)} - ${j.mode==='audio'?'Audio':'Video'}${langTag}${j.playlist?' - Playlist':''}</div>
${j.error?`<div class="err">${esc(j.error)}</div>`:''}<div class="acts">${live?`<button class="ic" data-a="cancel" data-id="${j.id}">Cancel</button>`:''}
${j.status==='done'?`<button class="ic play" data-a="play" data-id="${j.id}">&#9654; ${n>1?'Play all ('+n+')':'Play'}</button><button class="ic" data-a="reveal" data-id="${j.id}">Show in folder</button>`:''}${j.status==='error'||j.status==='cancelled'?`<button class="ic" data-a="retry" data-id="${j.id}">Retry</button>`:''}</div></div>
<div class="ring" style="--p:${p};--c:${col}"><span>${j.status==='done'?'&#10003;':j.status==='error'?'!':p+'%'}</span></div></div>`}).join(''):'<div class="empty"><div class="reactor big"></div>The queue is empty, sir. Search a movie/artist or paste a link to get started.</div>';
document.querySelectorAll('#jobs .reactor').forEach(e=>e.innerHTML=REACTOR)}
function celebrate(j){beep();say('Download complete, sir.');toast('Ready: '+j.title);
if('Notification'in window&&Notification.permission==='granted'&&document.hidden)new Notification('YTGrab - download complete',{body:j.title})}
const chip=(id,txt,on)=>{const e=$(id);e.textContent=txt;e.classList.toggle('ok',on)};
async function refresh(){try{const s=await fetch('/api/state').then(r=>r.json());cfg=s.config;if(!$('folder').value)$('folder').value=cfg.folder;
$('ver').textContent='yt-dlp '+cfg.version;$('warn').innerHTML=cfg.ffmpeg?'':'<div class="warn">FFmpeg was not found. Merging video+audio and MP3 conversion need it. Install it with <code>winget install ffmpeg</code>, then restart YTGrab.</div>';
chip('cf',cfg.ffmpeg?'FFMPEG ONLINE':'FFMPEG MISSING',cfg.ffmpeg);chip('cv',cfg.vlc?'VLC DETECTED':'VLC NOT FOUND - USING SYSTEM PLAYER',cfg.vlc);
const J=s.jobs;J.forEach(j=>{const o=prev[j.id];if(o&&o!==j.status){if(j.status==='done')celebrate(j);else if(j.status==='error')toast('Failed: '+j.title)}prev[j.id]=j.status});
$('sa').textContent=J.filter(j=>j.status==='downloading'||j.status==='processing').length;$('sq').textContent=J.filter(j=>j.status==='queued').length;$('sd').textContent=J.filter(j=>j.status==='done').length;
$('ss').textContent=mb(J.reduce((a,j)=>a+(j.status==='downloading'?j.speed||0:0),0));$('sf').textContent=cfg.disk_free!=null?cfg.disk_free+' GB':'--';render(J)}catch(e){}}
load();refresh().then(()=>{if(!$('folder').value)$('folder').value=cfg.folder});setInterval(refresh,800);
</script></body></html>"""


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"YTGrab Smart Discovery running at {url}  (Ctrl+C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
