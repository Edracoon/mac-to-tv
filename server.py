#!/usr/bin/env python3
"""mac-to-tv: web interface to control a TV on the local network and play videos on it.

    python3 server.py [movies_folder] [--lan]         # default folder: ~/Movies
    → http://127.0.0.1:8765

    python3 server.py --selftest                      # checks the pure functions (no network access)

The TV is picked in the page (local network search); it is remembered in config.json.
Environment variables: PORT (default 8765), TV_IP (forces a TV without going through the page).

Layout:
  - tv.py      : TV discovery, DLNA player, per-brand remote drivers, DIAL apps
  - sources.py : where the videos come from (links via yt-dlp, torrents via TorrServer, add-on sources)
  - subs.py    : subtitles (fetching, .srt and WebVTT, automatic sync check)
  - server.py  : playing a source on the TV or in the browser (ffmpeg streams, burned-in subtitles), playback
                 resume, HTTP API for the index.html page

Security: the interface and the API only answer the Mac itself, or (with --lan) devices on the local network
that present the key. Only /media/<token>/ is open to the local network, so the TV can read the current video.
"""
import hashlib
import hmac
import html
import ipaddress
import json
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


import sources
import subs as subslib
import tv as tvlib
from sources import CACHE, MIME, about, add_on_source, history, is_torrent, link_info, media_fetch, torrserver, youtube_id, yt_search
from subs import fetch_subs, probe, vtt

PORT = int(os.environ.get("PORT", "8765"))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
MEDIA_DIR = Path(ARGS[0] if ARGS else "~/Movies").expanduser().resolve()
HERE = Path(__file__).resolve().parent

APPS = ("YouTube",)  # apps we can launch through DIAL
I0 = "<InstanceID>0</InstanceID>"

# --lan: opens the interface to the local network, protected by a key (lan-key.txt). Without the option: Mac only.
LAN_KEY = None
if "--lan" in sys.argv:
    key_file = HERE / "lan-key.txt"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(9))
    key_file.chmod(0o600)  # other accounts of this Mac must not read the key
    LAN_KEY = key_file.read_text().strip()

# Everything that changes while the server runs lives in an object, one per role, each created once near the middle
# of this file: config, progress, stats (what is kept on disk), tv_player and browser (what is being played, each
# with its own launches). In sources.py: torrserver and history. The functions outside the classes keep no state.


# --- Launches: what the player area of the page shows while a video starts, and when it failed

mine = threading.local()  # .id: the launch this thread is running; .job: the Launches it reports to


class Superseded(Exception):
    """This launch was replaced by a newer one: it must stop instead of writing its stages into, or playing over, the new one."""


class Launches:
    """The launches of one player (the TV, or the page's own), one at a time; the page shows:
      busy/title/steps : a launch in progress and the stages it went through, each [label, detail]
      error/detail     : why the last playback failed, in plain words, plus the technical line for the curious
    stopped: the user asked to stop, so a player that is stopped is not a failure."""

    def __init__(self):
        self.busy, self.title, self.steps, self.error, self.detail, self.stopped, self.id = False, "", [], "", "", False, 0

    def step(self, label, detail=""):
        if getattr(mine, "id", self.id) != self.id:
            raise Superseded("replaced by a newer launch")
        if self.steps and self.steps[-1][0] == label:
            self.steps[-1][1] = detail
        else:
            self.steps.append([label, detail])

    def fail(self, message, detail=""):
        self.error, self.detail = message, detail

    def dismiss(self):
        self.error = self.detail = ""

    def run(self, title, fn, *args):
        """Runs a launch (link, file, torrent, catalog movie), publishing its stages and how it failed, if it did."""
        mine.id, mine.job = self.id + 1, self
        self.busy, self.title, self.steps, self.error, self.detail, self.stopped, self.id = True, str(title)[:120], [], "", "", False, mine.id
        try:
            return fn(*args)
        except Exception as e:
            if mine.id == self.id and not isinstance(e, Superseded):
                self.fail(*plain_error(e))
            raise
        finally:
            if mine.id == self.id:  # a replaced launch leaves the player, and the torrent, to its successor
                self.busy = False
                only_torrent()  # whatever happened, nothing else keeps downloading
            del mine.id, mine.job

    def public(self):
        return {k: getattr(self, k) for k in ("busy", "title", "steps", "error", "detail")}


def step(label, detail=""):
    """The stage reached by the launch this thread is running (outside a launch: reported to the TV's)."""
    getattr(mine, "job", tv_player.job).step(label, detail)


sources.step = step  # reading a link or a torrent reports its stages too


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
    if "not found on the network" in text or (isinstance(e, OSError) and getattr(mine, "job", tv_player.job) is tv_player.job):
        return "The TV is not answering. Is it switched on?", text
    return "Something went wrong while starting playback.", text


# --- What is kept on disk: settings, playback positions, figures

class Config:
    """config.json: the TV picked in the page and the list of Stremio add-ons (catalog, sources, subtitles)."""

    DEFAULT_ADDONS = ["https://v3-cinemeta.strem.io", "https://opensubtitles-v3.strem.io"]  # official Stremio catalog and subtitles

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            return {"addons": self.DEFAULT_ADDONS, **json.loads(self.path.read_text())}
        except (OSError, ValueError):
            return {"addons": self.DEFAULT_ADDONS}

    def save(self, cfg):
        self.path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    def addons(self):
        return {"addons": self.load()["addons"]}

    def set_addons(self, a):
        addons = [re.sub(r"/manifest\.json$", "", u.strip().replace("stremio://", "https://")).rstrip("/") for u in a.get("addons") or []]
        if not all(re.match(r"https?://", u) for u in addons):
            raise ValueError("invalid add-on address")
        self.save({**self.load(), "addons": list(dict.fromkeys(addons))})
        return self.addons()


class Progress:
    """progress.json: the catalog movies and episodes that were launched, with what, and how far each has got
    (single profile). Every change goes through here, under one lock."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            self.films = json.loads(path.read_text())
        except (OSError, ValueError):
            self.films = {}

    def save(self):
        self.path.write_text(json.dumps(self.films, indent=1, ensure_ascii=False))

    def list(self):
        return {"films": sorted(self.films.values(), key=lambda p: -p["updated"])}

    def launched(self, a):
        """Notes that a catalog movie or episode was launched, and with what → (title, movie id, start)."""
        title = str(a.get("title") or "Movie")
        film, start = a.get("film") or {}, max(int(a.get("start") or 0), 0)
        if film.get("id"):
            with self.lock:
                old = self.films.get(film["id"], {})
                self.films[film["id"]] = {"id": film["id"], "title": title, "poster": film.get("poster"),
                                          "play": {k: a[k] for k in ("infoHash", "fileIdx", "filename", "url", "sources", "subs", "subDelay", "title", "poster") if a.get(k) is not None},
                                          "position": start, "duration": old.get("duration", 0), "updated": int(time.time()),
                                          # an episode: its series (id and name), to group and resume by series on the page
                                          **{k: str(film[k])[:200] for k in ("series", "show") if film.get(k)},
                                          **{k: int(film[k]) for k in ("season", "episode") if isinstance(film.get(k), int)}}
                self.save()
        return title, film.get("id"), start

    def update(self, film, position=None, duration=0, sub_delay=None):
        """Records how far the movie `film` has got (a duration of 0 keeps the one known) and/or the delay of its
        subtitles. Does nothing for a video that is not a catalog movie."""
        with self.lock:
            p = self.films.get(film)
            if not p:
                return
            if position is not None:
                p.update(position=int(position), duration=duration or p["duration"], updated=int(time.time()))
            if sub_delay is not None:
                p["play"]["subDelay"] = sub_delay
            self.save()


class Stats:
    """stats.json, the figures of the home view: seconds actually played, per kind and per day, and how many different
    videos were played. Counted 10 s at a time: nothing to compute, a few hundred bytes to keep."""

    def __init__(self, path):
        self.path, self.last = path, None  # last: the video counted 10 s ago
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def count(self, src, title, tick=10):
        st, facts = self.data, src if isinstance(src, dict) else {}
        kind = "movies" if facts.get("film") else "links" if (facts.get("about") or {}).get("link") else "other"
        for group, key in (("seconds", kind), ("days", time.strftime("%Y-%m-%d"))):
            st.setdefault(group, {})[key] = st.get(group, {}).get(key, 0) + tick
        for day in sorted(st["days"])[:-60]:  # two months of daily figures are enough for "last 7 days"
            del st["days"][day]
        if (kind, title) != self.last:  # another video than 10 s ago (a server restart counts it again)
            self.last = (kind, title)
            st.setdefault("plays", {})[kind] = st.get("plays", {}).get(kind, 0) + 1
        self.path.write_text(json.dumps(st, indent=1))

    def summary(self):
        return stats_summary(self.data, time.time())


def stats_summary(st, today):
    """today: time.time(). Totals in seconds; week: the last 7 days, today included."""
    recent = {time.strftime("%Y-%m-%d", time.localtime(today - 86400 * n)) for n in range(7)}
    return {"seconds": sum((st.get("seconds") or {}).values()), "by": st.get("seconds") or {}, "plays": st.get("plays") or {},
            "week": sum(v for d, v in (st.get("days") or {}).items() if d in recent)}


# --- Files and streams: what may be read, what has to be converted, the ffmpeg commands

LIVE_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


def hms(t):
    return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}"


def secs(t):
    return sum(int(x) * 60 ** i for i, x in enumerate(reversed(t.split(":")))) if re.fullmatch(r"[\d:]+", t or "") else 0


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


# What the TV decodes. Video checked on the Grundig Ultralogic 4K with generated clips (2026-11-18): H.264 and HEVC
# (8 and 10 bit) play, AV1 makes the TV stop at once. Audio cannot be checked from here (a silent movie still
# "plays"): AAC, MP3 and Dolby Digital are assumed fine; DTS, TrueHD, FLAC, Opus… are converted.
# ponytail: one list for every TV; move it to the TV description in tv.py when a second TV disagrees.
TV_VIDEO = {"h264", "hevc", "mpeg2video", "mpeg4"}
TV_AUDIO = {"aac", "mp3", "mp2", "ac3", "eac3"}


def needs_convert(vcodec, acodec):
    """Which parts of a file have to be re-encoded for the TV: a list among "video", "audio"."""
    return [kind for kind, codec, ok in (("video", vcodec, TV_VIDEO), ("audio", acodec, TV_AUDIO)) if codec and codec not in ok]


# brew install ffmpeg-full: the standard ffmpeg lacks the subtitles filter (/opt/homebrew = Apple Silicon, /usr/local = Intel)
FFMPEG_SUBS = next((p for p in ("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg") if os.path.exists(p)), "ffmpeg")


def ffmpeg_inputs(inputs, offset, first=""):
    """ffmpeg arguments that read `inputs` ([(address, HTTP headers)]) from second `offset`, picture and sound mapped:
    every track, or with first=":0" only the first picture and the first sound of each input."""
    cmd = []
    for u, headers in inputs:
        if u.startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-headers", headers]
            if not u.startswith(torrserver.URL):
                # YouTube throttles a video asked for in one request to about its playback speed (measured: 240 s of
                # 1080p60 not delivered in 100 s, against 5 s in 10 MB requests), which stutters on busy scenes
                cmd += ["-request_size", "10M"]
        if offset:
            cmd += ["-ss", str(offset)]
        cmd += ["-i", u]
    for i in range(len(inputs)):
        cmd += ["-map", f"{i}:v{first}?", "-map", f"{i}:a{first}?"]
    return cmd


def live_cmd(live):
    cmd = [FFMPEG_SUBS if live.get("subs") else "ffmpeg", "-v", "error"] + ffmpeg_inputs(live["inputs"], live["offset"])
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


def sub_alts(a):
    return [u for u in a.get("subsAlt") or [] if isinstance(u, str) and re.match(r"https?://", u)]


def film_facts(a):
    return about({**(a.get("about") if isinstance(a.get("about"), dict) else {}), "film": (a.get("film") or {}).get("id")})


def torrent_hash(src):
    return src["torrent"]["hash"] if isinstance(src, dict) and src.get("torrent") else None


# --- Playing on the TV ---------------------------------------------------------

class TVPlayer:
    """The TV picked in the page, and the video we are playing on it: one at a time.
    A file the TV decodes is served as is, with seeking. Anything else (a link, burned-in subtitles, a format to
    convert) is a live stream: sources.py provides the addresses, ffmpeg muxes picture + sound on the fly. Such a
    stream has no middle: seeking = restarting it from there."""

    def __init__(self):
        self.tv = None  # tvlib.TV picked in the page (or forced by TV_IP)
        self.shared = {}  # token → what the TV may fetch: the video (a Path, or a dict for a relayed address or a live stream), its .srt
        self.sub_url = None  # address of the external subtitles for the current video
        self.job = Launches()

    # the TV itself

    def need(self):
        if not self.tv:
            raise ValueError("no TV selected: pick one by clicking the name at the top of the page")
        return self.tv

    def soap(self, service, action, args="", timeout=4):
        return self.need().soap(service, action, args, timeout=timeout)

    def dial(self, app, method="GET", body=None):
        return self.need().dial(app, method, body)

    def apps(self):
        return {app: self.need().app_state(app) for app in APPS}

    def connect(self, ip, save=True):
        """Selects the TV at this address (it must have a DLNA network player) and remembers it in config.json."""
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
        self.tv = t
        self.shared.clear()
        if save:
            config.save({**config.load(), "tv": {"ip": t.ip, "name": t.name}})
        return t.info()

    def restore(self):
        """On startup: restores the TV from TV_IP or config.json, without blocking if it is off (re-read on first use)."""
        saved = config.load().get("tv") or {}
        ip = os.environ.get("TV_IP") or saved.get("ip")
        if ip:
            self.tv = tvlib.TV(ip, saved.get("name", ""))
            try:
                self.tv.refresh()
            except Exception:
                pass

    def local_ip(self):
        """Address of the Mac on the local network (the one it reaches the TV through)."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self.tv.ip if self.tv else "192.0.2.1", 9))  # UDP: no packet sent, just picks the interface
            return s.getsockname()[0]

    # what it is playing

    def current(self):
        return next(iter(self.shared.values()), None)

    def live(self):
        src = self.current()
        return src if isinstance(src, dict) and "inputs" in src else None

    def share(self, source, name, subs=None):
        """Publishes a source (file, or live stream) for the TV under a tokenized address; only one at a time.
        subs: .srt file served alongside (same token + "s"); its address is kept in sub_url."""
        token = secrets.token_urlsafe(16)
        base = f"http://{self.local_ip()}:{PORT}/media/"
        self.shared.clear()
        self.shared[token] = source
        self.sub_url = None
        if subs:
            self.shared[token + "s"] = subs
            self.sub_url = f"{base}{token}s/{quote(subs.name)}"
        return f"{base}{token}/{quote(name)}"

    def send(self, url, title, mime, features, size=""):
        """Tells the TV to play the address `url`, as given by share()."""
        kind = {"audio": "audioItem.musicTrack", "image": "imageItem.photo"}.get(mime.split("/")[0], "videoItem")
        subs = ""
        if self.sub_url:  # the three common ways to advertise external subtitles to a DLNA player
            su = html.escape(self.sub_url)
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
            self.soap("AVTransport", "Stop", I0)
        except Exception:
            pass  # nothing playing: the TV may refuse Stop
        # The TV answers these two only once it has opened the video: over a torrent, on a large file, that can take
        # tens of seconds (it fetches the start and the end of the file first). Status polls keep their short timeout.
        self.soap("AVTransport", "SetAVTransportURI",
                  f"{I0}<CurrentURI>{html.escape(url)}</CurrentURI><CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>", timeout=45)
        self.soap("AVTransport", "Play", I0 + "<Speed>1</Speed>", timeout=45)
        threading.Thread(target=self.watch_start, args=(next(iter(self.shared), ""),), daemon=True).start()

    def watch_start(self, token):
        """A few seconds after a launch: if the TV is already stopped without having been asked to, say so.
        Typical cause: a video or audio format the TV cannot decode."""
        job, launched = self.job, self.job.id
        time.sleep(12)
        try:
            if launched == job.id and not job.busy and token in self.shared and not job.stopped and not job.error:
                state = self.soap("AVTransport", "GetTransportInfo", I0).get("CurrentTransportState")
                if state in ("STOPPED", "NO_MEDIA_PRESENT"):
                    job.fail("The TV stopped right away. It probably cannot play this format.")
        except Exception:
            pass

    def start_live(self, live):
        """(Re)starts the stream on the TV at live["offset"]: a stream has no middle, seeking = restarting from there."""
        live["pos"] = live["offset"]  # until the TV plays the new stream, this is where we are
        self.send(self.share(live, "stream.ts"), live["title"], "video/mpeg", LIVE_FLAGS)
        return {"title": live["title"]}

    def live_position(self, live, s=None):
        """Second of the movie a live stream is at. s: what the TV just reported, when it is known to be playing this
        stream; without it, the last position seen, and before any was seen, the point the stream was started at."""
        if s is not None and next(iter(self.shared), "\0") in s.get("TrackURI", ""):
            live["pos"] = secs(s.get("RelTime")) + live["offset"]
        return live.get("pos", live["offset"])

    def position(self, live):
        try:
            self.status()  # refreshes live["pos"] when the TV is playing this stream
        except Exception:
            pass  # TV busy restarting: the last position seen stays valid
        return self.live_position(live)

    def status(self):
        s = {}
        s |= self.soap("RenderingControl", "GetVolume", I0 + "<Channel>Master</Channel>")
        s |= self.soap("RenderingControl", "GetMute", I0 + "<Channel>Master</Channel>")
        s |= self.soap("AVTransport", "GetTransportInfo", I0)
        s |= self.soap("AVTransport", "GetPositionInfo", I0)
        m = re.search(r"<dc:title>(.*?)</dc:title>", html.unescape(s.pop("TrackMetaData", "")))
        s["title"] = html.unescape(m[1]) if m else ""
        live = self.live()
        if live and next(iter(self.shared)) not in s.get("TrackURI", ""):
            # The stream was just (re)started and the TV has not switched to it yet: what it reports belongs to the
            # previous stream (or is 0). Reading that as the position sent the movie back to its start.
            s["RelTime"] = hms(self.live_position(live, s))
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
            s["RelTime"] = hms(self.live_position(live, s))
            s["TrackDuration"] = hms(live["duration"])
            s["title"] = live["title"]
        if live and live.get("subs"):
            s["subDelay"] = live.get("sub_delay", 0)
            s["subsNote"] = live.get("note", "")
        src = self.current()
        if isinstance(src, dict) and src.get("about"):
            s["about"] = src["about"]
        if torrent_hash(src):
            try:
                s["torrent"] = torrserver.health(src, secs(s.get("TrackDuration")), secs(s.get("RelTime")))
            except Exception:
                pass
        return s

    def public_status(self):
        """status() for the page, without the address the video is served at: that address is for the TV only.
        `playing` changes when the video changes, which is all the page needs to know."""
        s = self.status()
        uri = s.pop("TrackURI", "")
        s["playing"] = hashlib.sha1(uri.encode()).hexdigest()[:12] if uri else ""
        return s

    def thumb(self):
        """Thumbnail of the current video: poster provided by the catalog, else a frame extracted from the file by ffmpeg."""
        p = self.current()
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

    def record(self):
        """While the TV plays what we serve: counts the time played, and for a catalog movie records how far it has
        got (works with the page closed). Called every 10 s."""
        src = self.current()
        if src is None:
            return
        s = self.status()
        state = s.get("CurrentTransportState")
        if next(iter(self.shared)) not in s.get("TrackURI", "") or state not in ("PLAYING", "PAUSED_PLAYBACK"):
            return
        if state == "PLAYING":
            stats.count(src, s.get("title"))
        if isinstance(src, dict) and src.get("film"):
            progress.update(src["film"], position=secs(s.get("RelTime")), duration=secs(s.get("TrackDuration")))

    # the buttons of the player

    def transport(self, action, seconds=0):
        if action == "play":
            return self.soap("AVTransport", "Play", I0 + "<Speed>1</Speed>")
        if action in ("pause", "stop"):
            return self.soap("AVTransport", action.capitalize(), I0)
        if action == "seek":
            t = max(int(seconds), 0)
            live = self.live()
            if live:
                live["offset"] = t
                return self.start_live(live)
            return self.soap("AVTransport", "Seek", f"{I0}<Unit>REL_TIME</Unit><Target>{hms(t)}</Target>")
        raise ValueError(f"unknown action: {action}")

    def stop(self):
        self.job.stopped = True
        self.job.dismiss()
        try:
            return self.transport("stop")
        finally:
            self.shared.clear()  # stopped: nothing is served any more…
            only_torrent()  # …and nothing keeps downloading

    def sub_delay(self, a):
        """Shifts the burned-in subtitles by a["delta"] seconds (positive = later): the stream restarts at the same spot.
        The subtitle file does not always match the release of the movie; the shift is remembered for resume."""
        live = self.live()
        if not (live and live.get("subs")):
            raise ValueError("no burned-in subtitles currently playing")
        live["sub_delay"] = round(min(max(live.get("sub_delay", 0) + float(a["delta"]), -120), 120), 1)
        live["offset"] = self.position(live)
        progress.update(live.get("film"), sub_delay=live["sub_delay"])
        self.start_live(live)
        return {"subDelay": live["sub_delay"]}

    # launching a video, by kind of source

    def cast(self, path, burn=None):
        """A file of the Mac. burn: .srt file to burn into the picture (the video then goes through the live stream)."""
        p = allowed_file(path, MIME)
        mime = MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "video/mp4"
        title = re.sub(r" \[[\w-]{11}\]$", "", p.stem)
        if burn:
            burn = allowed_file(burn, {".srt"})
        duration, _, vcodec, acodec = probe(str(p)) if mime.startswith("video") else (0, None, "", "")
        convert = needs_convert(vcodec, acodec)
        if burn or convert:
            return self.start_live({"title": title, "duration": duration, "thumbnail": None, "offset": 0,
                                    "inputs": [(str(p), "")], "subs": burn, "convert": convert})
        srt = p.with_suffix(".srt")
        self.send(self.share(p, p.name, srt if srt.is_file() else None), title, mime, DLNA_FLAGS, f' size="{p.stat().st_size}"')
        return {"title": title}

    def stream_link(self, url):
        """The page of a video site."""
        return self.start_live(link_info(url))

    def cast_torrent(self, link):
        url, name, size = torrserver.file(link)
        return self.play_url(url, name, Path(name).stem, size)

    def play(self, a):
        """A source returned by a Stremio add-on."""
        url, name, size = add_on_source(a)
        title, film, start = progress.launched(a)
        return self.play_url(url, name, title, size, a.get("subs"), a.get("poster"), film, start, float(a.get("subDelay") or 0), sub_alts(a), film_facts(a))

    def play_url(self, url, name, title, size=None, subs=None, poster=None, film=None, start=0, delay=0, alts=(), facts=None):
        """Plays on the TV an address the Mac can read (local TorrServer or direct link), relayed by us.
        subs: address of a .srt to burn in → goes through the live stream (re-encoding).
        film: movie identifier, so that record() keeps the position; start: resume at this second."""
        torrent = torrserver.source(url, size)
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
            return self.start_live(live)
        mime = MIME.get(Path(name).suffix.lower(), "video/mp4")
        src = {"proxy": url, "title": title, "mime": mime, "thumbnail": poster, "torrent": torrent, "about": facts}
        self.send(self.share(src, name), title, mime, DLNA_FLAGS, f' size="{size}"' if size else "")
        if start:
            step(f"Resuming at {hms(start)}")
            for _ in range(40):  # the TV can only seek once playback has started
                if self.soap("AVTransport", "GetTransportInfo", I0).get("CurrentTransportState") == "PLAYING":
                    break
                time.sleep(0.5)
            self.soap("AVTransport", "Seek", f"{I0}<Unit>REL_TIME</Unit><Target>{hms(start)}</Target>")
        src["film"] = film  # only now: record() must not keep "0:03" before the resume seek
        return {"title": title}


# --- Playing in the browser: the same sources, played by the page's own <video> instead of the TV. Kept apart from the
# TV side: its own object (BrowserPlayer, below), with its own launches and source, its own addresses (/watch/<token>/…, for the page
# only, never for the TV). A browser reads fewer formats than the TV and no MPEG-TS: what it reads as is (MP4 with
# H.264 + AAC) is served as a file, with seeking; the rest goes through ffmpeg as fragmented MP4, an H.264 picture
# copied, any other re-encoded. Such a stream has no middle: to seek, the page asks for a new one (see watch_at).
# HEVC is re-encoded although Firefox and Chrome decode it: x265 movies have "open GOP" key frames (CRA), and Firefox
# never starts a stream on one (checked 2026-11-19: it reads on for hundreds of MB, waiting for an IDR frame), so the
# start of such a movie played and every seek hung.
# Subtitles are not burned in: the page shows them itself, from the .srt served as WebVTT.
# Checked with Firefox on the Mac; Safari and iOS would need HLS for the converted streams (not built).
# ponytail: one video at a time for all browsers; make `watching` hold one source per page if two people watch at once.

BROWSER_FILES = {".mp4", ".m4v", ".mov", ".mp3", ".m4a", ".wav"}  # containers a <video> reads without help


def browser_convert(vcodec, acodec):
    """Which parts have to be re-encoded for a browser: a list among "video", "audio".
    ponytail: 10-bit H.264 is copied although browsers do not decode it; read the pixel format in probe() if it shows up."""
    return [kind for kind, codec, ok in (("video", vcodec, {"h264"}), ("audio", acodec, {"aac", "mp3"})) if codec and codec not in ok]


def watch_subs(src):
    """What the page's player has to know about the subtitles; `subs` changes when the file to show does."""
    return {"subs": src["subs"].name if src.get("subs") else "", "subDelay": src.get("sub_delay", 0), "note": src.get("note", "")}


def watch_cmd(src, start):
    """start: where watch_at() said the stream can start."""
    if "video" in src["convert"]:
        # hardware H.264, at most 1080p: 8 Mb/s is too little for 4K. ponytail: HDR is not tone-mapped, its colours come out dull.
        video = ["-vf", "scale=-2:'min(1080,ih)'", "-c:v", "h264_videotoolbox", "-b:v", "8M", "-pix_fmt", "yuv420p"]
    else:
        video = ["-c:v", "copy"]
        if start and src["vcodec"]:
            start += 0.2  # ffmpeg aims a little before the second asked for (B-frames): asked for a key frame, it would start on the one before
    audio = ["-c:a", "aac", "-ac", "2", "-b:a", "192k"] if "audio" in src["convert"] else ["-c:a", "copy"]
    return (["ffmpeg", "-v", "error"] + ffmpeg_inputs(src["inputs"], start, ":0") + video + audio
            + ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])


def watch_at(src, t):
    """Second of the movie at which a stream asked for at `t` really starts. A copied picture can only start on a key
    frame: the one before t, up to several seconds earlier. The page needs the true figure, to show the right time and
    to place the subtitles."""
    t = max(float(t), 0)
    if not t or "video" in src["convert"] or not src["vcodec"]:
        return t  # a re-encoded picture starts exactly where asked
    u, headers = src["inputs"][0]
    try:
        r = subprocess.run(["ffprobe", "-v", "error", *(["-headers", headers] if u.startswith("http") else []), "-select_streams", "v:0",
                            "-read_intervals", f"{t}%+#1", "-show_entries", "packet=pts_time", "-of", "csv=p=0", u],
                           capture_output=True, text=True, timeout=20)
        key = float(r.stdout.split()[0].strip(","))
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return t
    return key if 0 <= key <= t else t


class BrowserPlayer:
    """The video the page's own player is playing, served at /watch/<token>/…"""

    def __init__(self):
        self.watching = {}  # token → source, one at most
        self.job = Launches()

    def current(self):
        return next(iter(self.watching.values()), None)

    def start(self, a):
        """Prepares a video for the page's player. a["kind"]: "file" (path), "link" (url: page of a video site, or torrent),
        else a source returned by a Stremio add-on, as for TVPlayer.play(). Returns what the page needs to play it."""
        kind, track, start = a.get("kind"), None, 0
        if kind == "file":
            p = allowed_file(a["path"], MIME)
            if MIME[p.suffix.lower()].startswith("image"):
                raise ValueError("pictures can only be shown on the TV")
            srt = p.with_suffix(".srt")
            src = {"title": re.sub(r" \[[\w-]{11}\]$", "", p.stem), "inputs": [(str(p), "")], "name": p.name, "file": p, "subs": srt if srt.is_file() else None}
        elif kind == "link" and not is_torrent(str(a.get("url") or "")):
            src = link_info(a["url"]) | {"name": "video", "vcodec": "h264", "acodec": "aac"}  # what sources.FORMAT asks the site for
        else:
            if kind == "link":
                url, name, size = torrserver.file(a["url"])
                title, film, facts = Path(name).stem, None, None
            else:
                url, name, size = add_on_source(a)
                (title, film, start), facts = progress.launched(a), film_facts(a)
            src = {"title": title, "inputs": [(url, "")], "name": name, "proxy": url, "film": film, "about": facts,
                   "thumbnail": a.get("poster"), "torrent": torrserver.source(url, size)}
        if "vcodec" not in src:
            step("Checking the format")
            src["duration"], track, src["vcodec"], src["acodec"] = probe(src["inputs"][0][0])
        if kind not in ("file", "link") and a.get("subs"):
            step("Preparing subtitles")
            src["sub_delay"] = float(a.get("subDelay") or 0)
            synced_subs(src, src["proxy"], a["subs"], sub_alts(a), track)
        src["convert"] = browser_convert(src["vcodec"], src["acodec"])
        src["direct"] = not src["convert"] and len(src["inputs"]) == 1 and Path(src["name"]).suffix.lower() in BROWSER_FILES
        if src["convert"]:
            step("Converting for the browser", " and ".join(f"{src[k[0] + 'codec'].upper()} {k}" for k in src["convert"]))
        src.update(token=secrets.token_urlsafe(16), seen=time.time(), mime=MIME.get(Path(src["name"]).suffix.lower(), "video/mp4"))
        self.watching.clear()
        self.watching[src["token"]] = src
        return {"token": src["token"], "title": src["title"], "duration": src.get("duration") or 0, "direct": src["direct"], "start": start,
                "about": src.get("about") or {}, "poster": src.get("thumbnail") or "", "torrent": bool(src.get("torrent")), **watch_subs(src)}

    def report(self, a):
        """Every 10 s the page says where its player is: counts the time played, records the position of a catalog movie,
        and answers with what may have changed meanwhile (subtitles corrected by the check, state of the torrent)."""
        src = self.watching.get(str(a.get("token")))
        if not src:
            raise LookupError
        now, pos = time.time(), max(float(a.get("position") or 0), 0)
        duration = src.get("duration") or int(float(a.get("duration") or 0))
        if a.get("playing") and now - src.get("counted", 0) >= 9:  # the figures cannot be inflated by reporting more often
            src["counted"] = now
            stats.count(src, src["title"])
        src["seen"] = now
        if src.get("subs") and isinstance(a.get("subDelay"), (int, float)) and a.get("subs") == src["subs"].name:
            src["sub_delay"] = round(min(max(a["subDelay"], -120), 120), 1)
        progress.update(src.get("film"), position=pos, duration=duration, sub_delay=src.get("sub_delay", 0) if src.get("subs") else None)
        out = watch_subs(src)
        if src.get("torrent"):
            try:
                out["torrent"] = torrserver.health(src, duration, pos)
            except Exception:
                pass
        return out

    def stop(self, a):
        """The page stopped its player, or is being closed: keeps the last position, then lets go of the source."""
        if str(a.get("token")) in self.watching:
            if "position" in a:
                self.report({**a, "playing": False})
            self.watching.clear()
            self.job.dismiss()
            only_torrent()

    def expire(self):
        """Lets go of a video whose page is gone (closed, asleep) without saying so. Called every 10 s."""
        w = self.current()
        if w and time.time() - w["seen"] > 900:
            self.watching.clear()
            only_torrent()


# --- The state of the server: one object per role, and what the two players share

config = Config(HERE / "config.json")
progress = Progress(HERE / "progress.json")
stats = Stats(HERE / "stats.json")
tv_player = TVPlayer()
browser = BrowserPlayer()


def only_torrent(keep=None):
    """One download at a time, and none in the background: drops every torrent TorrServer still holds except `keep`;
    without `keep`, except what the TV and the browser are playing. Called after every launch and when playback is
    stopped; a torrent that starts drops the others by itself (TorrServer.file)."""
    torrserver.only({keep} if keep else {torrent_hash(tv_player.current()), torrent_hash(browser.current())})


def track():
    """Every 10 s: see TVPlayer.record() and BrowserPlayer.expire()."""
    while True:
        time.sleep(10)
        try:
            browser.expire()
            tv_player.record()
        except Exception:
            pass  # TV off or busy: we retry on the next round


def verify_subs(live, url, track, duration, paths, memo):
    """In the background during playback: checks the subtitles and, if a better sync stands out, restarts the stream with it.
    The check reads two other parts of the movie, so it competes with playback for the torrent's bandwidth: it only runs
    when the download is comfortably faster than the movie, and stops as soon as something else is launched."""
    alive = lambda: tv_player.current() is live or browser.current() is live
    try:
        time.sleep(20)  # let playback buffer, and the download speed settle
        if not alive():
            return
        if live.get("torrent"):
            h = torrserver.health(live, duration)
            if h["speed"] < 2.5 * h["need"]:
                live["note"] = 'not checked: the source is too slow to check while playing. Adjust with the buttons if needed'
                return
        checked = subslib.check(url, track, duration, paths, memo, alive)
        if not checked:
            return
        srt, note, changed = checked
        live["note"] = note
        if changed and alive():
            live["subs"] = srt  # the browser's player learns it from its next report and reloads its subtitle track
            live["sub_delay"] = 0  # a delay set by hand was for the uncorrected file
            progress.update(live.get("film"), sub_delay=0)
            if tv_player.current() is live:
                live["offset"] = tv_player.position(live)
                tv_player.start_live(live)
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


# --- The Mac: its addresses on the local network, its files, housekeeping

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
    return [f"http://{host}:{PORT}/?key={LAN_KEY}" for host in (tv_player.local_ip(), *LOCAL_NAMES)] if LAN_KEY else []


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


# --- HTTP -------------------------------------------------------------------

def post_action(path, a):
    if path == "/api/key":
        return tv_player.need().press(a.get("event"))
    if path == "/api/tv":
        return tv_player.connect(str(a.get("ip", "")).strip())
    if path == "/api/volume":
        return tv_player.soap("RenderingControl", "SetVolume", f"{I0}<Channel>Master</Channel><DesiredVolume>{min(max(int(a['value']), 0), 100)}</DesiredVolume>")
    if path == "/api/transport":
        if a.get("action") == "stop":
            return tv_player.stop()
        return tv_player.transport(a.get("action"), a.get("seconds", 0))
    if path == "/api/cast":
        return tv_player.job.run(Path(str(a.get("path", ""))).name, tv_player.cast, a["path"], a.get("burn"))
    if path == "/api/youtube":
        return tv_player.dial("YouTube", "POST", f"v={youtube_id(a['url'])}".encode())
    if path == "/api/castlink":
        if is_torrent(a["url"]):
            return tv_player.job.run("Torrent", tv_player.cast_torrent, a["url"])
        return tv_player.job.run(urlparse(str(a["url"])).hostname or "Link",
                                 lambda: tv_player.cast(media_fetch(a["url"])) if a.get("mode") == "download" else tv_player.stream_link(a["url"]))
    if path == "/api/subdelay":
        return tv_player.sub_delay(a)
    if path == "/api/play":
        return tv_player.job.run(a.get("title") or "Movie", tv_player.play, a)
    if path == "/api/dismiss":
        return (browser if a.get("watch") else tv_player).job.dismiss()
    if path == "/api/watch":
        return browser.job.run(a.get("title") or Path(str(a.get("path") or "")).name or "Video", browser.start, a)
    if path == "/api/watchat":
        return {"start": watch_at(browser.watching[str(a.get("token"))], a.get("t") or 0)}
    if path == "/api/watching":
        return browser.report(a)
    if path == "/api/unwatch":
        return browser.stop(a)
    if path == "/api/config":
        return config.set_addons(a)
    if path == "/api/app":
        if a.get("name") not in APPS:
            raise ValueError("unknown app")
        if a.get("action") == "stop":
            return tv_player.dial(a["name"] + "/run", "DELETE")
        return tv_player.dial(a["name"], "POST", b"")
    raise LookupError


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
        return bool(LAN_KEY and ipaddress.ip_address(self.client_address[0]).is_private and host.lower() in (tv_player.local_ip(), *LOCAL_NAMES)
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
        if self.path.startswith("/watch/"):
            return self.watch(head)
        if self.path.startswith("/api/qr.png"):
            try:
                return self.reply(200, qr_png(lan_links()[0]), "image/png")
            except Exception:
                return self.reply(404, {"error": "no QR code"})
        if self.path.startswith("/api/thumb"):
            try:
                return self.reply(200, tv_player.thumb(), "image/jpeg")
            except Exception:
                return self.reply(404, {"error": "no thumbnail"})
        if self.path.split("?")[0] in ("/", "/index.html"):
            return self.reply(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/ytsearch?"):
            return self.run(yt_search, (parse_qs(urlparse(self.path).query).get("q") or [""])[0])
        routes = {
            "/api/history": lambda: {"links": history.links()},
            "/api/stats": stats.summary,
            "/api/status": tv_player.public_status,
            "/api/apps": tv_player.apps,
            "/api/files": lambda: {"dir": str(MEDIA_DIR), "files": list_files()},
            "/api/config": config.addons,
            "/api/links": lambda: {"links": lan_links()},
            "/api/job": tv_player.job.public,
            "/api/watchjob": browser.job.public,
            "/api/tvs": lambda: {"tvs": tvlib.discover(), "current": tv_player.tv.ip if tv_player.tv else None},
            "/api/channels": lambda: tv_player.need().channels(),
            "/api/progress": progress.list,
            "/api/info": lambda: {"tv": tv_player.tv.info() if tv_player.tv else None, "mac": tv_player.local_ip(), "port": PORT},
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
        p = tv_player.shared.get(parts[2]) if len(parts) > 2 else None
        # Only the selected TV may fetch the video: the TV tells anyone on the network what it is playing, token included
        if self.client_address[0] not in ("127.0.0.1", tv_player.tv.ip if tv_player.tv else ""):
            p = None
        if not p:
            return self.reply(404, {"error": "not found"})
        if isinstance(p, dict):
            return self.live(p, head) if "inputs" in p else self.proxy(p, head)
        self.file(p, head)

    def watch(self, head):
        """/watch/<token>/file | stream?t=<second> | subs.vtt?shift=<seconds>: the video the page's own player is playing."""
        u = urlparse(self.path)
        parts, q = u.path.split("/"), parse_qs(u.query)
        src, what = browser.watching.get(parts[2]) if len(parts) > 3 else None, parts[-1]
        try:
            number = float((q.get("t") or q.get("shift") or ["0"])[0])
        except ValueError:
            src = None
        if src and what == "subs.vtt" and src.get("subs"):
            return self.reply(200, vtt(src["subs"].read_text("utf-8", "replace"), number).encode(), "text/vtt; charset=utf-8")
        if src and what == "file" and src["direct"]:
            return self.file(src["file"], head) if src.get("file") else self.proxy(src, head)
        if src and what == "stream":
            still = lambda: browser.current() is src
            return self.pipe(watch_cmd(src, max(number, 0)), "video/mp4", head, still, browser.job.fail)
        self.reply(404, {"error": "not found"})

    def file(self, p, head):
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
        if tv_player.sub_url and p.suffix.lower() != ".srt":
            self.send_header("CaptionInfo.sec", tv_player.sub_url)
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
            tv_player.job.fail("The torrent engine stopped answering.")
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
                if tv_player.shared.get(self.path.split("/")[2]) is src:
                    tv_player.job.fail("The torrent stopped sending data.", "no data from TorrServer for two minutes")

    def live(self, live, head):
        self.pipe(live_cmd(live), "video/mpeg", head, lambda: tv_player.current() is live, tv_player.job.fail)

    def pipe(self, cmd, ctype, head, still, failed):
        """Sends what ffmpeg produces, as long as the other end reads it. still(): this is still the video being played;
        failed(sentence, detail): where to report a stream that broke."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", LIVE_FLAGS)
        self.send_header("Connection", "close")  # unknown length: end of stream = close
        self.end_headers()
        if head:
            return
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
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
            if not dropped and still() and (sent == 0 or (code not in (0, -9) and log)):
                failed(*stream_error(log))


def stream_error(log):
    """ffmpeg's error output → (sentence for the player, technical detail)."""
    lines = [l.strip() for l in log.splitlines() if l.strip()]
    detail = re.sub(r"^\[[^\]]*\]\s*", "", lines[-1])[:200] if lines else ""  # without the "[in#0/hls @ 0x…]" prefix
    if "403" in detail or "Forbidden" in detail:
        return "The site refused to send the video. Try again, or use “Download first”.", detail
    return "The video source stopped responding.", detail


def selftest():
    job, wjob = tv_player.job, browser.job
    for outside in ("/etc/hosts", "~/.ssh/id_rsa", str(MEDIA_DIR / ".." / "x.mkv"), str(HERE / "lan-key.txt"), str(HERE / "server.py")):
        try:
            allowed_file(outside, MIME)
            raise AssertionError("file outside the movies folder accepted: " + outside)
        except ValueError:
            pass
    assert needs_convert("av1", "dts") == ["video", "audio"] and needs_convert("hevc", "dts") == ["audio"] and needs_convert("h264", "aac") == []
    assert needs_convert("", "") == []  # unknown format: leave it alone
    assert "-request_size 10M" in " ".join(live_cmd({"offset": 0, "inputs": [("https://x/v", "")]}))
    assert "-request_size" not in live_cmd({"offset": 0, "inputs": [(torrserver.URL + "/stream/x", "")]})
    assert "-c:v copy -c:a aac" in " ".join(live_cmd({"offset": 0, "inputs": [("/a.mkv", "")], "convert": ["audio"]}))
    assert stream_error("") == ("The video source stopped responding.", "")
    assert stream_error("[in#0/hls @ 0x1] Error during demuxing: Input/output error")[1] == "Error during demuxing: Input/output error"
    assert "refused" in stream_error("x\n[https @ 0x2] Server returned 403 Forbidden (access denied)")[0]
    assert plain_error(ValueError("unrecognized link")) == ("Unrecognized link.", "")
    assert plain_error(RuntimeError("yt-dlp failed: ERROR: gone"))[1] == "ERROR: gone"
    # browser player: what is copied, what is re-encoded, where subtitles land after a seek
    assert browser_convert("hevc", "ac3") == ["video", "audio"] and browser_convert("h264", "mp3") == [] and browser_convert("h264", "") == []
    cmd = " ".join(watch_cmd({"inputs": [("/a.mkv", "")], "convert": ["audio"], "vcodec": "h264"}, 90))
    assert "-ss 90.2 -i /a.mkv -map 0:v:0? -map 0:a:0? -c:v copy -c:a aac" in cmd and cmd.endswith("-f mp4 pipe:1"), cmd
    assert "-ss 90 -i" in " ".join(watch_cmd({"inputs": [("/a.mkv", "")], "convert": ["video"], "vcodec": "hevc"}, 90))
    assert watch_at({"convert": ["video"], "vcodec": "hevc"}, 61.5) == 61.5 and watch_at({"convert": [], "vcodec": ""}, 5) == 5
    # a launch for the browser reports to its own stages, not to the TV's
    globals()["only_torrent"] = lambda keep=None: None  # the test must not drop the torrents of a server that is running
    wjob.run("here", lambda: step("for the browser"))
    assert wjob.steps == [["for the browser", ""]] and wjob.title == "here" and not job.steps and not job.title, (wjob.public(), job.public())
    step("a"); step("b", "1"); step("b", "2")
    assert job.steps == [["a", ""], ["b", "2"]]
    job.steps.clear()
    # check: a launch replaced by a newer one stops at its next stage and leaves the newer one's stages alone
    globals()["only_torrent"] = lambda keep=None: None  # the test must not drop the torrents of a server that is running
    go, out = threading.Event(), []
    def old_launch():
        step("old"); go.wait(5); step("old, later")
    def run_old():
        try:
            job.run("old", old_launch)
        except Superseded:
            out.append("stopped")
    th = threading.Thread(target=run_old); th.start()
    while job.steps != [["old", ""]]:
        time.sleep(0.01)
    job.run("new", lambda: (step("new"), go.set(), th.join()))
    assert out == ["stopped"] and job.steps == [["new", ""]] and job.title == "new" and not job.busy and not job.error, (out, job.public())
    job.steps.clear()
    day = lambda n: time.strftime("%Y-%m-%d", time.localtime(1e9 - 86400 * n))
    assert stats_summary({"seconds": {"movies": 50, "links": 20}, "days": {day(0): 30, day(6): 20, day(7): 20}, "plays": {"links": 3}}, 1e9) \
        == {"seconds": 70, "by": {"movies": 50, "links": 20}, "plays": {"links": 3}, "week": 50}
    # check: while the TV has not switched to a restarted stream, the position is the restart point, not the TV's counter
    lv = {"offset": 1200}
    tv_player.shared.clear(); tv_player.shared["tok"] = lv
    assert tv_player.live_position(lv) == 1200
    assert tv_player.live_position(lv, {"TrackURI": "http://mac/media/old/stream.ts", "RelTime": "0:00:03"}) == 1200
    assert tv_player.live_position(lv, {"TrackURI": "http://mac/media/tok/stream.ts", "RelTime": "0:00:30"}) == 1230 and tv_player.live_position(lv) == 1230
    tv_player.shared.clear()
    # check: the position of a catalog movie is recorded, an unknown duration keeps the one known, another movie is ignored
    with tempfile.TemporaryDirectory() as d:
        pr = Progress(Path(d) / "p.json")
        assert pr.launched({"title": "T", "film": {"id": "tt1"}, "start": 30}) == ("T", "tt1", 30)
        pr.update("tt1", position=95.7, duration=5400); pr.update("tt1", position=96, sub_delay=1.5); pr.update("nope", position=1)
        f = pr.films["tt1"]
        assert (f["position"], f["duration"], f["play"]) == (96, 5400, {"title": "T", "subDelay": 1.5}) and list(pr.films) == ["tt1"], f
        assert json.loads((Path(d) / "p.json").read_text()) == pr.films
    tvlib.selftest()
    sources.selftest()
    subslib.selftest()
    assert secs("0:02:05") == 125 and secs("NOT_IMPLEMENTED") == 0 and hms(3725) == "1:02:05"
    assert "setpts=PTS+(367.5)/TB" in " ".join(live_cmd({"offset": 369, "sub_delay": 1.5, "inputs": [("/a.mkv", "")], "subs": "/s.srt"}))
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
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # so that TorrServer stops with us
    tidy()
    if os.path.exists("/usr/bin/caffeinate"):  # macOS: stay awake while plugged in, for as long as this server runs
        subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())])
    torrserver.start()
    threading.Thread(target=track, daemon=True).start()
    tv_player.restore()
    print(f"TV: {tv_player.tv.name + ' (' + tv_player.tv.ip + ')' if tv_player.tv else 'to be picked in the page'} · movies: {MEDIA_DIR}\n→ http://127.0.0.1:{PORT}", flush=True)
    if LAN_KEY:
        print(f"→ local network: http://{tv_player.local_ip()}:{PORT}/?key={LAN_KEY}", flush=True)
        print(f"→ or by name:    http://{LOCAL_NAMES[0]}:{PORT}/?key={LAN_KEY}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

