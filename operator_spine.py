"""Spine 3.8 operator overlay for Velia Playground.

Loads operator resource/op/<name>/{perf,mov}/<atlas,skel,png> without
converting atlas textures into sprites. QtWebEngine is needed to render
Spine 3.8 via the bundled official spine-player.js.
"""
import json
import re
import random
import math
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer, QEvent, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QApplication, QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QPushButton

from room_view import ROOM_SIDE_M, RoomView

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_AVAILABLE = True
except ImportError:
    WEBENGINE_AVAILABLE = False

ROOT = Path(__file__).resolve().parent
OP_DIR = ROOT / 'resource' / 'op'
RUNTIME_DIR = ROOT / 'resource' / 'spine'
BINDING_FILE = ROOT / 'local' / 'preferences' / 'operator_animation_keys.json'


def _load_animation_bindings():
    try:
        obj = json.loads(BINDING_FILE.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_animation_bindings(data):
    BINDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = BINDING_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(BINDING_FILE)

OPERATOR_HEIGHT_M = 1.70
# PERForm/MOV asset dimensions may differ; camera normalization handles both.


def _model_directory(folder, prefix, skin_number=1):
    """Resolve a numbered MOV/PERF directory, supporting optional $%$ labels."""
    candidates = []
    if not folder.is_dir():
        return None
    for item in folder.iterdir():
        if not item.is_dir():
            continue
        head = item.name.split('$%$', 1)[0]
        suffix = head[len(prefix):] if head.startswith(prefix) else None
        if suffix is None:
            continue
        number = 1 if suffix == '' else int(suffix) if suffix.isdecimal() else None
        if number == skin_number:
            candidates.append(item)
    # A duplicate model index is ambiguous; never choose a random model.
    if len(candidates) > 1:
        raise ValueError(f'Ambiguous {prefix} skin {skin_number} in {folder}')
    return candidates[0] if candidates else None


def _validated_model(path):
    if path is None:
        return None
    skels = sorted(path.glob('*.skel'))
    if len(skels) != 1:
        raise FileNotFoundError(f'{path}: expected exactly one .skel file')
    skel = skels[0]
    atlas = skel.with_suffix('.atlas')
    if not atlas.is_file():
        raise FileNotFoundError(atlas)
    lines = atlas.read_text(encoding='utf-8-sig').splitlines()
    pages = [line.strip() for line in lines if line.strip().lower().endswith('.png')]
    if not pages or any(not (path / page).is_file() for page in pages):
        raise FileNotFoundError(f'{atlas}: missing referenced PNG texture page')
    return skel, atlas


def _skin_name(path):
    return path.name.split('$%$', 1)[1].strip() if path and '$%$' in path.name else ''


def operator_skins(name):
    """Return {index: {'label','mov','perf'}} for available skins.

    For the label prefer MOV metadata over PERF metadata. Default skin is 1.
    A MOV skeleton is required; PERF is optional (MOV-only fallback).
    """
    folder = OP_DIR / name
    if not folder.is_dir():
        return {}
    numbers = set()
    for path in folder.iterdir():
        if not path.is_dir():
            continue
        base = path.name.split('$%$', 1)[0]
        if not base.startswith('mov'):
            continue
        rest = base[3:]
        if rest == '' or rest.isdecimal():
            numbers.add(int(rest) if rest else 1)
    out = {}
    for number in sorted(numbers):
        mov = _model_directory(folder, 'mov', number)
        perf = _model_directory(folder, 'perf', number)
        if mov is None or len(list(mov.glob('*.skel'))) != 1:
            continue
        label = '默认皮肤' if number == 1 else (_skin_name(mov) or _skin_name(perf) or f'皮肤 {number}')
        out[number] = {'label': label, 'mov': mov, 'perf': perf}
    return out


def load_operator(name, skin_number=1):
    """Load the requested skin pair, falling back to MOV-only when no PERF exists."""
    skin = operator_skins(name).get(skin_number)
    if skin is None:
        raise FileNotFoundError(f'{name}: skin {skin_number} does not exist')
    mov = _validated_model(skin['mov'])
    perf = _validated_model(skin['perf']) if skin['perf'] and name.casefold() != 'enya' else None
    return perf, mov


# Shared world scale for every operator: x/z 0..10 m, 1 tile = 1 m².
# Raised tiles are read from the SAME geometry function that renders the room.
ACTOR_RADIUS_M = 0.34
DEFAULT_WALK_MPS = 1.40
MOVE_RESPONSE_S = 0.085  # smooth acceleration / braking (seconds)
# Render a much larger *transparent* viewport without changing the visible
# operator height. The camera's world-space extent expands by this factor too.
SPINE_STAGE_OVERSCAN = 3.0
SPINE_FOOT_ANCHOR = 0.68  # relative to the oversized canvas, top=0



def can_stand(x, z, radius=ACTOR_RADIUS_M):
    """Circle-versus-raised-tile collision plus room boundary clearance."""
    if not (radius <= x <= ROOM_SIDE_M-radius and radius <= z <= ROOM_SIDE_M-radius):
        return False
    for row in range(ROOM_SIDE_M):
        for col in range(ROOM_SIDE_M):
            if RoomView._tile_height_m(None, row, col) <= 0:
                continue
            nearest_x = min(max(x, float(col)), col + 1.0)
            nearest_z = min(max(z, float(row)), row + 1.0)
            if (x - nearest_x)**2 + (z - nearest_z)**2 < radius**2:
                return False
    return True


def sample_spawn(rng=None):
    """Choose a safe random location, including clearance from raised steps."""
    rng = rng or random
    for _ in range(500):
        x = rng.uniform(ACTOR_RADIUS_M, ROOM_SIDE_M-ACTOR_RADIUS_M)
        z = rng.uniform(ACTOR_RADIUS_M, ROOM_SIDE_M-ACTOR_RADIUS_M)
        if can_stand(x,z):
            return x,z
    return 5.0,5.0


def speed_multiplier(name):
    path = OP_DIR / name / 'operator.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        value = float(data.get('speed_multiplier', data.get('speed multiplier', 1)))
        return value if math.isfinite(value) and value > 0 else 1.0
    except (OSError, ValueError, TypeError, AttributeError):
        return 1.0


def height_multiplier(name):
    """Visual height factor, based on a common 1.70 m reference actor.

    This is independent of source texture dimensions and movement speed.
    Invalid configuration values fall back to 1.0.
    """
    path = OP_DIR / name / 'operator.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        value = float(data.get('height_multiplier', 1.0))
        return value if math.isfinite(value) and value > 0 else 1.0
    except (OSError, ValueError, TypeError, AttributeError):
        return 1.0


def available_operators():
    """Only directories with an installed MOV skeleton are selectable."""
    if not OP_DIR.is_dir():
        return []
    return sorted((folder.name for folder in OP_DIR.iterdir()
                   if folder.is_dir() and 1 in operator_skins(folder.name)),
                  key=str.casefold)


def parse_console_entry(value):
    """Return ('command'|'speech'|'empty', payload).

    Any angle-bracket pair marks an ordinary speech line, even with //.
    """
    value = value.strip()
    if not value:
        return ('empty', '')
    if '<' in value and '>' in value and value.index('<') < value.rindex('>'):
        return ('speech', value)
    if value.startswith('//'):
        return ('command', value[2:].strip().lower())
    return ('speech', value)



_ANIM_MODIFIERS = re.compile(r"(?i)^(?P<name>[^{}\s]+)(?:\{(?P<mods>[^{}]*)\})?$")


def parse_alloc_animation(token):
    """Read a per-animation optional modifier block, without changing clip names."""
    match = _ANIM_MODIFIERS.fullmatch(token)
    if not match:
        raise ValueError('Invalid animation/modifier syntax: ' + token)
    item = {'name': match.group('name'), 'speed': 1.0, 'flip': 'auto', 'play': 'normal', 'loop': False}
    if match.group('mods') is not None:
        if not match.group('mods').strip():
            return item
        used = set()
        for part in match.group('mods').split(','):
            if '=' not in part:
                raise ValueError('Expected key=value in modifier: ' + part)
            key, value = (piece.strip().lower() for piece in part.split('=', 1))
            if key in used or key not in ('speed', 'flip', 'play', 'loop'):
                raise ValueError('Unknown or duplicate animation modifier: ' + key)
            used.add(key)
            if key == 'speed':
                try:
                    value = float(value)
                except ValueError:
                    raise ValueError('Invalid speed') from None
                if not math.isfinite(value) or value <= 0:
                    raise ValueError('Speed must be positive and finite')
            elif key == 'flip' and value not in ('auto','up','down','left','right'):
                raise ValueError('flip must be auto/up/down/left/right')
            elif key == 'play' and value not in ('normal','backward'):
                raise ValueError('play must be normal/backward')
            elif key == 'loop':
                if value not in ('true', 'false'):
                    raise ValueError('loop must be true/false')
                value = value == 'true'
            item[key] = value
    return item


CONSOLE_COMMANDS = (
    '//help', '//op sleep', '//op wake', '//op seize ', '//op list',
    '//releaseall', '//skin list', '//skin pick ', '//perfmode true',
    '//perfmode false', '//ani list', '//ani display ',
    '//command autocomplete true', '//command autocomplete false',
    '//command off', '//command on', '//ani alloc ', '//ani clearalloc',
)


class CommandLineEdit(QLineEdit):
    """Console input with Enter completion and PageUp/PageDown command history."""
    def __init__(self, console):
        super().__init__(console)
        self.console = console

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_PageUp:
            self.console._history_step(-1)
            event.accept()
            return
        if key == Qt.Key.Key_PageDown:
            self.console._history_step(1)
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not self.console._history_recalled and self.console._accept_completion():
                event.accept()
                return
            self.console._submit()
            event.accept()
            return
        super().keyPressEvent(event)


class RoomConsole(QFrame):
    """Bottom overlay: command input now, reusable speech log for multiplayer later."""
    def __init__(self, room):
        super().__init__(room)
        self.room = room
        self._autocomplete_enabled = True
        self._commands_enabled = True
        self._history = []
        self._history_index = None
        self._history_draft = ''
        self._completion = None
        self._history_recalled = False
        self.setObjectName('roomConsole')
        self.setStyleSheet("""
            QFrame#roomConsole { background: rgba(0, 0, 0, 145);
                border: 1px solid rgba(255,255,255,65); border-radius: 14px; }
            QPlainTextEdit { color: #f4f5f6; background: transparent; border: none;
                selection-background-color: #667080; font: 12px Consolas; }
            QLineEdit { color: white; background: transparent; border: none;
                selection-background-color: #596474; font: 13px Consolas; }
            QLabel { color: #d1d5db; background: transparent; border: none;
                font: bold 14px Consolas; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.log, 1)
        self.suggestion = QLabel('', self)
        self.suggestion.setStyleSheet('color: #aabbd3; font: 12px Consolas;')
        self.suggestion.hide()
        layout.addWidget(self.suggestion)
        line = QHBoxLayout()
        line.setSpacing(9)
        line.addWidget(QLabel('>', self))
        self.edit = CommandLineEdit(self)
        self.edit.setPlaceholderText('type a command or message use //help to see a list of command')
        self.edit.textChanged.connect(self._on_input_changed)
        self.edit.textEdited.connect(self._reset_history_browse)
        line.addWidget(self.edit, 1)
        layout.addLayout(line)
        self.hide()

    def _on_input_changed(self, value):
        self._completion = None
        if not self._autocomplete_enabled or not value.startswith('//'):
            self.suggestion.hide()
            return
        pool = CONSOLE_COMMANDS if self._commands_enabled else ('//command on',)
        candidate = next((item for item in pool if item.casefold().startswith(value.casefold())
                          and item.casefold() != value.casefold()), None)
        if candidate is None:
            self.suggestion.hide()
            return
        self._completion = candidate
        self.suggestion.setText('ENTER  →  ' + candidate)
        self.suggestion.show()

    def _accept_completion(self):
        if not self._completion or not self._autocomplete_enabled:
            return False
        target = self._completion
        self.edit.setText(target)
        self._history_recalled = False
        self.edit.setCursorPosition(len(target))
        # A completed command is executed on the next Enter; never execute it
        # merely because an autocomplete suggestion was accepted.
        return True

    def _reset_history_browse(self, _value):
        # Only actual user edits reset navigation; setText() during history
        # traversal must not reset the index.
        self._history_index = None
        self._history_draft = ''
        self._history_recalled = False

    def _history_step(self, direction):
        if not self._history:
            self.suggestion.setText('No command history yet.')
            self.suggestion.show()
            return
        if self._history_index is None:
            self._history_draft = self.edit.text()
            self._history_index = len(self._history)
        self._history_index = min(len(self._history), max(0, self._history_index + direction))
        value = (self._history_draft if self._history_index == len(self._history)
                 else self._history[self._history_index])
        self.edit.setText(value)
        self.edit.setCursorPosition(len(value))
        self._history_recalled = self._history_index != len(self._history)

    def _submit(self):
        raw = self.edit.text()
        self.edit.clear()
        self._history_index = None
        self._history_draft = ''
        self._history_recalled = False
        kind, content = parse_console_entry(raw)
        if kind == 'empty':
            return
        self.log.appendPlainText('> ' + raw.strip())
        if kind == 'command':
            if not self._history or self._history[-1] != raw.strip():
                self._history.append(raw.strip())
                self._history = self._history[-100:]
            if content == 'command on':
                self._commands_enabled = True
                self.log.appendPlainText('System: Command interface enabled.')
            elif not self._commands_enabled:
                self.log.appendPlainText('System: Commands disabled. Use //command on.')
            elif content == 'command off':
                self._commands_enabled = False
                self.log.appendPlainText('System: Commands disabled. Use //command on to restore.')
            elif content.startswith('command autocomplete'):
                pieces = content.split()
                if len(pieces) != 3 or pieces[2] not in ('true', 'false'):
                    self.log.appendPlainText('System: Usage //command autocomplete true|false')
                else:
                    self._autocomplete_enabled = pieces[2] == 'true'
                    self._on_input_changed(self.edit.text())
                    self.log.appendPlainText('System: Autocomplete ' + ('enabled.' if self._autocomplete_enabled else 'disabled.'))
            elif content == 'help':
                self.log.appendPlainText(
                    'Available commands:\n'
                    '//help — Show all available commands and their functions.\n'
                    '//command autocomplete true|false — Toggle command completion.\n'
                    '//command off — Disable all commands except //command on.\n'
                    '//command on — Restore command access.\n'
                    '//op sleep — Enter sleep mode, stop movement, and loop Sleep.\n'
                    '//op wake — Exit sleep mode and resume movement and Relax.\n'
                    '//op seize NAME — Control an operator already in the room.\n'
                    '//op list — List all installed operators.\n'
                    '//releaseall — Release all operators from the room.\n'
                    '//skin list — List skins for the controlled operator.\n'
                    '//skin pick NUMBER — Switch the controlled operator to a skin.\n'
                    '//perfmode [true|false] — Toggle PERF idle and lower-body MOV blend.\n'
                    '//ani list — List MOV and PERF animation names.\n'
                    '//ani display NAME [SPEED] [loop] — Play an animation.\n'
                    '//ani alloc ANIMATION{modifiers} [...] KEY — Bind an animation sequence to a key.\n'
                    '//ani clearalloc — Remove all custom animation key bindings.\n'
                    'Modifier: {speed=NUMBER,flip=auto|up|down|left|right,play=normal|backward,loop=true|false}; fields in any order; auto faces with operator.\n'
                    'E in PERF mode — Play Attack (or Attack_1) without stopping walking.\n'
                    'Tip: Commands begin with //; other text is speech. '
                    'Text containing <...> is always speech.'
                )
            elif content == 'op sleep':
                self.room.controlled_actor().set_sleeping(True) if self.room.controlled_actor() else None
                self.log.appendPlainText('System: Sleeping.')
            elif content == 'op wake':
                self.room.controlled_actor().set_sleeping(False) if self.room.controlled_actor() else None
                self.log.appendPlainText('System: Awake.')
            elif content == 'skin list':
                actor = self.room.controlled_actor()
                if actor is None:
                    self.log.appendPlainText('System: No operator is currently controlled.')
                else:
                    skins = operator_skins(actor.operator_name)
                    self.log.appendPlainText('Available skins (' + actor.operator_name + '):\n' +
                                             ('\\n'.join(f'{idx}. {entry["label"]}' for idx, entry in skins.items())
                                              or '(none)'))
            elif content.startswith('skin pick'):
                pieces = content.split()
                actor = self.room.controlled_actor()
                if len(pieces) != 3 or not pieces[2].isdecimal():
                    self.log.appendPlainText('System: Usage //skin pick NUMBER')
                elif actor is None:
                    self.log.appendPlainText('System: No operator is currently controlled.')
                elif actor.select_skin(int(pieces[2])):
                    self.log.appendPlainText(f'System: {actor.operator_name} skin {pieces[2]} selected.')
                else:
                    self.log.appendPlainText('System: Skin unavailable or could not load.')
            elif content == 'perfmode' or content.startswith('perfmode '):
                actor = self.room.controlled_actor()
                args = content.split()
                if len(args) > 2 or (len(args) == 2 and args[1] not in ('true', 'false')):
                    self.log.appendPlainText('System: Usage //perfmode [true|false]')
                elif actor is None:
                    self.log.appendPlainText('System: No controlled operator.')
                else:
                    target = (not actor._perf_mode) if len(args) == 1 else args[1] == 'true'
                    actor.set_perf_mode(target, self.log.appendPlainText)
            elif content == 'ani list':
                actor = self.room.controlled_actor()
                if actor is None:
                    self.log.appendPlainText('System: No controlled operator.')
                else:
                    actor.list_animations(self.log.appendPlainText)
            elif content.startswith('ani display'):
                actor = self.room.controlled_actor()
                args = content.split()
                if actor is None:
                    self.log.appendPlainText('System: No controlled operator.')
                elif len(args) < 3 or len(args) > 5:
                    self.log.appendPlainText('System: Usage //ani display NAME [SPEED] [loop]')
                else:
                    loop = args[-1] == 'loop'
                    body = args[2:-1] if loop else args[2:]
                    speed = 1.0
                    if len(body) == 2:
                        try:
                            speed = float(body[-1])
                        except ValueError:
                            speed = -1
                        name = body[0]
                    elif len(body) == 1:
                        name = body[0]
                    else:
                        name = ''
                    if not name or not math.isfinite(speed) or speed <= 0:
                        self.log.appendPlainText('System: Usage //ani display NAME [SPEED>0] [loop]')
                    else:
                        actor.display_animation(name, speed, loop, self.log.appendPlainText)
            elif content == 'ani clearalloc':
                self.room._animation_bindings.clear()
                try:
                    _save_animation_bindings(self.room._animation_bindings)
                    self.log.appendPlainText('System: All animation allocations cleared.')
                except OSError as error:
                    self.log.appendPlainText(f'System: Bindings cleared in memory but save failed: {error}')
            elif content.startswith('ani alloc'):
                args = content.split()
                actor = self.room.controlled_actor()
                if actor is None:
                    self.log.appendPlainText('System: No controlled operator.')
                elif len(args) < 4:
                    self.log.appendPlainText('System: Usage //ani alloc ANIMATION{modifiers} [ANIMATION{modifiers} ...] KEY')
                else:
                    keyname = args[-1].upper()
                    tokens = args[2:-1]
                    # Preserve legacy trailing loop/single in pre-existing commands.
                    sequence_loop = False
                    if tokens and tokens[-1].lower() in ('loop','single'):
                        sequence_loop = tokens.pop().lower() == 'loop'
                    try:
                        clips = [parse_alloc_animation(token) for token in tokens]
                        if not clips:
                            raise ValueError('At least one animation is required')
                        if not re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[0-2])|SPACE', keyname) or keyname in ('W','A','S','D','C','E'):
                            raise ValueError('Invalid or reserved key (W/A/S/D/C/E)')
                    except ValueError as exc:
                        self.log.appendPlainText('System: ' + str(exc))
                    else:
                        key = actor.operator_name + ':' + str(actor.skin_number)
                        self.room._animation_bindings.setdefault(key, {})[keyname] = {
                            'clips': clips, 'loop': sequence_loop or any(c['loop'] for c in clips)}
                        try:
                            _save_animation_bindings(self.room._animation_bindings)
                            self.log.appendPlainText(f'System: {keyname} bound to ' +
                                ' → '.join(x['name'] for x in clips) +
                                (' (sequence loop).' if sequence_loop else ' (single sequence).'))
                        except OSError as error:
                            self.log.appendPlainText(f'System: Could not save binding: {error}')
            elif content == 'op list':
                self.log.appendPlainText('Operators: ' + (', '.join(available_operators()) or '(none)'))
            elif content == 'releaseall':
                self.room.release_all()
                self.log.appendPlainText('System: All operators released.')
            elif content.startswith('op seize '):
                target = content[len('op seize '):].strip()
                if self.room.pick_operator(target):
                    self.log.appendPlainText('System: Controlling ' + self.room.controlled_actor().operator_name)
                else:
                    self.log.appendPlainText('System: Operator not present. Use //op list.')
            else:
                self.log.appendPlainText('System: Unknown command.')
        else:
            # Keep speech local in this single-player view. Multiplayer transport
            # will be wired when the networked room stage is implemented.
            self.log.appendPlainText('You: ' + content)


class SpineRoomView(RoomView):
    controlledChanged = Signal()
    """Room with one initially stationary Spine 3.8 operator above the floor."""
    def __init__(self, parent=None, operator_name='Enya'):
        super().__init__(parent)
        self.operator_name = operator_name
        self.skin_number = 1
        self.spawn_x, self.spawn_z = sample_spawn()
        self.overlay = None
        self.status = None
        self._ready = False
        self._deploying = False
        self._load_generation = 0
        self._poll_count = 0
        self._ready_poll = QTimer(self)
        self._ready_poll.setInterval(150)
        self._ready_poll.timeout.connect(self._poll_spine_readiness)
        self._held_keys = set()
        self._last_tick = time.monotonic()
        self._moving_animation = False
        self._sleeping = False
        self._perf_mode = False
        self._animation_bindings = _load_animation_bindings()
        self._facing_left = False
        self._velocity_x = 0.0
        self._velocity_z = 0.0
        self._last_depth_scale = None
        self._walk_timer = QTimer(self)
        self._walk_timer.setInterval(16)
        self._walk_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._walk_timer.timeout.connect(self._advance_motion)
        self._walk_timer.start()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # App-level filter keeps WASD functional even if a Qt button has focus.
        QApplication.instance().installEventFilter(self)
        self._is_npc = False
        self._npc_timer_end = 0.0
        self._npc_direction = (0, 0)
        self._actors = {}  # owned by root room; independent Spine instances
        self._control_name = operator_name
        if operator_name is not None:
            self._request_setup()
        self.console = RoomConsole(self)
        self.console.setGeometry(18, max(10, self.height()-225), max(200, self.width()-36), 205)
        self._keep_console_on_top()

    def _keep_console_on_top(self):
        """Depth-sort operators back-to-front; console always above all."""
        root = self
        while isinstance(root.parentWidget(), SpineRoomView):
            root = root.parentWidget()
        if root is not self:
            root._keep_console_on_top()
            return
        if not hasattr(root, 'console'):
            return
        layers = []
        if root.overlay is not None:
            layers.append((root.spawn_z, root.operator_name or '', root.overlay))
        for name, actor in root._actors.items():
            if actor.overlay is not None:
                # The ACTOR widget, not the inner WebEngine child, is a
                # sibling of the root overlay; Qt sibling z-order applies here.
                layers.append((actor.spawn_z, name, actor))
        layers.sort(key=lambda x: (x[0], x[1]))
        identity = tuple(id(layer) for _, _, layer in layers)
        # Reorder only when two actors cross in depth, are added, or removed.
        if identity != getattr(root, '_layer_order', None):
            for _, _, layer in layers:
                layer.raise_()
            root._layer_order = identity
            root.console.raise_()
        elif root.console.isVisible():
            # No repeated native-window reshuffle during normal movement.
            pass

    def controlled_actor(self):
        if self._control_name == self.operator_name and self.operator_name:
            return self
        return self._actors.get(self._control_name)

    def add_operator(self, name):
        canonical = next((n for n in available_operators() if n.casefold() == name.casefold()), None)
        if not canonical or canonical == self.operator_name or canonical in self._actors:
            return False
        if self.operator_name is None:
            self.operator_name = canonical
            self._control_name = canonical
            self.spawn_x, self.spawn_z = sample_spawn()
            self._request_setup()
        else:
            actor = SpineRoomView(self, operator_name=canonical)
            # Additional actor layers never repaint the walls or floor.
            actor.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
            actor.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            actor.paintEvent = lambda event: None
            actor.console.hide()
            QApplication.instance().removeEventFilter(actor)
            actor._is_npc = True
            actor.setGeometry(self.rect())
            actor.show()
            self._actors[canonical] = actor
        self._keep_console_on_top()
        self.controlledChanged.emit()
        return True

    def pick_operator(self, name):
        name = name.casefold().strip()
        available = {n.casefold(): n for n in self.placed_operator_names()}
        if name not in available:
            return False
        chosen = available[name]
        for old in ([self] if self.operator_name else []) + list(self._actors.values()):
            if old.operator_name != chosen and old._perf_mode:
                old.set_perf_mode(False)
        self._control_name = chosen
        self._is_npc = chosen != self.operator_name
        self._held_keys.clear()
        for actor in self._actors.values():
            actor._held_keys.clear()
            actor._is_npc = actor.operator_name != chosen
        self.controlledChanged.emit()
        return True

    def placed_operator_names(self):
        return ([self.operator_name] if self.operator_name else []) + list(self._actors)

    def release_all(self):
        for actor in list(self._actors.values()):
            actor.close()
            actor.setParent(None)
            actor.deleteLater()
        self._actors.clear()
        self._control_name = None
        self._held_keys.clear()
        self._is_npc = False
        if self.overlay:
            self.overlay.stop()
            self.overlay.deleteLater()
            self.overlay = None
        self._ready_poll.stop()
        self._ready = False
        self.operator_name = None
        self._keep_console_on_top()
        self.controlledChanged.emit()

    def _npc_decide(self, now):
        if now < self._npc_timer_end:
            return
        self._held_keys.clear()
        if self._npc_direction != (0, 0):
            self._npc_direction = (0, 0)
            self._npc_timer_end = now + random.uniform(7.0, 16.0)
        else:
            self._npc_direction = random.choice(((1, 0), (-1, 0), (0, 1), (0, -1),
                                                 (1, 1), (-1, 1), (1, -1), (-1, -1)))
            self._npc_timer_end = now + random.uniform(0.8, 2.2)
        x, z = self._npc_direction
        if x < 0: self._held_keys.add('a')
        if x > 0: self._held_keys.add('d')
        if z < 0: self._held_keys.add('w')
        if z > 0: self._held_keys.add('s')

    def select_skin(self, skin_number):
        """Switch a live actor's skeletons without moving its world position."""
        if skin_number not in operator_skins(self.operator_name):
            return False
        if self.skin_number == skin_number:
            return True
        try:
            # Resolve/validate first so an invalid skin keeps the current player.
            load_operator(self.operator_name, skin_number)
        except (OSError, ValueError) as error:
            print('[Velia Spine] Skin selection failed:', error, flush=True)
            return False
        previous_overlay = self.overlay
        self._ready_poll.stop()
        self._ready = False
        self._deploying = False
        self._poll_count = 0
        self._moving_animation = False
        self._last_depth_scale = None
        self.skin_number = skin_number
        self.overlay = None
        if previous_overlay is not None:
            previous_overlay.stop()
            previous_overlay.hide()
            previous_overlay.deleteLater()
        self._request_setup()
        return self.overlay is not None

    def _js_command(self, expression, callback=None):
        if not self.overlay or not self._ready or self._deploying:
            if callback:
                callback('System: Wait for deployment to finish.')
            return
        if callback:
            def received(result):
                try:
                    callback(str(result) if result is not None else 'System: Spine response unavailable.')
                except RuntimeError:
                    pass
            self.overlay.page().runJavaScript(expression, received)
        else:
            self.overlay.page().runJavaScript(expression)

    def set_perf_mode(self, enabled, callback=None):
        self._perf_mode = bool(enabled)
        self._js_command('window.veliaPerfMode && window.veliaPerfMode(' +
                         ('true' if enabled else 'false') + ')', callback)

    def attack_perf(self):
        self._js_command('window.veliaPerfAttack && window.veliaPerfAttack()')

    def list_animations(self, callback):
        self._js_command('window.veliaAnimationList && window.veliaAnimationList()', callback)

    def display_animation(self, name, speed=1.0, loop=False, callback=None):
        js = 'window.veliaDisplayAnimation && window.veliaDisplayAnimation(' + ','.join((
            json.dumps(name, ensure_ascii=False), str(float(speed)),
            'true' if loop else 'false')) + ')'
        self._js_command(js, callback)

    def display_sequence(self, names, speed=1.0, loop=False, callback=None):
        if not names:
            return
        js = 'window.veliaDisplaySequence && window.veliaDisplaySequence(' + ','.join((
            json.dumps(list(names), ensure_ascii=False), str(float(speed)),
            'true' if loop else 'false')) + ')'
        self._js_command(js, callback)

    def _request_setup(self):
        if not WEBENGINE_AVAILABLE:
            self._warning('Qt WebEngine is required: pip install PySide6-Addons')
            return
        try:
            (perf_info, mov_info) = load_operator(self.operator_name, self.skin_number)
            perf = perf_info[0] if perf_info else None
            mov = mov_info[0]
            js = RUNTIME_DIR / 'spine-player.js'
            css = RUNTIME_DIR / 'spine-player.css'
            if not (js.is_file() and css.is_file()):
                raise FileNotFoundError('Missing resource/spine/spine-player.js or spine-player.css')
        except (OSError, ValueError) as exc:
            self._warning(str(exc))
            return
        self._layer_order = None
        self._deploying = perf is not None
        self._held_keys.clear()
        self._velocity_x = self._velocity_z = 0.0
        self.overlay = QWebEngineView(self)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.overlay.setStyleSheet('background:transparent;border:none;')
        self.overlay.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.overlay.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.overlay.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.overlay.setMouseTracking(False)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # NEVER hide the WebEngine: hidden Chromium surfaces may not render,
        # so Spine never reaches the first-frame readiness callback.
        # Instead keep it alive completely outside the visible room until ready.
        self.overlay.loadFinished.connect(self._on_load)
        self.overlay.titleChanged.connect(self._on_spine_title)
        self._fit_overlay()
        # A local HTML document is constructed from the official Spine 3.8 player.
        html = (self._html_mov_only(mov, js, css) if perf is None
                else self._html(perf, mov, js, css))
        self.overlay.setHtml(html, QUrl.fromLocalFile(str(ROOT) + '/'))
        self.overlay.show()
        self._ready_poll.start()

    def _warning(self, message):
        self.status = QLabel('Spine: ' + message, self)
        self.status.setStyleSheet('background:#fff8e0;color:#513e00;padding:7px;border-radius:5px;')
        self.status.move(12, 12)
        self.status.adjustSize()
        self.status.show()

    def eventFilter(self, obj, event):
        typ = event.type()
        if typ not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return super().eventFilter(obj, event)
        if not self.isVisible() or not self.isVisibleTo(self.window()):
            self._held_keys.clear()
            return super().eventFilter(obj, event)
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit,QTextEdit,QPlainTextEdit,QSpinBox,QDoubleSpinBox,QComboBox)):
            self._held_keys.clear()
            return super().eventFilter(obj, event)
        if event.key() == Qt.Key.Key_C and typ == QEvent.Type.KeyPress and not event.isAutoRepeat():
            self.toggle_console()
            return True
        controlled = self.controlled_actor()
        if self.console.isVisible() or controlled is None or controlled._sleeping or controlled._deploying:
            self._held_keys.clear()
            return super().eventFilter(obj, event)
        if typ == QEvent.Type.KeyPress and not event.isAutoRepeat():
            if event.key() == Qt.Key.Key_E and controlled._perf_mode:
                controlled.attack_perf()
                return True
            keyname = ('SPACE' if event.key() == Qt.Key.Key_Space else
                       (event.text().upper() if event.text() and len(event.text()) == 1 else
                        ('F' + str(event.key()-Qt.Key.Key_F1+1) if Qt.Key.Key_F1 <= event.key() <= Qt.Key.Key_F12 else '')))
            bindings = self._animation_bindings.get(controlled.operator_name + ':' + str(controlled.skin_number), {})
            binding = bindings.get(keyname)
            if binding:
                clips = binding.get('clips') or binding.get('animations', [binding['animation']] if 'animation' in binding else [])
                looping = bool(binding.get('loop', False)) or any(isinstance(c, dict) and c.get('loop', False) for c in clips)
                if looping and getattr(controlled, '_active_alloc_key', None) == keyname:
                    controlled._active_alloc_key = None
                    controlled._js_command('window.veliaStopSequence && window.veliaStopSequence()')
                else:
                    controlled._active_alloc_key = keyname if looping else None
                    controlled.display_sequence(clips, binding.get('speed', 1.0), looping)
                return True
        key_map = {Qt.Key.Key_W:'w',Qt.Key.Key_A:'a',Qt.Key.Key_S:'s',Qt.Key.Key_D:'d'}
        key = key_map.get(event.key())
        if key is None:
            return super().eventFilter(obj, event)
        if not event.isAutoRepeat():
            if typ == QEvent.Type.KeyPress:
                controlled._held_keys.add(key)
            else:
                controlled._held_keys.discard(key)
        return True

    def _set_motion_animation(self, moving, facing_left):
        if self.overlay is None or not self._ready:
            return
        if moving == self._moving_animation and facing_left == self._facing_left:
            return
        self._moving_animation, self._facing_left = moving, facing_left
        self.overlay.page().runJavaScript(
            f'window.veliaSetMotion && window.veliaSetMotion({str(moving).lower()},'
            f'{str(facing_left).lower()});')

    def _advance_motion(self):
        now = time.monotonic()
        dt = min(max(0.0, now - self._last_tick), 0.045)
        self._last_tick = now
        if not self._ready or self._deploying or not self.isVisible():
            self._held_keys.clear()
            self._velocity_x = self._velocity_z = 0.0
            return

        if self._is_npc and not self._sleeping:
            self._npc_decide(now)
        if self._sleeping:
            self._held_keys.clear()
            self._velocity_x = self._velocity_z = 0.0
            return
        input_x = float('d' in self._held_keys) - float('a' in self._held_keys)
        input_z = float('s' in self._held_keys) - float('w' in self._held_keys)
        distance = math.hypot(input_x, input_z)
        if distance:
            input_x /= distance
            input_z /= distance
        multiplier = speed_multiplier(self.operator_name)
        desired_x = DEFAULT_WALK_MPS * multiplier * input_x
        desired_z = DEFAULT_WALK_MPS * multiplier * input_z
        # Framerate-independent exponential ease, smoothing both starting and stopping.
        blend = 1.0 - math.exp(-dt / MOVE_RESPONSE_S)
        self._velocity_x += (desired_x - self._velocity_x) * blend
        self._velocity_z += (desired_z - self._velocity_z) * blend
        if distance == 0 and math.hypot(self._velocity_x, self._velocity_z) < 0.025:
            self._velocity_x = self._velocity_z = 0.0

        delta_x = self._velocity_x * dt
        delta_z = self._velocity_z * dt
        old_x, old_z = self.spawn_x, self.spawn_z
        # Wall-sliding with axis-separated collision.
        if can_stand(old_x + delta_x, old_z):
            self.spawn_x += delta_x
        else:
            self._velocity_x = 0.0
        if can_stand(self.spawn_x, old_z + delta_z):
            self.spawn_z += delta_z
        else:
            self._velocity_z = 0.0
        if self.spawn_x != old_x or self.spawn_z != old_z:
            self._fit_overlay()
        moving = distance > 0 or math.hypot(self._velocity_x, self._velocity_z) > 0.09
        facing_left = self._facing_left
        if input_x < 0:
            facing_left = True
        elif input_x > 0:
            facing_left = False
        self._set_motion_animation(moving, facing_left)

    def _on_load(self, ok):
        # HTML loadFinished occurs BEFORE skeleton and texture readiness.
        if not ok:
            self._load_failed('Spine HTML could not be loaded')
        elif not self._ready:
            self._ready_poll.start()

    def _load_failed(self, message):
        self._ready = False
        self._deploying = False
        self._ready_poll.stop()
        # Keep the failed renderer outside the room; never show loading UI.
        if self.overlay is not None:
            self.overlay.setGeometry(-10000, -10000, 320, 320)
        print('[Velia Spine] ' + str(message), flush=True)

    def _poll_spine_readiness(self):
        if (self._ready and not self._deploying) or self.overlay is None:
            self._ready_poll.stop()
            return
        self._poll_count += 1
        # Qt may coalesce titleChanged events in an offscreen WebEngine.
        # Read the page title as a fallback while the renderer stays alive.
        try:
            self.overlay.page().runJavaScript('document.title', self._on_spine_title)
        except RuntimeError:
            self._ready_poll.stop()
        if self._poll_count >= 200 and not self._ready:
            self._load_failed('Timed out waiting for Spine/Relax readiness (30 seconds)')

    def _on_spine_title(self, title):
        if title == 'VELIA_SPINE_READY' and not self._ready:
            # Deploy immediately once, keeping the selected random spawn.
            self._ready = True
            if not self._deploying:
                self._ready_poll.stop()
            self._moving_animation = False
            self._facing_left = False
            self._last_tick = time.monotonic()
            self._last_depth_scale = None
            if self.overlay is not None:
                self._fit_overlay()
                self.overlay.show()
                self._keep_console_on_top()
                QTimer.singleShot(0, self._keep_console_on_top)
                if self._perf_mode and not self._deploying:
                    self.set_perf_mode(True)
        elif title == 'VELIA_SPINE_DEPLOYED' and self._deploying:
            # Qt can coalesce rapid HTML title changes: deployment must also
            # bring an actor on screen when the READY title was missed.
            if not self._ready:
                self._on_spine_title('VELIA_SPINE_READY')
            # PERF/Start has completed and MOV/Relax is already active.
            self._deploying = False
            self._ready_poll.stop()
            self._held_keys.clear()
            self._npc_direction = (0, 0)
            self._npc_timer_end = time.monotonic() + random.uniform(7.0, 16.0) if self._is_npc else 0.0
            self._velocity_x = self._velocity_z = 0.0
            self._last_tick = time.monotonic()
            self._moving_animation = False
            if self._perf_mode:
                self.set_perf_mode(True)
        elif title == 'VELIA_SPINE_ERROR' and not self._ready:
            self._load_failed('Spine reported a skeleton / texture error')


    def _html_mov_only(self, mov_skel, js, css):
        # Enya exception: MOV only.
        def file_url(path):
            return QUrl.fromLocalFile(str(path)).toString()
        conf = json.dumps({
            'movSkel': file_url(mov_skel),
            'movAtlas': file_url(mov_skel.with_suffix('.atlas')),
            'motionSpeed': speed_multiplier(self.operator_name),
        }, ensure_ascii=False)
        # JSON is injected as data; JS/CSS resources remain local to this project.
        return """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="%s"><style>
html,body{width:100%%;height:100%%;overflow:hidden;margin:0;background:transparent!important}
#mov{position:absolute;inset:0;background:transparent!important}
.spine-player{height:100%%!important;background:transparent!important}
.spine-player-canvas{background:transparent!important}
.spine-player-controls,.spine-player-error,.spine-player-loading,.spine-player-logo,
.spine-player-progress,.spine-player-loading-screen{display:none!important}
</style></head><body><div id="mov"></div><script src="%s"></script>
<script>
const files=%s;
const params={alpha:true,showControls:false,backgroundColor:'#00000000',
  premultipliedAlpha:true,showLoading:false,defaultMix:0};
function normalizeStage(player){
  if(!player || !player.skeleton || !player.currentViewport) return;
  player.skeleton.updateWorldTransform();
  const offset=new spine.Vector2(), size=new spine.Vector2();
  player.skeleton.getBounds(offset,size);
  if(!(size.x>0 && size.y>0)) return;
  const box=player.parent.getBoundingClientRect();
  const ratio=Math.max(.1,box.width/Math.max(1,box.height));
  const baseWorldHeight=Math.max(size.y/.80, size.x/(ratio*.85));
  const worldHeight=baseWorldHeight*3.0;
  const worldWidth=worldHeight*ratio;
  player.currentViewport={x:offset.x+size.x/2-worldWidth/2,
    y:offset.y-worldHeight*.32,width:worldWidth,height:worldHeight,
    padLeft:0,padRight:0,padTop:0,padBottom:0};
  player.previousViewport=null;
}
let veliaMovPlayer=null;
let veliaMotion='Relax';
let veliaSleeping=false, veliaInteracting=false;

let veliaDisplay=null;
window.veliaAnimationList=function(){
  if(!veliaMovPlayer) return 'System: MOV not loaded.';
  const names=veliaMovPlayer.skeleton.data.animations.map(x=>x.name);
  return 'mov/\\n'+names.map(n=>'  ├─ '+n).join('\\n');
};
window.veliaDisplayAnimation=function(name,speed,loop){
  if(!veliaMovPlayer) return 'System: MOV not loaded.';
  name=name.replace(/^mov\\//i,'');
  const matched=veliaMovPlayer.skeleton.data.animations.find(a=>a.name.toLowerCase()===name.toLowerCase());
  if(matched) name=matched.name;
  const clip=veliaMovPlayer.skeleton.data.findAnimation(name);
  if(!clip) return 'System: Animation not found: '+name;
  veliaDisplay={name:name, speed:speed, loop:loop};
  const st=veliaMovPlayer.animationState;
  st.timeScale=files.motionSpeed*speed;
  st.setAnimation(0,name,loop);
  if(!loop){
    st.addAnimation(0,'Relax',true,0);
    st.addListener({complete:function(entry){
      if(veliaDisplay && veliaDisplay.name===name && entry.animation && entry.animation.name===name){
        veliaDisplay=null;st.timeScale=files.motionSpeed;veliaMotion='Relax';
      }
    }});
  }
  return 'System: Playing mov/'+name+' at '+speed+'x'+(loop?' (loop)':'');
};
window.veliaDisplaySequence=function(names,speed,loop){
  if(!Array.isArray(names)||!names.length) return 'System: Empty animation sequence.';
  const resolved=[];
  for(const raw of names){
    const input=typeof raw==='string'?{name:raw}:raw;
    if(!input||typeof input.name!=='string') return 'System: Invalid animation binding.';
    const name=input.name;
    const prefix=/^(mov|perf)\\//i.exec(name);
    const model=prefix?prefix[1].toLowerCase():null;
    const asked=prefix?name.slice(prefix[0].length):name;
    const match=(p)=>p&&p.skeleton.data.animations.find(a=>a.name.toLowerCase()===asked.toLowerCase());
    const movMatch=match(veliaMovPlayer), perfMatch=match(null);
    if(!model && movMatch && perfMatch) return 'System: Ambiguous animation: '+name;
    const kind=model||(movMatch?'mov':perfMatch?'perf':null);
    const clip=kind==='mov'?movMatch:kind==='perf'?perfMatch:null;
    if(!clip) return 'System: Animation not found: '+name;
    const partSpeed=Number(input.speed===undefined?speed:input.speed);
    const flip=input.flip||'auto', play=input.play||'normal';
    if(input.loop!==undefined && typeof input.loop!=='boolean') return 'System: Invalid loop modifier for '+name;
    if(!Number.isFinite(partSpeed)||partSpeed<=0||!['auto','up','down','left','right'].includes(flip)||!['normal','backward'].includes(play))
      return 'System: Invalid animation modifier for '+name;
    resolved.push({name:kind+'/'+clip.name,model:kind,clip:clip.name,speed:partSpeed,flip:flip,play:play});
  }
  if(window.veliaSequenceToken) window.veliaSequenceToken.cancel();
  const token={active:true,listeners:[],raf:0,restorePerf:typeof perfMode==='boolean'?perfMode:false,transforms:new Map()};
  token.cancel=function(){
    token.active=false;
    if(token.raf) cancelAnimationFrame(token.raf);
    for(const item of token.listeners) item.state.removeListener(item.listener);
    token.listeners=[];
    for(const [canvas,transform] of token.transforms) canvas.style.transform=transform;
    token.transforms.clear();
  };
  window.veliaSequenceToken=token;
  let index=0;
  function finish(){
    if(!token.active) return;
    if(index+1<resolved.length){index++;playNext();return;}
    if(loop){index=0;playNext();return;}
    token.cancel(); window.veliaSequenceToken=null;
    interruptDisplay();
  }
  function playNext(){
    if(!token.active)return;
    for(const item of token.listeners)item.state.removeListener(item.listener);
    token.listeners=[];
    if(token.raf){cancelAnimationFrame(token.raf);token.raf=0;}
    const current=resolved[index];
    const result=window.veliaDisplayAnimation(current.name,current.speed,true);
    if(typeof result==='string'&&!result.startsWith('System: Playing')){token.cancel();return;}
    if(veliaDisplay && null) veliaDisplay.returnPerf=token.restorePerf;
    const player=current.model==='perf'?null:veliaMovPlayer;
    const canvas=document.querySelector('#'+current.model+' canvas');
    if(canvas){
      if(!token.transforms.has(canvas)) token.transforms.set(canvas,canvas.style.transform);
      const original=token.transforms.get(canvas);
      const defaultLeft=typeof requestedLeft!=='undefined'?requestedLeft:/scaleX\\(-1\\)|scale\\(-1[,)]/.test(original);
      const sx=current.flip==='left'?-1:current.flip==='right'?1:defaultLeft?-1:1;
      const sy=current.flip==='down'?-1:1;
      canvas.style.transform='scale('+sx+','+sy+')';
    }
    const state=player.animationState;
    const entry=state.getCurrent(0);
    if(!entry){token.cancel();return;}
    if(current.play==='backward'){
      // Spine 3.8 does not support reverse track update. Sample trackTime
      // backwards while keeping the built-in renderer and animation state alive.
      const duration=entry.animation.duration;
      const began=performance.now();
      state.timeScale=0;
      const tick=(now)=>{
        if(!token.active||window.veliaSequenceToken!==token)return;
        const elapsed=(now-began)/1000*files.motionSpeed*current.speed;
        entry.trackTime=Math.max(0.00001,duration-elapsed);
        if(elapsed>=duration){finish();return;}
        token.raf=requestAnimationFrame(tick);
      };
      token.raf=requestAnimationFrame(tick);
    }else{
      const listener={complete:function(done){
        if(!token.active||window.veliaSequenceToken!==token||done!==entry)return;
        state.removeListener(listener);
        token.listeners=token.listeners.filter(x=>x.listener!==listener);
        finish();
      }};
      state.addListener(listener);
      token.listeners.push({state:state,listener:listener});
    }
  }
  playNext();
  return 'System: Playing sequence: '+resolved.map(x=>x.name).join(' -> ');
};
window.veliaStopSequence=function(){
  if(!window.veliaSequenceToken || !window.veliaSequenceToken.active) return 'System: No active sequence.';
  interruptDisplay();
  return 'System: Animation sequence stopped.';
};
function interruptDisplay(){
  if(window.veliaSequenceToken){
    window.veliaSequenceToken.cancel();
    window.veliaSequenceToken=null;
  }
  if(!veliaDisplay) return;
  veliaDisplay=null;
  veliaMovPlayer.animationState.timeScale=files.motionSpeed;
}
window.veliaPerfMode=function(flag){return 'System: PERF model is unavailable for this operator.';};
window.veliaSetSleep=function(flag){
  interruptDisplay();
  veliaSleeping=!!flag; veliaInteracting=false;
  if(!veliaMovPlayer || !veliaMovPlayer.animationState) return;
  const name=veliaSleeping?'Sleep':'Relax';
  if(veliaMovPlayer.skeleton.data.findAnimation(name)){
    veliaMotion=name; veliaMovPlayer.animationState.setAnimation(0,name,true);
  }
};
window.veliaInteract=function(){
  interruptDisplay();
  if(veliaSleeping || veliaInteracting || !veliaMovPlayer) return;
  if(!veliaMovPlayer.skeleton.data.findAnimation('Interact')) return;
  veliaInteracting=true;
  const state=veliaMovPlayer.animationState;
  state.setAnimation(0,'Interact',false);
  state.addAnimation(0,'Relax',true,0);
};

window.veliaSetDepthScale=function(scale){
  const safe=Math.max(.01,Math.min(5,Number(scale)||1));
  for(const el of [document.getElementById('mov'),document.getElementById('perf')]){
    if(!el) continue;
    el.style.transformOrigin='50%% 68%%';
    el.style.transform='scale('+safe.toFixed(6)+')';
  }
};
window.veliaSetMotion=function(moving, left){
  if(moving && veliaDisplay) interruptDisplay();
  else if(!moving && veliaDisplay) return;
  if(!veliaMovPlayer || !veliaMovPlayer.animationState || veliaSleeping) return;
  const current=veliaMovPlayer.animationState.getCurrent(0);
  // Movement interrupts Interact immediately; setAnimation also clears its queued Relax.
  if(moving && current && current.animation && current.animation.name==='Interact'){
    veliaInteracting=false;
    veliaMotion='Interact';
  } else if(!moving && current && current.animation && current.animation.name==='Interact' && !current.isComplete()) return;
  if(veliaInteracting && current && current.animation && current.animation.name==='Relax') veliaInteracting=false;
  const anim=moving && veliaMovPlayer.skeleton.data.findAnimation('Move') ? 'Move':'Relax';
  // Skeletal scale mirror: right is the original direction, left is mirrored.
  const canvas=document.querySelector('#mov canvas');
  if(canvas) canvas.style.transform=left ? 'scaleX(-1)' : 'scaleX(1)';
  if(anim!==veliaMotion){
    veliaMotion=anim;
    veliaMovPlayer.animationState.setAnimation(0,anim,true);
  }
};
new spine.SpinePlayer('mov',Object.assign({},params,{
  skelUrl:files.movSkel,atlasUrl:files.movAtlas,animation:'Relax',
  success:function(player){
    veliaMovPlayer=player;
    player.animationState.timeScale=files.motionSpeed;
    if(!player.skeleton.data.findAnimation('Relax')){
      console.error('Missing MOV/Relax animation');document.title='VELIA_SPINE_ERROR';return;
    }
    player.animationState.setAnimation(0,'Relax',true);
    player.animationState.apply(player.skeleton);
    normalizeStage(player);
    setTimeout(()=>{document.title='VELIA_SPINE_READY';},120);
  },error:function(p,msg){console.error('MOV/Relax',msg);document.title='VELIA_SPINE_ERROR';}
}));
 </script></body></html>""" % (file_url(css), file_url(js), conf)

    def _html(self, perf_skel, mov_skel, js, css):
        """Load Start exactly once, then loop Relax from the other skeleton."""
        def file_url(path):
            return QUrl.fromLocalFile(str(path)).toString()
        perf_atlas = perf_skel.with_suffix('.atlas')
        mov_atlas = mov_skel.with_suffix('.atlas')
        conf = {key:file_url(path) for key,path in {
            'perfSkel':perf_skel,'perfAtlas':perf_atlas,
            'movSkel':mov_skel,'movAtlas':mov_atlas}.items()}
        conf['motionSpeed'] = speed_multiplier(self.operator_name)
        conf['operatorName'] = self.operator_name
        lib_url = file_url(js)
        css_url = file_url(css)
        # The player bounds are normalized to the whole overlay, independent of
        # the perf texture being smaller than the mov atlas image.
        return '''<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="%s"><style>
html,body{width:100%%;height:100%%;overflow:hidden;margin:0;background:transparent!important}
#perf,#mov{position:absolute;inset:0;background:transparent!important}
#mov{visibility:hidden}.spine-player{height:100%%!important;background:transparent!important}
/* Both skeletons use an identical normalized visible stage and foot anchor.
   No CSS scale: it would also shift the foot when two assets have different bounds. */
.spine-player-canvas{background:transparent!important}
.spine-player-controls,.spine-player-error,.spine-player-loading,.spine-player-logo,
.spine-player-progress,.spine-player-loading-screen{display:none!important}
</style></head><body><div id="perf"></div><div id="mov"></div>
<script src="%s"></script><script>
const files=%s;
const params={alpha:true,showControls:false,backgroundColor:'#00000000',
  premultipliedAlpha:true,showLoading:false,defaultMix:0};
let startComplete=false;
let relaxReady=false;
let switched=false;
let perfPlayer=null;
let movPlayer=null;
let requestedMotion=false, requestedLeft=false, lastMovAnim='Relax';
let veliaSleeping=false, veliaInteracting=false;

let veliaDisplay=null, perfMode=false, hybridInstalled=false;
let perfAttack=false;
const specialAngelina=files.operatorName==='Angelina_E';
const perfIdleName=specialAngelina?'Skill_1_Idle':'Idle';
const perfStartName=specialAngelina?'Start_2':'Start';
let legPairs=[];
let blendSamples=0;
function isLeg(name){
  const n=name.toLowerCase();
  return /(leg|thigh|calf|knee|shin|ankle|foot|toe|hip|pelvis|足|腿|胯|髋)/.test(n)
      && !/(hair|coat|cape|cloth|skirt|weapon|effect|finger)/.test(n);
}
function legMapping(){
  if(!perfPlayer||!movPlayer) return [];
  const source=new Map(movPlayer.skeleton.bones.map(b=>[b.data.name.toLowerCase(),b]));
  const legRoot=(bone)=>{
    let b=bone;
    while(b){
      const name=b.data.name.toLowerCase();
      if(/(leg|thigh|calf|knee|shin|ankle|foot|toe|足|腿)/.test(name) &&
         !/(hair|coat|cape|cloth|skirt|weapon|effect|finger)/.test(name)) return true;
      b=b.parent;
    }
    return false;
  };
  // Include both leg-chain helper bones (even if unnamed) and pelvis/hip.
  // Thorns uses multiple F_/B_ leg branches and pelvis-driven movement.
  return perfPlayer.skeleton.bones.filter(b=>
    (isLeg(b.data.name)||legRoot(b)) && source.has(b.data.name.toLowerCase()))
    .map(b=>[b,source.get(b.data.name.toLowerCase())]);
}
function installLegBlend(){
  if(hybridInstalled||!perfPlayer||!movPlayer) return;
  hybridInstalled=true;
  const skeleton=perfPlayer.skeleton;
  const original=skeleton.updateWorldTransform.bind(skeleton);
  let last=performance.now();
  skeleton.updateWorldTransform=function(){
    if(perfMode && requestedMotion && !specialAngelina && !veliaDisplay && !veliaSleeping){
      const now=performance.now(), dt=Math.min(.05,Math.max(0,(now-last)/1000));
      movPlayer.animationState.update(dt);
      movPlayer.animationState.apply(movPlayer.skeleton);
      for(const pair of legPairs){
        const dst=pair[0], src=pair[1];
        dst.x=src.x; dst.y=src.y; dst.rotation=src.rotation;
        dst.scaleX=src.scaleX; dst.scaleY=src.scaleY;
        dst.shearX=src.shearX; dst.shearY=src.shearY;
      }
    }
    last=performance.now();
    return original();
  };
}
window.veliaPerfMode=function(enabled){
  if(!switched||!perfPlayer||!movPlayer) return 'System: PERF model unavailable or deploying.';
  if(enabled && !perfPlayer.skeleton.data.findAnimation(perfIdleName))
    return 'System: PERF/Idle animation not available.';
  legPairs=legMapping();
  if(enabled && !specialAngelina && (!movPlayer.skeleton.data.findAnimation('Move')||!legPairs.length))
    return 'System: No compatible lower-body MOV bones; hybrid mode not enabled.';
  perfMode=!!enabled;
  veliaDisplay=null; perfAttack=false;
  installLegBlend();
  if(perfMode){
    // PERF/Default is a one-frame showcase pose and must NEVER be used as idle.
    // Re-activate the previously paused PERF player, then run Idle continuously
    // so the upper body animates even while the operator is standing still.
    perfPlayer.animationState.timeScale=files.motionSpeed;
    perfPlayer.animationState.clearTracks();
    perfPlayer.animationState.setAnimation(0,perfIdleName,true);
    movPlayer.animationState.setAnimation(0,'Move',true);
    if(perfPlayer.play) perfPlayer.play();
    if(movPlayer.pause) movPlayer.pause();
    document.getElementById('perf').style.visibility='visible';
    document.getElementById('mov').style.visibility='hidden';
  }else{
    if(perfPlayer.pause) perfPlayer.pause();
    if(movPlayer.play) movPlayer.play();
    movPlayer.animationState.timeScale=files.motionSpeed;
    movPlayer.animationState.setAnimation(0,requestedMotion?'Move':'Relax',true);
    document.getElementById('perf').style.visibility='hidden';
    document.getElementById('mov').style.visibility='visible';
  }
  return 'System: PERF mode '+(perfMode?'enabled ('+legPairs.length+' mapped lower-body bones including hip and leg helpers)':'disabled')+'.';
};
// PERF attack runs on the PERF skeleton; MOV Move keeps driving leg bones.
// Never reset this track when movement changes.
window.veliaPerfAttack=function(){
  if(!perfMode || !switched || !perfPlayer || perfAttack || veliaSleeping || veliaDisplay)
    return 'System: PERF attack is currently unavailable.';
  const state=perfPlayer.animationState;
  const animations=perfPlayer.skeleton.data.animations || [];
  const clip=animations.find(a=>a.name.toLowerCase()==='attack') ||
             animations.find(a=>a.name.toLowerCase()==='attack_1');
  if(!clip) return 'System: PERF has no Attack or Attack_1 animation.';
  perfAttack=true;
  const active=state.setAnimation(0,clip.name,false);
  state.addAnimation(0,perfIdleName,true,0);
  if(perfPlayer.play) perfPlayer.play();
  const listener={
    complete:function(entry){
      if(entry===active){
        perfAttack=false;
        if(state.removeListener) state.removeListener(listener);
      }
    },
    interrupt:function(entry){
      if(entry===active){
        perfAttack=false;
        if(state.removeListener) state.removeListener(listener);
      }
    }
  };
  state.addListener(listener);
  return 'System: Playing PERF/'+clip.name+'.';
};
window.veliaAnimationList=function(){
  const part=(label,p)=>label+'/\\n'+(p?p.skeleton.data.animations.map(a=>'  ├─ '+a.name).join('\\n'):'  (unavailable)');
  return part('mov',movPlayer)+'\\n'+part('perf',perfPlayer);
};
window.veliaDisplayAnimation=function(name,speed,loop){
  if(!switched) return 'System: Wait for deployment to finish.';
  const prefix=/^(mov|perf)\\//i.exec(name);
  const target=prefix?prefix[1].toLowerCase():null;
  const asked=prefix?name.slice(prefix[0].length):name;
  const resolve=(player)=>player&&player.skeleton.data.animations.find(a=>a.name.toLowerCase()===asked.toLowerCase());
  const hasMov=resolve(movPlayer), hasPerf=resolve(perfPlayer);
  const clipName=(target==='perf'?hasPerf:target==='mov'?hasMov:(hasMov||hasPerf))?.name||asked;
  if(!target && hasMov && hasPerf) return 'System: Ambiguous animation; use mov/'+clipName+' or perf/'+clipName;
  const model=target||(hasMov?'mov':hasPerf?'perf':null);
  const player=model==='mov'?movPlayer:model==='perf'?perfPlayer:null;
  if(!player||!player.skeleton.data.findAnimation(clipName))return 'System: Animation not found: '+name;
  veliaDisplay={model:model,name:clipName,returnPerf:perfMode};
  perfMode=false;
  if(perfPlayer.pause) perfPlayer.pause();
  if(movPlayer.pause) movPlayer.pause();
  if(player.play) player.play();
  const st=player.animationState;
  st.timeScale=files.motionSpeed*speed;
  st.clearTracks();
  st.setAnimation(0,clipName,loop);
  st.addListener({complete:function(entry){
    if(!loop && veliaDisplay && veliaDisplay.model===model && veliaDisplay.name===clipName &&
       entry.animation && entry.animation.name===clipName) interruptDisplay();
  }});
  document.getElementById('perf').style.visibility=model==='perf'?'visible':'hidden';
  document.getElementById('mov').style.visibility=model==='mov'?'visible':'hidden';
  return 'System: Playing '+model+'/'+clipName+' at '+speed+'x'+(loop?' (loop)':'');
};
window.veliaDisplaySequence=function(names,speed,loop){
  if(!Array.isArray(names)||!names.length) return 'System: Empty animation sequence.';
  const resolved=[];
  for(const raw of names){
    const input=typeof raw==='string'?{name:raw}:raw;
    if(!input||typeof input.name!=='string') return 'System: Invalid animation binding.';
    const name=input.name;
    const prefix=/^(mov|perf)\\//i.exec(name);
    const model=prefix?prefix[1].toLowerCase():null;
    const asked=prefix?name.slice(prefix[0].length):name;
    const match=(p)=>p&&p.skeleton.data.animations.find(a=>a.name.toLowerCase()===asked.toLowerCase());
    const movMatch=match(movPlayer), perfMatch=match(perfPlayer);
    if(!model && movMatch && perfMatch) return 'System: Ambiguous animation: '+name;
    const kind=model||(movMatch?'mov':perfMatch?'perf':null);
    const clip=kind==='mov'?movMatch:kind==='perf'?perfMatch:null;
    if(!clip) return 'System: Animation not found: '+name;
    const partSpeed=Number(input.speed===undefined?speed:input.speed);
    const flip=input.flip||'auto', play=input.play||'normal';
    if(input.loop!==undefined && typeof input.loop!=='boolean') return 'System: Invalid loop modifier for '+name;
    if(!Number.isFinite(partSpeed)||partSpeed<=0||!['auto','up','down','left','right'].includes(flip)||!['normal','backward'].includes(play))
      return 'System: Invalid animation modifier for '+name;
    resolved.push({name:kind+'/'+clip.name,model:kind,clip:clip.name,speed:partSpeed,flip:flip,play:play});
  }
  if(window.veliaSequenceToken) window.veliaSequenceToken.cancel();
  const token={active:true,listeners:[],raf:0,restorePerf:typeof perfMode==='boolean'?perfMode:false,transforms:new Map()};
  token.cancel=function(){
    token.active=false;
    if(token.raf) cancelAnimationFrame(token.raf);
    for(const item of token.listeners) item.state.removeListener(item.listener);
    token.listeners=[];
    for(const [canvas,transform] of token.transforms) canvas.style.transform=transform;
    token.transforms.clear();
  };
  window.veliaSequenceToken=token;
  let index=0;
  function finish(){
    if(!token.active) return;
    if(index+1<resolved.length){index++;playNext();return;}
    if(loop){index=0;playNext();return;}
    token.cancel(); window.veliaSequenceToken=null;
    interruptDisplay();
  }
  function playNext(){
    if(!token.active)return;
    for(const item of token.listeners)item.state.removeListener(item.listener);
    token.listeners=[];
    if(token.raf){cancelAnimationFrame(token.raf);token.raf=0;}
    const current=resolved[index];
    const result=window.veliaDisplayAnimation(current.name,current.speed,true);
    if(typeof result==='string'&&!result.startsWith('System: Playing')){token.cancel();return;}
    if(veliaDisplay && perfPlayer) veliaDisplay.returnPerf=token.restorePerf;
    const player=current.model==='perf'?perfPlayer:movPlayer;
    const canvas=document.querySelector('#'+current.model+' canvas');
    if(canvas){
      if(!token.transforms.has(canvas)) token.transforms.set(canvas,canvas.style.transform);
      const original=token.transforms.get(canvas);
      const defaultLeft=typeof requestedLeft!=='undefined'?requestedLeft:/scaleX\\(-1\\)|scale\\(-1[,)]/.test(original);
      const sx=current.flip==='left'?-1:current.flip==='right'?1:defaultLeft?-1:1;
      const sy=current.flip==='down'?-1:1;
      canvas.style.transform='scale('+sx+','+sy+')';
    }
    const state=player.animationState;
    const entry=state.getCurrent(0);
    if(!entry){token.cancel();return;}
    if(current.play==='backward'){
      // Spine 3.8 does not support reverse track update. Sample trackTime
      // backwards while keeping the built-in renderer and animation state alive.
      const duration=entry.animation.duration;
      const began=performance.now();
      state.timeScale=0;
      const tick=(now)=>{
        if(!token.active||window.veliaSequenceToken!==token)return;
        const elapsed=(now-began)/1000*files.motionSpeed*current.speed;
        entry.trackTime=Math.max(0.00001,duration-elapsed);
        if(elapsed>=duration){finish();return;}
        token.raf=requestAnimationFrame(tick);
      };
      token.raf=requestAnimationFrame(tick);
    }else{
      const listener={complete:function(done){
        if(!token.active||window.veliaSequenceToken!==token||done!==entry)return;
        state.removeListener(listener);
        token.listeners=token.listeners.filter(x=>x.listener!==listener);
        finish();
      }};
      state.addListener(listener);
      token.listeners.push({state:state,listener:listener});
    }
  }
  playNext();
  return 'System: Playing sequence: '+resolved.map(x=>x.name).join(' -> ');
};
window.veliaStopSequence=function(){
  if(!window.veliaSequenceToken || !window.veliaSequenceToken.active) return 'System: No active sequence.';
  interruptDisplay();
  return 'System: Animation sequence stopped.';
};
function interruptDisplay(){
  if(window.veliaSequenceToken){
    window.veliaSequenceToken.cancel();
    window.veliaSequenceToken=null;
  }
  if(!veliaDisplay) return;
  const restorePerf=veliaDisplay.returnPerf;
  veliaDisplay=null;
  for(const player of [movPlayer,perfPlayer]) if(player) player.animationState.timeScale=files.motionSpeed;
  if(perfPlayer&&perfPlayer.pause)perfPlayer.pause();
  if(movPlayer&&movPlayer.play)movPlayer.play();
  document.getElementById('perf').style.visibility='hidden';
  document.getElementById('mov').style.visibility='visible';
  movPlayer.animationState.setAnimation(0,requestedMotion?'Move':'Relax',true);
  lastMovAnim=requestedMotion?'Move':'Relax';
  if(restorePerf) window.veliaPerfMode(true);
}
window.veliaSetSleep=function(flag){
 interruptDisplay();
 veliaSleeping=!!flag; veliaInteracting=false;
 if(!movPlayer || !switched || !movPlayer.animationState) return;
 const anim=veliaSleeping?'Sleep':'Relax';
 if(movPlayer.skeleton.data.findAnimation(anim)){
   lastMovAnim=anim; movPlayer.animationState.setAnimation(0,anim,true);
 }
};
window.veliaInteract=function(){
 interruptDisplay();
 if(perfMode){perfMode=false;interruptDisplay();if(perfPlayer.pause)perfPlayer.pause();if(movPlayer.play)movPlayer.play();document.getElementById('perf').style.visibility='hidden';document.getElementById('mov').style.visibility='visible';}
 if(!switched || veliaSleeping || veliaInteracting || !movPlayer || !movPlayer.skeleton.data.findAnimation('Interact')) return;
 veliaInteracting=true;
 movPlayer.animationState.setAnimation(0,'Interact',false);
 movPlayer.animationState.addAnimation(0,'Relax',true,0);
 movPlayer.animationState.addListener({complete:function(entry){
   if(entry.animation && entry.animation.name==='Interact') veliaInteracting=false;
 }});
};
window.veliaSetDepthScale=function(scale){
  const safe=Math.max(.01,Math.min(5,Number(scale)||1));
  for(const el of [document.getElementById('mov'),document.getElementById('perf')]){
    if(!el) continue;
    el.style.transformOrigin='50%% 68%%';
    el.style.transform='scale('+safe.toFixed(6)+')';
  }
};
window.veliaSetMotion=function(moving,left){
  if(moving && veliaDisplay) interruptDisplay();
  else if(!moving && veliaDisplay) return;
  if(perfMode){
    requestedMotion=moving;requestedLeft=left;
    const c=document.querySelector('#perf canvas');if(c)c.style.transform=left?'scaleX(-1)':'scaleX(1)';
    return;
  }
  requestedMotion=moving; requestedLeft=left;
  if(!switched || !movPlayer || !movPlayer.animationState || veliaSleeping) return;
  const current=movPlayer.animationState.getCurrent(0);
  // Movement cancels Interact instead of allowing it to finish or resume.
  if(moving && current && current.animation && current.animation.name==='Interact'){
    veliaInteracting=false;
    lastMovAnim='Interact';
  } else if(!moving && current && current.animation && current.animation.name==='Interact' && !current.isComplete()) return;
  if(veliaInteracting && current && current.animation && current.animation.name==='Relax') veliaInteracting=false;
  const anim=moving && movPlayer.skeleton.data.findAnimation('Move') ? 'Move':'Relax';
  const canvas=document.querySelector('#mov canvas');
  if(canvas) canvas.style.transform=left ? 'scaleX(-1)' : 'scaleX(1)';
  if(anim!==lastMovAnim){lastMovAnim=anim;movPlayer.animationState.setAnimation(0,anim,true);}
};
// One world-space camera for BOTH skeletons. Never fit Start's effect bounds.
// The MOV Relax pose defines the shared unit scale; (0,0) is the same
// skeleton-origin/foot anchor in both files, as verified in the viewer.
let sharedCamera=null;
function initializeSharedCamera(player){
  if(!player || !player.skeleton || !player.currentViewport) return false;
  player.skeleton.updateWorldTransform();
  const offset=new spine.Vector2(), size=new spine.Vector2();
  player.skeleton.getBounds(offset,size);
  if(!(size.x>0 && size.y>0)) return false;
  const box=player.parent.getBoundingClientRect();
  const ratio=Math.max(.1,box.width/Math.max(1,box.height));
  const baseHeight=Math.max(size.y/.80,size.x/(ratio*.85));
  const worldHeight=baseHeight*3.0; // account for large Start effects
  const worldWidth=worldHeight*ratio;
  // Do NOT center on (offset.x, offset.y) from the current animation.
  // This keeps MOV and PERF at the exact same skeletal origin.
  sharedCamera={x:-worldWidth/2,y:-worldHeight*.32,
    width:worldWidth,height:worldHeight,
    padLeft:0,padRight:0,padTop:0,padBottom:0};
  return true;
}
function applySharedCamera(player){
  if(!sharedCamera || !player) return;
  player.currentViewport=Object.assign({},sharedCamera);
  player.previousViewport=null;
}
function showRelax(){
  if(switched || !startComplete || !relaxReady) return;
  switched=true;
  if (movPlayer && movPlayer.animationState) {
    movPlayer.animationState.setAnimation(0,'Relax',true);
    movPlayer.skeleton.setToSetupPose();
    movPlayer.animationState.apply(movPlayer.skeleton);
    applySharedCamera(movPlayer);
  }
  document.getElementById('mov').style.visibility='visible';
  document.getElementById('perf').style.visibility='hidden';
  if(perfPlayer && perfPlayer.pause) perfPlayer.pause();
  window.veliaSetMotion(requestedMotion,requestedLeft);
  document.title='VELIA_SPINE_DEPLOYED';
}
function readyPerf(player){
  perfPlayer=player;
  player.animationState.timeScale=files.motionSpeed;
  const clip=player.skeleton.data.findAnimation(perfStartName);
  if(!clip){console.error('Missing perf Start animation');document.title='VELIA_SPINE_ERROR';return;}
  // Keep the initial Start track installed while MOV and the camera load.
  // The Spine 3.8 player render loop expects a non-null current entry.
  player.animationState.timeScale=0;
  player.animationState.addListener({complete:function(entry){
    if(entry.animation && entry.animation.name===perfStartName){
      startComplete=true;
      showRelax();
    }
  }});
  maybeStartPerf();
}
// Load both skeletons in parallel to reduce startup latency; wait until MOV
// supplies the shared camera before making PERF visible / starting Start.
let perfStarted=false;
function maybeStartPerf(){
  if(perfStarted || !relaxReady || !perfPlayer || !sharedCamera) return;
  perfStarted=true;
  applySharedCamera(perfPlayer);
  // Begin PERF only when the MOV-based camera is available.
  perfPlayer.animationState.setAnimation(0,perfStartName,false);
  perfPlayer.animationState.timeScale=files.motionSpeed;
  perfPlayer.animationState.apply(perfPlayer.skeleton);
  setTimeout(()=>{document.title='VELIA_SPINE_READY';},120);
}
new spine.SpinePlayer('perf',Object.assign({},params,{
  skelUrl:files.perfSkel,atlasUrl:files.perfAtlas,animation:perfStartName,
  success:readyPerf,
  error:function(p,msg){console.error('PERF/Start',msg);document.title='VELIA_SPINE_ERROR';}
}));
new spine.SpinePlayer('mov',Object.assign({},params,{
  skelUrl:files.movSkel,atlasUrl:files.movAtlas,animation:'Relax',
  success:function(relax){
    movPlayer=relax;
    relax.animationState.timeScale=files.motionSpeed;
    if(!relax.skeleton.data.findAnimation('Relax')){
      console.error('Missing mov Relax animation');document.title='VELIA_SPINE_ERROR';return;
    }
    relax.animationState.setAnimation(0,'Relax',true);
    relax.animationState.apply(relax.skeleton);
    if(!initializeSharedCamera(relax)){
      console.error('Unable to calibrate MOV viewport');document.title='VELIA_SPINE_ERROR';return;
    }
    applySharedCamera(relax);
    relaxReady=true;
    maybeStartPerf();
  },error:function(p,msg){console.error('MOV/Relax',msg);document.title='VELIA_SPINE_ERROR';}
}));
</script></body></html>''' % (css_url, lib_url, json.dumps(conf, ensure_ascii=False))

    def _fit_overlay(self):
        if self.overlay is None:
            return
        # Same world coordinates / floor projection for all operators.
        w, h = float(self.width()), float(self.height())
        back_half = min(w * .315, h * .49)
        front_half = min(w * .466, h * .90)
        t = self._depth_fraction(self.spawn_z)
        half = back_half + (front_half - back_half) * t
        cx = w / 2 + (self.spawn_x / ROOM_SIDE_M * 2 - 1) * half
        floor_y = h * (.48 + (.925 - .48) * t)
        # Triple the transparent Chromium stage; the JS camera is expanded
        # by the same factor, preserving the original 150..200 px actor size.
        # This gives wide/long animations room to travel without clipping.
        reference_room_height = 733.0
        viewport_occupancy = .80
        original_h = (200.0 / reference_room_height * h) / viewport_occupancy
        original_w = original_h * 1.60
        actor_h = original_h * SPINE_STAGE_OVERSCAN
        actor_w = original_w * SPINE_STAGE_OVERSCAN
        target_w, target_h = max(1, round(actor_w)), max(1, round(actor_h))
        if not self._ready:
            self.overlay.setGeometry(-10000, -10000, target_w, target_h)
            return
        # Spine camera and CSS scale share the same fixed foot anchor.
        self.overlay.setGeometry(round(cx - actor_w / 2),
                                 round(floor_y - actor_h * SPINE_FOOT_ANCHOR),
                                 target_w, target_h)
        # Change ONLY the DOM element's size, not the WebEngine viewport.
        # One shared screen-space scale for all operators at the same depth.
        # Their individual height_multiplier is relative to 1.70 m, not PNG pixels.
        target_px = 150.0 + 50.0 * self.spawn_z / ROOM_SIDE_M
        depth_scale = (target_px / 200.0) * height_multiplier(self.operator_name)
        if self._last_depth_scale is None or abs(depth_scale - self._last_depth_scale) > 0.0002:
            self._last_depth_scale = depth_scale
            self.overlay.page().runJavaScript(
                'window.veliaSetDepthScale && window.veliaSetDepthScale(' +
                format(depth_scale, '.6f') + ');')
        if hasattr(self, 'console'):
            self._keep_console_on_top()

    def toggle_console(self):
        controlled = self.controlled_actor()
        if self.console.isVisible():
            self.console.hide()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._held_keys.clear()
            self._velocity_x = self._velocity_z = 0.0
            if controlled is not None:
                controlled._held_keys.clear()
                controlled._velocity_x = controlled._velocity_z = 0.0
                controlled._set_motion_animation(False, controlled._facing_left)
            self.console.show()
            self._keep_console_on_top()
            self.console.raise_()
            self.console.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_sleeping(self, sleeping):
        sleeping = bool(sleeping)
        if sleeping == self._sleeping:
            return
        self._sleeping = sleeping
        self._held_keys.clear()
        self._velocity_x = self._velocity_z = 0.0
        if self.overlay and self._ready:
            self.overlay.page().runJavaScript(
                'window.veliaSetSleep && window.veliaSetSleep(' +
                ('true' if sleeping else 'false') + ');')
        if not sleeping:
            self._moving_animation = False

    def mousePressEvent(self, event):
        active = self.controlled_actor()
        if active is not None and active is not self:
            return active._click_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and self._ready and not self._sleeping:
            w, h = float(self.width()), float(self.height())
            t = self._depth_fraction(self.spawn_z)
            half = min(w * .315, h * .49) + (min(w * .466, h * .90) - min(w * .315, h * .49)) * t
            center_x = w / 2 + (self.spawn_x / ROOM_SIDE_M * 2 - 1) * half
            foot_y = h * (.48 + (.925 - .48) * t)
            character_h = (150.0 + 50.0 * self.spawn_z / ROOM_SIDE_M) / 733.0 * h * height_multiplier(self.operator_name)
            # Bounding box is intentionally conservative for moving Spine parts.
            if abs(event.position().x() - center_x) <= character_h * .52 and foot_y-character_h <= event.position().y() <= foot_y:
                self.overlay.page().runJavaScript('window.veliaInteract && window.veliaInteract();')
                event.accept()
                return
        super().mousePressEvent(event)

    def _click_at(self, position):
        if not self._ready or self._sleeping:
            return
        from types import SimpleNamespace
        event = SimpleNamespace(button=lambda: Qt.MouseButton.LeftButton,
                                position=lambda: position,
                                accept=lambda: None)
        self.mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for actor in getattr(self, '_actors', {}).values():
            actor.setGeometry(self.rect())
        self._fit_overlay()
        if hasattr(self, 'console'):
            self.console.setGeometry(18, max(10, self.height() - 225),
                                     max(200, self.width() - 36), 205)
            self._keep_console_on_top()

    def closeEvent(self, event):
        self._walk_timer.stop()
        for actor in list(getattr(self, '_actors', {}).values()):
            actor.close()
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)

    def spawn_again(self):
        """Fresh random position; restart the operator's configured animation."""
        self._held_keys.clear()
        self._sleeping = False
        self._velocity_x = self._velocity_z = 0.0
        self._last_depth_scale = None
        self.spawn_x, self.spawn_z = sample_spawn()
        self._last_tick = time.monotonic()
        self._fit_overlay()
        if self.overlay is not None:
            self._ready = False
            self._deploying = False
            self._poll_count = 0
            self._fit_overlay()
            self._ready_poll.start()
            self._deploying = load_operator(self.operator_name, self.skin_number)[0] is not None
            self.overlay.reload()
