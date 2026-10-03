"""Ears: one always-open microphone stream + loudness-based phrase detection + local Whisper.
Provides the "listen" service used by the voice modes."""
import os, queue, re, signal, subprocess, tempfile, threading, time
import numpy as np
from hommie.core import AUDIO

PRIORITY = 35
REQUIRES = ["faster_whisper", "numpy"]
DEFAULTS = {"model": "base.en", "end_silence": 0.85, "threads": 4,
            "barge_in": True, "barge_gate": 400}   # talk over her to stop her (only with the echo canceller running)
UI_TITLE = "Listening"
UI = [{"key": "end_silence", "label": "Pause that ends a sentence (s)", "type": "range", "min": 0.3, "max": 1.2, "step": 0.05}]
RATE, FRAME = 16000, 480            # 30 ms of 16 kHz mono s16
FRAME_BYTES = FRAME * 2

class BargeDetector:
    """Decides when a loud sound while she is talking is YOU, not her own voice leaking back into the mic.
    It learns how much of her voice leaks (the quiet-ish level while she speaks) and only reacts to something
    clearly louder than that, for a sustained moment."""
    WARMUP, NEED = 20, 12                  # ~0.6 s of listening first; then ~0.36 s of sustained loudness

    def __init__(self, min_gate, floor):
        self.min_gate, self.floor = min_gate, floor
        self.reset()

    def reset(self):
        self.leak, self.hits, self.gate, self.seen = [], 0, self.min_gate, 0

    def feed(self, rms):
        """Returns True the moment you are judged to be talking over her."""
        self.seen += 1
        if self.seen <= self.WARMUP:
            self.leak.append(rms)
            return False
        loud = rms > self.gate
        if not loud:                       # only quiet frames describe her leak; your voice must not raise the bar
            self.leak.append(rms)
        self.gate = max(self.min_gate, 3.5 * float(np.percentile(self.leak, 90)), 4 * self.floor())
        self.hits = self.hits + 1 if loud else max(0, self.hits - 1)
        return self.hits >= self.NEED

    def stats(self):
        a = np.array(self.leak or [0.0])
        return f"mic level while she spoke: median {np.median(a):.0f}, p90 {np.percentile(a, 90):.0f}, max {a.max():.0f}; gate {self.gate:.0f}"

class Listener:
    def __init__(self, cfg, ctx):
        self.cfg, self.model, self.ctx, self.level = cfg, None, ctx, 0.0   # level: 0..1 mic loudness, for UIs
        self.proc, self.q, self.floor = None, queue.Queue(), []
        self.armed = False                                 # armed while she speaks: you talking over her stops her
        self.barge = BargeDetector(cfg["barge_gate"], self._floor)

    # microphone
    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        target = ["--target", AUDIO["source"]] if AUDIO["source"] else []
        self.proc = subprocess.Popen(["pw-record", *target, "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        while True:
            buf = proc.stdout.read(FRAME_BYTES)
            if len(buf) < FRAME_BYTES:
                return
            self.q.put(buf)
            a = np.frombuffer(buf, np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(a * a)))
            self.level = 0.6 * self.level + 0.4 * min(1.0, rms / 4000.0)
            if self.armed and self.barge.feed(rms):
                self.armed = False
                self.ctx.log("barge-in: " + self.barge.stats())
                self.ctx.barge_in()

    def _floor(self):
        return float(np.median(self.floor)) if self.floor else 0.0

    def flush(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def _frames(self):
        self.ensure()
        while True:
            try:
                yield self.q.get(timeout=3)
            except queue.Empty:
                self.ensure()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)

    # speech to text
    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            print("  (loading speech model...)", flush=True)
            self.model = WhisperModel(self.cfg["model"], device="cpu", compute_type="int8", cpu_threads=self.cfg["threads"])
        return self.model

    def transcribe(self, audio):
        segs, _ = self.load().transcribe(audio, language="en", beam_size=1, vad_filter=True)
        # drop what Whisper invents from noise ("Thank you.", "you"): it is very unsure of those segments
        return " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0).strip()

    def collect(self, frames, max_wait=None, end_silence=None, max_len=20.0, floor=None):
        """Loudness-based phrase detection. Returns float32 audio of one phrase, or None.
        The noise floor adapts to your room; max_wait gives up if nobody speaks within that many seconds."""
        import numpy as np
        end_silence = self.cfg["end_silence"] if end_silence is None else end_silence
        floor = self.floor if floor is None else floor
        frame_s, learned = FRAME / RATE, (0.5 if len(floor) >= 15 else 0)
        pre, kept, started, quiet, t = [], [], False, 0.0, 0.0
        for buf in frames:
            a = np.frombuffer(buf, np.int16).astype(np.float32)
            level = float(np.sqrt(np.mean(a * a)))
            t += frame_s
            if learned < 0.5:
                floor.append(level); learned += frame_s
                continue
            loud = level > max(float(np.median(floor)) * 3.0, 150.0)
            if not started:
                pre = (pre + [buf])[-10:]                    # ~0.3 s lead-in so the first syllable is kept
                if loud:
                    started, kept, quiet = True, list(pre), 0.0
                elif max_wait is not None and t > max_wait:
                    return None
                else:
                    floor.append(level); del floor[:-100]    # keep tracking background noise while idle
                continue
            kept.append(buf)
            quiet = 0.0 if loud else quiet + frame_s
            if quiet >= end_silence or len(kept) * frame_s > max_len:
                break
        if not started:
            return None
        pcm = np.frombuffer(b"".join(kept), np.int16).astype(np.float32) / 32768.0
        return pcm if len(pcm) > RATE * 0.4 else None

    STRONG = {"a", "an", "the", "to", "and", "or", "but", "so", "if", "of", "with", "for", "that", "my", "your",
              "because", "then", "like", "about", "is", "are", "give", "have", "has", "can", "could", "would", "also"}
    WEAK = {"me", "you", "i", "them", "us"}      # fine at the end of a sentence, but not without a full stop

    def unfinished(self, text):
        """True if the transcript sounds cut off mid-sentence."""
        t = text.strip().lower()
        if not t:
            return False
        if t.endswith((",", "...", "…", "-")):
            return True
        words = re.findall(r"[a-z']+", t)
        last = words[-1] if words else ""
        return last in self.STRONG or (last in self.WEAK and not t.endswith((".", "?", "!")))

    def hear(self, max_wait=None):
        """Listen for one phrase and return its text ('' if nothing was said)."""
        self.ctx.emit("state", state="listening")
        audio = self.collect(self._frames(), max_wait=max_wait)
        if audio is None:
            return ""
        t0 = time.perf_counter()
        text = self.transcribe(audio)
        for _ in range(2):                                   # you paused mid-sentence: wait a moment for the rest
            if not self.unfinished(text):
                break
            more = self.collect(self._frames(), max_wait=2.0)
            if more is None:
                break
            audio = np.concatenate([audio, np.zeros(int(RATE * 0.25), np.float32), more])
            text = self.transcribe(audio)
        self.ctx.log(f"timing: transcribed {len(audio) / RATE:.1f}s of speech in {time.perf_counter() - t0:.1f}s")
        return text

    def record_until_enter(self):
        """Keyboard push-to-talk: Enter starts recording, Enter again stops it."""
        import numpy as np, wave
        wav = tempfile.mktemp(suffix=".wav")
        rec = subprocess.Popen(["pw-record", "--rate", str(RATE), "--channels", "1", wav], stderr=subprocess.DEVNULL)
        try:
            input("  * listening... press Enter to stop ")
        finally:
            rec.send_signal(signal.SIGINT); rec.wait()
        try:
            if os.path.getsize(wav) < 4000:
                return ""
            with wave.open(wav) as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
            return self.transcribe(audio)
        finally:
            if os.path.exists(wav):
                os.remove(wav)

_listener = None

def setup(ctx):
    global _listener
    listener = _listener = Listener(ctx.cfg("stt_whisper", DEFAULTS), ctx)
    ctx.provide("listen", listener, "whisper", priority=100)

    def after_speech():                  # never treat her own voice as input
        if ctx.barged:                   # you interrupted: what you are saying is the next request, keep it
            return
        time.sleep(0.5)                  # the Bluetooth speaker plays a little behind the sound card
        listener.flush()

    def on_state(state=None, **kw):
        was = listener.armed
        if was and state != "speaking" and listener.barge.leak:
            ctx.log("no barge-in: " + listener.barge.stats())     # how much of her own voice leaked: the number to tune with
        listener.armed = state == "speaking" and bool(AUDIO["source"]) and listener.cfg["barge_in"]
        if listener.armed and not was:
            listener.barge.reset()

    def interrupt_cmd(m, text):
        on = not re.search(r"\b(don'?t|do not|stop|no more|disable|turn off)\b", text, re.I)
        listener.cfg["barge_in"] = on
        return "Okay, you can interrupt me now." if on else "Okay, I won't let you interrupt me."

    ctx.on("speech.end", after_speech)
    ctx.on("state", on_state)
    ctx.command(r"\b(let me|allow|enable|stop|don'?t let me|disable|turn (?:on|off))\b.*\binterrupt", interrupt_cmd, "barge_toggle", priority=50)

def teardown(ctx):
    _listener.stop()
