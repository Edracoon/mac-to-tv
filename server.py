#!/usr/bin/env python3
"""mac-to-tv: web interface to control a TV on the local network and play videos on it.

    python3 server.py [movies_folder] [--lan]         # default folder: ~/Movies
    → http://127.0.0.1:8765

    python3 server.py --selftest                      # checks the pure functions (no network access)

The TV is picked in the page (local network search); it is remembered in config.json.
Environment variables: PORT (default 8765), TV_IP (forces a TV without going through the page).

Layout:
  - tv.py     : TV discovery, DLNA player, per-brand remote drivers, DIAL apps
  - server.py : video sources (files, links via yt-dlp + ffmpeg, torrents via TorrServer), subtitles,
                playback resume, HTTP API for the index.html page

Security: the interface and the API only answer the Mac itself, or (with --lan) devices on the local network
that present the key. Only /media/<token>/ is open to the local network, so the TV can read the current video.
"""
import atexit
import base64
import bisect
import hashlib
import hmac
import html
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import tv as tvlib

PORT = int(os.environ.get("PORT", "8765"))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
MEDIA_DIR = Path(ARGS[0] if ARGS else "~/Movies").expanduser().resolve()
HERE = Path(__file__).resolve().parent

# Types advertised by the TV in GetProtocolInfo.
MIME = {
    ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".avi": "video/avi", ".ts": "video/vnd.dlna.mpeg-tts", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-asf", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/vnd.dlna.adts", ".wav": "audio/wav", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
}
APPS = ("YouTube",)  # apps we can launch through DIAL
I0 = "<InstanceID>0</InstanceID>"
CACHE = HERE / "cache"  # ponytail: never purged, empty the folder by hand if the disk fills up
sub_url = None  # address of the external subtitles for the current video
shared = {}

# --lan: opens the interface to the local network, protected by a key (lan-key.txt). Without the option: Mac only.
LAN_KEY = None
if "--lan" in sys.argv:
    key_file = HERE / "lan-key.txt"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(9))
    key_file.chmod(0o600)  # other accounts of this Mac must not read the key
    LAN_KEY = key_file.read_text().strip()


# --- TV ---------------------------------------------------------------------

# What the player area of the page shows besides the TV's own state:
#   busy/title/steps : a launch in progress and the stages it went through, each [label, detail]
#   error/detail     : why the last playback failed, in plain words, plus the technical line for the curious
job = {"busy": False, "title": "", "steps": [], "error": "", "detail": "", "stopped": False, "id": 0}
mine = threading.local()  # .id: the launch this thread is running


class Superseded(Exception):
    """This launch was replaced by a newer one: it must stop instead of writing its stages into, or playing over, the new one."""


def step(label, detail=""):
    if getattr(mine, "id", job["id"]) != job["id"]:
        raise Superseded("replaced by a newer launch")
    if job["steps"] and job["steps"][-1][0] == label:
        job["steps"][-1][1] = detail
    else:
        job["steps"].append([label, detail])


def fail(message, detail=""):
    job.update(error=message, detail=detail)


def plain_error(e):
    """An exception raised while launching → (sentence for the player, technical detail)."""
    text = str(e)
    if isinstance(e, ValueError):
        return text[:1].upper() + text[1:] + ".", ""  # already written for the user
    if "yt-dlp failed" in text:
        return "This link could not be read.", text.replace("yt-dlp failed: ", "")
    if "no source answered" in text:
        return "No source answered for this torrent.", ""
    if "torrent engine" in text:
        return "The torrent engine is not running.", text
    if "not found on the network" in text or isinstance(e, OSError):
        return "The TV is not answering. Is it switched on?", text
    return "Something went wrong while starting playback.", text


def launch(title, fn, *args):
    """Runs a launch (link, file, torrent, catalog movie), publishing its stages and how it failed, if it did."""
    mine.id = job["id"] + 1
    job.update(busy=True, title=str(title)[:120], steps=[], error="", detail="", stopped=False, id=mine.id)
    try:
        return fn(*args)
    except Exception as e:
        if mine.id == job["id"] and not isinstance(e, Superseded):
            fail(*plain_error(e))
        raise
    finally:
        if mine.id == job["id"]:  # a replaced launch leaves the player, and the torrent, to its successor
            job["busy"] = False
            only_torrent(keep=current_torrent())  # whatever happened, nothing else keeps downloading
        del mine.id


def watch_start(token):
    """A few seconds after a launch: if the TV is already stopped without having been asked to, say so.
    Typical cause: a video or audio format the TV cannot decode."""
    launched = job["id"]
    time.sleep(12)
    try:
        if launched == job["id"] and not job["busy"] and token in shared and not job["stopped"] and not job["error"]:
            state = soap("AVTransport", "GetTransportInfo", I0).get("CurrentTransportState")
            if state in ("STOPPED", "NO_MEDIA_PRESENT"):
                fail("The TV stopped right away. It probably cannot play this format.")
    except Exception:
        pass


current_tv = None  # tvlib.TV picked in the page (or forced by TV_IP)


def need_tv():
    if not current_tv:
        raise ValueError("no TV selected: pick one by clicking the name at the top of the page")
    return current_tv


def soap(service, action, args="", timeout=4):
    return need_tv().soap(service, action, args, timeout=timeout)


def dial(app, method="GET", body=None):
    return need_tv().dial(app, method, body)


def connect_tv(ip, save=True):
    """Selects the TV at this address (it must have a DLNA network player) and remembers it in config.json."""
    global current_tv
    try:
        if not ipaddress.ip_address(ip).is_private:
            raise ValueError
    except ValueError:
        raise ValueError("invalid IP address")
    t = tvlib.TV(ip)
    try:
        t.refresh()
    except RuntimeError:
        raise ValueError(f"no TV found at {ip} (switched off, or on another network?)")
    if not t.has_player:
        raise ValueError("this TV has no DLNA network player: cannot send it a video")
    current_tv = t
    shared.clear()
    if save:
        save_config({**load_config(), "tv": {"ip": t.ip, "name": t.name}})
    return t.info()


def restore_tv():
    """On startup: restores the TV from TV_IP or config.json, without blocking if it is off (re-read on first use)."""
    global current_tv
    saved = load_config().get("tv") or {}
    ip = os.environ.get("TV_IP") or saved.get("ip")
    if ip:
        current_tv = tvlib.TV(ip, saved.get("name", ""))
        try:
            current_tv.refresh()
        except Exception:
            pass


def about(d):
    """What the page shows under the player about the video: short texts only, whatever was sent."""
    limits = {"description": 3000, "info": 300, "credits": 400, "poster": 600, "link": 600, "film": 40}
    out = {k: str(d[k])[:n] for k, n in limits.items() if isinstance(d, dict) and d.get(k) and isinstance(d[k], (str, int))}
    for k in ("poster", "link"):
        if k in out and not re.match(r"https?://", out[k]):
            del out[k]
    return out


def live_position(live, s=None):
    """Second of the movie a live stream is at. s: what the TV just reported, when it is known to be playing this
    stream; without it, the last position seen, and before any was seen, the point the stream was started at."""
    if s is not None and next(iter(shared), "\0") in s.get("TrackURI", ""):
        live["pos"] = secs(s.get("RelTime")) + live["offset"]
    return live.get("pos", live["offset"])


def status():
    s = {}
    s |= soap("RenderingControl", "GetVolume", I0 + "<Channel>Master</Channel>")
    s |= soap("RenderingControl", "GetMute", I0 + "<Channel>Master</Channel>")
    s |= soap("AVTransport", "GetTransportInfo", I0)
    s |= soap("AVTransport", "GetPositionInfo", I0)
    m = re.search(r"<dc:title>(.*?)</dc:title>", html.unescape(s.pop("TrackMetaData", "")))
    s["title"] = html.unescape(m[1]) if m else ""
    live = current_live()
    if live and next(iter(shared)) not in s.get("TrackURI", ""):
        # The stream was just (re)started and the TV has not switched to it yet: what it reports belongs to the
        # previous stream (or is 0). Reading that as the position sent the movie back to its start.
        s["RelTime"] = hms(live_position(live, s))
        s["TrackDuration"] = hms(live.get("duration") or 0)
        s["title"] = live["title"]
    elif live:
        # the TV counts from the start of the stream and does not know its duration
        if not live.get("duration") and not live.get("probing") and live.get("inputs"):
            # the format check at launch failed (torrent too slow at that moment): without a duration there is no
            # seek bar, no buffer and no "needed" speed, so ask again now that the file is being read
            live["probing"] = True
            def reprobe():
                live["duration"] = probe(live["inputs"][0][0])[0]
                time.sleep(30)  # still unknown: try again later, not at every status request
                live["probing"] = False
            threading.Thread(target=reprobe, daemon=True).start()
        s["RelTime"] = hms(live_position(live, s))
        s["TrackDuration"] = hms(live["duration"])
        s["title"] = live["title"]
    if live and live.get("subs"):
        s["subDelay"] = live.get("sub_delay", 0)
        s["subsNote"] = live.get("note", "")
    src = current()
    if isinstance(src, dict) and src.get("about"):
        s["about"] = src["about"]
    if isinstance(src, dict) and src.get("torrent"):
        try:
            s["torrent"] = torrent_health(src, secs(s.get("TrackDuration")), secs(s.get("RelTime")))
        except Exception:
            pass
    return s


def public_status():
    """status() for the page, without the address the video is served at: that address is for the TV only.
    `playing` changes when the video changes, which is all the page needs to know."""
    s = status()
    uri = s.pop("TrackURI", "")
    s["playing"] = hashlib.sha1(uri.encode()).hexdigest()[:12] if uri else ""
    return s


def apps():
    return {app: need_tv().app_state(app) for app in APPS}


def local_ip():
    """Address of the Mac on the local network (the one it reaches the TV through)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((current_tv.ip if current_tv else "192.0.2.1", 9))  # UDP: no packet sent, just picks the interface
        return s.getsockname()[0]


def share(source, name, subs=None):
    """Publishes a source (file, or live stream) for the TV under a tokenized address; only one at a time.
    subs: .srt file served alongside (same token + "s"); its address is kept in sub_url."""
    global sub_url
    token = secrets.token_urlsafe(16)
    base = f"http://{local_ip()}:{PORT}/media/"
    shared.clear()
    shared[token] = source
    sub_url = None
    if subs:
        shared[token + "s"] = subs
        sub_url = f"{base}{token}s/{quote(subs.name)}"
    return f"{base}{token}/{quote(name)}"


def current():
    return next(iter(shared.values()), None)


def current_live():
    src = current()
    return src if isinstance(src, dict) and "inputs" in src else None


def play_on_tv(url, title, mime, features, size=""):
    kind = {"audio": "audioItem.musicTrack", "image": "imageItem.photo"}.get(mime.split("/")[0], "videoItem")
    subs = ""
    if sub_url:  # the three common ways to advertise external subtitles to a DLNA player
        su = html.escape(sub_url)
        size += f' pv:subtitleFileType="SRT" pv:subtitleFileUri="{su}"'
        subs = (f'<res protocolInfo="http-get:*:text/srt:*">{su}</res>'
                f'<sec:CaptionInfoEx sec:type="srt">{su}</sec:CaptionInfoEx><sec:CaptionInfo sec:type="srt">{su}</sec:CaptionInfo>')
    didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:sec="http://www.sec.co.kr/" xmlns:pv="http://www.pv.com/pvns/">'
            '<item id="0" parentID="-1" restricted="1">'
            f'<dc:title>{html.escape(title)}</dc:title><upnp:class>object.item.{kind}</upnp:class>'
            f'<res protocolInfo="http-get:*:{mime}:{features}"{size}>{html.escape(url)}</res>{subs}</item></DIDL-Lite>')
    step("Starting the TV")
    try:
        soap("AVTransport", "Stop", I0)
    except Exception:
        pass  # nothing playing: the TV may refuse Stop
    # The TV answers these two only once it has opened the video: over a torrent, on a large file, that can take
    # tens of seconds (it fetches the start and the end of the file first). Status polls keep their short timeout.
    soap("AVTransport", "SetAVTransportURI",
         f"{I0}<CurrentURI>{html.escape(url)}</CurrentURI><CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>", timeout=45)
    soap("AVTransport", "Play", I0 + "<Speed>1</Speed>", timeout=45)
    threading.Thread(target=watch_start, args=(next(iter(shared), ""),), daemon=True).start()


def allowed_file(path, suffixes):
    """The only files a request may make the server read: inside the movies folder or our own cache, of a known type.
    A device holding the network key must not be able to reach the rest of the Mac. Symlinks and ".." are resolved
    first, so a link inside the movies folder that points elsewhere is refused too."""
    p = Path(str(path)).expanduser().resolve()
    if not any(p.is_relative_to(root) for root in (MEDIA_DIR, CACHE.resolve())):
        raise ValueError("this file is outside the movies folder")
    if p.suffix.lower() not in suffixes or not p.is_file():
        raise ValueError("file not found, or not a media file")
    return p


def cast(path, burn=None):
    """burn: .srt file to burn into the picture (the video then goes through the live stream)."""
    p = allowed_file(path, MIME)
    mime = MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "video/mp4"
    title = re.sub(r" \[[\w-]{11}\]$", "", p.stem)
    if burn:
        burn = allowed_file(burn, {".srt"})
    duration, _, vcodec, acodec = probe(str(p)) if mime.startswith("video") else (0, None, "", "")
    convert = needs_convert(vcodec, acodec)
    if burn or convert:
        return start_live({"title": title, "duration": duration, "thumbnail": None, "offset": 0,
                           "inputs": [(str(p), "")], "subs": burn, "convert": convert})
    srt = p.with_suffix(".srt")
    play_on_tv(share(p, p.name, srt if srt.is_file() else None), title, mime, DLNA_FLAGS, f' size="{p.stat().st_size}"')
    return {"title": title}


# --- Live stream: yt-dlp provides the addresses, ffmpeg muxes picture + sound on the fly (without re-encoding)

# --- Links: the videos played lately (history.json) and YouTube search, for the "YouTube" tab of the page

HISTORY = HERE / "history.json"
history_lock = threading.Lock()


def link_history():
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def history_entry(url, d):
    return {"url": url, "title": d.get("title") or "Video", "channel": d.get("channel") or d.get("uploader") or "",
            "duration": int(d.get("duration") or 0), "thumbnail": d.get("thumbnail") or ""}


def remember_link(url, d, keep=40):
    """Puts the link at the top of the history; a link played again moves up instead of appearing twice."""
    with history_lock:
        rest = [h for h in link_history() if h.get("url") != url]
        HISTORY.write_text(json.dumps([history_entry(url, d) | {"played": int(time.time())}, *rest][:keep], indent=1, ensure_ascii=False))


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
    remember_link(url, d)
    day = str(d.get("upload_date") or "")
    facts = [d.get("channel") or d.get("uploader"), f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else "",
             f"{d['view_count']:,} views".replace(",", " ") if isinstance(d.get("view_count"), int) else ""]
    return {"title": d.get("title") or "Video", "duration": int(d.get("duration") or 0), "thumbnail": d.get("thumbnail"),
            "about": about({"description": d.get("description"), "info": " · ".join(f for f in facts if f),
                            "poster": d.get("thumbnail"), "link": d.get("webpage_url") or url}),
            "offset": 0,
            "inputs": [(f["url"], "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items()))
                       for f in d.get("requested_formats") or [d]]}


# brew install ffmpeg-full: the standard ffmpeg lacks the subtitles filter (/opt/homebrew = Apple Silicon, /usr/local = Intel)
FFMPEG_SUBS = next((p for p in ("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg") if os.path.exists(p)), "ffmpeg")


def live_cmd(live):
    cmd = [FFMPEG_SUBS if live.get("subs") else "ffmpeg", "-v", "error"]
    for u, headers in live["inputs"]:
        if u.startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-headers", headers]
            if not u.startswith(TS):
                # YouTube throttles a video asked for in one request to about its playback speed (measured: 240 s of
                # 1080p60 not delivered in 100 s, against 5 s in 10 MB requests), which stutters on busy scenes
                cmd += ["-request_size", "10M"]
        if live["offset"]:
            cmd += ["-ss", str(live["offset"])]
        cmd += ["-i", u]
    for i in range(len(live["inputs"])):
        cmd += ["-map", f"{i}:v?", "-map", f"{i}:a?"]
    if live.get("convert") and not live.get("subs"):
        # A format the TV cannot decode: re-encode only what is needed, copy the rest
        video = ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-pix_fmt", "yuv420p"] if "video" in live["convert"] else ["-c:v", "copy"]
        audio = ["-c:a", "aac", "-ac", "2", "-b:a", "192k"] if "audio" in live["convert"] else ["-c:a", "copy"]
        return cmd + video + audio + ["-f", "mpegts", "pipe:1"]
    if live.get("subs"):
        # The TV does not display external subtitles: we burn them into the picture (hardware re-encoding, ~8x real time).
        # setpts: after a seek, the picture restarts from 0; we shift it so the subtitles land at the right time.
        srt = str(live["subs"]).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        return cmd + ["-vf", f"setpts=PTS+({live['offset'] - live.get('sub_delay', 0)})/TB,subtitles='{srt}',setpts=PTS-STARTPTS",
                      "-c:v", "h264_videotoolbox", "-b:v", "8M", "-c:a", "aac", "-ac", "2", "-b:a", "192k",  # stereo AAC sound: always plays
                      "-f", "mpegts", "pipe:1"]
    return cmd + ["-c", "copy", "-f", "mpegts", "pipe:1"]


def start_live(live):
    """(Re)starts the stream on the TV at live["offset"]: a stream has no middle, seeking = restarting from there."""
    live["pos"] = live["offset"]  # until the TV plays the new stream, this is where we are
    play_on_tv(share(live, "stream.ts"), live["title"], "video/mpeg", LIVE_FLAGS)
    return {"title": live["title"]}


def stream_link(url):
    return start_live(link_info(url))


# --- Torrents: TorrServer (bin/TorrServer, started with us, listens locally only) downloads the
# pieces in playback order, in memory; we relay its HTTP stream to the TV like a file.

TS = "http://127.0.0.1:8090"


def local_names():
    """Names this Mac answers to on the local network: stable addresses for phones, unlike the IP of a laptop."""
    names = [socket.gethostname().lower()]  # name known to the router's DNS, e.g. "macbook-air.home"
    try:  # Bonjour name (macOS), e.g. "macbook-air.local"
        r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            names.insert(0, r.stdout.strip().lower() + ".local")
    except OSError:
        pass
    return list(dict.fromkeys(names))


LOCAL_NAMES = local_names()


def lan_links():
    """Addresses that open the interface from another device, access key included; empty without --lan."""
    return [f"http://{host}:{PORT}/?key={LAN_KEY}" for host in (local_ip(), *LOCAL_NAMES)] if LAN_KEY else []


def qr_png(text):
    """QR code of `text` as a PNG, drawn by ffmpeg-full (already needed for subtitles): no extra dependency."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:  # a file, so the address needs no escaping in the filter
        f.write(text)
        f.flush()
        r = subprocess.run([FFMPEG_SUBS, "-v", "error", "-f", "lavfi", "-i", f"qrencodesrc=textfile={f.name}:q=480:Q=560",
                            "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"], capture_output=True, timeout=20)
    if r.returncode or not r.stdout:
        raise LookupError
    return r.stdout


def tidy():
    """Startup housekeeping, so the server can run for months: empty an oversized log, drop cache files older than
    a week (downloads, thumbnails, subtitles), and stop a TorrServer left behind by a server that was killed."""
    log = HERE / "server.log"
    if log.exists() and log.stat().st_size > 5 << 20:
        log.write_text("")
    week_ago = time.time() - 7 * 86400
    for f in CACHE.rglob("*"):
        if f.is_file() and f.stat().st_mtime < week_ago:
            f.unlink()
    subprocess.run(["pkill", "-f", str(HERE / "bin" / "TorrServer")], capture_output=True)


TORRENT_CACHE = 512 << 20  # bytes of the movie TorrServer may hold in memory ahead of playback: the buffer you build by pausing


def start_torrserver():
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
                sets = ts("get", _path="/settings")
                if sets.get("CacheSize") != TORRENT_CACHE:
                    ts("set", _path="/settings", sets={**sets, "CacheSize": TORRENT_CACHE})
                return
            except Exception:
                time.sleep(0.5)
    threading.Thread(target=configure, daemon=True).start()


def ts(action, _path="/torrents", **args):
    req = urllib.request.Request(TS + _path, json.dumps({"action": action, **args}).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        raise ValueError("torrent link rejected: " + e.read().decode()[:200])
    except OSError:
        raise RuntimeError("torrent engine unreachable (bin/TorrServer missing or stopped)")


def is_torrent(link):
    link = link.strip()
    return (link.startswith("magnet:") or bool(re.fullmatch(r"[0-9a-fA-F]{40}", link))
            or bool(re.match(r"https?://", link)) and link.split("?")[0].endswith(".torrent"))


def current_torrent():
    src = current()
    return src["torrent"]["hash"] if isinstance(src, dict) and src.get("torrent") else None


def only_torrent(keep=None):
    """One download at a time, and none in the background: drops every torrent TorrServer still holds except `keep`.
    Called before a torrent starts, after every launch (successful or not) and when playback is stopped."""
    try:
        for t in ts("list"):
            if t.get("hash") and t["hash"] != keep:
                ts("drop", hash=t["hash"])
    except Exception:
        pass


def torrent_file(link, file_idx=None, filename=None):
    """Adds the torrent to TorrServer and returns (local stream address, name, size) of the movie.
    File choice: name given by the add-on, else position (Stremio fileIdx, 0-based), else largest video."""
    step("Finding torrent sources")
    h = ts("add", link=link.strip(), save_to_db=False)["hash"]
    only_torrent(keep=h)
    for _ in range(60):  # the file list arrives once a source has answered
        info = ts("get", hash=h)
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
    return f"{TS}/stream/{quote(name)}?link={h}&index={f['id']}&play", name, f["length"]


def current_position(live):
    try:
        status()  # refreshes live["pos"] when the TV is playing this stream
    except Exception:
        pass  # TV busy restarting: the last position seen stays valid
    return live_position(live)


def sub_delay(a):
    """Shifts the burned-in subtitles by a["delta"] seconds (positive = later): the stream restarts at the same spot.
    The subtitle file does not always match the release of the movie; the shift is remembered for resume."""
    live = current_live()
    if not (live and live.get("subs")):
        raise ValueError("no burned-in subtitles currently playing")
    live["sub_delay"] = round(min(max(live.get("sub_delay", 0) + float(a["delta"]), -120), 120), 1)
    live["offset"] = current_position(live)
    with progress_lock:
        p = progress.get(live.get("film"))
        if p:
            p["play"]["subDelay"] = live["sub_delay"]
            save_progress()
    start_live(live)
    return {"subDelay": live["sub_delay"]}


# --- Automatic subtitle check ---
# An add-on's .srt is often timed for another release of the movie. The page already ranks the files by
# similarity between their release name and the video file name. Here we check: when the video file contains
# a text subtitle track (any language, necessarily right), we read two short excerpts of it (10% and
# 70% of the movie) and look, for each candidate, for a shift + a speed (usual frame-rate ratios) that fits
# both excerpts at once. Timings alone are a weak signal: we only act if one candidate clearly stands out.

TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
SRT_CUE = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})[ \t]*-->[ \t]*(\d+):(\d\d):(\d\d)[,.](\d{1,3})[^\n]*\n(.*?)(?=\n[ \t]*\n|\Z)", re.S)


def parse_srt(text):
    t = lambda h, m, sec, ms: int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000
    return [(t(*m.groups()[:4]), t(*m.groups()[4:8]), m[9]) for m in SRT_CUE.finditer(text.replace("\r\n", "\n"))]


def write_srt(cues):
    def stamp(x):
        ms = round(max(x, 0) * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    return "\n".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{x}\n" for i, (a, b, x) in enumerate(cues, 1))


# What the TV decodes. Video checked on the Grundig Ultralogic 4K with generated clips (2026-11-18): H.264 and HEVC
# (8 and 10 bit) play, AV1 makes the TV stop at once. Audio cannot be checked from here (a silent movie still
# "plays"): AAC, MP3 and Dolby Digital are assumed fine; DTS, TrueHD, FLAC, Opus… are converted.
# ponytail: one list for every TV; move it to the TV description in tv.py when a second TV disagrees.
TV_VIDEO = {"h264", "hevc", "mpeg2video", "mpeg4"}
TV_AUDIO = {"aac", "mp3", "mp2", "ac3", "eac3"}


def needs_convert(vcodec, acodec):
    """Which parts of a file have to be re-encoded for the TV: a list among "video", "audio"."""
    return [kind for kind, codec, ok in (("video", vcodec, TV_VIDEO), ("audio", acodec, TV_AUDIO)) if codec and codec not in ok]


def probe(url):
    """(duration in s, index of the first text subtitle track or None, video codec, audio codec) — "" when unknown."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name", "-of", "json", url],
                           capture_output=True, text=True, timeout=90)
        d = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError):
        d = {}
    streams = d.get("streams", [])
    tracks = [st.get("codec_name") for st in streams if st.get("codec_type") == "subtitle"]
    first = lambda kind: next((st.get("codec_name") or "" for st in streams
                               if st.get("codec_type") == kind and st.get("codec_name") not in ("mjpeg", "png")), "")  # skip cover art
    return (int(float(d.get("format", {}).get("duration") or 0)), next((i for i, c in enumerate(tracks) if c in TEXT_SUBS), None),
            first("video"), first("audio"))


def ref_window(url, track, start, length=120, patience=90, alive=lambda: True):
    """Cue start times of the embedded track between start and start+length.
    This part of the movie has to be read (torrent speed: ~25 s in 4K, a few seconds in 1080p). ffmpeg does not stop
    by itself on a subtitle track: we read its output as it comes and kill it as soon as the excerpt is complete,
    after `patience` seconds, or as soon as `alive()` says the result is no longer wanted."""
    # -copyts: without it the times count from the key frame ffmpeg actually seeked to, up to a few seconds before
    # `start` (measured: 0, 0.6 and 2.8 s late on three excerpts of one movie), and the check corrected towards that
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", str(start), "-copyts", "-i", url, "-map", f"0:s:{track}",
                             "-c:s", "srt", "-flush_packets", "1", "-f", "srt", "pipe:1"], stdout=subprocess.PIPE)
    deadline = time.time() + patience

    def watchdog():
        while proc.poll() is None:
            if time.time() > deadline or not alive():
                proc.kill()
            time.sleep(1)
    threading.Thread(target=watchdog, daemon=True).start()
    text = ""
    try:
        while chunk := proc.stdout.read1(4096):
            text += chunk.decode("utf-8", "replace")
            cues = parse_srt(text)
            if cues and cues[-1][0] >= start + length:
                break
    finally:
        proc.kill()
        proc.wait()
    return sorted(a for a, _, _ in parse_srt(text) if start <= a < start + length)


# --- Without a reference track: match the subtitles to the sound of the movie ---
# People start talking when a subtitle appears: the speech band gets louder right at the start of a cue. We read
# the sound of the three stretches where the file has most cues and look for the shift at which that holds best.
# Measured on two movies: the right shift stands 7 to 10 deviations above the others in each stretch, the next
# candidate under 3. We only act when the three stretches agree.

def loudness(url, start, length, alive=lambda: True):
    """Loudness of the speech band from `start`, one value per 100 ms, standardized over the excerpt; [] if unreadable."""
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(length), "-i", url, "-vn", "-sn", "-ac", "1",
                             "-af", "highpass=f=200,lowpass=f=3500", "-ar", "4000", "-f", "s16le", "pipe:1"], stdout=subprocess.PIPE)
    deadline = time.time() + 300

    def watchdog():
        while proc.poll() is None:
            if time.time() > deadline or not alive():
                proc.kill()
            time.sleep(1)
    threading.Thread(target=watchdog, daemon=True).start()
    raw = proc.stdout.read()
    proc.wait()
    pcm = memoryview(raw[:len(raw) // 800 * 800]).cast("h")
    e = [math.log10(1 + math.sqrt(sum(x * x for x in pcm[i:i + 400]) / 400)) for i in range(0, len(pcm), 400)]
    if len(e) < length * 5:  # less than half of the excerpt came through
        return []
    mean = sum(e) / len(e)
    dev = math.sqrt(sum((x - mean) ** 2 for x in e) / len(e)) or 1
    return [(x - mean) / dev for x in e]


def dense_stretches(starts, duration, length=480, apart=900, n=3, least=15):
    """Starts of the n stretches of `length` seconds holding most cues, at least `apart` seconds from each other."""
    counts = sorted(((bisect.bisect_left(starts, t + length) - bisect.bisect_left(starts, t), t)
                     for t in range(0, max(int(duration) - length, 1), 60)), reverse=True)
    picks = []
    for count, t in counts:
        if count >= least and all(abs(t - p) >= apart for p in picks):
            picks.append(t)
    return sorted(picks[:n])


def sound_shift(starts, excerpts, reach=60):
    """excerpts: [(second it starts at, loudness)]. Returns (shift b such that the cues at starts + b fall where
    speech begins, clear?) — clear: one shift stands out overall and each excerpt, taken alone, points to it."""
    def peak(scores):
        mean = sum(scores) / len(scores)
        dev = math.sqrt(sum((x - mean) ** 2 for x in scores) / len(scores)) or 1
        k = max(range(len(scores)), key=scores.__getitem__)
        rest = max((x for j, x in enumerate(scores) if abs(j - k) > 20), default=mean)  # best one more than 2 s away
        return k, (scores[k] - mean) / dev, (rest - mean) / dev
    per = []
    for start, z in excerpts:
        pre = [0.0]
        for x in z:
            pre.append(pre[-1] + x)
        near = [a for a in starts if start - reach - 1 <= a <= start + len(z) / 10 + reach + 1]
        scores = []
        for k in range(-reach * 10, reach * 10 + 1):
            tot = 0.0
            for a in near:
                i = int((a - start) * 10) + k
                if 8 <= i <= len(z) - 8:
                    tot += pre[i + 8] - 2 * pre[i] + pre[i - 8]  # the 0.8 s after the cue starts, minus the 0.8 s before
            scores.append(tot)
        per.append(scores)
    if not per:
        return 0.0, False
    k, z, z2 = peak([sum(col) for col in zip(*per)])
    alone = [peak(s) for s in per]
    clear = len(per) >= 2 and z >= 6 and z2 <= 0.6 * z and all(abs(k1 - k) <= 7 and z1 >= 5 for k1, z1, _ in alone)
    return (k - reach * 10) / 10, clear


def fit_by_sound(url, duration, candidates, alive=lambda: True):
    """Same contract as fit_subs, for a video file without a reference track.
    ponytail: shift only. A file timed for another frame rate drifts, so its stretches disagree and nothing is
    changed; fit a speed across the stretches if such files turn out to be common."""
    starts = [a for a, _, _ in candidates[0]]
    excerpts = []
    for t in dense_stretches(starts, duration):
        z = loudness(url, max(t - 60, 0), 600, alive)
        if not alive():
            return 0, candidates[0], "", False
        if z:
            excerpts.append((max(t - 60, 0), z))
    if len(excerpts) < 2:
        return 0, candidates[0], 'not checked: the sound could not be read. Adjust with the buttons if needed', False
    found = []
    for i, cues in enumerate(candidates):
        b, clear = sound_shift([a for a, _, _ in cues], excerpts)
        if clear:
            found.append((-abs(b), -i, i, b))  # between files that fit, the one that needs the smallest shift
    if not found:
        return 0, candidates[0], 'not checked: the sound gives no clear answer. Adjust with the buttons if needed', False
    _, _, i, b = max(found)
    if abs(b) < 0.3:
        note, cues = "checked against the sound: in sync", candidates[i]
    else:
        note, cues = f"shifted by {b:+.1f} s to match the sound", [(a + b, e + b, x) for a, e, x in candidates[i]]
    return i, cues, note + (" — switched to a file that matches this release better" if i else ""), bool(i) or cues is not candidates[i]


SPEEDS = (1, 24 / 23.976, 23.976 / 24, 25 / 23.976, 23.976 / 25, 25 / 24, 24 / 25)


def match_subs(starts, windows, reach=120, tol=0.3):
    """Best (speed r, shift b) such that starts × r + b lands on the cues of both excerpts.
    Returns (r, b, clear?) — clear: the best score dominates the 2nd distinct peak and the typical score, in each excerpt."""
    scores = []
    for r in SPEEDS:
        near = [[t * r for t in starts if w[0] - 1 - reach <= t * r <= w[-1] + 1 + reach] for w in windows]
        for k in range(-reach * 10, reach * 10 + 1):
            b, hits = k / 10, []
            for w, ts in zip(windows, near):
                n = 0
                for t in ts:
                    u = t + b
                    if w[0] - 1 <= u <= w[-1] + 1:
                        i = bisect.bisect_left(w, u)
                        n += any(abs(u - w[j]) < tol for j in (i - 1, i) if 0 <= j < len(w))
                hits.append(n)
            scores.append((sum(hits), min(hits), -abs(b), r, b))
    scores.sort(reverse=True)
    best = scores[0]
    second = next((sc for sc in scores if sc[3] != best[3] or abs(sc[4] - best[4]) > 1.5), best)
    typical = scores[len(scores) // 2][0]
    clear = best[1] >= 8 and best[0] >= 1.2 * second[0] and best[0] >= 2 * max(typical, 1)
    return best[3], best[4], clear, best[0]


def fit_subs(candidates, windows):
    """candidates: lists of cues, the preferred one first. Returns (chosen index, cues, note, changed?)."""
    found = []
    for i, cues in enumerate(candidates):
        r, b, clear, score = match_subs([a for a, _, _ in cues], windows)
        if clear:
            found.append((score, -i, i, r, b))
    if not found:
        return 0, candidates[0], 'not checked: adjust with the buttons if needed', False
    _, _, i, r, b = max(found)
    if r == 1 and abs(b) < 0.3:
        note, cues = "checked: in sync", candidates[i]
    else:
        note = f"shifted by {b:+.1f} s" + (" (speed corrected)" if r != 1 else "")
        cues = [(a * r + b, e * r + b, x) for a, e, x in candidates[i]]
    return i, cues, note + (" — switched to a file that matches this release better" if i else ""), bool(i) or cues is not candidates[i]


def torrent_health(src, duration, position=0):
    """For a source read through TorrServer: download speed and speed the movie needs (bytes/s), connected peers,
    and `ahead`: seconds of movie already downloaded after the playback position (the buffer)."""
    h = src["torrent"]["hash"]
    t = ts("get", hash=h)
    need = int(src["torrent"]["size"] / duration) if duration else 0
    out = {"speed": int(t.get("download_speed") or 0), "need": need, "peers": t.get("active_peers") or 0, "ahead": 0}
    try:
        c = ts("get", _path="/cache", hash=h)
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


def verify_subs(live, url, track, duration, paths, memo):
    """In the background during playback: checks the subtitles and, if a better sync stands out, restarts the stream with it.
    The check reads two other parts of the movie, so it competes with playback for the torrent's bandwidth: it only runs
    when the download is comfortably faster than the movie, and stops as soon as something else is launched."""
    alive = lambda: current() is live
    try:
        time.sleep(20)  # let playback buffer, and the download speed settle
        if not alive():
            return
        if live.get("torrent"):
            h = torrent_health(live, duration)
            if h["speed"] < 2.5 * h["need"]:
                live["note"] = 'not checked: the source is too slow to check while playing. Adjust with the buttons if needed'
                return
        candidates = [parse_srt(p.read_text("utf-8", "replace")) for p in paths]
        windows = [ref_window(url, track, int(duration * f), alive=alive) for f in (0.10, 0.70)] if track is not None else []
        if not alive():
            return
        if track is not None and min(len(w) for w in windows) < 10:
            note, changed, srt = "not checked: too few reference cues", False, paths[0]
        else:
            i, cues, note, changed = fit_subs(candidates, windows) if track is not None else fit_by_sound(url, duration, candidates, alive)
            if not alive():
                return
            srt = paths[i]
            if changed:
                srt = memo.with_suffix(".srt")
                srt.write_text(write_srt(cues), "utf-8")
        memo.write_text(json.dumps({"srt": str(srt), "note": note}))
        live["note"] = note
        if changed and alive():
            live["subs"], live["offset"] = srt, current_position(live)
            live["sub_delay"] = 0  # a delay set by hand was for the uncorrected file
            with progress_lock:
                p = progress.get(live.get("film"))
                if p:
                    p["play"]["subDelay"] = 0
                    save_progress()
            start_live(live)
    except Exception as e:
        print("subtitle check:", e, flush=True)


def synced_subs(live, url, subs, alts=(), track=None):
    """Picks the .srt for the `live` stream: result of a past check, else the preferred one + a background check."""
    memo = CACHE / "subs" / ("sync-" + hashlib.sha1((url + "|" + subs).encode()).hexdigest() + ".json")
    if memo.exists() and Path((m := json.loads(memo.read_text()))["srt"]).exists():
        live["subs"], live["note"] = Path(m["srt"]), m["note"]
        return
    paths = [fetch_subs(u) for u in [subs, *list(alts)[:5]]]
    live["subs"] = paths[0]
    if live["duration"] < 900:
        live["note"] = "not checked: the movie is too short, or its duration is unknown"
    else:
        threading.Thread(target=verify_subs, args=(live, url, track, live["duration"], paths, memo), daemon=True).start()


def play_url(url, name, title, size=None, subs=None, poster=None, film=None, start=0, delay=0, alts=(), facts=None):
    """Plays on the TV an address the Mac can read (local TorrServer or direct link), relayed by us.
    subs: address of a .srt to burn in → goes through the live stream (re-encoding).
    film: movie identifier, so that track() records the position; start: resume at this second."""
    m = re.match(re.escape(TS) + r"/.*[?&]link=([0-9a-f]{40})", url)
    torrent = {"hash": m[1], "size": size or 0} if m else None
    step("Checking the format")
    duration, track, vcodec, acodec = probe(url)
    convert = needs_convert(vcodec, acodec)
    if subs or convert:
        live = {"title": title, "thumbnail": poster, "offset": start, "inputs": [(url, "")], "film": film, "sub_delay": delay,
                "duration": duration, "convert": convert, "torrent": torrent, "about": facts}
        if subs:
            step("Preparing subtitles")
            synced_subs(live, url, subs, alts, track)
        else:
            step("Converting for the TV", " and ".join(f"{c.upper()} {k}" for k, c in (("video", vcodec), ("audio", acodec)) if k in convert))
        return start_live(live)
    mime = MIME.get(Path(name).suffix.lower(), "video/mp4")
    src = {"proxy": url, "title": title, "mime": mime, "thumbnail": poster, "torrent": torrent, "about": facts}
    play_on_tv(share(src, name), title, mime, DLNA_FLAGS, f' size="{size}"' if size else "")
    if start:
        step(f"Resuming at {hms(start)}")
        for _ in range(40):  # the TV can only seek once playback has started
            if soap("AVTransport", "GetTransportInfo", I0).get("CurrentTransportState") == "PLAYING":
                break
            time.sleep(0.5)
        soap("AVTransport", "Seek", f"{I0}<Unit>REL_TIME</Unit><Target>{hms(start)}</Target>")
    src["film"] = film  # only now: track() must not record "0:03" before the resume seek
    return {"title": title}


def fetch_subs(url):
    if not re.match(r"https?://", url or ""):
        raise ValueError("invalid subtitles address")
    out = CACHE / "subs" / (hashlib.sha1(url.encode()).hexdigest() + ".srt")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            out.write_bytes(r.read(5 << 20))
    return out


def cast_torrent(link):
    url, name, size = torrent_file(link)
    return play_url(url, name, Path(name).stem, size)


def play(a):
    """Plays a source returned by a Stremio add-on: torrent (infoHash) or direct link (url)."""
    title = str(a.get("title") or "Movie")
    if a.get("infoHash"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", a["infoHash"]):
            raise ValueError("invalid torrent identifier")
        trackers = [s.split(":", 1)[1] for s in a.get("sources") or [] if isinstance(s, str) and s.startswith("tracker:")]
        link = f"magnet:?xt=urn:btih:{a['infoHash']}" + "".join("&tr=" + quote(t, safe="") for t in trackers)
        url, name, size = torrent_file(link, a.get("fileIdx"), a.get("filename"))
    elif re.match(r"https?://", a.get("url") or ""):
        url, name, size = a["url"], Path(urlparse(a["url"]).path).name or "film.mp4", None
    else:
        raise ValueError("this source is neither a torrent nor a direct link")
    film, start = a.get("film") or {}, max(int(a.get("start") or 0), 0)
    if film.get("id"):
        with progress_lock:
            old = progress.get(film["id"], {})
            progress[film["id"]] = {"id": film["id"], "title": title, "poster": film.get("poster"),
                                    "play": {k: a[k] for k in ("infoHash", "fileIdx", "filename", "url", "sources", "subs", "subDelay", "title", "poster") if a.get(k) is not None},
                                    "position": start, "duration": old.get("duration", 0), "updated": int(time.time())}
            save_progress()
    alts = [u for u in a.get("subsAlt") or [] if isinstance(u, str) and re.match(r"https?://", u)]
    return play_url(url, name, title, size, a.get("subs"), a.get("poster"), film.get("id"), start, float(a.get("subDelay") or 0), alts,
                    about({**(a.get("about") if isinstance(a.get("about"), dict) else {}), "film": film.get("id")}))


# --- Resume: playback position of the catalog movies, recorded in progress.json (single profile)

PROGRESS = HERE / "progress.json"
progress_lock = threading.Lock()
try:
    progress = json.loads(PROGRESS.read_text())
except (OSError, ValueError):
    progress = {}


def save_progress():
    PROGRESS.write_text(json.dumps(progress, indent=1, ensure_ascii=False))


# --- Figures for the home view (stats.json): seconds actually played on the TV, per kind and per day, and how many
# different videos were played. Counted by track(), 10 s at a time: nothing to compute, a few hundred bytes to keep.

STATS = HERE / "stats.json"
try:
    stats = json.loads(STATS.read_text())
except (OSError, ValueError):
    stats = {}
last_counted = None


def count_playback(src, title, tick=10):
    global last_counted
    facts = src if isinstance(src, dict) else {}
    kind = "movies" if facts.get("film") else "links" if (facts.get("about") or {}).get("link") else "other"
    for group, key in (("seconds", kind), ("days", time.strftime("%Y-%m-%d"))):
        stats.setdefault(group, {})[key] = stats.get(group, {}).get(key, 0) + tick
    for day in sorted(stats["days"])[:-60]:  # two months of daily figures are enough for "last 7 days"
        del stats["days"][day]
    if (kind, title) != last_counted:  # another video than 10 s ago (a server restart counts it again)
        last_counted = (kind, title)
        stats.setdefault("plays", {})[kind] = stats.get("plays", {}).get(kind, 0) + 1
    STATS.write_text(json.dumps(stats, indent=1))


def stats_summary(st, today):
    """today: time.time(). Totals in seconds; week: the last 7 days, today included."""
    recent = {time.strftime("%Y-%m-%d", time.localtime(today - 86400 * n)) for n in range(7)}
    return {"seconds": sum((st.get("seconds") or {}).values()), "by": st.get("seconds") or {}, "plays": st.get("plays") or {},
            "week": sum(v for d, v in (st.get("days") or {}).items() if d in recent)}


def track():
    """Every 10 s, while the TV plays what we serve: counts the time played, and for a catalog movie records how
    far it has got (works with the page closed)."""
    while True:
        time.sleep(10)
        try:
            src = current()
            if src is None:
                continue
            s = status()
            state = s.get("CurrentTransportState")
            if next(iter(shared)) not in s.get("TrackURI", "") or state not in ("PLAYING", "PAUSED_PLAYBACK"):
                continue
            if state == "PLAYING":
                count_playback(src, s.get("title"))
            if not (isinstance(src, dict) and src.get("film")):
                continue
            with progress_lock:
                p = progress.get(src["film"])
                if p:
                    p.update(position=secs(s.get("RelTime")), duration=secs(s.get("TrackDuration")) or p["duration"], updated=int(time.time()))
                    save_progress()
        except Exception:
            pass  # TV off or busy: we retry on the next round


# --- Settings: list of Stremio add-ons (catalog, sources, subtitles), kept in config.json

CONFIG = HERE / "config.json"
DEFAULT_ADDONS = ["https://v3-cinemeta.strem.io", "https://opensubtitles-v3.strem.io"]  # official Stremio catalog and subtitles


def load_config():
    try:
        return {"addons": DEFAULT_ADDONS, **json.loads(CONFIG.read_text())}
    except (OSError, ValueError):
        return {"addons": DEFAULT_ADDONS}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def get_config():
    return {"addons": load_config()["addons"]}


def set_config(a):
    addons = [re.sub(r"/manifest\.json$", "", u.strip().replace("stremio://", "https://")).rstrip("/") for u in a.get("addons") or []]
    if not all(re.match(r"https?://", u) for u in addons):
        raise ValueError("invalid add-on address")
    save_config({**load_config(), "addons": list(dict.fromkeys(addons))})
    return get_config()


def hms(t):
    return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}"


def secs(t):
    return sum(int(x) * 60 ** i for i, x in enumerate(reversed(t.split(":")))) if re.fullmatch(r"[\d:]+", t or "") else 0


def thumb():
    """Thumbnail of the current video: poster provided by the catalog, else a frame extracted from the file by ffmpeg."""
    p = current()
    if not p:
        raise LookupError
    if isinstance(p, dict):
        if not p.get("thumbnail"):
            raise LookupError
        with urllib.request.urlopen(p["thumbnail"], timeout=6) as r:
            return r.read()
    out = CACHE / "thumbs" / (hashlib.sha1(str(p).encode()).hexdigest() + ".jpg")
    for ss in ("20", "0"):  # 0: videos shorter than 20 s
        if out.exists():
            break
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-ss", ss, "-i", str(p), "-frames:v", "1", "-vf", "scale=1280:-2", "-y", str(out)],
                       capture_output=True, timeout=30)
    if not out.exists():
        raise LookupError
    return out.read_bytes()


def transport(action, seconds=0):
    if action == "play":
        return soap("AVTransport", "Play", I0 + "<Speed>1</Speed>")
    if action in ("pause", "stop"):
        return soap("AVTransport", action.capitalize(), I0)
    if action == "seek":
        t = max(int(seconds), 0)
        live = current_live()
        if live:
            live["offset"] = t
            return start_live(live)
        return soap("AVTransport", "Seek", f"{I0}<Unit>REL_TIME</Unit><Target>{hms(t)}</Target>")
    raise ValueError(f"unknown action: {action}")


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


def list_files():
    out = []
    for root, dirs, files in os.walk(MEDIA_DIR):  # os.walk skips unreadable folders
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not d.endswith(".tvlibrary"))
        for f in sorted(files):
            p = Path(root, f)
            if p.suffix.lower() in MIME and not f.startswith("."):
                out.append({"path": str(p), "name": str(p.relative_to(MEDIA_DIR)), "size": p.stat().st_size})
                if len(out) >= 2000:
                    return out
    return out


def parse_range(header, size):
    """Range header → (start, end) inclusive, None for the whole file, ValueError if out of bounds."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or not (m[1] or m[2]):
        return None
    if m[1]:
        start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
    else:
        start, end = max(size - int(m[2]), 0), size - 1
    if start > end:
        raise ValueError
    return start, end


def post_action(path, a):
    if path == "/api/key":
        return need_tv().press(a.get("event"))
    if path == "/api/tv":
        return connect_tv(str(a.get("ip", "")).strip())
    if path == "/api/volume":
        return soap("RenderingControl", "SetVolume", f"{I0}<Channel>Master</Channel><DesiredVolume>{min(max(int(a['value']), 0), 100)}</DesiredVolume>")
    if path == "/api/transport":
        if a.get("action") == "stop":
            job.update(stopped=True, error="", detail="")
            try:
                return transport("stop")
            finally:
                shared.clear()  # stopped: nothing is served any more…
                only_torrent()  # …and nothing keeps downloading
        return transport(a.get("action"), a.get("seconds", 0))
    if path == "/api/cast":
        return launch(Path(str(a.get("path", ""))).name, cast, a["path"], a.get("burn"))
    if path == "/api/youtube":
        return dial("YouTube", "POST", f"v={youtube_id(a['url'])}".encode())
    if path == "/api/castlink":
        if is_torrent(a["url"]):
            return launch("Torrent", cast_torrent, a["url"])
        return launch(urlparse(str(a["url"])).hostname or "Link",
                      lambda: cast(media_fetch(a["url"])) if a.get("mode") == "download" else stream_link(a["url"]))
    if path == "/api/subdelay":
        return sub_delay(a)
    if path == "/api/play":
        return launch(a.get("title") or "Movie", play, a)
    if path == "/api/dismiss":
        return job.update(error="", detail="")
    if path == "/api/config":
        return set_config(a)
    if path == "/api/app":
        if a.get("name") not in APPS:
            raise ValueError("unknown app")
        if a.get("action") == "stop":
            return dial(a["name"] + "/run", "DELETE")
        return dial(a["name"], "POST", b"")
    raise LookupError


# --- HTTP -------------------------------------------------------------------

LIVE_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # over HTTP/1.0 the TV does not use Range and stays stuck in TRANSITIONING

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass  # the TV drops its connections without warning on every seek in the video

    def is_local(self):
        """Access to the interface: the Mac itself, or (with --lan) a device on the local network that presents the key."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0]  # Host checked against DNS rebinding
        if self.client_address[0] == "127.0.0.1":
            return host in ("127.0.0.1", "localhost")
        cookie = re.search(r"(?:^|;\s*)tvkey=([\w-]+)", self.headers.get("Cookie", ""))
        return bool(LAN_KEY and ipaddress.ip_address(self.client_address[0]).is_private and host.lower() in (local_ip(), *LOCAL_NAMES)
                    and cookie and hmac.compare_digest(cookie[1], LAN_KEY))

    def lan_login(self):
        """/?key=… from the local network: sets the access cookie (1 year) and redirects to the page."""
        m = re.search(r"[?&]key=([\w-]+)", self.path)
        if not (LAN_KEY and m and hmac.compare_digest(m[1], LAN_KEY)):
            return False
        self.send_response(302)
        self.send_header("Set-Cookie", f"tvkey={LAN_KEY}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def reply(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def run(self, fn, *args):
        try:
            self.reply(200, fn(*args) or {"ok": True})
        except LookupError:
            self.reply(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {"error": str(e) or "invalid request"})
        except Exception as e:  # TV off, timeout, SOAP fault…
            self.reply(502, {"error": f"TV unreachable or command refused ({e})"})

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if self.path.startswith("/media/"):
            return self.media(head)
        if not self.is_local():
            return self.lan_login() or self.reply(403, {"error": "access denied: open the link with the key (?key=…)"})
        if self.path.startswith("/api/qr.png"):
            try:
                return self.reply(200, qr_png(lan_links()[0]), "image/png")
            except Exception:
                return self.reply(404, {"error": "no QR code"})
        if self.path.startswith("/api/thumb"):
            try:
                return self.reply(200, thumb(), "image/jpeg")
            except Exception:
                return self.reply(404, {"error": "no thumbnail"})
        if self.path.split("?")[0] in ("/", "/index.html"):
            return self.reply(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/ytsearch?"):
            return self.run(yt_search, (parse_qs(urlparse(self.path).query).get("q") or [""])[0])
        routes = {
            "/api/history": lambda: {"links": link_history()},
            "/api/stats": lambda: stats_summary(stats, time.time()),
            "/api/status": public_status,
            "/api/apps": apps,
            "/api/files": lambda: {"dir": str(MEDIA_DIR), "files": list_files()},
            "/api/config": get_config,
            "/api/links": lambda: {"links": lan_links()},
            "/api/job": lambda: {k: job[k] for k in ("busy", "title", "steps", "error", "detail")},
            "/api/tvs": lambda: {"tvs": tvlib.discover(), "current": current_tv.ip if current_tv else None},
            "/api/channels": lambda: need_tv().channels(),
            "/api/progress": lambda: {"films": sorted(progress.values(), key=lambda p: -p["updated"])},
            "/api/info": lambda: {"tv": current_tv.info() if current_tv else None, "mac": local_ip(), "port": PORT},
        }
        if self.path not in routes:
            return self.reply(404, {"error": "not found"})
        self.run(routes[self.path])

    def do_POST(self):
        # JSON required: forces a CORS preflight (never accepted) for third-party web pages.
        if not self.is_local() or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.reply(403, {"error": "local access only"})
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            args = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self.reply(400, {"error": "invalid JSON"})
        self.run(post_action, self.path, args)

    def media(self, head):
        print(f"{self.log_date_time_string()} TV {self.command} {self.path.rsplit('/', 1)[-1][:40]} Range={self.headers.get('Range')}", flush=True)
        parts = self.path.split("/")  # ["", "media", token, name]
        p = shared.get(parts[2]) if len(parts) > 2 else None
        # Only the selected TV may fetch the video: the TV tells anyone on the network what it is playing, token included
        if self.client_address[0] not in ("127.0.0.1", current_tv.ip if current_tv else ""):
            p = None
        if not p:
            return self.reply(404, {"error": "not found"})
        if isinstance(p, dict):
            return self.live(p, head) if "inputs" in p else self.proxy(p, head)
        size = p.stat().st_size
        try:
            rng = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = rng or (0, size - 1)
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Type", MIME.get(p.suffix.lower(), "text/srt" if p.suffix.lower() == ".srt" else "application/octet-stream"))
        if sub_url and p.suffix.lower() != ".srt":
            self.send_header("CaptionInfo.sec", sub_url)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", DLNA_FLAGS)
        self.end_headers()
        if head:
            return
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0 and (chunk := f.read(min(1 << 20, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the TV drops and retries with another Range when seeking

    def proxy(self, src, head):
        """Relays the TorrServer stream to the TV (with Range, so you can seek in the movie)."""
        req = urllib.request.Request(src["proxy"], method="HEAD" if head else "GET")
        if self.headers.get("Range"):
            req.add_header("Range", self.headers["Range"])
        try:
            r = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            r = e  # 416 and the like: relayed as is
        except OSError:
            fail("The torrent engine stopped answering.")
            return self.reply(502, {"error": "torrent engine unreachable"})
        with r:
            self.send_response(r.status)
            for h in ("Content-Length", "Content-Range"):
                if r.headers.get(h):
                    self.send_header(h, r.headers[h])
            self.send_header("Content-Type", src["mime"])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", DLNA_FLAGS)
            self.send_header("Connection", "close")
            self.end_headers()
            if head:
                return
            try:
                while chunk := r.read1(1 << 16):
                    self.wfile.write(chunk)
            except ConnectionError:
                pass  # the TV dropped the connection (seek, stop)
            except OSError:  # reading from TorrServer timed out
                if shared.get(self.path.split("/")[2]) is src:
                    fail("The torrent stopped sending data.", "no data from TorrServer for two minutes")

    def live(self, live, head):
        self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", LIVE_FLAGS)
        self.send_header("Connection", "close")  # unknown length: end of stream = close
        self.end_headers()
        if head:
            return
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(live_cmd(live), stdout=subprocess.PIPE, stderr=errors)
            sent, dropped = 0, False
            try:
                while chunk := proc.stdout.read1(1 << 16):
                    self.wfile.write(chunk)
                    sent += len(chunk)
            except ConnectionError:
                dropped = True  # the TV closed the connection (seek, stop): not a failure
            finally:
                proc.kill()
                code = proc.wait()
            errors.seek(0)
            log = errors.read().decode("utf-8", "replace").strip()
            if log:
                print("ffmpeg:", log[-2000:], flush=True)
            # ffmpeg ended by itself: a failure if it produced nothing or reported an error, the end of the video otherwise
            if not dropped and current() is live and (sent == 0 or (code not in (0, -9) and log)):
                fail(*stream_error(log))


def stream_error(log):
    """ffmpeg's error output → (sentence for the player, technical detail)."""
    lines = [l.strip() for l in log.splitlines() if l.strip()]
    detail = re.sub(r"^\[[^\]]*\]\s*", "", lines[-1])[:200] if lines else ""  # without the "[in#0/hls @ 0x…]" prefix
    if "403" in detail or "Forbidden" in detail:
        return "The site refused to send the video. Try again, or use “Download first”.", detail
    return "The video source stopped responding.", detail


def selftest():
    for outside in ("/etc/hosts", "~/.ssh/id_rsa", str(MEDIA_DIR / ".." / "x.mkv"), str(HERE / "lan-key.txt"), str(HERE / "server.py")):
        try:
            allowed_file(outside, MIME)
            raise AssertionError("file outside the movies folder accepted: " + outside)
        except ValueError:
            pass
    assert not is_torrent("/Users/x/a.torrent") and is_torrent("https://example.org/a.torrent")
    assert needs_convert("av1", "dts") == ["video", "audio"] and needs_convert("hevc", "dts") == ["audio"] and needs_convert("h264", "aac") == []
    assert needs_convert("", "") == []  # unknown format: leave it alone
    assert "-request_size 10M" in " ".join(live_cmd({"offset": 0, "inputs": [("https://x/v", "")]}))
    assert "-request_size" not in live_cmd({"offset": 0, "inputs": [(TS + "/stream/x", "")]})
    assert "-c:v copy -c:a aac" in " ".join(live_cmd({"offset": 0, "inputs": [("/a.mkv", "")], "convert": ["audio"]}))
    assert stream_error("") == ("The video source stopped responding.", "")
    assert stream_error("[in#0/hls @ 0x1] Error during demuxing: Input/output error")[1] == "Error during demuxing: Input/output error"
    assert "refused" in stream_error("x\n[https @ 0x2] Server returned 403 Forbidden (access denied)")[0]
    assert plain_error(ValueError("unrecognized link")) == ("Unrecognized link.", "")
    assert plain_error(RuntimeError("yt-dlp failed: ERROR: gone"))[1] == "ERROR: gone"
    step("a"); step("b", "1"); step("b", "2")
    assert job["steps"] == [["a", ""], ["b", "2"]]
    job["steps"].clear()
    # check: a launch replaced by a newer one stops at its next stage and leaves the newer one's stages alone
    globals()["only_torrent"] = lambda keep=None: None  # the test must not drop the torrents of a server that is running
    go, out = threading.Event(), []
    def old_launch():
        step("old"); go.wait(5); step("old, later")
    def run_old():
        try:
            launch("old", old_launch)
        except Superseded:
            out.append("stopped")
    th = threading.Thread(target=run_old); th.start()
    while job["steps"] != [["old", ""]]:
        time.sleep(0.01)
    launch("new", lambda: (step("new"), go.set(), th.join()))
    assert out == ["stopped"] and job["steps"] == [["new", ""]] and job["title"] == "new" and not job["busy"] and not job["error"], (out, job)
    job["steps"].clear()
    assert about({"description": "x" * 5000, "poster": "javascript:1", "link": "https://a/b", "other": 1, "film": "tt1"}) == {"description": "x" * 3000, "link": "https://a/b", "film": "tt1"}
    assert about(None) == {}
    day = lambda n: time.strftime("%Y-%m-%d", time.localtime(1e9 - 86400 * n))
    assert stats_summary({"seconds": {"movies": 50, "links": 20}, "days": {day(0): 30, day(6): 20, day(7): 20}, "plays": {"links": 3}}, 1e9) \
        == {"seconds": 70, "by": {"movies": 50, "links": 20}, "plays": {"links": 3}, "week": 50}
    # check: cues 12.3 s early against a "sound" that gets loud for 1.5 s when each line is spoken must come out as +12.3,
    # and unrelated cues must not come out as clear
    said = [[40 + 7.3 * n + (n * n * 37 % 50) / 10 + 900 * w for n in range(60)] for w in range(3)]
    def heard(lines, start, length=600):
        z = [-0.4 + ((i * 7919) % 13) / 40 for i in range(length * 10)]
        for t0 in lines:
            for i in range(int((t0 - start) * 10), int((t0 - start + 1.5) * 10)):
                if 0 <= i < len(z):
                    z[i] = 1.6
        return z
    excerpts = [(900 * w, heard(lines, 900 * w)) for w, lines in enumerate(said)]
    assert sound_shift([t0 - 12.3 for lines in said for t0 in lines], excerpts) == (12.3, True)
    assert not sound_shift([11.7 * n for n in range(240)], excerpts)[1]
    assert dense_stretches(sorted(t0 for lines in said for t0 in lines), 3000) == [0, 900, 1800]
    # check: while the TV has not switched to a restarted stream, the position is the restart point, not the TV's counter
    lv = {"offset": 1200}
    shared.clear(); shared["tok"] = lv
    assert live_position(lv) == 1200
    assert live_position(lv, {"TrackURI": "http://mac/media/old/stream.ts", "RelTime": "0:00:03"}) == 1200
    assert live_position(lv, {"TrackURI": "http://mac/media/tok/stream.ts", "RelTime": "0:00:30"}) == 1230 and live_position(lv) == 1230
    shared.clear()
    assert history_entry("u", {"title": "T", "uploader": "C", "duration": 61.4}) == {"url": "u", "title": "T", "channel": "C", "duration": 61, "thumbnail": ""}
    tvlib.selftest()
    assert secs("0:02:05") == 125 and secs("NOT_IMPLEMENTED") == 0 and hms(3725) == "1:02:05"
    assert is_torrent("magnet:?xt=urn:btih:abc") and is_torrent("08ada5a7a6183aae1e09d831df6748d566095a10") and not is_torrent("https://youtu.be/x")
    assert "setpts=PTS+(367.5)/TB" in " ".join(live_cmd({"offset": 369, "sub_delay": 1.5, "inputs": [("/a.mkv", "")], "subs": "/s.srt"}))
    # check: a .srt shifted by -9.7 s and played at 23.976 instead of 24 frames/s must land back on the reference,
    # and be preferred over an unrelated file, even if that one is the first choice
    truth = [(t, t + 2, "x") for t in (7.3 * n + (n * n * 37 % 50) / 10 for n in range(800))]
    wins = [sorted(t for t, _, _ in truth if a <= t <= a + 120) for a in (600, 4000)]
    slow = [((t - 9.7) * 24 / 23.976, (e - 9.7) * 24 / 23.976, x) for t, e, x in truth]
    i, fixed, note, changed = fit_subs([[(t * 1.3 + 500, e, x) for t, e, x in truth], slow], wins)
    assert i == 1 and changed and "shifted" in note and max(abs(f[0] - t[0]) for f, t in zip(fixed, truth)) < 0.5, (i, note)
    assert fit_subs([truth], wins)[2:] == ("checked: in sync", False)
    assert parse_srt(write_srt(truth[:3]))[2][0] == truth[2][0]
    assert norm_link(" https://youtu.be/dQw4w9WgXcQ ")[0] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-", 100) == (0, 99)
    assert parse_range("bytes=10-19", 100) == (10, 19)
    assert parse_range("bytes=90-500", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    try:
        parse_range("bytes=100-", 100)
        raise AssertionError("out-of-bounds Range accepted")
    except ValueError:
        pass
    for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", "https://youtu.be/dQw4w9WgXcQ?si=x",
              "https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"):
        assert youtube_id(u) == "dQw4w9WgXcQ", u
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # so that TorrServer stops with us
    tidy()
    if os.path.exists("/usr/bin/caffeinate"):  # macOS: stay awake while plugged in, for as long as this server runs
        subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())])
    start_torrserver()
    threading.Thread(target=track, daemon=True).start()
    restore_tv()
    print(f"TV: {current_tv.name + ' (' + current_tv.ip + ')' if current_tv else 'to be picked in the page'} · movies: {MEDIA_DIR}\n→ http://127.0.0.1:{PORT}", flush=True)
    if LAN_KEY:
        print(f"→ local network: http://{local_ip()}:{PORT}/?key={LAN_KEY}", flush=True)
        print(f"→ or by name:    http://{LOCAL_NAMES[0]}:{PORT}/?key={LAN_KEY}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
