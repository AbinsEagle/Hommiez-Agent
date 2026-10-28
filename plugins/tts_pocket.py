"""Natural voice: Pocket TTS (Kyutai, local, free, MIT). Runs in its own worker process (needs torch, so it lives in a
separate Python: see README). Until the worker has loaded (about a minute after start), or if it dies, every sentence
quietly falls back to the next voice (Kokoro). Can clone any voice from a short, clean .wav: set "voice" to its path."""
import itertools, json, os, queue, re, shutil, signal, subprocess, threading, time, ctypes
from hommie.core import HERE, play_wav, pronounce, sentences, temp_wav

PRIORITY = 36
PRESETS = ["alba", "anna", "charles", "giovanni", "marius", "jean", "eve", "azelma", "cosette"]
DEFAULTS = {"voice": "alba", "python": "~/tts-trial/.venv/bin/python", "threads": 4}
UI_TITLE = "Voice: Pocket (main)"
UI = [{"key": "voice", "label": "Voice", "type": "select", "options": [[v, v.title()] for v in PRESETS]}]
WORKER = os.path.join(HERE, "tools", "pocket_worker.py")
_pocket = None

def _die_with_parent():
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)

class Pocket:
    name = "pocket"

    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.proc, self.ready, self.dead = None, threading.Event(), False
        self.replies, self.mu, self.ids = queue.Queue(), threading.Lock(), itertools.count(1)
        self.cache = {}                                            # (voice, text) -> wav bytes of stock phrases

    def start(self):
        env = dict(os.environ, POCKET_THREADS=str(self.cfg["threads"]), PYTHONWARNINGS="ignore")
        self.proc = subprocess.Popen([os.path.expanduser(self.cfg["python"]), WORKER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                                     preexec_fn=_die_with_parent)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ready"):
                self.ready.set()
                self.ctx.log("natural voice ready")
            else:
                self.replies.put(msg)
        self.dead = True                                           # worker exited: use the fallback voice from now on
        self.ready.clear()

    def _fallback(self):
        for tts in self.ctx.services("tts"):
            if tts is not self and hasattr(tts, "render"):
                return tts
        raise RuntimeError("no fallback voice with render()")

    def render(self, text):
        """Text -> path of a wav (caller plays and deletes it)."""
        text = pronounce(text)
        voice = self.cfg["voice"]
        if not self.ready.is_set() or self.dead:
            return self._fallback().render(text)
        wav = temp_wav()
        hit = self.cache.get((voice, text))
        if hit:
            open(wav, "wb").write(hit)
            return wav
        with self.mu:
            try:
                rid = next(self.ids)
                self.proc.stdin.write(json.dumps({"id": rid, "text": text, "voice": voice, "out": wav}) + "\n")
                self.proc.stdin.flush()
                while (msg := self.replies.get(timeout=90)).get("id") != rid:
                    pass
                if msg.get("ok"):
                    return wav
                self.ctx.log(f"natural voice failed ({msg.get('error')}); using the fallback")
            except (BrokenPipeError, OSError, queue.Empty):
                self.dead = True
        return self._fallback().render(text)

    def prewarm(self, phrases):
        """Render stock phrases once the worker is up, so they play with no delay."""
        if not self.ready.wait(180):
            return
        for text in phrases:
            wav = self.render(text)
            if self.ready.is_set() and (self.cfg["voice"], pronounce(text)) not in self.cache:
                self.cache[(self.cfg["voice"], pronounce(text))] = open(wav, "rb").read()
            os.remove(wav)

    def speak(self, text):
        q = queue.Queue()

        def make():
            try:
                for sent in sentences(text):
                    q.put(self.render(sent))
            except Exception as e:
                q.put(e)
            finally:
                q.put(None)

        threading.Thread(target=make, daemon=True).start()
        played = False
        while (item := q.get()) is not None:
            if isinstance(item, Exception):
                if not played:
                    raise item
                break
            play_wav(item)
            played = True

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

def setup(ctx):
    global _pocket
    cfg = ctx.cfg("tts_pocket", DEFAULTS)
    py = os.path.expanduser(cfg["python"])
    if not (os.path.exists(py) and os.path.exists(WORKER)):
        raise FileNotFoundError(f"Pocket TTS python not found at {py} (see README)")
    pocket = _pocket = Pocket(ctx, cfg)
    pocket.start()
    ctx.provide("tts", pocket, "pocket", priority=110)             # above Kokoro; Kokoro stays as the fallback

    def voice_cmd(m, text):
        who = re.search(r"\b(" + "|".join(PRESETS) + r")\b", text, re.I)
        if not who:
            return None
        cfg["voice"] = who.group(1).lower()
        return f"Okay, switching to {who.group(1).title()}."

    ctx.command(r"\b(use|switch to|sound like|change to)\b.*\b(" + "|".join(PRESETS) + r")\b", voice_cmd, "pocket_voice", priority=55)

def teardown(ctx):
    if _pocket:
        _pocket.stop()
