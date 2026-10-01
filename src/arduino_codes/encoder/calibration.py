import argparse
import math
import threading
import time
from collections import deque
from pathlib import Path

import serial


DEFAULT_PORT = "COM10"
DEFAULT_BAUD = 115200
DEFAULT_SAMPLE_COUNT = 8
CONFIG_PATH = Path("configs/calib.yaml")
CALIB_POINT_REPLACE_TOLERANCE_RPM = 5.0


class RpmSamples:
    def __init__(self, maxlen):
        self._samples = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, rpm):
        with self._lock:
            self._samples.append((time.time(), rpm))

    def average(self):
        with self._lock:
            values = [rpm for _, rpm in self._samples if rpm > 0.1]

        if not values:
            return None, 0

        return sum(values) / len(values), len(values)


def parse_args():
    parser = argparse.ArgumentParser(description="Calibrate custom encoder RPM against the original controller.")
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"Serial port, default {DEFAULT_PORT}")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate, default {DEFAULT_BAUD}")
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLE_COUNT,
        help=f"Recent smoothed RPM samples to average, default {DEFAULT_SAMPLE_COUNT}",
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Calibration YAML path")
    return parser.parse_args()


def parse_encoder_line(line):
    parts = line.strip().split(",")
    if len(parts) != 7:
        return None

    try:
        return {
            "count": int(parts[0]),
            "delta": int(parts[1]),
            "counts_per_second": float(parts[2]),
            "rpm_raw": float(parts[3]),
            "rpm_smooth": float(parts[4]),
            "a": int(parts[5]),
            "b": int(parts[6]),
        }
    except ValueError:
        return None


def read_serial(port, samples, stop_event):
    while not stop_event.is_set():
        try:
            raw_line = port.readline()
        except serial.SerialException as exc:
            print(f"\nSerial read failed: {exc}")
            stop_event.set()
            return

        if not raw_line:
            continue

        line = raw_line.decode("utf-8", errors="replace").strip()
        reading = parse_encoder_line(line)
        if reading is None:
            if line:
                print(f"\nArduino: {line}")
            continue

        samples.add(reading["rpm_smooth"])
        print(
            f"\rMeasured smooth RPM: {reading['rpm_smooth']:8.0f} "
            f"(raw {reading['rpm_raw']:8.0f})   ",
            end="",
            flush=True,
        )


def read_simple_yaml(path):
    data = {}
    if not path.exists():
        return data

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        data[key.strip()] = parse_scalar(value.strip())

    return data


def parse_scalar(value):
    if value == "":
        return ""

    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"

    try:
        if any(char in value for char in ".eE"):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("\"'")


def write_simple_yaml(path, data):
    ordered_keys = [
        "counts_per_revolution",
        "rpm_scale",
        "calib_point_count",
        "last_reference_rpm",
        "last_measured_rpm",
        "sample_count",
        "pid_min_rpm",
        "pid_max_rpm",
    ]

    lines = []
    emitted = set()

    for key in ordered_keys:
        if key in data:
            lines.append(f"{key}: {format_scalar(data[key])}")
            emitted.add(key)

    for key in sorted(data):
        if key not in emitted:
            lines.append(f"{key}: {format_scalar(data[key])}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_scalar(value):
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.6g}"
        return "0"
    return str(value)


def read_calibration_points(data):
    points = []
    point_count = int(data.get("calib_point_count", 0) or 0)

    for index in range(point_count):
        measured = data.get(f"calib_measured_{index}")
        reference = data.get(f"calib_reference_{index}")

        if measured is None or reference is None:
            continue

        try:
            measured = float(measured)
            reference = float(reference)
        except (TypeError, ValueError):
            continue

        if measured > 0.0 and reference > 0.0:
            points.append((measured, reference))

    if not points:
        measured = data.get("last_measured_rpm")
        reference = data.get("last_reference_rpm")

        try:
            measured = float(measured)
            reference = float(reference)
        except (TypeError, ValueError):
            measured = 0.0
            reference = 0.0

        if measured > 0.0 and reference > 0.0:
            points.append((measured, reference))

    return sorted(points, key=lambda point: point[0])


def upsert_calibration_point(points, measured_rpm, reference_rpm):
    updated = []
    replaced = False

    for measured, reference in points:
        if not replaced and abs(measured - measured_rpm) <= CALIB_POINT_REPLACE_TOLERANCE_RPM:
            updated.append((measured_rpm, reference_rpm))
            replaced = True
        else:
            updated.append((measured, reference))

    if not replaced:
        updated.append((measured_rpm, reference_rpm))

    return sorted(updated, key=lambda point: point[0])


def write_calibration_points(data, points):
    for key in list(data):
        if key == "calib_point_count" or key.startswith("calib_measured_") or key.startswith("calib_reference_"):
            del data[key]

    data["calib_point_count"] = len(points)
    for index, (measured, reference) in enumerate(points):
        data[f"calib_measured_{index}"] = measured
        data[f"calib_reference_{index}"] = reference


def print_arduino_calibration_snippet(points):
    print("\nCopy these constants into impeller/impeller.ino:")
    print(f"const uint8_t RPM_CALIB_POINT_COUNT = {len(points)};")
    print(f"const float RPM_CALIB_MEASURED[RPM_CALIB_POINT_COUNT] = {{{format_float_list(points, 0)}}};")
    print(f"const float RPM_CALIB_REFERENCE[RPM_CALIB_POINT_COUNT] = {{{format_float_list(points, 1)}}};")


def format_float_list(points, item_index):
    return ", ".join(f"{point[item_index]:.2f}f" for point in points)


def calibrate(config_path, samples, reference_rpm):
    measured_rpm, sample_count = samples.average()
    if measured_rpm is None:
        print("\nNo positive encoder RPM samples are available yet.")
        return

    if measured_rpm <= 0.0:
        print("\nMeasured RPM is zero; calibration skipped.")
        return

    rpm_scale = reference_rpm / measured_rpm
    data = read_simple_yaml(config_path)
    data.setdefault("counts_per_revolution", 500.0)
    data.setdefault("pid_min_rpm", 0)
    data.setdefault("pid_max_rpm", 1200)
    points = upsert_calibration_point(read_calibration_points(data), measured_rpm, reference_rpm)
    write_calibration_points(data, points)
    data["rpm_scale"] = rpm_scale
    data["last_reference_rpm"] = reference_rpm
    data["last_measured_rpm"] = measured_rpm
    data["sample_count"] = sample_count
    write_simple_yaml(config_path, data)

    print(
        f"\nSaved calibration: reference={reference_rpm:.2f} RPM, "
        f"measured_avg={measured_rpm:.2f} RPM, scale={rpm_scale:.6f}"
    )
    print(f"Calibration points: {len(points)}")
    print_arduino_calibration_snippet(points)
    print(f"Updated {config_path}")


def main():
    args = parse_args()
    config_path = Path(args.config)
    samples = RpmSamples(max(1, args.samples))
    stop_event = threading.Event()

    try:
        port = serial.Serial(args.port, args.baud, timeout=0.2)
    except serial.SerialException as exc:
        print(f"Connection failed on {args.port} at {args.baud}: {exc}")
        return 1

    print(f"Connected to {args.port} at {args.baud}. Arduino may reset for a moment.")
    print("Type the RPM shown on the original controller, then press Enter.")
    print("Type q and press Enter to quit.")

    reader = threading.Thread(target=read_serial, args=(port, samples, stop_event), daemon=True)
    reader.start()

    try:
        while not stop_event.is_set():
            text = input("\nOriginal controller RPM> ").strip()
            if text.lower() in {"q", "quit", "exit"}:
                break

            try:
                reference_rpm = float(text)
            except ValueError:
                print("Enter a numeric RPM value or q to quit.")
                continue

            if reference_rpm <= 0.0:
                print("Reference RPM must be greater than zero.")
                continue

            calibrate(config_path, samples, reference_rpm)
    except KeyboardInterrupt:
        print("\nStopping calibration.")
    finally:
        stop_event.set()
        port.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
