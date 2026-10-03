"""Brain: your restructured knowledge base (~/AbinsAI/Brain, built by tools/build_brain.py) as Hommie's memory.

  - Every question is searched against the Brain (SQLite full-text, BM25). Strong matches are shown to Claude as
    background, with titles and file paths, so answers come from YOUR notes and can cite them.
  - "search my brain for X" lists the top matching notes instantly, without calling Claude.
  - Read-only for Claude: the Brain is generated, so rebuild it with tools/build_brain.py instead of editing it.
Delete this file and Hommie simply stops consulting it."""
import os, re, sqlite3

PRIORITY = 23
DEFAULTS = {"path": "/home/rona/AbinsAI/Brain", "top_k": 4, "snippet_chars": 320, "auto_context": True, "min_score": 6.0}
UI_TITLE = "Brain (knowledge base)"
UI = [{"key": "auto_context", "label": "Use my notes to answer questions", "type": "toggle"},
      {"key": "top_k", "label": "Notes shown to Claude per question", "type": "range", "min": 1, "max": 8, "step": 1}]

STOP = set("""a an and are as at be but by can could did do does for from had has have how i if in into is it its me my of on or
our she should so than that the their them then there these they this to up us was we were what when where which who why will
with would you your about tell show give find search look know remember think said say please hey hommie homie notes brain
note anything something everything much many some any""".split())

def terms(text):
    seen, out = set(), []
    for t in re.findall(r"[a-z0-9][a-z0-9\-]{1,}", text.lower()):
        if t not in STOP and t not in seen and not (t.isdigit() and len(t) > 4):
            seen.add(t); out.append(t)
    return out

class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = None

    def _db(self):
        if self.db is None:
            self.db = sqlite3.connect(os.path.join(self.cfg["path"], "_index", "brain.sqlite"), check_same_thread=False)
        return self.db

    def search(self, text, k=None):
        """[(score, title, date, rel_path, snippet)], best first. Score is positive; higher is better."""
        ts = terms(text)[:10]
        if not ts:
            return []
        q = " OR ".join(f'"{t}"' for t in ts)
        try:
            rows = self._db().execute(
                "SELECT title, date, path, snippet(notes, 3, '', '', ' … ', 24), bm25(notes, 12.0, 4.0, 4.0, 1.0) "
                "FROM notes WHERE notes MATCH ? ORDER BY bm25(notes, 12.0, 4.0, 4.0, 1.0) LIMIT ?", (q, k or self.cfg["top_k"])).fetchall()
        except sqlite3.Error:
            return []
        return [(-r[4], r[0], r[1], r[2], r[3]) for r in rows]

    def context(self, text):
        if not self.cfg["auto_context"] or len(terms(text)) < 1:
            return None
        about_me = re.search(r"\b(my notes|my brain|i noted|i wrote|i saved|i researched|i asked|i did|did i|i was working|i had)\b", text, re.I)
        bar = min(self.cfg["min_score"], 3.5) if about_me else self.cfg["min_score"]   # asking about your own past work: lower the bar
        hits = [h for h in self.search(text) if h[0] >= bar]
        if not hits:
            return None
        n = self.cfg["snippet_chars"]
        lines = [f"{i}. {t}" + (f" ({d})" if d else "") + f" — {re.sub(chr(10), ' ', s)[:n]}  [file: {os.path.join(self.cfg['path'], p)}]"
                 for i, (_, t, d, p, s) in enumerate(hits, 1)]
        return ("[Background from the user's own notes (their Brain). These matched the request by keyword and may or may not be "
                "relevant. Use them if they help and name the note titles you used; Read the file for more detail; don't claim "
                "more than the notes say.]\n" + "\n".join(lines))

def setup(ctx):
    cfg = ctx.cfg("brain_wiki", DEFAULTS)
    path = cfg["path"]
    if not os.path.exists(os.path.join(path, "_index", "brain.sqlite")):
        raise FileNotFoundError("Brain index missing; run tools/build_brain.py")
    brain = Brain(cfg)
    ctx.add_dir(path)
    for tool in ("Edit", "Write", "NotebookEdit"):
        ctx.deny(f"{tool}(/{path}/**)")
    ctx.persona(f"The user's personal knowledge base, the Brain, is at {path}: their ChatGPT conversations (Conversations/), "
                "project docs (Projects/) and topic overviews (Topics/), read-only. Relevant matches are often provided with "
                "their request; cite note titles when you use them.")
    ctx.add_context(brain.context, "brain_wiki")

    def find(m, text):
        q = m.group("q").strip(" ?.")
        hits = brain.search(q, 5) if q else []
        if not hits:
            return f"I couldn't find anything in your brain about {q}." if q else None
        top = "; ".join(f"{t}" + (f", {d[:4]}" if d else "") for _, t, d, _, _ in hits[:3])
        return f"I found {len(hits)} matches. The best are: {top}."

    ctx.command(r"\b(?:search|find|look up|look for)\b.*\b(?:my brain|the brain|brain|knowledge base)\b(?:\s+(?:for|about|on))?\s*(?P<q>.*)",
                find, "brain_search", priority=70)
    ctx.command(r"\b(?:search|find|look up|look for)\b.*\b(?:my notes|notes|vault|wiki)\b(?:\s+(?:for|about|on))?\s*(?P<q>.*)",
                find, "brain_search_notes", priority=62)   # beats the old-vault search in the obsidian plugin
