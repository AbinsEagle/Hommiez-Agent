"""Screen guide: ask "guide me to add a fillet" or "show me where the extrude tool is" and Hommie highlights, on your real
screen, the one thing to click next (amber box, numbered label, a ghost cursor gliding to it) and says it aloud.

How it knows what happened after you click: it doesn't listen to the mouse. While a step is showing it takes a cheap
screenshot about once a second and compares it with the one from when the step appeared. When the picture changes (a
menu or submenu opens, a dialog appears, a tab switches) and then stops moving, it sends that new screenshot to the vision
model together with the goal and the steps so far, and the model points at the next control - inside the submenu that just
appeared, or back on track if you clicked something else. Changes that stay inside one control (a checkbox) can't be
seen, so say "next" or "done". Also: "repeat" shows the step again, "stop the guide" ends it.

Privacy: a screenshot goes to Gemini (Google) only for each step of a guide you asked for, never in the background, and
Hommie says "looking at your screen" first. Needs: spectacle (KDE), the system python3 with GTK3 (tools/overlay.py),
GEMINI_API_KEY, and Pillow in the venv. Delete this file and the feature is gone."""
import json, os, re, socket, subprocess, threading, time
from hommie import vision
from hommie.core import HERE

PRIORITY = 72
REQUIRES = ["PIL"]
DEFAULTS = {"model": "claude:sonnet", "speak": True, "max_steps": 25, "idle_seconds": 240}
UI_TITLE = "Screen guide"
UI = [{"key": "model", "label": "Who looks at the screen", "type": "select",
       "options": [["claude:sonnet", "Claude Sonnet (best, uses quota)"], ["claude:haiku", "Claude Haiku (cheaper)"],
                   ["gemini-3.5-flash", "Gemini Flash (free: ~20 a day)"]]},
      {"key": "speak", "label": "Speak each step", "type": "toggle"}]

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hommie-overlay.sock")
OVERLAY = os.path.join(HERE, "tools", "overlay.py")
INTRO = 1.4          # the overlay animates this long; polling starts after it is still
CHANGE = 0.004       # fraction of the screen that must change to count as "something happened"
SETTLED = 0.003      # ...and it has settled once two shots in a row differ by less than this
POLL = 0.3           # pause between screenshots (a screenshot itself takes about a second)

class Guide:
    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.proc, self.thread = None, None
        self.stop, self.nudge, self.repeat = threading.Event(), threading.Event(), threading.Event()

    @property
    def active(self):
        return bool(self.thread and self.thread.is_alive())

    # ---- overlay process
    def _send(self, msg):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        s.connect(SOCK)
        s.sendall((json.dumps(msg) + "\n").encode())
        s.close()

    def overlay(self, **msg):
        """Send a command, starting the overlay process first if needed. False if it can't run."""
        try:
            self._send(msg)
            return True
        except OSError:
            pass
        if self.proc is None or self.proc.poll() is not None:
            log = open(os.path.join(HERE, "logs", "overlay.log"), "a")
            self.proc = subprocess.Popen(["/usr/bin/python3", OVERLAY], stdout=log, stderr=log,
                                         env={**os.environ, "GDK_BACKEND": "x11"})
        for _ in range(40):
            time.sleep(0.15)
            try:
                self._send(msg)
                return True
            except OSError:
                continue
        self.ctx.log("screen-guide overlay did not start (see logs/overlay.log)")
        return False

    # ---- the guide itself
    def start(self, goal):
        if self.active:
            self.stop.set()
            self.thread.join(timeout=5)
        self.stop.clear(); self.nudge.clear(); self.repeat.clear()
        self.thread = threading.Thread(target=self._run, args=(goal,), daemon=True)
        self.thread.start()

    def _say(self, text):
        if self.cfg["speak"] and text:
            threading.Thread(target=self.ctx.say, args=(text,), daemon=True).start()

    def _look(self, goal, history, event):
        self.overlay(cmd="hide")
        time.sleep(0.35)                                        # let the overlay vanish so the model sees the bare app
        img = vision.capture()
        res, err = vision.look(img, goal, history, event, self.cfg["model"])
        return img, res, err

    def _run(self, goal):
        history, event, n = [], "This is the first step.", 0
        try:
            while not self.stop.is_set() and n < self.cfg["max_steps"]:
                img, res, err = self._look(goal, history, event)
                if err:
                    self.ctx.say(err)
                    return
                if res["status"] == "done":
                    self.ctx.say(res.get("say") or "That's it. You're done.")
                    return
                if res["status"] == "blocked":
                    self.ctx.say(res.get("say") or "I can't guide you on this screen.")
                    return
                n += 1
                history.append(f"{n}. {res.get('say', '')}")
                self._show(res, img, n)
                self._say(res.get("say"))
                event = self._wait(img, res, n)
                if event is None:
                    return
        except Exception as e:                                  # a guide must never take Hommie down
            self.ctx.log(f"screen guide failed: {type(e).__name__}: {e}")
            try:
                self.ctx.say("The screen guide ran into a problem and stopped.")
            except Exception:
                pass
        finally:
            self.overlay(cmd="hide")

    def _show(self, res, img, n):
        label = res.get("label") or res.get("say") or ""
        if res.get("box"):
            self.overlay(cmd="show", box=vision.to_pixels(res["box"], img.width, img.height),
                         screen=[img.width, img.height], n=n, label=label)
        else:
            self.overlay(cmd="note", n=n, label=label)

    def _wait(self, img, res, n):
        """Watch the screen until something changed and settled, or the user says next. Returns the event text for the
        next question to the model, or None when the guide should end."""
        time.sleep(INTRO)
        base = vision.thumb(vision.capture())
        deadline = time.time() + self.cfg["idle_seconds"]
        moved, last, since = False, None, 0.0
        while not self.stop.is_set():
            if self.nudge.is_set():
                self.nudge.clear()
                return "The user says they finished the last step."
            if self.repeat.is_set():
                self.repeat.clear()
                self._show(res, img, n)
                self._say(res.get("say"))
                time.sleep(INTRO)
                base, moved, deadline = vision.thumb(vision.capture()), False, time.time() + self.cfg["idle_seconds"]
                continue
            if time.time() > deadline:
                self.ctx.say("I'll stop here. Ask me to guide you again whenever you like.")
                return None
            time.sleep(POLL)
            now = vision.thumb(vision.capture())
            if not moved:
                if vision.changed(base, now) > CHANGE:
                    moved, last, since = True, now, time.time()
            else:
                if vision.changed(last, now) < SETTLED or time.time() - since > 5:
                    return "The screen changed after the last step."
                last = now
        return None

    def close(self):
        self.stop.set()
        try:
            self._send({"cmd": "quit"})
        except OSError:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

_guide = None

def setup(ctx):
    global _guide
    if not os.path.exists("/usr/bin/spectacle") or not os.path.exists("/usr/bin/python3"):
        raise FileNotFoundError("needs spectacle (KDE) and the system python3 with GTK3")
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    cfg = ctx.cfg("guide", DEFAULTS)
    g = _guide = Guide(ctx, cfg)

    def goal_of(m, text):
        goal = (m.groupdict().get("goal") or "").strip(" .?!") or text.strip(" .?!")
        return re.sub(r"^(?:to|how to|how do i|how i can|how can i|where)\s+", "", goal, flags=re.I) or text

    def start_cmd(m, text):
        g.start(goal_of(m, text))
        return "Looking at your screen."

    def active_only(fn):
        return lambda m, text: fn(m, text) if g.active else None

    def next_cmd(m, text):
        g.nudge.set()
        return "Okay."

    def repeat_cmd(m, text):
        g.repeat.set()
        return "Sure."

    def stop_cmd(m, text):
        g.stop.set()
        g.overlay(cmd="hide")
        return "Okay, guide closed."

    start_words = (r"guide me|walk me through|on (?:my )?screen|highlight|point (?:me )?(?:to|at)|show me where|"
                   r"where (?:do i|can i) (?:click|find|go)|where(?:'s| is| are) the .{1,40}?(?:tool|button|menu|option|icon|panel|tab|setting)")
    ctx.command(rf"\b(?:{start_words})\b\s*(?P<goal>.*)", start_cmd, "guide_start", priority=74)
    ctx.command(r"^\W*(?:next(?: step)?|done|i(?:'?ve)? (?:did it|clicked it|done it)|finished|got it|okay,? done|ok,? done)\W*$",
                active_only(next_cmd), "guide_next", priority=88)
    ctx.command(r"^\W*(?:repeat|again|say that again|show me again|where(?:'s| is) (?:it|that))\W*$",
                active_only(repeat_cmd), "guide_repeat", priority=88)
    ctx.command(r"\b(?:stop|cancel|close|hide|end|exit|dismiss)\b.*\b(?:guide|overlay|highlight)\b|\bthat'?s enough\b",
                active_only(stop_cmd), "guide_stop", priority=89)

def teardown(ctx):
    if _guide:
        _guide.close()
