"""Spine 3.8 operator overlay for Velia Playground.

Loads operator resource/op/<name>/{perf,mov}/<atlas,skel,png> without
converting atlas textures into sprites. QtWebEngine is needed to render
Spine 3.8 via the bundled official spine-player.js.
"""
import json
import random
import math
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QApplication, QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox

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
OPERATOR_HEIGHT_M = 1.70
# PERForm/MOV asset dimensions may differ; camera normalization handles both.


def load_operator(name):
    """Find a generic operator's unique perf/mov skeleton pair."""
    folder = OP_DIR / name
    def lookup(model):
        path = folder / model
        skels = sorted(path.glob('*.skel'))
        if len(skels) != 1:
            raise FileNotFoundError(f'{path}: expected exactly one .skel file')
        skel = skels[0]
        atlas = skel.with_suffix('.atlas')
        if not atlas.is_file():
            raise FileNotFoundError(atlas)
        # Atlas can point at a different PNG basename, so verify actual pages.
        lines = atlas.read_text(encoding='utf-8-sig').splitlines()
        pages = [line.strip() for line in lines if line.strip().lower().endswith('.png')]
        if not pages or any(not (path / page).is_file() for page in pages):
            raise FileNotFoundError(f'{atlas}: missing referenced .png texture page')
        return skel, atlas
    return lookup('perf'), lookup('mov')


# Shared world scale for every operator: x/z 0..10 m, 1 tile = 1 m².
# Raised tiles are read from the SAME geometry function that renders the room.
ACTOR_RADIUS_M = 0.34
DEFAULT_WALK_MPS = 1.40
MOVE_RESPONSE_S = 0.085  # smooth acceleration / braking (seconds)


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


class SpineRoomView(RoomView):
    """Room with one initially stationary Spine 3.8 operator above the floor."""
    def __init__(self, parent=None, operator_name='Enya'):
        super().__init__(parent)
        self.operator_name = operator_name
        self.spawn_x, self.spawn_z = sample_spawn()
        self.overlay = None
        self.status = None
        self._ready = False
        self._load_generation = 0
        self._poll_count = 0
        self._ready_poll = QTimer(self)
        self._ready_poll.setInterval(150)
        self._ready_poll.timeout.connect(self._poll_spine_readiness)
        self._held_keys = set()
        self._last_tick = time.monotonic()
        self._moving_animation = False
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
        self._request_setup()

    def _request_setup(self):
        if not WEBENGINE_AVAILABLE:
            self._warning('Qt WebEngine is required: pip install PySide6-Addons')
            return
        try:
            if self.operator_name.casefold() == 'enya':
                # Enya is MOV-only: never inspect or load mismatched PERF assets.
                mov_dir = OP_DIR / self.operator_name / 'mov'
                mov_candidates = sorted(mov_dir.glob('*.skel'))
                if len(mov_candidates) != 1:
                    raise FileNotFoundError(f'{mov_dir}: expected exactly one .skel file')
                mov = mov_candidates[0]
                atlas = mov.with_suffix('.atlas')
                if not atlas.is_file():
                    raise FileNotFoundError(atlas)
                atlas_pages = [line.strip() for line in atlas.read_text(encoding='utf-8-sig').splitlines()
                               if line.strip().lower().endswith('.png')]
                if not atlas_pages or any(not (mov_dir / page).is_file() for page in atlas_pages):
                    raise FileNotFoundError(f'{atlas}: missing referenced PNG texture')
                perf = None
            else:
                (perf, _), (mov, _) = load_operator(self.operator_name)
            js = RUNTIME_DIR / 'spine-player.js'
            css = RUNTIME_DIR / 'spine-player.css'
            if not (js.is_file() and css.is_file()):
                raise FileNotFoundError('Missing resource/spine/spine-player.js or spine-player.css')
        except (OSError, ValueError) as exc:
            self._warning(str(exc))
            return
        self.overlay = QWebEngineView(self)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.overlay.setStyleSheet('background:transparent;border:none;')
        self.overlay.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.overlay.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.overlay.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.overlay.setMouseTracking(False)
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
            return super().eventFilter(obj, event)
        key_map = {Qt.Key.Key_W:'w',Qt.Key.Key_A:'a',Qt.Key.Key_S:'s',Qt.Key.Key_D:'d'}
        key = key_map.get(event.key())
        if key is None:
            return super().eventFilter(obj, event)
        if not event.isAutoRepeat():
            if typ == QEvent.Type.KeyPress:
                self._held_keys.add(key)
            else:
                self._held_keys.discard(key)
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
        if not self._ready or not self.isVisible():
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
        self._ready_poll.stop()
        # Keep the failed renderer outside the room; never show loading UI.
        if self.overlay is not None:
            self.overlay.setGeometry(-10000, -10000, 320, 320)
        print('[Velia Spine] ' + str(message), flush=True)

    def _poll_spine_readiness(self):
        if self._ready or self.overlay is None:
            self._ready_poll.stop()
            return
        self._poll_count += 1
        # Qt may coalesce titleChanged events in an offscreen WebEngine.
        # Read the page title as a fallback while the renderer stays alive.
        try:
            self.overlay.page().runJavaScript('document.title', self._on_spine_title)
        except RuntimeError:
            self._ready_poll.stop()
        if self._poll_count >= 200:
            self._load_failed('Timed out waiting for Spine/Relax readiness (30 seconds)')

    def _on_spine_title(self, title):
        if title == 'VELIA_SPINE_READY' and not self._ready:
            # Deploy immediately once, keeping the selected random spawn.
            self._ready = True
            self._ready_poll.stop()
            self._moving_animation = False
            self._facing_left = False
            self._last_tick = time.monotonic()
            self._last_depth_scale = None
            if self.overlay is not None:
                self._fit_overlay()
                self.overlay.show()
                self.overlay.raise_()
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
  const worldHeight=Math.max(size.y/.80, size.x/(ratio*.85));
  const worldWidth=worldHeight*ratio;
  player.currentViewport={x:offset.x+size.x/2-worldWidth/2,
    y:offset.y-worldHeight*.06,width:worldWidth,height:worldHeight,
    padLeft:0,padRight:0,padTop:0,padBottom:0};
  player.previousViewport=null;
}
let veliaMovPlayer=null;
let veliaMotion='Relax';
window.veliaSetDepthScale=function(scale){
  const safe=Math.max(.01,Math.min(5,Number(scale)||1));
  for(const el of [document.getElementById('mov'),document.getElementById('perf')]){
    if(!el) continue;
    el.style.transformOrigin='50%% 94%%';
    el.style.transform='scale('+safe.toFixed(6)+')';
  }
};
window.veliaSetMotion=function(moving, left){
  if(!veliaMovPlayer || !veliaMovPlayer.animationState) return;
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
window.veliaSetDepthScale=function(scale){
  const safe=Math.max(.01,Math.min(5,Number(scale)||1));
  for(const el of [document.getElementById('mov'),document.getElementById('perf')]){
    if(!el) continue;
    el.style.transformOrigin='50%% 94%%';
    el.style.transform='scale('+safe.toFixed(6)+')';
  }
};
window.veliaSetMotion=function(moving,left){
  requestedMotion=moving; requestedLeft=left;
  if(!switched || !movPlayer || !movPlayer.animationState) return;
  const anim=moving && movPlayer.skeleton.data.findAnimation('Move') ? 'Move':'Relax';
  const canvas=document.querySelector('#mov canvas');
  if(canvas) canvas.style.transform=left ? 'scaleX(-1)' : 'scaleX(1)';
  if(anim!==lastMovAnim){lastMovAnim=anim;movPlayer.animationState.setAnimation(0,anim,true);}
};
// A shared screen-space character box: 80%% of available stage height.
// Each distinct Spine skeleton gets its own camera bounds so the character
// retains identical screen size / bottom anchor when swapping skeletons.
function normalizeStage(player){
  if(!player || !player.skeleton || !player.currentViewport) return;
  player.skeleton.updateWorldTransform();
  const offset=new spine.Vector2(), size=new spine.Vector2();
  player.skeleton.getBounds(offset,size);
  if(!(size.x>0 && size.y>0)) return;
  const box=player.parent.getBoundingClientRect();
  const ratio=Math.max(.1,box.width/Math.max(1,box.height));
  // Size both renderers from their ACTUAL attachments, not PNG dimensions.
  const worldHeight=Math.max(size.y/.80, size.x/(ratio*.85));
  const worldWidth=worldHeight*ratio;
  // Visible foot rests at 94%% stage height, horizontally centered.
  const v={x:offset.x+size.x/2-worldWidth/2,
           y:offset.y-worldHeight*.06,
           width:worldWidth,height:worldHeight,
           padLeft:0,padRight:0,padTop:0,padBottom:0};
  player.currentViewport=v;
  player.previousViewport=null;
}
function showRelax(){
  if(switched || !startComplete || !relaxReady) return;
  switched=true;
  if (movPlayer && movPlayer.animationState) {
    movPlayer.animationState.setAnimation(0,'Relax',true);
    movPlayer.skeleton.setToSetupPose();
    movPlayer.animationState.apply(movPlayer.skeleton);
    normalizeStage(movPlayer);
  }
  document.getElementById('mov').style.visibility='visible';
  document.getElementById('perf').style.visibility='hidden';
  if(perfPlayer && perfPlayer.pause) perfPlayer.pause();
  window.veliaSetMotion(requestedMotion,requestedLeft);
}
function readyPerf(player){
  perfPlayer=player;
  player.animationState.timeScale=files.motionSpeed;
  const clip=player.skeleton.data.findAnimation('Start');
  if(!clip){console.error('Missing perf Start animation');document.title='VELIA_SPINE_ERROR';return;}
  // The bundled Spine player defaults to looping: explicitly play Start once.
  player.animationState.clearTracks();
  player.animationState.setAnimation(0,'Start',false);
  player.animationState.apply(player.skeleton);
  normalizeStage(player);
  setTimeout(()=>{document.title='VELIA_SPINE_READY';},120);
  player.animationState.addListener({complete:function(entry){
    if(entry.animation && entry.animation.name==='Start'){
      startComplete=true;
      showRelax();
    }
  }});
  // Load the second skeleton in parallel, but show it only after Start completes.
  new spine.SpinePlayer('mov',Object.assign({},params,{
    skelUrl:files.movSkel,atlasUrl:files.movAtlas,animation:'Relax',
    success:function(relax){
      movPlayer=relax;
      relax.animationState.timeScale=files.motionSpeed;
      if (!relax.skeleton.data.findAnimation('Relax')){
        console.error('Missing mov Relax animation');return;
      }
      relax.animationState.setAnimation(0,'Relax',true);
      relax.animationState.apply(relax.skeleton);
      normalizeStage(relax);
      relaxReady=true;
      showRelax();
    },error:function(p,msg){console.error('MOV/Relax',msg);document.title='VELIA_SPINE_ERROR';}
  }));
}
new spine.SpinePlayer('perf',Object.assign({},params,{
  skelUrl:files.perfSkel,atlasUrl:files.perfAtlas,animation:'Start',
  success:readyPerf,error:function(p,msg){console.error('PERF/Start',msg);document.title='VELIA_SPINE_ERROR';}
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
        # A constant-sized Chromium canvas prevents re-layout and camera
        # normalization from running for every 1px change in depth.
        reference_room_height = 733.0
        viewport_occupancy = .80
        max_actor_h = (200.0 / reference_room_height * h) / viewport_occupancy
        actor_w = max_actor_h * 1.60
        target_w, target_h = max(1, round(actor_w)), max(1, round(max_actor_h))
        if not self._ready:
            self.overlay.setGeometry(-10000, -10000, target_w, target_h)
            return
        # Place the constant canvas using one fixed 94% foot anchor.
        self.overlay.setGeometry(round(cx - actor_w / 2),
                                 round(floor_y - max_actor_h * .94),
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
        self.overlay.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_overlay()

    def closeEvent(self, event):
        self._walk_timer.stop()
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)

    def spawn_again(self):
        """Fresh random position; restart the operator's configured animation."""
        self._held_keys.clear()
        self._velocity_x = self._velocity_z = 0.0
        self._last_depth_scale = None
        self.spawn_x, self.spawn_z = sample_spawn()
        self._last_tick = time.monotonic()
        self._fit_overlay()
        if self.overlay is not None:
            self._ready = False
            self._poll_count = 0
            self._fit_overlay()
            self._ready_poll.start()
            self.overlay.reload()
