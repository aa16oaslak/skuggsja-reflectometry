"""What a running sweep's own communication shows, for the live view.

Written by the sweep thread as it talks to the hardware, read by the GUI
thread. Nothing here talks to hardware itself."""
from __future__ import annotations

import threading
from dataclasses import dataclass

from . import clock


class LinkMonitor:
    """When one device last answered, and the error if its latest call
    failed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.last_ok: float | None = None  # clock time
            self.error: str | None = None

    def ok(self) -> None:
        with self._lock:
            self.last_ok = clock.time()
            self.error = None

    def failed(self, exc: BaseException) -> None:
        with self._lock:
            self.error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> tuple[float | None, str | None]:
        with self._lock:
            return self.last_ok, self.error


@dataclass(frozen=True)
class ScanSnapshot:
    started: int  # scans begun so far in this sweep
    active: bool  # a scan is under way
    freq: float | None  # GHz, latest frequency set
    points: int  # points measured in the current scan
    total: int  # points the current scan will measure


class ScanProgress:
    """Where the TOptica scans are: how many have started, and the
    frequency and point count of the current one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._snap = ScanSnapshot(0, False, None, 0, 0)

    def begin(self, total: int) -> None:
        with self._lock:
            self._snap = ScanSnapshot(self._snap.started + 1, True, None, 0, total)

    def at(self, freq: float) -> None:
        with self._lock:
            s = self._snap
            self._snap = ScanSnapshot(s.started, s.active, float(freq), s.points, s.total)

    def measured(self) -> None:
        with self._lock:
            s = self._snap
            self._snap = ScanSnapshot(s.started, s.active, s.freq, s.points + 1, s.total)

    def end(self) -> None:
        with self._lock:
            s = self._snap
            self._snap = ScanSnapshot(s.started, False, s.freq, s.points, s.total)

    def snapshot(self) -> ScanSnapshot:
        with self._lock:
            return self._snap
