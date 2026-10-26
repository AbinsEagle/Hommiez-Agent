"""The brain: one long-lived `claude` process (your claude.ai login, no API key), run from a neutral folder
so each turn skips startup cost. Anything blocked is confirmed out loud, then allowed for that one action.
Which model it runs (and Groq/Gemini instead) is chosen by brain_router.py."""
import json, os, re, shutil, subprocess, threading
from hommie.core import HERE

PRIORITY = 40
DEFAULTS = {
    "model": "haiku",           # haiku = fastest, sonnet = smarter
    "effort": "low",
    "base_allow": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(ls:*)", "Bash(cat:*)"],
}
# The Brain dropdown and the "use the smart model" commands live in brain_router.py, which sets cfg["model"].
PERMISSION_NOTE = ("If a tool is blocked for permissions, say in one short sentence what you tried; the app "
                   "will ask the user to confirm out loud, so don't ask yourself.")

_brain = None

class Brain:
    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.proc, self.session, self.allow, self.model = None, None, (), None
        self.streams, self.last_spoken = True, False      # streams: ask() can hand text to a SpeechStream as it arrives

    def _start(self, extra_allow):
        self.close()
        persona = " ".join(self.ctx.persona_parts + [PERMISSION_NOTE])
        allowed = list(self.cfg["base_allow"]) + self.ctx.allowed + list(extra_allow)
        cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--model", self.cfg["model"], "--effort", self.cfg["effort"], "--disable-slash-commands",
               "--strict-mcp-config", "--append-system-prompt", persona, "--allowedTools", *allowed]
        if self.ctx.denied:
            cmd += ["--disallowedTools", *self.ctx.denied]
        for d in self.ctx.dirs:
            cmd += ["--add-dir", d]
        if self.session:
            cmd += ["--resume", self.session]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, cwd=HERE)
        self.allow, self.model = tuple(extra_allow), self.cfg["model"]

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None

    def _turn(self, prompt, extra_allow=(), on_text=None):
        """Returns (reply_text, permission_denials)."""
        if (not self.proc or self.proc.poll() is not None or self.allow != tuple(extra_allow)
                or self.model != self.cfg["model"]):
            self._start(extra_allow)
        try:
            self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            self.proc.stdin.flush()
            for line in self.proc.stdout:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("session_id"):
                    self.session = e["session_id"]
                if on_text and e.get("type") == "stream_event":
                    delta = (e.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        on_text(delta.get("text", ""))
                if e.get("type") == "result":
                    return e.get("result", ""), e.get("permission_denials") or []
        except (BrokenPipeError, OSError):
            pass
        self.close()
        return "Sorry, I lost my connection to Claude. Try that again.", []

    def ask(self, text, stream=None):
        self.last_spoken = False
        reply, denials = self._turn(text, on_text=stream.push if stream else None)
        if not denials:
            self.last_spoken = bool(stream and stream.began)   # the whole reply went through the stream
            return reply
        if stream:
            stream.finish()                                    # let what she already said end before asking permission
        spoken, shown, rule = self._describe(denials[0])
        if self._protected(denials[0]):               # a plugin denied this for good; asking would be pointless
            self.ctx.log(f"protected: {shown}")
            return "I'm not allowed to change that one. It's protected."
        self.ctx.log(f"blocked: {shown}")
        self.ctx.say(f"I need your permission to run this: {spoken}. Say yes to confirm.")
        if self.ctx.ask_confirm():
            reply, _ = self._turn("The user confirmed. Go ahead and do it now.", extra_allow=[rule])
            return reply
        return "Okay, I won't do that."

    def _protected(self, denial):
        """True if a ctx.deny() rule (e.g. a read-only vault) covers this file edit."""
        tool = denial.get("tool_name", "")
        path = os.path.realpath((denial.get("tool_input") or {}).get("file_path", "") or "/nonexistent")
        for rule in self.ctx.denied:
            m = re.match(r"(\w+)\(/(/.*?)/?\*\*\)$", rule)
            if m and m.group(1) == tool and path.startswith(os.path.realpath(m.group(2)) + os.sep):
                return True
        return False

    @staticmethod
    def _describe(denial):
        """(spoken description, text for the screen, allow-rule) for one blocked tool call."""
        tool, inp = denial.get("tool_name", "a tool"), denial.get("tool_input", {})
        if tool == "Bash":
            c = inp.get("command", "")
            return inp.get("description") or "a shell command", f"Bash: {c}", f"Bash({c})"
        target = inp.get("file_path") or inp.get("path") or ""
        return f"{tool} {os.path.basename(target)}".strip(), f"{tool}: {target}", tool

def setup(ctx):
    global _brain
    if not shutil.which("claude"):
        raise FileNotFoundError("the claude CLI is not on PATH")
    cfg = ctx.cfg("brain_claude", DEFAULTS)
    brain = _brain = Brain(ctx, cfg)
    ctx.provide("brain", brain, "claude", priority=100)
    ctx.on("start", lambda: threading.Thread(target=brain._turn, args=("Reply with just the word ready.",), daemon=True).start())

def teardown(ctx):
    _brain.close()
