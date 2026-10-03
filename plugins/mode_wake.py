"""Hands-free mode: say "Hey Hommie ...", then keep talking for a few seconds without repeating the wake word."""
import random, re, threading, time
from hommie.core import is_yes

PRIORITY = 60
DEFAULTS = {"follow_up_seconds": 25, "confirm_seconds": 10}
UI_TITLE = "Conversation"
UI = [{"key": "follow_up_seconds", "label": "Keep listening after a reply (s)", "type": "range", "min": 3, "max": 20, "step": 1}]
_NAME = r"(?:h[ao]m+[iey]+|home+y)"                       # Hommie / Homie / Hammy / Homey: whatever Whisper hears
_GREET = r"(?:hey|hi|hello|hallo|hay|hiya|howdy|yo|okay|ok)"
WAKE_RE = re.compile(                                      # "Hommie ...", "Hey/Hello Hommie ...", "... , Hommie"
    r"(?:^\W*(?:" + _GREET + r"\W+)?" + _NAME + r"\b[\s,.!?:-]*)"
    r"|(?:\b" + _GREET + r"\W+" + _NAME + r"\b[\s,.!?:-]*)"
    r"|(?:[\s,.!?:-]*\b" + _NAME + r"\W*$)", re.I)
ACKS = ["I'm here.", "Go ahead.", "What's up?", "Listening.", "Yeah, tell me.", "Mm-hm?",
        "What can I do for you?", "I'm all ears.", "Right here.", "Hit me."]

JUNK = {"", "you", "uh", "um", "hmm", "mm", "thanks for watching", "thank you for watching"}   # Whisper hears these in noise

def strip_wake(text):
    """'Hey Hommie open the log' -> (True, 'open the log')."""
    m = WAKE_RE.search(text)
    if not m:
        return False, text.strip()
    return True, (text[:m.start()] + " " + text[m.end():]).strip()

class WakeMode:
    def __init__(self, cfg):
        self.cfg, self.last_ack, self.last_said, self.said_at = cfg, None, set(), 0.0

    def remember(self, text, **kw):
        self.last_said, self.said_at = set(re.findall(r"[a-z']+", text.lower())), time.monotonic()

    def is_echo(self, heard):
        """True if this is (mostly) what she just said, picked up again through the mic."""
        words = re.findall(r"[a-z']+", heard.lower())
        return (len(words) >= 3 and time.monotonic() - self.said_at < 30
                and sum(w in self.last_said for w in words) / len(words) >= 0.75)

    def ack(self):
        self.last_ack = random.choice([a for a in ACKS if a != self.last_ack])
        return self.last_ack

    def run(self, ctx):
        ears = ctx.service("listen")
        if ears is None:
            print("Wake mode needs a 'listen' plugin (stt_whisper). Try:  jarvis.py --type")
            return
        print('Hommie online. Say "Hey Hommie ..." (Ctrl-C to quit).')
        ears.load(); ears.ensure()
        tts = ctx.service("tts")
        if hasattr(tts, "prewarm"):                         # stock phrases are rendered once, so they play with no delay
            threading.Thread(target=tts.prewarm, args=(ACKS,), daemon=True).start()
        ctx.say("Hommie online.")
        ctx.on("say", self.remember)
        ctx.confirm = lambda: is_yes(ears.hear(max_wait=self.cfg["confirm_seconds"]))
        ctx.wake_active, ctx.talk_now = True, False        # a UI's Talk button sets talk_now = "next phrase is for you"
        try:
            self._loop(ctx, ears)
        finally:
            ctx.wake_active = False

    def _loop(self, ctx, ears):
        while not ctx.quit_requested:
            print("  (listening for the wake word)", flush=True)
            heard = ears.hear()
            if self.is_echo(heard):
                continue
            woke, command = strip_wake(heard)
            if ctx.talk_now and heard:
                woke, command, ctx.talk_now = True, command if woke else heard, False
            if not woke:
                continue
            if not command:
                ctx.say(self.ack())
                command = self._next_command(ctx, ears, 10)
            # conversation window: keep going until it goes quiet or you say goodbye
            while command and not ctx.quit_requested:
                ctx.sleep_requested = False
                t0 = time.perf_counter()
                reply = ctx.handle(command)
                ctx.log(f"timing: request handled (thinking + speaking) in {time.perf_counter() - t0:.1f}s")
                ctx.say(reply)
                if ctx.sleep_requested or ctx.quit_requested:
                    break
                command = self._next_command(ctx, ears, self.cfg["follow_up_seconds"])
                ctx.talk_now = False

    def _next_command(self, ctx, ears, seconds):
        """Wait up to `seconds` for a real request. Noise, empty transcripts and a bare "Hommie" don't end the wait."""
        deadline = time.monotonic() + seconds
        while not ctx.quit_requested and (left := deadline - time.monotonic()) > 0:
            heard = ears.hear(max_wait=left)
            if heard.lower().strip(" .,!?") in JUNK or self.is_echo(heard):
                continue
            woke, command = strip_wake(heard)
            if command:
                return command
            if woke:                                         # just her name: answer, then keep waiting
                ctx.say(self.ack())
                deadline = time.monotonic() + 10
        return ""

def setup(ctx):
    ctx.provide("mode", WakeMode(ctx.cfg("mode_wake", DEFAULTS)), "wake")
