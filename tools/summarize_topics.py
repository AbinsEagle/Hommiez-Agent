#!/home/rona/jarvis/.venv/bin/python
"""Ask Claude (your subscription, via the claude CLI) for a short overview of each Brain topic hub.
Only note titles and the first question of each note are sent, with phone numbers, emails and long ID numbers redacted.
Results go to Brain/_meta/summaries.json; build_brain.py re-applies them to the hubs on every rebuild. Safe to re-run:
topics that already have a summary are skipped (use --redo to rewrite)."""
import json, os, re, subprocess, sys, time
BRAIN = "/home/rona/AbinsAI/Brain"

def redact(t):
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", t)
    t = re.sub(r"\+?\d[\d\s().-]{7,}\d", "[number]", t)
    t = re.sub(r"\b[A-Za-z0-9]{12,}\b", "[id]", t)
    return t

def topic_items(path):
    items = []
    for line in open(path, encoding="utf-8"):
        m = re.match(r"- (\S+) \[\[(.*?)\|(.*?)\]\](?: — (.*))?", line)
        if m:
            items.append((m.group(1), m.group(3), m.group(4) or ""))
    return items

def main():
    redo = "--redo" in sys.argv
    sp = os.path.join(BRAIN, "_meta", "summaries.json")
    sums = json.load(open(sp)) if os.path.exists(sp) else {}
    for fn in sorted(os.listdir(os.path.join(BRAIN, "Topics"))):
        name = fn[:-3]
        full = re.search(r'^title: "(.*)"', open(os.path.join(BRAIN, "Topics", fn), encoding="utf-8").read(), re.M).group(1)
        if full in sums and not redo:
            continue
        items = topic_items(os.path.join(BRAIN, "Topics", fn))
        if not items:
            continue
        step = max(1, len(items) // 70)                       # at most ~70 evenly spread notes per topic
        sample = items[::step][:70]
        listing = "\n".join(f"- {d} | {redact(t)}" + (f" | asked: {redact(q)[:110]}" if q and q != 'cdi' else "") for d, t, q in sample)
        prompt = (f"Below are {len(sample)} of {len(items)} notes (date | title | first question) from one person's saved ChatGPT "
                  f"conversations, grouped under the topic \"{full}\".\n\n{listing}\n\n"
                  "Write a short overview of this topic for their personal knowledge base: 3 to 5 plain sentences covering the "
                  "main sub-themes, what they seem to be working toward, and the time span (first to last date). Use ONLY what the "
                  "titles and questions show; do not add outside facts or guesses. No headings, bullets or markdown, no preamble.")
        t0 = time.time()
        r = subprocess.run(["claude", "-p", prompt, "--model", "sonnet", "--effort", "low", "--disable-slash-commands",
                            "--strict-mcp-config", "--output-format", "json"], capture_output=True, text=True, cwd="/home/rona/jarvis", timeout=300)
        try:
            text = json.loads(r.stdout)["result"].strip()
        except Exception:
            print(f"FAILED {full}: {r.stdout[:100]} {r.stderr[:100]}", flush=True)
            continue
        sums[full] = text
        json.dump(sums, open(sp, "w"), indent=1, ensure_ascii=False)
        print(f"[{len(sums)}] {full} ({len(items)} notes, {time.time() - t0:.0f}s)", flush=True)
    print("done", len(sums), "summaries")

if __name__ == "__main__":
    main()
