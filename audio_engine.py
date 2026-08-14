from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AUTO_FADE_SECONDS = 0.010
MIX_LIMITER_CEILING = 0.95
PROJECT_VERSION = 1


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = base_dir()


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


def ensure_tool(name: str) -> str:
    found = find_tool(name)
    if not found:
        raise RuntimeError(f"{name}が見つかりません。READMEの手順に従ってFFmpegを配置してください。")
    return found


def audio_args_for(path: str) -> list[str]:
    ext = Path(path).suffix.lower()
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if ext == ".m4a":
        return ["-c:a", "aac", "-b:a", "256k"]
    raise ValueError("出力形式はwav、mp3、m4aのいずれかを指定してください。")


def boundary_fade_filter(duration: float, fade_in: float = AUTO_FADE_SECONDS, fade_out: float = AUTO_FADE_SECONDS) -> str:
    duration = max(0.0, float(duration))
    fade_in = min(max(0.0, float(fade_in)), duration / 2.0)
    fade_out = min(max(0.0, float(fade_out)), duration / 2.0)
    filters: list[str] = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.6f}:curve=tri")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={max(0.0, duration-fade_out):.6f}:d={fade_out:.6f}:curve=tri")
    return ",".join(filters)


def command_line(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags(),
    )
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        message = lines[-1] if lines else "FFmpegの処理に失敗しました。"
        error = RuntimeError(message)
        error.returncode = proc.returncode  # type: ignore[attr-defined]
        error.stderr = proc.stderr  # type: ignore[attr-defined]
        raise error
    return proc


def run_ffmpeg(args: list[str], overwrite: bool = True) -> subprocess.CompletedProcess[str]:
    mode = "-y" if overwrite else "-n"
    return run_command([ensure_tool("ffmpeg"), "-hide_banner", mode, *args])


def probe_media(path: str) -> dict[str, Any]:
    input_path = Path(path).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"入力ファイルが見つかりません: {input_path}")
    command = [
        ensure_tool("ffprobe"), "-v", "error", "-show_entries",
        "format=duration,format_name,size:stream=index,codec_type,codec_name,sample_rate,channels,channel_layout",
        "-of", "json", str(input_path),
    ]
    proc = run_command(command)
    raw = json.loads(proc.stdout)
    audio_streams = [stream for stream in raw.get("streams", []) if stream.get("codec_type") == "audio"]
    first = audio_streams[0] if audio_streams else {}
    duration = max(0.0, float(raw.get("format", {}).get("duration") or 0.0))
    return {
        "input": str(input_path),
        "duration": duration,
        "format_name": raw.get("format", {}).get("format_name"),
        "size": int(raw.get("format", {}).get("size") or input_path.stat().st_size),
        "audio_streams": len(audio_streams),
        "codec": first.get("codec_name"),
        "sample_rate": int(first["sample_rate"]) if first.get("sample_rate") else None,
        "channels": first.get("channels"),
        "channel_layout": first.get("channel_layout"),
        "command": command,
    }


def probe_duration(path: str) -> float:
    return float(probe_media(path)["duration"])


def build_trim_command(input_path: str, start: float, end: float, output_path: str, overwrite: bool = False) -> list[str]:
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"入力ファイルが見つかりません: {source}")
    if start < 0 or end <= start:
        raise ValueError("startは0以上、endはstartより大きい秒数を指定してください。")
    if source == output:
        raise ValueError("入力ファイルと出力ファイルには別のパスを指定してください。")
    source_duration = probe_duration(str(source))
    if end > source_duration + 0.05:
        raise ValueError(f"endは入力ファイルの長さ（{source_duration:.3f}秒）以内にしてください。")
    if output.exists() and not overwrite:
        raise FileExistsError(f"出力先はすでに存在します。上書きする場合は--overwriteを付けてください: {output}")
    if not output.parent.is_dir():
        raise FileNotFoundError(f"出力先フォルダが見つかりません: {output.parent}")
    duration = end - start
    fade = boundary_fade_filter(duration)
    args = [
        ensure_tool("ffmpeg"), "-hide_banner", "-y" if overwrite else "-n",
        "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{duration:.6f}",
        "-map", "0:a:0", "-vn",
    ]
    if fade:
        args += ["-af", fade]
    return [*args, *audio_args_for(str(output)), str(output)]


def trim_media(input_path: str, start: float, end: float, output_path: str, overwrite: bool = False, dry_run: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    command = build_trim_command(input_path, start, end, output_path, overwrite)
    result: dict[str, Any] = {
        "status": "dry-run" if dry_run else "ok",
        "operation": "trim",
        "input": str(Path(input_path).expanduser().resolve()),
        "output": str(Path(output_path).expanduser().resolve()),
        "start": start,
        "end": end,
        "duration": end - start,
        "command": command,
        "command_line": command_line(command),
        "ffmpeg_exit_code": None if dry_run else 0,
    }
    if not dry_run:
        proc = run_command(command)
        result["ffmpeg_exit_code"] = proc.returncode
        result["output_size"] = Path(output_path).expanduser().resolve().stat().st_size
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return result


@dataclass
class AudioClip:
    id: str
    path: str
    name: str
    start: float
    duration: float
    lane: int
    color: str
    trim_in: float = 0.0
    trim_out: float | None = None
    gain_db: float = 0.0
    fade_in: float = AUTO_FADE_SECONDS
    fade_out: float = AUTO_FADE_SECONDS
    loop: bool = False
    mute: bool = False


@dataclass
class AudioProject:
    version: int
    sample_rate: int
    channels: int
    clips: list[AudioClip]
    duration: float | None = None
    loudness_lufs: float | None = None
    true_peak_db: float = -1.0
    project_path: str | None = None


def _number(value: Any, name: str, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}は数値で指定してください。") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{name}は{minimum}以上の有限な数値で指定してください。")
    return number


def load_project(project_path: str) -> AudioProject:
    source = Path(project_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"プロジェクトファイルが見つかりません: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONの形式が正しくありません（{exc.lineno}行目）。") from exc
    version = int(data.get("version", PROJECT_VERSION))
    if version != PROJECT_VERSION:
        raise ValueError(f"対応していないプロジェクトバージョンです: {version}")
    sample_rate = int(data.get("sample_rate", 48000))
    channels = int(data.get("channels", 2))
    if sample_rate <= 0 or channels not in (1, 2):
        raise ValueError("sample_rateは正の整数、channelsは1または2を指定してください。")
    raw_clips = data.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ValueError("clipsには1個以上の音声クリップを指定してください。")
    clips: list[AudioClip] = []
    for index, item in enumerate(raw_clips):
        if not isinstance(item, dict):
            raise ValueError(f"clips[{index}]はオブジェクトで指定してください。")
        raw_path = Path(str(item.get("path", ""))).expanduser()
        path = (source.parent / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"clips[{index}]のファイルが見つかりません: {path}")
        info = probe_media(str(path))
        source_duration = float(info["duration"])
        trim_in = _number(item.get("trim_in", 0.0), f"clips[{index}].trim_in", 0.0)
        trim_out = _number(item.get("trim_out", source_duration), f"clips[{index}].trim_out", 0.0)
        if trim_out <= trim_in or trim_out > source_duration + 0.05:
            raise ValueError(f"clips[{index}]のtrim_outはtrim_inより後で、音声の長さ以内にしてください。")
        clips.append(AudioClip(
            id=str(item.get("id") or uuid.uuid4()),
            path=str(path),
            name=str(item.get("name") or path.name),
            start=_number(item.get("start", 0.0), f"clips[{index}].start", 0.0),
            duration=trim_out - trim_in,
            lane=max(0, int(item.get("lane", 1)) - 1),
            color=str(item.get("color") or ""),
            trim_in=trim_in,
            trim_out=trim_out,
            gain_db=_number(item.get("gain_db", 0.0), f"clips[{index}].gain_db"),
            fade_in=_number(item.get("fade_in", AUTO_FADE_SECONDS), f"clips[{index}].fade_in", 0.0),
            fade_out=_number(item.get("fade_out", AUTO_FADE_SECONDS), f"clips[{index}].fade_out", 0.0),
            loop=bool(item.get("loop", False)),
            mute=bool(item.get("mute", False)),
        ))
    master = data.get("master") or {}
    duration = data.get("duration")
    loudness = master.get("loudness_lufs")
    project = AudioProject(
        version=version,
        sample_rate=sample_rate,
        channels=channels,
        clips=clips,
        duration=_number(duration, "duration", 0.001) if duration is not None else None,
        loudness_lufs=_number(loudness, "master.loudness_lufs") if loudness is not None else None,
        true_peak_db=_number(master.get("true_peak_db", -1.0), "master.true_peak_db"),
        project_path=str(source),
    )
    if project.loudness_lufs is not None and not (-70.0 <= project.loudness_lufs <= -5.0):
        raise ValueError("master.loudness_lufsは-70から-5の範囲で指定してください。")
    if not (-9.0 <= project.true_peak_db <= 0.0):
        raise ValueError("master.true_peak_dbは-9から0の範囲で指定してください。")
    return project


def save_project(project_path: str, clips: list[AudioClip], sample_rate: int = 48000, channels: int = 2, duration: float | None = None, loudness_lufs: float | None = None, true_peak_db: float = -1.0) -> str:
    target = Path(project_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    clip_data: list[dict[str, Any]] = []
    for clip in clips:
        path = Path(clip.path).resolve()
        try:
            stored_path = str(path.relative_to(target.parent))
        except ValueError:
            stored_path = str(path)
        clip_data.append({
            "id": clip.id,
            "path": stored_path,
            "name": clip.name,
            "start": round(clip.start, 6),
            "lane": clip.lane + 1,
            "trim_in": round(clip.trim_in, 6),
            "trim_out": round(clip.trim_out if clip.trim_out is not None else clip.trim_in + clip.duration, 6),
            "gain_db": round(clip.gain_db, 3),
            "fade_in": round(clip.fade_in, 6),
            "fade_out": round(clip.fade_out, 6),
            "loop": clip.loop,
            "mute": clip.mute,
        })
    data: dict[str, Any] = {
        "version": PROJECT_VERSION,
        "sample_rate": sample_rate,
        "channels": channels,
        "clips": clip_data,
        "master": {"loudness_lufs": loudness_lufs, "true_peak_db": true_peak_db},
    }
    if duration is not None:
        data["duration"] = duration
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(target)


def project_total_duration(project: AudioProject) -> float:
    non_loop_end = max([clip.start + clip.duration for clip in project.clips if not clip.mute and not clip.loop] + [0.0])
    total = project.duration if project.duration is not None else non_loop_end
    if total <= 0 and any(clip.loop and not clip.mute for clip in project.clips):
        raise ValueError("ループだけのプロジェクトでは、最上位のdurationを指定してください。")
    return max(total, non_loop_end)


def build_mix_command(project: AudioProject, output_path: str, overwrite: bool = False) -> tuple[list[str], str, float]:
    output = Path(output_path).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"出力先はすでに存在します。上書きする場合は--overwriteを付けてください: {output}")
    if not output.parent.is_dir():
        raise FileNotFoundError(f"出力先フォルダが見つかりません: {output.parent}")
    if any(Path(clip.path).resolve() == output for clip in project.clips):
        raise ValueError("入力クリップと出力ファイルには別のパスを指定してください。")
    active = [clip for clip in project.clips if not clip.mute]
    if not active:
        raise ValueError("ミュートされていないクリップがありません。")
    total = project_total_duration(project)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, clip in enumerate(active):
        inputs += ["-i", clip.path]
        play_duration = total - clip.start if clip.loop else clip.duration
        if play_duration <= 0:
            continue
        chain = [f"atrim=start={clip.trim_in:.6f}:end={(clip.trim_out or clip.trim_in+clip.duration):.6f}", "asetpts=PTS-STARTPTS"]
        if clip.loop:
            sample_rate = int(probe_media(clip.path).get("sample_rate") or project.sample_rate)
            loop_samples = max(1, round(clip.duration * sample_rate))
            chain += [f"aloop=loop=-1:size={loop_samples}", f"atrim=0:{play_duration:.6f}", "asetpts=PTS-STARTPTS"]
        if clip.gain_db:
            chain.append(f"volume={clip.gain_db:.3f}dB")
        fades = boundary_fade_filter(play_duration, clip.fade_in, clip.fade_out)
        if fades:
            chain.append(fades)
        delay = max(0, round(clip.start * 1000))
        chain.append(f"adelay={delay}:all=1")
        label = f"a{index}"
        filters.append(f"[{index}:a]{','.join(chain)}[{label}]")
        labels.append(f"[{label}]")
    if not labels:
        raise ValueError("出力時間内に再生されるクリップがありません。")
    master = f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0"
    if project.loudness_lufs is not None:
        master += f",loudnorm=I={project.loudness_lufs:.2f}:TP={project.true_peak_db:.2f}:LRA=11"
    else:
        master += f",alimiter=limit={MIX_LIMITER_CEILING}:level=0:latency=1"
    master += f",atrim=0:{total:.6f}[outa]"
    filters.append(master)
    filter_complex = ";".join(filters)
    command = [
        ensure_tool("ffmpeg"), "-hide_banner", "-y" if overwrite else "-n", *inputs,
        "-filter_complex", filter_complex, "-map", "[outa]", "-ar", str(project.sample_rate),
        "-ac", str(project.channels), *audio_args_for(str(output)), str(output),
    ]
    return command, filter_complex, total


def mix_project(project_path: str, output_path: str, overwrite: bool = False, dry_run: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    project = load_project(project_path)
    command, filter_complex, duration = build_mix_command(project, output_path, overwrite)
    result: dict[str, Any] = {
        "status": "dry-run" if dry_run else "ok",
        "operation": "mix",
        "project": str(Path(project_path).expanduser().resolve()),
        "output": str(Path(output_path).expanduser().resolve()),
        "duration": duration,
        "clips": len([clip for clip in project.clips if not clip.mute]),
        "filter_complex": filter_complex,
        "command": command,
        "command_line": command_line(command),
        "ffmpeg_exit_code": None if dry_run else 0,
    }
    if not dry_run:
        proc = run_command(command)
        result["ffmpeg_exit_code"] = proc.returncode
        result["output_size"] = Path(output_path).expanduser().resolve().stat().st_size
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return result
