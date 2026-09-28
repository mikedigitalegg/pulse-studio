"""
Tests for the Pulse Samples pipeline: importing WAVs, listing/deleting them, searching Live's
library in the browser index, and sending a sample to an audio clip, a Simpler or a Drum Rack
pad. The bridge is faked.

    python -m pytest tests/test_samples.py -q
"""
import asyncio
import math
import os
import sys
import types

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

SR = 24000


def _wav(lead_s=0.2, tone_s=0.5, amp=6000, sample_rate=SR):
    lead = [0] * int(sample_rate * lead_s)
    tone = [int(amp * math.sin(2 * math.pi * 110 * i / sample_rate)) for i in range(int(sample_rate * tone_s))]
    return srv._wav_from_pcm(lead + tone, sample_rate=sample_rate, channels=1)


class FakeBridge:
    """A tiny Live: tracks with devices and Drum Rack pads, slots and a meter."""

    def __init__(self, live_version="12.1.0"):
        self.live_version = live_version
        self.tracks = [
            {"name": "Drums", "audio": False, "devices": ["909 Core Kit"], "pads": {36: "Kick 909", 37: "Rim 909"}},
            {"name": "Empty", "audio": False, "devices": [], "pads": None},
            {"name": "Bass", "audio": False, "devices": ["Drift"], "pads": None},
        ]
        self.calls = []
        self.fired = False
        self.browser_misses = 0

    def cmds(self, name):
        return [p for c, p in self.calls if c == name]

    async def __call__(self, cmd, params=None, timeout_s=5.0):
        params = params or {}
        self.calls.append((cmd, params))
        t = self.tracks[params["track_index"]] if "track_index" in params else None
        if cmd == "hello":
            return {"live_version": self.live_version}
        if cmd == "get_song":
            return {"tempo": 120.0, "track_names": [x["name"] for x in self.tracks]}
        if cmd in ("create_audio_track", "create_midi_track"):
            self.tracks.append({"name": "new", "audio": cmd == "create_audio_track", "devices": [], "pads": None})
            return {"index": len(self.tracks) - 1}
        if cmd == "set_track":
            if "name" in params:
                t["name"] = params["name"]
            return {}
        if cmd == "get_track":
            return {"index": params["track_index"], "devices": [{"name": d} for d in t["devices"]]}
        if cmd == "find_free_slot":
            return {"slot": 1}
        if cmd == "load_audio_clip":
            return {"length": 2.0}
        if cmd == "load_device":
            t["devices"].append(params["device_name"])
            t["pads"] = {}
            return {"loaded": True}
        if cmd in ("load_item_at_path", "load_item_to_drum_pad"):
            if self.browser_misses:
                self.browser_misses -= 1
                raise RuntimeError("item_not_found: " + params["path"])
            if cmd == "load_item_to_drum_pad":
                t["pads"][params["pad_note"]] = os.path.splitext(params["name"])[0]
            else:
                t["devices"].append("Simpler")
            return {"loaded": True}
        if cmd == "get_drum_pads":
            if t["pads"] is None:
                return {"rack": None, "pads": []}
            return {"rack": "Drum Rack", "pads": [{"note": n, "name": nm} for n, nm in sorted(t["pads"].items())]}
        if cmd == "write_clip":
            return {}
        if cmd == "fire_clip":
            self.fired = True
            return {}
        if cmd == "get_track_meter":
            return {"peak": 0.4 if self.fired else 0.0, "mute": False, "volume": 0.85, "is_playing": True}
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def samples_dir(tmp_path, monkeypatch):
    d = tmp_path / "samples"
    d.mkdir()
    monkeypatch.setattr(srv, "SAMPLES_DIR", str(d))
    ul = tmp_path / "User Library"
    ul.mkdir()
    monkeypatch.setenv("PULSE_USER_LIBRARY", str(ul))
    monkeypatch.setattr(srv.asyncio, "sleep", _no_sleep)
    return d


@pytest.fixture
def client():
    return TestClient(srv.app, base_url="http://127.0.0.1")


def _use_bridge(monkeypatch, bridge, connected=True):
    monkeypatch.setattr(srv, "BRIDGE", types.SimpleNamespace(connected=connected, status=lambda: {}))
    monkeypatch.setattr(srv, "_bridge_call", bridge)


def _import(client, data, name="Deep Kick!.wav", **form):
    return client.post("/samples/import", files={"file": (name, data, "audio/wav")}, data=form).json()


def _send(**kw):
    return asyncio.run(srv.send_sample_to_live(srv.SampleSendRequest(**kw)))


# ---------------------------------------------------------------- import / list / delete

def test_import_names_dedupes_and_lists(samples_dir, client):
    res = _import(client, _wav())
    assert res["ok"], res
    assert res["filename"].startswith("smp_deep_kick_") and res["name"] == "deep kick"
    assert res["duration_s"] == pytest.approx(0.7, abs=0.01) and res["sample_rate"] == SR
    assert _import(client, _wav())["duplicate"] is True

    listed = client.get("/samples/list").json()["samples"]
    assert [s["filename"] for s in listed] == [res["filename"]]
    assert client.get(listed[0]["url"]).status_code in (200, 404)  # mounted on the real dir, not tmp


def test_import_trim_cuts_leading_silence(samples_dir, client):
    res = _import(client, _wav(lead_s=0.3), name="take.wav", trim="true")
    assert res["ok"] and res["duration_s"] < 0.55


def test_import_rejects_non_wav_and_24_bit(samples_dir, client):
    assert _import(client, b"ID3" + b"\0" * 64, name="x.mp3")["error"] == "invalid_wav"
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(3)
        wf.setframerate(SR)
        wf.writeframes(b"\0\0\1" * 100)
    assert _import(client, buf.getvalue())["error"] == "unsupported_wav"
    assert os.listdir(samples_dir) == []


def test_delete_only_touches_imported_samples(samples_dir, client):
    res = _import(client, _wav())
    (samples_dir / "notes.txt").write_text("keep")
    assert client.post("/samples/delete", json={"filename": "../notes.txt"}).json()["error"] == "sample_not_found"
    assert client.post("/samples/delete", json={"filename": res["filename"]}).json()["ok"]
    assert os.listdir(samples_dir) == ["notes.txt"]


def test_library_search_filters_to_samples(monkeypatch, client):
    items = [
        {"name": "808 Heavy E.wav", "path": "samples", "uri": "u1"},
        {"name": "Kick Deep.wav", "path": "samples", "uri": "u2"},
        {"name": "Kick Deep.wav", "path": "packs/Core Library/Samples", "uri": "u3"},  # duplicate listing
        {"name": "My Kick.aif", "path": "user_library/Samples/Mine", "uri": "u4"},
        {"name": "Kick Kit.adg", "path": "drums", "uri": "u5"},
    ]
    monkeypatch.setattr(srv, "_browser_index_items", lambda: items)
    res = client.get("/samples/library", params={"q": "kick"}).json()
    assert [(i["path"], i["name"]) for i in res["items"]] == [("samples", "Kick Deep.wav"), ("user_library/Samples/Mine", "My Kick.aif")]
    assert len(client.get("/samples/library").json()["items"]) == 3


# ---------------------------------------------------------------- send to Live

def test_audio_clip_creates_sample_track_and_confirms_audio(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    res = _send(filename=fn)
    assert res["ok"], res
    assert res["method"] == "audio_clip" and bridge.tracks[res["track_index"]]["name"] == "PS-SMP"
    load = bridge.cmds("load_audio_clip")[0]
    assert os.path.isabs(load["file_path"]) and load["warping"] is False and load["name"] == "deep kick"
    assert res["verified"]["playing"] is True


def test_audio_clip_loop_warps(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    assert _send(filename=fn, loop=True, fire=False)["ok"]
    load = bridge.cmds("load_audio_clip")[0]
    assert load["warping"] is True and load["looping"] is True


def test_audio_clip_needs_live_12_0_5(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    _use_bridge(monkeypatch, FakeBridge(live_version="12.0.1"))
    res = _send(filename=fn)
    assert res["error"] == "unsupported" and "Simpler" in res["hint"]


def test_simpler_copies_to_user_library_and_writes_note(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    bridge = FakeBridge()
    bridge.browser_misses = 2  # Live indexes the new file in the background
    _use_bridge(monkeypatch, bridge)
    res = _send(filename=fn, target="simpler")
    assert res["ok"], res
    ul = os.environ["PULSE_USER_LIBRARY"]
    assert os.path.isfile(os.path.join(ul, "Samples", "Pulse Samples", fn))
    load = bridge.cmds("load_item_at_path")[-1]
    assert load["path"] == "user_library/Samples/Pulse Samples" and load["name"] == fn
    assert bridge.tracks[res["track_index"]]["name"] == "PS-SMP deep kick"
    note = bridge.cmds("write_clip")[0]["notes"][0]
    assert note["pitch"] == 60 and note["duration"] == pytest.approx(0.7 * 2, rel=0.02)  # 120 bpm


def test_library_sample_to_simpler_uses_browser_path(monkeypatch):
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    res = _send(library_path="samples", library_name="Kick Deep.wav", library_uri="u2", target="simpler", fire=False)
    assert res["ok"], res
    assert bridge.cmds("load_item_at_path")[0] == {"track_index": res["track_index"], "path": "samples", "name": "Kick Deep.wav", "uri": "u2"}


def test_library_sample_cannot_be_audio_clip(monkeypatch):
    _use_bridge(monkeypatch, FakeBridge())
    assert _send(library_path="samples", library_name="Kick Deep.wav")["error"] == "library_not_audio_clip"


def test_drum_pad_picks_first_empty_pad(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    res = _send(filename=fn, target="drum_pad", track_index=0)
    assert res["ok"], res
    assert res["pad_note"] == 38 and res["method"] == "drum_pad" and res["verified"] is None
    assert bridge.tracks[0]["pads"][38] == os.path.splitext(fn)[0]
    assert not bridge.cmds("fire_clip")


def test_drum_pad_adds_rack_to_empty_track_and_refuses_synth(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    bridge = FakeBridge()
    _use_bridge(monkeypatch, bridge)
    res = _send(filename=fn, target="drum_pad", track_index=1, pad_note=40)
    assert res["ok"] and res["pad_note"] == 40
    assert "Added an empty Drum Rack to the track." in res["warnings"]
    assert _send(filename=fn, target="drum_pad", track_index=2)["error"] == "no_drum_rack"
    assert _send(filename=fn, target="drum_pad")["error"] == "missing_track"


def test_drum_pad_on_old_bridge_says_reinstall(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    bridge = FakeBridge()

    async def old_bridge(cmd, params=None, timeout_s=5.0):
        if cmd == "load_item_to_drum_pad":
            raise RuntimeError("unknown_command: load_item_to_drum_pad")
        return await bridge(cmd, params, timeout_s)

    _use_bridge(monkeypatch, old_bridge)
    res = _send(filename=fn, target="drum_pad", track_index=0)
    assert res["error"] == "drum_pad_load_failed" and "install.py" in res["hint"]


def test_not_connected_and_bad_input(samples_dir, client, monkeypatch):
    fn = _import(client, _wav())["filename"]
    _use_bridge(monkeypatch, FakeBridge(), connected=False)
    res = _send(filename=fn)
    assert res["error"] == "bridge_not_connected" and os.path.isfile(res["file_path"])
    assert _send(filename="../../secret.wav")["error"] == "sample_not_found"
    assert _send()["error"] == "missing_sample"
    assert _send(filename=fn, target="sampler")["error"] == "bad_target"


_real_sleep = asyncio.sleep


async def _no_sleep(s):
    await _real_sleep(0)
