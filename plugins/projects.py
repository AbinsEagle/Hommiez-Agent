"""Projects: one folder per project under ./projects. Switching ("work on the esp32 project", or the Projects setting) gives
Hommie that project's instructions, its folder and its own conversation, so a CAD chat never leaks into a firmware one.
  projects/<name>/project.md   what the project is and how she should work in it (she drafts it with you on "new project")
  projects/<name>/notes.md     short lessons she adds herself as you go
  projects/rules.md            how projects may interact; empty until you need it"""
import os, re, shutil
from hommie.core import HERE

PRIORITY = 23
DIR = os.path.join(HERE, "projects")
DEFAULTS = {"active": ""}
UI_TITLE = "Projects"
UI = [{"key": "active", "label": "Active project", "type": "select", "options": [["", "None"]]}]
MARK = "(not set up yet)"
NOTES_TAIL, FILE_LIMIT = 1500, 4000
MINDSET = (   # a way of thinking, not a process: no paperwork, sprints or ceremonies unless the client asks for them
    "In a project you work as an experienced PMI-certified project manager with an agile mind, and the user is your client. "
    "Think with three hats, silently: "
    "SPONSOR: always know why the project exists and what value it must deliver; before piling on more work, ask whether it "
    "still serves that goal, and say so when the effort or cost outgrows the value. "
    "PRODUCT OWNER: decide what matters most next; turn a vague wish into a small, testable outcome with a clear 'done'; "
    "prefer the smallest useful step, then improve it from feedback. "
    "SCRUM MASTER: spot blockers, assumptions and risks early and name them plainly; always offer the next small step. "
    "As the client's PM: when a request is unclear or fights the goal, ask one pointed question instead of guessing; present a "
    "scope change as a trade-off (this adds X, we could drop Y) and let the client decide; agree what done means before you "
    "start; give bad news early; recommend one option instead of listing five. Stay conversational and brief, with no jargon "
    "and no lists out loud, and do not create plans, documents or ceremonies unless the client asks.")

def _read(path, limit=FILE_LIMIT, tail=False):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    return text[-limit:] if tail else text[:limit]

def _stamp(*paths):
    out = []
    for p in paths:
        try:
            out.append(os.stat(p).st_mtime)
        except OSError:
            out.append(0)
    return out

def names():
    if not os.path.isdir(DIR):
        return []
    return sorted(n for n in os.listdir(DIR) if os.path.isfile(os.path.join(DIR, n, "project.md")))

def _key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())

def find(spoken):
    """The project a spoken name means ('free cad' -> 'freecad'), or None if it is not a clear match."""
    k = _key(spoken)
    if not k:
        return None
    have = names()
    for n in have:
        if _key(n) == k:
            return n
    near = [n for n in have if _key(n).startswith(k) or k in _key(n)]
    return near[0] if len(near) == 1 else None

SECRET_FILES = "**/.env*"        # what stays private to a project: other projects may have their own accounts (e.g. Supabase)

def repo_of(name):
    """The real code folder a project points at: a 'Repo: /path' line in its project.md (None if it has none)."""
    m = re.search(r"^Repo:\s*(\S.*?)\s*$", _read(os.path.join(DIR, name, "project.md")), re.M)
    return os.path.realpath(os.path.expanduser(m.group(1))) if m else None

class Projects:
    def __init__(self, ctx):
        self.ctx, self.cfg = ctx, ctx.cfg("projects", DEFAULTS)
        self.name, self.part, self.dir, self.stamp = "", None, None, None
        self.repo, self.rules = None, []
        self.refresh_options()

    def lock_secrets(self, active):
        """Hard-block Claude from reading or editing the .env files of every project except the active one."""
        for r in self.rules:
            if r in self.ctx.denied:
                self.ctx.denied.remove(r)
        self.rules = []
        for n in names():
            repo = repo_of(n)
            if n != active and repo:
                self.rules += [f"{tool}(/{repo}/{SECRET_FILES})" for tool in ("Read", "Edit", "Write")]
        for r in self.rules:
            self.ctx.deny(r)

    def refresh_options(self):
        UI[0]["options"][:] = [["", "None"]] + [[n, n] for n in names()]

    def _brain(self):
        return self.ctx.service("brain", "claude")

    def _session_file(self, name):
        return os.path.join(DIR, name, ".session")

    def save_session(self):
        brain = self._brain()
        if self.name and brain is not None and getattr(brain, "session", None):
            with open(self._session_file(self.name), "w") as f:
                f.write(brain.session)

    def paths(self, name):
        d = os.path.join(DIR, name)
        return d, os.path.join(d, "project.md"), os.path.join(d, "notes.md"), os.path.join(DIR, "rules.md")

    def apply(self, name):
        """Make `name` (or no project) the one Hommie is working in: instructions, folder and conversation."""
        ctx, brain = self.ctx, self._brain()
        self.save_session()
        if self.part in ctx.persona_parts:
            ctx.persona_parts.remove(self.part)
        for old in (self.dir, self.repo):
            if old in ctx.dirs:
                ctx.dirs.remove(old)
        self.name, self.part, self.dir, self.stamp, self.repo = name, None, None, None, None
        self.lock_secrets(name)
        if name:
            d, proj, notes, rules = self.paths(name)
            self.dir, self.stamp, self.repo = d, _stamp(proj, rules), repo_of(name)
            where = (f"The project's code lives in {self.repo}: work there, and keep Hommie's own notes in {d}. "
                     if self.repo and os.path.isdir(self.repo) else f"Keep this project's files in {d}. ")
            lines = [f"ACTIVE PROJECT: {name}. {where}Use absolute paths. "
                     "Do not read or change other projects' folders unless the rules below allow it; never copy keys or "
                     "passwords between projects, and never write them into notes.",
                     MINDSET,
                     "Project instructions:\n" + _read(proj)]
            if _read(rules):
                lines.append("Rules between projects:\n" + _read(rules))
            if _read(notes):
                lines.append("Notes so far:\n" + _read(notes, NOTES_TAIL, tail=True))
            lines.append(f"When you learn something lasting about this project (a decision, a measurement, a preference), append "
                         f"one short line to {notes} and mention it in a few words.")
            self.part = "\n".join(lines)
            ctx.persona(self.part)
            ctx.add_dir(d)
            if self.repo and os.path.isdir(self.repo):
                ctx.add_dir(self.repo)
        self.cfg["active"] = name
        if brain is not None:
            brain.session = _read(self._session_file(name), 200) or None if name else None
            brain.close()                                      # the next request starts with this project's instructions

    def sync(self):
        """Pick up a project chosen in the window, or a project.md / rules.md you edited."""
        want = self.cfg["active"]
        if want and want not in names():
            want = ""
        if want != self.name or (want and _stamp(*[self.paths(want)[i] for i in (1, 3)]) != self.stamp):
            self.apply(want)

    def context(self, text):
        self.sync()
        if self.name and MARK in _read(self.paths(self.name)[1]):
            return (f"[Project setup] The project '{self.name}' was just created and {self.paths(self.name)[1]} is still empty. "
                    "Interview your client like a project manager at kickoff: one short question at a time, three or four in all. "
                    "Cover why this project exists and what value it must deliver, what done looks like, what is out of scope, "
                    "and the biggest risk. When you have enough, write project.md (short lines under: Why, Done looks like, "
                    "Out of scope, Risks, without the placeholder; keep every other line already in the file, especially Repo:) "
                    "and give a one-sentence summary.")
        return None

def create(title):
    """Make projects/<slug>/project.md (unfinished, so Hommie interviews you about it). Returns (slug, already_existed)."""
    title = re.sub(r"^(?:called|named|for)\s+", "", title.strip(" .,:;-'\""), flags=re.I)
    title = re.split(r"[.,;!?]|\s+(?:let'?s|and then|and)\b", title, maxsplit=1, flags=re.I)[0].strip()   # a name, not the rest of the sentence
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    if not slug:
        return "", False
    if slug in names():
        return slug, True
    os.makedirs(os.path.join(DIR, slug), exist_ok=True)
    with open(os.path.join(DIR, slug, "project.md"), "w") as f:
        f.write(f"# {title}\n\n{MARK}\n")
    return slug, False

def _add_from_dashboard(ctx, text):
    slug, existed = create(text)
    if not slug:
        return {"note": "Give the project a name first."}
    _p.refresh_options()
    _p.cfg["active"] = slug
    with ctx.lock:
        _p.sync()
    if existed:
        return {"note": f"The {slug} project already exists, so I opened it."}
    return {"note": f"Created the {slug} project. Hommie will ask a few questions to set it up.",
            "ask": f"Let's do the kickoff for {slug}."}      # worded so it can't match the "new project" voice command

def _items(ctx):
    return [{"id": n, "label": n, "sub": "open" if n == _p.name else "", "on": n == _p.name} for n in names()]

def _pick(ctx, ident):
    """A click in the dashboard list: open that project, or close it if it is the open one."""
    if ident not in names():
        return {}
    _p.cfg["active"] = "" if ident == _p.name else ident
    with ctx.lock:
        _p.sync()
    return {"note": f"Now in the {ident} project." if _p.name else f"Closed the {ident} project."}

def _detail(ctx, ident):
    """What the dashboard shows when you open a project's details."""
    if ident not in names():
        return {}
    d, proj, notes, _ = _p.paths(ident)
    text = _read(proj)
    gh = re.search(r"^GitHub:\s*([\w.-]+/[\w.-]+)", text, re.M)
    rows = [["Hommie folder", d], ["Code folder", repo_of(ident) or "none"]]
    if gh:
        rows.append(["GitHub", "https://github.com/" + gh.group(1)])
    return {"title": ident, "rows": rows, "text": text, "notes": _read(notes, NOTES_TAIL, tail=True),
            "open": ident == _p.name}

def _archive(ctx, ident):
    """Take a project off the list without deleting it: its folder moves to projects/_archive/ and can be moved back."""
    if ident not in names():
        return {}
    if ident == _p.name:
        _p.cfg["active"] = ""
        with ctx.lock:
            _p.sync()
    dest, n = os.path.join(DIR, "_archive", ident), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(DIR, "_archive", f"{ident}-{n}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(os.path.join(DIR, ident), dest)
    _p.refresh_options()
    return {"note": f"Archived {ident}. Its folder is in projects/_archive/ if you want it back."}

ADD = {"title": "Projects", "label": "Add", "placeholder": "New project name", "action": _add_from_dashboard,
       "items": _items, "pick": _pick, "detail": _detail, "archive": _archive}

_p = None

def setup(ctx):
    global _p
    os.makedirs(DIR, exist_ok=True)
    p = _p = Projects(ctx)

    def switch(m, text):
        found = find(m.group(1))
        if not found:
            return None                                        # not one of ours: let the brain take it
        p.cfg["active"] = found
        p.sync()
        return f"Okay, we're in the {found} project."

    def new(m, text):
        slug, existed = create(m.group(1))
        if not slug:
            return "What should I call it? Say it like: new project home automation."
        if existed:
            p.cfg["active"] = slug
            p.sync()
            return f"The {slug} project already exists, so I opened it."
        p.refresh_options()
        p.cfg["active"] = slug
        p.sync()
        return None                                            # the brain takes it from here and asks the setup questions

    def leave(m, text):
        if not p.name:
            return "No project is open."
        left, p.cfg["active"] = p.name, ""
        p.sync()
        return f"Closed the {left} project."

    def which(m, text):
        have = names()
        here = f"We're in the {p.name} project." if p.name else "No project is open."
        return here + (" You have: " + ", ".join(have) + "." if have else " You have no projects yet. Say new project, and a name.")

    ctx.add_context(p.context, "projects")
    ctx.command(r"\b(?:new|create|start|make)\s+(?:a\s+)?project\b[\s:,.-]*(.*)", new, "project_new", priority=55)
    ctx.command(r"\b(?:switch to|work on|working on|open|go to|use)\s+(?:the\s+)?(.+?)\s+project\b", switch, "project_switch", priority=55)
    ctx.command(r"\b(?:close|leave|exit)\s+(?:the\s+|this\s+)?(?:current\s+)?project\b", leave, "project_close", priority=55)
    ctx.command(r"\blist (?:my |the |all )?projects\b|\b(?:which|what) project (?:am i|are we|is active)\b", which, "project_which", priority=55)
    ctx.on("start", p.sync)

def teardown(ctx):
    if _p:
        _p.save_session()
