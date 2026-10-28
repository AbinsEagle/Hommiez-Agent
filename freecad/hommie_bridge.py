"""Runs INSIDE the FreeCAD window (started once by send.py). Every second it looks in live/inbox for a script,
runs it against the open document, and writes what it printed (or the error) to live/outbox. One window, one model,
edited again and again. A heartbeat file tells send.py the window is alive."""
import os, time, traceback, io, contextlib
import FreeCAD, FreeCADGui
from PySide import QtCore

LIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live")
INBOX, OUTBOX, BEAT = os.path.join(LIVE, "inbox"), os.path.join(LIVE, "outbox"), os.path.join(LIVE, "alive")
for d in (INBOX, OUTBOX):
    os.makedirs(d, exist_ok=True)

def run(path):
    out = io.StringIO()
    try:
        doc = FreeCAD.ActiveDocument or FreeCAD.newDocument("Hommie")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(open(path).read(), path, "exec"),
                 {"FreeCAD": FreeCAD, "App": FreeCAD, "Gui": FreeCADGui, "doc": doc, "__name__": "__main__"})
            if FreeCAD.ActiveDocument:
                FreeCAD.ActiveDocument.recompute()
            FreeCADGui.SendMsgToActiveView("ViewFit")
        out.write("OK\n")
    except Exception:
        out.write("ERROR\n" + traceback.format_exc())
    name = os.path.basename(path)
    with open(os.path.join(OUTBOX, name + ".out"), "w") as f:
        f.write(out.getvalue())
    os.remove(path)

def tick():
    with open(BEAT, "w") as f:
        f.write(str(time.time()))
    for name in sorted(os.listdir(INBOX)):
        if name.endswith(".py"):
            run(os.path.join(INBOX, name))

timer = QtCore.QTimer()
timer.timeout.connect(tick)
timer.start(1000)
