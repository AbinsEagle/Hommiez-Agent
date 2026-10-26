"""Backup voice: Piper (local). Used only if the Kokoro plugin is missing or fails."""
import os
from hommie.core import HERE, play_wav, pronounce, temp_wav

PRIORITY = 31
REQUIRES = ["piper"]
VOICE_FILE = "en_US-kristin-medium.onnx"

class Piper:
    def __init__(self):
        self.v = None

    def speak(self, text):
        import wave
        if self.v is None:
            from piper import PiperVoice
            self.v = PiperVoice.load(os.path.join(HERE, "voices", VOICE_FILE))
        wav = temp_wav()
        with wave.open(wav, "wb") as w:
            self.v.synthesize_wav(pronounce(text), w)
        play_wav(wav)

def setup(ctx):
    if not os.path.exists(os.path.join(HERE, "voices", VOICE_FILE)):
        raise FileNotFoundError("voice file missing in voices/")
    ctx.provide("tts", Piper(), "piper", priority=50)
