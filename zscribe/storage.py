from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import Credentials, QueueJob, Settings


class AppPaths:
    def __init__(self, data_home: Path | None = None, cache_home: Path | None = None):
        data_root = data_home or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        cache_root = cache_home or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.data = data_root / "zscribe"
        self.cache = cache_root / "zscribe"
        self.work = self.cache / "work"
        self.queue = self.data / "queue.json"
        self.settings = self.data / "settings.json"
        self.credentials = self.data / "credentials.json"
        self.live_diagnostics = self.data / "live-diagnostics.log"
        self.data.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path: Path, fallback: object) -> object:
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError, TypeError):
        return fallback


class Store:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def load_jobs(self) -> list[QueueJob]:
        raw = _load(self.paths.queue, [])
        jobs = [QueueJob.from_dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        for job in jobs:
            if job.state in {"preparing", "transcribing", "translating", "summarizing"}:
                job.state = "queued"
                job.progress = 0.0
                job.status_message = "Recovered after the previous app session"
        return jobs

    def save_jobs(self, jobs: list[QueueJob]) -> None:
        _atomic_json(self.paths.queue, [job.as_dict() for job in jobs])

    def load_settings(self) -> Settings:
        raw = _load(self.paths.settings, {})
        return Settings.from_dict(raw if isinstance(raw, dict) else {})

    def save_settings(self, settings: Settings) -> None:
        settings.clamp()
        _atomic_json(self.paths.settings, asdict(settings))

    def load_credentials(self) -> Credentials:
        raw = _load(self.paths.credentials, {})
        if not isinstance(raw, dict):
            return Credentials()
        return Credentials(str(raw.get("api_key", "")), str(raw.get("api_secret", "")))

    def save_credentials(self, credentials: Credentials) -> None:
        _atomic_json(self.paths.credentials, asdict(credentials), mode=0o600)
        os.chmod(self.paths.credentials, 0o600)
