"""EXAMPLE plugin / template. Files starting with an underscore are ignored by the loader.
Copy this to e.g. plugins/cmd_time.py (no underscore) and Hommie picks it up next start. Delete the copy to remove it.

This one adds a local command: "what time is it" is answered instantly, without asking Claude."""
import datetime

NAME = "cmd_time"           # optional; defaults to the file name
PRIORITY = 50               # optional; load order, lower first
REQUIRES = []               # optional; pip modules that must be importable, else the plugin is skipped
DEFAULTS = {"use_24h": False}   # optional; saved in settings.json under the plugin name

def setup(ctx):
    cfg = ctx.cfg(NAME, DEFAULTS)

    def tell_time(match, text):
        now = datetime.datetime.now()
        return "It's " + now.strftime("%H:%M" if cfg["use_24h"] else "%-I:%M %p") + "."

    ctx.command(r"\bwhat('?s| is) the time\b|\bwhat time is it\b", tell_time, NAME)
    # Other things a plugin can do (see hommie/core.py):
    #   ctx.on("heard", lambda text, **kw: ...)  also "reply", "state", "say"   react to events (e.g. a UI, a logger)
    #   ctx.provide("tts", obj, "name", priority)                      add or replace a voice / brain / mode
    #   ctx.persona("...")  ctx.allow("Bash(git status:*)")  ctx.add_dir("/some/folder")   give Claude new abilities
