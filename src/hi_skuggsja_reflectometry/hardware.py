"""Is the hardware there and ready?

Read-only checks of the two rotation stages and the TOptica (they open,
read status and close; nothing moves and no setting changes), and a stage
wrapper that reports on a running sweep's own calls without adding any."""
from __future__ import annotations

import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from . import geometry, toptica
from .monitoring import LinkMonitor
from .stages import MVCMD_ERROR, MVCMD_RUNNING, HardwareUnavailable, _load_ximc

if TYPE_CHECKING:
    from .config import AppConfig, StageConfig, TopticaConfig
    from .simulation import SimRig

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
RECEIVER, SAMPLE, TOPTICA = "Receiver stage R1", "Sample stage R2", "TOptica"

# libximc status Flags (StateFlags), as plain ints like the flags in stages.py
STATE_ERRC, STATE_ERRD, STATE_ERRV = 0x1, 0x2, 0x4
STATE_IS_HOMED = 0x20
STATE_ALARM = 0x40
STATE_POWER_OVERHEAT = 0x100
STATE_CONTROLLER_OVERHEAT = 0x200
STATE_OVERLOAD_POWER_VOLTAGE = 0x400
STATE_OVERLOAD_POWER_CURRENT = 0x800
STATE_LOW_POWER_VOLTAGE = 0x10000
STATE_H_BRIDGE_FAULT = 0x20000

_FAIL_FLAGS = (
    (STATE_ALARM, "controller is in ALARM state and will not move until it is cleared"),
    (STATE_LOW_POWER_VOLTAGE, "motor power supply is off or too low"),
    (STATE_H_BRIDGE_FAULT, "motor driver fault"),
    (STATE_POWER_OVERHEAT | STATE_CONTROLLER_OVERHEAT, "overheated"),
    (STATE_OVERLOAD_POWER_VOLTAGE | STATE_OVERLOAD_POWER_CURRENT, "power overload"),
)
_WARN_FLAGS = ((STATE_ERRC | STATE_ERRD | STATE_ERRV, "controller reported a command or data error"),)
_LOCKIN_PARAMS = (
    "lockin:mod-out-amplitude",
    "lockin:mod-out-amplitude-default",
    "lockin:mod-out-offset",
    "lockin:mod-out-offset-default",
    "lockin:amplifier-gain",
)


@dataclass(frozen=True)
class DeviceStatus:
    name: str
    address: str  # COM port, host:port, or "simulated"
    state: str  # OK, WARN, FAIL or UNKNOWN
    summary: str  # one line
    details: list[str] = field(default_factory=list)


# -- read-only checks ----------------------------------------------------------------


def inspect_stage(axis: Any, name: str, address: str, large: bool, cfg: StageConfig) -> DeviceStatus:
    """Status of an open, calibrated stage, read without moving it."""
    status = axis.get_status()
    flags = int(getattr(status, "Flags", 0))
    position = axis.get_position_calb().Position
    angle = geometry.receiver_angle(position, cfg.zero_l) if large else geometry.sample_angle(position, cfg.zero_s)

    fails = [text for bit, text in _FAIL_FLAGS if flags & bit]
    warns = [text for bit, text in _WARN_FLAGS if flags & bit]
    if not flags & STATE_IS_HOMED:
        warns.append("not homed since the controller was switched on, so angles may be off (use --set-zero)")
    if int(status.MvCmdSts) & MVCMD_RUNNING:
        warns.append("moving right now")
    if large and not cfg.angle_min - 0.01 <= angle <= cfg.angle_max + 0.01:
        warns.append(f"outside its soft limits {cfg.angle_min:g}°-{cfg.angle_max:g}°")

    details = [f"Position {angle:.2f}°"]
    if getattr(status, "Upwr", None) is not None:
        details.append(f"Motor supply {status.Upwr / 100:.1f} V")
    try:
        details.append(f"Serial number {axis.get_serial_number()}")
    except Exception:  # noqa: BLE001, S110 -- nice to have, never a reason to fail
        pass

    problems = fails or warns
    summary = f"at {angle:.2f}°" + (f"; {problems[0]}" if problems else ", ready")
    return DeviceStatus(name, address, FAIL if fails else WARN if warns else OK, summary, fails + warns + details)


def probe_stage(name: str, uri: str, large: bool, cfg: StageConfig) -> DeviceStatus:
    """Opens the stage at `uri`, reads its status and closes it again."""
    address = port_name(uri)
    try:
        ximc = _load_ximc()
    except HardwareUnavailable as e:
        return DeviceStatus(name, address, FAIL, "Standa driver (libximc) not available", [str(e)])
    axis = ximc.Axis(uri)
    try:
        axis.open_device()
    except Exception as e:  # noqa: BLE001 -- any failure to open means not usable
        return DeviceStatus(
            name, address, FAIL,
            f"cannot open {address}: not connected, switched off, or in use by another program",
            [f"{uri}: {str(e).splitlines()[0] if str(e) else type(e).__name__}"],
        )
    try:
        res = cfg.res_large if large else cfg.res_small
        axis.set_calb(res, axis.get_engine_settings().MicrostepMode)  # library-side units only
        return inspect_stage(axis, name, address, large, cfg)
    except Exception as e:  # noqa: BLE001
        return DeviceStatus(name, address, FAIL, "opened, but reading its status failed", [f"{type(e).__name__}: {e}"])
    finally:
        try:
            axis.close_device()
        except Exception:  # noqa: BLE001, S110 -- the status is already known
            pass


def probe_toptica(cfg: TopticaConfig, timeout: float = 3.0) -> DeviceStatus:
    """Connects the way a scan does, reads the lock-in settings and the
    frequency, and disconnects. Changes nothing on the instrument."""
    address = f"{cfg.host}:{cfg.port}"
    if _is_placeholder(cfg.host):
        return DeviceStatus(
            TOPTICA, address, FAIL, "no instrument address configured",
            ["Set host under [toptica] in reflecto.toml ('reflecto config init' makes one)."],
        )
    try:
        sock = socket.create_connection((cfg.host, cfg.port), timeout=timeout)
    except OSError as e:
        return DeviceStatus(
            TOPTICA, address, FAIL, f"no answer at {address}",
            [str(e), "Is it switched on and on the network? Check host and port in reflecto.toml."],
        )
    with sock:
        try:
            toptica.read_until_prompt(sock)
            toptica.send_command(sock, "(exec 'change-ul 3 \"\")")  # as scan() does, for this connection
            values = [toptica.get_float(sock, param) for param in _LOCKIN_PARAMS]
            freq = toptica.get_float(sock, "frequency:frequency-act")
        except (OSError, ValueError) as e:
            return DeviceStatus(
                TOPTICA, address, FAIL, "connected, but it did not answer as expected", [f"{type(e).__name__}: {e}"]
            )
    amp, amp_default, offset, offset_default, gain = values
    details = [f"Frequency {freq:.2f} GHz", f"Lock-in amplitude {amp:g}, offset {offset:g}, gain {gain:g}"]
    problems = toptica.lockin_problems(amp, amp_default, offset, offset_default, gain, cfg)
    if problems:
        return DeviceStatus(
            TOPTICA, address, FAIL, "lock-in settings are off, so a scan would refuse to start", problems + details
        )
    return DeviceStatus(TOPTICA, address, OK, f"at {freq:.2f} GHz, ready", details)


def check_hardware(cfg: AppConfig) -> list[DeviceStatus]:
    """Checks both stages and the TOptica, reading only: receiver, sample,
    TOptica, in that order. The stages are checked one after the other, the
    TOptica meanwhile."""
    st = cfg.stages
    with ThreadPoolExecutor(max_workers=1) as pool:
        instrument = pool.submit(probe_toptica, cfg.toptica)
        receiver = probe_stage(RECEIVER, st.device_uri_large, True, st)
        sample = probe_stage(SAMPLE, st.device_uri_small, False, st)
        return [receiver, sample, instrument.result()]


def check_simulated(rig: SimRig) -> list[DeviceStatus]:
    """The same checks against a simulated setup."""
    st = rig.cfg.stages
    statuses = [
        inspect_stage(rig.large, RECEIVER, "simulated", True, st),
        inspect_stage(rig.small, SAMPLE, "simulated", False, st),
        probe_toptica(rig.cfg.toptica),
    ]
    return [replace(s, address="simulated") for s in statuses]


def unchecked(cfg: AppConfig, simulated: bool, summary: str) -> list[DeviceStatus]:
    """Placeholder statuses (state UNKNOWN) for the three devices."""
    st = cfg.stages
    addresses = (
        ["simulated"] * 3
        if simulated
        else [port_name(st.device_uri_large), port_name(st.device_uri_small), f"{cfg.toptica.host}:{cfg.toptica.port}"]
    )
    return [DeviceStatus(name, a, UNKNOWN, summary) for name, a in zip((RECEIVER, SAMPLE, TOPTICA), addresses)]


def all_usable(statuses: list[DeviceStatus]) -> bool:
    return all(s.state in (OK, WARN) for s in statuses)


def format_statuses(statuses: list[DeviceStatus]) -> str:
    tags = {OK: " ok ", WARN: "WARN", FAIL: "FAIL", UNKNOWN: " ?? "}
    width = max(len(f"{s.name} ({s.address})") for s in statuses)
    lines = []
    for s in statuses:
        lines.append(f"  [{tags[s.state]}] {f'{s.name} ({s.address})':<{width}}  {s.summary}")
        if s.state in (WARN, FAIL):
            lines.extend(f"         {'':<{width}}  - {d}" for d in s.details)
    return "\n".join(lines)


def port_name(uri: str) -> str:
    """'xi-com:\\\\.\\COM3' -> 'COM3'."""
    return uri.replace("\\", "/").rsplit("/", 1)[-1]


def _is_placeholder(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("192.0.2.0/24")  # the packaged default
    except ValueError:  # a hostname
        return False


# -- live status from a running sweep -------------------------------------------------


class WatchedAxis:
    """Wraps a stage axis. Every call goes to the stage unchanged; the
    wrapper notes whether it succeeded and, from the replies, where the
    stage is and whether it is moving. It never makes calls of its own.

    Wrap the axis after it is calibrated: its calibration (read with
    get_calb(), which the library answers without asking the stage) turns
    the raw position in status replies into degrees."""

    def __init__(self, axis: Any) -> None:
        self.inner = axis
        self.link = LinkMonitor()
        degrees_per_step, microstep_mode = axis.get_calb()
        self._steps_to_deg = degrees_per_step
        self._usteps_per_step = 2 ** (int(microstep_mode) - 1)
        self._lock = threading.Lock()
        self.position: float | None = None  # calibrated units (degrees)
        self.moving = False
        self.error_flag = False  # the controller flagged the last move command as failed
        self.homing = False

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if name == "command_homezero":
                self._set(homing=True, moving=True)
            try:
                result = attr(*args, **kwargs)
            except Exception as e:
                self.link.failed(e)
                raise
            finally:
                if name == "command_homezero":
                    self._set(homing=False, moving=False)
            self.link.ok()
            self._observe(name, args, result)
            return result

        return call

    def _set(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def _observe(self, name: str, args: tuple, result: Any) -> None:
        if name == "get_status":
            sts = int(result.MvCmdSts)
            values: dict[str, Any] = {"moving": bool(sts & MVCMD_RUNNING), "error_flag": bool(sts & MVCMD_ERROR)}
            if hasattr(result, "CurPosition") and hasattr(result, "uCurPosition"):
                steps = result.CurPosition + result.uCurPosition / self._usteps_per_step
                values["position"] = self._steps_to_deg * steps
            self._set(**values)
        elif name == "get_position_calb":
            self._set(position=result.Position)
        elif name == "command_homezero":
            self._set(position=0.0)
        elif name in ("command_move_calb", "command_movr_calb"):
            self._set(moving=True)
        elif name == "command_stop":
            self._set(moving=False)

    def snapshot(self) -> tuple[float | None, bool, bool, bool]:
        """(position, moving, error_flag, homing)"""
        with self._lock:
            return self.position, self.moving, self.error_flag, self.homing
