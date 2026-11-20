"""Replays the subtitle check of subs.py on what collect.py recorded, and counts what it gets right.

    python3 tests/bench/replay.py [--others N] [--sound]

For each movie, every subtitle file recorded for it is checked as the check does it during playback: against two
excerpts of the embedded track first, then against the sound when that gives nothing (--sound: the sound only).
Then N files of each other movie (default 5) are given to it, which it must leave alone.

The truth is the speed and shift that best fit the six recorded excerpts of the embedded track, four more than the
check reads. An answer is counted:
  right      within 0.7 s of the truth all along the movie
  WRONG      further than that from a truth that fits the file well (half of the reference cues or more)
  doubtful   further than that from a truth that fits the file badly: nobody knows which is right
  left       no answer; "missed" when the truth fits the file well, so an answer was possible
  FALSE FIX  an answer for a file of another movie

Count a second or two per file: 20 minutes for 15 movies with the default N.
"""
import bisect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import subs  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def covered(starts, r, b, window, tol=0.3):
    """Share of the reference cues of `window` that a cue of the file, moved by (r, b), falls on."""
    moved = sorted(t * r + b for t in starts)
    at = [bisect.bisect_left(moved, u) for u in window]
    return sum(any(abs(u - moved[j]) < tol for j in (i - 1, i) if 0 <= j < len(moved)) for u, i in zip(window, at)) / max(len(window), 1)


def truth(cues, movie):
    """(speed, shift, share of the reference covered over the six excerpts, the same in the worst excerpt)."""
    starts = [a for a, _, _ in cues]
    windows = [w for _, w in movie["windows"] if len(w) >= 5]
    r, b, _, _ = subs.match_subs(starts, windows)
    per = [covered(starts, r, b, w) for w in windows]
    return r, b, sum(per) / len(per), min(per)


def check(movie, cues, sound_only=False):
    """What subs.check() would answer for this file, from the recorded excerpts: ("reference" or "sound", answer or None)."""
    starts = [a for a, _, _ in cues]
    found = None
    if not sound_only:
        # the two recorded excerpts closest to where the check would read them
        windows = [min(movie["windows"], key=lambda w: abs(w[0] - place))[1] for place in subs.ref_places(starts, movie["duration"])]
        if min(len(w) for w in windows) >= 10:
            found = subs.fit_subs([cues], windows)
    if found is not None:
        return "reference", found
    excerpts = [(start, z) for start, z in movie["stretches"][:3] if z]
    return "sound", subs.fit_by_sound([cues], excerpts) if len(excerpts) >= 2 else None


def main():
    others = int(sys.argv[sys.argv.index("--others") + 1]) if "--others" in sys.argv else 5
    sound_only = "--sound" in sys.argv
    movies = [json.loads(p.read_text()) for p in sorted(DATA.glob("tt*.json"))]
    if not movies:
        sys.exit("nothing recorded yet: see collect.py")
    for m in movies:
        for c in m["subs"]:
            c["cues"] = [tuple(x) for x in c["cues"]]
    t0, tally = time.time(), {}

    def count(what):
        tally[what] = tally.get(what, 0) + 1
    for i, m in enumerate(movies):
        print(f"{m['imdb']} {m['file'][:70]}", flush=True)
        for c in m["subs"]:
            cues = c["cues"]
            r0, b0, fit, worst = truth(cues, m)
            strong = fit >= 0.5 and worst >= 0.3
            how, found = check(m, cues, sound_only)
            if found is None:
                count("missed" if strong else "left")
                continue
            fixed = found[1]
            r = (fixed[-1][0] - fixed[0][0]) / (cues[-1][0] - cues[0][0])
            b = fixed[0][0] - cues[0][0] * r
            off = max(abs((r - r0) * t + (b - b0)) for t in (cues[0][0], cues[len(cues) // 2][0], cues[-1][0]))
            verdict = "right" if off <= 0.7 else "WRONG" if strong else "doubtful"
            count(verdict)
            count("  answered by the " + how)
            if verdict != "right":
                print(f"  {c['lang']} {verdict} by the {how}: speed {r:.4f} shift {b:+.1f} s · truth {r0:.4f} {b0:+.1f} s, covering {fit:.0%} (worst excerpt {worst:.0%}) · up to {off:.1f} s off")
        for o in movies[:i] + movies[i + 1:]:
            for c in o["subs"][:others]:
                how, found = check(m, c["cues"], sound_only)
                count("other movie: left" if found is None else "other movie: FALSE FIX")
                if found:
                    print(f"  FALSE FIX by the {how}: a file of {o['imdb']}: {found[2]}")
    print(f"\n{len(movies)} movies, {sum(len(m['subs']) for m in movies)} subtitle files, {'sound only' if sound_only else 'reference, then sound'} · {time.time() - t0:.0f} s")
    for what in sorted(tally, key=lambda w: (w.startswith("other"), w.startswith(" "), w)):
        print(f"  {what:32} {tally[what]}")


if __name__ == "__main__":
    main()
