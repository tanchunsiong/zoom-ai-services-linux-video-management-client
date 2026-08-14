from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from threading import Event
from typing import Callable

from .models import AudioPart, MediaProbe, Settings
from .zoom import CancelledError


MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".aiff", ".alac",
}
UPLOAD_TARGET_BYTES = 80_000_000


def discover(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for value in paths:
        try:
            path = value.expanduser().resolve()
        except OSError:
            continue
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            found.append(path)
        elif path.is_dir():
            for directory, _subdirectories, filenames in os.walk(
                path, followlinks=False, onerror=lambda _error: None
            ):
                for filename in filenames:
                    item = Path(directory) / filename
                    if item.suffix.lower() in MEDIA_EXTENSIONS:
                        found.append(item)
    return sorted(dict.fromkeys(found), key=lambda item: str(item).lower())


def tool_available(command: str) -> bool:
    return bool(shutil.which(command) if os.sep not in command else Path(command).is_file())


def basic_validation_error(path: Path) -> str | None:
    """Reject empty downloads and obvious web/cache files carrying media suffixes."""
    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            header = source.read(512)
    except OSError as error:
        return f"cannot read the file ({error})"
    if size == 0:
        return "the file is empty"
    lowered = header.lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        return "the file contains a web page, not media"
    if path.suffix.lower() in {".mp4", ".m4v", ".mov", ".m4a"}:
        if b"ftyp" not in header[:64]:
            return "the file does not contain an MP4/QuickTime header"
    return None


def _run(command: list[str], cancel: Event | None = None) -> str:
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"Required media tool was not found: {command[0]}") from error
    while process.poll() is None:
        if cancel and cancel.wait(0.1):
            process.terminate()
            try:
                process.wait(3)
            except subprocess.TimeoutExpired:
                process.kill()
            raise CancelledError()
    stdout, stderr = process.communicate()
    if process.returncode:
        message = stderr.strip()[-2000:] or f"{command[0]} exited with status {process.returncode}"
        raise RuntimeError(message)
    return stdout


def probe(source: Path, settings: Settings, cancel: Event | None = None) -> MediaProbe:
    output = _run(
        [
            settings.ffprobe_path, "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,sample_rate,channels,bit_rate,duration,disposition",
            "-of", "json", str(source),
        ],
        cancel,
    )
    try:
        root = json.loads(output)
    except ValueError as error:
        raise RuntimeError("FFprobe returned invalid media metadata.") from error
    streams = root.get("streams", [])
    candidates = [
        (position, item) for position, item in enumerate(streams)
        if item.get("codec_type") == "audio" and item.get("codec_name")
    ]
    candidates.sort(key=lambda value: value[1].get("disposition", {}).get("default") == 1, reverse=True)
    audio = candidates[0] if candidates else (-1, {})
    durations: list[float] = []
    for value in [root.get("format", {}).get("duration"), audio[1].get("duration")] + [item.get("duration") for item in streams]:
        try:
            number = float(value)
            if number > 0 and math.isfinite(number):
                durations.append(number)
        except (TypeError, ValueError):
            pass
    if not durations:
        raise RuntimeError("FFprobe did not return a positive media duration.")
    return MediaProbe(
        duration=max(durations),
        audio_codec=str(audio[1].get("codec_name", "")).lower(),
        sample_rate=_integer(audio[1].get("sample_rate")),
        channels=_integer(audio[1].get("channels")),
        bit_rate=_optional_integer(audio[1].get("bit_rate")),
        has_video=any(item.get("codec_type") == "video" for item in streams),
        audio_stream_index=_integer(audio[1].get("index"), audio[0]),
    )


def audio_profile(media: MediaProbe) -> tuple[str, str, str, bool, int | None]:
    if media.audio_codec in {"aac", "alac"} and media.channels <= 2 and media.bit_rate:
        return "m4a", "audio/mp4", "copy", True, None
    if media.audio_codec == "mp3" and media.channels <= 2 and media.bit_rate:
        return "mp3", "audio/mpeg", "copy", True, None
    return "mp3", "audio/mpeg", "libmp3lame", False, 128_000


def segment_duration(
    media: MediaProbe,
    requested: float,
    target_bytes: int = UPLOAD_TARGET_BYTES,
) -> float:
    _, _, _, stream_copy, output_rate = audio_profile(media)
    if output_rate:
        bytes_per_second = max(1, output_rate // 8)
    elif stream_copy and media.bit_rate:
        bytes_per_second = max(1, media.bit_rate // 8)
    else:
        bytes_per_second = max(media.sample_rate, 48_000) * min(max(media.channels, 1), 2) * 2
    return min(requested, max(1, target_bytes // bytes_per_second))


def extract(
    source: Path,
    work: Path,
    media: MediaProbe,
    settings: Settings,
    cancel: Event | None = None,
    target_bytes: int = UPLOAD_TARGET_BYTES,
    maximum_duration: float | None = None,
    progress: Callable[[float], None] | None = None,
) -> list[AudioPart]:
    if not media.audio_codec:
        raise RuntimeError("The selected file has no audio stream.")
    work.mkdir(parents=True, exist_ok=True)
    extension, mime_type, codec, stream_copy, rate = audio_profile(media)
    duration = segment_duration(
        media, maximum_duration or settings.segment_minutes * 60, target_bytes
    )
    count = max(1, math.ceil(media.duration / duration))
    parts: list[AudioPart] = []
    for index in range(count):
        if cancel and cancel.is_set():
            raise CancelledError()
        start = index * duration
        part_duration = min(duration, media.duration - start)
        if part_duration <= 0:
            break
        output = work / f"audio-{index + 1:03d}.{extension}"
        command = [
            settings.ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-i", str(source),
            "-ss", f"{start:.3f}", "-t", f"{part_duration:.3f}",
            "-map", f"0:{media.audio_stream_index}" if media.audio_stream_index >= 0 else "0:a:0",
            "-vn", "-c:a", codec,
        ]
        if not stream_copy:
            command.extend(["-ac", str(min(max(media.channels, 1), 2)), "-b:a", f"{rate // 1000}k"])
        command.append(str(output))
        _run(command, cancel)
        size = output.stat().st_size if output.exists() else 0
        if size <= 0:
            raise RuntimeError("FFmpeg produced an empty audio segment.")
        if size >= 100 * 1024 * 1024:
            raise RuntimeError("An audio segment exceeded Zoom's 100 MB limit.")
        parts.append(AudioPart(index, output, start, part_duration, mime_type))
        if progress:
            progress((index + 1) / count)
    return parts


def _integer(value: object, fallback: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


def _optional_integer(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
