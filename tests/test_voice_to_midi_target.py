"""
/voice_to_midi/apply checks its destination before writing: the track must exist, be MIDI and
have a sounding instrument; pitch and slot are picked automatically when left out.

    python -m pytest tests/test_voice_to_midi_target.py -q
"""
import math
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

SR = 24000


class FakeBridge:
    connected = True

    def __init__(self, tracks):
        self.tracks = tracks

    def request(self, cmd, params=None, timeout_s=5.0):
        ti = (params or {}).get("track_index")
        if ti is None or ti >= len(self.tracks):
            raise RuntimeError(f"track_index_out_of_range: {ti}")
        t = self.tracks[ti]
        if cmd == "get_track":
            return {"name": t["name"], "is_midi": t["midi"], "devices": t["devices"]}
        if cmd == "get_drum_pads":
            return {"pads": [{"note": n, "name": ""} for n in t.get("pads", [])]}
        if cmd == "find_free_slot":
            return {"slot": 3}
        raise AssertionError(cmd)


class FakeCtrl:
    def __init__(self):
        self.clips, self.notes, self.fired = [], [], []

    def create_clip(self, track, slot, length_beats=4.0):
        self.clips.append((track, slot))

    def add_notes(self, track, slot, notes):
        self.notes.extend(notes)

    def fire_clip(self, track, slot):
        self.fired.append((track, slot))


KIT = {"index": 0, "name": "909 Core Kit", "type": "instrument", "is_drum_rack": True, "filled_pads": 16}
EMPTY_RACK = {"index": 0, "name": "Drum Rack", "type": "instrument", "is_drum_rack": True, "filled_pads": 0}
SYNTH = {"index": 0, "name": "Drift", "type": "instrument"}
TRACKS = [
    {"name": "PS-TRK-01", "midi": True, "devices": [KIT], "pads": [36, 38, 42]},
    {"name": "PS-TRK-02", "midi": True, "devices": [SYNTH]},
    {"name": "PS-VOX", "midi": False, "devices": []},
    {"name": "7-MIDI", "midi": True, "devices": [EMPTY_RACK]},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "AUDIO_DIR", str(tmp_path))
    pcm = []
    for _ in range(4):  # four short bursts -> four onsets
        pcm += [int(8000 * math.sin(i / 3.0)) for i in range(int(SR * 0.05))] + [0] * int(SR * 0.2)
    (tmp_path / "v.wav").write_bytes(srv._wav_from_pcm(pcm, sample_rate=SR, channels=1))
    fc = FakeCtrl()
    monkeypatch.setattr(srv, "ctrl", fc)
    monkeypatch.setattr(srv, "BRIDGE", FakeBridge(TRACKS))
    monkeypatch.setattr(srv, "ensure_tracks", lambda *a, **k: pytest.fail("must not create tracks"))
    return fc


def _apply(**kw):
    return srv.voice_to_midi_apply(srv.VoiceToMidiRequest(source_url="/audio_files/v.wav", **kw))


def test_drum_rack_uses_first_filled_pad_and_free_slot(env):
    res = _apply(track_index=0)
    assert res["ok"], res
    assert (res["base_pitch"], res["clip_slot_index"], res["instrument"], res["track_name"]) == (36, 3, "909 Core Kit", "PS-TRK-01")
    assert env.clips == [(0, 3)] and env.notes and all(n[0] == 36 for n in env.notes)


def test_synth_defaults_to_60_and_explicit_values_win(env):
    assert _apply(track_index=1)["base_pitch"] == 60
    res = _apply(track_index=1, base_pitch=48, clip_slot_index=0)
    assert (res["base_pitch"], res["clip_slot_index"]) == (48, 0)


@pytest.mark.parametrize("ti,error", [(9, "no_such_track"), (2, "not_midi_track"), (3, "no_instrument")])
def test_bad_targets_are_refused_without_writing(env, ti, error):
    res = _apply(track_index=ti)
    assert res["ok"] is False and res["error"] == error
    assert env.clips == []


def test_without_bridge_writes_as_requested(env, monkeypatch):
    monkeypatch.setattr(srv, "BRIDGE", types.SimpleNamespace(connected=False))
    res = _apply(track_index=5)
    assert res["ok"] and (res["clip_slot_index"], res["base_pitch"]) == (0, 60)
    assert res["warnings"]
