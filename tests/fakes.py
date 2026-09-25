"""Test doubles standing in for hardware the CI box doesn't have."""
from __future__ import annotations

from types import SimpleNamespace

import libximc.highlevel as ximc


class FakeAxis:
    """Duck-types just enough of libximc.highlevel.Axis for stages.py.

    Position is tracked synchronously (command_move_calb/command_movr_calb
    apply immediately), and get_status() always reports "stopped", so
    wait_for_stop() returns on its first poll instead of sleeping.
    """

    def __init__(self, position: float = 0):
        self.position = position
        self.calls: list[tuple] = []
        self.edges = SimpleNamespace(
            LeftBorder=-1_000_000,
            RightBorder=1_000_000,
            BorderFlags=ximc.BorderFlags(0),
            EnderFlags=0,
        )
        self.move_settings = SimpleNamespace(Speed=0, Accel=0)
        self.engine_settings = SimpleNamespace(MicrostepMode=9)
        self.calibration = None

    def get_position_calb(self):
        return SimpleNamespace(Position=self.position)

    def command_move_calb(self, target):
        self.calls.append(("move_calb", target))
        self.position = target

    def command_movr_calb(self, delta):
        self.calls.append(("movr_calb", delta))
        self.position += delta

    def get_status(self):
        return SimpleNamespace(MvCmdSts=ximc.MvcmdStatus(0))  # always "stopped"

    def command_stop(self):
        self.calls.append(("stop",))

    def get_move_settings(self):
        return self.move_settings

    def set_move_settings(self, settings):
        self.move_settings = settings

    def get_edges_settings(self):
        return self.edges

    def set_edges_settings(self, edges):
        self.edges = edges

    def get_engine_settings(self):
        return self.engine_settings

    def set_calb(self, res, microstep_mode):
        self.calibration = (res, microstep_mode)

    def command_homezero(self):
        self.calls.append(("homezero",))
        self.position = 0

    def close_device(self):
        self.calls.append(("close",))

    def open_device(self):
        self.calls.append(("open",))


class ImmediateThread:
    """Stand-in for threading.Thread that runs its target synchronously on
    .start(), so sweep and stop-command side effects happen deterministically
    instead of racing the main test thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False  # already finished by the time start() returns

    def join(self, *_args, **_kwargs):
        pass
