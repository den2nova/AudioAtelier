from __future__ import annotations

import argparse
import array
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import audio_engine as audio
from audio_engine import AudioClip


APP_NAME = "Audio Atelier"
APP_VERSION = "v1.3.0"
AUTO_FADE_SECONDS = audio.AUTO_FADE_SECONDS
MIX_LIMITER_CEILING = audio.MIX_LIMITER_CEILING
BG = "#17191f"
PANEL = "#22252d"
PANEL_2 = "#2b2f39"
TEXT = "#f1f3f5"
MUTED = "#a7adb8"
ACCENT = "#58a6ff"
ACCENT_2 = "#63d8c6"
WARNING = "#ffb454"
CLIP_COLORS = ["#3778c2", "#9472c9", "#c0628c", "#458f79", "#b27843", "#6676c8"]
MIN_TRIM_PREVIEW_SECONDS = 0.300


def base_dir() -> Path:
    return audio.BASE_DIR


BASE_DIR = base_dir()
DATA_DIR = BASE_DIR / "app_data"
TEMP_DIR = DATA_DIR / "temp"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)
    for pattern in ("trim_preview_*.wav", "mix_preview*.wav"):
        for path in TEMP_DIR.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def resource_path(relative: str) -> Path:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir / relative


def find_tool(name: str) -> str | None:
    return audio.find_tool(name)


def tool_path(name: str) -> str:
    return audio.tool_path(name)


FFMPEG = tool_path("ffmpeg")
FFPROBE = tool_path("ffprobe")
FFPLAY = tool_path("ffplay")


def creation_flags() -> int:
    return audio.creation_flags()


def probe_duration(path: str) -> float:
    return audio.probe_duration(path)


def waveform_points(path: str, count: int = 1400) -> list[float]:
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "2000", "-f", "s16le", "-"],
        capture_output=True,
        creationflags=creation_flags(),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "波形を読み込めませんでした。")
    samples = array.array("h")
    samples.frombytes(proc.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return [0.0] * count
    bucket = max(1, math.ceil(len(samples) / count))
    points: list[float] = []
    for i in range(0, len(samples), bucket):
        part = samples[i : i + bucket]
        points.append(max(abs(x) for x in part) / 32768.0)
    peak = max(points) or 1.0
    return [min(1.0, p / peak) for p in points]


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    sec = seconds - minutes * 60
    return f"{minutes:02d}:{sec:06.3f}"


def gain_db_to_percent(gain_db: float) -> float:
    """Convert an amplitude gain in dB to an easy-to-read percentage."""
    return 100.0 * (10.0 ** (gain_db / 20.0))


def audio_args_for(path: str) -> list[str]:
    return audio.audio_args_for(path)


def boundary_fade_filter(duration: float) -> str:
    return audio.boundary_fade_filter(duration)


def run_ffmpeg(args: list[str]) -> None:
    audio.run_ffmpeg(args)


class BackgroundJobs:
    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.events: queue.Queue[tuple] = queue.Queue()
        self.root.after(100, self._poll)

    def submit(self, work, done=None, failed=None) -> None:
        def runner():
            try:
                result = work()
                self.events.put((done, result, None))
            except Exception as exc:  # GUI境界で表示する
                self.events.put((failed, None, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                callback, result, error = self.events.get_nowait()
                if callback:
                    callback(error if error else result)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


class WaveformCanvas(tk.Canvas):
    def __init__(self, parent, on_range_change, **kwargs):
        super().__init__(parent, bg="#111319", highlightthickness=1, highlightbackground="#383d48", **kwargs)
        self.on_range_change = on_range_change
        self.points: list[float] = []
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.playhead: float | None = None
        self.dragging: str | None = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "dragging", None))

    def set_data(self, points: list[float], duration: float) -> None:
        self.points = points
        self.duration = duration
        self.start = 0.0
        self.end = duration
        self.redraw()

    def set_range(self, start: float, end: float, notify=False) -> None:
        if self.duration <= 0:
            return
        self.start = max(0.0, min(float(start), self.duration))
        self.end = max(self.start, min(float(end), self.duration))
        self.redraw()
        if notify:
            self.on_range_change(self.start, self.end)

    def set_playhead(self, seconds: float | None) -> None:
        self.playhead = seconds
        self.delete("playhead")
        self._draw_playhead()

    def _draw_playhead(self) -> None:
        if self.playhead is None or self.duration <= 0:
            return
        x = self._x_for(self.playhead)
        h = self.winfo_height()
        self.create_line(x, 0, x, h, fill="#ff4f67", width=3, tags="playhead")
        self.create_polygon(x - 7, 0, x + 7, 0, x, 11, fill="#ff4f67", outline="", tags="playhead")
        self.tag_raise("playhead")

    def _x_for(self, seconds: float) -> float:
        return 12 + (max(0.0, seconds) / max(self.duration, 0.001)) * max(1, self.winfo_width() - 24)

    def _time_for(self, x: float) -> float:
        usable = max(1, self.winfo_width() - 24)
        return max(0.0, min(self.duration, ((x - 12) / usable) * self.duration))

    def _press(self, event) -> None:
        if not self.points:
            return
        sx, ex = self._x_for(self.start), self._x_for(self.end)
        if abs(event.x - sx) <= abs(event.x - ex):
            self.dragging = "start"
        else:
            self.dragging = "end"
        self._motion(event)

    def _motion(self, event) -> None:
        value = self._time_for(event.x)
        minimum = min(0.05, self.duration)
        if self.dragging == "start":
            self.start = max(0.0, min(value, self.end - minimum))
        elif self.dragging == "end":
            self.end = min(self.duration, max(value, self.start + minimum))
        self.redraw()
        self.on_range_change(self.start, self.end)

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 24 or h <= 20:
            return
        mid = h / 2
        self.create_line(12, mid, w - 12, mid, fill="#3a404d")
        if not self.points:
            self.create_text(w / 2, mid, text="動画または音声ファイルを読み込んでください", fill=MUTED)
            return
        usable = w - 24
        step = max(1, len(self.points) / usable)
        for px in range(int(usable)):
            idx = min(len(self.points) - 1, int(px * step))
            amp = self.points[idx] * (h * 0.39)
            self.create_line(12 + px, mid - amp, 12 + px, mid + amp, fill=ACCENT_2)
        sx, ex = self._x_for(self.start), self._x_for(self.end)
        self.create_rectangle(0, 0, sx, h, fill="#232630", outline="")
        self.create_rectangle(ex, 0, w, h, fill="#232630", outline="")
        for x, color in ((sx, ACCENT), (ex, WARNING)):
            self.create_line(x, 0, x, h, fill=color, width=3)
            self.create_polygon(x - 7, 0, x + 7, 0, x, 10, fill=color, outline="")
        self.create_text(16, 14, anchor="w", text=format_time(self.start), fill=TEXT)
        self.create_text(w - 16, 14, anchor="e", text=format_time(self.end), fill=TEXT)
        self._draw_playhead()


class TrimTab(ttk.Frame):
    def __init__(self, parent, app: "AudioAtelierApp") -> None:
        super().__init__(parent, padding=18)
        self.app = app
        self.path = ""
        self.duration = 0.0
        self.start_var = tk.StringVar(value="0.000")
        self.end_var = tk.StringVar(value="0.000")
        self.status = tk.StringVar(value="動画または音声を選択してください")
        self.format_var = tk.StringVar(value="wav")
        self.preview_serial = 0
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Button(top, text="動画・音声を選択", command=self.choose_file, style="Accent.TButton").pack(side="left")
        self.file_label = ttk.Label(top, text="未選択", foreground=MUTED)
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)

        ttk.Label(self, text="波形の左右ハンドルをドラッグして、切り出す範囲を指定します。", foreground=MUTED).pack(anchor="w", pady=(18, 7))
        self.wave = WaveformCanvas(self, self._range_changed, height=260)
        self.wave.pack(fill="both", expand=True)

        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=14)
        ttk.Label(controls, text="開始（秒）").pack(side="left")
        start_entry = ttk.Entry(controls, textvariable=self.start_var, width=11)
        start_entry.pack(side="left", padx=(7, 18))
        ttk.Label(controls, text="終了（秒）").pack(side="left")
        end_entry = ttk.Entry(controls, textvariable=self.end_var, width=11)
        end_entry.pack(side="left", padx=(7, 18))
        ttk.Button(controls, text="数値を反映", command=self.apply_entries).pack(side="left")
        ttk.Label(controls, text="出力形式").pack(side="left", padx=(28, 7))
        ttk.Combobox(controls, textvariable=self.format_var, values=("wav", "mp3", "m4a"), state="readonly", width=7).pack(side="left")

        actions = ttk.Frame(self)
        actions.pack(fill="x")
        ttk.Button(actions, text="▶ 選択範囲を試聴", command=self.preview).pack(side="left")
        ttk.Button(actions, text="■ 停止", command=self.stop_preview).pack(side="left", padx=8)
        ttk.Button(actions, text="選択範囲を書き出す", command=self.export, style="Accent.TButton").pack(side="right")
        ttk.Label(self, textvariable=self.status, foreground=MUTED).pack(anchor="w", pady=(12, 0))

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="動画または音声を選択",
            filetypes=[
                ("動画・音声", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.mpeg *.mpg *.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma *.opus"),
                ("動画", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.mpeg *.mpg"),
                ("音声", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma *.opus"),
                ("すべて", "*.*"),
            ],
        )
        if not path:
            return
        self.preview_serial += 1
        self.app.stop_playback()
        self.path = path
        self.file_label.configure(text=Path(path).name)
        self.status.set("波形を解析しています…")

        def load():
            duration = probe_duration(path)
            return duration, waveform_points(path)

        self.app.jobs.submit(load, self._loaded, self._load_failed)

    def _loaded(self, result) -> None:
        self.duration, points = result
        self.wave.set_data(points, self.duration)
        self._range_changed(0.0, self.duration)
        self.status.set(f"読み込み完了　全体 {format_time(self.duration)}")

    def _load_failed(self, exc: Exception) -> None:
        self.status.set("読み込みに失敗しました")
        messagebox.showerror(APP_NAME, str(exc))

    def _range_changed(self, start: float, end: float) -> None:
        self.preview_serial += 1
        self.app.stop_playback()
        self.start_var.set(f"{start:.3f}")
        self.end_var.set(f"{end:.3f}")
        if self.duration:
            self.status.set(f"選択範囲 {format_time(start)} ～ {format_time(end)}（{end-start:.3f} 秒）")

    def apply_entries(self) -> bool:
        try:
            start, end = float(self.start_var.get()), float(self.end_var.get())
            if start < 0 or end <= start or end > self.duration + 0.001:
                raise ValueError
            self.wave.set_range(start, end, notify=True)
            return True
        except ValueError:
            messagebox.showwarning(APP_NAME, "開始・終了秒を正しく入力してください。")
            return False

    def stop_preview(self) -> None:
        self.preview_serial += 1
        self.app.stop_playback()
        self.status.set("試聴を停止しました")

    def preview(self) -> None:
        if not self.path or self.duration <= 0:
            messagebox.showinfo(APP_NAME, "先にファイルを読み込んでください。")
            return
        if not self.apply_entries():
            return
        start, end = float(self.start_var.get()), float(self.end_var.get())
        duration = end - start
        source = self.path
        out = str(TEMP_DIR / f"trim_preview_{uuid.uuid4().hex}.wav")
        self.preview_serial += 1
        serial = self.preview_serial
        self.app.stop_playback()
        self.status.set("選択範囲を正確に切り出しています…")

        def work():
            playback_duration = max(duration, MIN_TRIM_PREVIEW_SECONDS)
            preview_filter = audio.precise_trim_filter(start, end)
            if playback_duration > duration:
                preview_filter += f",apad=pad_dur={playback_duration-duration:.6f}"
            run_ffmpeg([
                "-i", source, "-map", "0:a:0", "-vn",
                "-af", preview_filter,
                "-c:a", "pcm_s16le", out,
            ])
            return serial, out, start, duration, playback_duration

        def failed(exc: Exception) -> None:
            Path(out).unlink(missing_ok=True)
            if serial == self.preview_serial:
                self.status.set("試聴の準備に失敗しました")
                messagebox.showerror(APP_NAME, str(exc))

        self.app.jobs.submit(work, self._preview_ready, failed)

    def _preview_ready(self, result) -> None:
        serial, path, start, duration, playback_duration = result
        if serial != self.preview_serial:
            Path(path).unlink(missing_ok=True)
            return

        def update_playhead(position: float | None) -> None:
            self.wave.set_playhead(None if position is None else start + min(position, duration))
            if position is None and serial == self.preview_serial:
                self.status.set(f"試聴終了　選択範囲 {duration:.3f} 秒")

        self.status.set(f"選択範囲を再生しています（{duration:.3f} 秒）")
        self.app.play(
            path,
            duration=playback_duration,
            progress_callback=update_playhead,
            cleanup_path=path,
        )

    def export(self) -> None:
        if not self.path or self.duration <= 0:
            messagebox.showinfo(APP_NAME, "先にファイルを読み込んでください。")
            return
        if not self.apply_entries():
            return
        fmt = self.format_var.get()
        suggested = f"{Path(self.path).stem}_trim.{fmt}"
        out = filedialog.asksaveasfilename(title="音声を書き出す", defaultextension=f".{fmt}", initialfile=suggested, filetypes=[(fmt.upper(), f"*.{fmt}")])
        if not out:
            return
        start, end = float(self.start_var.get()), float(self.end_var.get())
        self.status.set("音声を書き出しています…")

        def work():
            run_ffmpeg([
                "-i", self.path, "-map", "0:a:0", "-vn",
                "-af", audio.precise_trim_filter(start, end),
                *audio_args_for(out), out,
            ])
            return out

        self.app.jobs.submit(work, lambda p: self._exported(p), lambda e: self._export_failed(e))

    def _exported(self, path: str) -> None:
        self.status.set(f"保存しました: {path}")
        messagebox.showinfo(APP_NAME, f"音声を保存しました。\n{path}")

    def _export_failed(self, exc: Exception) -> None:
        self.status.set("書き出しに失敗しました")
        messagebox.showerror(APP_NAME, str(exc))


class TimelineCanvas(tk.Canvas):
    TOP = 34
    LANE_H = 66

    def __init__(self, parent, on_select, on_move, **kwargs):
        super().__init__(parent, bg="#111319", highlightthickness=1, highlightbackground="#383d48", **kwargs)
        self.on_select = on_select
        self.on_move = on_move
        self.clips: list[AudioClip] = []
        self.selected: str | None = None
        self.pixels_per_second = 30.0
        self.drag_clip: AudioClip | None = None
        self.drag_offset_x = 0.0
        self.playhead: float | None = None
        self.timeline_width = 1.0
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_zoom(self, value: float) -> None:
        self.pixels_per_second = float(value)
        self.redraw()

    def set_clips(self, clips: list[AudioClip]) -> None:
        self.clips = clips
        if self.selected and not any(c.id == self.selected for c in clips):
            self.selected = None
        self.redraw()

    def set_playhead(self, seconds: float | None) -> None:
        self.playhead = seconds
        self.delete("playhead")
        self._draw_playhead()
        if seconds is None:
            return
        x = 70 + seconds * self.pixels_per_second
        left = self.canvasx(0)
        right = self.canvasx(self.winfo_width())
        if x < left + 30 or x > right - 50:
            target = max(0.0, x - self.winfo_width() * 0.3)
            self.xview_moveto(min(1.0, target / max(1.0, self.timeline_width)))

    def _draw_playhead(self) -> None:
        if self.playhead is None:
            return
        x = 70 + self.playhead * self.pixels_per_second
        height = max(self.winfo_height(), int(float(self.cget("scrollregion").split()[-1])) if self.cget("scrollregion") else self.winfo_height())
        self.create_line(x, 0, x, height, fill="#ff4f67", width=3, tags="playhead")
        self.create_polygon(x - 7, 0, x + 7, 0, x, 11, fill="#ff4f67", outline="", tags="playhead")
        self.create_text(x + 6, 18, anchor="nw", text=format_time(self.playhead), fill="#ff8798", tags="playhead")
        self.tag_raise("playhead")

    def redraw(self) -> None:
        self.delete("all")
        max_end = max([c.start + c.duration for c in self.clips] + [60.0])
        width = max(self.winfo_width(), 120 + max_end * self.pixels_per_second)
        self.timeline_width = width
        lanes = max([c.lane for c in self.clips] + [3]) + 1
        height = max(self.winfo_height(), self.TOP + lanes * self.LANE_H)
        self.configure(scrollregion=(0, 0, width, height))

        interval = 1
        if self.pixels_per_second < 15:
            interval = 5
        elif self.pixels_per_second > 70:
            interval = 0.5
        t = 0.0
        while t <= max_end + 10:
            x = 70 + t * self.pixels_per_second
            self.create_line(x, 18, x, height, fill="#2c303a")
            self.create_text(x + 3, 8, anchor="nw", text=f"{t:g}s", fill=MUTED, font=("Segoe UI", 8))
            t += interval
        for lane in range(lanes):
            y = self.TOP + lane * self.LANE_H
            self.create_rectangle(0, y, width, y + self.LANE_H, fill="#161920" if lane % 2 == 0 else "#1a1d25", outline="")
            self.create_text(8, y + self.LANE_H / 2, anchor="w", text=f"{lane+1}", fill=MUTED)
            self.create_line(0, y, width, y, fill="#303541")

        for clip in self.clips:
            x1 = 70 + clip.start * self.pixels_per_second
            x2 = x1 + max(8, clip.duration * self.pixels_per_second)
            y1 = self.TOP + clip.lane * self.LANE_H + 7
            y2 = y1 + self.LANE_H - 14
            outline = "#ffffff" if clip.id == self.selected else clip.color
            self.create_rectangle(x1, y1, x2, y2, fill=clip.color, outline=outline, width=2, tags=("clip", clip.id))
            flags = ""
            if clip.loop:
                flags += "  ↻ループ"
            if clip.mute:
                flags += "  ミュート"
            if clip.gain_db:
                flags += f"  音量{gain_db_to_percent(clip.gain_db):.0f}%"
            label = f"{clip.name}  {clip.duration:.2f}s{flags}"
            self.create_text(x1 + 8, (y1 + y2) / 2, anchor="w", text=label, fill="white", tags=("clip", clip.id))
        self._draw_playhead()

    def _clip_at(self, x: float, y: float) -> AudioClip | None:
        for clip in reversed(self.clips):
            x1 = 70 + clip.start * self.pixels_per_second
            x2 = x1 + max(8, clip.duration * self.pixels_per_second)
            y1 = self.TOP + clip.lane * self.LANE_H + 7
            y2 = y1 + self.LANE_H - 14
            if x1 <= x <= x2 and y1 <= y <= y2:
                return clip
        return None

    def _press(self, event) -> None:
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        clip = self._clip_at(x, y)
        if clip:
            self.selected = clip.id
            self.drag_clip = clip
            self.drag_offset_x = x - (70 + clip.start * self.pixels_per_second)
            self.on_select(clip)
            self.redraw()

    def _motion(self, event) -> None:
        if not self.drag_clip:
            return
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        raw_start = (x - self.drag_offset_x - 70) / self.pixels_per_second
        self.drag_clip.start = max(0.0, round(raw_start * 10) / 10)
        self.drag_clip.lane = max(0, min(15, int((y - self.TOP) // self.LANE_H)))
        self.on_move(self.drag_clip)
        self.redraw()

    def _release(self, _event) -> None:
        self.drag_clip = None


class MixTab(ttk.Frame):
    def __init__(self, parent, app: "AudioAtelierApp") -> None:
        super().__init__(parent, padding=18)
        self.app = app
        self.clips: list[AudioClip] = []
        self.selected: AudioClip | None = None
        self.gap_var = tk.StringVar(value="0.0")
        self.start_var = tk.StringVar(value="0.000")
        self.lane_var = tk.StringVar(value="1")
        self.zoom_var = tk.DoubleVar(value=30)
        self.format_var = tk.StringVar(value="wav")
        self.status = tk.StringVar(value="音声ファイルを追加してください")
        self.project_path: str | None = None
        self.project_duration: float | None = None
        self.sample_rate = 48000
        self.channels = 2
        self.loudness_lufs: float | None = None
        self.true_peak_db = -1.0
        self.preview_serial = 0
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="＋ 音声を追加", command=self.add_files, style="Accent.TButton").pack(side="left")
        ttk.Button(toolbar, text="選択を削除", command=self.delete_selected).pack(side="left", padx=8)
        ttk.Button(toolbar, text="すべて消去", command=self.clear_all).pack(side="left")
        ttk.Button(toolbar, text="JSONを開く", command=self.open_project).pack(side="left", padx=(16, 8))
        ttk.Button(toolbar, text="JSON保存", command=self.save_project).pack(side="left")
        ttk.Label(toolbar, text="ズーム").pack(side="right", padx=(14, 5))
        ttk.Scale(toolbar, from_=5, to=100, variable=self.zoom_var, command=lambda v: self.timeline.set_zoom(float(v)), length=160).pack(side="right")

        ttk.Label(self, text="クリップを左右に動かすと開始時間、上下に動かすとレーンが変わります。時間が重なる部分は同時に鳴ります。", foreground=MUTED).pack(anchor="w", pady=(14, 7))
        holder = ttk.Frame(self)
        holder.pack(fill="x")
        # 秒数目盛りと4レーンがちょうど収まる高さ。5レーン目以降は縦スクロールで表示する。
        timeline_height = TimelineCanvas.TOP + TimelineCanvas.LANE_H * 4 + 2
        self.timeline = TimelineCanvas(holder, self.select_clip, self.clip_moved, height=timeline_height)
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=self.timeline.xview)
        ybar = ttk.Scrollbar(holder, orient="vertical", command=self.timeline.yview)
        self.timeline.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.timeline.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        arrange = ttk.Frame(self)
        arrange.pack(fill="x", pady=(12, 8))
        ttk.Label(arrange, text="クリップ間の無音（秒）").pack(side="left")
        ttk.Entry(arrange, textvariable=self.gap_var, width=8).pack(side="left", padx=7)
        ttk.Button(arrange, text="全て順番に並べる", command=self.arrange_sequential).pack(side="left", padx=(4, 8))
        ttk.Button(arrange, text="全て先頭で重ねる", command=self.arrange_overlay).pack(side="left")

        selected = ttk.LabelFrame(self, text="選択中のクリップ", padding=9)
        selected.pack(fill="x")
        self.selected_label = ttk.Label(selected, text="未選択", foreground=MUTED)
        self.selected_label.pack(side="left", fill="x", expand=True)
        ttk.Label(selected, text="開始（秒）").pack(side="left")
        ttk.Entry(selected, textvariable=self.start_var, width=10).pack(side="left", padx=6)
        ttk.Label(selected, text="レーン").pack(side="left", padx=(8, 0))
        ttk.Entry(selected, textvariable=self.lane_var, width=5).pack(side="left", padx=6)
        ttk.Button(selected, text="反映", command=self.apply_selected_values).pack(side="left")
        ttk.Button(selected, text="詳細設定", command=self.open_clip_details).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="▶ 全体を試聴", command=self.preview).pack(side="left")
        ttk.Button(actions, text="■ 停止", command=self.stop_preview).pack(side="left", padx=8)
        ttk.Label(actions, text="出力形式").pack(side="right", padx=(12, 6))
        ttk.Combobox(actions, textvariable=self.format_var, values=("wav", "mp3", "m4a"), state="readonly", width=7).pack(side="right")
        ttk.Button(actions, text="合成音声を書き出す", command=self.export, style="Accent.TButton").pack(side="right", padx=8)
        ttk.Label(self, textvariable=self.status, foreground=MUTED).pack(anchor="w", pady=(10, 0))

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="音声ファイルを追加",
            filetypes=[("音声", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma *.opus"), ("すべて", "*.*")],
        )
        if not paths:
            return
        self.status.set(f"{len(paths)} 個のファイルを解析しています…")

        def load():
            return [(p, probe_duration(p)) for p in paths]

        self.app.jobs.submit(load, self._files_loaded, lambda e: self._load_failed(e))

    def open_project(self) -> None:
        path = filedialog.askopenfilename(title="JSONプロジェクトを開く", filetypes=[("Audio Atelier JSON", "*.json"), ("すべて", "*.*")])
        if not path:
            return
        self.status.set("JSONプロジェクトを読み込んでいます…")
        self.app.jobs.submit(lambda: audio.load_project(path), self._project_loaded, lambda e: self._load_failed(e))

    def _project_loaded(self, project: audio.AudioProject) -> None:
        for index, clip in enumerate(project.clips):
            if not clip.color:
                clip.color = CLIP_COLORS[index % len(CLIP_COLORS)]
        self.clips[:] = project.clips
        self.project_path = project.project_path
        self.project_duration = project.duration
        self.sample_rate = project.sample_rate
        self.channels = project.channels
        self.loudness_lufs = project.loudness_lufs
        self.true_peak_db = project.true_peak_db
        self.selected = None
        self.selected_label.configure(text="未選択")
        self.timeline.set_clips(self.clips)
        self.status.set(f"JSONを開きました　{len(self.clips)} 個のクリップ　全体 {format_time(self.total_duration())}")

    def save_project(self) -> None:
        if not self.clips:
            messagebox.showinfo(APP_NAME, "先に音声ファイルを追加してください。")
            return
        path = filedialog.asksaveasfilename(
            title="JSONプロジェクトを保存",
            defaultextension=".json",
            initialfile=Path(self.project_path).name if self.project_path else "audio_project.json",
            filetypes=[("Audio Atelier JSON", "*.json")],
        )
        if not path:
            return
        try:
            self.project_path = audio.save_project(
                path, self.clips, self.sample_rate, self.channels, self.total_duration(),
                self.loudness_lufs, self.true_peak_db,
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"JSONを保存できませんでした。\n{exc}")
            return
        self.status.set(f"JSONを保存しました: {self.project_path}")

    def _files_loaded(self, items) -> None:
        cursor = max([c.start + c.duration for c in self.clips] + [0.0])
        try:
            gap = max(0.0, float(self.gap_var.get()))
        except ValueError:
            gap = 0.0
        for path, duration in items:
            index = len(self.clips)
            self.clips.append(AudioClip(str(uuid.uuid4()), path, Path(path).name, cursor, duration, index % 4, CLIP_COLORS[index % len(CLIP_COLORS)]))
            cursor += duration + gap
        self.timeline.set_clips(self.clips)
        self.status.set(f"{len(self.clips)} 個のクリップ　全体 {format_time(self.total_duration())}")

    def _load_failed(self, exc: Exception) -> None:
        self.status.set("読み込みに失敗しました")
        messagebox.showerror(APP_NAME, str(exc))

    def select_clip(self, clip: AudioClip) -> None:
        self.selected = clip
        self.selected_label.configure(text=clip.name)
        self.start_var.set(f"{clip.start:.3f}")
        self.lane_var.set(str(clip.lane + 1))

    def clip_moved(self, clip: AudioClip) -> None:
        if self.selected and self.selected.id == clip.id:
            self.start_var.set(f"{clip.start:.3f}")
            self.lane_var.set(str(clip.lane + 1))
        self.status.set(f"全体 {format_time(self.total_duration())}")

    def apply_selected_values(self) -> None:
        if not self.selected:
            return
        try:
            self.selected.start = max(0.0, float(self.start_var.get()))
            self.selected.lane = max(0, min(15, int(self.lane_var.get()) - 1))
        except ValueError:
            messagebox.showwarning(APP_NAME, "開始秒とレーンを正しく入力してください。")
            return
        self.select_clip(self.selected)
        self.timeline.redraw()

    def open_clip_details(self) -> None:
        if not self.selected:
            messagebox.showinfo(APP_NAME, "先にクリップを選択してください。")
            return
        clip = self.selected
        dialog = tk.Toplevel(self)
        dialog.title(f"クリップ詳細 - {clip.name}")
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        values = {
            "trim_in": tk.StringVar(value=f"{clip.trim_in:.3f}"),
            "trim_out": tk.StringVar(value=f"{(clip.trim_out if clip.trim_out is not None else clip.trim_in + clip.duration):.3f}"),
            "gain_db": tk.StringVar(value=f"{clip.gain_db:.2f}"),
            "fade_in": tk.StringVar(value=f"{clip.fade_in:.3f}"),
            "fade_out": tk.StringVar(value=f"{clip.fade_out:.3f}"),
        }
        trim_labels = (
            ("音声内の開始位置（秒）", "trim_in"),
            ("音声内の終了位置（秒）", "trim_out"),
        )
        for row, (label, key) in enumerate(trim_labels):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=values[key], width=14).grid(row=row, column=1, padx=(14, 0), pady=4)

        gain_scale_var = tk.DoubleVar(value=max(-40.0, min(12.0, clip.gain_db)))
        gain_description_var = tk.StringVar()

        def update_gain_description() -> None:
            try:
                gain_db = float(values["gain_db"].get())
                if not math.isfinite(gain_db):
                    raise ValueError
            except ValueError:
                gain_description_var.set("dBを数値で入力してください")
                return
            percent = gain_db_to_percent(gain_db)
            if abs(gain_db) < 0.005:
                direction = "元の音量"
            elif gain_db < 0:
                direction = "小さくする"
            else:
                direction = "大きくする"
            gain_description_var.set(f"{direction}（約{percent:.0f}% / {gain_db:+.1f} dB）")

        def set_gain(gain_db: float) -> None:
            values["gain_db"].set(f"{gain_db:.1f}")
            gain_scale_var.set(max(-40.0, min(12.0, gain_db)))
            update_gain_description()

        def gain_scale_changed(value: str) -> None:
            values["gain_db"].set(f"{float(value):.1f}")
            update_gain_description()

        def gain_entry_changed(_event=None) -> None:
            try:
                gain_db = float(values["gain_db"].get())
                if math.isfinite(gain_db):
                    gain_scale_var.set(max(-40.0, min(12.0, gain_db)))
            except ValueError:
                pass
            update_gain_description()

        ttk.Label(frame, text="音量調整").grid(row=2, column=0, sticky="w", pady=(12, 4))
        gain_entry_frame = ttk.Frame(frame)
        gain_entry_frame.grid(row=2, column=1, sticky="e", padx=(14, 0), pady=(12, 4))
        gain_entry = ttk.Entry(gain_entry_frame, textvariable=values["gain_db"], width=9)
        gain_entry.pack(side="left")
        ttk.Label(gain_entry_frame, text="dB").pack(side="left", padx=(5, 0))
        gain_entry.bind("<KeyRelease>", gain_entry_changed)
        gain_entry.bind("<FocusOut>", gain_entry_changed)
        ttk.Scale(
            frame,
            from_=-40.0,
            to=12.0,
            variable=gain_scale_var,
            command=gain_scale_changed,
            length=310,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 3))
        ttk.Label(frame, textvariable=gain_description_var).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frame,
            text="0 dB＝元の音量。マイナスで小さく、プラスで大きくなります。",
            foreground=MUTED,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 6))
        presets = ttk.Frame(frame)
        presets.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for text, gain_db in (("小さく -12", -12.0), ("少し小さく -6", -6.0), ("元の音量 0", 0.0), ("少し大きく +3", 3.0)):
            ttk.Button(presets, text=text, command=lambda value=gain_db: set_gain(value)).pack(side="left", padx=(0, 5))

        fade_labels = (
            ("フェードイン（秒）", "fade_in"),
            ("フェードアウト（秒）", "fade_out"),
        )
        for row, (label, key) in enumerate(fade_labels, start=7):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=values[key], width=14).grid(row=row, column=1, padx=(14, 0), pady=4)
        update_gain_description()

        loop_var = tk.BooleanVar(value=clip.loop)
        mute_var = tk.BooleanVar(value=clip.mute)
        ttk.Checkbutton(frame, text="プロジェクトの終端までループ", variable=loop_var).grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 2))
        ttk.Checkbutton(frame, text="ミュート", variable=mute_var).grid(row=10, column=0, columnspan=2, sticky="w", pady=2)

        def apply_details() -> None:
            try:
                trim_in = float(values["trim_in"].get())
                trim_out = float(values["trim_out"].get())
                gain_db = float(values["gain_db"].get())
                fade_in = float(values["fade_in"].get())
                fade_out = float(values["fade_out"].get())
                source_duration = probe_duration(clip.path)
                if trim_in < 0 or trim_out <= trim_in or trim_out > source_duration + 0.05:
                    raise ValueError("開始・終了位置は音声の長さ以内で、終了を開始より後にしてください。")
                if not all(math.isfinite(v) for v in (gain_db, fade_in, fade_out)) or fade_in < 0 or fade_out < 0:
                    raise ValueError("音量は有限な数値、フェードは0以上で入力してください。")
            except ValueError as exc:
                messagebox.showwarning(APP_NAME, str(exc), parent=dialog)
                return
            clip.trim_in = trim_in
            clip.trim_out = trim_out
            clip.duration = trim_out - trim_in
            clip.gain_db = gain_db
            clip.fade_in = fade_in
            clip.fade_out = fade_out
            clip.loop = loop_var.get()
            clip.mute = mute_var.get()
            self.timeline.redraw()
            self.status.set(f"クリップ設定を更新しました　全体 {format_time(self.total_duration())}")
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="left")
        ttk.Button(buttons, text="反映", command=apply_details, style="Accent.TButton").pack(side="left", padx=(8, 0))
        dialog.grab_set()

    def delete_selected(self) -> None:
        if not self.selected:
            return
        self.clips[:] = [c for c in self.clips if c.id != self.selected.id]
        self.selected = None
        self.selected_label.configure(text="未選択")
        self.timeline.set_clips(self.clips)

    def clear_all(self) -> None:
        if self.clips and messagebox.askyesno(APP_NAME, "タイムライン上のクリップをすべて消去しますか？"):
            self.clips.clear()
            self.selected = None
            self.project_path = None
            self.project_duration = None
            self.timeline.set_clips(self.clips)
            self.status.set("音声ファイルを追加してください")

    def arrange_sequential(self) -> None:
        try:
            gap = max(0.0, float(self.gap_var.get()))
        except ValueError:
            messagebox.showwarning(APP_NAME, "無音時間を秒数で入力してください。")
            return
        cursor = 0.0
        for clip in self.clips:
            clip.start = cursor
            clip.lane = 0
            cursor += clip.duration + gap
        self.timeline.redraw()
        self.status.set(f"順番に配置しました　全体 {format_time(self.total_duration())}")

    def arrange_overlay(self) -> None:
        for index, clip in enumerate(self.clips):
            clip.start = 0.0
            clip.lane = index
        self.timeline.redraw()
        self.status.set(f"先頭を揃えて配置しました　全体 {format_time(self.total_duration())}")

    def total_duration(self) -> float:
        return max([c.start + c.duration for c in self.clips] + [self.project_duration or 0.0])

    def _render_mix(self, out: str) -> None:
        project = audio.AudioProject(
            audio.PROJECT_VERSION, self.sample_rate, self.channels, self.clips,
            self.total_duration(), self.loudness_lufs, self.true_peak_db, self.project_path,
        )
        command, _filters, _duration = audio.build_mix_command(project, out, overwrite=True)
        audio.run_command(command)

    def preview(self) -> None:
        if not self.clips:
            messagebox.showinfo(APP_NAME, "先に音声ファイルを追加してください。")
            return
        self.preview_serial += 1
        serial = self.preview_serial
        duration = self.total_duration()
        out = str(TEMP_DIR / f"mix_preview_{uuid.uuid4().hex}.wav")
        self.app.stop_playback()
        self.status.set("試聴用の音声を作成しています…")

        def work():
            self._render_mix(out)
            return serial, out, duration

        def failed(exc: Exception) -> None:
            Path(out).unlink(missing_ok=True)
            if serial == self.preview_serial:
                self._export_failed(exc)

        self.app.jobs.submit(work, self._preview_ready, failed)

    def stop_preview(self) -> None:
        self.preview_serial += 1
        self.app.stop_playback()
        self.status.set("試聴を停止しました")

    def _preview_ready(self, result) -> None:
        serial, path, duration = result
        if serial != self.preview_serial:
            Path(path).unlink(missing_ok=True)
            return

        def update_playhead(position: float | None) -> None:
            self.timeline.set_playhead(position)
            if position is None and serial == self.preview_serial:
                self.status.set("合成結果の試聴が終わりました")

        self.status.set("合成結果を再生しています")
        self.app.play(path, duration=duration, progress_callback=update_playhead, cleanup_path=path)

    def export(self) -> None:
        if not self.clips:
            messagebox.showinfo(APP_NAME, "先に音声ファイルを追加してください。")
            return
        fmt = self.format_var.get()
        out = filedialog.asksaveasfilename(title="合成音声を書き出す", defaultextension=f".{fmt}", initialfile=f"mix.{fmt}", filetypes=[(fmt.upper(), f"*.{fmt}")])
        if not out:
            return
        self.status.set("合成音声を書き出しています…")
        self.app.jobs.submit(lambda: (self._render_mix(out), out)[1], self._exported, lambda e: self._export_failed(e))

    def _exported(self, path: str) -> None:
        self.status.set(f"保存しました: {path}")
        messagebox.showinfo(APP_NAME, f"合成音声を保存しました。\n{path}")

    def _export_failed(self, exc: Exception) -> None:
        self.status.set("処理に失敗しました")
        messagebox.showerror(APP_NAME, str(exc))


class AudioAtelierApp:
    def __init__(self) -> None:
        ensure_data_dirs()
        self.root = tk.Tk()
        self.root.title(f"Audio Atelier {APP_VERSION} - 動画音声切り出し・音声合成")
        initial_height = min(780, max(700, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"1100x{initial_height}")
        self.root.minsize(860, 700)
        self.root.configure(bg=BG)
        icon = resource_path("assets/audio_atelier.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        self.player: subprocess.Popen | None = None
        self.playback_serial = 0
        self.progress_callback = None
        self.play_started_at = 0.0
        self.play_origin = 0.0
        self.play_duration: float | None = None
        self.play_cleanup_path: str | None = None
        self.jobs = BackgroundJobs(self.root)
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(250, self._check_ffmpeg)

    def _check_ffmpeg(self) -> None:
        required_missing = [name for name in ("ffmpeg", "ffprobe") if find_tool(name) is None]
        ffplay_missing = find_tool("ffplay") is None
        if required_missing:
            extra = "\n\nffplayも見つからないため、試聴機能も利用できません。" if ffplay_missing else ""
            messagebox.showwarning(
                APP_NAME,
                "読み込み・書き出しに必要なファイルを見つけられませんでした。\n\n"
                + ", ".join(required_missing)
                + extra
                + "\n\nアプリと同じフォルダ、bin、ffmpeg、ffmpeg\\binのいずれかへ置くか、"
                + "WindowsのPATHへ追加してください。その後、Audio Atelierを完全に終了して起動し直してください。",
            )
        elif ffplay_missing:
            messagebox.showwarning(
                APP_NAME,
                "試聴に必要なffplayを見つけられませんでした。\n\n"
                "読み込み・書き出し・合成は利用できますが、試聴機能は利用できません。\n\n"
                "ffplay.exeをFFmpegと同じ場所へ置き、Audio Atelierを完全に終了して起動し直してください。",
            )

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_2, bordercolor="#3b404b", font=("Yu Gothic UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TLabelframe", background=BG, foreground=TEXT, bordercolor="#3b404b")
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT)
        style.configure("TButton", background=PANEL_2, foreground=TEXT, padding=(12, 7), borderwidth=1)
        style.map("TButton", background=[("active", "#3b414d")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#07111d")
        style.map("Accent.TButton", background=[("active", "#78b7ff")])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT)
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT, arrowcolor=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_2)], foreground=[("readonly", TEXT)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(22, 11))
        style.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", TEXT)])

    def _build(self) -> None:
        header = ttk.Frame(self.root, padding=(20, 14))
        header.pack(fill="x")
        ttk.Label(header, text="Audio Atelier", font=("Yu Gothic UI", 20, "bold")).pack(side="left")
        ttk.Label(header, text=APP_VERSION, foreground=ACCENT_2, font=("Yu Gothic UI", 11, "bold")).pack(side="left", padx=(9, 0), pady=(8, 0))
        ttk.Label(header, text="動画音声の切り出し・音声タイムライン合成", foreground=MUTED).pack(side="left", padx=16, pady=(7, 0))
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        notebook.add(TrimTab(notebook, self), text="  動画から音声を切り出す  ")
        notebook.add(MixTab(notebook, self), text="  音声を合成する  ")

    def play(
        self,
        path: str,
        start: float | None = None,
        duration: float | None = None,
        progress_callback=None,
        cleanup_path: str | None = None,
    ) -> None:
        self.stop_playback()
        args = [FFPLAY, "-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit"]
        if start is not None:
            args += ["-ss", f"{start:.6f}"]
        if duration is not None:
            args += ["-t", f"{max(0.01, duration):.6f}"]
        args.append(path)
        try:
            self.player = subprocess.Popen(args, creationflags=creation_flags())
            self.playback_serial += 1
            serial = self.playback_serial
            self.progress_callback = progress_callback
            self.play_started_at = time.monotonic()
            self.play_origin = start or 0.0
            self.play_duration = duration
            self.play_cleanup_path = cleanup_path
            if self.progress_callback:
                self.progress_callback(self.play_origin)
            self.root.after(40, lambda: self._update_playback(serial))
        except OSError as exc:
            self._cleanup_playback_file(cleanup_path)
            messagebox.showerror(APP_NAME, f"再生を開始できませんでした。\n{exc}")

    def _update_playback(self, serial: int) -> None:
        if serial != self.playback_serial or not self.player:
            return
        if self.player.poll() is not None:
            callback = self.progress_callback
            self.player = None
            self.progress_callback = None
            cleanup_path = self.play_cleanup_path
            self.play_cleanup_path = None
            if callback:
                callback(None)
            self._cleanup_playback_file(cleanup_path)
            return
        elapsed = time.monotonic() - self.play_started_at
        if self.play_duration is not None:
            elapsed = min(elapsed, self.play_duration)
        if self.progress_callback:
            self.progress_callback(self.play_origin + elapsed)
        self.root.after(40, lambda: self._update_playback(serial))

    def stop_playback(self) -> None:
        self.playback_serial += 1
        player = self.player
        if player and player.poll() is None:
            try:
                player.terminate()
                player.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    player.kill()
                    player.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
        self.player = None
        callback = self.progress_callback
        self.progress_callback = None
        cleanup_path = self.play_cleanup_path
        self.play_cleanup_path = None
        if callback:
            callback(None)
        self._cleanup_playback_file(cleanup_path)

    def _cleanup_playback_file(self, path: str | None, retries: int = 5) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            if retries > 0:
                try:
                    self.root.after(200, lambda: self._cleanup_playback_file(path, retries - 1))
                except tk.TclError:
                    pass

    def close(self) -> None:
        self.stop_playback()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _write_result(result: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return
    operation = result.get("operation", "処理")
    status = result.get("status", "ok")
    output = result.get("output")
    suffix = f": {output}" if output else ""
    print(f"{operation} {status}{suffix}", flush=True)


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="AudioAtelier.exe", description="Audio AtelierのヘッドレスCLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe", help="動画・音声の情報を取得します")
    probe.add_argument("--input", required=True, help="調べる動画・音声ファイル")
    probe.add_argument("--json", action="store_true", dest="json_output", help="結果をJSONで標準出力へ返します")
    probe.add_argument("--dry-run", action="store_true", help="ffprobeを実行せずコマンドだけを返します")

    trim = commands.add_parser("trim", help="動画・音声から指定範囲の音声を書き出します")
    trim.add_argument("--input", required=True)
    trim.add_argument("--start", required=True, type=float, help="開始位置（秒）")
    trim.add_argument("--end", required=True, type=float, help="終了位置（秒）")
    trim.add_argument("--output", required=True)
    trim.add_argument("--overwrite", action="store_true", help="既存の出力ファイルを上書きします")
    trim.add_argument("--dry-run", action="store_true", help="FFmpegを実行せずコマンドだけを返します")
    trim.add_argument("--json", action="store_true", dest="json_output", help="結果をJSONで標準出力へ返します")

    mix = commands.add_parser("mix", help="JSONプロジェクトの音声を合成します")
    mix.add_argument("--project", required=True, help="Audio Atelier JSONプロジェクト")
    mix.add_argument("--output", required=True)
    mix.add_argument("--overwrite", action="store_true", help="既存の出力ファイルを上書きします")
    mix.add_argument("--dry-run", action="store_true", help="FFmpegを実行せずコマンドとfilter_complexだけを返します")
    mix.add_argument("--json", action="store_true", dest="json_output", help="結果をJSONで標準出力へ返します")
    return parser


def cli_main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = _cli_parser()
    args = parser.parse_args(argv)
    json_output = bool(getattr(args, "json_output", False))
    try:
        if args.command == "probe":
            input_path = Path(args.input).expanduser().resolve()
            if not input_path.is_file():
                raise FileNotFoundError(f"入力ファイルが見つかりません: {input_path}")
            command = [
                audio.ensure_tool("ffprobe"), "-v", "error", "-show_entries",
                "format=duration,format_name,size:stream=index,codec_type,codec_name,sample_rate,channels,channel_layout",
                "-of", "json", str(input_path),
            ]
            if args.dry_run:
                result = {
                    "status": "dry-run", "operation": "probe", "input": str(input_path),
                    "command": command, "command_line": audio.command_line(command), "ffprobe_exit_code": None,
                }
            else:
                started = time.monotonic()
                result = audio.probe_media(str(input_path))
                result.update({
                    "status": "ok", "operation": "probe", "ffprobe_exit_code": 0,
                    "command_line": audio.command_line(result["command"]),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                })
        elif args.command == "trim":
            result = audio.trim_media(args.input, args.start, args.end, args.output, args.overwrite, args.dry_run)
        else:
            result = audio.mix_project(args.project, args.output, args.overwrite, args.dry_run)
        _write_result(result, json_output)
        return 0
    except Exception as exc:
        error = {
            "status": "error",
            "operation": args.command,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "ffmpeg_exit_code": getattr(exc, "returncode", None),
        }
        if json_output:
            print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        else:
            print(f"エラー: {exc}", file=sys.stderr, flush=True)
        return 1


def _hide_own_console_when_launched_from_explorer() -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        process_ids = (ctypes.c_uint32 * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(process_ids, len(process_ids))
        # PyInstallerのonefile版は、通常起動でも親と子の2プロセスが
        # 同じコンソールへ接続する。シェルから起動した場合はPowerShellや
        # cmdも加わって3以上になるため、2以下のときだけ自前の画面を隠す。
        if count <= 2:
            window = ctypes.windll.kernel32.GetConsoleWindow()
            if window:
                ctypes.windll.user32.ShowWindow(window, 0)
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli_main(sys.argv[1:]))
    _hide_own_console_when_launched_from_explorer()
    AudioAtelierApp().run()
