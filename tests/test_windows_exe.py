"""Smoke tests for the actual one-file artifact after running build.ps1."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave


EXE = Path(__file__).resolve().parents[1] / "dist" / "AudioAtelier.exe"


@unittest.skipUnless(sys.platform == "win32" and EXE.is_file(), "requires a Windows build")
class WindowsExeTests(unittest.TestCase):
    def test_gui_subsystem(self):
        with EXE.open("rb") as f:
            f.seek(0x3C)
            pe = int.from_bytes(f.read(4), "little")
            f.seek(pe)
            self.assertEqual(f.read(4), b"PE\0\0")
            f.seek(pe + 24 + 68)  # Optional header: Subsystem in PE32 and PE32+
            self.assertEqual(struct.unpack("<H", f.read(2))[0], 2)  # WINDOWS_GUI

    def test_cli_streams_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "音声.wav"
            with wave.open(str(path), "wb") as out:
                out.setparams((1, 2, 8000, 0, "NONE", ""))
                out.writeframes(bytes(1600))
            args = [EXE, "probe", "--input", path, "--json", "--dry-run"]
            result = subprocess.run(args, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "dry-run")
            self.assertFalse(result.stderr)
            path.unlink()
            result = subprocess.run(args, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)["status"], "error")
            self.assertFalse(result.stdout)
