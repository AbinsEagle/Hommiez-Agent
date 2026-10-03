"""Web UI: an animated orb, the conversation, a typing box and mic button, a settings panel built from whatever
plugins are loaded, and a plugin on/off list. Runs on http://127.0.0.1:8765 (this machine only).

Start with  jarvis.py --ui  (opens a window and runs hands-free), or just run Hommie normally and open the URL.
Delete this file and nothing else changes: it only listens to events and calls ctx.handle / ctx.say.

Safety: bound to 127.0.0.1; the Host header must be localhost (blocks DNS rebinding); every request that does
something needs a random per-run token that only the page we serve knows (blocks other websites from driving it).
"""
import collections, json, os, queue, secrets, shutil, subprocess, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from hommie.core import speakable

PRIORITY = 90
DEFAULTS = {"port": 8765, "open_window": True}

class WebUI:
    def __init__(self, ctx):
        self.ctx, self.cfg = ctx, ctx.cfg("ui_web", DEFAULTS)
        self.token = secrets.token_urlsafe(24)
        self.clients, self.history, self.state = [], collections.deque(maxlen=100), "idle"
        self.pending, self.last_spoken = {}, ""
        self.lock, self.url, self.server = threading.Lock(), "", None
        ctx.on("heard", self._heard)
        ctx.on("reply", self._reply)
        ctx.on("say", self._say)
        ctx.on("state", self._state)
        ctx.on("log", lambda text, **kw: self.broadcast({"type": "note", "text": text}))

    # ── events from Hommie -> browser ──
    def _add(self, role, text):
        self.history.append({"role": role, "text": text})
        self.broadcast({"type": "msg", "role": role, "text": text})

    def _heard(self, text, **kw):
        self._add("you", text)

    def _reply(self, text, **kw):
        self.last_spoken = speakable(text)         # the spoken version arrives next as a "say" event; don't show it twice
        self._add("hommie", text)

    def _say(self, text, **kw):
        if text == self.last_spoken:
            self.last_spoken = ""
            return
        self._add("hommie", text)

    def _state(self, state, **kw):
        self.state = state
        self.broadcast({"type": "state", "state": state})

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

    def snapshot(self):
        return {"type": "snapshot", "history": list(self.history), "state": self.state,
                "settings": self.settings_view(), "plugins": self.plugins_view(),
                "dirs": list(self.ctx.dirs), "can_listen": self.ctx.service("listen") is not None}

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
                    return self._send(200, PAGE.replace("__TOKEN__", ui.token).encode(), "text/html; charset=utf-8")
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
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
                except ValueError:
                    return self._send(400, b"bad json")
                path, ok = urlparse(self.path).path, True
                if path == "/api/say" and str(body.get("text", "")).strip():
                    ui.request(str(body["text"]).strip()[:2000])
                elif path == "/api/listen":
                    ui.listen_once()
                elif path == "/api/confirm":
                    ui.answer(body.get("id"), body.get("ok"))
                elif path == "/api/setting":
                    ok = ui.set_setting(body.get("plugin"), body.get("key"), body.get("value"))
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
        while True:
            time.sleep(0.1)
            ears = self.ctx.service("listen")
            if ears is not None and self.clients and self.state in ("idle", "listening"):
                self.broadcast({"type": "level", "v": round(getattr(ears, "level", 0.0), 3)})

    def stop(self):
        if self.server:
            self.server.shutdown()

def _spec(mod):
    """A plugin's settings-panel description: a module-level list named UI (see tts_kokoro.py)."""
    spec = getattr(mod, "UI", None)
    return spec if isinstance(spec, list) else []

def open_window(url):
    """Open the UI in its own app-style window if a Chromium-family browser exists, else the default browser."""
    for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser", "brave"):
        path = shutil.which(exe)
        if path:
            subprocess.Popen([path, f"--app={url}", "--window-size=1100,720"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    webbrowser.open(url)

class UiMode:
    """--ui: open the window, then run hands-free if the wake mode and a microphone are available."""
    def run(self, ctx):
        open_window(ctx.ui_url)
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

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hommie</title>
<style>
:root{--bg:#0b1117;--panel:#121a23;--line:#223040;--ink:#e6edf3;--muted:#8da0b3;--accent:#38bdac;--you:#1c2b3a;--hom:#16222d;--danger:#f87171;color-scheme:dark}
@media (prefers-color-scheme:light){:root:not([data-theme=dark]){--bg:#f1f5f9;--panel:#fff;--line:#d5dee8;--ink:#14202b;--muted:#5b6b7b;--accent:#0f8f80;--you:#e2ecf5;--hom:#f4f7fa;color-scheme:light}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0;font-weight:600;letter-spacing:.02em}
#status{color:var(--muted);font-size:13px;margin-left:auto}
button,select,input{font:inherit;color:inherit}
.btn{min-height:40px;min-width:40px;padding:0 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel);cursor:pointer}
.btn:hover{border-color:var(--accent)}.btn:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
main{flex:1;min-height:0;display:grid;grid-template-columns:minmax(260px,380px) 1fr;gap:0}
@media (max-width:760px){main{grid-template-columns:1fr;grid-template-rows:auto 1fr}}
.stage{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:16px;border-right:1px solid var(--line)}
@media (max-width:760px){.stage{border-right:0;border-bottom:1px solid var(--line);padding:8px}}
canvas{width:min(300px,70vw);height:min(300px,70vw)}
#label{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
#mic{border-radius:999px;padding:0 20px}
#mic[aria-pressed=true]{background:var(--accent);color:#04201c;border-color:var(--accent)}
.chat{display:flex;flex-direction:column;min-height:0}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:min(620px,88%);padding:10px 14px;border-radius:14px;border:1px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere}
.msg.you{align-self:flex-end;background:var(--you)}.msg.hommie{align-self:flex-start;background:var(--hom)}
.note{align-self:center;color:var(--muted);font-size:12px}
.empty{margin:auto;color:var(--muted);text-align:center;max-width:32ch}
form{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line)}
#text{flex:1;min-height:44px;padding:0 14px;border-radius:12px;border:1px solid var(--line);background:var(--panel)}
#confirm{display:none;gap:10px;align-items:center;padding:10px 16px;background:#3b2a05;border-top:1px solid #7a5a12;color:#fde68a}
#confirm.on{display:flex}#confirm span{flex:1}
aside{position:fixed;top:0;right:0;bottom:0;width:min(360px,100vw);background:var(--panel);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .2s;overflow-y:auto;padding:16px;z-index:5}
aside.open{transform:none}@media (prefers-reduced-motion:reduce){aside{transition:none}}
aside h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:20px 0 8px}
aside h2:first-of-type{margin-top:8px}
.field{display:grid;gap:4px;margin:10px 0}.field label{font-size:13px;color:var(--muted);display:flex;justify-content:space-between}
.field select,.field input[type=range]{width:100%}.field select{min-height:38px;border-radius:8px;border:1px solid var(--line);background:var(--bg);padding:0 8px}
.plug{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}.plug div{flex:1;min-width:0}
.plug small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.plug input{width:20px;height:20px;accent-color:var(--accent)}
#dirs{white-space:pre-wrap}.bad{color:var(--danger)}.muted{color:var(--muted);font-size:13px}
</style></head><body>
<header><h1>HOMMIE</h1><span id="status" aria-live="polite">connecting…</span>
<button class="btn" id="gear" aria-label="Settings" aria-expanded="false">⚙ Settings</button></header>
<main>
 <section class="stage" aria-label="Hommie status"><canvas id="orb" width="600" height="600" aria-hidden="true"></canvas>
  <div id="label">idle</div><button class="btn" id="mic" aria-pressed="false">🎤 Talk</button></section>
 <section class="chat"><div id="log" role="log" aria-live="polite"><div class="empty" id="empty">Say “Hey Hommie…”, press Talk, or type below.</div></div>
  <div id="confirm" role="alertdialog" aria-label="Permission needed"><span>Hommie needs your OK to run something.</span>
   <button class="btn" id="yes">Allow</button><button class="btn" id="no">Deny</button></div>
  <form id="f"><input id="text" autocomplete="off" placeholder="Type a request…" aria-label="Type a request"><button class="btn" type="submit">Send</button></form></section>
</main>
<aside id="panel" aria-label="Settings"><button class="btn" id="close" style="float:right">Close</button>
 <div id="settings"></div><h2>Plugins</h2><div id="plugins"></div><p class="muted" id="restart" hidden>Restart Hommie to apply plugin changes.</p>
 <h2>Workspace folders</h2><div id="dirs" class="muted"></div></aside>
<script>
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
let state="idle",level=0,smooth=0,confirmId=null,online=false;
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-Hommie-Token":TOKEN},body:JSON.stringify(b||{})});
function addMsg(role,text){const e=$("#empty");if(e)e.remove();const d=document.createElement("div");d.className="msg "+role;d.textContent=text;const l=$("#log");l.appendChild(d);l.scrollTop=l.scrollHeight}
function addNote(t){const d=document.createElement("div");d.className="note";d.textContent=t;$("#log").appendChild(d)}
function setState(s){state=s;$("#label").textContent={idle:"ready",listening:"listening",thinking:"thinking",speaking:"speaking"}[s]||s;$("#status").textContent=online?$("#label").textContent:"reconnecting…";$("#mic").setAttribute("aria-pressed",s==="listening")}
function renderSettings(groups){const box=$("#settings");box.textContent="";groups.forEach(g=>{const h=document.createElement("h2");h.textContent=g.title;box.appendChild(h);
 g.fields.forEach(f=>{const w=document.createElement("div");w.className="field";const lab=document.createElement("label");const nm=document.createElement("span");nm.textContent=f.label;const val=document.createElement("span");lab.append(nm,val);w.appendChild(lab);let el;
  if(f.type==="select"){el=document.createElement("select");f.options.forEach(([v,t])=>{const o=document.createElement("option");o.value=v;o.textContent=t;if(v===f.value)o.selected=true;el.appendChild(o)});el.onchange=()=>post("/api/setting",{plugin:g.plugin,key:f.key,value:el.value})}
  else if(f.type==="toggle"){el=document.createElement("input");el.type="checkbox";el.checked=!!f.value;val.textContent="";el.onchange=()=>post("/api/setting",{plugin:g.plugin,key:f.key,value:el.checked})}
  else{el=document.createElement("input");el.type="range";el.min=f.min;el.max=f.max;el.step=f.step;el.value=f.value;val.textContent=f.value;el.oninput=()=>val.textContent=el.value;el.onchange=()=>post("/api/setting",{plugin:g.plugin,key:f.key,value:+el.value})}
  el.id="f_"+g.plugin+"_"+f.key;lab.htmlFor=el.id;w.appendChild(el);box.appendChild(w)})})}
function renderPlugins(list){const box=$("#plugins");box.textContent="";list.forEach(p=>{const r=document.createElement("label");r.className="plug";const cb=document.createElement("input");cb.type="checkbox";cb.checked=p.on;cb.disabled=p.locked||(!p.on&&!/disabled/.test(p.status));
 cb.onchange=()=>{post("/api/plugin",{name:p.name,on:cb.checked});$("#restart").hidden=false};const d=document.createElement("div");const n=document.createElement("strong");n.textContent=p.name;const s=document.createElement("small");s.textContent=p.on?p.doc:p.status;if(!p.on&&!/disabled/.test(p.status))s.className="bad";d.append(n,s);r.append(cb,d);box.appendChild(r)})}
function connect(){const es=new EventSource("/events?t="+TOKEN);
 es.onopen=()=>{online=true;setState(state)};es.onerror=()=>{online=false;setState(state)};
 es.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.type==="snapshot"){$("#log").textContent="";if(!m.history.length){$("#log").innerHTML='<div class="empty" id="empty">Say “Hey Hommie…”, press Talk, or type below.</div>'}m.history.forEach(h=>addMsg(h.role,h.text));setState(m.state);renderSettings(m.settings);renderPlugins(m.plugins);$("#dirs").textContent=m.dirs.join("\n");$("#mic").hidden=!m.can_listen}
  else if(m.type==="msg")addMsg(m.role,m.text);else if(m.type==="note")addNote(m.text);else if(m.type==="state")setState(m.state);
  else if(m.type==="level")level=m.v;else if(m.type==="settings")renderSettings(m.settings);
  else if(m.type==="confirm"){confirmId=m.id;$("#confirm").classList.add("on")}else if(m.type==="confirm_done"){confirmId=null;$("#confirm").classList.remove("on")}}}
$("#f").onsubmit=e=>{e.preventDefault();const t=$("#text").value.trim();if(t){post("/api/say",{text:t});$("#text").value=""}};
$("#mic").onclick=()=>post("/api/listen");
$("#yes").onclick=()=>confirmId&&post("/api/confirm",{id:confirmId,ok:true});$("#no").onclick=()=>confirmId&&post("/api/confirm",{id:confirmId,ok:false});
const panel=$("#panel");$("#gear").onclick=()=>{panel.classList.toggle("open");$("#gear").setAttribute("aria-expanded",panel.classList.contains("open"))};$("#close").onclick=()=>{panel.classList.remove("open");$("#gear").setAttribute("aria-expanded","false")};
document.addEventListener("keydown",e=>{if(e.key==="Escape")panel.classList.remove("open")});
// orb
const cv=$("#orb"),ctx=cv.getContext("2d"),still=matchMedia("(prefers-reduced-motion:reduce)").matches;
const COL={idle:[56,189,172],listening:[56,189,248],thinking:[167,139,250],speaking:[251,191,36]};let cur=COL.idle.slice(),t=0;
function frame(){t+=still?0.002:0.016;const W=cv.width,c=W/2,tgt=COL[state]||COL.idle;cur=cur.map((v,i)=>v+(tgt[i]-v)*0.08);
 smooth+=((state==="listening"||state==="idle"?level:0)-smooth)*0.25;
 const amp={idle:5,listening:8+smooth*70,thinking:10,speaking:16+Math.sin(t*9)*7+Math.sin(t*13.7)*5}[state]||5,base=W*0.24,sp=state==="thinking"?1.6:state==="speaking"?1.2:0.6;
 ctx.clearRect(0,0,W,W);const col=`rgb(${cur.map(Math.round)})`;
 const g=ctx.createRadialGradient(c,c,base*0.2,c,c,base*2.1);g.addColorStop(0,`rgba(${cur.map(Math.round)},.28)`);g.addColorStop(1,"rgba(0,0,0,0)");ctx.fillStyle=g;ctx.fillRect(0,0,W,W);
 ctx.beginPath();for(let i=0;i<=120;i++){const a=i/120*Math.PI*2,n=Math.sin(a*3+t*sp*2)+0.6*Math.sin(a*5-t*sp*3)+0.4*Math.sin(a*2+t*sp),r=base+(still?0:n*amp*0.55)+Math.sin(t*1.3)*(still?0:4);const x=c+Math.cos(a)*r,y=c+Math.sin(a)*r;i?ctx.lineTo(x,y):ctx.moveTo(x,y)}
 ctx.closePath();const g2=ctx.createRadialGradient(c-base*0.3,c-base*0.3,base*0.1,c,c,base*1.3);g2.addColorStop(0,"rgba(255,255,255,.85)");g2.addColorStop(.35,col);g2.addColorStop(1,`rgba(${cur.map(v=>Math.round(v*.45))},1)`);
 ctx.fillStyle=g2;ctx.shadowColor=col;ctx.shadowBlur=40;ctx.fill();ctx.shadowBlur=0;
 if(state==="thinking"){ctx.strokeStyle=col;ctx.lineWidth=3;for(let k=0;k<3;k++){ctx.beginPath();const s=t*(1.2+k*.5)*(k%2?-1:1);ctx.arc(c,c,base*(1.35+k*.18),s,s+1.4);ctx.stroke()}}
 requestAnimationFrame(frame)}
connect();frame();
</script></body></html>
"""
