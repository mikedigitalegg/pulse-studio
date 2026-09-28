"""
Perform moves that wait on Live's grid: the bar-snap helper, Breakdown → Drop and Delay Throw.
The bridge is faked with a clock that runs at 600 BPM (a beat every 0.1 s), so waits are real
but short.

    python -m pytest tests/test_moves.py -q
"""
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
    def __init__(self, beat=0.3, playing=True, n_tracks=7, returns=(("A-Reverb", "Reverb"), ("PS-Delay", "Delay"))):
        self.t0, self.beat0, self.playing, self.connected = time.monotonic(), beat, playing, True
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
                        "signature_numerator": 4, "signature_denominator": 4}
            if cmd == "get_track":
                ti = params["track_index"]
                return {"mute": self.mute[ti], "sends": list(self.sends[ti])}
            if cmd == "set_track":
                self.mute[params["track_index"]] = params["mute"]
            elif cmd == "set_send":
                self.sends[params["track_index"]][params["send_index"]] = params["value"]
            elif cmd == "get_return_tracks":
                return {"returns": [{"return_index": i, "name": n, "devices": [{"name": d}]} for i, (n, d) in enumerate(self.returns)]}
            else:
                raise AssertionError(cmd)
            self.log.append((self.beat(), cmd, dict(params)))
            return {}


@pytest.fixture
def bridge(monkeypatch):
    def _make(**kw):
        b = ClockBridge(**kw)
        monkeypatch.setattr(srv, "BRIDGE", b)
        return b
    yield _make
    srv._BREAKDOWN.update({"running": False, "drop_now": None, "drop_beat": None})
    srv._THROW["running"] = False


def _role(role):
    return next(int(p["track_index"]) for p in srv.PULSE_TRACK_PLAN if p["role"] == role)


def _join_moves(timeout=5.0):
    end = time.monotonic() + timeout
    while srv._BREAKDOWN["running"] or srv._THROW["running"]:
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
