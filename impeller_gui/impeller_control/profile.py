"""Custom time-RPM profiles made of user-placed points (no Qt, no serial).

A :class:`CustomProfile` is a list of ``(time_s, rpm)`` points, sorted by time,
whose first point is at ``t = 0``. Between points the setpoint is interpolated
according to :class:`InterpolationMode`:

* ``LINEAR`` - straight ramps between points.
* ``SMOOTH`` - monotone cubic (Fritsch-Carlson) curve through the points. It never
  overshoots the surrounding points, so the setpoint stays inside the range the
  user drew.
* ``STEP``   - hold each point's RPM until the next point.

After the last point the profile either stops the motor or holds the last RPM
until the user presses Stop. The class exposes the same schedule interface as
:class:`ramp.RampProfile` (``setpoint_at``, ``phase_at``, ``total_seconds`` ...),
so the GUI can run either one.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .ramp import CONTROLLER_MAX_RPM, RampPhase

MIN_POINT_SPACING_S = 0.1


class InterpolationMode(str, Enum):
    LINEAR = "linear"
    SMOOTH = "smooth"
    STEP = "step"


class EndBehavior(str, Enum):
    STOP = "stop"  # send target 0 when the last point is reached
    HOLD = "hold"  # keep the last RPM until Stop is pressed


@dataclass(frozen=True)
class ProfilePoint:
    t: float
    rpm: float


@dataclass
class CustomProfile:
    points: list[ProfilePoint] = field(default_factory=list)
    mode: InterpolationMode = InterpolationMode.LINEAR
    end: EndBehavior = EndBehavior.STOP
    max_rpm: int = CONTROLLER_MAX_RPM

    # ------------------------------------------------------------ building
    @classmethod
    def from_pairs(cls, pairs, mode=InterpolationMode.LINEAR, end=EndBehavior.STOP, max_rpm=CONTROLLER_MAX_RPM):
        profile = cls([ProfilePoint(float(t), float(r)) for t, r in pairs], InterpolationMode(mode), EndBehavior(end), max_rpm)
        profile.sort()
        return profile

    def sort(self) -> None:
        self.points.sort(key=lambda p: p.t)

    def validate(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a profile needs at least two points")
        if abs(self.points[0].t) > 1e-9:
            raise ValueError("the first point must be at t = 0 s")
        if self.max_rpm <= 0 or self.max_rpm > CONTROLLER_MAX_RPM:
            raise ValueError(f"max_rpm must be in 1..{CONTROLLER_MAX_RPM}")
        previous = None
        for p in self.points:
            if not (math.isfinite(p.t) and math.isfinite(p.rpm)):
                raise ValueError("points must be finite numbers")
            if p.t < 0:
                raise ValueError("point times must be >= 0")
            if not (0 <= p.rpm <= self.max_rpm):
                raise ValueError(f"RPM must be in 0..{self.max_rpm} (got {p.rpm:g} at t = {p.t:g} s)")
            if previous is not None and p.t - previous.t < MIN_POINT_SPACING_S - 1e-9:
                raise ValueError(f"points must be at least {MIN_POINT_SPACING_S:g} s apart (t = {p.t:g} s)")
            previous = p

    # ------------------------------------------------------- schedule API
    @property
    def duration(self) -> float:
        return self.points[-1].t if self.points else 0.0

    @property
    def ramp_seconds(self) -> float:
        """Time until the drawn curve ends (mirrors RampProfile.ramp_seconds)."""
        return self.duration

    @property
    def total_seconds(self) -> Optional[float]:
        return self.duration if self.end == EndBehavior.STOP else None

    @property
    def stop_at_end(self) -> bool:
        return self.end == EndBehavior.STOP

    def value_at(self, t: float) -> float:
        """Interpolated RPM (float) at time ``t``; clamped to the profile's time span."""
        pts = self.points
        if not pts:
            return 0.0
        if t <= pts[0].t:
            return pts[0].rpm
        if t >= pts[-1].t:
            return pts[-1].rpm

        i = _segment_index(pts, t)
        p0, p1 = pts[i], pts[i + 1]
        if self.mode == InterpolationMode.STEP:
            return p0.rpm
        if self.mode == InterpolationMode.LINEAR or len(pts) == 2:
            u = (t - p0.t) / (p1.t - p0.t)
            return p0.rpm + u * (p1.rpm - p0.rpm)
        return _monotone_cubic(pts, i, t)

    def setpoint_at(self, t: float) -> int:
        value = self.value_at(t)
        return int(round(max(0.0, min(float(self.max_rpm), value))))

    def phase_at(self, t: float) -> RampPhase:
        if t < self.duration:
            return RampPhase.RAMPING
        if self.end == EndBehavior.HOLD:
            return RampPhase.HOLDING
        return RampPhase.FINISHED

    def progress_at(self, t: float) -> float:
        if self.duration <= 0:
            return 1.0
        return max(0.0, min(1.0, t / self.duration))

    def sample(self, step_s: float) -> list[tuple[float, float]]:
        """Dense samples of the curve for drawing."""
        if not self.points or step_s <= 0:
            return []
        out = []
        t = 0.0
        while t < self.duration:
            out.append((t, self.value_at(t)))
            t += step_s
        out.append((self.duration, self.points[-1].rpm))
        return out

    def describe(self) -> str:
        if not self.points:
            return "empty profile"
        peak = max(p.rpm for p in self.points)
        tail = "then stop" if self.end == EndBehavior.STOP else f"then hold {self.points[-1].rpm:g} RPM until Stop"
        return f"{len(self.points)} points, {self.mode.value}, {self.duration:g} s, peak {peak:g} RPM, {tail}"

    # ------------------------------------------------------------- JSON
    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "end": self.end.value,
            "max_rpm": self.max_rpm,
            "points": [[round(p.t, 3), round(p.rpm, 3)] for p in self.points],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CustomProfile":
        return cls.from_pairs(
            data.get("points", []),
            mode=data.get("mode", InterpolationMode.LINEAR.value),
            end=data.get("end", EndBehavior.STOP.value),
            max_rpm=int(data.get("max_rpm", CONTROLLER_MAX_RPM)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "CustomProfile":
        return cls.from_dict(json.loads(text))


def _segment_index(pts: list[ProfilePoint], t: float) -> int:
    lo, hi = 0, len(pts) - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if pts[mid].t <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _monotone_cubic(pts: list[ProfilePoint], i: int, t: float) -> float:
    """Fritsch-Carlson monotone cubic Hermite interpolation on segment ``i``."""
    n = len(pts)
    h = [pts[k + 1].t - pts[k].t for k in range(n - 1)]
    delta = [(pts[k + 1].rpm - pts[k].rpm) / h[k] for k in range(n - 1)]

    m = [0.0] * n
    m[0] = delta[0]
    m[-1] = delta[-1]
    for k in range(1, n - 1):
        if delta[k - 1] * delta[k] <= 0:
            m[k] = 0.0
        else:
            w1 = 2 * h[k] + h[k - 1]
            w2 = h[k] + 2 * h[k - 1]
            m[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])
    for k in range(n - 1):
        if delta[k] == 0:
            m[k] = 0.0
            m[k + 1] = 0.0
        else:
            a = m[k] / delta[k]
            b = m[k + 1] / delta[k]
            s = a * a + b * b
            if s > 9:
                tau = 3 / math.sqrt(s)
                m[k] = tau * a * delta[k]
                m[k + 1] = tau * b * delta[k]

    p0, p1 = pts[i], pts[i + 1]
    u = (t - p0.t) / h[i]
    h00 = 2 * u**3 - 3 * u**2 + 1
    h10 = u**3 - 2 * u**2 + u
    h01 = -2 * u**3 + 3 * u**2
    h11 = u**3 - u**2
    return h00 * p0.rpm + h10 * h[i] * m[i] + h01 * p1.rpm + h11 * h[i] * m[i + 1]
