"""Web UI: an animated orb, the conversation, a typing box and mic button, a settings panel built from whatever
plugins are loaded, and a plugin on/off list. Runs on http://127.0.0.1:8765 (this machine only).

Start with  jarvis.py --ui  (opens a window and runs hands-free), or just run Hommie normally and open the URL.
Delete this file and nothing else changes: it only listens to events and calls ctx.handle / ctx.say.

Safety: bound to 127.0.0.1; the Host header must be localhost (blocks DNS rebinding); every request that does
something needs a random per-run token that only the page we serve knows (blocks other websites from driving it).
"""
import collections, json, os, queue, re, secrets, shutil, subprocess, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from hommie.core import HERE, speakable

PRIORITY = 90
DEFAULTS = {"port": 8765, "open_window": True}

class WebUI:
    def __init__(self, ctx):
        self.ctx, self.cfg = ctx, ctx.cfg("ui_web", DEFAULTS)
        self.token = secrets.token_urlsafe(24)
        self.clients, self.history, self.state = [], collections.deque(maxlen=100), "idle"
        self.pending, self.last_spoken = {}, ""
        self.activity = collections.deque(maxlen=200)                  # what the right-hand "live activity" panel shows
        self.stats = {"replies": 0, "last": None, "total": 0.0, "t0": None, "since": time.time()}
        self.lock, self.url, self.server = threading.Lock(), "", None
        self.watch_window, self.seen_client, self.gone_since = False, False, None   # --ui: quit when the window is closed
        ctx.on("heard", self._heard)
        ctx.on("reply", self._reply)
        ctx.on("say", self._say)
        ctx.on("state", self._state)
        ctx.on("mic", self._mic)
        ctx.on("log", self._log)

    # ── events from Hommie -> browser ──
    def _add(self, role, text):
        self.history.append({"role": role, "text": text})
        self.broadcast({"type": "msg", "role": role, "text": text})

    def _heard(self, text, **kw):
        self.stats["t0"] = time.time()
        self._act("info", "You", text)
        self._add("you", text)

    def _reply(self, text, source="brain", **kw):
        t0, self.stats["t0"] = self.stats["t0"], None
        if t0:
            self.stats["last"] = time.time() - t0
            self.stats["total"] += self.stats["last"]
            self.stats["replies"] += 1
        self._act("success", source if source != "brain" else getattr(self.ctx.service("brain"), "name", "brain"), text)
        node = node_for(source)
        if node:
            self.broadcast({"type": "pulse", "node": node})
        self.last_spoken = speakable(text)         # the spoken version arrives next as a "say" event; don't show it twice
        self._add("hommie", text)
        self.broadcast({"type": "stats", "stats": self.stats_view()})

    def _log(self, text, **kw):
        level = "error" if re.search(r"fail|error|crash|couldn'?t|did not|can'?t", text, re.I) else \
                "warning" if re.search(r"block|protect|denied|not allowed|interrupt|skipp", text, re.I) else "info"
        self._act(level, "system", text)
        self.broadcast({"type": "note", "text": text})

    def _act(self, level, source, text):
        e = {"level": level, "source": str(source), "text": " ".join(str(text).split())[:400], "t": time.strftime("%H:%M:%S")}
        self.activity.append(e)
        self.broadcast({"type": "activity", **e})

    def _say(self, text, **kw):
        if text == self.last_spoken:
            self.last_spoken = ""
            return
        self._add("hommie", text)

    def _state(self, state, **kw):
        self.state = state
        self.broadcast({"type": "state", "state": state})

    def _mic(self, on, reason="", **kw):
        self.broadcast({"type": "mic", "on": on, "reason": reason})

    def broadcast(self, msg):
        data = json.dumps(msg)
        with self.lock:
            for q in self.clients:
                q.put(data)

    # ── what the page shows ──
    def settings_view(self):
        out = []
        for name, (mod, status) in self.ctx.plugins.items():
            spec = _spec(mod)
            if status != "ok" or not spec:
                continue
            cfg = self.ctx.cfg(name, getattr(mod, "DEFAULTS", {}))
            out.append({"plugin": name, "title": getattr(mod, "UI_TITLE", name),
                        "fields": [{**f, "value": cfg[f["key"]]} for f in spec]})
        return out

    def plugins_view(self):
        enabled = self.ctx.cfg("plugins")
        return [{"name": n, "status": s, "on": s == "ok", "locked": n == "ui_web",
                 "doc": ((getattr(m, "__doc__", "") or "").strip().splitlines() or [""])[0]}
                for n, (m, s) in self.ctx.plugins.items() if m is not None]

    def quick_view(self):
        """One-click buttons in the header: a plugin lists them in QUICK = [{"id", "label", "action": fn(ctx), "status": fn(ctx) -> text}]."""
        out = []
        for name, (mod, status) in self.ctx.plugins.items():
            if status != "ok":
                continue
            for q in getattr(mod, "QUICK", None) or []:
                try:
                    text = q["status"](self.ctx) if q.get("status") else ""
                except Exception:
                    text = ""
                out.append({"plugin": name, "id": q["id"], "label": q["label"], "status": text})
        return out

    def adds_view(self):
        """Input-and-button forms for the dashboard: a plugin lists one in ADD = {"title", "label", "placeholder", "action": fn(ctx, text) -> {"note", "ask"}}."""
        out = []
        for name, (mod, status) in self.ctx.plugins.items():
            a = getattr(mod, "ADD", None) if status == "ok" else None
            if isinstance(a, dict):
                try:
                    items = a["items"](self.ctx) if a.get("items") else []
                except Exception:
                    items = []
                out.append({"plugin": name, "title": a.get("title", name), "label": a.get("label", "Add"),
                            "placeholder": a.get("placeholder", ""), "items": items})
        return out

    def run_add(self, plugin, text):
        mod = self.ctx.plugins.get(plugin, (None, ""))[0]
        a = getattr(mod, "ADD", None)
        text = str(text or "").strip()[:200]
        if not isinstance(a, dict) or not text:
            return False
        def work():
            try:
                res = a["action"](self.ctx, text) or {}
            except Exception as e:
                res = {"note": f"{a.get('label', 'Add')} failed: {e}"}
            if res.get("note"):
                self.broadcast({"type": "note", "text": res["note"]})
            self.broadcast({"type": "settings", "settings": self.settings_view()})
            self.broadcast({"type": "adds", "adds": self.adds_view()})
            if res.get("ask"):
                self.request(res["ask"])
        threading.Thread(target=work, daemon=True).start()
        return True

    def get_detail(self, plugin, ident):
        a = getattr(self.ctx.plugins.get(plugin, (None, ""))[0], "ADD", None)
        if not isinstance(a, dict) or not a.get("detail"):
            return {}
        try:
            return a["detail"](self.ctx, str(ident or "")[:80]) or {}
        except Exception:
            return {}

    def run_pick(self, plugin, ident, key="pick"):
        mod = self.ctx.plugins.get(plugin, (None, ""))[0]
        a = getattr(mod, "ADD", None)
        if not isinstance(a, dict) or not a.get(key) or not ident:
            return False
        def work():
            try:
                res = a[key](self.ctx, str(ident)[:80]) or {}
            except Exception as e:
                res = {"note": f"That failed: {e}"}
            if res.get("note"):
                self.broadcast({"type": "note", "text": res["note"]})
            self.broadcast({"type": "settings", "settings": self.settings_view()})
            self.broadcast({"type": "adds", "adds": self.adds_view()})
        threading.Thread(target=work, daemon=True).start()
        return True

    def add_images(self, urls, text):
        """Pictures pasted/dropped in the window: save them (images plugin) and ask Hommie about them."""
        mod = self.ctx.plugins.get("images", (None, ""))[0]
        if mod is None or self.ctx.plugins["images"][1] != "ok" or not isinstance(urls, list) or not urls:
            return "images are not available"
        try:
            paths = mod.save(self.ctx, urls)
        except ValueError as e:
            return str(e)
        names = ", ".join(os.path.basename(p) for p in paths)
        self.request(f"{(text or '').strip()[:2000] or 'What do you see in this image?'}\n[Attached: {', '.join(paths)}]")
        return None

    def run_quick(self, plugin, qid):
        mod = self.ctx.plugins.get(plugin, (None, ""))[0]
        q = next((q for q in getattr(mod, "QUICK", None) or [] if q["id"] == qid), None)
        if q is None:
            return False
        def work():
            try:
                note = q["action"](self.ctx)
                if note:
                    self.broadcast({"type": "note", "text": str(note)})
            except Exception as e:
                self.broadcast({"type": "note", "text": f"{q['label']} failed: {e}"})
            self.broadcast({"type": "quick", "quick": self.quick_view()})
        threading.Thread(target=work, daemon=True).start()
        return True

    def nodes_view(self):
        """What orbits the orb on the page: the loaded parts of Hommie that can light up while they work."""
        ok = lambda n: self.ctx.plugins.get(n, (None, ""))[1] == "ok"
        name = lambda kind: getattr(self.ctx.service(kind), "name", "")
        out = []
        for nid, label, kind in (("ears", "Ears", "listen"), ("brain", "Brain", "brain"), ("voice", "Voice", "tts")):
            if self.ctx.service(kind):
                out.append({"id": nid, "label": label, "sub": name(kind)})
        for nid, label, plugins in (("guide", "Screen guide", ["guide"]), ("freecad", "FreeCAD", ["proj_freecad"]),
                                    ("remote", "Remote", ["remote_access"]), ("notes", "Notes", ["obsidian", "brain_wiki"])):
            if any(ok(p) for p in plugins):
                out.append({"id": nid, "label": label, "sub": ""})
        return out

    def stats_view(self):
        st = self.stats
        return {"brain": getattr(self.ctx.service("brain"), "name", ""), "voice": getattr(self.ctx.service("tts"), "name", ""),
                "ears": getattr(self.ctx.service("listen"), "name", ""), "replies": st["replies"], "last": st["last"],
                "avg": st["total"] / st["replies"] if st["replies"] else None, "up": int(time.time() - st["since"])}

    def snapshot(self):
        return {"type": "snapshot", "history": list(self.history), "state": self.state, "quick": self.quick_view(),
                "activity": list(self.activity), "stats": self.stats_view(), "nodes": self.nodes_view(),
                "settings": self.settings_view(), "plugins": self.plugins_view(),
                "dirs": list(self.ctx.dirs), "can_listen": self.ctx.service("listen") is not None,
                "adds": self.adds_view(), "can_mic": hasattr(self.ctx, "set_mic"), "mic_on": getattr(self.ctx, "mic_on", True),
                "can_images": self.ctx.plugins.get("images", (None, ""))[1] == "ok"}

    # ── actions from the browser ──
    def request(self, text):
        def work():
            with self.ctx.lock:
                self.ctx._tl.confirm = self.confirm
                try:
                    self.ctx.say(self.ctx.handle(text, echo=False))
                finally:
                    self.ctx._tl.confirm = None
        threading.Thread(target=work, daemon=True).start()

    def listen_once(self):
        ears = self.ctx.service("listen")
        if not ears:
            return
        if getattr(self.ctx, "wake_active", False):   # hands-free mode already owns the mic: just skip its wake word
            if not getattr(self.ctx, "mic_on", True):
                self.ctx.set_mic(True)
            self.ctx.talk_now = True
            self._state("listening")
            return
        def work():
            with self.ctx.lock:
                text = ears.hear(max_wait=8)
            if text:
                self.request(text)
            else:
                self._state("idle")
        threading.Thread(target=work, daemon=True).start()

    def confirm(self):
        cid, ev = uuid.uuid4().hex, threading.Event()
        self.pending[cid] = [ev, False]
        self.broadcast({"type": "confirm", "id": cid})
        ev.wait(60)
        ok = self.pending.pop(cid, [None, False])[1]
        self.broadcast({"type": "confirm_done", "id": cid})
        return ok

    def answer(self, cid, ok):
        if cid in self.pending:
            self.pending[cid][1] = bool(ok)
            self.pending[cid][0].set()

    def set_setting(self, plugin, key, value):
        mod = self.ctx.plugins.get(plugin, (None, ""))[0]
        field = next((f for f in _spec(mod) if f["key"] == key), None)
        if field is None:
            return False
        if field["type"] == "select":
            if value not in [o[0] for o in field["options"]]:
                return False
        elif field["type"] == "toggle":
            value = bool(value)
        elif field["type"] == "text":
            value = str(value)[:300]
        elif field["type"] in ("range", "number"):
            value = min(field["max"], max(field["min"], float(value)))
            if field.get("step", 0) >= 1:
                value = int(round(value))
        else:
            return False
        self.ctx.cfg(plugin, getattr(mod, "DEFAULTS", {}))[key] = value
        self.broadcast({"type": "settings", "settings": self.settings_view()})
        return True

    def set_plugin(self, name, on):
        if name not in self.ctx.plugins or name == "ui_web":
            return False
        self.ctx.cfg("plugins")[name] = bool(on)
        return True

    # ── server ──
    def start(self, ctx=None):
        port = int(self.cfg["port"])
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _host_ok(self):
                return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

            def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                if not self._host_ok():
                    return self._send(403, b"forbidden")
                if u.path == "/":
                    return self._send(200, page().replace("__TOKEN__", ui.token).encode(), "text/html; charset=utf-8")
                if u.path == "/favicon.ico":
                    return self._send(204)
                if u.path == "/events" and parse_qs(u.query).get("t", [""])[0] == ui.token:
                    return self._stream()
                self._send(404, b"not found")

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = queue.Queue()
                with ui.lock:
                    ui.clients.append(q)
                try:
                    self.wfile.write(f"data: {json.dumps(ui.snapshot())}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with ui.lock:
                        if q in ui.clients:
                            ui.clients.remove(q)

            def do_POST(self):
                if not self._host_ok() or self.headers.get("X-Hommie-Token") != ui.token:
                    return self._send(403, b"forbidden")
                path, ok = urlparse(self.path).path, True
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    limit = 70_000_000 if path == "/api/image" else 20000
                    if n > limit:
                        return self._send(413, b"too big")
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, b"bad json")
                if path == "/api/detail":
                    return self._send(200, json.dumps(ui.get_detail(body.get("plugin"), body.get("id"))).encode(), "application/json")
                if path == "/api/say" and str(body.get("text", "")).strip():
                    ui.request(str(body["text"]).strip()[:2000])
                elif path == "/api/listen":
                    ui.listen_once()
                elif path == "/api/mic" and hasattr(ui.ctx, "set_mic"):
                    ui.ctx.set_mic(bool(body.get("on")))
                elif path == "/api/confirm":
                    ui.answer(body.get("id"), body.get("ok"))
                elif path == "/api/setting":
                    ok = ui.set_setting(body.get("plugin"), body.get("key"), body.get("value"))
                elif path == "/api/image":
                    err = ui.add_images(body.get("images"), body.get("text"))
                    if err:
                        return self._send(400, json.dumps({"ok": False, "error": err}).encode(), "application/json")
                elif path == "/api/archive":
                    ok = ui.run_pick(body.get("plugin"), body.get("id"), "archive")
                elif path == "/api/pick":
                    ok = ui.run_pick(body.get("plugin"), body.get("id"))
                elif path == "/api/add":
                    ok = ui.run_add(body.get("plugin"), body.get("text"))
                elif path == "/api/quick":
                    ok = ui.run_quick(body.get("plugin"), body.get("id"))
                elif path == "/api/plugin":
                    ok = ui.set_plugin(body.get("name"), body.get("on"))
                else:
                    ok = False
                self._send(200 if ok else 400, b'{"ok":true}' if ok else b'{"ok":false}', "application/json")

        for p in range(port, port + 10):
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
                break
            except OSError:
                continue
        else:
            raise OSError("no free port for the web UI")
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.ctx.ui_url = self.url
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._levels, daemon=True).start()
        self.ctx.log(f"web UI at {self.url}")

    def _levels(self):
        tick = 0
        while True:
            time.sleep(0.1)
            tick += 1
            if tick % 20 == 0 and self.clients:                         # every 2 s: uptime, and remote/plugin status chips
                self.broadcast({"type": "stats", "stats": self.stats_view()})
                self.broadcast({"type": "quick", "quick": self.quick_view()})
            if self.watch_window:
                self._window_closed()
            ears = self.ctx.service("listen")
            if ears is not None and self.clients and self.state in ("idle", "listening"):
                self.broadcast({"type": "level", "v": round(getattr(ears, "level", 0.0), 3)})

    def _window_closed(self):
        """Started from the icon (no terminal to close): once the window has been open and then stays gone for a
        while (a page reload reconnects within seconds), Hommie quits instead of running on invisibly."""
        if self.clients:
            self.seen_client, self.gone_since = True, None
        elif self.seen_client:
            self.gone_since = self.gone_since or time.time()
            if time.time() - self.gone_since > 5:
                self.watch_window = False
                self.ctx.log("window closed, quitting")
                self.ctx.emit("closing")                           # things on the desktop (the character) go away at once
                self.ctx.quit_requested = True
                threading.Timer(8.0, self._force_exit).start()     # still here after 8 s (stuck in a long reply)? end her

    def _force_exit(self):
        try:
            self.ctx.shutdown()
        finally:
            os._exit(0)

    def stop(self):
        if self.server:
            self.server.shutdown()

def node_for(source):
    """Which node on the page lights up for a reply that came from this command or brain."""
    s = source or ""
    if s.startswith("guide"):
        return "guide"
    if s.startswith("remote"):
        return "remote"
    if "obsidian" in s or "wiki" in s:
        return "notes"
    return "brain" if s == "brain" else None

def _spec(mod):
    """A plugin's settings-panel description: a module-level list named UI (see tts_kokoro.py)."""
    spec = getattr(mod, "UI", None)
    return spec if isinstance(spec, list) else []

def open_window(url):
    """Open the UI in its own app-style window if a Chromium-family browser exists, else the default browser."""
    for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser", "brave"):
        path = shutil.which(exe)
        if path:
            subprocess.Popen([path, f"--app={url}", "--window-size=1440,880"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    webbrowser.open(url)

class UiMode:
    """--ui: open the window, then run hands-free if the wake mode and a microphone are available."""
    def run(self, ctx):
        open_window(ctx.ui_url)
        ui = ctx.service("background", "ui_web")
        if ui:
            ui.watch_window = True
        wake, ears = ctx.service("mode", "wake"), ctx.service("listen")
        if wake and ears:
            wake.run(ctx)
        else:
            print(f"Hommie UI running at {ctx.ui_url}  (Ctrl-C to quit)")
            ctx.say("Hommie online.")
            while not ctx.quit_requested:
                time.sleep(0.5)

def setup(ctx):
    ui = WebUI(ctx)
    ctx.provide("background", ui, "ui_web")
    ctx.provide("mode", UiMode(), "ui")

PAGE_PATH = os.path.join(HERE, "hommie", "ui.html")

def page():
    with open(PAGE_PATH, encoding="utf-8") as f:           # read per request, so editing the page needs no restart
        return f.read()
