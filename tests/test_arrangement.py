"""
Tests for how generated parts are tied together: shared swing, pitched stabs, groove-aware
layers, and full tracks derived from one core groove. OpenAI and Live are faked.

    python -m pytest tests/test_arrangement.py -q
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402


def _lane(hits, bars=1, vel=100):
    out = [0] * (bars * 16)
    for h in hits:
        out[h] = vel
    return out


class FakeCtrl:
    def __init__(self):
        self.clips = {}

    def create_clip(self, track, slot, length):
        self.clips[(track, slot)] = []

    def add_note(self, track, slot, pitch, start, duration, vel, **kw):
        self.clips.setdefault((track, slot), []).append((pitch, start, duration, vel))

    def send(self, *a, **k):
        pass

    def set_tempo(self, *a, **k):
        pass

    def query(self, *a, **k):
        return None


@pytest.fixture
def fake_ctrl(monkeypatch):
    fc = FakeCtrl()
    monkeypatch.setattr(srv, "ctrl", fc)
    return fc


@pytest.fixture
def no_phrase_moves(monkeypatch):
    """Phrase-end moves are random per scene; switch them off where a test compares scenes note for note."""
    monkeypatch.setattr(srv, "_apply_phrase_moves", lambda parts, style, rng: parts)


# ---------------------------------------------------------------- swing + stabs

def test_swing_delays_only_off_sixteenths():
    assert srv._swung_start(0, 0.2) == 0.0
    assert srv._swung_start(2, 0.2) == 0.5
    assert srv._swung_start(1, 0.2) == pytest.approx(0.25 + 0.05)
    assert srv._swung_start(3, 0.0) == 0.75


def test_style_swing_defaults_and_clamp():
    assert srv._style_swing("garage") == pytest.approx(0.18)
    assert srv._style_swing("techno") == 0.0
    assert srv._clamp_swing(2) == 0.33
    assert srv._clamp_swing(-1) == 0.0


def test_drums_and_bass_share_swing(fake_ctrl):
    drums = {"bars": 1, "step_division": "1/16", "lanes": {"ch": _lane([1])}, "swing": 0.2}
    bass = {"bars": 1, "step_division": "1/16", "root_midi": 43, "steps": [0, 43] + [0] * 14, "velocities": [0, 90] + [0] * 14, "swing": 0.2}
    srv._write_pattern_to_ableton(0, 0, drums)
    srv._write_bassline_to_ableton(1, 0, bass)
    assert fake_ctrl.clips[(0, 0)][0][1] == fake_ctrl.clips[(1, 0)][0][1]


def test_stabs_play_bar_chords(fake_ctrl):
    prog = {"chords": [{"bar": 0, "notes": [55, 58, 62]}, {"bar": 1, "notes": [51, 55, 58]}]}
    voicings = srv._stab_voicings(prog, 2)
    assert voicings == [[67, 70, 74], [63, 67, 70]]
    stabs = {"bars": 2, "step_division": "1/16", "lanes": {"stab": _lane([2, 18], bars=2)}, "voicings": voicings}
    srv._write_pattern_to_ableton(3, 0, stabs)
    pitches = sorted(n[0] for n in fake_ctrl.clips[(3, 0)])
    assert pitches == sorted([67, 70, 74, 63, 67, 70])


def test_stabs_without_voicings_keep_single_note(fake_ctrl):
    stabs = {"bars": 1, "step_division": "1/16", "lanes": {"stab": _lane([2])}}
    srv._write_pattern_to_ableton(3, 0, stabs)
    assert [n[0] for n in fake_ctrl.clips[(3, 0)]] == [srv.LANE_TO_MIDI_NOTE["stab"]]


def test_tonic_prog_matches_style_root():
    prog = srv._tonic_chord_prog(43, 2)
    assert [n % 12 for n in prog["chords"][0]["notes"]] == [7, 10, 2]  # G minor


def test_validate_keeps_swing_and_voicings():
    p = {"bars": 1, "step_division": "1/16", "lanes": {"stab": _lane([0])}, "swing": 0.1, "voicings": [[60, 63, 67], ["x"]]}
    valid, meta = srv._validate_pattern(p, 1)
    assert meta["ok"]
    assert valid["swing"] == 0.1
    assert valid["voicings"] == [[60, 63, 67]]


# ---------------------------------------------------------------- groove awareness

def test_groove_context_lists_anchor_steps():
    drums = {"lanes": {"kick": _lane([0, 4, 8, 12]), "clap": _lane([4, 12])}}
    bass = {"steps": [0, 0, 43, 0] * 4}
    ctx = srv._groove_context(drums, bass, 1)
    assert "kick: steps [0, 4, 8, 12]" in ctx
    assert "clap: steps [4, 12]" in ctx
    assert "bass notes: steps [2, 6, 10, 14]" in ctx


def test_declash_removes_perc_on_anchors():
    drums = {"lanes": {"kick": _lane([0, 8]), "snare": _lane([4]), "clap": _lane([12])}}
    perc = {"bars": 1, "step_division": "1/16", "lanes": {"perc1": _lane([4, 6, 12]), "perc2": _lane([0, 3, 8])}}
    out = srv._declash_perc(perc, drums)
    assert srv._hit_steps(out["lanes"]["perc1"], 16) == [6]
    assert srv._hit_steps(out["lanes"]["perc2"], 16) == [3]


# ---------------------------------------------------------------- scene derivation

def test_lane_ops():
    vals = _lane([0, 4, 8, 12], bars=2)
    vals[16] = vals[20] = 100
    assert srv._apply_lane_op(vals, "drop") == [0] * 32
    assert srv._hit_steps(srv._apply_lane_op(vals, "thin"), 32) == [0, 8, 16]
    assert srv._hit_steps(srv._apply_lane_op(vals, "sparse"), 32) == [0]
    assert max(srv._apply_lane_op(vals, 0.5)) == 50
    assert max(srv._apply_lane_op(vals, ["thin", 0.5])) == 50


def test_derive_bass_ops():
    steps = [43, 0, 46, 43] * 4
    bass = {"steps": list(steps), "velocities": [90 if n else 0 for n in steps]}
    roots = srv._derive_bass(bass, "roots")
    assert srv._hit_steps(roots["steps"], 16) == [0, 4, 8, 12]
    eighths = srv._derive_bass(bass, "eighths")
    assert all(i % 2 == 0 for i in srv._hit_steps(eighths["steps"], 16))


def test_derive_fx_positions():
    assert srv._hit_steps(srv._derive_fx(None, "end", 2)["lanes"]["fx"], 32) == [28]
    assert srv._hit_steps(srv._derive_fx(None, "start", 2)["lanes"]["fx"], 32) == [0]


# ---------------------------------------------------------------- full track end-to-end

class FakeOpenAI:
    """Deterministic stand-in for the model calls; records every call and its prompt."""

    def __init__(self):
        self.calls = []
        self.pair_count = 0

    async def pair(self, style, bars, drum_lanes, root, prompt, temperature):
        self.calls.append(("pair", prompt))
        self.pair_count += 1
        n = bars * 16
        # Each call returns a different kick so we can tell whether scenes share the core.
        kick_hits = [0, 4, 8, 12] if self.pair_count == 1 else [0, 6, 10]
        steps = [root if i % 4 == 2 else 0 for i in range(n)]
        return {
            "drums": {"bars": bars, "step_division": "1/16", "lanes": {
                "kick": _lane(kick_hits, bars), "clap": _lane([4, 12], bars), "ch": _lane([2, 6, 10, 14], bars), "oh": _lane([2, 10], bars),
            }},
            "bass": {"bars": bars, "step_division": "1/16", "root_midi": root, "steps": steps, "velocities": [100 if s else 0 for s in steps]},
        }, {"ok": True}

    def layer(self, kind, lane_hits):
        async def _fn(style, bars, prompt, temperature, context=None):
            self.calls.append((kind, context))
            return {"bars": bars, "step_division": "1/16", "lanes": {k: _lane(v, bars) for k, v in lane_hits.items()}}, {"ok": True}
        return _fn


@pytest.fixture
def fake_ai(monkeypatch, fake_ctrl):
    ai = FakeOpenAI()
    monkeypatch.setattr(srv, "_openai_generate_pair", ai.pair)
    monkeypatch.setattr(srv, "_openai_generate_perc_pattern", ai.layer("perc", {"perc1": [3, 7, 11, 15], "perc2": [0, 5]}))
    monkeypatch.setattr(srv, "_openai_generate_stabs_pattern", ai.layer("stabs", {"stab": [2, 6, 10, 14]}))
    monkeypatch.setattr(srv, "_openai_generate_fx_pattern", ai.layer("fx", {"fx": [12]}))
    monkeypatch.setattr(srv, "_ensure_scene_count", lambda n: {"ok": True})
    monkeypatch.setattr(srv, "_ensure_midi_tracks_prefix", lambda n, prefix="": {"ok": True})
    monkeypatch.setattr(srv.random, "random", lambda: 0.0)  # disable probabilistic bass dropouts
    return ai


def _run_full(style):
    req = srv.GenerateFullTrackRequest(style=style, bars_per_scene=1, clip_bars=4, apply_instruments=False)
    return asyncio.run(srv.generate_full_track(req))


def test_full_track_scenes_share_core_groove(fake_ai, no_phrase_moves):
    res = _run_full("techno")
    assert res["ok"], res
    slots = {s["name"]: s["slot"] for s in res["scenes"]}
    modes = {s["name"]: s["mode"] for s in res["scenes"]}
    assert modes["Peak"] == "core" and modes["Climax"] == "vary" and modes["Intro"] == "derive"

    core_kick = srv.GEN_CACHE.get_drums(0, slots["Peak"])["lanes"]["kick"]
    for name in ("Intro", "Build 1", "Build 2", "Drop", "Climax", "Outro"):
        assert srv.GEN_CACHE.get_drums(0, slots[name])["lanes"]["kick"] == core_kick, name
    assert not any(srv.GEN_CACHE.get_drums(0, slots["Breakdown"])["lanes"]["kick"])

    # Climax gets new top layers but keeps the core bassline.
    assert srv.GEN_CACHE.get_bass(1, slots["Climax"])["steps"] == srv.GEN_CACHE.get_bass(1, slots["Peak"])["steps"]

    # Only the core and the one vary scene hit the model: 2 pair calls, not one per scene.
    assert sum(1 for c in fake_ai.calls if c[0] == "pair") == 2


def test_full_track_layers_see_groove_and_stabs_follow_chords(fake_ai, fake_ctrl):
    res = _run_full("techno")
    assert res["ok"], res
    layer_contexts = [ctx for kind, ctx in fake_ai.calls if kind in {"perc", "stabs", "fx"}]
    assert layer_contexts and all("kick: steps" in ctx for ctx in layer_contexts)

    peak = next(s["slot"] for s in res["scenes"] if s["name"] == "Peak")
    chords = res["chords"]["progression"]["chords"]
    stab_notes = fake_ctrl.clips[(3, peak)]
    for pitch, start, _, _ in stab_notes:
        bar = int(start // 4)
        chord_pcs = {n % 12 for n in chords[bar % len(chords)]["notes"]}
        assert pitch % 12 in chord_pcs


def test_full_track_pad_fills_the_thin_sections(fake_ai, fake_ctrl):
    res = _run_full("techno")
    assert res["ok"], res
    slots = {s["name"]: s["slot"] for s in res["scenes"]}
    pad = srv.PAD_TRACK_INDEX
    assert fake_ctrl.clips.get((pad, slots["Breakdown"])) and fake_ctrl.clips.get((pad, slots["Intro"]))
    assert (pad, slots["Drop"]) not in fake_ctrl.clips
    included = {s["name"]: s["tracks"]["pad"]["included"] for s in res["scenes"]}
    assert included["Breakdown"] and not included["Drop"]
    # Held above the Chords track.
    chords_top = max(p for p, *_ in fake_ctrl.clips[(srv.CHORDS_TRACK_INDEX, slots["Breakdown"])])
    assert min(p for p, *_ in fake_ctrl.clips[(pad, slots["Breakdown"])]) > chords_top - 12


def test_full_track_swing_applies_to_every_layer(fake_ai):
    res = _run_full("garage")
    assert res["ok"], res
    slot = res["scenes"][1]["slot"]  # Main A (core)
    for getter in (srv.GEN_CACHE.get_drums, srv.GEN_CACHE.get_perc, srv.GEN_CACHE.get_stabs):
        track = {srv.GEN_CACHE.get_drums: 0, srv.GEN_CACHE.get_perc: 2, srv.GEN_CACHE.get_stabs: 3}[getter]
        assert getter(track, slot)["swing"] == pytest.approx(0.18)
    assert srv.GEN_CACHE.get_bass(1, slot)["swing"] == pytest.approx(0.18)


# ---------------------------------------------------------------- resilience

class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _fake_openai(monkeypatch, contents, sent):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append(json)
            return _FakeResp(contents.pop(0))

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(srv.httpx, "AsyncClient", Client)


def test_split_json_objects_are_merged(monkeypatch):
    # The shape gpt-4o-mini returned for a tekno Climax scene.
    broken = '{"style":"tekno","drums":{"bars":1,"lanes":{"kick":[127,0,0,0]}}},{"bass":{"bars":1,"steps":[0,43]}},{"applied":{"summary":"x"}}'
    sent = []
    _fake_openai(monkeypatch, [broken], sent)
    obj, meta = asyncio.run(srv._call_ai_async("sys JSON", "user", 0.7))
    assert meta["ok"], meta
    assert set(obj) == {"style", "drums", "bass", "applied"}
    assert sent[0]["response_format"] == {"type": "json_object"}


def test_unrecoverable_json_still_reports_bad_response(monkeypatch):
    sent = []
    _fake_openai(monkeypatch, ['{"a": 1,, }', '{"a": 1,, }'], sent)
    obj, meta = asyncio.run(srv._call_ai_async("sys JSON", "user", 0.7))
    assert obj is None and meta["error"] == "ai_bad_response"
    assert len(sent) == 2  # retried once


def test_failed_variation_falls_back_to_core(fake_ai, monkeypatch, no_phrase_moves):
    real_pair = fake_ai.pair

    async def flaky_pair(style, bars, drum_lanes, root, prompt, temperature):
        if fake_ai.pair_count >= 1:  # core succeeds, the Climax variation fails
            fake_ai.pair_count += 1
            return None, {"ok": False, "error": "ai_bad_response"}
        return await real_pair(style, bars, drum_lanes, root, prompt, temperature)

    monkeypatch.setattr(srv, "_openai_generate_pair", flaky_pair)
    res = _run_full("techno")
    assert res["ok"], res
    climax = next(s for s in res["scenes"] if s["name"] == "Climax")
    assert climax["mode"] == "derive_fallback"
    assert any("Climax" in w for w in res["warnings"])
    assert srv.GEN_CACHE.get_drums(0, climax["slot"])["lanes"]["kick"] == srv.GEN_CACHE.get_drums(0, 3)["lanes"]["kick"]
