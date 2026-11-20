"""Records, for each movie given, what the subtitle check reads, so that it can then be replayed offline (replay.py):
excerpts of the subtitle track embedded in the video, the sound of its most talkative stretches, and the subtitle
files the add-ons list for it.

    python3 tests/bench/collect.py tt0110912 tt6751668 …      # IMDb identifiers, as in the address of a movie page

Sources and subtitle files come from the add-ons of config.json. The videos are read through a second torrent engine
(port 8091), started and stopped here: the one of a running server is left alone. Count 5 to 15 minutes and about
1 GB of download per movie; a movie already recorded is skipped. Results go to tests/bench/data/, which git ignores.
"""
import atexit
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import sources  # noqa: E402
import subs  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
WINDOWS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)  # where the reference track is read: more places than the check itself reads, to judge it
engine = sources.TorrServer()
engine.URL = "http://127.0.0.1:8091"


def start_engine():
    exe, home = ROOT / "bin" / "TorrServer", DATA / "engine"
    if not exe.exists():
        sys.exit("bin/TorrServer is missing (see the README)")
    home.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([exe, "--ip", "127.0.0.1", "--port", "8091", "--path", home, "--logpath", home / "ts.log"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(proc.terminate)
    for _ in range(40):
        try:
            return engine.call("get", _path="/settings")
        except RuntimeError:
            time.sleep(0.5)
    sys.exit("the second torrent engine did not start")


def jget(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        return json.load(r)


def addons(kind, imdb):
    """What every add-on of config.json answers for this movie: kind "stream" or "subtitles"."""
    out = []
    for a in json.loads((ROOT / "config.json").read_text())["addons"]:
        try:
            out += jget(f"{a}/{kind}/movie/{imdb}.json").get("streams" if kind == "stream" else "subtitles") or []
        except Exception:
            pass  # an add-on that does not offer this kind
    return out


def torrents(imdb):
    """Torrent sources worth trying, best first: MKV (more often with an embedded subtitle track), 1 to 3 GB, most seeded."""
    out = []
    for s in addons("stream", imdb):
        if s.get("infoHash"):
            text = s.get("title") or s.get("description") or ""
            name = (s.get("behaviorHints") or {}).get("filename") or text.split("\n")[0]
            seeds = int((re.search(r"👤\s*(\d+)", text) or [0, 0])[1])
            size = re.search(r"💾\s*([\d.]+)\s*(GB|MB)", text)
            gb = float(size[1]) * (1 if size[2] == "GB" else 0.001) if size else 0
            out.append((not name.lower().endswith(".mkv"), not 0.9 <= gb <= 3, -seeds, name, s))
    return [(name, s) for *_, name, s in sorted(out, key=lambda x: x[:3])]


def subtitle_files(imdb, per_language={"eng": 8, "fre": 5}):
    out = []
    for s in addons("subtitles", imdb):
        if s.get("url") and sum(c["lang"] == s.get("lang") for c in out) < per_language.get(s.get("lang"), 0):
            try:
                with urllib.request.urlopen(urllib.request.Request(s["url"], headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
                    cues = subs.parse_srt(r.read(5 << 20).decode("utf-8", "replace"))
            except Exception as e:
                print("  subtitle file not fetched:", e)
                continue
            if len(cues) > 100:
                out.append({"lang": s["lang"], "id": s.get("id"), "cues": cues})
    return out


def record(imdb):
    out = DATA / f"{imdb}.json"
    if out.exists():
        return print(imdb, "already recorded")
    files = subtitle_files(imdb)
    print(imdb, len(files), "subtitle files", flush=True)
    if not files:
        return
    for name, s in torrents(imdb)[:8]:
        try:
            url, name, size = engine.file(s["infoHash"], s.get("fileIdx"), (s.get("behaviorHints") or {}).get("filename"))
        except Exception as e:
            print(f"  {name[:60]}: {e}")
            continue
        duration, track, _, _ = subs.probe(url)
        print(f"  {name[:70]} · {size / 1e9:.1f} GB · {duration} s · subtitle track {track}", flush=True)
        if duration < 900 or track is None:
            continue
        t0 = time.time()
        if not subs.loudness(url, duration // 2, 60) or time.time() - t0 > 20:
            print("    too slow: a minute of sound must come in well under a minute")
            continue
        windows = []
        for f in WINDOWS:
            t0, start = time.time(), int(duration * f)
            w = subs.ref_window(url, track, start)
            windows.append([start, [] if time.time() - t0 > 85 else w])  # cut short by the watchdog: incomplete, so misleading
            print(f"    reference at {start} s: {len(windows[-1][1])} cues", flush=True)
            if len(windows) == 2 and max(len(w) for _, w in windows) < 10:
                break  # a track with next to nothing in it (forced subtitles)
        if len(windows) < len(WINDOWS):
            print("    no usable reference track")
            continue
        stretches = []
        for t in subs.dense_stretches([a for a, _, _ in files[0]["cues"]], duration, n=4):
            z = subs.loudness(url, max(t - 60, 0), 600)
            stretches.append([max(t - 60, 0), [round(x, 3) for x in z]])
            print(f"    sound at {max(t - 60, 0)} s: {len(z)} values", flush=True)
        if sum(len(w) >= 10 for _, w in windows) < 4 or sum(bool(z) for _, z in stretches) < 3:
            print("    too little came through")
            continue
        out.write_text(json.dumps({"imdb": imdb, "file": name, "duration": duration, "track": track,
                                   "subs": files, "windows": windows, "stretches": stretches}))
        return print("  recorded", flush=True)
    print("  no source with a usable subtitle track")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if os.path.exists("/usr/bin/caffeinate"):  # a Mac that falls asleep halfway records empty excerpts
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    start_engine()
    for movie in sys.argv[1:]:
        try:
            record(movie)
        except Exception as e:
            print(movie, "failed:", repr(e), flush=True)
    engine.only(set())
