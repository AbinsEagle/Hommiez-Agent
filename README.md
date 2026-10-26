# Hommie (voice assistant)

Run: `jarvis.py` (hands-free, "Hey Hommie ...") · `jarvis.py --type` (keyboard) · `jarvis.py --plugins` (what loaded)
Desktop icons: "Hommie" (window + hands-free, same as `jarvis.py --ui`) and "Hommie (typing mode)".
Web UI: http://127.0.0.1:8765 whenever Hommie runs (plugins/ui_web.py; delete it to remove the UI).

## Setup (Linux with PipeWire)
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# put the voice models in voices/ (see voices/README.md)
# needs: the `claude` CLI logged in (claude.ai login, no API key), pw-record/pw-play (PipeWire)
# optional: Groq / Gemini keys, see "Choosing the brain" below
./jarvis.py --plugins   # check what loaded
./jarvis.py --ui        # hands-free + web window
```
Natural voice (optional): `plugins/tts_pocket.py` runs Pocket TTS in its own Python (torch is big, so it is not in the main venv):
`python3 -m venv ~/tts-trial/.venv && ~/tts-trial/.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu pocket-tts soundfile`.
Until it has loaded (about a minute) or if it fails, Kokoro speaks. Set `tts_pocket.voice` to a .wav path to clone a voice.

Only one Hommie can run at a time (a lock stops a second copy). Logs go to `logs/hommie.log`.
`audio_aec.py` starts a private echo canceller (`aec/echo-cancel.conf`) so she can hear you while she talks; delete it to turn that off.
Settings are saved to `settings.json` (created on first change, not in git).

## Everything is a plugin
`hommie/core.py` only loads plugins and routes events. Every feature is one file in `plugins/`:

| file | feature | delete it and... |
|---|---|---|
| brain_claude | Claude (your claude.ai login) as a brain, with file/shell tools | Claude is no longer an option |
| brain_router | the Brain switch: Claude / Groq / Gemini from a dropdown or by voice; Groq and Gemini get the same tools via `hommie/agent_tools.py` | Claude alone answers again |
| tts_kokoro / tts_piper / tts_espeak | voices, best one that works is used | next voice takes over |
| stt_whisper | microphone + speech recognition | no voice input; `--type` still works |
| mode_wake / mode_typed | hands-free / keyboard front ends | that mode disappears |
| persona_hommie | personality | she loses her personality prompt |
| proj_workspace / proj_firmware | folders and tools Claude may use | Claude loses access to them |
| cmd_session | "goodbye", "shut down Hommie" | those commands vanish |
| console | prints the conversation | silent terminal |
| brain_wiki | your restructured knowledge base (`~/AbinsAI/Brain`) as her memory: searches it on every question | she stops consulting your notes |
| obsidian | reads/searches your Obsidian vault, writes only to `Hommie/` ("note that ...") | Hommie stops knowing about Obsidian |
| audio_aec | echo-free mic + interrupting her (barge-in) | she can't hear you while talking |
| ui_web | web UI: orb, chat, settings panel, plugin toggles (`--ui`) | no UI; everything else unchanged |
| guide | screen guide: highlights the next thing to click in any app (see below) | no screen guide |

## Choosing the brain (Claude, Groq, Gemini)
Pick who answers in the web UI: Settings, **Brain**, **Who answers**. Or say or type it:
`use Groq` · `use Gemini` · `use Claude` · `use the smart model` · `use the fast model` · `which brain are you using`.
The choice is saved in `settings.json` (`brain_router.choice`).

| choice | good for | notes |
|---|---|---|
| Claude Haiku / Sonnet | the best at code and reasoning; uses your claude.ai quota | needs the `claude` CLI logged in |
| Groq gpt-oss 20B / 120B | fast (about 1 s), free tier, the dependable everyday pick | free tier rate-limits bursts of tool calls (HTTP 429) |
| Gemini 3.5 Flash / Pro | backup | free tier returns 429/503 often and is slow with tools; Pro has no free quota |

Groq and Gemini act like Claude: they read, search and edit files, run shell commands and search your Brain
(`hommie/agent_tools.py`), under the same rules. Reads and edits in the working folders are automatic; shell commands
run unasked only if they are simple and on the allow list (`brain_claude.base_allow` + `ctx.allow()`); anything else is
confirmed out loud; folders denied with `ctx.deny()` (the Brain, the Obsidian vault) stay read-only; and secret files
(`.bashrc`, `.ssh`, `.env`, `auth.json`...) are never read, because their text is sent to a third-party API.

### Add the API keys
1. Get a free key: Groq at https://console.groq.com/keys, Gemini at https://aistudio.google.com/apikey.
2. Put it in your shell config, typed yourself so it never lands in git or a chat. In Claude Code use the `!` prefix:
   ```
   echo 'export GROQ_API_KEY=your_key_here' >> ~/.bashrc
   echo 'export GEMINI_API_KEY=your_key_here' >> ~/.bashrc
   ```
3. Restart Hommie. The plugin reads `GROQ_API_KEY` / `GEMINI_API_KEY` from the environment, and if they are not there
   (the desktop icon does not load `.bashrc`) from the `export` lines in `~/.bashrc`. Keys are never stored in this repo.
If a key is missing, Hommie says so out loud and does not fall back to Claude by itself (so it never spends your quota unasked).

### Add a model or another provider
Both are edits in `plugins/brain_router.py`, nothing else:
- **A new model for an existing provider:** add a line to `CHOICES`, `("groq:<model id>", "Label shown in the dropdown")`.
  Model ids come from `opencode models groq` / `opencode models google`, or the provider's models page.
- **A new provider** (anything with an OpenAI-compatible chat API, e.g. OpenRouter, Together, Mistral): add an entry to `APIS`
  (`name: (chat-completions URL, ENV_VAR_NAME, "Name to say aloud")`), add its models to `CHOICES` as `"name:<model id>"`,
  put the key in `~/.bashrc`, and add the name to the `use X` regex in `setup()` so "use X" works by voice.
  The model must support tool calling for the file/shell abilities to work.
The dropdown is built from `CHOICES`, so it updates by itself. Delete `brain_router.py` to go back to Claude alone.

For coding in a plain terminal (not Hommie), [opencode](https://opencode.ai) reads the same two keys; `opencode -m groq/openai/gpt-oss-120b`
or `opencode -m google/gemini-3.5-flash`, and `/models` switches inside it.

## Screen guide (learn any program, one click at a time)
Say "guide me to add a fillet", "show me where the extrude tool is", "walk me through exporting an STL" or "highlight the save icon".
Hommie looks at your screen, draws an amber box and a numbered label on the one control to use next, glides a ghost cursor
to it (an overlay, so it never steals clicks) and says the step aloud. Then you just use the app:
- It does not listen to the mouse. While a step shows, it takes a cheap screenshot about once a second; when the picture changes
  and settles (a menu, submenu or dialog opened, a tab switched) it asks the vision model for the next step, with the goal and the
  steps so far, so it points *inside* the submenu that just appeared, or says "That's not it" and shows the way back.
- Changes inside one control (a checkbox) can't be seen: say "next" or "done". Also "repeat" and "stop the guide".
- Who looks at the screen: Settings, **Screen guide**: Claude Sonnet (default, accurate, about 5-10 s per step, uses your Claude
  quota), Claude Haiku (cheaper), or Gemini Flash (free tier is only about 20 requests a day). Groq has no vision model that points well.
- Privacy: a screenshot is sent to the chosen model only for each step of a guide you asked for, never in the background; Hommie says
  "looking at your screen" first. Screenshots are kept in the runtime tmpfs and deleted straight away.
- Needs: KDE `spectacle`, the *system* `python3` with GTK3 (`tools/overlay.py`, run through XWayland because KDE Wayland blocks normal
  always-on-top windows), Pillow in the venv (`pip install pillow`), and the `claude` CLI (or `GEMINI_API_KEY`).
- Not done: moving the real mouse pointer (needs `ydotool` + membership of the `input` group; the ghost cursor shows the way instead).

## Add a feature
Copy `plugins/_example.py` to `plugins/cmd_whatever.py` and edit. No other file changes.
A plugin can add controls to the UI settings panel (a module-level `UI = [...]` list, see tts_kokoro.py), voice commands, a voice/brain/mode, event listeners (a UI is just a plugin that listens),
folders and tools for Claude, and its own saved settings.

## Remove or pause a feature
Delete the file, or rename it with a leading `_`, or set `{"plugins": {"name": false}}` in `settings.json`.
A plugin that fails to import, set up, or is missing a dependency is skipped; the rest keep running.

Settings live in `settings.json` (created on first change), one section per plugin.
Backup of the old single-file version: `backup/`.

## The Brain (knowledge base)
`tools/build_brain.py` rebuilds `~/AbinsAI/Brain` from the markdown under `~/AbinsAI` (read-only on the sources, ~10 s, no model):
one note per conversation (full text kept), topic hubs, tags, related links, a search index, and provenance of every copy.
`tools/summarize_topics.py` asks Claude for the short overview at the top of each topic hub (titles + first questions only,
phone numbers/emails/IDs redacted). Re-run build after adding notes; summaries are kept in `Brain/_meta/summaries.json`.
