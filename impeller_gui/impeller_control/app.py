"""PySide6 main window: connect, ramp to a target RPM, record CSV."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .csv_logger import CsvLogger, make_log_path
from .plot_widget import RpmPlot
from .profile import CustomProfile
from .profile_editor import ProfileCanvas, ProfileModel, ProfilePanel
from .protocol import DEFAULT_BAUD, ImpellerLink, ImpellerStatus, LinkError, SerialImpellerLink, list_serial_ports
from .ramp import CONTROLLER_MAX_RPM, RampMode, RampPhase, RampProfile, RampSettings, rate_from_duration
from .simulator import SimulatedImpellerLink

SIMULATOR_PORT = "__SIMULATOR__"
POLL_INTERVAL_MS = 50
STALE_STATUS_S = 3.0
# The firmware stops the motor if it hears nothing for SERIAL_COMMAND_TIMEOUT_MS (2.5 s),
# so while a non-zero target is active we send a harmless status query this often.
# (Re-sending T<rpm> would also work but resets the firmware's PID integral.)
KEEPALIVE_INTERVAL_MS = 1000
DEFAULT_RPM_LIMIT = 700  # MAX_TARGET_RPM in the currently flashed impeller.ino
LIMIT_ERROR_RE = re.compile(r"ERR use T0-T(\d+)")

PHASE_STYLE = {
    RampPhase.IDLE: ("Idle", "#666666"),
    RampPhase.RAMPING: ("Ramping", "#e07b00"),
    RampPhase.HOLDING: ("Holding at target", "#1a8f3c"),
    RampPhase.FINISHED: ("Finished", "#1f77b4"),
    RampPhase.STOPPED: ("Stopped", "#c62828"),
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Impeller Ramp Control v{__version__}")
        self.resize(1180, 760)

        self.qsettings = QSettings("ViscNet", "ImpellerRampControl")
        self.link: Optional[ImpellerLink] = None
        self.logger = CsvLogger()
        self.profile: Optional[Union[RampProfile, CustomProfile]] = None  # the running schedule
        self.run_started_at: Optional[float] = None
        self.run_phase = RampPhase.IDLE
        self.connected_at: Optional[float] = None
        self.controller_ready = False
        self._last_status_stamp: Optional[float] = None
        self._last_sent_setpoint: Optional[int] = None
        self._last_command_at = 0.0  # monotonic time of the last command written to the link

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self._poll_link)

        self.ramp_timer = QTimer(self)
        self.ramp_timer.timeout.connect(self._ramp_tick)

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(200)
        self.clock_timer.timeout.connect(self._refresh_run_clock)

        self.keepalive_timer = QTimer(self)
        self.keepalive_timer.setInterval(KEEPALIVE_INTERVAL_MS)
        self.keepalive_timer.timeout.connect(self._keepalive)

        self._build_ui()
        self._load_settings()
        self.refresh_ports()
        self._refresh_profile_summary()
        self._update_ui_state()

    # ================================================================== UI
    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setSpacing(8)

        outer.addWidget(self._build_connection_group())

        # Custom profile editor: the model is shared by the canvas (right column)
        # and the control panel (left column).
        self.profile_model = ProfileModel()
        self.profile_canvas = ProfileCanvas(self.profile_model)
        self.profile_panel = ProfilePanel(self.profile_model, self.profile_canvas)

        body = QHBoxLayout()
        body.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.plan_tabs = QTabWidget()
        self.plan_tabs.addTab(self._build_ramp_group(), "1. Simple ramp")
        self.plan_tabs.addTab(self._build_profile_tab(), "1. Custom profile")
        left.addWidget(self.plan_tabs, 1)
        left.addWidget(self._build_logging_group())
        left.addWidget(self._build_run_group())
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(430)
        body.addWidget(left_widget)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self._build_readout_group())
        self.plot = RpmPlot(window_s=120.0)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.plot)  # index 0: simple ramp -> rolling plot
        self.right_stack.addWidget(self.profile_canvas)  # index 1: custom profile -> editor
        right.addWidget(self.right_stack, 1)
        right.addWidget(self._build_console_group())
        body.addLayout(right, 1)
        self.plan_tabs.currentChanged.connect(self._on_plan_tab_changed)

        outer.addLayout(body, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Select a port and press Connect.")

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        layout = QHBoxLayout(group)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(320)
        self.refresh_button = QPushButton("Refresh ports")
        self.connect_button = QPushButton("Connect")
        self.connect_button.setMinimumWidth(110)
        self.connection_dot = QLabel("●")
        self.connection_dot.setStyleSheet("color: #999999; font-size: 18px;")
        self.connection_label = QLabel("Disconnected")

        layout.addWidget(QLabel("Port"))
        layout.addWidget(self.port_combo, 1)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.connect_button)
        layout.addSpacing(12)
        layout.addWidget(self.connection_dot)
        layout.addWidget(self.connection_label, 1)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        return group

    def _build_ramp_group(self) -> QGroupBox:
        group = QGroupBox("1. Ramp settings")
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.target_spin = QSpinBox()
        self.target_spin.setRange(0, CONTROLLER_MAX_RPM)
        self.target_spin.setSuffix(" RPM")
        self.target_spin.setValue(300)
        form.addRow("Target speed", self.target_spin)

        start_row = QHBoxLayout()
        self.start_from_current_check = QCheckBox("from current target")
        self.start_from_current_check.setToolTip("Begin the ramp at the target the controller is currently holding (0 if stopped).")
        self.start_from_current_check.setChecked(True)
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, CONTROLLER_MAX_RPM)
        self.start_spin.setSuffix(" RPM")
        self.start_spin.setMinimumWidth(110)
        start_row.addWidget(self.start_spin, 1)
        start_row.addWidget(self.start_from_current_check)
        form.addRow("Start speed", start_row)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Linear (constant rate)", RampMode.LINEAR.value)
        self.mode_combo.addItem("Stepped (fixed increments)", RampMode.STEPPED.value)
        form.addRow("Ramp mode", self.mode_combo)

        # Linear: either a rate or a duration can be specified.
        linear_row = QHBoxLayout()
        self.linear_spec_combo = QComboBox()
        self.linear_spec_combo.addItem("Rate", "rate")
        self.linear_spec_combo.addItem("Duration", "duration")
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(0.1, 1000.0)
        self.rate_spin.setDecimals(1)
        self.rate_spin.setSuffix(" RPM/s")
        self.rate_spin.setValue(10.0)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.5, 36000.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(30.0)
        self.linear_derived_label = QLabel()
        self.linear_derived_label.setStyleSheet("color: #666666;")
        linear_row.addWidget(self.linear_spec_combo)
        linear_row.addWidget(self.rate_spin, 1)
        linear_row.addWidget(self.duration_spin, 1)
        linear_row.addWidget(self.linear_derived_label)
        self.linear_row_label = QLabel("Ramp speed")
        form.addRow(self.linear_row_label, linear_row)
        self._linear_row_widgets = [self.linear_spec_combo, self.rate_spin, self.duration_spin, self.linear_derived_label]

        step_row = QHBoxLayout()
        self.step_spin = QSpinBox()
        self.step_spin.setRange(1, CONTROLLER_MAX_RPM)
        self.step_spin.setSuffix(" RPM")
        self.step_spin.setValue(50)
        self.dwell_spin = QDoubleSpinBox()
        self.dwell_spin.setRange(0.5, 36000.0)
        self.dwell_spin.setDecimals(1)
        self.dwell_spin.setSuffix(" s / step")
        self.dwell_spin.setValue(10.0)
        step_row.addWidget(self.step_spin, 1)
        step_row.addWidget(self.dwell_spin, 1)
        self.step_row_label = QLabel("Step size / dwell")
        form.addRow(self.step_row_label, step_row)
        self._step_row_widgets = [self.step_spin, self.dwell_spin]

        hold_row = QHBoxLayout()
        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.0, 360000.0)
        self.hold_spin.setDecimals(1)
        self.hold_spin.setSuffix(" s")
        self.hold_spin.setValue(60.0)
        self.hold_forever_check = QCheckBox("until Stop is pressed")
        hold_row.addWidget(self.hold_spin, 1)
        hold_row.addWidget(self.hold_forever_check)
        form.addRow("Hold at target", hold_row)

        self.stop_after_check = QCheckBox("Stop the motor when the hold time ends")
        self.stop_after_check.setChecked(True)
        form.addRow("", self.stop_after_check)

        self.profile_summary = QLabel()
        self.profile_summary.setWordWrap(True)
        self.profile_summary.setStyleSheet("color: #1f4e79; font-weight: bold;")
        form.addRow("Plan", self.profile_summary)

        for widget in (self.target_spin, self.start_spin, self.rate_spin, self.duration_spin, self.step_spin, self.dwell_spin, self.hold_spin):
            widget.valueChanged.connect(self._refresh_profile_summary)
        for widget in (self.mode_combo, self.linear_spec_combo):
            widget.currentIndexChanged.connect(self._refresh_profile_summary)
        for widget in (self.start_from_current_check, self.hold_forever_check, self.stop_after_check):
            widget.toggled.connect(self._refresh_profile_summary)
        self.start_from_current_check.toggled.connect(lambda checked: self.start_spin.setEnabled(not checked))
        self.hold_forever_check.toggled.connect(lambda checked: self.hold_spin.setEnabled(not checked))
        self.start_spin.setEnabled(False)
        return group

    def _build_profile_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        intro = QLabel("Draw the schedule on the graph to the right; points are connected as chosen below.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #555555;")
        layout.addWidget(intro)
        layout.addWidget(self.profile_panel, 1)
        self.copy_ramp_button = QPushButton("Start from the simple ramp settings")
        self.copy_ramp_button.setToolTip("Replace the points with the ramp defined on the Simple ramp tab.")
        self.copy_ramp_button.clicked.connect(self._copy_simple_ramp_to_profile)
        layout.addWidget(self.copy_ramp_button)
        return tab

    def _build_logging_group(self) -> QGroupBox:
        group = QGroupBox("2. CSV logging")
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(Path.home() / "impeller_logs"))
        self.browse_button = QPushButton("Browse…")
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(self.browse_button)
        form.addRow("Folder", folder_row)

        self.prefix_edit = QLineEdit("impeller")
        self.prefix_edit.setPlaceholderText("file name prefix")
        form.addRow("File prefix", self.prefix_edit)

        self.autolog_start_check = QCheckBox("Start a new CSV when a run starts")
        self.autolog_start_check.setChecked(True)
        self.autolog_stop_check = QCheckBox("Close the CSV when the run ends")
        self.autolog_stop_check.setChecked(True)
        form.addRow("", self.autolog_start_check)
        form.addRow("", self.autolog_stop_check)

        button_row = QHBoxLayout()
        self.log_start_button = QPushButton("Start logging")
        self.log_stop_button = QPushButton("Stop logging")
        button_row.addWidget(self.log_start_button)
        button_row.addWidget(self.log_stop_button)
        form.addRow("Manual", button_row)

        self.log_status_label = QLabel("Not logging")
        self.log_status_label.setWordWrap(True)
        self.log_status_label.setStyleSheet("color: #666666;")
        form.addRow("Status", self.log_status_label)

        self.browse_button.clicked.connect(self._browse_folder)
        self.log_start_button.clicked.connect(self.start_logging)
        self.log_stop_button.clicked.connect(self.stop_logging)
        return group

    def _build_run_group(self) -> QGroupBox:
        group = QGroupBox("3. Run")
        layout = QVBoxLayout(group)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("▶  Start ramp")
        self.start_button.setMinimumHeight(44)
        self.start_button.setStyleSheet(
            "QPushButton { background: #1a8f3c; color: white; font-size: 15px; font-weight: bold; }"
            "QPushButton:disabled { background: #a5c9b0; color: #eeeeee; }"
        )
        self.stop_button = QPushButton("■  STOP motor")
        self.stop_button.setMinimumHeight(44)
        self.stop_button.setStyleSheet(
            "QPushButton { background: #c62828; color: white; font-size: 15px; font-weight: bold; }"
            "QPushButton:disabled { background: #e3a0a0; color: #eeeeee; }"
        )
        button_row.addWidget(self.start_button, 1)
        button_row.addWidget(self.stop_button, 1)
        layout.addLayout(button_row)

        common = QHBoxLayout()
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, CONTROLLER_MAX_RPM)
        self.limit_spin.setSuffix(" RPM")
        self.limit_spin.setValue(DEFAULT_RPM_LIMIT)
        self.limit_spin.setToolTip(
            "Highest target this program will send. The firmware has its own MAX_TARGET_RPM;\n"
            "if it rejects a target, the limit here is lowered to match automatically."
        )
        common.addWidget(QLabel("Max speed"))
        common.addWidget(self.limit_spin, 1)
        common.addSpacing(8)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.2, 10.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(0.5)
        self.interval_spin.setToolTip(
            "How often a new target is sent while following the schedule.\n"
            "The Arduino resets its PID integral on every new target, so 0.5-1 s is a good range."
        )
        common.addWidget(QLabel("Update every"))
        common.addWidget(self.interval_spin, 1)
        layout.addLayout(common)
        self.limit_spin.valueChanged.connect(self._apply_rpm_limit)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("Manual target"))
        self.manual_spin = QSpinBox()
        self.manual_spin.setRange(0, CONTROLLER_MAX_RPM)
        self.manual_spin.setSuffix(" RPM")
        self.manual_send_button = QPushButton("Set now (no ramp)")
        manual_row.addWidget(self.manual_spin, 1)
        manual_row.addWidget(self.manual_send_button)
        layout.addLayout(manual_row)

        self.start_button.clicked.connect(self.start_run)
        self.stop_button.clicked.connect(self.stop_motor)
        self.manual_send_button.clicked.connect(self._send_manual_target)
        return group

    def _build_readout_group(self) -> QGroupBox:
        group = QGroupBox("Live status")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(18)

        big = QFont()
        big.setPointSize(30)
        big.setBold(True)
        medium = QFont()
        medium.setPointSize(15)
        medium.setBold(True)

        self.measured_label = QLabel("--")
        self.measured_label.setFont(big)
        self.measured_label.setMinimumWidth(170)
        self.commanded_label = QLabel("--")
        self.commanded_label.setFont(medium)
        self.controller_target_label = QLabel("--")
        self.controller_target_label.setFont(medium)
        self.output_label = QLabel("--")
        self.output_label.setFont(medium)
        self.phase_label = QLabel("Idle")
        self.phase_label.setFont(medium)
        self.run_time_label = QLabel("--")
        self.run_time_label.setFont(medium)
        self.remaining_label = QLabel("--")
        self.remaining_label.setFont(medium)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)

        def caption(text: str) -> QLabel:
            label = QLabel(text)
            label.setStyleSheet("color: #666666;")
            return label

        grid.addWidget(caption("Measured RPM"), 0, 0)
        grid.addWidget(self.measured_label, 1, 0, 2, 1)
        grid.addWidget(caption("Commanded target"), 0, 1)
        grid.addWidget(self.commanded_label, 1, 1)
        grid.addWidget(caption("Controller target"), 2, 1)
        grid.addWidget(self.controller_target_label, 3, 1)
        grid.addWidget(caption("PID output"), 0, 2)
        grid.addWidget(self.output_label, 1, 2)
        grid.addWidget(caption("Phase"), 2, 2)
        grid.addWidget(self.phase_label, 3, 2)
        grid.addWidget(caption("Run time"), 0, 3)
        grid.addWidget(self.run_time_label, 1, 3)
        grid.addWidget(caption("Remaining"), 2, 3)
        grid.addWidget(self.remaining_label, 3, 3)
        grid.addWidget(self.progress, 4, 0, 1, 4)
        for column in (1, 2, 3):
            grid.setColumnStretch(column, 1)
        self.phase_label.setMinimumWidth(190)
        return group

    def _build_console_group(self) -> QGroupBox:
        group = QGroupBox("Controller messages")
        layout = QVBoxLayout(group)
        top = QHBoxLayout()
        self.show_status_check = QCheckBox("Show every status line (2 per second)")
        self.clear_console_button = QPushButton("Clear")
        top.addWidget(self.show_status_check)
        top.addStretch(1)
        top.addWidget(self.clear_console_button)
        layout.addLayout(top)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(600)
        self.console.setFixedHeight(130)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)
        self.console.setFont(mono)
        layout.addWidget(self.console)
        self.clear_console_button.clicked.connect(self.console.clear)
        return group

    # ============================================================ settings
    def _load_settings(self) -> None:
        s = self.qsettings
        self.limit_spin.setValue(int(s.value("ramp/limit", DEFAULT_RPM_LIMIT)))
        self._apply_rpm_limit(self.limit_spin.value())
        self.target_spin.setValue(int(s.value("ramp/target", 300)))
        self.start_spin.setValue(int(s.value("ramp/start", 0)))
        self.start_from_current_check.setChecked(_as_bool(s.value("ramp/start_from_current", True)))
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(s.value("ramp/mode", RampMode.LINEAR.value))))
        self.linear_spec_combo.setCurrentIndex(max(0, self.linear_spec_combo.findData(s.value("ramp/linear_spec", "rate"))))
        self.rate_spin.setValue(float(s.value("ramp/rate", 10.0)))
        self.duration_spin.setValue(float(s.value("ramp/duration", 30.0)))
        self.step_spin.setValue(int(s.value("ramp/step", 50)))
        self.dwell_spin.setValue(float(s.value("ramp/dwell", 10.0)))
        self.hold_spin.setValue(float(s.value("ramp/hold", 60.0)))
        self.hold_forever_check.setChecked(_as_bool(s.value("ramp/hold_forever", False)))
        self.stop_after_check.setChecked(_as_bool(s.value("ramp/stop_after", True)))
        self.interval_spin.setValue(float(s.value("ramp/interval", 0.5)))
        self.folder_edit.setText(str(s.value("log/folder", self.folder_edit.text())))
        self.prefix_edit.setText(str(s.value("log/prefix", "impeller")))
        self.autolog_start_check.setChecked(_as_bool(s.value("log/auto_start", True)))
        self.autolog_stop_check.setChecked(_as_bool(s.value("log/auto_stop", True)))
        self.show_status_check.setChecked(_as_bool(s.value("console/show_status", False)))
        self.manual_spin.setValue(int(s.value("manual/target", 0)))
        profile_json = s.value("profile/json")
        if profile_json:
            try:
                profile = CustomProfile.from_json(str(profile_json))
                profile.max_rpm = self.limit_spin.value()
                profile.validate()
                self.profile_model.replace(profile)
            except (ValueError, KeyError, TypeError):
                pass
        self.profile_panel.snap_t_spin.setValue(float(s.value("profile/snap_t", 1.0)))
        self.profile_panel.snap_rpm_spin.setValue(float(s.value("profile/snap_rpm", 10.0)))
        self.profile_panel.axis_spin.setValue(float(s.value("profile/axis_s", 0.0)))
        self.plan_tabs.setCurrentIndex(int(s.value("plan/tab", 0)))
        self._on_plan_tab_changed(self.plan_tabs.currentIndex())
        self._preferred_port = str(s.value("connection/port", ""))
        geometry = s.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        s = self.qsettings
        s.setValue("ramp/limit", self.limit_spin.value())
        s.setValue("ramp/target", self.target_spin.value())
        s.setValue("ramp/start", self.start_spin.value())
        s.setValue("ramp/start_from_current", self.start_from_current_check.isChecked())
        s.setValue("ramp/mode", self.mode_combo.currentData())
        s.setValue("ramp/linear_spec", self.linear_spec_combo.currentData())
        s.setValue("ramp/rate", self.rate_spin.value())
        s.setValue("ramp/duration", self.duration_spin.value())
        s.setValue("ramp/step", self.step_spin.value())
        s.setValue("ramp/dwell", self.dwell_spin.value())
        s.setValue("ramp/hold", self.hold_spin.value())
        s.setValue("ramp/hold_forever", self.hold_forever_check.isChecked())
        s.setValue("ramp/stop_after", self.stop_after_check.isChecked())
        s.setValue("ramp/interval", self.interval_spin.value())
        s.setValue("log/folder", self.folder_edit.text())
        s.setValue("log/prefix", self.prefix_edit.text())
        s.setValue("log/auto_start", self.autolog_start_check.isChecked())
        s.setValue("log/auto_stop", self.autolog_stop_check.isChecked())
        s.setValue("console/show_status", self.show_status_check.isChecked())
        s.setValue("manual/target", self.manual_spin.value())
        s.setValue("profile/json", self.profile_model.profile.to_json())
        s.setValue("profile/snap_t", self.profile_panel.snap_t_spin.value())
        s.setValue("profile/snap_rpm", self.profile_panel.snap_rpm_spin.value())
        s.setValue("profile/axis_s", self.profile_panel.axis_spin.value())
        s.setValue("plan/tab", self.plan_tabs.currentIndex())
        port = self.port_combo.currentData()
        if port:
            s.setValue("connection/port", port)
        s.setValue("window/geometry", self.saveGeometry())

    # ========================================================== connection
    def refresh_ports(self) -> None:
        current = self.port_combo.currentData() or getattr(self, "_preferred_port", "")
        self.port_combo.clear()
        ports = list_serial_ports()
        for device, description in ports:
            self.port_combo.addItem(f"{device}  -  {description}", device)
        self.port_combo.addItem("Simulator (no hardware, for testing the GUI)", SIMULATOR_PORT)
        index = self.port_combo.findData(current) if current else -1
        if index < 0:
            # Prefer an Arduino-looking port over the simulator.
            for i, (_, description) in enumerate(ports):
                if any(key in description.lower() for key in ("arduino", "ch340", "usb serial", "usb-serial")):
                    index = i
                    break
        if index < 0 and ports:
            index = 0
        self.port_combo.setCurrentIndex(max(0, index))

    def toggle_connection(self) -> None:
        if self.link is not None and self.link.is_connected:
            self.disconnect_link()
        else:
            self.connect_link()

    def connect_link(self) -> None:
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "No port", "Select a serial port first.")
            return
        link: ImpellerLink
        if port == SIMULATOR_PORT:
            link = SimulatedImpellerLink()
        else:
            link = SerialImpellerLink(port, DEFAULT_BAUD)
        try:
            link.connect()
        except LinkError as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            return

        self.link = link
        self.connected_at = time.monotonic()
        self.controller_ready = False
        self._last_status_stamp = None
        self._last_sent_setpoint = None
        self.plot.clear()
        self._set_connection_indicator("#e0a800", f"Connected to {link.name} - waiting for the controller…")
        self._console("info", f"Connected to {link.name}")
        self.poll_timer.start()
        self.keepalive_timer.start()
        self._update_ui_state()

    def disconnect_link(self) -> None:
        if self.profile is not None:
            self._end_run(RampPhase.STOPPED, send_stop=False)
        if self.link is not None:
            try:
                self.link.disconnect(stop=True)
            except LinkError:
                pass
            self._console("info", f"Disconnected from {self.link.name}")
        self.link = None
        self.poll_timer.stop()
        self.keepalive_timer.stop()
        self.controller_ready = False
        if self.logger.is_open:
            self.stop_logging()
        self._set_connection_indicator("#999999", "Disconnected")
        self._update_ui_state()

    def _handle_link_lost(self, exc: Exception) -> None:
        self._console("error", f"Link lost: {exc}")
        if self.profile is not None:
            self._end_run(RampPhase.STOPPED, send_stop=False)
        if self.link is not None:
            try:
                self.link.disconnect(stop=False)
            except Exception:
                pass
        self.link = None
        self.poll_timer.stop()
        self.keepalive_timer.stop()
        self.controller_ready = False
        if self.logger.is_open:
            self.stop_logging()
        self._set_connection_indicator("#c62828", f"Connection lost: {exc}")
        self._update_ui_state()
        QMessageBox.critical(self, "Connection lost", f"{exc}\n\nThe motor may still be running. Reconnect and press STOP.")

    def _set_connection_indicator(self, color: str, text: str) -> None:
        self.connection_dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self.connection_label.setText(text)

    # ============================================================ polling
    def _poll_link(self) -> None:
        if self.link is None:
            return
        try:
            lines = self.link.poll()
        except LinkError as exc:
            self._handle_link_lost(exc)
            return

        for line in lines:
            is_status = line.startswith("OK ") and "measured_rpm=" in line
            if line.startswith("ERR"):
                self._on_controller_error(line)
            if is_status and not self.show_status_check.isChecked():
                continue
            self._console("error" if line.startswith("ERR") else "rx", line)

        status = self.link.status
        if status.updated_at is not None and status.updated_at != self._last_status_stamp:
            self._last_status_stamp = status.updated_at
            self._on_status(status)
        elif self.controller_ready and not self.link.status_is_fresh(STALE_STATUS_S):
            self._set_connection_indicator("#e0a800", f"No status from controller for {status.age:.0f} s")

    def _on_controller_error(self, line: str) -> None:
        match = LIMIT_ERROR_RE.search(line)
        if match:
            limit = int(match.group(1))
            if limit < self.limit_spin.value():
                self.limit_spin.setValue(limit)
                self._console("info", f"Firmware limit is {limit} RPM; 'Max allowed speed' lowered to match.")
        if "stopped motor" in line and self.profile is not None:
            self.statusBar().showMessage(f"Controller stopped the motor: {line}")
            self._end_run(RampPhase.STOPPED, send_stop=False)

    def _keepalive(self) -> None:
        if self.link is None or not self.link.is_connected:
            self.keepalive_timer.stop()
            return
        if not self.link.commanded_rpm:
            return
        if time.monotonic() - self._last_command_at < KEEPALIVE_INTERVAL_MS / 1000.0:
            return  # a target was sent recently; that already counts as a command
        try:
            self.link.request_status()
            self._last_command_at = time.monotonic()
        except LinkError as exc:
            self._handle_link_lost(exc)

    def _apply_rpm_limit(self, limit: int) -> None:
        for spin in (self.target_spin, self.start_spin, self.manual_spin, self.step_spin):
            spin.setMaximum(limit)
        self.profile_model.set_max_rpm(limit)
        self._refresh_profile_summary()

    def _on_plan_tab_changed(self, index: int) -> None:
        self.right_stack.setCurrentIndex(1 if index == 1 else 0)

    @property
    def custom_profile_selected(self) -> bool:
        return self.plan_tabs.currentIndex() == 1

    def _copy_simple_ramp_to_profile(self) -> None:
        try:
            ramp = RampProfile(self._current_ramp_settings())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid ramp settings", str(exc))
            return
        from .profile import EndBehavior, InterpolationMode

        mode = InterpolationMode.STEP if ramp.settings.mode == RampMode.STEPPED else InterpolationMode.LINEAR
        end = EndBehavior.STOP if ramp.stop_at_end else EndBehavior.HOLD
        profile = CustomProfile.from_pairs(ramp.to_pairs(), mode=mode, end=end, max_rpm=self.limit_spin.value())
        self.profile_model.replace(profile)
        self.profile_canvas.set_selected(-1)

    def _current_schedule(self) -> Union[RampProfile, CustomProfile]:
        """The schedule described by the active tab; raises ValueError if invalid."""
        if self.custom_profile_selected:
            profile = self.profile_model.profile
            profile.validate()
            return profile
        return RampProfile(self._current_ramp_settings())

    def _on_status(self, status: ImpellerStatus) -> None:
        if not self.controller_ready:
            self.controller_ready = True
            self._set_connection_indicator("#1a8f3c", f"Ready - {self.link.name}")
            self.statusBar().showMessage("Controller is responding. Set the ramp and press Start.")
            self._refresh_profile_summary()
            self._update_ui_state()
        elif self.connection_label.text().startswith("No status"):
            self._set_connection_indicator("#1a8f3c", f"Ready - {self.link.name}")

        self.measured_label.setText("--" if status.measured_rpm is None else f"{status.measured_rpm:.0f}")
        self.controller_target_label.setText("--" if status.target_rpm is None else f"{status.target_rpm}")
        self.output_label.setText("--" if status.output is None else f"{status.output:.1f} %")
        commanded = self.link.commanded_rpm
        self.commanded_label.setText("--" if commanded is None else f"{commanded}")

        t = time.monotonic() - (self.connected_at or time.monotonic())
        self.plot.add_sample(t, None if commanded is None else float(commanded), status.measured_rpm)
        if isinstance(self.profile, CustomProfile) and self.run_started_at is not None:
            self.profile_canvas.add_live_sample(time.monotonic() - self.run_started_at, status.measured_rpm)

        if self.logger.is_open:
            self.logger.write(self._log_row(status))
            self._refresh_log_status()

    def _log_row(self, status: ImpellerStatus) -> dict:
        run_elapsed = None if self.run_started_at is None else time.monotonic() - self.run_started_at
        t_ms = status.fields.get("t_ms")
        rpm_t_ms = status.fields.get("rpm_t_ms")
        return {
            "run_elapsed_s": None if run_elapsed is None else round(run_elapsed, 3),
            "controller_time_s": None if t_ms is None else round(t_ms / 1000.0, 3),
            "controller_rpm_time_s": None if rpm_t_ms is None else round(rpm_t_ms / 1000.0, 3),
            "phase": self.run_phase.value,
            "commanded_rpm": self.link.commanded_rpm if self.link else None,
            "controller_target_rpm": status.target_rpm,
            "measured_rpm": status.measured_rpm,
            "raw_rpm": status.raw_rpm,
            "output_percent": status.output,
            "strength_percent": status.strength,
            "kp": status.kp,
            "ki": status.ki,
            "kd": status.kd,
            "controller_setpoint_rpm": status.fields.get("setpoint_rpm"),
            "encoder_rpm": status.fields.get("encoder_rpm"),
            "feedforward_percent": status.fields.get("ff"),
            "d_rpm_s": status.fields.get("d_rpm_s"),
            "hold": status.fields.get("hold"),
            "open_loop": status.fields.get("open_loop"),
        }

    # ================================================================ ramp
    def _current_ramp_settings(self) -> RampSettings:
        target = self.target_spin.value()
        if self.start_from_current_check.isChecked():
            start = 0
            if self.link is not None and self.link.status.target_rpm is not None:
                start = int(self.link.status.target_rpm)
        else:
            start = self.start_spin.value()

        mode = RampMode(self.mode_combo.currentData())
        if self.linear_spec_combo.currentData() == "duration":
            rate = rate_from_duration(start, target, self.duration_spin.value()) if target != start else self.rate_spin.value()
        else:
            rate = self.rate_spin.value()

        return RampSettings(
            start_rpm=start,
            target_rpm=target,
            mode=mode,
            rate_rpm_per_s=rate,
            step_rpm=self.step_spin.value(),
            step_dwell_s=self.dwell_spin.value(),
            hold_s=None if self.hold_forever_check.isChecked() else self.hold_spin.value(),
            stop_after_hold=self.stop_after_check.isChecked(),
            max_rpm=self.limit_spin.value(),
        )

    def _refresh_profile_summary(self) -> None:
        linear = self.mode_combo.currentData() == RampMode.LINEAR.value
        for widget in self._linear_row_widgets:
            widget.setVisible(linear)
        self.linear_row_label.setVisible(linear)
        for widget in self._step_row_widgets:
            widget.setVisible(not linear)
        self.step_row_label.setVisible(not linear)
        by_duration = self.linear_spec_combo.currentData() == "duration"
        self.rate_spin.setVisible(linear and not by_duration)
        self.duration_spin.setVisible(linear and by_duration)

        try:
            settings = self._current_ramp_settings()
            profile = RampProfile(settings)
        except ValueError as exc:
            self.profile_summary.setText(f"Invalid: {exc}")
            self.linear_derived_label.setText("")
            return

        if linear:
            if by_duration:
                self.linear_derived_label.setText(f"≈ {settings.rate_rpm_per_s:.1f} RPM/s")
            else:
                self.linear_derived_label.setText(f"≈ {profile.ramp_seconds:.1f} s")
        self.profile_summary.setText(profile.describe())

    def start_run(self) -> None:
        if self.link is None or not self.link.is_connected:
            QMessageBox.warning(self, "Not connected", "Connect to the controller first.")
            return
        if not self.controller_ready:
            QMessageBox.warning(self, "Controller not ready", "No status has been received from the controller yet.")
            return
        try:
            profile = self._current_schedule()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid schedule", str(exc))
            return

        if self.autolog_start_check.isChecked() and not self.logger.is_open:
            if not self.start_logging(metadata=self._run_metadata(profile)):
                return

        if isinstance(profile, CustomProfile):
            self.profile_canvas.clear_live()
        self.profile = profile
        self.run_started_at = time.monotonic()
        self.run_phase = RampPhase.RAMPING
        self._last_sent_setpoint = None
        self.ramp_timer.setInterval(int(self.interval_spin.value() * 1000))
        self._console("info", f"Run started: {profile.describe()}")
        self.statusBar().showMessage(f"Running: {profile.describe()}")
        self._ramp_tick()
        if self.profile is not None:  # the first tick may already have ended the run or lost the link
            self.ramp_timer.start()
            self.clock_timer.start()
        self._update_ui_state()

    def _ramp_tick(self) -> None:
        if self.profile is None or self.run_started_at is None:
            return
        t = time.monotonic() - self.run_started_at
        phase = self.profile.phase_at(t)
        setpoint = self.profile.setpoint_at(t)

        if setpoint != self._last_sent_setpoint:
            if not self._send_target(setpoint):
                return
            self._last_sent_setpoint = setpoint

        if phase == RampPhase.FINISHED:
            self._end_run(RampPhase.FINISHED, send_stop=self.profile.stop_at_end)
            return

        if phase != self.run_phase:
            self.run_phase = phase
            self._console("info", f"Phase: {PHASE_STYLE[phase][0]}")
        self._refresh_run_clock()

    def _refresh_run_clock(self) -> None:
        text, color = PHASE_STYLE[self.run_phase]
        if isinstance(self.profile, CustomProfile) and self.run_phase == RampPhase.RAMPING:
            text = "Following profile"
        self.phase_label.setText(text)
        self.phase_label.setStyleSheet(f"color: {color};")
        if self.run_started_at is None:
            self.run_time_label.setText("--")
            self.remaining_label.setText("--")
            self.progress.setValue(0)
            return
        t = time.monotonic() - self.run_started_at
        self.run_time_label.setText(_fmt_duration(t))
        if self.profile is None:
            self.remaining_label.setText("--")
            return
        total = self.profile.total_seconds
        if total is None:
            tail = " to end" if isinstance(self.profile, CustomProfile) else " to target"
            self.remaining_label.setText("until Stop" if t >= self.profile.ramp_seconds else _fmt_duration(self.profile.ramp_seconds - t) + tail)
        else:
            self.remaining_label.setText(_fmt_duration(max(0.0, total - t)))
        self.progress.setValue(int(self.profile.progress_at(t) * 1000))
        if isinstance(self.profile, CustomProfile):
            self.profile_canvas.add_live_sample(t, None)  # moves the cursor only

    def _end_run(self, phase: RampPhase, send_stop: bool) -> None:
        self.ramp_timer.stop()
        self.clock_timer.stop()
        self.profile = None
        self.run_phase = phase
        if send_stop:
            self._send_stop()
        self._console("info", f"Run {phase.value}.")
        self.statusBar().showMessage(f"Run {phase.value}.")
        self._refresh_run_clock()
        if phase == RampPhase.FINISHED:
            self.progress.setValue(1000)
        if self.autolog_stop_check.isChecked() and self.logger.is_open:
            # Give the controller a moment so the last status rows land in the file.
            QTimer.singleShot(1200, self._autoclose_log)
        self._update_ui_state()

    def _autoclose_log(self) -> None:
        if self.profile is None and self.logger.is_open:
            self.stop_logging()

    def stop_motor(self) -> None:
        if self.profile is not None:
            self._end_run(RampPhase.STOPPED, send_stop=True)
        else:
            self._send_stop()
            self.run_phase = RampPhase.STOPPED if self.run_started_at is not None else RampPhase.IDLE
            self._refresh_run_clock()

    def _send_manual_target(self) -> None:
        if self._send_target(self.manual_spin.value()):
            self._console("info", f"Manual target {self.manual_spin.value()} RPM")

    def _send_target(self, rpm: int) -> bool:
        if self.link is None:
            return False
        try:
            self.link.set_target_rpm(rpm)
            self._last_command_at = time.monotonic()
        except (LinkError, ValueError) as exc:
            self._handle_link_lost(exc)
            return False
        self._console("tx", f"T{rpm}")
        self.commanded_label.setText(str(rpm))
        return True

    def _send_stop(self) -> None:
        if self.link is None:
            return
        try:
            self.link.stop()
            self._last_command_at = time.monotonic()
        except LinkError as exc:
            self._handle_link_lost(exc)
            return
        self._console("tx", "S0")
        self.commanded_label.setText("0")

    # ============================================================= logging
    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose log folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def _run_metadata(self, schedule: Union[RampProfile, CustomProfile, None]) -> dict:
        meta = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "app_version": __version__,
            "link": self.link.name if self.link else None,
            "setpoint_update_interval_s": self.interval_spin.value(),
        }
        if isinstance(schedule, CustomProfile):
            meta["profile"] = schedule.to_dict()
        elif isinstance(schedule, RampProfile):
            settings = schedule.settings
            meta["ramp"] = {
                "start_rpm": settings.start_rpm,
                "target_rpm": settings.target_rpm,
                "mode": settings.mode.value,
                "rate_rpm_per_s": settings.rate_rpm_per_s,
                "step_rpm": settings.step_rpm,
                "step_dwell_s": settings.step_dwell_s,
                "hold_s": settings.hold_s,
                "stop_after_hold": settings.stop_after_hold,
            }
        return meta

    def start_logging(self, metadata: Optional[dict] = None) -> bool:
        if self.logger.is_open:
            return True
        folder = Path(self.folder_edit.text().strip() or ".")
        try:
            path = make_log_path(folder, self.prefix_edit.text())
            self.logger.open(path, metadata if metadata is not None else self._run_metadata(None))
        except OSError as exc:
            QMessageBox.critical(self, "Cannot write log", f"{exc}")
            return False
        self._console("info", f"Logging to {path}")
        self._refresh_log_status()
        self._update_ui_state()
        return True

    def stop_logging(self) -> None:
        if not self.logger.is_open:
            return
        rows = self.logger.rows_written
        path = self.logger.close()
        self._console("info", f"Log closed: {path} ({rows} rows)")
        self.log_status_label.setText(f"Saved {rows} rows to {path.name}")
        self.log_status_label.setToolTip(str(path))
        self.log_status_label.setStyleSheet("color: #1f77b4;")
        self._update_ui_state()

    def _refresh_log_status(self) -> None:
        if self.logger.is_open:
            self.log_status_label.setText(f"Recording {self.logger.path.name} ({self.logger.rows_written} rows)")
            self.log_status_label.setToolTip(str(self.logger.path))
            self.log_status_label.setStyleSheet("color: #c62828; font-weight: bold;")

    # ============================================================ helpers
    def _console(self, kind: str, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        prefix = {"tx": ">> ", "rx": "   ", "info": "-- ", "error": "!! "}.get(kind, "")
        self.console.appendPlainText(f"{stamp} {prefix}{text}")

    def _update_ui_state(self) -> None:
        connected = self.link is not None and self.link.is_connected
        ready = connected and self.controller_ready
        running = self.profile is not None

        self.port_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.start_button.setEnabled(ready and not running)
        self.stop_button.setEnabled(connected)
        self.manual_spin.setEnabled(ready and not running)
        self.manual_send_button.setEnabled(ready and not running)
        for widget in (
            self.target_spin, self.start_from_current_check, self.mode_combo, self.linear_spec_combo,
            self.rate_spin, self.duration_spin, self.step_spin, self.dwell_spin, self.hold_spin,
            self.hold_forever_check, self.stop_after_check, self.interval_spin, self.limit_spin,
        ):
            widget.setEnabled(not running)
        self.start_spin.setEnabled(not running and not self.start_from_current_check.isChecked())
        self.hold_spin.setEnabled(not running and not self.hold_forever_check.isChecked())
        self.plan_tabs.tabBar().setEnabled(not running)
        self.profile_panel.set_editable(not running)
        self.copy_ramp_button.setEnabled(not running)
        self.log_start_button.setEnabled(connected and not self.logger.is_open)
        self.log_stop_button.setEnabled(self.logger.is_open)
        self.folder_edit.setEnabled(not self.logger.is_open)
        self.browse_button.setEnabled(not self.logger.is_open)
        self.prefix_edit.setEnabled(not self.logger.is_open)
        if not connected:
            self.measured_label.setText("--")
            self.controller_target_label.setText("--")
            self.output_label.setText("--")
            self.commanded_label.setText("--")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        if self.profile is not None:
            answer = QMessageBox.question(
                self,
                "Run in progress",
                "A ramp is still running. Stop the motor and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_settings()
        if self.link is not None:
            self.disconnect_link()  # sends S0 before closing the port
        if self.logger.is_open:
            self.stop_logging()
        event.accept()


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Impeller Ramp Control")
    app.setOrganizationName("ViscNet")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
