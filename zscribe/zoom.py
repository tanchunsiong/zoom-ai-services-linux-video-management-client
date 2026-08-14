from __future__ import annotations

import base64
import json
import re
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Any, Callable
from uuid import uuid4

import jwt
import requests

from . import __version__
from .models import AudioPart, Credentials, Cue


SCRIBE_URL = "https://api.zoom.us/v2/aiservices/scribe/transcribe"
TRANSLATE_URL = "https://api.zoom.us/v2/aiservices/translator/translate"
SUMMARIZE_URL = "https://api.zoom.us/v2/aiservices/summarizer/summarize"
LIVE_URL = "wss://api.zoom.us/v2/aiservices/scribe/live"


class CancelledError(Exception):
    pass


class ZoomAPIError(RuntimeError):
    def __init__(self, service: str, status_code: int, body: str):
        self.service = service
        self.status_code = status_code
        self.body = body[:800]
        super().__init__(f"{service} returned HTTP {status_code}. {self.body}")


def create_token(credentials: Credentials, lifetime_seconds: int = 3600) -> str:
    if not credentials.complete:
        raise ValueError("Zoom AI Services credentials are incomplete.")
    now = int(time.time())
    return jwt.encode(
        {"iss": credentials.api_key.strip(), "iat": now - 30, "exp": now + lifetime_seconds},
        credentials.api_secret.strip(),
        algorithm="HS256",
    )


def normalize_summary(text: str) -> str:
    """Remove duplicate sibling sections returned by some full-summary responses."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^\s*#\s+Summary\s*$", line, re.I)), None)
    if start is None:
        return "\n".join(lines).strip()
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*#\s+\S", lines[i])), len(lines))
    starts = [i for i in range(start + 1, end) if re.match(r"^\s*#{2,}\s+\S", lines[i])]
    if len(starts) < 2:
        return "\n".join(lines).strip()
    sections: list[tuple[str, str]] = []
    for position, section_start in enumerate(starts):
        section_end = starts[position + 1] if position + 1 < len(starts) else end
        heading = lines[section_start].strip()
        body = "\n".join(lines[section_start + 1 : section_end]).strip()
        if not body:
            continue
        heading_key = re.sub(r"[^\w]+", " ", heading.lower()).strip()
        words = set(re.findall(r"\w{3,}", body.lower()))
        duplicate = None
        for i, (old_heading, old_body) in enumerate(sections):
            old_key = re.sub(r"[^\w]+", " ", old_heading.lower()).strip()
            old_words = set(re.findall(r"\w{3,}", old_body.lower()))
            similarity = len(words & old_words) / len(words | old_words) if words | old_words else 0
            if heading_key == old_key or similarity >= 0.70:
                duplicate = i
                break
        if duplicate is None:
            sections.append((heading, body))
        elif len(body) > len(sections[duplicate][1]):
            sections[duplicate] = (heading, body)
    rebuilt = lines[: start + 1] + [""]
    for heading, body in sections:
        rebuilt.extend([heading, body, ""])
    rebuilt.extend(lines[end:])
    return "\n".join(rebuilt).strip()


class ZoomClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def _headers(self, credentials: Credentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {create_token(credentials)}",
            "Content-Type": "application/json",
            "User-Agent": f"ZScribeLinux/{__version__}",
        }

    def _request(
        self,
        service: str,
        url: str,
        credentials: Credentials,
        *,
        payload: dict[str, Any] | None = None,
        body_file: Path | None = None,
        cancel: Event | None = None,
    ) -> dict[str, Any]:
        delays = (2, 5)
        for attempt in range(3):
            if cancel and cancel.is_set():
                raise CancelledError()
            try:
                if body_file:
                    with body_file.open("rb") as source:
                        response = self.session.post(
                            url, headers=self._headers(credentials), data=source, timeout=(20, 600)
                        )
                else:
                    response = self.session.post(
                        url, headers=self._headers(credentials), json=payload, timeout=(20, 180)
                    )
            except requests.RequestException:
                if attempt == 2:
                    raise
                self._wait(delays[attempt], cancel)
                continue
            if 200 <= response.status_code < 300:
                value = response.json()
                return value if isinstance(value, dict) else {}
            if response.status_code in {429, 502, 503, 504} and attempt < 2:
                try:
                    retry = float(response.headers.get("Retry-After", delays[attempt]))
                except ValueError:
                    retry = delays[attempt]
                self._wait(min(max(retry, 1), 300), cancel)
                continue
            raise ZoomAPIError(service, response.status_code, response.text)
        raise RuntimeError(f"Could not connect to {service}")

    @staticmethod
    def _wait(seconds: float, cancel: Event | None) -> None:
        if cancel and cancel.wait(seconds):
            raise CancelledError()
        if not cancel:
            time.sleep(seconds)

    def transcribe(
        self, part: AudioPart, language: str, credentials: Credentials, cancel: Event | None = None
    ) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(prefix="zscribe-body-", suffix=".json", delete=False) as output:
            body_path = Path(output.name)
            output.write(b'{"file":"data:')
            output.write(part.mime_type.encode())
            output.write(b";base64,")
            with part.path.open("rb") as audio:
                while chunk := audio.read(48 * 1024):
                    if cancel and cancel.is_set():
                        raise CancelledError()
                    output.write(base64.b64encode(chunk))
            output.write(b'","config":')
            output.write(json.dumps({"language": language, "channel_separation": False}).encode())
            output.write(b"}")
        try:
            root = self._request(
                "Zoom Scribe", SCRIBE_URL, credentials, body_file=body_path, cancel=cancel
            )
        finally:
            body_path.unlink(missing_ok=True)
        result = root.get("result") if isinstance(root.get("result"), dict) else root
        text = next((str(result[key]) for key in ("text_display", "text_lexical", "text") if result.get(key)), "")
        cues: list[Cue] = []
        for segment in result.get("segments", []):
            if not isinstance(segment, dict):
                continue
            body = next((str(segment[key]) for key in ("text_display", "text_lexical", "text") if segment.get(key)), "").strip()
            if not body:
                continue
            start = _number(segment, "start", "start_time", "start_sec")
            end = _number(segment, "end", "end_time", "end_sec")
            cues.append(Cue(len(cues) + 1, start, end if end > start else start + 2, body))
        if not cues and text:
            cues = [Cue(1, 0, max(2, part.duration), text)]
        return {
            "language": language,
            "cues": [asdict(cue) for cue in cues],
            "text": text,
            "request_id": root.get("request_id"),
            "model": root.get("model"),
        }

    def translate_cues(
        self,
        cues: list[Cue],
        source: str,
        target: str,
        credentials: Credentials,
        cancel: Event | None = None,
    ) -> tuple[list[Cue], int, int]:
        translated: dict[int, str] = {}
        input_chars = output_chars = 0
        for batch in _cue_batches(cues, 3600):
            body = "\n".join(f"[[[ZT_CUE_{cue.index:06d}]]] {' '.join(cue.text.split())}" for cue in batch)
            value, used_in, used_out = self.translate_text(body, source, target, credentials, cancel)
            input_chars += used_in
            output_chars += used_out
            for match in re.finditer(
                r"\[\[\[\s*ZT_CUE_(\d{6})\s*\]\]\]\s*([\s\S]*?)(?=\s*\[\[\[\s*ZT_CUE_\d{6}\s*\]\]\]|$)",
                value,
            ):
                translated[int(match.group(1))] = match.group(2).strip()
        for cue in cues:
            if cue.index not in translated:
                value, used_in, used_out = self.translate_text(
                    cue.text, source, target, credentials, cancel
                )
                translated[cue.index] = value
                input_chars += used_in
                output_chars += used_out
        return (
            [Cue(cue.index, cue.start, cue.end, translated.get(cue.index, cue.text)) for cue in cues],
            input_chars,
            output_chars,
        )

    def translate_text(
        self,
        text: str,
        source: str,
        target: str,
        credentials: Credentials,
        cancel: Event | None = None,
    ) -> tuple[str, int, int]:
        root = self._request(
            "Zoom Translator",
            TRANSLATE_URL,
            credentials,
            payload={
                "text": text,
                "config": {"source_language": source, "target_languages": [target]},
                "reference_id": f"linux-{uuid4().hex}",
            },
            cancel=cancel,
        )
        result = root.get("result", {})
        translated = result.get("translations", {}).get(target, "") if isinstance(result, dict) else ""
        usage = root.get("usage", {})
        return str(translated), int(usage.get("input_units", len(text))), int(usage.get("output_units", len(translated)))

    def summarize(
        self, text: str, language: str, credentials: Credentials, cancel: Event | None = None
    ) -> tuple[str, int, int]:
        if not text.strip():
            return "No spoken content was available to summarize.", 0, 0
        chunks = _split_utf8(text, 80 * 1024)
        input_chars = output_chars = 0
        if len(chunks) > 1:
            partials: list[str] = []
            for chunk in chunks:
                partial, used_in, used_out = self._summarize_text(
                    chunk, language, "summary", credentials, cancel
                )
                partials.append(partial)
                input_chars += used_in
                output_chars += used_out
            combined = "\n\n".join(partials)
            while len(combined.encode()) > 80 * 1024:
                reduced: list[str] = []
                for chunk in _split_utf8(combined, 80 * 1024):
                    partial, used_in, used_out = self._summarize_text(
                        chunk, language, "summary", credentials, cancel
                    )
                    reduced.append(partial)
                    input_chars += used_in
                    output_chars += used_out
                next_value = "\n\n".join(reduced)
                if len(next_value) >= len(combined):
                    raise RuntimeError("Transcript is too large for Zoom Summarizer Fast mode.")
                combined = next_value
        else:
            combined = chunks[0]
        final, used_in, used_out = self._summarize_text(
            combined, language, "full_summary", credentials, cancel
        )
        return normalize_summary(final), input_chars + used_in, output_chars + used_out

    def _summarize_text(
        self, text: str, language: str, task: str, credentials: Credentials, cancel: Event | None
    ) -> tuple[str, int, int]:
        root = self._request(
            "Zoom Summarizer",
            SUMMARIZE_URL,
            credentials,
            payload={
                "input": {"text": text},
                "config": {
                    "summary_type": "CONVERSATION",
                    "task": task,
                    "language": language,
                    "output_format": "text",
                },
            },
            cancel=cancel,
        )
        result = root.get("result", {})
        keys = ("full_summary", "summary_text", "text", "recap") if task == "full_summary" else ("summary_text", "text", "full_summary", "recap")
        summary = next((str(result[key]) for key in keys if isinstance(result, dict) and result.get(key)), "")
        usage = root.get("usage", {})
        return summary, int(usage.get("input_units", len(text))), int(usage.get("output_units", len(summary)))


def _number(obj: dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            return float(obj[key])
        except (KeyError, TypeError, ValueError):
            continue
    return 0.0


def _cue_batches(cues: list[Cue], maximum: int) -> list[list[Cue]]:
    result: list[list[Cue]] = []
    current: list[Cue] = []
    size = 0
    for cue in cues:
        item_size = len(cue.text) + 30
        if current and size + item_size > maximum:
            result.append(current)
            current, size = [], 0
        current.append(cue)
        size += item_size
    if current:
        result.append(current)
    return result


def _split_utf8(text: str, maximum: int) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    size = 0
    for character in text:
        count = len(character.encode())
        if current and size + count > maximum:
            result.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += count
    if current:
        result.append("".join(current))
    return result
