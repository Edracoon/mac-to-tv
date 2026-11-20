# Measuring the subtitle check on real movies

The thresholds of the automatic subtitle check (`subs.py`) were set from measurements on real movies. These two
scripts make them again, to be run by hand after a change to the check. They are not part of `python3 -m unittest`:
recording downloads videos by torrent and takes minutes per movie.

```sh
python3 tests/bench/collect.py tt0110912 tt6751668 …   # once per movie: records what the check reads
python3 tests/bench/replay.py                          # as often as needed: replays the check on the recordings
```

## 1. Recording (`collect.py`)

Give it IMDb identifiers (the `tt…` in the address of a movie page). For each movie it picks a torrent source from
the add-ons of `config.json`, keeps one whose video file has a text subtitle track, and records:

- six 2-minute excerpts of that track, along the movie: the reference;
- the sound of the four 10-minute stretches where the subtitles have most lines;
- the subtitle files the add-ons list (up to 8 English, 5 French).

A source that is too slow, or whose track is nearly empty, is skipped for the next one. The videos are read through
a second torrent engine on port 8091, so a server in use on the same Mac is not disturbed. Count 5 to 15 minutes and
about 1 GB of download per movie.

The recordings go to `tests/bench/data/`, which git ignores: they hold the text of the subtitle files. Pick varied
movies: talkative and quiet, old and recent, several languages.

## 2. Replaying (`replay.py`)

Runs the functions of `subs.py` on the recordings, as the check does during playback, and prints what it got right.
`--sound` replays the sound check alone; `--others N` sets how many files of each other movie are given on purpose
(default 5, 0 to skip them).

The truth is the speed and shift that best fit the six recorded excerpts; the check itself only reads two.

| Line | Meaning | What to aim for |
|---|---|---|
| `right` | the answer is within 0.7 s of the truth all along the movie | as many as possible |
| `left` | no answer | fine: these files fit no speed and shift |
| `missed` | no answer although the truth fits the file well | few |
| `doubtful` | the answer is further from a truth that itself fits the file badly | few; nobody knows which is right |
| `WRONG` | the answer is further from a truth that fits the file well | **none** |
| `other movie: FALSE FIX` | an answer for a file of another movie | **none** |

A change to the check is worth keeping when it adds `right` answers without adding a `WRONG` or a `FALSE FIX`.

## Figures of November 2026

15 movies, 97 subtitle files, `--others 5` (896 files of other movies): 75 right (48 by the reference, 27 by the
sound), 5 doubtful, 17 left, none missed, wrong or falsely fixed.
