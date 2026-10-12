"""CSV recording of controller status, one row per status line received."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

COLUMNS = [
    "timestamp",  # local ISO-8601 wall-clock time
    "unix_time_s",
    "log_elapsed_s",  # seconds since this log file was opened
    "run_elapsed_s",  # seconds since the current ramp run started (blank if idle)
    "controller_time_s",  # Arduino millis()/1000 when the status line was printed (newer firmware)
    "controller_rpm_time_s",  # Arduino clock of the last RPM calculation (newer firmware)
    "phase",  # idle / ramping / holding / finished / stopped
    "commanded_rpm",  # last target sent by this program
    "controller_target_rpm",  # target echoed by the Arduino
    "measured_rpm",
    "raw_rpm",
    "output_percent",
    "strength_percent",
    "kp",
    "ki",
    "kd",
    # Extra fields reported by newer firmware (blank when the sketch does not send them).
    "controller_setpoint_rpm",  # internal setpoint the firmware is currently tracking
    "encoder_rpm",  # uncalibrated RPM straight from the encoder
    "feedforward_percent",
    "d_rpm_s",
    "hold",
    "open_loop",
]


def make_log_path(folder: Path, prefix: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now()
    stem = f"{prefix.strip() or 'impeller'}_{when.strftime('%Y%m%d_%H%M%S')}"
    path = folder / f"{stem}.csv"
    counter = 1
    while path.exists():
        path = folder / f"{stem}_{counter}.csv"
        counter += 1
    return path


class CsvLogger:
    """Append-only CSV writer that flushes after every row so a crash loses nothing."""

    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self._file = None
        self._writer: Optional[csv.DictWriter] = None
        self._opened_at: Optional[float] = None
        self.rows_written = 0

    @property
    def is_open(self) -> bool:
        return self._file is not None

    def open(self, path: Path, metadata: Optional[dict[str, Any]] = None) -> Path:
        if self.is_open:
            raise RuntimeError("logger is already open")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=COLUMNS)
        self._writer.writeheader()
        self._file.flush()
        self._opened_at = time.monotonic()
        self.path = path
        self.rows_written = 0
        if metadata is not None:
            meta_path = path.with_name(path.stem + "_meta.json")
            meta_path.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
        return path

    def write(self, row: dict[str, Any]) -> None:
        if not self.is_open:
            return
        now = time.time()
        full = {
            "timestamp": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
            "unix_time_s": f"{now:.3f}",
            "log_elapsed_s": f"{time.monotonic() - self._opened_at:.3f}",
        }
        for key in COLUMNS:
            if key in full:
                continue
            value = row.get(key)
            full[key] = "" if value is None else _fmt(value)
        self._writer.writerow(full)
        self._file.flush()
        self.rows_written += 1

    def close(self) -> Optional[Path]:
        path = self.path
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None
        self._opened_at = None
        return path


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value)}"
    return str(value)
