# hi-skuggsja-reflectometry

THz reflectometry stage + TOptica sweep control for the RANNIS-funded project,
with an on-screen STOP button checked by every blocking wait.

Controls two Standa rotation stages (via `libximc`) and a TOptica THz
source/lock-in (via socket) to run angle x frequency reflectometry sweeps,
either specular (theta-2theta) or non-specular (receiver-only).

## Install

Requires [Miniconda](https://docs.anaconda.com/miniconda/) (or Anaconda),
git and, on the control PC, Standa's `libximc` vendor driver installed
separately (the Python package alone does not include it).

Open the **Anaconda Prompt** from the Start menu, create a conda environment
for the setup, then clone the repository and install it into that
environment:

```powershell
conda create -n reflecto -c conda-forge python=3.12 pip
conda activate reflecto
git clone https://github.com/aa16oaslak/skuggsja-reflectometry
cd skuggsja-reflectometry
pip install -e .
```

(No git on the machine? Run `conda install -c conda-forge git` after
`conda activate reflecto`.)

This installs the `reflecto` command into the `reflecto` environment. Using
pip inside a conda environment is the normal way to install a package from
its source folder. The `-e` (editable) flag means the installed program *is*
the cloned folder: after you edit a file or `git pull`, the next `reflecto`
run uses the new code without reinstalling.

Each time you open a new Anaconda Prompt, activate the environment before
using `reflecto`:

```powershell
conda activate reflecto
```

To update, pull and reinstall from the clone with the environment active
(the reinstall only matters if the dependencies in `pyproject.toml`
changed, but it is quick and harmless):

```powershell
conda activate reflecto
cd C:\path\to\skuggsja-reflectometry
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
`--config path\to\reflecto.toml`. A mistyped setting is reported by name
instead of being ignored.

The `[view]` section only changes how the setup is drawn, never how a stage
moves: `receiver_turns` is the way the receiver moves away from Tx as its
angle grows, seen from above (`"clockwise"`, as in Fig. 1 of
arXiv:2407.05512), and `tx_direction` is where Tx is drawn, in degrees
counterclockwise from the right (0 right, 90 up, 180 left, 270 down).
`reflecto position` sets both against the real arms and saves them.

## Check the hardware

```powershell
reflecto check
```

Opens a window with a light for each device: the receiver stage (R1), the
sample stage (R2) and the TOptica. Green is ready, amber works but needs a
look, red is not usable. Each row says why: for example the COM port can't
be opened, the controller is in alarm or its motor supply is off, the stage
has not been homed since it was switched on, the TOptica does not answer at
the configured address, or its lock-in settings are ones a scan would refuse.
"Check again" re-runs it. The check only reads status: nothing moves and no
setting changes.

`--no-gui` just prints the result (exit code 0 if everything is usable, 1
if not), and `--simulate` checks a simulated setup, e.g. to see the window.

## See where the arms are

```powershell
reflecto position
```

Opens a window that draws both arms where the stages say they are, read
from the controllers every quarter of a second without moving anything:
Tx, the receiver on its ring, the sample and its normal, and the angles.
Next to it are both angles, the controller positions, and whether each
stage has been homed since it was switched on.

Stand where you can see the table and make the drawing look like it:
**Mirror** swaps the direction the angles grow, **Turn** rotates the drawing
in 15° or 90° steps, and **Save** writes both into the `[view]` section of
`reflecto.toml`. Then check:

- Tx, the receiver and the sample plate should all sit where they are on
  the table. If the receiver or the sample is somewhere else, the angles
  themselves are wrong: a stage that is not homed only counts steps from
  wherever it was when switched on. Home the stages with `--set-zero`.
- After homing, the receiver should be at its home, almost opposite Tx.

`--no-gui` prints the angles instead, and `--simulate` shows a simulated
setup, e.g. to try the window.

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

Every sweep starts with the same hardware check as `reflecto check`, printed
in the terminal. If a device is not usable, the sweep stops there, before
anything has moved. (`--skip-check` starts anyway, for the case where you
are sure the check itself is wrong.)

A real sweep then goes through three steps, and nothing moves before the
third:

1. **Where the arms are.** The `reflecto position` window, with the angles
   this sweep will visit marked on the ring. Press *The drawing matches the
   setup* only if it does; *Cancel* ends the run.
2. **The same sweep, simulated from there.** The simulated arms start
   exactly where the real ones are (at the speeds stored in the
   controllers) and run the whole sweep, 20x faster (`--speed` to change
   it), so you can watch every move before it happens. At the end the window
   says whether every step reached its planned angles and asks *Run on the
   real hardware* or *Cancel*. A sweep that fails in the simulation, for
   example because it would pass a soft limit, is not offered.
3. **The real sweep**, as below. If a stage has moved since step 1, it stops
   instead, because the simulation no longer shows what would happen.

With `--no-gui` the same steps are asked in the terminal (type `yes`, then
`run`). `--skip-preview` goes straight to step 3; use it only once both the
drawing and the sweep have been checked.

While a sweep runs, an always-on-top window shows the setup from above, like
Fig. 1 of [arXiv:2407.05512](https://arxiv.org/abs/2407.05512) (see *Test a
sweep without hardware* below), with a light per device and a red **STOP**
button. The lights follow the sweep's own communication with each device:
green while it answers, red with the error as soon as one stops answering.
The drawing and lights never talk to the hardware themselves.

Clicking STOP (or pressing Esc while that window is focused, closing the
window, or pressing Ctrl+C in the terminal) stops both stages immediately,
saves any data already collected, and disconnects the devices cleanly. The
window closes by itself when the sweep ends.

With `--no-gui` no window is opened and Ctrl+C in the terminal is the stop,
e.g. when running over a remote shell without a display.

An unexpected error during the sweep also halts both stages before the
error is reported.

## Test a sweep without hardware

Add `--simulate` to any sweep command to rehearse it on any computer, with
nothing connected:

```powershell
reflecto spec --start-angle 15 --end-angle 45 --step 7.5 --filename 270826_specular_ref --simulate
```

The same sweep code runs, but against simulated rotation stages and a
simulated TOptica on your own computer, so no hardware is opened or moved.
The stages are calibrated and limited from your config exactly like the real
ones, and the simulation is plainly labelled everywhere it shows up.

- A live window draws the setup from above, like Fig. 1 of
  [arXiv:2407.05512](https://arxiv.org/abs/2407.05512): the transmitter,
  the receiver moving on its ring (with its allowed range), the sample and
  its normal, the angles φ1 (incidence) and φ2 (receiver, from the normal),
  and the planned receiver positions. Next to it: both stage angles against
  the plan, scan progress, the output file, the hardware lights, and
  warnings, e.g. when a stage is outside its soft limits. The STOP button
  works as in a real sweep.
- Time runs 20x faster by default (`--speed` to change it). Settle times,
  stage travel and the printed estimates stay in real-world seconds.
- Output files (with a made-up photocurrent) go to `reflecto_simulated/`,
  never next to your real data. If a real file would be overwritten, the
  simulation stops with the same error the real sweep would give.
- Afterwards, a summary lists planned vs. actual angles for every step, any
  move stopped by a soft limit, and the estimated time on the setup.

Stage speeds are guesses (the real ones are stored in the stage
controllers), so the time estimate is approximate. `--simulate --no-gui`
skips the window and just prints the summary.

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

## Credits

Originally written by Ashali Asrun
([ashaliasrun/rannis_nyskopunarsjodur2026](https://github.com/ashaliasrun/rannis_nyskopunarsjodur2026))
in a project funded by the RANNÍS Student Innovation Fund 2026 at the
University of Iceland. This repository continues that work, and the
original commit history is preserved.

