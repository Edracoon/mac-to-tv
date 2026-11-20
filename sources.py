"""mac-to-tv: where the videos come from. Links to video sites (yt-dlp), torrents (TorrServer), sources returned by
Stremio add-ons. Nothing here knows about the TV or the page; server.py decides what to do with a source.

Tests: tests/unit/test_sources.py
"""
import atexit
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"  # ponytail: never purged, empty the folder by hand if the disk fills up


def step(label, detail=""):
    """The stage a launch has reached ("Reading the link"…). server.py puts its own function here, which shows the
    stage in the page and stops a launch that was replaced by a newer one."""


# The kinds of files we handle, with the type the TV advertises for each in GetProtocolInfo.
MIME = {
    ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".avi": "video/avi", ".ts": "video/vnd.dlna.mpeg-tts", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-asf", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/vnd.dlna.adts", ".wav": "audio/wav", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def about(d):
    """What the page shows under the player about the video: short texts only, whatever was sent."""
    limits = {"description": 3000, "info": 300, "credits": 400, "poster": 600, "link": 600, "film": 40}
    out = {k: str(d[k])[:n] for k, n in limits.items() if isinstance(d, dict) and d.get(k) and isinstance(d[k], (str, int))}
    for k in ("poster", "link"):
        if k in out and not re.match(r"https?://", out[k]):
            del out[k]
    return out


# --- Links: the videos played lately (history.json) and YouTube search, for the "YouTube" tab of the page

def history_entry(url, d):
    return {"url": url, "title": d.get("title") or "Video", "channel": d.get("channel") or d.get("uploader") or "",
            "duration": int(d.get("duration") or 0), "thumbnail": d.get("thumbnail") or ""}


class History:
    """The links played lately, most recent first, kept in a JSON file."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def links(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def remember(self, url, d, keep=40):
        """Puts the link at the top of the history; a link played again moves up instead of appearing twice."""
        with self.lock:
            rest = [h for h in self.links() if h.get("url") != url]
            self.path.write_text(json.dumps([history_entry(url, d) | {"played": int(time.time())}, *rest][:keep], indent=1, ensure_ascii=False))


history = History(HERE / "history.json")


def yt_search(q, n=18):
    q = " ".join(str(q).split())[:200]
    if not q:
        raise ValueError("empty search")
    r = subprocess.run(["yt-dlp", "--flat-playlist", "--quiet", "--no-warnings", "-J", "--", f"ytsearch{n}:{q}"],
                       capture_output=True, text=True, timeout=45)
    if r.returncode:
        raise ValueError("YouTube search failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    return {"results": [history_entry(f"https://www.youtube.com/watch?v={e['id']}", e | {"thumbnail": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"})
                        for e in json.loads(r.stdout).get("entries") or [] if e.get("id")]}


def link_info(url):
    url, _ = norm_link(url)
    step("Reading the link")
    r = subprocess.run(["yt-dlp", "--no-playlist", "--quiet", "--no-warnings", "-f", FORMAT, "-j", "--", url],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError("yt-dlp failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    d = json.loads(r.stdout)
    # YouTube rejects (403) the addresses until the ad delay is over: yt-dlp gives the time from which they work
    ready = max((f.get("available_at") or 0) for f in d.get("requested_formats") or [d])
    if ready > time.time():
        step("Waiting for the video to be released")
    time.sleep(min(max(ready - time.time() + 0.5, 0), 30))
    history.remember(url, d)
    day = str(d.get("upload_date") or "")
    facts = [d.get("channel") or d.get("uploader"), f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else "",
             f"{d['view_count']:,} views".replace(",", " ") if isinstance(d.get("view_count"), int) else ""]
    return {"title": d.get("title") or "Video", "duration": int(d.get("duration") or 0), "thumbnail": d.get("thumbnail"),
            "about": about({"description": d.get("description"), "info": " · ".join(f for f in facts if f),
                            "poster": d.get("thumbnail"), "link": d.get("webpage_url") or url}),
            "offset": 0,
            "inputs": [(f["url"], "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items()))
                       for f in d.get("requested_formats") or [d]]}


# --- Torrents

def is_torrent(link):
    link = link.strip()
    return (link.startswith("magnet:") or bool(re.fullmatch(r"[0-9a-fA-F]{40}", link))
            or bool(re.match(r"https?://", link)) and link.split("?")[0].endswith(".torrent"))


class TorrServer:
    """The torrent engine: bin/TorrServer, started with us, listening locally only. It downloads the pieces in
    playback order, in memory; its HTTP stream is then read like a file. One torrent at a time (see only())."""

    URL = "http://127.0.0.1:8090"
    CACHE = 512 << 20  # bytes of the movie it may hold in memory ahead of playback: the buffer you build by pausing

    def start(self):
        exe, data = HERE / "bin" / "TorrServer", HERE / "torrserver-data"
        if not exe.exists():
            return
        data.mkdir(exist_ok=True)
        proc = subprocess.Popen([exe, "--ip", "127.0.0.1", "--port", "8090", "--path", data, "--logpath", data / "ts.log"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(proc.terminate)

        def configure():  # its default buffer is 64 MB: about a minute of a large movie
            for _ in range(20):
                try:
                    sets = self.call("get", _path="/settings")
                    if sets.get("CacheSize") != self.CACHE:
                        self.call("set", _path="/settings", sets={**sets, "CacheSize": self.CACHE})
                    return
                except Exception:
                    time.sleep(0.5)
        threading.Thread(target=configure, daemon=True).start()

    def call(self, action, _path="/torrents", **args):
        req = urllib.request.Request(self.URL + _path, json.dumps({"action": action, **args}).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            raise ValueError("torrent link rejected: " + e.read().decode()[:200])
        except OSError:
            raise RuntimeError("torrent engine unreachable (bin/TorrServer missing or stopped)")

    def only(self, kept):
        """Drops every torrent still held except the hashes in `kept`."""
        try:
            for t in self.call("list"):
                if t.get("hash") and t["hash"] not in kept:
                    self.call("drop", hash=t["hash"])
        except Exception:
            pass

    def file(self, link, file_idx=None, filename=None):
        """Adds the torrent and returns (local stream address, name, size) of the movie.
        File choice: name given by the add-on, else position (Stremio fileIdx, 0-based), else largest video."""
        step("Finding torrent sources")
        h = self.call("add", link=link.strip(), save_to_db=False)["hash"]
        self.only({h})  # one download at a time: it replaces the others, the TV's and the browser's alike
        for _ in range(60):  # the file list arrives once a source has answered
            info = self.call("get", hash=h)
            files = info.get("file_stats") or []
            if files:
                break
            step("Finding torrent sources", f"{info.get('active_peers') or 0} connected")
            time.sleep(1)
        else:
            raise RuntimeError("no source answered within 60 s for this torrent")
        videos = [f for f in files if MIME.get(Path(f["path"]).suffix.lower(), "").startswith("video")]
        if not videos:
            raise ValueError("no video the TV can play in this torrent")
        f = (next((f for f in videos if filename and Path(f["path"]).name == filename), None)
             or next((f for f in videos if file_idx is not None and f["id"] == file_idx + 1), None)
             or max(videos, key=lambda f: f["length"]))
        name = Path(f["path"]).name
        return f"{self.URL}/stream/{quote(name)}?link={h}&index={f['id']}&play", name, f["length"]

    def source(self, url, size):
        """The torrent behind a stream address given by file(): {"hash", "size"}; None for any other address."""
        m = re.match(re.escape(self.URL) + r"/.*[?&]link=([0-9a-f]{40})", url)
        return {"hash": m[1], "size": size or 0} if m else None

    def health(self, src, duration, position=0):
        """For a source read through TorrServer: download speed and speed the movie needs (bytes/s), connected peers,
        and `ahead`: seconds of movie already downloaded after the playback position (the buffer)."""
        h = src["torrent"]["hash"]
        t = self.call("get", hash=h)
        need = int(src["torrent"]["size"] / duration) if duration else 0
        out = {"speed": int(t.get("download_speed") or 0), "need": need, "peers": t.get("active_peers") or 0, "ahead": 0}
        try:
            c = self.call("get", _path="/cache", hash=h)
            done = {int(k) for k, p in (c.get("Pieces") or {}).items() if p.get("Completed")}
            # ponytail: position → piece assumes the movie fills the torrent; fine for single-movie torrents, approximate in packs
            at = int(position / duration * c["PiecesCount"]) if duration else 0
            readers = [r["Reader"] for r in c.get("Readers") or [] if isinstance(r.get("Reader"), int)]
            if readers:
                at = min(readers, key=lambda r: abs(r - at))  # the reader that feeds playback, not a subtitle check elsewhere
            n = 0
            while at + n in done:
                n += 1
            out["ahead"] = int(n * c["PiecesLength"] / need) if need else 0
        except Exception:
            pass
        return out


torrserver = TorrServer()


def add_on_source(a):
    """A source returned by a Stremio add-on, torrent (infoHash) or direct link (url) → (address the Mac can read, name, size)."""
    if a.get("infoHash"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", a["infoHash"]):
            raise ValueError("invalid torrent identifier")
        trackers = [s.split(":", 1)[1] for s in a.get("sources") or [] if isinstance(s, str) and s.startswith("tracker:")]
        link = f"magnet:?xt=urn:btih:{a['infoHash']}" + "".join("&tr=" + quote(t, safe="") for t in trackers)
        return torrserver.file(link, a.get("fileIdx"), a.get("filename"))
    if re.match(r"https?://", a.get("url") or ""):
        return a["url"], Path(urlparse(a["url"]).path).name or "film.mp4", None
    raise ValueError("this source is neither a torrent nor a direct link")


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", url) or re.fullmatch(r"([\w-]{11})", url.strip())
    if not m:
        raise ValueError("unrecognized YouTube link")
    return m[1]


FORMAT = "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[vcodec^=avc1][height<=1080]+ba/b[ext=mp4]/b"  # H.264 + AAC: what the TV always plays


def norm_link(url):
    """Link → (address to give yt-dlp, cache key)."""
    url = url.strip()
    if "youtu" in url or re.fullmatch(r"[\w-]{11}", url):
        vid = youtube_id(url)
        return f"https://www.youtube.com/watch?v={vid}", vid
    if re.match(r"https?://", url):
        return url, hashlib.sha1(url.encode()).hexdigest()[:11]
    raise ValueError("unrecognized link")


def media_fetch(url):
    """Downloads the video of a link into cache/ and returns the file (fallback when live streaming misbehaves)."""
    url, vid = norm_link(url)
    found = sorted(CACHE.glob(f"*[[]{vid}[]].mp4"))
    if found:
        return found[0]
    step("Downloading the video")
    r = subprocess.run(
        ["yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
         "-f", FORMAT,
         "--merge-output-format", "mp4", "--postprocessor-args", "Merger:-movflags +faststart",
         "-o", str(CACHE / f"%(title).80s [{vid}].%(ext)s"), "--", url],
        capture_output=True, text=True, timeout=3600)
    found = sorted(CACHE.glob(f"*[[]{vid}[]].mp4"))
    if r.returncode or not found:
        raise RuntimeError("yt-dlp failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    return found[0]
