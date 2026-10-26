"""How words are SAID, without changing how they are written. Add more in settings.json:
  "pronunciations": {"words": {"Abins": "Ebins", "ESP32": "E S P thirty two"}}"""
from hommie.core import PRONUNCIATIONS

PRIORITY = 8
DEFAULTS = {"words": {"Abins": "Ebins"}}

def setup(ctx):
    PRONUNCIATIONS.update(ctx.cfg("pronunciations", DEFAULTS)["words"])
