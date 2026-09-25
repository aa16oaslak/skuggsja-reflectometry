from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import numpy as np

from .config import TopticaConfig


def build_output_filename(filename: str, int_time: float, start_freq: float, stop_freq: float) -> str:
    return f"{filename}_{int_time}ms_{start_freq}GHz_to_{stop_freq}.txt"


def read_until_prompt(sock: socket.socket) -> str:
    response = b""
    while True:
        chunk = sock.recv(4096)
        response += chunk
        if response.endswith(b"> "):
            break
    return response.decode("utf-8", errors="ignore").strip()


def send_command(sock: socket.socket, cmd: str) -> str:
    sock.sendall((cmd + "\n").encode())
    return read_until_prompt(sock)


def get_float(sock: socket.socket, param: str) -> float:
    """Read a parameter and return it as a float."""
    response = send_command(sock, f"(param-ref '{param})")
    return float(response.split("\n")[0])


def scan(
    start_freq: float,
    stop_freq: float,
    int_time: float,
    filename: str,
    stop_event: threading.Event,
    cfg: TopticaConfig,
    overwrite: bool = False,
) -> None:
    output_path = build_output_filename(filename, int_time, start_freq, stop_freq)
    if not overwrite and Path(output_path).exists():
        raise FileExistsError(
            f"Output file already exists: {output_path} (pass --overwrite to replace it)"
        )

    s = socket.socket()
    s.connect((cfg.host, cfg.port))

    # -- Connect and authenticate --------------------------------------------
    read_until_prompt(s)
    send_command(s, "(exec 'change-ul 3 \"\")")

    # -- Scan parameters ------------------------------------------------------
    FREQ_START = start_freq
    FREQ_STOP = stop_freq
    FREQ_STEP = cfg.freq_step
    INTEGRATION = int_time
    SETTLE_FREQ_TOL = cfg.settle_freq_tol
    SETTLE_TIMEOUT = cfg.settle_timeout
    AMP = get_float(s, "lockin:mod-out-amplitude")
    AMP_DEF = get_float(s, "lockin:mod-out-amplitude-default")
    AMP_TOL = cfg.amp_tol
    OFFSET = get_float(s, "lockin:mod-out-offset")
    OFFSET_DEF = get_float(s, "lockin:mod-out-offset-default")
    OFFSET_TOL = cfg.offset_tol
    GAIN = get_float(s, "lockin:amplifier-gain")
    GAIN_DEF = cfg.gain_default

    # -- Apply settings ---------------------------------------------------------
    if not np.abs(float(AMP) - float(AMP_DEF)) < AMP_TOL:
        raise ValueError("lockin:mod_out_amplitude incorrect.")
    if not np.abs(OFFSET - OFFSET_DEF) < OFFSET_TOL:
        raise ValueError("lockin:mod_out_offset incorrect.")
    if not GAIN == GAIN_DEF:
        raise ValueError("lockin:amplifier_gain incorrect.")
    print(f"lockin:mod_out_amplitude: {AMP}")
    print(f"lockin:mod_out_offset: {OFFSET}")
    print(f"lockin:amplifier_gain: {GAIN}")
    print("Configuring precise scan mode...")
    send_command(s, "(param-set! 'frequency:scan-mode-fast #f)")
    send_command(s, f"(param-set! 'lockin:integration-time {INTEGRATION})")

    if stop_event.is_set():  # check after setup, before scan starts
        print("🛑 Emergency stop — aborting scan before it started")
        s.close()
        return

    # -- Build frequency list ---------------------------------------------------
    frequencies = np.arange(FREQ_START, FREQ_STOP + FREQ_STEP, FREQ_STEP)
    print(f"Scan: {FREQ_START} to {FREQ_STOP} GHz in {FREQ_STEP} GHz steps = {len(frequencies)} points")

    # -- Storage ------------------------------------------------------------------
    results_freq_set = []
    results_freq_act = []
    results_photocurrent = []

    # -- Move to start frequency and wait for full stabilization ---------------
    print(f"Moving to start frequency {FREQ_START} GHz...")
    send_command(s, f"(param-set! 'frequency:frequency-set {FREQ_START})")

    STABILIZE_TOL = cfg.stabilize_tol
    STABILIZE_HOLD = cfg.stabilize_hold
    STABILIZE_TIMEOUT = cfg.stabilize_timeout

    t_start = time.time()
    stable_since = None

    while True:
        if stop_event.is_set():  # check during initial stabilisation wait
            print("🛑 Emergency stop during frequency stabilisation")
            s.close()
            return

        freq_act = get_float(s, "frequency:frequency-act")
        within_tol = abs(freq_act - FREQ_START) < STABILIZE_TOL

        if within_tol:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= STABILIZE_HOLD:
                print(f"  Frequency stable at {freq_act:.3f} GHz")
                break
        else:
            stable_since = None

        if time.time() - t_start > STABILIZE_TIMEOUT:
            print(
                f"  Warning: frequency did not fully stabilize "
                f"(actual: {freq_act:.3f} GHz, target: {FREQ_START} GHz)"
            )
            break

        print(f"  Waiting... current frequency: {freq_act:.3f} GHz")
        time.sleep(0.2)

    print("Proceeding with scan.\n")
    scan_start_time = time.time()

    # -- Scan loop ------------------------------------------------------------------
    for i, freq in enumerate(frequencies):
        if stop_event.is_set():  # check at start of every frequency step
            print(f"🛑 Emergency stop at frequency step {i + 1}/{len(frequencies)}")
            break

        # 1. Set frequency
        send_command(s, f"(param-set! 'frequency:frequency-set {freq})")

        # 2. Wait for frequency to settle
        t_start = time.time()
        while True:
            if stop_event.is_set():  # check while waiting to settle
                print("🛑 Emergency stop during frequency settle")
                break
            freq_act = get_float(s, "frequency:frequency-act")
            if abs(freq_act - freq) < SETTLE_FREQ_TOL:
                break
            if time.time() - t_start > SETTLE_TIMEOUT:
                print(
                    f"  Warning: frequency did not settle at {freq} GHz "
                    f"(actual: {freq_act:.2f} GHz)"
                )
                break
            time.sleep(0.1)

        if stop_event.is_set():  # check after settle loop
            break

        # 3. Reset lock-in and wait integration time
        send_command(s, "(exec 'lockin:lock-in-reset)")

        # split sleep into small chunks so ESC is caught during integration
        integration_s = INTEGRATION / 1000.0
        elapsed = 0.0
        while elapsed < integration_s:
            if stop_event.is_set():
                print("🛑 Emergency stop during integration")
                break
            time.sleep(0.05)
            elapsed += 0.05

        if stop_event.is_set():  # check after integration sleep
            break

        # 4. Read lock-in value
        response = send_command(s, "(param-ref 'lockin:lock-in-value-nanoamp)")
        response = response.split("\n")[0].strip("()")
        parts = response.split()
        photocurrent = float(parts[0])
        is_valid = parts[1] == "#t"

        if not is_valid:
            print(
                f"  Warning: lock-in value not valid at {freq} GHz, "
                f"waiting extra integration time..."
            )
            elapsed = 0.0
            while elapsed < integration_s:
                if stop_event.is_set():  # check during retry integration
                    break
                time.sleep(0.05)
                elapsed += 0.05

            if stop_event.is_set():
                break

            response = send_command(s, "(param-ref 'lockin:lock-in-value-nanoamp)")
            response = response.split("\n")[0].strip("()")
            parts = response.split()
            photocurrent = float(parts[0])

        # 5. Read actual frequency
        freq_act = get_float(s, "frequency:frequency-act")

        # 6. Store results
        results_freq_set.append(freq)
        results_freq_act.append(freq_act)
        results_photocurrent.append(photocurrent)

        # Progress update every 30 points
        if i % 30 == 0:
            elapsed_total = time.time() - scan_start_time
            remaining = (elapsed_total / (i + 1)) * (len(frequencies) - i - 1)
            print(
                f"  [{i + 1}/{len(frequencies)}] {freq:.1f} GHz → "
                f"actual: {freq_act:.2f} GHz, "
                f"photocurrent: {photocurrent:.2f} nA, "
                f"est. remaining: {remaining:.0f}s"
            )

    # -- Save whatever was collected, even if interrupted ---------------------------
    if results_freq_set:
        if stop_event.is_set():
            print(f"\n🛑 Scan interrupted — saving {len(results_freq_set)} points collected so far")
        else:
            print(
                f"\nScan complete! {len(results_freq_set)} points in "
                f"{time.time() - scan_start_time:.1f}s"
            )

        data = np.column_stack(
            [
                np.arange(len(results_freq_set)),
                results_freq_set,
                results_freq_act,
                results_photocurrent,
            ]
        )
        np.savetxt(
            output_path,
            data,
            header="point_number\tfreq_set_GHz\tfreq_act_GHz\tphotocurrent_nA",
            delimiter="\t",
        )
        print(f"Saved to {output_path}")
    else:
        print("No data collected — nothing saved")

    s.close()
