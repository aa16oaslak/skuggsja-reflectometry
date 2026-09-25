"""Simulated hardware for test sweeps: both rotation stages and the TOptica,
so a sweep runs end to end on any computer with nothing connected.

Only the hardware is simulated. The sweep, stage and TOptica code run
unchanged: the stages are stand-ins for libximc.highlevel.Axis, and the
TOptica is a small server on localhost speaking the instrument's text
protocol. The photocurrent is made up, just plausible enough to look at."""
from __future__ import annotations

import math
import random
import socket
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from . import clock, geometry
from .config import AppConfig, TopticaConfig
from .stages import (
    BORDER_STOP_LEFT,
    BORDER_STOP_RIGHT,
    MVCMD_ERROR,
    MVCMD_RUNNING,
    configure_stages,
)
from .sweeps import PlannedStep

# Guesses: the real speeds come from settings stored in the stage controllers.
RECEIVER_SPEED = 5.0  # degrees per second
SAMPLE_SPEED = 10.0
# Where the simulated stages "were left" before the run, in degrees.
START_RECEIVER_ANGLE = 90.0
START_SAMPLE_ANGLE = 0.0


class SimAxis:
    """Stand-in for libximc.highlevel.Axis, covering the calls this package
    makes, in calibrated units (degrees).

    Moves take clock time at a fixed speed. As on the controller, a move
    that would cross an active soft limit stops at the limit and flags an
    error, and command_stop() halts immediately, including a homing."""

    def __init__(self, label: str, speed: float, position: float) -> None:
        self.label = label
        self.speed = speed
        self.moves = 0
        self.border_hits: list[tuple[float, float]] = []  # (target, where it stopped)
        self._lock = threading.RLock()
        self._start = self._target = float(position)
        self._t_start = self._t_end = clock.time()
        self._error = False
        self._stop_requested = False
        self._open = False
        self._calb = 1.0  # degrees per full step, from set_calb()
        self._edges = SimpleNamespace(
            LeftBorder=-(10**9), RightBorder=10**9, BorderFlags=0, EnderFlags=0
        )
        self._move_settings = SimpleNamespace(Speed=0, Accel=0)

    # -- state, readable from any thread, open or not ---------------------------

    @property
    def position(self) -> float:
        with self._lock:
            return self._position_at(clock.time())

    @property
    def moving(self) -> bool:
        with self._lock:
            return clock.time() < self._t_end

    def _position_at(self, t: float) -> float:
        if t >= self._t_end:
            return self._target
        return self._start + (self._target - self._start) * (t - self._t_start) / (self._t_end - self._t_start)

    def _go(self, target: float, respect_borders: bool = True) -> None:
        with self._lock:
            now = clock.time()
            here = self._position_at(now)
            stop_at = target
            if respect_borders:
                flags = int(self._edges.BorderFlags)
                if flags & BORDER_STOP_LEFT:
                    stop_at = max(stop_at, self._edges.LeftBorder * self._calb)
                if flags & BORDER_STOP_RIGHT:
                    stop_at = min(stop_at, self._edges.RightBorder * self._calb)
            self._error = abs(stop_at - target) > 1e-9
            if self._error:
                self.border_hits.append((target, stop_at))
            self._start, self._target = here, stop_at
            self._t_start, self._t_end = now, now + abs(stop_at - here) / self.speed
            self.moves += 1

    def _check_open(self) -> None:
        if not self._open:
            raise RuntimeError(f"Simulated {self.label} stage: device is not open")

    # -- the libximc.highlevel.Axis calls this package uses ----------------------

    def open_device(self) -> None:
        self._open = True

    def close_device(self) -> None:
        self._open = False

    def set_calb(self, A: float, MicrostepMode: int) -> None:
        self._calb = A

    def get_engine_settings(self) -> SimpleNamespace:
        return SimpleNamespace(MicrostepMode=9)

    def get_edges_settings(self) -> SimpleNamespace:
        self._check_open()
        return SimpleNamespace(**vars(self._edges))

    def set_edges_settings(self, edges) -> None:
        self._check_open()
        self._edges = SimpleNamespace(**vars(edges))

    def get_move_settings(self) -> SimpleNamespace:
        return SimpleNamespace(**vars(self._move_settings))

    def set_move_settings(self, settings) -> None:
        self._move_settings = SimpleNamespace(**vars(settings))

    def get_position_calb(self) -> SimpleNamespace:
        self._check_open()
        return SimpleNamespace(Position=self.position, EncPosition=0.0)

    def get_status(self) -> SimpleNamespace:
        self._check_open()
        with self._lock:
            sts = MVCMD_RUNNING if clock.time() < self._t_end else 0
            if self._error:
                sts |= MVCMD_ERROR
        return SimpleNamespace(MvCmdSts=sts)

    def command_move_calb(self, position: float) -> None:
        self._check_open()
        self._go(position)

    def command_movr_calb(self, delta: float) -> None:
        self._check_open()
        with self._lock:
            now = clock.time()
            base = self._target if now < self._t_end else self._position_at(now)
            self._go(base + delta)

    def command_stop(self) -> None:
        self._check_open()
        with self._lock:
            now = clock.time()
            here = self._position_at(now)
            self._start = self._target = here
            self._t_start = self._t_end = now
            self._stop_requested = True

    def command_homezero(self) -> None:
        """Blocks while the stage travels home (calibrated position 0),
        like the real call. If a stop interrupts it, the stage stays where
        it stopped; what the real controller does then is not known."""
        self._check_open()
        with self._lock:
            self._stop_requested = False
            self._go(0.0, respect_borders=False)
        while True:
            with self._lock:
                if self._stop_requested:
                    return
                if clock.time() >= self._t_end:
                    self._error = False
                    return
            clock.sleep(0.02)


@dataclass
class SimScan:
    """What the simulated TOptica saw of one scan (one connection)."""

    sample: float  # stage angles when the scan connected
    receiver: float
    points: int = 0  # lock-in values read
    freq: float | None = None  # latest frequency set, GHz
    done: bool = False


class SimToptica:
    """Answers the TOptica's text protocol on localhost. Each scan() call is
    one connection; the photocurrent depends on the simulated stage angles
    (strong near the specular angle) and on frequency. Made-up numbers."""

    AMPLITUDE = 0.5  # lock-in modulation output amplitude reported (and its default)

    def __init__(self, cfg: TopticaConfig, angles, seed: int = 1) -> None:
        self._cfg = cfg
        self._angles = angles  # () -> (sample, receiver) in degrees
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.scans: list[SimScan] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, name="sim-toptica", daemon=True).start()

    @property
    def current(self) -> SimScan | None:
        with self._lock:
            return self.scans[-1] if self.scans and not self.scans[-1].done else None

    def close(self) -> None:
        self._server.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:  # closed
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        sample, receiver = self._angles()
        record = SimScan(sample, receiver)
        with self._lock:
            self.scans.append(record)
        conn.sendall(b"> ")
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    conn.sendall(f"{self._answer(line.decode().strip(), record)}\n> ".encode())
        except OSError:
            pass
        finally:
            with self._lock:
                record.done = True

    def _answer(self, cmd: str, record: SimScan) -> str:
        body = cmd.strip("()").split()
        if len(body) >= 2 and body[0] == "param-set!":
            if body[1] == "'frequency:frequency-set":
                with self._lock:
                    record.freq = float(body[2])
            return "0"
        if len(body) == 2 and body[0] == "param-ref":
            name = body[1].lstrip("'")
            if name in ("lockin:mod-out-amplitude", "lockin:mod-out-amplitude-default"):
                return str(self.AMPLITUDE)
            if name in ("lockin:mod-out-offset", "lockin:mod-out-offset-default"):
                return "0.0"
            if name == "lockin:amplifier-gain":
                return str(self._cfg.gain_default)
            if name == "frequency:frequency-act":
                return repr((record.freq or 0.0) + self._rng.uniform(-0.002, 0.002))
            if name == "lockin:lock-in-value-nanoamp":
                with self._lock:
                    record.points += 1
                return f"({self._photocurrent(record.freq or 0.0):.4f} #t)"
            return f"Error: simulator does not know parameter {name}"
        if body and body[0] == "exec":
            return "0"
        return f"Error: simulator does not understand {cmd!r}"

    def _photocurrent(self, freq: float) -> float:
        sample, receiver = self._angles()
        lobe = math.exp(-0.5 * ((receiver - 2 * sample) / 3.0) ** 2) + 0.02  # specular lobe + diffuse floor
        envelope = 120.0 * math.exp(-(freq - 70.0) / 150.0)  # source power falls with frequency
        fringes = math.cos(2 * math.pi * freq * 1.3)  # ~1.3 ns path difference
        return envelope * lobe * fringes + self._rng.gauss(0.0, 0.05)


class SimRig:
    """Both simulated stages, calibrated and limited exactly like the real
    ones from the same config, plus the simulated TOptica. `cfg` is the
    config to run the sweep with: identical, except the TOptica address
    points at the simulator."""

    def __init__(self, cfg: AppConfig, output_dir: Path) -> None:
        st = cfg.stages
        self.zero_l, self.zero_s = st.zero_l, st.zero_s
        self.large = SimAxis("receiver", RECEIVER_SPEED, st.zero_l - START_RECEIVER_ANGLE)
        self.small = SimAxis("sample", SAMPLE_SPEED, START_SAMPLE_ANGLE + st.zero_s)
        for axis in (self.large, self.small):
            axis.open_device()
        configure_stages(self.large, self.small, st)
        self.toptica = SimToptica(cfg.toptica, self.angles)
        self.cfg = replace(cfg, toptica=replace(cfg.toptica, host="127.0.0.1", port=self.toptica.port))
        self.output_dir = output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self.t_start = clock.time()

    def angles(self) -> tuple[float, float]:
        """(sample, receiver) angles right now, exactly."""
        return (
            geometry.sample_angle(self.small.position, self.zero_s),
            geometry.receiver_angle(self.large.position, self.zero_l),
        )

    def close(self) -> None:
        self.toptica.close()

    def summary(self, plan: list[PlannedStep], files: list[Path], real_seconds: float) -> str:
        """Planned vs. actual angles for every step, and anything else worth
        a look before running the same sweep on the setup."""
        rows = [
            "",
            "=" * 30 + " Simulation summary (no hardware was used) " + "=" * 30,
            (
                f"{'step':>5}  {'planned sample/receiver':>23}  {'actual at scan':>17}     "
                f"{'phi1':>7} {'phi2':>7} {'points':>7}  file"
            ),
        ]
        mismatches = 0
        for k, (step, path) in enumerate(zip(plan, files)):
            planned = f"{step.sample:8.2f}° / {step.receiver:7.2f}°"
            if k >= len(self.toptica.scans):
                rows.append(f"{k + 1:>2}/{len(plan):<2}  {planned}  not reached (the sweep ended early)")
                continue
            scan = self.toptica.scans[k]
            ok = abs(scan.sample - step.sample) < 0.01 and abs(scan.receiver - step.receiver) < 0.01
            mismatches += not ok
            phi1, phi2 = geometry.phi_angles(scan.sample, scan.receiver)
            saved = _data_lines(path)
            rows.append(
                f"{k + 1:>2}/{len(plan):<2}  {planned}  {scan.sample:7.2f}° / {scan.receiver:7.2f}°  "
                f"{'ok' if ok else 'XX'}  {phi1:6.2f}° {phi2:6.2f}° {saved if saved is not None else '-':>7}  {path}"
            )
        rows.append("")
        if mismatches:
            rows.append(f"XX {mismatches} step(s) scanned at different angles than planned.")
        for axis, to_angle in (
            (self.large, lambda p: geometry.receiver_angle(p, self.zero_l)),
            (self.small, lambda p: geometry.sample_angle(p, self.zero_s)),
        ):
            for target, stopped in axis.border_hits:
                rows.append(
                    f"!! {axis.label} move to {to_angle(target):.4f}° stopped at the soft limit, "
                    f"{to_angle(stopped):.4f}°."
                )
        sim_seconds = clock.time() - self.t_start
        rows.append(
            f"Stage moves: receiver {self.large.moves}, sample {self.small.moves}. "
            f"Estimated time on the setup: {clock.hms(sim_seconds)} "
            f"(simulated in {clock.hms(real_seconds)}, x{clock.speed():g})."
        )
        rows.append(f"Simulated data (fake photocurrent) is in {self.output_dir}/")
        return "\n".join(rows)


def _data_lines(path: Path) -> int | None:
    if not path.exists():
        return None
    return sum(1 for line in path.read_text().splitlines() if line and not line.startswith("#"))
