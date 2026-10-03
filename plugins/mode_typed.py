"""Keyboard mode (--type): type a request, or press Enter on an empty line to talk."""
from hommie.core import is_yes

PRIORITY = 61

class TypedMode:
    def run(self, ctx):
        ears = ctx.service("listen")
        print("Hommie online. Type a request" + (", or press Enter to talk" if ears else "") + " (Ctrl-D or 'exit' to quit).")
        ctx.say("Hommie online.")
        ctx.confirm = lambda: is_yes(input("  confirm? (yes/no" + (", or Enter to answer by voice" if ears else "") + ") ")
                                     or (ears.record_until_enter() if ears else ""))
        while not ctx.quit_requested:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            typed = bool(line)
            if not line and ears:
                line = ears.record_until_enter()
                if not line:
                    print("  (heard nothing)")
                    continue
            if not line:
                continue
            if line.lower() in ("exit", "quit", "bye"):
                break
            ctx.sleep_requested = False
            try:
                ctx.say(ctx.handle(line, echo=not typed))
            except KeyboardInterrupt:
                pass
        ctx.say("Goodbye.")

def setup(ctx):
    ctx.provide("mode", TypedMode(), "type")
