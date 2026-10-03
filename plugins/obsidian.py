"""Obsidian: connects Hommie to your vault (found automatically from Obsidian's own config).

  Read   Claude can read and search the whole vault, so "what did I note about the CDI project?" just works.
  Write  Hommie only ever writes inside <vault>/Hommie/ (daily notes, build log). Claude itself is blocked from
         editing anything in the vault, so your existing notes can't be changed by a misheard command.

Voice/typed commands (instant, no Claude needed):
  "note that the TDS probe needs calibrating"      -> today's daily note, Hommie/Daily/YYYY-MM-DD.md
  "add to the build log: UI plugin finished"       -> Hommie/Build Log.md, dated
  "search my notes for peltier"                    -> top matching note titles
  "read me today's note"
Delete this file and Hommie simply stops knowing about Obsidian.
"""
import datetime, json, os, re

PRIORITY = 22
DEFAULTS = {
    "vault": "",                 # empty = the vault Obsidian has open
    "folder": "Hommie",          # the only place Hommie writes
    "log_conversations": False,  # also append each brain conversation turn to today's daily note
}
UI_TITLE = "Obsidian"
UI = [{"key": "log_conversations", "label": "Log conversations to the daily note", "type": "toggle"}]

def find_vault(configured):
    if configured and os.path.isdir(configured):
        return configured
    try:
        with open(os.path.expanduser("~/.config/obsidian/obsidian.json")) as f:
            vaults = list(json.load(f).get("vaults", {}).values())
    except (OSError, ValueError):
        return None
    vaults.sort(key=lambda v: not v.get("open"))          # the open one first
    for v in vaults:
        if os.path.isdir(v.get("path", "")):
            return v["path"]
    return None

class Vault:
    def __init__(self, root, folder):
        self.root = os.path.realpath(root)
        self.out = os.path.join(self.root, folder)

    def _safe(self, *parts):
        """A path inside the Hommie folder, never anywhere else in the vault."""
        p = os.path.realpath(os.path.join(self.out, *parts))
        if os.path.commonpath([p, self.out]) != self.out:
            raise ValueError("refusing to write outside the Hommie folder")
        return p

    def append(self, rel, line, header=None):
        path = self._safe(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new and header:
                f.write(header)
            f.write(line.rstrip("\n") + "\n")
        return path

    def daily_path(self, day=None):
        return os.path.join("Daily", f"{(day or datetime.date.today()).isoformat()}.md")

    def add_daily(self, text):
        now = datetime.datetime.now()
        head = f"---\ntags: [hommie, daily]\n---\n# {now.date().isoformat()}\n\n"
        return self.append(self.daily_path(), f"- {now:%H:%M} {text}", header=head)

    def add_build_log(self, text):
        head = "---\ntags: [hommie, build-log]\n---\n# Build Log\n\nSee [[Hommie]].\n\n"
        return self.append("Build Log.md", f"- {datetime.date.today().isoformat()}: {text}", header=head)

    def read_daily(self):
        try:
            with open(self._safe(self.daily_path()), encoding="utf-8") as f:
                return [l[2:].strip() for l in f if l.startswith("- ")]
        except OSError:
            return []

    def search(self, query, limit=5):
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        if not words:
            return []
        scored = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                score = 5 * sum(w in fn.lower() for w in words)
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore") as f:
                        text = f.read().lower()
                except OSError:
                    continue
                hits = [text.count(w) for w in words]
                if all(hits):                              # every word must appear somewhere
                    score += sum(min(h, 10) for h in hits)
                elif not score:
                    continue
                scored.append((score, re.sub(r"^\d{4}-\d{2}-\d{2}_", "", fn[:-3])))
        scored.sort(key=lambda s: -s[0])
        return [title for _, title in scored[:limit]]

def setup(ctx):
    cfg = ctx.cfg("obsidian", DEFAULTS)
    root = find_vault(cfg["vault"])
    if not root:
        raise FileNotFoundError("no Obsidian vault found (set \"vault\" in settings.json under \"obsidian\")")
    vault = Vault(root, cfg["folder"])

    ctx.add_dir(vault.root)                                          # Claude may read and search it...
    ctx.deny(f"Edit(/{vault.root}/**)")                              # ...but never change it
    ctx.deny(f"Write(/{vault.root}/**)")
    ctx.deny(f"NotebookEdit(/{vault.root}/**)")
    ctx.persona(f"The user's Obsidian vault (their Brain Wiki of past notes and ChatGPT chats) is at {vault.root}, "
                "read-only for you. Use Grep and Read there to answer questions about their past notes, and mention "
                "note titles. You cannot save notes yourself: if asked, tell them to say 'note that ...'.")

    def capture(m, text):
        what = m.group("text").strip(" .")
        if not what:
            return None
        vault.add_daily(what)
        return "Noted in today's daily note."

    def build_log(m, text):
        what = m.group("text").strip(" .")
        if not what:
            return None
        vault.add_build_log(what)
        return "Added to the build log."

    def search(m, text):
        q = m.group("q").strip(" ?.")
        if not q:
            return None
        hits = vault.search(q)
        if not hits:
            return f"I couldn't find any notes about {q}."
        n = len(hits)
        return (f"I found {n} notes on that. Top ones: " if n > 1 else "I found one note on that: ") + "; ".join(hits[:3]) + "."

    def today(m, text):
        items = vault.read_daily()
        if not items:
            return "Nothing in today's note yet."
        return f"Today's note has {len(items)} items. " + " Then ".join(re.sub(r"^\d\d:\d\d ", "", i) for i in items[-3:])

    ctx.command(r"\b(?:add|put|write)\b.*\b(?:to|in|into)\b.*\bbuild ?log\b[\s:,-]*(?P<text>.*)", build_log, "obsidian_buildlog", priority=70)
    ctx.command(r"\b(?:take a note|note that|note down|note this|jot down|add a note|log this)\b[\s:,-]*(?P<text>.*)", capture, "obsidian_note", priority=65)
    ctx.command(r"\b(?:search|find|look up|look for)\b.*\b(?:my notes|notes|vault|obsidian|wiki)\b(?:\s+(?:for|about|on))?\s*(?P<q>.*)", search, "obsidian_search", priority=60)
    ctx.command(r"\b(?:read|show|what'?s in)\b.*\b(?:today'?s|my) (?:daily )?note\b", today, "obsidian_today", priority=60)

    last = {"heard": ""}
    ctx.on("heard", lambda text, **kw: last.update(heard=text))

    def log_turn(text, source, **kw):
        if source == "brain" and cfg["log_conversations"]:
            vault.add_daily(f"**You:** {last['heard']} — **Hommie:** {text}".replace("\n", " "))

    ctx.on("reply", log_turn)
