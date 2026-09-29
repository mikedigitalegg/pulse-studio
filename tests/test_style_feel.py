"""
Tests for style-specific feel from knowledge/styles.json: drum groove skeletons, data-driven
bass shaping, and the chord track's scale / voicing / rhythm.

    python -m pytest tests/test_style_feel.py -q
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

with open(os.path.join(ROOT, "knowledge", "styles.json"), encoding="utf-8") as f:
    STYLES = json.load(f)["styles"]


def _lane(hits, bars=1, vel=100):
    out = [0] * (bars * 16)
    for h in hits:
        out[h] = vel
    return out


def _drums(**lanes):
    return {"bars": 1, "step_division": "1/16", "lanes": {k: _lane(v) for k, v in lanes.items()}}


def _bass(steps, root=43):
    return {"bars": 1, "step_division": "1/16", "root_midi": root, "steps": list(steps), "velocities": [90 if n else 0 for n in steps]}


class FakeCtrl:
    def __init__(self):
        self.notes = []

    def create_clip(self, *a):
        pass

    def add_note(self, track, slot, pitch, start, duration, vel, **kw):
        self.notes.append((pitch, start, duration, vel))


@pytest.fixture
def fake_ctrl(monkeypatch):
    fc = FakeCtrl()
    monkeypatch.setattr(srv, "ctrl", fc)
    return fc


@pytest.fixture
def no_dropouts(monkeypatch):
    monkeypatch.setattr(srv.random, "random", lambda: 0.0)


# ---------------------------------------------------------------- styles.json coverage

@pytest.mark.parametrize("key", sorted(STYLES))
def test_every_style_defines_feel(key):
    style = STYLES[key]
    assert style["groove"]["anchors"], key
    assert "density_keep" in style["bass_profile"], key
    hp = style["harmony_profile"]
    assert hp["voicing"] in srv.CHORD_VOICINGS, key
    assert hp["rhythm"] in srv.CHORD_RHYTHMS, key
    assert hp["scale"] in srv.SCALE_INTERVALS, key


# ---------------------------------------------------------------- groove skeletons

def test_exact_kick_is_four_on_the_floor():
    out = srv._apply_groove_to_drums(_drums(kick=[0, 3, 8]), "techno")
    assert srv._hit_steps(out["lanes"]["kick"], 16) == [0, 4, 8, 12]


def test_required_hits_are_added_and_extras_kept():
    out = srv._apply_groove_to_drums(_drums(kick=[0, 4, 8, 12], clap=[12, 15]), "house")
    assert srv._hit_steps(out["lanes"]["clap"], 16) == [4, 12, 15]
    assert srv._hit_steps(out["lanes"]["oh"], 16) == [2, 6, 10, 14]


def test_forbidden_steps_are_cleared():
    out = srv._apply_groove_to_drums(_drums(kick=[0, 8, 10], clap=[4, 12]), "garage")
    assert srv._hit_steps(out["lanes"]["kick"], 16) == [0, 10]


def test_accents_shape_hat_velocity():
    out = srv._apply_groove_to_drums(_drums(kick=[0, 4, 8, 12], ch=list(range(0, 16, 2))), "techno")
    ch = out["lanes"]["ch"]
    assert ch[2] > 100 > ch[0]


def test_unknown_style_is_untouched():
    d = _drums(kick=[0, 3])
    assert srv._apply_groove_to_drums(d, "no_such_style") is d


def test_groove_cues_describe_skeleton():
    cues = srv._groove_cues("dnb")
    assert "kick always on steps [0, 10]" in cues
    assert "never put kick on steps [4, 12]" in cues
    assert "Groove skeleton" in srv._style_cues_for_generation("dnb")


def test_style_swing_comes_from_groove():
    assert srv._style_swing("garage") == pytest.approx(0.18)
    assert srv._style_swing("techno") == 0.0


# ---------------------------------------------------------------- bass shaping

def test_house_bass_sits_on_offbeats(no_dropouts):
    out, meta = srv._apply_style_to_bassline(_bass([43] * 16), "house")
    assert srv._hit_steps(out["steps"], 16) == [2, 6, 10, 14]


def test_psytrance_bass_rolls_between_kicks(no_dropouts):
    out, _ = srv._apply_style_to_bassline(_bass([43, 0, 0, 0] * 4), "psytrance")
    hits = srv._hit_steps(out["steps"], 16)
    assert hits == [i for i in range(16) if i % 4 != 0]


def test_techno_bass_avoids_kick(no_dropouts):
    drums = _drums(kick=[0, 4, 8, 12])
    out, _ = srv._apply_style_to_bassline(_bass([43, 0, 43, 0] * 4), "techno", drums)
    assert srv._hit_steps(out["steps"], 16) == [2, 6, 10, 14]


def test_bass_shaping_never_silences_the_line(no_dropouts):
    drums = _drums(kick=[0, 4, 8, 12])
    out, _ = srv._apply_style_to_bassline(_bass([43, 0, 0, 0] * 4), "techno", drums)
    assert any(out["steps"])


def test_generated_styles_inherit_feel_from_bases():
    new = {"bass_profile": {"scale": "major"}, "harmony_profile": {"progressions": []}}
    out = srv._inherit_feel_from_bases(new, STYLES["garage"], STYLES["techno"])
    assert out["groove"] == STYLES["garage"]["groove"]
    assert out["bass_profile"]["density_keep"] == STYLES["garage"]["bass_profile"]["density_keep"]
    assert out["harmony_profile"]["rhythm"] == "two_step"
    assert "scale" not in out["harmony_profile"]  # follows the new style's own bass scale


# ---------------------------------------------------------------- chord track

def test_happy_house_chords_are_major():
    prog, _ = srv._generate_chords("happy_house", 4, 43, 4)
    tonic = sorted(prog["chords"][0]["notes"])
    pcs = {n % 12 for n in tonic}
    assert pcs == {7, 11, 2}  # G major
    assert prog["chords"][0]["quality"] == "maj"


def test_chords_are_voice_led(monkeypatch):
    monkeypatch.setattr(srv.random, "choice", lambda seq: seq[0])
    prog, _ = srv._generate_chords("house", 4, 43, 4)
    voicings = [sorted(c["notes"]) for c in prog["chords"]]
    for a, b in zip(voicings, voicings[1:]):
        assert sum(abs(x - y) for x, y in zip(a, b)) <= 8, (a, b)
    assert all(len(v) == 4 for v in voicings)  # sevenths
    assert all(55 <= sum(v) / len(v) <= 70 for v in voicings)


def test_two_bar_harmonic_rhythm_holds_chords(monkeypatch):
    monkeypatch.setattr(srv.random, "choice", lambda seq: seq[0])
    prog, _ = srv._generate_chords("dnb", 4, 41, 4)
    degrees = [c["degree"] for c in prog["chords"]]
    assert degrees[0] == degrees[1] and degrees[2] == degrees[3]


def test_drone_styles_hold_root_and_fifth():
    prog, _ = srv._generate_chords("psytrance", 4, 41, 4)
    notes = {tuple(c["notes"]) for c in prog["chords"]}
    assert len(notes) == 1
    lo, hi = next(iter(notes))
    assert hi - lo == 7 and lo % 12 == 41 % 12


def test_sustained_repeats_are_tied(fake_ctrl):
    prog, _ = srv._generate_chords("tribal", 4, 43, 4)
    srv._write_chords_to_ableton(5, 0, prog)
    assert len(fake_ctrl.notes) == 2  # root + fifth held across all 4 bars
    assert all(n[2] == 16.0 for n in fake_ctrl.notes)


def test_rhythmic_chords_follow_style_pattern(fake_ctrl):
    prog, _ = srv._generate_chords("gabber", 1, 43, 4)
    srv._write_chords_to_ableton(5, 0, prog)
    starts = sorted({n[1] for n in fake_ctrl.notes})
    assert starts == [0.5, 1.5, 2.5, 3.5]  # offbeat stabs
    assert max(n[3] for n in fake_ctrl.notes) == prog["velocity"]


# ---------------------------------------------------------------- harder techno kicks

def test_techno_kick_is_pinned_to_full_velocity():
    out = srv._apply_groove_to_drums(_drums(kick=[0, 4, 8, 12]), "techno")
    # _lane() writes 100; the anchor pins every kick to 127.
    assert [out["lanes"]["kick"][i] for i in (0, 4, 8, 12)] == [127] * 4


def test_kick_room_lowers_hats_under_the_kick():
    d = _drums(kick=[0, 4, 8, 12], ch=list(range(16)))
    out = srv._apply_groove_to_drums(d, "techno")
    ch = out["lanes"]["ch"]
    assert ch[0] < ch[1] and ch[4] < ch[5]  # on-kick hats sit under the transient


def test_house_kick_is_not_pinned():
    out = srv._apply_groove_to_drums(_drums(kick=[0, 4, 8, 12]), "house")
    assert out["lanes"]["kick"][0] == 100
