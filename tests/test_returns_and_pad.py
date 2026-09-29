"""
The Pad track holds the progression above the Chords track in the thinner sections, and the set
gets a reverb and a delay return with per-role sends. The bridge is faked.

    python -m pytest tests/test_returns_and_pad.py -q
"""
import asyncio
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

PROG = {
    "bars": 4, "root_midi": 43, "scale": "minor", "velocity": 80, "rhythm": "dub",
    "chords": [
        {"bar": 0, "degree": 1, "quality": "min", "notes": [55, 58, 62, 65]},
        {"bar": 1, "degree": 6, "quality": "maj", "notes": [51, 55, 58, 62]},
        {"bar": 2, "degree": 3, "quality": "maj", "notes": [58, 62, 65, 69]},
        {"bar": 3, "degree": 7, "quality": "maj", "notes": [53, 57, 60, 89]},
    ],
}


# ---------------------------------------------------------------- pad parts

def test_drop_has_no_pad_and_unknown_scenes_neither():
    assert srv._pad_part(PROG, "Drop", 4, 80) == (None, 0)
    assert srv._pad_part(None, "Breakdown", 4, 80) == (None, 0)


def test_breakdown_pad_is_held_an_octave_up_without_the_root():
    prog, vel = srv._pad_part(PROG, "Breakdown", 4, 80)
    assert vel == 80 and prog["rhythm"] == "sustain"
    assert prog["chords"][0]["notes"] == [70, 74, 77]
    assert all(n <= srv.PAD_MAX_PITCH for ch in prog["chords"] for n in ch["notes"])


def test_intro_holds_the_tonic_for_the_whole_clip():
    prog, vel = srv._pad_part(PROG, "Intro", 4, 80)
    assert vel == 64
    assert [c["bar"] for c in prog["chords"]] == [0, 1, 2, 3]
    assert all(c["notes"] == [70, 74, 77] for c in prog["chords"])


def test_busy_sections_get_a_quieter_two_note_pad():
    prog, vel = srv._pad_part(PROG, "Peak", 4, 80)
    assert vel == 40
    assert all(len(c["notes"]) == 2 for c in prog["chords"])


def test_pad_is_written_as_held_notes(monkeypatch):
    notes = []
    monkeypatch.setattr(srv, "ctrl", types.SimpleNamespace(
        create_clip=lambda *a: None, add_note=lambda t, s, p, st, d, v, **kw: notes.append((t, p, st, d, v))))
    prog, vel = srv._pad_part(PROG, "Intro", 4, 80)
    srv._write_chords_to_ableton(srv.PAD_TRACK_INDEX, 0, prog, vel)
    assert notes == [(srv.PAD_TRACK_INDEX, p, 0.0, 16.0, 64) for p in (70, 74, 77)]


def test_pad_is_part_of_the_track_plan():
    pad = next(p for p in srv.PULSE_TRACK_PLAN if p["role"] == "pad")
    assert pad["track_index"] == srv.PAD_TRACK_INDEX
    assert srv._pulse_required_track_count() == 7
    assert "pad" in srv.instrument_palette.ROLE_POOLS


def test_heuristic_pad_differs_from_chords():
    a, b = {"uri": "a", "name": "Warm Pad.adv"}, {"uri": "b", "name": "Air Pad.adv"}
    picks = srv.instrument_palette.heuristic_palette({"chords": [a, b], "pad": [a, b]})
    assert picks["chords"]["item"] is a and picks["pad"]["item"] is b


# ---------------------------------------------------------------- returns + sends

class FakeBridge:
    def __init__(self, returns=(), n_tracks=7, max_returns=12, old=False):
        self.returns = [{"name": n, "devices": list(d)} for n, d in returns]
        self.sends = {ti: [0.0] * len(self.returns) for ti in range(n_tracks)}
        self.max_returns, self.old = max_returns, old
        self.params = {}

    async def __call__(self, cmd, params=None, timeout_s=5.0):
        params = params or {}
        if self.old and cmd == "get_return_tracks":
            raise srv.BridgeError("unknown_command: get_return_tracks")
        if cmd == "get_return_tracks":
            return {"returns": [{"return_index": i, "name": r["name"], "devices": [{"index": j, "name": n} for j, n in enumerate(r["devices"])]}
                                for i, r in enumerate(self.returns)]}
        if cmd == "create_return_track":
            if len(self.returns) >= self.max_returns:
                raise srv.BridgeError("return_track_limit: Maximum number of return tracks reached")
            self.returns.append({"name": params.get("name"), "devices": []})
            for s in self.sends.values():
                s.append(0.0)
            return {"return_index": len(self.returns) - 1}
        if cmd == "load_device":
            self.returns[params["return_index"]]["devices"].append(params["device_name"])
            return {}
        if cmd == "get_track":
            if "return_index" in params:
                return {"devices": [{"index": j, "name": n} for j, n in enumerate(self.returns[params["return_index"]]["devices"])]}
            return {"devices": [], "sends": list(self.sends[params["track_index"]])}
        if cmd == "get_device_params":
            return {"parameters": [{"index": 0, "name": "Device On", "min": 0, "max": 1}, {"index": 1, "name": "Dry/Wet", "min": 0.0, "max": 1.0}]}
        if cmd == "set_device_param":
            self.params[(params["return_index"], params["param_index"])] = params["value"]
            return {}
        if cmd == "set_send":
            self.sends[params["track_index"]][params["send_index"]] = params["value"]
            return {}
        raise AssertionError(cmd)


@pytest.fixture
def use(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "PALETTE_STATE_FILE", str(tmp_path / "palette.json"))

    def _use(bridge, effects=("Reverb", "Delay", "Drum Buss")):
        monkeypatch.setattr(srv, "_bridge_call", bridge)
        monkeypatch.setattr(srv, "BRIDGE", types.SimpleNamespace(connected=True, capabilities={"devices": {"audio_effects": list(effects)}}))
    return _use


def _returns(style="techno"):
    return asyncio.run(srv._apply_returns(style))


def _send(bridge, role, key):
    ti = next(p["track_index"] for p in srv.PULSE_TRACK_PLAN if p["role"] == role)
    ri = 0 if key == "reverb" else 1
    return bridge.sends[ti][ri]


def test_empty_set_gets_a_reverb_and_a_delay_return(use):
    b = FakeBridge()
    use(b)
    res = _returns()
    assert res["action"] == "applied"
    assert [(r["name"], r["device"], r["action"]) for r in res["returns"]] == [("PS-Reverb", "Reverb", "created"), ("PS-Delay", "Delay", "created")]
    assert b.params[(0, 1)] == 1.0 and b.params[(1, 1)] == 1.0  # fully wet on a return
    assert _send(b, "pad", "reverb") == srv.DEFAULT_SENDS["reverb"]["pad"]
    assert _send(b, "stabs", "delay") == srv.DEFAULT_SENDS["delay"]["stabs"]
    assert _send(b, "drums", "reverb") == 0.0 and _send(b, "bass", "delay") == 0.0


def test_live_default_returns_are_reused_and_running_again_adds_nothing(use):
    b = FakeBridge(returns=[("A-Reverb", ["Reverb"]), ("B-Delay", ["Delay"])])
    use(b)
    res = _returns()
    assert [r["action"] for r in res["returns"]] == ["reused", "reused"]
    assert len(b.returns) == 2 and not b.params  # the user's own returns aren't retuned
    _returns()
    assert len(b.returns) == 2


def test_sends_moved_by_hand_are_kept_but_pulse_sends_follow_the_style(use, monkeypatch):
    b = FakeBridge()
    use(b)
    _returns("techno")
    stabs = next(p["track_index"] for p in srv.PULSE_TRACK_PLAN if p["role"] == "stabs")
    b.sends[stabs][0] = 0.9  # the producer turns the stab reverb up

    monkeypatch.setitem(srv.STYLE_CONFIG, "house", {**srv.STYLE_CONFIG.get("house", {}), "sends": {"reverb": {"pad": 0.75}}})
    res = _returns("house")
    assert b.sends[stabs][0] == 0.9
    assert next(s for s in res["sends"] if s["role"] == "stabs")["kept"] == {"reverb": 0.9}
    assert _send(b, "pad", "reverb") == 0.75


def test_older_bridge_is_skipped_with_an_update_hint(use):
    use(FakeBridge(old=True))
    res = _returns()
    assert res["action"] == "skipped" and "install.py" in res["reason"]


def test_return_limit_skips_what_does_not_fit(use):
    b = FakeBridge(returns=[("A-Reverb", ["Reverb"])], max_returns=1)
    use(b)
    res = _returns()
    assert [r["action"] for r in res["returns"]] == ["reused", "skipped"]
    assert _send(b, "pad", "reverb") == srv.DEFAULT_SENDS["reverb"]["pad"]


def test_missing_delay_devices_fall_through_the_chain(use):
    b = FakeBridge()
    use(b, effects=("Reverb", "Simple Delay"))
    res = _returns()
    assert res["returns"][1]["device"] == "Simple Delay"


# ---------------------------------------------------------------- perform endpoints

class _SyncBridge:
    def __init__(self, old):
        self.old, self.sent, self.connected = old, [], True

    def request(self, cmd, params=None, timeout_s=0):
        if cmd == "stop_all_clips":
            if self.old:
                raise srv.BridgeError("unknown_command: stop_all_clips")
            return {"stopped": True}
        if cmd == "get_snapshot":
            return {"tracks": [{"index": 0, "playing_slot_index": 2}, {"index": 1, "playing_slot_index": -1}, {"index": 2, "playing_slot_index": 0}]}
        raise AssertionError(cmd)

    def send(self, cmd, params=None):
        self.sent.append((cmd, params))


def test_stop_all_clips_uses_the_bridge_command(monkeypatch):
    b = _SyncBridge(old=False)
    monkeypatch.setattr(srv, "BRIDGE", b)
    assert srv.clips_stop_all() == {"ok": True, "via": "bridge"}


def test_stop_all_clips_on_an_older_bridge_stops_each_playing_clip(monkeypatch):
    b = _SyncBridge(old=True)
    monkeypatch.setattr(srv, "BRIDGE", b)
    res = srv.clips_stop_all()
    assert res["via"] == "per_track" and res["stopped"] == 2
    assert b.sent == [("stop_clip", {"track_index": 0, "clip_slot_index": 2}), ("stop_clip", {"track_index": 2, "clip_slot_index": 0})]


def test_track_plan_lists_roles():
    roles = [t["role"] for t in srv.tracks_plan()["tracks"]]
    assert roles == ["drums", "bass", "perc", "stabs", "fx", "chords", "pad"]
