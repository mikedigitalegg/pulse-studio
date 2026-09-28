"""
Command handlers. Every handler runs on Live's main thread and takes a params dict.

Handlers feature-detect the Live API instead of checking version numbers, so the same
script works on Live 11 and 12 and in every edition. Anything edition-specific is
reported by get_capabilities rather than assumed.
"""
from __future__ import absolute_import, print_function, unicode_literals

import os
import sys

try:
    import Live  # provided by Ableton Live
except ImportError:  # unit tests
    Live = None

BRIDGE_VERSION = "0.2.0"
PROTOCOL_VERSION = 1

BROWSER_ROOTS = (
    "instruments",
    "drums",
    "sounds",
    "audio_effects",
    "midi_effects",
    "max_for_live",
    "plugins",
    "clips",
    "samples",
    "packs",
    "user_library",
    "current_project",
)
DEVICE_ROOTS = ("instruments", "audio_effects", "midi_effects", "drums")

# Preference order for a melodic fallback instrument. Drift ships with every Live 12 edition.
MELODIC_PREFERENCE = ("Wavetable", "Drift", "Analog", "Operator", "Simpler")
DRUM_PREFERENCE = ("Drum Rack", "Drum Sampler", "Impulse")
SUITE_ONLY_INSTRUMENTS = ("Operator", "Sampler", "Analog", "Collision", "Electric", "Tension", "Meld")
STANDARD_UP_INSTRUMENTS = ("Wavetable",)
URI_SEARCH_BUDGET = 20000
DEVICE_TYPES = {1: "instrument", 2: "audio_effect", 4: "midi_effect"}  # Live.Device.DeviceType


class CommandError(Exception):
    pass


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _int(params, key, default=None):
    v = params.get(key, default)
    if v is None:
        raise CommandError("missing_param: %s" % key)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise CommandError("bad_param: %s" % key)


def _float(params, key, default=None):
    v = params.get(key, default)
    if v is None:
        raise CommandError("missing_param: %s" % key)
    try:
        return float(v)
    except (TypeError, ValueError):
        raise CommandError("bad_param: %s" % key)


class Commands(object):
    def __init__(self, host):
        # host provides song(), application(), log(msg)
        self.host = host
        self._caps = None
        self._uri_cache = {}
        self.handlers = {
            "hello": self.hello,
            "get_capabilities": self.get_capabilities,
            "get_song": self.get_song,
            "get_snapshot": self.get_snapshot,
            "set_tempo": self.set_tempo,
            "play": self.play,
            "stop": self.stop,
            "stop_all_clips": self.stop_all_clips,
            "set_song_key": self.set_song_key,
            "create_midi_track": self.create_midi_track,
            "create_audio_track": self.create_audio_track,
            "create_return_track": self.create_return_track,
            "get_return_tracks": self.get_return_tracks,
            "set_send": self.set_send,
            "get_track": self.get_track,
            "set_track": self.set_track,
            "create_scene": self.create_scene,
            "set_scene_name": self.set_scene_name,
            "fire_scene": self.fire_scene,
            "has_clip": self.has_clip,
            "find_free_slot": self.find_free_slot,
            "create_clip": self.create_clip,
            "delete_clip": self.delete_clip,
            "set_clip_name": self.set_clip_name,
            "fire_clip": self.fire_clip,
            "stop_clip": self.stop_clip,
            "add_notes": self.add_notes,
            "get_notes": self.get_notes,
            "remove_notes": self.remove_notes,
            "write_clip": self.write_clip,
            "get_device_params": self.get_device_params,
            "set_device_param": self.set_device_param,
            "load_device": self.load_device,
            "load_item_at_path": self.load_item_at_path,
            "delete_device": self.delete_device,
            "get_drum_pads": self.get_drum_pads,
            "load_item_to_drum_pad": self.load_item_to_drum_pad,
            "load_audio_clip": self.load_audio_clip,
            "get_track_meter": self.get_track_meter,
            # AbletonMCP-compatible commands (same names and result shapes).
            "get_session_info": self.get_session_info,
            "get_browser_tree": self.get_browser_tree,
            "get_browser_items_at_path": self.get_browser_items_at_path,
            "load_browser_item": self.load_browser_item,
        }

    # ------------------------------------------------------------------ helpers

    @property
    def song(self):
        return self.host.song()

    @property
    def browser(self):
        return self.host.application().browser

    def _track(self, params, key="track_index"):
        """A regular track, or a return track when params has "return_index" instead."""
        if params.get("return_index") is not None:
            i = _int(params, "return_index")
            tracks = self.song.return_tracks
            if i < 0 or i >= len(tracks):
                raise CommandError("return_index_out_of_range: %d (returns: %d)" % (i, len(tracks)))
            return i, tracks[i]
        i = _int(params, key)
        tracks = self.song.tracks
        if i < 0 or i >= len(tracks):
            raise CommandError("track_index_out_of_range: %d (tracks: %d)" % (i, len(tracks)))
        return i, tracks[i]

    def _slot(self, params, create_scenes=False):
        ti, track = self._track(params)
        si = _int(params, "clip_slot_index")
        if si < 0:
            raise CommandError("clip_slot_index_out_of_range: %d" % si)
        if si >= len(track.clip_slots):
            if not create_scenes:
                raise CommandError("clip_slot_index_out_of_range: %d (scenes: %d)" % (si, len(track.clip_slots)))
            while len(self.song.scenes) <= si:
                self.song.create_scene(-1)
        return ti, si, track.clip_slots[si]

    def _clip(self, params):
        ti, si, slot = self._slot(params)
        if not slot.has_clip:
            raise CommandError("no_clip: track %d slot %d" % (ti, si))
        return slot.clip

    def _device(self, params):
        ti, track = self._track(params)
        di = _int(params, "device_index")
        devices = track.devices
        if di < 0 or di >= len(devices):
            raise CommandError("device_index_out_of_range: %d (devices: %d)" % (di, len(devices)))
        return devices[di]

    def _browser_root(self, name):
        b = self.browser
        key = str(name or "").strip().lower().replace(" ", "_")
        if key in BROWSER_ROOTS and hasattr(b, key):
            return getattr(b, key)
        return None

    @staticmethod
    def _children(item):
        try:
            return list(item.children)
        except Exception:
            return []

    # ------------------------------------------------------------------ info

    def hello(self, params):
        app = self.host.application()
        return {
            "bridge": "PulseBridge",
            "bridge_version": BRIDGE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "live_version": "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version()),
            "python_version": "%d.%d.%d" % tuple(sys.version_info[:3]),
        }

    def get_capabilities(self, params):
        if self._caps is not None and not params.get("refresh"):
            return self._caps
        caps = self.hello({})
        song = self.song
        note_spec = bool(Live is not None and hasattr(Live, "Clip") and hasattr(Live.Clip, "MidiNoteSpecification"))
        caps["features"] = {
            "note_extended": note_spec,  # probability, velocity_deviation, release_velocity
            "song_key": hasattr(song, "root_note") and hasattr(song, "scale_name"),
            "undo_steps": hasattr(song, "begin_undo_step"),
            "groove_pool": hasattr(song, "groove_pool"),
        }

        names = {}
        for root in ("instruments", "audio_effects", "midi_effects"):
            item = self._browser_root(root)
            names[root] = sorted(c.name for c in self._children(item)) if item is not None else []
        m4l = self._browser_root("max_for_live")
        caps["features"]["max_for_live_content"] = bool(m4l is not None and self._children(m4l))
        caps["devices"] = names

        inst = set(names["instruments"])
        if any(n in inst for n in SUITE_ONLY_INSTRUMENTS):
            edition, limit = "suite", None
        elif any(n in inst for n in STANDARD_UP_INSTRUMENTS):
            edition, limit = "standard", None
        elif inst:
            # Lite allows 8 tracks, Intro 16; the browser can't tell them apart.
            edition, limit = "intro_or_lite", 8
        else:
            edition, limit = "unknown", None
        caps["edition_guess"] = edition
        caps["track_limit_hint"] = limit
        caps["defaults"] = {
            "drums": next((n for n in DRUM_PREFERENCE if n in inst), "Drum Rack"),
            "melodic": next((n for n in MELODIC_PREFERENCE if n in inst), "Simpler"),
        }
        self._caps = caps
        return caps

    def get_song(self, params):
        s = self.song
        out = {
            "tempo": s.tempo,
            "is_playing": bool(s.is_playing),
            "current_song_time": s.current_song_time,
            "signature_numerator": s.signature_numerator,
            "signature_denominator": s.signature_denominator,
            "clip_trigger_quantization": int(s.clip_trigger_quantization),  # Live's launch quantization menu, 0 = None
            "num_tracks": len(s.tracks),
            "num_scenes": len(s.scenes),
            "track_names": [t.name for t in s.tracks],
            "scene_names": [sc.name for sc in s.scenes],
        }
        if hasattr(s, "root_note"):
            out["root_note"] = s.root_note
            out["scale_name"] = getattr(s, "scale_name", None)
            out["scale_mode"] = bool(getattr(s, "scale_mode", False))
        return out

    def get_snapshot(self, params):
        out = self.get_song(params)
        tracks = []
        for i, t in enumerate(self.song.tracks):
            tracks.append({
                "index": i,
                "name": t.name,
                "is_midi": bool(t.has_midi_input),
                "devices": [d.name for d in t.devices],
                "playing_slot_index": t.playing_slot_index,
                "fired_slot_index": t.fired_slot_index,
                "mute": bool(t.mute),
            })
        out["tracks"] = tracks
        return out

    # ------------------------------------------------------------------ song

    def set_tempo(self, params):
        self.song.tempo = _clamp(_float(params, "tempo"), 20.0, 999.0)
        return {"tempo": self.song.tempo}

    def play(self, params):
        self.song.start_playing()
        return {"is_playing": True}

    def stop(self, params):
        self.song.stop_playing()
        return {"is_playing": False}

    def stop_all_clips(self, params):
        """Stop every clip (quantized like Live's own Stop All Clips button); transport keeps running."""
        self.song.stop_all_clips()
        return {"stopped": True}

    def set_song_key(self, params):
        s = self.song
        if not hasattr(s, "root_note"):
            raise CommandError("unsupported: song key needs Live 12")
        if params.get("root_note") is not None:
            s.root_note = _clamp(_int(params, "root_note"), 0, 11)
        if params.get("scale_name"):
            s.scale_name = str(params["scale_name"])
        if params.get("scale_mode") is not None and hasattr(s, "scale_mode"):
            s.scale_mode = bool(params["scale_mode"])
        return {"root_note": s.root_note, "scale_name": s.scale_name}

    def create_midi_track(self, params):
        index = int(params.get("index", -1))
        self.song.create_midi_track(index)
        n = len(self.song.tracks)
        return {"index": n - 1 if index < 0 else index, "num_tracks": n}

    def create_audio_track(self, params):
        index = int(params.get("index", -1))
        self.song.create_audio_track(index)
        n = len(self.song.tracks)
        return {"index": n - 1 if index < 0 else index, "num_tracks": n}

    def create_return_track(self, params):
        """Add a return track at the end. Every track gets a new send for it."""
        s = self.song
        before = len(s.return_tracks)
        try:
            s.create_return_track()
        except Exception as e:
            # Live refuses past the edition's return limit (Lite and Intro allow 2).
            raise CommandError("return_track_limit: %s" % e)
        if len(s.return_tracks) <= before:
            raise CommandError("return_track_limit: Live didn't add a return track")
        index = len(s.return_tracks) - 1
        if params.get("name") is not None:
            s.return_tracks[index].name = str(params["name"])
        return {"return_index": index, "num_returns": len(s.return_tracks)}

    def get_return_tracks(self, params):
        return {"returns": [
            {"return_index": i, "name": t.name, "devices": [self._device_info(j, d) for j, d in enumerate(t.devices)]}
            for i, t in enumerate(self.song.return_tracks)
        ]}

    def set_send(self, params):
        ti, t = self._track(params)
        si = _int(params, "send_index")
        sends = list(t.mixer_device.sends)
        if si < 0 or si >= len(sends):
            raise CommandError("send_index_out_of_range: %d (sends: %d)" % (si, len(sends)))
        p = sends[si]
        p.value = _clamp(_float(params, "value"), p.min, p.max)
        return {"track_index": ti, "send_index": si, "value": p.value}

    # ------------------------------------------------------------------ tracks

    @staticmethod
    def _device_info(i, d):
        info = {
            "index": i,
            "name": d.name,
            "class_name": d.class_name,
            "type": DEVICE_TYPES.get(int(getattr(d, "type", 0) or 0), "unknown"),
        }
        if getattr(d, "can_have_drum_pads", False):
            # An empty Drum Rack is silent; callers treat it as "no instrument".
            info["is_drum_rack"] = True
            info["filled_pads"] = sum(1 for p in d.drum_pads if len(p.chains) > 0)
        return info

    def get_track(self, params):
        ti, t = self._track(params)
        return {
            "index": ti,
            "name": t.name,
            "is_midi": bool(t.has_midi_input),
            "num_devices": len(t.devices),
            "devices": [self._device_info(i, d) for i, d in enumerate(t.devices)],
            "volume": t.mixer_device.volume.value,
            "panning": t.mixer_device.panning.value,
            "sends": [p.value for p in getattr(t.mixer_device, "sends", ())],
            "mute": bool(t.mute),
            "solo": bool(t.solo),
            "playing_slot_index": getattr(t, "playing_slot_index", -1),  # return tracks have no slots
        }

    def set_track(self, params):
        ti, t = self._track(params)
        if params.get("name") is not None:
            t.name = str(params["name"])
        if params.get("volume") is not None:
            p = t.mixer_device.volume
            p.value = _clamp(_float(params, "volume"), p.min, p.max)
        if params.get("panning") is not None:
            p = t.mixer_device.panning
            p.value = _clamp(_float(params, "panning"), p.min, p.max)
        for flag in ("mute", "solo", "arm"):
            if params.get(flag) is not None:
                if flag == "arm" and not t.can_be_armed:
                    continue
                setattr(t, flag, bool(params[flag]))
        return {"index": ti, "name": t.name}

    # ------------------------------------------------------------------ scenes

    def create_scene(self, params):
        index = int(params.get("index", -1))
        self.song.create_scene(index)
        return {"num_scenes": len(self.song.scenes)}

    def set_scene_name(self, params):
        i = _int(params, "scene_index")
        scenes = self.song.scenes
        if i < 0 or i >= len(scenes):
            raise CommandError("scene_index_out_of_range: %d" % i)
        scenes[i].name = str(params.get("name", ""))
        return {"scene_index": i}

    def fire_scene(self, params):
        i = _int(params, "scene_index")
        scenes = self.song.scenes
        if i < 0 or i >= len(scenes):
            raise CommandError("scene_index_out_of_range: %d" % i)
        scenes[i].fire()
        return {"scene_index": i}

    # ------------------------------------------------------------------ clips

    def has_clip(self, params):
        ti, t = self._track(params)
        si = _int(params, "clip_slot_index")
        if si < 0 or si >= len(t.clip_slots):
            return {"has_clip": False, "exists": False}
        return {"has_clip": bool(t.clip_slots[si].has_clip), "exists": True}

    def find_free_slot(self, params):
        ti, t = self._track(params)
        start = max(0, int(params.get("start_slot_index", 0)))
        max_slots = _clamp(int(params.get("max_slots", 256)), 1, 2048)
        slots = t.clip_slots
        for si in range(start, start + max_slots):
            if si >= len(slots) or not slots[si].has_clip:
                # An index past the last scene is free; create_clip adds scenes as needed.
                return {"track_index": ti, "slot": si, "needs_scene": si >= len(slots)}
        raise CommandError("no_free_slot_found")

    def create_clip(self, params):
        ti, si, slot = self._slot(params, create_scenes=True)
        if slot.has_clip:
            if params.get("replace", True):
                slot.delete_clip()
            else:
                raise CommandError("slot_has_clip")
        length = max(0.25, _float(params, "length", 4.0))
        slot.create_clip(length)
        return {"track_index": ti, "clip_slot_index": si, "length": slot.clip.length}

    def delete_clip(self, params):
        ti, t = self._track(params)
        si = _int(params, "clip_slot_index")
        if 0 <= si < len(t.clip_slots) and t.clip_slots[si].has_clip:
            t.clip_slots[si].delete_clip()
            return {"deleted": True}
        return {"deleted": False}

    def set_clip_name(self, params):
        clip = self._clip(params)
        clip.name = str(params.get("name", ""))
        return {"name": clip.name}

    def fire_clip(self, params):
        ti, si, slot = self._slot(params)
        slot.fire()
        return {"fired": bool(slot.has_clip)}

    def stop_clip(self, params):
        ti, si, slot = self._slot(params)
        slot.stop()
        return {"stopped": True}

    @staticmethod
    def _note_fields(n):
        return (
            _clamp(int(n.get("pitch", 60)), 0, 127),
            max(0.0, float(n.get("start_time", 0.0))),
            max(1.0 / 128.0, float(n.get("duration", 0.25))),
            _clamp(float(n.get("velocity", 100)), 1.0, 127.0),
            bool(n.get("mute", False)),
        )

    def _add_notes_to_clip(self, clip, notes):
        if not notes:
            return 0
        if Live is not None and hasattr(Live.Clip, "MidiNoteSpecification") and hasattr(clip, "add_new_notes"):
            specs = []
            for n in notes:
                pitch, start, dur, vel, mute = self._note_fields(n)
                specs.append(Live.Clip.MidiNoteSpecification(
                    pitch=pitch,
                    start_time=start,
                    duration=dur,
                    velocity=vel,
                    mute=mute,
                    probability=_clamp(float(n.get("probability", 1.0)), 0.0, 1.0),
                    velocity_deviation=_clamp(float(n.get("velocity_deviation", 0.0)), -127.0, 127.0),
                    release_velocity=_clamp(float(n.get("release_velocity", 64.0)), 0.0, 127.0),
                ))
            clip.add_new_notes(tuple(specs))
        else:
            clip.set_notes(tuple(self._note_fields(n) for n in notes))
        return len(notes)

    def add_notes(self, params):
        clip = self._clip(params)
        notes = params.get("notes") or []
        if not isinstance(notes, list):
            raise CommandError("bad_param: notes")
        return {"note_count": self._add_notes_to_clip(clip, notes)}

    def get_notes(self, params):
        clip = self._clip(params)
        length = clip.length
        out = []
        if hasattr(clip, "get_notes_extended"):
            for n in clip.get_notes_extended(0, 128, 0.0, length):
                out.append({
                    "pitch": n.pitch,
                    "start_time": n.start_time,
                    "duration": n.duration,
                    "velocity": n.velocity,
                    "mute": bool(n.mute),
                    "probability": n.probability,
                    "velocity_deviation": n.velocity_deviation,
                    "release_velocity": n.release_velocity,
                })
        else:
            for pitch, start, dur, vel, mute in clip.get_notes(0.0, 0, length, 128):
                out.append({"pitch": pitch, "start_time": start, "duration": dur, "velocity": vel, "mute": bool(mute)})
        return {"length": length, "notes": out}

    def remove_notes(self, params):
        clip = self._clip(params)
        from_time = float(params.get("from_time", 0.0))
        time_span = float(params.get("time_span", clip.length))
        from_pitch = int(params.get("from_pitch", 0))
        pitch_span = int(params.get("pitch_span", 128))
        if hasattr(clip, "remove_notes_extended"):
            clip.remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
        else:
            clip.remove_notes(from_time, from_pitch, time_span, pitch_span)
        return {"removed": True}

    def write_clip(self, params):
        """Replace a slot's clip with new notes in one step (one undo entry)."""
        created = self.create_clip(params)
        slot = self.song.tracks[created["track_index"]].clip_slots[created["clip_slot_index"]]
        count = self._add_notes_to_clip(slot.clip, params.get("notes") or [])
        if params.get("name"):
            slot.clip.name = str(params["name"])
        if params.get("fire"):
            slot.fire()
        created["note_count"] = count
        return created

    # ------------------------------------------------------------------ devices

    def get_device_params(self, params):
        d = self._device(params)
        return {
            "name": d.name,
            "class_name": d.class_name,
            "parameters": [
                {
                    "index": i,
                    "name": p.name,
                    "value": p.value,
                    "min": p.min,
                    "max": p.max,
                    "is_quantized": bool(p.is_quantized),
                }
                for i, p in enumerate(d.parameters)
            ],
        }

    def set_device_param(self, params):
        d = self._device(params)
        pi = _int(params, "param_index")
        ps = d.parameters
        if pi < 0 or pi >= len(ps):
            raise CommandError("param_index_out_of_range: %d (params: %d)" % (pi, len(ps)))
        p = ps[pi]
        p.value = _clamp(_float(params, "value"), p.min, p.max)
        return {"name": p.name, "value": p.value}

    def _select_track_and_load(self, track, item):
        self.song.view.selected_track = track
        self.browser.load_item(item)

    def load_device(self, params):
        """Load a stock device by name (e.g. "Drift", "Drum Rack", "Reverb")."""
        ti, track = self._track(params)
        wanted = str(params.get("device_name") or params.get("name") or "").strip()
        if not wanted:
            raise CommandError("missing_param: device_name")
        lw = wanted.lower()
        candidates = []
        for root in DEVICE_ROOTS:
            item = self._browser_root(root)
            if item is not None:
                candidates.extend(c for c in self._children(item) if c.is_loadable)
        match = next((c for c in candidates if c.name.lower() == lw), None)
        if match is None:
            match = next((c for c in candidates if c.name.lower().startswith(lw)), None)
        if match is None:
            raise CommandError("device_not_found: %s" % wanted)
        before = len(track.devices)
        self._select_track_and_load(track, match)
        return {"loaded": True, "item_name": match.name, "track_index": ti, "num_devices_before": before}

    def _item_at_path(self, path):
        parts = [p for p in str(path or "").strip("/").split("/") if p]
        if not parts:
            raise CommandError("missing_param: path")
        current = self._browser_root(parts[0])
        if current is None:
            raise CommandError("unknown_browser_root: %s" % parts[0])
        for part in parts[1:]:
            lp = part.lower()
            nxt = next((c for c in self._children(current) if c.name.lower() == lp), None)
            if nxt is None:
                raise CommandError("path_not_found: %s (at '%s')" % (path, part))
            current = nxt
        return current

    def _loadable_item(self, params):
        """The browser item named by params path + name (or uri) inside that folder."""
        folder = self._item_at_path(params.get("path"))
        name = str(params.get("name") or "")
        uri = params.get("uri")
        children = self._children(folder)
        item = next((c for c in children if uri and getattr(c, "uri", None) == uri), None)
        if item is None:
            item = next((c for c in children if c.name == name), None)
        if item is None:
            raise CommandError("item_not_found: %s/%s" % (params.get("path"), name))
        if not item.is_loadable:
            raise CommandError("not_loadable: %s" % item.name)
        return item

    def load_item_at_path(self, params):
        """
        Load a browser item given its folder path and name (as stored in the Pulse browser
        index). Walking the path is exact and fast, unlike searching the library by URI.
        """
        ti, track = self._track(params)
        item = self._loadable_item(params)
        self._select_track_and_load(track, item)
        return {"loaded": True, "item_name": item.name, "track_index": ti}

    # ------------------------------------------------------------------ audio

    def load_audio_clip(self, params):
        """
        Put an audio file into a Session clip slot on an audio track (Live 12.0.5+,
        ClipSlot.create_audio_clip). Defaults suit one-shot vocals: warping and looping off,
        so the phrase plays once at its natural speed.
        """
        path = str(params.get("file_path") or "").strip()
        if not path:
            raise CommandError("missing_param: file_path")
        if not os.path.isabs(path) or not os.path.isfile(path):
            raise CommandError("file_not_found: %s" % path)
        ti, si, slot = self._slot(params, create_scenes=True)
        track = self.song.tracks[ti]
        if track.has_midi_input:
            raise CommandError("not_audio_track: %d" % ti)
        if getattr(track, "is_frozen", False):
            raise CommandError("track_frozen: %d" % ti)
        if not hasattr(slot, "create_audio_clip"):
            raise CommandError("unsupported: create_audio_clip needs Live 12.0.5+")
        if slot.has_clip:
            if params.get("replace", True):
                slot.delete_clip()
            else:
                raise CommandError("slot_has_clip")
        slot.create_audio_clip(path)
        clip = slot.clip
        if params.get("name"):
            clip.name = str(params["name"])
        # Warping first: Live only allows looping on warped clips.
        for attr in ("warping", "looping"):
            value = params.get(attr, False)
            if value is not None and hasattr(clip, attr):
                try:
                    setattr(clip, attr, bool(value))
                except Exception:
                    pass
        return {
            "track_index": ti,
            "clip_slot_index": si,
            "name": clip.name,
            "length": clip.length,
            "warping": getattr(clip, "warping", None),
            "looping": getattr(clip, "looping", None),
        }

    def get_track_meter(self, params):
        """Current output level of a track plus the mixer state that decides whether it's heard."""
        ti, t = self._track(params)
        s = self.song

        def level(tr):
            if not getattr(tr, "has_audio_output", True):
                return 0.0
            return max(float(tr.output_meter_left), float(tr.output_meter_right))

        return {
            "track_index": ti,
            "peak": level(t),
            "master_peak": level(s.master_track),
            "mute": bool(t.mute),
            "volume": t.mixer_device.volume.value,
            "soloed_elsewhere": any(bool(o.solo) for o in s.tracks if o is not t) and not bool(t.solo),
            "master_volume": s.master_track.mixer_device.volume.value,
            "is_playing": bool(s.is_playing),
            "playing_slot_index": t.playing_slot_index,
        }

    def delete_device(self, params):
        ti, track = self._track(params)
        di = _int(params, "device_index")
        if di < 0 or di >= len(track.devices):
            raise CommandError("device_index_out_of_range: %d" % di)
        name = track.devices[di].name
        track.delete_device(di)
        return {"deleted": name, "track_index": ti}

    def get_drum_pads(self, params):
        """Filled pads of the track's first Drum Rack: [{note, name}], or no rack."""
        ti, track = self._track(params)
        rack = next((d for d in track.devices if getattr(d, "can_have_drum_pads", False)), None)
        if rack is None:
            return {"track_index": ti, "rack": None, "pads": []}
        pads = [{"note": int(p.note), "name": p.name} for p in rack.drum_pads if len(p.chains) > 0]
        return {"track_index": ti, "rack": rack.name, "pads": pads}

    def load_item_to_drum_pad(self, params):
        """
        Load a browser item (usually a sample) onto one pad of the track's first Drum Rack.
        Live loads into the selected pad when the rack is the selected device, the same as
        double-clicking a sample in the browser with a pad selected.
        """
        ti, track = self._track(params)
        rack = next((d for d in track.devices if getattr(d, "can_have_drum_pads", False)), None)
        if rack is None:
            raise CommandError("no_drum_rack: %d" % ti)
        note = _int(params, "pad_note", 36)
        pad = next((p for p in rack.drum_pads if int(p.note) == note), None)
        if pad is None:
            raise CommandError("pad_out_of_range: %d" % note)
        item = self._loadable_item(params)
        view = self.song.view
        view.selected_track = track
        if hasattr(view, "select_device"):
            view.select_device(rack)
        rack.view.selected_drum_pad = pad
        before = len(track.devices)
        self.browser.load_item(item)
        return {
            "loaded": True,
            "item_name": item.name,
            "track_index": ti,
            "pad_note": note,
            "pad_name": pad.name,
            "pad_filled": len(pad.chains) > 0,
            # Live replacing the rack instead of filling the pad would change the device list.
            "rack_intact": len(track.devices) == before and rack in list(track.devices),
        }

    # ------------------------------------------------------------------ AbletonMCP compatibility

    def get_session_info(self, params):
        s = self.song
        return {
            "tempo": s.tempo,
            "signature_numerator": s.signature_numerator,
            "signature_denominator": s.signature_denominator,
            "track_count": len(s.tracks),
            "return_track_count": len(s.return_tracks),
            "master_track": {
                "name": "Master",
                "volume": s.master_track.mixer_device.volume.value,
                "panning": s.master_track.mixer_device.panning.value,
            },
        }

    def _item_info(self, item, path=None):
        children = self._children(item)
        info = {
            "name": item.name,
            "is_folder": bool(children),
            "is_device": bool(getattr(item, "is_device", False)),
            "is_loadable": bool(getattr(item, "is_loadable", False)),
            "uri": getattr(item, "uri", None),
        }
        if path:
            info["path"] = path
        if info["uri"]:
            self._uri_cache[info["uri"]] = item
        return info

    def get_browser_tree(self, params):
        category_type = str(params.get("category_type", "all") or "all").lower()
        available = [r for r in BROWSER_ROOTS if hasattr(self.browser, r)]
        cats = []
        for root in available:
            if category_type not in ("all", root):
                continue
            item = getattr(self.browser, root)
            info = self._item_info(item, path=root)
            info["name"] = root.replace("_", " ").title()
            info["children"] = []
            cats.append(info)
        return {"type": category_type, "categories": cats, "available_categories": available}

    def get_browser_items_at_path(self, params):
        path = str(params.get("path", "") or "").strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise CommandError("missing_param: path")
        current = self._browser_root(parts[0])
        if current is None:
            return {
                "path": path,
                "error": "Unknown or unavailable category: %s" % parts[0],
                "available_categories": [r for r in BROWSER_ROOTS if hasattr(self.browser, r)],
                "items": [],
            }
        for part in parts[1:]:
            lp = part.lower()
            nxt = next((c for c in self._children(current) if c.name.lower() == lp), None)
            if nxt is None:
                return {"path": path, "error": "Path part '%s' not found" % part, "items": []}
            current = nxt
        items = [self._item_info(c, path="%s/%s" % (path, c.name)) for c in self._children(current)]
        result = self._item_info(current)
        result["path"] = path
        result["items"] = items
        return result

    def _find_by_uri(self, uri):
        item = self._uri_cache.get(uri)
        if item is not None:
            try:
                if item.uri == uri:
                    return item
            except Exception:
                pass
        # This runs on Live's main thread, so cap the walk: device roots first (stock devices
        # and presets are shallow), then the rest, with a global node budget.
        budget = [URI_SEARCH_BUDGET]
        ordered = list(DEVICE_ROOTS) + [r for r in BROWSER_ROOTS if r not in DEVICE_ROOTS]
        for root_name in ordered:
            root = self._browser_root(root_name)
            if root is None:
                continue
            frontier = [root]
            depth = 0
            while frontier and depth < 8 and budget[0] > 0:
                nxt = []
                for it in frontier:
                    budget[0] -= 1
                    if getattr(it, "uri", None) == uri:
                        self._uri_cache[uri] = it
                        return it
                    nxt.extend(self._children(it))
                frontier = nxt
                depth += 1
        return None

    def load_browser_item(self, params):
        ti, track = self._track(params)
        uri = str(params.get("item_uri") or params.get("uri") or "").strip()
        if not uri:
            raise CommandError("missing_param: item_uri")
        item = self._find_by_uri(uri)
        if item is None:
            raise CommandError("Browser item with URI '%s' not found" % uri)
        self._select_track_and_load(track, item)
        return {"loaded": True, "item_name": item.name, "track_name": track.name, "uri": uri}
