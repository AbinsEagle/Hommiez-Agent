"""Images: paste, drop or attach pictures in the Hommie window and she looks at them, and a folder of images she may open
any time ("what's in my latest screenshot?"). Pasted pictures are saved into that folder, so they stay available.
Works with the Claude brain (it opens the files itself); the Groq and Gemini brains read text only.
Delete this file and the window loses its attach button; nothing else changes."""
import base64, binascii, os, re, time
from hommie.core import HERE

PRIORITY = 24
DEFAULTS = {"dir": os.path.join(HERE, "images")}
UI_TITLE = "Images"
UI = [{"key": "dir", "label": "Folder of images Hommie can open (restart to change)", "type": "text"}]
MAX_BYTES, MAX_FILES = 10_000_000, 6
KINDS = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "gif": b"GIF8"}      # webp is checked separately (RIFF....WEBP)

def folder(ctx):
    return os.path.realpath(os.path.expanduser(ctx.cfg("images", DEFAULTS)["dir"]))

def _kind(raw):
    for ext, magic in KINDS.items():
        if raw.startswith(magic):
            return ext
    return "webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else None

def save(ctx, data_urls):
    """Save pasted/dropped pictures (data: URLs) into the folder. Returns the file paths. Anything that is not a real
    PNG, JPEG, GIF or WebP, or is too big, is refused; names are made here, never taken from the browser."""
    d = folder(ctx)
    os.makedirs(d, exist_ok=True)
    out, stamp = [], time.strftime("%Y%m%d-%H%M%S")
    for i, url in enumerate(list(data_urls)[:MAX_FILES]):
        m = re.fullmatch(r"data:image/[a-z+.-]+;base64,([A-Za-z0-9+/=]+)", str(url))
        if not m or len(m.group(1)) > MAX_BYTES * 4 // 3 + 8:
            raise ValueError("that is not a picture I can take, or it is over 10 MB")
        try:
            raw = base64.b64decode(m.group(1), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("that picture is damaged")
        ext = _kind(raw)
        if ext is None:
            raise ValueError("only PNG, JPEG, GIF and WebP pictures are supported")
        path = os.path.join(d, f"pasted-{stamp}-{i + 1}.{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        out.append(path)
    return out

def setup(ctx):
    d = folder(ctx)
    os.makedirs(d, exist_ok=True)
    ctx.add_dir(d)
    ctx.allow(f"Read(/{d}/**)")
    ctx.persona(f"The user's pictures are in {d}. When they paste, drop or mention an image, screenshot or photo, open it with "
                "the Read tool and look at it before answering; for 'the latest one' use ls -t in that folder. When a message "
                "says an image is attached, its path is in the message.")
