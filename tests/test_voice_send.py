"""
Tests for sending generated voices into Live: audio prep, the audio-clip path (Live 12.0.5+),
the Simpler fallback, and the "is it actually audible" check. The bridge is faked.

    python -m pytest tests/test_voice_send.py -q
"""
import asyncio
import math
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

SR = 24000


def _tone_with_silence(lead_s=0.25, tone_s=0.5, tail_s=0.2, amp=6000):
    lead = [0] * int(SR * lead_s)
    tone = [int(amp * math.sin(2 * math.pi * 220 * i / SR)) for i in range(int(SR * tone_s))]
    return lead + tone + [0] * int(SR * tail_s)


class FakeBridge:
    """Records commands and plays back a tiny Live: tracks, slots, meter levels."""

    def __init__(self, live_version="12.1.0", meter_after_fire=0.3, **meter_state):
        self.live_version = live_version
        self.tracks = [{"name": "PS-TRK-01", "audio": False}]
        self.calls = []
        self.fired = False
        self.meter_after_fire = meter_after_fire
        self.meter_state = {"mute": False, "volume": 0.85, "soloed_elsewhere": False, "master_volume": 0.85, "is_playing": True}
        self.meter_state.update(meter_state)
        self.browser_misses = 0

    async def __call__(self, cmd, params=None, timeout_s=5.0):
        params = params or {}
        self.calls.append((cmd, params))
        if cmd == "hello":
            return {"live_version": self.live_version}
        if cmd == "get_song":
            return {"tempo": 240.0, "track_names": [t["name"] for t in self.tracks]}
        if cmd in ("create_audio_track", "create_midi_track"):
            self.tracks.append({"name": "new", "audio": cmd == "create_audio_track"})
            return {"index": len(self.tracks) - 1}
        if cmd == "set_track":
            t = self.tracks[params["track_index"]]
            if "name" in params:
                t["name"] = params["name"]
            if "mute" in params:
                self.meter_state["mute"] = params["mute"]
            if "volume" in params:
                self.meter_state["volume"] = params["volume"]
            return {}
        if cmd == "find_free_slot":
            return {"slot": 2}
        if cmd == "load_audio_clip":
            if tuple(int(x) for x in self.live_version.split(".")) < (12, 0, 5):
                raise RuntimeError("unsupported: create_audio_clip needs Live 12.0.5+")
            return {"length": 2.0, "warping": False, "looping": False}
        if cmd == "load_item_at_path":
            if self.browser_misses:
                self.browser_misses -= 1
                raise RuntimeError("item_not_found")
            return {"loaded": True}
        if cmd == "write_clip":
            return {}
        if cmd == "fire_clip":
            self.fired = True
            return {"fired": True}
        if cmd == "get_track_meter":
            return {"peak": self.meter_after_fire if self.fired else 0.0, **self.meter_state}
        raise AssertionError(f"unexpected command {cmd}")

    def cmds(self, name):
        return [p for c, p in self.calls if c == name]


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "AUDIO_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "RECORDINGS_DIR", str(tmp_path / "rec"))
    wav = srv._wav_from_pcm(_tone_with_silence(), sample_rate=SR, channels=1)
    (tmp_path / "tts_hands_up_3a748ffc975b.wav").write_bytes(wav)
    return tmp_path


def _use_bridge(monkeypatch, bridge, connected=True):
    monkeypatch.setattr(srv, "BRIDGE", types.SimpleNamespace(connected=connected, status=lambda: {}))
    monkeypatch.setattr(srv, "_bridge_call", bridge)


def _send(**kw):
    return asyncio.run(srv.send_voice_to_live(srv.VoiceSendRequest(**kw)))


# ---------------------------------------------------------------- audio prep

def test_prepare_trims_leading_silence_and_normalizes():
    src = _tone_with_silence(lead_s=0.25)
    out = srv._prepare_voice_pcm(src, 1, SR)
    lead = next(i for i, v in enumerate(out) if abs(v) > 327)
    assert lead < SR * 0.01  # phrase now starts within ~10 ms, not 250 ms
    assert max(abs(v) for v in out) == pytest.approx(0.89 * 32767, rel=0.02)
    assert len(out) < len(src)


def test_voice_label_from_filename():
    assert srv._voice_label_from_filename("tts_hands_up_3a748ffc975b.wav") == "hands up"
    assert srv._voice_label_from_filename("tts_4ba0e8c40cee31de9ed1bfd89c101a670a7c59a2.wav") == "voice"
    assert srv._voice_label_from_filename("vox_tts_bring_it_back_220dd7512780.wav") == "bring it back"


def test_prepare_leaves_silence_alone():
    assert srv._prepare_voice_pcm([0] * 100, 1, SR) == [0] * 100


def test_resolve_source_blocks_directory_escape(audio_dir):
    assert srv._resolve_voice_source(None, "/audio_files/tts_hands_up_3a748ffc975b.wav")
    assert srv._resolve_voice_source("../../etc/passwd.wav", None) is None
    assert srv._resolve_voice_source("notes.txt", None) is None


# ---------------------------------------------------------------- send to Live

def test_send_creates_voice_track_loads_clip_and_confirms_audio(audio_dir, monkeypatch):
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    res = _send(filename="tts_hands_up_3a748ffc975b.wav")
    assert res["ok"], res
    assert res["method"] == "audio_clip"
    assert bridge.tracks[res["track_index"]] == {"name": "PS-VOX", "audio": True}
    load = bridge.cmds("load_audio_clip")[0]
    assert os.path.isabs(load["file_path"]) and os.path.basename(load["file_path"]).startswith("vox_")
    assert load["clip_slot_index"] == 2 and load["warping"] is False and load["looping"] is False
    assert load["name"] == "hands up"
    assert res["verified"]["playing"] is True
    assert res["duration_s"] < 0.6  # silence trimmed


def test_send_reuses_existing_voice_track(audio_dir, monkeypatch):
    bridge = FakeBridge()
    bridge.tracks.append({"name": "PS-VOX", "audio": True})
    _use_bridge(monkeypatch, bridge)
    res = _send(filename="tts_hands_up_3a748ffc975b.wav", clip_slot_index=5)
    assert res["track_index"] == 1 and res["clip_slot_index"] == 5
    assert not bridge.cmds("create_audio_track")


def test_send_fixes_muted_track_and_reports_why_silent(audio_dir, monkeypatch):
    bridge = FakeBridge(meter_after_fire=0.0, mute=True, soloed_elsewhere=True)
    _use_bridge(monkeypatch, bridge)
    res = _send(filename="tts_hands_up_3a748ffc975b.wav", verify_timeout_s=0.3)
    assert res["ok"]
    assert {"track_index": res["track_index"], "mute": False} in bridge.cmds("set_track")
    assert "Unmuted the voice track." in res["warnings"]
    assert res["verified"]["playing"] is False
    assert "another track is soloed" in res["verified"]["reasons"]


def test_send_without_fire_skips_verification(audio_dir, monkeypatch):
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    res = _send(filename="tts_hands_up_3a748ffc975b.wav", fire=False)
    assert res["ok"] and res["verified"] is None and not bridge.cmds("fire_clip")


def test_old_live_falls_back_to_simpler(audio_dir, monkeypatch, tmp_path):
    ul = tmp_path / "User Library"
    ul.mkdir()
    monkeypatch.setenv("PULSE_USER_LIBRARY", str(ul))
    monkeypatch.setattr(srv.asyncio, "sleep", _no_sleep)
    bridge = FakeBridge(live_version="12.0.1")
    bridge.browser_misses = 2  # Live takes a moment to index the new sample
    _use_bridge(monkeypatch, bridge)
    res = _send(filename="tts_hands_up_3a748ffc975b.wav")
    assert res["ok"], res
    assert res["method"] == "simpler"
    assert (ul / "Samples" / "Pulse Voices" / "vox_tts_hands_up_3a748ffc975b.wav").is_file()
    assert bridge.tracks[res["track_index"]]["audio"] is False
    note = bridge.cmds("write_clip")[0]["notes"][0]
    assert note["pitch"] == 60 and note["duration"] == pytest.approx(res["duration_s"] * 4, rel=0.01)  # 240 bpm
    assert res["verified"]["playing"] is True


def test_not_connected_returns_file_for_manual_drag(audio_dir, monkeypatch):
    _use_bridge(monkeypatch, FakeBridge(), connected=False)
    res = _send(source_url="/audio_files/tts_hands_up_3a748ffc975b.wav")
    assert res["ok"] is False and res["error"] == "bridge_not_connected"
    assert os.path.isfile(res["file_path"])


def test_missing_file(audio_dir, monkeypatch):
    _use_bridge(monkeypatch, FakeBridge())
    assert _send(filename="nope.wav")["error"] == "voice_file_not_found"


_real_sleep = asyncio.sleep


async def _no_sleep(s):
    await _real_sleep(0)
