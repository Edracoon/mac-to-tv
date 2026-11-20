"""Tests of mac-to-tv. Standard library only, like the rest of the project. From the root of the repository:

    python3 -m unittest                              # everything
    python3 -m unittest discover -s tests/unit -t .  # unit tests: no network, no ffmpeg, a second or two
    python3 -m unittest discover -s tests/e2e -t .   # the server driving a fake TV (needs ffmpeg and ffprobe)

Nothing here touches an installation: no real TV, no torrent engine, and the files the server keeps (config.json,
progress.json…) are replaced by temporary ones where a test writes to them.
"""
import sys

_argv, sys.argv = sys.argv, ["server.py"]  # server.py reads its options from the command line when it is imported
import server  # noqa: E402 (the tests take it from here: `from tests import server`)
import sources  # noqa: E402

sys.argv = _argv
# A launch ends by dropping the torrents nobody plays: pointed at a port nothing listens on, so that running the tests
# never drops the torrent of a server in use on this Mac.
sources.torrserver.URL = "http://127.0.0.1:9"
