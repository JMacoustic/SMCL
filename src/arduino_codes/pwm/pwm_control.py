import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QDoubleSpinBox,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import serial
from serial.tools import list_ports


DEFAULT_BAUD = 115200
DEFAULT_PORT = "COM10"
DEFAULT_OUTPUT_MILLI_PERCENT = 0
HEARTBEAT_INTERVAL_MS = 500


class PwmControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.serial_port = None
        self.pending_strength_milli_percent = DEFAULT_OUTPUT_MILLI_PERCENT

        self.setWindowTitle("Impeller Safe PWM Control")
        self.resize(820, 260)

        self.port_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 1_000_000)
        self.baud_spin.setValue(DEFAULT_BAUD)
        self.baud_spin.setSingleStep(1200)

        self.connect_button = QPushButton("Connect")
        self.status_label = QLabel("Disconnected")

        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(0, 100000)
        self.strength_slider.setValue(DEFAULT_OUTPUT_MILLI_PERCENT)
        self.strength_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.strength_slider.setTickInterval(10000)
        self.strength_slider.setMinimumWidth(620)

        self.strength_spin = QDoubleSpinBox()
        self.strength_spin.setRange(0.0, 100.0)
        self.strength_spin.setDecimals(3)
        self.strength_spin.setSingleStep(0.001)
        self.strength_spin.setValue(DEFAULT_OUTPUT_MILLI_PERCENT / 1000.0)
        self.strength_spin.setSuffix("%")
        self.strength_spin.setMinimumWidth(112)

        self.strength_label = QLabel()
        self.strength_label.setMinimumWidth(112)
        self.strength_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop PWM output")

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(95)

        self.send_timer = QTimer(self)
        self.send_timer.setSingleShot(True)
        self.send_timer.setInterval(50)
        self.send_timer.timeout.connect(self.send_pending_strength)

        self.read_timer = QTimer(self)
        self.read_timer.setInterval(100)
        self.read_timer.timeout.connect(self.read_serial)

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(HEARTBEAT_INTERVAL_MS)
        self.heartbeat_timer.timeout.connect(self.send_pending_strength)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.stop_button.clicked.connect(self.stop_motor)
        self.strength_slider.valueChanged.connect(self.on_strength_slider_changed)
        self.strength_spin.valueChanged.connect(self.on_strength_spin_changed)

        self.setCentralWidget(self.build_ui())
        self.refresh_ports()
        self.update_strength_label(DEFAULT_OUTPUT_MILLI_PERCENT)

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

        slider_layout = QHBoxLayout()
        slider_layout.addWidget(QLabel("Safe output"))
        slider_layout.addWidget(self.strength_slider, 1)
        slider_layout.addWidget(self.strength_spin)
        slider_layout.addWidget(self.strength_label)
        slider_layout.addWidget(self.stop_button)
        layout.addLayout(slider_layout)

        layout.addWidget(self.log)
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
        if self.serial_port and self.serial_port.is_open:
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self):
        port = self.port_combo.currentData()
        if not port:
            self.append_log("No serial port selected.")
            return

        try:
            self.serial_port = serial.Serial(port, self.baud_spin.value(), timeout=0)
        except serial.SerialException as exc:
            self.serial_port = None
            self.status_label.setText("Disconnected")
            self.append_log(f"Connection failed: {exc}")
            return

        self.connect_button.setText("Disconnect")
        self.status_label.setText(f"Connected to {port}")
        self.read_timer.start()
        self.heartbeat_timer.start()
        self.append_log(f"Connected to {port}. Arduino may reset for a moment.")
        self.append_log("S100 means the Arduino sketch's safe configured maximum, not full PWM.")
        QTimer.singleShot(1800, self.send_pending_strength)

    def disconnect_serial(self, send_stop=True):
        self.read_timer.stop()
        self.send_timer.stop()
        self.heartbeat_timer.stop()

        if self.serial_port:
            if send_stop and self.serial_port.is_open:
                self.send_stop_command()

            self.serial_port.close()
            self.serial_port = None

        self.reset_strength_controls()
        self.connect_button.setText("Connect")
        self.status_label.setText("Disconnected")
        self.append_log("Disconnected.")

    def on_strength_slider_changed(self, value):
        spin_value = value / 1000.0
        if round(self.strength_spin.value() * 1000) != value:
            self.strength_spin.blockSignals(True)
            self.strength_spin.setValue(spin_value)
            self.strength_spin.blockSignals(False)

        self.set_pending_strength(value)

    def on_strength_spin_changed(self, value):
        milli_percent = int(round(value * 1000))
        if self.strength_slider.value() != milli_percent:
            self.strength_slider.blockSignals(True)
            self.strength_slider.setValue(milli_percent)
            self.strength_slider.blockSignals(False)

        self.set_pending_strength(milli_percent)

    def set_pending_strength(self, milli_percent):
        self.pending_strength_milli_percent = milli_percent
        self.update_strength_label(milli_percent)

        if self.serial_port and self.serial_port.is_open:
            self.send_timer.start()

    def stop_motor(self):
        self.strength_slider.setValue(0)
        self.strength_spin.setValue(0.0)
        self.send_pending_strength()

    def reset_strength_controls(self):
        self.pending_strength_milli_percent = 0

        self.strength_slider.blockSignals(True)
        self.strength_spin.blockSignals(True)
        self.strength_slider.setValue(0)
        self.strength_spin.setValue(0.0)
        self.strength_slider.blockSignals(False)
        self.strength_spin.blockSignals(False)

        self.update_strength_label(0)

    def send_pending_strength(self):
        if not self.serial_port or not self.serial_port.is_open:
            return

        command = f"S{self.pending_strength_milli_percent / 1000.0:.3f}\n".encode("ascii")

        try:
            self.serial_port.write(command)
        except serial.SerialException as exc:
            self.append_log(f"Write failed: {exc}")
            self.disconnect_serial(send_stop=False)

    def send_stop_command(self):
        try:
            self.serial_port.write(b"S0\n")
            self.serial_port.flush()
        except serial.SerialException:
            pass

    def read_serial(self):
        if not self.serial_port or not self.serial_port.is_open:
            return

        try:
            while self.serial_port.in_waiting:
                line = self.serial_port.readline().decode("utf-8", errors="replace").strip()
                if line:
                    self.append_log(line)
        except serial.SerialException as exc:
            self.append_log(f"Read failed: {exc}")
            self.disconnect_serial(send_stop=False)

    def update_strength_label(self, milli_percent):
        self.strength_label.setText(f"{milli_percent / 1000.0:.3f}% safe")

    def append_log(self, message):
        self.log.append(message)

    def closeEvent(self, event):
        self.disconnect_serial(send_stop=True)
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = PwmControlWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
