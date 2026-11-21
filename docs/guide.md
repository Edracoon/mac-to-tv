# Using mac-to-tv

Installing, running, leaving it on, reaching it from a phone, and what is kept where. For what happens behind each
button, see [how-it-works.md](how-it-works.md).

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
