from __future__ import annotations

import array
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "Audio Atelier"
APP_VERSION = "v1.1"
AUTO_FADE_SECONDS = 0.010
MIX_LIMITER_CEILING = 0.95
BG = "#17191f"
PANEL = "#22252d"
PANEL_2 = "#2b2f39"
TEXT = "#f1f3f5"
MUTED = "#a7adb8"
ACCENT = "#58a6ff"
ACCENT_2 = "#63d8c6"
WARNING = "#ffb454"
CLIP_COLORS = ["#3778c2", "#9472c9", "#c0628c", "#458f79", "#b27843", "#6676c8"]


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = base_dir()
DATA_DIR = BASE_DIR / "app_data"
TEMP_DIR = DATA_DIR / "temp"
DATA_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)


def resource_path(relative: str) -> Path:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir / relative


def find_tool(name: str) -> str | None:
    """実行中のEXEを基準に指定順でFFmpeg関連コマンドを探す。"""
    exe_name = f"{name}.exe" if os.name == "nt" else name
    candidates = (
        BASE_DIR / exe_name,
        BASE_DIR / "bin" / exe_name,
        BASE_DIR / "ffmpeg" / exe_name,
        BASE_DIR / "ffmpeg" / "bin" / exe_name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def tool_path(name: str) -> str:
    return find_tool(name) or name


FFMPEG = tool_path("ffmpeg")
FFPROBE = tool_path("ffprobe")
FFPLAY = tool_path("ffplay")


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def probe_duration(path: str) -> float:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags(),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "再生時間を取得できませんでした。")
    data = json.loads(proc.stdout)
    return max(0.0, float(data["format"]["duration"]))


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


def audio_args_for(path: str) -> list[str]:
    ext = Path(path).suffix.lower()
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if ext == ".m4a":
        return ["-c:a", "aac", "-b:a", "256k"]
    raise ValueError("対応していない出力形式です。")


def boundary_fade_filter(duration: float) -> str:
    """クリップ端のクリックノイズを抑える短い自動フェードを返す。"""
    duration = max(0.0, float(duration))
    fade = min(AUTO_FADE_SECONDS, duration / 2.0)
    fade_out_start = max(0.0, duration - fade)
    return (
        f"afade=t=in:st=0:d={fade:.6f}:curve=tri,"
        f"afade=t=out:st={fade_out_start:.6f}:d={fade:.6f}:curve=tri"
    )


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-y", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags(),
    )
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        raise RuntimeError(lines[-1] if lines else "FFmpegの処理に失敗しました。")


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
        ttk.Button(actions, text="■ 停止", command=self.app.stop_playback).pack(side="left", padx=8)
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

    def preview(self) -> None:
        if not self.path or self.duration <= 0:
            messagebox.showinfo(APP_NAME, "先にファイルを読み込んでください。")
            return
        if not self.apply_entries():
            return
        self.app.play(
            self.path,
            float(self.start_var.get()),
            float(self.end_var.get()) - float(self.start_var.get()),
            self.wave.set_playhead,
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
            duration = end - start
            run_ffmpeg([
                "-ss", f"{start:.6f}", "-i", self.path, "-t", f"{duration:.6f}",
                "-map", "0:a:0", "-vn", "-af", boundary_fade_filter(duration),
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


@dataclass
class AudioClip:
    id: str
    path: str
    name: str
    start: float
    duration: float
    lane: int
    color: str


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
            label = f"{clip.name}  {clip.duration:.2f}s"
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
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="＋ 音声を追加", command=self.add_files, style="Accent.TButton").pack(side="left")
        ttk.Button(toolbar, text="選択を削除", command=self.delete_selected).pack(side="left", padx=8)
        ttk.Button(toolbar, text="すべて消去", command=self.clear_all).pack(side="left")
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

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="▶ 全体を試聴", command=self.preview).pack(side="left")
        ttk.Button(actions, text="■ 停止", command=self.app.stop_playback).pack(side="left", padx=8)
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
        return max([c.start + c.duration for c in self.clips] + [0.0])

    def _render_mix(self, out: str) -> None:
        inputs: list[str] = []
        filters: list[str] = []
        labels: list[str] = []
        for i, clip in enumerate(self.clips):
            inputs += ["-i", clip.path]
            delay = max(0, round(clip.start * 1000))
            label = f"a{i}"
            filters.append(
                f"[{i}:a]atrim=0:{clip.duration:.6f},asetpts=PTS-STARTPTS,"
                f"{boundary_fade_filter(clip.duration)},adelay={delay}:all=1[{label}]"
            )
            labels.append(f"[{label}]")
        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            f"dropout_transition=0:normalize=0,alimiter=limit={MIX_LIMITER_CEILING}:"
            "level=0:latency=1[outa]"
        )
        run_ffmpeg([*inputs, "-filter_complex", ";".join(filters), "-map", "[outa]", *audio_args_for(out), out])

    def preview(self) -> None:
        if not self.clips:
            messagebox.showinfo(APP_NAME, "先に音声ファイルを追加してください。")
            return
        out = str(TEMP_DIR / "mix_preview.wav")
        self.status.set("試聴用の音声を作成しています…")
        self.app.jobs.submit(lambda: (self._render_mix(out), out)[1], self._preview_ready, lambda e: self._export_failed(e))

    def _preview_ready(self, path: str) -> None:
        self.status.set("合成結果を再生しています")
        self.app.play(path, duration=self.total_duration(), progress_callback=self.timeline.set_playhead)

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

    def play(self, path: str, start: float | None = None, duration: float | None = None, progress_callback=None) -> None:
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
            if self.progress_callback:
                self.progress_callback(self.play_origin)
            self.root.after(40, lambda: self._update_playback(serial))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"再生を開始できませんでした。\n{exc}")

    def _update_playback(self, serial: int) -> None:
        if serial != self.playback_serial or not self.player:
            return
        if self.player.poll() is not None:
            callback = self.progress_callback
            self.player = None
            self.progress_callback = None
            if callback:
                callback(None)
            return
        elapsed = time.monotonic() - self.play_started_at
        if self.play_duration is not None:
            elapsed = min(elapsed, self.play_duration)
        if self.progress_callback:
            self.progress_callback(self.play_origin + elapsed)
        self.root.after(40, lambda: self._update_playback(serial))

    def stop_playback(self) -> None:
        self.playback_serial += 1
        if self.player and self.player.poll() is None:
            self.player.terminate()
        self.player = None
        callback = self.progress_callback
        self.progress_callback = None
        if callback:
            callback(None)

    def close(self) -> None:
        self.stop_playback()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    AudioAtelierApp().run()
