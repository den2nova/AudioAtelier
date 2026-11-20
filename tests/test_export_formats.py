import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import wave

import app


class ExportFormatsTests(unittest.TestCase):
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

    @unittest.skipUnless(app.find_tool("ffmpeg") and app.find_tool("ffprobe"), "requires FFmpeg")
    def test_trim_and_mix_export_all_profiles(self):
        expected = {"wav": "pcm_s16le", "mp3": "mp3", "m4a": "aac", "ogg": "vorbis"}
        settings = {"wav": {"standard": []},
                    "mp3": {"standard": ["-q:a", "2"], "small": ["-q:a", "5"], "high": ["-q:a", "0"]},
                    "m4a": {"standard": ["-b:a", "256k"], "small": ["-b:a", "128k"], "high": ["-b:a", "320k"]},
                    "ogg": {"standard": ["-q:a", "5"], "small": ["-q:a", "2"], "high": ["-q:a", "8"]}}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "音源.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 44100, 0, "NONE", ""))
                wav.writeframes(b"\x30\x00\xd0\xff" * 22050)
            trim, mix = self.ui.trim_tab, self.ui.mix_tab
            trim.path = str(source)
            trim._loaded((1, [.25] * 100))
            trim.wave.set_range(.2, .7, notify=True)
            mix.clips = [app.AudioClip("a", str(source), "音源", 0, 1, 0, "blue")]
            for tab in (trim, mix):
                for fmt, quality in ((f, q) for f in expected for q in app.QUALITY_PRESETS.values()):
                    with self.subTest(tab=type(tab).__name__, fmt=fmt, quality=quality):
                        label = fmt.upper()
                        tab.export_quality.set(next(k for k, v in app.QUALITY_PRESETS.items() if v == quality))
                        tab.last_export_type.set("WAV")
                        base = Path(tmp) / (type(tab).__name__ + "-音声-" + quality)
                        def save(**opts):
                            self.assertEqual(opts["filetypes"], [(f.upper(), "*." + f) for f in expected])
                            self.assertEqual(opts["typevariable"].get(), "WAV")
                            opts["typevariable"].set(label)
                            return str(base)
                        with patch.object(app.filedialog, "asksaveasfilename", side_effect=save), \
                                patch.object(app.audio, "run_command", wraps=app.audio.run_command) as runner, \
                                patch.object(self.ui.jobs, "submit", side_effect=lambda work, *_: work()):
                            tab.export()
                            cmd = runner.call_args.args[0]
                            self.assertEqual(cmd[cmd.index("-c:a") + 2:-1], [] if fmt == "wav" else settings[fmt][quality])
                        info = app.audio.probe_media(str(base.with_suffix("." + fmt)))
                        self.assertEqual(info["codec"], expected[fmt])
                        self.assertGreater(info["duration"], .4)
                        self.assertEqual(tab.last_export_type.get(), label)

    def test_cancel_or_decline_overwrite(self):
        tab = self.ui.trim_tab
        initial = "MP3"
        tab.last_export_type.set(initial)
        with patch.object(app.filedialog, "asksaveasfilename", return_value=""):
            self.assertIsNone(app.choose_audio_output(tab, "保存", "test", tab.last_export_type))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "test.ogg"
            dest.touch()
            def save(**opts):
                self.assertEqual(opts["typevariable"].get(), initial)
                opts["typevariable"].set("OGG")
                return str(dest.with_suffix(""))
            with patch.object(app.filedialog, "asksaveasfilename", side_effect=save), \
                    patch.object(app.messagebox, "askyesno", return_value=False) as prompt:
                self.assertIsNone(app.choose_audio_output(tab, "保存", "test", tab.last_export_type))
                prompt.assert_called_once()
            self.assertEqual(tab.last_export_type.get(), initial)

    def test_explicit_extension_does_not_reset_quality(self):
        tab = self.ui.trim_tab
        tab.last_export_type.set("WAV")
        tab.export_quality.set("高音質")
        with patch.object(app.filedialog, "asksaveasfilename", return_value="mix.OGG"):
            self.assertEqual(app.choose_audio_output(tab, "保存", "mix", tab.last_export_type),
                             "mix.OGG")
        self.assertEqual(tab.last_export_type.get(), "OGG")
        self.assertEqual(tab.export_quality.get(), "高音質")
