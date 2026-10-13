"""10 m × 10 m Playground room renderer and PNG wallpaper catalogue.

Place optional PNG backgrounds in resource/rm. No avatar or chat dependencies.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QFileSystemWatcher
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QWidget

ROOM_AREA_M2 = 100
ROOM_SIDE_M = 10
ROOM_WALL_HEIGHT_M = 3
ROOM_IMAGES = Path(__file__).resolve().parent / 'resource' / 'rm'


def wallpapers():
    """Return (file_name, display_name) for every PNG in resource/rm."""
    ROOM_IMAGES.mkdir(parents=True, exist_ok=True)
    paths = sorted((p for p in ROOM_IMAGES.iterdir()
                    if p.is_file() and p.suffix.lower() == '.png'),
                   key=lambda p: p.name.casefold())
    return [(p.name, p.stem.rsplit('$$', 1)[-1] if '$$' in p.stem else p.stem)
            for p in paths]


class RoomView(QWidget):
    """HiDPI-aware empty room; perspective 10×10 m floor with 1 m grid."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = "light"
        self.wallpaper_name = None
        self._cached_path = None
        self._cached_pixmap = QPixmap()
        self.setMinimumSize(480, 320)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def set_wallpaper(self, filename):
        self.wallpaper_name = filename
        self._cached_path = None
        self.update()

    def _wallpaper(self):
        if not self.wallpaper_name:
            return QPixmap()
        path = ROOM_IMAGES / self.wallpaper_name
        # Deny paths outside resource/rm, including accidental absolute paths.
        if path.parent != ROOM_IMAGES or path.suffix.lower() != '.png' or not path.is_file():
            return QPixmap()
        stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if stamp != self._cached_path:
            self._cached_path = stamp
            self._cached_pixmap = QPixmap(str(path))
        return self._cached_pixmap

    @staticmethod
    def _depth_fraction(metres):
        """Perspective placement of a measured 0..10 m distance from back to front.

        Uses gentle perspective instead of quadratic clustering near the back.
        """
        t = max(0.0, min(1.0, metres / ROOM_SIDE_M))
        return t ** 1.15

    @staticmethod
    def _interpolate(a, b, fraction):
        return QPointF(a.x() + (b.x()-a.x())*fraction,
                       a.y() + (b.y()-a.y())*fraction)

    def paintEvent(self, event):
        w, h = float(self.width()), float(self.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor('#15171b' if self.theme == 'dark' else '#e8eaed'))
        pixmap = self._wallpaper()
        if not pixmap.isNull():
            scaled = pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            ox, oy = (scaled.width()-self.width())//2, (scaled.height()-self.height())//2
            painter.drawPixmap(0, 0, scaled, ox, oy, self.width(), self.height())
        else:
            # One uniform canvas: the old half-height fill left an
            # unintended translucent-looking horizontal band below the room.
            painter.fillRect(self.rect(), QColor('#242831' if self.theme == 'dark' else '#f2f3f5'))

        # Inset geometry: back wall and a 10 m × 10 m floor seen in perspective.
        cx = w * .5
        back_half = min(w * .315, h * .49)
        front_half = min(w * .466, h * .90)
        back_y = h * .48
        floor_y = h * .925
        upper_y = h * .11
        back_left = QPointF(cx-back_half, back_y)
        back_right = QPointF(cx+back_half, back_y)
        front_left = QPointF(cx-front_half, floor_y)
        front_right = QPointF(cx+front_half, floor_y)
        wall_tl = QPointF(cx-back_half, upper_y)
        wall_tr = QPointF(cx+back_half, upper_y)

        # Four-sided room: floor, back wall and two side walls, sharing edges.
        outer_left_top = QPointF(front_left.x(), upper_y)
        outer_right_top = QPointF(front_right.x(), upper_y)
        left_wall = [outer_left_top, wall_tl, back_left, front_left]
        right_wall = [wall_tr, outer_right_top, front_right, back_right]
        floor = [back_left, back_right, front_right, front_left]
        back_wall = [wall_tl, wall_tr, back_right, back_left]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 27, 36, 38) if self.theme == 'dark' else QColor(15, 20, 30, 20))
        painter.drawPolygon(QPolygonF(left_wall))
        painter.drawPolygon(QPolygonF(right_wall))
        painter.setBrush(QColor(255, 255, 255, 13) if self.theme == 'dark' else QColor(255, 255, 255, 37))
        painter.drawPolygon(QPolygonF(back_wall))
        painter.setBrush(QColor(255, 255, 255, 8) if self.theme == 'dark' else QColor(20, 23, 29, 15))
        painter.drawPolygon(QPolygonF(floor))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(0, 0, 0, 115), 1.1)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        # Floor: ten divisions in both directions, each cell 1 m x 1 m.
        for i in range(ROOM_SIDE_M + 1):
            t = i / ROOM_SIDE_M
            painter.drawLine(self._interpolate(back_left, back_right, t),
                             self._interpolate(front_left, front_right, t))
            depth = self._depth_fraction(i)
            painter.drawLine(self._interpolate(back_left, front_left, depth),
                             self._interpolate(back_right, front_right, depth))
        # Back wall: 10 metres across and 3 metres high; 1 m x 1 m cells.
        for i in range(ROOM_SIDE_M + 1):
            t = i / ROOM_SIDE_M
            painter.drawLine(self._interpolate(wall_tl, wall_tr, t),
                             self._interpolate(back_left, back_right, t))
        for i in range(ROOM_WALL_HEIGHT_M + 1):
            t = i / ROOM_WALL_HEIGHT_M
            painter.drawLine(self._interpolate(wall_tl, back_left, t),
                             self._interpolate(wall_tr, back_right, t))
        # Side walls: matching perspective depth grid and wall-height divisions.
        for side in (left_wall, right_wall):
            top_front, top_back, bottom_back, bottom_front = side
            for i in range(ROOM_SIDE_M + 1):
                # Each division follows one physical metre of wall depth.
                t = self._depth_fraction(i)
                painter.drawLine(self._interpolate(top_back, top_front, t),
                                 self._interpolate(bottom_back, bottom_front, t))
            for i in range(ROOM_WALL_HEIGHT_M + 1):
                t = i / ROOM_WALL_HEIGHT_M
                painter.drawLine(self._interpolate(top_front, bottom_front, t),
                                 self._interpolate(top_back, bottom_back, t))
        painter.drawLine(wall_tl, back_left)
        painter.drawLine(wall_tr, back_right)
        painter.drawLine(back_left, front_left)
        painter.drawLine(back_right, front_right)
        painter.end()
