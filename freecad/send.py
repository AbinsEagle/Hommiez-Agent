#!/usr/bin/env python3
"""send.py <script.py>: run a FreeCAD script in the ONE open FreeCAD window (starting it first if needed), print the result.
Inside the script `doc` is the open document, plus FreeCAD, App and Gui. Exit code 1 if the script raised."""
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "live")
INBOX, OUTBOX, BEAT = (os.path.join(LIVE, n) for n in ("inbox", "outbox", "alive"))

def alive():
    try:
        return time.time() - float(open(BEAT).read()) < 5
    except (OSError, ValueError):
        return False

def wait(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False

def main():
    if len(sys.argv) != 2 or not os.path.isfile(sys.argv[1]):
        sys.exit("usage: send.py <script.py>")
    for d in (INBOX, OUTBOX):
        os.makedirs(d, exist_ok=True)
    if not alive():
        print("starting FreeCAD...")
        subprocess.Popen(["flatpak", "run", "org.freecad.FreeCAD", os.path.join(HERE, "hommie_bridge.py")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        if not wait(alive, 90):
            sys.exit("FreeCAD did not start in 90 seconds")
    name = "%d.py" % (time.time() * 1000)
    tmp = os.path.join(INBOX, name + ".tmp")
    with open(sys.argv[1]) as src, open(tmp, "w") as dst:
        dst.write(src.read())
    os.replace(tmp, os.path.join(INBOX, name))
    out = os.path.join(OUTBOX, name + ".out")
    if not wait(lambda: os.path.exists(out), 60):
        sys.exit("FreeCAD did not answer in 60 seconds (is a dialog open in the window?)")
    text = open(out).read()
    os.remove(out)
    print(text, end="")
    sys.exit(1 if "ERROR" in text.splitlines() else 0)

main()
