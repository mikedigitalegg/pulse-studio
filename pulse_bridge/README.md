# PulseBridge

The Ableton Live Remote Script behind Pulse Studio. It replaces AbletonOSC + AbletonMCP
with a single, reliable connection. It is a plain control-surface script, so it works in
**every Live edition** (Lite, Intro, Standard, Suite) without Max for Live.

## Install

```
python pulse_bridge/install.py
```

The installer reads Live's own `Library.cfg`, so it finds the User Library even if it
has moved (OneDrive, external drive, macOS). Then, in Live:

**Settings → Link, Tempo & MIDI → Control Surface → PulseBridge** (Input/Output: None)

Live shows "PulseBridge 0.1.0 ready on port 9880" in the status bar. You can run
AbletonOSC alongside it in another slot; the Pulse Studio server uses PulseBridge when
it is connected and falls back to OSC when it is not.

Check it: `python pulse_bridge/smoke_test.py` (read-only), or add `--write` to create a
test track and clip. Remove it: `python pulse_bridge/install.py --uninstall`.

## Why not OSC

| | AbletonOSC + AbletonMCP | PulseBridge |
|---|---|---|
| Transport | UDP (no delivery guarantee) + a second TCP script | one TCP connection |
| Replies | separate listener port, matched by address and timing | matched by request id |
| Changes in Live | polled | pushed as events |
| Loading stock devices by name | not supported (`/live/track/load_device` does not exist) | `load_device` |
| Note probability / velocity deviation | no | yes (Live 11+) |
| Edition / version detection | no | `get_capabilities` |
| Undo | one entry per message | changes from each tick are grouped into one undo step |

## Protocol

Newline-delimited JSON on `127.0.0.1:9880` (override with `PULSE_BRIDGE_PORT`).

```
→ {"id": 1, "cmd": "get_song", "params": {}}
← {"id": 1, "ok": true, "result": {"tempo": 128.0, ...}}
← {"id": 2, "ok": false, "error": "track_index_out_of_range: 9 (tracks: 6)"}
← {"event": "song.tempo", "data": {"tempo": 130.0}}        (after "subscribe")
```

Omit `id` for fire-and-forget; only failures are reported, as a reply with `"id": null`.
Messages are executed in order on Live's main thread, about every 100 ms, up to 500 per
tick. So a burst of sends followed by a query sees every send applied.

### Commands

| Command | Params |
|---|---|
| `hello`, `ping`, `subscribe`, `unsubscribe` | |
| `get_capabilities` | `refresh?` |
| `get_song`, `get_snapshot` | |
| `set_tempo` | `tempo` |
| `play`, `stop` | |
| `set_song_key` (Live 12) | `root_note?` 0–11, `scale_name?`, `scale_mode?` |
| `create_midi_track`, `create_audio_track`, `create_scene` | `index` (-1 = end) |
| `get_track` | `track_index` |
| `set_track` | `track_index`, `name?`, `volume?`, `panning?`, `mute?`, `solo?`, `arm?` |
| `set_scene_name`, `fire_scene` | `scene_index`, `name` |
| `has_clip`, `delete_clip`, `fire_clip`, `stop_clip` | `track_index`, `clip_slot_index` |
| `find_free_slot` | `track_index`, `start_slot_index?`, `max_slots?` |
| `create_clip` | `track_index`, `clip_slot_index`, `length`, `replace?` (adds scenes if needed) |
| `set_clip_name` | … `name` |
| `add_notes` | … `notes: [{pitch, start_time, duration, velocity, mute?, probability?, velocity_deviation?, release_velocity?}]` |
| `get_notes`, `remove_notes` | … `from_time?`, `time_span?`, `from_pitch?`, `pitch_span?` |
| `write_clip` | `create_clip` + `notes`, `name?`, `fire?` in one step |
| `get_device_params` | `track_index`, `device_index` |
| `set_device_param` | … `param_index`, `value` (clamped to the parameter's range) |
| `load_device` | `track_index`, `device_name` (a stock device such as "Drift" or "Drum Rack") |
| `load_audio_clip` (Live 12.0.5+) | `track_index` (an audio track), `clip_slot_index`, `file_path` (absolute), `name?`, `warping?` / `looping?` (default off), `replace?` |
| `get_track_meter` | `track_index` → `peak`, `master_peak`, `mute`, `volume`, `soloed_elsewhere`, `master_volume`, `is_playing` |
| `batch` | `commands: [{cmd, params}]`, `stop_on_error?` |
| `get_session_info`, `get_browser_tree`, `get_browser_items_at_path`, `load_browser_item` | same as AbletonMCP |

### Events

`song.tempo`, `song.is_playing`, `song.tracks`, `song.scenes`, `song.key`, `track.name`,
`track.devices`, `track.playing` (playing/fired slot), `beat` (once per beat while playing).

The Pulse Studio server re-publishes these as Server-Sent Events at `GET /live/events`.
Related endpoints: `GET /bridge/status` and `GET /live/snapshot`.

### Capabilities

`get_capabilities` reports the Live and Python versions and API features
(`note_extended`, `song_key`, `undo_steps`, `groove_pool`, `max_for_live_content`).
It also lists the stock instruments, audio effects and MIDI effects found, plus:

- `edition_guess`: `suite` / `standard` / `intro_or_lite`, inferred from the stock instruments.
- `track_limit_hint`: 8 when Intro/Lite is likely. Lite allows 8 tracks and Intro 16, and the two can't be told apart.
- `defaults`: fallback instruments that exist in this edition, e.g. `Drift` when there is no `Wavetable`.

## Development

The script must stay **Python 3.7 compatible** (Live 11); Live 12 runs 3.11.
Tests run the real script against a fake Live object model:

```
python -m pytest tests/test_pulse_bridge.py -q
```

After editing, re-run `install.py`, then in Live set the Control Surface slot to None and back
to PulseBridge. The script reloads its own modules on each selection; Live itself caches
Remote Script modules, so versions before 0.1.0's self-reload need a Live restart.
Script errors go to Live's `Log.txt`. On Windows that is
`%APPDATA%\Ableton\Live <version>\Preferences\Log.txt`.
