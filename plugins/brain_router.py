"""Brain switch: pick who answers - Claude, Groq or Gemini - from the Brain dropdown in the settings panel,
or by voice/typing: "use Groq", "use Gemini", "use Claude", "use the smart model", "use the fast model",
"which brain are you using". Groq and Gemini are answered over their OpenAI-compatible HTTP APIs, with the same abilities as
Claude (file read/search/edit, shell, Brain search) through hommie/agent_tools.py and the same permission rules.
API keys: GROQ_API_KEY / GEMINI_API_KEY from the environment, else the `export` lines in ~/.bashrc.
Delete this file and Hommie goes back to Claude alone (brain_claude still works)."""
import json, os, re, time, urllib.error, urllib.request
from hommie.agent_tools import SCHEMAS, ToolBox

PRIORITY = 41                      # after brain_claude (40), which it wraps
DEFAULTS = {"choice": "claude:haiku", "history_turns": 10}
CHOICES = [
    ("claude:haiku", "Claude - fast (Haiku)"),
    ("claude:sonnet", "Claude - smarter (Sonnet)"),
    ("groq:openai/gpt-oss-20b", "Groq - fast (gpt-oss 20B)"),
    ("groq:openai/gpt-oss-120b", "Groq - smarter (gpt-oss 120B)"),
    ("gemini:gemini-3.5-flash", "Gemini - fast (3.5 Flash)"),
    ("gemini:gemini-pro-latest", "Gemini - smarter (Pro, needs paid quota)"),
]
UI_TITLE = "Brain"
UI = [{"key": "choice", "label": "Who answers", "type": "select", "options": [list(c) for c in CHOICES]}]

APIS = {   # provider -> (chat-completions URL, env var, name to say out loud)
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "Groq"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY", "Gemini"),
}
MAX_STEPS = 8
TOOLS_NOTE = ("You have tools: read_file, list_dir, find_files, grep, write_file, edit_file, bash and search_brain (the user's "
              "own notes). Use them instead of guessing, and never invent file contents. If a tool is refused, say so in one "
              "short sentence. Anything not pre-approved is confirmed out loud by the app, so don't ask the user yourself. "
              "You are speaking aloud: keep replies short, no markdown.")

def api_key(var):
    if os.environ.get(var):
        return os.environ[var]
    try:
        with open(os.path.expanduser("~/.bashrc")) as f:
            found = None
            for line in f:
                m = re.match(rf"\s*export\s+{var}=['\"]?([^'\"\s]+)", line)
                if m:
                    found = m.group(1)           # the last assignment wins, as in a shell
            return found
    except OSError:
        return None

class Router:
    streams = True

    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.history, self.last_spoken, self.tools = [], False, None

    def choice(self):
        c = self.cfg["choice"]
        return c if c in dict(CHOICES) else DEFAULTS["choice"]

    def label(self):
        return dict(CHOICES)[self.choice()]

    def ask(self, text, stream=None):
        provider, _, model = self.choice().partition(":")
        if provider == "claude":
            claude = self.ctx.service("brain", "claude")
            self.ctx.cfg("brain_claude")["model"] = model      # the dropdown owns Claude's model now
            reply = claude.ask(text, stream=stream) if stream else claude.ask(text)
            self.last_spoken = claude.last_spoken
            return reply
        return self._chat(provider, model, text, stream)

    def _chat(self, provider, model, text, stream):
        """Agent loop: ask the model; if it calls tools, run them (hommie/agent_tools.py) and ask again, up to MAX_STEPS."""
        url, var, nice = APIS[provider]
        self.last_spoken = False
        key = api_key(var)
        if not key:
            return f"I don't have a {nice} key. Add {var} to your bashrc, or say use Claude."
        if self.tools is None:
            self.tools = ToolBox(self.ctx, self.ctx.cfg("brain_claude").get("base_allow", []))
        keep = int(self.cfg["history_turns"]) * 2
        msgs = ([{"role": "system", "content": " ".join(self.ctx.persona_parts + [TOOLS_NOTE])}]
                + self.history[-keep:] + [{"role": "user", "content": text}])
        said = []
        for _ in range(MAX_STEPS):
            msg, err = self._call(url, key, nice, model, msgs, stream)
            if err:
                return err
            if msg["content"].strip():
                said.append(msg["content"].strip())
            if not msg.get("tool_calls"):
                break
            msgs.append(msg)
            for tc in msg["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                result = self.tools.run(tc["function"]["name"], args)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"], "content": result})
        else:
            said.append("I went round in circles on that one, so I stopped.")
        reply = " ".join(said).strip()
        if not reply:
            return f"{nice} sent back nothing. Try again."
        self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        self.history = self.history[-keep:]
        self.last_spoken = bool(stream and stream.began)
        return reply

    def _call(self, url, key, nice, model, msgs, stream):
        """One streamed request. Returns (assistant message dict, error text or None)."""
        body = {"model": model, "messages": msgs, "stream": True, "tools": SCHEMAS}
        if model.startswith(("openai/gpt-oss", "gemini-3")):
            body["reasoning_effort"] = "low"                   # a voice reply shouldn't think for ten seconds
        req = urllib.request.Request(url, json.dumps(body).encode(), {
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "hommie/1.0"})
        text, calls = [], []
        try:
            try:
                resp = urllib.request.urlopen(req, timeout=90)
            except urllib.error.HTTPError as e:
                if e.code != 503:                              # overloaded: nothing was sent, so one retry is safe
                    raise
                time.sleep(1.5)
                resp = urllib.request.urlopen(req, timeout=90)
            with resp as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:") or line.endswith("[DONE]"):
                        continue
                    try:
                        delta = json.loads(line[5:])["choices"][0].get("delta") or {}
                    except (ValueError, KeyError, IndexError):
                        continue
                    if delta.get("content"):
                        text.append(delta["content"])
                        if stream:
                            stream.push(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        if fn.get("name") or not calls:        # a name arrives once per call, in both APIs
                            calls.append({"id": tc.get("id") or f"call_{len(calls)}", "type": "function",
                                          "function": {"name": fn.get("name", ""), "arguments": ""}})
                        slot = calls[tc["index"]] if isinstance(tc.get("index"), int) and tc["index"] < len(calls) and not fn.get("name") else calls[-1]
                        slot["function"]["arguments"] += fn.get("arguments") or ""
                        if tc.get("extra_content"):            # Gemini 3 needs its thought signature sent back
                            slot["extra_content"] = tc["extra_content"]
        except urllib.error.HTTPError as e:
            self.ctx.log(f"{nice} HTTP {e.code}: {e.read()[:300]!r}")
            return None, {429: f"{nice} says I'm out of free requests for now. Say use Claude, or try again in a minute.",
                          503: f"{nice} is overloaded right now. Try again, or say use Claude."}.get(
                e.code, f"{nice} gave an error, number {e.code}.")
        except (urllib.error.URLError, OSError) as e:
            self.ctx.log(f"{nice} failed: {e}")
            return None, f"I couldn't reach {nice}. Check the internet, or say use Claude."
        msg = {"role": "assistant", "content": "".join(text)}
        if calls:
            msg["tool_calls"] = calls
        return msg, None

def setup(ctx):
    cfg = ctx.cfg("brain_router", DEFAULTS)
    router = Router(ctx, cfg)
    ctx.provide("brain", router, "router", priority=200)

    def pick(choice):
        cfg["choice"] = choice
        return f"Okay, using {re.sub(r' *[(].*', '', dict(CHOICES)[choice]).replace(' - ', ', ')}."

    def switch_cmd(m, text):
        who = m.group(1).lower().replace("grok", "groq")
        if who == "groq" or who == "gemini":
            if not api_key(APIS[who][1]):
                return f"I can't, there's no {APIS[who][2]} key set."
        return pick(next(c for c, _ in CHOICES if c.startswith(who + ":")))   # first = the fast one

    def size_cmd(m, text):
        want_big = bool(re.search(r"\b(smart|smarter|big|think harder)\b", text, re.I))
        provider = router.choice().split(":")[0]
        mine = [c for c, _ in CHOICES if c.startswith(provider + ":")]
        return pick(mine[-1] if want_big else mine[0])

    def which_cmd(m, text):
        return f"I'm using {re.sub(r' *[(].*', '', router.label()).replace(' - ', ', ')}."

    ctx.command(r"\b(?:use|switch to|switch over to|change to|go to)\s+(?:the\s+)?(groq|grok|gemini|claude)\b", switch_cmd,
                "brain_switch", priority=60)
    ctx.command(r"\b(?:use|switch to|change to)\b.*\b(smart|smarter|big|fast|faster|quick|small)\b.*\b(?:model|brain)\b"
                r"|\bthink harder\b", size_cmd, "brain_size", priority=60)
    ctx.command(r"\bwhich (?:brain|model)\b|\bwhat (?:brain|model)\b.*\busing\b", which_cmd, "brain_which", priority=60)

def teardown(ctx):
    pass
