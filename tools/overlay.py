#!/usr/bin/python3
"""Screen-guide overlay for Hommie: a transparent, always-on-top, click-through window that draws a highlight box, a
numbered label and a ghost cursor over whatever app is underneath. Run by plugins/guide.py with the SYSTEM python3
(it needs gi/GTK3, which the venv doesn't have) and GDK_BACKEND=x11, because on KDE Wayland only an X11 (XWayland)
override-redirect window can sit above everything at absolute coordinates without taking clicks or focus.

Commands arrive as one JSON line on the unix socket $XDG_RUNTIME_DIR/hommie-overlay.sock:
  {"cmd": "show", "box": [x1, y1, x2, y2], "screen": [W, H], "n": 2, "label": "Click File"}   box in screenshot pixels
  {"cmd": "note", "label": "Press Ctrl+S", "n": 3}                                           label only, no box
  {"cmd": "hide"}    {"cmd": "quit"}
After a short intro animation the picture is completely still, so plugins/guide.py can tell when the app underneath changes."""
import json, math, os, socket, sys, threading, time
import gi, cairo
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hommie-overlay.sock")
ACCENT = (1.0, 0.72, 0.1)           # warm amber: visible on dark and light apps
INTRO = 1.2                         # seconds of movement/ping before the picture freezes
IDLE_HIDE = 600                     # hide itself after 10 minutes with no command

class Overlay(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)           # override-redirect: no WM placement, focus or decoration
        geo = Gdk.Display.get_default().get_monitor(0).get_geometry()
        self.W, self.H = geo.width, geo.height
        self.set_visual(self.get_screen().get_rgba_visual())
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.move(geo.x, geo.y)
        self.resize(self.W, self.H)
        self.connect("draw", self.on_draw)
        self.connect("realize", lambda w: w.get_window().input_shape_combine_region(cairo.Region(), 0, 0))  # click-through
        self.step, self.t0, self.cur, self.last_cmd = None, 0.0, (self.W / 2, self.H / 2), time.time()
        self.anim = False

    # ---- commands (always on the GTK thread)
    def handle(self, msg):
        self.last_cmd = time.time()
        cmd = msg.get("cmd")
        if cmd in ("show", "note"):
            box = None
            if cmd == "show" and msg.get("box"):
                sw, sh = msg.get("screen") or [self.W, self.H]
                fx, fy = self.W / sw, self.H / sh
                x1, y1, x2, y2 = msg["box"]
                box = (min(x1, x2) * fx, min(y1, y2) * fy, max(x1, x2) * fx, max(y1, y2) * fy)
            old = self.step["target"] if self.step and self.step["target"] else self.cur
            arrow = None
            if cmd == "show" and msg.get("arrow"):                # a big pointing arrow instead of a box
                sw, sh = msg.get("screen") or [self.W, self.H]
                ax, ay = msg["arrow"][0] * self.W / sw, msg["arrow"][1] * self.H / sh
                box = (ax - 3, ay - 3, ax + 3, ay + 3)
                arrow = (ax, ay, msg.get("from_deg", 225))        # 225 = the arrow comes in from the upper left
            self.step = {"box": box, "n": msg.get("n", 1), "label": msg.get("label", ""), "arrow": arrow,
                         "target": ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) if box else None, "from": old}
            self.t0 = time.time()
            self.show_all()
            self.get_window().input_shape_combine_region(cairo.Region(), 0, 0)
            if not self.anim:
                self.anim = True
                GLib.timeout_add(16, self.tick)
        elif cmd == "hide":
            self.step = None
            self.hide()
        elif cmd == "quit":
            Gtk.main_quit()

    def tick(self):
        self.queue_draw()
        if time.time() - self.t0 > INTRO:
            self.anim = False
            return False                                      # stop redrawing: the picture is now static
        return True

    # ---- drawing
    def on_draw(self, w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        s = self.step
        if not s:
            return
        t = min(1.0, (time.time() - self.t0) / INTRO)
        ease = 1 - (1 - min(1.0, t / 0.55)) ** 3                 # cursor glides in during the first half
        if s.get("arrow"):
            self.draw_arrow(cr, *s["arrow"], t)
            self.cur = s["target"]
        elif s["box"]:
            self.draw_box(cr, s["box"], t)
            fx, fy = s["from"]
            tx, ty = s["target"]
            self.draw_cursor(cr, fx + (tx - fx) * ease, fy + (ty - fy) * ease)
            self.cur = (tx, ty)
        self.draw_label(cr, s)

    def draw_box(self, cr, box, t):
        x1, y1, x2, y2 = box[0] - 6, box[1] - 6, box[2] + 6, box[3] + 6
        for width, alpha in ((12, 0.10), (8, 0.18), (4, 0.35)):  # soft glow; the inside stays see-through
            self.round_rect(cr, x1, y1, x2 - x1, y2 - y1, 8)
            cr.set_source_rgba(*ACCENT, alpha)
            cr.set_line_width(width)
            cr.stroke()
        self.round_rect(cr, x1, y1, x2 - x1, y2 - y1, 8)
        cr.set_source_rgba(*ACCENT, 1)
        cr.set_line_width(2.5)
        cr.stroke()
        if t < 1.0:                                            # one expanding ping while the cursor arrives
            grow = 40 * t
            self.round_rect(cr, x1 - grow, y1 - grow, x2 - x1 + 2 * grow, y2 - y1 + 2 * grow, 8 + grow)
            cr.set_source_rgba(*ACCENT, 0.6 * (1 - t))
            cr.set_line_width(3)
            cr.stroke()

    def draw_arrow(self, cr, x, y, from_deg, t):
        """Chunky amber arrow whose tip sits on (x, y), arriving from the from_deg direction (screen angle, y down)
        and sliding the last few pixels in during the intro."""
        slide = 30 * (1 - min(1.0, t / 0.55)) ** 2
        cr.save()
        cr.translate(x, y)
        cr.rotate(math.radians(from_deg))                       # local +x now points away from the tip
        cr.translate(slide, 0)
        length, head, shaft_h, head_h = 90, 34, 9, 30
        pts = [(0, 0), (head, -head_h), (head, -shaft_h), (length, -shaft_h),
               (length, shaft_h), (head, shaft_h), (head, head_h)]
        for dx, dy, col in ((3, 4, (0, 0, 0, 0.35)), (0, 0, None)):
            cr.move_to(pts[0][0] + dx, pts[0][1] + dy)
            for px, py in pts[1:]:
                cr.line_to(px + dx, py + dy)
            cr.close_path()
            if col:
                cr.set_source_rgba(*col)
                cr.fill()
            else:
                cr.set_source_rgba(*ACCENT, 1)
                cr.fill_preserve()
                cr.set_source_rgba(0.1, 0.1, 0.1, 1)
                cr.set_line_width(2)
                cr.stroke()
        cr.restore()

    def draw_cursor(self, cr, x, y):
        pts = [(0, 0), (0, 19), (4.5, 15), (8, 22), (11, 20.5), (7.5, 14), (13, 14)]   # arrow with its tip at (x, y)
        cr.move_to(x + 2, y + 3)
        for px, py in pts[1:]:
            cr.line_to(x + px + 2, y + py + 3)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 0.35)                      # shadow
        cr.fill()
        cr.move_to(x, y)
        for px, py in pts[1:]:
            cr.line_to(x + px, y + py)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 1)
        cr.set_line_width(1.5)
        cr.stroke()

    def draw_label(self, cr, s):
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(17)
        words, lines, cur = s["label"].split(), [], ""
        for wd in words:                                       # wrap at ~34 characters
            if len(cur) + len(wd) > 34 and cur:
                lines.append(cur)
                cur = wd
            else:
                cur = (cur + " " + wd).strip()
        lines.append(cur)
        tw = max(cr.text_extents(l).x_advance for l in lines)
        bw, bh = tw + 58, 14 + 24 * len(lines)
        if s["box"]:
            x1, y1, x2, y2 = s["box"]
            bx = min(max(10, x1), self.W - bw - 10)
            by = y2 + 16 if y2 + 16 + bh < self.H - 10 else y1 - 16 - bh
            by = max(10, by)
        else:
            bx, by = (self.W - bw) / 2, self.H - bh - 90
        self.round_rect(cr, bx, by, bw, bh, 10)
        cr.set_source_rgba(0.08, 0.08, 0.1, 0.94)
        cr.fill_preserve()
        cr.set_source_rgba(*ACCENT, 1)
        cr.set_line_width(1.5)
        cr.stroke()
        cr.arc(bx + 22, by + bh / 2, 12, 0, 2 * math.pi)
        cr.set_source_rgba(*ACCENT, 1)
        cr.fill()
        cr.set_source_rgba(0.1, 0.1, 0.1, 1)
        cr.set_font_size(15)
        num = str(s["n"])
        cr.move_to(bx + 22 - cr.text_extents(num).x_advance / 2, by + bh / 2 + 5)
        cr.show_text(num)
        cr.set_font_size(17)
        cr.set_source_rgba(1, 1, 1, 1)
        for i, l in enumerate(lines):
            cr.move_to(bx + 44, by + 27 + 24 * i)
            cr.show_text(l)

    @staticmethod
    def round_rect(cr, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

def serve(win):
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        with conn, conn.makefile("r") as f:
            try:
                msg = json.loads(f.readline())
            except ValueError:
                continue
        GLib.idle_add(lambda m=msg: win.handle(m) or False)

def watchdog(win, parent):
    if os.getppid() != parent:                                 # Hommie exited: don't leave a stray overlay
        Gtk.main_quit()
        return False
    if win.step and time.time() - win.last_cmd > IDLE_HIDE:
        win.handle({"cmd": "hide"})
    return True

if __name__ == "__main__":
    win = Overlay()
    threading.Thread(target=serve, args=(win,), daemon=True).start()
    GLib.timeout_add_seconds(2, watchdog, win, os.getppid())
    print("overlay ready", flush=True)
    Gtk.main()
    try:
        os.unlink(SOCK)
    except OSError:
        pass
