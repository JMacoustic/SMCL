"""Software stand-in for the Arduino so the GUI can be exercised without hardware.

It speaks the same text protocol as the sketch (status lines every 500 ms,
``OK``/``ERR`` replies), so everything downstream of :class:`ImpellerLink`
is exercised exactly as with the real device.
"""

from __future__ import annotations

import math
import random
import time

from .protocol import ImpellerLink
from .ramp import CONTROLLER_MAX_RPM


class SimulatedImpellerLink(ImpellerLink):
    STATUS_INTERVAL_S = 0.5

    def __init__(
        self,
        time_constant_s: float = 2.0,
        noise_rpm: float = 3.0,
        seed: int | None = None,
        command_timeout_s: float | None = 2.5,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.time_constant_s = time_constant_s
        self.noise_rpm = noise_rpm
        # Like the firmware: while the target is non-zero, silence on the serial
        # line for longer than this stops the motor.
        self.command_timeout_s = command_timeout_s
        self._last_command = 0.0
        self._rng = random.Random(seed)
        self._target = 0
        self._rpm = 0.0
        self._last_update = 0.0
        self._last_status = 0.0
        self._outbox: list[str] = []

    @property
    def name(self) -> str:
        return "Simulator"

    # ---------------------------------------------------------------- link
    def _open(self) -> None:
        now = time.monotonic()
        self._target = 0
        self._rpm = 0.0
        self._last_update = now
        self._last_status = now
        self._last_command = now
        self._outbox = [
            "Impeller PID controller ready. (SIMULATED)",
            "Encoder: A->D2, B->D3. PWM: D9 / OC1A.",
            self._status_line(),
        ]

    def _close(self) -> None:
        self._target = 0
        self._rpm = 0.0

    def _write_line(self, command: str) -> None:
        command = command.strip()
        self._last_command = time.monotonic()
        if command == "?":
            self._outbox.append(self._status_line())
        elif command[:1] in ("T", "t"):
            try:
                value = int(command[1:])
            except ValueError:
                self._outbox.append("ERR use T0-T1200")
                return
            if not (0 <= value <= CONTROLLER_MAX_RPM):
                self._outbox.append("ERR use T0-T1200")
                return
            self._target = value
            self._outbox.append(self._status_line())
        elif command == "S0":
            self._target = 0
            self._outbox.append(self._status_line())
        else:
            self._outbox.append("ERR use T<rpm>, P<kp>, I<ki>, D<kd>, S0, or ?")

    def _read_available(self) -> str:
        self._advance()
        if not self._outbox:
            return ""
        text = "".join(line + "\r\n" for line in self._outbox)
        self._outbox = []
        return text

    # ------------------------------------------------------------- physics
    def _advance(self) -> None:
        now = time.monotonic()
        dt = now - self._last_update
        if dt > 0:
            # First-order lag toward the target, slower when spinning down.
            tau = self.time_constant_s if self._target >= self._rpm else self.time_constant_s * 1.5
            self._rpm += (self._target - self._rpm) * (1.0 - math.exp(-dt / tau))
            self._last_update = now
        if (
            self.command_timeout_s is not None
            and self._target != 0
            and now - self._last_command > self.command_timeout_s
        ):
            self._target = 0
            self._outbox.append("ERR command timeout; stopped motor")
            self._outbox.append(self._status_line())
        if now - self._last_status >= self.STATUS_INTERVAL_S:
            self._last_status = now
            self._outbox.append(self._status_line())

    def _status_line(self) -> str:
        noise = self._rng.gauss(0.0, self.noise_rpm) if self._rpm > 1.0 else 0.0
        measured = max(0.0, self._rpm + noise * 0.3)
        raw = max(0.0, self._rpm + noise)
        output = 0.0 if self._target == 0 else min(100.0, 100.0 * self._rpm / 760.0)
        return (
            f"OK target_rpm={self._target} measured_rpm={measured:.0f} raw_rpm={raw:.0f} "
            f"output={output:.2f} kp=0.0700 ki=0.0200 kd=0.0030 strength={output:.3f} "
            f"calib_points=10 pulse_ticks=0 period_ticks=0 pulse_ns=0 rest_us=0 period_us=0 "
            f"duty_ppm=0 max_duty_ppm=70000"
        )
