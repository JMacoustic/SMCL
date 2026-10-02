# Impeller Ramp Control (Windows GUI)

A small PySide6 program for the Arduino impeller controller
(`src/arduino_codes/impeller/impeller.ino`). It does three things:

1. **Connect** to the Arduino over USB serial.
2. **Ramp** the impeller from its current speed up (or down) to a target RPM,
   hold it there, and optionally stop the motor afterwards.
3. **Record** everything the controller reports to a CSV file.

No camera, robot arm, or other lab hardware is needed. A built-in simulator
lets you try the program without an Arduino.

---

## 1. Setup (once)

Requirements: Windows 10/11 and Python 3.9 – 3.12, either from python.org or
from Anaconda/Miniconda. Copy or clone this folder (`impeller_gui/`) anywhere
on the PC, then pick **one** of the two options.

**A. python.org Python** — install it from <https://www.python.org/downloads/>
(tick **"Add python.exe to PATH"**), then double-click **`setup_venv.bat`**.
It creates a private virtual environment in `impeller_gui\.venv\` and installs
`PySide6` and `pyserial` into it. Nothing else on the PC is changed.

**B. Anaconda / Miniconda** — open *Anaconda Prompt*, `cd` into this folder and
run **`setup_conda.bat`**. It creates a conda environment named `smcl` from
`environment.yml` (Python 3.12 + PySide6 + pyserial), or updates it if it
already exists.

Command-line equivalents:

```bat
cd impeller_gui
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bat
cd impeller_gui
conda env create -f environment.yml
```

## 2. Run

Double-click **`run_gui.bat`**. It uses `.venv` if present, otherwise the
conda environment `smcl`; if neither exists it runs the matching setup first.
When using conda and the script says `conda was not found on PATH`, start it
from *Anaconda Prompt* instead.

Or, from a terminal:

```bat
cd impeller_gui
.venv\Scripts\python.exe -m impeller_control
```

```bat
cd impeller_gui
conda run -n smcl --no-capture-output python -m impeller_control
```

## 3. Using the program

The window is laid out top to bottom in the order you use it.

### Connection (top bar)
* Plug the Arduino in, press **Refresh ports**, choose the `COM` port
  (it is usually labelled "Arduino" or "USB Serial"), then **Connect**.
* The indicator turns **green / "Ready"** once the controller sends its first
  status line (the Arduino restarts when the port opens, so this takes 1–2 s).
* Choose **Simulator** instead of a COM port to practise without hardware.

### 1. Simple ramp / 1. Custom profile
The speed schedule comes from whichever of the two tabs is showing.

**Simple ramp** — type the numbers:

| Field | Meaning |
|---|---|
| Target speed | RPM to reach. |
| Start speed | Where the ramp begins. Leave **from current target** ticked to continue from whatever the controller is doing now (0 when stopped). |
| Ramp mode | **Linear**: speed rises at a constant rate. **Stepped**: speed jumps by a fixed increment every *dwell* seconds. |
| Ramp speed | For linear mode, give either a **Rate** (RPM/s) or a total **Duration** (s); the other value is shown next to it. |
| Step size / dwell | For stepped mode. |
| Hold at target | How long to stay at the target before the run ends. Tick **until Stop is pressed** to hold indefinitely. |
| Stop the motor when the hold time ends | Sends a stop command (target 0) when the hold finishes. Untick to leave the impeller spinning. |

The **Plan** line summarises the run in words before you start it.

**Custom profile** — draw the schedule on the graph (time on the x axis, RPM
on the y axis) that replaces the rolling plot on the right while this tab is
active:

| Action | Effect |
|---|---|
| Click empty space | Add a point there (snapped to the grid). |
| Drag a point | Move it. The first point stays at t = 0; only its speed changes. |
| Double-click a point | Type exact time and speed. |
| Right-click / Delete | Remove the point (the first point cannot be removed; two points minimum). |
| Table on the left | Same points as the graph; editing a cell updates the graph. |

| Field | Meaning |
|---|---|
| Connect points | **Linear** ramps between points, a **Smooth** monotone curve through them (no overshoot beyond neighbouring points), or **Step** (hold each speed until the next point). |
| After last point | **Stop the motor** or **Hold last speed until Stop**. |
| Snap to grid | Time / RPM grid used when clicking and dragging; 0 turns it off. |
| Time axis length | Fixed x-axis length, or *auto* to fit the profile. |
| Add point / Remove / Clear | Table-based editing. |
| Load… / Save… | Store the profile as a JSON file and reuse it later. |
| Start from the simple ramp settings | Convert the Simple ramp tab into points. |

While a profile runs, the measured RPM (red) and a time cursor are drawn over
the planned curve, and editing is locked.

Settings shared by both tabs live in the **3. Run** box: **Max speed** (the
highest target the program will send, default 700) and **Update every** (how
often an intermediate target is sent, 0.5 s is a good default, see notes).

### 2. CSV logging
* Choose a **Folder** and a **File prefix**. Files are named
  `<prefix>_YYYYMMDD_HHMMSS.csv` and are never overwritten.
* With the two auto boxes ticked (default), each press of **Start ramp** creates
  a new CSV and closes it about one second after the run ends.
* **Start logging / Stop logging** record at any time, independent of a run
  (for example to capture the spin-down after a stop).
* A `<name>_meta.json` file with the ramp settings or the profile points is written next to each CSV.

### 3. Run
* **▶ Start ramp** starts the plan. The settings are locked while it runs.
* **■ STOP motor** immediately sends target 0 to the controller. It works at any
  time while connected, run or no run.
* **Manual target / Set now** sends a target directly without ramping.

### Live status, plot, messages
* Big numbers show measured RPM, the target this program last sent, the target
  the controller echoes back, PID output, phase, run time and remaining time.
* The plot shows the last two minutes of commanded vs measured RPM.
* "Controller messages" shows commands sent (`>>`), controller replies and
  errors (`!!`). Tick *Show every status line* to see the raw 2 Hz stream.

Closing the window sends a stop command, closes the CSV and remembers your
settings for next time.

## 4. CSV columns

| Column | Description |
|---|---|
| `timestamp` | Local wall-clock time (ISO 8601, ms). |
| `unix_time_s` | Same instant as Unix seconds. |
| `log_elapsed_s` | Seconds since this file was opened. |
| `run_elapsed_s` | Seconds since **Start ramp** was pressed (blank before any run). |
| `phase` | `idle`, `ramping`, `holding`, `finished` or `stopped`. |
| `commanded_rpm` | Last target sent by this program. |
| `controller_target_rpm` | Target the Arduino reports it is using. |
| `measured_rpm` | Calibrated, smoothed RPM from the encoder. |
| `raw_rpm` | Calibrated but unsmoothed RPM. |
| `output_percent` | PID output (0–100 %). |
| `strength_percent` | PWM strength actually applied. |
| `kp`, `ki`, `kd` | Controller gains. |
| `controller_setpoint_rpm`, `encoder_rpm`, `feedforward_percent`, `d_rpm_s`, `hold`, `open_loop` | Extra fields from newer firmware; blank if the sketch does not report them. |

One row is written per status line from the controller, i.e. every 0.5 s.

## 5. Notes and limits

* The board must be flashed with `impeller.ino` (the one with `T<rpm>`
  commands), not `pwm.ino`.
* **Max speed** (default 700 RPM, in the Run box) caps every target this program sends.
  The firmware has its own `MAX_TARGET_RPM`; if it rejects a target, the GUI
  lowers this setting to the firmware's value automatically.
* The firmware stops the motor when it receives no serial command for 2.5 s
  (`ERR command timeout; stopped motor`). While a target is active the GUI
  therefore sends a status query every second; you do not need to do anything,
  but expect about three status rows per second in the CSV instead of two.
  If the firmware does stop the motor on its own, the run is marked `stopped`.
* The sketch resets its PID integral term every time it receives a new target.
  During a ramp this happens at every setpoint update, so very short update
  intervals (0.2 s) make the controller rely mostly on its proportional term.
  0.5–1 s works well; the rate of the ramp itself is unaffected.
* If the sketch is compiled with `USE_FIXED_TARGET_OUTPUT_TABLE = true` the
  PID is bypassed and the output comes from a fixed table (calibration mode).
  The measured RPM will then ignore the ramp; set the flag to `false` and
  re-flash for closed-loop control.
* Only one program can own the COM port at a time. Close the Arduino Serial
  Monitor or any other tool before connecting.
* If the USB cable is pulled mid-run the program shows "Connection lost"; the
  Arduino keeps the last target, so reconnect and press **STOP motor**.

## 6. For developers

```bat
run_tests.bat
```
runs the unit tests (`tests/`) for the ramp schedule, the serial protocol
parser, the simulator and the CSV logger. Layout:

```
impeller_control/
  app.py          PySide6 window (all UI logic)
  ramp.py         RampSettings / RampProfile: setpoint and phase as a function of time
  profile.py      CustomProfile: user-drawn points with linear / smooth / step interpolation
  profile_editor.py  ProfileModel / ProfileCanvas / ProfilePanel: the interactive editor
  protocol.py     ImpellerLink + SerialImpellerLink: the Arduino text protocol
  simulator.py    SimulatedImpellerLink: fake controller for testing
  csv_logger.py   CsvLogger and CSV column definitions
  plot_widget.py  QPainter rolling plot
```
