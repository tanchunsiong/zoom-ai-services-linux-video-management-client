from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path

from .models import JobState, QueueJob, Settings, translation_route


@dataclass(frozen=True)
class Breakdown:
    scribe: float | None
    translate: float | None
    summarize: float | None

    @property
    def total(self) -> float | None:
        values = (self.scribe, self.translate, self.summarize)
        return sum(values) if all(value is not None for value in values) else None


@dataclass(frozen=True)
class Comparison:
    estimate: Breakdown
    actual: Breakdown


@dataclass(frozen=True)
class TimeRate:
    intercept: float
    seconds_per_unit: float
    samples: int = 0

    def estimate(self, units: float) -> float:
        return max(0.0, self.intercept + max(0.0, units) * self.seconds_per_unit)


@dataclass(frozen=True)
class TimeCalibration:
    scribe: TimeRate
    translate: TimeRate
    summarize: TimeRate
    scribe_by_extension: dict[str, TimeRate]


DEFAULT_CALIBRATION = TimeCalibration(
    TimeRate(12, 18), TimeRate(3, 0.6), TimeRate(6, 0.6), {},
)


def source_characters(job: QueueJob, settings: Settings) -> int:
    if job.has_audio is False:
        return 0
    if job.transcript_characters > 0:
        return job.transcript_characters
    if not job.duration_seconds:
        return 0
    density = settings.estimated_characters_per_minute or {
        "zh-CN": 300, "ja-JP": 300, "es-ES": 900, "it-IT": 850,
    }.get(job.source_language, 800)
    return math.ceil(max(0, job.duration_seconds) / 60 * density)


def cost_comparison(job: QueueJob, settings: Settings) -> Comparison:
    scribe_estimate = 0.0 if job.reuse_existing_transcript or job.has_audio is False else (
        job.duration_seconds / 60 * settings.scribe_usd_per_minute
        if job.duration_seconds is not None and settings.scribe_usd_per_minute > 0 else None
    )
    scribe_actual = 0.0 if job.reuse_existing_transcript or job.has_audio is False else (
        scribe_estimate if job.completed_at else None
    )
    steps = len(translation_route(job.source_language, job.translation_language)) if job.has_translation else 0
    estimated_translation_chars = source_characters(job, settings) * 2 * steps
    actual_translation_chars = job.translation_input_characters + job.translation_output_characters
    translate_estimate = 0.0 if not steps or job.reuse_existing_translation else (
        estimated_translation_chars / 1_000_000 * settings.translator_usd_per_million_characters
        if estimated_translation_chars and settings.translator_usd_per_million_characters > 0 else None
    )
    translate_actual = 0.0 if not steps or job.reuse_existing_translation else (
        actual_translation_chars / 1_000_000 * settings.translator_usd_per_million_characters
        if job.completed_at and actual_translation_chars else None
    )
    estimated_summary_chars = round(source_characters(job, settings) * 1.21) if job.summarize else 0
    actual_summary_chars = job.summary_input_characters + job.summary_output_characters
    summary_estimate = 0.0 if not job.summarize or job.reuse_existing_summary else (
        estimated_summary_chars / 1_000_000 * settings.summarizer_usd_per_million_characters
        if estimated_summary_chars and settings.summarizer_usd_per_million_characters > 0 else None
    )
    summary_actual = 0.0 if not job.summarize or job.reuse_existing_summary else (
        actual_summary_chars / 1_000_000 * settings.summarizer_usd_per_million_characters
        if job.completed_at and actual_summary_chars else None
    )
    return Comparison(
        Breakdown(scribe_estimate, translate_estimate, summary_estimate),
        Breakdown(scribe_actual, translate_actual, summary_actual),
    )


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _actual_stage(job: QueueJob, stages: set[str]) -> float | None:
    started, completed = _parse(job.started_at), _parse(job.completed_at)
    if not started or not completed:
        return None
    events = sorted(
        ((at, str(event.get("stage", ""))) for event in job.events
         if (at := _parse(event.get("at"))) and started <= at <= completed),
        key=lambda item: item[0],
    )
    total, found = 0.0, False
    for index, (at, stage) in enumerate(events):
        if stage not in stages or (index and events[index - 1][1] in stages):
            continue
        end = next((later for later, next_stage in events[index + 1:] if next_stage not in stages), completed)
        if end > at:
            total += (end - at).total_seconds()
            found = True
    return total if found else None


def _fit(observations: list[tuple[float, float]], fallback: TimeRate) -> TimeRate:
    if not observations:
        return fallback
    if len(observations) == 1:
        return TimeRate(observations[0][1], 0, 1)
    mean_units = sum(item[0] for item in observations) / len(observations)
    mean_seconds = sum(item[1] for item in observations) / len(observations)
    denominator = sum((units - mean_units) ** 2 for units, _seconds in observations)
    slope = fallback.seconds_per_unit if denominator <= 1e-12 else sum(
        (units - mean_units) * (seconds - mean_seconds)
        for units, seconds in observations
    ) / denominator
    intercept = mean_seconds - slope * mean_units
    return TimeRate(
        min(max(intercept, 0), 60),
        min(max(slope, 0.01), fallback.seconds_per_unit * 4),
        len(observations),
    )


def learn_time_calibration(jobs: list[QueueJob]) -> TimeCalibration:
    scribe: list[tuple[float, float, str]] = []
    translate: list[tuple[float, float]] = []
    summarize: list[tuple[float, float]] = []
    for job in jobs:
        if not job.completed_at:
            continue
        actual_scribe = _actual_stage(job, {JobState.PREPARING.value, JobState.TRANSCRIBING.value})
        if job.duration_seconds and actual_scribe and actual_scribe > 0:
            scribe.append((job.duration_seconds / 60, actual_scribe, Path(job.source_path).suffix.lower()))
        actual_translate = _actual_stage(job, {JobState.TRANSLATING.value})
        characters = job.translation_input_characters + job.translation_output_characters
        if characters and actual_translate and actual_translate > 0:
            translate.append((characters / 1000, actual_translate))
        actual_summary = _actual_stage(job, {JobState.SUMMARIZING.value})
        if job.summary_input_characters and actual_summary and actual_summary > 0:
            summarize.append((job.summary_input_characters / 1000, actual_summary))
    by_extension: dict[str, TimeRate] = {}
    for extension in {item[2] for item in scribe}:
        observations = [(units, seconds) for units, seconds, ext in scribe if ext == extension]
        if len(observations) >= 3:
            by_extension[extension] = _fit(observations, DEFAULT_CALIBRATION.scribe)
    return TimeCalibration(
        _fit([(units, seconds) for units, seconds, _ext in scribe], DEFAULT_CALIBRATION.scribe),
        _fit(translate, DEFAULT_CALIBRATION.translate),
        _fit(summarize, DEFAULT_CALIBRATION.summarize),
        by_extension,
    )


def time_comparison(
    job: QueueJob, settings: Settings, calibration: TimeCalibration | None = None,
) -> Comparison:
    calibration = calibration or DEFAULT_CALIBRATION
    steps = len(translation_route(job.source_language, job.translation_language)) if job.has_translation else 0
    scribe_estimate = 0.0 if job.reuse_existing_transcript or job.has_audio is False else (
        ((calibration.scribe_by_extension.get(Path(job.source_path).suffix.lower(), calibration.scribe).estimate(job.duration_seconds / 60))
         if calibration.scribe.samples > 0 or Path(job.source_path).suffix.lower() in calibration.scribe_by_extension
         else 12 + job.duration_seconds * 0.30)
        if job.duration_seconds else None
    )
    estimated_translation_chars = source_characters(job, settings) * 2 * steps
    translate_estimate = 0.0 if not steps or job.reuse_existing_translation else (
        (calibration.translate.estimate(estimated_translation_chars / 1000)
         if calibration.translate.samples > 0
         else steps * 3 + estimated_translation_chars / 1000 * 0.60)
        if estimated_translation_chars else None
    )
    estimated_summary_chars = round(source_characters(job, settings) * 1.21) if job.summarize else 0
    summary_estimate = 0.0 if not job.summarize or job.reuse_existing_summary else (
        (calibration.summarize.estimate(estimated_summary_chars / 1000)
         if calibration.summarize.samples > 0
         else 6 + estimated_summary_chars / 1000 * 0.60)
        if estimated_summary_chars else None
    )
    scribe_actual = 0.0 if job.reuse_existing_transcript or job.has_audio is False else _actual_stage(
        job, {JobState.PREPARING.value, JobState.TRANSCRIBING.value}
    )
    translate_actual = 0.0 if not steps or job.reuse_existing_translation else _actual_stage(
        job, {JobState.TRANSLATING.value}
    )
    summary_actual = 0.0 if not job.summarize or job.reuse_existing_summary else _actual_stage(
        job, {JobState.SUMMARIZING.value}
    )
    return Comparison(
        Breakdown(scribe_estimate, translate_estimate, summary_estimate),
        Breakdown(scribe_actual, translate_actual, summary_actual),
    )


def format_usd(value: float | None) -> str:
    if value is None:
        return "--"
    return f"${value:.4f}" if 0 < value < 0.01 else f"${value:.2f}"


def format_time(value: float | None) -> str:
    if value is None:
        return "--"
    seconds = max(0, int(value + 0.999))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds // 60 % 60:02d}m"


def sum_breakdowns(values: list[Breakdown]) -> Breakdown:
    def total(field: str) -> float | None:
        items = [getattr(value, field) for value in values]
        return sum(items) if all(item is not None for item in items) else None
    return Breakdown(total("scribe"), total("translate"), total("summarize"))
