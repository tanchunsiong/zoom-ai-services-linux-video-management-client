from __future__ import annotations

import json
import math
import queue
import ssl
import threading
import time
from dataclasses import dataclass
from array import array
from typing import Any, Callable

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

try:
    import websocket  # type: ignore  # noqa: E402
except ModuleNotFoundError:  # The GTK UI remains usable before Python deps are installed.
    websocket = None  # type: ignore

from .models import Credentials
from .zoom import LIVE_URL, create_token


DEFAULT_VOCABULARY = """{
  "phrases": ["Zoom", "Zoom AI Companion"]
}"""

PCM16_FRAME_BYTES = 16_000 * 2 // 10


def completed_transcript_display(segments: list[str]) -> str:
    """Render finalized Live segments newest-first for the scrolling UI."""
    return "\n\n".join(reversed(segments))


def completed_transcript_source(segments: list[str]) -> str:
    """Build chronological source-only text for downstream Zoom services."""
    return "\n\n".join(segments).strip()


def parse_vocabulary(value: str) -> dict[str, Any] | None:
    if not value.strip():
        return None
    root = json.loads(value)
    if not isinstance(root, dict):
        raise ValueError("Vocabulary JSON must be an object.")
    if isinstance(root.get("config"), dict) and "vocabulary" in root["config"]:
        root = root["config"]["vocabulary"]
    elif "vocabulary" in root:
        root = root["vocabulary"]
    if not isinstance(root, dict):
        raise ValueError("The vocabulary value must be an object.")
    phrases = root.get("phrases")
    if phrases is not None and not isinstance(phrases, list):
        raise ValueError("vocabulary.phrases must be an array.")
    return root


def session_update(language: str, vocabulary_json: str = "") -> dict[str, Any]:
    vocabulary = parse_vocabulary(vocabulary_json)
    config: dict[str, Any] = {"language": language}
    payload: dict[str, Any] = {
        "type": "session.update",
        "input_audio_format": "pcm16",
        "language": language,
        "audio": {"format": "pcm16"},
        "config": config,
    }
    if vocabulary:
        config["vocabulary"] = vocabulary
        payload["vocabulary"] = vocabulary
    return payload


def parse_event(value: str) -> dict[str, Any]:
    root = json.loads(value)
    if not isinstance(root, dict):
        raise ValueError("Zoom Live returned invalid JSON.")

    def find(item: Any, depth: int = 0) -> str | None:
        if depth > 4:
            return None
        if isinstance(item, dict):
            for key in ("transcript", "text", "delta"):
                if isinstance(item.get(key), str):
                    return item[key]
            for nested in item.values():
                found = find(nested, depth + 1)
                if found and found.strip():
                    return found
        elif isinstance(item, list):
            for nested in item:
                found = find(nested, depth + 1)
                if found and found.strip():
                    return found
        return None

    error = root.get("error")
    if isinstance(error, dict):
        error = error.get("message") or str(error)
    event_type = str(root.get("type", "unknown"))
    return {
        "type": event_type,
        "transcript": find(root),
        "error": str(error) if error else None,
        "delta": "delta" in event_type.lower() or "delta" in root,
        "raw": root,
    }


def audio_sources() -> tuple[list[str], list[str]]:
    """Return physical/virtual inputs and output monitors visible to GStreamer."""
    Gst.init(None)
    monitor = Gst.DeviceMonitor()
    monitor.add_filter("Audio/Source", None)
    if not monitor.start():
        return [], []
    inputs: list[str] = []
    output_monitors: list[str] = []
    try:
        for device in monitor.get_devices():
            name = device.get_display_name() or "Audio source"
            properties = device.get_properties()
            device_class = ""
            if properties and properties.has_field("device.class"):
                device_class = properties.get_string("device.class") or ""
            if device_class == "monitor" or name.lower().startswith("monitor of "):
                output_monitors.append(name)
            else:
                inputs.append(name)
    finally:
        monitor.stop()
    return inputs, output_monitors


@dataclass(frozen=True)
class AudioDeviceOption:
    device_id: str
    name: str
    source: str


def audio_device_options(source: str) -> list[AudioDeviceOption]:
    Gst.init(None)
    monitor = Gst.DeviceMonitor()
    monitor.add_filter("Audio/Source", None)
    if not monitor.start():
        return []
    values: list[AudioDeviceOption] = []
    try:
        for device in monitor.get_devices():
            properties = device.get_properties()
            device_class = properties.get_string("device.class") if properties and properties.has_field("device.class") else ""
            is_monitor = device_class == "monitor" or (device.get_display_name() or "").lower().startswith("monitor of ")
            if (source == "system") != is_monitor:
                continue
            node_name = properties.get_string("node.name") if properties and properties.has_field("node.name") else ""
            device_id = f"{node_name}.monitor" if is_monitor and node_name and not node_name.endswith(".monitor") else (node_name or "")
            values.append(AudioDeviceOption(device_id, device.get_display_name() or "Audio source", source))
    finally:
        monitor.stop()
    return values


def _pulse_source(device_id: str, default: str) -> str:
    if not device_id:
        return default
    safe_device = device_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'pulsesrc device="{safe_device}"'


def audio_capture_pipeline(
    source: str, device_id: str = "", secondary_device_id: str = "",
) -> str:
    """Build the capture graph; Both mode mixes mic and monitor before PCM output."""
    output = (
        "audioconvert ! audioresample ! "
        "audio/x-raw,format=S16LE,rate=16000,channels=1 ! "
        "appsink name=sink emit-signals=true sync=false max-buffers=20 drop=true"
    )
    if source != "both":
        default = "pulsesrc device=@DEFAULT_MONITOR@" if source == "system" else "autoaudiosrc"
        return f"{_pulse_source(device_id, default)} ! {output}"
    microphone = _pulse_source(device_id, "autoaudiosrc")
    monitor = _pulse_source(secondary_device_id, "pulsesrc device=@DEFAULT_MONITOR@")
    branch = (
        "queue max-size-buffers=20 leaky=downstream ! audioconvert ! audioresample ! "
        "audio/x-raw,format=S16LE,rate=16000,channels=1 ! mix."
    )
    return f"audiomixer name=mix ! {output} {microphone} ! {branch} {monitor} ! {branch}"


class AudioCapture:
    """Capture the default PipeWire/PulseAudio source as 16 kHz mono PCM16."""

    def __init__(
        self,
        on_frame: Callable[[bytes], None],
        on_level: Callable[[float], None],
        source: str = "microphone",
        device_id: str = "",
        secondary_device_id: str = "",
    ):
        Gst.init(None)
        self.on_frame = on_frame
        self.on_level = on_level
        self.pipeline = Gst.parse_launch(
            audio_capture_pipeline(source, device_id, secondary_device_id)
        )
        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._sample)

    def _sample(self, sink: Any) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            frame = bytes(info.data)
            buffer.unmap(info)
            if frame:
                self.on_frame(frame)
                peak = max((abs(int.from_bytes(frame[i:i + 2], "little", signed=True)) for i in range(0, len(frame) - 1, 2)), default=0)
                self.on_level(min(peak / 32768.0, 1.0))
        return Gst.FlowReturn.OK

    def start(self) -> None:
        change = self.pipeline.set_state(Gst.State.PLAYING)
        if change != Gst.StateChangeReturn.FAILURE:
            change, _state, _pending = self.pipeline.get_state(3 * Gst.SECOND)
        if change == Gst.StateChangeReturn.FAILURE:
            message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if message:
                error, _debug = message.parse_error()
                raise RuntimeError(f"Could not start Linux audio capture: {error.message}")
            raise RuntimeError("Could not start the selected Linux audio source.")

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)


@dataclass
class LiveCallbacks:
    event: Callable[[dict[str, Any]], None]
    level: Callable[[float], None]
    state: Callable[[str], None]
    error: Callable[[str], None]


class LiveSession:
    def __init__(
        self,
        credentials: Credentials,
        language: str,
        vocabulary_json: str,
        callbacks: LiveCallbacks,
        capture_source: str = "microphone",
        capture_device_id: str = "",
        automatic_gain: bool = False,
        secondary_device_id: str = "",
    ):
        self.credentials = credentials
        self.language = language
        self.vocabulary_json = vocabulary_json
        self.callbacks = callbacks
        self.capture_source = capture_source
        self.capture_device_id = capture_device_id
        self.secondary_device_id = secondary_device_id
        self.automatic_gain = automatic_gain
        self.frames: queue.Queue[bytes | None] = queue.Queue(maxsize=50)
        self.stop_event = threading.Event()
        self.capture: AudioCapture | None = None
        self.socket: websocket.WebSocket | None = None
        self.thread: threading.Thread | None = None
        self.frames_received = 0
        self.non_silent_frames = 0
        self.signal_announced = False
        self.pending_pcm = bytearray()
        self.frame_lock = threading.Lock()
        self.frames_sent = 0
        self.bytes_sent = 0
        self.receive_failure: str | None = None
        self.gain = 1.0

    def start(self) -> None:
        if websocket is None:
            raise RuntimeError(
                "Live captions require websocket-client. Run scripts/install-local.sh first."
            )
        if not self.credentials.complete:
            raise ValueError("Save Zoom Build credentials before starting Live captions.")
        parse_vocabulary(self.vocabulary_json)
        inputs, monitors = audio_sources()
        if self.capture_source == "microphone" and not inputs:
            available = f" Only {', '.join(monitors)} is available." if monitors else ""
            raise RuntimeError(
                "No microphone input is available to Linux."
                f"{available} Choose System audio or attach a microphone."
            )
        if self.capture_source == "system" and not monitors:
            raise RuntimeError(
                "No system-audio monitor is available. Start an output device or "
                "select Microphone."
            )
        if self.capture_source == "both" and (not inputs or not monitors):
            missing = []
            if not inputs:
                missing.append("a microphone")
            if not monitors:
                missing.append("a system-audio monitor")
            raise RuntimeError(
                "Microphone + system audio requires " + " and ".join(missing) + "."
            )
        self.thread = threading.Thread(target=self._run, daemon=True, name="zscribe-live")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.capture:
            self.capture.stop()
        self._drain_pcm()
        try:
            self.frames.put(None, timeout=1)
        except queue.Full:
            # Preserve a termination marker even if capture briefly outran the
            # sender. Dropping one oldest 100 ms frame is preferable to hanging
            # the Live worker during shutdown.
            try:
                self.frames.get_nowait()
                self.frames.put_nowait(None)
            except queue.Empty:
                pass

    def _frame(self, data: bytes) -> None:
        data, peak = self._process_audio(data)
        self._level(peak)
        complete: list[bytes] = []
        with self.frame_lock:
            self.pending_pcm.extend(data)
            while len(self.pending_pcm) >= PCM16_FRAME_BYTES:
                complete.append(bytes(self.pending_pcm[:PCM16_FRAME_BYTES]))
                del self.pending_pcm[:PCM16_FRAME_BYTES]
        for frame in complete:
            self._queue_frame(frame)

    def _process_audio(self, data: bytes) -> tuple[bytes, float]:
        if not data:
            return data, 0.0
        samples = array("h")
        samples.frombytes(data[: len(data) // 2 * 2])
        peak = max((abs(value) for value in samples), default=0) / 32768
        rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples))) / 32768
        if self.automatic_gain:
            if rms >= 0.0018:
                desired = min(max(min(0.1259 / rms, 0.8913 / peak if peak else 8), 0.25), 8)
                smoothing = 0.65 if desired < self.gain else 0.25
                self.gain += (desired - self.gain) * smoothing
            else:
                self.gain += (1 - self.gain) * 0.1
            for index, value in enumerate(samples):
                samples[index] = min(max(round(value * self.gain), -32768), 32767)
            peak = max((abs(value) for value in samples), default=0) / 32768
            data = samples.tobytes()
        return data, min(peak, 1.0)

    def _drain_pcm(self) -> None:
        with self.frame_lock:
            remainder = bytes(self.pending_pcm)
            self.pending_pcm.clear()
        if remainder:
            self._queue_frame(remainder)

    def _queue_frame(self, data: bytes) -> None:
        self.frames_received += 1
        try:
            self.frames.put_nowait(data)
        except queue.Full:
            try:
                self.frames.get_nowait()
                self.frames.put_nowait(data)
            except queue.Empty:
                pass

    def _level(self, value: float) -> None:
        self.callbacks.level(value)
        if value >= 0.01:
            self.non_silent_frames += 1
            if not self.signal_announced:
                self.signal_announced = True
                self.callbacks.state("Listening — audio signal detected")

    def _watch_signal(self) -> None:
        if self.stop_event.wait(5):
            return
        if self.frames_received == 0:
            self.callbacks.state(
                "Listening — no PCM frames; check the selected Linux audio source"
            )
        elif self.non_silent_frames == 0:
            prompt = (
                "play audio through the desktop output"
                if self.capture_source == "system"
                else "check both microphone and desktop audio levels"
                if self.capture_source == "both"
                else "check microphone input and mute settings"
            )
            self.callbacks.state(f"Listening — silence detected; {prompt}")

    def _run(self) -> None:
        try:
            self.callbacks.state("Connecting to Zoom Live…")
            self.socket = websocket.create_connection(
                LIVE_URL,
                header=[f"Authorization: Bearer {create_token(self.credentials)}"],
                subprotocols=["live-asr"],
                timeout=10,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            )
            self.socket.send(json.dumps(session_update(self.language, self.vocabulary_json)))
            while True:
                event = parse_event(self.socket.recv())
                self.callbacks.event(event)
                if event["type"] == "error":
                    raise RuntimeError(event["error"] or "Zoom Live returned an error.")
                if event["type"] == "session.updated":
                    break
            self.capture = AudioCapture(
                self._frame, lambda _level: None, self.capture_source,
                self.capture_device_id, self.secondary_device_id,
            )
            self.capture.start()
            self.callbacks.state(
                "Listening — play desktop audio"
                if self.capture_source == "system"
                else "Listening — speak and play desktop audio"
                if self.capture_source == "both"
                else "Listening — speak into the microphone"
            )
            threading.Thread(target=self._watch_signal, daemon=True).start()
            receiver = threading.Thread(target=self._receive, daemon=True)
            receiver.start()
            # Stop capture first, then consume through the sentinel inserted by
            # stop() so the final partial frame reaches Zoom before session.close.
            while True:
                try:
                    frame = self.frames.get(timeout=0.2)
                except queue.Empty:
                    if self.stop_event.is_set():
                        break
                    continue
                if frame is None:
                    break
                self.socket.send_binary(frame)
                self.frames_sent += 1
                self.bytes_sent += len(frame)
            if self.capture:
                self.capture.stop()
            self.callbacks.state("Finishing the last caption…")
            self.socket.send(json.dumps({"type": "session.close"}))
            receiver.join(timeout=15)
        except Exception as error:
            if not self.stop_event.is_set() and not self.receive_failure:
                self.callbacks.error(str(error))
        finally:
            if self.capture:
                self.capture.stop()
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
            self.callbacks.state("Stopped")

    def _receive(self) -> None:
        assert self.socket
        while not self.stop_event.is_set() or self.socket.connected:
            try:
                event = parse_event(self.socket.recv())
            except Exception as error:
                if websocket is not None and isinstance(
                    error, websocket.WebSocketTimeoutException
                ):
                    continue
                if not self.stop_event.is_set():
                    self.receive_failure = f"Zoom Live receive failed: {error}"
                    self.callbacks.error(self.receive_failure)
                    self.stop_event.set()
                return
            self.callbacks.event(event)
            if event["type"] in {"session.closed", "error"}:
                return
