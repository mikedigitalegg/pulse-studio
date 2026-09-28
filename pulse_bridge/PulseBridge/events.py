"""
Live API listeners -> bridge events, so clients never need to poll.

Events (name -> data):
  song.tempo        {"tempo"}
  song.is_playing   {"is_playing"}
  song.tracks       {"num_tracks", "track_names"}
  song.scenes       {"num_scenes"}
  song.key          {"root_note", "scale_name"}              (Live 12+)
  track.name        {"track_index", "name"}
  track.devices     {"track_index", "num_devices", "devices"}
  track.playing     {"track_index", "playing_slot_index", "fired_slot_index"}
  track.mute        {"track_index", "mute"}
  beat              {"beat", "bar", "beat_in_bar", "song_time"}   (while playing)
  meters            {"master": [l, r], "tracks": [level, ...]}   (~10 Hz, only to
                    connections that sent set_meters; nothing while everything is silent)
"""
from __future__ import absolute_import, print_function, unicode_literals


class EventHub(object):
    def __init__(self, song, emit, log=None):
        self._song = song
        self._emit = emit
        self._log = log or (lambda msg: None)
        self._bound = []  # (obj, prop, callback)
        self._last_beat = None
        self._meters_silent = False

    # ------------------------------------------------------------------ binding

    def _listen(self, obj, prop, cb):
        add = getattr(obj, "add_%s_listener" % prop, None)
        if add is None:
            return
        try:
            add(cb)
            self._bound.append((obj, prop, cb))
        except Exception as e:
            self._log("PulseBridge: could not listen to %s: %s" % (prop, e))

    def _unbind(self, predicate=None):
        keep = []
        for obj, prop, cb in self._bound:
            if predicate is not None and not predicate(obj):
                keep.append((obj, prop, cb))
                continue
            try:
                has = getattr(obj, "%s_has_listener" % prop)
                if has(cb):
                    getattr(obj, "remove_%s_listener" % prop)(cb)
            except Exception:
                pass
        self._bound = keep

    def start(self):
        s = self._song
        self._listen(s, "tempo", lambda: self._emit("song.tempo", {"tempo": s.tempo}))
        self._listen(s, "is_playing", self._on_is_playing)
        self._listen(s, "tracks", self._on_tracks)
        self._listen(s, "scenes", lambda: self._emit("song.scenes", {"num_scenes": len(s.scenes)}))
        if hasattr(s, "root_note"):
            self._listen(s, "root_note", self._on_key)
            self._listen(s, "scale_name", self._on_key)
        self._bind_tracks()

    def stop(self):
        self._unbind()

    def _bind_tracks(self):
        song = self._song
        self._unbind(lambda obj: obj is not song)
        for track in song.tracks:
            self._bind_track(track)

    def _bind_track(self, track):
        # Resolve the index at event time: indices shift when tracks are inserted.
        def idx():
            try:
                return list(self._song.tracks).index(track)
            except ValueError:
                return -1

        self._listen(track, "name", lambda: self._emit("track.name", {"track_index": idx(), "name": track.name}))
        self._listen(track, "devices", lambda: self._emit("track.devices", {
            "track_index": idx(),
            "num_devices": len(track.devices),
            "devices": [d.name for d in track.devices],
        }))

        self._listen(track, "mute", lambda: self._emit("track.mute", {"track_index": idx(), "mute": bool(track.mute)}))

        def on_playing():
            self._emit("track.playing", {
                "track_index": idx(),
                "playing_slot_index": track.playing_slot_index,
                "fired_slot_index": track.fired_slot_index,
            })

        if getattr(track, "playing_slot_index", None) is not None:
            self._listen(track, "playing_slot_index", on_playing)
            self._listen(track, "fired_slot_index", on_playing)

    # ------------------------------------------------------------------ callbacks

    def _on_is_playing(self):
        self._last_beat = None
        self._emit("song.is_playing", {"is_playing": bool(self._song.is_playing)})

    def _on_tracks(self):
        s = self._song
        self._bind_tracks()
        self._emit("song.tracks", {"num_tracks": len(s.tracks), "track_names": [t.name for t in s.tracks]})

    def _on_key(self):
        s = self._song
        self._emit("song.key", {"root_note": s.root_note, "scale_name": getattr(s, "scale_name", None)})

    def tick(self):
        """Called from update_display (~10 Hz). Emits one event per beat while playing."""
        s = self._song
        if not s.is_playing:
            return
        t = s.current_song_time
        beat = int(t)
        if beat == self._last_beat:
            return
        self._last_beat = beat
        num = max(1, int(s.signature_numerator))
        self._emit("beat", {"beat": beat, "bar": beat // num, "beat_in_bar": beat % num, "song_time": t})

    @staticmethod
    def _meter(track):
        # MIDI tracks without an instrument have no audio output to meter.
        try:
            if not track.has_audio_output:
                return 0.0, 0.0
            return float(track.output_meter_left), float(track.output_meter_right)
        except Exception:
            return 0.0, 0.0

    def meters(self):
        """Output levels (0..1, as Live's meters display them) or None when all silent twice running."""
        s = self._song
        ml, mr = self._meter(s.master_track)
        tracks = [round(max(self._meter(t)), 3) for t in s.tracks]
        silent = ml == 0.0 and mr == 0.0 and not any(tracks)
        if silent and self._meters_silent:
            return None  # one all-zero frame lets clients drop to zero, then stay quiet
        self._meters_silent = silent
        return {"master": [round(ml, 3), round(mr, 3)], "tracks": tracks}
