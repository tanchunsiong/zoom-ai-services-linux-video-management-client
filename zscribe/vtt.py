from __future__ import annotations

import re

from .models import Cue


_TIMESTAMP = re.compile(
    r"(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{3})"
)


def _seconds(value: str) -> float:
    match = _TIMESTAMP.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Invalid WebVTT timestamp: {value}")
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"]) / 1000
    )


def _timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    return f"{milliseconds // 3_600_000:02d}:{milliseconds // 60_000 % 60:02d}:{milliseconds // 1000 % 60:02d}.{milliseconds % 1000:03d}"


def parse(text: str) -> list[Cue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", normalized)
    cues: list[Cue] = []
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        try:
            start_text, end_text = lines[timing_index].split("-->", 1)
            end_text = end_text.strip().split()[0]
            body = "\n".join(lines[timing_index + 1 :]).strip()
            if body:
                cues.append(Cue(len(cues) + 1, _seconds(start_text), _seconds(end_text), body))
        except (ValueError, IndexError):
            continue
    return cues


def write(cues: list[Cue]) -> str:
    blocks = ["WEBVTT"]
    for index, cue in enumerate(cues, 1):
        blocks.append(
            f"{index}\n{_timestamp(cue.start)} --> {_timestamp(max(cue.end, cue.start + 0.001))}\n{cue.text.strip()}"
        )
    return "\n\n".join(blocks) + "\n"
