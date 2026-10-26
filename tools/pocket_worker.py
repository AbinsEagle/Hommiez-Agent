"""Pocket TTS worker: runs under a Python that has torch + pocket-tts, talks JSON lines over stdin/stdout.
Started by plugins/tts_pocket.py. Request: {"id", "text", "voice", "out"} -> reply {"id", "ok"} (voice = preset name or a .wav to clone)."""
import json, os, re, sys

real_out, sys.stdout = sys.stdout, sys.stderr          # library chatter must not corrupt the JSON channel
import numpy as np, soundfile as sf, torch
torch.set_num_threads(int(os.environ.get("POCKET_THREADS", "4")))
from pocket_tts import TTSModel

def reply(**kw):
    real_out.write(json.dumps(kw) + "\n"); real_out.flush()

def pieces(text, limit=180):
    """Very long sentences are voiced in comma-sized pieces."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for part in re.split(r"(?<=,)\s+", text):
        if buf and len(buf) + len(part) > limit:
            out.append(buf); buf = part
        else:
            buf = (buf + " " + part).strip()
    return out + [buf] if buf else out

model = TTSModel.load_model()
states = {}
reply(ready=True, rate=model.sample_rate)
for line in sys.stdin:
    req = json.loads(line)
    try:
        voice = os.path.expanduser(req["voice"])
        if voice not in states:
            states[voice] = model.get_state_for_audio_prompt(voice)
        audio = [model.generate_audio(states[voice], p).numpy().squeeze() for p in pieces(req["text"])]
        sf.write(req["out"], np.concatenate(audio), model.sample_rate)
        reply(id=req["id"], ok=True)
    except Exception as e:
        reply(id=req.get("id"), ok=False, error=str(e)[:200])
