"""ESP32 / Hommiez firmware know-how: lets Hommie compile sketches. Uploading still needs a spoken yes."""
PRIORITY = 21

def setup(ctx):
    ctx.allow("Bash(arduino-cli compile:*)")
    ctx.persona("The ESP32 firmware (Service Sense OS / Hommiez) is in /home/rona/hommiez-fw. Board FQBN "
                "esp32:esp32:esp32 on /dev/ttyUSB0. Compiling is fine; uploading to the board needs the user's okay.")
