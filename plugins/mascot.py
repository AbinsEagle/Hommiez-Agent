"""Hommie's desktop character: a cool sticker character (or a paperclip) that floats above your windows while Hommie runs and
acts out what she is doing - idle, listening (ripples follow your voice), thinking, speaking. Drag it anywhere; right-click it
for Hide / Quit. Say "hide the character" / "show the character", or use the Character settings (show, which one, how big).
Needs the system python3 with GTK3 (tools/mascot.py).
Delete this file and the character is gone; nothing else changes."""
import ctypes, json, os, re, signal, socket, subprocess, threading, time
from hommie.core import HERE

PRIORITY = 73
DEFAULTS = {"show": True, "character": "cool", "size": "medium"}
UI_TITLE = "Character"
UI = [{"key": "show", "label": "Show the character on the desktop", "type": "toggle"},
      {"key": "character", "label": "Which character", "type": "select", "options": [["cool", "Cool sticker"], ["clip", "Paperclip"]]},
      {"key": "size", "label": "Size", "type": "select", "options": [["small", "Small"], ["medium", "Medium"], ["large", "Large"]]}]
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hommie-mascot.sock")
TOOL = os.path.join(HERE, "tools", "mascot.py")

def _die_with_parent():
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)          # PR_SET_PDEATHSIG: Clip leaves when Hommie does, even after a crash

class Mascot:
    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.proc, self.sock, self.lock = None, None, threading.Lock()
        self.state, self.shown, self.look = "idle", None, None

    def _connect(self):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(SOCK)
        self.sock = s

    def _spawn(self):
        if self.proc is None or self.proc.poll() is not None:
            log = open(os.path.join(HERE, "logs", "mascot.log"), "a")
            self.proc = subprocess.Popen(["/usr/bin/python3", TOOL], stdout=log, stderr=log, preexec_fn=_die_with_parent,
                                         env={**os.environ, "GDK_BACKEND": "x11"})
        for _ in range(40):
            time.sleep(0.15)
            try:
                self._connect()
                return True
            except OSError:
                continue
        self.ctx.log("Clip did not start (see logs/mascot.log)")
        return False

    def send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.sendall(data)
                    return True
                except OSError:
                    self.sock = None
                    if attempt == 1 and not self._spawn():
                        return False
        return False

    def set_state(self, state):
        self.state = state
        self.send({"state": state})

    def watch(self):
        """Follows the Settings switch, and feeds the mic loudness to the ripples while she listens."""
        while True:
            time.sleep(0.12)
            want = bool(self.cfg["show"])
            look = (self.cfg["character"], self.cfg["size"])
            if look != self.look:                                  # picked another character or size in Settings
                self.look = look
                self.send({"character": look[0], "size": look[1]})
            if want != self.shown:
                self.shown = want
                self.send({"show": want})
            if want and self.state == "listening":
                ears = self.ctx.service("listen")
                self.send({"level": round(float(getattr(ears, "level", 0.0)), 3)})

    def close(self):
        self.send({"quit": True})
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

_mascot = None

def setup(ctx):
    global _mascot
    if not os.path.exists("/usr/bin/python3") or not os.path.exists(TOOL):
        raise FileNotFoundError("needs the system python3 with GTK3 and tools/mascot.py")
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    cfg = ctx.cfg("mascot", DEFAULTS)
    m = _mascot = Mascot(ctx, cfg)
    ctx.on("state", lambda state=None, **kw: m.set_state(state or "idle"))
    threading.Thread(target=m.watch, daemon=True).start()

    def show(m_, text):
        cfg["show"] = True
        return "Here I am!"

    def hide(m_, text):
        cfg["show"] = False
        return "Okay, I'll hide. Say show the character when you want me back."

    name = r"(?:clip|clippy|the character|the paper ?clip|the mascot|the sticker|my buddy)"
    ctx.command(rf"\b(?:hide|dismiss|get rid of|remove|put away)\b.*\b{name}\b", hide, "mascot_hide", priority=70)
    ctx.command(rf"\b(?:show|bring back|summon|where(?:'s| is)|come back)\b.*\b{name}\b", show, "mascot_show", priority=70)

def teardown(ctx):
    if _mascot:
        _mascot.close()
