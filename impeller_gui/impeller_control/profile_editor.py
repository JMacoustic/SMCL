"""Interactive time-RPM profile editor.

* :class:`ProfileModel`  - holds a :class:`CustomProfile` and emits ``changed``.
* :class:`ProfileCanvas` - the graph: click to add points, drag to move, right-click
  or Delete to remove, double-click to type exact values. Shows the live
  measured RPM during a run.
* :class:`ProfilePanel`  - the controls next to the graph: interpolation mode, end
  behaviour, snapping, an editable point table, and save/load.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .profile import MIN_POINT_SPACING_S, CustomProfile, EndBehavior, InterpolationMode, ProfilePoint

CURVE_COLOR = QColor("#1f77b4")
POINT_COLOR = QColor("#1f77b4")
SELECTED_COLOR = QColor("#e07b00")
LIVE_COLOR = QColor("#d62728")
CURSOR_COLOR = QColor("#1a8f3c")
GRID_COLOR = QColor(0, 0, 0, 35)
TEXT_COLOR = QColor("#333333")
HIT_RADIUS_PX = 9

DEFAULT_POINTS = [(0.0, 0.0), (20.0, 300.0), (80.0, 300.0), (90.0, 0.0)]


class ProfileModel(QObject):
    changed = Signal()

    def __init__(self, profile: Optional[CustomProfile] = None, parent=None):
        super().__init__(parent)
        self._profile = profile or CustomProfile.from_pairs(DEFAULT_POINTS)

    @property
    def profile(self) -> CustomProfile:
        return self._profile

    def replace(self, profile: CustomProfile) -> None:
        profile.sort()
        self._profile = profile
        self.changed.emit()

    @property
    def points(self) -> list[ProfilePoint]:
        return self._profile.points

    def set_mode(self, mode: InterpolationMode) -> None:
        if self._profile.mode != mode:
            self._profile.mode = mode
            self.changed.emit()

    def set_end(self, end: EndBehavior) -> None:
        if self._profile.end != end:
            self._profile.end = end
            self.changed.emit()

    def set_max_rpm(self, limit: int) -> None:
        self._profile.max_rpm = limit
        self._profile.points = [ProfilePoint(p.t, min(p.rpm, float(limit))) for p in self._profile.points]
        self.changed.emit()

    def add_point(self, t: float, rpm: float) -> int:
        t = max(0.0, t)
        rpm = max(0.0, min(float(self._profile.max_rpm), rpm))
        pts = self._profile.points
        # Keep a minimum spacing from existing points.
        for p in pts:
            if abs(p.t - t) < MIN_POINT_SPACING_S:
                t = p.t + MIN_POINT_SPACING_S
        pts.append(ProfilePoint(t, rpm))
        self._profile.sort()
        self.changed.emit()
        return next(i for i, p in enumerate(self._profile.points) if p.t == t and p.rpm == rpm)

    def move_point(self, index: int, t: float, rpm: float) -> int:
        pts = self._profile.points
        if not (0 <= index < len(pts)):
            return index
        rpm = max(0.0, min(float(self._profile.max_rpm), rpm))
        if index == 0:
            t = 0.0
        else:
            lo = pts[index - 1].t + MIN_POINT_SPACING_S
            hi = pts[index + 1].t - MIN_POINT_SPACING_S if index + 1 < len(pts) else math.inf
            t = max(lo, min(hi, t))
        pts[index] = ProfilePoint(t, rpm)
        self.changed.emit()
        return index

    def remove_point(self, index: int) -> bool:
        pts = self._profile.points
        if index <= 0 or index >= len(pts) or len(pts) <= 2:
            return False
        del pts[index]
        self.changed.emit()
        return True

    def clear(self) -> None:
        self._profile.points = [ProfilePoint(0.0, 0.0), ProfilePoint(10.0, 0.0)]
        self.changed.emit()


class ProfileCanvas(QWidget):
    """Graph on which the profile points are placed and dragged."""

    selectionChanged = Signal(int)

    def __init__(self, model: ProfileModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.snap_t = 1.0
        self.snap_rpm = 10.0
        self.editable = True
        self.selected = -1
        self._dragging = False
        self._live: list[tuple[float, float]] = []
        self._cursor_t: Optional[float] = None
        self._time_axis_s: Optional[float] = None  # None = automatic
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor("#fafafa"))
        self.setPalette(palette)
        self.model.changed.connect(self._on_model_changed)

    # ------------------------------------------------------------ public
    def set_editable(self, editable: bool) -> None:
        self.editable = editable
        self.setCursor(Qt.CursorShape.ArrowCursor if editable else Qt.CursorShape.ForbiddenCursor)
        self.update()

    def set_snap(self, snap_t: float, snap_rpm: float) -> None:
        self.snap_t = max(0.0, snap_t)
        self.snap_rpm = max(0.0, snap_rpm)

    def set_time_axis(self, seconds: Optional[float]) -> None:
        self._time_axis_s = seconds if (seconds and seconds > 0) else None
        self.update()

    def set_selected(self, index: int) -> None:
        if index != self.selected:
            self.selected = index
            self.selectionChanged.emit(index)
        self.update()

    def clear_live(self) -> None:
        self._live = []
        self._cursor_t = None
        self.update()

    def add_live_sample(self, t: float, rpm: Optional[float]) -> None:
        if rpm is not None:
            self._live.append((t, rpm))
        self._cursor_t = t
        self.update()

    # --------------------------------------------------------- geometry
    def _x_max(self) -> float:
        if self._time_axis_s:
            return self._time_axis_s
        duration = self.model.profile.duration
        live_t = self._live[-1][0] if self._live else 0.0
        return _nice_ceiling(max(60.0, duration * 1.15, live_t * 1.05))

    def _plot_rect(self) -> QRectF:
        return QRectF(56, 22, max(10.0, self.width() - 56 - 16), max(10.0, self.height() - 22 - 30))

    def _to_px(self, t: float, rpm: float) -> QPointF:
        r = self._plot_rect()
        x = r.left() + t / self._x_max() * r.width()
        y = r.bottom() - rpm / float(self.model.profile.max_rpm) * r.height()
        return QPointF(x, y)

    def _from_px(self, pos: QPointF) -> tuple[float, float]:
        r = self._plot_rect()
        t = (pos.x() - r.left()) / r.width() * self._x_max()
        rpm = (r.bottom() - pos.y()) / r.height() * float(self.model.profile.max_rpm)
        return max(0.0, t), max(0.0, min(float(self.model.profile.max_rpm), rpm))

    def _snap(self, t: float, rpm: float) -> tuple[float, float]:
        if self.snap_t > 0:
            t = round(t / self.snap_t) * self.snap_t
        if self.snap_rpm > 0:
            rpm = round(rpm / self.snap_rpm) * self.snap_rpm
        return t, rpm

    def _hit(self, pos: QPointF) -> int:
        best, best_d = -1, HIT_RADIUS_PX
        for i, p in enumerate(self.model.points):
            d = (self._to_px(p.t, p.rpm) - pos).manhattanLength()
            if d <= best_d:
                best, best_d = i, d
        return best

    # ------------------------------------------------------------ events
    def _on_model_changed(self) -> None:
        if self.selected >= len(self.model.points):
            self.set_selected(-1)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        index = self._hit(pos)
        if event.button() == Qt.MouseButton.LeftButton:
            if not self.editable:
                return
            if index >= 0:
                self.set_selected(index)
                self._dragging = True
            elif self._plot_rect().contains(pos):
                t, rpm = self._snap(*self._from_px(pos))
                self.set_selected(self.model.add_point(t, rpm))
                self._dragging = True
        elif event.button() == Qt.MouseButton.RightButton and self.editable and index >= 0:
            self._delete(index)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._dragging and self.selected >= 0:
            t, rpm = self._snap(*self._from_px(pos))
            self.model.move_point(self.selected, t, rpm)
            return
        if self.editable:
            self.setCursor(Qt.CursorShape.PointingHandCursor if self._hit(pos) >= 0 else Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if not self.editable:
            return
        index = self._hit(event.position())
        if index >= 0:
            self.edit_point(index)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.editable and self.selected >= 0:
            self._delete(self.selected)
        else:
            super().keyPressEvent(event)

    def _delete(self, index: int) -> None:
        if index == 0:
            QMessageBox.information(self, "Profile", "The first point (t = 0) cannot be removed.")
            return
        if not self.model.remove_point(index):
            QMessageBox.information(self, "Profile", "A profile needs at least two points.")
            return
        self.set_selected(-1)

    def edit_point(self, index: int) -> None:
        pts = self.model.points
        if not (0 <= index < len(pts)):
            return
        dialog = _PointDialog(self, pts[index], index == 0, self.model.profile.max_rpm)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            t, rpm = dialog.values()
            self.model.move_point(index, t, rpm)

    # ---------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()
        x_max = self._x_max()
        y_max = float(self.model.profile.max_rpm)

        painter.setPen(QPen(QColor("#888888"), 1))
        painter.drawRect(rect)

        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)

        y_step = _nice_step(y_max / 5)
        value = 0.0
        while value <= y_max + 1e-9:
            y = self._to_px(0, value).y()
            painter.setPen(QPen(GRID_COLOR, 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(TEXT_COLOR)
            painter.drawText(QRectF(0, y - 8, rect.left() - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:g}")
            value += y_step
        x_step = _nice_step(x_max / 8)
        t = 0.0
        while t <= x_max + 1e-9:
            x = self._to_px(t, 0).x()
            painter.setPen(QPen(GRID_COLOR, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(TEXT_COLOR)
            painter.drawText(QRectF(x - 30, rect.bottom() + 4, 60, 16), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, f"{t:g}")
            t += x_step
        painter.setPen(TEXT_COLOR)
        painter.drawText(QRectF(rect.left(), 2, rect.width(), 16), Qt.AlignmentFlag.AlignLeft, "RPM")
        painter.drawText(QRectF(rect.left(), rect.bottom() + 15, rect.width(), 14), Qt.AlignmentFlag.AlignRight, "time [s]")
        hint = "click: add point   drag: move   double-click: edit   right-click / Delete: remove" if self.editable else "locked while running"
        painter.drawText(QRectF(rect.left() + 4, rect.top() + 2, rect.width() - 8, 16), Qt.AlignmentFlag.AlignRight, hint)

        painter.setClipRect(rect)
        profile = self.model.profile
        # Curve.
        if len(profile.points) >= 2:
            painter.setPen(QPen(CURVE_COLOR, 2))
            if profile.mode == InterpolationMode.STEP:
                prev = None
                for p in profile.points:
                    pt = self._to_px(p.t, p.rpm)
                    if prev is not None:
                        painter.drawLine(prev, QPointF(pt.x(), prev.y()))
                        painter.drawLine(QPointF(pt.x(), prev.y()), pt)
                    prev = pt
            else:
                step = max(0.05, profile.duration / max(1.0, rect.width()))
                prev = None
                for t_s, v in profile.sample(step):
                    pt = self._to_px(t_s, v)
                    if prev is not None:
                        painter.drawLine(prev, pt)
                    prev = pt
            # After the end: dashed continuation.
            last = profile.points[-1]
            end_rpm = 0.0 if profile.end == EndBehavior.STOP else last.rpm
            pen = QPen(CURVE_COLOR, 1.2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            start = self._to_px(last.t, last.rpm)
            if profile.end == EndBehavior.STOP:
                painter.drawLine(start, self._to_px(last.t, 0.0))
            painter.drawLine(self._to_px(last.t, end_rpm), self._to_px(x_max, end_rpm))

        # Live measurement.
        if self._live:
            painter.setPen(QPen(LIVE_COLOR, 1.6))
            prev = None
            for t_s, v in self._live:
                pt = self._to_px(t_s, v)
                if prev is not None:
                    painter.drawLine(prev, pt)
                prev = pt
        if self._cursor_t is not None:
            painter.setPen(QPen(CURSOR_COLOR, 1.5, Qt.PenStyle.DashLine))
            x = self._to_px(self._cursor_t, 0).x()
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

        painter.setClipping(False)
        # Points (drawn unclipped so markers on the axes stay fully visible).
        for i, p in enumerate(profile.points):
            pt = self._to_px(p.t, p.rpm)
            color = SELECTED_COLOR if i == self.selected else POINT_COLOR
            painter.setPen(QPen(QColor("white"), 1.5))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(pt, 6, 6)
            painter.setPen(TEXT_COLOR)
            label_x = min(max(pt.x() - 50, rect.left()), rect.right() - 100)
            label_y = pt.y() - 22 if pt.y() - 22 > rect.top() else pt.y() + 8
            painter.drawText(QRectF(label_x, label_y, 100, 14), Qt.AlignmentFlag.AlignHCenter, f"{p.t:g} s, {p.rpm:g}")

        # Legend.
        x, y = rect.right() - 170, rect.bottom() - 40
        for label, color in (("profile (commanded)", CURVE_COLOR), ("measured", LIVE_COLOR)):
            painter.setPen(QPen(color, 3))
            painter.drawLine(QPointF(x, y + 6), QPointF(x + 18, y + 6))
            painter.setPen(TEXT_COLOR)
            painter.drawText(QRectF(x + 24, y - 2, 140, 16), Qt.AlignmentFlag.AlignLeft, label)
            y += 16


class _PointDialog(QDialog):
    def __init__(self, parent, point: ProfilePoint, time_locked: bool, max_rpm: int):
        super().__init__(parent)
        self.setWindowTitle("Edit point")
        form = QFormLayout(self)
        self.t_spin = QDoubleSpinBox()
        self.t_spin.setRange(0.0, 1e6)
        self.t_spin.setDecimals(1)
        self.t_spin.setSuffix(" s")
        self.t_spin.setValue(point.t)
        self.t_spin.setEnabled(not time_locked)
        self.rpm_spin = QDoubleSpinBox()
        self.rpm_spin.setRange(0.0, float(max_rpm))
        self.rpm_spin.setDecimals(0)
        self.rpm_spin.setSuffix(" RPM")
        self.rpm_spin.setValue(point.rpm)
        form.addRow("Time", self.t_spin)
        form.addRow("Speed", self.rpm_spin)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[float, float]:
        return self.t_spin.value(), self.rpm_spin.value()


class ProfilePanel(QWidget):
    """Controls and point table for the profile shown on a :class:`ProfileCanvas`."""

    def __init__(self, model: ProfileModel, canvas: ProfileCanvas, parent=None):
        super().__init__(parent)
        self.model = model
        self.canvas = canvas
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Linear ramps between points", InterpolationMode.LINEAR.value)
        self.mode_combo.addItem("Smooth curve through points", InterpolationMode.SMOOTH.value)
        self.mode_combo.addItem("Step (hold until next point)", InterpolationMode.STEP.value)
        form.addRow("Connect points", self.mode_combo)

        self.end_combo = QComboBox()
        self.end_combo.addItem("Stop the motor", EndBehavior.STOP.value)
        self.end_combo.addItem("Hold last speed until Stop", EndBehavior.HOLD.value)
        form.addRow("After last point", self.end_combo)

        snap_row = QHBoxLayout()
        self.snap_t_spin = QDoubleSpinBox()
        self.snap_t_spin.setRange(0.0, 60.0)
        self.snap_t_spin.setDecimals(1)
        self.snap_t_spin.setSingleStep(0.5)
        self.snap_t_spin.setSuffix(" s")
        self.snap_t_spin.setValue(1.0)
        self.snap_t_spin.setSpecialValueText("off")
        self.snap_rpm_spin = QDoubleSpinBox()
        self.snap_rpm_spin.setRange(0.0, 100.0)
        self.snap_rpm_spin.setDecimals(0)
        self.snap_rpm_spin.setSuffix(" RPM")
        self.snap_rpm_spin.setValue(10.0)
        self.snap_rpm_spin.setSpecialValueText("off")
        snap_row.addWidget(self.snap_t_spin, 1)
        snap_row.addWidget(self.snap_rpm_spin, 1)
        form.addRow("Snap to grid", snap_row)

        self.axis_spin = QDoubleSpinBox()
        self.axis_spin.setRange(0.0, 36000.0)
        self.axis_spin.setDecimals(0)
        self.axis_spin.setSuffix(" s")
        self.axis_spin.setSpecialValueText("auto")
        self.axis_spin.setValue(0.0)
        form.addRow("Time axis length", self.axis_spin)
        layout.addLayout(form)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Time [s]", "Speed [RPM]"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(110)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add point")
        self.remove_button = QPushButton("Remove")
        self.clear_button = QPushButton("Clear")
        self.load_button = QPushButton("Load…")
        self.save_button = QPushButton("Save…")
        for b in (self.add_button, self.remove_button, self.clear_button, self.load_button, self.save_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: #1f4e79; font-weight: bold;")
        layout.addWidget(self.summary)

        self.mode_combo.currentIndexChanged.connect(lambda _: self.model.set_mode(InterpolationMode(self.mode_combo.currentData())))
        self.end_combo.currentIndexChanged.connect(lambda _: self.model.set_end(EndBehavior(self.end_combo.currentData())))
        self.snap_t_spin.valueChanged.connect(self._apply_snap)
        self.snap_rpm_spin.valueChanged.connect(self._apply_snap)
        self.axis_spin.valueChanged.connect(lambda v: self.canvas.set_time_axis(v))
        self.table.cellChanged.connect(self._on_cell_changed)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.canvas.selectionChanged.connect(self._on_canvas_selection)
        self.add_button.clicked.connect(self._add_point)
        self.remove_button.clicked.connect(self._remove_point)
        self.clear_button.clicked.connect(self._clear)
        self.load_button.clicked.connect(self.load_from_file)
        self.save_button.clicked.connect(self.save_to_file)
        self.model.changed.connect(self._refresh)
        self._apply_snap()
        self._refresh()

    # ------------------------------------------------------------ public
    def set_editable(self, editable: bool) -> None:
        for w in (self.mode_combo, self.end_combo, self.table, self.add_button, self.remove_button, self.clear_button, self.load_button):
            w.setEnabled(editable)
        self.canvas.set_editable(editable)

    # ------------------------------------------------------------ slots
    def _apply_snap(self) -> None:
        self.canvas.set_snap(self.snap_t_spin.value(), self.snap_rpm_spin.value())

    def _refresh(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            profile = self.model.profile
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(profile.mode.value)))
            self.end_combo.setCurrentIndex(max(0, self.end_combo.findData(profile.end.value)))
            self.table.setRowCount(len(profile.points))
            for row, p in enumerate(profile.points):
                for col, value in ((0, p.t), (1, p.rpm)):
                    item = QTableWidgetItem(f"{value:g}")
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if row == 0 and col == 0:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, col, item)
            if 0 <= self.canvas.selected < len(profile.points):
                self.table.selectRow(self.canvas.selected)
            try:
                profile.validate()
                self.summary.setText(profile.describe())
            except ValueError as exc:
                self.summary.setText(f"Invalid: {exc}")
        finally:
            self._updating = False

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._updating:
            return
        item = self.table.item(row, col)
        try:
            value = float(item.text())
        except (TypeError, ValueError):
            self._refresh()
            return
        p = self.model.points[row]
        t, rpm = (value, p.rpm) if col == 0 else (p.t, value)
        # Re-sort may move the row; moving through the model keeps constraints.
        pts = self.model.points
        if col == 0 and row > 0:
            # Allow re-ordering by time: remove and re-add.
            del pts[row]
            self.model.add_point(t, rpm)
        else:
            self.model.move_point(row, t, rpm)

    def _on_table_selection(self) -> None:
        if self._updating:
            return
        rows = self.table.selectionModel().selectedRows()
        self.canvas.set_selected(rows[0].row() if rows else -1)

    def _on_canvas_selection(self, index: int) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            if index >= 0:
                self.table.selectRow(index)
            else:
                self.table.clearSelection()
        finally:
            self._updating = False

    def _add_point(self) -> None:
        pts = self.model.points
        last = pts[-1] if pts else ProfilePoint(0.0, 0.0)
        index = self.model.add_point(last.t + 10.0, last.rpm)
        self.canvas.set_selected(index)
        self.canvas.edit_point(index)

    def _remove_point(self) -> None:
        if self.canvas.selected >= 0:
            self.canvas._delete(self.canvas.selected)

    def _clear(self) -> None:
        if QMessageBox.question(self, "Clear profile", "Remove all points?") == QMessageBox.StandardButton.Yes:
            self.model.clear()
            self.canvas.set_selected(-1)

    def load_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load profile", "", "Profile (*.json)")
        if not path:
            return
        try:
            profile = CustomProfile.from_json(Path(path).read_text(encoding="utf-8"))
            profile.max_rpm = self.model.profile.max_rpm
            profile.validate()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.critical(self, "Load failed", f"{exc}")
            return
        self.model.replace(profile)
        self.canvas.set_selected(-1)

    def save_to_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save profile", "profile.json", "Profile (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(self.model.profile.to_json(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"{exc}")


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiplier in (1, 2, 5, 10):
        step = multiplier * magnitude
        if step >= raw:
            return float(step)
    return float(10 * magnitude)


def _nice_ceiling(value: float) -> float:
    step = _nice_step(value / 5)
    return step * math.ceil(value / step)
