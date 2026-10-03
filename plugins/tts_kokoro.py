"""Main voice: Kokoro neural TTS (local, free). Speaks sentence by sentence while the next one is generated.
Owns its own voice commands: speak faster/slower, use the Heart voice, blend in some Nicole..."""
import os, queue, re, threading, time
from hommie.core import HERE, play_wav, sentences, temp_wav

PRIORITY = 30
REQUIRES = ["kokoro_onnx", "soundfile"]
DEFAULTS = {
    "voice": "af_bella",        # af_heart and af_nicole are saved for other projects
    "speed": 1.0,               # 0.7 slow ... 1.4 fast
    "blend": "",                # e.g. "af_heart:0.3" mixes 30% of that voice into the main one
}
VOICES = {
    "bella": "af_bella", "heart": "af_heart", "nicole": "af_nicole", "sky": "af_sky", "sarah": "af_sarah",
    "nova": "af_nova", "jessica": "af_jessica", "alloy": "af_alloy", "aoede": "af_aoede", "kore": "af_kore",
    "river": "af_river", "emma": "bf_emma", "isabella": "bf_isabella", "alice": "bf_alice", "lily": "bf_lily",
}

UI_TITLE = "Voice"
UI = [  # the web UI builds its settings panel from this; remove the plugin and the controls disappear with it
    {"key": "voice", "label": "Voice", "type": "select", "options": [[v, k.title()] for k, v in VOICES.items()]},
    {"key": "speed", "label": "Speed", "type": "range", "min": 0.7, "max": 1.4, "step": 0.02},
]

class Kokoro:
    def __init__(self, cfg):
        self.cfg, self.k, self.cache, self.mu = cfg, None, {}, threading.Lock()

    def _load(self):
        if self.k is None:
            from kokoro_onnx import Kokoro as K
            vd = os.path.join(HERE, "voices")
            self.k = K(os.path.join(vd, "kokoro-v1.0.onnx"), os.path.join(vd, "voices-v1.0.bin"))
        return self.k

    def _style(self):
        k, c = self._load(), self.cfg
        if not c["blend"]:
            return c["voice"]
        other, _, w = c["blend"].partition(":")
        w = float(w or 0.3)
        return k.get_voice_style(c["voice"]) * (1 - w) + k.get_voice_style(other) * w

    def _key(self, text):
        return (text, self.cfg["voice"], self.cfg["blend"], self.cfg["speed"])

    def render(self, text):
        """Text -> path of a wav file (the caller plays and deletes it). Pre-rendered phrases come back instantly."""
        import soundfile as sf
        k = self._load()
        cached = self.cache.get(self._key(text))
        if cached is None:
            with self.mu:
                cached = k.create(text, voice=self._style(), speed=self.cfg["speed"], lang="en-us")
        wav = temp_wav()
        sf.write(wav, *cached)
        return wav

    def prewarm(self, phrases):
        """Render short stock phrases ahead of time so saying them has no delay."""
        self._load()
        for text in phrases:
            key = self._key(text)
            if key not in self.cache:
                with self.mu:
                    self.cache[key] = self._load().create(text, voice=self._style(), speed=self.cfg["speed"], lang="en-us")

    def speak(self, text):
        import soundfile as sf
        k, style, speed, q = self._load(), self._style(), self.cfg["speed"], queue.Queue()

        def make():
            try:
                for sent in sentences(text):
                    q.put(self.render(sent))
            except Exception as e:
                q.put(e)
            finally:
                q.put(None)

        t0 = time.perf_counter()
        threading.Thread(target=make, daemon=True).start()
        played = False
        while (item := q.get()) is not None:
            if isinstance(item, Exception):
                if not played:
                    raise item            # nothing said yet: let the next voice plugin take over
                break
            if not played:
                print(f"  [timing: first audio ready in {time.perf_counter() - t0:.1f}s]", flush=True)
            play_wav(item)
            played = True

def setup(ctx):
    if not os.path.exists(os.path.join(HERE, "voices", "kokoro-v1.0.onnx")):
        raise FileNotFoundError("voices/kokoro-v1.0.onnx not downloaded")
    cfg = ctx.cfg("tts_kokoro", DEFAULTS)
    ctx.provide("tts", Kokoro(cfg), "kokoro", priority=100)

    def voice_cmd(m, text):
        t = text.lower()
        if re.search(r"\b(speak|talk)\b.*\b(faster|quicker)\b|\bspeed up\b", t):
            cfg["speed"] = min(1.4, round(cfg["speed"] + 0.08, 2)); return "Okay, picking up the pace."
        if re.search(r"\b(speak|talk)\b.*\bslower\b|\bslow down\b", t):
            cfg["speed"] = max(0.7, round(cfg["speed"] - 0.08, 2)); return "Sure, taking it a bit slower."
        if re.search(r"\b(normal|default) (speed|pace)\b", t):
            cfg["speed"] = 1.0; return "Back to my normal pace."
        if re.search(r"\b(no|remove the|reset the) blend\b|\breset (my|your|the) voice\b", t):
            cfg["blend"] = ""; return "Done, back to my regular voice."
        who = re.search(r"\b(" + "|".join(VOICES) + r")\b", t)
        if not who or not re.search(r"\b(voice|blend|switch|use|sound like)\b", t):
            return None
        vid = VOICES[who.group(1)]
        if "blend" in t or "mix" in t:
            other, _, w = cfg["blend"].partition(":")
            cur = float(w) if other == vid and w else 0.0
            w = max(0.0, min(0.6, cur + (-0.15 if "less" in t else 0.15 if cur else 0.3)))
            cfg["blend"] = f"{vid}:{round(w, 2)}" if w else ""
            return f"Okay, blending in {who.group(1)} at {int(round(w * 100))} percent." if w else "Okay, no blend."
        cfg["voice"], cfg["blend"] = vid, ""
        return f"Okay, switching to the {who.group(1)} voice."

    ctx.command(r"faster|quicker|slower|speed up|slow down|(normal|default) (speed|pace)|blend|reset.*voice|\bvoice\b|sound like",
                voice_cmd, "kokoro_voice", priority=50)
