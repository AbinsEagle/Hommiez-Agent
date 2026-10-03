#!/home/rona/jarvis/.venv/bin/python
"""Build ~/AbinsAI/Brain from the markdown scattered around ~/AbinsAI. Read-only on the sources; writes only into Brain/.

  1. Collect: one canonical note per conversation (full copy in md_output > old ChatGPT-exports > condensed wiki_output)
              + standalone notes + project docs. Everything else under ~/AbinsAI is a derived copy and is skipped.
  2. Structure: clean frontmatter, topics (reusing your build_kb.py TOPICS), keyword tags (TF-IDF), related-note links
              (only between genuinely similar notes), your original text kept intact, provenance to every source copy.
  3. Hubs: Topics/<topic>.md and Categories index. Optional summaries written later live in _meta/summaries.json and
              are re-applied on every rebuild.
  4. Search index: _index/brain.sqlite (SQLite FTS5, BM25).
No language model is used here, so nothing in the notes is invented.
"""
import ast, collections, datetime, hashlib, json, math, os, re, shutil, sqlite3, sys, time

ROOT = "/home/rona/AbinsAI"
OUT = os.path.join(ROOT, "Brain")
SRC_FULL = os.path.join(ROOT, "ChatGPTfiles latest/md_output")
SRC_OLD = os.path.join(ROOT, "AbinsAI old/ChatGPT-exports")
SRC_WIKI = os.path.join(ROOT, "ChatGPTfiles latest/wiki_output")
SRC_V0 = os.path.join(ROOT, "ChatGPTfiles latest/Abins Brain Wiki V0")      # only used for its category folders
SRC_NOTES = os.path.join(ROOT, "AbinsAI old")                                 # root-level standalone notes only
PROJECT_DOCS = {
    "Service Sense OS": [os.path.join(ROOT, "Service Sense OS", p) for p in ("README.md", "BUILD_LOG.md", "docs/PRD.md", "docs/pinmap.md")],
    "Fishbowl": [os.path.join(ROOT, "Fishbowl", p) for p in ("README.md", "README-EN.md")],
}
TOPICS_FILE = os.path.join(ROOT, "AbinsAI old/build_kb.py")

STOP = set("""a about above after again all also am an and any are as at be because been before being below between both but by can
could did do does doing down during each few for from further get got had has have having he her here hers him his how i if in into
is it its just let like me more most my no nor not now of off on once only or other our out over own please same she should so some
such than that the their them then there these they this those through to too under until up us use using very was we were what when
where which while who whom why will with would you your yours yes okay ok thanks thank hi hello hey want need know make made give
tell show help way also one two three new good best better tried try etc can't don't isn't it's i'm i've that's there's let's
chatgpt gpt assistant user answer question questions sure certainly ask asked says said using used etc however also may might must
shall take takes need needs want wants lot much many really actually something anything everything someone""".split())

def read(p):
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""

def key(name):
    return re.sub(r"\W+", "", re.sub(r"\.md$", "", name).lower())

def split_frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    return (m.group(1), text[m.end():]) if m else ("", text)

def fm_value(fm, k):
    m = re.search(rf"^{k}:\s*\"?(.*?)\"?\s*$", fm, re.M)
    return m.group(1).strip() if m else ""

def title_from(stem, text):
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else re.sub(r"^\d{4}-\d{2}-\d{2}_", "", stem)).strip(" .")

def date_from(stem, fm, text):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", stem) or re.search(r"(\d{4}-\d{2}-\d{2})", fm or text[:300])
    return m.group(1) if m else ""

def normalise_conversation(body):
    """Return the conversation as '**You:** ... / **ChatGPT:** ...' with the exact original text."""
    body = re.sub(r"^\s*#\s+.+\n", "", body, count=1)
    body = re.sub(r"^\s*\*\*Date:\*\*.*\n", "", body, count=1, flags=re.M)
    body = re.sub(r"^\s*---\s*$", "", body, count=1, flags=re.M)
    body = re.sub(r"^\*\*(User|You):\*\*", "**You:**", body, flags=re.M)
    body = re.sub(r"^\*\*(Assistant|ChatGPT):\*\*", "**ChatGPT:**", body, flags=re.M)
    return body.strip()

def first_question(conv, n=140):
    m = re.search(r"\*\*You:\*\*\s*(.+?)(?=\n\*\*ChatGPT:\*\*|\Z)", conv, re.S)
    q = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return q[:n] + ("…" if len(q) > n else "")

def safe_name(s):
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", s).strip(" .")[:150] or "untitled"

# ── 1. collect ─────────────────────────────────────────────────────────────────

def collect():
    notes, copies = {}, collections.defaultdict(list)
    for tier, folder, label in ((0, SRC_FULL, "md_output"), (1, SRC_OLD, "ChatGPT-exports"), (2, SRC_WIKI, "wiki_output")):
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(folder, fn)
            k = key(fn)
            copies[k].append(os.path.relpath(p, ROOT))
            if k not in notes:
                notes[k] = {"tier": tier, "path": p, "fn": fn, "kind": "conversation"}
    v0_cat = {}
    for cat in os.listdir(SRC_V0):
        d = os.path.join(SRC_V0, cat)
        if os.path.isdir(d) and not cat.startswith("."):
            for fn in os.listdir(d):
                v0_cat[key(fn)] = cat
    for fn in sorted(os.listdir(SRC_NOTES)):
        p = os.path.join(SRC_NOTES, fn)
        if fn.endswith(".md") and os.path.isfile(p):
            k = "note" + key(fn)
            notes[k] = {"tier": 0, "path": p, "fn": fn, "kind": "note"}
            copies[k].append(os.path.relpath(p, ROOT))
    for proj, files in PROJECT_DOCS.items():
        for p in files:
            if os.path.exists(p):
                k = "proj" + key(proj + os.path.basename(p))
                notes[k] = {"tier": 0, "path": p, "fn": os.path.basename(p), "kind": "project", "project": proj}
                copies[k].append(os.path.relpath(p, ROOT))
    return notes, copies, v0_cat

def load_topics():
    tree = ast.parse(read(TOPICS_FILE))
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "TOPICS":
            return ast.literal_eval(n.value)
    return []

def topic_scores(topics, fn, text):
    low_fn, low = fn.lower(), text[:2500].lower()
    out = {}
    for slug, name, fk, ck in topics:
        s = 3 * sum(k in low_fn for k in fk) + sum(k in low for k in ck)
        if s:
            out[name] = s
    return out

def tokens(text):
    return [t for t in re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower()) if t not in STOP and not re.search(r"\d{3,}", t)]

# ── build ──────────────────────────────────────────────────────────────────────

def build():
    t0 = time.time()
    notes, copies, v0_cat = collect()
    topics = load_topics()
    docs = []
    for k, n in notes.items():
        raw = read(n["path"])
        fm, body = split_frontmatter(raw)
        stem = n["fn"][:-3]
        title = title_from(stem, body)
        if n["kind"] == "project":
            title = f"{n['project']} — {stem}" + (" (Chinese)" if re.search(r"[\u4e00-\u9fff]", stem) else "")
        date = date_from(stem, fm, body)
        if n["kind"] == "conversation":
            conv = normalise_conversation(body)
            condensed = n["tier"] == 2
        else:
            conv, condensed = body.strip(), False
        if n["kind"] == "note" and len(conv) < 400:
            continue                                        # skip scratch notes like "create a link"
        docs.append({"k": k, "n": n, "title": title, "date": date, "conv": conv, "condensed": condensed,
                     "stem": safe_name(((date + "_") if date and n["kind"] == "conversation" else "") + title)})
    # unique file names
    seen = collections.Counter()
    for d in docs:
        seen[d["stem"]] += 1
        if seen[d["stem"]] > 1:
            d["stem"] += f" ({seen[d['stem']]})"

    # topics, tags (TF-IDF), similarity
    df, tf = collections.Counter(), {}
    for d in docs:
        toks = tokens(d["title"]) * 3 + tokens(d["conv"][:20000])
        tf[d["k"]] = collections.Counter(toks)
        df.update(set(tf[d["k"]]))
        d["topic_scores"] = topic_scores(topics, d["n"]["fn"], d["conv"])
    N = len(docs)
    vec = {}
    for d in docs:
        w = {t: (1 + math.log(c)) * math.log(N / df[t]) for t, c in tf[d["k"]].items() if df[t] >= 2 and df[t] < N * 0.2}
        top = sorted(w.items(), key=lambda x: -x[1])[:40]
        norm = math.sqrt(sum(v * v for _, v in top)) or 1
        vec[d["k"]] = {t: v / norm for t, v in top}
        d["tags"] = [t for t, _ in top[:6]]
    inv = collections.defaultdict(list)
    for d in docs:
        for t, v in vec[d["k"]].items():
            inv[t].append((d["k"], v))
    by_key = {d["k"]: d for d in docs}
    for d in docs:
        acc, shared = collections.Counter(), collections.Counter()
        for t, v in vec[d["k"]].items():
            if len(inv[t]) > 60:
                continue
            for k2, v2 in inv[t]:
                if k2 != d["k"]:
                    acc[k2] += v * v2
                    shared[k2] += 1
        d["related"] = [by_key[k2] for k2, s in acc.most_common(8) if s >= 0.22 and shared[k2] >= 3][:5]
        ranked = sorted(d["topic_scores"].items(), key=lambda x: -x[1])
        d["topics"] = [ranked[0][0]] + [t for t, s in ranked[1:3] if s >= max(3, ranked[0][1] * 0.6)] if ranked else (["Miscellaneous"] if d["n"]["kind"] == "conversation" else [])
        d["category"] = v0_cat.get(d["k"], "") if d["n"]["kind"] == "conversation" else ""

    # write
    summaries = {}
    sp = os.path.join(OUT, "_meta", "summaries.json")
    if os.path.exists(sp):
        summaries = json.load(open(sp))
    for sub in ("Conversations", "Notes", "Projects", "Topics", "_index"):
        shutil.rmtree(os.path.join(OUT, sub), ignore_errors=True)
    for d in docs:
        n = d["n"]
        sub = {"conversation": "Conversations", "note": "Notes", "project": os.path.join("Projects", n.get("project", ""))}[n["kind"]]
        if n["kind"] == "conversation":
            sub = os.path.join(sub, d["category"] or "Uncategorised")
        d["rel"] = os.path.join(sub, d["stem"] + ".md")
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)
        src_list = copies[d["k"]]
        fm = ["---", f"title: \"{d['title'].replace(chr(34), chr(39))}\""]
        if d["date"]:
            fm.append(f"date: {d['date']}")
        fm += [f"type: {n['kind']}"]
        if d["category"]:
            fm.append(f"category: {d['category']}")
        if d["topics"]:
            fm.append("topics: [" + ", ".join(f'"{t}"' for t in d["topics"]) + "]")
        if d["tags"]:
            fm.append("tags: [" + ", ".join(d["tags"]) + "]")
        fm.append("sources:\n" + "\n".join(f"  - \"{s}\"" for s in src_list))
        fm.append("---")
        head = [f"# {d['title']}", ""]
        meta = []
        if d["topics"]:
            meta.append("**Topics:** " + " · ".join(f"[[{safe_name(t)}]]" for t in d["topics"]))
        if d["related"]:
            meta.append("**Related:** " + " · ".join(f"[[{r['stem']}|{r['title']}]]" for r in d["related"]))
        if d["condensed"]:
            meta.append("> ⚠ Only a condensed copy of this conversation survives (the full export is missing).")
        body = "\n".join(fm) + "\n\n" + "\n\n".join(head + meta + [("## Conversation\n\n" if n["kind"] == "conversation" else "") + d["conv"]]) + "\n"
        with open(os.path.join(OUT, d["rel"]), "w", encoding="utf-8") as f:
            f.write(body)

    # topic hubs
    by_topic = collections.defaultdict(list)
    for d in docs:
        for t in d["topics"]:
            by_topic[t].append(d)
    os.makedirs(os.path.join(OUT, "Topics"), exist_ok=True)
    for t, ds in by_topic.items():
        ds.sort(key=lambda d: d["date"])
        lines = ["---", f"title: \"{t}\"", "type: topic", f"notes: {len(ds)}", "---", f"# {t}", ""]
        lines += ["<!-- summary:start -->", summaries.get(t, "_No summary yet._"), "<!-- summary:end -->", "", f"## Notes ({len(ds)})", ""]
        for d in ds:
            q = first_question(d["conv"]) if d["n"]["kind"] == "conversation" else ""
            lines.append(f"- {d['date'] or '—'} [[{d['stem']}|{d['title']}]]" + (f" — {q}" if q else ""))
        with open(os.path.join(OUT, "Topics", safe_name(t) + ".md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # index (SQLite FTS5)
    os.makedirs(os.path.join(OUT, "_index"), exist_ok=True)
    dbp = os.path.join(OUT, "_index", "brain.sqlite")
    db = sqlite3.connect(dbp)
    db.execute("CREATE VIRTUAL TABLE notes USING fts5(title, topics, tags, body, path UNINDEXED, date UNINDEXED, kind UNINDEXED, tokenize='porter unicode61')")
    db.executemany("INSERT INTO notes VALUES (?,?,?,?,?,?,?)",
                   [(d["title"], " ".join(d["topics"]), " ".join(d["tags"]), d["conv"], d["rel"], d["date"], d["n"]["kind"]) for d in docs])
    db.commit(); db.close()

    # manifest + map
    os.makedirs(os.path.join(OUT, "_meta"), exist_ok=True)
    manifest = {d["rel"]: {"sources": copies[d["k"]], "topics": d["topics"], "tags": d["tags"], "condensed": d["condensed"]} for d in docs}
    json.dump({"built": datetime.datetime.now().isoformat(timespec="seconds"), "notes": manifest}, open(os.path.join(OUT, "_meta", "manifest.json"), "w"), indent=1)
    kinds = collections.Counter(d["n"]["kind"] for d in docs)
    cats = collections.Counter(d["category"] or "Uncategorised" for d in docs if d["n"]["kind"] == "conversation")
    readme = ["---", "type: map", "---", "# Abins Brain", "",
              f"Built {datetime.date.today().isoformat()} from everything under `~/AbinsAI` (originals untouched). "
              f"{kinds['conversation']} conversations, {kinds['note']} standalone notes, {kinds['project']} project docs.", "",
              "## Topics", ""]
    readme += [f"- [[{safe_name(t)}]] ({len(ds)})" for t, ds in sorted(by_topic.items(), key=lambda x: -len(x[1]))]
    readme += ["", "## Folders", "", "- `Conversations/<Category>/` your ChatGPT conversations, full text, one note each",
               "- `Notes/` standalone notes · `Projects/` project docs · `Topics/` hub notes",
               "- `_index/brain.sqlite` search index (used by Hommie) · `_meta/` provenance of every note", "", "## Categories", ""]
    readme += [f"- {c}: {n}" for c, n in cats.most_common()]
    open(os.path.join(OUT, "README.md"), "w", encoding="utf-8").write("\n".join(readme) + "\n")
    print(f"built {len(docs)} notes ({dict(kinds)}) in {time.time() - t0:.1f}s; topics: {len(by_topic)}; "
          f"condensed-only: {sum(d['condensed'] for d in docs)}; with related links: {sum(bool(d['related']) for d in docs)}; "
          f"no topic: {sum(not d['topics'] for d in docs)}")

if __name__ == "__main__":
    build()
