from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from threading import Event

from .models import Settings
from .storage import AppPaths
from .zoom import CancelledError


class PlaybackResolver:
    """Normalize media that GStreamer cannot discover into cached H.264/AAC MP4."""

    def __init__(self, paths: AppPaths):
        self.cache = paths.work / "playback"

    def resolve(
        self, source: Path, settings: Settings, cancel: Event | None = None,
        rate: float = 1.0,
    ) -> Path:
        if rate == 1.0 and self._discoverable(source):
            return source
        stat = source.stat()
        identity = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|rate={rate:g}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.cache.mkdir(parents=True, exist_ok=True)
        output = self.cache / f"{key}.mp4"
        if output.is_file() and output.stat().st_size > 0:
            return output
        temporary = self.cache / f".{key}.tmp.mp4"
        command = [
            settings.ffmpeg_path, "-hide_banner", "-nostdin", "-y",
            "-err_detect", "ignore_err", "-i", str(source),
        ]
        if rate == 1.0:
            command += [
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
            ]
        else:
            streams = self._stream_types(source, settings.ffprobe_path)
            if "video" in streams:
                command += [
                    "-map", "0:v:0", "-filter:v", f"setpts=PTS/{rate:g}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                ]
            if "audio" in streams:
                atempo = "atempo=2,atempo=2" if rate == 4 else f"atempo={rate:g}"
                command += [
                    "-map", "0:a:0", "-filter:a", atempo,
                    "-c:a", "aac", "-b:a", "160k",
                ]
            if not streams.intersection({"video", "audio"}):
                raise RuntimeError("FFprobe did not find a playable audio or video stream.")
        command += ["-movflags", "+faststart", str(temporary)]
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            while process.poll() is None:
                if cancel and cancel.wait(0.1):
                    process.terminate()
                    raise CancelledError()
            _stdout, stderr = process.communicate()
            if process.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
                detail = (stderr or "").strip()[-1200:]
                raise RuntimeError(f"FFmpeg could not prepare compatible playback media. {detail}")
            temporary.replace(output)
            return output
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _discoverable(source: Path) -> bool:
        executable = shutil.which("gst-discoverer-1.0")
        if not executable:
            return True
        result = subprocess.run(
            [executable, str(source)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _stream_types(source: Path, ffprobe: str) -> set[str]:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(source)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            value = json.loads(result.stdout)
            return {
                str(stream.get("codec_type", ""))
                for stream in value.get("streams", []) if isinstance(stream, dict)
            }
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"FFprobe could not inspect playback streams: {error}") from error
