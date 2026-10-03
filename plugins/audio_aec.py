"""Echo cancellation: runs a small private PipeWire helper that gives Hommie an echo-free microphone and a speaker
that the canceller can subtract. With it she can hear you while she is talking (you can interrupt her).
Remove this file and nothing else changes; if the helper cannot start, Hommie just carries on without it."""
import ctypes, os, shutil, signal, subprocess, time
from hommie.core import AUDIO, HERE

PRIORITY = 5
CONF = os.path.join(HERE, "aec", "echo-cancel.conf")
SOURCE, SINK = "hommie-aec-source", "hommie-aec-sink"
_proc = None

def _nodes_up():
    out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5).stdout
    return SOURCE in out and SINK in out

def _die_with_parent():
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)       # PR_SET_PDEATHSIG: helper stops if Hommie crashes

def setup(ctx):
    global _proc
    if not (shutil.which("pipewire") and shutil.which("pw-cli") and os.path.exists(CONF)):
        raise FileNotFoundError("pipewire or the echo-cancel config is missing")
    if not _nodes_up():
        _proc = subprocess.Popen(["pipewire", "-c", CONF], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 preexec_fn=_die_with_parent)
        for _ in range(30):
            time.sleep(0.2)
            if _proc.poll() is not None or _nodes_up():
                break
        if not _nodes_up():
            teardown(ctx)
            raise RuntimeError("the echo canceller did not start")
    AUDIO.update(source=SOURCE, sink=SINK)

def teardown(ctx):
    global _proc
    AUDIO.update(source=None, sink=None)
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None
