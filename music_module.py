"""Velia desktop music player. Run via run.bat from the project root.

The local directory holds music, preferences and the disposable ZIP cache.
"""

import hashlib
import json
import math
import random
import re
import shutil
import sys
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import monster_siren_client as siren

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, Property, QPropertyAnimation, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QFontComboBox, QFrame, QGraphicsDropShadowEffect,
    QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QSpinBox, QStackedWidget, QStyle, QStyleOptionButton,
    QToolTip, QVBoxLayout,
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
SETTINGS_FILE = PREFERENCE_DIR / "settings.json"
LIBRARY_STATE_FILE = PREFERENCE_DIR / "library_state.json"
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


class MusicTrackRow(QFrame):
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
        self.setStyleSheet("""
            QFrame#musicTrackRow {
                background: transparent;
                border: 0;
                border-radius: 16px;
            }
            QFrame#musicTrackRow:hover {
                background: rgba(255,255,255,0.075);
            }
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 9, 10, 9)
        lay.setSpacing(10)

        cover = QLabel()
        cover.setFixedSize(48, 48)
        cover.setPixmap(_rounded_cover(cover_pixmap, 48, 12))
        cover.setStyleSheet("border-radius: 12px;")
        lay.addWidget(cover)

        words = QVBoxLayout()
        words.setSpacing(1)
        title = QLabel(track["title"])
        title.setStyleSheet("font-size: 14px; font-weight: 650; color: #ededed;")
        artist = QLabel(track["artist"])
        artist.setStyleSheet("font-size: 12px; color: #919191;")
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


class MusicAlbumHeader(QFrame):
    """Visual header for a group of tracks sharing the same album metadata."""

    def __init__(self, album_name, tracks, cover_pixmap, parent=None):
        super().__init__(parent)
        self.setObjectName("musicAlbumHeader")
        self.setStyleSheet("""
            QFrame#musicAlbumHeader {
                background: rgba(255,255,255,0.030);
                border: 1px solid rgba(255,255,255,0.055);
                border-radius: 18px;
                margin-top: 8px;
            }
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 12, 10)
        lay.setSpacing(10)

        cover = QLabel()
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
            f"color:{color}; background:{bg_rgba}; border-radius:18px; padding:8px 16px;"
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
        height = 92
        self.resize(width, height)
        if saved:
            x = max(geo.x(), min(x, geo.right() - width + 1))
            y = max(geo.y(), min(y, geo.bottom() - height + 1))
        else:
            x = geo.x() + (geo.width() - width) // 2
            y = geo.y() + geo.height() - height - 34
        self.move(x, y)


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
        self.online_limit = 90
        self.network = NetworkResults(self)
        self.network.finished.connect(self._network_done)
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="velia-artwork")
        self.media_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="velia-audio")
        self._selection_token = 0
        self._pending_save = False
        self._cover_pending = set()
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
        QTimer.singleShot(0, self.reload_library)
        QTimer.singleShot(100, self._load_online_catalogue)

    def _submit(self, kind, fn, *args):
        priority = kind.startswith(("audio:", "catalogue", "wiki:"))
        future = (self.media_executor if priority else self.executor).submit(fn, *args)
        def deliver(done):
            try:
                self.network.finished.emit(kind, done.result(), None)
            except Exception as exc:
                self.network.finished.emit(kind, None, str(exc))
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

    def _install_online_catalogue(self, songs):
        previously = (self.tracks[self.current_index].get("cid")
                      if self.source_kind == "online" and 0 <= self.current_index < len(self.tracks) else None)
        self.online_tracks = []
        for s in songs:
            cid = str(s.get("cid", ""))
            if not cid.isdigit():
                continue
            key = "siren/" + cid
            self.online_tracks.append({
                "online": True, "cid": cid, "path": key,
                "title": s.get("title", "—"), "artist": s.get("artist", "—"),
                "album": s.get("album", ""), "album_cid": s.get("album_cid", ""),
                "cover_url": s.get("cover_url", ""), "cover_bytes": None,
                "cover_desc": TXT("塞壬唱片", "Monster Siren"), "track": "", "year": "",
                "duration_ms": 0, "lyrics": [],
                "favorite": bool(self.library_state.get("favorites", {}).get(key)),
                "category": str(self.library_state.get("categories", {}).get(key, "")),
            })
        if self.source_kind == "online":
            self.tracks = self.online_tracks
            self.current_index = -1
            self._rebuild_playlist()
            self.scan_status.setText(TXT(f"塞壬唱片 · {len(self.tracks)} 首", f"Monster Siren · {len(self.tracks)} tracks"))
            if self.tracks:
                target = next((i for i, t in enumerate(self.tracks) if t["cid"] == previously), 0)
                self.select_track(target, autoplay=False)

    def _network_done(self, kind, value, error):
        if kind == "catalogue":
            if error:
                if not self.online_tracks:
                    self.scan_status.setText(TXT("在线曲库暂不可用 · 请检查网络", "Online catalogue unavailable · check connection"))
                return
            _write_json(PREFERENCE_DIR / "siren_catalogue.json", value)
            if [t["cid"] for t in self.online_tracks] != [str(t.get("cid")) for t in value]:
                self._install_online_catalogue(value)
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
                if self.source_kind == "online" and not getattr(self, "_cover_refresh_scheduled", False):
                    self._cover_refresh_scheduled = True
                    QTimer.singleShot(240, self._refresh_cover_rows)
        elif kind.startswith("audio:"):
            _, token, cid = kind.split(":")
            if str(self._selection_token) != token or self.current_index < 0 or self.tracks[self.current_index].get("cid") != cid:
                return
            self.play_btn.setEnabled(True)
            if error:
                self.scan_status.setText(TXT("下载失败：", "Download failed: ") + error[:120])
                return
            track = self.tracks[self.current_index]
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

    def _refresh_cover_rows(self):
        self._cover_refresh_scheduled = False
        if self.source_kind == "online":
            self._rebuild_playlist()

    def _switch_source(self, index):
        self._selection_token += 1
        self.player.stop()
        self.player.setSource(QUrl())
        self.source_kind = self.source_combo.itemData(index)
        self.tracks = self.online_tracks if self.source_kind == "online" else self.local_tracks
        self.current_index = -1
        self.online_limit = 90
        self._rebuild_playlist()
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
            self._pending_save = True
            self.scan_status.setText(TXT("下载完成后保存到本地…", "Saving after download…"))
            return
        self._pending_save = False
        folder = MUSIC_LIBRARY_DIR / "Monster Siren"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (track["cid"] + " - " + re.sub(r'[<>:"/\\|?*]', "_", track["title"])[:90] + source.suffix)
        shutil.copy2(source, destination)
        for ext in (".lrc",):
            lyric_path = source.with_suffix(ext)
            if lyric_path.exists():
                shutil.copy2(lyric_path, destination.with_suffix(ext))
        cover = track.get("cover_bytes")
        if cover:
            destination.with_suffix(".jpg").write_bytes(cover)
        (destination.with_suffix(".json")).write_text(json.dumps({
            "source": siren.ROOT + "/music/song/" + track["cid"],
            "title": track["title"], "artist": track["artist"],
            "album": track["album"], "intro": track.get("intro", ""),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.scan_status.setText(TXT("已保留到本地音乐文件夹", "Saved to local music folder"))

    def _show_track_details(self, track):
        for key, name in (("Album", "album"), ("Track", "track"), ("Year", "year"), ("Cover", "cover_desc")):
            self.detail_values[key].setText(str(track.get(name) or "—"))
        intro = track.get("intro") or ""
        self.album_intro.setText(re.sub(r"<[^>]+>", "", intro)[:500])
        self.album_intro.setToolTip(track.get("intro_source") or
                                    (siren.ROOT + "/" if track.get("online") else ""))
        self.album_intro.setVisible(bool(intro))

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
        self.search_box.setPlaceholderText(TXT("搜索歌曲或专辑…", "Search tracks or albums…"))
        self.save_online_btn.setText(TXT("保留到本地", "Keep Locally"))
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
        self.window().setWindowTitle(TXT("Velia · 音乐播放器", "Velia · Music Player"))
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
        QApplication.instance().setStyleSheet(theme_css(STYLE))
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
        try:
            return str(Path(path).resolve().relative_to(self.library_dir.resolve())).replace("\\", "/")
        except Exception:
            return str(Path(path).resolve()).replace("\\", "/")

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
        title = self._localized_label("音乐播放器", "Music Player")
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
        self.search_box.setPlaceholderText(TXT("搜索歌曲或专辑…", "Search tracks or albums…"))
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
        ll.addWidget(self.playlist_scroll, 1)
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

        self.save_online_btn = FloatingButton(TXT("保留到本地", "Keep Locally"))
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
        grid.addWidget(self.open_folder_btn, 3, 0, 1, 2)

        self.open_preference_btn = FloatingButton(TXT("打开偏好设置文件夹", "Open Preferences Folder"))
        self.open_preference_btn.setObjectName("chipButton")
        self.open_preference_btn.clicked.connect(self._open_preference_folder)
        grid.addWidget(self.open_preference_btn, 4, 0, 1, 2)

        note = self._localized_label(
            "歌曲和个人偏好保存在项目下的 local 文件夹中。",
            "Music and preferences are stored in this project's local folder."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#787878; font-size:12px;")
        grid.addWidget(note, 5, 0, 1, 2)
        grid.setRowStretch(6, 1)
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
            cover_file = path.with_suffix(".jpg")
            if cover_file.is_file():
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

    def _rebuild_playlist(self):
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
            visible = [(i, track) for i, track in visible if query in
                       (track["title"] + " " + track["artist"] + " " + track["album"]).casefold()]
        remaining = len(visible) - self.online_limit if self.source_kind == "online" else 0
        if self.source_kind == "online":
            visible = visible[:self.online_limit]
            for _, track in visible:
                cid = track["cid"]
                if track.get("cover_bytes") or not track.get("cover_url") or cid in self._cover_pending:
                    continue
                cover_path = ARTWORK_DIR / (cid + ".img")
                if cover_path.exists():
                    track["cover_bytes"] = cover_path.read_bytes()
                else:
                    self._cover_pending.add(cid)
                    self._submit("cover:" + cid, siren.fetch_bytes, track["cover_url"], 5 * 1024 * 1024)

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

        for i, track in visible:
            album_key = self._album_key(track)

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
                album_name = str(
                    first_track.get("album", "") or ""
                ).strip()

                header = MusicAlbumHeader(
                    album_name,
                    [member_track for _, member_track in members],
                    self._pixmap_for_track(first_track, 80),
                )
                self.playlist_layout.insertWidget(
                    self.playlist_layout.count() - 1,
                    header
                )

                for member_index, member_track in members:
                    row = MusicTrackRow(
                        member_index,
                        member_track,
                        self._pixmap_for_track(member_track, 64)
                    )
                    row.setContentsMargins(10, 0, 0, 0)
                    row.clicked.connect(self.select_track)
                    row.favoriteToggled.connect(self._set_track_favorite)
                    row.categoryRequested.connect(
                        self._set_track_category_dialog
                    )
                    self.playlist_layout.insertWidget(
                        self.playlist_layout.count() - 1,
                        row
                    )
                    shown += 1
                continue

            # Albumless songs and albums represented by only one visible track
            # remain normal standalone rows.
            row = MusicTrackRow(
                i,
                track,
                self._pixmap_for_track(track, 64)
            )
            row.clicked.connect(self.select_track)
            row.favoriteToggled.connect(self._set_track_favorite)
            row.categoryRequested.connect(self._set_track_category_dialog)
            self.playlist_layout.insertWidget(
                self.playlist_layout.count() - 1,
                row
            )
            shown += 1

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

    def _show_more_online(self):
        self.online_limit += 90
        self._rebuild_playlist()


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
        self.current_index = index
        track = self.tracks[index]
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
        self.main_stack.setCurrentIndex(1 if enabled else 0)
        self.settings_btn.setText(
            TXT("返回播放器", "Back to Player") if enabled else TXT("音乐设置", "Music Settings")
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
        self.setWindowTitle("Velia · Music Player" if language == "en" else "Velia · 音乐播放器")
        self.resize(1380, 850)
        self.setMinimumSize(1070, 710)
        self.page = MusicPage(self)
        self.page.setObjectName("root")
        self.setCentralWidget(self.page)

    def closeEvent(self, event):
        self.page.desktop_lyrics.close()
        self.page.player.stop()
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
