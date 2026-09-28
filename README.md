# Pulse Studio

Pulse Studio is a web app for AI-assisted music making in Ableton Live. It generates drums, bass, chords and full arrangements, writes them into Live's Session View, loads instruments, sends voice clips and samples into your set, and has a live visualizer. It all runs locally: a Python server talks to Live through a small Remote Script (PulseBridge), and you use it from your browser.

```
Browser (pulse_studio.html)  ⇄  Python server (FastAPI, port 8005)  ⇄  PulseBridge Remote Script in Live (TCP 9880)
                                         └── AI provider: OpenAI, Anthropic or a local model (patterns)
                                             OpenAI (voices)
```

## What you need

| | |
|---|---|
| **Ableton Live 11 or 12** | Any edition: Lite, Intro, Standard or Suite. No Max for Live needed. Live 12.0.5+ is needed for voice clips and samples to land as audio clips. |
| **Python 3.10 or newer** | [python.org/downloads](https://www.python.org/downloads/). On Windows, tick "Add python.exe to PATH" in the installer. |
| **Git** | To clone the repo ([git-scm.com](https://git-scm.com/downloads)). You can also download the ZIP from GitHub. |
| **An AI provider** | For pattern generation: an [OpenAI key](https://platform.openai.com/api-keys), an [Anthropic key](https://console.anthropic.com/settings/keys), or a local model server such as [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai). Voices need an OpenAI key. The app opens and controls Live without any of these, but the generators won't work. |
| **A modern browser** | Chrome, Edge, Firefox or Safari. |

Windows and macOS are both supported.

## Setup

### 1. Get the code

```bash
git clone https://github.com/mikedigitalegg/pulse-studio.git
cd pulse-studio
```

### 2. Create a virtual environment and install dependencies

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell refuses to run the activate script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation and call `.venv\Scripts\python.exe` directly in the commands below.

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. Choose your AI provider

The easiest way is in the app: once it's running (step 5), open **System → AI Provider**, pick OpenAI, Anthropic or Local, choose a model, paste your key and press **Test**. The key is saved to `.env` for you.

Or set it up by hand. Copy the example settings file and paste your key into it:

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Then edit `.env`:

```
OPENAI_API_KEY=sk-...
# or ANTHROPIC_API_KEY=sk-ant-... and PULSE_AI_PROVIDER=anthropic
```

For a local model, install Ollama, run `ollama pull llama3.1`, then pick **Local** on the System page (server URL `http://localhost:11434/v1`). Small local models make rougher patterns than the hosted ones.

`.env` is git-ignored, so the key stays on your machine. See [Configuration](#configuration) for the optional settings.

### 4. Install PulseBridge into Live

PulseBridge is the Remote Script that lets the server control Live. With Live **closed or open**, run:

```bash
python pulse_bridge/install.py
```

It finds your User Library from Live's own settings (including moved libraries, e.g. on OneDrive or an external drive) and installs for every Live version it finds. If it can't find it, pass the path: `python pulse_bridge/install.py --user-library "D:/Music/Ableton/User Library"`.

Then in Live:

1. Open **Settings** (Preferences on macOS) → **Link, Tempo & MIDI**.
2. In an empty **Control Surface** slot choose **PulseBridge**. Leave Input and Output as **None**.
3. Live's status bar shows `PulseBridge 0.2.0 ready on port 9880`.

Updating from an older copy? Run the installer again, then set the Control Surface slot to None and back to PulseBridge. Version 0.2.0 adds the return tracks and sends that full tracks use for reverb and delay; older versions still work, just without them.

If Live was already open and PulseBridge doesn't appear in the list, restart Live.

Optional check, with Live open: `python pulse_bridge/smoke_test.py` reads your set and prints what it sees (add `--write` to also create a test track and clip; undo with Ctrl+Z / Cmd+Z).

### 5. Start the server

From the repo folder, with the virtual environment active:

```bash
python -m uvicorn ableton_web_poc_server:app --host 127.0.0.1 --port 8005
```

Leave this terminal open while you use the app. Stop it with Ctrl+C.

### 6. Open Pulse Studio

Go to **<http://127.0.0.1:8005/>** (it redirects to `/pulse_studio.html`).

The pill under the logo shows the connection: it turns green with your Live version and edition once PulseBridge is connected. If it doesn't, click it for details, or see [Troubleshooting](#troubleshooting).

## First run

1. Open a new, empty Live set and make sure PulseBridge is selected as a Control Surface.
2. In Pulse Studio, go to **Compose**, pick a **Style** (Techno, House, DnB, …) and generate. Pulse creates its tracks (named `PS-TRK-01`, `PS-TRK-02`, …), loads instruments, writes clips into Session View and can fire them.
3. Press play in Live (or use the Play button in the sidebar).

Leave the `PS-TRK-…` track names as they are: Pulse uses them to recognise tracks it has already set up. Renaming them makes it create a fresh set of tracks on the next generate.

## Using the app

| View | What it does |
|---|---|
| **Compose** | Generate drum/bass pairs, chords and full arrangements (Intro, Main, Break, Drop, Outro) into Session View. A full track fills seven tracks (Drums, Bass, Perc, Stabs, FX, Chords and Pad) and adds a reverb and a delay return with sends set for each part. Returns already in your set are reused, and sends you've moved by hand are left alone. **Voice** generates spoken/sung lines and **Send to Live** puts them on a `PS-VOX` audio track. |
| **Styles** | Browse and edit the style knowledge base (`knowledge/styles.json`): tempo, kit, bass and harmony rules. **Rebuild browser index** rescans Live's browser so Pulse can pick presets from your Library. |
| **Samples** | Import WAVs (optionally trimming silence), search Live's library, and send a sample to an audio clip, a Simpler or a Drum Rack pad. Imported files live in `samples/` (git-ignored). |
| **Perform** | Built for playing live. A pinned deck shows play/stop, position, tempo (with nudge) and what is playing now and next, plus **Autopilot** (plays the Compose arrangement until you launch a scene yourself) and **Stop clips**. Below it: scene pads named after your scenes, part toggles that mute each track with a level meter, moves (Wash & Drop, Breakdown → Drop, Delay Throw, and Fade Out, which you press twice), and vox pads. Moves wait for Live's grid: they start on the next bar (Delay Throw on the next beat), so press a little ahead. Keyboard: Space play/stop, 1–0 scenes, Q–P parts, A–K vox pads, B wash, N breakdown (press again to drop early), V delay throw, [ and ] tempo. Track knobs and the recorder are in the collapsed sections at the bottom. |
| **Live Overview** | Transport, tempo, key and the tracks, devices and playing clips in your set, updated live. |
| **Visualizer** | Animated scenes driven by Live's output meters (no microphone or audio routing). Pick a scene and theme, set it to change every 4/8/16 bars, or go fullscreen. |
| **System** | AI provider and model, connection status, diagnostics, browser index tools and logs. |

## Configuration

All settings go in `.env` (or real environment variables). You need a key for the AI provider you use (none for most local servers). The provider and model picked on the System page are saved in `ai_settings.json` and take priority over `PULSE_AI_PROVIDER` / `PULSE_AI_MODEL`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | OpenAI provider, and voices (text-to-speech always uses OpenAI). |
| `ANTHROPIC_API_KEY` | — | Anthropic provider. |
| `PULSE_AI_PROVIDER` / `PULSE_AI_MODEL` | `openai` / provider default | Starting provider (`openai`, `anthropic` or `local`) and model, before anything is picked on the System page. |
| `LOCAL_AI_BASE_URL` / `LOCAL_AI_API_KEY` | `http://localhost:11434/v1` / — | OpenAI-compatible local server (Ollama, LM Studio, vLLM...) and its key if it needs one. |
| `PULSE_BRIDGE_HOST` / `PULSE_BRIDGE_PORT` | `127.0.0.1` / `9880` | Where the server finds PulseBridge. To change the port, set `PULSE_BRIDGE_PORT` as a system environment variable (not just in `.env`) so Live's copy of PulseBridge sees it too, then restart Live. |
| `PULSE_USER_LIBRARY` | Found from Live's settings | Your Ableton User Library, used when voice clips fall back to a Simpler on older Live versions. |
| `PULSE_OSC_DEFAULT_DRUM` / `PULSE_OSC_DEFAULT_MELODIC` | `Drum Rack` / `Wavetable` | Instruments put on empty tracks. On Live Intro/Lite, use one you own (e.g. `Simpler` or `Drift`). |
| `ABLETON_MCP_HOST` / `ABLETON_MCP_PORT` | `127.0.0.1` / `9877` | Only for the legacy AbletonMCP connection (see below). |

To run on a different port, change `--port` in the start command and open that port in the browser.

### Legacy connection: AbletonOSC / AbletonMCP

PulseBridge replaces both, and the server prefers it whenever it's connected. If PulseBridge isn't connected, the server falls back to [AbletonOSC](https://github.com/ideoforms/AbletonOSC) (Live receives on UDP `11000`, replies on UDP `11001`) and, for browser/preset loading, [AbletonMCP](https://github.com/ahujasid/ableton-mcp). You don't need either for a new setup.

## Troubleshooting

**The pill says Live isn't connected.**
- Check Live is open and **PulseBridge** is selected in a Control Surface slot. Selecting None and then PulseBridge again restarts it.
- Check the server terminal is still running and shows no errors.
- Open <http://127.0.0.1:8005/bridge/status> to see what the server sees.
- Remote Script errors go to Live's `Log.txt`: on Windows `%APPDATA%\Ableton\Live <version>\Preferences\Log.txt`, on macOS `~/Library/Preferences/Ableton/Live <version>/Log.txt`.

**`ModuleNotFoundError` when starting the server.** The virtual environment isn't active, or dependencies aren't installed. Activate `.venv` (step 2) and re-run `python -m pip install -r requirements.txt`.

**`[Errno 10048]` / "address already in use".** Something else is on port 8005, often an earlier server. Stop it, or start on another port (`--port 8006`) and open that instead.

**Generating does nothing or reports `missing_ai_api_key`.** The selected provider has no key. Add one on **System → AI Provider** (or in `.env` and restart the server). **Test** there shows the provider's own error if the key or model is wrong.

**Local model is slow or its patterns fail to parse.** Try a bigger model, or check the server URL ends in `/v1`. Requests to local servers wait up to 5 minutes.

**Tracks are silent or an instrument is missing.** Lite and Intro don't include every instrument. Set `PULSE_OSC_DEFAULT_MELODIC` to one you have (e.g. `Drift` or `Simpler`). Lite allows 8 tracks and Intro 16, so large arrangements may not fit.

**After pulling a new version, something that used to work fails.** PulseBridge may be out of date. Re-run `python pulse_bridge/install.py`, then in Live set the Control Surface slot to None and back to PulseBridge.

## Development

Run the tests (they use fakes for Live and the AI providers, so Live doesn't need to be open):

```bash
python -m pip install pytest
python -m pytest tests -q
```

| Path | What it is |
|---|---|
| `ableton_web_poc_server.py` | The FastAPI server: generation, Live control, voices, samples, event stream. |
| `pulse_studio.html` | The whole web app (single file). |
| `pulse_bridge/` | The PulseBridge Remote Script, installer and smoke test. Protocol and commands: [pulse_bridge/README.md](pulse_bridge/README.md). |
| `pulse_bridge_client.py`, `live_link.py` | The server's connections to PulseBridge and AbletonOSC. |
| `knowledge/styles.json` | Style definitions: tempo, grooves, kits, bass and harmony rules. |
| `knowledge/ableton_browser_index.json` | Cached index of Live's browser, rebuilt from the Styles or System view. |
| `pulse_canvas/` | The original standalone visualizer (captures browser/system audio), served at `/pulse_canvas/`. |
| `tests/` | pytest suite. |
| `ableton_*.py`, `ableton_web_poc.html`, `volca_drum_v2.html` | Early experiments and prototypes, kept for reference. |

With the server running, interactive API docs are at <http://127.0.0.1:8005/docs>.
