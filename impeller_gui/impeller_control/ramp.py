"""Pure ramp-profile logic (no Qt, no serial).

A :class:`RampProfile` turns a :class:`RampSettings` into a function of time:
``setpoint_at(t)`` gives the RPM that should be commanded ``t`` seconds after
the run started, and ``phase_at(t)`` tells which phase the run is in.
Keeping this separate from the GUI makes it easy to unit-test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# The Arduino sketch rejects targets above this value ("ERR use T0-T1200").
CONTROLLER_MAX_RPM = 1200


class RampMode(str, Enum):
    LINEAR = "linear"
    STEPPED = "stepped"


class RampPhase(str, Enum):
    IDLE = "idle"
    RAMPING = "ramping"
    HOLDING = "holding"
    FINISHED = "finished"  # hold time elapsed (motor stopped if stop_after_hold)
    STOPPED = "stopped"  # aborted by the user or by an error


@dataclass(frozen=True)
class RampSettings:
    start_rpm: int
    target_rpm: int
    mode: RampMode = RampMode.LINEAR
    rate_rpm_per_s: float = 10.0  # linear mode
    step_rpm: int = 50  # stepped mode
    step_dwell_s: float = 10.0  # stepped mode
    hold_s: Optional[float] = 60.0  # None -> hold indefinitely until Stop
    stop_after_hold: bool = True
    max_rpm: int = CONTROLLER_MAX_RPM

    def validate(self) -> None:
        if self.max_rpm <= 0 or self.max_rpm > CONTROLLER_MAX_RPM:
            raise ValueError(f"max_rpm must be in 1..{CONTROLLER_MAX_RPM}")
        for name in ("start_rpm", "target_rpm"):
            value = getattr(self, name)
            if not (0 <= value <= self.max_rpm):
                raise ValueError(f"{name} must be in 0..{self.max_rpm} RPM (got {value})")
        if self.mode == RampMode.LINEAR:
            if not (self.rate_rpm_per_s > 0) or not math.isfinite(self.rate_rpm_per_s):
                raise ValueError("ramp rate must be a positive number of RPM/s")
        elif self.mode == RampMode.STEPPED:
            if self.step_rpm <= 0:
                raise ValueError("step size must be a positive number of RPM")
            if not (self.step_dwell_s > 0) or not math.isfinite(self.step_dwell_s):
                raise ValueError("step dwell must be a positive number of seconds")
        else:
            raise ValueError(f"unknown ramp mode {self.mode!r}")
        if self.hold_s is not None and (self.hold_s < 0 or not math.isfinite(self.hold_s)):
            raise ValueError("hold time must be zero or positive")


def rate_from_duration(start_rpm: int, target_rpm: int, duration_s: float) -> float:
    """Convert a desired ramp duration into an equivalent RPM/s rate."""
    if duration_s <= 0:
        raise ValueError("ramp duration must be positive")
    return abs(target_rpm - start_rpm) / duration_s


class RampProfile:
    """Deterministic setpoint schedule derived from :class:`RampSettings`."""

    def __init__(self, settings: RampSettings):
        settings.validate()
        self.settings = settings
        self._delta = settings.target_rpm - settings.start_rpm
        self._sign = 1 if self._delta >= 0 else -1
        self._span = abs(self._delta)

        if self._span == 0:
            self._ramp_s = 0.0
            self._step_count = 0
        elif settings.mode == RampMode.LINEAR:
            self._ramp_s = self._span / settings.rate_rpm_per_s
            self._step_count = 0
        else:
            self._step_count = math.ceil(self._span / settings.step_rpm)
            # The first step is commanded at t = 0, the last at (n - 1) * dwell.
            self._ramp_s = (self._step_count - 1) * settings.step_dwell_s

    # ------------------------------------------------------------------ info
    @property
    def ramp_seconds(self) -> float:
        """Time until the commanded setpoint first equals the target."""
        return self._ramp_s

    @property
    def total_seconds(self) -> Optional[float]:
        """Ramp + hold duration, or ``None`` when holding indefinitely."""
        if self.settings.hold_s is None:
            return None
        return self._ramp_s + self.settings.hold_s

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def stop_at_end(self) -> bool:
        """Whether the motor should be stopped when the schedule finishes."""
        return self.settings.stop_after_hold and self.settings.hold_s is not None

    def to_pairs(self) -> list[tuple[float, float]]:
        """The schedule as (time, rpm) points, e.g. to seed a custom profile."""
        s = self.settings
        pts: list[tuple[float, float]] = []
        if s.mode == RampMode.STEPPED and self._span > 0:
            # Stepped: the first step is commanded at t = 0 (use STEP interpolation).
            for k in range(self._step_count):
                pts.append((k * s.step_dwell_s, float(self.setpoint_at(k * s.step_dwell_s + 1e-9))))
        else:
            pts.append((0.0, float(s.start_rpm)))
            if self._span > 0:
                pts.append((self._ramp_s, float(s.target_rpm)))
        hold = s.hold_s if s.hold_s is not None else 60.0
        if hold > 0:
            pts.append((self._ramp_s + hold, float(s.target_rpm)))
        return pts

    # -------------------------------------------------------------- schedule
    def setpoint_at(self, t: float) -> int:
        if t <= 0 or self._span == 0:
            return self.settings.start_rpm if t <= 0 else self.settings.target_rpm

        if self.settings.mode == RampMode.LINEAR:
            travelled = min(self.settings.rate_rpm_per_s * t, self._span)
        else:
            steps_done = math.floor(t / self.settings.step_dwell_s) + 1
            travelled = min(self.settings.step_rpm * steps_done, self._span)

        return int(round(self.settings.start_rpm + self._sign * travelled))

    def phase_at(self, t: float) -> RampPhase:
        if t < self._ramp_s:
            return RampPhase.RAMPING
        total = self.total_seconds
        if total is None or t < total:
            return RampPhase.HOLDING
        return RampPhase.FINISHED

    def progress_at(self, t: float) -> float:
        """0..1 fraction of the scheduled run (ramp only when hold is indefinite)."""
        total = self.total_seconds
        if total is None:
            total = self._ramp_s
        if total <= 0:
            return 1.0
        return max(0.0, min(1.0, t / total))

    def describe(self) -> str:
        s = self.settings
        if s.mode == RampMode.LINEAR:
            how = f"linear at {s.rate_rpm_per_s:g} RPM/s"
        else:
            how = f"{self._step_count} step(s) of {s.step_rpm} RPM every {s.step_dwell_s:g} s"
        hold = "hold until stopped" if s.hold_s is None else f"hold {s.hold_s:g} s"
        tail = ", then stop" if (s.stop_after_hold and s.hold_s is not None) else ""
        return f"{s.start_rpm} -> {s.target_rpm} RPM, {how} (~{self._ramp_s:.1f} s), {hold}{tail}"
