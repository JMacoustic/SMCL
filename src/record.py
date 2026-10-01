from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import serial
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QWidget,
)

from camera_utils import (
    ORBBEC_RGB_VID_PID,
    open_configured_camera,
)
from impeller_mixer import DEFAULT_BAUD, DEFAULT_PORT, ImpellerMixer
from tapo_utils import (
    LIGHT_CONDITIONS,
    apply_light_condition,
    parse_light_condition,
    validate_light_condition,
)


TARGET_RPMS = (270, 290, 310, 330, 350, 370, 390, 410, 430, 450)
RPM_TOLERANCE = 10
STABLE_SECONDS = 3.0
POST_STABLE_MIX_SECONDS = 2.0
DISSIPATION_SECONDS = 5.0
PRE_ROLL_SECONDS = 1.0
DEFAULT_RECORD_CAMERA_INDEX = 2
DEFAULT_RECORD_CAMERA_WIDTH = 1920
DEFAULT_RECORD_CAMERA_HEIGHT = 1080
DEFAULT_RECORD_CAMERA_FPS = 30.0
DEFAULT_RECORD_CAMERA_BACKEND = cv2.CAP_DSHOW
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
VIDEO_DIR = DATA_DIR / "videos"
CONFIG_DIR = DATA_DIR / "configs"
RPM_DIR = DATA_DIR / "rpms"


@dataclass
class ProtocolState:
    phase: str = "pre_roll"
    cycle_index: int = 0
    target_rpm: int = 0
    stable_since: Optional[float] = None
    stop_at: Optional[float] = None
    stopped_at: Optional[float] = None


class RecordingMonitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.closed = False

        self.setWindowTitle("Impeller Mixing Recording")
        self.resize(1120, 520)

        self.preview_label = QLabel("Camera preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(760, 428)
        self.preview_label.setStyleSheet("background: #111; color: #ddd;")

        self.status_text = QPlainTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMinimumWidth(300)
        self.status_text.setStyleSheet(
            "QPlainTextEdit {"
            "background: #202226;"
            "color: #f0f0f0;"
            "border: 1px solid #444;"
            "font-family: Consolas, monospace;"
            "font-size: 18px;"
            "padding: 14px;"
            "}"
        )

        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        layout.addWidget(self.preview_label, 1)
        layout.addWidget(self.status_text)
        self.setCentralWidget(root)

    def update_display(self, frame, status_text: str):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        bytes_per_line = channels * width
        image = QImage(
            rgb.data,
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        ).copy()
        pixmap = QPixmap.fromImage(image).scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(pixmap)
        self.status_text.setPlainText(status_text)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.closed = True
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closed = True
        event.accept()


class MixingRecorder:
    def __init__(
        self,
        mixer: ImpellerMixer,
        camera_index: Optional[int],
        camera_width: Optional[int],
        camera_height: Optional[int],
        camera_fps: Optional[float],
        preview: bool,
        viscosity_class: int,
        light_condition: Optional[int] = None,
        pre_roll_seconds: float = PRE_ROLL_SECONDS,
        targets: tuple[int, ...] = TARGET_RPMS,
    ):
        self.mixer = mixer
        self.camera_index = DEFAULT_RECORD_CAMERA_INDEX if camera_index is None else camera_index
        self.camera_width = DEFAULT_RECORD_CAMERA_WIDTH if camera_width is None else camera_width
        self.camera_height = DEFAULT_RECORD_CAMERA_HEIGHT if camera_height is None else camera_height
        self.camera_fps = DEFAULT_RECORD_CAMERA_FPS if camera_fps is None else camera_fps
        self.preview = preview
        self.viscosity_class = validate_viscosity_class(viscosity_class)
        self.light_condition = (
            None if light_condition is None else validate_light_condition(light_condition)
        )
        self.pre_roll_seconds = pre_roll_seconds
        self.targets = targets
        self.state = ProtocolState()
        self.frame_index = 0
        self.start_perf = 0.0
        self.start_timestamp = ""
        self.run_id = ""
        self.file_stem = ""
        self.video_path = Path()
        self.frame_log_path = Path()
        self.config_path = Path()
        self.light_states: dict[str, dict[str, object]] = {}

    def run(self):
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RPM_DIR.mkdir(parents=True, exist_ok=True)

        capture = None
        writer = None
        frame_log_file = None
        preview_app = None
        monitor = None

        try:
            self.apply_lighting()
            print(f"Connecting mixer on {self.mixer.port} @ {self.mixer.baud} baud.")
            self.mixer.connect()
            time.sleep(1.8)
            self.mixer.request_status()

            camera_started_at = time.perf_counter()
            print(
                "Opening camera "
                f"index {self.camera_index} at "
                f"{self.camera_width}x{self.camera_height}@{self.camera_fps:g} "
                "with DirectShow."
            )
            capture, camera_props = open_configured_camera(
                camera_index=self.camera_index,
                width=self.camera_width,
                height=self.camera_height,
                fps=self.camera_fps,
                backend=DEFAULT_RECORD_CAMERA_BACKEND,
            )
            print(
                "Camera opened: "
                f"{camera_props.width}x{camera_props.height}@{camera_props.fps:g} "
                f"in {time.perf_counter() - camera_started_at:.1f}s."
            )

            writer_fps = camera_props.fps if camera_props.fps > 1.0 else 30.0
            self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.file_stem = build_file_stem(
                self.viscosity_class,
                self.run_id,
                self.light_condition,
            )
            self.video_path = VIDEO_DIR / f"{self.file_stem}.mp4"
            self.frame_log_path = RPM_DIR / f"{self.file_stem}_frames.csv"
            self.config_path = CONFIG_DIR / f"{self.file_stem}.json"

            writer = cv2.VideoWriter(
                str(self.video_path),
                cv2.VideoWriter_fourcc(*"mp4v"),
                writer_fps,
                (camera_props.width, camera_props.height),
            )
            if not writer.isOpened():
                raise RuntimeError(f"Could not open video writer: {self.video_path}")

            frame_log_file = self.frame_log_path.open("w", newline="", encoding="utf-8")
            frame_log = csv.DictWriter(
                frame_log_file,
                fieldnames=[
                    "frame_index",
                    "elapsed_s",
                    "wall_time_s",
                    "cycle_number",
                    "phase",
                    "target_rpm",
                    "mixer_target_rpm",
                    "measured_rpm",
                    "raw_rpm",
                    "rpm_status_age_s",
                ],
            )
            frame_log.writeheader()

            self.start_timestamp = datetime.now().astimezone().isoformat()
            self.start_perf = time.perf_counter()
            self.write_config(camera_props, writer_fps)
            if self.preview:
                preview_app = ensure_qapplication()
                monitor = RecordingMonitor()
                monitor.show()

            print(f"Recording started: {self.video_path}")
            print(f"Frame/RPM log: {self.frame_log_path}")

            while self.state.phase != "done":
                self.mixer.poll()
                ok, frame = capture.read()
                now = time.perf_counter()
                if not ok:
                    raise RuntimeError("Camera frame read failed.")

                self.update_protocol(now)
                writer.write(frame)
                self.write_frame_log(frame_log, now)
                self.frame_index += 1

                if monitor:
                    monitor.update_display(frame, self.monitor_text(now))
                    preview_app.processEvents()
                    if monitor.closed:
                        print("Preview window closed. Stopping experiment.")
                        break

            self.mixer.stop()
            print("Recording complete.")
            print(f"Video: {self.video_path}")
            print(f"Config: {self.config_path}")

        finally:
            try:
                if self.mixer.is_connected:
                    self.mixer.stop()
            except (RuntimeError, serial.SerialException):
                pass
            self.mixer.disconnect(stop=False)

            if writer:
                writer.release()
            if capture:
                capture.release()
            if frame_log_file:
                frame_log_file.close()
            if monitor:
                monitor.close()
                if preview_app:
                    preview_app.processEvents()

    def update_protocol(self, now: float):
        elapsed = now - self.start_perf

        if self.state.phase == "pre_roll":
            if elapsed >= self.pre_roll_seconds:
                self.start_cycle(now, 0)
            return

        if self.state.phase == "accelerating":
            target = self.state.target_rpm
            rpm = self.mixer.current_rpm
            is_recent = self.mixer.current_rpm_is_recent()
            is_stable = is_recent and rpm is not None and abs(rpm - target) <= RPM_TOLERANCE

            if is_stable and self.state.stable_since is None:
                self.state.stable_since = now
                print(f"Target {target} RPM entered tolerance band.")
            elif not is_stable:
                self.state.stable_since = None

            if self.state.stable_since and now - self.state.stable_since >= STABLE_SECONDS:
                self.state.phase = "stable_hold"
                self.state.stop_at = now + POST_STABLE_MIX_SECONDS
                print(f"Target {target} RPM stable for {STABLE_SECONDS:.1f}s. Stop scheduled.")
            return

        if self.state.phase == "stable_hold":
            if self.state.stop_at and now >= self.state.stop_at:
                self.mixer.stop()
                self.state.phase = "dissipation"
                self.state.stopped_at = now
                print(f"Target {self.state.target_rpm} RPM stopped.")
            return

        if self.state.phase == "dissipation":
            if self.state.stopped_at and now - self.state.stopped_at >= DISSIPATION_SECONDS:
                next_cycle = self.state.cycle_index + 1
                if next_cycle >= len(self.targets):
                    self.state.phase = "done"
                else:
                    self.start_cycle(now, next_cycle)

    def start_cycle(self, now: float, cycle_index: int):
        target = self.targets[cycle_index]
        self.mixer.set_target_rpm(target)
        self.state = ProtocolState(
            phase="accelerating",
            cycle_index=cycle_index,
            target_rpm=target,
        )
        print(f"Cycle {cycle_index + 1}/{len(self.targets)} started: target {target} RPM.")

    def apply_lighting(self):
        if self.light_condition is None:
            return

        print(f"Applying light condition {self.light_condition}.")
        self.light_states = apply_light_condition(self.light_condition)
        mismatches = [
            name
            for name, state in self.light_states.items()
            if bool(state["requested_on"]) != bool(state["device_on"])
        ]
        if mismatches:
            raise RuntimeError(f"Tapo light state mismatch: {', '.join(mismatches)}")

        for name, state in self.light_states.items():
            print(
                f"{name}: requested_on={state['requested_on']} "
                f"device_on={state['device_on']}"
            )

    def write_config(self, camera_props, writer_fps: float):
        desired_lights = (
            LIGHT_CONDITIONS[self.light_condition] if self.light_condition is not None else {}
        )
        config = {
            "run_id": self.run_id,
            "file_stem": self.file_stem,
            "viscosity_class": self.viscosity_class,
            "light_condition": self.light_condition,
            "start_timestamp": self.start_timestamp,
            "video_path": str(self.video_path),
            "frame_log_path": str(self.frame_log_path),
            "lighting": {
                "condition": self.light_condition,
                "desired": desired_lights,
                "applied": self.light_states,
            },
            "camera": {
                "requested_vid_pid": ORBBEC_RGB_VID_PID,
                "index": camera_props.index,
                "backend": camera_props.backend,
                "width": camera_props.width,
                "height": camera_props.height,
                "fps": camera_props.fps,
                "fourcc": camera_props.fourcc,
            },
            "video": {
                "writer_fps": writer_fps,
                "codec": "mp4v",
            },
            "mixer": {
                "port": self.mixer.port,
                "baud": self.mixer.baud,
            },
            "protocol": {
                "target_rpms": list(self.targets),
                "rpm_tolerance": RPM_TOLERANCE,
                "stable_seconds": STABLE_SECONDS,
                "post_stable_mix_seconds": POST_STABLE_MIX_SECONDS,
                "dissipation_seconds": DISSIPATION_SECONDS,
                "pre_roll_seconds": self.pre_roll_seconds,
            },
        }

        self.config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_frame_log(self, frame_log: csv.DictWriter, now: float):
        status = self.mixer.status
        age = self.mixer.last_status_age
        cycle_number = self.state.cycle_index + 1 if self.state.phase != "pre_roll" else 0

        frame_log.writerow(
            {
                "frame_index": self.frame_index,
                "elapsed_s": f"{now - self.start_perf:.6f}",
                "wall_time_s": f"{time.time():.6f}",
                "cycle_number": cycle_number,
                "phase": self.state.phase,
                "target_rpm": self.state.target_rpm,
                "mixer_target_rpm": status.target_rpm,
                "measured_rpm": status.measured_rpm,
                "raw_rpm": status.raw_rpm,
                "rpm_status_age_s": f"{age:.6f}" if age is not None else "",
            }
        )

    def monitor_text(self, now: float):
        elapsed = max(0.0, now - self.start_perf)
        current_rpm = self.mixer.current_rpm
        cycle_number = self.state.cycle_index + 1 if self.state.phase != "pre_roll" else 0
        target_rpm = self.state.target_rpm
        light = "--" if self.light_condition is None else f"light{self.light_condition}"
        rpm_text = "--" if current_rpm is None else f"{current_rpm:.0f}"

        return "\n".join(
            [
                f"Elapsed      {format_elapsed(elapsed)}",
                f"Target RPM   {target_rpm}",
                f"Current RPM  {rpm_text}",
                f"Cycle        {cycle_number}/{len(self.targets)}",
                f"Phase        {self.state.phase}",
                f"Light        {light}",
                "",
                f"File         {self.file_stem}",
            ]
        )


def ensure_qapplication():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def format_elapsed(seconds: float):
    total_seconds = int(seconds)
    minutes, sec = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    tenth = int((seconds - total_seconds) * 10)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}.{tenth}"
    return f"{minutes:02d}:{sec:02d}.{tenth}"


def validate_viscosity_class(value: int):
    viscosity_class = int(value)
    if viscosity_class < 0 or viscosity_class > 9:
        raise ValueError("viscosity class must be between 0 and 9.")
    return viscosity_class


def build_file_stem(
    viscosity_class: int,
    run_id: str,
    light_condition: Optional[int] = None,
):
    parts = [f"class{validate_viscosity_class(viscosity_class)}"]
    if light_condition is not None:
        parts.append(f"light{validate_light_condition(light_condition)}")
    parts.append(run_id)
    return "_".join(parts)


def parse_viscosity_class(value: str):
    try:
        return validate_viscosity_class(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args():
    parser = argparse.ArgumentParser(description="Record impeller mixing dissipation cycles.")
    parser.add_argument(
        "--viscosity-class",
        "--visc-class",
        "--class",
        dest="viscosity_class",
        type=parse_viscosity_class,
        required=True,
        help="Viscosity class number from 0 to 9. Used in output filenames, e.g. class0_light0_*.mp4.",
    )
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Arduino serial baud.")
    parser.add_argument(
        "--camera-index",
        type=int,
        default=None,
        help=f"OpenCV camera index. Defaults to Orbbec index {DEFAULT_RECORD_CAMERA_INDEX}.",
    )
    parser.add_argument(
        "--camera-width",
        type=int,
        default=None,
        help=f"Requested camera width. Defaults to {DEFAULT_RECORD_CAMERA_WIDTH}.",
    )
    parser.add_argument(
        "--camera-height",
        type=int,
        default=None,
        help=f"Requested camera height. Defaults to {DEFAULT_RECORD_CAMERA_HEIGHT}.",
    )
    parser.add_argument(
        "--camera-fps",
        type=float,
        default=None,
        help=f"Requested camera FPS. Defaults to {DEFAULT_RECORD_CAMERA_FPS:g}.",
    )
    parser.add_argument(
        "--light-condition",
        "--light",
        dest="light_condition",
        type=parse_light_condition,
        default=None,
        help=(
            "Optional Tapo light condition bitmask: 0 both off, 1 light_1 on, "
            "2 light_2 on, 3 both on."
        ),
    )
    parser.add_argument("--preview", action="store_true", help="Show live preview while recording.")
    parser.add_argument("--pre-roll", type=float, default=PRE_ROLL_SECONDS, help="Seconds to record before first target RPM.")
    return parser.parse_args()


def main():
    args = parse_args()
    mixer = ImpellerMixer(port=DEFAULT_PORT, baud=args.baud)
    recorder = MixingRecorder(
        mixer=mixer,
        camera_index=args.camera_index,
        camera_width=args.camera_width,
        camera_height=args.camera_height,
        camera_fps=args.camera_fps,
        preview=args.preview,
        viscosity_class=args.viscosity_class,
        light_condition=args.light_condition,
        pre_roll_seconds=args.pre_roll,
    )
    recorder.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user.")
        sys.exit(130)
