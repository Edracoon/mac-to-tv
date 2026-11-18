"""TV layer: discovery on the local network and control, with nothing brand-specific in the rest of the code.

Three capabilities, detected separately for each device:

  - player      : any DLNA/UPnP network player (MediaRenderer with AVTransport). This is what makes a TV
                  "compatible": we give it a video address, it plays it, and we control play/pause/seek/volume.
  - remote      : one driver per TV family (REMOTES list). Each driver declares the keys it can send;
                  the interface only shows those. With no brand driver, UpnpRemote provides volume and mute.
  - apps        : DIAL (standard), to open the TV's YouTube app.

Adding a brand = writing a class with `label`, `keys`, `probe(tv)` and `press(key)`, and putting it first in REMOTES.
"""
import base64
import json
import os
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCHES = ("ssdp:all", "urn:schemas-upnp-org:device:MediaRenderer:1", "urn:dial-multiscreen-org:service:dial:1")
# Logical keys known to the interface (a driver offers a subset)
ALL_KEYS = ("power", "mute", "v+", "v-", "bl+", "bl-", "memc")


# --- Discovery ----------------------------------------------------------------

def ssdp(timeout=3.0, only_ip=None):
    """Queries the local network; returns {ip: {description addresses}}."""
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        for st in SEARCHES:
            s.sendto(f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: {st}\r\n\r\n'.encode(), SSDP_ADDR)
        try:
            while True:
                data, addr = s.recvfrom(4096)
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m and (only_ip is None or addr[0] == only_ip):
                    found.setdefault(addr[0], set()).add(m[1].decode())
        except socket.timeout:
            pass
    return found


def parse_description(xml, location, app_url=None):
    """UPnP description → what we keep from it. services: {short name: (full type, control address)}."""
    tag = lambda name, text=xml: (re.search(rf"<{name}>\s*([^<]*?)\s*</{name}>", text) or ["", ""])[1]
    services = {}
    for block in re.findall(r"<service>(.*?)</service>", xml, re.S):
        stype, control = tag("serviceType", block), tag("controlURL", block)
        if stype and control:
            services[stype.split(":")[-2]] = (stype, urljoin(location, control))
    return {"name": re.sub(r"\s*\((?:[0-9a-f]{2}:){5}[0-9a-f]{2}\)$", "", tag("friendlyName"), flags=re.I),
            "manufacturer": tag("manufacturer"), "model": tag("modelName"), "type": tag("deviceType"),
            "services": services, "app_url": app_url}


def describe(location):
    try:
        with urllib.request.urlopen(location, timeout=3) as r:
            return parse_description(r.read().decode("utf-8", "replace"), location, r.headers.get("Application-URL"))
    except (OSError, ValueError):
        return None


def summarize(descs):
    """Merges the descriptions of a single device (a TV often publishes several: player, DIAL…)."""
    player = next((d for d in descs if "AVTransport" in d["services"]), None)
    main = player or next((d for d in descs if d["app_url"] or "tv" in d["type"].lower()), None)
    if not main:
        return None  # router, printer, NAS…: not a TV
    return {"name": main["name"] or main["model"] or "TV", "manufacturer": main["manufacturer"].strip(), "model": main["model"],
            "services": player["services"] if player else {}, "app_url": next((d["app_url"] for d in descs if d["app_url"]), None)}


def discover(timeout=3.0):
    """Lists the TVs on the local network, compatible or not, with what we can do with each."""
    out = []
    for ip, locations in sorted(ssdp(timeout).items()):
        info = summarize([d for d in map(describe, locations) if d])
        if not info:
            continue
        tv = TV(ip, info["name"])
        tv.load(info)
        out.append({**tv.info(), "manufacturer": info["manufacturer"], "compatible": tv.has_player,
                    "reason": "" if tv.has_player else "no DLNA network player: cannot send it a video"})
    return out


# --- Remotes ------------------------------------------------------------------

class ArcelikRemote:
    """Arçelik group TVs (Grundig, Beko…) running Smart Inter@ctive: an unauthenticated test WebSocket
    was left open on port 8085 (/foo). It accepts {"event": key}."""
    label = "full (Arçelik: Grundig, Beko…)"
    keys = ("power", "mute", "v+", "v-", "bl+", "bl-", "memc")
    has_channels = True

    def __init__(self, tv):
        self.ip = tv.ip

    @staticmethod
    def handshake(ip):
        s = socket.create_connection((ip, 8085), timeout=2)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(f"GET /foo HTTP/1.1\r\nHost: {ip}:8085\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        if b" 101 " not in s.recv(1024):
            s.close()
            raise OSError("WebSocket refused")
        return s

    @classmethod
    def probe(cls, tv):
        try:
            cls.handshake(tv.ip).close()
            return True
        except OSError:
            return False

    def press(self, key):
        payload = json.dumps({"event": key}).encode()  # masked text frame, < 126 bytes
        mask = os.urandom(4)
        with self.handshake(self.ip) as s:
            s.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
            s.settimeout(0.3)  # gives the TV time to process the frame before closing
            try:
                s.recv(1024)
            except socket.timeout:
                pass

    def channels(self):
        with urllib.request.urlopen(f"http://{self.ip}:8085/list", timeout=4) as r:
            return [{"n": c["channelNumber"], "name": c["name"]} for c in json.load(r)["channellist"]]


class UpnpRemote:
    """With no brand driver: volume and mute through the DLNA player itself (RenderingControl)."""
    label = "volume only (no driver for this brand)"
    keys = ("mute", "v+", "v-")
    has_channels = False
    STEP = 3
    CH = "<InstanceID>0</InstanceID><Channel>Master</Channel>"

    def __init__(self, tv):
        self.tv = tv

    @staticmethod
    def probe(tv):
        return "RenderingControl" in tv.services

    def press(self, key):
        if key == "mute":
            on = self.tv.soap("RenderingControl", "GetMute", self.CH).get("CurrentMute") in ("1", "true")
            return self.tv.soap("RenderingControl", "SetMute", f"{self.CH}<DesiredMute>{0 if on else 1}</DesiredMute>")
        vol = int(self.tv.soap("RenderingControl", "GetVolume", self.CH).get("CurrentVolume") or 0)
        vol = min(max(vol + (self.STEP if key == "v+" else -self.STEP), 0), 100)
        return self.tv.soap("RenderingControl", "SetVolume", f"{self.CH}<DesiredVolume>{vol}</DesiredVolume>")


REMOTES = (ArcelikRemote, UpnpRemote)  # from most specific to most general: the first one that answers is used


# --- A TV ---------------------------------------------------------------------

class TV:
    def __init__(self, ip, name=""):
        self.ip, self.name, self.model = ip, name, ""
        self.services, self.app_url, self.remote = {}, None, None

    def load(self, info):
        self.name, self.model = info["name"] or self.name, info["model"]
        self.services, self.app_url = info["services"], info["app_url"]
        self.remote = next((R(self) for R in REMOTES if R.probe(self)), None)

    def refresh(self):
        """(Re)reads what the TV publishes: its control addresses change when its player restarts."""
        locations = ssdp(2.0, only_ip=self.ip).get(self.ip, ())
        info = summarize([d for d in map(describe, locations) if d])
        if not info:
            raise RuntimeError(f"TV {self.ip} not found on the network (switched off?)")
        self.load(info)

    @property
    def has_player(self):
        return "AVTransport" in self.services

    @property
    def keys(self):
        return list(self.remote.keys) if self.remote else []

    def info(self):
        return {"ip": self.ip, "name": self.name, "model": self.model, "keys": self.keys,
                "remote": self.remote.label if self.remote else "none",
                "channels": bool(self.remote and self.remote.has_channels), "apps": bool(self.app_url)}

    # DLNA player
    def soap(self, service, action, args="", retry=True):
        if service not in self.services:
            if not retry:
                raise RuntimeError(f"this TV does not have the {service} service")
            self.refresh()
            return self.soap(service, action, args, retry=False)
        stype, url = self.services[service]
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{stype}">{args}</u:{action}></s:Body></s:Envelope>')
        req = urllib.request.Request(url, body.encode(), {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{stype}#{action}"'})
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                return dict(re.findall(r"<(\w+)>([^<]*)</\1>", r.read().decode("utf-8", "replace")))
        except urllib.error.HTTPError:
            raise  # SOAP fault: the TV answered, the address is right
        except OSError:
            if not retry:
                raise
            self.refresh()
            return self.soap(service, action, args, retry=False)

    # Remote
    def press(self, key):
        if not self.remote or key not in self.remote.keys:
            raise ValueError("key not available on this TV")
        return self.remote.press(key)

    def channels(self):
        if not (self.remote and self.remote.has_channels):
            raise ValueError("channel list not available on this TV")
        return self.remote.channels()

    # Apps (DIAL)
    def dial(self, app, method="GET", body=None):
        if not self.app_url:
            raise ValueError("this TV does not advertise any apps (DIAL)")
        req = urllib.request.Request(self.app_url.rstrip("/") + "/" + app, body, method=method)
        if body is not None:
            req.add_header("Content-Type", "text/plain; charset=utf-8")
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.read().decode()

    def app_state(self, app):
        try:
            return (re.search(r"<state>(\w+)</state>", self.dial(app)) or [0, "?"])[1]
        except (OSError, ValueError):
            return "absent"


def selftest():
    xml = ("<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
           "<friendlyName>Salon (00:11:E1:5b:3f:32)</friendlyName><manufacturer>Acme</manufacturer><modelName>X1</modelName>"
           "<serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
           "<controlURL>/aw/AVT/control</controlURL></service></serviceList></device></root>")
    d = parse_description(xml, "http://10.0.0.5:61726/aw")
    assert d["name"] == "Salon" and d["services"]["AVTransport"] == ("urn:schemas-upnp-org:service:AVTransport:1", "http://10.0.0.5:61726/aw/AVT/control"), d
    assert summarize([d])["services"] and summarize([{**d, "services": {}, "type": "urn:x:device:Printer:1"}]) is None
    only_dial = summarize([{**d, "services": {}, "type": "urn:schemas-upnp-org:device:tvdevice:1", "app_url": "http://10.0.0.5:1/apps/"}])
    assert only_dial["services"] == {} and only_dial["app_url"]


if __name__ == "__main__":
    selftest()
    for t in discover():
        print(t)
