"""Persistent activity and process-close history."""

from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import get_app_data_dir

LOGGER = logging.getLogger(__name__)
HISTORY_FILENAME = "history.json"


@dataclass(slots=True)
class ActivityEvent:
    """One user-visible activity timeline entry."""

    id: str
    timestamp: float
    kind: str
    title: str
    detail: str
    severity: str = "info"


@dataclass(slots=True)
class ClosedProcessEntry:
    """One process closure that can be shown in history."""

    id: str
    timestamp: float
    pid: int
    name: str
    exe: str
    reason: str
    mode: str
    restored_at: float | None = None
    restore_error: str = ""


@dataclass(slots=True)
class HistoryState:
    """Serialized history payload."""

    events: list[ActivityEvent] = field(default_factory=list)
    closed_processes: list[ClosedProcessEntry] = field(default_factory=list)


class HistoryManager:
    """Stores activity events and recently closed processes in JSON."""

    def __init__(self, path: Path | None = None, max_events: int = 300, max_closed: int = 100) -> None:
        self.path = path or get_app_data_dir() / HISTORY_FILENAME
        self.max_events = max_events
        self.max_closed = max_closed
        self.state = self._load()

    def add_event(self, kind: str, title: str, detail: str, severity: str = "info") -> ActivityEvent:
        """Append an activity timeline event and persist it."""

        event = ActivityEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            kind=kind,
            title=title,
            detail=detail,
            severity=severity,
        )
        self.state.events.insert(0, event)
        del self.state.events[self.max_events :]
        self.save()
        LOGGER.info("Activity event: %s - %s", title, detail)
        return event

    def add_closed_process(
        self,
        pid: int,
        name: str,
        exe: str,
        reason: str,
        mode: str,
    ) -> ClosedProcessEntry:
        """Record a process closure and add a matching activity event."""

        entry = ClosedProcessEntry(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            pid=pid,
            name=name,
            exe=exe,
            reason=reason,
            mode=mode,
        )
        self.state.closed_processes.insert(0, entry)
        del self.state.closed_processes[self.max_closed :]
        self.add_event(
            "process_closed",
            f"Closed {name}",
            f"PID {pid}. Reason: {reason}. Unsaved work cannot be restored automatically.",
            "warning",
        )
        self.save()
        return entry

    def restore_process(self, entry_id: str) -> tuple[bool, str]:
        """Restart an executable from a history entry."""

        entry = self.get_closed_process(entry_id)
        if entry is None:
            return False, "History entry not found"
        if not entry.exe:
            return False, "Executable path is unknown"
        exe_path = Path(entry.exe)
        if not exe_path.exists():
            return False, f"Executable not found: {entry.exe}"

        try:
            subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))
        except OSError as exc:
            entry.restore_error = str(exc)
            self.save()
            LOGGER.warning("Failed to restore process from history: %s", exc)
            return False, str(exc)

        entry.restored_at = time.time()
        entry.restore_error = ""
        self.add_event("process_restored", f"Opened {entry.name}", entry.exe, "success")
        self.save()
        return True, "Application started"

    def get_events(self) -> list[ActivityEvent]:
        """Return events newest first."""

        return list(self.state.events)

    def get_closed_processes(self) -> list[ClosedProcessEntry]:
        """Return process history newest first."""

        return list(self.state.closed_processes)

    def get_closed_process(self, entry_id: str) -> ClosedProcessEntry | None:
        """Find one process history entry."""

        for entry in self.state.closed_processes:
            if entry.id == entry_id:
                return entry
        return None

    def save(self) -> None:
        """Persist current state atomically."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "events": [asdict(event) for event in self.state.events],
            "closed_processes": [asdict(entry) for entry in self.state.closed_processes],
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def _load(self) -> HistoryState:
        if not self.path.exists():
            return HistoryState()
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            events = [ActivityEvent(**item) for item in raw.get("events", []) if isinstance(item, dict)]
            closed = [
                ClosedProcessEntry(**item)
                for item in raw.get("closed_processes", [])
                if isinstance(item, dict)
            ]
            return HistoryState(events=events[: self.max_events], closed_processes=closed[: self.max_closed])
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            LOGGER.warning("Failed to load history from %s: %s", self.path, exc)
            return HistoryState()
