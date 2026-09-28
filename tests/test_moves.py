"""
Perform moves that wait on Live's grid: the bar-snap helper, Breakdown → Drop and Delay Throw.
The bridge is faked with a clock that runs at 600 BPM (a beat every 0.1 s), so waits are real
but short.

    python -m pytest tests/test_moves.py -q
"""
import math
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

TEMPO = 600.0
BEAT_S = 60.0 / TEMPO


class ClockBridge:
    def __init__(self, beat=0.3, playing=True, n_tracks=7, returns=(("A-Reverb", "Reverb"), ("PS-Delay", "Delay")), launch_q=4, playing_slot=0):
        self.t0, self.beat0, self.playing, self.connected = time.monotonic(), beat, playing, True
        self.launch_q = launch_q  # Live's menu value; 4 = 1 bar
        self.launches = [(float("-inf"), ti, playing_slot) for ti in range(n_tracks)]  # (beat, track, slot)
        self.clips = {}  # (track, slot) -> write_clip params
        self.free_slot = 5
        self.mute = {ti: False for ti in range(n_tracks)}
        self.returns = list(returns)
        self.sends = {ti: [0.2, 0.3][: len(self.returns)] for ti in range(n_tracks)}
        self.log = []  # (beat, cmd, params)
        self.lock = threading.Lock()

    def beat(self):
        return self.beat0 + (time.monotonic() - self.t0) / BEAT_S if self.playing else self.beat0

    def request(self, cmd, params=None, timeout_s=0):
        params = params or {}
        with self.lock:
            if cmd == "get_song":
                return {"tempo": TEMPO, "is_playing": self.playing, "current_song_time": self.beat(),
                        "signature_numerator": 4, "signature_denominator": 4, "clip_trigger_quantization": self.launch_q}
            if cmd == "get_track":
                ti = params["track_index"]
                return {"mute": self.mute[ti], "sends": list(self.sends[ti]), "playing_slot_index": self._playing_slot(ti)}
            if cmd == "find_free_slot":
                return {"slot": self.free_slot}
            if cmd == "set_track":
                self.mute[params["track_index"]] = params["mute"]
            elif cmd == "set_send":
                self.sends[params["track_index"]][params["send_index"]] = params["value"]
            elif cmd == "get_return_tracks":
                return {"returns": [{"return_index": i, "name": n, "devices": [{"name": d}]} for i, (n, d) in enumerate(self.returns)]}
            elif cmd == "write_clip":
                self.clips[(params["track_index"], params["clip_slot_index"])] = params
            elif cmd == "delete_clip":
                self.clips.pop((params["track_index"], params["clip_slot_index"]), None)
            elif cmd in ("fire_clip", "stop_clip"):
                slot = params["clip_slot_index"] if cmd == "fire_clip" else -1
                self.launches.append((self._launch_at(self.beat()), params["track_index"], slot))
            elif cmd == "fire_scene":
                at = self._launch_at(self.beat())
                self.launches += [(at, ti, params["scene_index"]) for ti in self.mute]
            else:
                raise AssertionError(cmd)
            self.log.append((self.beat(), cmd, dict(params)))
            return {}

    def _launch_at(self, beat):
        """Where Live starts a clip fired at beat: the next 1-bar boundary, or then with quantization off."""
        return beat if self.launch_q == 0 else math.ceil(beat / 4.0) * 4.0

    def _playing_slot(self, ti):
        now = self.beat()
        return [slot for at, t, slot in self.launches if t == ti and at <= now][-1]

    def launched(self, cmd):
        """(beat Live launches it, params) for each fire of cmd."""
        return [(round(self._launch_at(beat)), p) for beat, c, p in self.log if c == cmd]


@pytest.fixture
def bridge(monkeypatch):
    def _make(**kw):
        b = ClockBridge(**kw)
        monkeypatch.setattr(srv, "BRIDGE", b)
        return b
    yield _make
    srv._BREAKDOWN.update({"running": False, "drop_now": None, "drop_beat": None})
    srv._THROW["running"] = False
    srv._TAIL["running"] = False
    srv._FILL["running"] = False


def _role(role):
    return next(int(p["track_index"]) for p in srv.PULSE_TRACK_PLAN if p["role"] == role)


def _join_moves(timeout=5.0):
    end = time.monotonic() + timeout
    while srv._BREAKDOWN["running"] or srv._THROW["running"] or srv._TAIL["running"] or srv._FILL["running"]:
        assert time.monotonic() < end, "move didn't finish"
        time.sleep(0.02)


# ---------------------------------------------------------------- bar snap

@pytest.mark.parametrize("beat,unit,want", [
    (5.3, 4.0, 8.0),
    (8.03, 4.0, 8.0),  # a hair after the downbeat counts as on it
    (8.2, 4.0, 12.0),
    (3.9, 1.0, 4.0),
])
def test_next_boundary(beat, unit, want):
    assert srv._next_boundary({"beat": beat}, unit) == want


def test_six_eight_bars_are_three_quarter_notes(bridge, monkeypatch):
    b = bridge()
    orig = b.request
    monkeypatch.setattr(b, "request", lambda cmd, params=None, timeout_s=0: {**orig(cmd, params), "signature_numerator": 6, "signature_denominator": 8} if cmd == "get_song" else orig(cmd, params))
    assert srv._song_clock()["beats_per_bar"] == 3.0


def test_wait_lands_on_the_target_beat(bridge):
    b = bridge(beat=0.2)
    assert srv._wait_for_beat(4.0)
    assert 4.0 - 0.25 < b.beat() < 4.0 + 0.25  # within a quarter of a (very fast) beat


def test_wait_returns_at_once_when_stopped_or_unreachable(bridge, monkeypatch):
    bridge(playing=False)
    t = time.monotonic()
    assert srv._wait_for_beat(1000.0)
    monkeypatch.setattr(srv, "BRIDGE", type("B", (), {"connected": False})())
    assert srv._wait_for_beat(1000.0)
    assert time.monotonic() - t < 0.2


def test_wait_can_be_interrupted(bridge):
    bridge()
    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()
    assert srv._wait_for_beat(1000.0, interrupt=ev) is False


# ---------------------------------------------------------------- Breakdown → Drop

def test_breakdown_needs_the_transport_running(bridge):
    bridge(playing=False)
    res = srv.move_breakdown(srv.BreakdownRequest())
    assert res["ok"] is False and res["error"] == "not_playing"


def test_breakdown_mutes_on_the_bar_and_drops_on_the_downbeat(bridge):
    b = bridge(beat=0.5)
    drums, bass = _role("drums"), _role("bass")
    res = srv.move_breakdown(srv.BreakdownRequest(bars=1))
    assert res["ok"] and res["start_beat"] == 4.0 and res["drop_beat"] == 8.0
    _join_moves()
    mutes = [(round(beat), p["track_index"], p["mute"]) for beat, cmd, p in b.log if cmd == "set_track"]
    assert mutes == [(4, drums, True), (4, bass, True), (8, drums, False), (8, bass, False)]
    assert not srv._BREAKDOWN["running"]


def test_parts_muted_by_hand_stay_muted(bridge):
    b = bridge(beat=3.5)
    b.mute[_role("bass")] = True
    srv.move_breakdown(srv.BreakdownRequest(bars=1))
    _join_moves()
    assert b.mute[_role("bass")] is True and b.mute[_role("drums")] is False
    assert all(p["track_index"] != _role("bass") for _, cmd, p in b.log if cmd == "set_track")


def test_pressing_again_drops_on_the_next_bar(bridge):
    b = bridge(beat=3.5)
    srv.move_breakdown(srv.BreakdownRequest(bars=8))  # would drop on beat 36
    time.sleep(0.15)  # into the breakdown (bar 2, beat ~5)
    assert srv.move_breakdown(srv.BreakdownRequest())["status"] == "dropping"
    _join_moves()
    drop = [beat for beat, cmd, p in b.log if cmd == "set_track" and p["mute"] is False]
    assert drop and all(round(x) == 8 for x in drop)


# ---------------------------------------------------------------- Delay Throw

def test_throw_opens_only_the_delay_send_for_a_beat_and_puts_it_back(bridge):
    b = bridge(beat=0.5)
    stabs, chords, vox = _role("stabs"), _role("chords"), 6
    b.sends[stabs] = [0.2, 0.45]
    res = srv.move_delay_throw(srv.DelayThrowRequest(track_indices=[vox]))
    assert res["ok"] and res["return_index"] == 1 and res["start_beat"] == 1.0 and res["end_beat"] == 2.0
    assert res["tracks"] == sorted({stabs, chords, vox})
    _join_moves()
    sets = [(round(beat), p["track_index"], p["value"]) for beat, cmd, p in b.log if cmd == "set_send"]
    assert all(p["send_index"] == 1 for _, cmd, p in b.log if cmd == "set_send")
    assert (1, stabs, 1.0) in sets and (2, stabs, 0.45) in sets
    assert b.sends[stabs] == [0.2, 0.45] and b.sends[chords] == [0.2, 0.3]


def test_a_second_throw_while_one_runs_changes_nothing(bridge):
    b = bridge(beat=0.5)
    srv.move_delay_throw(srv.DelayThrowRequest())
    assert srv.move_delay_throw(srv.DelayThrowRequest()) == {"ok": True, "status": "throwing"}
    _join_moves()
    assert b.sends[_role("stabs")] == [0.2, 0.3]


def test_throw_without_a_delay_return_says_how_to_get_one(bridge):
    bridge(returns=(("A-Reverb", "Reverb"),))
    res = srv.move_delay_throw(srv.DelayThrowRequest())
    assert res["error"] == "no_delay_return" and "palette" in res["hint"]


def test_live_default_delay_return_is_used(bridge):
    bridge(returns=(("A-Reverb", "Reverb"), ("B-Delay", "Delay")))
    assert srv.move_delay_throw(srv.DelayThrowRequest())["return_index"] == 1
    _join_moves()


# ---------------------------------------------------------------- launch timing

@pytest.mark.parametrize("q,want", [(4, 4.0), (3, 8.0), (0, 0.0), (7, 1.0), (None, 4.0)])
def test_launch_quantum_in_beats(q, want):
    assert srv._launch_quantum({"launch_q": q, "beats_per_bar": 4.0}) == want


def test_launch_boundary_skips_a_bar_that_is_too_close():
    assert srv._launch_boundary({"beat": 5.0, "beats_per_bar": 4.0}) == 8.0
    assert srv._launch_boundary({"beat": 7.8, "beats_per_bar": 4.0}) == 12.0
    assert srv._launch_boundary({"beat": 8.02, "beats_per_bar": 4.0}) == 12.0  # just after a downbeat is too late to launch on it


def test_a_launch_goes_out_just_ahead_so_live_places_it_on_the_bar(bridge):
    b = bridge(beat=0.5)
    srv._launch_on(4.0, lambda: b.request("fire_scene", {"scene_index": 2}))
    (fired_at, _, _), = [e for e in b.log if e[1] == "fire_scene"]
    assert 3.5 < fired_at < 4.0 and b.launched("fire_scene")[0][0] == 4


def test_with_quantization_off_the_launch_goes_out_on_the_beat(bridge):
    b = bridge(beat=0.5, launch_q=0)
    srv._launch_on(4.0, lambda: b.request("fire_scene", {"scene_index": 2}))
    assert abs(b.log[-1][0] - 4.0) < 0.25


# ---------------------------------------------------------------- Reverb Tail Out

def test_tail_out_feeds_the_reverb_cuts_the_dry_parts_and_lands_the_next_scene(bridge):
    b = bridge(beat=0.5)
    ri = 0  # A-Reverb
    b.sends[_role("pad")] = [0.6, 0.15]
    res = srv.move_tail_out(srv.TailOutRequest(bars=2, feed_beats=2, scene_index=3))
    assert res["ok"] and (res["start_beat"], res["cut_beat"], res["land_beat"]) == (4.0, 6.0, 12.0)
    assert res["return_index"] == ri and "warning" not in res
    _join_moves()
    sends_up = {round(beat) for beat, cmd, p in b.log if cmd == "set_send" and p["value"] == 1.0}
    assert sends_up == {4} and all(p["send_index"] == ri for _, cmd, p in b.log if cmd == "set_send")
    mutes = {(round(beat), p["mute"]) for beat, cmd, p in b.log if cmd == "set_track"}
    assert mutes == {(6, True), (12, False)}
    assert b.launched("fire_scene") == [(12, {"scene_index": 3})]
    assert b.sends[_role("pad")] == [0.6, 0.15] and not any(b.mute.values())


def test_tail_out_without_a_next_scene_brings_the_same_parts_back(bridge):
    b = bridge(beat=0.5)
    srv.move_tail_out(srv.TailOutRequest(bars=1, feed_beats=8))  # feed can't eat the whole tail
    _join_moves()
    assert not b.launched("fire_scene")
    cut = [beat for beat, cmd, p in b.log if cmd == "set_track" and p["mute"]]
    assert cut and all(round(x) == 7 for x in cut)  # a beat of tail at least
    assert not any(b.mute.values())


def test_tail_out_keeps_hand_mutes_and_needs_a_reverb(bridge):
    b = bridge(beat=0.5)
    b.mute[_role("fx")] = True
    srv.move_tail_out(srv.TailOutRequest(bars=1))
    _join_moves()
    assert b.mute[_role("fx")] is True
    assert all(p["track_index"] != _role("fx") for _, cmd, p in b.log if cmd in ("set_track", "set_send"))
    bridge(returns=(("PS-Delay", "Delay"),))
    assert srv.move_tail_out(srv.TailOutRequest())["error"] == "no_reverb_return"


def test_long_launch_quantization_is_flagged(bridge):
    bridge(beat=0.5, launch_q=2)  # 4 bars
    res = srv.move_tail_out(srv.TailOutRequest(bars=1, scene_index=1))
    assert "1 Bar" in res["warning"]
    _join_moves(10)


# ---------------------------------------------------------------- Fill → Next

def test_fill_notes_roll_faster_and_louder():
    notes = srv._fill_notes("snare", 4.0)
    snare = [n for n in notes if n["pitch"] == srv.LANE_TO_MIDI_NOTE["snare"]]
    assert [n["start_time"] for n in snare][:5] == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert len(snare) == 4 + 4 + 8  # 8ths, 16ths, 32nds
    assert snare[-1]["start_time"] == 3.875 and snare[-1]["velocity"] > snare[0]["velocity"]
    assert any(n["pitch"] == srv.LANE_TO_MIDI_NOTE["kick"] and n["start_time"] == 0.0 for n in notes)
    hats = srv._fill_notes("hats", 3.0)  # a 3/4 bar
    assert max(n["start_time"] for n in hats) < 3.0 and any(n["pitch"] == srv.LANE_TO_MIDI_NOTE["oh"] for n in hats)


def test_fill_plays_for_a_bar_then_the_next_scene_and_cleans_up(bridge):
    b = bridge(beat=0.5)
    drums = _role("drums")
    res = srv.move_fill(srv.FillRequest(scene_index=2))
    assert res["ok"] and res["slot"] == 5 and (res["start_beat"], res["next_beat"]) == (4.0, 8.0)
    clip = b.clips[(drums, 5)]
    assert clip["name"] == "PS-FILL" and clip["length"] == 4.0
    _join_moves()
    assert b.launched("fire_clip") == [(4, {"track_index": drums, "clip_slot_index": 5})]
    assert b.launched("fire_scene") == [(8, {"scene_index": 2})]
    assert (drums, 5) not in b.clips  # deleted once the scene took over


def test_fill_without_a_next_scene_goes_back_to_the_drum_clip(bridge):
    b = bridge(beat=0.5, playing_slot=1)
    drums = _role("drums")
    srv.move_fill(srv.FillRequest())
    _join_moves()
    assert [(at, p["clip_slot_index"]) for at, p in b.launched("fire_clip")] == [(4, 5), (8, 1)]
    assert (drums, 5) not in b.clips


def test_fill_pressed_just_before_a_bar_waits_for_the_one_after(bridge):
    b = bridge(beat=3.85)
    res = srv.move_fill(srv.FillRequest(scene_index=1))
    assert res["start_beat"] == 8.0
    _join_moves()
    assert b.launched("fire_clip")[0][0] == 8


def test_a_fill_while_one_runs_changes_nothing(bridge):
    b = bridge(beat=0.5)
    srv.move_fill(srv.FillRequest())
    assert srv.move_fill(srv.FillRequest()) == {"ok": True, "status": "filling"}
    _join_moves()
    assert len([1 for _, cmd, _ in b.log if cmd == "write_clip"]) == 1
