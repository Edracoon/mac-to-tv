"""mac-to-tv: subtitles. Fetching the .srt files, reading and writing them, and checking that a file is in sync with
the movie (against a subtitle track embedded in the video, else against its sound). Nothing here knows about the TV
or the page; server.py decides what to do with the result.

Tests: tests/unit/test_subs.py
"""
import bisect
import hashlib
import json
import math
import re
import subprocess
import threading
import time
import urllib.request

from sources import CACHE

# --- Automatic check ---
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


# --- Against the sound of the movie: when the reference track is missing, nearly empty, or gives no clear answer ---
# People start talking when a subtitle appears: the speech band gets louder right at the start of a cue. We read
# the sound of the three stretches where the file has most cues and look for the shift, and the speed, at which that
# holds best. We only act when the stretches agree.
# Measured on 15 movies (2026-11-20; 97 subtitle files, their truth taken from six excerpts of the embedded track): a file
# that fits scores 7 to 14 deviations above the other shifts overall, and 4.3 to 9.8 in each stretch taken alone. Files
# of the other movies, tried at every speed, never went above 5.1 overall or 3.5 in a stretch (7 movies looked at), and
# none of 896 passed the rule below. On the movies' own files it answered 71 times: 66 within half a second of the
# truth, 5 one to two seconds from it, on files the reference itself only half fits (made for another cut, probably).
# ponytail: where it fails. A movie of people talking over each other in one room (12 Angry Men: 7 files unanswered,
# all settled by the reference track), and a file made for another cut, which no single shift puts right.

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


def sound_shift(starts, excerpts, reach=90):
    """excerpts: [(second it starts at, loudness)]. Returns (shift b such that the cues at starts + b fall where
    speech begins, clear?, score) — clear: one shift stands out overall and each excerpt, taken alone, points to it;
    score: how far it stands out, to compare two clear answers."""
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
        return 0.0, False, 0.0
    k, z, z2 = peak([sum(col) for col in zip(*per)])
    heard = [p for p in map(peak, per) if p[1] > 0]  # an excerpt where the file has no cue says nothing (a file that stops early)
    # 1.2 s between excerpts: some files wander by a second along the movie, and one shift still puts them right
    clear = (len(heard) >= 2 and z >= 6 and z2 <= 0.6 * z
             and all(abs(k1 - k) <= 12 and z1 >= (4.5 if len(heard) >= 3 else 6) for k1, z1, _ in heard))
    return (k - reach * 10) / 10, clear, z


def sound_excerpts(url, duration, starts, alive=lambda: True):
    """Reads the sound of the stretches where a file with cues at `starts` has most of them → [(start, loudness)];
    None when `alive()` said meanwhile that it is no longer wanted. About 30 minutes of the movie have to be read."""
    excerpts = []
    for t in dense_stretches(starts, duration):
        start = max(t - 60, 0)
        z = loudness(url, start, 600, alive)
        if not alive():
            return None
        # A read that hiccups (a torrent piece late or broken) gives sound with a hole in it: what follows the hole
        # comes too early, and that stretch would contradict the others. Seen live: 5968 values instead of 6000.
        if len(z) >= min(600, duration - start) * 10 - 3:
            excerpts.append((start, z))
    return excerpts


def fit_by_sound(candidates, excerpts):
    """Same contract as fit_subs, against the sound read by sound_excerpts()."""
    found = []
    for i, cues in enumerate(candidates):
        fits = [(z, r, b) for r in SPEEDS for b, clear, z in [sound_shift([a * r for a, _, _ in cues], excerpts)] if clear]
        if fits:  # at a neighbouring speed the stretches can still agree, but less sharply: the best score is the right speed
            found.append((i, *max(fits)))
            if i == 0:
                break  # the preferred file fits: no need for another
    if not found:
        return None
    i, _, r, b = max(found, key=lambda f: f[1])
    if r == 1 and abs(b) <= 0.5:  # speech starts a moment after its line appears: half a second is not a file to correct
        note, cues = "checked against the sound: in sync", candidates[i]
    else:
        note = f"shifted by {b:+.1f} s to match the sound" + (" (speed corrected)" if r != 1 else "")
        cues = [(a * r + b, e * r + b, x) for a, e, x in candidates[i]]
    return i, cues, note + (" — switched to a file that matches this release better" if i else ""), bool(i) or cues is not candidates[i]


SPEEDS = (1, 24 / 23.976, 23.976 / 24, 25 / 23.976, 23.976 / 25, 25 / 24, 24 / 25)


def match_subs(starts, windows, reach=120, tol=0.3):
    """Best (speed r, shift b) such that starts × r + b lands on the cues of both excerpts.
    Returns (r, b, clear?, score) — clear: the best score dominates the 2nd distinct peak and the typical score, and
    enough of each excerpt is covered."""
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
    r, b = best[3], best[4]
    moved = sorted(t * r + b for t in starts)

    def covered(w):  # share of the reference cues that a cue of the file lands on
        at = [bisect.bisect_left(moved, u) for u in w]
        return sum(any(abs(u - moved[j]) < tol for j in (i - 1, i) if 0 <= j < len(moved)) for u, i in zip(w, at)) / len(w)
    # Measured on 15 movies (2026-11-20): with the three conditions on the scores alone, 5 files of another movie out of
    # 896 came out as "clear", all at a 4 % speed change, where many cues fall on a reference cue by chance; they covered
    # 28 to 35 % of the worse excerpt. Right answers covered 41 % and more (one exception at 30 %), 58 % and more at 4 %.
    enough = 0.5 if abs(r - 1) > 0.01 else 0.4
    clear = (best[1] >= 8 and best[0] >= 1.2 * second[0] and best[0] >= 2 * max(typical, 1)
             and min(covered(w) for w in windows if w) >= enough)
    return r, b, clear, best[0]


def ref_places(starts, duration):
    """Where to read the two excerpts of the reference track: the two 2-minute stretches where the file has most cues,
    at least 30 % of the movie apart. At fixed places (10 % and 70 %, as before) one of them often fell in a scene
    without a word: on the 7 movies measured, the check then concluded for 11 files out of 42, against 19 this way."""
    picks = dense_stretches(starts, duration, length=120, apart=int(duration * 0.3), n=2, least=10)
    return picks if len(picks) == 2 else [int(duration * f) for f in (0.10, 0.70)]


def fit_subs(candidates, windows):
    """candidates: lists of cues, the preferred one first. Returns (chosen index, cues, note, changed?), or None when
    no file clearly fits the reference."""
    found = []
    for i, cues in enumerate(candidates):
        r, b, clear, score = match_subs([a for a, _, _ in cues], windows)
        if clear:
            found.append((score, -i, i, r, b))
    if not found:
        return None
    _, _, i, r, b = max(found)
    if r == 1 and abs(b) < 0.3:
        note, cues = "checked: in sync", candidates[i]
    else:
        note = f"shifted by {b:+.1f} s" + (" (speed corrected)" if r != 1 else "")
        cues = [(a * r + b, e * r + b, x) for a, e, x in candidates[i]]
    return i, cues, note + (" — switched to a file that matches this release better" if i else ""), bool(i) or cues is not candidates[i]


def fetch_subs(url):
    if not re.match(r"https?://", url or ""):
        raise ValueError("invalid subtitles address")
    out = CACHE / "subs" / (hashlib.sha1(url.encode()).hexdigest() + ".srt")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            out.write_bytes(r.read(5 << 20))
    return out


def vtt(text, shift=0):
    """.srt text → WebVTT, every cue moved by `shift` seconds (cues that end before 0 are dropped)."""
    def stamp(x):
        ms = round(max(x, 0) * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"
    return "WEBVTT\n\n" + "\n".join(f"{stamp(a + shift)} --> {stamp(b + shift)}\n{re.sub(r'{\\[^}]*}', '', x).strip()}\n"
                                    for a, b, x in parse_srt(text) if b + shift > 0)


def check(url, track, duration, paths, memo, alive=lambda: True):
    """Checks the candidate .srt files `paths` (the preferred one first) against the video at `url`; track: index of
    its embedded text subtitle track, None when it has none. Returns (file to show, note for the page, changed?),
    remembered in `memo` — or None when `alive()` said meanwhile that the result is no longer wanted.
    This reads other parts of the movie than the one being played: a few minutes for the reference track, and half
    an hour of sound when that gives nothing."""
    candidates = [parse_srt(p.read_text("utf-8", "replace")) for p in paths]
    starts = [a for a, _, _ in candidates[0]]
    found = None
    if track is not None:
        windows = [ref_window(url, track, t, alive=alive) for t in ref_places(starts, duration)]
        if not alive():
            return None
        if min(len(w) for w in windows) >= 10:
            found = fit_subs(candidates, windows)
    if found is None:
        # no embedded track, one with next to nothing in it (forced subtitles), or one worded too differently to be
        # compared (another language, cut into other lines): the sound of the movie is the other witness
        excerpts = sound_excerpts(url, duration, starts, alive)
        if excerpts is None:
            return None
        if len(excerpts) < 2:  # the source is too slow, or broken there: say so, and try again at the next playback
            return paths[0], "not checked: the video could not be read. Adjust with the buttons if needed", False
        found = fit_by_sound(candidates, excerpts)
    if found is None:
        srt, note, changed = paths[0], "not checked: no clear answer from the video. Adjust with the buttons if needed", False
    else:
        i, cues, note, changed = found
        srt = paths[i]
        if changed:
            srt = memo.with_suffix(".srt")
            srt.write_text(write_srt(cues), "utf-8")
    memo.write_text(json.dumps({"srt": str(srt), "note": note}))
    return srt, note, changed
