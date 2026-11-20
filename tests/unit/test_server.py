import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests import server
from server import (MIME, allowed_file, browser_convert, hms, live_cmd, needs_convert, parse_range, plain_error, secs, stats_summary,
                    step, stream_error, watch_at, watch_cmd)
from sources import torrserver


class Files(unittest.TestCase):
    def test_nothing_outside_the_movies_folder_can_be_read(self):
        for outside in ("/etc/hosts", "~/.ssh/id_rsa", str(server.MEDIA_DIR / ".." / "x.mkv"), str(server.HERE / "lan-key.txt"), str(server.HERE / "server.py")):
            with self.assertRaises(ValueError, msg=outside):
                allowed_file(outside, MIME)

    def test_range_header(self):
        self.assertIsNone(parse_range(None, 100))
        self.assertEqual(parse_range("bytes=0-", 100), (0, 99))
        self.assertEqual(parse_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(ValueError):
            parse_range("bytes=100-", 100)

    def test_times(self):
        self.assertEqual((secs("0:02:05"), secs("NOT_IMPLEMENTED"), hms(3725)), (125, 0, "1:02:05"))


class StreamsForTheTV(unittest.TestCase):
    def test_only_what_the_tv_cannot_decode_is_converted(self):
        self.assertEqual(needs_convert("av1", "dts"), ["video", "audio"])
        self.assertEqual(needs_convert("hevc", "dts"), ["audio"])
        self.assertEqual(needs_convert("h264", "aac"), [])
        self.assertEqual(needs_convert("", ""), [])  # unknown format: leave it alone

    def test_a_site_is_asked_in_10_mb_requests_but_not_the_torrent_engine(self):
        self.assertIn("-request_size 10M", " ".join(live_cmd({"offset": 0, "inputs": [("https://x/v", "")]})))
        self.assertNotIn("-request_size", live_cmd({"offset": 0, "inputs": [(torrserver.URL + "/stream/x", "")]}))

    def test_the_picture_is_copied_when_only_the_sound_is_converted(self):
        self.assertIn("-c:v copy -c:a aac", " ".join(live_cmd({"offset": 0, "inputs": [("/a.mkv", "")], "convert": ["audio"]})))

    def test_burned_in_subtitles_follow_a_seek_and_the_delay(self):
        cmd = " ".join(live_cmd({"offset": 369, "sub_delay": 1.5, "inputs": [("/a.mkv", "")], "subs": "/s.srt"}))
        self.assertIn("setpts=PTS+(367.5)/TB", cmd)

    def test_why_a_stream_broke(self):
        self.assertEqual(stream_error(""), ("The video source stopped responding.", ""))
        self.assertEqual(stream_error("[in#0/hls @ 0x1] Error during demuxing: Input/output error")[1], "Error during demuxing: Input/output error")
        self.assertIn("refused", stream_error("x\n[https @ 0x2] Server returned 403 Forbidden (access denied)")[0])


class StreamsForTheBrowser(unittest.TestCase):
    def test_what_is_copied_and_what_is_re_encoded(self):
        self.assertEqual(browser_convert("hevc", "ac3"), ["video", "audio"])
        self.assertEqual(browser_convert("h264", "mp3"), [])
        self.assertEqual(browser_convert("h264", ""), [])

    def test_a_copied_picture_starts_on_the_key_frame_asked_for(self):
        cmd = " ".join(watch_cmd({"inputs": [("/a.mkv", "")], "convert": ["audio"], "vcodec": "h264"}, 90))
        self.assertIn("-ss 90.2 -i /a.mkv -map 0:v:0? -map 0:a:0? -c:v copy -c:a aac", cmd)
        self.assertTrue(cmd.endswith("-f mp4 pipe:1"), cmd)

    def test_a_re_encoded_picture_starts_exactly_where_asked(self):
        self.assertIn("-ss 90 -i", " ".join(watch_cmd({"inputs": [("/a.mkv", "")], "convert": ["video"], "vcodec": "hevc"}, 90)))
        self.assertEqual(watch_at({"convert": ["video"], "vcodec": "hevc"}, 61.5), 61.5)
        self.assertEqual(watch_at({"convert": [], "vcodec": ""}, 5), 5)


class Launching(unittest.TestCase):
    def setUp(self):
        self.job = server.tv_player.job = server.Launches()
        self.wjob = server.browser.job = server.Launches()

    def test_a_stage_reported_twice_updates_its_detail(self):
        step("a"); step("b", "1"); step("b", "2")
        self.assertEqual(self.job.steps, [["a", ""], ["b", "2"]])

    def test_a_launch_for_the_browser_reports_to_its_own_stages(self):
        self.wjob.run("here", lambda: step("for the browser"))
        self.assertEqual((self.wjob.steps, self.wjob.title), ([["for the browser", ""]], "here"))
        self.assertEqual((self.job.steps, self.job.title), ([], ""))

    def test_a_launch_replaced_by_a_newer_one_stops_at_its_next_stage(self):
        job, go, out = self.job, threading.Event(), []

        def old_launch():
            step("old"); go.wait(5); step("old, later")

        def run_old():
            try:
                job.run("old", old_launch)
            except server.Superseded:
                out.append("stopped")
        th = threading.Thread(target=run_old)
        th.start()
        while job.steps != [["old", ""]]:
            time.sleep(0.01)
        job.run("new", lambda: (step("new"), go.set(), th.join()))
        self.assertEqual(out, ["stopped"])
        self.assertEqual(job.public(), {"busy": False, "title": "new", "steps": [["new", ""]], "error": "", "detail": ""})

    def test_a_failed_launch_says_why(self):
        with self.assertRaises(ValueError):
            self.job.run("x", allowed_file, "/etc/hosts", MIME)
        self.assertEqual((self.job.error, self.job.busy), ("This file is outside the movies folder.", False))

    def test_errors_in_plain_words(self):
        self.assertEqual(plain_error(ValueError("unrecognized link")), ("Unrecognized link.", ""))
        self.assertEqual(plain_error(RuntimeError("yt-dlp failed: ERROR: gone"))[1], "ERROR: gone")


class PlayingOnTheTV(unittest.TestCase):
    def test_a_restarted_stream_is_at_its_restart_point_until_the_tv_switches_to_it(self):
        tv, live = server.TVPlayer(), {"offset": 1200}
        tv.shared["tok"] = live
        self.assertEqual(tv.live_position(live), 1200)
        self.assertEqual(tv.live_position(live, {"TrackURI": "http://mac/media/old/stream.ts", "RelTime": "0:00:03"}), 1200)
        self.assertEqual(tv.live_position(live, {"TrackURI": "http://mac/media/tok/stream.ts", "RelTime": "0:00:30"}), 1230)
        self.assertEqual(tv.live_position(live), 1230)


class KeptOnDisk(unittest.TestCase):
    def test_the_position_of_a_catalog_movie_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            pr = server.Progress(Path(d) / "p.json")
            self.assertEqual(pr.launched({"title": "T", "film": {"id": "tt1"}, "start": 30}), ("T", "tt1", 30))
            pr.update("tt1", position=95.7, duration=5400)
            pr.update("tt1", position=96, sub_delay=1.5)  # an unknown duration keeps the one known
            pr.update("nope", position=1)  # not a catalog movie: ignored
            f = pr.films["tt1"]
            self.assertEqual((f["position"], f["duration"], f["play"]), (96, 5400, {"title": "T", "subDelay": 1.5}))
            self.assertEqual(list(pr.films), ["tt1"])
            self.assertEqual(json.loads((Path(d) / "p.json").read_text()), pr.films)

    def test_figures_of_the_last_seven_days(self):
        day = lambda n: time.strftime("%Y-%m-%d", time.localtime(1e9 - 86400 * n))
        st = {"seconds": {"movies": 50, "links": 20}, "days": {day(0): 30, day(6): 20, day(7): 20}, "plays": {"links": 3}}
        self.assertEqual(stats_summary(st, 1e9), {"seconds": 70, "by": {"movies": 50, "links": 20}, "plays": {"links": 3}, "week": 50})

    def test_time_played_is_counted_per_kind_and_a_video_once(self):
        with tempfile.TemporaryDirectory() as d:
            st = server.Stats(Path(d) / "s.json")
            st.count({"film": "tt1"}, "Film"); st.count({"film": "tt1"}, "Film"); st.count(Path("/a.mkv"), "a")
            self.assertEqual((st.data["seconds"], st.data["plays"]), ({"movies": 20, "other": 10}, {"movies": 1, "other": 1}))
            self.assertEqual(json.loads((Path(d) / "s.json").read_text()), st.data)
