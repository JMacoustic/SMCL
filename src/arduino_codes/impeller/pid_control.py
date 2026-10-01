from __future__ import annotations

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import cv2
import serial
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from camera_utils import open_configured_camera
from impeller_mixer import DEFAULT_BAUD, DEFAULT_PORT, ImpellerMixer


DEFAULT_TARGET_RPM = 0
CONFIG_PATH = Path("configs/calib.yaml")


class PidControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.config = read_simple_yaml(CONFIG_PATH)
        self.min_rpm = int(float(self.config.get("pid_min_rpm", 0)))
        self.max_rpm = int(float(self.config.get("pid_max_rpm", 1200)))

        if self.max_rpm <= self.min_rpm:
            self.min_rpm = 0
            self.max_rpm = 1200

        self.mixer: ImpellerMixer | None = None
        self.capture = None
        self.pending_target_rpm = DEFAULT_TARGET_RPM

        self.setWindowTitle("Impeller RPM Control")
        self.resize(1100, 680)

        self.port_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 1_000_000)
        self.baud_spin.setValue(DEFAULT_BAUD)
        self.baud_spin.setSingleStep(1200)

        self.connect_button = QPushButton("Connect")
        self.status_label = QLabel("Disconnected")

        self.target_slider = QSlider(Qt.Orientation.Horizontal)
        self.target_slider.setRange(self.min_rpm, self.max_rpm)
        self.target_slider.setValue(DEFAULT_TARGET_RPM)
        self.target_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.target_slider.setTickInterval(max(50, (self.max_rpm - self.min_rpm) // 10))
        self.target_slider.setMinimumWidth(420)

        self.target_spin = QSpinBox()
        self.target_spin.setRange(self.min_rpm, self.max_rpm)
        self.target_spin.setValue(DEFAULT_TARGET_RPM)
        self.target_spin.setSingleStep(1)
        self.target_spin.setSuffix(" RPM")

        self.current_rpm_value = QLabel("-- RPM")
        self.current_rpm_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.current_rpm_value.setMinimumWidth(120)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Set target RPM to 0")

        self.preview_label = QLabel("Camera preview unavailable")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(640, 480)
        self.preview_label.setStyleSheet("background: #111; color: #ddd;")

        self.send_timer = QTimer(self)
        self.send_timer.setSingleShot(True)
        self.send_timer.setInterval(50)
        self.send_timer.timeout.connect(self.send_pending_target)

        self.read_timer = QTimer(self)
        self.read_timer.setInterval(100)
        self.read_timer.timeout.connect(self.read_mixer)

        self.video_timer = QTimer(self)
        self.video_timer.setInterval(15)
        self.video_timer.timeout.connect(self.update_preview)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.stop_button.clicked.connect(self.stop_motor)
        self.target_slider.valueChanged.connect(self.on_slider_changed)
        self.target_spin.valueChanged.connect(self.on_spin_changed)

        self.setCentralWidget(self.build_ui())
        self.refresh_ports()
        self.start_camera()

    def build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        connection_layout = QGridLayout()
        connection_layout.addWidget(QLabel("Port"), 0, 0)
        connection_layout.addWidget(self.port_combo, 0, 1)
        connection_layout.addWidget(self.refresh_button, 0, 2)
        connection_layout.addWidget(QLabel("Baud"), 0, 3)
        connection_layout.addWidget(self.baud_spin, 0, 4)
        connection_layout.addWidget(self.connect_button, 0, 5)
        connection_layout.addWidget(self.status_label, 0, 6)
        connection_layout.setColumnStretch(1, 1)
        layout.addLayout(connection_layout)

        control_layout = QGridLayout()
        control_layout.addWidget(QLabel("Target"), 0, 0)
        control_layout.addWidget(self.target_slider, 0, 1)
        control_layout.addWidget(self.target_spin, 0, 2)
        control_layout.addWidget(self.stop_button, 0, 3)
        control_layout.addWidget(QLabel("Measured"), 1, 0)
        control_layout.addWidget(self.current_rpm_value, 1, 2)
        control_layout.setColumnStretch(1, 1)
        layout.addLayout(control_layout)

        preview_layout = QHBoxLayout()
        preview_layout.addWidget(self.preview_label, 1)
        layout.addLayout(preview_layout, 1)

        return root

    def refresh_ports(self):
        current = self.port_combo.currentData()
        self.port_combo.clear()

        ports = list(list_ports.comports())
        for port in ports:
            label = f"{port.device} - {port.description}"
            self.port_combo.addItem(label, port.device)

        if not ports:
            self.port_combo.addItem("No serial ports found", None)
        elif current and self.port_combo.findData(current) >= 0:
            self.port_combo.setCurrentIndex(self.port_combo.findData(current))
        else:
            index = self.port_combo.findData(DEFAULT_PORT)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def toggle_connection(self):
        if self.mixer and self.mixer.is_connected:
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self):
        port = self.port_combo.currentData()
        if not port:
            self.status_label.setText("No serial port selected")
            return

        self.mixer = ImpellerMixer(port=port, baud=self.baud_spin.value())

        try:
            self.mixer.connect()
        except serial.SerialException as exc:
            self.mixer = None
            self.status_label.setText(f"Connection failed: {exc}")
            return

        self.connect_button.setText("Disconnect")
        self.status_label.setText(f"Connected to {port}")
        self.read_timer.start()
        QTimer.singleShot(1800, self.send_pending_target)

    def disconnect_serial(self):
        self.read_timer.stop()

        if self.mixer:
            self.mixer.disconnect()
            self.mixer = None

        self.connect_button.setText("Connect")
        self.status_label.setText("Disconnected")
        self.current_rpm_value.setText("-- RPM")

    def on_slider_changed(self, value):
        if self.target_spin.value() != value:
            self.target_spin.blockSignals(True)
            self.target_spin.setValue(value)
            self.target_spin.blockSignals(False)

        self.set_pending_target(value)

    def on_spin_changed(self, value):
        if self.target_slider.value() != value:
            self.target_slider.blockSignals(True)
            self.target_slider.setValue(value)
            self.target_slider.blockSignals(False)

        self.set_pending_target(value)

    def set_pending_target(self, value):
        self.pending_target_rpm = int(value)
        if self.mixer and self.mixer.is_connected:
            self.send_timer.start()

    def stop_motor(self):
        self.target_slider.setValue(0)
        self.target_spin.setValue(0)
        self.pending_target_rpm = 0
        self.send_pending_target()

    def send_pending_target(self):
        if not self.mixer or not self.mixer.is_connected:
            return

        try:
            self.mixer.set_target_rpm(self.pending_target_rpm)
        except serial.SerialException as exc:
            self.status_label.setText(f"Write failed: {exc}")
            self.disconnect_serial()

    def read_mixer(self):
        if not self.mixer or not self.mixer.is_connected:
            return

        try:
            self.mixer.poll()
        except serial.SerialException as exc:
            self.status_label.setText(f"Read failed: {exc}")
            self.disconnect_serial()
            return

        rpm = self.mixer.current_rpm
        if rpm is not None:
            self.current_rpm_value.setText(f"{rpm:.0f} RPM")

    def start_camera(self):
        try:
            self.capture, _camera_props = open_configured_camera()
        except RuntimeError as exc:
            self.preview_label.setText(str(exc))
            return

        self.video_timer.start()

    def update_preview(self):
        if not self.capture:
            return

        ok, frame = self.capture.read()
        if not ok:
            self.preview_label.setText("Camera frame read failed")
            return

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        bytes_per_line = channels * width
        image = QImage(rgb.data, width, height, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(image).scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(pixmap)

    def closeEvent(self, event):
        self.disconnect_serial()
        self.video_timer.stop()

        if self.capture:
            self.capture.release()
            self.capture = None

        event.accept()


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

    try:
        if any(char in value for char in ".eE"):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("\"'")


def main():
    app = QApplication(sys.argv)
    window = PidControlWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
