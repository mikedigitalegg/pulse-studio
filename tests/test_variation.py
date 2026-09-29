"""
Tests for per-loop and per-phrase variation: Live's note chance / velocity spread on ghost hits,
and the small moves written into the last beat of each phrase.

    python -m pytest tests/test_variation.py -q
"""
import os
import random
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402


def _lane(hits, bars=1, vel=100):
    out = [0] * (bars * 16)
    for h in hits:
        out[h] = vel
    return out


def _every_bar(steps, bars):
    return [b * 16 + s for b in range(bars) for s in steps]


def _parts(bars=8):
    drums = {"bars": bars, "step_division": "1/16", "lanes": {
        "kick": _lane(_every_bar([0, 4, 8, 12], bars), bars, 127),
        "clap": _lane(_every_bar([4, 12], bars), bars),
        "ch": _lane(_every_bar([0, 2, 4, 6, 8, 10, 12, 14], bars), bars, 80),
    }}
    for i in _every_bar([0, 4, 8, 12], bars):
        drums["lanes"]["ch"][i] = 64  # softer on-beat hats, as the groove's off-beat accents leave them
    perc = {"bars": bars, "step_division": "1/16", "lanes": {"perc1": _lane(_every_bar([3, 11], bars), bars, 70)}}
    stabs = {"bars": bars, "step_division": "1/16", "lanes": {"stab": _lane(_every_bar([6], bars), bars, 90)}}
    steps = [43 if i % 4 == 2 else 0 for i in range(bars * 16)]
    bass = {"bars": bars, "step_division": "1/16", "root_midi": 43, "steps": steps, "velocities": [90 if n else 0 for n in steps]}
    return {"drums": drums, "perc": perc, "stabs": stabs, "bass": bass}


class FakeCtrl:
    def __init__(self):
        self.notes = []

    def create_clip(self, *a):
        pass

    def add_note(self, track, slot, pitch, start, duration, vel, **kw):
        self.notes.append({"pitch": pitch, "start": start, "vel": vel, **kw})


@pytest.fixture
def fake_ctrl(monkeypatch):
    fc = FakeCtrl()
    monkeypatch.setattr(srv, "ctrl", fc)
    return fc


# ---------------------------------------------------------------- note chance + spread

def test_chance_skips_anchors_accents_and_loud_hits():
    drums = _parts(1)["drums"]
    chance = srv._note_chance(drums, "techno", {"ch": 0.8, "kick": 0.5, "clap": 0.5})
    assert "kick" not in chance and "clap" not in chance  # every hit is an anchor
    ch = chance["ch"]
    assert ch[2] == 1.0 and ch[6] == 1.0  # accent steps [2, 6, 10, 14] always play
    assert ch[0] == ch[4] == ch[8] == 0.8  # on-beat ghost hats roll the dice


def test_uniform_lane_with_no_accents_stays_firm():
    perc = {"bars": 1, "step_division": "1/16", "lanes": {"perc2": _lane([1, 5, 9], vel=90)}}
    assert srv._note_chance(perc, "techno", {"perc2": 0.7}) == {}


def test_style_override_merges_over_defaults():
    cfg = srv._style_variation("dnb")
    assert cfg["chance"]["snare"] == 0.6  # dnb ghost snares
    assert cfg["chance"]["stab"] == srv.DEFAULT_VARIATION["chance"]["stab"]  # untouched default kept
    assert "perc_fill" not in cfg["drum_moves"]


def test_writer_passes_chance_and_spread(fake_ctrl):
    parts = srv._stamp_note_variation(_parts(1), "techno")
    srv._write_pattern_to_ableton(0, 0, parts["drums"])
    hats = [n for n in fake_ctrl.notes if n["pitch"] == srv.LANE_TO_MIDI_NOTE["ch"]]
    assert all(n["velocity_spread"] == 14 for n in hats)
    assert any(n["probability"] < 1.0 for n in hats)
    kicks = [n for n in fake_ctrl.notes if n["pitch"] == srv.LANE_TO_MIDI_NOTE["kick"]]
    assert kicks and all("probability" not in n for n in kicks)  # no spread or chance: plain notes


def test_stab_chords_never_get_chance(fake_ctrl):
    stabs = {"bars": 1, "step_division": "1/16", "lanes": {"stab": _lane([2, 6], vel=60)}, "voicings": [[60, 63, 67]],
             "chance": {"stab": [0.5] * 16}, "spread": {"stab": 10}}
    stabs["lanes"]["stab"][6] = 100
    srv._write_pattern_to_ableton(3, 0, stabs)
    assert len(fake_ctrl.notes) == 6 and all(n["probability"] == 1.0 for n in fake_ctrl.notes)


def test_bass_gets_spread(fake_ctrl):
    parts = srv._stamp_note_variation(_parts(1), "techno")
    srv._write_bassline_to_ableton(1, 0, parts["bass"])
    assert fake_ctrl.notes and all(n["velocity_spread"] == 8 for n in fake_ctrl.notes)


# ---------------------------------------------------------------- phrase-end moves

def test_last_bar_always_varies_and_other_bars_are_untouched():
    src = _parts(8)
    for seed in range(20):
        out = srv._apply_phrase_moves(src, "techno", random.Random(seed))
        touched = set(out["drums"].get("phrase_steps", [])) | set(out["perc"].get("phrase_steps", []))
        assert set(range(7 * 16 + 12, 8 * 16)) <= touched, seed
        assert touched <= set(range(3 * 16 + 12, 4 * 16)) | set(range(7 * 16 + 12, 8 * 16)), seed
    assert src["drums"]["lanes"]["ch"] == _parts(8)["drums"]["lanes"]["ch"]  # input not mutated


def test_single_bar_clip_has_no_moves():
    src = _parts(1)
    assert srv._apply_phrase_moves(src, "techno", random.Random(1)) is src


def test_moves_only_touch_lanes_already_playing():
    parts = _parts(2)
    parts["drums"]["lanes"] = {"kick": parts["drums"]["lanes"]["kick"]}  # an intro: kick only
    parts["perc"] = None
    for seed in range(10):
        out = srv._apply_phrase_moves(parts, "techno", random.Random(seed))
        assert set(out["drums"]["lanes"]) == {"kick"}
        assert not any(out["drums"]["lanes"]["kick"][28:32])  # the only move left is a kick gap


def test_hat_roll_and_bass_moves():
    beat = range(12, 16)
    drums = _parts(1)["drums"]
    assert srv._drum_move("hat_roll", drums, None, beat)
    assert drums["lanes"]["ch"][12:16] == [70, 83, 97, 110]

    bass = _parts(1)["bass"]
    assert srv._bass_move("octave_up", bass, beat) and bass["steps"][14] == 55
    bass = _parts(1)["bass"]
    assert srv._bass_move("rest", bass, beat) and bass["steps"][14] == 0 and bass["velocities"][14] == 0


def test_stab_echo_repeats_last_hit_softer():
    stabs = _parts(1)["stabs"]
    assert srv._stab_move("echo", stabs, range(12, 16))
    assert stabs["lanes"]["stab"][14] == 63


def test_moves_can_be_switched_off_per_style(monkeypatch):
    monkeypatch.setitem(srv.STYLE_CONFIG["techno"]["groove"], "variation", {"phrase_bars": 0})
    src = _parts(8)
    assert srv._apply_phrase_moves(src, "techno", random.Random(0)) is src


def test_phrase_steps_are_exempt_from_chance():
    drums = _parts(4)["drums"]
    out = srv._apply_phrase_moves({"drums": drums}, "techno", random.Random(3))
    chance = srv._note_chance(out["drums"], "techno", {"ch": 0.5})
    for i in out["drums"].get("phrase_steps", []):
        assert chance.get("ch", [1.0] * 64)[i] == 1.0


# ---------------------------------------------------------------- LiveLink

def test_live_link_sends_chance_and_centred_spread():
    from live_link import LiveLink

    sent = []
    link = LiveLink(types.SimpleNamespace(connected=True, send=lambda cmd, p: sent.append((cmd, p))))
    link.add_note(0, 1, 42, 0.5, 0.1, 80, probability=0.75, velocity_spread=14)
    cmd, params = sent[0]
    note = params["notes"][0]
    assert cmd == "add_notes" and note["probability"] == 0.75
    assert note["velocity"] == 73 and note["velocity_deviation"] == 14


def test_live_link_plain_note_without_bridge_uses_osc():
    from live_link import LiveLink

    link = LiveLink(types.SimpleNamespace(connected=False))
    sent = []
    link.client = types.SimpleNamespace(send_message=lambda a, args: sent.append((a, args)))
    link.add_note(0, 1, 42, 0.5, 0.1, 80, probability=0.5, velocity_spread=10)
    assert sent and sent[0][0] == "/live/clip/add/notes" and len(sent[0][1]) == 7
