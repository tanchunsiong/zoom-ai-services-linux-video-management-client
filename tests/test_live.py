from __future__ import annotations

import unittest
import queue
from array import array

from zscribe.live import (
    PCM16_FRAME_BYTES,
    LiveCallbacks,
    LiveSession,
    audio_capture_pipeline,
    completed_transcript_display,
    completed_transcript_source,
    parse_event,
    parse_vocabulary,
    session_update,
)
from zscribe.models import Credentials


class LiveTests(unittest.TestCase):
    def test_both_capture_mode_mixes_microphone_and_loopback(self) -> None:
        pipeline = audio_capture_pipeline(
            "both", "alsa_input.usb-mic", "alsa_output.hdmi.monitor"
        )
        self.assertIn("audiomixer name=mix", pipeline)
        self.assertIn('pulsesrc device="alsa_input.usb-mic"', pipeline)
        self.assertIn('pulsesrc device="alsa_output.hdmi.monitor"', pipeline)
        self.assertEqual(pipeline.count("! mix."), 2)
        self.assertIn("rate=16000,channels=1", pipeline)

    def test_completed_segments_display_newest_first_but_summarize_chronologically(self) -> None:
        segments = ["First point", "Latest point"]
        self.assertEqual(
            completed_transcript_display(segments), "Latest point\n\nFirst point"
        )
        self.assertEqual(
            completed_transcript_source(segments), "First point\n\nLatest point"
        )

    def test_vocabulary_can_be_extracted_from_full_envelope(self) -> None:
        value = '{"config":{"vocabulary":{"phrases":[{"phrase":"Zoom"}]}}}'
        self.assertEqual(parse_vocabulary(value)["phrases"][0]["phrase"], "Zoom")

    def test_session_update_mirrors_transition_schema(self) -> None:
        payload = session_update("en-US", '{"phrases":[]}')
        self.assertEqual(payload["input_audio_format"], "pcm16")
        self.assertEqual(payload["config"]["language"], "en-US")
        self.assertIn("vocabulary", payload["config"])
        self.assertIn("vocabulary", payload)

    def test_nested_transcript_event(self) -> None:
        event = parse_event('{"type":"transcript.delta","result":{"text":"hello"}}')
        self.assertEqual(event["transcript"], "hello")
        self.assertTrue(event["delta"])
        self.assertEqual(event["raw"]["result"]["text"], "hello")

    def test_zoom_live_speech_event_shape(self) -> None:
        event = parse_event(
            '{"type":"input_audio_buffer.speech_started","audio_start_ms":100}'
        )
        self.assertTrue(event["type"].endswith("speech_started"))

    def test_live_frame_size_is_one_tenth_second_pcm16(self) -> None:
        self.assertEqual(PCM16_FRAME_BYTES, 3200)

    def test_arbitrary_capture_buffers_are_assembled_into_100ms_frames(self) -> None:
        callbacks = LiveCallbacks(lambda _event: None, lambda _level: None, lambda _state: None, lambda _error: None)
        session = LiveSession(Credentials("key", "secret"), "en-US", "", callbacks)
        session._frame(b"\x00" * 1000)
        session._frame(b"\x01" * 3000)
        frame = session.frames.get_nowait()
        self.assertEqual(len(frame), PCM16_FRAME_BYTES)
        self.assertEqual(len(session.pending_pcm), 800)
        with self.assertRaises(queue.Empty):
            session.frames.get_nowait()

    def test_automatic_gain_raises_quiet_pcm_without_clipping(self) -> None:
        levels = []
        callbacks = LiveCallbacks(lambda _event: None, levels.append, lambda _state: None, lambda _error: None)
        session = LiveSession(
            Credentials("key", "secret"), "en-US", "", callbacks,
            automatic_gain=True,
        )
        quiet = array("h", [500] * (PCM16_FRAME_BYTES // 2)).tobytes()
        for _ in range(5):
            session._frame(quiet)
        samples = array("h")
        samples.frombytes(session.frames.get_nowait())
        self.assertGreater(max(samples), 500)
        self.assertLessEqual(max(samples), 32767)
        self.assertGreater(levels[-1], 500 / 32768)

    def test_stop_queues_partial_audio_before_shutdown_marker(self) -> None:
        callbacks = LiveCallbacks(lambda _event: None, lambda _level: None, lambda _state: None, lambda _error: None)
        session = LiveSession(Credentials("key", "secret"), "en-US", "", callbacks)
        session._frame(b"\x01\x00" * 200)
        session.stop()
        self.assertEqual(len(session.frames.get_nowait()), 400)
        self.assertIsNone(session.frames.get_nowait())


if __name__ == "__main__":
    unittest.main()
