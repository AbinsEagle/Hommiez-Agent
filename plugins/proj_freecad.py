"""FreeCAD know-how: Hommie writes a FreeCAD Python script for what you describe and runs it in ONE open FreeCAD window,
so a model can be changed again and again without a new window each time (freecad/send.py + freecad/hommie_bridge.py).
Models land in ./freecad. FreeCAD is the Flatpak org.freecad.FreeCAD."""
import os, subprocess, time
from hommie.core import HERE

PRIORITY = 22
DEFAULTS = {"dir": os.path.join(HERE, "freecad")}

def _alive():
    try:
        with open(os.path.join(DEFAULTS["dir"], "live", "alive")) as f:
            return time.time() - float(f.read()) < 5
    except (OSError, ValueError):
        return False

def _open(ctx=None):
    """Quick action: open FreeCAD once, with the bridge that lets Hommie edit the model in this same window."""
    if _alive():
        return "FreeCAD is already open."
    subprocess.Popen(["flatpak", "run", "org.freecad.FreeCAD", os.path.join(DEFAULTS["dir"], "hommie_bridge.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return "Opening FreeCAD. Ask Hommie for a part once it's up."

QUICK = [{"id": "open", "label": "FreeCAD", "action": _open, "status": lambda ctx: "on" if _alive() else "open"}]

def setup(ctx):
    cfg = ctx.cfg("freecad", DEFAULTS)
    d = cfg["dir"]
    os.makedirs(d, exist_ok=True)
    ctx.add_dir(d)
    ctx.allow(f"Bash(python3 {d}/send.py:*)")
    ctx.persona(
        f"CAD work uses FreeCAD, kept open in a single window. Keep one script per model at {d}/<name>.py and change that same "
        f"file for every edit; never make a copy per change. Run it with: python3 {d}/send.py {d}/<name>.py. "
        "That starts FreeCAD only if it is not already open and runs the script in the open window. Never launch FreeCAD any "
        "other way, since that opens another window. In the script `doc` is the open document, and FreeCAD, App, Gui are ready. "
        "Make the script rebuild the whole model each run: first remove every object (for o in list(doc.Objects): "
        "doc.removeObject(o.Name)), then build with Part (makeBox, makeCylinder, cut, fuse, fillet) or PartDesign, show each "
        "result with doc.addObject('Part::Feature', 'Name').Shape = shape, keep dimensions as variables at the top, then "
        f"doc.saveAs('{d}/<name>.FCStd') and shape.exportStep('{d}/<name>.step'), and print the volume and bounding box to check "
        "the size. Units are millimetres. If the output says ERROR, read it, fix the script and run it again. Then say in one or "
        "two sentences what changed.")
