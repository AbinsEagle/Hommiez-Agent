"""Session commands: "goodbye" ends the conversation window, "shut down Hommie" quits the app."""
PRIORITY = 45

def setup(ctx):
    def quit_cmd(m, text):
        ctx.quit_requested = True
        return "Goodbye."

    def sleep_cmd(m, text):
        ctx.sleep_requested = True
        return "Okay."

    ctx.command(r"\b(shut ?down|quit|exit|turn off)\b.*\b(hommie|homie|yourself|assistant)\b|^\W*(quit|exit|shut ?down)\W*$",
                quit_cmd, "quit", priority=90)
    ctx.command(r"\b(goodbye|good bye|go to sleep|that'?s all|never ?mind)\b", sleep_cmd, "sleep", priority=80)
