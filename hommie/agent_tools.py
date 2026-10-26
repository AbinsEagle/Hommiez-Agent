"""Tools for the chat-API brains (Groq, Gemini) so they can do what Claude does: read/search/edit files, run shell
commands, search the Brain. Same rules as brain_claude: reads and edits inside the working folders are automatic,
shell commands run only if they match an allow rule (ctx.allowed + base_allow), ctx.deny() rules (e.g. a read-only
vault) are final, and anything else is confirmed out loud, one action at a time. Files that hold secrets are never
read, because the text would be sent to a third-party API."""
import glob as _glob, json, os, re, subprocess
from hommie.core import HERE

OUT_MAX = 6000
B = r"(^|[/\s'\"=])"                                   # start of a path or of a word in a shell command
SECRET = re.compile(B + r"(\.ssh|\.gnupg|\.aws|\.password-store)(/|$|\s)|" + B + r"\.env\S*|" + B + r"\.(bashrc|bash_profile|profile|zshrc|netrc)(\s|$)"
                    r"|auth\.json|id_(rsa|ed25519|ecdsa)|telegram_approval|" + B + r"credentials", re.I)
SHELL_META = re.compile(r"[;&|`$<>\n()]")

def _schema(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

S = {"type": "string"}
SCHEMAS = [
    _schema("read_file", "Read a text file (numbered lines).", {"path": S, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
    _schema("list_dir", "List a folder's files and subfolders.", {"path": S}, ["path"]),
    _schema("find_files", "Find files by glob pattern, e.g. '**/*.py', under a folder.", {"pattern": S, "path": S}, ["pattern"]),
    _schema("grep", "Search file contents for a regular expression under a file or folder.", {"pattern": S, "path": S}, ["pattern"]),
    _schema("write_file", "Create or overwrite a file with the given content.", {"path": S, "content": S}, ["path", "content"]),
    _schema("edit_file", "Replace one exact piece of text in a file with new text (old_text must match exactly once).",
            {"path": S, "old_text": S, "new_text": S}, ["path", "old_text", "new_text"]),
    _schema("bash", "Run a shell command. Give a short plain-English description of what it does.",
            {"command": S, "description": S}, ["command"]),
    _schema("search_brain", "Search the user's personal knowledge base (their past ChatGPT conversations, projects, topics).",
            {"query": S}, ["query"]),
]

class ToolBox:
    def __init__(self, ctx, base_allow):
        self.ctx, self.base_allow = ctx, base_allow

    # ---- permission helpers
    def _path(self, p):
        return os.path.realpath(os.path.join(HERE, os.path.expanduser(str(p or "."))))

    def _in_scope(self, path):
        return any(path == d or path.startswith(os.path.realpath(d) + os.sep) for d in [HERE] + list(self.ctx.dirs))

    def _protected(self, tool, path):
        for rule in self.ctx.denied:                       # same rule shape brain_claude uses: Edit(//abs/path/**)
            m = re.match(r"(\w+)\(/(/.*?)/?\*\*\)$", rule)
            if m and m.group(1) == tool and path.startswith(os.path.realpath(m.group(2)) + os.sep):
                return True
        return False

    def _confirm(self, spoken, shown):
        self.ctx.log(f"blocked: {shown}")
        self.ctx.say(f"I need your permission to run this: {spoken}. Say yes to confirm.")
        return self.ctx.ask_confirm()

    def _file_gate(self, tool, path, writing):
        """None = go ahead, else the refusal text to give the model."""
        if SECRET.search(path):
            return "Refused: that file holds secrets and is never shared."
        if writing and self._protected("Edit" if tool == "edit_file" else "Write", path):
            return "Refused: that location is protected and cannot be changed."
        if not self._in_scope(path) and not self._confirm(f"{tool.replace('_', ' ')} {os.path.basename(path)}", f"{tool}: {path}"):
            return "The user said no. Do not retry this or ask again; use another approach or just say you could not."
        return None

    def _bash_allowed(self, cmd):
        if SHELL_META.search(cmd):
            return False
        rules = [r for r in list(self.base_allow) + list(self.ctx.allowed) if r.startswith("Bash(")]
        for r in rules:
            body = r[5:-1]
            if body.endswith(":*"):
                pre = body[:-2]
                if cmd == pre or cmd.startswith(pre + " "):
                    return True
            elif cmd == body:
                return True
        return False

    # ---- dispatch
    def run(self, name, args):
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return f"Unknown tool {name}."
        try:
            out = fn(**{k: v for k, v in (args or {}).items()})
        except TypeError as e:
            return f"Bad arguments: {e}"
        except Exception as e:                                # a tool failing is information for the model, not a crash
            return f"Error: {type(e).__name__}: {e}"
        out = out if isinstance(out, str) else json.dumps(out)
        self.ctx.log(f"tool {name}: {out[:80]!r}")
        return out if len(out) <= OUT_MAX else out[:OUT_MAX] + "\n... (cut off)"

    # ---- tools
    def t_read_file(self, path, offset=0, limit=300):
        p = self._path(path)
        if (r := self._file_gate("read_file", p, False)):
            return r
        with open(p, "r", errors="replace") as f:
            lines = f.read().splitlines()
        offset, limit = max(0, int(offset or 0)), min(int(limit or 300), 600)
        return "\n".join(f"{i + 1}\t{l}" for i, l in enumerate(lines[offset:offset + limit], offset)) or "(empty file)"

    def t_list_dir(self, path="."):
        p = self._path(path)
        if (r := self._file_gate("list_dir", p, False)):
            return r
        return "\n".join(sorted(e + ("/" if os.path.isdir(os.path.join(p, e)) else "") for e in os.listdir(p))[:200]) or "(empty)"

    def t_find_files(self, pattern, path="."):
        p = self._path(path)
        if (r := self._file_gate("find_files", p, False)):
            return r
        hits = [h for h in _glob.glob(os.path.join(p, pattern), recursive=True) if not SECRET.search(h)]
        return "\n".join(sorted(hits)[:100]) or "No files matched."

    def t_grep(self, pattern, path="."):
        p = self._path(path)
        if (r := self._file_gate("grep", p, False)):
            return r
        res = subprocess.run(["grep", "-rIn", "-m", "5", "--exclude-dir=.venv", "--exclude-dir=.git", "--exclude-dir=node_modules",
                              "--exclude=.env*", "--exclude=.bashrc", "-E", "--", pattern, p],
                             capture_output=True, text=True, timeout=20)
        return "\n".join(res.stdout.splitlines()[:60]) or "No matches."

    def t_write_file(self, path, content):
        p = self._path(path)
        if (r := self._file_gate("write_file", p, True)):
            return r
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return f"Wrote {len(content)} characters to {p}."

    def t_edit_file(self, path, old_text, new_text):
        p = self._path(path)
        if (r := self._file_gate("edit_file", p, True)):
            return r
        with open(p, "r") as f:
            data = f.read()
        n = data.count(old_text)
        if n != 1:
            return f"old_text matched {n} times; it must match exactly once. Read the file and use a more exact piece."
        with open(p, "w") as f:
            f.write(data.replace(old_text, new_text))
        return f"Edited {p}."

    def t_bash(self, command, description=""):
        cmd = command.strip()
        if SECRET.search(cmd):
            return "Refused: that command touches a file that holds secrets."
        if not self._bash_allowed(cmd) and not self._confirm(description or "a shell command", f"Bash: {cmd}"):
            return "The user said no. Do not retry this or ask again; use another approach or just say you could not."
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30, cwd=HERE)
        return (res.stdout + res.stderr).strip() or f"(no output, exit code {res.returncode})"

    def t_search_brain(self, query):
        mod = self.ctx.plugins.get("brain_wiki", (None, ""))[0]
        if mod is None:
            return "The Brain is not loaded."
        hits = mod.Brain(self.ctx.cfg("brain_wiki")).search(query, 6)
        root = self.ctx.cfg("brain_wiki")["path"]
        return "\n".join(f"{t} ({d or 'n/d'}): {re.sub(r'[\ue000-\uf8ff\n]+', ' ', s)[:240]}  [file: {os.path.join(root, p)}]"
                         for _, t, d, p, s in hits) or "Nothing found."
