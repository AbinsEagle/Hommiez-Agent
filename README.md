# Hommie (voice assistant)

Run: `jarvis.py` (hands-free, "Hey Hommie ...") · `jarvis.py --type` (keyboard) · `jarvis.py --plugins` (what loaded)
Desktop icons: "Hommie" (window + hands-free, same as `jarvis.py --ui`) and "Hommie (typing mode)".
Web UI: http://127.0.0.1:8765 whenever Hommie runs (plugins/ui_web.py; delete it to remove the UI).

## Setup (Linux with PipeWire)
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# put the voice models in voices/ (see voices/README.md)
# needs: the `claude` CLI logged in (claude.ai login, no API key), pw-record/pw-play (PipeWire)
./jarvis.py --plugins   # check what loaded
./jarvis.py --ui        # hands-free + web window
```
Only one Hommie can run at a time (a lock stops a second copy). Logs go to `logs/hommie.log`.
`audio_aec.py` starts a private echo canceller (`aec/echo-cancel.conf`) so she can hear you while she talks; delete it to turn that off.
Settings are saved to `settings.json` (created on first change, not in git).

## Everything is a plugin
`hommie/core.py` only loads plugins and routes events. Every feature is one file in `plugins/`:

| file | feature | delete it and... |
|---|---|---|
| brain_claude | Claude as the brain (+ "use the smart model") | no brain; only local commands work |
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
