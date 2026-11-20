"""A fake TV: a DLNA/UPnP network player (MediaRenderer) that runs on this Mac, for tests without the real one.

It publishes a device description, answers the AVTransport and RenderingControl commands the server uses, and really
fetches the address it is told to play (HEAD, then GET with Range), counting playback time like a TV does. With
ssdp=True it also answers discovery (M-SEARCH) on the local network.

Knobs, to reproduce what real TVs do (all back to their default with reset()):
  lag            seconds during which, after being given a new address, it keeps reporting the previous one and its
                 position (the Grundig does this for a few seconds)
  stops_at_once  it stops by itself right after Play, like a TV given a format it cannot decode
  duration       seconds: what it reports as the length of a file. A TV reads it in the file; this one cannot.

What it was asked is kept for the tests to look at: calls (the commands, in order), fetches (the media requests, each
(method, Range, status)), received (bytes of the video read so far).

    from tests.e2e.fake_tv import FakeTV
    fake = FakeTV()
    tv = tvlib.TV("127.0.0.1"); tv.load(tvlib.summarize([tvlib.describe(fake.location)]))
"""
import html
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SERVICES = {"AVT": "urn:schemas-upnp-org:service:AVTransport:1", "RC": "urn:schemas-upnp-org:service:RenderingControl:1"}
DESCRIPTION = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
               "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><friendlyName>Fake TV</friendlyName>"
               "<manufacturer>mac-to-tv tests</manufacturer><modelName>Fake 1</modelName><serviceList>"
               + "".join(f"<service><serviceType>{stype}</serviceType><controlURL>/{name}/control</controlURL></service>" for name, stype in SERVICES.items())
               + "</serviceList></device></root>")


class Fault(Exception):
    """The TV refuses a command: answered as a SOAP fault (HTTP 500)."""


def hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}"


def secs(t):
    return sum(int(x) * 60 ** i for i, x in enumerate(reversed(t.split(":"))))


def http_open(url, method, rng=None):
    """Sends the request and reads the headers → (socket, status, headers, start of the body). A plain socket, so that
    a download in progress can be cut from another thread, as a TV does when it is told to play something else."""
    u = urlsplit(url)
    s = socket.create_connection((u.hostname, u.port or 80), timeout=30)
    try:
        s.sendall((f"{method} {u.path}{'?' + u.query if u.query else ''} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                   + (f"Range: {rng}\r\n" if rng else "") + "Connection: close\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise OSError("connection closed before the headers")
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        return s, int(lines[0].split()[1]), {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}, body
    except Exception:
        s.close()
        raise


class FakeTV:
    def __init__(self, ssdp=False):
        self.lock = threading.RLock()
        self.stream = None  # socket of the video being read
        self.reset()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, code, body):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.answer(200, DESCRIPTION) if self.path == "/description.xml" else self.answer(404, "")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
                stype, _, action = self.headers.get("SOAPACTION", "").strip('"').partition("#")
                envelope = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>{}</s:Body></s:Envelope>'
                try:
                    out = fake.command(action, dict(re.findall(r"<(\w+)>([^<]*)</\1>", body)))
                except Fault as e:
                    return self.answer(500, envelope.format(f"<s:Fault><faultstring>UPnPError</faultstring><detail>{html.escape(str(e))}</detail></s:Fault>"))
                self.answer(200, envelope.format(f'<u:{action}Response xmlns:u="{stype}">' + "".join(f"<{k}>{v}</{k}>" for k, v in out.items()) + f"</u:{action}Response>"))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.location = f"http://127.0.0.1:{self.httpd.server_port}/description.xml"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.ssdp = self.answer_discovery() if ssdp else None

    def reset(self):
        """Back to a TV that was just switched on, with the default behaviour."""
        with self.lock:
            self.cut()
            self.lag, self.stops_at_once, self.duration = 0.0, False, 0
            self.volume, self.mute = 10, False
            self.calls, self.fetches, self.received = [], [], 0
            self.state, self.uri, self.meta, self.length = "NO_MEDIA_PRESENT", "", "", 0  # length: bytes of a file; 0 for a stream
            self.base, self.since = 0.0, None  # the playback clock: seconds played before `since`, and when it last started
            self.left_at = 0.0  # where the last video was when it was stopped
            self.old = None  # (address, position, until when) still reported after a new address was given

    def close(self):
        self.cut()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.ssdp:
            self.ssdp.close()

    def answer_discovery(self):
        """Answers the M-SEARCH queries of the local network with the address of the description.
        Raises OSError where the discovery port cannot be shared."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", 1900))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0"))
        reply = f"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=60\r\nLOCATION: {self.location}\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\nUSN: uuid:fake-tv\r\n\r\n".encode()

        def loop():
            while True:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:
                    return  # closed
                if data.startswith(b"M-SEARCH") and (b"ssdp:all" in data or b"MediaRenderer" in data):
                    s.sendto(reply, addr)
        threading.Thread(target=loop, daemon=True).start()
        return s

    # the video

    def position(self):
        with self.lock:
            p = self.base + (time.time() - self.since if self.since else 0)
            if self.length and self.duration and p >= self.duration:  # the end of a file
                self.base, self.since, self.state, p = float(self.duration), None, "STOPPED", self.duration
            return p

    def cut(self):
        with self.lock:
            s, self.stream = self.stream, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)  # wakes the thread that is reading it
            except OSError:
                pass
            s.close()

    def fetch(self, uri, method, start=None):
        rng = None if start is None else f"bytes={start}-"
        try:
            s, status, headers, body = http_open(uri, method, rng)
        except OSError as e:
            raise Fault(f"cannot open the video: {e}")
        self.fetches.append((method, rng, status))
        if status >= 400:
            s.close()
            raise Fault(f"cannot open the video: HTTP {status}")
        return s, headers, body

    def read(self, uri, start=0):
        """Opens the video at byte `start` and returns once its first bytes are there; the rest is read in the
        background, until the end or until cut(). Returns the headers."""
        s, headers, body = self.fetch(uri, "GET", start)
        try:
            if not body:
                body = s.recv(65536)
        except OSError:
            body = b""
        if not body:
            s.close()
            raise Fault("cannot open the video: no data")
        s.settimeout(None)
        self.cut()
        with self.lock:
            self.stream = s
            self.received += len(body)

        def rest():
            try:
                while chunk := s.recv(1 << 16):
                    with self.lock:
                        if self.stream is not s:
                            return
                        self.received += len(chunk)
            except OSError:
                pass  # cut
        threading.Thread(target=rest, daemon=True).start()
        return headers

    def halt(self):
        with self.lock:
            self.left_at = self.position()
            self.base, self.since, self.state = 0.0, None, "STOPPED"
        self.cut()

    # the commands

    def command(self, action, args):
        self.calls.append(action)
        fn = getattr(self, "do_" + action, None)
        if not fn:
            raise Fault(f"401 invalid action {action}")
        return fn(args) or {}

    def do_SetAVTransportURI(self, a):
        uri = html.unescape(a.get("CurrentURI", ""))
        previous = (self.uri, self.left_at if self.state == "STOPPED" else self.position())  # the server sends Stop first
        self.fetch(uri, "HEAD")[0].close()
        headers = self.read(uri)  # like the TV, answer only once the video is open
        with self.lock:
            self.uri, self.meta, self.length = uri, a.get("CurrentURIMetaData", ""), int(headers.get("content-length") or 0)
            self.base, self.since, self.state = 0.0, None, "TRANSITIONING"
            self.old = (*previous, time.time() + self.lag) if self.lag else None

    def do_Play(self, a):
        with self.lock:
            if not self.uri:
                raise Fault("701 transition not available")
            if self.state != "PLAYING":
                self.since, self.state = time.time(), "PLAYING"
        if self.stops_at_once:
            threading.Timer(0.3, self.halt).start()

    def do_Pause(self, a):
        with self.lock:
            if self.state != "PLAYING":
                raise Fault("701 transition not available")
            self.base, self.since, self.state = self.position(), None, "PAUSED_PLAYBACK"

    def do_Stop(self, a):
        if self.uri:
            self.halt()

    def do_Seek(self, a):
        if not self.length:
            raise Fault("710 seek mode not supported")  # a stream of unknown length has no middle
        t = secs(a.get("Target", "0"))
        self.read(self.uri, int(self.length * t / self.duration) if self.duration else 0)
        with self.lock:
            self.base, self.since = float(t), time.time() if self.state == "PLAYING" else None

    def do_GetTransportInfo(self, a):
        self.position()  # notices the end of the file
        return {"CurrentTransportState": self.state, "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}

    def do_GetPositionInfo(self, a):
        with self.lock:
            uri, pos = self.uri, self.position()
            if self.old and time.time() < self.old[2]:
                uri, pos = self.old[:2]
            return {"Track": "1", "TrackDuration": hms(self.duration if self.length else 0), "TrackMetaData": self.meta,
                    "TrackURI": html.escape(uri), "RelTime": hms(pos), "AbsTime": hms(pos)}

    def do_GetVolume(self, a):
        return {"CurrentVolume": self.volume}

    def do_SetVolume(self, a):
        self.volume = int(a["DesiredVolume"])

    def do_GetMute(self, a):
        return {"CurrentMute": int(self.mute)}

    def do_SetMute(self, a):
        self.mute = a["DesiredMute"] in ("1", "true")
