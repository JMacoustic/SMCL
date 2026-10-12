"""Serial protocol of the Arduino impeller PID sketch (``src/arduino_codes/impeller/impeller.ino``).

Commands (newline terminated, 115200 baud):
    T<rpm>   set integer target RPM (T0 stops)
    S0       stop
    ?        print status now

The sketch prints a status line every 500 ms and after every accepted command::

    OK target_rpm=300 measured_rpm=298 raw_rpm=301 output=42.10 kp=0.0700 ki=0.0200 kd=0.0030 strength=42.100 ...

Lines starting with ``ERR`` report rejected commands.

Note: the sketch resets its PID integral term every time a ``T`` command is
received, so callers should only send a new target when the value changes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ramp import CONTROLLER_MAX_RPM

DEFAULT_BAUD = 115200
STATUS_RE = re.compile(r"(?P<key>[A-Za-z_]+)=(?P<value>[-+]?\d+(?:\.\d+)?)")


@dataclass
class ImpellerStatus:
    target_rpm: Optional[int] = None
    measured_rpm: Optional[float] = None
    raw_rpm: Optional[float] = None
    output: Optional[float] = None  # PID output, percent
    strength: Optional[float] = None  # applied PWM strength, percent
    kp: Optional[float] = None
    ki: Optional[float] = None
    kd: Optional[float] = None
    updated_at: Optional[float] = None  # time.monotonic()
    fields: dict = field(default_factory=dict)
    line: str = ""

    @property
    def age(self) -> Optional[float]:
        if self.updated_at is None:
            return None
        return time.monotonic() - self.updated_at


def parse_status_line(line: str, now: Optional[float] = None) -> Optional[ImpellerStatus]:
    """Parse one ``OK target_rpm=... measured_rpm=...`` line; ``None`` if it is not a status line."""
    if "target_rpm=" not in line or "measured_rpm=" not in line:
        return None

    fields: dict = {}
    for match in STATUS_RE.finditer(line):
        fields[match.group("key")] = float(match.group("value"))
    if not fields:
        return None

    return ImpellerStatus(
        target_rpm=int(fields["target_rpm"]) if "target_rpm" in fields else None,
        measured_rpm=fields.get("measured_rpm"),
        raw_rpm=fields.get("raw_rpm"),
        output=fields.get("output"),
        strength=fields.get("strength"),
        kp=fields.get("kp"),
        ki=fields.get("ki"),
        kd=fields.get("kd"),
        updated_at=time.monotonic() if now is None else now,
        fields=fields,
        line=line,
    )


def format_target_command(rpm: int) -> str:
    rpm = int(rpm)
    if not (0 <= rpm <= CONTROLLER_MAX_RPM):
        raise ValueError(f"target RPM must be in 0..{CONTROLLER_MAX_RPM}")
    return f"T{rpm}"


class LinkError(RuntimeError):
    """Raised when the controller link is lost or a write/read fails."""


class ImpellerLink:
    """Common behaviour for the real serial link and the simulator.

    Subclasses implement :meth:`_open`, :meth:`_close`, :meth:`_write_line`
    and :meth:`_read_available`. ``poll()`` must be called regularly from the
    GUI thread; it is non-blocking.
    """

    def __init__(self, status_callback: Optional[Callable[[ImpellerStatus], None]] = None):
        self.status_callback = status_callback
        self.status = ImpellerStatus()
        self.commanded_rpm: Optional[int] = None  # last value we sent
        self._rx_buffer = ""
        self._connected = False

    # ---------------------------------------------------------- public API
    @property
    def name(self) -> str:
        return "link"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if self._connected:
            return
        self._open()
        self._rx_buffer = ""
        self.status = ImpellerStatus()
        self.commanded_rpm = None
        self._connected = True

    def disconnect(self, stop: bool = True) -> None:
        if not self._connected:
            return
        try:
            if stop:
                self.stop()
        except LinkError:
            pass
        finally:
            self._connected = False
            self._close()

    def set_target_rpm(self, rpm: int) -> None:
        command = format_target_command(rpm)
        self._send(command)
        self.commanded_rpm = int(rpm)

    def stop(self) -> None:
        self._send("S0")
        self.commanded_rpm = 0

    def request_status(self) -> None:
        self._send("?")

    def set_status_interval_ms(self, interval_ms: int) -> None:
        """Ask the firmware to print status every ``interval_ms`` (``R`` command, newer firmware)."""
        interval_ms = int(interval_ms)
        if not (100 <= interval_ms <= 5000):
            raise ValueError("status interval must be 100..5000 ms")
        self._send(f"R{interval_ms}")

    def poll(self) -> list[str]:
        """Read whatever is available, parse status lines, return complete text lines."""
        if not self._connected:
            return []

        chunk = self._read_available()
        if not chunk:
            return []

        self._rx_buffer += chunk
        raw_lines = self._rx_buffer.splitlines(keepends=True)
        self._rx_buffer = ""
        if raw_lines and not raw_lines[-1].endswith(("\n", "\r")):
            self._rx_buffer = raw_lines.pop()

        lines: list[str] = []
        for raw in raw_lines:
            line = raw.strip()
            if not line:
                continue
            lines.append(line)
            status = parse_status_line(line)
            if status is not None:
                self.status = status
                if self.status_callback:
                    self.status_callback(status)
        return lines

    def status_is_fresh(self, max_age_s: float = 2.0) -> bool:
        age = self.status.age
        return age is not None and age <= max_age_s

    # -------------------------------------------------------- subclass API
    def _send(self, command: str) -> None:
        if not self._connected:
            raise LinkError("controller is not connected")
        self._write_line(command)

    def _open(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    def _write_line(self, command: str) -> None:
        raise NotImplementedError

    def _read_available(self) -> str:
        raise NotImplementedError


class SerialImpellerLink(ImpellerLink):
    """pyserial-backed link to the Arduino."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, **kwargs):
        super().__init__(**kwargs)
        self.port = port
        self.baud = int(baud)
        self._serial = None

    @property
    def name(self) -> str:
        return f"{self.port} @ {self.baud}"

    def _open(self) -> None:
        import serial  # imported lazily so the parser/tests do not need pyserial

        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=0, write_timeout=1.0)
            self._serial.reset_input_buffer()
        except (serial.SerialException, OSError, ValueError) as exc:
            self._serial = None
            raise LinkError(f"could not open {self.port}: {exc}") from exc

    def _close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._serial = None

    def _write_line(self, command: str) -> None:
        import serial

        try:
            self._serial.write(f"{command}\n".encode("ascii"))
        except (serial.SerialException, OSError) as exc:
            self._connected = False
            raise LinkError(f"write to {self.port} failed: {exc}") from exc

    def _read_available(self) -> str:
        import serial

        try:
            pending = self._serial.in_waiting
            if not pending:
                return ""
            return self._serial.read(pending).decode("utf-8", errors="replace")
        except (serial.SerialException, OSError) as exc:
            self._connected = False
            raise LinkError(f"read from {self.port} failed: {exc}") from exc


def list_serial_ports() -> list[tuple[str, str]]:
    """Return ``[(device, description), ...]`` for the ports pyserial can see."""
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover
        return []
    return [(p.device, p.description) for p in list_ports.comports()]
