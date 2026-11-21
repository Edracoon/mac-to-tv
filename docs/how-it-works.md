# How mac-to-tv works

What happens behind each button, what was measured, and the known limits. To install and run it, see
[guide.md](guide.md).

Five files, Python with no pip dependency and no build step: `server.py` (playback and API), `sources.py` (links,
torrents, add-on sources), `subs.py` (subtitles and their sync check), `tv.py` (TV discovery and control) and
`index.html` (the page). The project is a media center first; the remote for other brands grows by contribution: see
[the decision](adr/0001-media-center-first.md).

## Which TVs are compatible?

| | What you need | Status |
|---|---|---|
| **Playing videos** (the whole player, the catalog, subtitles) | A TV with a **DLNA/UPnP** network player ("MediaRenderer"), found on most smart TVs: Samsung, LG, Sony, Philips, Panasonic, Grundig, Beko… | Tested only on a **Grundig Ultralogic 4K** (Arçelik Smart Inter@ctive 4.0 OS). The rest is standard DLNA, not verified elsewhere. |
| **Full remote** (power/standby, mute, volume, backlight, motion smoothing, channel list) | A driver for the brand | Only one driver: **Arçelik group TVs** (Grundig, Beko…) running Smart Inter@ctive. |
| **Minimal remote** (volume, mute) | The TV's DLNA player | Automatic on any compatible TV without a brand driver. |
| **Opening the TV's YouTube app** | DIAL (standard) | Automatic if the TV advertises it. |

The TV is chosen in the page: a click on its name, at the top, starts a search on the local network. The TVs found
are listed with what will work; a TV detected without a network player is shown as "not compatible". The remote's
buttons adapt to the chosen TV.

Chromecast, Apple TV / AirPlay and Fire TV are not supported (no DLNA player).

**Your TV is not in the list?** Test it and add it: see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Movie catalog (Stremio add-ons)

The page queries Stremio-format add-ons directly (open protocol: `manifest.json`, `/catalog`, `/meta`,
`/stream`, `/subtitles`). Nothing to install: they are web services.

- Add-on list: `config.json` (created on the first change; copy it to another Mac to keep your add-ons).
- By default: Cinemeta (`https://v3-cinemeta.strem.io`, catalog and detail pages) and OpenSubtitles v3
  (`https://opensubtitles-v3.strem.io`, subtitles), the two official Stremio add-ons.
- No **source** add-on is provided: add some in the page ("Add-ons", below the catalog) by pasting
  the address of its `manifest.json`. Without one, the detail pages are shown but have nothing to play.
- A click on a source calls `POST /api/play`: torrent (`infoHash`) → TorrServer; direct link (`url`) → relayed as is.
- Subtitles chosen in the detail page → downloaded into `cache/subs/` then burned in (live stream, sound converted to stereo AAC).
- **Automatic subtitle selection and checking.** An add-on's file is often timed for another
  release of the movie. Two safeguards:
  1. the page ranks the files of a language by similarity between their release name and the video file name
     (WEB/BluRay/CAM source, resolution, year, group) — the "automatic" option of the detail page;
  2. during playback, the server checks in the background, against two witnesses found in the video itself:
     - **its own subtitles**: if the video file contains a text subtitle track (any language), the server reads two
       2 min excerpts of it, where the candidate has most lines, and looks for an offset and a speed that fit both;
     - **its sound**, when there is no such track, when it is nearly empty (forced subtitles) or when it is worded too
       differently to compare: people start talking when a subtitle appears, so the server reads the sound of the
       three most talkative stretches (10 min each) and looks for the offset and the speed at which the lines start
       where speech starts. All stretches must agree.

     If one candidate clearly stands out, it is kept (file changed and/or timings corrected) and the stream restarts
     at the same position; otherwise nothing is changed. The result is kept in `cache/subs/sync2-*` and the page
     announces it ("Subtitles checked…", "Subtitles shifted by…").

  Measured on 15 movies with 97 subtitle files (November 2026): 75 corrected or confirmed within half a second, 5 put
  one to two seconds from where they should be, 17 left alone, none made worse by a wrong correction. 896 files of
  other movies, given on purpose, were all left alone. On a 16th movie, not used to set the thresholds: 3 of 9 files
  right, 4 put about a second off by the sound check, 2 left alone. So the reference track is exact, and the sound is
  good to a second and a half; the "earlier" / "later" buttons do the rest.
  Limits: a file made for another cut of the movie (scenes added or removed) cannot be fixed by one offset: it is left
  alone, or put right for part of the movie only. The sound check does not conclude on a movie where people talk
  over each other all along. It reads half an hour of the movie, so over a torrent it only runs when the download is
  well ahead of playback; when the video could not be read, the check is made again at the next playback.
- **Manual adjustment**: the player's "earlier" / "later" buttons shift the subtitles in 0.5 s steps
  (`POST /api/subdelay {delta}`); the stream restarts at the same position and the offset is kept for resume.
  The burn-in itself is exact (checked with a clock video, including after a seek): pausing and resuming shift nothing.
- Series: seasons and episodes, with one resume position per episode. The next episode does not start by itself.

### Resume

- As long as a catalog movie is playing, the server records its position every 10 s in `progress.json` (a single profile),
  with the chosen source and subtitles. It works with the page closed.
- The page shows a "Continue or rewatch" row and, in the detail page, "Resume at xx:xx" / "Start over".
  A movie watched past 95% is marked as finished.
- The page address keeps the choice: `/?film=tt…&source=<torrent identifier>&file=<no.>&subs=<id>`.
  Reopening this link reopens the detail page with the source and subtitles preselected; nothing starts without a click.
- Starting from scratch: delete `progress.json` (server stopped).

## How each source is played

| Source | Path |
|---|---|
| File from the Mac | served as is to the TV (with Range) |
| YouTube & co link | `yt-dlp` gives the addresses → `ffmpeg` muxes to MPEG-TS without re-encoding → TV. A seek restarts the stream. |
| Magnet link / torrent | TorrServer downloads in playback order (in memory) → relayed to the TV like a file |
| "Download first" | `yt-dlp` downloads into `cache/`, then the file is sent (fallback) |
| Catalog movie | source chosen in the detail page → TorrServer or direct link, relayed to the TV |
| With subtitles | goes through the live stream, subtitles burned in (chosen in the movie's detail page; for a local file: `POST /api/cast {path, burn}`, no button) |

Known limits: Arte does not start at the beginning of a movie (unresolved); multi-file torrents → largest video file.

### Torrent speed and buffer

A torrent slower than the movie's bitrate stutters, whatever the Mac or the TV do. Nothing is refused for being slow;
the player shows what is going on instead:

- a light grey bar on the seek bar: the part of the movie already downloaded after the playback position;
- a line under the player: connected sources, download speed, speed the movie needs, and time buffered;
- a warning when the source is slower than the movie. Pausing lets the buffer grow, as in other streaming players.

TorrServer holds up to 512 MB of the movie in memory ahead of playback (`TorrServer.CACHE` in `sources.py`).

**One download at a time, nothing in the background.** Every other torrent is dropped before a torrent starts, after
every launch (successful or not) and when playback is stopped (`only_torrent()`). The background subtitle check reads
the torrent that is playing, never another one; it only runs when the download is at least 2.5 times faster than the
movie, and it is cancelled if another video is launched.

### Formats the TV cannot decode

Before sending a video (file or catalog movie), the server reads its codecs with `ffprobe` and compares them with what
the TV decodes (`TV_VIDEO`, `TV_AUDIO` in `server.py`). When something does not fit, the video goes through the live
stream and only that part is re-encoded: AV1 video becomes H.264, DTS audio becomes stereo AAC, the rest is copied.
The launch shows a "Converting for the TV" stage. As with burned-in subtitles, seeking then restarts the stream.

Checked on the Grundig with generated clips: H.264 and HEVC (8 and 10 bit) play as they are; AV1 makes the TV stop at
once. Audio support cannot be detected from the server (a silent movie still "plays"): AAC, MP3 and Dolby Digital are
assumed fine, everything else is converted. HDR video that has to be re-encoded is not tone-mapped yet.

### Playing in the browser instead of the TV

Every "Play" button comes with a switch, **On the TV / Here** (remembered per browser). "Here" plays the same
source in the page's own player: nothing is sent to the TV, which can keep playing something else, and no TV is
needed at all. The two sides share what comes before playback (catalog, sources, torrents, subtitles download,
resume positions, figures) and nothing after it:

| | On the TV | Here |
|---|---|---|
| Server state | the `TVPlayer` object, addresses under `/media/<token>/` (open to the TV) | the `BrowserPlayer` object, addresses under `/watch/<token>/` (the page only) |
| MP4 with H.264 + AAC/MP3 | served as a file | served as a file: seeking is instant |
| MKV, YouTube, AC3/DTS sound, HEVC, AV1… | file, or MPEG-TS live stream | fragmented MP4 from `ffmpeg`: H.264 picture copied, anything else re-encoded (hardware H.264, 1080p at most), sound to stereo AAC |
| Seeking in a stream | restarts the stream | restarts the stream (`/api/watchat` says where it really starts: a copied picture starts on the key frame before) |
| Subtitles | burned into the picture | a text track drawn by the browser (`.srt` served as WebVTT): changing the delay is instant |
| Position, time played | read from the TV every 10 s | reported by the page every 10 s (`/api/watching`) |

While a video plays here and the TV answers, a **Switch to the TV** button under the player hands it over: same
source, same point, same subtitles and delay; the torrent keeps what it has already downloaded.

The automatic subtitle check also runs for a video played here; the page reloads its subtitle track when a
better-timed file is found.

Limits: checked with **Firefox on the Mac** only. Safari and iOS would need HLS for the converted streams (not built).
HEVC is always re-encoded: Firefox does not start a stream on the "open GOP" key frames x265 movies are made of, so
copying it worked at the start of a movie and hung after every seek. Only the first sound track is played. One video
at a time for all browsers, and still one torrent at a time for the TV and the browser together: starting a torrent on
one side stops the other side's. HDR that is re-encoded is not tone-mapped. A page closed without stopping lets go of
its torrent at once when the browser says so, after 15 minutes otherwise.

### Launch stages and failures

Starting a link or a torrent takes a while, and a stream can break after it was sent. Both are shown in the player
area of the page rather than in pop-ups:

- while launching: a spinner, what is being started, and the stages passed so far ("Reading the link",
  "Finding torrent sources — 3 connected", "Preparing subtitles", "Starting the TV");
- on failure: one plain sentence ("This link could not be read.", "The torrent stopped sending data.", "The TV stopped
  right away. It probably cannot play this format."), the technical line behind "Technical details", and
  "Try again" / "Dismiss".

The server publishes this state on `GET /api/job` (`busy`, `title`, `steps`, `error`, `detail`); any open page shows
it, including one that did not start the launch. `POST /api/dismiss` clears a failure.

## TV control (`tv.py`)

Each capability is detected separately, for each device found by SSDP:

| Capability | How | Class |
|---|---|---|
| Player | UPnP services `AVTransport` and `RenderingControl`, addresses read from the device description | `TV.soap()` |
| Remote | the first driver in the `REMOTES` list whose `probe()` answers | `ArcelikRemote`, then `UpnpRemote` |
| Apps | DIAL: address given by the `Application-URL` header | `TV.dial()` |

**Adding a brand**: write a class with `label`, `keys` (among `power`, `mute`, `v+`, `v-`, `bl+`, `bl-`, `memc`),
`has_channels`, `probe(tv)` and `press(key)`, then put it at the head of `REMOTES`. The UI shows the advertised keys
by itself. Known leads, not written for lack of TVs to test them on: Samsung (WebSocket 8001/8002, on-screen pairing),
LG webOS (WebSocket 3000, pairing), Sony Bravia (IRCC over HTTP, pre-shared key), Roku (ECP over HTTP, port 8060),
Philips (JointSpace), Android TV (encrypted pairing, the heaviest).

Check what discovery sees: `python3 tv.py`.

The step-by-step guide to test your TV, fix playback or write a driver is in [CONTRIBUTING.md](../CONTRIBUTING.md).

### Arçelik driver (Grundig, Beko… running Smart Inter@ctive), probed on 2026-11-17

| Port | Service | Use here |
|---|---|---|
| variable (found by SSDP) | DLNA player | play, pause, seek, volume |
| 8085 (and 443) | WebSocket `/foo` without authentication ("remote tester" left by the manufacturer) | keys `power`, `mute`, `v+`, `v-`, `bl+`, `bl-`, `memc` |
| 8085 `/list` | list of digital terrestrial (DTT) channels (JSON) | display only |
| 56789 / 56790 | DIAL | open the YouTube app (Netflix answers 503) |

Quirks observed on this TV (other DLNA players may behave differently):
- the DLNA player's port changes every time the player restarts → addresses re-read automatically;
- files must be served over HTTP/1.1 with Range, otherwise the TV stays on "loading" and refuses pause and seek;
- formats played: MKV, MP4, MOV, AVI, MPEG-TS, FLV; H.264 + AAC is the safe combination;
- **subtitles**: the TV ignores external `.srt` files and does not display embedded tracks (MKV/MP4) →
  burn-in into the picture with `ffmpeg-full` (`h264_videotoolbox` hardware re-encoding). It is also the most
  portable method: it does not depend on the TV.

## Tests

```sh
python3 -m unittest                              # everything, about 15 s
python3 -m unittest discover -s tests/unit -t .  # unit tests only: no network, no ffmpeg
python3 -m unittest discover -s tests/e2e -t .   # the server driving a fake TV
```

- `tests/unit/`: the functions that can be checked alone (links, subtitle sync, ffmpeg commands, launches, files kept
  on disk), one file per module.
- `tests/e2e/`: the real server, asked through its HTTP API like the page does, driving `fake_tv.py`: a small DLNA
  player that runs on the Mac, answers the same commands as a TV and really fetches the video. It reproduces what
  caused bugs on the real one: a TV that keeps reporting the previous video for a few seconds, one that stops right
  away on a format it cannot decode. These tests need `ffmpeg` and `ffprobe` (the clips are generated); the one on
  burned-in subtitles also needs `ffmpeg-full` and is skipped without it.

The tests use no real TV and no torrent engine, and write nothing to the files of an installation.

`tests/bench/` is apart: two scripts, run by hand, that measure the automatic subtitle check on real movies
(see [its README](../tests/bench/README.md)). The thresholds of the check come from there.
