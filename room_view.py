"""10 m × 10 m Playground room renderer and PNG wallpaper catalogue.

Place optional PNG backgrounds in resource/rm. No avatar or chat dependencies.
"""
from pathlib import Path

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QPainterPath
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

    @staticmethod
    def _shift_up(point, dy):
        return QPointF(point.x(), point.y() - dy)

    def _tile_height_m(self, row, col):
        """Return height step in metres for corner raised square platforms.

        Highest tiles are 60 cm, lowest raised tiles are 25 cm.
        row=0 is the back row, col=0 is the left-most column.
        """
        steps = {
            # left rear cluster
            (0, 0): 0.60,
            (0, 1): 0.45,
            (0, 2): 0.25,
            (1, 0): 0.45,
            (1, 1): 0.25,
            # right rear cluster
            (0, 9): 0.60,
            (0, 8): 0.45,
            (0, 7): 0.25,
            (1, 9): 0.45,
            (1, 8): 0.25,
        }
        return steps.get((row, col), 0.0)

    def _tile_white_level(self, row, col):
        """Keep tiles visually white with only tiny surface variation."""
        seed = (row * 13 + col * 17 + row * col) % 5
        return 246 + (seed - 2)

    def _draw_tile(self, painter, row, col, grid, vertical_px_per_m):
        base_tl = grid[row][col]
        base_tr = grid[row][col + 1]
        base_bl = grid[row + 1][col]
        base_br = grid[row + 1][col + 1]

        h_m = self._tile_height_m(row, col)
        lift_px = vertical_px_per_m * h_m
        top_tl = self._shift_up(base_tl, lift_px)
        top_tr = self._shift_up(base_tr, lift_px)
        top_bl = self._shift_up(base_bl, lift_px)
        top_br = self._shift_up(base_br, lift_px)

        front_neighbor = self._tile_height_m(row + 1, col) if row + 1 < ROOM_SIDE_M else 0.0
        left_neighbor = self._tile_height_m(row, col - 1) if col - 1 >= 0 else 0.0
        right_neighbor = self._tile_height_m(row, col + 1) if col + 1 < ROOM_SIDE_M else 0.0

        # Exposed front face.
        if h_m > front_neighbor:
            lower_l = self._shift_up(base_bl, vertical_px_per_m * front_neighbor)
            lower_r = self._shift_up(base_br, vertical_px_per_m * front_neighbor)
            face = QPolygonF([top_bl, top_br, lower_r, lower_l])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(226, 228, 232, 255) if self.theme != 'dark'
                             else QColor(198, 201, 207, 255))
            painter.drawPolygon(face)
            face_pen = QPen(QColor(0, 0, 0, 78 if self.theme != 'dark' else 70), 1.0)
            face_pen.setCosmetic(True)
            painter.setPen(face_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(face)

        # Exposed side faces.
        if h_m > left_neighbor:
            lower_t = self._shift_up(base_tl, vertical_px_per_m * left_neighbor)
            lower_b = self._shift_up(base_bl, vertical_px_per_m * left_neighbor)
            face = QPolygonF([top_tl, top_bl, lower_b, lower_t])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(232, 234, 238, 255) if self.theme != 'dark'
                             else QColor(202, 205, 211, 255))
            painter.drawPolygon(face)
        if h_m > right_neighbor:
            lower_t = self._shift_up(base_tr, vertical_px_per_m * right_neighbor)
            lower_b = self._shift_up(base_br, vertical_px_per_m * right_neighbor)
            face = QPolygonF([top_tr, top_br, lower_b, lower_t])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(220, 222, 226, 255) if self.theme != 'dark'
                             else QColor(194, 197, 203, 255))
            painter.drawPolygon(face)

        # Top white slab.
        white = self._tile_white_level(row, col)
        top_poly = QPolygonF([top_tl, top_tr, top_br, top_bl])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(white, white, white, 255))
        painter.drawPolygon(top_poly)

        # Very soft local tone only; do not darken overall tile color.
        soft_shadow = QPolygonF([
            top_tl,
            QPointF((top_tl.x() + top_tr.x()) * 0.52,
                    (top_tl.y() + top_tr.y()) * 0.52 + top_bl.y() * 0.48),
            QPointF((top_tl.x() + top_bl.x()) * 0.50,
                    (top_tl.y() + top_bl.y()) * 0.50),
        ])
        painter.setBrush(QColor(0, 0, 0, 7 if self.theme != 'dark' else 10))
        painter.drawPolygon(soft_shadow)

        # Seams: slightly larger and darker while keeping the tile itself white.
        joint_shadow = QPen(QColor(0, 0, 0, 28 if self.theme != 'dark' else 40), 2.3)
        joint_shadow.setCosmetic(True)
        painter.setPen(joint_shadow)
        painter.drawLine(top_tr, top_br)
        painter.drawLine(top_bl, top_br)

        groove = QPen(QColor(122, 128, 138, 190) if self.theme != 'dark'
                      else QColor(85, 92, 103, 205), 1.55)
        groove.setCosmetic(True)
        groove.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(groove)
        painter.drawLine(top_tr, top_br)
        painter.drawLine(top_bl, top_br)

        highlight = QPen(QColor(255, 255, 255, 165) if self.theme != 'dark'
                         else QColor(255, 255, 255, 70), 1.0)
        highlight.setCosmetic(True)
        painter.setPen(highlight)
        painter.drawLine(top_tl, top_tr)
        painter.drawLine(top_tl, top_bl)

        outline = QPen(QColor(95, 101, 110, 132) if self.theme != 'dark'
                       else QColor(130, 136, 147, 120), 1.0)
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
        floor_poly = QPolygonF([back_left, back_right, front_right, front_left])
        room_inner_poly = QPolygonF([
            outer_left_top, wall_tl, wall_tr, outer_right_top, front_right, front_left
        ])

        # Wall base fills.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 27, 36, 38) if self.theme == 'dark' else QColor(15, 20, 30, 20))
        painter.drawPolygon(QPolygonF(left_wall))
        painter.drawPolygon(QPolygonF(right_wall))
        painter.setBrush(QColor(255, 255, 255, 13) if self.theme == 'dark' else QColor(255, 255, 255, 37))
        painter.drawPolygon(QPolygonF(back_wall))

        # Build floor grid points.
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

        # One vertical metre = one wall-metre on screen.
        vertical_px_per_m = (back_y - upper_y) / ROOM_WALL_HEIGHT_M

        # Draw wall grids first so raised tiles appear in front of the wall instead of being blocked by it.
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

        # Every wall edge, including the corners and wall-to-floor seams, belongs
        # BELOW the solid tiles. Do not redraw these over the floor later.
        wall_edge = QPen(QColor(0, 0, 0, 138), 1.2)
        wall_edge.setCosmetic(True)
        painter.setPen(wall_edge)
        painter.drawLine(wall_tl, wall_tr)
        painter.drawLine(wall_tl, back_left)
        painter.drawLine(wall_tr, back_right)
        painter.drawLine(back_left, front_left)
        painter.drawLine(back_right, front_right)
        painter.drawLine(back_left, back_right)

        # Clip tile drawing to the room interior, not only the floor plane.
        painter.save()
        clip_path = QPainterPath()
        clip_path.addPolygon(room_inner_poly)
        painter.setClipPath(clip_path)
        for row in range(ROOM_SIDE_M):
            for col in range(ROOM_SIDE_M):
                self._draw_tile(painter, row, col, floor_grid, vertical_px_per_m)
        painter.restore()

        # The front border is outside the raised back-corner blocks. It is safe
        # to keep this one visible above the tiled floor.
        painter.setPen(wall_edge)
        painter.drawLine(front_left, front_right)
        painter.end()
