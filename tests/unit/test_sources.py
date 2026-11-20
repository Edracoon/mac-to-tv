import unittest

from sources import about, history_entry, is_torrent, norm_link, torrserver, youtube_id


class Links(unittest.TestCase):
    def test_youtube_id_from_every_shape_of_link(self):
        for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", "https://youtu.be/dQw4w9WgXcQ?si=x",
                  "https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"):
            self.assertEqual(youtube_id(u), "dQw4w9WgXcQ", u)

    def test_a_youtube_link_is_normalised(self):
        self.assertEqual(norm_link(" https://youtu.be/dQw4w9WgXcQ ")[0], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_history_entry_keeps_short_facts(self):
        self.assertEqual(history_entry("u", {"title": "T", "uploader": "C", "duration": 61.4}),
                         {"url": "u", "title": "T", "channel": "C", "duration": 61, "thumbnail": ""})

    def test_about_keeps_short_texts_and_web_addresses_only(self):
        self.assertEqual(about({"description": "x" * 5000, "poster": "javascript:1", "link": "https://a/b", "other": 1, "film": "tt1"}),
                         {"description": "x" * 3000, "link": "https://a/b", "film": "tt1"})
        self.assertEqual(about(None), {})


class Torrents(unittest.TestCase):
    def test_what_counts_as_a_torrent_link(self):
        for link in ("https://example.org/a.torrent", "magnet:?xt=urn:btih:abc", "08ada5a7a6183aae1e09d831df6748d566095a10"):
            self.assertTrue(is_torrent(link), link)
        for link in ("/Users/x/a.torrent", "https://youtu.be/x"):
            self.assertFalse(is_torrent(link), link)

    def test_the_torrent_behind_a_stream_address(self):
        url = torrserver.URL + "/stream/a.mkv?link=" + "a" * 40 + "&index=1&play"
        self.assertEqual(torrserver.source(url, 9), {"hash": "a" * 40, "size": 9})
        self.assertIsNone(torrserver.source("https://x/a.mkv", 9))
