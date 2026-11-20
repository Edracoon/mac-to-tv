import unittest

from subs import dense_stretches, fit_subs, parse_srt, sound_shift, vtt, write_srt


class Formats(unittest.TestCase):
    def test_vtt_shifts_the_cues_and_drops_those_before_zero(self):
        srt = "1\n00:00:01,000 --> 00:00:02,500\n{\\an8}<i>Hi</i>\n\n2\n00:01:00,000 --> 00:01:02,000\nLater\n"
        self.assertEqual(vtt(srt, -50), "WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nLater\n")

    def test_vtt_drops_the_position_tags(self):
        self.assertTrue(vtt("1\n00:00:01,000 --> 00:00:02,500\n{\\an8}<i>Hi</i>\n").endswith("00:00:01.000 --> 00:00:02.500\n<i>Hi</i>\n"))

    def test_srt_written_then_read_keeps_the_times(self):
        cues = [(7.3 * n + 0.4, 7.3 * n + 2.4, "x") for n in range(3)]
        self.assertEqual(parse_srt(write_srt(cues))[2][0], cues[2][0])


class SyncAgainstAReferenceTrack(unittest.TestCase):
    """A .srt shifted by -9.7 s and played at 23.976 instead of 24 frames/s must land back on the reference, and be
    preferred over an unrelated file, even if that one is the first choice."""

    def setUp(self):
        self.truth = [(t, t + 2, "x") for t in (7.3 * n + (n * n * 37 % 50) / 10 for n in range(800))]
        self.windows = [sorted(t for t, _, _ in self.truth if a <= t <= a + 120) for a in (600, 4000)]

    def test_shift_and_speed_are_corrected_and_the_right_file_is_picked(self):
        slow = [((t - 9.7) * 24 / 23.976, (e - 9.7) * 24 / 23.976, x) for t, e, x in self.truth]
        unrelated = [(t * 1.3 + 500, e, x) for t, e, x in self.truth]
        i, fixed, note, changed = fit_subs([unrelated, slow], self.windows)
        self.assertEqual(i, 1)
        self.assertTrue(changed)
        self.assertIn("shifted", note)
        self.assertLess(max(abs(f[0] - t[0]) for f, t in zip(fixed, self.truth)), 0.5)

    def test_a_file_in_sync_is_left_alone(self):
        self.assertEqual(fit_subs([self.truth], self.windows)[2:], ("checked: in sync", False))


class SyncAgainstTheSound(unittest.TestCase):
    """Cues 12.3 s early against a "sound" that gets loud for 1.5 s when each line is spoken must come out as +12.3,
    and unrelated cues must not come out as clear."""

    def setUp(self):
        self.said = [[40 + 7.3 * n + (n * n * 37 % 50) / 10 + 900 * w for n in range(60)] for w in range(3)]

        def heard(lines, start, length=600):
            z = [-0.4 + ((i * 7919) % 13) / 40 for i in range(length * 10)]
            for t0 in lines:
                for i in range(int((t0 - start) * 10), int((t0 - start + 1.5) * 10)):
                    if 0 <= i < len(z):
                        z[i] = 1.6
            return z
        self.excerpts = [(900 * w, heard(lines, 900 * w)) for w, lines in enumerate(self.said)]

    def test_the_shift_is_found(self):
        self.assertEqual(sound_shift([t0 - 12.3 for lines in self.said for t0 in lines], self.excerpts), (12.3, True))

    def test_unrelated_cues_give_no_clear_answer(self):
        self.assertFalse(sound_shift([11.7 * n for n in range(240)], self.excerpts)[1])

    def test_the_stretches_with_most_cues_are_picked(self):
        self.assertEqual(dense_stretches(sorted(t0 for lines in self.said for t0 in lines), 3000), [0, 900, 1800])
