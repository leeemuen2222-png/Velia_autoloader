"""10 m × 10 m Playground room renderer and PNG wallpaper catalogue.

Place optional PNG backgrounds in resource/rm. No avatar or chat dependencies.
"""
from pathlib import Path

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
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
    """HiDPI-aware empty room with detailed 10×10 1m² floor tiles."""

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
        if path.parent != ROOM_IMAGES or path.suffix.lower() != '.png' or not path.is_file():
            return QPixmap()
        stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if stamp != self._cached_path:
            self._cached_path = stamp
            self._cached_pixmap = QPixmap(str(path))
        return self._cached_pixmap

    @staticmethod
    def _depth_fraction(metres):
        """Perspective placement for measured 0..10 m distance from back to front."""
        t = max(0.0, min(1.0, metres / ROOM_SIDE_M))
        return t ** 1.15

    @staticmethod
    def _interpolate(a, b, fraction):
        return QPointF(a.x() + (b.x() - a.x()) * fraction,
                       a.y() + (b.y() - a.y()) * fraction)

    def _shift_up(self, point, dy):
        return QPointF(point.x(), point.y() - dy)

    def _tile_height_m(self, row, col):
        """Return height step in metres for corner basalt-style raised tiles.

        row=0 is the back row, col=0 is the left-most column.
        """
        left_steps = {
            (0, 0): 0.18,
            (0, 1): 0.12,
            (0, 2): 0.06,
            (1, 0): 0.12,
            (1, 1): 0.06,
        }
        right_steps = {
            (0, 9): 0.18,
            (0, 8): 0.12,
            (0, 7): 0.06,
            (1, 9): 0.12,
            (1, 8): 0.06,
        }
        return left_steps.get((row, col), 0.0) + right_steps.get((row, col), 0.0)

    def _tile_base_gray(self, row, col):
        seed = (row * 17 + col * 29 + row * col * 3) % 9
        return 214 - seed * 4 if self.theme != 'dark' else 66 + seed * 3

    def _draw_tile(self, painter, row, col, grid, pixels_per_m):
        """Draw one floor tile with seams and optional step-lifted faces."""
        base_tl = grid[row][col]
        base_tr = grid[row][col + 1]
        base_bl = grid[row + 1][col]
        base_br = grid[row + 1][col + 1]

        h_m = self._tile_height_m(row, col)
        lift_px = pixels_per_m * h_m
        top_tl = self._shift_up(base_tl, lift_px)
        top_tr = self._shift_up(base_tr, lift_px)
        top_bl = self._shift_up(base_bl, lift_px)
        top_br = self._shift_up(base_br, lift_px)

        # Draw visible risers before the top slab.
        front_neighbor = self._tile_height_m(row + 1, col) if row + 1 < ROOM_SIDE_M else 0.0
        left_neighbor = self._tile_height_m(row, col - 1) if col - 1 >= 0 else 0.0
        right_neighbor = self._tile_height_m(row, col + 1) if col + 1 < ROOM_SIDE_M else 0.0

        # Front exposed face.
        if h_m > front_neighbor:
            lower_l = self._shift_up(base_bl, pixels_per_m * front_neighbor)
            lower_r = self._shift_up(base_br, pixels_per_m * front_neighbor)
            face = QPolygonF([top_bl, top_br, lower_r, lower_l])
            painter.setPen(Qt.PenStyle.NoPen)
            if self.theme == 'dark':
                painter.setBrush(QColor(42, 46, 56, 210))
            else:
                painter.setBrush(QColor(158, 166, 178, 205))
            painter.drawPolygon(face)
            face_pen = QPen(QColor(0, 0, 0, 70 if self.theme == 'dark' else 95), 1.0)
            face_pen.setCosmetic(True)
            painter.setPen(face_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(face)

        # Outer or internal side exposed faces where height steps down.
        if h_m > left_neighbor:
            lower_t = self._shift_up(base_tl, pixels_per_m * left_neighbor)
            lower_b = self._shift_up(base_bl, pixels_per_m * left_neighbor)
            face = QPolygonF([top_tl, top_bl, lower_b, lower_t])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(34, 38, 48, 190) if self.theme == 'dark'
                             else QColor(171, 179, 191, 188))
            painter.drawPolygon(face)
        if h_m > right_neighbor:
            lower_t = self._shift_up(base_tr, pixels_per_m * right_neighbor)
            lower_b = self._shift_up(base_br, pixels_per_m * right_neighbor)
            face = QPolygonF([top_tr, top_br, lower_b, lower_t])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(31, 35, 45, 180) if self.theme == 'dark'
                             else QColor(163, 171, 183, 180))
            painter.drawPolygon(face)

        # Top slab.
        gray = self._tile_base_gray(row, col)
        top_poly = QPolygonF([top_tl, top_tr, top_br, top_bl])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(gray, gray, gray, 255))
        painter.drawPolygon(top_poly)

        # Soft basalt-like corner shading.
        shadow = QPolygonF([
            top_tl,
            QPointF((top_tl.x() + top_tr.x()) * 0.54, (top_tl.y() + top_tr.y()) * 0.54 + top_bl.y() * 0.46),
            QPointF((top_tl.x() + top_bl.x()) * 0.5, (top_tl.y() + top_bl.y()) * 0.5),
        ])
        painter.setBrush(QColor(0, 0, 0, 8 if self.theme == 'dark' else 10))
        painter.drawPolygon(shadow)

        # Seams: dark groove plus thin highlight, inspired by the reference slab joint.
        groove = QPen(QColor(55, 60, 70, 180) if self.theme == 'dark'
                      else QColor(108, 114, 126, 170), 1.15)
        groove.setCosmetic(True)
        groove.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(groove)
        painter.drawLine(top_tr, top_br)
        painter.drawLine(top_bl, top_br)

        highlight = QPen(QColor(255, 255, 255, 56) if self.theme == 'dark'
                         else QColor(255, 255, 255, 155), 0.9)
        highlight.setCosmetic(True)
        painter.setPen(highlight)
        painter.drawLine(top_tl, top_tr)
        painter.drawLine(top_tl, top_bl)

        outline = QPen(QColor(0, 0, 0, 86 if self.theme == 'dark' else 108), 1.0)
        outline.setCosmetic(True)
        painter.setPen(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(top_poly)

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
            ox = (scaled.width() - self.width()) // 2
            oy = (scaled.height() - self.height()) // 2
            painter.drawPixmap(0, 0, scaled, ox, oy, self.width(), self.height())
        else:
            painter.fillRect(self.rect(), QColor('#242831' if self.theme == 'dark' else '#f2f3f5'))

        # Room geometry.
        cx = w * 0.5
        back_half = min(w * 0.315, h * 0.49)
        front_half = min(w * 0.466, h * 0.90)
        back_y = h * 0.48
        floor_y = h * 0.925
        upper_y = h * 0.11
        back_left = QPointF(cx - back_half, back_y)
        back_right = QPointF(cx + back_half, back_y)
        front_left = QPointF(cx - front_half, floor_y)
        front_right = QPointF(cx + front_half, floor_y)
        wall_tl = QPointF(cx - back_half, upper_y)
        wall_tr = QPointF(cx + back_half, upper_y)
        outer_left_top = QPointF(front_left.x(), upper_y)
        outer_right_top = QPointF(front_right.x(), upper_y)

        left_wall = [outer_left_top, wall_tl, back_left, front_left]
        right_wall = [wall_tr, outer_right_top, front_right, back_right]
        back_wall = [wall_tl, wall_tr, back_right, back_left]

        # First pass: wall fills.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 27, 36, 38) if self.theme == 'dark' else QColor(15, 20, 30, 20))
        painter.drawPolygon(QPolygonF(left_wall))
        painter.drawPolygon(QPolygonF(right_wall))
        painter.setBrush(QColor(255, 255, 255, 13) if self.theme == 'dark' else QColor(255, 255, 255, 37))
        painter.drawPolygon(QPolygonF(back_wall))

        # Floor grid points used for per-tile drawing.
        left_edge = [self._interpolate(back_left, front_left, self._depth_fraction(i))
                     for i in range(ROOM_SIDE_M + 1)]
        right_edge = [self._interpolate(back_right, front_right, self._depth_fraction(i))
                      for i in range(ROOM_SIDE_M + 1)]
        floor_grid = []
        for row in range(ROOM_SIDE_M + 1):
            floor_grid.append([
                self._interpolate(left_edge[row], right_edge[row], col / ROOM_SIDE_M)
                for col in range(ROOM_SIDE_M + 1)
            ])

        pixels_per_m = max(2.0, abs(floor_grid[2][0].y() - floor_grid[1][0].y()))

        # Draw tiles from back to front.
        for row in range(ROOM_SIDE_M):
            for col in range(ROOM_SIDE_M):
                self._draw_tile(painter, row, col, floor_grid, pixels_per_m)

        # Wall grids.
        wall_pen = QPen(QColor(0, 0, 0, 118), 1.05)
        wall_pen.setCosmetic(True)
        wall_pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(wall_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        for i in range(ROOM_SIDE_M + 1):
            t = i / ROOM_SIDE_M
            painter.drawLine(self._interpolate(wall_tl, wall_tr, t),
                             self._interpolate(back_left, back_right, t))
        for i in range(ROOM_WALL_HEIGHT_M + 1):
            t = i / ROOM_WALL_HEIGHT_M
            painter.drawLine(self._interpolate(wall_tl, back_left, t),
                             self._interpolate(wall_tr, back_right, t))
        for side in (left_wall, right_wall):
            top_front, top_back, bottom_back, bottom_front = side
            for i in range(ROOM_SIDE_M + 1):
                t = self._depth_fraction(i)
                painter.drawLine(self._interpolate(top_back, top_front, t),
                                 self._interpolate(bottom_back, bottom_front, t))
            for i in range(ROOM_WALL_HEIGHT_M + 1):
                t = i / ROOM_WALL_HEIGHT_M
                painter.drawLine(self._interpolate(top_front, bottom_front, t),
                                 self._interpolate(top_back, bottom_back, t))

        edge_pen = QPen(QColor(0, 0, 0, 138), 1.2)
        edge_pen.setCosmetic(True)
        painter.setPen(edge_pen)
        painter.drawLine(wall_tl, back_left)
        painter.drawLine(wall_tr, back_right)
        painter.drawLine(back_left, front_left)
        painter.drawLine(back_right, front_right)
        painter.drawLine(wall_tl, wall_tr)
        painter.drawLine(front_left, front_right)
        painter.end()
