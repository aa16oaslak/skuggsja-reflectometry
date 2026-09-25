# hi-skuggsja-reflectometry

THz reflectometry stage + TOptica sweep control for the RANNIS-funded project,
with an on-screen STOP button checked by every blocking wait.

Controls two Standa rotation stages (via `libximc`) and a TOptica THz
source/lock-in (via socket) to run angle x frequency reflectometry sweeps,
either specular (theta-2theta) or non-specular (receiver-only).

## Install

Requires Python 3.10 or newer, git and, on the control PC, Standa's
`libximc` vendor driver installed separately (the Python package alone does
not include it).

Clone the repository and install it into a virtual environment inside the
clone:

```powershell
git clone https://github.com/ashaliasrun/rannis_nyskopunarsjodur2026
cd rannis_nyskopunarsjodur2026
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

(If `python` is not found, use `py` instead.)

This installs the `reflecto` command into the virtual environment. The `-e`
(editable) flag means the installed program *is* this folder: after you
edit a file or `git pull`, the next `reflecto` run uses the new code without
reinstalling.

Each time you open a new terminal, activate the environment before using
`reflecto`:

```powershell
C:\path\to\rannis_nyskopunarsjodur2026\.venv\Scripts\activate
```

If PowerShell refuses with "running scripts is disabled on this system",
run the same command in Command Prompt (`cmd`) instead, or allow it once for
your user with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

To update, pull and reinstall from the clone with the environment active
(the reinstall only matters if the dependencies in `pyproject.toml`
changed, but it is quick and harmless):

```powershell
cd C:\path\to\rannis_nyskopunarsjodur2026
git pull
pip install -e .
```

## Configure

Hardware calibration (COM ports, stage zero offsets, angle limits) and
TOptica connection settings live in a config file shipped with the package.
Copy it to your working folder to override any of it:

```powershell
cd C:\path\to\your\workfolder
reflecto config init
```

This writes `reflecto.toml`. Edit only the keys you need to
change -- anything you don't set falls back to the packaged default. If you
run commands from a different folder, point at it explicitly with
`--config path\to\reflecto.toml`.

## Run a sweep

```powershell
reflecto spec --start-angle 30 --end-angle 30 --step 7.5 --filename 270826_specular_ref
reflecto nonspec --start-angle 45 --end-angle 75 --step 15 --filename 310826_nonspec_linear
```

`--freq-start`, `--freq-stop`, and `--int-time` default from the config's
`[scan_defaults]` if omitted. Add `--set-zero` to home and zero both stages
before the sweep (requires typing `clear` to confirm the homing path is
clear of obstacles, since homing can move stages outside their normal
operating range).

While a sweep runs, a small always-on-top window with a red **STOP** button
is shown. Clicking it (or pressing Esc while that window is focused, closing
the window, or pressing Ctrl+C in the terminal) stops both stages
immediately, saves any data already collected, and disconnects the devices
cleanly. The window closes by itself when the sweep ends.

With `--no-gui` no window is opened and Ctrl+C in the terminal is the stop,
e.g. when running over a remote shell without a display.

An unexpected error during the sweep also halts both stages before the
error is reported.

## Notes

- The STOP button is a software stop: it needs the program to be running
  and responsive. It is not a substitute for a physical emergency stop.
- If you press STOP while the terminal is asking you to type `clear` (homing
  with `--set-zero`), the stages halt at once, but the prompt still waits
  for you to press Enter in the terminal before the program exits.
- `libximc` requires Standa's vendor driver/DLLs on the machine, independent
  of the pip package.

## Development

Tests run without any hardware attached -- the rotation stages are replaced
with fakes (see `tests/fakes.py`). The STOP-window tests are skipped when
there is no display to open a window on.

From the clone, with the environment active:

```powershell
pip install -e ".[dev]"
pytest
```
