"""Spine 3.8 operator overlay for Velia Playground.

Loads operator resource/op/<name>/{perf,mov}/<atlas,skel,png> without
converting atlas textures into sprites. QtWebEngine is needed to render
Spine 3.8 via the bundled official spine-player.js.
"""
import json
import random
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel

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


def sample_spawn(rng=None):
    """Return a random cell centre outside the two raised rear corner clusters."""
    rng = rng or random
    blocked = {(0,0),(0,1),(0,2),(1,0),(1,1),
               (0,9),(0,8),(0,7),(1,9),(1,8)}
    candidates = [(col+0.5, row+0.5)
                  for row in range(ROOM_SIDE_M)
                  for col in range(ROOM_SIDE_M)
                  if (row,col) not in blocked]
    return rng.choice(candidates)


class SpineRoomView(RoomView):
    """Room with one initially stationary Spine 3.8 operator above the floor."""
    def __init__(self, parent=None, operator_name='Enya'):
        super().__init__(parent)
        self.operator_name = operator_name
        self.spawn_x, self.spawn_z = sample_spawn()
        self.overlay = None
        self.status = None
        self._ready = False
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
        self.overlay.loadFinished.connect(self._on_load)
        self._ready = True
        self._fit_overlay()
        # A local HTML document is constructed from the official Spine 3.8 player.
        html = (self._html_mov_only(mov, js, css) if perf is None
                else self._html(perf, mov, js, css))
        self.overlay.setHtml(html, QUrl.fromLocalFile(str(ROOT) + '/'))

    def _warning(self, message):
        self.status = QLabel('Spine: ' + message, self)
        self.status.setStyleSheet('background:#fff8e0;color:#513e00;padding:7px;border-radius:5px;')
        self.status.move(12, 12)
        self.status.adjustSize()
        self.status.show()

    def _on_load(self, ok):
        if not ok:
            self._warning('Spine renderer page failed to load')

    def _html_mov_only(self, mov_skel, js, css):
        # Enya exception: MOV only.
        def file_url(path):
            return QUrl.fromLocalFile(str(path)).toString()
        conf = json.dumps({
            'movSkel': file_url(mov_skel),
            'movAtlas': file_url(mov_skel.with_suffix('.atlas')),
        }, ensure_ascii=False)
        # JSON is injected as data; JS/CSS resources remain local to this project.
        return """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="%s"><style>
html,body{width:100%%;height:100%%;overflow:hidden;margin:0;background:transparent!important}
#mov{position:absolute;inset:0;background:transparent!important}
.spine-player{height:100%%!important;background:transparent!important}
.spine-player-canvas{background:transparent!important}
.spine-player-controls,.spine-player-error{display:none!important}
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
new spine.SpinePlayer('mov',Object.assign({},params,{
  skelUrl:files.movSkel,atlasUrl:files.movAtlas,animation:'Relax',
  success:function(player){
    if(!player.skeleton.data.findAnimation('Relax')){
      console.error('Missing MOV/Relax animation');return;
    }
    player.animationState.setAnimation(0,'Relax',true);
    player.animationState.apply(player.skeleton);
    normalizeStage(player);
  },error:function(p,msg){console.error('MOV/Relax',msg)}
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
.spine-player-controls,.spine-player-error{display:none!important}
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
}
function readyPerf(player){
  perfPlayer=player;
  const clip=player.skeleton.data.findAnimation('Start');
  if(!clip){console.error('Missing perf Start animation');return;}
  // The bundled Spine player defaults to looping: explicitly play Start once.
  player.animationState.clearTracks();
  player.animationState.setAnimation(0,'Start',false);
  player.animationState.apply(player.skeleton);
  normalizeStage(player);
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
      if (!relax.skeleton.data.findAnimation('Relax')){
        console.error('Missing mov Relax animation');return;
      }
      relax.animationState.setAnimation(0,'Relax',true);
      relax.animationState.apply(relax.skeleton);
      normalizeStage(relax);
      relaxReady=true;
      showRelax();
    },error:function(p,msg){console.error('MOV/Relax',msg)}
  }));
}
new spine.SpinePlayer('perf',Object.assign({},params,{
  skelUrl:files.perfSkel,atlasUrl:files.perfAtlas,animation:'Start',
  success:readyPerf,error:function(p,msg){console.error('PERF/Start',msg)}
}));
</script></body></html>''' % (css_url, lib_url, json.dumps(conf, ensure_ascii=False))

    def _fit_overlay(self):
        if self.overlay is None:
            return
        # The room uses a 10×10 m floor. x is distance from left wall,
        # z is distance from back wall. Project with the same floor geometry
        # and depth interpolation used by room_view.RoomView.
        w, h = float(self.width()), float(self.height())
        back_half = min(w * .315, h * .49)
        front_half = min(w * .466, h * .90)
        t = self._depth_fraction(self.spawn_z)
        half = back_half + (front_half - back_half) * t
        cx = w / 2 + (self.spawn_x / ROOM_SIDE_M * 2 - 1) * half
        floor_y = h * (.48 + (.925 - .48) * t)
        # A 1m vertical segment on each side wall scales between rear and front.
        back_met = h * (.48 - .11) / 3
        front_met = h * (.925 - .11) / 3
        one_m = back_met + (front_met - back_met) * t
        actor_h = OPERATOR_HEIGHT_M * one_m
        actor_w = actor_h * 1.60
        # Height of embedded player viewport; Spine player fits skeleton inside.
        self.overlay.setGeometry(round(cx - actor_w / 2),
                                 round(floor_y - actor_h),
                                 max(1,round(actor_w)),max(1,round(actor_h)))
        self.overlay.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_overlay()

    def spawn_again(self):
        """Fresh random position; restart the operator's configured animation."""
        self.spawn_x, self.spawn_z = sample_spawn()
        self._fit_overlay()
        if self.overlay is not None:
            self.overlay.reload()
