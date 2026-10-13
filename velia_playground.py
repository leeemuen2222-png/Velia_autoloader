"""Velia Playground: clean room-only foundation (no character or music systems)."""
import json
import os
import sys
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEasingCurve, QPropertyAnimation, QPoint
from PySide6.QtGui import QFont, QPainter, QPen, QColor
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
    QComboBox, QSizePolicy, QScrollArea, QGraphicsDropShadowEffect)
from multiplayer_client import MultiplayerClient, MULTIPLAYER_AVAILABLE
from room_view import RoomView, wallpapers, ROOM_SIDE_M

ROOT = Path(__file__).resolve().parent
PREF = ROOT / "local" / "preferences" / "playground_settings.json"
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

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.prefs = load_preferences()
        self.client = MultiplayerClient(self)
        self.joined = False
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
        self.stage = RoomView()
        self.stage.setObjectName("stage")
        pl.addWidget(self.stage, 1)
        self._refresh_wallpapers()
        self.stack.addWidget(self.playground)

        self.room = QWidget()
        rl = QVBoxLayout(self.room)
        rl.setContentsMargins(16,12,16,12)
        rl.setSpacing(16)
        self.room_title = QLabel()
        self.room_title.setObjectName("cardTitle")
        rl.addWidget(self.room_title)
        self.room_note = QLabel()
        self.room_note.setObjectName("muted")
        rl.addWidget(self.room_note)
        form = QFrame()
        form.setObjectName("card")
        fl = QVBoxLayout(form)
        fl.setContentsMargins(22,22,22,22)
        fl.setSpacing(11)
        self.url_label = QLabel()
        fl.addWidget(self.url_label)
        self.url_input = QLineEdit(DEFAULT_URL)
        fl.addWidget(self.url_input)
        self.id_label = QLabel()
        fl.addWidget(self.id_label)
        self.id_input = QLineEdit(os.environ.get("VELIA_MULTIPLAYER_ROOM", "public-01"))
        fl.addWidget(self.id_input)
        self.nick_label = QLabel()
        fl.addWidget(self.nick_label)
        self.nick_input = QLineEdit()
        self.nick_input.setMaxLength(24)
        fl.addWidget(self.nick_input)
        controls=QHBoxLayout()
        self.connect_btn = self._button(action=self._join,kind="primary")
        self.disconnect_btn = self._button(action=self._disconnect)
        self.disconnect_btn.setEnabled(False)
        controls.addWidget(self.connect_btn)
        controls.addWidget(self.disconnect_btn)
        controls.addStretch()
        fl.addLayout(controls)
        rl.addWidget(form)
        self.status = QLabel()
        self.status.setObjectName("muted")
        rl.addWidget(self.status)
        self.members_label = QLabel()
        self.members_label.setObjectName("cardTitle")
        rl.addWidget(self.members_label)
        self.members_view = QLabel()
        self.members_view.setObjectName("muted")
        self.members_view.setWordWrap(True)
        rl.addWidget(self.members_view)
        rl.addStretch()
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

    def _enter_playground(self):
        self._refresh_wallpapers()
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
        self.playground_desc.setText(t("角色、互动与场景系统已清空，等待重新设计。", "Character, interaction and scene systems cleared for redesign."))
        self.enter_btn.setText(t("进入游乐场 ↗", "Enter Playground ↗"))
        self.stage_title.setText(t("游乐场 / 10 × 10 m", "Playground / 10 × 10 m"))
        self.wallpaper_label.setText(t("房间壁纸", "Room wallpaper"))
        self._refresh_wallpapers()
        self.to_room.setText(t("联机房间 ↗", "Multiplayer Room ↗"))
        self.room_title.setText(t("联机房间", "Multiplayer Room"))
        self.room_note.setText(t("仅保留连接与成员列表。已移除角色、动画和所有聊天功能。", "Connections and member lists only. All avatars, animations and chat removed."))
        self.url_label.setText(t("服务器地址", "Server URL"))
        self.id_label.setText(t("房间编号", "Room ID"))
        self.nick_label.setText(t("昵称", "Nickname"))
        self.connect_btn.setText(t("连接", "Connect"))
        self.disconnect_btn.setText(t("断开", "Disconnect"))
        self.members_label.setText(t("房间成员", "Room members"))
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
        """)

    def _change_theme(self):
        self.prefs["theme"]=self.theme_combo.currentData()
        save_preferences(self.prefs)
        self._theme()

    def _change_language(self):
        self.prefs["language"]=self.language_combo.currentData()
        save_preferences(self.prefs)
        self._translate()

    def _join(self):
        url=self.url_input.text().strip()
        room=self.id_input.text().strip()
        name=self.nick_input.text().strip()
        if not url.startswith(("ws://","wss://")) or not room or not name:
            QMessageBox.warning(self,self.tr2("无效信息","Invalid input"),self.tr2("请输入有效的 WebSocket 地址、房间编号与昵称。","Enter a WebSocket URL, room ID and nickname."))
            return
        if not MULTIPLAYER_AVAILABLE:
            QMessageBox.warning(self,"WebSocket",self.tr2("缺少 QtWebSockets 模块。","QtWebSockets is unavailable."))
            return
        self.connect_btn.setEnabled(False)
        self.status.setText(self.tr2("正在连接…", "Connecting…"))
        self.client.connect_to(url)

    def _connected(self):
        self.client.send({"type":"join","data":{"room_id":self.id_input.text().strip(),"username":self.nick_input.text().strip()}})
        self.status.setText(self.tr2("已连接，正在加入房间…", "Connected, joining room…"))

    def _disconnect(self):
        self.client.close()
        self._disconnected()

    def _disconnected(self):
        self.joined=False
        self.members.clear()
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.status.setText(self.tr2("连接已断开", "Disconnected"))
        self._render_members()

    def _error(self, message):
        self._disconnected()
        self.status.setText(message)

    def _message(self, packet):
        kind=packet.get("type")
        data=packet.get("data") or {}
        if kind=="welcome":
            self.joined=True
            self.members={p.get("username","") for p in data.get("members",[]) if isinstance(p,dict)}
            self.members.add(self.nick_input.text().strip())
            self.disconnect_btn.setEnabled(True)
            self.connect_btn.setEnabled(False)
            self.status.setText(self.tr2("已加入房间", "Joined room"))
        elif kind=="member_joined":
            self.members.add(str(data.get("username", "")))
        elif kind=="member_left":
            self.members.discard(str(data.get("username", "")))
        elif kind=="error":
            self.status.setText(str(packet.get("message", "Server error")))
            self.client.close()
        self._render_members()

    def _render_members(self):
        self.members_view.setText(" · ".join(sorted(n for n in self.members if n)) or self.tr2("暂无成员", "No members yet"))

    def closeEvent(self,event):
        self.client.close()
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
