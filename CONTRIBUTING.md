# Contributing: making your TV compatible

The project has only been developed on one TV (Grundig Ultralogic 4K, Arçelik Smart Inter@ctive 4.0 OS). Everything
related to TVs is isolated in [`tv.py`](tv.py) so that anyone can add their own. Three levels of contribution,
from the simplest to the most advanced:

1. [say whether playback works for you](#1-test-your-tv-and-report-it) (no code);
2. [fix playback](#2-fix-playback-on-a-tv) if your TV behaves differently;
3. [write the remote driver](#3-write-a-remote-driver) for your brand.

## 1. Test your TV and report it

TV on, on the same network as the Mac:

```sh
python3 tv.py
```

The command lists the TVs found, one per line. The useful fields:

| Field | Meaning |
|---|---|
| `compatible: True` | the TV has a DLNA network player (`AVTransport`): playback should work |
| `remote` | remote driver selected (`volume only…` = no brand driver) |
| `keys` | keys the UI will show |
| `apps: True` | the TV advertises DIAL: the YouTube app can be opened |

If your TV does not appear at all: it does not answer SSDP discovery (guest network, Wi-Fi isolation, "network
sharing" turned off in its settings). If it appears with `compatible: False`, it has no DLNA player: this project
cannot send video to it.

Then run `python3 server.py`, choose the TV in the page and try, in order: a file from the Mac, pause,
seeking in the video, a YouTube link, a movie with subtitles.

**Open an issue with**: brand, model, year, the line printed by `python3 tv.py`, and what works or not.
Even an "everything works" is useful: it extends the list of verified TVs.

## 2. Fix playback on a TV

Playback relies on standard DLNA, but each player has its own requirements. Everything happens in `server.py`:

| Symptom | Where to look |
|---|---|
| The TV rejects the video as soon as it is sent | `TVPlayer.send()`: DIDL-Lite metadata, MIME type (`MIME` table in `sources.py`), `DLNA_FLAGS` |
| The video starts but pause or seek are refused | `Handler.media()`: HTTP headers, `Range` responses |
| Links (YouTube…) do not work, files do | `live_cmd()` and `Handler.live()`: MPEG-TS stream with no known length |
| The displayed state is wrong | `TVPlayer.status()`: state names returned by `GetTransportInfo` |

To understand what the TV asks for, start the server with `> server.log 2>&1`: every request from the TV is logged there
(`TV GET film.mkv Range=bytes=…`), as well as `ffmpeg` errors.

Rules for these fixes:
- do not break the TVs already verified: if your TV requires something else, make it conditional on what it advertises
  (manufacturer, model, services), not on a constant;
- note the quirk you found in [docs/how-it-works.md](docs/how-it-works.md), in the driver section or the compatibility table.

No TV at hand? `tests/e2e/fake_tv.py` is a DLNA player that runs on the Mac; `python3 -m unittest` plays files and
streams on it. If your TV misbehaves in a way it does not reproduce, add the behaviour to it, with a test.

## 3. Write a remote driver

Without a brand driver, the remote is limited to volume and mute (`UpnpRemote`, through the DLNA player). A driver
adds the keys your TV accepts over the network.

### Finding out how your TV is controlled

- Look for the brand's network protocol. Known leads: Samsung (WebSocket, ports 8001/8002), LG webOS (WebSocket,
  port 3000), Sony Bravia (IRCC over HTTP), Roku (ECP over HTTP, port 8060), Philips (JointSpace), Android TV.
- The brand's official mobile app uses this protocol: watching its traffic (mitmproxy, Wireshark) gives the
  exact commands.
- Only use commands intended for normal control of the TV. No bypassing of protections, no
  firmware modification.

### The driver

A class in `tv.py`, with no external dependency (Python standard library only):

```python
class MyBrandRemote:
    """One sentence: which TVs, which protocol, which port."""
    label = "full (MyBrand)"                 # shown in the page after "Remote"
    keys = ("power", "mute", "v+", "v-")     # keys actually handled, among ALL_KEYS
    has_channels = False                      # True if channels() is provided

    def __init__(self, tv):
        self.ip = tv.ip

    @staticmethod
    def probe(tv):
        """True if this TV speaks this protocol. Must be fast (2 s max) and change nothing on the TV."""
        ...

    def press(self, key):
        """Sends the key. Raise an exception if the TV refuses."""
        ...

    # optional, if has_channels = True:
    def channels(self):
        return [{"n": 1, "name": "TF1"}]
```

Then add the class **before** `UpnpRemote` in `REMOTES`: the first driver whose `probe()` answers is selected.

```python
REMOTES = (ArcelikRemote, MyBrandRemote, UpnpRemote)
```

`ArcelikRemote` serves as a complete example. `tv` gives access to `tv.ip`, `tv.services` (advertised UPnP services) and
`tv.soap()` if the protocol goes through UPnP.

### The keys

The UI shows by itself the buttons whose key is listed in `keys`. Existing keys:

| Key | Button |
|---|---|
| `power` | power / standby |
| `mute` | mute |
| `v+`, `v-` | volume |
| `bl+`, `bl-` | backlight |
| `memc` | motion smoothing |

For a new key (HDMI source, next channel…): add its name to `ALL_KEYS` in `tv.py`, and a button
`<button data-key="name">` in the remote of `index.html`. It will stay hidden for TVs that do not handle it.

### Pairing

Some brands require accepting the connection on the TV screen, then reusing a token. Nothing is in place yet
to store this token: propose your solution in the pull request (a file ignored by git, next to
`config.json`, is the natural location). A token must never be committed.

### Trying your driver

```sh
python3 tv.py                 # your TV must show your label and your keys
python3 -m unittest           # must not regress
```

```python
import tv
t = tv.TV("192.168.1.20")     # your TV's address
t.refresh()
print(t.info())
t.press("v+")
```

Then in the page: every button shown must have its effect on the TV, and no button without effect must remain.

## Submitting the change

- One driver per pull request, with: brand, model and year of the TV tested, the output of `python3 tv.py`, and the list
  of keys tried one by one.
- Update [docs/how-it-works.md](docs/how-it-works.md): the "Which TVs are compatible?" table and, if useful, a section on the driver
  (ports, quirks), modeled on the Arçelik one.
- Only declare compatible what you have seen working on a real TV.
- No dependency to install with `pip`, no personal file (`config.json`, `progress.json`, `lan-key.txt`
  are ignored by git: they must stay that way).
- Comments and UI messages in English, like the rest of the project.
