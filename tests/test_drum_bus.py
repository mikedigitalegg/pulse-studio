"""
Techno-family styles put a drum bus after the kit for harder kicks; switching to a style without
one removes it. The bridge is faked.

    python -m pytest tests/test_drum_bus.py -q
"""
import asyncio
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402

PARAMS = {
    "Drum Buss": [("Device On", 0, 1, True), ("Drive", 0.0, 1.0, False), ("Crunch", 0.0, 1.0, False),
                  ("Transients", -1.0, 1.0, False), ("Boom Amt", 0.0, 1.0, False), ("Boom Freq", 30.0, 90.0, False),
                  ("Boom Decay", 0.0, 1.0, False), ("Compressor On", 0, 1, True)],
    "Saturator": [("Device On", 0, 1, True), ("Drive", -36.0, 36.0, False), ("Soft Clip", 0, 1, True)],
}


class FakeBridge:
    def __init__(self, devices):
        self.devices = devices  # list of (name, type)
        self.set = {}

    async def __call__(self, cmd, params=None, timeout_s=5.0):
        params = params or {}
        if cmd == "get_track":
            return {"devices": [{"index": i, "name": n, "type": t} for i, (n, t) in enumerate(self.devices)]}
        if cmd == "load_device":
            self.devices.append((params["device_name"], "audio_effect"))
            return {}
        if cmd == "delete_device":
            del self.devices[params["device_index"]]
            return {}
        if cmd == "get_device_params":
            name = self.devices[params["device_index"]][0]
            return {"parameters": [{"index": i, "name": n, "value": lo, "min": lo, "max": hi, "is_quantized": q}
                                   for i, (n, lo, hi, q) in enumerate(PARAMS[name])]}
        if cmd == "set_device_param":
            name = self.devices[params["device_index"]][0]
            self.set[(name, PARAMS[name][params["param_index"]][0])] = params["value"]
            return {}
        raise AssertionError(cmd)


@pytest.fixture
def use(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "PALETTE_STATE_FILE", str(tmp_path / "palette.json"))

    def _use(bridge, effects=("Drum Buss", "Saturator", "Reverb")):
        monkeypatch.setattr(srv, "_bridge_call", bridge)
        monkeypatch.setattr(srv, "BRIDGE", types.SimpleNamespace(connected=True, capabilities={"devices": {"audio_effects": list(effects)}}))
    return _use


def _bus(style):
    return asyncio.run(srv._apply_drum_bus(style, 0))


def test_techno_loads_drum_buss_with_normalized_params(use):
    bridge = FakeBridge([("909 Core Kit", "instrument")])
    use(bridge)
    res = _bus("techno")
    assert res["action"] == "loaded" and res["device"] == "Drum Buss"
    assert [n for n, _ in bridge.devices] == ["909 Core Kit", "Drum Buss"]
    assert bridge.set[("Drum Buss", "Boom Amt")] == pytest.approx(0.45)   # alias matched, not "Boom Freq"
    assert bridge.set[("Drum Buss", "Transients")] == pytest.approx(0.3)  # 0.65 across -1..1
    assert bridge.set[("Drum Buss", "Compressor On")] == 1.0
    assert "unmatched_params" not in res


def test_same_style_again_updates_without_duplicating(use):
    bridge = FakeBridge([("909 Core Kit", "instrument")])
    use(bridge)
    _bus("techno")
    assert _bus("techno")["action"] == "updated"
    assert [n for n, _ in bridge.devices].count("Drum Buss") == 1


def test_editions_without_drum_buss_fall_back_to_saturator(use):
    bridge = FakeBridge([("909 Core Kit", "instrument")])
    use(bridge, effects=("Saturator", "Reverb"))
    res = _bus("gabber")
    assert res["device"] == "Saturator"
    assert bridge.set[("Saturator", "Drive")] == pytest.approx(-36 + 0.85 * 72)


def test_switching_to_house_removes_the_bus(use):
    bridge = FakeBridge([("909 Core Kit", "instrument")])
    use(bridge)
    _bus("techno")
    res = _bus("house")
    assert res["action"] == "removed" and res["removed"] == "Drum Buss"
    assert [n for n, _ in bridge.devices] == ["909 Core Kit"]


def test_user_added_drum_buss_is_left_alone_on_style_change(use):
    bridge = FakeBridge([("909 Core Kit", "instrument"), ("Drum Buss", "audio_effect")])
    use(bridge)
    assert _bus("house")["action"] == "none"
    assert [n for n, _ in bridge.devices] == ["909 Core Kit", "Drum Buss"]


def test_bus_is_moved_after_a_reloaded_kit(use):
    bridge = FakeBridge([("Drum Buss", "audio_effect"), ("808 Core Kit", "instrument")])
    use(bridge)
    res = _bus("techno")
    assert res["action"] == "loaded"
    assert [n for n, _ in bridge.devices] == ["808 Core Kit", "Drum Buss"]
