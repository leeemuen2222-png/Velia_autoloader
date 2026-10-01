"""Qt WebSocket client for Velia Multiplayer Playground.

No AI logic lives here. This module only transports room state and chat events.
"""

import json
from PySide6.QtCore import QObject, QUrl, Signal

try:
    from PySide6.QtWebSockets import QWebSocket
    MULTIPLAYER_AVAILABLE = True
except Exception:
    QWebSocket = None
    MULTIPLAYER_AVAILABLE = False


class MultiplayerClient(QObject):
    connected = Signal()
    disconnected = Signal()
    connectionError = Signal(str)
    messageReceived = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = None
        self.url = ""
        if MULTIPLAYER_AVAILABLE:
            self.socket = QWebSocket()
            self.socket.connected.connect(self.connected)
            self.socket.disconnected.connect(self.disconnected)
            self.socket.textMessageReceived.connect(self._on_text)
            try:
                self.socket.errorOccurred.connect(
                    lambda _err: self.connectionError.emit(
                        self.socket.errorString() or "WebSocket error"
                    )
                )
            except AttributeError:
                self.socket.error.connect(
                    lambda _err: self.connectionError.emit(
                        self.socket.errorString() or "WebSocket error"
                    )
                )

    def connect_to(self, url):
        self.url = str(url or "").strip()
        if not MULTIPLAYER_AVAILABLE or self.socket is None:
            self.connectionError.emit(
                "PySide6.QtWebSockets is unavailable in this installation."
            )
            return
        if not self.url:
            self.connectionError.emit("Missing WebSocket server URL.")
            return
        self.socket.open(QUrl(self.url))

    def close(self):
        if self.socket is not None:
            self.socket.close()

    def send(self, payload):
        if self.socket is None:
            return False
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            return self.socket.sendTextMessage(text) >= 0
        except (TypeError, ValueError):
            return False

    def _on_text(self, text):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            self.messageReceived.emit(payload)
