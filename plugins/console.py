"""Prints the conversation to the terminal. Delete this file for a silent run."""
PRIORITY = 5

def setup(ctx):
    ctx.on("heard", lambda text, echo=True, **kw: echo and print(f"you> {text}", flush=True))
    ctx.on("say", lambda text, **kw: print(f"hommie> {text}\n", flush=True))
    ctx.on("log", lambda text, **kw: print(f"  ({text})", flush=True))
