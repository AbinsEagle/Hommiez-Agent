"""Remote access in one click: a header button (or "Hey Hommie, start remote access") brings up Tailscale, starts the Sunshine
game-stream server and keeps the laptop awake, so the tablet's Moonlight app can connect to the desktop. Click again, or say
"stop remote access", to turn it off. Needs the Sunshine Flatpak and Tailscale (see README)."""
import json, re, shutil, socket, subprocess, threading, time

PRIORITY = 60
APP = "dev.lizardbyte.app.Sunshine"
PORT = 47990                     # Sunshine's web page: open means Sunshine is up
WHO = "Hommie remote access"
_lock = threading.Lock()
_awake = None                    # the process that stops the laptop sleeping

def _sunshine_up():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False

def _tailscale():
    """-> (backend state such as 'Running' or 'NeedsLogin', this laptop's Tailscale IPv4)."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=8).stdout
        d = json.loads(out)
        return d.get("BackendState"), next((i for i in (d.get("Self") or {}).get("TailscaleIPs") or [] if "." in i), "")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ""

def _wait(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False

def _connect_tailscale(ctx):
    """Bring Tailscale up. When it needs a sign-in, open the login page and say what to do. Returns a problem text, or None once connected."""
    def tell(text):
        if ctx:
            ctx.log(text)
    proc = subprocess.Popen(["tailscale", "up"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen = []
    threading.Thread(target=lambda: [seen.append(l) for l in proc.stdout], daemon=True).start()
    url = None
    end = time.time() + 15
    while time.time() < end and proc.poll() is None and not url:
        url = next((m.group(0) for l in seen if (m := re.search(r"https://login\.tailscale\.com/\S+", l))), None)
        time.sleep(0.3)
    text = "".join(seen)
    if proc.poll() == 0 or _tailscale()[0] == "Running":
        return None
    if re.search(r"access denied|operator", text, re.I):
        proc.terminate()
        return ("Tailscale won't let me connect without permission. Run this once in a terminal, then try again: "
                "sudo tailscale set --operator=$USER")
    if url:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tell("Tailscale needs you to sign in. I opened the login page in your browser: " + url)
        tell("Sign in with the same account as on your tablet (abins.mic@gmail.com), approve this device, then wait a few seconds. "
             "I'll carry on by myself.")
        if ctx:
            ctx.say("Tailscale needs you to sign in. I opened the login page. Sign in with the same account as your tablet.")
        if _wait(lambda: _tailscale()[0] == "Running", 180):
            return None
        proc.terminate()
        return "I didn't see you sign in to Tailscale in time. Press the button again, or run 'sudo tailscale up' in a terminal."
    proc.terminate()
    return ("Tailscale isn't connecting. Check your internet, then press the button again. "
            "If it keeps failing, run 'sudo tailscale up' in a terminal and sign in with the link it shows.")

def start(ctx=None):
    global _awake
    with _lock:
        if not shutil.which("flatpak") or not shutil.which("tailscale"):
            return "Remote access needs flatpak and tailscale installed."
        state, ip = _tailscale()
        if state != "Running":
            problem = _connect_tailscale(ctx)
            if problem:
                return problem
            state, ip = _tailscale()
        if not _sunshine_up():
            subprocess.Popen(["flatpak", "run", APP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            if not _wait(_sunshine_up, 45):
                return "Sunshine didn't start. Check ~/.var/app/%s/config/sunshine/sunshine.log." % APP
        if _awake is None or _awake.poll() is not None:
            _awake = subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", f"--who={WHO}", "--why=Remote access is on",
                                       "sleep", "infinity"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Remote access is on. In Moonlight, connect to {ip}."

def stop(ctx=None):
    global _awake
    with _lock:
        if _awake and _awake.poll() is None:
            _awake.terminate()
        _awake = None
        subprocess.run(["pkill", "-f", f"who={WHO}"], capture_output=True)       # one left over from before Hommie restarted
        subprocess.run(["flatpak", "kill", APP], capture_output=True)
        _wait(lambda: not _sunshine_up(), 8)
        return "Remote access is off. Tailscale is still connected."

def toggle(ctx=None):
    return stop(ctx) if _sunshine_up() else start(ctx)

def status(ctx=None):
    return "on" if _sunshine_up() else "off"

QUICK = [{"id": "remote", "label": "Remote access", "action": toggle, "status": status}]

def setup(ctx):
    def later(fn):                                  # starting takes a few seconds: answer now, report when done
        threading.Thread(target=lambda: ctx.say(fn(ctx)), daemon=True).start()

    ctx.command(r"\b(?:start|turn on|enable|open|switch on)\b.*\bremote (?:access|control|desktop)\b",
                lambda m, t: (later(start), "Starting remote access.")[1], "remote_on", priority=60)
    ctx.command(r"\b(?:stop|turn off|disable|close|switch off|end)\b.*\bremote (?:access|control|desktop)\b",
                lambda m, t: (later(stop), "Stopping remote access.")[1], "remote_off", priority=60)
