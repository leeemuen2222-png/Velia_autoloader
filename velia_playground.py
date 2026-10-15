"""Velia Playground: clean room-only foundation (no character or music systems)."""
import json
import os
import sys
import subprocess
import secrets
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEasingCurve, QPropertyAnimation, QPoint, QPointF, QProcess
from PySide6.QtGui import QFont, QPainter, QPen, QColor, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
    QComboBox, QSizePolicy, QScrollArea, QGraphicsDropShadowEffect, QDialog, QToolButton, QGridLayout)
from multiplayer_client import MultiplayerClient, MULTIPLAYER_AVAILABLE
from room_view import wallpapers, ROOM_SIDE_M
from operator_spine import (SpineRoomView, available_operators, load_operator,
                            RUNTIME_DIR, OP_DIR, WEBENGINE_AVAILABLE)
from lan_invite import create_invite, decode_invite, local_ipv4

ROOT = Path(__file__).resolve().parent
PREF = ROOT / "local" / "preferences" / "playground_settings.json"
if WEBENGINE_AVAILABLE:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEngineSettings

DEFAULT_URL = os.environ.get("VELIA_MULTIPLAYER_URL", "ws://127.0.0.1:8765")

def load_preferences():
    try:
        data = json.loads(PREF.read_text(encoding="utf-8"))
        return {"theme": data.get("theme", "light") if data.get("theme") in ("light", "dark") else "light",
                "language": data.get("language", "zh") if data.get("language") in ("zh", "en") else "zh"}
    except (OSError, ValueError, TypeError):
        return {"theme": "light", "language": "zh"}

def save_preferences(data):
    PREF.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREF.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PREF)

class AnimatedButton(QPushButton):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)
        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    def enterEvent(self, event):
        self._animate(44)
        super().enterEvent(event)
    def leaveEvent(self, event):
        self._animate(40)
        super().leaveEvent(event)
    def _animate(self, height):
        self._anim.stop()
        self._anim.setStartValue(self.height())
        self._anim.setEndValue(height)
        self._anim.start()

class RoundCloseButton(QToolButton):
    """Self-painted square close button; unrelated to global QPushButton QSS."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark = False
        self.setObjectName("pickerClose")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(36, 36)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("")

    def set_dark(self, dark):
        self._dark = bool(dark)
        self.update()

    def paintEvent(self, event):
        # QToolButton does not inherit the dialog's QPushButton min-height/padding.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(1, 1, -1, -1)
        if self.isDown():
            fill = QColor('#485262' if self._dark else '#dbe5f0')
        elif self.underMouse():
            fill = QColor('#343a45' if self._dark else '#edf1f6')
        else:
            fill = QColor('#242a33' if self._dark else '#edf2f8')
        border = QColor('#454c58' if self._dark else '#cbd3df')
        ink = QColor('#f4f5f7' if self._dark else '#19222e')
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(bounds, 10, 10)
        painter.setPen(QPen(ink, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center = self.rect().center()
        x, y = center.x(), center.y()
        painter.drawLine(x - 5, y - 5, x + 5, y + 5)
        painter.drawLine(x + 5, y - 5, x - 5, y + 5)
        painter.end()


class RoundedComboBox(QComboBox):
    # Frameless popup avoids native Windows combo borders.
    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup = None
        self._dark = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)

    def paintEvent(self, event):
        super().paintEvent(event)
        # Draw our own theme-aware chevron. Native Qt arrows can create
        # unexpected black squares on Windows with custom combo popups.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#f3f5f7' if self._dark else '#17202b'), 1.8))
        x = self.width() - 21
        y = self.height() // 2
        is_open = self._popup is not None and self._popup.isVisible()
        if is_open:  # downward chevron (v)
            painter.drawLine(x - 5, y - 3, x, y + 2)
            painter.drawLine(x, y + 2, x + 5, y - 3)
        else:  # right-pointing chevron (>)
            painter.drawLine(x - 2, y - 5, x + 3, y)
            painter.drawLine(x + 3, y, x - 2, y + 5)
        painter.end()

    def set_dark(self, dark):
        self._dark = bool(dark)
        if self._popup is not None:
            self._popup.close()
            self._popup = None

    def showPopup(self):
        if self.count() == 0:
            return
        if self._popup is not None:
            self._popup.close()
        popup = QFrame(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        popup.setObjectName('veliaPopupShell')
        outer = QVBoxLayout(popup)
        outer.setContentsMargins(3, 3, 3, 3)
        outer.setSpacing(0)
        body = QFrame(popup)
        body.setObjectName('veliaPopupBody')
        outer.addWidget(body)
        b_layout = QVBoxLayout(body)
        b_layout.setContentsMargins(5, 5, 5, 5)
        b_layout.setSpacing(3)
        surface = '#1b1e24' if self._dark else '#ffffff'
        border = '#444a55' if self._dark else '#c9d1db'
        text_color = '#f3f5f7' if self._dark else '#17202b'
        hovered = '#323842' if self._dark else '#edf1f6'
        chosen = '#404854' if self._dark else '#dbe5f0'
        body.setStyleSheet(f'QFrame#veliaPopupBody {{background:{surface};border:1px solid {border};border-radius:13px;}}')
        scroll = QScrollArea(body)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet('QScrollArea {border:0;background:transparent;} QScrollArea > QWidget > QWidget {background:transparent;}')
        rows = QWidget()
        rows.setStyleSheet('background:transparent;')
        row_layout = QVBoxLayout(rows)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)
        for i in range(self.count()):
            button = QPushButton(self.itemText(i), rows)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(37)
            button.setEnabled(self.model().item(i).isEnabled() if hasattr(self.model(), 'item') and self.model().item(i) is not None else True)
            button.setStyleSheet(f'''QPushButton {{text-align:left;padding:7px 12px;color:{text_color};background:{chosen if i == self.currentIndex() else surface};border:none;border-radius:8px;font-weight:500;}}
                QPushButton:hover {{background:{hovered};}}
                QPushButton:pressed {{background:{chosen};padding-top:9px;padding-bottom:5px;}}''')
            button.clicked.connect(lambda checked=False, index=i, menu=popup: (self.setCurrentIndex(index), menu.close(), self.update()))
            row_layout.addWidget(button)
        scroll.setWidget(rows)
        b_layout.addWidget(scroll)
        width = max(self.width(), 230)
        height = min(self.count() * 39 + 20, 320)
        popup.resize(width, height)
        pos = self.mapToGlobal(QPoint(0, self.height() + 3))
        screen = self.screen().availableGeometry()
        if pos.x() + width > screen.right():
            pos.setX(screen.right() - width)
        if pos.y() + height > screen.bottom():
            pos.setY(self.mapToGlobal(QPoint(0, -height - 3)).y())
        popup.move(pos)
        self._popup = popup
        popup.destroyed.connect(lambda: setattr(self, '_popup', None) if self._popup is popup else None)
        popup.show()
        self.update()

class OperatorPickerDialog(QDialog):
    """Theme-aware operator picker using the application's custom rounded combo."""

    def __init__(self, parent, choices, title, dark=False, language="zh"):
        super().__init__(parent)
        self.setObjectName("operatorPicker")
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(430)
        self.resize(470, 250)
        background = '#1b1e24' if dark else '#ffffff'
        foreground = '#f4f5f7' if dark else '#19222e'
        secondary = '#b4bdca' if dark else '#637082'
        border = '#454c58' if dark else '#cbd3df'
        hover = '#343a45' if dark else '#edf1f6'
        pressed = '#485262' if dark else '#dbe5f0'
        self.setStyleSheet(f"""
            QDialog#operatorPicker {{background-color:{background}; border:1px solid {border};
                                    border-radius:18px;}}
            QDialog#operatorPicker QLabel {{color:{foreground}; background:transparent; border:none;}}
            QDialog#operatorPicker QLabel#pickerSub {{color:{secondary};font-size:13px;}}
            QDialog#operatorPicker QLabel#pickerTitle {{font-size:20px;font-weight:700;}}
            QDialog#operatorPicker QPushButton {{color:{foreground};background:{background};
                       border:1px solid {border};border-radius:11px;min-height:40px;padding:4px 16px;font-weight:600;}}
            QDialog#operatorPicker QPushButton:hover {{background:{hover};}}
            QDialog#operatorPicker QPushButton:pressed {{background:{pressed};padding-top:6px;padding-bottom:2px;}}
            QDialog#operatorPicker QComboBox {{background:{background};color:{foreground};border:1px solid {border};
                       border-radius:10px;padding:10px 14px;min-height:28px;}}
            QDialog#operatorPicker QComboBox::drop-down {{border:none;width:34px;background:transparent;}}
            QDialog#operatorPicker QComboBox::down-arrow {{image:none;width:0;height:0;}}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 21, 25, 23)
        layout.setSpacing(14)
        title_bar = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName('pickerTitle')
        title_bar.addWidget(heading, 1)
        close_button = RoundCloseButton(self)
        close_button.set_dark(dark)
        close_button.clicked.connect(self.reject)
        title_bar.addWidget(close_button)
        layout.addLayout(title_bar)

        subtitle = QLabel('选择需要加入房间的干员' if language == 'zh' else 'Choose an operator to deploy in the room')
        subtitle.setObjectName('pickerSub')
        layout.addWidget(subtitle)
        self.combo = RoundedComboBox(self)
        self.combo.set_dark(dark)
        self.combo.setMinimumHeight(48)
        for name in choices:
            self.combo.addItem(name, name)
        layout.addWidget(self.combo)
        layout.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton('取消' if language == 'zh' else 'Cancel')
        cancel.clicked.connect(self.reject)
        confirm = QPushButton('确认添加' if language == 'zh' else 'Deploy')
        confirm.setEnabled(bool(choices))
        confirm.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(confirm)
        layout.addLayout(actions)

    def selected_operator(self):
        return self.combo.currentData()


class OperatorCard(AnimatedButton):
    """Whole-card click with portrait and pre-rendered operator artwork."""

    def _animate(self, height):
        # Cards must never inherit AnimatedButton's 40/44 px height animation.
        return

    def enterEvent(self, event):
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(23)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 52))
        self.setGraphicsEffect(shadow)
        self._glow = QPropertyAnimation(shadow, b'blurRadius', self)
        self._glow.setDuration(160)
        self._glow.setStartValue(9)
        self._glow.setEndValue(23)
        self._glow.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setGraphicsEffect(None)
        super().leaveEvent(event)

    def __init__(self, name, dark=False, parent=None):
        super().__init__('', parent)
        self.name = name
        self.setObjectName('operatorCard')
        self.setMinimumHeight(202)
        self.setMaximumHeight(202)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet('')
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(6)
        left.setContentsMargins(0, 0, 0, 0)
        portrait = QLabel()
        portrait.setFixedSize(58, 61)
        portrait.setAlignment(Qt.AlignmentFlag.AlignCenter)
        portrait.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        portrait.setObjectName('portrait')
        folder = OP_DIR / name / 'pic'
        portrait_file = folder / 'oph.png'
        if portrait_file.is_file():
            pix = QPixmap(str(portrait_file))
            if not pix.isNull():
                portrait.setPixmap(pix.scaled(58, 61, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
            else:
                portrait.setText('OP')
        else:
            portrait.setText('OP')
        left.addWidget(portrait, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.name_label = QLabel(name)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        left.addSpacing(6)
        left.addWidget(self.name_label)
        left.addStretch(1)
        self._layout.addLayout(left)
        self._layout.addStretch(1)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.preview_label.setMinimumSize(170, 170)
        self.preview_label.setMaximumSize(240, 170)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        image_file = folder / 'op_select.png'
        if not image_file.is_file():
            image_file = folder / (name + '_op_select.png')
        if not image_file.is_file():
            image_file = ROOT / (name + '_op_select.png')
        pix = QPixmap(str(image_file)) if image_file.is_file() else QPixmap()
        if not pix.isNull():
            self.preview_label.setPixmap(pix.scaled(
                self.preview_label.maximumWidth(), self.preview_label.maximumHeight(),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview_label.setText('请安装 pic/op_select.png')
        self._layout.addWidget(self.preview_label)

    def activate_preview(self):
        return

    def deactivate_preview(self):
        return


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.prefs = load_preferences()
        self.client = MultiplayerClient(self)
        self.joined = False
        self._lan_host = False
        self._host_entered = False
        self._lan_invite = ""
        self._server_process = None
        self._join_key = ""
        self.members = set()
        self._build()
        self._connect_signals()
        self._translate()
        self._theme()
        self.resize(1360, 850)
        self.setMinimumSize(900, 620)

    def tr2(self, zh, en):
        return en if self.prefs["language"] == "en" else zh

    def _button(self, label="", action=None, kind="normal"):
        b = AnimatedButton(label)
        b.setProperty("kind", kind)
        if action: b.clicked.connect(action)
        return b

    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(32, 26, 32, 28)
        outer.setSpacing(20)
        header = QHBoxLayout()
        self.brand = QLabel()
        self.brand.setObjectName("brand")
        header.addWidget(self.brand)
        header.addStretch()
        self.home_btn = self._button(action=lambda: self.stack.setCurrentWidget(self.home))
        self.room_btn = self._button(action=lambda: self.stack.setCurrentWidget(self.room))
        self.settings_btn = self._button(action=lambda: self.stack.setCurrentWidget(self.settings))
        for b in (self.home_btn, self.room_btn, self.settings_btn): header.addWidget(b)
        outer.addLayout(header)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.home = QWidget()
        hl = QVBoxLayout(self.home)
        hl.setContentsMargins(16, 22, 16, 16)
        hl.setSpacing(18)
        self.welcome = QLabel()
        self.welcome.setObjectName("hero")
        hl.addWidget(self.welcome)
        self.home_sub = QLabel()
        self.home_sub.setObjectName("muted")
        hl.addWidget(self.home_sub)
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(34, 34, 34, 34)
        cl.setSpacing(17)
        self.playground_title = QLabel()
        self.playground_title.setObjectName("cardTitle")
        cl.addWidget(self.playground_title)
        self.playground_desc = QLabel()
        self.playground_desc.setObjectName("muted")
        cl.addWidget(self.playground_desc)
        self.enter_btn = self._button(action=self._enter_playground,kind="primary")
        cl.addWidget(self.enter_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        hl.addWidget(card)
        hl.addStretch()
        self.stack.addWidget(self.home)

        self.playground = QWidget()
        pl = QVBoxLayout(self.playground)
        pl.setContentsMargins(16, 12, 16, 12)
        pl.setSpacing(12)
        phead = QHBoxLayout()
        self.stage_title = QLabel()
        self.stage_title.setObjectName("cardTitle")
        phead.addWidget(self.stage_title)
        phead.addStretch()
        self.add_operator_btn = self._button(action=self._add_operator)
        phead.addWidget(self.add_operator_btn)
        self.operator_label = QLabel()
        phead.addWidget(self.operator_label)
        self.wallpaper_label = QLabel()
        phead.addWidget(self.wallpaper_label)
        self.wallpaper_combo = RoundedComboBox()
        self.wallpaper_combo.setMinimumWidth(205)
        self.wallpaper_combo.setMaximumWidth(340)
        self.wallpaper_combo.currentIndexChanged.connect(self._wallpaper_changed)
        phead.addWidget(self.wallpaper_combo)
        self.to_room = self._button(action=lambda: self.stack.setCurrentWidget(self.room))
        phead.addWidget(self.to_room)
        pl.addLayout(phead)
        self.stage = SpineRoomView(operator_name=None)
        self.stage.setObjectName("stage")
        self.stage.controlledChanged.connect(self._update_operator_label)
        self.stage.networkSpeech.connect(self._send_room_speech)
        self.stage.networkAction.connect(self._send_room_action)
        self._lan_status_overlay = QLabel(self.stage)
        self._lan_status_overlay.setObjectName('lanStatusOverlay')
        self._lan_status_overlay.setStyleSheet('background:rgba(18,25,33,185);color:#ffffff;'
                                               'border:1px solid rgba(255,255,255,80);'
                                               'border-radius:12px;padding:9px 12px;'
                                               'font:12px Segoe UI;')
        self._lan_status_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._lan_status_overlay.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._lan_status_overlay.hide()
        self._lan_status_overlay.setFixedWidth(205)
        # Measure Qt's actual scheduled frame-update cadence (not GPU/WebEngine FPS).
        self._frame_update_count = 0
        self._frame_update_fps = 0.0
        self._frame_measure_start = time.perf_counter()
        self._frame_tick_timer = QTimer(self)
        self._frame_tick_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_tick_timer.setInterval(16)  # ~60 Hz; Qt/OS may schedule later.
        self._frame_tick_timer.timeout.connect(self._measure_frame_tick)
        self._frame_tick_timer.start()
        self._lan_overlay_timer = QTimer(self)
        self._lan_overlay_timer.setInterval(250)
        self._lan_overlay_timer.timeout.connect(self._position_lan_overlay)
        self._lan_overlay_timer.start()
        self._net_send_timer = QTimer(self)
        self._net_send_timer.setInterval(50)
        self._net_send_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._net_send_timer.timeout.connect(self._send_actor_state)
        self._net_send_timer.start()
        self._remote_actors = {}
        self._remote_pending = {}
        self._remote_action_queue = {}
        self._local_user_id = None
        self._roster_by_id = {}
        self._lan_active = False
        self._local_actor_published = False
        self.stage_layout = pl
        pl.addWidget(self.stage, 1)
        self._refresh_wallpapers()
        self.stack.addWidget(self.playground)

        self.operator_page = QWidget()
        self.operator_page.setObjectName('operatorPage')
        op_layout = QVBoxLayout(self.operator_page)
        op_layout.setContentsMargins(22, 14, 22, 14)
        op_layout.setSpacing(12)
        chooser_header = QHBoxLayout()
        self.chooser_heading = QLabel('选择干员')
        self.chooser_heading.setObjectName('cardTitle')
        chooser_header.addWidget(self.chooser_heading)
        chooser_header.addStretch(1)
        self.chooser_back = self._button('返回', self._leave_operator_page)
        chooser_header.addWidget(self.chooser_back)
        op_layout.addLayout(chooser_header)
        filters = QHBoxLayout()
        self.operator_search = QLineEdit()
        self.operator_search.setPlaceholderText('搜索干员 / Search operators')
        self.operator_search.textChanged.connect(self._filter_operator_cards)
        filters.addWidget(self.operator_search,1)
        self.chooser_filter = self._button('筛选（即将推出）')
        self.chooser_filter.setEnabled(False)
        filters.addWidget(self.chooser_filter)
        op_layout.addLayout(filters)
        self.chooser_scroll = QScrollArea()
        self.chooser_scroll.setWidgetResizable(True)
        self.chooser_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chooser_scroll.setObjectName('operatorChooserScroll')
        self.chooser_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chooser_content = QWidget()
        self.chooser_content.setObjectName('operatorChooserContent')
        self.chooser_items = QGridLayout(self.chooser_content)
        # Keep short rows packed at the top instead of spreading them across the tall viewport.
        self.chooser_items.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chooser_items.setContentsMargins(10, 10, 10, 10)
        self.chooser_items.setHorizontalSpacing(14)
        self.chooser_items.setVerticalSpacing(14)
        self.chooser_items.setColumnStretch(0, 1)
        self.chooser_items.setColumnStretch(1, 1)
        self.chooser_scroll.setWidget(self.chooser_content)
        op_layout.addWidget(self.chooser_scroll,1)
        self._operator_cards = []
        self._preview_timer = None
        self._pending_previews = []
        self._preview_batch_timer = None
        self._chooser_source = self.home
        self.stack.addWidget(self.operator_page)

        self.room = QWidget()
        rl = QVBoxLayout(self.room)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(15)
        self.room_title = QLabel('联机房间')
        self.room_title.setObjectName('cardTitle')
        rl.addWidget(self.room_title)
        self.lan_pages = QStackedWidget()
        rl.addWidget(self.lan_pages, 1)
        self.lan_start = QWidget()
        v = QVBoxLayout(self.lan_start)
        v.addWidget(QLabel('选择联机方式 / Connection mode'))
        btn=self._button('本地联机 / LAN', lambda: self.lan_pages.setCurrentWidget(self.lan_actions), 'primary'); v.addWidget(btn)
        btn=self._button('网络联机（即将推出）', lambda: self.lan_pages.setCurrentWidget(self.internet_placeholder)); v.addWidget(btn)
        v.addStretch()
        self.lan_pages.addWidget(self.lan_start)
        self.internet_placeholder=QWidget()
        v=QVBoxLayout(self.internet_placeholder)
        v.addWidget(QLabel('网络联机仍在开发中 / Internet multiplayer coming soon'))
        v.addWidget(self._button('返回', lambda: self.lan_pages.setCurrentWidget(self.lan_start)))
        v.addStretch()
        self.lan_pages.addWidget(self.internet_placeholder)
        self.lan_actions=QWidget()
        v=QVBoxLayout(self.lan_actions)
        v.addWidget(QLabel('本地联机 / LAN'))
        v.addWidget(self._button('建立房间', lambda: self.lan_pages.setCurrentWidget(self.lan_host_page), 'primary'))
        v.addWidget(self._button('加入房间', lambda: self.lan_pages.setCurrentWidget(self.lan_join_page)))
        v.addWidget(self._button('返回', lambda: self.lan_pages.setCurrentWidget(self.lan_start)))
        v.addStretch()
        self.lan_pages.addWidget(self.lan_actions)
        self.lan_host_page=QWidget()
        v=QVBoxLayout(self.lan_host_page)
        v.setSpacing(9)
        v.addWidget(QLabel('建立本地房间'))
        v.addWidget(QLabel('房间名称'))
        self.host_room_name=QLineEdit('Velia LAN Room'); v.addWidget(self.host_room_name)
        v.addWidget(QLabel('房主昵称'))
        self.host_nickname=QLineEdit(); self.host_nickname.setPlaceholderText('未知玩家'); v.addWidget(self.host_nickname)
        self.host_launch=self._button('启动服务器并生成邀请码', self._create_lan_room, 'primary')
        v.addWidget(self.host_launch)
        self.host_info=QLabel('尚未创建房间'); self.host_info.setWordWrap(True); self.host_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.host_info)
        self.host_copy=self._button('复制邀请码', self._copy_invite); self.host_copy.setEnabled(False); v.addWidget(self.host_copy)
        v.addWidget(self._button('返回', lambda: self.lan_pages.setCurrentWidget(self.lan_actions)))
        v.addStretch()
        self.lan_pages.addWidget(self.lan_host_page)
        self.lan_join_page=QWidget()
        v=QVBoxLayout(self.lan_join_page)
        v.addWidget(QLabel('输入房主分享的邀请码，无需填写服务器地址'))
        self.join_invite_input=QLineEdit(); self.join_invite_input.setMaxLength(40); v.addWidget(self.join_invite_input)
        self.invite_hint=QLabel('等待输入邀请码'); v.addWidget(self.invite_hint)
        self.join_invite_input.textChanged.connect(self._validate_invite)
        v.addWidget(QLabel('昵称'))
        self.join_nickname=QLineEdit(); self.join_nickname.setPlaceholderText('未知玩家'); v.addWidget(self.join_nickname)
        self.join_connect=self._button('加入房间', self._join_lan, 'primary'); self.join_connect.setEnabled(False); v.addWidget(self.join_connect)
        v.addWidget(self._button('返回', lambda: self.lan_pages.setCurrentWidget(self.lan_actions)))
        v.addStretch()
        self.lan_pages.addWidget(self.lan_join_page)
        self.lan_wait=QWidget()
        v=QVBoxLayout(self.lan_wait)
        self.lan_wait_heading=QLabel('房间等候区'); self.lan_wait_heading.setObjectName('cardTitle'); v.addWidget(self.lan_wait_heading)
        self.status=QLabel('正在连接...'); v.addWidget(self.status)
        self.room_key_view=QLabel(); self.room_key_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); v.addWidget(self.room_key_view)
        self.members_label=QLabel('房间内玩家'); v.addWidget(self.members_label)
        self.members_view=QLabel(''); self.members_view.setWordWrap(True); v.addWidget(self.members_view)
        self.enter_multiplayer=self._button('进入房间并选择干员', self._enter_lan_playground, 'primary')
        self.enter_multiplayer.setEnabled(False)
        v.addWidget(self.enter_multiplayer)
        self.disconnect_btn=self._button('退出房间', self._disconnect); v.addWidget(self.disconnect_btn)
        v.addStretch()
        self.lan_pages.addWidget(self.lan_wait)
        self.lan_pages.setCurrentWidget(self.lan_start)
        self.stack.addWidget(self.room)

        self.settings = QWidget()
        sl = QVBoxLayout(self.settings)
        sl.setContentsMargins(16,12,16,12)
        sl.setSpacing(16)
        self.settings_title = QLabel()
        self.settings_title.setObjectName("cardTitle")
        sl.addWidget(self.settings_title)
        self.theme_label = QLabel()
        sl.addWidget(self.theme_label)
        self.theme_combo = RoundedComboBox()
        self.theme_combo.addItem("", "light")
        self.theme_combo.addItem("", "dark")
        self.theme_combo.setCurrentIndex(0 if self.prefs["theme"] == "light" else 1)
        self.theme_combo.currentIndexChanged.connect(self._change_theme)
        sl.addWidget(self.theme_combo)
        self.language_label = QLabel()
        sl.addWidget(self.language_label)
        self.language_combo = RoundedComboBox()
        self.language_combo.addItem("简体中文", "zh")
        self.language_combo.addItem("English", "en")
        self.language_combo.setCurrentIndex(0 if self.prefs["language"] == "zh" else 1)
        self.language_combo.currentIndexChanged.connect(self._change_language)
        sl.addWidget(self.language_combo)
        sl.addStretch()
        self.stack.addWidget(self.settings)

    def _choose_operator(self, choices, title):
        if not choices:
            QMessageBox.information(self, 'Velia Playground',
                self.tr2('没有可添加的干员。', 'No operators available to add.'))
            return None
        picker = OperatorPickerDialog(self, choices, title,
                                      dark=self.prefs['theme'] == 'dark',
                                      language=self.prefs['language'])
        return picker.selected_operator() if picker.exec() == QDialog.DialogCode.Accepted else None

    def _show_operator_page(self, source):
        self._chooser_source = source
        if self._preview_timer is not None:
            self._preview_timer.stop()
        if self._preview_batch_timer is not None:
            self._preview_batch_timer.stop()
        self._pending_previews.clear()
        for card in self._operator_cards:
            card.deactivate_preview()
            self.chooser_items.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._operator_cards = []
        available = [name for name in available_operators() if name not in self.stage.placed_operator_names()]
        dark = self.prefs['theme'] == 'dark'
        for idx, name in enumerate(available):
            card = OperatorCard(name,dark,self.chooser_content)
            card.clicked.connect(lambda checked=False, selected=name: self._select_operator_card(selected))
            self.chooser_items.addWidget(card, idx // 2, idx % 2)
            self._operator_cards.append(card)
        self._pending_previews = []
        self.operator_search.clear()
        self.chooser_heading.setText(self.tr2('选择干员','Select operator'))
        self.stack.setCurrentWidget(self.operator_page)

    def _filter_operator_cards(self, query):
        search = query.casefold().strip()
        for card in self._operator_cards:
            card.setVisible(search in card.name.casefold())
        self._reflow_operator_cards()

    def _reflow_operator_cards(self):
        # Reflow visible cards without destroying running Spine previews.
        row = 0
        col = 0
        for card in self._operator_cards:
            if card.isHidden():
                continue
            self.chooser_items.addWidget(card, row, col)
            col += 1
            if col == 2:
                col = 0
                row += 1

    def _refresh_operator_previews(self):
        return

    def _load_next_operator_preview(self):
        return

    def _select_operator_card(self, name):
        if self.stage.add_operator(name):
            self._pending_previews.clear()
            self._update_operator_label()
            self.stack.setCurrentWidget(self.playground)
            if self._lan_active:
                self._send_actor_state()
                for uid, state in list(self._remote_pending.items()):
                    self._update_remote_actor(uid, state)
                self._update_lan_status()

    def _leave_operator_page(self):
        if self._preview_timer is not None:
            self._preview_timer.stop()
        if self._preview_batch_timer is not None:
            self._preview_batch_timer.stop()
        self._pending_previews.clear()
        for card in self._operator_cards:
            card.deactivate_preview()
        self.stack.setCurrentWidget(self._chooser_source)

    def _add_operator(self):
        self._show_operator_page(self.playground)

    def _update_operator_label(self):
        controlled = self.stage.controlled_actor()
        name = controlled.operator_name if controlled else '—'
        self.operator_label.setText(self.tr2('控制中: ', 'Controlling: ') + name)

    def _enter_playground(self):
        self._refresh_wallpapers()
        if not self.stage.placed_operator_names():
            self._show_operator_page(self.home)
            return
        self._update_operator_label()
        self.stack.setCurrentWidget(self.playground)

    def _refresh_wallpapers(self):
        current = self.wallpaper_combo.currentData()
        self.wallpaper_combo.blockSignals(True)
        self.wallpaper_combo.clear()
        self.wallpaper_combo.addItem(self.tr2("默认初始房间", "Default room"), None)
        for filename, label in wallpapers():
            self.wallpaper_combo.addItem(label, filename)
        index = self.wallpaper_combo.findData(current)
        self.wallpaper_combo.setCurrentIndex(index if index >= 0 else 0)
        self.wallpaper_combo.blockSignals(False)
        self._wallpaper_changed()

    def _wallpaper_changed(self, *_):
        self.stage.set_wallpaper(self.wallpaper_combo.currentData())

    def _connect_signals(self):
        self.client.connected.connect(self._connected)
        self.client.disconnected.connect(self._disconnected)
        self.client.connectionError.connect(self._error)
        self.client.messageReceived.connect(self._message)

    def _translate(self):
        t=self.tr2
        self.setWindowTitle("Velia Playground")
        self.brand.setText("VELIA / PLAYGROUND")
        self.home_btn.setText(t("主页", "Home"))
        self.room_btn.setText(t("联机房间", "Multiplayer"))
        self.settings_btn.setText(t("设置", "Settings"))
        self.welcome.setText(t("欢迎来到 Velia", "Welcome to Velia"))
        self.home_sub.setText(t("Playground · 独立开发版本", "Playground · Standalone foundation"))
        self.playground_title.setText(t("游乐场", "Playground"))
        self.playground_desc.setText(t("多干员游乐场 · 自主巡游与角色控制", "Multi-operator playground · autonomous wandering and player control"))
        self.enter_btn.setText(t("进入游乐场 ↗", "Enter Playground ↗"))
        self.stage_title.setText(t("游乐场 / 10 × 10 m", "Playground / 10 × 10 m"))
        self.add_operator_btn.setText(t("添加干员 +", "Add operator +"))
        self._update_operator_label()
        self.wallpaper_label.setText(t("房间壁纸", "Room wallpaper"))
        self._refresh_wallpapers()
        self.to_room.setText(t("联机房间 ↗", "Multiplayer Room ↗"))
        self.room_title.setText(t("联机房间", "Multiplayer Room"))
        self.settings_title.setText(t("界面设置", "Appearance settings"))
        self.theme_label.setText(t("主题", "Theme"))
        self.theme_combo.setItemText(0,t("白昼", "Light"))
        self.theme_combo.setItemText(1,t("黑夜", "Dark"))
        self.language_label.setText(t("语言", "Language"))
        self._render_members()
        if not self.joined:self.status.setText(t("尚未连接", "Not connected"))

    def _theme(self):
        light=self.prefs["theme"]=="light"
        bg, panel, text, muted, border, button, hover, accent, active = (
            ("#f4f5f7","#ffffff","#131820","#637082","#d5dbe3","#ffffff","#edf0f4","#202831","#d7dfe9") if light else
            ("#101114","#1a1c21","#f4f5f7","#a6afbc","#393d45","#24272e","#363a43","#f0f2f5","#525864"))
        for combo in (self.wallpaper_combo, self.theme_combo, self.language_combo):
            combo.set_dark(not light)
        self.stage.set_theme("light" if light else "dark")
        self.setStyleSheet(f"""
        QWidget#root {{background:{bg}; color:{text};font-family:'Segoe UI',Arial;font-size:14px;}}
        QLabel {{color:{text};}}
        QLabel#brand {{font-size:23px; font-weight:800;letter-spacing:1px;}}
        QLabel#hero {{font-size:34px;font-weight:700;}}
        QLabel#cardTitle {{font-size:23px;font-weight:700;}}
        QLabel#muted {{color:{muted};font-size:14px;}}
        QFrame#card,QFrame#stage {{background:{panel};border:1px solid {border};border-radius:22px;}}
        QPushButton {{color:{text};background:{button};border:1px solid {border};border-radius:12px;padding:8px 18px;font-weight:600;}}
        QPushButton:hover {{background:{hover};border-color:{muted};}}
        QPushButton:pressed {{background:{active};padding-top:10px;padding-bottom:6px;}}
        QPushButton:disabled {{color:{muted};opacity:0.5;}}
        QPushButton[kind='primary'] {{background:{accent};color:{bg};border-color:{accent};}}
        QPushButton[kind='primary']:hover {{background:{muted};border-color:{muted};}}
        QPushButton[kind='primary']:pressed {{background:{active};color:{text};}}
        QLineEdit,QComboBox {{background:{panel};color:{text};border:1px solid {border};border-radius:10px;padding:12px;min-height:22px;}}
        QLineEdit:focus,QComboBox:focus {{border:1px solid {muted};}}
        QComboBox::drop-down {{border:0px; width:29px; background:transparent;}}
        QComboBox::down-arrow {{image:none;width:0px;height:0px;}}
        QComboBox:hover {{background:{hover};border-color:{muted};}}
        QComboBox:pressed {{background:{active};}}
        QWidget#operatorPage {{background:{bg};}}
        QScrollArea {{background:{bg};border:0px;}}
        QScrollArea > QWidget > QWidget {{background:{bg};}}
        QWidget#operatorChooserContent {{background:{bg};}}
        QScrollBar:vertical {{background:{bg};width:10px;margin:4px 1px;border:none;}}
        QScrollBar::handle:vertical {{background:{border};min-height:40px;border-radius:4px;}}
        QScrollBar::handle:vertical:hover {{background:{muted};}}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0px;border:none;background:transparent;}}
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;}}
        QPushButton#operatorCard {{background:{panel};border:1px solid {border};border-radius:16px;padding:0;text-align:left;}}
        QPushButton#operatorCard:hover {{background:{hover};border-color:{muted};}}
        QPushButton#operatorCard:pressed {{background:{active};border-color:{text};padding:0;}}
        QLabel#portrait {{background:{hover};border:1px solid {border};border-radius:9px;}}
        QPushButton#operatorCard QLabel {{font-size:11px;}}
        """)

    def _change_theme(self):
        self.prefs["theme"]=self.theme_combo.currentData()
        save_preferences(self.prefs)
        self._theme()

    def _change_language(self):
        self.prefs["language"]=self.language_combo.currentData()
        save_preferences(self.prefs)
        self._translate()

    def _validate_invite(self, value):
        try:
            url, key = decode_invite(value)
            self.invite_hint.setText('已识别地址：' + url)
            self.join_connect.setEnabled(True)
        except ValueError as exc:
            self.invite_hint.setText(str(exc))
            self.join_connect.setEnabled(False)

    def _copy_invite(self):
        if self._lan_invite:
            QApplication.clipboard().setText(self._lan_invite)

    def _create_lan_room(self):
        if not MULTIPLAYER_AVAILABLE:
            QMessageBox.warning(self, 'QtWebSockets', '当前 PySide6 未包含 QtWebSockets。')
            return
        ip=local_ipv4()
        if ip.startswith('127.'):
            QMessageBox.warning(self, 'LAN', '没有找到可用的局域网 IPv4 地址。请先连接 Wi-Fi 或以太网。')
            return
        key=create_invite(ip)
        self._lan_invite=key
        self._join_key=key
        self._lan_host=True
        self._host_entered=False
        if self._server_process is not None:
            self._server_process.kill()
            self._server_process.waitForFinished(3000)
        self._server_process=QProcess(self)
        from PySide6.QtCore import QProcessEnvironment
        env=QProcessEnvironment.systemEnvironment()
        env.insert('VELIA_ROOM_CODE',key)
        env.insert('VELIA_ROOM_NAME',self.host_room_name.text().strip() or 'Velia LAN Room')
        self._server_process.setProcessEnvironment(env)
        server=ROOT/'server.py'
        self._server_process.setProgram(sys.executable)
        self._server_process.setArguments([str(server)])
        self._server_process.readyReadStandardOutput.connect(self._server_output)
        self._server_process.readyReadStandardError.connect(self._server_error)
        self._server_process.start()
        self.host_launch.setEnabled(False)
        self.host_info.setText('正在启动服务器...')

    def _server_output(self):
        output=bytes(self._server_process.readAllStandardOutput()).decode('utf-8','replace')
        if 'VELIA_LAN_READY:' in output:
            url, _ = decode_invite(self._lan_invite)
            self.host_info.setText(f'服务器地址：{url}\n房间名称：{self.host_room_name.text().strip() or "Velia LAN Room"}\n邀请码：{self._lan_invite}')
            self.host_copy.setEnabled(True)
            self._connect_lan(url)
        elif 'VELIA_LAN_ERROR:' in output:
            self._server_error()

    def _server_error(self):
        error=bytes(self._server_process.readAllStandardError()).decode('utf-8','replace')
        self.host_launch.setEnabled(True)
        self.host_info.setText('服务器启动失败：'+(error.strip() or '未知错误。请检查端口 8765 是否被占用以及 websockets 是否安装。'))

    def _join_lan(self):
        try:
            url,key=decode_invite(self.join_invite_input.text())
        except ValueError as exc:
            QMessageBox.warning(self,'邀请码',str(exc))
            return
        self._lan_host=False
        self._join_key=key
        self._host_entered=False
        self._connect_lan(url)

    def _connect_lan(self, url):
        self.join_connect.setEnabled(False)
        self.status.setText('正在连接 ' + url)
        self.client.connect_to(url)
        self.lan_pages.setCurrentWidget(self.lan_wait)

    def _connected(self):
        nickname=(self.host_nickname if self._lan_host else self.join_nickname).text().strip() or '未知玩家'
        self.client.send({'type':'join','data':{'room_id':self._join_key,'username':nickname,'host':self._lan_host,'room_name':self.host_room_name.text().strip() if self._lan_host else ''}})
        self.status.setText('已连接，等待服务器确认...')

    def _disconnect(self):
        self.client.close()
        if self._lan_host and self._server_process is not None:
            self._server_process.kill()
            self._server_process.waitForFinished(3000)
            self._server_process=None
            self.host_launch.setEnabled(True)
        self._disconnected()
        self.lan_pages.setCurrentWidget(self.lan_actions)

    def _disconnected(self):
        self.joined=False
        self._lan_active = False
        self._local_actor_published = False
        self._local_user_id = None
        for uid in list(self._remote_actors):
            self._remove_remote_actor(uid)
        self._remote_pending.clear()
        self._remote_action_queue.clear()
        self._lan_status_overlay.hide()
        self.members.clear()
        self.join_connect.setEnabled(bool(self.join_invite_input.text().strip()))
        self.enter_multiplayer.setEnabled(False)
        self.status.setText('连接已断开')
        self._render_members()

    def _error(self, message):
        self._disconnected()
        self.status.setText(message)

    def _message(self, packet):
        kind=packet.get('type')
        data=packet.get('data') or {}
        if kind=='welcome':
            self.joined=True
            self._lan_host=bool(data.get('is_host'))
            self._host_entered=bool(data.get('host_entered'))
            self._local_user_id = data.get('your_id')
            self._remote_pending.update(data.get('actors') or {})
            self.status.setText('已加入：' + data.get('room_name',''))
            self.room_key_view.setText('邀请码：' + self._join_key)
            self._set_members(data.get('members', []))
        elif kind=='roster':
            self._set_members(data.get('members', []))
            self._host_entered=bool(data.get('host_entered'))
        elif kind=='host_state':
            self._host_entered=bool(data.get('host_entered'))
        elif kind == 'actor':
            uid = data.get('id')
            if uid != self._local_user_id and isinstance(data.get('state'), dict):
                self._remote_pending[uid] = data['state']
                self._update_remote_actor(uid, data['state'])
        elif kind == 'actor_left':
            self._remove_remote_actor(data.get('id'))
        elif kind == 'chat':
            if data.get('id') != self._local_user_id:
                self.stage.console.log.appendPlainText(
                    str(data.get('username') or '未知玩家') + ': ' + str(data.get('text') or ''))
        elif kind == 'action':
            uid = data.get('id')
            if uid != self._local_user_id:
                actor = self._remote_actors.get(uid)
                expression = str(data.get('expression') or '')
                if actor is not None and actor._ready and not actor._deploying:
                    self.stage.network_replay_action(actor, expression)
                else:
                    queue = self._remote_action_queue.setdefault(uid, [])
                    queue.append((time.monotonic(), expression))
                    del queue[:-12]
        elif kind=='error':
            self.status.setText(str(packet.get('message','Server error')))
            self.client.close()
        self.enter_multiplayer.setEnabled(self.joined and (self._lan_host or self._host_entered))
        self._update_lan_status()

    def _set_members(self, members):
        self._roster_by_id = {p.get('id'):p for p in members if isinstance(p, dict) and p.get('id')}
        self.members={p.get('username') or '未知玩家' for p in members if isinstance(p,dict)}
        self._render_members()
        self._update_lan_status()

    def _render_members(self):
        self.members_view.setText('\n'.join(sorted(self.members)) if self.members else '暂无成员')

    def _enter_lan_playground(self):
        if not self.joined or not (self._lan_host or self._host_entered):
            return
        if self._lan_host:
            self.client.send({'type':'host_entered'})
            self._host_entered=True
        self._lan_active = True
        self.client.send({'type':'entered'})
        self._refresh_wallpapers()
        self._show_operator_page(self.room)

    def _position_lan_overlay(self):
        if not self._lan_active or not self.joined:
            self._lan_status_overlay.hide()
            return
        for uid, queue in list(self._remote_action_queue.items()):
            actor = self._remote_actors.get(uid)
            if actor is None or not actor._ready or actor._deploying:
                continue
            # Replaying stale several-second-old motions is worse than
            # catching up to the latest still relevant action.
            for stamp, expression in queue:
                if time.monotonic() - stamp <= 3.0:
                    self.stage.network_replay_action(actor, expression)
            self._remote_action_queue.pop(uid, None)
        panel = self._lan_status_overlay
        panel.adjustSize()
        panel.move(max(8, self.stage.width() - panel.width() - 18), 16)
        panel.raise_()
        panel.show()

    def _measure_frame_tick(self):
        # A measured Qt event-loop update rate. WebEngine renders via its own
        # requestAnimationFrame loop, so do not label this as actual GPU FPS.
        now = time.perf_counter()
        self._frame_update_count += 1
        elapsed = now - self._frame_measure_start
        if elapsed >= 0.65:
            self._frame_update_fps = self._frame_update_count / elapsed
            self._frame_update_count = 0
            self._frame_measure_start = now
            if self._lan_active and self.joined:
                self._update_lan_status()

    def _update_lan_status(self):
        if not hasattr(self, '_lan_status_overlay') or not self._lan_active:
            return
        connected = self.joined
        text = 'LAN  ·  ' + ('已连接' if connected else '已断开')
        text += '\nUI 更新: ' + (f'{self._frame_update_fps:.0f} FPS' if self._frame_update_fps > 0 else '计算中...') + '  / 目标 60'
        text += '\n' + ('房主已进入' if self._host_entered or self._lan_host else '等待房主进入')
        text += '\n成员 (' + str(len(self._roster_by_id)) + '):'
        for info in self._roster_by_id.values():
            username = str(info.get('username') or '未知玩家')
            text += '\n• ' + username + ('  [房主]' if info.get('is_host') else '')
            text += '  ●' if info.get('entered') else '  ○'
        self._lan_status_overlay.setText(text)
        self._position_lan_overlay()

    def _send_room_speech(self, text):
        if self._lan_active and self.joined:
            self.client.send({'type':'chat','data':{'text':str(text)[:500]}})

    def _send_room_action(self, expression):
        if self._lan_active and self.joined:
            self.client.send({'type':'action','data':{'expression':str(expression)[:5000]}})

    def _send_actor_state(self):
        if not self._lan_active or not self.joined or self.stack.currentWidget() != self.playground:
            return
        actor = self.stage.controlled_actor()
        if actor is None or not actor.operator_name:
            if self._local_actor_published:
                self.client.send({'type':'actor_clear'})
                self._local_actor_published = False
            return
        self._local_actor_published = True
        self.client.send({'type':'actor','data':{
            'operator':actor.operator_name,
            'x':round(actor.spawn_x,4), 'z':round(actor.spawn_z,4),
            'skin':actor.skin_number, 'moving':actor._moving_animation,
            'left':actor._facing_left}})

    def _update_remote_actor(self, uid, state):
        if not uid or not self._lan_active or self.stack.currentWidget() != self.playground:
            return
        actor = self._remote_actors.get(uid)
        operator = state.get('operator')
        if actor is not None and actor.operator_name != operator:
            self._remove_remote_actor(uid)
            actor = None
        if actor is None:
            if not self.stage.placed_operator_names():
                return
            actor = self.stage.network_add_actor(operator)
            if actor is None:
                return
            self._remote_actors[uid] = actor
        self.stage.network_apply_actor(actor, state)

    def _remove_remote_actor(self, uid):
        actor = self._remote_actors.pop(uid, None)
        self._remote_pending.pop(uid, None)
        self._remote_action_queue.pop(uid, None)
        if actor is None:
            return
        for key, value in list(self.stage._actors.items()):
            if value is actor:
                self.stage._actors.pop(key, None)
                break
        actor.close()
        actor.setParent(None)
        actor.deleteLater()
        self.stage._layer_order = None
        self.stage._keep_console_on_top()

    def closeEvent(self,event):
        self.client.close()
        if self._server_process is not None:
            self._server_process.kill()
            self._server_process.waitForFinished(3000)
        super().closeEvent(event)

def main():
    app=QApplication(sys.argv)
    app.setApplicationName("Velia Playground")
    app.setStyle("Fusion")
    window=MainWindow()
    window.show()
    return app.exec()

if __name__=="__main__":
    sys.exit(main())
