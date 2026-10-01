from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import serial
from serial.tools import list_ports


DEFAULT_BAUD = 115200
DEFAULT_PORT = "COM11"
STATUS_RE = re.compile(r"(?P<key>[A-Za-z_]+)=(?P<value>[-+]?\d+(?:\.\d+)?)")


@dataclass
class MixerStatus:
    target_rpm: Optional[int] = None
    measured_rpm: Optional[float] = None
    raw_rpm: Optional[float] = None
    output: Optional[float] = None
    kp: Optional[float] = None
    ki: Optional[float] = None
    kd: Optional[float] = None
    updated_at: Optional[float] = None
    fields: dict[str, float] = field(default_factory=dict)
    line: str = ""


class ImpellerMixer:
    """Thin Python wrapper for the Arduino impeller PID serial protocol."""

    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = DEFAULT_BAUD,
        timeout: float = 0.0,
        status_callback: Optional[Callable[[MixerStatus], None]] = None,
    ):
        self.port = port or DEFAULT_PORT
        self.baud = int(baud)
        self.timeout = timeout
        self.status_callback = status_callback
        self.serial_port: Optional[serial.Serial] = None
        self.status = MixerStatus()
        self.target_rpm = 0
        self._rx_buffer = ""

    @staticmethod
    def available_ports():
        return list(list_ports.comports())

    @property
    def is_connected(self) -> bool:
        return bool(self.serial_port and self.serial_port.is_open)

    @property
    def current_rpm(self) -> Optional[float]:
        return self.status.measured_rpm

    @property
    def last_status_age(self) -> Optional[float]:
        if self.status.updated_at is None:
            return None
        return time.monotonic() - self.status.updated_at

    def connect(self):
        if self.is_connected:
            return

        self.serial_port = serial.Serial(self.port, self.baud, timeout=self.timeout)
        self.serial_port.reset_input_buffer()

    def disconnect(self, stop: bool = True):
        if not self.serial_port:
            return

        try:
            if stop and self.serial_port.is_open:
                self.stop()
        except serial.SerialException:
            pass
        finally:
            self.serial_port.close()
            self.serial_port = None

    def close(self):
        self.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.disconnect()

    def set_target_rpm(self, rpm: int):
        rpm = max(0, int(rpm))
        self.target_rpm = rpm
        self._send_command(f"T{rpm}")

    def stop(self):
        self.target_rpm = 0
        self._send_command("S0")

    def request_status(self):
        self._send_command("?")

    def poll(self, max_lines: Optional[int] = None) -> list[str]:
        if not self.is_connected:
            return []

        lines: list[str] = []
        pending = self.serial_port.in_waiting
        if not pending:
            return lines

        self._rx_buffer += self.serial_port.read(pending).decode("utf-8", errors="replace")
        raw_lines = self._rx_buffer.splitlines(keepends=True)
        self._rx_buffer = ""

        if raw_lines and not raw_lines[-1].endswith(("\n", "\r")):
            self._rx_buffer = raw_lines.pop()

        if max_lines is not None and len(raw_lines) > max_lines:
            lines_to_process = raw_lines[:max_lines]
            self._rx_buffer = "".join(raw_lines[max_lines:]) + self._rx_buffer
        else:
            lines_to_process = raw_lines

        for raw_line in lines_to_process:
            line = raw_line.strip()
            if line:
                lines.append(line)
                status = self.parse_status_line(line)
                if status:
                    self.status = status
                    if self.status_callback:
                        self.status_callback(status)

        return lines

    def current_rpm_is_recent(self, max_age_seconds: float = 2.0) -> bool:
        age = self.last_status_age
        return age is not None and age <= max_age_seconds and self.current_rpm is not None

    def _send_command(self, command: str):
        if not self.is_connected:
            raise RuntimeError("Mixer serial port is not connected.")

        try:
            self.serial_port.write(f"{command}\n".encode("ascii"))
        except serial.SerialException:
            self.serial_port = None
            raise

    @staticmethod
    def parse_status_line(line: str) -> Optional[MixerStatus]:
        if "target_rpm=" not in line or "measured_rpm=" not in line:
            return None

        fields: dict[str, float] = {}
        for match in STATUS_RE.finditer(line):
            fields[match.group("key")] = float(match.group("value"))

        if not fields:
            return None

        return MixerStatus(
            target_rpm=int(fields["target_rpm"]) if "target_rpm" in fields else None,
            measured_rpm=fields.get("measured_rpm"),
            raw_rpm=fields.get("raw_rpm"),
            output=fields.get("output"),
            kp=fields.get("kp"),
            ki=fields.get("ki"),
            kd=fields.get("kd"),
            updated_at=time.monotonic(),
            fields=fields,
            line=line,
        )
