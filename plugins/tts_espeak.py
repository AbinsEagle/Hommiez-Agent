"""Last-resort robot voice (espeak-ng). Keeps Hommie talking if every other voice fails."""
import shutil, subprocess

PRIORITY = 32

class Espeak:
    def speak(self, text):
        subprocess.run(["espeak-ng", "-s", "165", "-p", "40", text], check=False)

def setup(ctx):
    if not shutil.which("espeak-ng"):
        raise FileNotFoundError("espeak-ng is not installed")
    ctx.provide("tts", Espeak(), "espeak", priority=10)
