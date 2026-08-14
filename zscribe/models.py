from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


LANGUAGES = {
    "en-US": "English",
    "zh-CN": "Chinese (Simplified)",
    "ja-JP": "Japanese",
    "es-ES": "Spanish",
    "it-IT": "Italian",
}


class JobState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    TRANSCRIBING = "transcribing"
    TRANSLATING = "translating"
    SUMMARIZING = "summarizing"
    READY = "ready"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def processing(self) -> bool:
        return self in {
            self.PREPARING,
            self.TRANSCRIBING,
            self.TRANSLATING,
            self.SUMMARIZING,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobEvent:
    stage: str
    message: str
    at: str = field(default_factory=now_iso)


@dataclass
class QueueJob:
    source_path: str
    source_language: str = "en-US"
    translation_language: str = ""
    summarize: bool = False
    id: str = field(default_factory=lambda: str(uuid4()))
    state: str = JobState.QUEUED.value
    progress: float = 0.0
    status_message: str = "Waiting in queue"
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    has_audio: bool | None = None
    transcript_characters: int = 0
    translation_input_characters: int = 0
    translation_output_characters: int = 0
    summary_input_characters: int = 0
    summary_output_characters: int = 0
    original_vtt_path: str | None = None
    translated_vtt_path: str | None = None
    transcript_json_path: str | None = None
    summary_path: str | None = None
    reuse_existing_transcript: bool = False
    reuse_existing_translation: bool = False
    reuse_existing_summary: bool = False
    existing_transcript_is_stale: bool = False
    existing_translation_is_stale: bool = False
    existing_summary_is_stale: bool = False
    events: list[dict[str, str]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return Path(self.source_path).name

    @property
    def has_translation(self) -> bool:
        return bool(self.translation_language and self.translation_language != self.source_language)

    @property
    def can_review(self) -> bool:
        return self.state == JobState.READY.value and bool(
            self.original_vtt_path and Path(self.original_vtt_path).is_file()
        )

    def report(self, stage: JobState, progress: float, message: str) -> None:
        self.state = stage.value
        self.progress = min(max(progress, 0.0), 1.0)
        self.status_message = message
        self.events.append(asdict(JobEvent(stage=stage.value, message=message)))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QueueJob":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass
class Settings:
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    scribe_concurrency: int = 2
    segment_minutes: int = 15
    scribe_usd_per_minute: float = 0.0033
    translator_usd_per_million_characters: float = 7.50
    summarizer_usd_per_million_characters: float = 0.40
    estimated_characters_per_minute: int = 0
    live_source: str = "microphone"
    live_language: str = "en-US"
    live_translation_language: str = ""
    live_vocabulary_json: str = ""
    live_auto_gain: bool = False
    live_device_id: str = ""
    live_microphone_device_id: str = ""
    live_system_device_id: str = ""

    def clamp(self) -> None:
        self.scribe_concurrency = min(max(int(self.scribe_concurrency), 1), 4)
        self.segment_minutes = min(max(int(self.segment_minutes), 1), 30)
        self.scribe_usd_per_minute = max(float(self.scribe_usd_per_minute), 0.0)
        self.translator_usd_per_million_characters = max(
            float(self.translator_usd_per_million_characters), 0.0
        )
        self.summarizer_usd_per_million_characters = max(
            float(self.summarizer_usd_per_million_characters), 0.0
        )
        self.estimated_characters_per_minute = min(
            max(int(self.estimated_characters_per_minute), 0), 10_000
        )
        if self.live_source not in {"microphone", "system", "both"}:
            self.live_source = "microphone"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Settings":
        allowed = cls.__dataclass_fields__.keys()
        result = cls(**{key: item for key, item in value.items() if key in allowed})
        result.clamp()
        return result


@dataclass(frozen=True)
class Credentials:
    api_key: str = ""
    api_secret: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.api_key.strip() and self.api_secret.strip())


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


@dataclass
class MediaProbe:
    duration: float
    audio_codec: str
    sample_rate: int
    channels: int
    bit_rate: int | None
    has_video: bool
    audio_stream_index: int


@dataclass
class AudioPart:
    index: int
    path: Path
    timeline_start: float
    duration: float
    mime_type: str


def translation_route(source: str, target: str) -> list[tuple[str, str]]:
    if source.lower() == target.lower():
        return []
    if source == "en-US" or target == "en-US":
        return [(source, target)]
    return [(source, "en-US"), ("en-US", target)]
