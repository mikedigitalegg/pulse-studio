# Pulse Studio (formerly Ableton Web POC)

Pulse Studio is a modern web interface for AI-assisted music creation in Ableton Live. It allows you to generate drum patterns, basslines, and full track arrangements using OpenAI, and control Ableton Live via OSC.

## Features

- **Dashboard**: Live monitoring of all sequencer layers (Drums, Bass, Perc, Stabs, FX).
- **Composition**:
  - **Smart Pair Generator**: Create complementary Drum and Bass patterns in one go.
  - **Full Track Arranger**: Generate a complete set of scenes (Intro, Main, Break, Drop, Outro).
  - **Individual Tools**: Targeted generators for specific layers.
- **Performance**: Instant style launching and macro controls for live jamming.
- **System**: Connection status and debug logs.

## Prereqs

- Ableton Live running
- AbletonOSC installed and running in Live
- AbletonOSC ports matching this repo defaults:
  - Ableton receives on UDP port `11000`
  - Responses (optional) sent to UDP port `11001`
- `OPENAI_API_KEY` environment variable set (for AI generation features)

## Files

- `ableton_web_poc_server.py` - FastAPI server
- `pulse_studio.html` - Main application interface
- `ableton_web_poc.html` - Legacy minimal POC
- `volca_drum_v2.html` - Legacy v2 POC

## Install Python deps

In your Python environment:

- `python -m pip install fastapi uvicorn python-osc pydantic`

Verify the `pythonosc` module is importable:

- `python -c "import pythonosc; print('pythonosc ok')"`

## Run the server

Run (from the repo folder):

- `python -m uvicorn ableton_web_poc_server:app --host 127.0.0.1 --port 8005`

## Open the UI

Open **Pulse Studio** in your browser:

- `http://127.0.0.1:8005/pulse_studio.html`

(Or just `http://127.0.0.1:8005/`, it will redirect).

## Usage Guide

1.  **Dashboard**: Check here to see what patterns are currently loaded in the generator's memory.
2.  **Composition**:
    *   Use **Smart Pair Generator** to get a solid groove started.
    *   Use **Full Track Arranger** to expand that groove into a full song structure in Ableton's Session View.
3.  **Performance**:
    *   Use **Style Pads** to instantly switch genres/kits (if your template supports it).
    *   Use **Macro Knobs** to tweak parameters live (requires mapping in Ableton).

## Expected behavior

- **Play/Stop**: Controls Ableton transport.
- **Generate**: Calls OpenAI to create a pattern, then sends it to Ableton Live via OSC.
- **Launch**: Triggers the clip in Session View at the designated track/slot.

## Template Setup

For best results, set up your Ableton Live set as follows:

- **Track 0**: Drums (Drum Rack)
- **Track 1**: Bass (Monophonic Synth)
- **Track 2**: Percussion (Drum Rack or Sampler)
- **Track 3**: Stabs (Polyphonic Synth/Chord hits)
- **Track 4**: FX (Noise/Risers)
