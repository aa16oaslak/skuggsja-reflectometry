"""Time source for the stage and TOptica code.

Real runs use the wall clock unchanged. A simulated run shortens every wait
(sleep) by a speed factor and moves the clock ahead by the time it skipped,
so a sweep that takes an hour on the setup finishes in minutes, while
timeouts, settle times, stage travel and the printed time estimates stay in
real-world seconds -- independent of the speed factor.

Assumes one thread at a time waits on this clock (the sweep thread)."""
from __future__ import annotations

import threading
import time as _time
from collections.abc import Iterator
from contextlib import contextmanager

_speed = 1.0
_ahead = 0.0  # seconds this clock is ahead of the wall clock
_lock = threading.Lock()


def time() -> float:
    return _time.time() + _ahead


def sleep(seconds: float) -> None:
    global _ahead
    real = seconds / _speed
    _time.sleep(real)
    if _speed != 1.0:
        with _lock:
            _ahead += seconds - real


def speed() -> float:
    return _speed


def set_speed(factor: float) -> None:
    global _speed
    if factor <= 0:
        raise ValueError("Clock speed must be positive.")
    _speed = float(factor)


def hms(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


@contextmanager
def accelerated(factor: float) -> Iterator[None]:
    previous = _speed
    set_speed(factor)
    try:
        yield
    finally:
        set_speed(previous)
