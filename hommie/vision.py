"""Eyes for the screen guide: take a screenshot (spectacle, KDE), tell when the screen has changed and settled, and ask
a vision model (Gemini) where the next thing to click is. Only called when the user asks for a guide; a screenshot is
sent to the model only for each step, never in the background. Screenshots live in the runtime tmpfs and are deleted."""
import base64, io, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.request
from PIL import Image, ImageChops
from hommie.core import HERE
from hommie.keys import api_key

URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
TMP = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()

def capture():
    """Full-screen screenshot as a PIL image (spectacle ~1 s). Raises RuntimeError if it fails."""
    path = os.path.join(TMP, f"hommie-shot-{os.getpid()}.png")
    try:
        subprocess.run(["spectacle", "-b", "-n", "-f", "-o", path], check=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with Image.open(path) as im:
            return im.convert("RGB")
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"screenshot failed: {e}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

def thumb(img):
    """Small grayscale signature of a screenshot, for cheap change detection."""
    return img.convert("L").resize((240, 135), Image.BILINEAR)

def changed(a, b, threshold=10):
    """Fraction (0..1) of the picture that differs noticeably between two thumbnails."""
    d = ImageChops.difference(a, b).point(lambda v: 255 if v > threshold else 0)
    return d.histogram()[255] / (a.width * a.height)

PROMPT = """You are a screen guide that teaches the user to use any software, one click at a time. Below is a screenshot of \
their screen ({w}x{h}). The user's goal: "{goal}".
Steps you already gave: {history}
What just happened: {event}

Decide the ONE next action and reply with ONLY a JSON object, no other text:
{{"status": "next" | "done" | "blocked", "say": "...", "label": "...", "box": {box_fmt} or null}}
- say: at most 20 words, spoken aloud, plain words: what to click or press and where it is ("Click the Part Design tab at the top").
- label: 2 to 5 words shown on screen next to the target ("Part Design tab").
- box: a TIGHT bounding box around the exact control to use, {box_desc}. null if the action has no \
target (press a key combination, type text).
- If the control you need is not visible (inside a closed menu, another tab, a collapsed panel), the next step is whatever reveals it. \
If the user just opened a menu, submenu or dialog, point at the item inside it.
- If the screen shows the user did something other than the last step, give a step that gets them back on track, starting "That's not it,".
- status "done" when the goal is complete; "blocked" only if the screen cannot be used for this (locked, wrong program) and say why.
- Only point at things you can actually see. Never invent controls."""

def ask(img, goal, history, event, model="gemini-3.5-flash"):
    """-> (dict with status/say/label/box, None) or (None, error text to say aloud)."""
    key = api_key("GEMINI_API_KEY")
    if not key:
        return None, "I need a Gemini key to look at your screen. Add GEMINI_API_KEY to your bashrc."
    small = img.copy()
    small.thumbnail((1280, 1280))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    prompt = PROMPT.format(w=img.width, h=img.height, goal=goal, event=event, history="; ".join(history) if history else "none yet",
                           box_fmt="[ymin, xmin, ymax, xmax]", box_desc="normalized 0-1000 with (0,0) at the top-left")
    body = {"model": model, "reasoning_effort": "low", "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}]}]}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "hommie/1.0"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = json.load(r)["choices"][0]["message"]["content"]
            break
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt == 1:
                time.sleep(2)
                continue
            return None, {429: "Gemini says I'm out of free requests for now. Try again in a minute.",
                          503: "Gemini is overloaded right now. Try again in a moment."}.get(e.code, f"Gemini gave an error, number {e.code}.")
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
            if attempt == 1:                                    # a hung or dropped request: one quick retry
                continue
            return None, "I couldn't reach Gemini to look at your screen."
    return parse(text)

def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        res = json.loads(m.group(0))
        res.setdefault("status", "next")
        box = res.get("box")
        if box and (len(box) != 4 or not all(isinstance(v, (int, float)) for v in box)):
            res["box"] = None
        return res, None
    except (AttributeError, ValueError):
        return None, "I couldn't make sense of what I saw. Try again."

def ask_claude(img, goal, history, event, model="sonnet"):
    """Same question, answered by Claude (your claude.ai login) reading the screenshot with its Read tool."""
    if not shutil.which("claude"):
        return None, "The claude command isn't available to look at your screen."
    small = img.copy()
    small.thumbnail((1280, 1280))
    w, h = small.size
    path = os.path.join(TMP, f"hommie-vision-{os.getpid()}.jpg")
    small.save(path, "JPEG", quality=88)
    prompt = (f"First use the Read tool to look at the screenshot file {path}. " +
              PROMPT.format(w=w, h=h, goal=goal, event=event, history="; ".join(history) if history else "none yet",
                            box_fmt="[x1, y1, x2, y2]", box_desc=f"in PIXELS of that {w}x{h} image, (0,0) at the top-left, x to the right") +
              "\nReply with only the JSON object.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}      # never let an API key take over billing
    try:
        out = subprocess.run(["claude", "-p", "--model", model, "--effort", "low", "--strict-mcp-config", "--disable-slash-commands",
                              "--allowedTools", "Read", "--add-dir", TMP],
                             input=prompt, capture_output=True, text=True, timeout=120, cwd=HERE, env=env)   # prompt on stdin: the flags above take lists
    except (OSError, subprocess.SubprocessError):
        return None, "Claude didn't answer in time."
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    res, err = parse(out.stdout)
    if res and res.get("box"):             # pixels -> the 0-1000 [ymin, xmin, ymax, xmax] grid the overlay code uses
        x1, y1, x2, y2 = res["box"]
        res["box"] = [y1 / h * 1000, x1 / w * 1000, y2 / h * 1000, x2 / w * 1000]
    if err and re.search(r"limit|quota", out.stdout, re.I):
        err = "Claude says I've hit my usage limit for now."
    return res, err

def look(img, goal, history, event, model):
    """model is 'claude:sonnet', 'claude:haiku' or a Gemini model name."""
    if model.startswith("claude:"):
        return ask_claude(img, goal, history, event, model.split(":", 1)[1])
    return ask(img, goal, history, event, model)

def to_pixels(box, w, h):
    """Gemini's [ymin, xmin, ymax, xmax] on a 0-1000 grid -> [x1, y1, x2, y2] in screenshot pixels, clamped."""
    ymin, xmin, ymax, xmax = (max(0, min(1000, v)) for v in box)
    return [xmin / 1000 * w, ymin / 1000 * h, xmax / 1000 * w, ymax / 1000 * h]
