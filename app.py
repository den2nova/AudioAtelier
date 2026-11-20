from __future__ import annotations

import argparse
import array
import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinterdnd2 import DND_FILES, TkinterDnD

import audio_engine as audio
from audio_engine import AudioClip


APP_NAME = "Audio Atelier"
APP_VERSION = "v1.6.0"
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
RULER_DRAG_PIXELS = 6  # Ignore small click jitter before starting a scrub.
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".mpeg", ".mpg"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".opus"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


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


class WaveformPeaks:
    """Keep the original envelope detail; coarser levels bound redraw cost."""
    GROUP = 8

    def __init__(self, samples, rate):
        self.rate = rate
        self.levels = [samples]
        while len(samples) > 1024:
            peaks = array.array("f")
            for i in range(0, len(samples), self.GROUP):
                block = samples[i:i + self.GROUP]
                peaks.append(max(max(block), -min(block)))
            self.levels.append(peaks)
            samples = peaks
        self.peak = max(max(samples, default=0), -min(samples, default=0)) or 1

    def pixels(self, start, span, width):
        step = span * self.rate / width
        offset = start * self.rate
        level = 0
        while step >= self.GROUP and level + 1 < len(self.levels):
            step /= self.GROUP
            offset /= self.GROUP
            level += 1
        samples = self.levels[level]
        for px in range(width):
            first = int(offset + px * step)
            last = max(first + 1, int(offset + (px + 1) * step))
            block = samples[first:last]
            yield max(max(block), -min(block)) / self.peak if block else 0.0


def waveform_points(path: str) -> WaveformPeaks:
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
    return WaveformPeaks(samples, 2000)


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    sec = seconds - minutes * 60
    return f"{minutes:02d}:{sec:06.3f}"


def gain_db_to_percent(gain_db: float) -> float:
    """Convert an amplitude gain in dB to an easy-to-read percentage."""
    return 100.0 * (10.0 ** (gain_db / 20.0))


def trim_filter_with_fades(start: float, end: float, fade_in: float, fade_out: float) -> str:
    filters = [audio.precise_trim_filter(start, end, apply_boundary_fade=False)]
    fades = audio.boundary_fade_filter(end - start, fade_in, fade_out)
    if fades:
        filters.append(fades)
    return ",".join(filters)


def supported_media_paths(paths) -> list[str]:
    """Return existing supported media files while preserving drop order."""
    accepted: list[str] = []
    for value in paths:
        path = Path(value)
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            accepted.append(str(path.resolve()))
    return accepted


STANDARD_QUALITY = "標準（おすすめ）"
QUALITY_PRESETS = {"軽量": "small", STANDARD_QUALITY: "standard", "高音質": "high"}


class ExportActions(ttk.Frame):
    def __init__(self, parent, button_text, command):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.transport = ttk.Frame(self)
        self.transport.grid(row=0, column=0, sticky="w")
        self.output = ttk.Frame(self)
        self.output.grid(row=0, column=1, sticky="e")
        self.quality = tk.StringVar(self, value=STANDARD_QUALITY)
        ttk.Label(self.output, text="圧縮品質").pack(side="left")
        ttk.Combobox(self.output, textvariable=self.quality, values=tuple(QUALITY_PRESETS), state="readonly",
                     width=15).pack(side="left", padx=(4, 6))
        ttk.Button(self.output, text=button_text, command=command, style="Accent.TButton").pack(side="left")
        self.wrapped = False
        self.bind("<Configure>", self._layout)

    def _layout(self, event):
        wrap = event.width < self.transport.winfo_reqwidth() + self.output.winfo_reqwidth()
        if wrap != self.wrapped:
            self.wrapped = wrap
            self.output.grid(row=int(wrap), column=0 if wrap else 1, sticky="e", pady=(4 if wrap else 0, 0))


def choose_audio_output(parent, title: str, stem: str, last_type: tk.StringVar) -> str | None:
    initial = last_type.get()
    selected = tk.StringVar(parent, value=initial)
    raw = filedialog.asksaveasfilename(
        parent=parent, title=title, initialfile=stem, typevariable=selected,
        filetypes=[(fmt.upper(), f"*.{fmt}") for fmt in audio.OUTPUT_ENCODERS],
    )
    if not raw:
        return None
    label = selected.get()
    chosen = label.lower()
    path = Path(raw)
    suffix = path.suffix[1:].lower()
    if suffix not in audio.OUTPUT_ENCODERS:
        path = Path(f"{raw}.{chosen}")
    elif label != initial and chosen != suffix:
        path = path.with_suffix(f".{chosen}")
    # The native overwrite prompt only covers the exact path it returned.
    if path != Path(raw) and path.exists() and not messagebox.askyesno(
        APP_NAME, f"「{path.name}」は既にあります。上書きしますか？", parent=parent
    ):
        return None
    last_type.set(path.suffix[1:].upper())
    return str(path)


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
    RULER_HEIGHT = 48
    GRIP_HEIGHT = 24
    SIDE_PAD = 21
    MIN_VIEW_SECONDS = 0.05

    def __init__(self, parent, on_range_change, **kwargs):
        super().__init__(parent, bg="#111319", highlightthickness=1, highlightbackground="#383d48", **kwargs)
        self.on_range_change = on_range_change
        self.points = None
        self.duration = 0.0
        self.view_start = self.view_span = 0.0
        self.start = 0.0
        self.end = 0.0
        self.playhead = 0.0
        self.seek = Playhead(parent, self.set_playhead)
        self.dragging: str | None = None
        self.press_x = self.grip_x = 0.0
        self.pan_origin = 0.0
        self.moved = False
        self.ruler_click = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self.bind("<MouseWheel>", self._wheel)

    def set_data(self, points, duration: float) -> None:
        self.points = points if isinstance(points, WaveformPeaks) else WaveformPeaks(points, len(points) / duration if duration else 0)
        self.duration = duration
        self.view_start, self.view_span = 0.0, duration
        self.dragging = None
        self.moved = self.ruler_click = self.seek.dragging = False
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

    def set_playhead(self, seconds: float) -> None:
        self.playhead = seconds
        self.delete("playhead")
        self._draw_playhead()

    def _draw_playhead(self) -> None:
        if self.playhead < self.view_start or self.playhead > self._view_end:
            return
        x = self._x_for(self.playhead)
        h = self.winfo_height()
        self.create_line(x, 15, x, max(17, h - self.GRIP_HEIGHT), fill="#ff4f67", width=2, tags="playhead")
        self.create_polygon(x - 8, 3, x + 8, 3, x, 17, fill="#ff4f67", outline="", tags="playhead")
        right = x > self.winfo_width() - 100
        self.create_text(x + (-10 if right else 10), 10, anchor="e" if right else "w",
                         text=format_time(self.playhead), fill="#ff8798", tags="playhead")
        self.tag_raise("trim_grip")

    @property
    def _view_end(self) -> float:
        # Panning clamps start to duration - span. Adding span back need not
        # reproduce duration exactly, especially at high zoom.
        if self.view_start >= self.duration - self.view_span:
            return self.duration
        return self.view_start + self.view_span

    def _x_for(self, seconds: float) -> float:
        width_seconds = self._view_end - self.view_start
        fraction = (seconds - self.view_start) / width_seconds if width_seconds > 0 else 0
        return self.SIDE_PAD + fraction * max(1, self.winfo_width() - 2 * self.SIDE_PAD)

    def _time_for(self, x: float) -> float:
        usable = max(1, self.winfo_width() - 2 * self.SIDE_PAD)
        fraction = max(0.0, min(1.0, (x - self.SIDE_PAD) / usable))
        end = self._view_end
        return end if fraction == 1 else self.view_start + fraction * (end - self.view_start)

    def _wheel(self, event):
        if self.duration <= 0 or self.dragging or self.ruler_click or not event.delta:
            return "break"
        anchor = self._time_for(event.x)
        fraction = (anchor - self.view_start) / self.view_span
        steps = max(-32, min(32, event.delta / 120))
        span = min(self.duration, max(self.MIN_VIEW_SECONDS, self.view_span * 1.5 ** -steps))
        if abs(span - self.duration) < 1e-9:
            span = self.duration
        if span != self.view_span:
            self.view_start = max(0.0, min(self.duration - span, anchor - fraction * span))
            self.view_span = span
            self.redraw()
        return "break"

    def _pan_metrics(self):
        track = max(1, self.winfo_width() - 2 * self.SIDE_PAD)
        thumb = min(track, max(18, track * self.view_span / self.duration))
        return thumb, max(1, track - thumb)

    def _target(self, event):
        if self.duration <= 0:
            return None
        sx, ex, px = map(self._x_for, (self.start, self.end, self.playhead))
        left, right = self.SIDE_PAD, self.winfo_width() - self.SIDE_PAD
        in_ruler = self._in_ruler(event)
        head_hit = left <= px <= right and abs(event.x - px) <= (9 if in_ruler else 7)
        # Separate hit areas even when the playhead and both boundaries coincide.
        if in_ruler:
            return "playhead" if head_hit else None
        if event.y >= self.winfo_height() - self.GRIP_HEIGHT:
            if left <= sx <= right and sx - 20 <= event.x <= sx:
                return "start"
            if left <= ex <= right and ex <= event.x <= ex + 20:
                return "end"
        elif head_hit:
            return "playhead"
        distance, target = min(((abs(event.x - x), handle) for x, handle in ((sx, "start"), (ex, "end"))
                                if left <= x <= right), default=(math.inf, None))
        if distance <= 6:
            return target
        if event.y >= self.winfo_height() - self.GRIP_HEIGHT and self.view_span < self.duration:
            return "pan"
        return None

    def _in_ruler(self, event):
        return self.duration > 0 and 0 <= event.y <= self.RULER_HEIGHT

    def _hover(self, event):
        target = self._target(event)
        self.configure(cursor="hand2" if target == "pan" else "sb_h_double_arrow" if target
                       else "hand2" if self._in_ruler(event) else "")

    def _press(self, event) -> None:
        self.dragging = self._target(event)
        self.moved = False
        self.press_x = event.x
        self.ruler_click = self._in_ruler(event)
        if self.dragging == "pan":
            self.pan_origin = self.view_start
        elif self.dragging:
            self.grip_x = self._x_for(getattr(self, self.dragging))
        if self.ruler_click:
            # Latch the press, even if the pointer drifts outside the ruler
            # before release. Playback ticks must not overwrite this position.
            self.dragging = "playhead"
            self.grip_x = self.press_x
            self.seek.dragging = True
            self.seek.show(self._time_for(self.press_x))

    def _motion(self, event) -> None:
        threshold = RULER_DRAG_PIXELS if self.ruler_click else 3
        if not self.dragging or (not self.moved and abs(event.x - self.press_x) < threshold):
            return
        self.moved = True
        if self.dragging == "pan":
            limit = self.duration - self.view_span
            _, travel = self._pan_metrics()
            start = max(0.0, min(limit, self.pan_origin + (event.x - self.press_x) * limit / travel))
            if start != self.view_start:
                self.view_start = start
                self.redraw()
            return
        value = self._time_for(self.grip_x + event.x - self.press_x)
        if self.dragging == "playhead":
            self.seek.dragging = True
            self.seek.show(value)
            return
        previous = self.start, self.end
        minimum = min(0.05, self.duration)
        if self.dragging == "start":
            self.start = max(0.0, min(value, self.end - minimum))
        elif self.dragging == "end":
            self.end = min(self.duration, max(value, self.start + minimum))
        if previous != (self.start, self.end):
            self.redraw()
            self.on_range_change(self.start, self.end)

    def _release(self, event):
        if self.moved or self.ruler_click:
            self._motion(event)
        if self.ruler_click or (self.moved and self.dragging == "playhead"):
            self.seek.finish_drag()
        self.dragging = None
        self.moved = False
        self.ruler_click = False

    def _draw_ruler(self, width):
        bottom = self.RULER_HEIGHT
        self.create_rectangle(0, 0, width, bottom, fill="#1b1e26", outline="")
        self.create_line(0, bottom, width, bottom, fill="#474e5b")
        if self.duration <= 0:
            return
        # Choose a readable 1/2/5 interval based on seconds per screen pixel.
        target = self.view_span / max(1, (width - 2 * self.SIDE_PAD) // 88)
        unit = 10 ** math.floor(math.log10(target))
        major = next(factor * unit for factor in (1, 2, 5, 10) if factor * unit >= target)
        decimals = max(0, -math.floor(math.log10(major)))
        minor = major / 5
        first = math.ceil(self.view_start / minor - 1e-9)
        last = math.floor(self._view_end / minor + 1e-9)
        for i in range(first, last + 1):
            seconds = i * minor
            x = self._x_for(seconds)
            labelled = i % 5 == 0
            self.create_line(x, bottom - (9 if labelled else 4), x, bottom, fill=MUTED)
            if labelled:
                right = x > width - 48
                self.create_text(x + (-2 if right else 2), bottom - 12,
                                 anchor="se" if right else "sw", text=f"{seconds:.{decimals}f}s",
                                 fill=MUTED, font=("Segoe UI", 9), tags="ruler_label")

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 2 * self.SIDE_PAD or h <= self.RULER_HEIGHT + self.GRIP_HEIGHT:
            return
        top = self.RULER_HEIGHT
        self._draw_ruler(w)
        bottom = h - self.GRIP_HEIGHT
        mid = (bottom + top) / 2
        self.create_rectangle(0, bottom, w, h, fill=BG, outline="")
        self.create_line(0, bottom, w, bottom, fill="#474e5b")
        if 0 < self.view_span < self.duration:
            thumb, travel = self._pan_metrics()
            x = self.SIDE_PAD + travel * self.view_start / (self.duration - self.view_span)
            y = bottom + self.GRIP_HEIGHT / 2
            self.create_line(self.SIDE_PAD, y, w - self.SIDE_PAD, y, fill="#3a404d", width=3)
            self.create_rectangle(x, y - 3, x + thumb, y + 3, fill=MUTED, outline="", tags="pan_thumb")
        self.create_line(self.SIDE_PAD, mid, w - self.SIDE_PAD, mid, fill="#3a404d")
        if not self.points:
            self.create_text(w / 2, mid, text="動画または音声ファイルを読み込んでください", fill=MUTED)
            self._draw_playhead()
            return
        usable = w - 2 * self.SIDE_PAD
        for px, peak in enumerate(self.points.pixels(self.view_start, self.view_span, usable)):
            amp = peak * ((bottom - top) * 0.39)
            self.create_line(self.SIDE_PAD + px, mid - amp, self.SIDE_PAD + px, mid + amp, fill=ACCENT_2)
        sx, ex = self._x_for(self.start), self._x_for(self.end)
        self.create_rectangle(0, top + 1, max(0, min(w, sx)), bottom, fill="#232630", outline="")
        self.create_rectangle(max(0, min(w, ex)), top + 1, w, bottom, fill="#232630", outline="")
        for x, direction, color in ((sx, -1, ACCENT), (ex, 1, WARNING)):
            if not self.SIDE_PAD <= x <= w - self.SIDE_PAD:
                continue
            self.create_line(x, top, x, bottom, fill=color, width=3)
            self.create_polygon(x, bottom - 3, x + direction * 18, bottom + 9,
                                x + direction * 18, h - 3, x, h - 3,
                                fill=color, outline="", tags="trim_grip")
        self.create_text(16, bottom - 12, anchor="w", text=format_time(self.start), fill=TEXT)
        self.create_text(w - 16, bottom - 12, anchor="e", text=format_time(self.end), fill=TEXT)
        self._draw_playhead()


class Playhead:
    def __init__(self, tab, draw_playhead):
        self.tab = tab
        self.draw_playhead = draw_playhead
        self.start = self.end = 0.0
        self.dragging = False
        self.looping = False
        self.position = 0.0

    def bounds(self, start, end):
        if (self.start, self.end) != (start, end):
            self.start, self.end = start, end
            position = self.position
            self.show(position if start <= position <= end else start)

    def show(self, position):
        value = max(self.start, min(position, self.end))
        self.position = value
        self.draw_playhead(value)

    def finish_drag(self):
        self.dragging = False
        self.tab.app.seek(self)


def loop_preview_graph(preview_file: Path, offset: float) -> str:
    # Only app-generated ASCII basenames are passed to lavfi. Use cwd for the
    # directory so spaces, drive letters and special chars never enter the graph.
    source = f"amovie=filename={preview_file.name}"
    repeat = f"{source}:loop=0,asetpts=N/SR/TB"
    if offset <= 0:
        return repeat
    return (f"{source}:sp={offset:.9f},asetpts=N/SR/TB[tail];"
            f"{repeat}[cycles];[tail][cycles]concat=n=2:v=0:a=1")


class TrimTab(ttk.Frame):
    def __init__(self, parent, app: "AudioAtelierApp") -> None:
        super().__init__(parent, padding=18)
        self.app = app
        self.path = ""
        self.duration = 0.0
        self.start_var = tk.StringVar(value="0.000")
        self.end_var = tk.StringVar(value="0.000")
        self.fade_in_var = tk.StringVar(value=f"{AUTO_FADE_SECONDS:.3f}")
        self.fade_out_var = tk.StringVar(value=f"{AUTO_FADE_SECONDS:.3f}")
        self.status = tk.StringVar(value="動画または音声を選択するか、画面へドラッグ＆ドロップしてください")
        self.last_export_type = tk.StringVar(value="WAV")
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Button(top, text="動画・音声を選択", command=self.choose_file, style="Accent.TButton").pack(side="left")
        self.file_label = ttk.Label(top, text="未選択（画面へドラッグ＆ドロップ可）", foreground=MUTED)
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)

        ttk.Label(self, text="ホイールで拡大・縮小。下のバーで表示を移動、青・オレンジで範囲を調整。上の目盛りで再生位置を指定します。", foreground=MUTED).pack(anchor="w", pady=(18, 7))
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

        fades = ttk.Frame(self)
        fades.pack(fill="x", pady=(0, 14))
        ttk.Label(fades, text="フェードイン（秒）").pack(side="left")
        ttk.Entry(fades, textvariable=self.fade_in_var, width=9).pack(side="left", padx=(7, 18))
        ttk.Label(fades, text="フェードアウト（秒）").pack(side="left")
        ttk.Entry(fades, textvariable=self.fade_out_var, width=9).pack(side="left", padx=(7, 14))
        ttk.Label(fades, text="0で無効。初期値の0.010秒は境界ノイズを抑える設定です。", foreground=MUTED).pack(side="left")

        self.seek = self.wave.seek
        bar = ExportActions(self, "選択範囲を書き出す", self.export)
        bar.pack(fill="x")
        actions, self.export_quality = bar.transport, bar.quality
        ttk.Button(actions, text="▶ 切り出し範囲を視聴", command=self.preview).pack(side="left")
        ttk.Button(actions, text="↻ ループ再生", command=lambda: self.preview(True)).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="繋ぎ目を確認", command=lambda: self.preview(True, seam=True)).pack(side="left", padx=(4, 0))
        ttk.Button(actions, text="一時停止", command=lambda: self.app.pause_preview(self.seek)).pack(side="left", padx=(4, 0))
        ttk.Button(actions, text="■ 停止", command=self.stop_preview).pack(side="left", padx=8)
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
        self.load_file(path)

    def load_file(self, path: str) -> None:
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
        self.seek.show(0.0)
        self.status.set(f"読み込み完了　全体 {format_time(self.duration)}")

    def _load_failed(self, exc: Exception) -> None:
        self.status.set("読み込みに失敗しました")
        messagebox.showerror(APP_NAME, str(exc))

    def _range_changed(self, start: float, end: float) -> None:
        if self.app.active_bar is self.seek:
            self.app.stop_playback()
        self.seek.bounds(start, end)
        self.start_var.set(f"{start:.3f}")
        self.end_var.set(f"{end:.3f}")
        if self.duration:
            self.status.set(f"選択範囲 {format_time(start)} ～ {format_time(end)}（{end-start:.3f} 秒）")

    def apply_entries(self) -> bool:
        try:
            start, end = float(self.start_var.get()), float(self.end_var.get())
            if not (0 <= start < end <= self.duration + 0.001) or start >= self.duration:
                raise ValueError
            if (start, end) != (round(self.wave.start, 3), round(self.wave.end, 3)):
                self.wave.set_range(start, end, notify=True)
            return True
        except ValueError:
            messagebox.showwarning(APP_NAME, "開始・終了秒を正しく入力してください。")
            return False

    def fade_values(self) -> tuple[float, float] | None:
        try:
            fade_in = float(self.fade_in_var.get())
            fade_out = float(self.fade_out_var.get())
            if not all(math.isfinite(value) and value >= 0 for value in (fade_in, fade_out)):
                raise ValueError
            return fade_in, fade_out
        except ValueError:
            messagebox.showwarning(APP_NAME, "フェードイン・アウトは0以上の秒数で入力してください。")
            return None

    def stop_preview(self) -> None:
        self.app.stop_playback()
        self.seek.show(self.seek.start)
        self.status.set("試聴を停止しました")

    def preview(self, loop=False, *, seam=False) -> None:
        if not self.path or self.duration <= 0:
            messagebox.showinfo(APP_NAME, "先にファイルを読み込んでください。")
            return
        if not self.apply_entries():
            return
        fade_values = self.fade_values()
        if fade_values is None:
            return
        start, end = self.wave.start, self.wave.end
        fade_in, fade_out = fade_values
        duration = end - start
        source = self.path
        out = str(TEMP_DIR / f"trim_preview_{uuid.uuid4().hex}.wav")
        restart = self.seek.looping
        self.app.stop_playback()
        self.app.active_bar = self.seek
        self.seek.looping = loop
        if seam:
            self.seek.show(max(start, end - 3.0))
        elif restart or self.seek.position >= end:
            self.seek.show(start)
        request = self.app.playback_serial
        self.status.set("選択範囲を正確に切り出しています…")

        def work():
            playback_duration = max(duration, MIN_TRIM_PREVIEW_SECONDS)
            preview_filter = trim_filter_with_fades(start, end, fade_in, fade_out)
            if not loop and playback_duration > duration:
                preview_filter += f",apad=pad_dur={playback_duration-duration:.6f}"
            run_ffmpeg([
                "-i", source, "-map", "0:a:0", "-vn",
                "-af", preview_filter,
                "-c:a", "pcm_s16le", out,
            ])
            return request, out, loop

        def failed(exc: Exception) -> None:
            Path(out).unlink(missing_ok=True)
            if request == self.app.playback_serial:
                self.app.stop_playback()
                self.status.set("試聴の準備に失敗しました")
                messagebox.showerror(APP_NAME, str(exc))

        self.app.jobs.submit(work, self._preview_ready, failed)

    def _preview_ready(self, result) -> None:
        request, path, loop = result
        if request != self.app.playback_serial:
            Path(path).unlink(missing_ok=True)
            return

        self.app.play(path, self.seek, loop)

    def export(self) -> None:
        if not self.path or self.duration <= 0:
            messagebox.showinfo(APP_NAME, "先にファイルを読み込んでください。")
            return
        if not self.apply_entries():
            return
        fade_values = self.fade_values()
        if fade_values is None:
            return
        out = choose_audio_output(self, "音声を書き出す", f"{Path(self.path).stem}_trim", self.last_export_type)
        if not out:
            return
        quality = QUALITY_PRESETS[self.export_quality.get()]
        start, end = self.wave.start, self.wave.end
        fade_in, fade_out = fade_values
        self.status.set("音声を書き出しています…")

        def work():
            run_ffmpeg([
                "-i", self.path, "-map", "0:a:0", "-vn",
                "-af", trim_filter_with_fades(start, end, fade_in, fade_out),
                *audio.audio_args_for(out, quality), out,
            ])
            return out

        self.app.jobs.submit(work, lambda p: self._exported(p), lambda e: self._export_failed(e))

    def _exported(self, path: str) -> None:
        self.status.set(f"保存しました: {path}")
        messagebox.showinfo(APP_NAME, f"音声を保存しました。\n{path}")

    def _export_failed(self, exc: Exception) -> None:
        self.status.set("書き出しに失敗しました")
        messagebox.showerror(APP_NAME, str(exc))


def free_clip_start(clips, moving, lane, requested, snap=0.0):
    """Project the requested start onto the nearest gap which fits the whole clip."""
    requested = max(0.0, requested)
    occupied = sorted((c.start, math.inf if c.loop else c.start + c.duration)
                      for c in clips if c is not moving and c.lane == lane)
    candidates = []
    left = 0.0
    for begin, end in occupied:
        right = begin - moving.duration
        if not moving.loop and right >= left - 1e-9:
            candidates.append((left, max(left, right)))
        left = max(left, end)
    if math.isfinite(left):
        candidates.append((left, math.inf))
    if not candidates:
        return None
    def closest(bounds):
        lo, hi = bounds
        return min(hi, max(lo, requested))
    bounds = min(candidates, key=lambda b: (abs(closest(b) - requested), abs(closest(b) - moving.start)))
    start = closest(bounds)
    edge = min(bounds, key=lambda t: abs(t - start))
    return edge if abs(edge - start) <= snap else start


class TimelineCanvas(tk.Canvas):
    TOP = 34
    LANE_H = 66

    def __init__(self, parent, tab, on_select, on_move, **kwargs):
        super().__init__(parent, bg="#111319", highlightthickness=1, highlightbackground="#383d48", **kwargs)
        self.on_select = on_select
        self.on_move = on_move
        self.clips: list[AudioClip] = []
        self.selected: str | None = None
        self.pixels_per_second = 30.0
        self.drag_clip: AudioClip | None = None
        self.drag_offset_x = 0.0
        self.playhead = 0.0
        self.seek = Playhead(tab, self.set_playhead)
        self.seek_grab = None
        self._seek_moved = False
        self.timeline_width = 1.0
        self.hold_zoom_view = False
        self.label_font = tkfont.nametofont("TkDefaultFont")
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<MouseWheel>", self._wheel)

    def _extent(self):
        return max([c.start + c.duration for c in self.clips] + [60.0, self.seek.tab.project_duration or 0])

    def _time_at(self, screen_x):
        return (self.canvasx(screen_x) - 70) / self.pixels_per_second

    def _wheel(self, event):
        if not event.delta or self.drag_clip is not None or self.seek_grab is not None:
            return "break"
        anchor = self._time_at(event.x)
        plot_width = max(1, self.winfo_width() - 120)
        extent = self._extent()
        fit = plot_width / extent
        # Keep Tk's pixel coordinates representable even for very long projects.
        maximum = max(fit, min(plot_width / WaveformCanvas.MIN_VIEW_SECONDS, 1e8 / extent))
        factor = 1.5 ** max(-32, min(32, event.delta / 120))
        scale = max(fit, min(maximum, self.pixels_per_second * factor))
        if scale == self.pixels_per_second:
            return "break"
        self.pixels_per_second = scale
        self.redraw()
        left = 70 + anchor * scale - event.x
        self.xview_moveto((left + self.winfo_pixels(self.cget("highlightthickness"))) / self.timeline_width)
        self.hold_zoom_view = self.seek.tab.app.active_bar is self.seek
        return "break"

    def draw_ticks(self):
        # Only draw the visible ruler; zooming should not allocate one item
        # per tick for the entire project. Called on horizontal scrolling too.
        self.delete("ruler_tick")
        pps = self.pixels_per_second
        target = 76 / pps
        unit = 10 ** math.floor(math.log10(target))
        step = next(k * unit for k in (1, 2, 5, 10) if k * unit >= target)
        first = max(0, math.floor(self._time_at(0) / step))
        last = math.ceil(min(self._extent(), self._time_at(self.winfo_width())) / step)
        digits = max(0, -math.floor(math.log10(step)))
        for index in range(first, last + 1):
            t = index * step
            x = 70 + t * pps
            self.create_line(x, 18, x, self.TOP, fill="#2c303a", tags="ruler_tick")
            self.create_text(x + 3, 8, anchor="nw", text=f"{t:.{digits}f}s", fill=MUTED,
                             font=("Segoe UI", 8), tags="ruler_tick")
        self.tag_raise("playhead")

    def set_clips(self, clips: list[AudioClip]) -> None:
        self.clips = clips
        if self.selected and not any(c.id == self.selected for c in clips):
            self.selected = None
        self.redraw()

    def set_playhead(self, seconds: float) -> None:
        self.playhead = seconds
        self.delete("playhead")
        self._draw_playhead()
        if self.seek.dragging:
            return
        x = 70 + seconds * self.pixels_per_second
        left = self.canvasx(0)
        right = self.canvasx(self.winfo_width())
        if self.hold_zoom_view and self.seek.tab.app.active_bar is self.seek:
            # A playback tick must not immediately scroll away from the zoom anchor.
            if not left + 30 <= x <= right - 50:
                return
        self.hold_zoom_view = False
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
        max_end = self._extent()
        width = max(self.winfo_width(), 120 + max_end * self.pixels_per_second)
        self.timeline_width = width
        lanes = max([c.lane for c in self.clips] + [3]) + 1
        height = max(self.winfo_height(), self.TOP + lanes * self.LANE_H)
        self.configure(scrollregion=(0, 0, width, height))

        self.draw_ticks()
        for lane in range(lanes):
            y = self.TOP + lane * self.LANE_H
            self.create_rectangle(0, y, width, y + self.LANE_H, fill="#161920" if lane % 2 == 0 else "#1a1d25", outline="")
            self.create_text(8, y + self.LANE_H / 2, anchor="w", text=f"{lane+1}", fill=MUTED)
            self.create_line(0, y, width, y, fill="#303541")

        for clip in self.clips:
            x1 = 70 + clip.start * self.pixels_per_second
            x2 = x1 + clip.duration * self.pixels_per_second
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
            space = x2 - x1 - 12
            if space > 0 and self.label_font.measure(label) > space:
                lo, hi = 0, len(label)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if self.label_font.measure(label[:mid] + "…") <= space:
                        lo = mid
                    else:
                        hi = mid - 1
                label = label[:lo] + "…" if lo else ""
            if space > 0 and label:
                self.create_text(x1 + 8, (y1 + y2) / 2, anchor="w", text=label, font=self.label_font, fill="white", tags=("clip", clip.id))
        self._draw_playhead()
        self.event_generate("<<TimelineRedrawn>>", when="tail")

    def _clip_at(self, x: float, y: float) -> AudioClip | None:
        nearby = None
        for clip in reversed(self.clips):
            x1 = 70 + clip.start * self.pixels_per_second
            x2 = x1 + clip.duration * self.pixels_per_second
            y1 = self.TOP + clip.lane * self.LANE_H + 7
            y2 = y1 + self.LANE_H - 14
            if y1 <= y <= y2:
                if x1 <= x < x2:
                    return clip
                if nearby is None and x1 <= x <= x1 + max(8, x2 - x1):
                    nearby = clip
        return nearby

    def _press(self, event) -> None:
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        if 0 <= y <= self.TOP and self.seek.end > 0:
            self.seek_grab = (event.x, 0)
            self.seek.dragging = True
            self.seek.show(self._time_at(event.x))
            return
        clip = self._clip_at(x, y)
        head_x = 70 + self.playhead * self.pixels_per_second
        if not clip and self.seek.end > 0 and abs(x - head_x) <= 8:
            self.seek_grab = (event.x, head_x - x)
            return
        if clip:
            self.selected = clip.id
            self.drag_clip = clip
            self.drag_offset_x = x - (70 + clip.start * self.pixels_per_second)
            self.on_select(clip)
            self.redraw()

    def _motion(self, event) -> None:
        if self.seek_grab is not None:
            press_x, offset = self.seek_grab
            if self._seek_moved or abs(event.x - press_x) >= RULER_DRAG_PIXELS:
                self._seek_moved = True
                self.seek.dragging = True
                self.seek.show((self.canvasx(event.x) + offset - 70) / self.pixels_per_second)
            return
        if not self.drag_clip:
            return
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        raw_start = (x - self.drag_offset_x - 70) / self.pixels_per_second
        lane = max(0, min(15, int((y - self.TOP) // self.LANE_H)))
        self.on_move(self.drag_clip, round(raw_start * 10) / 10, lane,
                     snap=min(.1, 4 / self.pixels_per_second))
        self.redraw()

    def _release(self, event) -> None:
        if self.seek.dragging:
            self._motion(event)
            self.seek.finish_drag()
        self.seek_grab = None
        self._seek_moved = False
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
        self.last_export_type = tk.StringVar(value="WAV")
        self.status = tk.StringVar(value="音声・動画を追加するか、画面へドラッグ＆ドロップしてください")
        self.project_path: str | None = None
        self.project_duration: float | None = None
        self.sample_rate = 48000
        self.channels = 2
        self.loudness_lufs: float | None = None
        self.true_peak_db = -1.0
        self._build()

    def _build(self) -> None:
        panel = ttk.Frame(self)
        panel.pack(fill="x")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        toolbar = ttk.Frame(panel)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(toolbar, text="＋ 音声・動画を追加", command=self.add_files, style="Accent.TButton").pack(side="left")
        ttk.Button(toolbar, text="選択を削除", command=self.delete_selected).pack(side="left", padx=8)
        ttk.Button(toolbar, text="すべて消去", command=self.clear_all).pack(side="left")
        ttk.Button(toolbar, text="JSONを開く", command=self.open_project).pack(side="left", padx=(16, 8))
        ttk.Button(toolbar, text="JSON保存", command=self.save_project).pack(side="left")

        ttk.Label(panel, text="ホイールで拡大・縮小。上の目盛りをクリックし、そのままドラッグして再生位置を指定します。", foreground=MUTED).grid(row=1, column=0, sticky="w", pady=(14, 7))
        holder = ttk.Frame(panel)
        holder.grid(row=2, column=0, sticky="nsew")
        # 秒数目盛りと4レーンがちょうど収まる高さ。5レーン目以降は縦スクロールで表示する。
        timeline_height = TimelineCanvas.TOP + TimelineCanvas.LANE_H * 4 + 2
        self.timeline = TimelineCanvas(holder, self, self.select_clip, self.clip_moved, height=timeline_height)
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=self.timeline.xview)
        ybar = ttk.Scrollbar(holder, orient="vertical", command=self.timeline.yview)
        def scrolled(first, last):
            xbar.set(first, last)
            self.timeline.draw_ticks()
        self.timeline.configure(xscrollcommand=scrolled, yscrollcommand=ybar.set)
        self.timeline.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        arrange = ttk.Frame(panel)
        arrange.grid(row=3, column=0, sticky="ew", pady=(12, 8))
        ttk.Label(arrange, text="クリップ間の無音（秒）").pack(side="left")
        ttk.Entry(arrange, textvariable=self.gap_var, width=8).pack(side="left", padx=7)
        ttk.Button(arrange, text="全て順番に並べる", command=self.arrange_sequential).pack(side="left", padx=(4, 8))
        ttk.Button(arrange, text="全て先頭で重ねる", command=self.arrange_overlay).pack(side="left")

        selected = ttk.LabelFrame(panel, text="選択中のクリップ", padding=9)
        selected.grid(row=4, column=0, sticky="ew")
        self.selected_label = ttk.Label(selected, text="未選択", foreground=MUTED)
        self.selected_label.pack(side="left", fill="x", expand=True)
        ttk.Label(selected, text="開始（秒）").pack(side="left")
        ttk.Entry(selected, textvariable=self.start_var, width=10).pack(side="left", padx=6)
        ttk.Label(selected, text="レーン").pack(side="left", padx=(8, 0))
        ttk.Entry(selected, textvariable=self.lane_var, width=5).pack(side="left", padx=6)
        ttk.Button(selected, text="反映", command=self.apply_selected_values).pack(side="left")
        ttk.Button(selected, text="詳細設定", command=self.open_clip_details).pack(side="left", padx=(8, 0))

        self.seek = self.timeline.seek
        self.timeline.bind("<<TimelineRedrawn>>", self._sync_seek_bounds)
        bar = ExportActions(panel, "合成音声を書き出す", self.export)
        bar.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        actions, self.export_quality = bar.transport, bar.quality
        ttk.Button(actions, text="▶ 全体を試聴", command=self.preview).pack(side="left")
        ttk.Button(actions, text="↻ ループ再生", command=lambda: self.preview(True)).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="一時停止", command=lambda: self.app.pause_preview(self.seek)).pack(side="left", padx=(4, 0))
        ttk.Button(actions, text="■ 停止", command=self.stop_preview).pack(side="left", padx=8)
        ttk.Label(panel, textvariable=self.status, foreground=MUTED).grid(row=7, column=0, sticky="w", pady=(10, 0))

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="音声・動画ファイルを追加",
            filetypes=[
                ("動画・音声", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.mpeg *.mpg *.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma *.opus"),
                ("音声", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma *.opus"),
                ("動画", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.mpeg *.mpg"),
                ("すべて", "*.*"),
            ],
        )
        if not paths:
            return
        self.add_paths(paths)

    def add_paths(self, paths) -> None:
        paths = list(paths)
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

    def clip_moved(self, clip: AudioClip, requested: float, lane: int, snap: float = 0.0) -> None:
        start = free_clip_start(self.clips, clip, lane, requested, snap)
        if start is not None:
            clip.start, clip.lane = start, lane
        if self.selected and self.selected.id == clip.id:
            self.start_var.set(f"{clip.start:.3f}")
            self.lane_var.set(str(clip.lane + 1))
        self.status.set(f"全体 {format_time(self.total_duration())}" if start is not None
                        else "このレーンにはクリップを置ける空きがありません")

    def apply_selected_values(self) -> None:
        if not self.selected:
            return
        try:
            start = float(self.start_var.get())
            if not math.isfinite(start):
                raise ValueError
            if self.start_var.get() == f"{self.selected.start:.3f}":
                start = self.selected.start
            lane = max(0, min(15, int(self.lane_var.get()) - 1))
        except ValueError:
            messagebox.showwarning(APP_NAME, "開始秒とレーンを正しく入力してください。")
            return
        self.clip_moved(self.selected, start, lane)
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

    def _sync_seek_bounds(self, _event):
        end = self.total_duration() if self.clips else 0.0
        if self.seek.end != end and self.app.active_bar is self.seek:
            self.app.stop_playback()
        self.seek.bounds(0, end)

    def _render_mix(self, out: str, quality: str = "standard") -> None:
        project = audio.AudioProject(
            audio.PROJECT_VERSION, self.sample_rate, self.channels, self.clips,
            self.total_duration(), self.loudness_lufs, self.true_peak_db, self.project_path,
        )
        command, _filters, _duration = audio.build_mix_command(project, out, overwrite=True, quality=quality)
        audio.run_command(command)

    def preview(self, loop=False) -> None:
        if not self.clips:
            messagebox.showinfo(APP_NAME, "先に音声ファイルを追加してください。")
            return
        duration = self.total_duration()
        out = str(TEMP_DIR / f"mix_preview_{uuid.uuid4().hex}.wav")
        restart = self.seek.looping
        self.app.stop_playback()
        self.app.active_bar = self.seek
        self.seek.bounds(0, duration)
        if restart or self.seek.position >= duration:
            self.seek.show(0)
        self.seek.looping = loop
        request = self.app.playback_serial
        self.status.set("試聴用の音声を作成しています…")

        def work():
            self._render_mix(out)
            return request, out, loop

        def failed(exc: Exception) -> None:
            Path(out).unlink(missing_ok=True)
            if request == self.app.playback_serial:
                self.app.stop_playback()
                self._export_failed(exc)

        self.app.jobs.submit(work, self._preview_ready, failed)

    def stop_preview(self) -> None:
        self.app.stop_playback()
        self.seek.show(0.0)
        self.status.set("試聴を停止しました")

    def _preview_ready(self, result) -> None:
        request, path, loop = result
        if request != self.app.playback_serial:
            Path(path).unlink(missing_ok=True)
            return

        self.app.play(path, self.seek, loop)

    def export(self) -> None:
        if not self.clips:
            messagebox.showinfo(APP_NAME, "先に音声ファイルを追加してください。")
            return
        out = choose_audio_output(self, "合成音声を書き出す", "mix", self.last_export_type)
        if not out:
            return
        quality = QUALITY_PRESETS[self.export_quality.get()]
        self.status.set("合成音声を書き出しています…")
        self.app.jobs.submit(lambda: (self._render_mix(out, quality), out)[1], self._exported, self._export_failed)

    def _exported(self, path: str) -> None:
        self.status.set(f"保存しました: {path}")
        messagebox.showinfo(APP_NAME, f"合成音声を保存しました。\n{path}")

    def _export_failed(self, exc: Exception) -> None:
        self.status.set("処理に失敗しました")
        messagebox.showerror(APP_NAME, str(exc))


class AudioAtelierApp:
    def __init__(self) -> None:
        ensure_data_dirs()
        self.root = TkinterDnD.Tk()
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
        self.active_bar = None
        self.play_started_at = 0.0
        self.play_origin = 0.0
        self.play_cleanup_path: str | None = None
        self.jobs = BackgroundJobs(self.root)
        self._style()
        self._build()
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._drop_event)
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
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.trim_tab = TrimTab(self.notebook, self)
        self.mix_tab = MixTab(self.notebook, self)
        self.notebook.add(self.trim_tab, text="  音声を切り出す  ")
        self.notebook.add(self.mix_tab, text="  音声を合成する  ")

    def _files_dropped(self, paths) -> None:
        accepted = supported_media_paths(paths)
        if not accepted:
            messagebox.showwarning(
                APP_NAME,
                "対応している動画・音声ファイルが見つかりませんでした。",
                parent=self.root,
            )
            return
        if self.notebook.select() == str(self.trim_tab):
            self.trim_tab.load_file(accepted[0])
            if len(accepted) > 1:
                self.trim_tab.status.set(f"複数ファイルのうち先頭を読み込み中: {Path(accepted[0]).name}")
        else:
            self.mix_tab.add_paths(accepted)

    def _drop_event(self, event) -> None:
        self._files_dropped(self.root.tk.splitlist(event.data))

    def play(self, path, bar, loop):
        self.play_cleanup_path = path
        self.active_bar = bar
        bar.looping = loop
        if bar.position >= bar.end:
            if not loop:
                self.stop_playback()
                return
            bar.show(bar.start)
        bar.tab.status.set("ループ再生中" if loop else "試聴中")
        self._start_player(bar.position - bar.start)

    def _start_player(self, offset):
        args = [FFPLAY, "-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit"]
        path = Path(self.play_cleanup_path)
        if self.active_bar.looping:
            # Feed each cycle into the same audio device without an EOF/relaunch gap.
            args += ["-f", "lavfi", "-i", loop_preview_graph(path, offset)]
        else:
            if offset > 0:
                args += ["-ss", f"{offset:.6f}"]
            args.append(str(path))
        self.playback_serial += 1
        try:
            self.player = subprocess.Popen(args, cwd=path.parent, creationflags=creation_flags())
        except OSError as exc:
            self.stop_playback()
            messagebox.showerror(APP_NAME, f"再生を開始できませんでした。\n{exc}")
            return
        self.play_origin = offset
        self.play_started_at = time.monotonic()
        serial = self.playback_serial
        self.root.after(40, lambda: self._update_playback(serial))

    def seek(self, bar):
        if self.active_bar is not bar or self.player is None:
            return
        self._stop_player()
        if bar.position >= bar.end:
            if not bar.looping:
                self.stop_playback()
                return
            bar.show(bar.start)
        self._start_player(bar.position - bar.start)

    def _update_playback(self, serial):
        if serial != self.playback_serial or self.player is None:
            return
        bar = self.active_bar
        result = self.player.poll()
        if result is not None and not bar.dragging:
            self.stop_playback()
            bar.show(bar.start)
            bar.tab.status.set("試聴終了" if result == 0 else "試聴に失敗しました")
            return
        if not bar.dragging:
            bar.show(self._play_position(bar))
        self.root.after(40, lambda: self._update_playback(serial))

    def _play_position(self, bar):
        elapsed = self.play_origin + time.monotonic() - self.play_started_at
        if bar.looping and bar.end > bar.start:
            elapsed %= bar.end - bar.start
        return bar.start + elapsed

    def pause_preview(self, bar):
        if self.active_bar is not bar:
            return
        position = self._play_position(bar) if self.player and not bar.dragging else bar.position
        self.stop_playback()
        bar.show(position)
        bar.tab.status.set(f"一時停止　{format_time(bar.position)}")

    def _stop_player(self):
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

    def stop_playback(self) -> None:
        self._stop_player()
        if self.active_bar:
            self.active_bar.tab.status.set("試聴を停止しました")
        self.active_bar = None
        for tab in (self.trim_tab, self.mix_tab):
            tab.seek.looping = False
        self._cleanup_playback_file(self.play_cleanup_path)
        self.play_cleanup_path = None

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


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli_main(sys.argv[1:]))
    AudioAtelierApp().run()
