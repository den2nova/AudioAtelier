import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import app


def write_wav(path, data):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 8000, 0, "NONE", ""))
        out.writeframes(data)


@unittest.skipUnless(app.find_tool("ffmpeg") and app.find_tool("ffplay"), "requires FFmpeg")
class PreviewTransportTests(unittest.TestCase):
    def test_loop_has_no_gap_or_skipped_samples(self):
        data = struct.pack("<800h", *range(800))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trim_preview_test.wav"
            write_wav(path, data)
            for offset in (0, .075):
                result = subprocess.run([
                    app.FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                    app.loop_preview_graph(path, offset), "-t", "0.3", "-f", "s16le", "pipe:1"
                ], cwd=folder, capture_output=True, check=True, timeout=10)
                expected = data[2 * round(offset * 8000):] + data * 3
                self.assertEqual(result.stdout, expected[:len(data) * 3])

    def test_seam_pause_resume_stop(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "TEMP_DIR", Path(folder)), \
                patch.object(app, "ensure_data_dirs"), patch.dict(os.environ, SDL_AUDIODRIVER="dummy"):
            path = Path(folder) / "tone.wav"
            write_wav(path, struct.pack("<800h", *range(800)) * 80)
            ui = app.AudioAtelierApp()
            def wait(test):
                deadline = time.monotonic() + 6
                while not test():
                    ui.root.update()
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
            try:
                tab = ui.trim_tab
                tab.path = str(path)
                tab._loaded((8, [.5] * 150))
                tab.wave.set_range(1, 7, notify=True)
                tab.preview(True, seam=True)
                self.assertEqual(tab.seek.position, 4)
                wait(lambda: ui.player is not None and tab.seek.position > 4.1)
                player = ui.player
                ui.pause_preview(tab.seek)
                pos = tab.seek.position
                self.assertIsNotNone(player.poll())
                self.assertIsNone(ui.player)
                time.sleep(.08)
                ui.root.update()
                self.assertEqual(tab.seek.position, pos)
                tab.preview()
                wait(lambda: ui.player is not None)
                self.assertAlmostEqual(ui.play_origin, pos - 1)
                def click_ruler(seconds):
                    x, y = round(tab.wave._x_for(seconds)), tab.wave.RULER_HEIGHT - 1
                    tab.wave.event_generate("<ButtonPress-1>", x=x, y=y)
                    time.sleep(.06)
                    ui.root.update()  # allow a playback tick before the mouse is released
                    tab.wave.event_generate("<B1-Motion>", x=x + 4, y=y + 2)
                    tab.wave.event_generate("<ButtonRelease-1>", x=x + 4, y=y + 2)
                click_ruler(3)
                self.assertIsNotNone(ui.player)
                self.assertAlmostEqual(ui.play_origin, 2, places=2)
                click_ruler(8)
                self.assertIsNone(ui.player)
                self.assertEqual(tab.seek.position, 7)
                click_ruler(0)
                self.assertIsNone(ui.player)
                self.assertEqual(tab.seek.position, 1)
                tab.stop_preview()
                self.assertEqual(tab.seek.position, 1)
                tab.wave.set_range(2, 2.25, notify=True)
                tab.preview(True, seam=True)
                self.assertEqual(tab.seek.position, 2)
                wait(lambda: ui.player is not None)
                player = ui.player
                wait(lambda: tab.seek.position > 2.14)
                wait(lambda: tab.seek.position < 2.1)
                self.assertIs(player, ui.player)
                self.assertIsNone(player.poll())
                tab.preview()
                wait(lambda: ui.player is not None)
                self.assertEqual(ui.play_origin, 0)
                self.assertFalse(tab.seek.looping)
                tab.stop_preview()
                self.assertEqual(tab.seek.position, 2)
                mix = ui.mix_tab
                mix.clips = [app.AudioClip("tone", str(path), "tone", 0, 4, 0, "blue")]
                mix.timeline.set_clips(mix.clips)
                ui.notebook.select(mix)
                ui.root.update()
                mix.preview()
                wait(lambda: ui.player is not None and mix.seek.position > .12)
                c = mix.timeline
                def mix_x(t):
                    return round(70 + t * c.pixels_per_second - c.canvasx(0))
                c.event_generate("<ButtonPress-1>", x=mix_x(1), y=8)
                c.event_generate("<B1-Motion>", x=mix_x(2), y=80)
                c.event_generate("<ButtonRelease-1>", x=mix_x(2), y=80)
                self.assertIsNotNone(ui.player)
                self.assertAlmostEqual(ui.play_origin, 2, places=2)
                ui.pause_preview(mix.seek)
                pos = mix.seek.position
                self.assertIsNone(ui.player)
                mix.preview()
                wait(lambda: ui.player is not None)
                self.assertAlmostEqual(ui.play_origin, pos)
                mix.stop_preview()
                self.assertEqual(mix.seek.position, 0)
            finally:
                for timer in ui.root.tk.call("after", "info"):
                    ui.root.after_cancel(timer)
                ui.close()
