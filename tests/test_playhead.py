import unittest
from unittest.mock import patch

import app


class PlayheadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(app, "ensure_data_dirs"):
            cls.ui = app.AudioAtelierApp()
        cls.ui.root.update()

    @classmethod
    def tearDownClass(cls):
        for timer in cls.ui.root.tk.call("after", "info"):
            cls.ui.root.after_cancel(timer)
        cls.ui.close()

    def gesture(self, canvas, x, y, dx=None):
        x = round(x)
        canvas.event_generate("<ButtonPress-1>", x=x, y=y)
        if dx is not None:
            x += round(dx)
            canvas.event_generate("<B1-Motion>", x=x, y=y)
        canvas.event_generate("<ButtonRelease-1>", x=x, y=y)

    def test_trim(self):
        tab = self.ui.trim_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        wave = tab.wave
        self.assertTrue(wave.find_withtag("playhead"))
        tab._loaded((10, [0.5] * 100))
        x = wave._x_for
        grip_y = wave.winfo_height() - 6
        bottom = wave.winfo_height() - wave.GRIP_HEIGHT
        for grip in wave.find_withtag("trim_grip"):
            y = wave.coords(grip)[1::2]
            self.assertLess(min(y), bottom)
            self.assertGreater(max(y), bottom)
        self.assertGreater(len(wave.find_withtag("ruler_label")), 1)
        for pos in (0, 5, 10):
            self.gesture(wave, x(pos), 5)
            self.gesture(wave, x(pos), grip_y)
        self.gesture(wave, x(10), 5, -100)  # End handle cannot be grabbed in the ruler.
        self.gesture(wave, x(5), 5, 100)  # Starting on the ruler never edits the range.
        self.gesture(wave, x(0), 5)
        self.assertEqual((wave.start, wave.end, wave.playhead), (0, 10, 0))
        with patch.object(self.ui, "seek") as seek:
            self.gesture(wave, x(0), 5, x(4) - x(0))
            seek.assert_called_once_with(tab.seek)
        pos = wave.playhead
        self.assertAlmostEqual(pos, 4, places=1)
        self.gesture(wave, x(0) - 16, grip_y, x(2) - x(0))
        self.assertAlmostEqual(wave.start, 2, places=1)
        self.assertEqual(wave.playhead, pos)
        self.gesture(wave, x(10) + 16, grip_y, x(3) - x(10))
        self.assertAlmostEqual(wave.end, 3, places=1)
        self.assertEqual(wave.playhead, wave.start)
        self.gesture(wave, x(wave.start), 5, x(9) - x(2))
        self.assertEqual(wave.playhead, wave.end)
        # Numeric edits including an exact endpoint retain the same position.
        tab.start_var.set("1"); tab.end_var.set("5")
        pos = wave.playhead
        self.assertTrue(tab.apply_entries())
        self.assertEqual(wave.playhead, pos)
        wave.set_range(pos, 6, notify=True)
        self.assertEqual(wave.playhead, pos)
        wave.set_range(5, 6, notify=True)
        self.assertEqual(wave.playhead, 5)

    def test_ruler_click_clamps_without_editing_range(self):
        tab = self.ui.trim_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        tab._loaded((10, [.5] * 100))
        wave = tab.wave
        wave.set_range(2, 7, notify=True)
        with patch.object(self.ui, "seek") as seek:
            for seconds, expected in ((5, 5), (0, 2), (10, 7), (4, 4)):
                self.gesture(wave, wave._x_for(seconds), wave.RULER_HEIGHT - 1)
                self.assertAlmostEqual(wave.playhead, expected, places=2)
                self.assertEqual((wave.start, wave.end), (2, 7))
                seek.assert_called_once_with(tab.seek)
                seek.reset_mock()
            last = wave.playhead
            for y in (wave.RULER_HEIGHT + 10, wave.winfo_height() - 5):
                self.gesture(wave, wave._x_for(6), y)
            self.assertEqual(wave.playhead, last)
            seek.assert_not_called()

    def test_ruler_click_survives_pointer_jitter(self):
        tab = self.ui.trim_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        tab._loaded((10, [.5] * 100))
        w = tab.wave
        w.set_range(2, 8, notify=True)
        for dx, dy in ((4, 0), (0, 2), (-5, 3)):
            with self.subTest(dx=dx, dy=dy), patch.object(self.ui, "seek") as seek:
                tab.seek.show(2)
                x, y = round(w._x_for(6)), w.RULER_HEIGHT
                w.event_generate("<ButtonPress-1>", x=x, y=y)
                self.assertEqual(w.playhead, w._time_for(x))
                self.assertTrue(w.seek.dragging)
                w.event_generate("<B1-Motion>", x=x + dx, y=y + dy)
                w.event_generate("<ButtonRelease-1>", x=x + dx, y=y + dy)
                self.assertEqual(w.playhead, w._time_for(x))
                self.assertEqual((w.start, w.end), (2, 8))
                self.assertFalse(w.seek.dragging)
                seek.assert_called_once_with(tab.seek)

    def test_mix(self):
        tab = self.ui.mix_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        canvas = tab.timeline
        self.assertTrue(canvas.find_withtag("playhead"))
        tab.clips = [app.AudioClip("tone", "tone.wav", "tone", 0, 60, 0, "blue")]
        canvas.set_clips(tab.clips)
        self.ui.root.update()
        self.gesture(canvas, 70 - canvas.canvasx(0), 5, 180)
        self.assertEqual(canvas.playhead, 6)
        self.assertEqual(tab.clips[0].start, 0)
        canvas.xview_moveto(0.05)
        x = 70 + 6 * canvas.pixels_per_second - canvas.canvasx(0)
        self.gesture(canvas, x, 5, 90)
        self.assertEqual(canvas.playhead, 9)
        # A clip below the ruler can still be moved under the playhead.
        x = 70 + 9 * canvas.pixels_per_second - canvas.canvasx(0)
        self.gesture(canvas, x, canvas.TOP + 20, 30)
        self.assertEqual(tab.clips[0].start, 1)
        self.assertEqual(canvas.playhead, 9)

    def test_mix_movement_respects_lane_edges(self):
        tab = self.ui.mix_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        c = tab.timeline
        a = app.AudioClip("a", "a.wav", "a", 0, 2.375, 0, "blue")
        b = app.AudioClip("b", "b.wav", "b", 4, 1, 0, "green")
        tail = app.AudioClip("c", "c.wav", "c", 6.125, 2, 0, "red")
        tab.clips = [a, b, tail]
        c.set_clips(tab.clips)
        c.xview_moveto(0)
        y = c.TOP + 20
        def drag_to(seconds, lane=0):
            x = round(70 + (b.start + .5) * c.pixels_per_second - c.canvasx(0))
            to = x + round((seconds - b.start) * c.pixels_per_second)
            c.event_generate("<ButtonPress-1>", x=x, y=y + b.lane * c.LANE_H)
            for event in ("<B1-Motion>", "<ButtonRelease-1>"):
                c.event_generate(event, x=to, y=y + lane * c.LANE_H)
        drag_to(2)
        self.assertEqual(b.start, a.start + a.duration)
        drag_to(5.5)
        self.assertEqual(b.start + b.duration, tail.start)
        drag_to(1, 1)
        self.assertEqual((b.lane, b.start), (1, 1))
        drag_to(1, 0)
        self.assertEqual((b.lane, b.start), (0, a.duration))
        tab.start_var.set("5.75")
        tab.apply_selected_values()
        self.assertEqual(b.start + b.duration, tail.start)
        self.assertEqual((a.start, tail.start), (0, 6.125))

    def test_narrow_lane_gaps_and_looping_clips(self):
        a = app.AudioClip("a", "", "", 0, 2, 0, "blue")
        b = app.AudioClip("b", "", "", 0, 1, 1, "blue")
        c = app.AudioClip("c", "", "", 3, 5, 0, "blue")
        clips = [a, b, c]
        self.assertEqual(app.free_clip_start(clips, b, 0, 2.1), 2)
        c.start = 2.5
        self.assertEqual(app.free_clip_start(clips, b, 0, 2), 7.5)
        c.loop = True
        self.assertIsNone(app.free_clip_start(clips, b, 0, 2))
        c.start = 3
        self.assertEqual(app.free_clip_start(clips, b, 0, 9), 2)
        b.loop = True
        self.assertIsNone(app.free_clip_start(clips, b, 0, 2))
        c.loop = False
        self.assertEqual(app.free_clip_start(clips, b, 0, 2), 8)

    def test_cursor_anchored_zoom(self):
        tab = self.ui.trim_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        tab._loaded((20, [.25] * 2000))
        w = tab.wave
        w.set_range(8, 15, notify=True)
        tab.seek.show(11)
        x = round(w._x_for(12.5))
        anchor = w._time_for(x)
        def wheel(delta, at=x):
            w.event_generate("<MouseWheel>", x=at, y=w.RULER_HEIGHT + 15, delta=delta)
        wheel(120)
        wheel(120)
        self.assertAlmostEqual(w._time_for(x), anchor)
        self.assertAlmostEqual(w._x_for(anchor), x)
        self.assertLess(w.view_span, 10)
        self.assertEqual((w.start, w.end, w.playhead), (8, 15, 11))
        self.gesture(w, w._x_for(13), 12)
        self.assertAlmostEqual(w.playhead, 13, delta=.01)
        self.gesture(w, w._x_for(15) + 8, w.winfo_height() - 5, w._x_for(14) - w._x_for(15))
        self.assertAlmostEqual(w.end, 14, delta=.01)
        wheel(120 * 32)
        self.assertAlmostEqual(w.view_span, w.MIN_VIEW_SECONDS)
        self.assertAlmostEqual(w._time_for(x), anchor)
        self.assertFalse(w.find_withtag("trim_grip"))  # both handles now offscreen
        wheel(-120 * 32)
        self.assertEqual((w.view_start, w.view_span), (0, 20))
        wheel(-120)
        self.assertEqual((w.view_start, w.view_span), (0, 20))
        wheel(120, at=w.SIDE_PAD)
        self.assertEqual(w.view_start, 0)
        tab._loaded((.03, [.5] * 60))
        wheel(120)
        self.assertEqual((w.view_start, w.view_span), (0, .03))

    def test_pan_with_bottom_strip(self):
        tab = self.ui.trim_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        tab._loaded((60, [.25] * 1000))
        w = tab.wave
        y, x = w.winfo_height() - 10, round(w._x_for(30))
        w.set_range(28, 32, notify=True)
        tab.seek.show(30)
        self.gesture(w, x, y, 100)
        self.assertEqual(w.view_start, 0)
        self.assertFalse(w.find_withtag("pan_thumb"))
        w.event_generate("<MouseWheel>", x=x, y=y - 50, delta=480)
        origin, span = w.view_start, w.view_span
        thumb_left = w.coords("pan_thumb")[0]
        self.gesture(w, x, y)
        self.assertEqual(w.view_start, origin)
        with patch.object(w, "on_range_change") as edit, patch.object(self.ui, "seek") as seek:
            self.gesture(w, x, y, 55)
            self.assertGreater(w.view_start, origin)
            self.assertGreater(w.coords("pan_thumb")[0], thumb_left)
            self.gesture(w, x, y, w.winfo_width() * 2)
            self.assertAlmostEqual(w.view_start + span, 60)
            self.gesture(w, x, y, -w.winfo_width() * 2)
            self.assertEqual(w.view_start, 0)
            self.assertEqual(w.view_span, span)
            self.assertEqual((w.start, w.end, w.playhead), (28, 32, 30))
            seek.assert_not_called()
            edit.assert_not_called()
        w.set_range(2, 7, notify=True)
        for which, offset in (("start", -12), ("end", 12)):
            old = getattr(w, which)
            self.gesture(w, w._x_for(old) + offset, y, 16)
            self.assertGreater(getattr(w, which), old)
            self.assertEqual(w.view_start, 0)

    def test_end_grip_after_panning_to_file_end(self):
        tab = self.ui.trim_tab
        self.ui.notebook.select(tab)
        self.ui.root.update()
        w = tab.wave
        right = w.winfo_width() - w.SIDE_PAD
        y = w.winfo_height() - 6
        for duration, notches in ((10.1, 8), (3600.234, 32)):
            with self.subTest(duration=duration):
                tab._loaded((duration, [.25] * 1000))
                x = round(w._x_for(duration / 2))
                w.event_generate("<MouseWheel>", x=x, y=y - 50, delta=120 * notches)
                self.assertFalse(w.find_withtag("trim_grip"))
                self.gesture(w, x, y, 2 * w.winfo_width())
                grips = w.find_withtag("trim_grip")
                self.assertEqual(len(grips), 1)
                self.assertEqual(w.coords(grips[0])[0], right)
                self.assertEqual(w._time_for(right), duration)
                origin = w.view_start
                self.gesture(w, right + 12, y, -30)
                self.assertLess(w.end, duration)
                self.assertEqual(w.view_start, origin)
                self.gesture(w, w._x_for(w.end) + 12, y, 60)
                self.assertEqual(w.end, duration)
                self.assertEqual(len(w.find_withtag("trim_grip")), 1)

    def test_waveform_peaks_zoom_into_actual_audio(self):
        samples = [0] * 40000
        samples[25000] = -32000  # transient at 12.5 s, not at the start of the file
        peaks = app.WaveformPeaks(samples, 2000)
        for start, span in ((0, 20), (12, 1), (12.49, .05)):
            pixels = list(peaks.pixels(start, span, 100))
            expected = round((12.5 - start) / span * 100)
            self.assertAlmostEqual(pixels.index(max(pixels)), expected, delta=1)
            self.assertEqual(max(pixels), 1)
        self.assertFalse(any(peaks.pixels(0, 1, 100)))

    def test_mix_wheel_keeps_scrolled_cursor_time(self):
        tab = self.ui.mix_tab
        self.ui.notebook.select(tab)
        c = tab.timeline
        self.addCleanup(setattr, c, "pixels_per_second", c.pixels_per_second)
        clip = app.AudioClip("long", "long.wav", "long", 4, 3600, 0, "blue")
        tab.clips = [clip]
        c.set_clips(tab.clips)
        self.ui.root.update()
        c.xview_moveto(.35)
        x = c.winfo_width() // 3
        initial, anchor, pos = c.pixels_per_second, c._time_at(x), c.seek.position
        for delta, expected in ((120, initial * 1.5), (-120, initial)):
            c.event_generate("<MouseWheel>", x=x, y=c.TOP + 24, delta=delta)
            self.ui.root.update()
            self.assertAlmostEqual(c.pixels_per_second, expected)
            self.assertAlmostEqual(c._time_at(x), anchor, delta=1.5 / expected)
            self.assertEqual((clip.start, c.seek.position), (4, pos))
            self.assertLess(len(c.find_withtag("ruler_tick")), 80)
        c.xview_moveto(0)

    def test_both_rulers_click_then_drag(self):
        for tab in (self.ui.trim_tab, self.ui.mix_tab):
            with self.subTest(tab=type(tab).__name__):
                self.ui.notebook.select(tab)
                if tab is self.ui.trim_tab:
                    tab._loaded((20, [.25] * 200))
                    c = tab.wave
                    c.set_range(2, 15, notify=True)
                    screen = c._x_for
                else:
                    c = tab.timeline
                    tab.clips = [app.AudioClip("test", "test.wav", "test", 0, 20, 0, "blue")]
                    c.set_clips(tab.clips)
                    c.xview_moveto(0)
                    screen = lambda t: 70 + t * c.pixels_per_second - c.canvasx(0)
                self.ui.root.update()
                with patch.object(self.ui, "seek") as seek:
                    c.event_generate("<ButtonPress-1>", x=round(screen(5)), y=10)
                    self.assertAlmostEqual(tab.seek.position, 5, delta=.02)
                    for t in (8, 11, 7):
                        c.event_generate("<B1-Motion>", x=round(screen(t)), y=85)
                        self.assertAlmostEqual(tab.seek.position, t, delta=.02)
                    seek.assert_not_called()
                    c.event_generate("<ButtonRelease-1>", x=round(screen(9)), y=85)
                    self.assertAlmostEqual(tab.seek.position, 9, delta=.02)
                    self.assertFalse(tab.seek.dragging)
                    seek.assert_called_once_with(tab.seek)
                if tab is self.ui.trim_tab:
                    self.assertEqual((c.start, c.end), (2, 15))
                else:
                    self.assertEqual((tab.clips[0].start, tab.clips[0].lane), (0, 0))


if __name__ == "__main__":
    unittest.main()
