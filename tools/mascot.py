#!/usr/bin/python3
"""Clip, Hommie's desktop character: a small paperclip with a face that floats above every window and acts out what Hommie is
doing - idle (bobbing, blinking, glancing about), listening (leans in, eyes wide, ripples that follow your voice), thinking
(eyes up, thought bubble), speaking (mouth moves, bounces). Run by plugins/mascot.py with the SYSTEM python3 (it needs
gi/GTK3) and GDK_BACKEND=x11, so KDE Wayland lets this one small window stay on top. The drawing is original artwork.

Messages arrive as JSON lines on the unix socket $XDG_RUNTIME_DIR/hommie-mascot.sock:
  {"state": "idle" | "listening" | "thinking" | "speaking"}   {"level": 0.0-1.0}  (mic loudness, for the ripples)
  {"show": true | false}   {"quit": true}
Drag it with the left mouse button (the spot is remembered); right-click for Hide / Quit."""
import json, math, os, random, socket, threading, time
import gi, cairo
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hommie-mascot.sock")
POS = os.path.join(HERE, "logs", "mascot_pos.json")
W, H, SCALE = 210, 250, 1.15                        # drawing units; the window is these times k (the size setting)
SPRITE = os.path.join(HERE, "assets", "mascot.png")
RIG = os.path.join(HERE, "assets", "mascot")           # the layered version (tools/make_mascot_layers.py): hands, sparkles, body
SIZES = {"small": 0.62, "medium": 0.85, "large": 1.15}
BODY, SHINE, SHADE = (0.04, 0.52, 1.0), (0.62, 0.84, 1.0), (0.0, 0.0, 0.0)
LABELS = {"listening": "listening", "thinking": "thinking", "speaking": "speaking"}
TARGET = {   # how each state poses the face: eye size, brow lift, mouth open, lean (degrees), look (x, y)
    "idle":      dict(eye=1.0, brow=0.0, mouth=0.0, lean=0.0, lx=0.0, ly=0.0),
    "listening": dict(eye=1.22, brow=1.0, mouth=0.3, lean=-7.0, lx=0.0, ly=0.0),
    "thinking":  dict(eye=0.95, brow=0.6, mouth=0.0, lean=4.0, lx=2.6, ly=-2.6),
    "speaking":  dict(eye=1.08, brow=0.35, mouth=0.0, lean=0.0, lx=0.0, ly=0.3),
}

def clip_path(cr):
    """A paperclip drawn as one wire: outer loop, then a smaller inner loop. Centred on (0, 0)."""
    cr.new_path()
    cr.move_to(-22, 26)
    cr.line_to(-22, -38)
    cr.arc(0, -38, 22, math.pi, 2 * math.pi)
    cr.line_to(22, 40)
    cr.arc(5.5, 40, 16.5, 0, math.pi)
    cr.line_to(-11, -24)
    cr.arc(0, -24, 11, math.pi, 2 * math.pi)
    cr.line_to(11, 24)

class Spring:
    """A value that glides to its target and settles with a little overshoot, like something with weight. Frame-rate independent."""
    def __init__(self, x=0.0, k=90.0, zeta=0.8):
        self.x, self.v, self.target, self.k, self.zeta = x, 0.0, x, k, zeta

    def step(self, dt):
        c = 2.0 * self.zeta * math.sqrt(self.k)
        n = max(1, int(dt / 0.008))
        h = dt / n
        for _ in range(n):
            self.v += (self.k * (self.target - self.x) - c * self.v) * h
            self.x += self.v * h

def bump(u, power=1.5):
    """0 -> 1 -> 0 over u in [0, 1] (a smooth gesture shape); 0 outside."""
    return math.sin(math.pi * u) ** power if 0.0 <= u <= 1.0 else 0.0

class Rig:
    """The layered character: a body with its hands lifted out, each hand pre-bent to every angle, and four loose sparkles/dashes."""
    def __init__(self, folder):
        with open(os.path.join(folder, "layers.json")) as f:
            self.meta = json.load(f)
        png = lambda n: cairo.ImageSurface.create_from_png(os.path.join(folder, n + ".png"))
        self.base = png("base")
        self.hands = {n: png(n) for n in ("hand_up", "hand_low")}
        self.loose = {n: png(n) for n in ("sparkle1", "sparkle2", "dash1", "dash2")}

    def draw_hand(self, cr, name, angle):
        m = self.meta[name]
        x0, y0, x1, y1 = m["rect"]
        w, h = x1 - x0, y1 - y0
        i = int(round(max(m["amin"], min(m["amax"], angle)))) - m["amin"]
        cr.save()
        cr.rectangle(x0, y0, w, h)
        cr.clip()
        cr.set_source_surface(self.hands[name], x0 - i * w, y0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()

class Clip(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Clip")
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)                          # clicking Clip must never steal focus from what you are typing in
        self.set_focus_on_map(False)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.set_visual(self.get_screen().get_rgba_visual())
        self.character, self.size_name, self.k = "cool", "medium", SIZES["medium"]
        try:
            self.sprite = cairo.ImageSurface.create_from_png(SPRITE)
        except Exception:                                      # no picture: fall back to the paperclip
            self.sprite, self.character = None, "clip"
        self.sprite_ok = self.sprite is not None
        try:
            self.rig = Rig(RIG)
            self.character = "cool"
        except Exception:                                      # no layers built: use the flat picture, or the paperclip
            self.rig = None
        self.init_motion()
        self.set_default_size(int(W * self.k), int(H * self.k))
        self.set_resizable(False)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("configure-event", self.on_moved)
        self.state, self.level, self.shown, self.save_id = "idle", 0.0, True, None
        self.t0 = time.time()
        self.cur = dict(TARGET["idle"], bob=0.0, look_x=0.0, look_y=0.0, level=0.0)
        self.blink_at, self.glance_at, self.wave_at = self.t0 + 2.0, self.t0 + 4.0, self.t0 + 20.0
        self.blink_t = self.wave_t = self.hop_t = -9.0
        self.glance = (0.0, 0.0)
        self.place()
        self.connect("realize", lambda w: self.apply_size())
        self.menu = Gtk.Menu()
        for label, fn in (("Hide Clip", lambda *_: self.set_shown(False)), ("Quit Clip", lambda *_: Gtk.main_quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", fn)
            self.menu.append(item)
        self.menu.show_all()
        self.show_all()
        GLib.timeout_add(40, self.tick)

    # ---- where it sits
    def place(self):
        geo = Gdk.Display.get_default().get_monitor(0).get_geometry()
        x, y = geo.x + geo.width - int(W * self.k) - 30, geo.y + geo.height - int(H * self.k) - 70   # bottom right, above the panel
        try:
            with open(POS) as f:
                p = json.load(f)
            x, y = int(p["x"]), int(p["y"])
        except (OSError, ValueError, KeyError):
            pass
        self.move(max(0, min(x, geo.width - 60)), max(0, min(y, geo.height - 60)))

    def apply_size(self):
        """Window = drawing size x k. Only the character itself takes clicks; the empty corners let them through to the app below."""
        self.resize(int(W * self.k), int(H * self.k))
        win = self.get_window()
        if win is not None:
            k = self.k
            box = cairo.RectangleInt(int(18 * k), int(40 * k), int(176 * k), int(200 * k))
            win.input_shape_combine_region(cairo.Region(box), 0, 0)

    def on_moved(self, w, ev):
        if self.save_id:
            GLib.source_remove(self.save_id)
        self.save_id = GLib.timeout_add(600, self.save_pos)

    def save_pos(self):
        self.save_id = None
        x, y = self.get_position()
        try:
            os.makedirs(os.path.dirname(POS), exist_ok=True)
            with open(POS, "w") as f:
                json.dump({"x": x, "y": y}, f)
        except OSError:
            pass
        return False

    def on_press(self, w, ev):
        if ev.button == 1:
            self.begin_move_drag(1, int(ev.x_root), int(ev.y_root), ev.time)
        elif ev.button == 3:
            self.menu.popup_at_pointer(ev)
        return True

    # ---- messages from Hommie (always on the GTK thread)
    def handle(self, msg):
        if "state" in msg and msg["state"] in TARGET and msg["state"] != self.state:
            if msg["state"] == "listening":
                self.hop_t = time.time() - self.t0              # a little hop when she starts listening to you
            self.on_state(self.state, msg["state"])
            self.state = msg["state"]
        if "level" in msg:
            self.level = max(0.0, min(1.0, float(msg["level"])))
        if msg.get("character") in ("cool", "clip") and (msg["character"] != "cool" or self.sprite or self.rig):
            self.character = msg["character"]
        if msg.get("size") in SIZES and msg["size"] != self.size_name:
            self.size_name, self.k = msg["size"], SIZES[msg["size"]]
            self.apply_size()
        if "show" in msg:
            self.set_shown(bool(msg["show"]))
        if msg.get("quit"):
            Gtk.main_quit()

    def set_shown(self, on):
        self.shown = on
        if on:
            self.show_all()
            self.set_keep_above(True)
        else:
            self.hide()

    # ---- motion (the layered character)
    def init_motion(self):
        sp = lambda k, z, x=0.0: Spring(x, k, z)
        self.sp = {"lean": sp(55, 0.62), "bob": sp(150, 0.5), "grow": sp(70, 0.7, 1.0), "squash": sp(230, 0.42),
                   "up": sp(190, 0.5, -1.5), "low": sp(190, 0.5, 2.0), "exc": sp(16, 1.0)}
        self.sp["grow"].target = 1.0
        self.last_now, self.lv, self.s1, self.s2 = None, 0.0, 0.0, 0.0
        self.gest, self.next_gest = ("none", -9.0), 6.0

    def on_state(self, old, new):
        """A state change gets a small physical 'reaction' on top of the new pose: anticipation, a hop, an inhale."""
        sp = self.sp
        if new == "listening":
            sp["squash"].v += 3.2; sp["bob"].v -= 150; sp["up"].v += 55
        elif new == "thinking":
            sp["lean"].v += 22; sp["up"].v -= 60; sp["squash"].v -= 1.0
        elif new == "speaking":
            sp["grow"].v += 0.55; sp["bob"].v -= 70; sp["low"].v += 40
        elif new == "idle":
            sp["exc"].x = max(sp["exc"].x, 0.8); sp["squash"].v -= 2.2; sp["lean"].v -= 10

    def advance(self, dt, now):
        """Work out where everything wants to be for the current state, then let the springs carry it there."""
        sp, st = self.sp, self.state
        self.lv += (self.level - self.lv) * min(1.0, dt * 10.0)
        talk = st == "speaking"
        self.s1 = abs(math.sin(now * 12.5) * 0.6 + math.sin(now * 8.1) * 0.4) if talk else self.s1 * 0.85
        self.s2 = abs(math.sin(now * 9.7 + 1.3) * 0.55 + math.sin(now * 6.3 + 0.4) * 0.45) if talk else self.s2 * 0.85
        s1, s2, lv = self.s1, self.s2, self.lv
        up_off = low_off = 0.0
        if st == "idle" and now > self.next_gest:               # now and then it does something on its own, never twice alike in a row
            kinds = [k for k in ("point", "flourish", "wiggle") if k != self.gest[0]]
            self.gest, self.next_gest = (random.choice(kinds), now), now + random.uniform(9.0, 17.0)
        kind, t0 = self.gest
        u = now - t0
        if st == "idle":
            if kind == "point":
                low_off = 13.0 * bump(u / 0.5); sp["squash"].v += 0.4 * bump(u / 0.5) if u < 0.05 else 0
            elif kind == "flourish":
                up_off = -13.0 * bump(u / 0.9)
                if 0 <= u < dt * 1.5:
                    sp["exc"].x = 1.0
            elif kind == "wiggle":
                low_off = 9.0 * math.sin(2 * math.pi * 3.2 * u) * math.exp(-2.4 * u) if 0 <= u < 1.6 else 0.0
        if st == "idle":
            tg = dict(lean=0.9 * math.sin(now * 0.55) + 0.5 * math.sin(now * 1.3 + 2), bob=1.6 * math.sin(now * 1.0), grow=1.0,
                      squash=0.010 * math.sin(now * 1.7), up=-1.5 + 1.2 * math.sin(now * 0.8 + 1) + up_off,
                      low=2.0 + 1.5 * math.sin(now * 1.15) + low_off)
        elif st == "listening":
            flick = (4.0 + 8.0 * lv) * math.sin(2 * math.pi * 2.0 * now)
            tg = dict(lean=-6.0 + 0.8 * math.sin(now * 1.1), bob=0.8 * math.sin(now * 2.2), grow=1.05,
                      squash=0.012 * math.sin(now * 2.4), up=4.0 + 1.5 * math.sin(now * 4.4), low=7.0 + flick)
        elif st == "thinking":
            x = now % 2.2
            tap = max(0.0, math.sin(2 * math.pi * x * 2.5)) ** 2 if x < 1.2 else 0.0      # tap, tap, tap ... pause
            tg = dict(lean=4.5 + 2.0 * math.sin(now * 0.9), bob=1.2 * math.sin(now * 1.6), grow=1.0,
                      squash=0.012 * math.sin(now * 1.3), up=-8.0 + 2.0 * math.sin(now * 0.9), low=1.0 + 12.0 * tap)
        else:                                                                                # speaking
            tg = dict(lean=1.4 * math.sin(now * 2.1) * (0.4 + 0.6 * s1), bob=-3.5 * abs(math.sin(now * 5.5)) * (0.4 + 0.6 * s1), grow=1.0,
                      squash=0.045 * s1 - 0.01, up=-6.0 + 11.0 * s2, low=1.0 + 13.0 * s1)
        for k, v in tg.items():
            sp[k].target = v
        # follow-through: the hands lag behind the body, so they swing the other way when it moves
        sp["up"].v -= sp["lean"].v * 0.35 * dt * 60 * 0.1
        sp["low"].v -= sp["bob"].v * 0.25 * dt * 60 * 0.02
        for s_ in sp.values():
            s_.step(dt)

    def draw_rig(self, cr, now):
        sp, rig = self.sp, self.rig
        sw, sh = rig.base.get_width(), rig.base.get_height()
        unit = 150.0 / sh
        lean, bob = sp["lean"].x, sp["bob"].x
        exc, lv = max(0.0, sp["exc"].x), self.lv
        # the four loose sparkles and dashes drift on their own, following the body only a little
        cr.save()
        cr.translate(W / 2 + 4, 222 + bob * 0.6)
        cr.rotate(math.radians(lean * 0.35))
        cr.scale(unit, unit)
        cr.translate(-sw / 2, -sh)
        for i, (name, surf) in enumerate(rig.loose.items()):
            x0, y0, x1, y1 = rig.meta[name]["box"]
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            ph = i * 1.7
            cr.save()
            cr.translate(cx, cy)
            if name.startswith("sparkle"):
                pulse = 1.0 + 0.09 * math.sin(now * 2.1 + ph) + 0.38 * exc + 0.16 * lv * (1 if self.state == "listening" else 0)
                cr.rotate(math.radians(7 * math.sin(now * 1.2 + ph) + 26 * exc * (1 if i % 2 else -1)))
                cr.scale(pulse, pulse)
                alpha = 0.93 + 0.07 * math.sin(now * 2.6 + ph)
            else:
                swoosh = 5.0 * self.s1 * (1 if i % 2 else -1) if self.state == "speaking" else 0.0
                cr.translate(2.0 * math.sin(now * 1.1 + ph) + swoosh, 0)
                alpha = 0.92 + 0.08 * math.sin(now * 3.0 + ph)
            cr.set_source_surface(surf, x0 - cx, y0 - cy)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint_with_alpha(max(0.0, min(1.0, alpha)))
            cr.restore()
        cr.restore()
        # the body, tilting and bouncing from its feet, with the hands drawn on top at their bend
        sq = sp["squash"].x
        grow = sp["grow"].x
        cr.save()
        cr.translate(W / 2 + 4, 222 + bob)
        cr.rotate(math.radians(lean))
        cr.scale((1.0 - 0.6 * sq) * grow * unit, (1.0 + sq) * grow * unit)
        cr.translate(-sw / 2, -sh)
        cr.set_source_surface(rig.base, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        rig.draw_hand(cr, "hand_up", sp["up"].x)
        rig.draw_hand(cr, "hand_low", sp["low"].x)
        if self.state == "speaking":
            self.draw_mouth(cr, self.s1)
        cr.restore()
        if self.state == "listening":                            # ripples beside its ear
            cr.new_path()
            for i in range(3):
                ph = (now * 1.3 + i / 3.0) % 1.0
                cr.set_source_rgba(0.35, 0.65, 1.0, (1 - ph) * (0.4 + lv * 0.6))
                cr.set_line_width(2.6)
                cr.arc(W / 2 + 20, 130, 52 + ph * (16 + lv * 26), math.radians(-38), math.radians(38))
                cr.stroke()

    @staticmethod
    def draw_mouth(cr, speak):
        """Wipe the little drawn smile and put a mouth there that opens with the speech. (Sprite coordinates.)"""
        mx, my = 183.0, 135.5
        cr.save(); cr.translate(mx, my); cr.scale(1.0, 0.5); cr.arc(0, 0, 22, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgb(1, 1, 1)
        cr.fill()
        ry = 1.6 + speak * 7.0
        cr.save(); cr.translate(mx, my); cr.scale(1.0, ry / 8.0); cr.arc(0, 0, 8.0, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgb(0.1, 0.03, 0.05)
        cr.fill()

    # ---- animation
    def tick(self):
        if self.shown:
            self.queue_draw()
        busy = self.state != "idle"
        GLib.timeout_add(33 if busy else 50, self.tick)           # 30 frames a second while it acts, 20 at rest
        return False

    def on_draw(self, w, cr):
        now = time.time() - self.t0
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.new_path()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.scale(self.k, self.k)
        if self.character == "cool" and self.rig:
            dt = 1 / 30 if self.last_now is None else max(0.001, min(0.05, now - self.last_now))
            self.last_now = now
            self.advance(dt, now)
            self.draw_rig(cr, now)
            if self.state == "thinking":
                self.draw_thought(cr, now)
            if self.state in LABELS:
                self.draw_label(cr, LABELS[self.state])
            return
        c, tg = self.cur, TARGET[self.state]
        for k in ("eye", "brow", "mouth", "lean", "lx", "ly"):                       # ease toward the pose for this state
            c[k] += (tg[k] - c[k]) * 0.18
        c["level"] += (self.level - c["level"]) * 0.35
        # idle life: blinks, glances, the odd wave
        if now > self.blink_at:
            self.blink_t, self.blink_at = now, now + random.uniform(2.5, 6.0)
        if now > self.glance_at:
            self.glance, self.glance_at = (random.uniform(-2.2, 2.2), random.uniform(-1.2, 1.0)), now + random.uniform(3.0, 8.0)
        if self.state == "idle" and now > self.wave_at:
            self.wave_t, self.wave_at = now, now + random.uniform(18, 35)
        blink = 1.0 - max(0.0, math.sin(math.pi * (now - self.blink_t) / 0.17)) if 0 <= now - self.blink_t < 0.17 else 1.0
        wave = math.sin((now - self.wave_t) * 16) * 9 * math.exp(-(now - self.wave_t) * 2.2) if 0 <= now - self.wave_t < 1.6 else 0.0
        hop = -14 * math.sin(math.pi * min(1.0, (now - self.hop_t) / 0.45)) if 0 <= now - self.hop_t < 0.45 else 0.0
        speak = abs(math.sin(now * 13) * 0.6 + math.sin(now * 8.3) * 0.4) if self.state == "speaking" else 0.0
        bob = math.sin(now * 2.0) * 2.5 - (abs(math.sin(now * 7.0)) * 3.5 if self.state == "speaking" else 0.0) \
              + (math.sin(now * 3.2) * 1.5 if self.state == "thinking" else 0.0) + hop
        sway = math.sin(now * 1.4) * 1.6 + (math.sin(now * 2.3) * 3.0 if self.state == "thinking" else 0.0)

        if self.character != "cool":                       # soft shadow on the "floor" (the sticker is cut off at the feet, so it has none)
            cr.save()
            cr.translate(W / 2, H - 52)
            cr.scale(1.0, 0.22)
            cr.arc(0, 0, 34 - bob * 0.4, 0, 2 * math.pi)
            cr.set_source_rgba(0, 0, 0, 0.16)
            cr.fill()
            cr.restore()

        if self.character == "cool":
            self.draw_sticker(cr, c, now, bob, sway, wave, speak)
        else:
            cr.save()
            cr.translate(W / 2, H / 2 + 4 + bob)
            cr.rotate(math.radians(c["lean"] + sway + wave))
            cr.scale(SCALE, SCALE)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            # the wire: shade, body, shine
            clip_path(cr); cr.translate(1.6, 2.4); cr.set_source_rgba(*SHADE, 0.22); cr.set_line_width(10); cr.stroke(); cr.translate(-1.6, -2.4)
            clip_path(cr); cr.set_source_rgb(*BODY); cr.set_line_width(8.5); cr.stroke()
            clip_path(cr); cr.translate(-1.4, -1.4); cr.set_source_rgba(*SHINE, 0.85); cr.set_line_width(2.4); cr.stroke(); cr.translate(1.4, 1.4)
            self.draw_ripples(cr, now)
            self.draw_face(cr, c, blink, speak)
            cr.restore()
        if self.state == "thinking":
            self.draw_thought(cr, now)
        if self.state in LABELS:
            self.draw_label(cr, LABELS[self.state])

    def draw_sticker(self, cr, c, now, bob, sway, wave, speak):
        """The cool sticker character: one picture, brought to life with squash-and-stretch, tilt and a moving mouth."""
        sw, sh = self.sprite.get_width(), self.sprite.get_height()
        unit = 150.0 / sh
        breathe = math.sin(now * 1.8) * 0.012
        sx = 1.0 - breathe * 0.6 - speak * 0.03
        sy = 1.0 + breathe + speak * 0.05 + (math.sin(now * 2.6) * 0.012 if self.state == "thinking" else 0.0)
        grow = 1.05 if self.state == "listening" else 1.0
        cr.save()
        cr.translate(W / 2 + 4, 222 + bob)                                    # pivot at the feet, so it tilts and bounces from the ground
        cr.rotate(math.radians(c["lean"] + sway + wave))
        cr.scale(sx * grow * unit, sy * grow * unit)
        cr.set_source_surface(self.sprite, -sw / 2, -sh)
        cr.paint()
        if self.state == "speaking":
            mx, my = 183 - sw / 2, 132 - sh                                   # where the sticker's little smile is
            cr.save()
            cr.translate(mx, my + 3.5)
            cr.scale(1.0, 0.5)
            cr.arc(0, 0, 22, 0, 2 * math.pi)                                  # wipe the drawn smile...
            cr.restore()
            cr.set_source_rgb(1, 1, 1)
            cr.fill()
            ry = 1.6 + speak * 7.0
            cr.save()
            cr.translate(mx, my + 3.5)
            cr.scale(1.0, ry / 8.0)
            cr.arc(0, 0, 8.0, 0, 2 * math.pi)                                 # ...and draw a mouth that opens and closes
            cr.restore()
            cr.set_source_rgb(0.1, 0.03, 0.05)
            cr.fill()
        cr.restore()
        if self.state == "listening":                                         # ripples beside its ear
            cr.new_path()
            lvl = c["level"]
            for i in range(3):
                ph = (now * 1.3 + i / 3.0) % 1.0
                cr.set_source_rgba(0.35, 0.65, 1.0, (1 - ph) * (0.4 + lvl * 0.6))
                cr.set_line_width(2.6)
                cr.arc(W / 2 + 20, 130, 52 + ph * (16 + lvl * 26), math.radians(-38), math.radians(38))
                cr.stroke()

    @staticmethod
    def ink(cr, width, color, fill=False):
        """Draw the current path in dark ink with a light halo, so features read on dark and light desktops alike."""
        cr.set_source_rgba(1, 1, 1, 0.92); cr.set_line_width(width + 3.2)
        cr.stroke_preserve()
        cr.set_source_rgb(*color)
        if fill:
            cr.fill_preserve()
        cr.set_line_width(width)
        cr.stroke()

    def draw_ripples(self, cr, now):
        cr.new_path()
        if self.state != "listening":
            return
        lvl = self.cur["level"]
        for i in range(3):
            ph = (now * 1.3 + i / 3.0) % 1.0
            r = 30 + ph * (16 + lvl * 26)
            cr.set_source_rgba(*SHINE, (1 - ph) * (0.35 + lvl * 0.6))
            cr.set_line_width(2.4)
            cr.arc(0, -8, r, math.radians(-42), math.radians(42))
            cr.stroke()

    def draw_face(self, cr, c, blink, speak):
        look_x, look_y = c["lx"] + self.glance[0] * (self.state == "idle"), c["ly"] + self.glance[1] * (self.state == "idle")
        for sx in (-1, 1):
            ex, ey, er = sx * 8.6, -33.0, 8.4 * c["eye"]
            cr.save()
            cr.translate(ex, ey)
            cr.scale(1.0, max(0.08, blink))
            cr.arc(0, 0, er, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1); cr.fill_preserve()
            cr.set_source_rgba(0.05, 0.12, 0.3, 0.55); cr.set_line_width(1.1); cr.stroke()
            pr = er * 0.5
            px, py = max(-er + pr + 0.6, min(er - pr - 0.6, look_x)), max(-er + pr + 0.6, min(er - pr - 0.6, look_y))
            cr.arc(px, py, pr, 0, 2 * math.pi)
            cr.set_source_rgb(0.06, 0.09, 0.16); cr.fill()
            cr.arc(px - pr * 0.3, py - pr * 0.35, pr * 0.3, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.9); cr.fill()
            cr.restore()
            # eyebrows: lifted when listening, one cocked while thinking
            lift = c["brow"] * 5.0 + (3.5 if (self.state == "thinking" and sx > 0) else 0.0) + (speak * 1.5 if self.state == "speaking" else 0.0)
            tilt = (-2.0 * sx if self.state == "thinking" and sx < 0 else 0.0) + (1.5 * sx if self.state == "listening" else 0.0)
            cr.move_to(sx * 8.6 - 6.5, -45.0 - lift + tilt * 0.6)
            cr.curve_to(sx * 8.6 - 2, -48.0 - lift - tilt, sx * 8.6 + 2, -48.0 - lift - tilt, sx * 8.6 + 6.5, -45.0 - lift - tilt * 0.6)
            self.ink(cr, 2.4, (0.04, 0.2, 0.55))
        # mouth
        my = -14.0
        if self.state == "speaking":
            ry = 1.2 + speak * 6.0
            cr.save(); cr.translate(0, my + ry * 0.3); cr.scale(1.0, ry / 4.5); cr.arc(0, 0, 4.8, 0, 2 * math.pi); cr.restore()
            self.ink(cr, 1.6, (0.08, 0.04, 0.16), fill=True)
        elif self.state == "listening":
            cr.arc(0, my, 2.6 + c["level"] * 2.5, 0, 2 * math.pi)
            self.ink(cr, 1.6, (0.08, 0.04, 0.16), fill=True)
        elif self.state == "thinking":
            cr.move_to(-4.5, my + 1); cr.line_to(4.5, my - 1)
            self.ink(cr, 2.2, (0.04, 0.16, 0.45))
        else:
            cr.arc(0, my - 3, 6.2, math.radians(30), math.radians(150))
            self.ink(cr, 2.2, (0.04, 0.16, 0.45))

    def draw_thought(self, cr, now):
        cr.new_path()
        ox, oy = W / 2, H / 2 + 4
        for x, y, r in ((ox + 38, oy - 70, 2.8), (ox + 48, oy - 82, 4.2)):
            cr.arc(x, y, r, 0, 2 * math.pi); cr.set_source_rgba(1, 1, 1, 0.96); cr.fill_preserve()
            cr.set_source_rgba(0.1, 0.2, 0.4, 0.3); cr.set_line_width(1); cr.stroke()
        bx, by = ox + 66, oy - 104
        cr.save(); cr.translate(bx, by); cr.scale(1.0, 0.64); cr.arc(0, 0, 30, 0, 2 * math.pi); cr.restore()
        cr.set_source_rgba(1, 1, 1, 0.97); cr.fill_preserve(); cr.set_source_rgba(0.1, 0.2, 0.4, 0.3); cr.set_line_width(1.2); cr.stroke()
        for i in range(3):
            k = max(0.0, math.sin(now * 5.0 - i * 0.9))
            cr.arc(bx - 14 + i * 14, by - k * 3, 3.2 + k * 1.4, 0, 2 * math.pi)
            cr.set_source_rgba(*BODY, 0.55 + 0.45 * k); cr.fill()

    def draw_label(self, cr, text):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(11)
        ext = cr.text_extents(text)
        w, h, x, y = ext.width + 18, 20, W / 2, H - 24
        cr.new_sub_path()
        r = h / 2
        cr.arc(x - w / 2 + r, y, r, math.pi / 2, 3 * math.pi / 2); cr.arc(x + w / 2 - r, y, r, -math.pi / 2, math.pi / 2); cr.close_path()
        cr.set_source_rgba(0.08, 0.1, 0.16, 0.82); cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y + h / 2 - 6)
        cr.show_text(text)

def serve(win):
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)

    def reader(conn):
        with conn, conn.makefile("r") as f:                   # one connection stays open and carries many lines
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                GLib.idle_add(lambda m=msg: win.handle(m) or False)

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=reader, args=(conn,), daemon=True).start()

def watchdog(parent):
    if os.getppid() != parent:                                # Hommie exited: don't leave a stray character on the desktop
        Gtk.main_quit()
        return False
    return True

if __name__ == "__main__":
    win = Clip()
    threading.Thread(target=serve, args=(win,), daemon=True).start()
    GLib.timeout_add_seconds(2, watchdog, os.getppid())
    print("mascot ready", flush=True)
    Gtk.main()
    try:
        os.unlink(SOCK)
    except OSError:
        pass
