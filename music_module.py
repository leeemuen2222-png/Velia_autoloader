"""Velia desktop music player. Run via run.bat from the project root.

The local directory holds music, preferences and the disposable ZIP cache.
"""

import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
import monster_siren_client as siren
from multiplayer_client import MultiplayerClient, MULTIPLAYER_AVAILABLE

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, Property, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QImage, QImageReader, QMovie, QPainter, QPainterPath, QPalette, QPen, QPixmap, QRegion
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFontComboBox, QFrame, QGraphicsDropShadowEffect,
    QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QSpinBox, QStackedWidget, QStyle, QStyleOptionButton,
    QTextEdit, QToolTip, QVBoxLayout,
    QWidget,
)

try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
except ImportError:
    MutagenFile = None
    MUTAGEN_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent
LOCAL_DIR = BASE_DIR / "local"
MUSIC_LIBRARY_DIR = LOCAL_DIR / "music"
PREFERENCE_DIR = LOCAL_DIR / "preferences"
CACHE_DIR = LOCAL_DIR / "cache"
ARTWORK_DIR = LOCAL_DIR / "artwork_cache"
RESOURCE_DIR = BASE_DIR / "resource"
RESOURCE_PIC_DIR = RESOURCE_DIR / "pic"
RESOURCE_GIF_DIR = RESOURCE_DIR / "gif"
SETTINGS_FILE = PREFERENCE_DIR / "settings.json"
LIBRARY_STATE_FILE = PREFERENCE_DIR / "library_state.json"
PLAYGROUND_AFFECTION_FILE = PREFERENCE_DIR / "playground_affection.json"
MULTIPLAYER_SERVER_URL = os.environ.get("VELIA_MULTIPLAYER_URL", "ws://127.0.0.1:8765")
MULTIPLAYER_ROOM_ID = os.environ.get("VELIA_MULTIPLAYER_ROOM", "public-01")
APP_SETTINGS = {"language": "zh"}
ACTIVE_THEME = "night"


class NetworkResults(QObject):
    finished = Signal(str, object, object)



def TXT(zh, en):
    return en if APP_SETTINGS["language"] == "en" else zh


def _write_json(path, data):
    """Write preferences atomically to avoid a truncated file after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class LocalPreferences:
    """The small QSettings-like interface used by the existing music module."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def value(self, key, default=None):
        return self.data.get(key, default)

    def setValue(self, key, value):
        self.data[key] = value
        _write_json(self.path, self.data)


DAY_COLORS = {
    "#070707": "#f3f5f8", "#080808": "#fafbfd", "#090909": "#ffffff",
    "#0b0b0b": "#ffffff", "#0c0c0c": "#ffffff", "#0d0d0d": "#f8f9fb",
    "#111": "#f1f3f6", "#121212": "#f2f4f7", "#141416": "#ffffff",
    "#151515": "#eceff4", "#171719": "#ffffff",
    "#171717": "#e3e6eb", "#181818": "#e1e5eb", "#222329": "#e7eaf1",
    "#28292f": "#dfe5f2", "#303033": "#cbd2dc", "#303035": "#cbd2dc",
    "#34353a": "#c2c8d2", "#303034": "#d5dbe3", "#424550": "#d6e0f7",
    "#40434d": "#dce5f8", "#e7e8ef": "#586b91", "#a9abb5": "#768bb3",
    "#fff": "#18202c", "#ffffff": "#17202b", "#f9f9fd": "#121925",
    "#f2f2f4": "#222c3c", "#f1f1f1": "#1d2735",
    "#f0f0f0": "#182230", "#eeeeee": "#1e2836", "#ededed": "#1e2836",
    "#e1e1e4": "#252e3b", "#d9d9d9": "#27313e", "#dddddd": "#24303e",
    "#cecece": "#34404f", "#b7b7bb": "#475466", "#919191": "#586477",
    "#888": "#606e81", "#858585": "#627084", "#777": "#627084",
    "#787878": "#5d6d81", "#6a6a6a": "#59677c", "#676767": "#627084",
    "#606060": "#67778d", "#565656": "#8390a0", "#343434": "#a0aab8",
    "#6e6e6e": "#657286", "#f2cf55": "#b77b1c", "#f5d66b": "#c58b25",
}
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def theme_css(css):
    if ACTIVE_THEME == "night":
        return css
    css = _COLOR_RE.sub(lambda match: DAY_COLORS.get(match.group(0).lower(), match.group(0)), css)
    return css.replace("rgba(255,255,255,0.075)", "rgba(26,39,60,0.065)") \
              .replace("rgba(255,255,255,0.030)", "rgba(26,39,60,0.040)") \
              .replace("rgba(255,255,255,0.055)", "rgba(26,39,60,0.10)")


def recolor_widget(widget):
    """Keep the source stylesheet so changing themes is reversible."""
    current = widget.styleSheet()
    rendered = widget.property("veliaRenderedStyle")
    source = widget.property("veliaSourceStyle")
    if current != rendered:
        source = current
    if source:
        updated = theme_css(source)
        if updated != current:
            widget.setStyleSheet(updated)
        widget.setProperty("veliaSourceStyle", source)
        widget.setProperty("veliaRenderedStyle", updated)
    if isinstance(widget, FloatingButton):
        widget.graphicsEffect().setColor(
            QColor(29, 46, 81, 85) if ACTIVE_THEME == "day"
            else QColor(190, 198, 220, 100)
        )


def _rounded_cover(pixmap, size, radius):
    """Actually clip album art; a QLabel border radius alone cannot mask images."""
    source = pixmap.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    result = QPixmap(size, size)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return result


class FloatingButton(QPushButton):
    """A subtle animated halo makes controls appear to lift on hover."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.PointingHandCursor)
        effect = QGraphicsDropShadowEffect(self)
        effect.setColor(QColor(190, 198, 220, 100))
        effect.setBlurRadius(0)
        effect.setOffset(0, 0)
        self.setGraphicsEffect(effect)
        self._halo = QPropertyAnimation(effect, b"blurRadius", self)
        self._halo.setDuration(170)
        self._halo.setEasingCurve(QEasingCurve.OutCubic)

    def _animate_halo(self, target):
        self._halo.stop()
        self._halo.setStartValue(self.graphicsEffect().blurRadius())
        self._halo.setEndValue(target)
        self._halo.start()

    def enterEvent(self, event):
        if self.isEnabled():
            self._animate_halo(20)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_halo(0)
        super().leaveEvent(event)


class VisibleComboBox(QComboBox):
    """Draw the dropdown marker ourselves so it stays visible on Windows."""

    def paintEvent(self, event):
        super().paintEvent(event)
        draw_dropdown_marker(self)


class VisibleFontComboBox(QFontComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        draw_dropdown_marker(self)


def draw_dropdown_marker(widget):
    p = QPainter(widget)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#586478") if ACTIVE_THEME == "day" else QColor("#d5d8e0"), 2.2,
                  Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    x, y = widget.width() - 17, widget.height() // 2
    path = QPainterPath(QPointF(x - 5, y - 2))
    path.lineTo(x, y + 3)
    path.lineTo(x + 5, y - 2)
    p.drawPath(path)


class VisibleCheckBox(QCheckBox):
    """Draw a reliable checkbox over Qt's platform indicator."""

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        base = QColor("#ffffff") if ACTIVE_THEME == "day" else QColor("#181a20")
        border = QColor("#71809c") if ACTIVE_THEME == "day" else QColor("#9da5b6")
        if self.isChecked():
            base = QColor("#566d9a") if ACTIVE_THEME == "day" else QColor("#b3bfdc")
        p.setPen(QPen(border, 1.4))
        p.setBrush(base)
        p.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 4, 4)
        if self.isChecked():
            p.setPen(QPen(QColor("#ffffff") if ACTIVE_THEME == "day" else QColor("#10151e"),
                          2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            mark = QPainterPath(QPointF(rect.left() + 4, rect.center().y()))
            mark.lineTo(rect.left() + 8, rect.bottom() - 5)
            mark.lineTo(rect.right() - 3, rect.top() + 5)
            p.drawPath(mark)


class IconSpinBox(QSpinBox):
    """Paint large, antialiased up/down controls and retain click/hold stepping."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.setMouseTracking(True)
        self.setCursor(Qt.IBeamCursor)
        self._hover_part = 0
        self._pressed_part = 0
        self._repeating = False
        self._repeat_timer = QTimer(self)
        self._repeat_timer.timeout.connect(self._repeat_step)

    def _button_part(self, pos):
        if pos.x() < self.width() - 29:
            return 0
        return 1 if pos.y() < self.height() / 2 else -1

    def _repeat_step(self):
        if self._pressed_part == 0:
            self._repeat_timer.stop()
            return
        self.stepBy(self._pressed_part)
        if not self._repeating:
            self._repeating = True
            self._repeat_timer.start(75)

    def mouseMoveEvent(self, event):
        part = self._button_part(event.position().toPoint())
        if part != self._hover_part:
            self._hover_part = part
            self.setCursor(Qt.PointingHandCursor if part else Qt.IBeamCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_part = 0
        self.setCursor(Qt.IBeamCursor)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        part = self._button_part(event.position().toPoint())
        if event.button() == Qt.LeftButton and part:
            self.setFocus()
            self._pressed_part = part
            self._repeating = False
            self.stepBy(part)
            self._repeat_timer.start(370)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pressed_part:
            self._pressed_part = 0
            self._repeat_timer.stop()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x = self.width() - 28
        half = self.height() / 2
        if self._hover_part:
            top = 2 if self._hover_part > 0 else int(half)
            height = max(6, int(half) - 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(81, 104, 145, 32) if ACTIVE_THEME == "day"
                       else QColor(190, 204, 237, 34))
            p.drawRoundedRect(QRectF(x + 2, top, 23, height), 5, 5)
        p.setPen(QPen(QColor("#c5cddd") if ACTIVE_THEME == "night" else QColor("#7787a2"), 1))
        p.drawLine(x, 6, x, self.height() - 6)
        p.setPen(QPen(QColor("#eff2fb") if ACTIVE_THEME == "night" else QColor("#435577"),
                      2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        center_x = self.width() - 14
        for center_y, direction in ((half * .54, -1), (half * 1.46, 1)):
            shape = QPainterPath(QPointF(center_x - 4.5, center_y - direction * 2.0))
            shape.lineTo(center_x, center_y + direction * 2.0)
            shape.lineTo(center_x + 4.5, center_y - direction * 2.0)
            p.drawPath(shape)


class DialogSpinControls(QWidget):
    """Give QColorDialog's built-in numeric fields the same clear arrows."""

    def __init__(self, spin):
        super().__init__(spin)
        self.spin = spin
        self._hover_part = 0
        self._pressed_part = 0
        self._repeat_timer = QTimer(self)
        self._repeat_timer.timeout.connect(self._repeat_step)
        self._repeating = False
        spin.installEventFilter(self)
        self._fit()
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.show()

    def _fit(self):
        self.setGeometry(self.spin.width() - 25, 1, 24, max(12, self.spin.height() - 2))

    def eventFilter(self, watched, event):
        if watched is self.spin and event.type() == QEvent.Resize:
            self._fit()
        return super().eventFilter(watched, event)

    def _repeat_step(self):
        if not self._pressed_part:
            self._repeat_timer.stop()
            return
        self.spin.stepBy(self._pressed_part)
        if not self._repeating:
            self._repeating = True
            self._repeat_timer.start(75)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed_part = 1 if event.position().y() < self.height() / 2 else -1
            self._repeating = False
            self.spin.stepBy(self._pressed_part)
            self._repeat_timer.start(370)
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        self._pressed_part = 0
        self._repeat_timer.stop()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        self._hover_part = 1 if event.position().y() < self.height() / 2 else -1
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_part = 0
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._hover_part:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(81, 104, 145, 35) if ACTIVE_THEME == "day"
                       else QColor(190, 204, 237, 39))
            p.drawRoundedRect(QRectF(1, 1 if self._hover_part > 0 else self.height()/2,
                                     self.width() - 2, self.height()/2 - 1), 4, 4)
        p.setPen(QPen(QColor("#b8c4d4") if ACTIVE_THEME == "day" else QColor("#515b6c"), 1))
        p.drawLine(0, 2, 0, self.height() - 3)
        p.setPen(QPen(QColor("#435577") if ACTIVE_THEME == "day" else QColor("#eff2fb"),
                      2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x = self.width() / 2
        for y, direction in ((self.height() * .27, -1), (self.height() * .73, 1)):
            path = QPainterPath(QPointF(x - 4, y - direction * 2))
            path.lineTo(x, y + direction * 2)
            path.lineTo(x + 4, y - direction * 2)
            p.drawPath(path)


class FolderAddButton(FloatingButton):
    """Scalable folder outline with a small plus badge."""

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate((self.width() - 28) / 2, (self.height() - 26) / 2)
        ink = QColor("#425476") if ACTIVE_THEME == "day" else QColor("#e3e7f1")
        p.setPen(QPen(ink, 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        folder = QPainterPath(QPointF(2, 7))
        folder.lineTo(2, 20)
        folder.quadTo(2, 22, 4, 22)
        folder.lineTo(22, 22)
        folder.quadTo(24, 22, 24, 20)
        folder.lineTo(24, 10)
        folder.quadTo(24, 8, 22, 8)
        folder.lineTo(12, 8)
        folder.lineTo(9, 5)
        folder.lineTo(4, 5)
        folder.quadTo(2, 5, 2, 7)
        p.drawPath(folder)
        p.setBrush(QColor("#17191e") if ACTIVE_THEME == "night" else QColor("#ffffff"))
        p.drawEllipse(QPointF(22, 5), 5.0, 5.0)
        p.drawLine(QPointF(22, 2.6), QPointF(22, 7.4))
        p.drawLine(QPointF(19.6, 5), QPointF(24.4, 5))


_STAR_CACHE = {}


def rounded_star_pixmap(checked, hovered):
    """512 px antialiased source; rounded five-point outline at display size."""
    key = (checked, hovered, ACTIVE_THEME)
    if key in _STAR_CACHE:
        return _STAR_CACHE[key]
    pixmap = QPixmap(512, 512)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    if checked:
        ink = QColor("#b78020") if ACTIVE_THEME == "day" else QColor("#f2cf55")
    elif hovered:
        ink = QColor("#ab8534") if ACTIVE_THEME == "day" else QColor("#e8cc73")
    else:
        ink = QColor("#7e8a9b") if ACTIVE_THEME == "day" else QColor("#7d8697")
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = 211 if i % 2 == 0 else 118
        points.append(QPointF(256 + math.cos(angle) * radius,
                              256 + math.sin(angle) * radius))
    path = QPainterPath()
    for i, point in enumerate(points):
        before = points[(i - 1) % 10]
        after = points[(i + 1) % 10]
        # Curving around each vertex produces a soft, clean five-point star.
        first = QPointF(point.x() * 0.84 + before.x() * 0.16,
                        point.y() * 0.84 + before.y() * 0.16)
        last = QPointF(point.x() * 0.84 + after.x() * 0.16,
                       point.y() * 0.84 + after.y() * 0.16)
        if i == 0:
            path.moveTo(first)
        else:
            path.lineTo(first)
        path.quadTo(point, last)
    path.closeSubpath()
    painter.setPen(QPen(ink, 15, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(ink if checked else Qt.NoBrush)
    painter.drawPath(path)
    painter.end()
    _STAR_CACHE[key] = pixmap
    return pixmap


class RoundedStarButton(FloatingButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setStyleSheet("QPushButton { border:0; background:transparent; padding:0; }")
        self._scale = 1.0
        self._hovered = False
        self._zoom = QPropertyAnimation(self, b"iconScale", self)
        self._zoom.setDuration(180)
        self._zoom.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self.update)

    def _get_scale(self):
        return self._scale

    def _set_scale(self, value):
        self._scale = float(value)
        self.update()

    iconScale = Property(float, _get_scale, _set_scale)

    def enterEvent(self, event):
        self._hovered = True
        self._zoom.stop()
        self._zoom.setStartValue(self._scale)
        self._zoom.setEndValue(1.16)
        self._zoom.start()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._zoom.stop()
        self._zoom.setStartValue(self._scale)
        self._zoom.setEndValue(1.0)
        self._zoom.start()
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        # The star is vector-rendered at 512 px, then smoothly scaled to the UI.
        pixmap = rounded_star_pixmap(self.isChecked(), self._hovered)
        size = int(min(self.width(), self.height()) * 0.74 * self._scale)
        if size <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap((self.width() - size) // 2, (self.height() - size) // 2,
                     pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))


AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".wave", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
LYRIC_EXTENSIONS = {".lrc", ".lyc"}


def _format_ms(ms):
    ms = max(0, int(ms or 0))
    sec = ms // 1000
    return f"{sec // 60}:{sec % 60:02d}"


def _safe_extract_zip(zip_path, out_dir):
    """Extract a zip without allowing path traversal."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = (out_dir / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src_f, open(target, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)


def _metadata_text(easy_tags, key, default=""):
    if not easy_tags:
        return default
    value = easy_tags.get(key)
    if not value:
        return default
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else default
    return str(value)


def _extract_cover_bytes(raw_audio):
    if raw_audio is None:
        return None
    try:
        pictures = getattr(raw_audio, "pictures", None)
        if pictures:
            return bytes(pictures[0].data)
    except Exception:
        pass

    tags = getattr(raw_audio, "tags", None)
    if tags is None:
        return None

    try:
        if hasattr(tags, "getall"):
            apic = tags.getall("APIC")
            if apic:
                return bytes(apic[0].data)
    except Exception:
        pass

    try:
        covr = tags.get("covr")
        if covr:
            return bytes(covr[0])
    except Exception:
        pass

    return None


def _read_track_info(path):
    path = Path(path)
    info = {
        "path": path,
        "title": path.stem,
        "artist": TXT("未知艺术家", "Unknown Artist"),
        "album": "",
        "track": "",
        "year": "",
        "duration_ms": 0,
        "cover_bytes": None,
        "cover_desc": TXT("无", "None"),
        "lyrics": [],
    }
    if not MUTAGEN_AVAILABLE:
        return info

    try:
        easy = MutagenFile(str(path), easy=True)
        if easy is not None:
            tags = getattr(easy, "tags", None) or {}
            info["title"] = _metadata_text(tags, "title", path.stem)
            info["artist"] = _metadata_text(tags, "artist", info["artist"])
            info["album"] = _metadata_text(tags, "album", "")
            info["track"] = _metadata_text(tags, "tracknumber", "")
            info["year"] = _metadata_text(tags, "date", "")
            length = getattr(getattr(easy, "info", None), "length", 0) or 0
            info["duration_ms"] = int(float(length) * 1000)
    except Exception:
        pass

    try:
        raw = MutagenFile(str(path), easy=False)
        cover = _extract_cover_bytes(raw)
        if cover:
            info["cover_bytes"] = cover
            info["cover_desc"] = TXT("内嵌封面", "Embedded")
    except Exception:
        pass

    # Plain WAV files frequently contain no metadata. Even then, read duration
    # directly from the RIFF/WAVE header so playlist timing still works.
    if path.suffix.lower() in {".wav", ".wave"} and not info["duration_ms"]:
        try:
            with wave.open(str(path), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.getnframes()
                if rate:
                    info["duration_ms"] = int((frames / float(rate)) * 1000)
        except Exception:
            pass

    return info


def _parse_lrc(path):
    """Parse LRC/LYC and group same-timestamp lines into one lyric slot.

    This supports bilingual lyrics naturally:
        [00:12.00]Original language
        [00:12.00]Translated language

    Both lines are kept together and displayed at the same time.
    More than two same-timestamp lines are also preserved.
    """
    grouped = {}
    if not path or not Path(path).exists():
        return []

    try:
        raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return []

    stamp_re = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")

    for line in raw.splitlines():
        stamps = list(stamp_re.finditer(line))
        if not stamps:
            continue

        lyric = stamp_re.sub("", line).strip()
        if not lyric:
            continue

        for m in stamps:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"

            if len(frac) == 1:
                frac_ms = int(frac) * 100
            elif len(frac) == 2:
                frac_ms = int(frac) * 10
            else:
                frac_ms = int(frac[:3])

            stamp_ms = minutes * 60000 + seconds * 1000 + frac_ms
            bucket = grouped.setdefault(stamp_ms, [])

            # Avoid accidental duplicated bilingual lines while preserving order.
            if lyric not in bucket:
                bucket.append(lyric)

    return [(stamp, lines) for stamp, lines in sorted(grouped.items())]



class DelayedToolTipButton(FloatingButton):
    """Icon button whose help text appears only after a 1-second hover."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_tip = ""
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.setInterval(1000)
        self._tip_timer.timeout.connect(self._show_delayed_tip)
        self.setMouseTracking(True)

    def setDelayedToolTip(self, text):
        self._hover_tip = str(text or "")

    def enterEvent(self, event):
        self._tip_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._tip_timer.stop()
        QToolTip.hideText()
        super().leaveEvent(event)

    def _show_delayed_tip(self):
        if self.underMouse() and self._hover_tip:
            QToolTip.showText(
                self.mapToGlobal(QPointF(self.width() / 2, self.height()).toPoint()),
                self._hover_tip,
                self,
            )


class TiltCoverLabel(QLabel):
    """A small perspective tilt that follows the parent card's hover progress."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.depth = 0.0

    def set_depth(self, depth):
        self.depth = float(depth)
        self.update()

    def paintEvent(self, event):
        pixmap = self.pixmap()
        if not pixmap or pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.translate(self.width() / 2 + self.depth * 1.5, self.height() / 2 - self.depth * 1.5)
        painter.shear(-0.075 * self.depth, 0.025 * self.depth)
        size = min(self.width(), self.height()) - 3
        rect = QRectF(-size / 2, -size / 2, size, size)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 11, 11)
        painter.setClipPath(clip)
        painter.drawPixmap(rect.toRect(), pixmap)


class HoverDepthCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._depth = 0.0
        self._lift_animation = QPropertyAnimation(self, b"cardDepth", self)
        self._lift_animation.setDuration(190)
        self._lift_animation.setEasingCurve(QEasingCurve.OutCubic)

    def _get_depth(self):
        return self._depth

    def _set_depth(self, value):
        self._depth = float(value)
        if hasattr(self, "cover_label"):
            self.cover_label.set_depth(self._depth)
        self.update()

    cardDepth = Property(float, _get_depth, _set_depth)

    def enterEvent(self, event):
        self._lift_animation.stop()
        self._lift_animation.setStartValue(self._depth)
        self._lift_animation.setEndValue(1.0)
        self._lift_animation.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._lift_animation.stop()
        self._lift_animation.setStartValue(self._depth)
        self._lift_animation.setEndValue(0.0)
        self._lift_animation.start()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        x, y = 2, 2 - self._depth
        w, h = self.width() - 5, self.height() - 7
        if w <= 0 or h <= 0:
            return
        day = ACTIVE_THEME == "day"
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(56, 73, 103, int(17 + 25 * self._depth)) if day
                         else QColor(0, 0, 0, int(45 + 55 * self._depth)))
        painter.drawRoundedRect(QRectF(x + 1 + self._depth * 2, y + 4 + self._depth * 3, w, h), 17, 17)
        painter.setBrush(QColor("#f8faff" if day else "#11161e"))
        painter.setPen(QPen(QColor(185, 201, 224, int(85 + 100 * self._depth)) if day
                            else QColor(105, 128, 164, int(35 + 96 * self._depth)), 1))
        painter.drawRoundedRect(QRectF(x, y, w, h), 17, 17)
        if self._depth > 0:
            painter.setPen(QPen(QColor(255, 255, 255, int(110 * self._depth)) if day
                                else QColor(198, 211, 238, int(84 * self._depth)), 1))
            painter.drawLine(QPointF(x + 17, y + 2), QPointF(x + w - 17, y + 2))


class MusicTrackRow(HoverDepthCard):
    clicked = Signal(int)
    favoriteToggled = Signal(int, bool)
    categoryRequested = Signal(int)

    def __init__(self, index, track, cover_pixmap, parent=None):
        super().__init__(parent)
        self.index = index
        self.track = track
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("musicTrackRow")
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setStyleSheet("QFrame#musicTrackRow { background:transparent; border:0; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 9, 10, 9)
        lay.setSpacing(10)

        cover = TiltCoverLabel()
        self.cover_label = cover
        cover.setFixedSize(48, 48)
        cover.setPixmap(_rounded_cover(cover_pixmap, 48, 12))
        cover.setStyleSheet("border-radius: 12px;")
        lay.addWidget(cover)

        words = QVBoxLayout()
        words.setSpacing(1)
        title = QLabel(track["title"])
        title.setStyleSheet("font-size: 14px; font-weight: 650; color: #ededed;")
        self.title_label = title
        artist = QLabel(track["artist"])
        artist.setStyleSheet("font-size: 12px; color: #919191;")
        self.artist_label = artist
        title.setTextInteractionFlags(Qt.NoTextInteraction)
        artist.setTextInteractionFlags(Qt.NoTextInteraction)
        words.addWidget(title)
        words.addWidget(artist)
        lay.addLayout(words, 1)

        self.favorite_btn = RoundedStarButton()
        self.favorite_btn.setCheckable(True)
        self.favorite_btn.setChecked(bool(track.get("favorite", False)))
        self.favorite_btn.setFixedSize(40, 40)
        self.favorite_btn.setCursor(Qt.PointingHandCursor)
        self.favorite_btn.setToolTip(TXT("收藏", "Favorite"))
        self.favorite_btn.clicked.connect(self._favorite_clicked)
        self._sync_star()
        lay.addWidget(self.favorite_btn)
        self.sync_download_state()

    def sync_download_state(self):
        saved = self.track.get("saved_locally", False)
        ready = saved or self.track.get("downloaded", False) or not self.track.get("online")
        self.title_label.setStyleSheet("font-size:14px; font-weight:650; color:" +
                                       ("#ededed" if ready else "#919191") + ";")
        if ACTIVE_THEME == "day":
            recolor_widget(self.title_label)
        self.title_label.setToolTip(TXT("已保存到本地", "Saved locally") if saved else "")
        self.setProperty("savedLocally", saved)
        self.update()

    def _sync_star(self):
        self.favorite_btn.update()

    def _favorite_clicked(self, checked):
        self._sync_star()
        self.favoriteToggled.emit(self.index, bool(checked))

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        category_action = menu.addAction(TXT("设置分类…", "Set Category…"))
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == category_action:
            self.categoryRequested.emit(self.index)

    def mousePressEvent(self, event):
        # Clicking the star must not also select/start the row through propagation.
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.position().toPoint())
            if child is self.favorite_btn:
                super().mousePressEvent(event)
                return
            self.clicked.emit(self.index)
        super().mousePressEvent(event)


class MusicAlbumHeader(HoverDepthCard):
    """Visual header for a group of tracks sharing the same album metadata."""
    toggled = Signal()

    def __init__(self, album_name, tracks, cover_pixmap, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("musicAlbumHeader")
        self.setStyleSheet("QFrame#musicAlbumHeader { background:transparent; border:0; }")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 12, 10)
        lay.setSpacing(10)

        cover = TiltCoverLabel()
        self.cover_label = cover
        cover.setFixedSize(56, 56)
        cover.setPixmap(
            _rounded_cover(cover_pixmap, 56, 12)
        )
        cover.setStyleSheet("border-radius: 12px;")
        lay.addWidget(cover)

        words = QVBoxLayout()
        words.setContentsMargins(0, 0, 0, 0)
        words.setSpacing(2)

        title = QLabel(str(album_name))
        title.setStyleSheet(
            "font-size: 14px; font-weight: 700; color: #eeeeee;"
        )
        title.setWordWrap(True)
        words.addWidget(title)

        artists = []
        for track in tracks:
            artist = str(track.get("artist", "") or "").strip()
            if artist and artist not in artists:
                artists.append(artist)

        if len(artists) == 1:
            detail = TXT(
                f"{artists[0]} · {len(tracks)} 首",
                f"{artists[0]} · {len(tracks)} tracks"
            )
        elif artists:
            detail = TXT(
                f"{len(tracks)} 首 · 多位艺术家",
                f"{len(tracks)} tracks · Various Artists"
            )
        else:
            detail = TXT(
                f"{len(tracks)} 首",
                f"{len(tracks)} tracks"
            )

        subtitle = QLabel(detail)
        subtitle.setStyleSheet(
            "font-size: 11px; color: #858585;"
        )
        words.addWidget(subtitle)
        words.addStretch(1)

        lay.addLayout(words, 1)
        self.arrow = QLabel("›")
        self.arrow.setStyleSheet("font-size:23px; color:#919191; background:transparent; border:0;")
        lay.addWidget(self.arrow)

    def set_expanded(self, expanded):
        self.arrow.setText("⌄" if expanded else "›")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggled.emit()
        super().mouseReleaseEvent(event)


class DesktopLyricsWindow(QWidget):
    positionChanged = Signal(int, int)

    def __init__(self, parent=None, saved_position=None):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.label = QLabel("", self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 12, 26, 12)
        lay.addWidget(self.label)

        self.settings = {}
        self.saved_position = saved_position if isinstance(saved_position, dict) else None
        self.editing = False
        self.real_lyric = ""
        self._drag_offset = None
        self._hue = 0
        self.rainbow_timer = QTimer(self)
        self.rainbow_timer.setInterval(55)
        self.rainbow_timer.timeout.connect(self._advance_rainbow)
        self.apply_settings({
            "font_family": QApplication.font().family(),
            "font_size": 30,
            "font_weight": 700,
            "text_color": "#ffffff",
            "rainbow": False,
            "background_enabled": False,
            "background_color": "#000000",
        })

    def apply_settings(self, settings):
        self.settings = dict(settings)
        if self.settings.get("rainbow"):
            self.rainbow_timer.start()
        else:
            self.rainbow_timer.stop()
        self._apply_style()
        self._reposition()

    def _advance_rainbow(self):
        self._hue = (self._hue + 3) % 360
        self._apply_style()

    def _apply_style(self):
        font = QFont(self.settings.get("font_family", QApplication.font().family()))
        font.setPointSize(int(self.settings.get("font_size", 30)))
        font.setWeight(QFont.Weight(int(self.settings.get("font_weight", 700))))
        self.label.setFont(font)

        if self.settings.get("rainbow"):
            color = QColor.fromHsv(self._hue, 220, 255).name()
        else:
            color = self.settings.get("text_color", "#ffffff")

        if self.settings.get("background_enabled"):
            bg = QColor(self.settings.get("background_color", "#000000"))
            bg_rgba = f"rgba({bg.red()},{bg.green()},{bg.blue()},170)"
        else:
            bg_rgba = "rgba(0,0,0,145)" if self.editing else "rgba(0,0,0,0)"

        self.label.setStyleSheet(
            f"color:{color}; background:{bg_rgba}; border-radius:18px; padding:8px 16px; "
            f"font-family:'{font.family()}'; font-size:{font.pointSize()}pt; font-weight:{font.weight()};"
            + ("border:1px solid #92a5d2;" if self.editing else "border:0;")
        )

    def set_lyric(self, text):
        self.real_lyric = text or ""
        if not self.editing:
            self.label.setText(self.real_lyric)
        if self.isVisible() and not self.editing:
            self._reposition()

    def set_position_editing(self, enabled):
        enabled = bool(enabled)
        if self.editing == enabled:
            return
        visible = self.isVisible()
        self.editing = enabled
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        if not enabled:
            flags |= Qt.WindowTransparentForInput
        self.hide()
        self.setWindowFlags(flags)
        self.setCursor(Qt.OpenHandCursor if enabled else Qt.ArrowCursor)
        self.label.setText(TXT("左键拖动此歌词片段", "Drag this lyric fragment")
                           if enabled else self.real_lyric)
        self._apply_style()
        self._reposition()
        if enabled or visible:
            self.show()
            self.raise_()

    def reset_position(self):
        self.saved_position = None
        self._reposition()

    def mousePressEvent(self, event):
        if self.editing and event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.editing and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.editing and event.button() == Qt.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self.setCursor(Qt.OpenHandCursor)
            self.saved_position = {"x": self.x(), "y": self.y()}
            self.positionChanged.emit(self.x(), self.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _reposition(self):
        saved = self.saved_position
        try:
            x = int(saved["x"])
            y = int(saved["y"])
        except (TypeError, ValueError, KeyError):
            saved = None
        screen = (QApplication.screenAt(QPoint(x, y)) if saved else None) or QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        width = max(520, int(geo.width() * 0.72))
        # Account for wrapped lyrics, padding and window margins at the selected
        # point size. A fixed 92 px window clipped large fonts and hid the change.
        available_text_width = max(100, width - 84)
        display_font = QFont(self.settings.get("font_family", QApplication.font().family()))
        display_font.setPointSize(int(self.settings.get("font_size", 30)))
        display_font.setWeight(QFont.Weight(int(self.settings.get("font_weight", 700))))
        metrics = QFontMetrics(display_font)
        text = self.label.text() or " "
        text_height = metrics.boundingRect(
            0, 0, available_text_width, 10000, Qt.TextWordWrap | Qt.AlignCenter, text
        ).height()
        height = min(geo.height(), max(92, text_height + 56))
        self.resize(width, height)
        if saved:
            x = max(geo.x(), min(x, geo.right() - width + 1))
            y = max(geo.y(), min(y, geo.bottom() - height + 1))
        else:
            x = geo.x() + (geo.width() - width) // 2
            y = geo.y() + geo.height() - height - 34
        self.move(x, y)


class HoverHomePanel(QFrame):
    """Animated edge highlight without repainting child controls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WA_Hover, True)
        self._hover_level = 0.0
        self._hover_animation = QPropertyAnimation(self, b'hoverLevel', self)
        self._hover_animation.setDuration(190)
        self._hover_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._background_thumbnail = None

    def _get_hover_level(self):
        return self._hover_level

    def _set_hover_level(self, value):
        self._hover_level = float(value)
        self.update()

    hoverLevel = Property(float, _get_hover_level, _set_hover_level)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._hover_level <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor('#5673a4' if ACTIVE_THEME == 'day' else '#9ab5e7')
        color.setAlpha(int(205 * self._hover_level))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 2))
        radius = 27 if self.objectName() == 'homeBanner' else 24
        painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), radius, radius)

    def enable_thumbnail(self):
        label = QLabel(self)
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName('homeBackground')
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        label.setStyleSheet('QLabel#homeBackground { background:#273244; color:#a2b1cb; border:0; border-radius:19px; }')
        self._background_thumbnail = label
        label.lower()
        self._layout_thumbnail()

    def _layout_thumbnail(self):
        if self._background_thumbnail is not None:
            x = int(self.width() * float(self.property('thumbnailStart') or 0.39))
            self._background_thumbnail.setGeometry(x, 18, max(20, self.width() - x - 18),
                                                   max(20, self.height() - 36))
            self._background_thumbnail.lower()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_thumbnail()

    def _animate_shadow(self, hovered):
        self._hover_animation.stop()
        self._hover_animation.setStartValue(self._hover_level)
        self._hover_animation.setEndValue(1.0 if hovered else 0.0)
        self._hover_animation.start()

    def enterEvent(self, event):
        self._animate_shadow(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_shadow(False)
        super().leaveEvent(event)


class CoverImageLabel(QLabel):
    """Paint a pixmap edge-to-edge using cover cropping without distortion."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def setSourcePixmap(self, pixmap):
        self._source_pixmap = QPixmap(pixmap)
        self.update()

    def clearSourcePixmap(self):
        self._source_pixmap = QPixmap()
        self.update()

    def paintEvent(self, event):
        if self._source_pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        target = QRectF(self.rect())
        source = QRectF(self._source_pixmap.rect())
        if target.width() <= 0 or target.height() <= 0:
            return

        target_ratio = target.width() / target.height()
        source_ratio = source.width() / source.height()
        if source_ratio > target_ratio:
            crop_w = source.height() * target_ratio
            source.setX((self._source_pixmap.width() - crop_w) / 2.0)
            source.setWidth(crop_w)
        else:
            crop_h = source.width() / target_ratio
            source.setY((self._source_pixmap.height() - crop_h) / 2.0)
            source.setHeight(crop_h)
        painter.drawPixmap(target, self._source_pixmap, source)


class HomeArtworkPanel(HoverHomePanel):
    """Foreground content sits over a large background thumbnail."""

    def __init__(self):
        super().__init__()
        self.enable_thumbnail()


class ClickableHomePanel(HoverHomePanel):
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class HomeLibraryBanner(HoverHomePanel):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.enable_thumbnail()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)



class PlaygroundCanvas(QWidget):
    themeTransitionFinished = Signal(str)
    """Multi-operator pseudo-3D playground.

    Each complete r/m/s GIF set becomes one actor. The selected actor is driven
    by WASD; every other awake actor uses personality-driven local pathfinding.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(560)
        self._keys = set()
        self._actors = []
        self._selected = -1
        self._asset_error = ''
        self._conversations = []
        self._next_conversation_id = 1
        self._manual_speeches = {}
        self._speech_actor_id = None
        self._social_history = []
        self._history_sequence = 0
        self._history_dialog = None

        # Dynamic pairwise affection is independent of the static relationship graph.
        # Every previously unseen pair starts at 50%.
        self._affection = self._load_affection_state()

        # Day/night playground transition. 1.0 = daylight, 0.0 = night.
        self._daylight_mix = 1.0 if ACTIVE_THEME == 'day' else 0.0
        self._daylight_start = self._daylight_mix
        self._daylight_target = self._daylight_mix
        self._daylight_elapsed = 0.0
        self._daylight_duration = 1.85
        self._daylight_running = False
        self._daylight_target_theme = ACTIVE_THEME

        self._speech_editor = QTextEdit(self)
        self._speech_editor.setAcceptRichText(False)
        self._speech_editor.setPlaceholderText(
            TXT('输入内容…  再按 Enter 发送 · Shift+Enter 换行',
                'Type…  press Enter again to send · Shift+Enter newline')
        )
        self._speech_editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._speech_editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._speech_editor.setLineWrapMode(QTextEdit.WidgetWidth)
        self._speech_editor.setStyleSheet(
            'QTextEdit {'
            ' background: rgba(232,235,240,218);'
            ' color: #252a33;'
            ' border: 1px solid rgba(150,158,170,220);'
            ' border-radius: 11px;'
            ' padding: 8px 10px;'
            ' selection-background-color: rgba(115,135,165,110);'
            '}'
        )
        self._speech_editor.hide()
        self._speech_editor.installEventFilter(self)
        self._speech_editor.textChanged.connect(self._resize_speech_editor)

        self._personality_data = self._load_personalities()
        self._discover_and_load_actors()

        self._clock = QTimer(self)
        self._clock.setInterval(16)
        self._clock.timeout.connect(self._tick)
        self._clock.start()

    @staticmethod
    def _mix_color(a, b, t):
        t = max(0.0, min(1.0, float(t)))
        return QColor(
            round(a.red() + (b.red() - a.red()) * t),
            round(a.green() + (b.green() - a.green()) * t),
            round(a.blue() + (b.blue() - a.blue()) * t),
            round(a.alpha() + (b.alpha() - a.alpha()) * t),
        )

    def _sleep_all_actors(self):
        self._keys.clear()
        self._selected = -1
        if self._speech_editor.isVisible():
            self._close_speech_editor(submit=False)
        for conversation in list(self._conversations):
            self._end_conversation(conversation)
        for actor in self._actors:
            actor['sleeping'] = True
            actor['target_actor_id'] = None
            actor['interaction_partner'] = None
            actor['conversation_id'] = None
            actor['rest_timer'] = 0.0
            actor['ai_timer'] = 0.0
            self._set_actor_mode(actor, 'sleep')

    def _wake_all_actors(self):
        self._keys.clear()
        self._selected = -1
        for actor in self._actors:
            actor['sleeping'] = False
            actor['interaction_partner'] = None
            actor['conversation_id'] = None
            actor['interaction_mode'] = None
            self._set_actor_mode(actor, 'relax')
            self._choose_ai_target(actor, force=True)

    def begin_daylight_transition(self, target_theme):
        target_theme = str(target_theme).lower()
        if target_theme not in ('day', 'night'):
            return

        # Actor state changes immediately; the room lighting then changes slowly.
        if target_theme == 'night':
            self._sleep_all_actors()
        else:
            self._wake_all_actors()

        desired = 1.0 if target_theme == 'day' else 0.0
        self._daylight_start = float(self._daylight_mix)
        self._daylight_target = desired
        self._daylight_elapsed = 0.0
        self._daylight_target_theme = target_theme

        # If the visual state already matches, skip the redundant animation/theme switch.
        if abs(self._daylight_start - desired) < 0.001 and ACTIVE_THEME == target_theme:
            self._daylight_running = False
            self.update()
            return

        self._daylight_running = True
        self.update()

    def sync_daylight_to_theme(self):
        if self._daylight_running:
            return
        self._daylight_mix = 1.0 if ACTIVE_THEME == 'day' else 0.0
        self._daylight_start = self._daylight_mix
        self._daylight_target = self._daylight_mix
        self.update()

    def _tick_daylight_transition(self, dt):
        if not self._daylight_running:
            return
        self._daylight_elapsed += dt
        progress = min(1.0, self._daylight_elapsed / max(0.1, self._daylight_duration))
        # Smoothstep gives a slow, cinematic start/end rather than a linear flash.
        eased = progress * progress * (3.0 - 2.0 * progress)
        self._daylight_mix = (
            self._daylight_start
            + (self._daylight_target - self._daylight_start) * eased
        )
        if progress >= 1.0:
            self._daylight_mix = self._daylight_target
            self._daylight_running = False
            self.themeTransitionFinished.emit(self._daylight_target_theme)

    # ---------- dynamic affection ----------

    @staticmethod
    def _affection_pair_key(a, b):
        left = str(a.get('id', a) if isinstance(a, dict) else a).casefold()
        right = str(b.get('id', b) if isinstance(b, dict) else b).casefold()
        return '::'.join(sorted((left, right)))

    def _load_affection_state(self):
        try:
            loaded = json.loads(
                PLAYGROUND_AFFECTION_FILE.read_text(encoding='utf-8')
            )
            if isinstance(loaded, dict):
                pairs = loaded.get('pairs', loaded)
                if isinstance(pairs, dict):
                    clean = {}
                    for key, value in pairs.items():
                        try:
                            clean[str(key)] = max(0.0, min(200.0, float(value)))
                        except (TypeError, ValueError):
                            pass
                    return clean
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_affection_state(self):
        try:
            _write_json(
                PLAYGROUND_AFFECTION_FILE,
                {'version': 1, 'pairs': self._affection}
            )
        except OSError:
            pass

    def _affection_between(self, actor, other):
        key = self._affection_pair_key(actor, other)
        try:
            return max(0.0, min(200.0, float(self._affection.get(key, 50.0))))
        except (TypeError, ValueError):
            return 50.0

    def _affection_category(self, value):
        value = max(0.0, min(200.0, float(value)))
        if value < 10.0:
            return TXT('恶劣', 'Hostile')
        if value < 20.0:
            return TXT('较差', 'Poor')
        if value < 50.0:
            return TXT('疏远', 'Distant')
        if value < 90.0:
            return TXT('普通', 'Neutral')
        if value < 140.0:
            return TXT('良好', 'Good')
        return TXT('深厚', 'Deep')

    def _change_affection(self, actor, other, delta):
        key = self._affection_pair_key(actor, other)
        before = self._affection_between(actor, other)
        # Hard safety limits requested by the design.
        delta = max(-1.65, min(2.0, float(delta)))
        after = max(0.0, min(200.0, before + delta))
        self._affection[key] = round(after, 4)
        self._save_affection_state()
        return before, after, delta

    @staticmethod
    def _conversation_type_table():
        # 12 dialogue/social outcomes. The four neutral types intentionally
        # change no affection, while positive/negative values remain capped.
        return (
            {
                'id': 'deep_resonance',
                'name_zh': '深度共鸣', 'name_en': 'Deep resonance',
                'kind': 'positive', 'delta': (1.45, 2.00), 'base': 0.045,
            },
            {
                'id': 'warm_support',
                'name_zh': '温和支持', 'name_en': 'Warm support',
                'kind': 'positive', 'delta': (1.00, 1.55), 'base': 0.060,
            },
            {
                'id': 'shared_humor',
                'name_zh': '轻松共鸣', 'name_en': 'Shared humor',
                'kind': 'positive', 'delta': (0.60, 1.20), 'base': 0.075,
            },
            {
                'id': 'pleasant_exchange',
                'name_zh': '愉快交流', 'name_en': 'Pleasant exchange',
                'kind': 'positive', 'delta': (0.20, 0.70), 'base': 0.090,
            },
            {
                'id': 'ordinary_chat',
                'name_zh': '普通闲谈', 'name_en': 'Ordinary chat',
                'kind': 'neutral', 'delta': (0.0, 0.0), 'base': 0.165,
            },
            {
                'id': 'practical_exchange',
                'name_zh': '事务交流', 'name_en': 'Practical exchange',
                'kind': 'neutral', 'delta': (0.0, 0.0), 'base': 0.155,
            },
            {
                'id': 'quiet_company',
                'name_zh': '安静陪伴', 'name_en': 'Quiet company',
                'kind': 'neutral', 'delta': (0.0, 0.0), 'base': 0.145,
            },
            {
                'id': 'awkward_pause',
                'name_zh': '略显尴尬', 'name_en': 'Awkward pause',
                'kind': 'neutral', 'delta': (0.0, 0.0), 'base': 0.125,
            },
            {
                'id': 'mild_disagreement',
                'name_zh': '轻微分歧', 'name_en': 'Mild disagreement',
                'kind': 'negative', 'delta': (-0.50, -0.15), 'base': 0.060,
            },
            {
                'id': 'sharp_disagreement',
                'name_zh': '明显分歧', 'name_en': 'Sharp disagreement',
                'kind': 'negative', 'delta': (-0.85, -0.45), 'base': 0.040,
            },
            {
                'id': 'offense',
                'name_zh': '产生冒犯', 'name_en': 'Offense',
                'kind': 'negative', 'delta': (-1.20, -0.75), 'base': 0.025,
            },
            {
                'id': 'serious_clash',
                'name_zh': '严重冲突', 'name_en': 'Serious clash',
                'kind': 'negative', 'delta': (-1.65, -1.10), 'base': 0.015,
            },
        )

    def _choose_conversation_outcome(self, actor, other):
        relation_weight = self._relationship(actor, other)['weight']
        affection = self._affection_between(actor, other)

        # Static relationship and dynamic affection influence probabilities
        # independently. A neutral colleague relationship strongly favors
        # no-change dialogue, while close relationships favor positive outcomes.
        relation_positive = max(0.0, min(1.0, (relation_weight - 1.0) / 1.20))
        relation_neutral = max(0.0, 1.0 - abs(relation_weight - 1.0) * 1.10)

        affection_positive = max(0.0, min(1.0, (affection - 65.0) / 90.0))
        affection_negative = max(0.0, min(1.0, (45.0 - affection) / 45.0))

        weighted = []
        total = 0.0
        for entry in self._conversation_type_table():
            weight = float(entry['base'])
            kind = entry['kind']

            if kind == 'positive':
                weight *= (
                    0.78
                    + 2.15 * relation_positive
                    + 0.80 * affection_positive
                )
                if relation_weight <= 1.05:
                    weight *= 0.78
            elif kind == 'neutral':
                weight *= (
                    1.0
                    + 0.85 * relation_neutral
                    - 0.25 * relation_positive
                )
                if 35.0 <= affection <= 80.0:
                    weight *= 1.18
            else:
                weight *= (
                    0.90
                    + 1.55 * affection_negative
                    - 0.52 * relation_positive
                )
                if relation_weight >= 1.65:
                    weight *= 0.60

            # Every evaluation includes a bounded random parameter.
            weight *= random.uniform(0.84, 1.16)
            weight = max(0.0001, weight)
            weighted.append((entry, weight))
            total += weight

        pick = random.random() * total
        chosen = weighted[-1][0]
        for entry, weight in weighted:
            pick -= weight
            if pick <= 0:
                chosen = entry
                break

        low, high = chosen['delta']
        delta = random.uniform(float(low), float(high))
        # Prevent tiny floating-point residue on neutral outcomes.
        if abs(delta) < 1e-9:
            delta = 0.0
        before, after, applied = self._change_affection(actor, other, delta)
        return {
            'id': chosen['id'],
            'name': TXT(chosen['name_zh'], chosen['name_en']),
            'kind': chosen['kind'],
            'delta': applied,
            'before': before,
            'after': after,
            'category': self._affection_category(after),
        }

    def _low_affection_response(self, actor, other):
        """Return (social multiplier, avoidance probability) by personality."""
        affection = self._affection_between(actor, other)
        if affection >= 20.0:
            return 1.0, 0.0

        profile = actor['profile']
        mode = str(profile.get('low_affection_response', 'withdraw')).lower()
        social_mult = float(
            profile.get('low_affection_social_multiplier', 0.45) or 0.45
        )
        avoid = float(
            profile.get('low_affection_avoidance', 0.35) or 0.35
        )

        # 0-10 is more severe than 10-20.
        severity = 1.0 if affection < 10.0 else 0.68
        if mode == 'persistent':
            social_mult = max(social_mult, 0.68)
            avoid *= 0.35
        elif mode == 'avoid':
            social_mult *= 0.72
            avoid = max(avoid, 0.62)
        elif mode == 'volatile':
            social_mult *= random.uniform(0.55, 0.95)
            avoid *= random.uniform(0.70, 1.25)

        return max(0.05, min(1.0, social_mult)), max(
            0.0, min(0.95, avoid * severity)
        )

    def _avoid_low_affection_actor(self, actor, others):
        candidates = []
        for other in others:
            affection = self._affection_between(actor, other)
            if affection >= 20.0:
                continue
            social_mult, avoid_probability = self._low_affection_response(
                actor, other
            )
            if random.random() >= avoid_probability:
                continue
            distance = self._world_distance(actor, other)
            radius = float(
                actor['profile'].get('low_affection_avoid_radius', 0.31) or 0.31
            )
            if distance <= radius:
                candidates.append((affection, distance, other))

        if not candidates:
            return None

        # Prefer avoiding the lowest-affection nearby person.
        _affection, _distance, other = min(
            candidates, key=lambda item: (item[0], item[1])
        )
        vx = actor['x'] - other['x']
        vd = actor['depth'] - other['depth']
        mag = max(0.001, math.hypot(vx, vd))
        push = float(
            actor['profile'].get('low_affection_avoid_distance', 0.30) or 0.30
        )
        actor['target_actor_id'] = None
        return (
            max(0.10, min(0.90, actor['x'] + vx / mag * push)),
            max(0.14, min(0.90, actor['depth'] + vd / mag * push)),
            'affection_avoid',
        )

    # ---------- asset discovery ----------

    @staticmethod
    def _animation_token(filename, fallback='actor'):
        """Return (actor_id, mode) for foo_m/foo_r/foo_s/foo_in GIF names."""
        stem = Path(filename).stem
        stem = re.sub(r'\(\d+\)$', '', stem).strip()
        match = re.match(r'^(.*?)[_-](in|[mrs])$', stem, re.I)
        if match:
            actor_id = match.group(1).strip(' _-') or fallback
            token = match.group(2).lower()
        elif stem.lower() in ('m', 'r', 's', 'in'):
            actor_id, token = fallback, stem.lower()
        else:
            return None
        mode = {'m': 'move', 'r': 'relax', 's': 'sleep', 'in': 'social'}[token]
        return actor_id, mode

    @staticmethod
    def _safe_actor_id(value):
        value = re.sub(r'\s+', '_', str(value).strip())
        value = re.sub(r'[^\w.-]+', '_', value, flags=re.UNICODE).strip('._')
        return value or 'actor'

    def _discover_actor_sets(self):
        """Discover complete r/m/s sets from loose GIFs and every ZIP under resource/gif."""
        root = RESOURCE_GIF_DIR
        cache = CACHE_DIR / 'playground_actors'
        shutil.rmtree(cache, ignore_errors=True)
        cache.mkdir(parents=True, exist_ok=True)

        groups = {}
        sources = {}

        def add_file(actor_id, mode, path, source_name):
            key = self._safe_actor_id(actor_id).casefold()
            groups.setdefault(key, {})[mode] = Path(path)
            sources.setdefault(key, source_name)

        # Loose GIFs / folders are supported as well as ZIP packs.
        if root.is_dir():
            for path in root.rglob('*.gif'):
                parsed = self._animation_token(path.name, path.parent.name)
                if parsed:
                    actor_id, mode = parsed
                    add_file(actor_id, mode, path, str(path.parent))

        archives = sorted(root.rglob('*.zip')) if root.is_dir() else []
        errors = []
        for archive in archives:
            archive_groups = {}
            try:
                with zipfile.ZipFile(archive, 'r') as zf:
                    for member in zf.infolist():
                        if member.is_dir() or not member.filename.lower().endswith('.gif'):
                            continue
                        fallback = Path(archive.stem).name
                        parsed = self._animation_token(Path(member.filename).name, fallback)
                        if not parsed:
                            continue
                        actor_id, mode = parsed
                        key = self._safe_actor_id(actor_id).casefold()
                        archive_groups.setdefault(key, {})[mode] = member

                    for key, modes in archive_groups.items():
                        if not all(m in modes for m in ('move', 'relax', 'sleep')):
                            continue
                        # Avoid overriding a complete loose-file set.
                        if key in groups and all(m in groups[key] for m in ('move', 'relax', 'sleep')):
                            continue
                        actor_cache = cache / self._safe_actor_id(f'{archive.stem}_{key}')
                        actor_cache.mkdir(parents=True, exist_ok=True)
                        extracted = {}
                        for mode, suffix in (('move', 'm'), ('relax', 'r'), ('sleep', 's'), ('social', 'in')):
                            if mode not in modes:
                                continue
                            target = actor_cache / f'{key}_{suffix}.gif'
                            with zf.open(modes[mode], 'r') as source, target.open('wb') as dest:
                                shutil.copyfileobj(source, dest)
                            if target.stat().st_size > 128:
                                extracted[mode] = target
                        if all(m in extracted for m in ('move', 'relax', 'sleep')):
                            groups[key] = extracted
                            sources[key] = archive.name
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                errors.append(f'{archive.name}: {error}')

        complete = []
        for key, modes in sorted(groups.items()):
            if all(m in modes for m in ('move', 'relax', 'sleep')):
                complete.append({
                    'id': key,
                    'source': sources.get(key, ''),
                    'paths': modes,
                })

        if not complete:
            details = '; '.join(errors) if errors else TXT(
                'resource/gif 中没有发现完整的 _m/_r/_s GIF 组合',
                'No complete _m/_r/_s GIF set was found under resource/gif')
            self._asset_error = details
        return complete

    # ---------- personality JSON ----------

    def _load_personalities(self):
        """Load AI profiles and the weighted relationship graph."""
        built_in = {
            'profiles': {},
            'characters': {},
            'relationships': {},
            'relationship_tiers': {},
            'defaults': {'relationship_weight': 1.0},
        }
        path = RESOURCE_GIF_DIR / 'playground_personalities.json'
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    for key in ('profiles', 'characters', 'relationships',
                                'relationship_tiers', 'defaults'):
                        value = loaded.get(key)
                        if isinstance(value, dict):
                            built_in[key].update(value)
            except (OSError, ValueError, TypeError):
                pass
        return built_in

    def _personality_for(self, actor_id):
        profiles = self._personality_data.get('profiles') or {}
        characters = self._personality_data.get('characters') or {}
        char = characters.get(actor_id, characters.get(actor_id.casefold(), {})) or {}
        if not char:
            for _key, data in characters.items():
                aliases = [str(a).casefold() for a in (data.get('aliases') or [])]
                if actor_id.casefold() in aliases:
                    char = data
                    break

        profile_name = char.get('profile')
        if profile_name not in profiles:
            names = sorted(profiles) or ['default']
            digest = hashlib.sha1(actor_id.encode('utf-8', errors='ignore')).hexdigest()
            profile_name = names[int(digest[:8], 16) % len(names)] if names else 'default'

        profile = dict(profiles.get(profile_name) or {})
        for key, value in char.items():
            if key not in ('profile', 'aliases'):
                profile[key] = value
        profile['profile_name'] = profile_name
        profile['display_name'] = profile.get('display_name') or actor_id
        profile['speed_multiplier'] = float(profile.get('speed_multiplier', 1.0) or 1.0)
        return profile

    @staticmethod
    def _visible_rect(pixmap):
        if pixmap.isNull():
            return QRect()
        try:
            mask = pixmap.mask()
            if not mask.isNull():
                rect = QRegion(mask).boundingRect()
                if rect.isValid() and not rect.isNull():
                    return rect
        except Exception:
            pass
        return pixmap.rect()

    def _make_movies(self, spec):
        movies = {}
        visible_union = QRect()
        invalid = []
        required = ('move', 'relax', 'sleep')
        available = list(required)
        if spec['paths'].get('social'):
            available.append('social')

        frame_rect = QRect()
        for mode in available:
            path = spec['paths'][mode]
            movie = QMovie(str(path), parent=self)
            movie.setCacheMode(QMovie.CacheAll)
            movie.setSpeed(100)
            if not movie.isValid():
                if mode in required:
                    invalid.append(f'{mode}: {Path(path).name}')
                movie.deleteLater()
                continue

            movie.jumpToFrame(0)
            frame = movie.currentPixmap()
            if not frame.isNull():
                if frame_rect.isNull():
                    frame_rect = QRect(frame.rect())
                visible = self._visible_rect(frame)
                if visible.isValid() and not visible.isNull():
                    visible_union = (
                        QRect(visible) if visible_union.isNull()
                        else visible_union.united(visible)
                    )

            movie.frameChanged.connect(lambda _frame: self.update())
            movies[mode] = movie

        if invalid or any(mode not in movies for mode in required):
            for movie in movies.values():
                movie.deleteLater()
            return None, QRect(), invalid

        # One crop rectangle for the whole operator, shared by m/r/s/in.
        # Therefore switching action never changes the operator's physical scale.
        if visible_union.isNull():
            visible_union = QRect(frame_rect)
        else:
            pad = max(
                6,
                int(max(visible_union.width(), visible_union.height()) * 0.055)
            )
            visible_union = visible_union.adjusted(
                -pad, -pad, pad, pad
            ).intersected(frame_rect)

        return movies, visible_union, []

    def _discover_and_load_actors(self):
        specs = self._discover_actor_sets()
        invalid = []
        count = max(1, len(specs))
        for index, spec in enumerate(specs):
            movies, source_rect, problems = self._make_movies(spec)
            if not movies:
                invalid.extend(f"{spec['id']}: {p}" for p in problems)
                continue

            profile = self._personality_for(spec['id'])
            # Spread initial actors across the floor deterministically.
            x = 0.16 + (0.68 * ((index * 0.61803398875) % 1.0))
            depth = 0.34 + (0.46 * ((index * 0.38196601125 + 0.22) % 1.0))
            actor = {
                'id': spec['id'],
                'display_name': profile.get('display_name', spec['id']),
                'source': spec.get('source', ''),
                'movies': movies,
                'mode': None,
                'source_rect': source_rect,
                'x': x,
                'depth': depth,
                'facing': 1 if index % 2 == 0 else -1,
                'sleeping': False,
                'rect': QRectF(),
                'profile': profile,
                'target_x': x,
                'target_depth': depth,
                'target_reason': 'initial',
                'target_actor_id': None,
                'ai_timer': 0.0,
                'rest_timer': 0.0,
                'social_cooldown': 0.0,
                'interaction_partner': None,
                'interaction_timer': 0.0,
                'interaction_mode': None,
                'recent_social': {},
                'post_social_roam_timer': 0.0,
                'burst_timer': 0.0,
                'move_speed': 0.0,
                'perimeter_index': index % 4,
                'perimeter_reverse': bool(index % 2),
                'conversation_id': None,
                'conversation_role': None,
            }
            self._actors.append(actor)
            self._set_actor_mode(actor, 'relax')
            self._choose_ai_target(actor, force=True)

        if invalid:
            self._asset_error = '; '.join(invalid)
        if self._actors:
            self._selected = 0

    def _set_actor_mode(self, actor, mode):
        if mode == actor.get('mode'):
            return
        old = actor.get('mode')
        if old in actor['movies']:
            actor['movies'][old].stop()
        actor['mode'] = mode
        movie = actor['movies'].get(mode)
        if movie:
            movie.start()

    # ---------- AI ----------

    @staticmethod
    def _range_value(value, default):
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                return random.uniform(float(value[0]), float(value[1]))
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _other_actors(self, actor):
        return [other for other in self._actors
                if other is not actor and not other.get('sleeping')]

    @staticmethod
    def _world_distance(a, b):
        return math.hypot(a['x'] - b['x'], a['depth'] - b['depth'])

    def _actor_identity_set(self, actor):
        values = {
            str(actor.get('id', '')).casefold(),
            str(actor.get('display_name', '')).casefold(),
        }
        characters = self._personality_data.get('characters') or {}
        for key, data in characters.items():
            aliases = [str(a).casefold() for a in (data.get('aliases') or [])]
            if actor.get('id', '').casefold() == str(key).casefold() or actor.get('id', '').casefold() in aliases:
                values.add(str(key).casefold())
                values.update(aliases)
        return {v for v in values if v}

    def _relationship(self, actor, other):
        """Unknown or friendly-colleague relationships use the neutral weight 1."""
        default_weight = float(
            (self._personality_data.get('defaults') or {}).get('relationship_weight', 1.0) or 1.0)
        relationships = self._personality_data.get('relationships') or {}
        a_ids = self._actor_identity_set(actor)
        b_ids = self._actor_identity_set(other)

        def lookup(left_ids, right_ids):
            for left in left_ids:
                mapping = relationships.get(left) or relationships.get(left.casefold()) or {}
                for right in right_ids:
                    value = mapping.get(right) or mapping.get(right.casefold())
                    if value is not None:
                        return value
            return None

        data = lookup(a_ids, b_ids)
        if data is None:
            data = lookup(b_ids, a_ids)
        if isinstance(data, (int, float)):
            data = {'weight': float(data)}
        data = dict(data or {})
        data.setdefault('label', '友好同事/外人')
        data.setdefault('weight', default_weight)
        data['weight'] = max(0.05, float(data['weight']))
        return data

    def _weighted_social_choice(self, actor, others):
        candidates, total = [], 0.0
        recent = actor.get('recent_social') or {}
        for other in others:
            # A recently-socialized partner is temporarily excluded. This stops
            # leave-return-interact loops while keeping the relationship intact.
            if float(recent.get(other['id'], 0.0) or 0.0) > 0:
                continue
            relation = self._relationship(actor, other)
            distance = max(0.04, self._world_distance(actor, other))
            affection = self._affection_between(actor, other)
            social_mult, _avoid_probability = self._low_affection_response(
                actor, other
            )
            affinity_factor = max(0.38, min(1.55, 0.72 + affection / 120.0))
            score = (
                relation['weight']
                * affinity_factor
                * social_mult
                / (0.18 + distance)
            )
            candidates.append((other, relation, score))
            total += score

        if total <= 0:
            return None
        pick = random.random() * total
        for other, relation, score in candidates:
            pick -= score
            if pick <= 0:
                return other, relation
        return candidates[-1][0], candidates[-1][1]

    def _social_target(self, actor):
        profile = actor['profile']
        if actor.get('social_cooldown', 0.0) > 0:
            return None
        if actor.get('post_social_roam_timer', 0.0) > 0:
            return None

        others = self._other_actors(actor)
        if not others:
            return None

        # Very low dynamic affection can suppress contact or cause active
        # avoidance, but how strongly this happens depends on personality.
        affection_avoid = self._avoid_low_affection_actor(actor, others)
        if affection_avoid is not None:
            return affection_avoid

        # Crowd avoidance is evaluated before attraction.
        crowd_radius = float(profile.get('crowd_radius', 0.28) or 0.28)
        nearby = [o for o in others if self._world_distance(actor, o) <= crowd_radius]
        avoidance = float(profile.get('crowd_avoidance', 0.0) or 0.0)
        if nearby and random.random() < avoidance:
            cx = sum(o['x'] for o in nearby) / len(nearby)
            cd = sum(o['depth'] for o in nearby) / len(nearby)
            vx, vd = actor['x'] - cx, actor['depth'] - cd
            mag = max(0.001, math.hypot(vx, vd))
            push = float(profile.get('avoidance_distance', 0.30) or 0.30)
            actor['target_actor_id'] = None
            return (
                max(0.10, min(0.90, actor['x'] + vx / mag * push)),
                max(0.14, min(0.90, actor['depth'] + vd / mag * push)),
                'avoid',
            )

        picked = self._weighted_social_choice(actor, others)
        if not picked:
            return None
        other, relation = picked
        weight = relation['weight']

        # Sociality is a possible goal, not the default goal. Relationship weight
        # modifies the chance, but even highly social actors continue normal movement.
        base_goal = float(profile.get('social_goal_chance', 0.12) or 0.12)
        relation_gain = float(profile.get('relationship_goal_gain', 0.55) or 0.55)
        chance = base_goal * (1.0 + relation_gain * max(0.0, weight - 1.0))
        social_mult, _avoid_probability = self._low_affection_response(actor, other)
        affection = self._affection_between(actor, other)
        affinity_factor = max(0.45, min(1.30, 0.72 + affection / 150.0))
        chance *= social_mult * affinity_factor
        chance = min(float(profile.get('max_social_goal_chance', 0.48) or 0.48), chance)
        if random.random() >= chance:
            return None

        preferred = float(profile.get('preferred_social_distance', 0.18) or 0.18)
        preferred /= max(0.86, min(1.28, weight ** 0.16))
        if self._world_distance(actor, other) <= preferred * 0.70:
            return None

        jitter = float(profile.get('social_jitter', 0.05) or 0.05)
        actor['target_actor_id'] = other['id']
        return (
            max(0.10, min(0.90, other['x'] + random.uniform(-jitter, jitter))),
            max(0.14, min(0.90, other['depth'] + random.uniform(-jitter, jitter))),
            'social',
        )

    def _actor_by_id(self, actor_id):
        for actor in self._actors:
            if actor.get('id') == actor_id:
                return actor
        return None

    def _social_duration(self, actor, other):
        a = self._range_value(actor['profile'].get('social_duration'), 1.8)
        b = self._range_value(other['profile'].get('social_duration'), 1.8)
        weight = self._relationship(actor, other)['weight']
        return max(0.7, ((a + b) * 0.5) * min(1.45, 0.90 + 0.16 * weight))

    def _conversation_by_id(self, conversation_id):
        if not conversation_id:
            return None
        for conversation in self._conversations:
            if conversation.get('id') == conversation_id:
                return conversation
        return None

    def _conversation_members(self, conversation):
        members = []
        ids = set(conversation.get('participants') or [])
        for actor in self._actors:
            if actor.get('id') in ids and actor.get('conversation_id') == conversation.get('id'):
                members.append(actor)
        return members

    def _conversation_centroid(self, conversation):
        members = self._conversation_members(conversation)
        if not members:
            return 0.5, 0.5
        return (
            sum(actor['x'] for actor in members) / len(members),
            sum(actor['depth'] for actor in members) / len(members),
        )

    def _speech_style_for(self, actor):
        style = str(actor['profile'].get('speech_style', 'neutral')).strip().lower()
        if style:
            return style
        social = float(actor['profile'].get('conversation_join_chance', 0.0) or 0.0)
        if social >= 0.22:
            return 'lively'
        if social <= 0.05:
            return 'brief'
        return 'neutral'

    def _generate_gibberish(self, actor):
        style = self._speech_style_for(actor)
        if style == 'chaotic':
            alphabet = list('xzvnmkrst#%&*@!?/\\|~+-=')
            groups = random.randint(7, 14)
            width = (2, 5)
        elif style == 'lively':
            alphabet = list('aeiourstlmnpqyz<>[]{}?!+-=*')
            groups = random.randint(6, 12)
            width = (3, 6)
        elif style == 'brief':
            alphabet = list('mnrstlvx.-_?')
            groups = random.randint(3, 6)
            width = (2, 4)
        elif style == 'stern':
            alphabet = list('TRKLVMNXYZ/\\|-+*')
            groups = random.randint(4, 8)
            width = (2, 5)
        else:
            alphabet = list('aemnorstuvxyz#?+-=')
            groups = random.randint(4, 9)
            width = (2, 5)
        chunks = []
        for _ in range(groups):
            n = random.randint(width[0], width[1])
            chunks.append(''.join(random.choice(alphabet) for _ in range(n)))
        line = ' '.join(chunks)
        if random.random() < 0.35:
            line += random.choice([' ...', ' ??', ' !!', ' ~'])
        return line

    def _conversation_turn_limit(self, participants, duration):
        low, high = 4.0, 8.0
        weights = []
        for actor in participants:
            turns = actor['profile'].get('conversation_turns')
            if isinstance(turns, (list, tuple)) and len(turns) >= 2:
                try:
                    weights.append((float(turns[0]), float(turns[1])))
                except (TypeError, ValueError):
                    pass
        if weights:
            low = sum(pair[0] for pair in weights) / len(weights)
            high = sum(pair[1] for pair in weights) / len(weights)
        base = random.randint(max(2, int(round(low))), max(3, int(round(high))))
        bonus = int(max(0.0, duration - 1.0) * 1.35)
        return max(3, base + bonus)

    def _conversation_speaker_weights(self, conversation):
        members = self._conversation_members(conversation)
        last_id = conversation.get('speaker_id')
        weighted = []
        for actor in members:
            weight = float(actor['profile'].get('conversation_speakiness', 1.0) or 1.0)
            if actor.get('id') == last_id and len(members) > 1:
                weight *= 0.24
            weight = max(0.05, weight)
            weighted.append((actor, weight))
        return weighted

    def _speaker_animation_mode(self, actor):
        if 'social' in actor['movies'] and random.random() < 0.20:
            return 'social'
        return 'relax'

    def _begin_conversation_turn(self, conversation, speaker=None):
        members = self._conversation_members(conversation)
        if len(members) < 2:
            self._end_conversation(conversation)
            return
        if speaker is None:
            weighted = self._conversation_speaker_weights(conversation)
            total = sum(weight for _actor, weight in weighted)
            pick = random.random() * total if total > 0 else 0.0
            speaker = weighted[-1][0]
            for actor, weight in weighted:
                pick -= weight
                if pick <= 0:
                    speaker = actor
                    break
        conversation['speaker_id'] = speaker['id']
        conversation['text'] = self._generate_gibberish(speaker)
        conversation['shown_text'] = ''
        conversation['char_progress'] = 0.0

        # Gibberish placeholder dialogue is real dialogue for history purposes.
        # Record the complete generated line immediately so an interrupted turn
        # is still present in Social History.
        target_ids = [
            actor_id
            for actor_id in conversation.get('participants', [])
            if actor_id != speaker.get('id')
        ]
        affection_event = None
        if not conversation.get('affection_event_logged'):
            affection_event = conversation.get('affection_event')
            conversation['affection_event_logged'] = True
        self._record_social_history(
            speaker,
            conversation['text'],
            target_ids=target_ids,
            source='ai',
            conversation_id=conversation.get('id'),
            affection_event=affection_event,
        )
        conversation['history_recorded'] = True
        conversation['state'] = 'typing'
        conversation['pause_timer'] = 0.0
        conversation['typing_speed'] = max(
            8.0,
            self._range_value(speaker['profile'].get('typing_speed'), 18.0)
        )
        center_x, center_depth = self._conversation_centroid(conversation)
        for actor in members:
            actor['interaction_mode'] = 'relax'
            if abs(center_x - actor['x']) > 0.01:
                actor['facing'] = 1 if center_x > actor['x'] else -1
        speaker['interaction_mode'] = self._speaker_animation_mode(speaker)
        conversation['join_check_timer'] = min(conversation.get('join_check_timer', 1.6), 1.6)

    def _start_conversation(self, actor, other, duration):
        affection_event = self._choose_conversation_outcome(actor, other)
        conversation = {
            'id': self._next_conversation_id,
            'participants': [actor['id'], other['id']],
            'anchor_pair': [actor['id'], other['id']],
            'speaker_id': None,
            'text': '',
            'shown_text': '',
            'char_progress': 0.0,
            'typing_speed': 16.0,
            'pause_timer': 0.0,
            'state': 'typing',
            'turn_index': 0,
            'max_turns': self._conversation_turn_limit([actor, other], duration),
            'join_check_timer': random.uniform(1.1, 2.1),
            'affection_event': affection_event,
            'affection_event_logged': False,
        }
        self._next_conversation_id += 1
        self._conversations.append(conversation)
        actor['conversation_id'] = conversation['id']
        other['conversation_id'] = conversation['id']
        actor['conversation_role'] = 'anchor'
        other['conversation_role'] = 'anchor'
        self._begin_conversation_turn(conversation, random.choice([actor, other]))
        return conversation

    def _end_conversation(self, conversation):
        if isinstance(conversation, int):
            conversation = self._conversation_by_id(conversation)
        if conversation is None:
            return
        members = self._conversation_members(conversation)
        if conversation in self._conversations:
            self._conversations.remove(conversation)
        for actor in members:
            for other in members:
                if other is actor:
                    continue
                cooldown = self._range_value(
                    actor['profile'].get('repeat_partner_cooldown'), 16.0)
                actor.setdefault('recent_social', {})[other['id']] = cooldown
            actor['conversation_id'] = None
            actor['conversation_role'] = None
            actor['interaction_partner'] = None
            actor['interaction_timer'] = 0.0
            actor['interaction_mode'] = None
            actor['social_cooldown'] = self._range_value(
                actor['profile'].get('social_cooldown'), 5.0)
            actor['post_social_roam_timer'] = self._range_value(
                actor['profile'].get('post_social_roam'), 7.0)
            chance = float(
                actor['profile'].get('burst_after_social_chance', 0.0) or 0.0)
            if random.random() < chance:
                actor['burst_timer'] = self._range_value(
                    actor['profile'].get('burst_duration'), 1.5)
            actor['target_actor_id'] = None
            actor['ai_timer'] = 0.0
            self._set_actor_mode(actor, 'sleep' if actor.get('sleeping') else 'relax')

    def _leave_conversation(self, actor):
        conversation = self._conversation_by_id(actor.get('conversation_id'))
        if conversation is not None:
            self._end_conversation(conversation)
        else:
            actor['conversation_id'] = None
            actor['conversation_role'] = None

    def _maybe_join_conversation(self, conversation):
        members = self._conversation_members(conversation)
        if len(members) < 2:
            return False
        center_x, center_depth = self._conversation_centroid(conversation)
        candidates = []
        for actor in self._actors:
            if actor.get('sleeping') or actor.get('conversation_id'):
                continue
            if actor.get('interaction_partner'):
                continue
            if 0 <= self._selected < len(self._actors) and self._actors[self._selected] is actor and self._keys:
                continue
            profile = actor['profile']
            base = float(profile.get('conversation_join_chance', 0.06) or 0.06)
            if base <= 0:
                continue
            near = min(self._world_distance(actor, other) for other in members)
            radius = float(profile.get('conversation_join_radius', 0.24) or 0.24)
            if near > radius:
                continue
            social_weight = max(self._relationship(actor, other)['weight'] for other in members)
            crowd_penalty = max(0.45, 1.0 - 0.10 * max(0, len(members) - 2))
            chance = min(0.72, base * (0.72 + 0.36 * social_weight) * crowd_penalty)
            candidates.append((actor, chance, abs(actor['x'] - center_x) + abs(actor['depth'] - center_depth)))
        candidates.sort(key=lambda item: item[2])
        for actor, chance, _priority in candidates:
            if random.random() < chance:
                actor['conversation_id'] = conversation['id']
                actor['conversation_role'] = 'joiner'
                actor['target_actor_id'] = None
                actor['target_reason'] = 'conversation'
                actor['rest_timer'] = 0.0
                actor['ai_timer'] = 0.0
                actor['interaction_mode'] = 'relax'
                if actor['id'] not in conversation['participants']:
                    conversation['participants'].append(actor['id'])

                # Joining an existing conversation is also a social event.
                # Apply exactly one outcome against the nearest current member.
                existing = [member for member in members if member is not actor]
                if existing:
                    nearest_member = min(
                        existing,
                        key=lambda other: self._world_distance(actor, other)
                    )
                    join_event = self._choose_conversation_outcome(
                        actor, nearest_member
                    )
                    self._record_social_history(
                        actor,
                        self._generate_gibberish(actor),
                        target_ids=[nearest_member['id']],
                        source='ai',
                        conversation_id=conversation.get('id'),
                        affection_event=join_event,
                    )

                actor['facing'] = 1 if center_x > actor['x'] else -1
                return True
        return False

    def _tick_conversations(self, dt):
        for conversation in list(self._conversations):
            members = self._conversation_members(conversation)
            if len(members) < 2:
                self._end_conversation(conversation)
                continue

            conversation['join_check_timer'] = max(
                0.0, float(conversation.get('join_check_timer', 0.0)) - dt)
            if conversation['join_check_timer'] <= 0:
                conversation['join_check_timer'] = random.uniform(1.2, 2.6)
                self._maybe_join_conversation(conversation)

            state = conversation.get('state', 'typing')
            speaker = self._actor_by_id(conversation.get('speaker_id'))
            if speaker is None or speaker.get('conversation_id') != conversation.get('id'):
                self._begin_conversation_turn(conversation)
                continue

            if state == 'typing':
                text = conversation.get('text', '')
                conversation['char_progress'] += float(conversation.get('typing_speed', 16.0)) * dt
                visible = min(len(text), int(conversation['char_progress']))
                conversation['shown_text'] = text[:visible]
                if visible >= len(text):
                    if not conversation.get('history_recorded'):
                        target_ids = [
                            actor_id
                            for actor_id in conversation.get('participants', [])
                            if actor_id != speaker.get('id')
                        ]
                        affection_event = None
                        if not conversation.get('affection_event_logged'):
                            affection_event = conversation.get('affection_event')
                            conversation['affection_event_logged'] = True
                        self._record_social_history(
                            speaker,
                            text,
                            target_ids=target_ids,
                            source='ai',
                            conversation_id=conversation.get('id'),
                            affection_event=affection_event,
                        )
                        conversation['history_recorded'] = True
                    conversation['state'] = 'pause'
                    pause = self._range_value(
                        speaker['profile'].get('conversation_turn_pause'), 0.62)
                    conversation['pause_timer'] = max(0.18, pause)
            else:
                conversation['pause_timer'] = max(0.0, float(conversation.get('pause_timer', 0.0)) - dt)
                if conversation['pause_timer'] <= 0:
                    conversation['turn_index'] = int(conversation.get('turn_index', 0)) + 1
                    if conversation['turn_index'] >= int(conversation.get('max_turns', 5)):
                        self._end_conversation(conversation)
                        continue
                    self._begin_conversation_turn(conversation)

    def _start_social_interaction(self, actor, other):
        if actor is other or actor.get('sleeping') or other.get('sleeping'):
            return False
        if actor.get('interaction_partner') or other.get('interaction_partner'):
            return False
        if actor.get('conversation_id') or other.get('conversation_id'):
            return False
        if self._selected >= 0:
            selected = self._actors[self._selected]
            if selected in (actor, other) and self._keys:
                return False

        relation = self._relationship(actor, other)
        base = float(actor['profile'].get('interaction_chance', 0.55) or 0.55)
        social_mult, _avoid_probability = self._low_affection_response(actor, other)
        chance = min(
            0.90,
            base
            * max(0.65, min(1.45, relation['weight']))
            * social_mult
        )
        if random.random() > chance:
            return False

        duration = self._social_duration(actor, other)
        actor['interaction_partner'] = other['id']
        other['interaction_partner'] = actor['id']
        actor['interaction_timer'] = duration
        other['interaction_timer'] = duration
        actor['target_actor_id'] = None
        other['target_actor_id'] = None
        actor['facing'] = 1 if other['x'] >= actor['x'] else -1
        other['facing'] = 1 if actor['x'] >= other['x'] else -1
        self._start_conversation(actor, other, duration)
        return True

    def _end_social_interaction(self, actor):
        conversation = self._conversation_by_id(actor.get('conversation_id'))
        if conversation is not None:
            self._end_conversation(conversation)
            return

        partner_id = actor.get('interaction_partner')
        partner = self._actor_by_id(partner_id) if partner_id else None
        pair = [actor] + ([partner] if partner is not None else [])
        for participant in pair:
            other = partner if participant is actor else actor
            if other is not None:
                cooldown = self._range_value(
                    participant['profile'].get('repeat_partner_cooldown'), 16.0)
                participant.setdefault('recent_social', {})[other['id']] = cooldown
            participant['interaction_partner'] = None
            participant['interaction_timer'] = 0.0
            participant['interaction_mode'] = None
            participant['social_cooldown'] = self._range_value(
                participant['profile'].get('social_cooldown'), 5.0)
            participant['post_social_roam_timer'] = self._range_value(
                participant['profile'].get('post_social_roam'), 7.0)
            self._set_actor_mode(participant, 'relax')
            chance = float(
                participant['profile'].get('burst_after_social_chance', 0.0) or 0.0)
            if random.random() < chance:
                participant['burst_timer'] = self._range_value(
                    participant['profile'].get('burst_duration'), 1.5)
            participant['target_actor_id'] = None
            participant['ai_timer'] = 0.0

    def _maybe_observe(self, actor):
        profile = actor['profile']
        chance = float(profile.get('observe_chance', 0.0) or 0.0)
        if chance <= 0 or random.random() >= chance:
            return False
        others = self._other_actors(actor)
        if not others:
            return False
        radius = float(profile.get('observe_radius', 0.30) or 0.30)
        visible = [o for o in others if self._world_distance(actor, o) <= radius]
        if not visible:
            return False
        nearest = min(visible, key=lambda o: self._world_distance(actor, o))
        actor['facing'] = 1 if nearest['x'] > actor['x'] else -1
        actor['rest_timer'] = self._range_value(profile.get('observe_duration'), 2.4)
        self._set_actor_mode(actor, 'relax')
        return True

    def _choose_behavior_style(self, profile):
        mix = profile.get('behavior_mix')
        if isinstance(mix, dict) and mix:
            weighted = []
            total = 0.0
            for name, weight in mix.items():
                try:
                    value = max(0.0, float(weight))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    weighted.append((str(name).lower(), value))
                    total += value
            if total > 0:
                pick = random.random() * total
                for name, weight in weighted:
                    pick -= weight
                    if pick <= 0:
                        return name
                return weighted[-1][0]
        return str(profile.get('behavior', 'wander')).lower()

    def _choose_ai_target(self, actor, force=False):
        profile = actor['profile']
        if not force and actor['rest_timer'] > 0:
            return

        actor['target_actor_id'] = None

        social = self._social_target(actor)
        if social is not None:
            actor['target_x'], actor['target_depth'], actor['target_reason'] = social
        else:
            behavior = self._choose_behavior_style(profile)

            if behavior == 'perimeter':
                corners = [(0.12, 0.18), (0.88, 0.18),
                           (0.88, 0.88), (0.12, 0.88)]
                idx = actor['perimeter_index'] % 4
                actor['target_x'], actor['target_depth'] = corners[idx]
                actor['target_reason'] = 'perimeter'

            elif behavior in ('drifter', 'drift'):
                actor['target_x'] = 0.84 if actor['x'] < 0.5 else 0.16
                bias = float(profile.get('depth_bias', 0.58))
                variance = float(profile.get('depth_variance', 0.18))
                actor['target_depth'] = max(
                    0.16, min(0.88, random.uniform(bias - variance, bias + variance)))
                actor['target_reason'] = 'drift'

            elif behavior == 'patrol':
                actor['target_x'] = random.choice((0.18, 0.82))
                bias = float(profile.get('depth_bias', 0.56))
                variance = float(profile.get('depth_variance', 0.24))
                actor['target_depth'] = max(
                    0.16, min(0.88, random.uniform(bias - variance, bias + variance)))
                actor['target_reason'] = 'patrol'

            elif behavior == 'local_wander':
                # Small nearby displacement; useful for active characters who
                # frequently change their mind without crossing the whole room.
                actor['target_x'] = max(
                    0.12, min(0.88, actor['x'] + random.uniform(-0.22, 0.22)))
                actor['target_depth'] = max(
                    0.14, min(0.90, actor['depth'] + random.uniform(-0.18, 0.18)))
                actor['target_reason'] = 'local_wander'

            else:  # wander
                actor['target_x'] = random.uniform(0.12, 0.88)
                bias = float(profile.get('depth_bias', 0.58))
                variance = float(profile.get('depth_variance', 0.32))
                actor['target_depth'] = max(
                    0.14, min(0.90, random.uniform(bias - variance, bias + variance)))
                actor['target_reason'] = 'wander'

        actor['ai_timer'] = self._range_value(
            profile.get('turn_interval'), 3.5)
        actor['move_speed'] = self._range_value(
            profile.get('speed'), 0.09)

    def _tick_ai(self, actor, dt):
        if actor['sleeping']:
            if actor.get('interaction_partner') or actor.get('conversation_id'):
                self._leave_conversation(actor)
            self._set_actor_mode(actor, 'sleep')
            return

        actor['social_cooldown'] = max(
            0.0, actor.get('social_cooldown', 0.0) - dt)
        actor['post_social_roam_timer'] = max(
            0.0, actor.get('post_social_roam_timer', 0.0) - dt)
        actor['burst_timer'] = max(
            0.0, actor.get('burst_timer', 0.0) - dt)

        recent = actor.get('recent_social') or {}
        for partner_id in list(recent):
            recent[partner_id] = max(0.0, float(recent[partner_id]) - dt)
            if recent[partner_id] <= 0:
                recent.pop(partner_id, None)

        conversation = self._conversation_by_id(actor.get('conversation_id'))
        if actor.get('interaction_partner'):
            partner = self._actor_by_id(actor['interaction_partner'])
            if conversation is None or partner is None:
                self._end_social_interaction(actor)
            else:
                mode = actor.get('interaction_mode') or 'relax'
                if mode not in actor['movies']:
                    mode = 'relax'
                cx, _cy = self._conversation_centroid(conversation)
                if abs(cx - actor['x']) > 0.01:
                    actor['facing'] = 1 if cx > actor['x'] else -1
                self._set_actor_mode(actor, mode)
            return

        if conversation is not None:
            mode = actor.get('interaction_mode') or 'relax'
            if mode not in actor['movies']:
                mode = 'relax'
            cx, _cy = self._conversation_centroid(conversation)
            if abs(cx - actor['x']) > 0.01:
                actor['facing'] = 1 if cx > actor['x'] else -1
            self._set_actor_mode(actor, mode)
            return

        if actor['rest_timer'] > 0:
            before = actor['rest_timer']
            actor['rest_timer'] = max(0.0, actor['rest_timer'] - dt)
            self._set_actor_mode(actor, 'relax')
            if before > 0 and actor['rest_timer'] <= 0:
                chance = float(
                    actor['profile'].get('burst_after_pause_chance', 0.0) or 0.0)
                if random.random() < chance:
                    actor['burst_timer'] = self._range_value(
                        actor['profile'].get('burst_duration'), 1.3)
            return

        actor['ai_timer'] -= dt
        dx = actor['target_x'] - actor['x']
        dd = actor['target_depth'] - actor['depth']
        distance = math.hypot(dx, dd)

        if actor.get('target_reason') == 'social' and actor.get('target_actor_id'):
            other = self._actor_by_id(actor['target_actor_id'])
            if other is not None:
                trigger = float(
                    actor['profile'].get('interaction_distance', 0.095) or 0.095)
                if self._world_distance(actor, other) <= trigger:
                    if self._start_social_interaction(actor, other):
                        return
                    actor['social_cooldown'] = self._range_value(
                        actor['profile'].get('social_cooldown'), 4.0)
                    actor['target_actor_id'] = None
                    actor['ai_timer'] = 0.0

        if distance < 0.025 or actor['ai_timer'] <= 0:
            profile = actor['profile']
            if self._maybe_observe(actor):
                return
            if random.random() < float(profile.get('rest_chance', 0.1)):
                actor['rest_timer'] = self._range_value(
                    profile.get('rest_duration'), 1.5)
                self._set_actor_mode(actor, 'relax')
                return
            if (actor.get('target_reason') == 'perimeter'
                    and distance < 0.05):
                step = -1 if actor.get('perimeter_reverse') else 1
                actor['perimeter_index'] = (
                    actor['perimeter_index'] + step) % 4
            self._choose_ai_target(actor, force=True)
            dx = actor['target_x'] - actor['x']
            dd = actor['target_depth'] - actor['depth']
            distance = max(0.0001, math.hypot(dx, dd))

        speed = float(
            actor.get('move_speed')
            or self._range_value(actor['profile'].get('speed'), 0.09)
        )
        speed *= float(actor['profile'].get('speed_multiplier', 1.0))
        if actor.get('burst_timer', 0.0) > 0:
            speed *= float(
                actor['profile'].get('burst_multiplier', 1.6) or 1.6)

        amount = min(distance, speed * dt)
        if distance > 0:
            actor['x'] += (dx / distance) * amount
            actor['depth'] += (dd / distance) * amount
            if abs(dx) > 0.002:
                actor['facing'] = 1 if dx > 0 else -1
        self._set_actor_mode(actor, 'move')

    def _tick_selected(self, actor, dt):
        if actor['sleeping']:
            self._keys.clear()
            self._set_actor_mode(actor, 'sleep')
            return

        moving = bool(self._keys & {Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D})
        if moving and actor.get('conversation_id'):
            self._leave_conversation(actor)
        if not moving:
            conversation = self._conversation_by_id(actor.get('conversation_id'))
            if conversation is not None:
                mode = actor.get('interaction_mode') or 'relax'
                if mode not in actor['movies']:
                    mode = 'relax'
                self._set_actor_mode(actor, mode)
            else:
                self._set_actor_mode(actor, 'relax')
            return

        speed = self._range_value(actor['profile'].get('speed'), 0.10)
        speed *= max(1.0, float(actor['profile'].get('speed_multiplier', 1.0)))
        manual = max(0.105, speed * 1.15)
        depth_speed = manual * 0.88

        if Qt.Key_A in self._keys:
            actor['x'] -= manual * dt
            actor['facing'] = -1
        if Qt.Key_D in self._keys:
            actor['x'] += manual * dt
            actor['facing'] = 1
        if Qt.Key_W in self._keys:
            actor['depth'] -= depth_speed * dt
        if Qt.Key_S in self._keys:
            actor['depth'] += depth_speed * dt
        self._set_actor_mode(actor, 'move')

    def _tick(self):
        dt = self._clock.interval() / 1000.0
        for index, actor in enumerate(self._actors):
            if index == self._selected:
                self._tick_selected(actor, dt)
            else:
                self._tick_ai(actor, dt)

            actor['x'] = max(0.07, min(0.93, actor['x']))
            actor['depth'] = max(0.10, min(0.93, actor['depth']))

        self._tick_conversations(dt)
        self._tick_manual_speeches(dt)
        self._tick_daylight_transition(dt)
        if self._speech_editor.isVisible():
            self._position_speech_editor()
        self.update()

    def _history_actor_name(self, actor_id):
        actor = self._actor_by_id(actor_id)
        return actor.get('display_name', actor_id) if actor is not None else str(actor_id)

    def _record_social_history(self, speaker, text, target_ids=None,
                               source='ai', conversation_id=None,
                               affection_event=None):
        text = str(text or '').strip()
        if not text or speaker is None:
            return

        if isinstance(speaker, dict):
            speaker_id = speaker.get('id', '')
            speaker_name = speaker.get('display_name', speaker_id)
        else:
            speaker_id = str(speaker)
            speaker_name = self._history_actor_name(speaker_id)

        targets = []
        for target_id in (target_ids or []):
            if target_id and target_id != speaker_id:
                targets.append({
                    'id': target_id,
                    'name': self._history_actor_name(target_id),
                })

        self._history_sequence += 1
        self._social_history.append({
            'sequence': self._history_sequence,
            'speaker_id': speaker_id,
            'speaker_name': speaker_name,
            'targets': targets,
            'text': text,
            'source': source,
            'conversation_id': conversation_id,
            'affection_event': dict(affection_event or {}),
        })

        # Avoid unbounded growth during very long sessions.
        if len(self._social_history) > 600:
            self._social_history = self._social_history[-600:]

    def _show_social_history(self):
        if self._history_dialog is not None:
            try:
                self._history_dialog.close()
            except RuntimeError:
                pass

        dialog = QDialog(self)
        dialog.setWindowTitle(TXT('Social History · 社交记录',
                                  'Social History'))
        dialog.resize(720, 620)
        dialog.setModal(False)
        self._history_dialog = dialog

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel(TXT('Social History', 'Social History'))
        title.setStyleSheet('font-size:22px; font-weight:700;')
        header.addWidget(title)
        header.addStretch()

        count = QLabel()
        count.setStyleSheet('color:#7d8795;')
        header.addWidget(count)
        outer.addLayout(header)

        controls = QHBoxLayout()
        user_only = VisibleCheckBox(TXT('仅显示用户发言', 'User messages only'))
        user_only.setChecked(False)
        user_only.setStyleSheet(
            'QCheckBox { spacing: 8px; padding: 2px 0; }'
        )
        controls.addWidget(user_only)
        controls.addStretch()
        outer.addLayout(controls)

        sub = QLabel(TXT(
            '按时间顺序记录干员之间的对话；玩家发送的信息会标记为「用户控制」。',
            'Conversation history in chronological order; player messages are marked as user-controlled.'
        ))
        sub.setWordWrap(True)
        sub.setStyleSheet('color:#8b939f;')
        outer.addWidget(sub)

        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(scroll)
        feed = QVBoxLayout(body)
        feed.setContentsMargins(2, 4, 8, 4)
        feed.setSpacing(10)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        def clear_feed():
            while feed.count():
                item = feed.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
                child_layout = item.layout()
                if child_layout is not None:
                    while child_layout.count():
                        child_item = child_layout.takeAt(0)
                        child_widget = child_item.widget()
                        if child_widget is not None:
                            child_widget.deleteLater()

        def refresh_history():
            clear_feed()
            entries = self._social_history
            if user_only.isChecked():
                entries = [
                    entry for entry in entries
                    if entry.get('source') == 'user'
                ]

            if user_only.isChecked():
                count.setText(TXT(
                    f'{len(entries)} 条用户记录',
                    f'{len(entries)} user messages'
                ))
            else:
                count.setText(TXT(
                    f'{len(entries)} 条记录',
                    f'{len(entries)} messages'
                ))

            if not entries:
                empty = QLabel(
                    TXT(
                        '没有用户发言记录。' if user_only.isChecked()
                        else '还没有社交记录。',
                        'No user messages yet.' if user_only.isChecked()
                        else 'No social history yet.'
                    )
                )
                empty.setAlignment(Qt.AlignCenter)
                empty.setStyleSheet('color:#8b939f; padding:36px;')
                feed.addWidget(empty)
            else:
                for entry in entries:
                    card = QFrame(body)
                    card.setObjectName('socialHistoryCard')
                    card.setStyleSheet(
                        'QFrame#socialHistoryCard {'
                        ' background: rgba(128,136,148,22);'
                        ' border: 1px solid rgba(128,136,148,48);'
                        ' border-radius: 12px;'
                        '}'
                    )
                    card_layout = QVBoxLayout(card)
                    card_layout.setContentsMargins(13, 10, 13, 11)
                    card_layout.setSpacing(5)

                    targets = entry.get('targets') or []
                    if entry.get('source') == 'user':
                        relation_text = TXT(
                            '用户控制 · 无固定社交目标',
                            'User-controlled · no fixed social target'
                        )
                    elif targets:
                        target_names = '、'.join(t['name'] for t in targets)
                        relation_text = TXT(
                            f'对 {target_names}',
                            f'to {", ".join(t["name"] for t in targets)}'
                        )
                    else:
                        relation_text = TXT(
                            '无固定社交目标',
                            'no fixed social target'
                        )

                    meta = QLabel(
                        f"<b>{entry.get('speaker_name', '')}</b>"
                        f"  <span style='color:#8b939f'>· {relation_text}</span>"
                    )
                    meta.setTextFormat(Qt.RichText)
                    card_layout.addWidget(meta)

                    affection_event = entry.get('affection_event') or {}
                    if affection_event:
                        delta = float(affection_event.get('delta', 0.0) or 0.0)
                        after = float(affection_event.get('after', 50.0) or 50.0)
                        category = affection_event.get('category', '')
                        sign = '+' if delta > 0 else ''
                        outcome = QLabel(TXT(
                            f"对话类型：{affection_event.get('name', '')} · "
                            f"好感 {sign}{delta:.2f}% · 当前 {after:.2f}%（{category}）",
                            f"Dialogue: {affection_event.get('name', '')} · "
                            f"Affection {sign}{delta:.2f}% · now {after:.2f}% ({category})"
                        ))
                        outcome.setStyleSheet(
                            'color:#7d8795; font-size:12px; padding:0 0 2px 0;'
                        )
                        card_layout.addWidget(outcome)

                    message = QLabel(entry.get('text', ''))
                    message.setWordWrap(True)
                    message.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    message.setStyleSheet(
                        'font-size:14px; padding:2px 0 1px 0;'
                    )
                    card_layout.addWidget(message)
                    feed.addWidget(card)

            feed.addStretch()

        user_only.toggled.connect(refresh_history)
        refresh_history()

        close_btn = FloatingButton(TXT('关闭', 'Close'))
        close_btn.clicked.connect(dialog.close)
        outer.addWidget(close_btn, 0, Qt.AlignRight)

        dialog.finished.connect(
            lambda _result: setattr(self, '_history_dialog', None)
            if self._history_dialog is dialog else None
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _selected_actor(self):
        if 0 <= self._selected < len(self._actors):
            return self._actors[self._selected]
        return None

    def _open_speech_editor(self):
        actor = self._selected_actor()
        if actor is None or actor.get('sleeping'):
            return
        self._keys.clear()
        self._speech_actor_id = actor['id']
        self._speech_editor.clear()
        self._speech_editor.show()
        self._resize_speech_editor()
        self._position_speech_editor()
        self._speech_editor.raise_()
        self._speech_editor.setFocus(Qt.ShortcutFocusReason)

    def _close_speech_editor(self, submit=False):
        if not self._speech_editor.isVisible():
            return
        actor = self._actor_by_id(self._speech_actor_id)
        content = self._speech_editor.toPlainText().strip()
        if submit and actor is not None and content:
            # Second Enter sends. Player speech remains above the operator
            # for at least two visible seconds and has no fixed social target.
            self._manual_speeches[actor['id']] = {
                'text': content,
                'created_at': time.monotonic(),
                'expires_at': time.monotonic() + 2.35,
            }
            self._record_social_history(
                actor,
                content,
                target_ids=[],
                source='user',
                conversation_id=None,
            )
        self._speech_editor.hide()
        self._speech_editor.clear()
        self._speech_actor_id = None
        self.setFocus(Qt.ShortcutFocusReason)
        self.update()

    def _resize_speech_editor(self):
        if not self._speech_editor.isVisible():
            return

        text_value = self._speech_editor.toPlainText()
        fm = QFontMetrics(self._speech_editor.font())

        # Grow horizontally until the maximum width, then wrap naturally.
        longest = max(text_value.splitlines() or [''], key=len)
        wanted_width = fm.horizontalAdvance(longest) + 46
        width = max(190, min(390, wanted_width))

        document = self._speech_editor.document()
        document.setTextWidth(max(120, width - 28))
        wanted_height = int(document.size().height()) + 28
        height = max(48, min(230, wanted_height))

        self._speech_editor.resize(width, height)
        self._position_speech_editor()

    def _position_speech_editor(self):
        if not self._speech_editor.isVisible():
            return
        actor = self._actor_by_id(self._speech_actor_id)
        if actor is None:
            self._close_speech_editor(False)
            return

        rect = actor.get('rect') or QRectF()
        if rect.isNull():
            return

        width = self._speech_editor.width()
        height = self._speech_editor.height()
        x = int(rect.center().x() - width / 2)
        y = int(rect.top() - height - 18)

        x = max(8, min(self.width() - width - 8, x))
        y = max(8, min(self.height() - height - 8, y))
        self._speech_editor.move(x, y)
        self._speech_editor.raise_()

    def _tick_manual_speeches(self, dt):
        # Manual speech is deliberately independent from AI/social state.
        # Ending a conversation, changing modes, or returning to AI must not
        # remove a player-sent bubble before its own expiry time.
        now = time.monotonic()
        for actor_id in list(self._manual_speeches):
            data = self._manual_speeches[actor_id]
            expires_at = float(data.get('expires_at', now))
            if now >= expires_at:
                self._manual_speeches.pop(actor_id, None)

    def eventFilter(self, watched, event):
        if watched is self._speech_editor and event.type() == QEvent.KeyPress:
            if (event.key() == Qt.Key_S
                    and event.modifiers() & Qt.ControlModifier):
                self._show_social_history()
                event.accept()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                self._close_speech_editor(submit=True)
                event.accept()
                return True
            if event.key() == Qt.Key_Escape:
                self._close_speech_editor(submit=False)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_S
                and event.modifiers() & Qt.ControlModifier):
            self._keys.clear()
            self._show_social_history()
            event.accept()
            return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._selected_actor() is not None:
                self._open_speech_editor()
            event.accept()
            return

        if event.key() == Qt.Key_R:
            if self._speech_editor.isVisible():
                self._close_speech_editor(submit=False)
            # Release manual control: no actor remains selected.
            self._selected = -1
            self._keys.clear()
            for actor in self._actors:
                if not actor.get('sleeping') and not actor.get('conversation_id'):
                    self._choose_ai_target(actor, force=True)
            self.update()
            event.accept()
            return

        if event.key() in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
            if 0 <= self._selected < len(self._actors) and not self._actors[self._selected]['sleeping']:
                self._keys.add(event.key())
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        self._keys.discard(event.key())
        if event.key() in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _actor_at(self, point):
        # Near actors are visually on top, so test from nearest to farthest.
        order = sorted(range(len(self._actors)), key=lambda i: self._actors[i]['depth'], reverse=True)
        for index in order:
            if self._actors[index]['rect'].contains(point):
                return index
        return -1

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        hit = self._actor_at(event.position())

        if event.button() == Qt.LeftButton and hit >= 0:
            if self._speech_editor.isVisible():
                self._close_speech_editor(submit=False)
            self._selected = hit
            self._keys.clear()
            self.update()
            event.accept()
            return

        if event.button() == Qt.RightButton and hit >= 0:
            actor = self._actors[hit]
            menu = QMenu(self)
            action = menu.addAction(
                TXT('把她叫醒', 'Wake up') if actor['sleeping']
                else TXT('让她睡觉', 'Let sleep')
            )
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen is action:
                actor['sleeping'] = not actor['sleeping']
                if hit == self._selected:
                    self._keys.clear()
                self._set_actor_mode(actor, 'sleep' if actor['sleeping'] else 'relax')
                if not actor['sleeping'] and hit != self._selected:
                    self._choose_ai_target(actor, force=True)
            event.accept()
            return

        super().mousePressEvent(event)

    # ---------- room projection ----------

    def _room_geometry(self):
        """Return a pseudo-3D room whose far end is a rectangular wall."""
        w, h = float(self.width()), float(self.height())
        return {
            'front_left': w * 0.045,
            'front_right': w * 0.955,
            'floor_bottom': h * 0.965,
            'back_left': w * 0.255,
            'back_right': w * 0.745,
            'back_top': h * 0.085,
            'back_bottom': h * 0.355,
        }

    def _floor_bounds_at_depth(self, depth):
        room = self._room_geometry()
        t = max(0.0, min(1.0, float(depth)))
        left = room['back_left'] + (room['front_left'] - room['back_left']) * t
        right = room['back_right'] + (room['front_right'] - room['back_right']) * t
        return left, right

    def _ground_y_at_depth(self, depth):
        room = self._room_geometry()
        t = max(0.0, min(1.0, float(depth)))
        return room['back_bottom'] + (room['floor_bottom'] - room['back_bottom']) * (t ** 1.72)

    def _draw_grid(self, painter):
        # `_daylight_mix` is independent from ACTIVE_THEME while animating.
        # 0 = full night, 1 = full daylight.
        daylight = max(0.0, min(1.0, float(self._daylight_mix)))
        bg = self._mix_color(QColor('#06080b'), QColor('#eef8ff'), daylight)
        wall_fill = self._mix_color(QColor('#090d12'), QColor('#e7f4fd'), daylight)
        floor_fill = self._mix_color(QColor('#070a0e'), QColor('#edf8ff'), daylight)
        major = self._mix_color(
            QColor(195, 210, 230, 82),
            QColor(35, 145, 220, 125),
            daylight,
        )
        minor = self._mix_color(
            QColor(145, 160, 180, 42),
            QColor(60, 165, 225, 58),
            daylight,
        )

        painter.fillRect(self.rect(), bg)
        room = self._room_geometry()
        bl, br = room['back_left'], room['back_right']
        bt, bb = room['back_top'], room['back_bottom']
        fl, fr = room['front_left'], room['front_right']
        fb = room['floor_bottom']

        # Real rectangular back wall.
        back_wall = QRectF(bl, bt, br - bl, bb - bt)
        painter.fillRect(back_wall, wall_fill)

        floor = QPainterPath()
        floor.moveTo(bl, bb)
        floor.lineTo(br, bb)
        floor.lineTo(fr, fb)
        floor.lineTo(fl, fb)
        floor.closeSubpath()
        painter.fillPath(floor, floor_fill)

        cols, rows = 10, 6
        for i in range(cols + 1):
            x = bl + (br - bl) * i / cols
            painter.setPen(QPen(major if i % 5 == 0 else minor, 1))
            painter.drawLine(QPointF(x, bt), QPointF(x, bb))
        for i in range(rows + 1):
            y = bt + (bb - bt) * i / rows
            painter.setPen(QPen(major if i % 3 == 0 else minor, 1))
            painter.drawLine(QPointF(bl, y), QPointF(br, y))

        floor_cols = 14
        for i in range(floor_cols + 1):
            u = i / floor_cols
            back_x = bl + (br - bl) * u
            front_x = fl + (fr - fl) * u
            painter.setPen(QPen(major if i % 2 == 0 else minor, 1))
            painter.drawLine(QPointF(back_x, bb), QPointF(front_x, fb))

        depth_rows = 14
        for i in range(depth_rows + 1):
            t = i / depth_rows
            y = bb + (fb - bb) * (t ** 1.72)
            left = bl + (fl - bl) * t
            right = br + (fr - br) * t
            painter.setPen(QPen(major if i % 3 == 0 else minor, 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))

        # Side-room structure; collision planes themselves are never drawn.
        painter.setPen(QPen(major, 1))
        painter.drawLine(QPointF(bl, bt), QPointF(fl, 0.0))
        painter.drawLine(QPointF(br, bt), QPointF(fr, 0.0))
        painter.drawLine(QPointF(bl, bb), QPointF(fl, fb))
        painter.drawLine(QPointF(br, bb), QPointF(fr, fb))

        for i in range(1, 6):
            t = i / 6.0
            top_l = QPointF(bl + (fl - bl) * t, bt * (1.0 - t))
            bottom_l = QPointF(bl + (fl - bl) * t, bb + (fb - bb) * (t ** 1.72))
            top_r = QPointF(br + (fr - br) * t, bt * (1.0 - t))
            bottom_r = QPointF(br + (fr - br) * t, bb + (fb - bb) * (t ** 1.72))
            painter.setPen(QPen(minor, 1))
            painter.drawLine(top_l, bottom_l)
            painter.drawLine(top_r, bottom_r)

    # ---------- painting ----------

    def _world_scale_at_depth(self, depth):
        """Perspective scale shared by every operator at the same depth.

        depth=0 is the back wall; depth=1 is the near edge of the floor.
        The scale is derived from the room's visible floor width so actor size,
        horizontal movement and the grid all use the same coordinate system.
        """
        depth = max(0.0, min(1.0, float(depth)))
        room = self._room_geometry()
        back_width = max(1.0, room['back_right'] - room['back_left'])
        front_width = max(1.0, room['front_right'] - room['front_left'])
        left, right = self._floor_bounds_at_depth(depth)
        width_here = max(1.0, right - left)

        # Normalize the room width to 0..1 perspective progress.
        denom = max(1.0, front_width - back_width)
        perspective = max(0.0, min(1.0, (width_here - back_width) / denom))

        # Far actors are still readable; near actors become substantially larger.
        return 0.46 + 0.54 * perspective

    def _world_actor_height(self, depth):
        """Pixel height of a standard operator at a given world depth."""
        base_near_height = min(300.0, max(230.0, self.height() * 0.34))
        return base_near_height * self._world_scale_at_depth(depth)

    def _actor_destination(self, actor):
        source = actor.get('source_rect') or QRect()
        if source.isNull() or source.height() <= 0:
            return QRectF()

        # IMPORTANT: all operators at the same depth receive exactly the same
        # visual height. Source GIF dimensions only determine aspect ratio.
        actor_h = self._world_actor_height(actor['depth'])
        actor_w = actor_h * source.width() / max(1.0, source.height())

        ground_y = self._ground_y_at_depth(actor['depth'])
        left_bound, right_bound = self._floor_bounds_at_depth(actor['depth'])
        center_x = left_bound + (right_bound - left_bound) * actor['x']

        return QRectF(
            center_x - actor_w / 2.0,
            ground_y - actor_h,
            actor_w,
            actor_h,
        )

    def _draw_conversation_bubble(self, painter, actor, conversation):
        text = conversation.get('shown_text') or conversation.get('text') or ''
        if not text:
            return
        dest = actor.get('rect') or QRectF()
        if dest.isNull():
            return

        font = painter.font()
        font.setPointSizeF(max(9.2, min(12.8, dest.height() * 0.10)))
        painter.save()
        painter.setFont(font)
        fm = QFontMetrics(font)
        max_width = min(max(180, int(self.width() * 0.22)), 320)
        text_rect = fm.boundingRect(QRect(0, 0, max_width, 600), Qt.TextWordWrap, text)
        bubble = QRectF(
            dest.center().x() - text_rect.width() / 2.0 - 14,
            max(8.0, dest.top() - text_rect.height() - 40),
            text_rect.width() + 28,
            text_rect.height() + 20,
        )
        bubble.translate(0, min(0.0, self.width() - 8 - bubble.right()))
        if bubble.left() < 8:
            bubble.moveLeft(8)
        if bubble.right() > self.width() - 8:
            bubble.moveRight(self.width() - 8)

        fill = QColor(236, 239, 244, 205)
        border = QColor(160, 168, 178, 212)
        text_color = QColor(44, 49, 58, 242)
        if ACTIVE_THEME == 'night':
            fill = QColor(224, 227, 232, 190)
            border = QColor(192, 198, 208, 212)
            text_color = QColor(24, 28, 36, 245)
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(bubble, 11, 11)

        tail_x = max(bubble.left() + 18, min(dest.center().x(), bubble.right() - 18))
        tail = QPainterPath()
        tail.moveTo(tail_x - 9, bubble.bottom() - 1)
        tail.lineTo(tail_x + 8, bubble.bottom() - 1)
        tail.lineTo(dest.center().x(), min(self.height() - 6.0, dest.top() - 8))
        tail.closeSubpath()
        painter.fillPath(tail, fill)
        painter.setPen(QPen(border, 1.0))
        painter.drawPath(tail)

        painter.setPen(text_color)
        painter.drawText(
            bubble.adjusted(14, 10, -14, -10),
            Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter,
            text,
        )
        painter.restore()

    def _draw_conversations(self, painter):
        for conversation in self._conversations:
            speaker = self._actor_by_id(conversation.get('speaker_id'))
            if speaker is None or speaker.get('conversation_id') != conversation.get('id'):
                continue
            self._draw_conversation_bubble(painter, speaker, conversation)

    def _draw_manual_speeches(self, painter):
        for actor_id, speech in self._manual_speeches.items():
            actor = self._actor_by_id(actor_id)
            if actor is None:
                continue
            self._draw_conversation_bubble(
                painter,
                actor,
                {'shown_text': speech.get('text', '')},
            )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self._draw_grid(painter)

        if not self._actors:
            painter.setPen(QColor('#53657d' if ACTIVE_THEME == 'day' else '#aeb8c8'))
            message = self._asset_error or TXT(
                'resource/gif 中未发现完整动画包',
                'No complete animation pack found under resource/gif')
            painter.drawText(self.rect().adjusted(40, 40, -40, -40),
                             Qt.AlignCenter | Qt.TextWordWrap, message)
            return

        order = sorted(range(len(self._actors)), key=lambda i: self._actors[i]['depth'])
        for index in order:
            actor = self._actors[index]
            movie = actor['movies'].get(actor['mode'])
            frame = movie.currentPixmap() if movie else QPixmap()
            if frame.isNull():
                actor['rect'] = QRectF()
                continue

            source = QRectF(actor['source_rect'])
            dest = self._actor_destination(actor)
            actor['rect'] = dest

            painter.save()
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if actor['facing'] < 0:
                painter.translate(dest.center().x() * 2, 0)
                painter.scale(-1, 1)
            painter.drawPixmap(dest, frame, source)
            painter.restore()

        # AI dialogue and player speech are two independent overlay layers.
        self._draw_conversations(painter)
        self._draw_manual_speeches(painter)

class PlaygroundPage(QWidget):
    backRequested = Signal()
    themeRequested = Signal(str)
    multiplayerRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 24)
        header = QHBoxLayout()
        self.back_btn = FloatingButton(TXT('← 返回主页', '← Back Home'))
        self.back_btn.clicked.connect(self.backRequested)
        header.addWidget(self.back_btn)
        title = QLabel(TXT('游乐场', 'Playground'))
        title.setStyleSheet('font-size:24px; font-weight:700;')
        header.addWidget(title)
        header.addStretch()

        self.multiplayer_btn = FloatingButton(TXT('联机房间', 'Multiplayer Room'))
        self.night_btn = FloatingButton(TXT('天黑了', 'Night falls'))
        self.day_btn = FloatingButton(TXT('天亮了', 'Day breaks'))
        header.addWidget(self.multiplayer_btn)
        header.addWidget(self.night_btn)
        header.addWidget(self.day_btn)
        self.multiplayer_btn.clicked.connect(self.multiplayerRequested)

        hint = QLabel(TXT('左键选择角色 · WASD 移动 · R 释放控制 · Enter 输入/发送 · Ctrl+S 社交记录 · 右键：睡觉/叫醒', 'Left-click to select · WASD move · R release control · Enter type/send · Ctrl+S social history · Right-click: sleep/wake'))
        header.addWidget(hint)
        outer.addLayout(header)

        self.canvas = PlaygroundCanvas(self)
        self.canvas.themeTransitionFinished.connect(self.themeRequested)
        self.night_btn.clicked.connect(
            lambda: self.canvas.begin_daylight_transition('night')
        )
        self.day_btn.clicked.connect(
            lambda: self.canvas.begin_daylight_transition('day')
        )
        outer.addWidget(self.canvas, 1)


class MultiplayerCanvas(QWidget):
    """Human-only multiplayer room.

    Local user is always in control. Remote users are rendered from synchronized
    network state. There is no AI, release-control command, affection, or
    automatic social system in this room.
    """

    backRequested = Signal()

    def __init__(self, actor_assets, operator_id, nickname, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(560)

        self.actor_assets = {
            actor['id'].casefold(): actor
            for actor in actor_assets
        }

        # Multiplayer renders remote actors independently from the local AI canvas.
        # Keep both move/relax movies alive so every client sees actual animated GIF
        # playback rather than a frozen frame from a stopped QMovie.
        for asset in self.actor_assets.values():
            for mode in ('move', 'relax'):
                movie = (asset.get('movies') or {}).get(mode)
                if movie is not None:
                    movie.start()

        self.local_operator = operator_id.casefold()
        self.nickname = nickname
        self.room_id = MULTIPLAYER_ROOM_ID
        self.local_player_id = None
        self.players = {}
        self._keys = set()
        self._chat_history = []
        self._chat_bubbles = {}
        self._seen_chat_ids = set()
        self._local_chat_sequence = 0
        self._state_send_accum = 0.0
        self._last_sent_state = None

        self._local = {
            'player_id': '__local__',
            'username': nickname,
            'operator': self.local_operator,
            'x': 0.50,
            'depth': 0.62,
            'facing': 1,
            'animation': 'relax',
            'display_animation': 'relax',
            'target_x': 0.50,
            'target_depth': 0.62,
            'display_x': 0.50,
            'display_depth': 0.62,
        }

        self.status = TXT('正在连接联机房间…', 'Connecting to multiplayer room…')

        self._speech_editor = QTextEdit(self)
        self._speech_editor.setAcceptRichText(False)
        self._speech_editor.setPlaceholderText(
            TXT('输入消息… 再按 Enter 发送 · Shift+Enter 换行',
                'Type a message… press Enter again to send · Shift+Enter newline')
        )
        self._speech_editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._speech_editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._speech_editor.setLineWrapMode(QTextEdit.WidgetWidth)
        self._speech_editor.setStyleSheet(
            'QTextEdit {'
            ' background: rgba(232,235,240,225);'
            ' color:#252a33;'
            ' border:1px solid rgba(150,158,170,220);'
            ' border-radius:11px;'
            ' padding:8px 10px;'
            '}'
        )
        self._speech_editor.hide()
        self._speech_editor.installEventFilter(self)
        self._speech_editor.textChanged.connect(self._resize_editor)

        self.client = MultiplayerClient(self)
        self.client.connected.connect(self._network_connected)
        self.client.disconnected.connect(self._network_disconnected)
        self.client.connectionError.connect(self._network_error)
        self.client.messageReceived.connect(self._network_message)

        self._clock = QTimer(self)
        self._clock.setInterval(16)
        self._clock.timeout.connect(self._tick)
        self._clock.start()

        self.client.connect_to(MULTIPLAYER_SERVER_URL)

    def close_room(self):
        self.client.close()

    def _asset(self, operator_id):
        return self.actor_assets.get(str(operator_id).casefold())

    def _network_connected(self):
        self.status = TXT('已连接，正在加入房间…', 'Connected, joining room…')
        self.client.send({
            'type': 'join',
            'data': {
                'room_id': self.room_id,
                'username': self.nickname,
                'operator': self.local_operator,
            }
        })
        self.update()

    def _network_disconnected(self):
        self.status = TXT('已与房间断开连接', 'Disconnected from room')
        self.update()

    def _network_error(self, message):
        self.status = TXT(
            f'联机不可用：{message}',
            f'Multiplayer unavailable: {message}'
        )
        self.update()

    def _network_message(self, packet):
        kind = packet.get('type')
        data = packet.get('data') or {}

        if kind == 'welcome':
            self.local_player_id = data.get('player_id')
            self._local['player_id'] = self.local_player_id or '__local__'
            self.players = {}
            for state in data.get('players') or []:
                self._upsert_remote(state, immediate=True)
            self.status = TXT(
                f'已加入 {data.get("room_id", self.room_id)}',
                f'Joined {data.get("room_id", self.room_id)}'
            )

        elif kind == 'player_joined':
            self._upsert_remote(data, immediate=True)

        elif kind == 'player_left':
            self.players.pop(str(data.get('player_id')), None)

        elif kind == 'state':
            self._upsert_remote(data, immediate=False)

        elif kind == 'chat':
            username = str(data.get('username', '')).strip() or TXT('匿名', 'Anonymous')
            message = str(data.get('message', '')).strip()
            player_id = str(data.get('player_id', ''))
            message_id = str(data.get('message_id', '')).strip()

            if message and (not message_id or message_id not in self._seen_chat_ids):
                if message_id:
                    self._seen_chat_ids.add(message_id)
                    if len(self._seen_chat_ids) > 1200:
                        self._seen_chat_ids = set(list(self._seen_chat_ids)[-800:])

                self._chat_history.append({
                    'username': username,
                    'message': message,
                    'message_id': message_id,
                })
                self._chat_history = self._chat_history[-600:]

                # This is keyed by the speaking player's network id, therefore
                # every connected client shows the same exact input above the
                # same remote/local avatar.
                self._chat_bubbles[player_id] = {
                    'text': message,
                    'expires_at': time.monotonic() + 2.35,
                }

        elif kind == 'error':
            self.status = TXT(
                f'服务器错误：{packet.get("message", "")}',
                f'Server error: {packet.get("message", "")}'
            )

        self.update()

    def _upsert_remote(self, data, immediate=False):
        player_id = str(data.get('player_id', ''))
        if not player_id or player_id == self.local_player_id:
            return
        if self._asset(data.get('operator')) is None:
            return

        current = self.players.get(player_id)
        x = max(0.07, min(0.93, float(data.get('x', 0.5))))
        depth = max(0.10, min(0.93, float(data.get('depth', 0.62))))
        if current is None:
            current = {
                'player_id': player_id,
                'username': str(data.get('username', '')),
                'operator': str(data.get('operator', '')).casefold(),
                'x': x,
                'depth': depth,
                'display_x': x,
                'display_depth': depth,
                'facing': -1 if int(data.get('facing', 1)) < 0 else 1,
                'animation': str(data.get('animation', 'relax')),
                'display_animation': str(data.get('animation', 'relax')),
            }
            self.players[player_id] = current
        else:
            current['username'] = str(data.get('username', current['username']))
            current['operator'] = str(data.get('operator', current['operator'])).casefold()
            current['x'] = x
            current['depth'] = depth
            current['facing'] = -1 if int(data.get('facing', 1)) < 0 else 1
            current['animation'] = str(data.get('animation', 'relax'))
            if immediate:
                current['display_x'] = x
                current['display_depth'] = depth

    def _room_geometry(self):
        w, h = float(self.width()), float(self.height())
        return {
            'front_left': w * 0.045,
            'front_right': w * 0.955,
            'floor_bottom': h * 0.965,
            'back_left': w * 0.255,
            'back_right': w * 0.745,
            'back_top': h * 0.085,
            'back_bottom': h * 0.355,
        }

    def _floor_bounds_at_depth(self, depth):
        room = self._room_geometry()
        t = max(0.0, min(1.0, float(depth)))
        left = room['back_left'] + (room['front_left'] - room['back_left']) * t
        right = room['back_right'] + (room['front_right'] - room['back_right']) * t
        return left, right

    def _ground_y_at_depth(self, depth):
        room = self._room_geometry()
        t = max(0.0, min(1.0, float(depth)))
        return (
            room['back_bottom']
            + (room['floor_bottom'] - room['back_bottom']) * (t ** 1.72)
        )

    def _world_actor_height(self, depth):
        room = self._room_geometry()
        back_width = max(1.0, room['back_right'] - room['back_left'])
        front_width = max(1.0, room['front_right'] - room['front_left'])
        left, right = self._floor_bounds_at_depth(depth)
        width_here = max(1.0, right - left)
        denom = max(1.0, front_width - back_width)
        perspective = max(0.0, min(1.0, (width_here - back_width) / denom))
        scale = 0.46 + 0.54 * perspective
        return min(300.0, max(230.0, self.height() * 0.34)) * scale

    def _destination(self, player):
        asset = self._asset(player.get('operator'))
        if asset is None:
            return QRectF()
        source = asset.get('source_rect') or QRect()
        if source.isNull() or source.height() <= 0:
            return QRectF()

        depth = player.get('display_depth', player.get('depth', 0.6))
        x = player.get('display_x', player.get('x', 0.5))
        h = self._world_actor_height(depth)
        w = h * source.width() / max(1.0, source.height())
        left, right = self._floor_bounds_at_depth(depth)
        cx = left + (right - left) * x
        gy = self._ground_y_at_depth(depth)
        return QRectF(cx - w / 2.0, gy - h, w, h)

    def _draw_grid(self, painter):
        day = ACTIVE_THEME == 'day'
        bg = QColor('#eef8ff' if day else '#06080b')
        wall_fill = QColor('#e7f4fd' if day else '#090d12')
        floor_fill = QColor('#edf8ff' if day else '#070a0e')
        major = QColor(35,145,220,125) if day else QColor(195,210,230,82)
        minor = QColor(60,165,225,58) if day else QColor(145,160,180,42)

        painter.fillRect(self.rect(), bg)
        room = self._room_geometry()
        bl, br = room['back_left'], room['back_right']
        bt, bb = room['back_top'], room['back_bottom']
        fl, fr = room['front_left'], room['front_right']
        fb = room['floor_bottom']

        painter.fillRect(QRectF(bl, bt, br - bl, bb - bt), wall_fill)

        floor = QPainterPath()
        floor.moveTo(bl, bb)
        floor.lineTo(br, bb)
        floor.lineTo(fr, fb)
        floor.lineTo(fl, fb)
        floor.closeSubpath()
        painter.fillPath(floor, floor_fill)

        for i in range(11):
            x = bl + (br - bl) * i / 10
            painter.setPen(QPen(major if i % 5 == 0 else minor, 1))
            painter.drawLine(QPointF(x, bt), QPointF(x, bb))
        for i in range(7):
            y = bt + (bb - bt) * i / 6
            painter.setPen(QPen(major if i % 3 == 0 else minor, 1))
            painter.drawLine(QPointF(bl, y), QPointF(br, y))

        for i in range(13):
            t = i / 12
            x_back = bl + (br - bl) * t
            x_front = fl + (fr - fl) * t
            painter.setPen(QPen(major if i % 3 == 0 else minor, 1))
            painter.drawLine(QPointF(x_back, bb), QPointF(x_front, fb))

        for i in range(9):
            t = i / 8
            depth = t
            left, right = self._floor_bounds_at_depth(depth)
            y = self._ground_y_at_depth(depth)
            painter.setPen(QPen(major if i % 2 == 0 else minor, 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))

    def _display_players(self):
        values = list(self.players.values())
        local = dict(self._local)
        local['display_x'] = local['x']
        local['display_depth'] = local['depth']
        values.append(local)
        return values

    def _current_frame(self, player):
        asset = self._asset(player.get('operator'))
        if asset is None:
            return None, None
        mode = (
            'move'
            if player.get('display_animation', player.get('animation')) == 'move'
            else 'relax'
        )
        movie = asset['movies'].get(mode) or asset['movies'].get('relax')
        if movie is None:
            return None, None
        return movie.currentPixmap(), asset.get('source_rect')

    def _draw_bubble(self, painter, player, text):
        text = str(text or '')
        if not text:
            return
        dest = self._destination(player)
        if dest.isNull():
            return

        font = painter.font()
        font.setPointSizeF(max(9.2, min(12.8, dest.height() * 0.10)))
        painter.save()
        painter.setFont(font)
        fm = QFontMetrics(font)
        max_width = min(max(180, int(self.width() * 0.22)), 320)
        text_rect = fm.boundingRect(
            QRect(0, 0, max_width, 600),
            Qt.TextWordWrap,
            text,
        )
        bubble = QRectF(
            dest.center().x() - text_rect.width() / 2.0 - 14,
            max(8.0, dest.top() - text_rect.height() - 40),
            text_rect.width() + 28,
            text_rect.height() + 20,
        )
        if bubble.left() < 8:
            bubble.moveLeft(8)
        if bubble.right() > self.width() - 8:
            bubble.moveRight(self.width() - 8)

        fill = QColor(236,239,244,205)
        border = QColor(160,168,178,212)
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(bubble, 11, 11)
        painter.setPen(QColor(44,49,58,242))
        painter.drawText(
            bubble.adjusted(14,10,-14,-10),
            Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter,
            text,
        )
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self._draw_grid(painter)

        players = sorted(
            self._display_players(),
            key=lambda p: p.get('display_depth', p.get('depth', 0.5)),
        )
        for player in players:
            frame, source = self._current_frame(player)
            if frame is None or frame.isNull() or source is None:
                continue
            dest = self._destination(player)
            painter.save()
            if int(player.get('facing', 1)) < 0:
                painter.translate(dest.center().x() * 2, 0)
                painter.scale(-1, 1)
            painter.drawPixmap(dest, frame, QRectF(source))
            painter.restore()

            # Username is the online identity; operator name is intentionally omitted.
            painter.save()
            painter.setPen(QColor('#596b83' if ACTIVE_THEME == 'day' else '#d7dde8'))
            painter.drawText(
                QRectF(dest.left() - 30, dest.bottom() + 3, dest.width() + 60, 22),
                Qt.AlignHCenter | Qt.AlignTop,
                str(player.get('username', '')),
            )
            painter.restore()

        now = time.monotonic()
        for player_id in list(self._chat_bubbles):
            bubble = self._chat_bubbles[player_id]
            if now >= bubble.get('expires_at', 0.0):
                self._chat_bubbles.pop(player_id, None)
                continue
            if player_id == self.local_player_id:
                player = self._local
            else:
                player = self.players.get(player_id)
            if player is not None:
                self._draw_bubble(painter, player, bubble.get('text', ''))

        painter.setPen(QColor('#596b83' if ACTIVE_THEME == 'day' else '#aab6c7'))
        painter.drawText(
            self.rect().adjusted(15, 10, -15, -10),
            Qt.AlignLeft | Qt.AlignTop,
            self.status,
        )

    def _tick(self):
        dt = self._clock.interval() / 1000.0

        moving = bool(self._keys & {Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D})
        speed = 0.115
        depth_speed = speed * 0.88
        if Qt.Key_A in self._keys:
            self._local['x'] -= speed * dt
            self._local['facing'] = -1
        if Qt.Key_D in self._keys:
            self._local['x'] += speed * dt
            self._local['facing'] = 1
        if Qt.Key_W in self._keys:
            self._local['depth'] -= depth_speed * dt
        if Qt.Key_S in self._keys:
            self._local['depth'] += depth_speed * dt

        self._local['x'] = max(0.07, min(0.93, self._local['x']))
        self._local['depth'] = max(0.10, min(0.93, self._local['depth']))
        self._local['animation'] = 'move' if moving else 'relax'
        self._local['display_animation'] = self._local['animation']

        # Smooth remote players toward the latest network snapshot.
        # Interpolation is still visual movement, so keep the move GIF playing
        # until the remote avatar has actually reached the server target.
        alpha = min(1.0, dt * 9.0)
        for player in self.players.values():
            dx = player['x'] - player['display_x']
            dd = player['depth'] - player['display_depth']
            visual_distance = math.hypot(dx, dd)

            player['display_x'] += dx * alpha
            player['display_depth'] += dd * alpha

            still_interpolating = visual_distance > 0.0025
            network_moving = player.get('animation') == 'move'
            player['display_animation'] = (
                'move' if (network_moving or still_interpolating) else 'relax'
            )

        # Send ~12.5Hz, not every 60FPS paint/update frame.
        self._state_send_accum += dt
        state = (
            round(self._local['x'], 4),
            round(self._local['depth'], 4),
            self._local['facing'],
            self._local['animation'],
        )
        if self.local_player_id and self._state_send_accum >= 0.08:
            self._state_send_accum = 0.0
            if state != self._last_sent_state:
                self.client.send({
                    'type': 'state',
                    'data': {
                        'x': self._local['x'],
                        'depth': self._local['depth'],
                        'facing': self._local['facing'],
                        'animation': self._local['animation'],
                    }
                })
                self._last_sent_state = state

        if self._speech_editor.isVisible():
            self._position_editor()
        self.update()

    def _open_editor(self):
        self._keys.clear()
        self._speech_editor.clear()
        self._speech_editor.show()
        self._resize_editor()
        self._position_editor()
        self._speech_editor.raise_()
        self._speech_editor.setFocus(Qt.ShortcutFocusReason)

    def _send_editor(self):
        # Preserve the user's actual typed text. Only leading/trailing whitespace
        # is removed; internal spaces and manual newlines are retained.
        message = self._speech_editor.toPlainText().strip()
        self._speech_editor.hide()
        self._speech_editor.clear()
        self.setFocus(Qt.ShortcutFocusReason)
        if not message:
            return

        self._local_chat_sequence += 1
        message_id = (
            f"{self.local_player_id or 'offline'}:"
            f"{int(time.time() * 1000)}:{self._local_chat_sequence}"
        )

        # Optimistic local rendering: the sender sees the message immediately,
        # rather than waiting for a network round trip.
        local_id = self.local_player_id or '__local__'
        self._seen_chat_ids.add(message_id)
        self._chat_history.append({
            'username': self.nickname,
            'message': message,
            'message_id': message_id,
        })
        self._chat_history = self._chat_history[-600:]
        self._chat_bubbles[local_id] = {
            'text': message,
            'expires_at': time.monotonic() + 2.35,
        }

        if self.local_player_id:
            self.client.send({
                'type': 'chat',
                'data': {
                    'message': message,
                    'message_id': message_id,
                }
            })
        self.update()

    def _resize_editor(self):
        if not self._speech_editor.isVisible():
            return
        fm = QFontMetrics(self._speech_editor.font())
        longest = max(self._speech_editor.toPlainText().splitlines() or [''], key=len)
        width = max(190, min(390, fm.horizontalAdvance(longest) + 46))
        document = self._speech_editor.document()
        document.setTextWidth(max(120, width - 28))
        height = max(48, min(230, int(document.size().height()) + 28))
        self._speech_editor.resize(width, height)
        self._position_editor()

    def _position_editor(self):
        if not self._speech_editor.isVisible():
            return
        dest = self._destination(self._local)
        if dest.isNull():
            return
        w, h = self._speech_editor.width(), self._speech_editor.height()
        x = max(8, min(self.width() - w - 8, int(dest.center().x() - w / 2)))
        y = max(8, min(self.height() - h - 8, int(dest.top() - h - 18)))
        self._speech_editor.move(x, y)
        self._speech_editor.raise_()

    def _show_chat_history(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(TXT('聊天记录', 'Chat History'))
        dialog.resize(660, 560)
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(18,16,18,16)
        title = QLabel(TXT('聊天记录', 'Chat History'))
        title.setStyleSheet('font-size:22px; font-weight:700;')
        outer.addWidget(title)

        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        feed = QVBoxLayout(host)
        feed.setContentsMargins(4,4,8,4)
        feed.setSpacing(10)

        if not self._chat_history:
            empty = QLabel(TXT('还没有消息。', 'No messages yet.'))
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8b939f; padding:30px;')
            feed.addWidget(empty)
        else:
            for entry in self._chat_history:
                card = QFrame()
                card.setStyleSheet(
                    'QFrame { background:rgba(128,136,148,22);'
                    ' border:1px solid rgba(128,136,148,48);'
                    ' border-radius:12px; }'
                )
                box = QVBoxLayout(card)
                box.setContentsMargins(13,10,13,11)
                user = QLabel(f"<b>{entry.get('username','')}</b>")
                user.setTextFormat(Qt.RichText)
                box.addWidget(user)
                message = QLabel(entry.get('message',''))
                message.setWordWrap(True)
                message.setTextInteractionFlags(Qt.TextSelectableByMouse)
                box.addWidget(message)
                feed.addWidget(card)

        feed.addStretch()
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)
        close = FloatingButton(TXT('关闭', 'Close'))
        close.clicked.connect(dialog.accept)
        outer.addWidget(close, 0, Qt.AlignRight)
        dialog.exec()

    def eventFilter(self, watched, event):
        if watched is self._speech_editor and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
                self._show_chat_history()
                event.accept()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                self._send_editor()
                event.accept()
                return True
            if event.key() == Qt.Key_Escape:
                self._speech_editor.hide()
                self._speech_editor.clear()
                self.setFocus(Qt.ShortcutFocusReason)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self._keys.clear()
            self._show_chat_history()
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._open_editor()
            event.accept()
            return
        if event.key() in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
            self._keys.add(event.key())
            event.accept()
            return
        # R intentionally does nothing here: the online avatar is never released.
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        self._keys.discard(event.key())
        if event.key() in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
            event.accept()
            return
        super().keyReleaseEvent(event)


class MultiplayerRoomPage(QWidget):
    backRequested = Signal()

    def __init__(self, actor_assets, operator_id, nickname, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28,20,28,24)

        header = QHBoxLayout()
        back = FloatingButton(TXT('← 返回游乐场', '← Back to Playground'))
        back.clicked.connect(self.backRequested)
        header.addWidget(back)

        title = QLabel(TXT('联机房间', 'Multiplayer Room'))
        title.setStyleSheet('font-size:24px; font-weight:700;')
        header.addWidget(title)
        header.addStretch()

        identity = QLabel(TXT(
            f'当前昵称：{nickname}',
            f'Nickname: {nickname}'
        ))
        identity.setStyleSheet('color:#8b939f;')
        header.addWidget(identity)
        outer.addLayout(header)

        hint = QLabel(TXT(
            'WASD 移动 · Enter 输入/发送 · Ctrl+S 聊天记录 · 本房间无法释放角色',
            'WASD move · Enter type/send · Ctrl+S chat history · avatar control cannot be released'
        ))
        hint.setStyleSheet('color:#8b939f;')
        outer.addWidget(hint)

        self.canvas = MultiplayerCanvas(
            actor_assets,
            operator_id,
            nickname,
            self,
        )
        outer.addWidget(self.canvas, 1)

    def close_room(self):
        self.canvas.close_room()



class MusicPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.settings_store = LocalPreferences(SETTINGS_FILE)
        saved_language = self.settings_store.value("language", "zh")
        APP_SETTINGS["language"] = saved_language if saved_language in ("zh", "en") else "zh"
        self.theme_mode = self.settings_store.value("theme", "system")
        if self.theme_mode not in ("day", "night", "system"):
            self.theme_mode = "system"
        self._localized_labels = []
        self.library_dir = MUSIC_LIBRARY_DIR
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = CACHE_DIR
        # The extraction cache is disposable. Rebuild it every time the app starts
        # so ZIP contents can never become stale after users replace archives.
        self._clear_music_cache()

        self.tracks = []
        self.local_tracks = []
        self.online_tracks = []
        self.source_kind = "online"
        self.online_limit = 55
        self._more_scheduled = False
        self.network = NetworkResults(self)
        self.network.finished.connect(self._network_done)
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="velia-artwork")
        self.media_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="velia-audio")
        self.metadata_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="velia-metadata")
        self._selection_token = 0
        self._purge_token = -1
        self._pending_save = False
        self._pending_metadata_save = None
        self._saving_count = 0
        self._cover_pending = set()
        self._credits_pending = set()
        self.album_expanded = set()
        self._cover_widgets = {}
        self.prts_cache_file = PREFERENCE_DIR / "prts_credits.json"
        self.saved_index_file = PREFERENCE_DIR / "siren_saved.json"
        try:
            self.saved_packages = json.loads(self.saved_index_file.read_text(encoding="utf-8"))
            if not isinstance(self.saved_packages, dict):
                self.saved_packages = {}
        except (OSError, ValueError):
            self.saved_packages = {}
        try:
            self.prts_cache = json.loads(self.prts_cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.prts_cache = {}
        self.search_index_file = PREFERENCE_DIR / 'prts_music_index.json'
        try:
            cached_index = json.loads(self.search_index_file.read_text(encoding='utf-8'))
            self.music_search_index = cached_index if isinstance(cached_index, dict) else {}
        except (OSError, ValueError):
            self.music_search_index = {}
        self.current_index = -1
        self.current_lyrics = []
        self.current_lyric_index = -1
        self._seeking = False

        # One shared playback-mode control: sequence -> random -> repeat-one.
        self.playback_modes = ("sequence", "random", "repeat_one")
        self.playback_mode = "sequence"

        self.library_state_path = LIBRARY_STATE_FILE
        self.library_state = self._load_library_state()
        self.active_category = "__all__"

        saved_mode = str(self.settings_store.value("playback_mode", "sequence"))
        if saved_mode in self.playback_modes:
            self.playback_mode = saved_mode

        self.desktop_lyrics = DesktopLyricsWindow(
            self, saved_position=self.settings_store.value("desktop_lyrics_position")
        )
        self.desktop_lyrics.positionChanged.connect(self._save_lyrics_position)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.82)
        self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._play_state_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)

        self._build_ui()
        self._load_customization()
        self._apply_theme()
        self._daily_date = date.today().isoformat()
        self.daily_timer = QTimer(self)
        self.daily_timer.setInterval(60 * 1000)
        self.daily_timer.timeout.connect(self._daily_midnight_check)
        self.daily_timer.start()
        QTimer.singleShot(0, self.reload_library)
        QTimer.singleShot(100, self._load_online_catalogue)
        QTimer.singleShot(150, self._refresh_music_search_index)

    def _submit(self, kind, fn, *args):
        queue = (self.media_executor if kind.startswith(("audio:", "save:")) else
                 self.metadata_executor if kind.startswith(("credits:", "catalogue", "wiki:", "music_index")) else self.executor)
        future = queue.submit(fn, *args)
        def deliver(done):
            try:
                self.network.finished.emit(kind, done.result(), None)
            except Exception as exc:
                try:
                    self.network.finished.emit(kind, None, str(exc))
                except RuntimeError:
                    pass
        future.add_done_callback(deliver)

    def _load_online_catalogue(self):
        snapshot = PREFERENCE_DIR / "siren_catalogue.json"
        try:
            cached = json.loads(snapshot.read_text(encoding="utf-8"))
            if isinstance(cached, list) and cached:
                self._install_online_catalogue(cached)
        except (OSError, ValueError):
            pass
        self._submit("catalogue", siren.catalogue)

    def _refresh_music_search_index(self):
        # Keep the cached associations usable offline and update them in one request.
        self._submit('music_index', siren.music_search_index)

    def _install_online_catalogue(self, songs):
        if not isinstance(songs, list) or not songs:
            return False
        previous = {track['cid']: track for track in self.online_tracks}
        current = (self.tracks[self.current_index]
                   if self.source_kind == 'online' and 0 <= self.current_index < len(self.tracks) else None)
        new_tracks = []
        seen = set()
        for s in songs:
            cid = str(s.get("cid", ""))
            if not cid.isdigit() or cid in seen:
                continue
            seen.add(cid)
            if cid in previous:
                track = previous[cid]
                for field in ('title', 'artist', 'album', 'album_cid', 'cover_url'):
                    if s.get(field):
                        track[field] = s[field]
                new_tracks.append(track)
                continue
            key = "siren/" + cid
            new_tracks.append({
                "online": True, "cid": cid, "path": key,
                "title": s.get("title", "—"), "artist": s.get("artist", ""),
                "album": s.get("album", ""), "album_cid": s.get("album_cid", ""),
                "cover_url": s.get("cover_url", ""), "cover_bytes": None,
                "cover_desc": TXT("塞壬唱片", "Monster Siren"), "track": "", "year": "",
                "duration_ms": 0, "lyrics": [],
                "kind": "ost" if re.search(r"OST\s*$", s.get("album", ""), re.I) else "other",
                "saved_locally": Path(self.saved_packages.get(cid, "")).is_file(),
                "downloaded": False,
                "favorite": bool(self.library_state.get("favorites", {}).get(key)),
                "category": str(self.library_state.get("categories", {}).get(key, "")),
            })
        if not new_tracks:
            return False
        # A partial catalogue response must never delete cached songs. Keep the
        # existing order, append new IDs, and reuse track objects held by playback.
        self.online_tracks = list(previous.values()) + [track for track in new_tracks if track['cid'] not in previous]
        if self.source_kind == "online":
            self.tracks = self.online_tracks
            self.current_index = (self.tracks.index(current) if current in self.tracks else -1)
            self._rebuild_playlist()
            self.scan_status.setText(TXT(f"塞壬唱片 · {len(self.tracks)} 首", f"Monster Siren · {len(self.tracks)} tracks"))
            if self.current_index < 0 and self.tracks:
                self.select_track(0, autoplay=False)
        self._refresh_daily_song()
        return True

    def _network_done(self, kind, value, error):
        if kind == 'music_index':
            if not error and isinstance(value, dict) and value:
                self.music_search_index = value
                _write_json(self.search_index_file, value)
                if self.source_kind == 'online' and self.search_box.text().strip():
                    self._rebuild_playlist()
                self._refresh_daily_song()
            return
        if kind.startswith('daily_intro:'):
            cid = kind.split(':', 1)[1]
            if not error and isinstance(value, dict):
                track = next((t for t in self.online_tracks if t['cid'] == cid), None)
                if track:
                    track['intro'] = value.get('intro', '')
                    if cid == getattr(self, 'home_daily_cid', None):
                        self._refresh_daily_song()
            return
        if kind == "catalogue":
            if error:
                if not self.online_tracks:
                    self.scan_status.setText(TXT("在线曲库暂不可用 · 请检查网络", "Online catalogue unavailable · check connection"))
                return
            if not isinstance(value, list) or not value:
                return
            if self._install_online_catalogue(value):
                # Cache the union, so an incomplete response cannot replace a
                # complete catalogue on the next startup.
                catalogue = [{key: t.get(key, '') for key in
                              ('cid', 'title', 'artist', 'album', 'album_cid', 'cover_url')}
                             for t in self.online_tracks]
                _write_json(PREFERENCE_DIR / 'siren_catalogue.json', catalogue)
        elif kind.startswith("cover:"):
            cid = kind.split(":", 1)[1]
            self._cover_pending.discard(cid)
            if not error and value:
                ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
                (ARTWORK_DIR / (cid + ".img")).write_bytes(value)
                for track in self.online_tracks:
                    if track["cid"] == cid:
                        track["cover_bytes"] = value
                        if self.source_kind == "online" and self.current_index >= 0 and self.tracks[self.current_index] is track:
                            self.hero_cover.setPixmap(_rounded_cover(self._pixmap_for_track(track, 112), 112, 17))
                        break
                for label, size, radius in self._cover_widgets.get(cid, []):
                    label.setPixmap(_rounded_cover(self._pixmap_for_track(track, size), size, radius))
                if cid == getattr(self, 'home_daily_cid', None):
                    self.home_daily_cover.setText('')
                    self.home_daily_cover.setPixmap(_rounded_cover(self._pixmap_for_track(track, 144), 144, 17))
        elif kind.startswith("audio:"):
            _, token, cid = kind.split(":")
            if int(token) <= self._purge_token:
                if value and value[0].exists():
                    value[0].unlink(missing_ok=True)
                    value[0].with_suffix(".lrc").unlink(missing_ok=True)
                return
            if str(self._selection_token) != token or self.current_index < 0 or self.tracks[self.current_index].get("cid") != cid:
                return
            self.play_btn.setEnabled(True)
            if error:
                self.scan_status.setText(TXT("下载失败：", "Download failed: ") + error[:120])
                return
            track = self.tracks[self.current_index]
            track["downloaded"] = True
            for row in self.playlist_host.findChildren(MusicTrackRow):
                if row.track is track:
                    row.sync_download_state()
            path, detail, album, lyrics = value
            track["lyrics"] = lyrics
            track["track"] = str(next((i for i, x in enumerate(album.get("songs", [])) if str(x.get("cid")) == cid), -1) + 1) if album else ""
            track["intro"] = (album or {}).get("intro", "")
            if not track["intro"]:
                self._submit("wiki:" + token, siren.wiki_fallback, track["title"])
            self.current_lyrics = lyrics
            self._update_lyric_roller(-1)
            self._show_track_details(track)
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.player.play()
            self.scan_status.setText(TXT("正在播放 · 临时缓存", "Playing · temporary cache"))
            if self._pending_save:
                self._save_online_track()
        elif kind.startswith("wiki:"):
            _, token = kind.split(":")
            if str(self._selection_token) == token and self.current_index >= 0 and value:
                track = self.tracks[self.current_index]
                if track.get("online") and not track.get("intro"):
                    track["intro"] = value[0]["summary"]
                    track["intro_source"] = value[0]["url"]
                    self._show_track_details(track)
        elif kind.startswith("credits:"):
            key = kind.split(":", 1)[1]
            self._credits_pending.discard(key)
            if not error and isinstance(value, dict):
                self.prts_cache[key] = value
                try:
                    _write_json(self.prts_cache_file, self.prts_cache)
                except OSError:
                    pass
            for track in (*self.online_tracks, *self.local_tracks):
                if self._credits_key(track) == key:
                    self._apply_credits(track, value if isinstance(value, dict) else {})
            if key == getattr(self, 'home_daily_cid', None):
                self._refresh_daily_song()
            if self._pending_metadata_save == key:
                self._pending_metadata_save = None
                if 0 <= self.current_index < len(self.tracks) and self._credits_key(self.tracks[self.current_index]) == key:
                    self._save_online_track()
        elif kind.startswith("save:"):
            self._saving_count = max(0, self._saving_count - 1)
            self.clear_cache_btn.setEnabled(self._saving_count == 0)
            self.refresh_btn.setEnabled(self._saving_count == 0)
            self.save_online_btn.setEnabled(True)
            if not error and value:
                cid = kind.split(":", 1)[1]
                self.saved_packages[cid] = str(value)
                _write_json(self.saved_index_file, self.saved_packages)
                for track in self.online_tracks:
                    if track["cid"] == cid:
                        track["saved_locally"] = True
                        break
                for row in self.playlist_host.findChildren(MusicTrackRow):
                    if row.track.get("cid") == cid:
                        row.sync_download_state()
            self.scan_status.setText((TXT("保存失败：", "Save failed: ") + error[:150]) if error else
                                     TXT(f"已保存：{value.name}", f"Saved: {value.name}"))


    def _apply_credits(self, track, credits):
        if credits:
            track["year"] = credits.get("year") or track.get("year", "")
            prior_composer = track.get('composer', '')
            if prior_composer.strip().casefold() in ('塞壬唱片-msr', '塞壬唱片', 'monster siren records'):
                prior_composer = ''
            track["composer"] = credits.get("composer") or prior_composer
            for field in ('performer', 'lyricist', 'arranger', 'character', 'event', 'credited_artist'):
                track[field] = credits.get(field, '')
            track["credits_url"] = credits.get("url", "")
            track["kind"] = credits.get("kind") or track.get("kind", "other")
            artist_parts = [part.strip() for part in re.split(r'[,，、]', track.get('artist') or '')
                            if part.strip() and part.strip().casefold() not in
                            ('塞壬唱片-msr', '塞壬唱片', 'monster siren records')]
            track['artist'] = ', '.join(artist_parts)
            if not track.get('artist') or track['artist'].strip().casefold() in (
                    '塞壬唱片-msr', '塞壬唱片', 'monster siren records'):
                track['artist'] = (credits.get('credited_artist') or credits.get('performer')
                                   or credits.get('composer') or '')
            if self.current_index >= 0 and self.tracks[self.current_index] is track:
                self.detail_values['Artist'].setText(track.get('artist') or '—')
                self.detail_values['Year'].setText(track.get('year') or '—')
                self.hero_artist.setText(track.get('artist') or '')
                self._show_track_details(track)

    def _credits_key(self, track):
        if track.get('online'):
            return track['cid']
        return 'local_' + hashlib.sha1(self._track_key(track['path']).encode('utf-8')).hexdigest()[:16]

    def _request_credits(self, track):
        key = self._credits_key(track)
        cached = self.prts_cache.get(key, {})
        if cached:
            self._apply_credits(track, cached)
        if cached.get('metadata_version', 0) < 3 and key not in self._credits_pending:
            self._credits_pending.add(key)
            self._submit('credits:' + key, siren.prts_credits, track['title'], track['album'])

    def _online_job(self, track):
        cid = track["cid"]
        detail = siren.song_detail(cid)
        album = {}
        if track.get("album_cid"):
            try:
                album = siren.album_detail(track["album_cid"])
            except Exception:
                pass
        url = detail.get("sourceUrl", "")
        suffix = Path(__import__("urllib.parse", fromlist=["urlparse"]).urlparse(url).path).suffix.lower()
        if suffix not in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
            suffix = ".wav"
        path = CACHE_DIR / "siren" / "audio" / (cid + suffix)
        siren.download(url, path)
        lyrics = []
        if detail.get("lyricUrl"):
            try:
                raw = siren.fetch_bytes(detail["lyricUrl"], 2 * 1024 * 1024)
                lrc = path.with_suffix(".lrc")
                lrc.write_bytes(raw)
                lyrics = _parse_lrc(lrc)
            except Exception:
                pass
        return path, detail, album, lyrics

    def _switch_source(self, index):
        self._selection_token += 1
        self.player.stop()
        self.player.setSource(QUrl())
        self.source_kind = self.source_combo.itemData(index)
        self.tracks = self.online_tracks if self.source_kind == "online" else self.local_tracks
        self.current_index = -1
        self.online_limit = 55
        self._rebuild_playlist()
        self._refresh_daily_song()
        self.scan_status.setText(TXT(f"已列出 {len(self.tracks)} 首歌曲", f"{len(self.tracks)} tracks available"))
        if self.tracks:
            self.select_track(0, autoplay=False)
        else:
            self.hero_title.setText(TXT("暂无歌曲", "No tracks"))
            self.hero_cover.clear()
            self._sync_hero_favorite()

    def _save_online_track(self):
        if self.current_index < 0 or not self.tracks[self.current_index].get("online"):
            return
        track = self.tracks[self.current_index]
        source = next((p for p in (CACHE_DIR / "siren" / "audio").glob(track["cid"] + ".*") if p.suffix != ".lrc"), None)
        if source is None:
            if self.play_btn.isEnabled():
                self.select_track(self.current_index)
            self._pending_save = True
            self.scan_status.setText(TXT("下载完成后保存到本地…", "Saving after download…"))
            return
        if track["cid"] in self._credits_pending:
            self._pending_metadata_save = track["cid"]
            self.scan_status.setText(TXT("等待 PRTS 发行资料…", "Waiting for PRTS release details…"))
            return
        self._pending_save = False
        self._saving_count += 1
        self.save_online_btn.setEnabled(False)
        self.clear_cache_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.scan_status.setText(TXT("后台整理并保存标准歌曲包…", "Creating song package in background…"))
        self._submit("save:" + track["cid"], siren.save_ep_zip, dict(track), source,
                     source.with_suffix(".lrc"), MUSIC_LIBRARY_DIR / "Monster Siren",
                     bool(self.compress_check.isChecked()),
                     int(self.bitrate_combo.currentData()), int(self.sample_combo.currentData()))

    def _show_track_details(self, track):
        for key, name in (("Album", "album"), ("Track", "track"), ("Year", "year"), ("Cover", "cover_desc")):
            self.detail_values[key].setText(str(track.get(name) or "—"))
        intro = track.get("intro") or ""
        self.album_intro.setText(re.sub(r"<[^>]+>", "", intro)[:500])
        self.album_intro.setToolTip(track.get("intro_source") or
                                    (siren.ROOT + "/" if track.get("online") else ""))
        self.album_intro.setVisible(bool(intro))
        kind = track.get('kind', 'other')
        character = track.get('character', '').strip(' 〖〗')
        event = track.get('event', '').strip(' 〖〗')
        album = track.get('album', '').strip()
        if kind == 'ep' and character:
            context = TXT(f'{character} 的 EP', f'{character} · Operator EP')
        elif kind == 'ost' and event:
            context = TXT(f'{event} 活动 OST', f'{event} · Event OST')
        elif event:
            context = TXT(f'相关活动：{event}', f'Related event: {event}')
        elif kind in ('ep', 'ost', 'op', 'ed') and album:
            context = f'{kind.upper()} · {album}'
        else:
            context = ''
        self.context_label.setText(context)
        self.context_label.setToolTip(track.get('credits_url', ''))
        self.context_label.setVisible(bool(context))
        parts = []
        for field, zh, en in (('composer', '作曲', 'Composer'),
                              ('lyricist', '作词', 'Lyrics'),
                              ('arranger', '编曲', 'Arrangement'),
                              ('performer', '演唱／演奏', 'Performed by')):
            if track.get(field):
                parts.append(TXT(zh, en) + '：' + track[field])
        self.credits_label.setText('    ·    '.join(parts))
        self.credits_label.setToolTip(track.get("credits_url", ""))
        self.credits_label.setVisible(bool(parts))

    def _localized_label(self, zh, en):
        label = QLabel(TXT(zh, en))
        self._localized_labels.append((label, zh, en))
        return label

    def _language_changed(self, index):
        language = self.language_combo.itemData(index)
        if language not in ("zh", "en") or language == APP_SETTINGS["language"]:
            return
        APP_SETTINGS["language"] = language
        self.settings_store.setValue("language", language)
        self.home_btn.setText(TXT('主页', 'Home'))
        self.home_msr_title.setText(TXT('前往明日方舟音乐库', 'Explore Monster Siren'))
        self.home_msr_note.setText(TXT('塞壬唱片 · 在线曲目与本地音乐', 'Monster Siren · online and local music'))
        self.home_msr_button.setText(TXT('进入音乐库  ↗', 'Enter Library  ↗'))
        self.home_daily_heading.setText(TXT('今日舟乐推荐', 'Today’s Arknights Track'))
        self.home_daily_play.setText(TXT('播放今日推荐  ▶', 'Play Today’s Pick  ▶'))
        self.home_news_heading.setText(TXT('每日药闻', 'Daily Pharmaceutical News'))
        self.home_liked_heading.setText(TXT('22N7O 喜欢听的', '22N7O’s Favorites'))
        self.home_playground_heading.setText(TXT('游乐场', 'Playground'))
        self.home_playground_note.setText(TXT('请君入园·依律镇抚', 'Ingredere hortum · lege compesce'))
        self.home_playground_button.setText(TXT('进入游乐场  ↗', 'Enter Playground  ↗'))
        if hasattr(self, 'playground_page'):
            self.playground_page.back_btn.setText(TXT('← 返回主页', '← Back Home'))
            self.playground_page.multiplayer_btn.setText(
                TXT('联机房间', 'Multiplayer Room')
            )
        self.home_guide.setText(TXT('打开使用指南', 'Open User Guide'))
        self._refresh_daily_song()
        for label, zh, en in self._localized_labels:
            label.setText(TXT(zh, en))
        detail_names = {
            "Title": ("歌名", "Title"),
            "Artist": ("艺术家", "Artist"),
            "Album": ("专辑名", "Album"),
            "Track": ("曲目编号", "Track"),
            "Year": ("年份", "Year"),
            "Cover": ("封面", "Cover"),
        }
        for key, (zh, en) in detail_names.items():
            self.detail_labels[key].setText(TXT(zh, en) + ":")
        self.desktop_btn.setText(TXT("桌面歌词", "Desktop Lyrics"))
        self.refresh_btn.setText(TXT("刷新音乐", "Refresh"))
        self.settings_btn.setText(TXT("返回播放器", "Back to Player") if self.settings_btn.isChecked()
                                  else TXT("音乐设置", "Music Settings"))
        self.system_nav.setText(TXT("系统设置", "System Settings"))
        self.customize_nav.setText(TXT("歌词自定义", "Lyrics Customization"))
        self.position_btn.setText(TXT("保存歌词位置", "Save Lyric Position") if self.desktop_lyrics.editing
                                  else TXT("拖动歌词位置", "Move Lyric Position"))
        self.reset_position_btn.setText(TXT("恢复默认位置", "Reset Default Position"))
        for row, (zh, en) in enumerate((("白昼", "Day"), ("黑夜", "Night"),
                                        ("跟随系统主题", "Follow System Theme"))):
            self.theme_combo.setItemText(row, TXT(zh, en))
        self.open_folder_btn.setText(TXT("打开音乐文件夹", "Open Music Folder"))
        self.open_preference_btn.setText(TXT("打开偏好设置文件夹", "Open Preferences Folder"))
        self.source_combo.setItemText(0, TXT("塞壬唱片 · 在线", "Monster Siren · Online"))
        self.source_combo.setItemText(1, TXT("本地音乐", "Local Music"))
        self.search_box.setPlaceholderText(TXT("搜索歌曲、干员 EP 或活动 OST…", "Search tracks, operator EPs or event OSTs…"))
        self.save_online_btn.setText(TXT("保存到本地", "Save Locally"))
        self.compress_check.setText(TXT("后台转为 MP3 并压缩", "Convert to MP3 in background"))
        self.clear_cache_btn.setText(TXT("一键清理临时歌曲缓存", "Clear Temporary Song Cache"))
        self.add_category_btn.setToolTip(TXT("新建分类", "New Category"))
        self.batch_add_btn.setToolTip(TXT("批量加入歌曲", "Batch Add Tracks"))
        self.delete_category_btn.setToolTip(TXT("删除当前歌单 / 分类", "Delete Current Playlist / Category"))
        self.hero_favorite_btn.setToolTip(TXT("收藏", "Favorite"))
        self.text_color_btn.setText(TXT("色轮", "Color Wheel"))
        self.bg_color_btn.setText(TXT("背景色轮", "Background Color"))
        self.rainbow_check.setText(TXT("彩虹模式", "Rainbow"))
        self.bg_enable.setText(TXT("启用背景", "Enable Background"))
        for row, (zh, en) in enumerate((("常规", "Regular"), ("中等", "Medium"),
                                        ("粗体", "Bold"), ("特粗", "Extra Bold"))):
            self.weight_combo.setItemText(row, TXT(zh, en))
        for track in self.tracks:
            if track["artist"] in ("未知艺术家", "Unknown Artist"):
                track["artist"] = TXT("未知艺术家", "Unknown Artist")
            if track["cover_desc"] in ("无", "None", "内嵌封面", "Embedded"):
                is_embedded = track["cover_desc"] in ("内嵌封面", "Embedded")
                track["cover_desc"] = TXT("内嵌封面", "Embedded") if is_embedded else TXT("无", "None")
        self._rebuild_category_combo()
        self._rebuild_playlist()
        self._sync_playback_mode_button()
        self.scan_status.setText(TXT(f"已读取 {len(self.tracks)} 首歌曲",
                                     f"{len(self.tracks)} tracks loaded"))
        if self.current_index >= 0:
            track = self.tracks[self.current_index]
            self.hero_artist.setText(track["artist"])
            self.detail_values["Artist"].setText(track["artist"])
            self.detail_values["Cover"].setText(track["cover_desc"])
        else:
            self.hero_title.setText(TXT("未选择歌曲", "No track selected"))
        self._update_lyric_roller(self.current_lyric_index)
        self.window().setWindowTitle(TXT("Velia音乐播放器", "Velia Music Player"))
        if self.desktop_lyrics.editing:
            self.desktop_lyrics.set_position_editing(False)
            self.desktop_lyrics.set_position_editing(True)

    def _effective_theme(self):
        if self.theme_mode != "system":
            return self.theme_mode
        hints = QApplication.instance().styleHints()
        return "day" if hints.colorScheme() == Qt.ColorScheme.Light else "night"

    def _apply_theme(self):
        global ACTIVE_THEME
        ACTIVE_THEME = self._effective_theme()
        _STAR_CACHE.clear()
        if hasattr(self, 'playground_page'):
            QTimer.singleShot(0, self.playground_page.canvas.sync_daylight_to_theme)
        QApplication.instance().setStyleSheet(theme_css(STYLE))
        QTimer.singleShot(0, self._update_playground_art)
        self._rebuild_playlist()
        if 0 <= self.current_index < len(self.tracks):
            track = self.tracks[self.current_index]
            self.hero_cover.setPixmap(_rounded_cover(self._pixmap_for_track(track, 112), 112, 17))
        self._update_lyric_roller(self.current_lyric_index)
        for widget in [self, *self.findChildren(QWidget)]:
            recolor_widget(widget)
            if isinstance(widget, (RoundedStarButton, FolderAddButton,
                                   VisibleComboBox, VisibleCheckBox, VisibleFontComboBox)):
                widget.update()
        day = ACTIVE_THEME == 'day'
        for card in self.findChildren(QFrame, 'homePanel'):
            card.setStyleSheet('QFrame#homePanel { background:%s; border:1px solid %s; border-radius:24px; } '
                               'QFrame#homePanel:hover { border-color:%s; }' %
                               (('#ffffff', '#c7d2df', '#7890b3') if day else
                                ('#15181f', '#303742', '#7790b9')))
        for banner in self.findChildren(QFrame, 'homeBanner'):
            banner.setStyleSheet('QFrame#homeBanner { background:%s; border:1px solid %s; border-radius:27px; } '
                                 'QFrame#homeBanner:hover { border-color:%s; }' %
                                 (('#e6edf6', '#bac8d9', '#7890b3') if day else
                                  ('#202836', '#475369', '#92a8ce')))
        self.home_msr_note.setStyleSheet('color:%s; background:transparent; border:0;' %
                                         ('#455a74' if day else '#afb9cc'))
        for holder in self.findChildren(QLabel, 'homeThumbnail'):
            holder.setStyleSheet('QLabel#homeThumbnail { background:%s; border:1px dashed %s; border-radius:17px; color:%s; }' %
                                 (('#d8e2ef', '#8699af', '#455a74') if day else
                                  ('#303a4d', '#69758a', '#c6cfdb')))
        for holder in self.findChildren(QLabel, 'homeBackground'):
            holder.setStyleSheet('QLabel#homeBackground { background:%s; color:%s; border:0; border-radius:19px; }' %
                                 (('#d7e1ee', '#657994') if day else ('#273244', '#a2b1cb')))
        for holder in self.findChildren(QLabel, 'homeLikedCover'):
            holder.setStyleSheet('QLabel#homeLikedCover { background:%s; border:1px dashed %s; border-radius:12px; }' %
                                 (('#d8e2ef', '#8699af') if day else ('#303a4d', '#69758a')))
        self.home_daily_cover.setStyleSheet('background:%s; border:1px dashed %s; border-radius:17px;' %
                                            (('#d8e2ef', '#8699af') if day else ('#303a4d', '#69758a')))
        self.setStyleSheet("QWidget#root { background: #f3f5f8; }" if ACTIVE_THEME == "day"
                           else "QWidget#root { background: #070707; }")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f3f5f8" if ACTIVE_THEME == "day" else "#070707"))

    def _theme_changed(self, index):
        theme = self.theme_combo.itemData(index)
        if theme not in ("day", "night", "system"):
            return
        self.theme_mode = theme
        self.settings_store.setValue("theme", theme)
        self._apply_theme()

    def _system_theme_changed(self, _scheme):
        if self.theme_mode == "system":
            self._apply_theme()

    def _select_settings_page(self, index):
        self.settings_content_stack.setCurrentIndex(index)
        self.system_nav.setChecked(index == 0)
        self.customize_nav.setChecked(index == 1)

    def _track_key(self, path):
        if isinstance(path, str) and path.startswith("siren/"):
            return path
        resolved = Path(path).resolve()
        for extracted, archive_key in getattr(self, "archive_roots", {}).items():
            try:
                inside = resolved.relative_to(extracted)
                return "archive:" + archive_key + "!/" + inside.as_posix()
            except ValueError:
                continue
        try:
            return str(resolved.relative_to(self.library_dir.resolve())).replace("\\", "/")
        except Exception:
            return str(resolved).replace("\\", "/")

    def _load_library_state(self):
        try:
            if self.library_state_path.exists():
                raw = json.loads(self.library_state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw.setdefault("favorites", {})
                    raw.setdefault("categories", {})
                    raw.setdefault("custom_categories", [])
                    return raw
        except Exception:
            pass
        return {"favorites": {}, "categories": {}, "custom_categories": []}

    def _save_library_state(self):
        try:
            _write_json(self.library_state_path, self.library_state)
        except Exception:
            pass

    def _all_categories(self):
        builtins = [
            ("__all__", TXT("全部歌曲", "All Songs")),
            ("__favorites__", TXT("收藏", "Favorites")),
            ("__uncategorized__", TXT("未分类", "Uncategorized")),
        ]
        custom = sorted(
            {str(x).strip() for x in self.library_state.get("custom_categories", []) if str(x).strip()},
            key=str.casefold
        )
        return builtins, custom

    def _rebuild_category_combo(self):
        current = self.active_category
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        builtins, custom = self._all_categories()
        for key, label in builtins:
            self.category_combo.addItem(label, key)
        for name in custom:
            self.category_combo.addItem(name, name)
        ix = self.category_combo.findData(current)
        if ix < 0:
            ix = 0
            self.active_category = "__all__"
        self.category_combo.setCurrentIndex(ix)
        self.category_combo.blockSignals(False)
        if hasattr(self, "batch_add_btn"):
            self._sync_batch_add_button()

    def _category_changed(self, _index):
        self.active_category = self.category_combo.currentData() or "__all__"
        self._sync_batch_add_button()
        self._rebuild_playlist()

    def _sync_batch_add_button(self):
        custom_categories = {
            str(x).strip()
            for x in self.library_state.get("custom_categories", [])
            if str(x).strip()
        }
        is_custom = self.active_category in custom_categories
        self.batch_add_btn.setVisible(is_custom)

        # Built-in views (All Songs / Favorites / Uncategorized) are protected.
        # Only user-created playlists/categories may be deleted.
        if hasattr(self, "delete_category_btn"):
            self.delete_category_btn.setVisible(is_custom)
            self.delete_category_btn.setEnabled(is_custom)

    def _batch_add_to_active_category(self):
        """Assign multiple tracks to the currently selected custom category."""
        custom_categories = {
            str(x).strip()
            for x in self.library_state.get("custom_categories", [])
            if str(x).strip()
        }
        category = self.active_category
        if category not in custom_categories:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(TXT(
            f"批量加入 · {category}",
            f"Batch Add · {category}"
        ))
        dialog.resize(520, 620)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel(TXT(
            f"选择要加入「{category}」的歌曲",
            f"Select tracks to add to “{category}”"
        ))
        title.setStyleSheet("font-size:16px; font-weight:700;")
        layout.addWidget(title)

        hint = QLabel(TXT(
            "可同时选择多首歌曲。已经属于该分类的歌曲会预先选中。",
            "Select multiple tracks at once. Tracks already in this category are pre-selected."
        ))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777; font-size:11px;")
        layout.addWidget(hint)

        track_list = QListWidget()
        track_list.setSelectionMode(QAbstractItemView.MultiSelection)
        track_list.setAlternatingRowColors(False)

        for i, track in enumerate(self.tracks):
            title_text = track.get("title") or Path(track["path"]).stem
            artist_text = track.get("artist") or TXT("未知艺术家", "Unknown Artist")
            item = QListWidgetItem(f"{title_text}   —   {artist_text}")
            item.setData(Qt.UserRole, i)
            track_list.addItem(item)
            if track.get("category", "") == category:
                item.setSelected(True)

        layout.addWidget(track_list, 1)

        select_row = QHBoxLayout()
        select_all = FloatingButton(TXT("全选", "Select All"))
        select_none = FloatingButton(TXT("清空选择", "Clear Selection"))
        select_all.setObjectName("chipButton")
        select_none.setObjectName("chipButton")
        select_all.clicked.connect(lambda: [
            track_list.item(i).setSelected(True)
            for i in range(track_list.count())
        ])
        select_none.clicked.connect(lambda: track_list.clearSelection())
        select_row.addWidget(select_all)
        select_row.addWidget(select_none)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(TXT("应用", "Apply"))
        buttons.button(QDialogButtonBox.Cancel).setText(TXT("取消", "Cancel"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        selected_indices = {
            int(item.data(Qt.UserRole))
            for item in track_list.selectedItems()
        }

        categories = self.library_state.setdefault("categories", {})

        # Batch editor semantics:
        # - selected tracks become members of the current category;
        # - tracks that were in this category but are now unselected are removed
        #   from this category and return to Uncategorized.
        for i, track in enumerate(self.tracks):
            key = self._track_key(track["path"])
            current = track.get("category", "")

            if i in selected_indices:
                categories[key] = category
                track["category"] = category
            elif current == category:
                categories.pop(key, None)
                track["category"] = ""

        self._save_library_state()
        self._rebuild_playlist()

    def _create_category(self):
        name, ok = QInputDialog.getText(
            self,
            TXT("新建分类", "New Category"),
            TXT("分类名称：", "Category name:")
        )
        name = str(name).strip()
        if not ok or not name:
            return
        reserved = {
            TXT("全部歌曲", "All Songs").casefold(),
            TXT("收藏", "Favorites").casefold(),
            TXT("未分类", "Uncategorized").casefold(),
        }
        if name.casefold() in reserved:
            return
        cats = self.library_state.setdefault("custom_categories", [])
        if name not in cats:
            cats.append(name)
            self._save_library_state()
        self.active_category = name
        self._rebuild_category_combo()
        self._rebuild_playlist()

    def _delete_active_category(self):
        custom_categories = [
            str(x).strip()
            for x in self.library_state.get("custom_categories", [])
            if str(x).strip()
        ]
        category = str(self.active_category or "").strip()
        if category not in custom_categories:
            return

        member_count = sum(
            1
            for track in self.tracks
            if str(track.get("category", "")) == category
        )

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(TXT("删除歌单", "Delete Playlist"))
        box.setText(TXT(
            f"确定删除歌单「{category}」吗？",
            f"Delete playlist “{category}”?"
        ))
        box.setInformativeText(TXT(
            f"其中的 {member_count} 首歌曲不会从电脑中删除，只会回到“未分类”。此操作无法撤销。",
            f"The {member_count} track(s) will not be deleted from your computer; they will return to Uncategorized. This cannot be undone."
        ))

        delete_btn = box.addButton(
            TXT("删除", "Delete"),
            QMessageBox.DestructiveRole
        )
        box.addButton(
            TXT("取消", "Cancel"),
            QMessageBox.RejectRole
        )
        box.exec()

        if box.clickedButton() is not delete_btn:
            return

        # Remove the category definition.
        self.library_state["custom_categories"] = [
            name for name in custom_categories
            if name != category
        ]

        # Remove all track -> category assignments pointing at it.
        categories = self.library_state.setdefault("categories", {})
        for key in list(categories.keys()):
            if str(categories.get(key, "")) == category:
                categories.pop(key, None)

        # Keep the in-memory track objects in sync immediately.
        for track in self.tracks:
            if str(track.get("category", "")) == category:
                track["category"] = ""

        self.active_category = "__all__"
        self._save_library_state()
        self._rebuild_category_combo()
        self._rebuild_playlist()

    def _set_track_favorite(self, index, favorite):
        if not (0 <= index < len(self.tracks)):
            return
        track = self.tracks[index]
        key = self._track_key(track["path"])
        favs = self.library_state.setdefault("favorites", {})
        if favorite:
            favs[key] = True
        else:
            favs.pop(key, None)
        track["favorite"] = bool(favorite)
        self._save_library_state()
        if self.active_category == "__favorites__":
            self._rebuild_playlist()

    def _set_track_category_dialog(self, index):
        if not (0 <= index < len(self.tracks)):
            return

        track = self.tracks[index]
        current = track.get("category", "")
        custom = sorted(
            {str(x).strip() for x in self.library_state.get("custom_categories", []) if str(x).strip()},
            key=str.casefold
        )

        choices = [TXT("未分类", "Uncategorized")] + custom + [TXT("＋ 新建分类…", "＋ New Category…")]
        default_index = 0
        if current in custom:
            default_index = custom.index(current) + 1

        choice, ok = QInputDialog.getItem(
            self,
            TXT("歌曲分类", "Track Category"),
            TXT("将这首歌分类到：", "Place this track in:"),
            choices,
            default_index,
            False,
        )
        if not ok:
            return

        new_name_label = TXT("＋ 新建分类…", "＋ New Category…")
        uncategorized_label = TXT("未分类", "Uncategorized")

        if choice == new_name_label:
            name, created = QInputDialog.getText(
                self,
                TXT("新建分类", "New Category"),
                TXT("分类名称：", "Category name:")
            )
            if not created:
                return
            choice = str(name).strip()
            if not choice:
                return
            cats = self.library_state.setdefault("custom_categories", [])
            if choice not in cats:
                cats.append(choice)
        elif choice == uncategorized_label:
            choice = ""

        key = self._track_key(track["path"])
        categories = self.library_state.setdefault("categories", {})
        if choice:
            categories[key] = choice
        else:
            categories.pop(key, None)

        track["category"] = choice
        self._save_library_state()
        self._rebuild_category_combo()
        self._rebuild_playlist()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(12)

        top = QHBoxLayout()
        title = self._localized_label("Velia音乐播放器", "Velia Music Player")
        title.setObjectName("pageTitle")
        top.addWidget(title)
        top.addStretch(1)

        self.desktop_btn = FloatingButton(TXT("桌面歌词", "Desktop Lyrics"))
        self.desktop_btn.setCheckable(True)
        self.desktop_btn.setObjectName("chipButton")
        self.desktop_btn.toggled.connect(self._toggle_desktop_lyrics)
        top.addWidget(self.desktop_btn)

        self.refresh_btn = FloatingButton(TXT("刷新音乐", "Refresh"))
        self.refresh_btn.setObjectName("chipButton")
        self.refresh_btn.clicked.connect(self._refresh_music_library)
        top.addWidget(self.refresh_btn)

        self.settings_btn = FloatingButton(TXT("音乐设置", "Music Settings"))
        self.settings_btn.setCheckable(True)
        self.settings_btn.setObjectName("chipButton")
        self.settings_btn.toggled.connect(self._toggle_settings)
        top.addWidget(self.settings_btn)
        root.addLayout(top)

        self.main_stack = QStackedWidget()
        root.addWidget(self.main_stack, 1)
        self.main_stack.addWidget(self._build_player_view())
        self.main_stack.addWidget(self._build_settings_view())
        self.main_stack.addWidget(self._build_home_view())
        self.main_stack.addWidget(self._build_playground_view())
        self._page_before_settings = 2
        self.home_btn = FloatingButton(TXT("主页", "Home"))
        self.home_btn.clicked.connect(lambda: self.main_stack.setCurrentIndex(2))
        top.insertWidget(1, self.home_btn)
        self.main_stack.setCurrentIndex(2)

    def _home_panel(self, title, artwork=False):
        panel = HomeArtworkPanel() if artwork else HoverHomePanel()
        panel.setObjectName('homePanel')
        panel.setStyleSheet(
            'QFrame#homePanel { background:#15181f; border:1px solid #303742; border-radius:24px; }')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(25, 20, 25, 20)
        layout.setSpacing(11)
        heading = QLabel(title)
        heading.setStyleSheet('font-size:18px; font-weight:700; border:0; background:transparent;')
        layout.addWidget(heading)
        return panel, layout, heading

    def _build_home_view(self):
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(32, 24, 32, 30)
        layout.setSpacing(17)
        heading = self._localized_label('欢迎来到 Velia', 'Welcome to Velia')
        heading.setStyleSheet('font-size:29px; font-weight:700;')
        layout.addWidget(heading)

        banner = HomeLibraryBanner()
        banner.setObjectName('homeBanner')
        banner.setProperty('thumbnailStart', 0.24)
        banner.setCursor(Qt.PointingHandCursor)
        banner.clicked.connect(lambda: self.main_stack.setCurrentIndex(0))
        banner.setMinimumHeight(195)
        banner.setStyleSheet(
            'QFrame#homeBanner { background:#202836; border:1px solid #475369; border-radius:27px; }')
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(29, 21, 25, 21)
        banner_text = QVBoxLayout()
        self.home_msr_title = QLabel(TXT('前往明日方舟音乐库', 'Explore Monster Siren'))
        self.home_msr_title.setStyleSheet('font-size:24px; font-weight:700; background:transparent; border:0;')
        banner_text.addWidget(self.home_msr_title)
        self.home_msr_note = QLabel(TXT('塞壬唱片 · 在线曲目与本地音乐',
                                       'Monster Siren · online and local music'))
        self.home_msr_note.setStyleSheet('color:#afb9cc; background:transparent; border:0;')
        banner_text.addWidget(self.home_msr_note)
        banner_text.addStretch()
        self.home_msr_button = FloatingButton(TXT('进入音乐库  ↗', 'Enter Library  ↗'))
        self.home_msr_button.clicked.connect(lambda: self.main_stack.setCurrentIndex(0))
        banner_text.addWidget(self.home_msr_button, 0, Qt.AlignLeft)
        banner_layout.addLayout(banner_text, 1)
        layout.addWidget(banner)

        daily, daily_layout, self.home_daily_heading = self._home_panel(
            TXT('今日舟乐推荐', 'Today’s Arknights Track'))
        daily.setMinimumHeight(225)
        daily_row = QHBoxLayout()
        self.home_daily_cover = QLabel()
        self.home_daily_cover.setFixedSize(144, 144)
        self.home_daily_cover.setAlignment(Qt.AlignCenter)
        self.home_daily_cover.setStyleSheet(
            'background:#303a4d; border:1px dashed #69758a; border-radius:17px;')
        self.home_daily_cover.setText(TXT('封面', 'Cover'))
        daily_row.addWidget(self.home_daily_cover)
        daily_text = QVBoxLayout()
        self.home_daily_title = QLabel(TXT('正在准备今日推荐…', 'Preparing today’s track…'))
        self.home_daily_title.setStyleSheet('font-size:23px; font-weight:700; background:transparent;')
        self.home_daily_title.setWordWrap(True)
        daily_text.addWidget(self.home_daily_title)
        self.home_daily_meta = QLabel('')
        self.home_daily_meta.setWordWrap(True)
        daily_text.addWidget(self.home_daily_meta)
        self.home_daily_intro = QLabel(TXT('从塞壬唱片音乐库中每日选出一首。',
                                           'A daily pick from the Monster Siren catalogue.'))
        self.home_daily_intro.setWordWrap(True)
        self.home_daily_intro.setMaximumHeight(58)
        daily_text.addWidget(self.home_daily_intro)
        daily_text.addStretch()
        self.home_daily_play = FloatingButton(TXT('播放今日推荐  ▶', 'Play Today’s Pick  ▶'))
        self.home_daily_play.setEnabled(False)
        self.home_daily_play.clicked.connect(self._play_daily_song)
        daily_text.addWidget(self.home_daily_play, 0, Qt.AlignLeft)
        daily_row.addLayout(daily_text, 1)
        daily_layout.addLayout(daily_row)
        layout.addWidget(daily)

        bottom = QHBoxLayout()
        news, news_layout, self.home_news_heading = self._home_panel(TXT('每日药闻', 'Daily Pharmaceutical News'))
        news.setMinimumHeight(225)
        news_text = self._localized_label('内容即将上线 · 此处暂为占位。',
                                          'Coming soon · this panel is a placeholder.')
        news_text.setWordWrap(True)
        news_layout.addWidget(news_text)
        news_layout.addStretch()
        bottom.addWidget(news, 1)
        liked, liked_layout, self.home_liked_heading = self._home_panel(
            TXT('22N7O 喜欢听的', '22N7O’s Favorites'), artwork=True)
        liked.setProperty('thumbnailStart', 0.28)
        liked.setMinimumHeight(225)
        liked_cover = QLabel(TXT('封面 · 待添加', 'Cover · placeholder'))
        liked_cover.setObjectName('homeLikedCover')
        liked_cover.setFixedSize(80, 80)
        liked_cover.setAlignment(Qt.AlignCenter)
        liked_cover.setStyleSheet('background:#303a4d; border:1px dashed #69758a; border-radius:12px;')
        liked_row = QHBoxLayout()
        liked_row.addWidget(liked_cover)
        liked_placeholder = self._localized_label('歌曲、信息与简介 · 待添加',
                                                  'Track, details and description · coming soon')
        liked_placeholder.setWordWrap(True)
        liked_row.addWidget(liked_placeholder, 1)
        liked_layout.addLayout(liked_row)
        liked_layout.addStretch()
        bottom.addWidget(liked, 1)
        layout.addLayout(bottom)

        self.home_playground = ClickableHomePanel()
        self.home_playground.setObjectName('homePanel')
        self.home_playground.setCursor(Qt.PointingHandCursor)
        self.home_playground.setMinimumHeight(220)
        self.home_playground.setStyleSheet(
            'QFrame#homePanel { background:#15181f; border:1px solid #303742; border-radius:24px; }')
        pg_layout = QHBoxLayout(self.home_playground)
        pg_layout.setContentsMargins(25, 20, 25, 20)
        pg_text = QVBoxLayout()
        self.home_playground_heading = QLabel(TXT('游乐场', 'Playground'))
        self.home_playground_heading.setStyleSheet('font-size:22px; font-weight:700; background:transparent; border:0;')
        pg_text.addWidget(self.home_playground_heading)
        self.home_playground_note = QLabel(TXT(
            '请君入园·依律镇抚',
            'Ingredere hortum · lege compesce'))
        self.home_playground_note.setWordWrap(True)
        self.home_playground_note.setStyleSheet('background:transparent; border:0;')
        pg_text.addWidget(self.home_playground_note)
        pg_text.addStretch()
        self.home_playground_button = FloatingButton(TXT('进入游乐场  ↗', 'Enter Playground  ↗'))
        self.home_playground_button.clicked.connect(self._open_playground)
        pg_text.addWidget(self.home_playground_button, 0, Qt.AlignLeft)
        pg_layout.addLayout(pg_text, 1)
        self.home_playground_art = CoverImageLabel()
        self.home_playground_art.setMinimumSize(430, 165)
        self.home_playground_art.setAlignment(Qt.AlignCenter)
        self.home_playground_art.setStyleSheet('background:transparent; border:0;')
        pg_layout.addWidget(self.home_playground_art, 1)
        self.home_playground.clicked.connect(self._open_playground)
        layout.addWidget(self.home_playground)
        self._update_playground_art()
        self.home_guide = FloatingButton(TXT('打开使用指南', 'Open User Guide'))
        self.home_guide.clicked.connect(self._show_guide)
        layout.addWidget(self.home_guide, 0, Qt.AlignRight)
        layout.addStretch()
        page.setWidget(host)
        return page

    def _update_playground_art(self):
        if not hasattr(self, 'home_playground_art'):
            return
        name = 'day_theme.png' if ACTIVE_THEME == 'day' else 'night_theme.png'
        path = RESOURCE_PIC_DIR / name
        pix = QPixmap(str(path)) if path.is_file() else QPixmap()
        if pix.isNull():
            self.home_playground_art.clearSourcePixmap()
            self.home_playground_art.setText(TXT('游乐场配图 · 待添加', 'Playground artwork · missing'))
            return
        self.home_playground_art.setText('')
        self.home_playground_art.setSourcePixmap(pix)

    def _open_playground(self):
        self.main_stack.setCurrentIndex(3)
        if hasattr(self, 'playground_page'):
            self.playground_page.canvas.setFocus(Qt.OtherFocusReason)

    def _build_playground_view(self):
        self.playground_page = PlaygroundPage(self)
        self.playground_page.backRequested.connect(lambda: self.main_stack.setCurrentIndex(2))
        self.playground_page.themeRequested.connect(self._playground_theme_requested)
        self.playground_page.multiplayerRequested.connect(self._open_multiplayer_join)
        return self.playground_page

    def _open_multiplayer_join(self):
        actors = list(getattr(self.playground_page.canvas, '_actors', []))
        if not actors:
            QMessageBox.warning(
                self,
                TXT('联机房间', 'Multiplayer Room'),
                TXT('没有可用的干员动画包。', 'No operator animation packs are available.')
            )
            return

        chooser = QDialog(self)
        chooser.setWindowTitle(TXT('选择干员', 'Choose Operator'))
        chooser.resize(430, 500)
        outer = QVBoxLayout(chooser)
        title = QLabel(TXT('选择进入房间的干员', 'Choose your operator'))
        title.setStyleSheet('font-size:21px; font-weight:700;')
        outer.addWidget(title)

        note = QLabel(TXT(
            '联机房间只有真实用户控制的角色，没有 AI。',
            'The multiplayer room contains only human-controlled avatars; there is no AI.'
        ))
        note.setWordWrap(True)
        note.setStyleSheet('color:#8b939f;')
        outer.addWidget(note)

        list_widget = QListWidget()
        for actor in actors:
            item = QListWidgetItem(
                str(actor.get('display_name') or actor.get('id'))
            )
            item.setData(Qt.UserRole, actor.get('id'))
            list_widget.addItem(item)
        if list_widget.count():
            list_widget.setCurrentRow(0)
        outer.addWidget(list_widget, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = FloatingButton(TXT('取消', 'Cancel'))
        ok = FloatingButton(TXT('下一步', 'Next'))
        cancel.clicked.connect(chooser.reject)
        ok.clicked.connect(chooser.accept)
        actions.addWidget(cancel)
        actions.addWidget(ok)
        outer.addLayout(actions)

        if chooser.exec() != QDialog.Accepted or list_widget.currentItem() is None:
            return
        operator_id = str(list_widget.currentItem().data(Qt.UserRole))

        nickname, accepted = QInputDialog.getText(
            self,
            TXT('房间昵称', 'Room Nickname'),
            TXT('你想在这个房间里叫什么？', 'What should others call you in this room?')
        )
        nickname = str(nickname).strip()[:24]
        if not accepted or not nickname:
            return

        if hasattr(self, 'multiplayer_page') and self.multiplayer_page is not None:
            try:
                self.multiplayer_page.close_room()
                self.main_stack.removeWidget(self.multiplayer_page)
                self.multiplayer_page.deleteLater()
            except RuntimeError:
                pass

        self.multiplayer_page = MultiplayerRoomPage(
            actors,
            operator_id,
            nickname,
            self,
        )
        self.multiplayer_page.backRequested.connect(self._leave_multiplayer_room)
        self.main_stack.addWidget(self.multiplayer_page)
        self.main_stack.setCurrentWidget(self.multiplayer_page)
        self.multiplayer_page.canvas.setFocus(Qt.OtherFocusReason)

    def _leave_multiplayer_room(self):
        page = getattr(self, 'multiplayer_page', None)
        if page is None:
            self.main_stack.setCurrentWidget(self.playground_page)
            return
        page.close_room()
        self.main_stack.setCurrentWidget(self.playground_page)
        self.main_stack.removeWidget(page)
        page.deleteLater()
        self.multiplayer_page = None

    def _playground_theme_requested(self, theme):
        if theme not in ('day', 'night'):
            return

        # Skip a redundant theme application when already in the requested mode.
        if self._effective_theme() == theme and self.theme_mode == theme:
            if hasattr(self, 'playground_page'):
                self.playground_page.canvas.sync_daylight_to_theme()
            return

        self.theme_mode = theme
        self.settings_store.setValue('theme', theme)

        if hasattr(self, 'theme_combo'):
            index = self.theme_combo.findData(theme)
            if index >= 0:
                blocked = self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(index)
                self.theme_combo.blockSignals(blocked)

        self._apply_theme()
        if hasattr(self, 'playground_page'):
            self.playground_page.canvas.sync_daylight_to_theme()

    def _show_guide(self):
        if getattr(self, 'guide_window', None) is not None and self.guide_window.isVisible():
            self.guide_window.raise_()
            self.guide_window.activateWindow()
            return
        guide = QDialog(self)
        guide.setWindowTitle(TXT('Velia 使用指南', 'Velia User Guide'))
        guide.setWindowFlag(Qt.Window, True)
        guide.resize(690, 570)
        outer = QVBoxLayout(guide)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 20, 24, 24)
        body.setSpacing(13)
        sections = [
            (TXT('主页', 'Home'), TXT(
                '点击“前往明日方舟音乐库”进入播放器。今日舟乐每天推荐一首曲目；每日药闻和 22N7O 喜欢听的目前为占位内容。',
                'Enter the Monster Siren library from Home. Today’s Pick recommends one track per day; the other two panels are placeholders.')),
            (TXT('搜索与分类', 'Search and Categories'), TXT(
                '在播放器搜索框输入曲名、专辑或艺术家；还可输入干员名称查找对应 EP，输入活动名称查找关联 OST。首次联网时会在后台更新 PRTS 索引，之后可使用本地缓存。点击专辑展开曲目；用五角星收藏，用文件夹管理分类。',
                'Search by title, album or artist. Enter an operator name for related EPs, or an event name for related OSTs. The PRTS search index updates in the background and remains cached. Expand albums with a click; use stars and folders for favorites and categories.')),
            (TXT('播放与保存', 'Playback and Saving'), TXT(
                '在线曲目首次播放会下载到临时缓存；“保存到本地”会将歌曲整理至 local/music。关闭时清理临时歌曲缓存，也可在设置中手动清理。',
                'Online songs download to a temporary cache on first play. Save Locally places music in local/music. Temporary audio is cleared on exit or with the settings button.')),            (TXT('游乐场', 'Playground'), TXT(
                '游乐场会扫描 resource/gif 中所有 ZIP 与 GIF，自动载入完整的 _m/_r/_s 动画组合。左键选择角色后用 WASD 控制；其他角色按性格自动寻路；右键角色可睡觉或叫醒。',
                'Playground scans every ZIP and GIF under resource/gif for complete _m/_r/_s animation sets. Left-click an actor to control it with WASD; all others pathfind automatically according to personality data.')),
            (TXT('文件位置', 'Files'), TXT(
                '个人偏好位于 local/preferences；游乐场主题配图位于 resource/pic；角色动画包与性格配置位于 resource/gif。',
                'Preferences live in local/preferences; playground theme art lives in resource/pic; actor packs and personality configuration live in resource/gif.')),
        ]
        for heading, description in sections:
            title = QLabel(heading)
            title.setStyleSheet('font-size:19px; font-weight:700;')
            body.addWidget(title)
            paragraph = QLabel(description)
            paragraph.setWordWrap(True)
            paragraph.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body.addWidget(paragraph)
            body.addSpacing(9)
        body.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        close = FloatingButton(TXT('关闭', 'Close'))
        close.clicked.connect(guide.close)
        outer.addWidget(close, 0, Qt.AlignRight)
        self.guide_window = guide
        guide.show()

    def _refresh_daily_song(self):
        if not hasattr(self, 'home_daily_title'):
            return
        if not self.online_tracks:
            self.home_daily_title.setText(TXT('正在准备今日推荐…', 'Preparing today’s track…'))
            return
        today = date.today().isoformat()
        eligible = [t for t in self.online_tracks if t.get('title') and t.get('cid')]
        if not eligible:
            return
        saved = self.settings_store.value('daily_recommendation', {})
        track = next((t for t in eligible if t['cid'] == saved.get('cid')), None) if (
            isinstance(saved, dict) and saved.get('date') == today) else None
        if track is None:
            eligible.sort(key=lambda t: int(t['cid']))
            index = int(hashlib.sha256(today.encode('ascii')).hexdigest(), 16) % len(eligible)
            track = eligible[index]
            self.settings_store.setValue('daily_recommendation', {'date': today, 'cid': track['cid']})
        self.home_daily_cid = track['cid']
        self.home_daily_title.setText(track['title'])
        self.home_daily_meta.setText('  ·  '.join(filter(None, (
            track.get('artist', ''), track.get('album', ''),
            self.prts_cache.get(track['cid'], {}).get('year', '')))))
        intro = re.sub(r'<[^>]+>', '', track.get('intro') or '').strip()
        self.home_daily_intro.setText(intro[:240] if intro else TXT(
            '该曲目暂无官方简介；点击播放可查看歌曲信息。',
            'No description available yet. Play to explore the song.'))
        self.home_daily_play.setEnabled(True)
        cover_path = ARTWORK_DIR / (track['cid'] + '.img')
        if cover_path.is_file():
            try:
                track['cover_bytes'] = cover_path.read_bytes()
            except OSError:
                pass
        if track.get('cover_bytes'):
            self.home_daily_cover.setText('')
            self.home_daily_cover.setPixmap(_rounded_cover(
                self._pixmap_for_track(track, 144), 144, 17))
        else:
            self.home_daily_cover.setText(TXT('封面载入中', 'Loading cover'))
            self.home_daily_cover.clear()
            self.home_daily_cover.setText(TXT('封面载入中', 'Loading cover'))
            self._ensure_cover(track)
        if track.get('album_cid') and getattr(self, '_daily_intro_cid', None) != track['cid']:
            self._daily_intro_cid = track['cid']
            self._submit('daily_intro:' + track['cid'], siren.album_detail, track['album_cid'])

    def _play_daily_song(self):
        cid = getattr(self, 'home_daily_cid', '')
        index = next((i for i, t in enumerate(self.online_tracks) if t['cid'] == cid), -1)
        if index < 0:
            return
        if self.source_kind != 'online':
            self.source_combo.setCurrentIndex(0)
        self.main_stack.setCurrentIndex(0)
        self.select_track(index, autoplay=True)

    def _daily_midnight_check(self):
        current_day = date.today().isoformat()
        if getattr(self, '_daily_date', None) != current_day:
            self._daily_date = current_day
            self._refresh_daily_song()

    def _build_player_view(self):
        page = QWidget()
        main = QHBoxLayout(page)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(18)

        # Left playlist
        left = QFrame()
        left.setMinimumWidth(300)
        left.setMaximumWidth(380)
        left.setObjectName("playlistPanel")
        left.setStyleSheet("QFrame#playlistPanel { background:#0c0c0c; border:1px solid #181818; border-radius:22px; }")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(7)

        playlist_head = QHBoxLayout()
        label = self._localized_label("歌单", "Playlist")
        label.setStyleSheet("font-size:15px; font-weight:700; color:#dddddd;")
        playlist_head.addWidget(label)
        playlist_head.addStretch(1)

        self.category_combo = VisibleComboBox()
        self.category_combo.setMinimumWidth(118)
        self.category_combo.setMaximumWidth(160)
        self.category_combo.currentIndexChanged.connect(self._category_changed)
        playlist_head.addWidget(self.category_combo)

        self.add_category_btn = FolderAddButton()
        self.add_category_btn.setFixedSize(38, 34)
        self.add_category_btn.setToolTip(TXT("新建分类", "New Category"))
        self.add_category_btn.setObjectName("chipButton")
        self.add_category_btn.setStyleSheet(
            """
            QPushButton {
                font-size: 16px;
                font-weight: 700;
                padding: 0;
            }
            """
        )
        self.add_category_btn.clicked.connect(self._create_category)
        playlist_head.addWidget(self.add_category_btn)

        self.batch_add_btn = FloatingButton("＋")
        self.batch_add_btn.setFixedSize(30, 28)
        self.batch_add_btn.setObjectName("chipButton")
        self.batch_add_btn.setToolTip(TXT("批量加入歌曲", "Batch Add Tracks"))
        self.batch_add_btn.clicked.connect(self._batch_add_to_active_category)
        self.batch_add_btn.hide()
        playlist_head.addWidget(self.batch_add_btn)

        self.delete_category_btn = FloatingButton("−")
        self.delete_category_btn.setFixedSize(30, 28)
        self.delete_category_btn.setObjectName("chipButton")
        self.delete_category_btn.setToolTip(
            TXT("删除当前歌单 / 分类", "Delete Current Playlist / Category")
        )
        self.delete_category_btn.setStyleSheet(
            """
            QPushButton {
                font-size: 18px;
                font-weight: 700;
                padding: 0;
            }
            QPushButton:hover {
                background: #2a1212;
                border-color: #5c2525;
            }
            """
        )
        self.delete_category_btn.clicked.connect(
            self._delete_active_category
        )
        self.delete_category_btn.hide()
        playlist_head.addWidget(self.delete_category_btn)

        ll.addLayout(playlist_head)
        self._rebuild_category_combo()

        online_row = QHBoxLayout()
        self.source_combo = VisibleComboBox()
        self.source_combo.addItem(TXT("塞壬唱片 · 在线", "Monster Siren · Online"), "online")
        self.source_combo.addItem(TXT("本地音乐", "Local Music"), "local")
        self.source_combo.currentIndexChanged.connect(self._switch_source)
        online_row.addWidget(self.source_combo)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(TXT("搜索歌曲、干员 EP 或活动 OST…", "Search tracks, operator EPs or event OSTs…"))
        self.search_box.textChanged.connect(self._search_changed)
        online_row.addWidget(self.search_box, 1)
        ll.addLayout(online_row)

        self.scan_status = QLabel("")
        self.scan_status.setStyleSheet("color:#777; font-size:11px;")
        self.scan_status.setWordWrap(True)
        ll.addWidget(self.scan_status)

        self.playlist_scroll = QScrollArea()
        self.playlist_scroll.setWidgetResizable(True)
        self.playlist_scroll.setFrameShape(QFrame.NoFrame)
        self.playlist_host = QWidget()
        self.playlist_layout = QVBoxLayout(self.playlist_host)
        self.playlist_layout.setContentsMargins(0, 0, 0, 0)
        self.playlist_layout.setSpacing(3)
        self.playlist_layout.addStretch(1)
        self.playlist_scroll.setWidget(self.playlist_host)
        self.playlist_scroll.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        ll.addWidget(self.playlist_scroll, 1)
        self.to_top_btn = FloatingButton(TXT("↑ 回到顶部", "↑ Back to Top"))
        self.to_top_btn.setToolTip(TXT("立即回到歌曲列表顶部", "Jump to the top of the song list"))
        self.to_top_btn.clicked.connect(lambda: self.playlist_scroll.verticalScrollBar().setValue(0))
        self.to_top_btn.setVisible(False)
        ll.addWidget(self.to_top_btn, 0, Qt.AlignRight)
        main.addWidget(left)

        # Right detail / lyrics / transport
        right = QFrame()
        right.setObjectName("detailPanel")
        right.setStyleSheet("QFrame#detailPanel { background:#090909; border:1px solid #171717; border-radius:22px; }")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(20, 18, 20, 18)
        rl.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(18)

        info_box = QWidget()
        info_box.setMaximumWidth(430)
        info_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        info_grid = QGridLayout(info_box)
        info_grid.setContentsMargins(0, 0, 0, 0)
        info_grid.setHorizontalSpacing(8)
        info_grid.setVerticalSpacing(2)
        info_grid.setColumnStretch(1, 1)

        self.detail_values = {}
        self.detail_labels = {}
        details = [
            ("Title", TXT("歌名", "Title")),
            ("Artist", TXT("艺术家", "Artist")),
            ("Album", TXT("专辑名", "Album")),
            ("Track", TXT("曲目编号", "Track")),
            ("Year", TXT("年份", "Year")),
            ("Cover", TXT("封面", "Cover")),
        ]
        for row, (key, zhlabel) in enumerate(details):
            lab = QLabel(f"{zhlabel}:")
            lab.setFixedWidth(66)
            lab.setFixedHeight(21)
            lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            lab.setStyleSheet(
                "color:#777; font-size:11px; background:transparent; border:0; padding:0;"
            )

            val = QLabel("—")
            val.setFixedHeight(21)
            val.setMinimumWidth(120)
            val.setMaximumWidth(340)
            val.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            val.setStyleSheet(
                "color:#cecece; font-size:11px; background:transparent; border:0; padding:0;"
            )
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setToolTip("")

            info_grid.addWidget(lab, row, 0)
            info_grid.addWidget(val, row, 1)
            self.detail_labels[key] = lab
            self.detail_values[key] = val

        header.addWidget(info_box, 0, Qt.AlignLeft | Qt.AlignTop)
        header.addStretch(1)

        now = QVBoxLayout()
        now.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.hero_cover = QLabel()
        self.hero_cover.setFixedSize(112, 112)
        self.hero_cover.setAlignment(Qt.AlignCenter)
        self.hero_cover.setStyleSheet("background:#121212; border:1px solid #252525; border-radius:18px;")
        now.addWidget(self.hero_cover, 0, Qt.AlignRight)
        hero_title_row = QHBoxLayout()
        hero_title_row.setSpacing(6)
        hero_title_row.addStretch(1)

        self.hero_favorite_btn = RoundedStarButton()
        self.hero_favorite_btn.setCheckable(True)
        self.hero_favorite_btn.setFixedSize(44, 44)
        self.hero_favorite_btn.setCursor(Qt.PointingHandCursor)
        self.hero_favorite_btn.setToolTip(TXT("收藏", "Favorite"))
        self.hero_favorite_btn.clicked.connect(self._hero_favorite_toggled)
        hero_title_row.addWidget(self.hero_favorite_btn)

        self.hero_title = QLabel(TXT("未选择歌曲", "No track selected"))
        self.hero_title.setAlignment(Qt.AlignRight)
        self.hero_title.setWordWrap(True)
        self.hero_title.setMaximumWidth(260)
        self.hero_title.setStyleSheet("font-size:15px; font-weight:700; color:#eeeeee;")
        hero_title_row.addWidget(self.hero_title)
        now.addLayout(hero_title_row)
        self.hero_artist = QLabel("")
        self.hero_artist.setAlignment(Qt.AlignRight)
        self.hero_artist.setStyleSheet("color:#888; font-size:12px;")
        now.addWidget(self.hero_artist, 0, Qt.AlignRight)
        header.addLayout(now)
        rl.addLayout(header)
        self.album_intro = QLabel("")
        self.album_intro.setWordWrap(True)
        self.album_intro.setMaximumHeight(76)
        self.album_intro.setStyleSheet("color:#858585; font-size:11px; padding:2px 5px;")
        self.album_intro.hide()
        rl.addWidget(self.album_intro)

        self.context_label = QLabel('')
        self.context_label.setStyleSheet('color:#8a9ab9; font-size:12px; padding:1px 5px;')
        self.context_label.setWordWrap(True)
        self.context_label.hide()
        rl.addWidget(self.context_label)

        self.credits_label = QLabel("")
        self.credits_label.setStyleSheet("color:#858585; font-size:11px; padding:2px 5px;")
        self.credits_label.setWordWrap(True)
        self.credits_label.hide()
        rl.addWidget(self.credits_label)

        self.save_online_btn = FloatingButton(TXT("保存到本地", "Save Locally"))
        self.save_online_btn.setObjectName("chipButton")
        self.save_online_btn.clicked.connect(self._save_online_track)
        self.save_online_btn.hide()
        rl.addWidget(self.save_online_btn, 0, Qt.AlignRight)

        # Large lyric roller area between metadata and transport.
        lyric_frame = QFrame()
        lyric_frame.setObjectName("lyricsPanel")
        lyric_frame.setStyleSheet("QFrame#lyricsPanel { background:#080808; border:0; border-radius:18px; }")
        lyric_lay = QVBoxLayout(lyric_frame)
        lyric_lay.setContentsMargins(30, 12, 30, 12)
        lyric_lay.setSpacing(4)

        self.lyric_labels = []
        for i in range(7):
            lbl = QLabel("")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setMinimumHeight(34 if i != 3 else 50)
            self.lyric_labels.append(lbl)
            lyric_lay.addWidget(lbl)
        rl.addWidget(lyric_frame, 1)

        transport = QFrame()
        transport.setObjectName("transportPanel")
        transport.setStyleSheet("QFrame#transportPanel { background:#0d0d0d; border:1px solid #181818; border-radius:18px; }")
        tl = QVBoxLayout(transport)
        tl.setContentsMargins(14, 10, 14, 10)
        tl.setSpacing(7)

        time_row = QHBoxLayout()
        self.current_time = QLabel("0:00")
        self.current_time.setStyleSheet("color:#777; font-size:11px;")
        self.total_time = QLabel("0:00")
        self.total_time.setStyleSheet("color:#777; font-size:11px;")
        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.sliderPressed.connect(self._seek_started)
        self.progress.sliderReleased.connect(self._seek_finished)
        time_row.addWidget(self.current_time)
        time_row.addWidget(self.progress, 1)
        time_row.addWidget(self.total_time)
        tl.addLayout(time_row)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.prev_btn = FloatingButton("◀")
        self.prev_btn.setFixedSize(38, 34)
        self.prev_btn.clicked.connect(self._previous)
        self.play_btn = FloatingButton("▶")
        self.play_btn.setFixedSize(52, 38)
        self.play_btn.clicked.connect(self._toggle_play)
        self.next_btn = FloatingButton("▶")
        self.next_btn.setFixedSize(38, 34)
        self.next_btn.clicked.connect(self._next)

        self.play_mode_btn = DelayedToolTipButton()
        self.play_mode_btn.setFixedSize(38, 34)
        self.play_mode_btn.setObjectName("chipButton")
        self.play_mode_btn.clicked.connect(self._cycle_playback_mode)

        for b in (self.prev_btn, self.play_btn, self.next_btn):
            b.setObjectName("chipButton")
        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_btn)
        controls.addWidget(self.play_mode_btn)
        self._sync_playback_mode_button()

        controls.addSpacing(14)
        vol = self._localized_label("音量", "Vol")
        vol.setStyleSheet("color:#777; font-size:11px;")
        controls.addWidget(vol)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(82)
        self.volume.setFixedWidth(110)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100.0))
        controls.addWidget(self.volume)
        tl.addLayout(controls)
        rl.addWidget(transport)

        main.addWidget(right, 1)
        return page

    def _build_settings_view(self):
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(16)

        rail = QFrame()
        rail.setFixedWidth(230)
        rail.setObjectName("settingsRail")
        rail.setStyleSheet("QFrame#settingsRail { background:#0b0b0b; border:1px solid #181818; border-radius:22px; }")
        rlay = QVBoxLayout(rail)
        rlay.setContentsMargins(12, 12, 12, 12)

        heading = self._localized_label("音乐设置", "Music Settings")
        heading.setStyleSheet("font-size:17px; font-weight:700;")
        rlay.addWidget(heading)

        self.system_nav = FloatingButton(TXT("系统设置", "System Settings"))
        self.system_nav.setObjectName("chipButton")
        self.system_nav.setCheckable(True)
        self.system_nav.setChecked(True)
        self.system_nav.clicked.connect(lambda: self._select_settings_page(0))
        rlay.addWidget(self.system_nav)

        self.customize_nav = FloatingButton(TXT("歌词自定义", "Lyrics Customization"))
        self.customize_nav.setObjectName("chipButton")
        self.customize_nav.setCheckable(True)
        self.customize_nav.clicked.connect(lambda: self._select_settings_page(1))
        rlay.addWidget(self.customize_nav)
        rlay.addStretch(1)
        outer.addWidget(rail)

        self.settings_content_stack = QStackedWidget()
        self.settings_content_stack.addWidget(self._build_system_settings_view())
        self.settings_content_stack.addWidget(self._build_lyric_settings_view())
        outer.addWidget(self.settings_content_stack, 1)
        return page

    def _build_system_settings_view(self):
        system = QScrollArea()
        system.setWidgetResizable(True)
        system.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(24, 18, 24, 18)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(18)
        title = self._localized_label("系统设置", "System Settings")
        title.setStyleSheet("font-size:20px; font-weight:700;")
        grid.addWidget(title, 0, 0, 1, 2)

        grid.addWidget(self._localized_label("界面语言", "Interface Language"), 1, 0)
        self.language_combo = VisibleComboBox()
        self.language_combo.addItem("简体中文", "zh")
        self.language_combo.addItem("English", "en")
        self.language_combo.setCurrentIndex(self.language_combo.findData(APP_SETTINGS["language"]))
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        grid.addWidget(self.language_combo, 1, 1)

        grid.addWidget(self._localized_label("界面主题", "Appearance"), 2, 0)
        self.theme_combo = VisibleComboBox()
        self.theme_combo.addItem(TXT("白昼", "Day"), "day")
        self.theme_combo.addItem(TXT("黑夜", "Night"), "night")
        self.theme_combo.addItem(TXT("跟随系统主题", "Follow System Theme"), "system")
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.theme_mode))
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        grid.addWidget(self.theme_combo, 2, 1)

        self.open_folder_btn = FloatingButton(TXT("打开音乐文件夹", "Open Music Folder"))
        self.open_folder_btn.setObjectName("chipButton")
        self.open_folder_btn.clicked.connect(self._open_music_folder)
        grid.addWidget(self.open_folder_btn, 6, 0, 1, 2)

        self.open_preference_btn = FloatingButton(TXT("打开偏好设置文件夹", "Open Preferences Folder"))
        self.open_preference_btn.setObjectName("chipButton")
        self.open_preference_btn.clicked.connect(self._open_preference_folder)
        grid.addWidget(self.open_preference_btn, 7, 0, 1, 2)

        self.compress_check = VisibleCheckBox(TXT("后台转为 MP3 并压缩", "Convert to MP3 in background"))
        self.compress_check.setChecked(bool(self.settings_store.value("encode_mp3", True)))
        self.compress_check.toggled.connect(lambda checked: self.settings_store.setValue("encode_mp3", checked))
        grid.addWidget(self.compress_check, 8, 0, 1, 2)

        grid.addWidget(self._localized_label("MP3 比特率", "MP3 Bitrate"), 9, 0)
        self.bitrate_combo = VisibleComboBox()
        for bitrate in (128, 192, 256, 320):
            self.bitrate_combo.addItem(f"{bitrate} kbps", bitrate)
        bitrate_index = self.bitrate_combo.findData(int(self.settings_store.value("mp3_bitrate", 256)))
        self.bitrate_combo.setCurrentIndex(max(0, bitrate_index))
        self.bitrate_combo.currentIndexChanged.connect(
            lambda _: self.settings_store.setValue("mp3_bitrate", self.bitrate_combo.currentData()))
        grid.addWidget(self.bitrate_combo, 9, 1)

        grid.addWidget(self._localized_label("MP3 采样率", "MP3 Sample Rate"), 10, 0)
        self.sample_combo = VisibleComboBox()
        for rate in (32000, 44100, 48000):
            self.sample_combo.addItem(f"{rate:,} Hz", rate)
        rate_index = self.sample_combo.findData(int(self.settings_store.value("mp3_sample_rate", 44100)))
        self.sample_combo.setCurrentIndex(max(0, rate_index))
        self.sample_combo.currentIndexChanged.connect(
            lambda _: self.settings_store.setValue("mp3_sample_rate", self.sample_combo.currentData()))
        grid.addWidget(self.sample_combo, 10, 1)

        self.clear_cache_btn = FloatingButton(TXT("一键清理临时歌曲缓存", "Clear Temporary Song Cache"))
        self.clear_cache_btn.setObjectName("chipButton")
        self.clear_cache_btn.clicked.connect(self._clear_online_audio_cache)
        grid.addWidget(self.clear_cache_btn, 11, 0, 1, 2)

        note = self._localized_label(
            "歌曲和个人偏好保存在项目下的 local 文件夹中。",
            "Music and preferences are stored in this project's local folder."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#787878; font-size:12px;")
        grid.addWidget(note, 12, 0, 1, 2)
        grid.setRowStretch(13, 1)
        system.setWidget(host)
        return system

    def _build_lyric_settings_view(self):

        custom = QScrollArea()
        custom.setWidgetResizable(True)
        custom.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(24, 18, 24, 18)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)

        title = self._localized_label("桌面歌词自定义", "Desktop Lyrics Customization")
        title.setStyleSheet("font-size:20px; font-weight:700;")
        grid.addWidget(title, 0, 0, 1, 3)

        row = 1
        grid.addWidget(self._localized_label("字幕大小", "Subtitle Size"), row, 0)
        self.font_size_spin = IconSpinBox()
        self.font_size_spin.setRange(14, 72)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.valueChanged.connect(self._customization_changed)
        grid.addWidget(self.font_size_spin, row, 1)
        row += 1

        grid.addWidget(self._localized_label("字体", "Font"), row, 0)
        self.font_combo = VisibleFontComboBox()
        self.font_combo.currentFontChanged.connect(self._customization_changed)
        grid.addWidget(self.font_combo, row, 1, 1, 2)
        row += 1

        grid.addWidget(self._localized_label("粗细", "Weight"), row, 0)
        self.weight_combo = VisibleComboBox()
        self.weight_combo.addItem(TXT("常规", "Regular"), 400)
        self.weight_combo.addItem(TXT("中等", "Medium"), 500)
        self.weight_combo.addItem(TXT("粗体", "Bold"), 700)
        self.weight_combo.addItem(TXT("特粗", "Extra Bold"), 800)
        self.weight_combo.currentIndexChanged.connect(self._customization_changed)
        grid.addWidget(self.weight_combo, row, 1)
        row += 1

        grid.addWidget(self._localized_label("字幕颜色", "Subtitle Color"), row, 0)
        color_row = QHBoxLayout()

        self.rgb_r = IconSpinBox()
        self.rgb_g = IconSpinBox()
        self.rgb_b = IconSpinBox()
        for prefix, spin in (("R", self.rgb_r), ("G", self.rgb_g), ("B", self.rgb_b)):
            spin.setRange(0, 255)
            spin.setPrefix(prefix + " ")
            spin.setFixedWidth(92)
            spin.valueChanged.connect(self._rgb_changed)
            color_row.addWidget(spin)

        self.text_color_edit = QLineEdit("#ffffff")
        self.text_color_edit.setFixedWidth(92)
        self.text_color_edit.editingFinished.connect(self._hex_color_changed)
        color_row.addWidget(self.text_color_edit)

        self.text_color_btn = FloatingButton(TXT("色轮", "Color Wheel"))
        self.text_color_btn.setObjectName("chipButton")
        self.text_color_btn.clicked.connect(self._choose_text_color)
        color_row.addWidget(self.text_color_btn)

        self.rainbow_check = VisibleCheckBox(TXT("彩虹模式", "Rainbow"))
        self.rainbow_check.toggled.connect(self._customization_changed)
        color_row.addWidget(self.rainbow_check)
        color_row.addStretch(1)
        grid.addLayout(color_row, row, 1, 1, 2)
        row += 1

        grid.addWidget(self._localized_label("字幕背景", "Subtitle Background"), row, 0)
        bg_row = QHBoxLayout()
        self.bg_enable = VisibleCheckBox(TXT("启用背景", "Enable Background"))
        self.bg_enable.toggled.connect(self._customization_changed)
        self.bg_color_edit = QLineEdit("#000000")
        self.bg_color_edit.setFixedWidth(100)
        self.bg_color_edit.editingFinished.connect(self._customization_changed)
        self.bg_color_btn = FloatingButton(TXT("背景色轮", "Background Color"))
        self.bg_color_btn.setObjectName("chipButton")
        self.bg_color_btn.clicked.connect(self._choose_bg_color)
        bg_row.addWidget(self.bg_enable)
        bg_row.addWidget(self.bg_color_edit)
        bg_row.addWidget(self.bg_color_btn)
        bg_row.addStretch(1)
        grid.addLayout(bg_row, row, 1, 1, 2)
        row += 1

        grid.addWidget(self._localized_label("歌词位置", "Lyric Position"), row, 0)
        position_row = QHBoxLayout()
        self.position_btn = FloatingButton(TXT("拖动歌词位置", "Move Lyric Position"))
        self.position_btn.setObjectName("chipButton")
        self.position_btn.clicked.connect(self._toggle_position_editing)
        position_row.addWidget(self.position_btn)
        self.reset_position_btn = FloatingButton(TXT("恢复默认位置", "Reset Default Position"))
        self.reset_position_btn.setObjectName("chipButton")
        self.reset_position_btn.clicked.connect(self._reset_lyrics_position)
        position_row.addWidget(self.reset_position_btn)
        position_row.addStretch(1)
        grid.addLayout(position_row, row, 1, 1, 2)
        row += 1

        note = self._localized_label(
            "默认背景为透明。RGB/十六进制颜色与色轮可同时使用；开启彩虹模式后字幕颜色会持续变化。",
            "Background is transparent by default. Hex/RGB-style color entry and the color wheel can be used together; Rainbow mode continuously cycles lyric color."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#787878; font-size:12px;")
        grid.addWidget(note, row, 0, 1, 3)
        row += 1
        grid.setRowStretch(row, 1)

        custom.setWidget(host)
        return custom

    def _clear_online_audio_cache(self):
        if self._saving_count:
            return
        self._selection_token += 1
        self._purge_token = self._selection_token
        self._pending_save = False
        current_file = self.player.source().toLocalFile()
        if current_file and Path(current_file).is_relative_to(CACHE_DIR / "siren" / "audio"):
            self.player.stop()
            self.player.setSource(QUrl())
        target = CACHE_DIR / "siren" / "audio"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        self.scan_status.setText(TXT("临时歌曲缓存已清理", "Temporary song cache cleared"))

    def _clear_music_cache(self):
        """Completely remove the disposable extraction cache before a rescan."""
        # QMediaPlayer on Windows may briefly retain a handle to the current source.
        # Disconnect the source first, then give Qt a chance to release that handle.
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except Exception:
            pass
        QApplication.processEvents()

        if self.cache_dir.exists():
            for _ in range(3):
                shutil.rmtree(self.cache_dir, ignore_errors=True)
                if not self.cache_dir.exists():
                    break
                QApplication.processEvents()

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir.exists()

    def _refresh_music_library(self):
        """Refresh button path: clear cache first, then perform a clean rescan."""
        self.refresh_btn.setEnabled(False)
        self.scan_status.setText(TXT(
            "正在清除音乐缓存…",
            "Clearing music cache…"
        ))
        QApplication.processEvents()

        self._clear_music_cache()

        self.scan_status.setText(TXT(
            "缓存已清除，正在重新读取音乐…",
            "Cache cleared. Rescanning music…"
        ))
        QApplication.processEvents()

        # Run the actual scan on the next event-loop turn so the user can see that
        # cache removal completed before the library scan begins.
        QTimer.singleShot(0, self._reload_library_after_cache_clear)

    def _reload_library_after_cache_clear(self):
        try:
            self.reload_library(clear_cache=False)
        finally:
            self.refresh_btn.setEnabled(True)

    def _extract_archives_recursive(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.archive_roots = {}
        queue = [
            p for p in self.library_dir.rglob("*")
            if p.is_file() and p.suffix.lower() == ".zip" and self.cache_dir not in p.parents
        ]
        seen = set()

        while queue:
            zp = queue.pop(0)
            try:
                stat = zp.stat()
                signature = f"{zp.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
            except Exception:
                continue
            digest = hashlib.sha1(signature.encode("utf-8", errors="ignore")).hexdigest()[:14]
            if digest in seen:
                continue
            seen.add(digest)

            out = self.cache_dir / digest
            try:
                archive_key = zp.resolve().relative_to(self.library_dir.resolve()).as_posix()
            except ValueError:
                archive_key = None
                for parent, source in self.archive_roots.items():
                    try:
                        archive_key = source + "!/" + zp.resolve().relative_to(parent).as_posix()
                        break
                    except ValueError:
                        continue
                if archive_key is None:
                    archive_key = zp.name
            marker = out / ".source_signature"
            needs_extract = True
            if marker.exists():
                try:
                    needs_extract = marker.read_text(encoding="utf-8") != signature
                except Exception:
                    pass
            if needs_extract:
                if out.exists():
                    shutil.rmtree(out, ignore_errors=True)
                out.mkdir(parents=True, exist_ok=True)
                try:
                    _safe_extract_zip(zp, out)
                    marker.write_text(signature, encoding="utf-8")
                except Exception:
                    continue

            self.archive_roots[out.resolve()] = archive_key

            for nested in out.rglob("*.zip"):
                queue.append(nested)

    def reload_library(self, clear_cache=True):
        previously_selected = (
            self._track_key(self.tracks[self.current_index]["path"])
            if 0 <= self.current_index < len(self.tracks) else None
        )
        if clear_cache:
            self.scan_status.setText(TXT(
                "正在清理缓存并读取音乐文件…",
                "Clearing cache and scanning music files…"
            ))
            QApplication.processEvents()
            self._clear_music_cache()
        else:
            self.scan_status.setText(TXT(
                "正在重新读取音乐文件…",
                "Rescanning music files…"
            ))
            QApplication.processEvents()

        self._extract_archives_recursive()

        all_files = [p for p in self.library_dir.rglob("*") if p.is_file()]
        # ZIP files are kept in local/music; their playable contents live in
        # disposable extraction directories under local/cache.
        for extracted_root in self.archive_roots:
            all_files.extend(p for p in extracted_root.rglob("*") if p.is_file())
        lyric_files = [p for p in all_files if p.suffix.lower() in LYRIC_EXTENSIONS]
        audio_files = [p for p in all_files if p.suffix.lower() in AUDIO_EXTENSIONS]

        # Same-folder LRC gets priority; otherwise fall back to any same-stem LRC.
        by_stem = {}
        for lp in lyric_files:
            by_stem.setdefault(lp.stem.casefold(), []).append(lp)

        tracks = []
        for path in sorted(audio_files, key=lambda p: (p.name.casefold(), str(p).casefold())):
            info = _read_track_info(path)
            sidecar = path.with_suffix(".json")
            if sidecar.is_file():
                try:
                    stored = json.loads(sidecar.read_text(encoding="utf-8"))
                    if isinstance(stored, dict) and stored.get("source", "").startswith(siren.ROOT):
                        for field in ("title", "artist", "album", "intro"):
                            info[field] = stored.get(field) or info.get(field, "")
                except (OSError, ValueError):
                    pass
            cover_file = next((path.with_suffix(ext) for ext in (".png", ".jpg", ".jpeg")
                               if path.with_suffix(ext).is_file()), None)
            if cover_file:
                try:
                    info["cover_bytes"] = cover_file.read_bytes()
                    info["cover_desc"] = TXT("本地封面", "Local cover")
                except OSError:
                    pass
            local_lrc = path.with_suffix(".lrc")
            lrc = local_lrc if local_lrc.exists() else None
            if lrc is None:
                candidates = by_stem.get(path.stem.casefold(), [])
                if candidates:
                    lrc = candidates[0]
            info["lyrics"] = _parse_lrc(lrc) if lrc else []
            key = self._track_key(path)
            info["favorite"] = bool(self.library_state.get("favorites", {}).get(key, False))
            info["category"] = str(self.library_state.get("categories", {}).get(key, ""))
            tracks.append(info)

        self.local_tracks = tracks
        self.tracks = self.online_tracks if self.source_kind == "online" else self.local_tracks
        self._rebuild_playlist()
        self.scan_status.setText(TXT(
            f"本地 {len(tracks)} 首 · 塞壬唱片 {len(self.online_tracks)} 首",
            f"Local {len(tracks)} · Monster Siren {len(self.online_tracks)}"
        ))

        match = next(
            (i for i, track in enumerate(self.tracks)
             if self._track_key(track["path"]) == previously_selected),
            None,
        )
        if self.tracks:
            self.select_track(match if match is not None else 0, autoplay=False)
        else:
            self.current_index = -1
            self.current_lyrics = []
            self.hero_cover.clear()
            self.hero_title.setText(TXT("未选择歌曲", "No track selected"))
            self.hero_artist.clear()
            for label in self.detail_values.values():
                label.setText("—")
            self._sync_hero_favorite()
            self.progress.setValue(0)
            self.current_time.setText("0:00")
            self.total_time.setText("0:00")
            self._update_lyric_roller(-1)

    @staticmethod
    def _album_key(track):
        # Album grouping is intentionally based on the Album metadata field, as
        # requested. Whitespace/case differences are normalized.
        album = str(track.get("album", "") or "").strip()
        if not album:
            return ""
        return " ".join(album.split()).casefold()

    @staticmethod
    def _album_track_order(track):
        """Return a stable sortable key from common track-number metadata.

        Handles values such as: 1, 01, 3/12, "Track 4", A1, or missing data.
        Tracks without a usable number are placed after numbered tracks while
        retaining their original library order through the caller's index.
        """
        raw = str(track.get("track", "") or "").strip()
        if not raw:
            return (1, 10**9, raw.casefold())

        # Prefer the first integer because common tags are "3/12".
        m = re.search(r"\d+", raw)
        if m:
            try:
                return (0, int(m.group(0)), raw.casefold())
            except Exception:
                pass

        return (1, 10**9, raw.casefold())

    def _track_visible_in_active_category(self, track):
        category = track.get("category", "")
        favorite = bool(track.get("favorite", False))

        if self.active_category == "__favorites__" and not favorite:
            return False
        if self.active_category == "__uncategorized__" and category:
            return False
        if self.active_category not in (
            "__all__", "__favorites__", "__uncategorized__"
        ):
            if category != self.active_category:
                return False
        return True

    def _search_changed(self):
        self.online_limit = 90
        self._rebuild_playlist()

    def _track_matches_query(self, track, query):
        basic = ' '.join(str(track.get(key, '')) for key in ('title', 'artist', 'album'))
        if query in basic.casefold():
            return True
        if not track.get('online'):
            return False
        normalize = lambda text: re.sub(r'[^\w]+', '', str(text), flags=re.UNICODE).casefold()
        record = self.music_search_index.get(normalize(track.get('title', '')), {})
        album_record = self.music_search_index.get(normalize(track.get('album', '')), {})
        credits = self.prts_cache.get(track.get('cid'), {})
        character = str(record.get('character') or album_record.get('character') or
                        credits.get('character') or '')
        event = str(record.get('event') or album_record.get('event') or
                    credits.get('event') or '')
        kind = str(credits.get('kind') or track.get('kind') or '').lower()
        is_ost = kind == 'ost' or bool(re.search(r'(?:OST|原声带|Original Soundtrack)\s*$',
                                                  track.get('album', ''), re.I))
        term = normalize(query)
        if not term:
            return False
        if character and not is_ost and kind not in ('op', 'ed') and term in normalize(character):
            return True
        if event and (is_ost or not character) and term in normalize(event):
            return True
        return False

    def _rebuild_playlist(self):
        scroll_y = self.playlist_scroll.verticalScrollBar().value() if hasattr(self, "playlist_scroll") else 0
        if hasattr(self, "playlist_scroll"):
            self.playlist_scroll.verticalScrollBar().blockSignals(True)
        self._cover_widgets = {}
        while self.playlist_layout.count() > 1:
            item = self.playlist_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        visible = [
            (i, track)
            for i, track in enumerate(self.tracks)
            if self._track_visible_in_active_category(track)
        ]
        query = self.search_box.text().strip().casefold() if hasattr(self, "search_box") else ""
        if query:
            visible = [(i, track) for i, track in visible if self._track_matches_query(track, query)]
        remaining = 0

        # Gather visible tracks by normalized album metadata.
        album_members = {}
        for i, track in visible:
            key = self._album_key(track)
            if key:
                album_members.setdefault(key, []).append((i, track))

        # Only albums with at least two visible tracks become visual groups.
        grouped_keys = {
            key
            for key, members in album_members.items()
            if len(members) >= 2
        }

        # Preserve the natural library order for groups/singles. The first visible
        # member determines where an album appears in the list.
        rendered_album_keys = set()
        shown = 0
        rendered = 0

        for i, track in visible:
            album_key = self._album_key(track)
            if album_key in rendered_album_keys:
                continue
            if self.source_kind == "online" and rendered >= self.online_limit:
                remaining += 1
                continue

            if album_key in grouped_keys:
                if album_key in rendered_album_keys:
                    continue
                rendered_album_keys.add(album_key)

                members = list(album_members[album_key])
                members.sort(
                    key=lambda pair: (
                        self._album_track_order(pair[1]),
                        pair[0],
                    )
                )

                # "First song" means the first song after album-track sorting.
                first_index, first_track = members[0]
                self._ensure_cover(first_track)
                album_name = str(
                    first_track.get("album", "") or ""
                ).strip()

                header = MusicAlbumHeader(
                    album_name,
                    [member_track for _, member_track in members],
                    self._pixmap_for_track(first_track, 80),
                )
                header.set_expanded(album_key in self.album_expanded)
                if first_track.get("online"):
                    self._cover_widgets.setdefault(first_track["cid"], []).append((header.cover_label, 80, 12))
                self.playlist_layout.insertWidget(
                    self.playlist_layout.count() - 1,
                    header
                )
                body = QWidget()
                body.setMaximumHeight(0)
                body_layout = QVBoxLayout(body)
                body_layout.setContentsMargins(9, 0, 0, 0)
                body_layout.setSpacing(2)
                body.setVisible(False)
                self.playlist_layout.insertWidget(self.playlist_layout.count() - 1, body)
                def toggle_group(_checked=False, key=album_key, widget=body, title=header, group=members):
                    expanded = key not in self.album_expanded
                    if expanded:
                        self.album_expanded.add(key)
                        if widget.layout().count() == 0:
                            for member_index, member_track in group:
                                self._ensure_cover(member_track)
                                row = MusicTrackRow(member_index, member_track, self._pixmap_for_track(member_track, 64))
                                row.clicked.connect(self.select_track)
                                row.favoriteToggled.connect(self._set_track_favorite)
                                row.categoryRequested.connect(self._set_track_category_dialog)
                                widget.layout().addWidget(row)
                                if ACTIVE_THEME == "day":
                                    for visual in (row, *row.findChildren(QWidget)):
                                        recolor_widget(visual)
                                if member_track.get("online"):
                                    self._cover_widgets.setdefault(member_track["cid"], []).append((row.cover_label, 64, 12))
                        widget.setVisible(True)
                        widget.setMaximumHeight(16777215)
                        target_height = widget.layout().sizeHint().height()
                        widget.setMaximumHeight(0)
                    else:
                        self.album_expanded.discard(key)
                        target_height = 0
                    title.set_expanded(expanded)
                    animation = QPropertyAnimation(widget, b"maximumHeight", widget)
                    animation.setDuration(220)
                    animation.setEasingCurve(QEasingCurve.OutCubic)
                    animation.setStartValue(widget.height())
                    animation.setEndValue(target_height)
                    if not expanded:
                        animation.finished.connect(lambda: widget.setVisible(False))
                    widget._height_animation = animation
                    animation.start()
                header.toggled.connect(toggle_group)
                if album_key in self.album_expanded:
                    self.album_expanded.discard(album_key)
                    toggle_group()
                shown += len(members)
                rendered += 1
                continue

            # Albumless songs and albums represented by only one visible track
            # remain normal standalone rows.
            self._ensure_cover(track)
            row = MusicTrackRow(
                i,
                track,
                self._pixmap_for_track(track, 64)
            )
            row.clicked.connect(self.select_track)
            row.favoriteToggled.connect(self._set_track_favorite)
            row.categoryRequested.connect(self._set_track_category_dialog)
            if track.get("online"):
                self._cover_widgets.setdefault(track["cid"], []).append((row.cover_label, 64, 12))
            self.playlist_layout.insertWidget(
                self.playlist_layout.count() - 1,
                row
            )
            shown += 1
            rendered += 1

        if shown == 0:
            empty = QLabel(
                TXT(
                    "这个分类里还没有歌曲",
                    "No tracks in this category"
                )
            )
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color:#606060; padding:24px 4px; border:0;"
            )
            self.playlist_layout.insertWidget(
                self.playlist_layout.count() - 1,
                empty
            )
        if remaining > 0:
            more = FloatingButton(TXT(f"显示更多 · 剩余 {remaining} 首", f"Show more · {remaining} remaining"))
            more.clicked.connect(self._show_more_online)
            self.playlist_layout.insertWidget(self.playlist_layout.count() - 1, more)
        if ACTIVE_THEME == "day":
            for widget in self.playlist_host.findChildren(QWidget):
                recolor_widget(widget)
        # Preserve the viewport as rows are appended. Never queue a delayed
        # scroll update: rapid wheel events could race with an older rebuild.
        bar = self.playlist_scroll.verticalScrollBar()
        self.playlist_host.layout().activate()
        bar.setValue(min(scroll_y, bar.maximum()))
        bar.blockSignals(False)
        self.to_top_btn.setVisible(bar.value() > 250)

    def _show_more_online(self):
        self._more_scheduled = False
        self.online_limit += 55
        self._rebuild_playlist()

    def _placeholder_cover(self):
        pix = QPixmap(160, 160)
        pix.fill(QColor("#e8ecf2" if ACTIVE_THEME == "day" else "#151515"))
        p = QPainter(pix)
        p.setPen(QColor("#708099" if ACTIVE_THEME == "day" else "#555555"))
        f = QFont()
        f.setPointSize(28)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pix.rect(), Qt.AlignCenter, "♪")
        p.end()
        return pix

    def _pixmap_for_track(self, track, size=160):
        data = track.get("cover_bytes")
        if data:
            pix = QPixmap()
            if pix.loadFromData(data):
                return pix.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        return self._placeholder_cover().scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

    def _ensure_cover(self, track):
        if not track.get("online") or track.get("cover_bytes") or not track.get("cover_url"):
            return
        cid = track["cid"]
        cover_path = ARTWORK_DIR / (cid + ".img")
        if cover_path.exists():
            track["cover_bytes"] = cover_path.read_bytes()
        elif cid not in self._cover_pending:
            self._cover_pending.add(cid)
            self._submit("cover:" + cid, siren.fetch_bytes, track["cover_url"], 5 * 1024 * 1024)

    def _maybe_load_more(self, value):
        self.to_top_btn.setVisible(value > 250)
        if self.source_kind != "online" or self._more_scheduled:
            return
        scrollbar = self.playlist_scroll.verticalScrollBar()
        if scrollbar.maximum() > 0 and value >= scrollbar.maximum() - 180 and self.online_limit < len(self.tracks):
            self._more_scheduled = True
            QTimer.singleShot(100, self._show_more_online)


    def _sync_hero_favorite(self):
        if 0 <= self.current_index < len(self.tracks):
            favorite = bool(self.tracks[self.current_index].get("favorite", False))
        else:
            favorite = False
        self.hero_favorite_btn.setVisible(0 <= self.current_index < len(self.tracks))
        self.hero_favorite_btn.blockSignals(True)
        self.hero_favorite_btn.setChecked(favorite)
        self.hero_favorite_btn.update()
        self.hero_favorite_btn.blockSignals(False)

    def _hero_favorite_toggled(self, checked):
        if 0 <= self.current_index < len(self.tracks):
            self._set_track_favorite(self.current_index, bool(checked))
            self._sync_hero_favorite()

    def select_track(self, index, autoplay=True):
        if index < 0 or index >= len(self.tracks):
            return
        self._selection_token += 1
        self._pending_save = False
        self._pending_metadata_save = None
        self.current_index = index
        track = self.tracks[index]
        msr_local = ('Monster Siren' in str(track.get('path', '')) or
                     'Monster Siren' in self._track_key(track.get('path', '')) or
                     '塞壬唱片' in str(track.get('artist', '')))
        artist_parts = [part.strip() for part in re.split(r'[,，、]', track.get('artist') or '')
                        if part.strip() and part.strip().casefold() not in
                        ('塞壬唱片-msr', '塞壬唱片', 'monster siren records')]
        track['artist'] = ', '.join(artist_parts)
        if track.get('online') or msr_local:
            self._request_credits(track)
        self.current_lyrics = track.get("lyrics", [])
        self.current_lyric_index = -1
        self.player.stop()
        self.player.setSource(QUrl())
        if track.get("online") and autoplay:
            self.play_btn.setEnabled(False)
            self.scan_status.setText(TXT("正在获取歌曲并缓存…", "Fetching song into temporary cache…"))
            self._submit("audio:" + str(self._selection_token) + ":" + track["cid"], self._online_job, dict(track))
        elif not track.get("online"):
            self.play_btn.setEnabled(True)
            self.player.setSource(QUrl.fromLocalFile(str(track["path"])))

        self.detail_values["Title"].setText(track["title"] or "—")
        self.detail_values["Artist"].setText(track["artist"] or "—")
        self.detail_values["Album"].setText(track["album"] or "—")
        self.detail_values["Track"].setText(track["track"] or "—")
        self.detail_values["Year"].setText(track["year"] or "—")
        self.detail_values["Cover"].setText(track["cover_desc"] or "—")

        self.hero_cover.setPixmap(_rounded_cover(self._pixmap_for_track(track, 112), 112, 17))
        self.hero_title.setText(track["title"])
        self.hero_artist.setText(track["artist"])
        self._sync_hero_favorite()
        self.total_time.setText(_format_ms(track["duration_ms"]))
        self.progress.setValue(0)
        self.current_time.setText("0:00")
        self._update_lyric_roller(-1)
        self._show_track_details(track)
        self.save_online_btn.setVisible(bool(track.get("online")))
        if autoplay and not track.get("online"):
            self.player.play()

    def _toggle_play(self):
        if self.current_index < 0 and self.tracks:
            self.select_track(0)
        if self.current_index < 0:
            return
        if self.tracks[self.current_index].get("online") and not self.player.source().isValid():
            self.select_track(self.current_index)
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _play_state_changed(self, state):
        self.play_btn.setText("Ⅱ" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _playback_mode_label(self):
        labels = {
            "sequence": TXT("顺序播放", "Sequential playback"),
            "random": TXT("随机播放", "Shuffle"),
            "repeat_one": TXT("单曲循环", "Repeat one"),
        }
        return labels.get(self.playback_mode, labels["sequence"])

    def _sync_playback_mode_button(self):
        # Icon-only UI. The name appears only after hovering for one second.
        icons = {
            "sequence": "⇥",
            "random": "⤨",
            "repeat_one": "↻¹",
        }
        self.play_mode_btn.setText(icons.get(self.playback_mode, "⇥"))
        self.play_mode_btn.setDelayedToolTip(self._playback_mode_label())
        self.play_mode_btn.setAccessibleName(self._playback_mode_label())

    def _cycle_playback_mode(self):
        try:
            i = self.playback_modes.index(self.playback_mode)
        except ValueError:
            i = 0
        self.playback_mode = self.playback_modes[(i + 1) % len(self.playback_modes)]
        self.settings_store.setValue("playback_mode", self.playback_mode)
        self._sync_playback_mode_button()

    def _random_track_index(self):
        if not self.tracks:
            return -1
        if len(self.tracks) == 1:
            return 0
        choices = [i for i in range(len(self.tracks)) if i != self.current_index]
        return random.choice(choices)

    def _advance_after_end(self):
        if not self.tracks or self.current_index < 0:
            return

        if self.playback_mode == "repeat_one":
            # Reuse the same loaded source and restart from the beginning.
            self.player.setPosition(0)
            self.player.play()
            return

        if self.playback_mode == "random":
            nxt = self._random_track_index()
        else:
            nxt = (self.current_index + 1) % len(self.tracks)

        if nxt >= 0:
            self.select_track(nxt)

    def _next(self):
        if not self.tracks:
            return
        if self.playback_mode == "random":
            nxt = self._random_track_index()
        else:
            nxt = (self.current_index + 1) % len(self.tracks)
        if nxt >= 0:
            self.select_track(nxt)

    def _previous(self):
        if not self.tracks:
            return
        # Previous remains deterministic so the user can manually go back even
        # while random playback is enabled.
        prev = (self.current_index - 1) % len(self.tracks)
        self.select_track(prev)

    def _media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._advance_after_end()

    def _duration_changed(self, duration):
        self.total_time.setText(_format_ms(duration))

    def _position_changed(self, pos):
        if not self._seeking:
            duration = self.player.duration()
            if duration > 0:
                self.progress.setValue(int(pos * 1000 / duration))
        self.current_time.setText(_format_ms(pos))

        idx = -1
        for i, (stamp, _) in enumerate(self.current_lyrics):
            if stamp <= pos:
                idx = i
            else:
                break
        if idx != self.current_lyric_index:
            self.current_lyric_index = idx
            self._update_lyric_roller(idx)

    def _seek_started(self):
        self._seeking = True

    def _seek_finished(self):
        self._seeking = False
        duration = self.player.duration()
        if duration > 0:
            self.player.setPosition(int(duration * self.progress.value() / 1000))

    @staticmethod
    def _lyric_slot_text(slot):
        """Return one display string for a bilingual/multilingual lyric slot."""
        if not slot:
            return ""
        if isinstance(slot, str):
            return slot
        return "\n".join(str(line) for line in slot if str(line).strip())

    def _lyric_slot_lines(self, slot):
        if not slot:
            return []
        if isinstance(slot, str):
            return [slot]
        return [str(line) for line in slot if str(line).strip()]

    def _update_lyric_roller(self, current):
        if not self.current_lyrics:
            slot_lines = [[], [], [], [TXT("暂无歌词", "No lyrics available")], [], [], []]
        else:
            slot_lines = []
            for offset in range(-3, 4):
                idx = current + offset
                if current < 0:
                    # Initial state: put the very first lyric in the center slot.
                    # Previous-lyric slots above it remain empty, while upcoming
                    # lyrics can already appear below.
                    idx = offset if offset >= 0 else -1
                if 0 <= idx < len(self.current_lyrics):
                    slot_lines.append(self._lyric_slot_lines(self.current_lyrics[idx][1]))
                else:
                    slot_lines.append([])

        for i, (lbl, lines) in enumerate(zip(self.lyric_labels, slot_lines)):
            value = "\n".join(lines)
            lbl.setText(value)

            # One timestamp = one roller position. If two languages are present,
            # they remain stacked inside this same position instead of consuming
            # two separate lyric rows.
            is_bilingual = len(lines) >= 2
            dist = abs(i - 3)

            if i == 3:
                size = 20 if is_bilingual else 22
                min_h = 72 if is_bilingual else 50
                lbl.setMinimumHeight(min_h)
                lbl.setStyleSheet(
                    f"color:#f0f0f0; font-size:{size}px; font-weight:750; "
                    "background:transparent; border:0;"
                )
            elif dist == 1:
                size = 14 if is_bilingual else 16
                lbl.setMinimumHeight(54 if is_bilingual else 34)
                lbl.setStyleSheet(
                    f"color:#8d8d8d; font-size:{size}px; font-weight:550; "
                    "background:transparent; border:0;"
                )
            elif dist == 2:
                size = 13 if is_bilingual else 14
                lbl.setMinimumHeight(48 if is_bilingual else 34)
                lbl.setStyleSheet(
                    f"color:#565656; font-size:{size}px; "
                    "background:transparent; border:0;"
                )
            else:
                size = 12 if is_bilingual else 13
                lbl.setMinimumHeight(44 if is_bilingual else 34)
                lbl.setStyleSheet(
                    f"color:#343434; font-size:{size}px; "
                    "background:transparent; border:0;"
                )

        current_text = "\n".join(slot_lines[3]) if len(slot_lines) >= 4 else ""
        self.desktop_lyrics.set_lyric(current_text)
        if ACTIVE_THEME == "day":
            for label in self.lyric_labels:
                recolor_widget(label)


    def _toggle_desktop_lyrics(self, enabled):
        if enabled:
            self.desktop_lyrics.show()
            self.desktop_lyrics.raise_()
            self.desktop_lyrics._reposition()
        else:
            if self.desktop_lyrics.editing:
                self._toggle_position_editing()
            self.desktop_lyrics.hide()

    def _save_lyrics_position(self, x, y):
        self.settings_store.setValue("desktop_lyrics_position", {"x": x, "y": y})

    def _toggle_position_editing(self):
        enabled = not self.desktop_lyrics.editing
        self.desktop_lyrics.set_position_editing(enabled)
        self.position_btn.setText(TXT("保存歌词位置", "Save Lyric Position") if enabled
                                  else TXT("拖动歌词位置", "Move Lyric Position"))
        if not enabled and not self.desktop_btn.isChecked():
            self.desktop_lyrics.hide()

    def _reset_lyrics_position(self):
        self.desktop_lyrics.reset_position()
        self.settings_store.setValue("desktop_lyrics_position", None)

    def _toggle_settings(self, enabled):
        # Return to the page the user came from instead of always jumping to the
        # player. This keeps Home and Playground navigation consistent.
        if enabled:
            current = self.main_stack.currentIndex()
            if current != 1:
                self._page_before_settings = current
            self.main_stack.setCurrentIndex(1)
        else:
            target = getattr(self, '_page_before_settings', 2)
            if target == 1 or target < 0 or target >= self.main_stack.count():
                target = 2
            self.main_stack.setCurrentIndex(target)
        self.settings_btn.setText(
            TXT("返回", "Back") if enabled else TXT("音乐设置", "Music Settings")
        )

    def _open_music_folder(self):
        self.library_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library_dir.resolve())))

    def _open_preference_folder(self):
        PREFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(PREFERENCE_DIR.resolve())))

    def _set_text_color_controls(self, color):
        if not color.isValid():
            return
        widgets = (self.rgb_r, self.rgb_g, self.rgb_b)
        for widget in widgets:
            widget.blockSignals(True)
        self.text_color_edit.blockSignals(True)
        self.rgb_r.setValue(color.red())
        self.rgb_g.setValue(color.green())
        self.rgb_b.setValue(color.blue())
        self.text_color_edit.setText(color.name())
        self.text_color_edit.blockSignals(False)
        for widget in widgets:
            widget.blockSignals(False)

    def _rgb_changed(self, *args):
        color = QColor(self.rgb_r.value(), self.rgb_g.value(), self.rgb_b.value())
        self.text_color_edit.blockSignals(True)
        self.text_color_edit.setText(color.name())
        self.text_color_edit.blockSignals(False)
        self._customization_changed()

    def _hex_color_changed(self):
        color = QColor(self.text_color_edit.text())
        if not color.isValid():
            color = QColor("#ffffff")
        self._set_text_color_controls(color)
        self._customization_changed()

    def _pick_color(self, initial, chinese_title, english_title):
        if not initial.isValid():
            initial = QColor("#ffffff")
        dialog = QColorDialog(initial, self)
        dialog.setOption(QColorDialog.DontUseNativeDialog, True)
        dialog.setWindowTitle(TXT(chinese_title, english_title))
        light = ACTIVE_THEME == "day"
        background = "#f3f5f8" if light else "#15171c"
        field = "#ffffff" if light else "#22252d"
        foreground = "#253147" if light else "#edf0f7"
        border = "#b8c4d4" if light else "#515b6c"
        hover = "#e5ebf5" if light else "#343d4d"
        palette = dialog.palette()
        palette.setColor(QPalette.Window, QColor(background))
        palette.setColor(QPalette.WindowText, QColor(foreground))
        palette.setColor(QPalette.Text, QColor(foreground))
        palette.setColor(QPalette.Base, QColor(field))
        palette.setColor(QPalette.Button, QColor(field))
        palette.setColor(QPalette.ButtonText, QColor(foreground))
        dialog.setPalette(palette)
        dialog.setStyleSheet(f"""
            QColorDialog {{ background:{background}; color:{foreground}; }}
            QColorDialog QLabel {{ color:{foreground}; background:transparent; }}
            QColorDialog QLineEdit, QColorDialog QSpinBox {{
                color:{foreground}; background:{field}; border:1px solid {border};
                border-radius:7px; padding:5px 8px;
            }}
            QColorDialog QPushButton {{
                color:{foreground}; background:{field}; border:1px solid {border};
                border-radius:9px; padding:7px 12px;
            }}
            QColorDialog QPushButton:hover {{ background:{hover}; border-color:{foreground}; }}
        """)
        # Replace the tiny platform arrows while retaining editing, scrolling,
        # and press-and-hold stepping in the dialog's own numeric fields.
        for spin in dialog.findChildren(QSpinBox):
            spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
            spin.setMinimumWidth(91)
            spin.setStyleSheet("QSpinBox { padding-right: 29px; }")
            spin._velia_arrows = DialogSpinControls(spin)
        if APP_SETTINGS["language"] == "zh":
            translations = {
                "Basic colors": "基础颜色", "Custom colors": "自定义颜色",
                "Hue:": "色相：", "Sat:": "饱和度：", "Val:": "亮度：",
                "Red:": "红色：", "Green:": "绿色：", "Blue:": "蓝色：",
                "Alpha channel:": "透明度：", "HTML:": "十六进制：",
                "Pick Screen Color": "从屏幕取色", "Add to Custom Colors": "加入自定义颜色",
                "OK": "确定", "Cancel": "取消",
            }
            for widget in [*dialog.findChildren(QLabel), *dialog.findChildren(QPushButton)]:
                key = widget.text().replace("&", "").strip()
                if key in translations:
                    widget.setText(translations[key])
        return dialog.selectedColor() if dialog.exec() == QDialog.Accepted else QColor()

    def _choose_text_color(self):
        initial = QColor(self.text_color_edit.text())
        color = self._pick_color(initial, "选择歌词颜色", "Select Lyric Color")
        if color.isValid():
            self._set_text_color_controls(color)
            self._customization_changed()

    def _choose_bg_color(self):
        initial = QColor(self.bg_color_edit.text())
        color = self._pick_color(initial if initial.isValid() else QColor("#000000"),
                                 "选择歌词背景颜色", "Select Lyric Background Color")
        if color.isValid():
            self.bg_color_edit.setText(color.name())
            self._customization_changed()

    def _load_customization(self):
        family = self.settings_store.value("font_family", QApplication.font().family())
        size = int(self.settings_store.value("font_size", 30))
        weight = int(self.settings_store.value("font_weight", 700))
        text_color = str(self.settings_store.value("text_color", "#ffffff"))
        rainbow = str(self.settings_store.value("rainbow", "false")).lower() == "true"
        bg_enabled = str(self.settings_store.value("background_enabled", "false")).lower() == "true"
        bg_color = str(self.settings_store.value("background_color", "#000000"))

        self.font_combo.setCurrentFont(QFont(family))
        self.font_size_spin.setValue(size)
        idx = self.weight_combo.findData(weight)
        self.weight_combo.setCurrentIndex(max(0, idx))
        self._set_text_color_controls(QColor(text_color))
        self.rainbow_check.setChecked(rainbow)
        self.bg_enable.setChecked(bg_enabled)
        self.bg_color_edit.setText(bg_color)
        self._customization_changed()

    def _customization_changed(self, *args):
        color = QColor(self.text_color_edit.text())
        text_color = color.name() if color.isValid() else "#ffffff"
        bg = QColor(self.bg_color_edit.text())
        bg_color = bg.name() if bg.isValid() else "#000000"

        settings = {
            "font_family": self.font_combo.currentFont().family(),
            "font_size": self.font_size_spin.value(),
            "font_weight": int(self.weight_combo.currentData() or 400),
            "text_color": text_color,
            "rainbow": self.rainbow_check.isChecked(),
            "background_enabled": self.bg_enable.isChecked(),
            "background_color": bg_color,
        }

        self.settings_store.setValue("font_family", settings["font_family"])
        self.settings_store.setValue("font_size", settings["font_size"])
        self.settings_store.setValue("font_weight", settings["font_weight"])
        self.settings_store.setValue("text_color", settings["text_color"])
        self.settings_store.setValue("rainbow", settings["rainbow"])
        self.settings_store.setValue("background_enabled", settings["background_enabled"])
        self.settings_store.setValue("background_color", settings["background_color"])
        self.desktop_lyrics.apply_settings(settings)

STYLE = """
* { outline: none; }
QMainWindow, QWidget#root { background: #070707; }
QWidget {
    color: #d9d9d9;
    background: transparent;
    font-family: "Segoe UI", "Microsoft YaHei UI", Arial, sans-serif;
    font-size: 13px;
}
QLabel#pageTitle { color: #f1f1f1; font-size: 29px; font-weight: 650; }
QPushButton#chipButton, QPushButton {
    color: #b7b7bb;
    background: #141416;
    border: 1px solid #303033;
    border-radius: 13px;
    min-height: 28px;
    padding: 3px 12px;
}
QPushButton:hover, QPushButton#chipButton:hover {
    color: #f9f9fd;
    background: #222329;
    border-color: #737887;
}
QPushButton:pressed, QPushButton#chipButton:pressed {
    background: #303139;
    border-color: #a0a5b4;
}
QPushButton:checked, QPushButton#chipButton:checked {
    background: #28292f;
    color: #f2f2f4;
    border-color: #777b87;
}
QPushButton:disabled { color: #555; background: #111; border-color: #262626; }
QComboBox, QSpinBox, QLineEdit, QListWidget {
    color: #e1e1e4;
    background: #141416;
    border: 1px solid #303035;
    border-radius: 11px;
    padding: 5px 9px;
    selection-background-color: #424550;
}
QSpinBox { padding-right: 34px; }
QComboBox:hover, QSpinBox:hover, QLineEdit:hover { border-color: #656976; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
QComboBox QAbstractItemView { background: #171719; color: #eee; selection-background-color: #40434d; }
QCheckBox { spacing: 9px; color: #d9d9d9; }
QCheckBox::indicator { width: 21px; height: 21px; border: 0; background: transparent; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 3px 0; }
QScrollBar::handle:vertical { background: #34353a; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSlider::groove:horizontal { background: #303034; height: 5px; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #a9abb5; border-radius: 3px; }
QSlider::handle:horizontal { background: #e7e8ef; width: 15px; height: 15px; margin: -5px 0; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: #fff; }
QDialog, QMessageBox, QInputDialog {
    background: #0d0d0d;
    color: #e1e1e4;
}
QDialog QLabel, QMessageBox QLabel, QInputDialog QLabel { color: #d9d9d9; }
QMenu {
    background: #141416;
    color: #e1e1e4;
    border: 1px solid #303035;
    border-radius: 9px;
    padding: 5px;
}
QMenu::item { padding: 7px 18px; border-radius: 5px; }
QMenu::item:selected { background: #424550; }
QToolTip { color: #eee; background: #1b1b1f; border: 1px solid #444; border-radius: 7px; padding: 5px; }
"""


class MusicWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        language = LocalPreferences(SETTINGS_FILE).value("language", "zh")
        self.setWindowTitle("Velia Music Player" if language == "en" else "Velia音乐播放器")
        self.resize(1380, 850)
        self.setMinimumSize(1070, 710)
        self.page = MusicPage(self)
        self.page.setObjectName("root")
        self.setCentralWidget(self.page)

    def closeEvent(self, event):
        self.page.desktop_lyrics.close()
        self.page.player.stop()
        self.page.player.setSource(QUrl())
        self.page.executor.shutdown(wait=False, cancel_futures=True)
        self.page.media_executor.shutdown(wait=False, cancel_futures=True)
        self.page.metadata_executor.shutdown(wait=False, cancel_futures=True)
        def clean_after_network():
            self.page.executor.shutdown(wait=True)
            self.page.media_executor.shutdown(wait=True)
            self.page.metadata_executor.shutdown(wait=True)
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
        threading.Thread(target=clean_after_network, name="velia-cache-cleanup", daemon=False).start()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Velia Music Player")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MusicWindow()
    hints = app.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(window.page._system_theme_changed)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
