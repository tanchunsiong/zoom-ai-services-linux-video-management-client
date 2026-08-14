from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import jwt

from zscribe import vtt
from zscribe.estimators import cost_comparison, learn_time_calibration, time_comparison
from zscribe.media import audio_profile, basic_validation_error, discover, segment_duration
from zscribe.models import (
    Credentials,
    Cue,
    JobState,
    MediaProbe,
    QueueJob,
    translation_route,
)
from zscribe.pipeline import MediaPipeline, apply_existing, sidecars
from zscribe.models import Settings
from zscribe.storage import AppPaths, Store
from zscribe.zoom import ZoomClient, create_token, normalize_summary


class VTTTests(unittest.TestCase):
    def test_round_trip_multiline_and_hours(self) -> None:
        original = [
            Cue(1, 0.125, 2.5, "Hello\nworld"),
            Cue(2, 3661.001, 3662.25, "After an hour"),
        ]
        parsed = vtt.parse(vtt.write(original))
        self.assertEqual([cue.text for cue in parsed], ["Hello\nworld", "After an hour"])
        self.assertAlmostEqual(parsed[1].start, 3661.001)

    def test_parser_ignores_bad_blocks(self) -> None:
        self.assertEqual(vtt.parse("WEBVTT\n\nnot a cue\n"), [])


class ModelTests(unittest.TestCase):
    def test_both_live_source_is_persisted_and_invalid_values_are_clamped(self) -> None:
        both = Settings.from_dict({"live_source": "both"})
        invalid = Settings.from_dict({"live_source": "anything"})
        self.assertEqual(both.live_source, "both")
        self.assertEqual(invalid.live_source, "microphone")

    def test_non_english_translation_pivots_through_english(self) -> None:
        self.assertEqual(
            translation_route("ja-JP", "it-IT"),
            [("ja-JP", "en-US"), ("en-US", "it-IT")],
        )
        self.assertEqual(translation_route("en-US", "zh-CN"), [("en-US", "zh-CN")])
        self.assertEqual(translation_route("en-US", "en-US"), [])

    def test_queue_job_reports_bounded_progress(self) -> None:
        job = QueueJob("/tmp/media.mp4")
        job.report(JobState.PREPARING, 2, "Working")
        self.assertEqual(job.progress, 1)
        self.assertEqual(job.events[-1]["message"], "Working")


class EstimateTests(unittest.TestCase):
    def test_cost_is_itemized_for_all_enabled_zoom_services(self) -> None:
        job = QueueJob(
            "/tmp/meeting.mp4", "ja-JP", "zh-CN", True,
            duration_seconds=120, has_audio=True,
        )
        costs = cost_comparison(job, Settings())
        self.assertAlmostEqual(costs.estimate.scribe, 0.0066)
        self.assertGreater(costs.estimate.translate, 0)
        self.assertGreater(costs.estimate.summarize, 0)
        self.assertIsNone(costs.actual.total)

    def test_time_estimates_learn_from_completed_queue_items(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        completed = QueueJob(
            "/tmp/complete.mp4", duration_seconds=60, has_audio=True,
            started_at=start.isoformat(), completed_at=(start + timedelta(seconds=20)).isoformat(),
            state=JobState.READY.value,
            events=[
                {"at": start.isoformat(), "stage": "preparing", "message": "Preparing"},
                {"at": (start + timedelta(seconds=20)).isoformat(), "stage": "ready", "message": "Ready"},
            ],
        )
        queued = QueueJob("/tmp/next.mp4", duration_seconds=120, has_audio=True)
        calibration = learn_time_calibration([completed, queued])
        estimate = time_comparison(queued, Settings(), calibration).estimate
        self.assertEqual(calibration.scribe.samples, 1)
        self.assertEqual(estimate.scribe, 20)


class StorageTests(unittest.TestCase):
    def test_credentials_are_mode_0600_and_jobs_recover(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            paths = AppPaths(Path(root) / "data", Path(root) / "cache")
            store = Store(paths)
            store.save_credentials(Credentials("key", "secret"))
            self.assertEqual(os.stat(paths.credentials).st_mode & 0o777, 0o600)
            job = QueueJob("/tmp/file.mp4", state=JobState.TRANSCRIBING.value)
            store.save_jobs([job])
            loaded = store.load_jobs()[0]
            self.assertEqual(loaded.state, JobState.QUEUED.value)
            self.assertIn("Recovered", loaded.status_message)


class MediaTests(unittest.TestCase):
    def test_discover_is_recursive_and_filters_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "nested").mkdir()
            (directory / "a.mp4").touch()
            (directory / "nested/b.MP3").touch()
            (directory / "ignore.txt").touch()
            self.assertEqual(len(discover([directory])), 2)

    def test_segment_duration_accounts_for_bitrate(self) -> None:
        media = MediaProbe(7200, "mp3", 48000, 2, 128_000, False, 0)
        self.assertEqual(audio_profile(media)[3], True)
        self.assertEqual(segment_duration(media, 900), 900)
        self.assertLess(segment_duration(media, 900, 1_000_000), 900)

    def test_obvious_non_media_downloads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            empty = directory / "empty.mp4"
            empty.touch()
            webpage = directory / "download.mp4"
            webpage.write_text("<!DOCTYPE html><title>Blocked</title>")
            cache = directory / "cache.mp4"
            cache.write_bytes(b"not an mp4 cache record")
            valid = directory / "valid.mp4"
            valid.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)
            self.assertIn("empty", basic_validation_error(empty))
            self.assertIn("web page", basic_validation_error(webpage))
            self.assertIn("header", basic_validation_error(cache))
            self.assertIsNone(basic_validation_error(valid))


class ZoomTests(unittest.TestCase):
    def test_jwt_uses_build_key_and_hs256(self) -> None:
        token = create_token(Credentials("build-key", "secret"))
        payload = jwt.decode(token, "secret", algorithms=["HS256"])
        self.assertEqual(payload["iss"], "build-key")
        self.assertGreater(payload["exp"], payload["iat"])

    def test_summary_normalization_removes_duplicate_heading(self) -> None:
        value = """# Summary

## Topics
Alpha beta gamma delta.

## Topics
Alpha beta gamma delta and epsilon.
"""
        normalized = normalize_summary(value)
        self.assertEqual(normalized.count("## Topics"), 1)
        self.assertIn("epsilon", normalized)

    def test_translation_payload_contract(self) -> None:
        class Response:
            status_code = 200
            headers = {}
            text = ""
            def json(self):
                return {"result": {"translations": {"ja-JP": "こんにちは"}}, "usage": {"input_units": 5, "output_units": 5}}

        class Session:
            def __init__(self):
                self.payload = None
                self.headers = None
            def post(self, _url, *, headers, json=None, data=None, timeout=None):
                self.payload = json
                self.headers = headers
                return Response()

        session = Session()
        client = ZoomClient(session)
        result, used_in, used_out = client.translate_text(
            "hello", "en-US", "ja-JP", Credentials("key", "secret")
        )
        self.assertEqual(result, "こんにちは")
        self.assertEqual((used_in, used_out), (5, 5))
        self.assertEqual(session.payload["config"]["target_languages"], ["ja-JP"])
        self.assertTrue(session.payload["reference_id"].startswith("linux-"))
        self.assertTrue(session.headers["Authorization"].startswith("Bearer "))


class ExistingOutputTests(unittest.TestCase):
    def test_complete_sidecars_restore_ready_job(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "meeting.mp4"
            source.touch()
            outputs = sidecars(source, "zh-CN")
            outputs["original"].write_text(vtt.write([Cue(1, 0, 1, "Hello")]), encoding="utf-8")
            outputs["translated"].write_text(vtt.write([Cue(1, 0, 1, "你好")]), encoding="utf-8")
            outputs["summary"].write_text("# Summary", encoding="utf-8")
            job = apply_existing(QueueJob(str(source), "en-US", "zh-CN", True))
            self.assertEqual(job.state, JobState.READY.value)
            self.assertTrue(job.can_review)

    def test_stale_sidecars_are_not_restored_after_language_change(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "meeting.mp4"
            source.touch()
            outputs = sidecars(source, "")
            outputs["original"].write_text(
                vtt.write([Cue(1, 0, 1, "Old language")]), encoding="utf-8"
            )
            job = apply_existing(QueueJob(
                str(source), "ja-JP", existing_transcript_is_stale=True,
            ))
            self.assertEqual(job.state, JobState.QUEUED.value)
            self.assertIsNone(job.original_vtt_path)

    def test_pipeline_reuses_caption_without_media_tools_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            source = directory / "meeting.mp4"
            source.touch()
            outputs = sidecars(source, "")
            outputs["original"].write_text(
                vtt.write([Cue(1, 0, 1, "Already done")]), encoding="utf-8"
            )

            class NoNetwork:
                def __getattr__(self, _name):
                    raise AssertionError("Existing captions must not call Zoom")

            paths = AppPaths(directory / "data", directory / "cache")
            updates = []
            result = MediaPipeline(paths, NoNetwork()).process(
                QueueJob(str(source)),
                Settings(ffmpeg_path="missing", ffprobe_path="missing"),
                Credentials("key", "secret"),
                lambda job: updates.append(job.state),
                Event(),
            )
            self.assertEqual(result.state, JobState.READY.value)
            self.assertTrue(result.reuse_existing_transcript)
            self.assertIn(JobState.READY.value, updates)


if __name__ == "__main__":
    unittest.main()
