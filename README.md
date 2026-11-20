# mac-to-tv

Local web UI, served by a Mac, to control a TV on the local network and play videos on it: YouTube-style player,
Stremio-style movie catalog, files from the Mac, ad-free YouTube links, torrents, burned-in subtitles,
remote. The same videos can also be played in the browser itself, without the TV.

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

**Your TV is not in the list?** Test it and add it: see [CONTRIBUTING.md](CONTRIBUTING.md).

Five files, Python with no pip dependency: `server.py` (playback and API), `sources.py` (links, torrents, add-on
sources), `subs.py` (subtitles and their sync check), `tv.py` (TV discovery and control) and `index.html` (the page).

The project is a media center first; the remote for other brands grows by contribution. No `pip` dependency and no
build step: see [the decision](docs/adr/0001-media-center-first.md).

## Installing on a new Mac

Requirements: macOS, [Homebrew](https://brew.sh), and the Mac on the same local network as the TV.

```sh
# 1. Tools (Python 3 comes with yt-dlp; so does deno, which yt-dlp needs for YouTube)
brew install yt-dlp ffmpeg ffmpeg-full

# 2. The project
git clone https://github.com/Edracoon/mac-to-tv.git && cd mac-to-tv

# 3. Torrent engine: TorrServer (not in Homebrew, not in the repository), version MatriX.147.2
#    Apple Silicon Mac: TorrServer-darwin-arm64 — Intel Mac: TorrServer-darwin-amd64
mkdir -p bin
curl -L -o bin/TorrServer \
  https://github.com/YouROK/TorrServer/releases/download/MatriX.147.2/TorrServer-darwin-arm64
chmod +x bin/TorrServer

# 4. Check
python3 -m unittest
```

| Tool | Role | Required? |
|---|---|---|
| `python3` (≥ 3.10) | runs `server.py` | yes |
| `yt-dlp` | finds the video streams of a link (YouTube…) | for links |
| `ffmpeg` | muxes picture + sound on the fly, thumbnails | for links |
| `ffmpeg-full` | burns the subtitles into the picture (Homebrew's standard `ffmpeg` lacks the `subtitles` filter) | for subtitles |
| `bin/TorrServer` | progressive playback of torrents | for torrents |

Versions used during development (November 2026): Python 3.15.2, yt-dlp 2026.10.26, ffmpeg 9.0.2, TorrServer MatriX.147.2.
YouTube changes often: if a link fails, start with `brew upgrade yt-dlp`.

## Running

```sh
python3 server.py [movies_folder] [--lan]     # default folder: ~/Movies; --lan: local network access
# → open http://127.0.0.1:8765
```

- On first launch, the page opens the TV search; the choice is saved in `config.json`.
- Environment variables: `PORT` (default `8765`), `TV_IP` (forces a TV without going through the page).
- The first time a video is sent, macOS asks to allow Python to accept incoming connections: accept, otherwise the TV cannot read the stream.
- `server.py` starts and stops TorrServer by itself (port 8090, local only).
- Stopping: `kill $(lsof -ti tcp:8765 -sTCP:LISTEN)`. Log: `server.log` if started with `> server.log 2>&1`.
- The Mac must stay on and awake during playback.

## Running it all the time (macOS service)

To leave it running on a Mac that stays at home:

```sh
./service.sh install [movies_folder]   # start now, at every login, and restart if it crashes
./service.sh status                    # is it running?
./service.sh logs                      # follow server.log
./service.sh uninstall                 # stop and remove the service
```

- It installs a per-user launchd agent (`~/Library/LaunchAgents/com.mac-to-tv.plist`) that runs `server.py --lan`.
  Nothing system-wide is changed, and `uninstall` removes everything.
- **Sleep**: while the server runs, the Mac stays awake when it is plugged in (the server starts `caffeinate -s`).
  On battery, or with a laptop lid closed, the Mac sleeps as usual: playback stops, and the server resumes on wake.
  For a laptop used as a server, leave it plugged in with the lid open.
- **After a reboot** the server starts once you log in.
- At each start the server tidies up: cache files older than a week are deleted, `server.log` is emptied above 5 MB,
  and a TorrServer left behind by a killed server is stopped.
- The QR button next to the remote button shows the local network link, key included, as a QR code: scan it
  from a phone instead of typing the address. The code is drawn by `ffmpeg-full` (`GET /api/qr.png`).
- From a phone, use the name printed at startup (`http://<mac-name>.local:8765/?key=…`) rather than the IP address,
  which can change on a laptop.
- If YouTube links stop working, update the extractor: `brew upgrade yt-dlp`.

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
  2. during playback, the server checks in the background: if the video file contains a text subtitle track
     (any language), it reads two 2 min excerpts from it (10% and 70% of the movie) and looks, for each candidate,
     for an offset and a speed that fit both. If one candidate clearly stands out, it keeps it (file changed
     and/or timings corrected) and restarts the stream at the same position; otherwise it changes nothing. The result is kept in
     `cache/subs/sync-*` and the page announces it ("Subtitles checked…").
  Limits: without a subtitle track in the video file, no checking; the thresholds were tuned on a single movie.
- **Manual adjustment**: the player's "earlier" / "later" buttons shift the subtitles in 0.5 s steps
  (`POST /api/subdelay {delta}`); the stream restarts at the same position and the offset is kept for resume.
  The burn-in itself is exact (checked with a clock video, including after a seek): pausing and resuming shift nothing.
- Movies only for now (no series).

### Resume

- As long as a catalog movie is playing, the server records its position every 10 s in `progress.json` (a single profile),
  with the chosen source and subtitles. It works with the page closed.
- The page shows a "Continue or rewatch" row and, in the detail page, "Resume at xx:xx" / "Start over".
  A movie watched past 95% is marked as finished.
- The page address keeps the choice: `/?film=tt…&source=<torrent identifier>&file=<no.>&subs=<id>`.
  Reopening this link reopens the detail page with the source and subtitles preselected; nothing starts without a click.
- Starting from scratch: delete `progress.json` (server stopped).

## Network and security

- By default, the UI and the API only answer on `127.0.0.1`.
- **Access from a phone or another PC**: start with `python3 server.py --lan`. The server then prints an address
  like `http://192.168.1.18:8765/?key=…`. Open it once on the device: the key is remembered for one year (cookie).
  The key is in `lan-key.txt`; delete this file and restart to change it (the devices will have to open the new link).
  Without the key, a device on the network gets a refusal (403). Access stays limited to the local network: do not forward this port on the router.
- **What a device holding the key can and cannot do.** It can use the whole interface: remote, catalog, launching
  videos. It cannot read the Mac's files: the server only opens files inside the movies folder (and its own cache),
  after resolving `..` and symbolic links, and only media files. Torrents are accepted as magnet links, hashes or web
  addresses, never as a local path. A TV can only be selected at a private network address.
- **The video being played** is served at `/media/<token>/…` (random token, renewed on every send). That address is
  never given to the page, and the server only answers it for the selected TV: the TV itself tells anyone on the
  network what it is playing, so the token alone is not treated as a secret.
- `lan-key.txt` is readable by its owner only.
- Not covered: the link is plain HTTP, so the key can be captured by someone listening on the Wi-Fi; and the TV's own
  network remote has no protection, with or without this project. Use `--lan` on networks you trust.
- Reserve the TV's IP in the router (static DHCP lease); if it changes, just choose the TV again in the page.

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

The step-by-step guide to test your TV, fix playback or write a driver is in [CONTRIBUTING.md](CONTRIBUTING.md).

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

## How each source is played

| Source | Path |
|---|---|
| File from the Mac | served as is to the TV (with Range) |
| YouTube & co link | `yt-dlp` gives the addresses → `ffmpeg` muxes to MPEG-TS without re-encoding → TV. A seek restarts the stream. |
| Magnet link / torrent | TorrServer downloads in playback order (in memory) → relayed to the TV like a file |
| "Download first" | `yt-dlp` downloads into `cache/`, then the file is sent (fallback) |
| Catalog movie | source chosen in the detail page → TorrServer or direct link, relayed to the TV |
| With subtitles | goes through the live stream, subtitles burned in (chosen in the movie's detail page; for a local file: `POST /api/cast {path, burn}`, no button) |

Known limits: Arte does not start at the beginning of a movie (unresolved); multi-file torrents → largest video file;
no re-encoding for formats the TV cannot decode.

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

## Folders

| Folder | Contents | Copy to another Mac? |
|---|---|---|
| `server.py`, `sources.py`, `subs.py`, `tv.py`, `index.html`, `tests/`, `service.sh`, `README.md`, `CONTRIBUTING.md` | the application | yes |
| `config.json` | chosen TV and add-on list | yes, if you want to keep them |
| `lan-key.txt` | local network access key (created by `--lan`) | no, it is recreated |
| `progress.json` | movies started or finished and their position | yes, if you want to keep the history |
| `bin/` | TorrServer | no, download it again (architecture-specific binary) |
| `cache/` | "Download first" videos, thumbnails, subtitles; files older than a week are deleted at startup | no |
| `torrserver-data/` | TorrServer settings and log | no |
