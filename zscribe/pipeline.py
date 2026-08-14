from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from . import vtt
from .media import UPLOAD_TARGET_BYTES, extract, probe
from .models import Credentials, Cue, JobState, QueueJob, Settings, translation_route
from .storage import AppPaths
from .zoom import CancelledError, ZoomAPIError, ZoomClient, normalize_summary


Update = Callable[[QueueJob], None]


def sidecars(source: Path, target_language: str) -> dict[str, Path]:
    base = source.with_suffix("")
    translated = base.with_name(f"{base.name}.translated-{target_language}.vtt")
    return {
        "original": base.with_suffix(".vtt"),
        "translated": translated,
        "transcript": base.with_suffix(".transcript.json"),
        "summary": base.with_suffix(".summary.md"),
    }


def apply_existing(job: QueueJob) -> QueueJob:
    outputs = sidecars(Path(job.source_path), job.translation_language)
    original = outputs["original"]
    if (not job.existing_transcript_is_stale and original.is_file() and
            original.stat().st_size > 0 and vtt.parse(original.read_text(encoding="utf-8"))):
        job.original_vtt_path = str(original)
        job.transcript_json_path = str(outputs["transcript"]) if outputs["transcript"].is_file() else None
        if job.has_translation and not job.existing_translation_is_stale and outputs["translated"].is_file():
            job.translated_vtt_path = str(outputs["translated"])
        if job.summarize and not job.existing_summary_is_stale and outputs["summary"].is_file():
            job.summary_path = str(outputs["summary"])
        expected = not job.has_translation or bool(job.translated_vtt_path)
        expected = expected and (not job.summarize or bool(job.summary_path))
        if expected:
            job.state = JobState.READY.value
            job.progress = 1.0
            job.status_message = "Existing outputs are ready to review"
    return job


class MediaPipeline:
    def __init__(self, paths: AppPaths, zoom: ZoomClient | None = None):
        self.paths = paths
        self.zoom = zoom or ZoomClient()

    def process(
        self,
        job: QueueJob,
        settings: Settings,
        credentials: Credentials,
        update: Update,
        cancel: Event,
    ) -> QueueJob:
        if not credentials.complete:
            raise ValueError("Save Zoom Build API credentials in Settings before starting the queue.")
        job.started_at = datetime.now(timezone.utc).isoformat()
        job.completed_at = None
        job.error = None
        job.translation_input_characters = job.translation_output_characters = 0
        job.summary_input_characters = job.summary_output_characters = 0
        source = Path(job.source_path)
        if not source.is_file():
            raise FileNotFoundError(f"Media file no longer exists: {source}")
        outputs = sidecars(source, job.translation_language)
        work = self.paths.work / job.id
        shutil.rmtree(work, ignore_errors=True)
        try:
            cues = self._transcribe_or_reuse(job, source, outputs, work, settings, credentials, update, cancel)
            transcript_text = "\n".join(cue.text for cue in cues)
            job.transcript_characters = len(transcript_text)
            summary_text, summary_language = transcript_text, job.source_language
            if job.has_translation:
                translated, used_in, used_out = self._translate_or_reuse(
                    job, cues, outputs, credentials, update, cancel
                )
                job.translation_input_characters = used_in
                job.translation_output_characters = used_out
                summary_text = "\n".join(cue.text for cue in translated)
                summary_language = job.translation_language
            if job.summarize:
                self._summarize_or_reuse(
                    job, summary_text, summary_language, outputs, credentials, update, cancel
                )
            if cancel.is_set():
                raise CancelledError()
            job.completed_at = datetime.now(timezone.utc).isoformat()
            job.report(JobState.READY, 1.0, "Transcript is ready to review")
            update(job)
            return job
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _transcribe_or_reuse(
        self, job: QueueJob, source: Path, outputs: dict[str, Path], work: Path,
        settings: Settings, credentials: Credentials, update: Update, cancel: Event,
    ) -> list[Cue]:
        if not job.existing_transcript_is_stale and outputs["original"].is_file():
            existing = vtt.parse(outputs["original"].read_text(encoding="utf-8"))
            if existing:
                job.original_vtt_path = str(outputs["original"])
                job.transcript_json_path = str(outputs["transcript"]) if outputs["transcript"].is_file() else None
                job.reuse_existing_transcript = True
                job.report(JobState.PREPARING, 0.65, "Using existing original captions")
                update(job)
                return existing
        job.reuse_existing_transcript = False
        job.report(JobState.PREPARING, 0.02, "Inspecting media")
        update(job)
        media = probe(source, settings, cancel)
        job.duration_seconds = media.duration
        job.has_audio = bool(media.audio_codec)
        if not job.has_audio:
            raise RuntimeError("The selected file has no audio stream.")
        target = UPLOAD_TARGET_BYTES
        maximum_duration: float | None = None
        documents: list[tuple[int, float, dict]] = []
        while not documents:
            shutil.rmtree(work, ignore_errors=True)
            parts = extract(
                source, work, media, settings, cancel, target, maximum_duration,
                lambda value: self._report(job, update, JobState.PREPARING, 0.03 + value * 0.17, "Preparing audio segments"),
            )
            job.report(JobState.TRANSCRIBING, 0.2, f"Transcribing {len(parts)} audio segment{'s' if len(parts) != 1 else ''}")
            update(job)
            try:
                with ThreadPoolExecutor(max_workers=settings.scribe_concurrency) as pool:
                    futures = {
                        pool.submit(self.zoom.transcribe, part, job.source_language, credentials, cancel): part
                        for part in parts
                    }
                    completed = 0
                    for future in as_completed(futures):
                        part = futures[future]
                        document = future.result()
                        documents.append((part.index, part.timeline_start, document))
                        part.path.unlink(missing_ok=True)
                        completed += 1
                        self._report(
                            job, update, JobState.TRANSCRIBING,
                            0.2 + 0.45 * completed / len(parts),
                            f"Transcribed {completed} of {len(parts)} segments",
                        )
            except ZoomAPIError as error:
                documents.clear()
                if error.status_code == 413 and target // 2 >= 10_000_000:
                    target //= 2
                    self._report(job, update, JobState.PREPARING, 0.2, f"Zoom rejected the audio size; retrying with {target // 1_000_000} MB parts")
                    continue
                if error.status_code == 503:
                    current = maximum_duration or settings.segment_minutes * 60
                    if current // 2 >= 60:
                        maximum_duration = current // 2
                        self._report(job, update, JobState.PREPARING, 0.2, f"Zoom could not process a segment; retrying with {int(maximum_duration // 60)} minute parts")
                        continue
                raise
        cues: list[Cue] = []
        first_document: dict | None = None
        for _, offset, document in sorted(documents):
            first_document = first_document or document
            for raw in document.get("cues", []):
                cues.append(Cue(0, float(raw["start"]) + offset, float(raw["end"]) + offset, str(raw["text"])))
        cues.sort(key=lambda cue: cue.start)
        for index, cue in enumerate(cues, 1):
            cue.index = index
        outputs["original"].write_text(vtt.write(cues), encoding="utf-8")
        transcript = {
            "language": job.source_language,
            "cues": [asdict(cue) for cue in cues],
            "text": "\n".join(cue.text for cue in cues),
            "request_id": (first_document or {}).get("request_id"),
            "model": (first_document or {}).get("model"),
        }
        outputs["transcript"].write_text(json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        job.original_vtt_path = str(outputs["original"])
        job.transcript_json_path = str(outputs["transcript"])
        job.existing_transcript_is_stale = False
        return cues

    def _translate_or_reuse(
        self, job: QueueJob, cues: list[Cue], outputs: dict[str, Path],
        credentials: Credentials, update: Update, cancel: Event,
    ) -> tuple[list[Cue], int, int]:
        if not job.existing_translation_is_stale and outputs["translated"].is_file():
            existing = vtt.parse(outputs["translated"].read_text(encoding="utf-8"))
            if existing:
                job.reuse_existing_translation = True
                job.translated_vtt_path = str(outputs["translated"])
                self._report(job, update, JobState.TRANSLATING, 0.82, "Using existing translated captions")
                return existing, 0, 0
        translated = cues
        used_in = used_out = 0
        route = translation_route(job.source_language, job.translation_language)
        for index, (source, target) in enumerate(route):
            self._report(job, update, JobState.TRANSLATING, 0.67 + 0.14 * index / len(route), f"Translating {source} to {target}")
            translated, step_in, step_out = self.zoom.translate_cues(
                translated, source, target, credentials, cancel
            )
            used_in += step_in
            used_out += step_out
        outputs["translated"].write_text(vtt.write(translated), encoding="utf-8")
        job.translated_vtt_path = str(outputs["translated"])
        job.existing_translation_is_stale = False
        return translated, used_in, used_out

    def _summarize_or_reuse(
        self, job: QueueJob, text: str, language: str, outputs: dict[str, Path],
        credentials: Credentials, update: Update, cancel: Event,
    ) -> None:
        if (not job.existing_summary_is_stale and outputs["summary"].is_file() and
                outputs["summary"].stat().st_size):
            existing = outputs["summary"].read_text(encoding="utf-8")
            normalized = normalize_summary(existing)
            if normalized != existing:
                outputs["summary"].write_text(normalized, encoding="utf-8")
            job.reuse_existing_summary = True
            job.summary_path = str(outputs["summary"])
            self._report(job, update, JobState.SUMMARIZING, 0.96, "Using existing summary")
            return
        self._report(job, update, JobState.SUMMARIZING, 0.84, f"Summarizing in {language}")
        summary, used_in, used_out = self.zoom.summarize(text, language, credentials, cancel)
        outputs["summary"].write_text(summary, encoding="utf-8")
        job.summary_input_characters = used_in
        job.summary_output_characters = used_out
        job.summary_path = str(outputs["summary"])
        job.existing_summary_is_stale = False

    @staticmethod
    def _report(job: QueueJob, update: Update, stage: JobState, progress: float, message: str) -> None:
        job.report(stage, progress, message)
        update(job)
