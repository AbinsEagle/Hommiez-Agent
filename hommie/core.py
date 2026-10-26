"""Hommie core: a tiny plugin host. Every feature lives in ../plugins as one file.

A plugin is a .py file in plugins/ with (all optional except setup):

    NAME = "my_feature"            # defaults to the file name
    PRIORITY = 50                  # load order, lower loads first (default 50)
    REQUIRES = ["some_pip_module"] # skipped with a note if any of these can't be imported
    def setup(ctx): ...            # wire yourself in using the ctx API below
    def teardown(ctx): ...         # optional cleanup on exit

Add a feature = drop a file in plugins/. Remove it = delete the file (or rename it with a leading "_",
or set  "plugins": {"my_feature": false}  in settings.json). Nothing else needs to change.

The ctx API (see Context below):
    ctx.cfg(name, defaults)            persistent settings section, saved to settings.json on every change
    ctx.provide(kind, obj, name, prio) offer a swappable service: "tts", "listen", "brain", "mode", "background"
    ctx.service(kind) / services(kind) best provider / all providers, best first
    ctx.command(regex, fn, name, prio) a local voice/typed command; fn(match, text) -> reply or None
    ctx.ask_confirm() / ctx.lock        yes/no question; one-request-at-a-time lock shared by all front ends
    ctx.on(event, fn) / ctx.emit(...)  events: start, heard, say, reply, state, speech.end, log
                                       (handlers take keyword args and should accept **kw for forward compatibility)
    ctx.persona(text)                  add a line to Claude's instructions
    ctx.add_context(fn)                fn(text) -> relevant background to show Claude with each request, or None
    ctx.allow(rule) / ctx.deny(rule)   let Claude use a tool without asking / never let it
    ctx.add_dir(p)                     let Claude work in a folder
    ctx.say(text)                      speak (and show) text through whatever "tts" providers are loaded
"""
import importlib.util, json, os, queue, re, subprocess, sys, tempfile, threading, time, traceback
from collections.abc import MutableMapping

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(HERE, "plugins")
SETTINGS_PATH = os.path.join(HERE, "settings.json")

# ── Text helpers shared by plugins ─────────────────────────────────────────────

def clean(text):
    text = re.sub(r"```.*?```", " code omitted. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#>|]+", "", text)
    text = re.sub(r"https?://\S+", "link", text)
    return re.sub(r"\s+", " ", text).strip()

PRONUNCIATIONS = {}          # written word -> how to spell it for the voice, e.g. {"Abins": "Ebins"} (see plugins/pronunciations.py)

def pronounce(text):
    """Respell words the voice says wrong. Used only right before synthesis, so screens and logs keep the real spelling."""
    for word, spoken in PRONUNCIATIONS.items():
        text = re.sub(r"\b%s\b" % re.escape(word), spoken, text, flags=re.I)
    return text

def speakable(text):
    """Shape text so a voice phrases it like speech: shorter breaths, no symbols read out."""
    t = clean(text)
    t = t.replace("—", ", ").replace("–", ", ").replace(" - ", ", ").replace(";", ",")
    t = re.sub(r"\(([^)]*)\)", r", \1, ", t)
    t = re.sub(r"\s*,\s*,+", ", ", t)
    t = re.sub(r"(\d)\s*V\b", r"\1 volts", t)
    t = re.sub(r"(\d)\s*mA\b", r"\1 milliamps", t)
    t = re.sub(r"(\d)\s*ms\b", r"\1 milliseconds", t)
    t = t.replace("GPIO", "G P I O").replace("ESP32", "E S P thirty two").replace("WiFi", "Wi-Fi")
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()

def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text)
    out, buf = [], ""
    for p in parts:                       # merge tiny fragments so we don't chop the rhythm
        buf = (buf + " " + p).strip()
        if len(buf) >= 25:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out

class SpeechStream:
    """Speaks a reply while it is still being written: sentences are rendered and played in a pipeline, so the
    first one starts as soon as it is complete. Needs a voice plugin with render(text) -> wav path."""
    def __init__(self, ctx, tts):
        self.ctx, self.tts = ctx, tts
        PLAY["stop"] = False
        self.buf, self.said, self.queued, self.played, self.began, self.done = "", [], 0, 0, False, False
        self.todo, self.ready = queue.Queue(), queue.Queue()
        self.threads = [threading.Thread(target=self._render, daemon=True), threading.Thread(target=self._play, daemon=True)]
        for t in self.threads:
            t.start()

    def push(self, delta):
        self.began = True
        self.buf += delta
        self._pop()

    def _pop(self, final=False):
        while True:
            want = 12 if self.queued == 0 else 25            # start talking early, then keep a natural rhythm
            m = next((m for m in re.finditer(r"[.!?]+[\"')\]]*\s+", self.buf) if m.end() >= want), None)
            if not m:
                break
            self._queue(self.buf[:m.end()])
            self.buf = self.buf[m.end():]
        if final and self.buf.strip():
            self._queue(self.buf)
            self.buf = ""

    def _queue(self, sentence):
        text = speakable(sentence)
        if not text:
            return
        if self.queued == 0:
            self.ctx.emit("state", state="speaking")
        self.queued += 1
        self.said.append(text)
        self.todo.put(text)

    def _render(self):
        while (text := self.todo.get()) is not None:
            if PLAY["stop"]:
                continue
            try:
                self.ready.put(self.tts.render(text))
            except Exception as e:
                self.ctx.log(f"voice '{self.tts.name}' failed while streaming ({e})")
        self.ready.put(None)

    def _play(self):
        while (wav := self.ready.get()) is not None:
            play_wav(wav)
            self.played += 1

    def finish(self):
        """Flush the last words and wait until everything has been spoken. Safe to call twice."""
        if not self.done:
            self.done = True
            self._pop(final=True)
            self.todo.put(None)
            for t in self.threads:
                t.join()
        return self.played > 0

def is_yes(text):
    t = text.lower()
    if re.search(r"\b(no|nope|don'?t|stop|cancel|negative|nah)\b", t):
        return False
    return bool(re.search(r"\b(yes|yeah|yep|yup|sure|confirm(ed)?|go ahead|do it|approved?|okay|ok)\b", t))

AUDIO = {"source": None, "sink": None}      # an echo-cancelling mic/speaker pair, when the audio_aec plugin made one
PLAY = {"proc": None, "stop": False}         # the sound being played, so it can be cut off when you talk over her

def play_wav(path):
    try:
        if PLAY["stop"]:
            return
        cmd = ["pw-play"] + (["--target", AUDIO["sink"]] if AUDIO["sink"] else []) + [path]
        PLAY["proc"] = proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
        if proc.wait() != 0 and AUDIO["sink"] and not PLAY["stop"]:
            AUDIO["sink"] = None                                   # the echo canceller went away: play normally
            subprocess.run(["pw-play", path], check=False)
    finally:
        PLAY["proc"] = None
        if os.path.exists(path):
            os.remove(path)

def interrupt_playback():
    PLAY["stop"] = True
    proc = PLAY["proc"]
    if proc and proc.poll() is None:
        proc.terminate()

def temp_wav():
    return tempfile.mktemp(suffix=".wav")

# ── Settings ───────────────────────────────────────────────────────────────────

class Section(MutableMapping):
    """One plugin's settings. Reads fall back to the plugin's defaults; writes are saved immediately."""
    def __init__(self, store, name, defaults):
        self._store, self._name, self._defaults = store, name, dict(defaults)

    def _over(self):                       # reading never creates anything in settings.json
        return self._store.data.get(self._name, {})

    def __getitem__(self, k):
        o = self._over()
        return o[k] if k in o else self._defaults[k]

    def __setitem__(self, k, v):
        self._store.data.setdefault(self._name, {})[k] = v
        self._store.save()

    def __delitem__(self, k):
        self._over().pop(k, None)
        self._store.save()

    def __iter__(self):
        return iter({**self._defaults, **self._over()})

    def __len__(self):
        return len({**self._defaults, **self._over()})

class Settings:
    def __init__(self, path):
        self.path, self.data = path, {}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def section(self, name, defaults=None):
        return Section(self, name, defaults or {})

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

# ── Context ────────────────────────────────────────────────────────────────────

class Context:
    def __init__(self, settings_path=SETTINGS_PATH, plugin_dir=PLUGIN_DIR):
        self.settings = Settings(settings_path)
        self.plugin_dir = plugin_dir
        self._services, self._commands, self._handlers = {}, [], {}
        self.persona_parts, self.allowed, self.denied, self.dirs = [], [], [], []
        self._context = []                   # functions text -> extra context for the brain (or None)
        self.plugins = {}                    # name -> (module, status)
        self.confirm = lambda: False         # a "mode" plugin replaces this with a voice/keyboard yes-no
        self.quit_requested = self.sleep_requested = self.barged = False
        self.lock = threading.RLock()        # one request/utterance at a time, whichever front end it came from
        self._tl = threading.local()         # per-thread overrides (e.g. a UI request confirming with buttons)

    # settings
    def cfg(self, name, defaults=None):
        return self.settings.section(name, defaults)

    # services
    def provide(self, kind, obj, name, priority=0):
        obj.name = getattr(obj, "name", name)
        self._services.setdefault(kind, []).append((priority, name, obj))
        self._services[kind].sort(key=lambda x: -x[0])

    def services(self, kind):
        return [o for _, _, o in self._services.get(kind, [])]

    def service(self, kind, name=None):
        for _, n, o in self._services.get(kind, []):
            if name is None or n == name:
                return o
        return None

    # commands
    def command(self, pattern, fn, name, priority=0):
        rx = re.compile(pattern, re.I) if isinstance(pattern, str) else pattern
        self._commands.append((priority, name, rx, fn))

    # events
    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def emit(self, event, **data):
        for fn in list(self._handlers.get(event, [])):
            try:
                fn(**data)
            except Exception:
                print(f"[plugin error in '{event}' handler]\n{traceback.format_exc()}", file=sys.stderr)

    def log(self, msg):
        self.emit("log", text=msg)

    # claude instructions / permissions
    def persona(self, text):
        self.persona_parts.append(text)

    def allow(self, rule):
        if rule not in self.allowed:
            self.allowed.append(rule)

    def deny(self, rule):
        """Block a Claude tool pattern for good (deny beats allow), e.g. ctx.deny("Edit(//path/**)")."""
        if rule not in self.denied:
            self.denied.append(rule)

    def add_context(self, fn, name="context"):
        """Register fn(text) -> str | None. Whatever it returns is shown to the brain alongside the user's request
        (e.g. matching notes from a knowledge base). Return None when you have nothing relevant."""
        self._context.append((name, fn))

    def add_dir(self, path):
        if path not in self.dirs:
            self.dirs.append(path)

    def barge_in(self):
        """You started talking over her: stop speaking now and keep what you said for the next request."""
        if not self.barged:
            self.barged = True
            interrupt_playback()
            self.log("(interrupted)")

    # speaking and the request pipeline
    def ask_confirm(self):
        """Ask the user a yes/no. A front end can override this for its own requests (thread-local)."""
        fn = getattr(self._tl, "confirm", None) or self.confirm
        return fn()

    def say(self, text):
        with self.lock:
            self._say(text)

    def _say(self, text):
        text = speakable(text)
        if not text:
            return
        PLAY["stop"] = False
        self.emit("state", state="speaking")
        self.emit("say", text=text)
        for tts in self.services("tts"):
            try:
                tts.speak(text)
                break
            except Exception as e:
                self.log(f"voice '{tts.name}' failed ({e}); trying the next one")
        self.emit("speech.end")
        self.barged = False
        self.emit("state", state="idle")

    def handle(self, text, echo=True):
        with self.lock:
            return self._handle(text, echo)

    def _handle(self, text, echo):
        """Run one request: local commands first, then the brain. Returns the reply text (not yet spoken)."""
        self.emit("heard", text=text, echo=echo)
        for _, name, rx, fn in sorted(self._commands, key=lambda c: -c[0]):
            m = rx.search(text)
            if not m:
                continue
            try:
                reply = fn(m, text)
            except Exception:
                print(f"[command '{name}' failed]\n{traceback.format_exc()}", file=sys.stderr)
                reply = None
            if reply is not None:
                self.emit("reply", text=reply, source=name)
                return reply
        brain = self.service("brain")
        if brain is None:
            return "I don't have a brain plugin loaded, so I can only run built-in commands."
        self.emit("state", state="thinking")
        extra = []
        for name, fn in self._context:
            try:
                got = fn(text)
            except Exception:
                print(f"[context '{name}' failed]\n{traceback.format_exc()}", file=sys.stderr)
                got = None
            if got:
                extra.append(got)
        prompt = "\n\n".join(extra + [text]) if extra else text
        tts = self.service("tts")
        stream = SpeechStream(self, tts) if getattr(brain, "streams", False) and hasattr(tts, "render") else None
        reply = brain.ask(prompt, stream=stream) if stream else brain.ask(prompt)
        spoken = bool(stream) and stream.finish() and getattr(brain, "last_spoken", False)
        barged = self.barged                                   # you talked over her: don't read the rest out
        self.emit("reply", text=reply, source="brain")
        if stream and stream.played:
            if spoken:
                self.emit("say", text=" ".join(stream.said))
            self.emit("speech.end")
            self.barged = False
            self.emit("state", state="idle")
        return "" if (spoken or barged) else reply

    # plugin loading
    def load_plugins(self):
        enabled = self.cfg("plugins")
        files = sorted(f for f in os.listdir(self.plugin_dir) if f.endswith(".py") and not f.startswith("_"))
        mods = []
        for f in files:
            path = os.path.join(self.plugin_dir, f)
            fallback = f[:-3]
            try:
                spec = importlib.util.spec_from_file_location("hommie_plugin_" + fallback, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as e:
                self.plugins[fallback] = (None, f"failed to import: {e}")
                continue
            mods.append((getattr(mod, "PRIORITY", 50), getattr(mod, "NAME", fallback), mod))
        for _, name, mod in sorted(mods, key=lambda m: (m[0], m[1])):
            if name in enabled and enabled[name] is False:
                self.plugins[name] = (mod, "disabled in settings")
                continue
            missing = [r for r in getattr(mod, "REQUIRES", []) if importlib.util.find_spec(r) is None]
            if missing:
                self.plugins[name] = (mod, "skipped, missing: " + ", ".join(missing))
                continue
            try:
                mod.setup(self)
                self.plugins[name] = (mod, "ok")
            except Exception as e:
                self.plugins[name] = (mod, f"failed in setup: {e}")

    def report(self):
        lines = []
        for name, (_, status) in self.plugins.items():
            lines.append(f"  {'✓' if status == 'ok' else '✗'} {name}" + ("" if status == "ok" else f"  ({status})"))
        return "\n".join(lines)

    def shutdown(self):
        for bg in self.services("background"):
            try:
                bg.stop()
            except Exception:
                pass
        for name, (mod, status) in self.plugins.items():
            if status == "ok" and hasattr(mod, "teardown"):
                try:
                    mod.teardown(self)
                except Exception:
                    pass

class Tee:
    """Mirror everything printed to ~/jarvis/logs/hommie.log (timestamped), so problems can be read afterwards."""
    def __init__(self, stream, file):
        self.stream, self.file, self.line = stream, file, ""

    def write(self, text):
        self.stream.write(text)
        self.line += text
        while "\n" in self.line:
            line, self.line = self.line.split("\n", 1)
            if line.strip():
                self.file.write(time.strftime("%H:%M:%S ") + line + "\n")
        self.file.flush()
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)

def start_log():
    path = os.path.join(HERE, "logs", "hommie.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
        os.replace(path, path + ".old")
    f = open(path, "a", buffering=1)
    f.write(time.strftime("--- started %Y-%m-%d %H:%M:%S ---\n"))
    sys.stdout, sys.stderr = Tee(sys.stdout, f), Tee(sys.stderr, f)

def single_instance():
    """Only one Hommie may listen at a time: two would both answer and hear each other. Returns the lock to keep open."""
    import fcntl
    lock = open(os.path.join(HERE, ".hommie.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return lock

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--plugins" not in argv:
        lock = single_instance()
        if lock is None:
            print("Hommie is already running (another window or the desktop icon). Close that one first.")
            return
        start_log()
    ctx = Context()
    ctx.load_plugins()
    if "--plugins" in argv:
        print("Hommie plugins:\n" + ctx.report())
        return
    flagged = [a[2:] for a in argv if a.startswith("--") and ctx.service("mode", a[2:])]
    mode_name = flagged[0] if flagged else ctx.cfg("core", {"mode": "wake"})["mode"]
    mode = ctx.service("mode", mode_name)
    if mode is None:
        print(f"No '{mode_name}' mode plugin is loaded. Loaded plugins:\n" + ctx.report())
        return
    try:
        for bg in ctx.services("background"):
            bg.start(ctx)
        ctx.emit("start")
        mode.run(ctx)
    except KeyboardInterrupt:
        print()
    finally:
        ctx.shutdown()
