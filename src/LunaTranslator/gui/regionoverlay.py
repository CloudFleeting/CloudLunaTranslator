from __future__ import annotations

from qtsymbols import *
import windows
from myutils.config import globalconfig
from textio.regionoverlay import (
    RegionRect,
    RegionSnapshot,
    estimate_wrapped_layout,
    place_above_original,
    place_in_original,
)


class RegionOverlayWidget(QWidget):
    def __init__(self):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        try:
            flags |= Qt.WindowType.WindowTransparentForInput
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        except AttributeError:
            pass
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._text = ""
        self._font_size = 12
        self._placement = "above"
        windows.WindowFocus.giveup(self.winId())

    @property
    def padding(self):
        return max(0, int(globalconfig.get("ocr_overlay_text_padding", 6)))

    @property
    def minimum_font_size(self):
        return max(6, int(globalconfig.get("ocr_overlay_min_font_size", 10)))

    def _font(self, size):
        font = QFont(globalconfig.get("fonttype2", ""))
        font.setPointSize(max(self.minimum_font_size, int(size)))
        return font

    def _flags(self):
        return (
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.TextFlag.TextWordWrap
        )

    def _effective_padding(self, width, height):
        return min(self.padding, max(0, (min(width, height) - 2) // 2))

    def _fit_font(self, width, height):
        maximum = max(self.minimum_font_size, int(globalconfig.get("fontsize", 16)))
        padding = self._effective_padding(width, height)
        estimate, _, _ = estimate_wrapped_layout(
            self._text,
            width,
            height,
            padding,
            self.minimum_font_size,
            maximum,
        )
        content = QRect(
            padding,
            padding,
            max(1, width - padding * 2),
            max(1, height - padding * 2),
        )
        for size in range(min(maximum, estimate + 2), self.minimum_font_size - 1, -1):
            metrics = QFontMetrics(self._font(size))
            needed = metrics.boundingRect(content, self._flags(), self._text)
            if needed.width() <= content.width() and needed.height() <= content.height():
                return size
        return self.minimum_font_size

    def measure_above(self, source_width, screen_width):
        maximum = max(self.minimum_font_size, int(globalconfig.get("fontsize", 16)))
        width = min(screen_width, max(120, source_width, min(480, source_width * 2)))
        padding = self._effective_padding(width, 10000)
        metrics = QFontMetrics(self._font(maximum))
        probe = QRect(0, 0, max(1, width - padding * 2), 10000)
        needed = metrics.boundingRect(probe, self._flags(), self._text)
        return width, max(
            maximum + padding * 2,
            needed.height() + padding * 2,
        )

    def set_content(self, text, placement, geometry: RegionRect):
        self._text = text
        self._placement = placement
        self._font_size = self._fit_font(geometry.width, geometry.height)
        self.setGeometry(geometry.x, geometry.y, geometry.width, geometry.height)
        self.update()
        if text:
            self.show()
        else:
            self.hide()

    def paintEvent(self, event):
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        opacity = max(
            0, min(100, int(globalconfig.get("ocr_overlay_background_opacity", 70)))
        )
        background = QColor(0, 0, 0, round(255 * opacity / 100))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(self.rect(), 5, 5)
        painter.setFont(self._font(self._font_size))
        painter.setPen(QColor("#ffffff"))
        padding = self._effective_padding(self.width(), self.height())
        content = self.rect().adjusted(
            padding, padding, -padding, -padding
        )
        painter.drawText(content, self._flags(), self._text)
        painter.end()


class RegionOverlayManager(QObject):
    """Owns one click-through top-level overlay widget per OCR region."""

    def __init__(self):
        super().__init__()
        self._widgets: dict[str, RegionOverlayWidget] = {}
        self._snapshots: tuple[RegionSnapshot, ...] = ()
        self._bounds = RegionRect(0, 0, 0, 0)
        self._screen_rects: tuple[RegionRect, ...] = ()
        self._placements: dict[str, str] = {}

    def update_regions(self, snapshots, bounds):
        if not bounds or not bounds.valid:
            self.clear()
            self._bounds = bounds or RegionRect(0, 0, 0, 0)
            return
        self._snapshots = tuple(snapshots or ())
        self._bounds = bounds
        self.refresh()

    def refresh(self, *_):
        if not globalconfig.get("ocr_fullscreen_detection", False):
            self.clear()
            return
        active_ids = {
            snapshot.region_id
            for snapshot in self._snapshots
            if snapshot.translation and snapshot.rect.valid
        }
        self._placements = {
            region_id: placement
            for region_id, placement in self._placements.items()
            if region_id in active_ids
        }
        for region_id in tuple(self._widgets):
            if region_id not in active_ids:
                widget = self._widgets.pop(region_id)
                widget.close()
                widget.deleteLater()

        occupied = []
        screen_rects = []
        source_rects = {
            snapshot.region_id: snapshot.rect.translated(
                self._bounds.x, self._bounds.y
            )
            for snapshot in self._snapshots
            if snapshot.rect.valid
        }
        for snapshot in self._snapshots:
            if not snapshot.translation or not snapshot.rect.valid:
                continue
            widget = self._widgets.get(snapshot.region_id)
            if widget is None:
                widget = RegionOverlayWidget()
                self._widgets[snapshot.region_id] = widget
            source = source_rects[snapshot.region_id]
            placement = globalconfig.get("ocr_translation_placement", "above")
            if placement == "inplace":
                geometry = place_in_original(source, self._bounds)
                actual_placement = "inplace"
            else:
                widget._text = snapshot.translation
                desired_width, desired_height = widget.measure_above(
                    source.width, self._bounds.width
                )
                actual_placement, geometry = place_above_original(
                    source,
                    self._bounds,
                    desired_width,
                    desired_height,
                    max(0, int(globalconfig.get("ocr_overlay_gap", 6))),
                    tuple(occupied)
                    + tuple(
                        rect
                        for region_id, rect in source_rects.items()
                        if region_id != snapshot.region_id
                    ),
                )
            if not geometry.valid:
                widget.hide()
                continue
            widget.set_content(snapshot.translation, actual_placement, geometry)
            self._placements[snapshot.region_id] = actual_placement
            occupied.append(geometry)
            screen_rects.append(geometry)
        self._screen_rects = tuple(screen_rects)

    def screen_rects(self):
        return self._screen_rects

    def clear(self):
        self._snapshots = ()
        self._screen_rects = ()
        self._placements.clear()
        for widget in self._widgets.values():
            widget.close()
            widget.deleteLater()
        self._widgets.clear()
