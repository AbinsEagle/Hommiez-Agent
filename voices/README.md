# Voice models (not stored in git: too large)

Hommie's main voice is Kokoro. Download these two files into this folder:

- `kokoro-v1.0.onnx`
- `voices-v1.0.bin`

from the kokoro-onnx project's `model-files-v1.0` release: https://github.com/thewh1teagle/kokoro-onnx/releases

Optional fallback voices (Piper, `en_US-amy-medium`, `en_US-kristin-medium`, `en_US-hfc_female-medium`; each needs its `.onnx` and `.onnx.json`)
come from https://huggingface.co/rhasspy/piper-voices. If neither is present Hommie falls back to espeak.
