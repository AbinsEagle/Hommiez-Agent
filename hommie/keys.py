"""API keys for the chat/vision providers: the environment first, else the `export NAME=...` lines in ~/.bashrc
(the desktop icon doesn't load .bashrc). Keys are never stored in this repo."""
import os, re

def api_key(var):
    if os.environ.get(var):
        return os.environ[var]
    try:
        with open(os.path.expanduser("~/.bashrc")) as f:
            found = None
            for line in f:
                m = re.match(rf"\s*export\s+{var}=['\"]?([^'\"\s]+)", line)
                if m:
                    found = m.group(1)           # the last assignment wins, as in a shell
            return found
    except OSError:
        return None
