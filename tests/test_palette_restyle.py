"""
Instruments Pulse picked for one style are swapped when you generate a different style;
instruments you loaded yourself are kept. The bridge is faked.

    python -m pytest tests/test_palette_restyle.py -q
"""
import asyncio
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402


class FakeBridge:
    def __init__(self, devices):
        self.devices = devices  # track_index -> device name (or None)
        self.deleted, self.loaded = [], []

    async def __call__(self, cmd, params=None, timeout_s=5.0):
        params = params or {}
        ti = params.get("track_index")
        if cmd == "get_track":
            name = self.devices.get(ti)
            devs = [{"index": 0, "name": name, "type": "instrument"}] if name else []
            if name and name.endswith("Kit"):
                devs[0].update({"is_drum_rack": True, "filled_pads": 16})
            return {"devices": devs}
        if cmd == "delete_device":
            self.deleted.append(ti)
            self.devices[ti] = None
            return {}
        if cmd == "load_item_at_path":
            name = srv.instrument_palette.display_name({"name": params["name"]})
            self.devices[ti] = name
            self.loaded.append((ti, name))
            return {"loaded": True}
        if cmd == "load_device":
            self.devices[ti] = params["device_name"]
            return {}
        if cmd == "get_return_tracks":
            raise srv.BridgeError("unknown_command: get_return_tracks")  # a PulseBridge before 0.2.0
        raise AssertionError(cmd)


INDEX = [
    {"name": "909 Core Kit.adg", "path": "drums", "uri": "u1", "is_loadable": True},
    {"name": "Dark Rumble Bass.adv", "path": "sounds/bass", "uri": "u2", "is_loadable": True},
    {"name": "Deep House Organ Bass.adv", "path": "sounds/bass", "uri": "u3", "is_loadable": True},
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "PALETTE_STATE_FILE", str(tmp_path / "palette.json"))
    monkeypatch.setattr(srv, "_browser_index_items", lambda: INDEX)
    monkeypatch.setattr(srv, "PULSE_TRACK_PLAN", [{"track_index": 1, "role": "bass"}])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(srv.asyncio, "sleep", _no_sleep)

    def use(bridge):
        monkeypatch.setattr(srv, "_bridge_call", bridge)
        monkeypatch.setattr(srv, "BRIDGE", types.SimpleNamespace(connected=True, capabilities={}, status=lambda: {}))
    return use


_real_sleep = asyncio.sleep


async def _no_sleep(s):
    await _real_sleep(0)


def _apply(style):
    return asyncio.run(srv._apply_instrument_palette(style))["results"][0]


def test_pulse_picks_are_swapped_when_style_changes(env):
    bridge = FakeBridge({1: None})
    env(bridge)
    first = _apply("techno")
    assert first["action"] == "loaded" and first["name"] == "Dark Rumble Bass"

    again = _apply("techno")
    assert again["action"] == "kept"  # same style: nothing to change

    house = _apply("house")
    assert house["action"] == "loaded" and house["name"] == "Deep House Organ Bass"
    assert house["replaced_style"] == "techno"
    assert bridge.deleted == [1]


def test_user_loaded_instruments_are_kept(env):
    bridge = FakeBridge({1: None})
    env(bridge)
    _apply("techno")
    bridge.devices[1] = "My Favourite Bass"  # user swapped the instrument by hand
    res = _apply("house")
    assert res["action"] == "kept" and res["name"] == "My Favourite Bass"
