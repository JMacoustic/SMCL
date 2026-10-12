import csv
import json
import tempfile
import time
import unittest
from pathlib import Path

from impeller_control.csv_logger import COLUMNS, CsvLogger, make_log_path
from impeller_control.protocol import ImpellerLink, LinkError, format_target_command, parse_status_line
from impeller_control.simulator import SimulatedImpellerLink

STATUS_LINE = (
    "OK t_ms=123456 rpm_t_ms=123250 target_rpm=300 measured_rpm=298 raw_rpm=301 output=42.10 kp=0.0700 ki=0.0200 kd=0.0030 "
    "strength=42.100 calib_points=10 pulse_ticks=56 period_ticks=1600 pulse_ns=3500 rest_us=97 "
    "period_us=100 duty_ppm=35000 max_duty_ppm=70000"
)


class ParseTests(unittest.TestCase):
    def test_parses_all_fields(self):
        status = parse_status_line(STATUS_LINE, now=123.0)
        self.assertIsNotNone(status)
        self.assertEqual(status.target_rpm, 300)
        self.assertEqual(status.measured_rpm, 298.0)
        self.assertEqual(status.raw_rpm, 301.0)
        self.assertAlmostEqual(status.output, 42.10)
        self.assertAlmostEqual(status.strength, 42.1)
        self.assertAlmostEqual(status.kp, 0.07)
        self.assertEqual(status.updated_at, 123.0)
        self.assertEqual(status.fields["max_duty_ppm"], 70000.0)
        self.assertEqual(status.fields["t_ms"], 123456.0)
        self.assertEqual(status.fields["rpm_t_ms"], 123250.0)

    def test_ignores_non_status_lines(self):
        self.assertIsNone(parse_status_line("Impeller PID controller ready."))
        self.assertIsNone(parse_status_line("ERR use T0-T1200"))

    def test_target_command_bounds(self):
        self.assertEqual(format_target_command(0), "T0")
        self.assertEqual(format_target_command(1200), "T1200")
        with self.assertRaises(ValueError):
            format_target_command(1201)
        with self.assertRaises(ValueError):
            format_target_command(-1)


class FakeLink(ImpellerLink):
    """Scripted link to exercise the buffering logic of ImpellerLink.poll()."""

    def __init__(self):
        super().__init__()
        self.sent = []
        self.chunks = []

    def _open(self):
        pass

    def _close(self):
        pass

    def _write_line(self, command):
        self.sent.append(command)

    def _read_available(self):
        return self.chunks.pop(0) if self.chunks else ""


class LinkBufferingTests(unittest.TestCase):
    def test_partial_lines_are_reassembled(self):
        link = FakeLink()
        link.connect()
        link.chunks = ["OK target_rpm=10 measured_rpm=9 raw", "_rpm=9 output=1.00\r\nERR something\r\nOK target_rpm="]
        self.assertEqual(link.poll(), [])  # first chunk has no complete line
        self.assertEqual(link.poll(), ["OK target_rpm=10 measured_rpm=9 raw_rpm=9 output=1.00", "ERR something"])
        self.assertEqual(link.status.target_rpm, 10)
        self.assertEqual(link.status.raw_rpm, 9.0)
        link.chunks = ["20 measured_rpm=18\n"]
        self.assertEqual(link.poll(), ["OK target_rpm=20 measured_rpm=18"])
        self.assertEqual(link.status.target_rpm, 20)

    def test_commands_and_connection_guard(self):
        link = FakeLink()
        with self.assertRaises(LinkError):
            link.set_target_rpm(100)
        link.connect()
        link.set_target_rpm(250)
        link.stop()
        link.request_status()
        self.assertEqual(link.sent, ["T250", "S0", "?"])
        self.assertEqual(link.commanded_rpm, 0)
        link.disconnect(stop=True)
        self.assertEqual(link.sent[-1], "S0")
        self.assertFalse(link.is_connected)


class SimulatorTests(unittest.TestCase):
    def test_simulator_speaks_the_protocol(self):
        link = SimulatedImpellerLink(time_constant_s=0.05, noise_rpm=0.0, seed=1)
        link.connect()
        lines = link.poll()
        self.assertTrue(any("SIMULATED" in line for line in lines))
        self.assertEqual(link.status.target_rpm, 0)
        self.assertIn("t_ms", link.status.fields)
        self.assertIn("rpm_t_ms", link.status.fields)

        link.set_target_rpm(400)
        link.poll()
        self.assertEqual(link.status.target_rpm, 400)
        time.sleep(0.6)
        link.poll()
        self.assertGreater(link.status.measured_rpm, 380)

        link._write_line("T9999")
        self.assertIn("ERR use T0-T1200", link.poll())

        link.set_status_interval_ms(500)
        link.poll()
        self.assertEqual(link.status.fields["status_ms"], 500.0)
        t0 = time.monotonic()
        count = 0
        while time.monotonic() - t0 < 1.2:
            count += sum(1 for line in link.poll() if line.startswith("OK"))
            time.sleep(0.02)
        self.assertTrue(1 <= count <= 3, count)  # ~2 periodic lines per second at R500
        with self.assertRaises(ValueError):
            link.set_status_interval_ms(50)
        link.disconnect()
        self.assertFalse(link.is_connected)

    def test_simulator_stops_on_command_timeout_unless_queried(self):
        link = SimulatedImpellerLink(time_constant_s=0.05, noise_rpm=0.0, command_timeout_s=0.3)
        link.connect()
        link.set_target_rpm(200)
        for _ in range(4):  # queries inside the timeout keep it alive
            time.sleep(0.15)
            link.request_status()
            link.poll()
        self.assertEqual(link.status.target_rpm, 200)
        time.sleep(0.5)  # silence longer than the timeout
        lines = link.poll()
        self.assertTrue(any("command timeout" in line for line in lines))
        self.assertEqual(link.status.target_rpm, 0)
        link.disconnect()


class CsvLoggerTests(unittest.TestCase):
    def test_writes_header_rows_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = make_log_path(folder, "test")
            logger = CsvLogger()
            logger.open(path, metadata={"ramp": {"target_rpm": 300}})
            logger.write({"phase": "ramping", "commanded_rpm": 120, "measured_rpm": 118.0, "kp": 0.07, "controller_time_s": 12.345})
            logger.write({"phase": "holding", "commanded_rpm": 300, "measured_rpm": 299.5})
            self.assertEqual(logger.rows_written, 2)
            closed = logger.close()
            self.assertEqual(closed, path)
            self.assertFalse(logger.is_open)

            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0].keys()), COLUMNS)
            self.assertEqual(rows[0]["phase"], "ramping")
            self.assertEqual(rows[0]["commanded_rpm"], "120")
            self.assertEqual(rows[0]["measured_rpm"], "118")
            self.assertEqual(rows[0]["kp"], "0.07")
            self.assertEqual(rows[0]["controller_time_s"], "12.345")
            self.assertEqual(rows[1]["controller_time_s"], "")
            self.assertEqual(rows[1]["measured_rpm"], "299.5")
            self.assertEqual(rows[1]["run_elapsed_s"], "")
            self.assertTrue(rows[0]["timestamp"])

            meta = json.loads(path.with_name(path.stem + "_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["ramp"]["target_rpm"], 300)

    def test_log_path_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = make_log_path(folder, "run")
            first.write_text("x")
            second = make_log_path(folder, "run")
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.startswith("run_"))

    def test_write_without_open_is_a_noop(self):
        logger = CsvLogger()
        logger.write({"phase": "idle"})
        self.assertEqual(logger.rows_written, 0)


if __name__ == "__main__":
    unittest.main()
