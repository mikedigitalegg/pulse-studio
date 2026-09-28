"""
LiveLink: a drop-in AbletonController that prefers PulseBridge and falls back to AbletonOSC.

Every AbletonController method funnels through send(address, args), so mapping OSC
addresses here moves the whole server onto the bridge without touching call sites.
Addresses that have no bridge mapping (or when the bridge is down) still go out as OSC.
"""
from __future__ import annotations

from typing import Callable

from ableton_track_creator import AbletonController
from pulse_bridge_client import BridgeError, BridgeUnavailable, PulseBridgeClient


def _note(args):
    t, s, pitch, start, dur, vel = args[:6]
    mute = bool(args[6]) if len(args) > 6 else False
    return "add_notes", {
        "track_index": int(t),
        "clip_slot_index": int(s),
        "notes": [{"pitch": int(pitch), "start_time": float(start), "duration": float(dur), "velocity": float(vel), "mute": mute}],
    }


def _ts(cmd: str) -> Callable:
    return lambda a: (cmd, {"track_index": int(a[0]), "clip_slot_index": int(a[1])})


def _track_set(field: str, conv: Callable) -> Callable:
    return lambda a: ("set_track", {"track_index": int(a[0]), field: conv(a[1])})


# OSC address -> (bridge command, params) for fire-and-forget messages.
_SEND_MAP: dict[str, Callable] = {
    "/live/song/start_playing": lambda a: ("play", {}),
    "/live/song/stop_playing": lambda a: ("stop", {}),
    "/live/song/set/tempo": lambda a: ("set_tempo", {"tempo": float(a[0])}),
    "/live/song/create_midi_track": lambda a: ("create_midi_track", {"index": int(a[0]) if a else -1}),
    "/live/song/create_audio_track": lambda a: ("create_audio_track", {"index": int(a[0]) if a else -1}),
    "/live/song/create_scene": lambda a: ("create_scene", {"index": int(a[0]) if a else -1}),
    "/live/scene/fire": lambda a: ("fire_scene", {"scene_index": int(a[0])}),
    "/live/scene/set/name": lambda a: ("set_scene_name", {"scene_index": int(a[0]), "name": str(a[1])}),
    "/live/track/set/name": _track_set("name", str),
    "/live/track/set/volume": _track_set("volume", float),
    "/live/track/set/panning": _track_set("panning", float),
    "/live/track/set/arm": _track_set("arm", bool),
    "/live/track/set/mute": _track_set("mute", bool),
    "/live/track/set/solo": _track_set("solo", bool),
    "/live/track/load_device": lambda a: ("load_device", {"track_index": int(a[0]), "device_name": str(a[1])}),
    "/live/clip_slot/delete_clip": _ts("delete_clip"),
    "/live/clip_slot/create_clip": lambda a: (
        "create_clip", {"track_index": int(a[0]), "clip_slot_index": int(a[1]), "length": float(a[2])}
    ),
    "/live/clip_slot/fire": _ts("fire_clip"),
    "/live/clip_slot/stop": _ts("stop_clip"),
    "/live/clip/set/name": lambda a: ("set_clip_name", {"track_index": int(a[0]), "clip_slot_index": int(a[1]), "name": str(a[2])}),
    "/live/clip/add/notes": _note,
    "/live/clip/remove/notes": lambda a: ("remove_notes", {
        "track_index": int(a[0]), "clip_slot_index": int(a[1]),
        "from_time": float(a[2]), "time_span": float(a[3]),
        "from_pitch": int(a[4]), "pitch_span": int(a[5]),
    }),
    "/live/device/set/parameter/value": lambda a: ("set_device_param", {
        "track_index": int(a[0]), "device_index": int(a[1]), "param_index": int(a[2]), "value": float(a[3]),
    }),
}


def _song_field(key: str, conv: Callable = lambda v: v) -> tuple:
    return "get_song", lambda a: {}, lambda a, r: (conv(r[key]),)


# OSC query address -> (bridge command, params builder, reply -> AbletonOSC-shaped args tuple).
_QUERY_MAP: dict[str, tuple] = {
    "/live/song/get/tempo": _song_field("tempo"),
    "/live/song/get/is_playing": _song_field("is_playing", int),
    "/live/song/get/current_song_time": _song_field("current_song_time"),
    "/live/song/get/num_tracks": _song_field("num_tracks"),
    "/live/song/get/num_scenes": _song_field("num_scenes"),
    "/live/song/get/track_names": ("get_song", lambda a: {}, lambda a, r: tuple(r["track_names"])),
    "/live/track/get/name": (
        "get_track", lambda a: {"track_index": int(a[0])}, lambda a, r: (int(a[0]), r["name"])
    ),
    "/live/track/get/num_devices": (
        "get_track", lambda a: {"track_index": int(a[0])}, lambda a, r: (int(a[0]), r["num_devices"])
    ),
    "/live/clip_slot/get/has_clip": (
        "has_clip",
        lambda a: {"track_index": int(a[0]), "clip_slot_index": int(a[1])},
        lambda a, r: (int(a[0]), int(a[1]), bool(r["has_clip"])),
    ),
    "/live/device/get/parameters/name": (
        "get_device_params",
        lambda a: {"track_index": int(a[0]), "device_index": int(a[1])},
        lambda a, r: (int(a[0]), int(a[1]), *[p["name"] for p in r["parameters"]]),
    ),
}


class LiveLink(AbletonController):
    def __init__(self, bridge: PulseBridgeClient):
        super().__init__()
        self.bridge = bridge

    @property
    def bridge_connected(self) -> bool:
        return self.bridge.connected

    def send(self, address, args=None):
        args = list(args or [])
        mapper = _SEND_MAP.get(address)
        if mapper is not None and self.bridge.connected:
            cmd, params = mapper(args)
            try:
                self.bridge.send(cmd, params)
                return
            except BridgeUnavailable:
                pass  # dropped mid-call: fall through to OSC
        super().send(address, args)

    def query(self, address: str, args: list | None = None, timeout_s: float = 1.0) -> dict | None:
        """
        Answer an AbletonOSC-style query through the bridge, in the same
        {"ok", "address", "args"} shape the server already parses.
        Returns None when the bridge can't answer, so callers can fall back to OSC.
        """
        entry = _QUERY_MAP.get(address)
        if entry is None or not self.bridge.connected:
            return None
        args = list(args or [])
        cmd, build, shape = entry
        try:
            result = self.bridge.request(cmd, build(args), timeout_s=max(1.0, float(timeout_s)))
        except BridgeUnavailable:
            return None
        except BridgeError as e:
            return {"ok": False, "address": address, "error": str(e), "source": "bridge"}
        return {"ok": True, "address": address, "args": shape(args, result), "source": "bridge"}
