"""Minimal rolling time-series plot (QPainter only, no extra dependencies)."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

TARGET_COLOR = QColor("#1f77b4")
MEASURED_COLOR = QColor("#d62728")
GRID_COLOR = QColor(0, 0, 0, 35)
TEXT_COLOR = QColor("#333333")


class RpmPlot(QWidget):
    """Shows commanded target and measured RPM over the last ``window_s`` seconds."""

    def __init__(self, window_s: float = 120.0, parent=None):
        super().__init__(parent)
        self.window_s = window_s
        self._samples: deque[tuple[float, float | None, float | None]] = deque(maxlen=20000)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor("#fafafa"))
        self.setPalette(palette)

    def clear(self) -> None:
        self._samples.clear()
        self.update()

    def add_sample(self, t: float, target: float | None, measured: float | None) -> None:
        self._samples.append((t, target, measured))
        # Drop points far outside the window so memory stays bounded on long runs.
        cutoff = t - self.window_s * 1.1
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        self.update()

    def set_window(self, window_s: float) -> None:
        self.window_s = max(5.0, float(window_s))
        self.update()

    # ----------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin_left, margin_right, margin_top, margin_bottom = 56, 14, 26, 28
        rect = QRectF(
            margin_left,
            margin_top,
            max(10.0, self.width() - margin_left - margin_right),
            max(10.0, self.height() - margin_top - margin_bottom),
        )

        painter.setPen(QPen(QColor("#888888"), 1))
        painter.drawRect(rect)

        if not self._samples:
            painter.setPen(TEXT_COLOR)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No data yet")
            self._draw_legend(painter, rect)
            return

        # Keep the x-axis anchored at 0 until the window fills, then scroll.
        t_end = max(self._samples[-1][0], self.window_s)
        t_start = t_end - self.window_s
        visible = [s for s in self._samples if s[0] >= t_start]

        y_max = 100.0
        for _, target, measured in visible:
            for value in (target, measured):
                if value is not None and value > y_max:
                    y_max = value
        y_max = _nice_ceiling(y_max * 1.08)

        def to_x(t: float) -> float:
            return rect.left() + (t - t_start) / self.window_s * rect.width()

        def to_y(v: float) -> float:
            return rect.bottom() - (v / y_max) * rect.height()

        # Grid and axis labels.
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(GRID_COLOR, 1))
        y_step = _nice_step(y_max / 5)
        value = 0.0
        while value <= y_max + 1e-9:
            y = to_y(value)
            painter.setPen(QPen(GRID_COLOR, 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(TEXT_COLOR)
            painter.drawText(
                QRectF(0, y - 8, margin_left - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value:g}",
            )
            value += y_step

        x_step = _nice_step(self.window_s / 6)
        offset = (-t_start) % x_step  # grid aligned to absolute seconds
        t = t_start + offset
        while t <= t_end + 1e-9:
            x = to_x(t)
            painter.setPen(QPen(GRID_COLOR, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(TEXT_COLOR)
            painter.drawText(
                QRectF(x - 30, rect.bottom() + 4, 60, 16),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                f"{t:.0f}",
            )
            t += x_step

        painter.setPen(TEXT_COLOR)
        painter.drawText(QRectF(rect.left(), 4, rect.width(), 16), Qt.AlignmentFlag.AlignLeft, "RPM")
        painter.drawText(
            QRectF(rect.left(), rect.bottom() + 14, rect.width(), 14),
            Qt.AlignmentFlag.AlignRight,
            "time since connect [s]",
        )

        # Series.
        painter.setClipRect(rect)
        for index, color, width in ((1, TARGET_COLOR, 2.0), (2, MEASURED_COLOR, 1.6)):
            painter.setPen(QPen(color, width))
            previous = None
            for sample in visible:
                value = sample[index]
                if value is None:
                    previous = None
                    continue
                point = QPointF(to_x(sample[0]), to_y(value))
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point
        painter.setClipping(False)
        self._draw_legend(painter, rect)

    def _draw_legend(self, painter: QPainter, rect: QRectF) -> None:
        x = rect.right() - 190
        y = rect.top() + 8
        for label, color in (("commanded target", TARGET_COLOR), ("measured", MEASURED_COLOR)):
            painter.setPen(QPen(color, 3))
            painter.drawLine(QPointF(x, y + 6), QPointF(x + 18, y + 6))
            painter.setPen(TEXT_COLOR)
            painter.drawText(QRectF(x + 24, y - 2, 110, 16), Qt.AlignmentFlag.AlignLeft, label)
            y += 16


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    magnitude = 10 ** int(f"{raw:e}".split("e")[1])
    for multiplier in (1, 2, 5, 10):
        step = multiplier * magnitude
        if step >= raw:
            return float(step)
    return float(10 * magnitude)


def _nice_ceiling(value: float) -> float:
    step = _nice_step(value / 5)
    return step * (int(value / step) + 1)
