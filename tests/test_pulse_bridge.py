"""
End-to-end tests for PulseBridge without Ableton: the real Remote Script code runs
against a fake Live object model, with a thread standing in for Live's main-thread tick,
and the real PulseBridgeClient / LiveLink talk to it over TCP.

    python -m pytest tests/test_pulse_bridge.py -q
"""
import os
import socket
import sys
import threading
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pulse_bridge"))


# ---------------------------------------------------------------- fake Live object model

class Observable:
    """Supports Live's add_<prop>_listener / remove_<prop>_listener / <prop>_has_listener."""

    def __init__(self):
        object.__setattr__(self, "_listeners", {})

    def __getattr__(self, name):
        ls = object.__getattribute__(self, "_listeners")
        if name.startswith("add_") and name.endswith("_listener"):
            return lambda cb, p=name[4:-9]: ls.setdefault(p, []).append(cb)
        if name.startswith("remove_") and name.endswith("_listener"):
            return lambda cb, p=name[7:-9]: ls.get(p, []).remove(cb)
        if name.endswith("_has_listener"):
            return lambda cb, p=name[:-13]: cb in ls.get(p, [])
        raise AttributeError(name)

    def notify(self, prop):
        for cb in list(self._listeners.get(prop, [])):
            cb()


class Param:
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0):
        self.name, self.value, self.min, self.max, self.is_quantized = name, value, lo, hi, False


class Pad:
    def __init__(self, note, name, filled=True):
        self.note, self.name, self.chains = note, name, [object()] if filled else []


class Device:
    def __init__(self, name, pads=None):
        self.name = self.class_name = name
        self.type = 1  # instrument
        self.parameters = [Param("Device On", 1.0), Param("Macro 1", 0.0, 0.0, 127.0)]
        self.can_have_drum_pads = pads is not None
        self.drum_pads = [Pad(n, "", False) for n in range(128)]
        for note, pad_name in (pads or {}).items():
            self.drum_pads[note] = Pad(note, pad_name)
        self.view = types.SimpleNamespace(selected_drum_pad=None)


class Clip:
    def __init__(self, length):
        self.length, self.name, self.notes = length, "", []

    def set_notes(self, notes):
        self.notes.extend(notes)

    def get_notes(self, t0, p0, span, pspan):
        return tuple(self.notes)


class AudioClip:
    def __init__(self, file_path):
        self.file_path, self.name, self.length = file_path, "", 8.0
        self.warping, self.looping = True, True

    @property
    def looping(self):
        return self._looping

    @looping.setter
    def looping(self, value):
        if value and not getattr(self, "warping", True):
            raise RuntimeError("Live only loops warped clips")
        self._looping = value


class Slot:
    def __init__(self, audio=False):
        self.clip, self.fired, self.audio = None, 0, audio

    def create_audio_clip(self, file_path):
        assert self.clip is None, "Live raises if the slot is occupied"
        if not self.audio:
            raise RuntimeError("not an audio track")
        self.clip = AudioClip(file_path)

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        assert self.clip is None, "Live raises if the slot is occupied"
        self.clip = Clip(length)

    def delete_clip(self):
        self.clip = None

    def fire(self):
        self.fired += 1

    def stop(self):
        pass


class Mixer:
    def __init__(self):
        self.volume, self.panning = Param("Volume", 0.85), Param("Pan", 0.0, -1.0, 1.0)


class Track(Observable):
    def __init__(self, name, n_scenes, audio=False):
        super().__init__()
        self.name, self.devices, self.mixer_device = name, [], Mixer()
        self.clip_slots = [Slot(audio) for _ in range(n_scenes)]
        self.has_midi_input, self.mute, self.solo, self.arm, self.can_be_armed = not audio, False, False, False, True
        self.playing_slot_index = self.fired_slot_index = -1
        self.has_audio_output, self.output_meter_left, self.output_meter_right = True, 0.0, 0.0

    def delete_device(self, index):
        del self.devices[index]


class Scene:
    def __init__(self, name=""):
        self.name, self.fired = name, 0

    def fire(self):
        self.fired += 1


class Song(Observable):
    def __init__(self):
        super().__init__()
        self.tempo, self.is_playing, self.current_song_time = 120.0, False, 0.0
        self.signature_numerator = self.signature_denominator = 4
        self.scenes = [Scene() for _ in range(2)]
        self.tracks = [Track("1-MIDI", 2), Track("2-MIDI", 2)]
        self.return_tracks, self.master_track = [], Track("Master", 0)
        self.view = types.SimpleNamespace(selected_track=None, selected_device=None)
        self.view.select_device = lambda d: setattr(self.view, "selected_device", d)
        self.root_note, self.scale_name = 0, "Major"
        self.undo_depth = 0

    def begin_undo_step(self):
        self.undo_depth += 1

    def end_undo_step(self):
        self.undo_depth -= 1

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False

    def create_midi_track(self, index):
        t = Track("MIDI", len(self.scenes))
        self.tracks.insert(len(self.tracks) if index < 0 else index, t)
        self.notify("tracks")

    def create_audio_track(self, index):
        t = Track("Audio", len(self.scenes), audio=True)
        self.tracks.insert(len(self.tracks) if index < 0 else index, t)
        self.notify("tracks")

    def create_scene(self, index):
        self.scenes.append(Scene())
        for t in self.tracks:
            t.clip_slots.append(Slot(not t.has_midi_input))


class Item:
    def __init__(self, name, children=(), loadable=True, device=False):
        self.name, self.children, self.is_loadable, self.is_device = name, list(children), loadable, device
        self.uri = "query:" + name.replace(" ", "")


class Browser:
    def __init__(self, instruments):
        self.instruments = Item("Instruments", [Item(n, [Item(n + " Preset")], device=True) for n in instruments], False)
        self.drums = Item("Drums", [Item("909 Kit"), Item("909 Core Kit.adg")], False)
        self.audio_effects = Item("Audio Effects", [Item("Reverb", device=True)], False)
        self.midi_effects = Item("MIDI Effects", [Item("Arpeggiator", device=True)], False)
        self.sounds = Item("Sounds", [], False)
        self.samples = Item("Samples", [Item("Kick Deep.wav"), Item("Clap Wide.wav")], False)
        self.loaded = []
        self._song = None

    def load_item(self, item):
        self.loaded.append(item.name)
        track = self._song.view.selected_track
        if item.name.endswith(".wav"):
            # A sample goes onto the selected pad of a selected Drum Rack, else into a new Simpler.
            rack = self._song.view.selected_device
            pad = rack.view.selected_drum_pad if rack is not None and rack in track.devices else None
            if pad is not None:
                pad.chains, pad.name = [object()], item.name[:-4]
            else:
                track.devices.append(Device("Simpler"))
            return
        pads = {36: "Kick 909", 38: "Snare 909", 42: "Hihat Closed 909"} if item.name.endswith(".adg") else None
        track.devices.append(Device(item.name.replace(".adg", ""), pads))


class App:
    def __init__(self, browser):
        self.browser = browser

    def get_major_version(self):
        return 12

    def get_minor_version(self):
        return 3

    def get_bugfix_version(self):
        return 7


# ---------------------------------------------------------------- fixtures

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(monkeypatch):
    """Start the real PulseBridge surface on a fake Live; yields (surface, song, browser, port)."""
    instruments = os.environ.get("FAKE_INSTRUMENTS", "Drift,Drum Rack,Operator,Simpler,Wavetable").split(",")
    song = Song()
    browser = Browser(instruments)
    browser._song = song
    app = App(browser)

    class ControlSurface:
        def __init__(self, c_instance):
            pass

        def song(self):
            return song

        def application(self):
            return app

        def log_message(self, msg):
            pass

        def show_message(self, msg):
            pass

        def update_display(self):
            pass

        def disconnect(self):
            pass

    fw = types.ModuleType("_Framework")
    cs = types.ModuleType("_Framework.ControlSurface")
    cs.ControlSurface = ControlSurface
    monkeypatch.setitem(sys.modules, "_Framework", fw)
    monkeypatch.setitem(sys.modules, "_Framework.ControlSurface", cs)
    for mod in [m for m in sys.modules if m == "PulseBridge" or m.startswith("PulseBridge.")]:
        monkeypatch.delitem(sys.modules, mod)

    port = _free_port()
    monkeypatch.setenv("PULSE_BRIDGE_PORT", str(port))
    import PulseBridge

    surface = PulseBridge.create_instance(None)
    stop = threading.Event()

    def main_thread():  # stands in for Live calling update_display ~10 Hz (faster here)
        while not stop.is_set():
            surface.update_display()
            time.sleep(0.005)

    th = threading.Thread(target=main_thread, daemon=True)
    th.start()
    yield surface, song, browser, port
    stop.set()
    th.join(1)
    surface.disconnect()


@pytest.fixture
def client(live):
    from pulse_bridge_client import PulseBridgeClient

    c = PulseBridgeClient("127.0.0.1", live[3], reconnect_s=0.1).start()
    assert c.wait_connected(3)
    deadline = time.time() + 3
    while c.capabilities is None and time.time() < deadline:
        time.sleep(0.01)
    yield c
    c.close()


# ---------------------------------------------------------------- tests

def test_handshake_and_capabilities(client):
    assert client.hello["bridge"] == "PulseBridge"
    assert client.hello["live_version"] == "12.3.7"
    caps = client.capabilities
    assert caps["edition_guess"] == "suite"
    assert caps["defaults"] == {"drums": "Drum Rack", "melodic": "Wavetable"}
    assert caps["features"]["song_key"] is True


def test_intro_edition_falls_back_to_drift(monkeypatch, live):
    from PulseBridge.commands import Commands

    live[2].instruments.children = [Item(n, device=True) for n in ("Drift", "Drum Rack", "Simpler", "Impulse")]
    caps = Commands(live[0]).get_capabilities({})
    assert caps["edition_guess"] == "intro_or_lite"
    assert caps["track_limit_hint"] == 8
    assert caps["defaults"]["melodic"] == "Drift"


def test_write_clip_creates_scenes_and_notes(client, live):
    song = live[1]
    res = client.request("write_clip", {
        "track_index": 1, "clip_slot_index": 4, "length": 8, "name": "Bass",
        "notes": [{"pitch": 43, "start_time": 0, "duration": 0.25, "velocity": 200}],
    })
    assert res["note_count"] == 1
    assert len(song.scenes) == 5  # slot 4 needed three new scenes
    clip = song.tracks[1].clip_slots[4].clip
    assert clip.name == "Bass" and clip.length == 8
    assert clip.notes == [(43, 0.0, 0.25, 127.0, False)]  # velocity clamped
    client.request("ping")  # replies go out mid-tick; the undo step closes at the tick's end
    assert song.undo_depth == 0


def test_note_extended_api_used_when_available(client, live, monkeypatch):
    import PulseBridge.commands as commands

    made = []

    class Spec:
        def __init__(self, **kw):
            made.append(kw)

    monkeypatch.setattr(commands, "Live", types.SimpleNamespace(Clip=types.SimpleNamespace(MidiNoteSpecification=Spec)))
    slot = live[1].tracks[0].clip_slots[0]
    slot.create_clip(4)
    slot.clip.add_new_notes = lambda specs: None
    client.request("add_notes", {"track_index": 0, "clip_slot_index": 0, "notes": [
        {"pitch": 36, "start_time": 0, "duration": 0.25, "velocity": 100, "probability": 0.5, "velocity_deviation": 20},
    ]})
    assert made[0]["probability"] == 0.5 and made[0]["velocity_deviation"] == 20.0


def test_live_link_routes_osc_calls_in_order(client, live):
    from live_link import LiveLink

    song = live[1]
    link = LiveLink(client)
    link.create_clip(0, 1, 4.0)  # delete + create, fire-and-forget
    for i in range(16):
        link.add_note(0, 1, 36, i * 0.25, 0.1, 100)
    link.set_clip_name(0, 1, "Kick")
    link.set_tempo(133)
    # A query is answered after the queued sends, so it observes all of them.
    assert link.query("/live/song/get/tempo", [])["args"] == (133.0,)
    clip = song.tracks[0].clip_slots[1].clip
    assert len(clip.notes) == 16 and clip.name == "Kick"
    assert link.query("/live/clip_slot/get/has_clip", [0, 1])["args"] == (0, 1, True)
    assert link.query("/live/track/get/num_devices", [0])["args"] == (0, 0)


def test_live_link_falls_back_to_osc_when_bridge_down():
    from live_link import LiveLink
    from pulse_bridge_client import PulseBridgeClient

    link = LiveLink(PulseBridgeClient("127.0.0.1", _free_port()))  # never started
    sent = []
    link.client = types.SimpleNamespace(send_message=lambda a, args: sent.append(a))
    link.play()
    assert sent == ["/live/song/start_playing"]
    assert link.query("/live/song/get/tempo", []) is None


def test_load_device_by_name_uses_browser(client, live):
    song, browser = live[1], live[2]
    res = client.request("load_device", {"track_index": 1, "device_name": "drift"})
    assert res["item_name"] == "Drift"
    assert [d.name for d in song.tracks[1].devices] == ["Drift"]
    with pytest.raises(Exception, match="device_not_found"):
        client.request("load_device", {"track_index": 1, "device_name": "Nonexistent"})


def test_abletonmcp_compatible_browser_commands(client, live):
    tree = client.request("get_browser_tree", {"category_type": "all"})
    assert "instruments" in tree["available_categories"]
    assert any(c.get("path") == "instruments" for c in tree["categories"])
    items = client.request("get_browser_items_at_path", {"path": "instruments"})
    drift = next(i for i in items["items"] if i["name"] == "Drift")
    assert drift["is_folder"] and drift["path"] == "instruments/Drift"
    presets = client.request("get_browser_items_at_path", {"path": "instruments/Drift"})
    uri = presets["items"][0]["uri"]
    res = client.request("load_browser_item", {"track_index": 0, "item_uri": uri})
    assert res["item_name"] == "Drift Preset"


def test_load_by_path_track_device_types_and_drum_pads(client, live):
    song = live[1]
    song.tracks[0].devices.append(Device("Drum Rack", pads={}))  # empty rack
    info = client.request("get_track", {"track_index": 0})
    assert info["devices"][0]["is_drum_rack"] and info["devices"][0]["filled_pads"] == 0
    assert info["devices"][0]["type"] == "instrument"

    client.request("delete_device", {"track_index": 0, "device_index": 0})
    res = client.request("load_item_at_path", {"track_index": 0, "path": "drums", "name": "909 Core Kit.adg"})
    assert res["item_name"] == "909 Core Kit.adg"
    pads = client.request("get_drum_pads", {"track_index": 0})
    assert pads["rack"] == "909 Core Kit"
    assert {p["note"]: p["name"] for p in pads["pads"]} == {36: "Kick 909", 38: "Snare 909", 42: "Hihat Closed 909"}

    with pytest.raises(Exception, match="item_not_found"):
        client.request("load_item_at_path", {"track_index": 0, "path": "drums", "name": "Nope.adg"})
    with pytest.raises(Exception, match="path_not_found"):
        client.request("load_item_at_path", {"track_index": 0, "path": "sounds/Nope", "name": "x"})
    assert client.request("get_drum_pads", {"track_index": 1}) == {"track_index": 1, "rack": None, "pads": []}


def test_load_sample_onto_drum_pad(client, live):
    song = live[1]
    song.tracks[0].devices.append(Device("Drum Rack", pads={36: "Kick 909"}))
    res = client.request("load_item_to_drum_pad", {"track_index": 0, "pad_note": 38, "path": "samples", "name": "Clap Wide.wav"})
    assert res["pad_filled"] and res["rack_intact"] and res["pad_name"] == "Clap Wide"
    pads = client.request("get_drum_pads", {"track_index": 0})
    assert {p["note"]: p["name"] for p in pads["pads"]} == {36: "Kick 909", 38: "Clap Wide"}
    assert len(song.tracks[0].devices) == 1

    with pytest.raises(Exception, match="no_drum_rack"):
        client.request("load_item_to_drum_pad", {"track_index": 1, "path": "samples", "name": "Kick Deep.wav"})
    with pytest.raises(Exception, match="pad_out_of_range"):
        client.request("load_item_to_drum_pad", {"track_index": 0, "pad_note": 200, "path": "samples", "name": "Kick Deep.wav"})
    with pytest.raises(Exception, match="item_not_found"):
        client.request("load_item_to_drum_pad", {"track_index": 0, "path": "samples", "name": "Nope.wav"})


def test_find_free_slot(client, live):
    song = live[1]
    song.tracks[1].clip_slots[0].create_clip(4)
    assert client.request("find_free_slot", {"track_index": 1})["slot"] == 1
    song.tracks[1].clip_slots[1].create_clip(4)
    res = client.request("find_free_slot", {"track_index": 1})
    assert res == {"track_index": 1, "slot": 2, "needs_scene": True}


def test_events_pushed_to_subscribers(client, live):
    song = live[1]
    got = []
    client.on_event(lambda e, d: got.append((e, d)))
    song.tempo = 128.0
    song.notify("tempo")
    song.is_playing = True
    song.current_song_time = 5.2
    client.request("create_midi_track", {"index": -1})
    deadline = time.time() + 2
    names = lambda: [e for e, _ in got]
    while time.time() < deadline and not {"song.tempo", "song.tracks", "beat"} <= set(names()):
        time.sleep(0.01)
    assert ("song.tempo", {"tempo": 128.0}) in got
    beat = next(d for e, d in got if e == "beat")
    assert beat["beat"] == 5 and beat["bar"] == 1 and beat["beat_in_bar"] == 1
    assert next(d for e, d in got if e == "song.tracks")["num_tracks"] == 3


def test_meters_only_while_requested_and_not_when_silent(client, live):
    song = live[1]
    got = []
    client.on_event(lambda e, d: got.append(d) if e == "meters" else None)
    song.tracks[0].output_meter_left, song.tracks[0].output_meter_right = 0.2, 0.6
    song.tracks[1].has_audio_output = False  # MIDI track with no instrument
    song.master_track.output_meter_left = 0.5
    time.sleep(0.1)
    assert got == []  # not requested yet

    client.request("set_meters", {"enabled": True})
    deadline = time.time() + 2
    while not got and time.time() < deadline:
        time.sleep(0.01)
    assert got[-1] == {"master": [0.5, 0.0], "tracks": [0.6, 0.0]}

    # Silence: one all-zero frame, then nothing.
    song.tracks[0].output_meter_left = song.tracks[0].output_meter_right = 0.0
    song.master_track.output_meter_left = 0.0
    time.sleep(0.15)
    n = len(got)
    assert got[-1] == {"master": [0.0, 0.0], "tracks": [0.0, 0.0]}
    time.sleep(0.15)
    assert len(got) == n


def test_errors_and_batch(client):
    from pulse_bridge_client import BridgeError

    with pytest.raises(BridgeError, match="unknown_command"):
        client.request("nope")
    with pytest.raises(BridgeError, match="track_index_out_of_range"):
        client.request("get_track", {"track_index": 99})
    res = client.request("batch", {"commands": [
        {"cmd": "set_tempo", "params": {"tempo": 90}},
        {"cmd": "get_track", "params": {"track_index": 99}},
        {"cmd": "get_song"},
    ]})
    assert [r["ok"] for r in res] == [True, False, True]
    assert res[2]["result"]["tempo"] == 90.0


def test_reconnects_after_bridge_restart(live, monkeypatch):
    from pulse_bridge_client import PulseBridgeClient

    c = PulseBridgeClient("127.0.0.1", live[3], reconnect_s=0.05).start()
    assert c.wait_connected(3)
    for conn in live[0]._server.connections():
        conn.close()
    deadline = time.time() + 3
    while time.time() < deadline:
        time.sleep(0.05)
        if c.connected:
            try:
                assert c.request("ping")["pong"]
                break
            except Exception:
                pass
    else:
        pytest.fail("client did not reconnect")
    c.close()


def test_load_audio_clip_on_audio_track(client, live, tmp_path):
    _, song, _, _ = live
    wav = tmp_path / "vox_hands_up.wav"
    wav.write_bytes(b"RIFF")
    idx = client.request("create_audio_track", {"index": -1})["index"]
    res = client.request("load_audio_clip", {"track_index": idx, "clip_slot_index": 3, "file_path": str(wav), "name": "hands up"})
    clip = song.tracks[idx].clip_slots[3].clip
    assert clip.file_path == str(wav) and clip.name == "hands up"
    assert (clip.warping, clip.looping) == (False, False)  # one-shot: natural speed, plays once
    assert res["clip_slot_index"] == 3 and len(song.scenes) >= 4

    # Replaces an existing clip by default.
    client.request("load_audio_clip", {"track_index": idx, "clip_slot_index": 3, "file_path": str(wav), "name": "again"})
    assert song.tracks[idx].clip_slots[3].clip.name == "again"


def test_load_audio_clip_errors(client, live, tmp_path):
    from pulse_bridge_client import BridgeError

    _, song, _, _ = live
    wav = tmp_path / "v.wav"
    wav.write_bytes(b"RIFF")
    with pytest.raises(BridgeError, match="not_audio_track"):
        client.request("load_audio_clip", {"track_index": 0, "clip_slot_index": 0, "file_path": str(wav)})
    idx = client.request("create_audio_track", {"index": -1})["index"]
    with pytest.raises(BridgeError, match="file_not_found"):
        client.request("load_audio_clip", {"track_index": idx, "clip_slot_index": 0, "file_path": str(tmp_path / "missing.wav")})

    class OldSlot(Slot):  # Live before 12.0.5 has no create_audio_clip
        create_audio_clip = property(lambda self: (_ for _ in ()).throw(AttributeError()))

    song.tracks[idx].clip_slots[0] = OldSlot(audio=True)
    with pytest.raises(BridgeError, match="unsupported"):
        client.request("load_audio_clip", {"track_index": idx, "clip_slot_index": 0, "file_path": str(wav)})


def test_get_track_meter_reports_level_and_mixer_state(client, live):
    _, song, _, _ = live
    song.tracks[0].output_meter_left, song.tracks[0].output_meter_right = 0.2, 0.4
    song.tracks[1].solo = True
    res = client.request("get_track_meter", {"track_index": 0})
    assert res["peak"] == pytest.approx(0.4)
    assert res["soloed_elsewhere"] is True and res["mute"] is False
