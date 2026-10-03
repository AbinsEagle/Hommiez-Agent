"""Folders Hommie may read and edit. Add a folder in settings.json under "proj_workspace" -> "dirs"."""
PRIORITY = 20
DEFAULTS = {"dirs": ["/home/rona/AbinsAI", "/home/rona/hommiez-fw"]}

def setup(ctx):
    cfg = ctx.cfg("proj_workspace", DEFAULTS)
    for d in cfg["dirs"]:
        ctx.add_dir(d)
    ctx.persona("The user's projects live in: " + ", ".join(cfg["dirs"]) + ". Use absolute paths there.")
