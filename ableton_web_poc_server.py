import os
import json
import urllib.request
import urllib.error
import threading
import time
import random

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

try:
    from ableton_track_creator import AbletonController
except ModuleNotFoundError as e:
    raise RuntimeError(
        "Missing dependency in the current Python environment. "
        "Run: python -m pip install python-osc\n"
        "Then verify: python -c \"import pythonosc; print('pythonosc ok')\""
    ) from e


app = FastAPI(title="Ableton Web POC")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=APP_DIR), name="static")

VOLCA_DRUM_TRACK_INDEX = 0
PERC_TRACK_INDEX = 2
STABS_TRACK_INDEX = 3
FX_TRACK_INDEX = 4
VOLCA_STYLE_TO_CLIP_SLOT = {
    "techno": 0,
    "house": 1,
    "dnb": 2,
    "breakbeat": 3,
    "garage": 4,
    "tribal": 5,
    "acid_techno": 6,
    "psytrance": 7,
}

BASS_STYLE_DEFAULTS = {
    "techno": {"root": 43, "octave": 0},
    "house": {"root": 43, "octave": 0},
    "dnb": {"root": 41, "octave": 0},
    "breakbeat": {"root": 43, "octave": 0},
    "garage": {"root": 43, "octave": 0},
    "tribal": {"root": 43, "octave": 0},
    "acid_techno": {"root": 45, "octave": 0},
    "psytrance": {"root": 41, "octave": 0},
}

BASS_STYLE_PROFILE = {
    "techno": {
        "scale": "minor",
        "octave_range": [2, 3],
        "density": "medium",
        "rhythm": "straight_16",
        "note_pool": "root_fifth_flat7_octave",
        "explain": "Techno basslines are usually hypnotic and repetitive. Straight 16th timing with a small note pool keeps it driving without sounding 'busy'.",
    },
    "house": {
        "scale": "minor",
        "octave_range": [2, 3],
        "density": "low",
        "rhythm": "offbeat_8",
        "note_pool": "root_fifth_octave",
        "explain": "House bass often breathes around the kick. Offbeat notes and simpler density keep it groovy and danceable.",
    },
    "dnb": {
        "scale": "minor",
        "octave_range": [1, 2],
        "density": "high",
        "rhythm": "syncopated_16",
        "note_pool": "root_minor3_fifth_flat7_octave",
        "explain": "DnB bass can be more syncopated and denser. Lower octave range helps weight under fast drums.",
    },
    "breakbeat": {
        "scale": "minor",
        "octave_range": [2, 3],
        "density": "medium",
        "rhythm": "syncopated_16",
        "note_pool": "root_fifth_flat7_octave",
        "explain": "Breakbeats often pair well with syncopation in the bass. Moderate density keeps it punchy without masking the breaks.",
    },
    "garage": {
        "scale": "minor",
        "octave_range": [2, 3],
        "density": "medium",
        "rhythm": "swingy_16",
        "note_pool": "root_fifth_octave",
        "explain": "Garage/2-step basslines often feel bouncy. A swingy 16th feel with a simple note pool keeps it rolling.",
    },
    "tribal": {
        "scale": "minor",
        "octave_range": [2, 3],
        "density": "low",
        "rhythm": "straight_8",
        "note_pool": "root_fifth",
        "explain": "Tribal/afro-inspired grooves often benefit from space. Lower density and simpler rhythms leave room for percussion.",
    },
    "acid_techno": {
        "scale": "minor",
        "octave_range": [2, 3],
        "density": "medium",
        "rhythm": "syncopated_16",
        "note_pool": "root_minor2_minor3_fifth_flat7_octave",
        "explain": "Acid techno basslines often use a small, edgy note pool with syncopation. The feel comes from accents, short notes, and filter movement (cutoff/resonance).",
    },
    "psytrance": {
        "scale": "minor",
        "octave_range": [1, 2],
        "density": "high",
        "rhythm": "steady_16",
        "note_pool": "root_octave_fifth",
        "explain": "Psytrance bass is typically tight, consistent, and driving (often 16ths). It sits low and leaves space for the kick by careful timing/short notes.",
    },
}

STYLE_CONFIG = {
    "techno": {"tempo": 140.0, "clip_slot": 0, "kit": "909 Core Kit"},
    "house": {"tempo": 124.0, "clip_slot": 1, "kit": "909 Core Kit"},
    "dnb": {"tempo": 174.0, "clip_slot": 2, "kit": "909 Core Kit"},
    "breakbeat": {"tempo": 132.0, "clip_slot": 3, "kit": "909 Core Kit"},
    "garage": {"tempo": 132.0, "clip_slot": 4, "kit": "909 Core Kit"},
    "tribal": {"tempo": 138.0, "clip_slot": 5, "kit": "909 Core Kit"},
    "acid_techno": {"tempo": 145.0, "clip_slot": 6, "kit": "909 Core Kit"},
    "psytrance": {"tempo": 145.0, "clip_slot": 7, "kit": "909 Core Kit"},
}

DRUM_KIT_TEST_NAME = "909 Core Kit"
BASS_DEVICE_TEST_NAME = "Basic 303 Bass"

STYLE_RECOMMENDATIONS = {
    "techno": {"drums": "909/808 style kit (tight kick, crisp hats)", "bass": "acid/mono synth (303-ish) or simple sub"},
    "house": {"drums": "house kit (soft clap, open hat offbeat)", "bass": "warm mono bass (simple sine/saw)"},
    "dnb": {"drums": "break kit + tight kick layering", "bass": "sub + reese/wobble (depending on subgenre)"},
    "breakbeat": {"drums": "breaks kit (Amen-ish) + punchy kick", "bass": "simple mono bass with syncopation"},
    "garage": {"drums": "2-step kit (skippy hats, snappy clap)", "bass": "bouncy mono bass"},
    "tribal": {"drums": "tribal percussion + steady kick", "bass": "simple root/fifth pulse"},
    "acid_techno": {"drums": "909 kit + extra percussion, tight hats", "bass": "303-style mono synth with filter movement"},
    "psytrance": {"drums": "tight kick + crisp hats, minimal clutter", "bass": "tight low bass (often steady 16ths), short notes"},
}


class _OSCState:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_by_address: dict[str, tuple[float, tuple]] = {}

    def set(self, address: str, args: tuple):
        with self.lock:
            self.last_by_address[address] = (time.time(), args)

    def get(self, address: str):
        with self.lock:
            return self.last_by_address.get(address)


OSC_STATE = _OSCState()
OSC_LISTENER_ERROR: str | None = None


def _osc_default_handler(address, *args):
    OSC_STATE.set(address, args)


def _start_osc_listener():
    global OSC_LISTENER_ERROR
    try:
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(_osc_default_handler)
        server = ThreadingOSCUDPServer(("127.0.0.1", 11001), dispatcher)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
    except Exception as e:
        OSC_LISTENER_ERROR = str(e)


_start_osc_listener()


class _GenCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.drums: dict[tuple[int, int], dict] = {}
        self.bass: dict[tuple[int, int], dict] = {}
        self.perc: dict[tuple[int, int], dict] = {}
        self.stabs: dict[tuple[int, int], dict] = {}
        self.fx: dict[tuple[int, int], dict] = {}

    def set_drums(self, track_index: int, clip_slot_index: int, pattern: dict):
        with self.lock:
            self.drums[(track_index, clip_slot_index)] = pattern

    def get_drums(self, track_index: int, clip_slot_index: int):
        with self.lock:
            return self.drums.get((track_index, clip_slot_index))

    def set_bass(self, track_index: int, clip_slot_index: int, pattern: dict):
        with self.lock:
            self.bass[(track_index, clip_slot_index)] = pattern

    def get_bass(self, track_index: int, clip_slot_index: int):
        with self.lock:
            return self.bass.get((track_index, clip_slot_index))

    def set_perc(self, track_index: int, clip_slot_index: int, pattern: dict):
        with self.lock:
            self.perc[(track_index, clip_slot_index)] = pattern

    def get_perc(self, track_index: int, clip_slot_index: int):
        with self.lock:
            return self.perc.get((track_index, clip_slot_index))

    def set_stabs(self, track_index: int, clip_slot_index: int, pattern: dict):
        with self.lock:
            self.stabs[(track_index, clip_slot_index)] = pattern

    def get_stabs(self, track_index: int, clip_slot_index: int):
        with self.lock:
            return self.stabs.get((track_index, clip_slot_index))

    def set_fx(self, track_index: int, clip_slot_index: int, pattern: dict):
        with self.lock:
            self.fx[(track_index, clip_slot_index)] = pattern

    def get_fx(self, track_index: int, clip_slot_index: int):
        with self.lock:
            return self.fx.get((track_index, clip_slot_index))


GEN_CACHE = _GenCache()

LANE_TO_MIDI_NOTE = {
    "kick": 36,
    "snare": 38,
    "clap": 39,
    "ch": 42,
    "oh": 46,
    "perc1": 50,
    "perc2": 45,
    "stab": 60,
    "fx": 72,
}

MACRO_TRACK_INDEX = 0
MACRO_DEVICE_INDEX = 0
MACRO_PARAM_BY_MACRO = {
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
    8: 8,
}

BASS_RACK_TRACK_INDEX = 1
BASS_RACK_DEVICE_INDEX = 0
DRUMS_FX_RACK_TRACK_INDEX = 0
DRUMS_FX_RACK_DEVICE_INDEX = 1

BASS_KNOB_TO_MACRO = {
    "CUTOFF": 1,
    "RESONANCE": 2,
    "ENV_AMOUNT": 3,
    "DECAY": 4,
    "ACCENT": 5,
    "DRIVE": 6,
    "LOW_EQ": 7,
    "SPACE": 8,
}

DRUMS_KNOB_TO_MACRO = {
    "FILTER": 1,
    "RESONANCE": 2,
    "DRIVE": 3,
    "TRANSIENT": 4,
    "LOW_CUT": 5,
    "PRESENCE": 6,
    "SPACE": 7,
    "HAT_BRIGHT": 8,
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


ctrl = AbletonController()


class TempoRequest(BaseModel):
    bpm: float


class FireClipRequest(BaseModel):
    track_index: int
    clip_slot_index: int


class FireSceneRequest(BaseModel):
    scene_index: int


class LaunchStyleRequest(BaseModel):
    style: str


class LoadStyleRequest(BaseModel):
    style: str
    launch: bool = True
    load_kit: bool = True


class LoadDeviceRequest(BaseModel):
    track_index: int
    device_name: str


class SetDeviceParamRequest(BaseModel):
    track_index: int
    device_index: int
    param_index: int
    value: float


class SetMacroRequest(BaseModel):
    macro: int
    value: float


class SetNamedKnobRequest(BaseModel):
    name: str
    value: float


class GenerateAIPatternRequest(BaseModel):
    style: str
    bars: int = 1
    track_index: int = VOLCA_DRUM_TRACK_INDEX
    clip_slot_index: int = 0
    prompt: str | None = None
    temperature: float = 0.7


class GenerateAIPercPatternRequest(BaseModel):
    style: str
    bars: int = 1
    track_index: int = PERC_TRACK_INDEX
    clip_slot_index: int = 0
    prompt: str | None = None
    temperature: float = 0.7


class GenerateAIStabsPatternRequest(BaseModel):
    style: str
    bars: int = 1
    track_index: int = STABS_TRACK_INDEX
    clip_slot_index: int = 0
    prompt: str | None = None
    temperature: float = 0.7


class GenerateAIFxPatternRequest(BaseModel):
    style: str
    bars: int = 1
    track_index: int = FX_TRACK_INDEX
    clip_slot_index: int = 0
    prompt: str | None = None
    temperature: float = 0.7


class GenerateAIPairRequest(BaseModel):
    style: str
    bars: int = 1
    drum_track_index: int = 0
    drum_clip_slot_index: int = 0
    bass_track_index: int = 1
    bass_clip_slot_index: int = 0
    perc_track_index: int = PERC_TRACK_INDEX
    perc_clip_slot_index: int = 0
    stabs_track_index: int = STABS_TRACK_INDEX
    stabs_clip_slot_index: int = 0
    fx_track_index: int = FX_TRACK_INDEX
    fx_clip_slot_index: int = 0
    include_drums: bool = True
    include_bass: bool = True
    include_perc: bool = True
    include_stabs: bool = True
    include_fx: bool = False
    bass_root_midi: int = 43
    prompt: str | None = None
    temperature: float = 0.7


class TweakBassRequest(BaseModel):
    track_index: int
    clip_slot_index: int
    transpose: int = 0
    velocity_scale: float = 1.0


class TweakDrumsRequest(BaseModel):
    track_index: int
    clip_slot_index: int
    velocity_scale: float = 1.0


class GenerateAIBasslineRequest(BaseModel):
    style: str
    bars: int = 1
    track_index: int = 1
    clip_slot_index: int = 0
    root_midi: int = 43
    prompt: str | None = None
    temperature: float = 0.7


class GenerateFullTrackRequest(BaseModel):
    style: str
    start_slot_index: int = 0
    bars_per_scene: int = 1
    temperature: float = 0.7


class SceneExportSpec(BaseModel):
    name: str | None = None
    slot: int
    include: dict | None = None


class ExportFullTrackRequest(BaseModel):
    style: str | None = None
    bars_per_scene: int | None = None
    start_slot_index: int | None = None
    scenes: list[SceneExportSpec]
    suggested_arrangement: list[dict] | None = None


class ApplyFullTrackExportRequest(BaseModel):
    export: dict


class ApplyPairExportRequest(BaseModel):
    export: dict


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse(url="/pulse_studio.html")


@app.get("/ableton_web_poc.html")
def poc_ui():
    return FileResponse(
        os.path.join(APP_DIR, "ableton_web_poc.html"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/volca_drum_v2.html")
def volca_drum_v2_ui():
    return FileResponse(
        os.path.join(APP_DIR, "volca_drum_v2.html"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/pulse_studio.html")
def pulse_studio_ui():
    return FileResponse(
        os.path.join(APP_DIR, "pulse_studio.html"),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/transport/play")
def play():
    ctrl.play()
    return {"ok": True}


@app.post("/transport/stop")
def stop():
    ctrl.stop()
    return {"ok": True}


@app.post("/transport/tempo")
def set_tempo(req: TempoRequest):
    ctrl.set_tempo(req.bpm)
    return {"ok": True, "bpm": req.bpm}


@app.get("/transport/status")
def transport_status():
    # Best-effort queries; values depend on what AbletonOSC exposes.
    playing = _query_with_timeout("/live/song/get/is_playing", [])
    time_now = _query_with_timeout("/live/song/get/current_song_time", [])
    tempo = _query_with_timeout("/live/song/get/tempo", [])
    return {
        "ok": True,
        "playing": playing,
        "song_time": time_now,
        "tempo": tempo,
        "listener": {"ok": OSC_LISTENER_ERROR is None, "error": OSC_LISTENER_ERROR},
    }


@app.post("/clips/fire")
def fire_clip(req: FireClipRequest):
    ctrl.fire_clip(req.track_index, req.clip_slot_index)
    return {"ok": True, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/scenes/fire")
def fire_scene(req: FireSceneRequest):
    ctrl.fire_scene(req.scene_index)
    return {"ok": True, "scene_index": req.scene_index}


@app.post("/patterns/launch")
def launch_style(req: LaunchStyleRequest):
    style = (req.style or "").strip().lower()
    if style not in VOLCA_STYLE_TO_CLIP_SLOT:
        return {"ok": False, "error": "unknown_style", "available": sorted(VOLCA_STYLE_TO_CLIP_SLOT.keys())}
    slot = VOLCA_STYLE_TO_CLIP_SLOT[style]
    ctrl.fire_clip(VOLCA_DRUM_TRACK_INDEX, slot)
    return {"ok": True, "style": style, "track_index": VOLCA_DRUM_TRACK_INDEX, "clip_slot_index": slot}


@app.post("/styles/load")
def load_style(req: LoadStyleRequest):
    style = (req.style or "").strip().lower()
    if style not in STYLE_CONFIG:
        return {"ok": False, "error": "unknown_style", "available": sorted(STYLE_CONFIG.keys())}

    cfg = STYLE_CONFIG[style]
    tempo = float(cfg["tempo"])
    clip_slot = int(cfg["clip_slot"])
    kit = str(cfg["kit"])

    ctrl.set_tempo(tempo)

    if req.load_kit:
        ctrl.load_device(VOLCA_DRUM_TRACK_INDEX, kit)

    if req.launch:
        ctrl.fire_clip(VOLCA_DRUM_TRACK_INDEX, clip_slot)

    return {
        "ok": True,
        "style": style,
        "tempo": tempo,
        "track_index": VOLCA_DRUM_TRACK_INDEX,
        "clip_slot_index": clip_slot,
        "kit": kit,
        "launch": req.launch,
        "load_kit": req.load_kit,
    }


@app.post("/pair/generate_ai")
def generate_ai_pair(req: GenerateAIPairRequest):
    style = (req.style or "").strip().lower()
    drum_lanes = ["kick", "snare", "clap", "ch", "oh", "perc1", "perc2"]
    root = int(req.bass_root_midi)
    if style in BASS_STYLE_DEFAULTS:
        root = int(BASS_STYLE_DEFAULTS[style]["root"])

    obj, meta = _openai_generate_pair(style, req.bars, drum_lanes, root, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    if not isinstance(obj, dict) or "drums" not in obj or "bass" not in obj:
        return {"ok": False, "error": "invalid_pair_shape", "raw": obj}

    drums_valid, dmeta = _validate_pattern(obj["drums"], req.bars)
    if not dmeta.get("ok"):
        return {"ok": False, "error": "invalid_drums", "detail": dmeta, "raw": obj}

    bass_valid, bmeta = _validate_bassline(obj["bass"], req.bars)
    if not bmeta.get("ok"):
        return {"ok": False, "error": "invalid_bass", "detail": bmeta, "raw": obj}

    bass_styled, sp = _apply_style_to_bassline(bass_valid, style)

    perc_valid = None
    if bool(req.include_perc):
        perc_pattern, pmeta = _openai_generate_perc_pattern(style, req.bars, req.prompt, req.temperature)
        if not pmeta.get("ok"):
            return pmeta

        perc_valid, pvmeta = _validate_pattern(perc_pattern, req.bars)
        if not pvmeta.get("ok"):
            return {"ok": False, "error": "invalid_perc", "detail": pvmeta, "raw": perc_pattern}

    stabs_valid = None
    if bool(req.include_stabs):
        stabs_pattern, smeta = _openai_generate_stabs_pattern(style, req.bars, req.prompt, req.temperature)
        if not smeta.get("ok"):
            return smeta

        stabs_valid, svmeta = _validate_pattern(stabs_pattern, req.bars)
        if not svmeta.get("ok"):
            return {"ok": False, "error": "invalid_stabs", "detail": svmeta, "raw": stabs_pattern}

    fx_valid = None
    if bool(req.include_fx):
        fx_pattern, fxmeta = _openai_generate_fx_pattern(style, req.bars, req.prompt, req.temperature)
        if not fxmeta.get("ok"):
            return fxmeta

        fx_valid, fxvmeta = _validate_pattern(fx_pattern, req.bars)
        if not fxvmeta.get("ok"):
            return {"ok": False, "error": "invalid_fx", "detail": fxvmeta, "raw": fx_pattern}

    if bool(req.include_drums):
        _write_pattern_to_ableton(int(req.drum_track_index), int(req.drum_clip_slot_index), drums_valid)
        GEN_CACHE.set_drums(int(req.drum_track_index), int(req.drum_clip_slot_index), drums_valid)

    if bool(req.include_bass):
        _write_bassline_to_ableton(int(req.bass_track_index), int(req.bass_clip_slot_index), bass_styled)
        GEN_CACHE.set_bass(int(req.bass_track_index), int(req.bass_clip_slot_index), bass_styled)

    if bool(req.include_perc) and perc_valid is not None:
        _write_pattern_to_ableton(int(req.perc_track_index), int(req.perc_clip_slot_index), perc_valid)
        GEN_CACHE.set_perc(int(req.perc_track_index), int(req.perc_clip_slot_index), perc_valid)

    if bool(req.include_stabs) and stabs_valid is not None:
        _write_pattern_to_ableton(int(req.stabs_track_index), int(req.stabs_clip_slot_index), stabs_valid)
        GEN_CACHE.set_stabs(int(req.stabs_track_index), int(req.stabs_clip_slot_index), stabs_valid)

    if bool(req.include_fx) and fx_valid is not None:
        _write_pattern_to_ableton(int(req.fx_track_index), int(req.fx_clip_slot_index), fx_valid)
        GEN_CACHE.set_fx(int(req.fx_track_index), int(req.fx_clip_slot_index), fx_valid)

    return {
        "ok": True,
        "style": style,
        "drums": {"track_index": req.drum_track_index, "clip_slot_index": req.drum_clip_slot_index, "pattern": drums_valid, "included": bool(req.include_drums)},
        "bass": {"track_index": req.bass_track_index, "clip_slot_index": req.bass_clip_slot_index, "bassline": bass_styled, "included": bool(req.include_bass)},
        "perc": {"track_index": req.perc_track_index, "clip_slot_index": req.perc_clip_slot_index, "pattern": perc_valid, "included": bool(req.include_perc)},
        "stabs": {"track_index": req.stabs_track_index, "clip_slot_index": req.stabs_clip_slot_index, "pattern": stabs_valid, "included": bool(req.include_stabs)},
        "fx": {"track_index": req.fx_track_index, "clip_slot_index": req.fx_clip_slot_index, "pattern": fx_valid, "included": bool(req.include_fx)},
        "applied": {**(obj.get("applied") or {}), "post": sp},
    }


@app.post("/pair/apply_export")
def apply_pair_export(req: ApplyPairExportRequest):
    export = req.export
    if not isinstance(export, dict) or export.get("format") != "ableton_pair_export":
        return {"ok": False, "error": "invalid_export_format"}

    payload = export.get("payload")
    if not isinstance(payload, dict) or not payload.get("ok"):
        return {"ok": False, "error": "invalid_payload"}

    applied = {}

    drums = payload.get("drums")
    if isinstance(drums, dict) and bool(drums.get("included")) and isinstance(drums.get("pattern"), dict):
        track = int(drums.get("track_index", 0))
        slot = int(drums.get("clip_slot_index", 0))
        bars = int(drums["pattern"].get("bars", 1) or 1)
        valid, meta = _validate_pattern(drums["pattern"], bars)
        if not meta.get("ok"):
            return {"ok": False, "error": "invalid_drums", "detail": meta}
        _write_pattern_to_ableton(track, slot, valid)
        GEN_CACHE.set_drums(track, slot, valid)
        applied["drums"] = {"track_index": track, "clip_slot_index": slot}

    bass = payload.get("bass")
    if isinstance(bass, dict) and bool(bass.get("included")) and isinstance(bass.get("bassline"), dict):
        track = int(bass.get("track_index", 1))
        slot = int(bass.get("clip_slot_index", 0))
        bars = int(bass["bassline"].get("bars", 1) or 1)
        valid, meta = _validate_bassline(bass["bassline"], bars)
        if not meta.get("ok"):
            return {"ok": False, "error": "invalid_bass", "detail": meta}
        _write_bassline_to_ableton(track, slot, valid)
        GEN_CACHE.set_bass(track, slot, valid)
        applied["bass"] = {"track_index": track, "clip_slot_index": slot}

    perc = payload.get("perc")
    if isinstance(perc, dict) and bool(perc.get("included")) and isinstance(perc.get("pattern"), dict):
        track = int(perc.get("track_index", PERC_TRACK_INDEX))
        slot = int(perc.get("clip_slot_index", 0))
        bars = int(perc["pattern"].get("bars", 1) or 1)
        valid, meta = _validate_pattern(perc["pattern"], bars)
        if not meta.get("ok"):
            return {"ok": False, "error": "invalid_perc", "detail": meta}
        _write_pattern_to_ableton(track, slot, valid)
        GEN_CACHE.set_perc(track, slot, valid)
        applied["perc"] = {"track_index": track, "clip_slot_index": slot}

    stabs = payload.get("stabs")
    if isinstance(stabs, dict) and bool(stabs.get("included")) and isinstance(stabs.get("pattern"), dict):
        track = int(stabs.get("track_index", STABS_TRACK_INDEX))
        slot = int(stabs.get("clip_slot_index", 0))
        bars = int(stabs["pattern"].get("bars", 1) or 1)
        valid, meta = _validate_pattern(stabs["pattern"], bars)
        if not meta.get("ok"):
            return {"ok": False, "error": "invalid_stabs", "detail": meta}
        _write_pattern_to_ableton(track, slot, valid)
        GEN_CACHE.set_stabs(track, slot, valid)
        applied["stabs"] = {"track_index": track, "clip_slot_index": slot}

    fx = payload.get("fx")
    if isinstance(fx, dict) and bool(fx.get("included")) and isinstance(fx.get("pattern"), dict):
        track = int(fx.get("track_index", FX_TRACK_INDEX))
        slot = int(fx.get("clip_slot_index", 0))
        bars = int(fx["pattern"].get("bars", 1) or 1)
        valid, meta = _validate_pattern(fx["pattern"], bars)
        if not meta.get("ok"):
            return {"ok": False, "error": "invalid_fx", "detail": meta}
        _write_pattern_to_ableton(track, slot, valid)
        GEN_CACHE.set_fx(track, slot, valid)
        applied["fx"] = {"track_index": track, "clip_slot_index": slot}

    return {"ok": True, "applied": applied}


@app.post("/track/generate_full")
def generate_full_track(req: GenerateFullTrackRequest):
    style = (req.style or "").strip().lower()
    if not style:
        return {"ok": False, "error": "missing_style"}

    bars = int(req.bars_per_scene)
    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    start_slot = int(req.start_slot_index)
    if start_slot < 0:
        start_slot = 0

    # Set tempo to match style defaults if available.
    if style in STYLE_CONFIG:
        try:
            ctrl.set_tempo(float(STYLE_CONFIG[style]["tempo"]))
        except Exception:
            pass

    # A small number of unique scenes + suggested repeats gives a "full track" feel
    # without exploding OpenAI calls.
    scenes = [
        {"name": "Intro", "slot": start_slot + 0, "prompt": "Intro: sparse drums, no bass, light hats, no stabs, no fx. Minimal energy.", "include": {"drums": True, "bass": False, "perc": True, "stabs": False, "fx": False}},
        {"name": "Main A", "slot": start_slot + 1, "prompt": "Main groove A: full drums + bass + perc. Add simple stabs sparsely. No fx.", "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": False}},
        {"name": "Main B", "slot": start_slot + 2, "prompt": "Main groove B: variation of the main groove. Change hats/perc and add a different stab rhythm. No fx.", "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": False}},
        {"name": "Break", "slot": start_slot + 3, "prompt": "Break: remove kick, reduce drums to clap/snare/hat texture, no bass, airy perc, very sparse stabs, add 1 fx swoosh near end.", "include": {"drums": True, "bass": False, "perc": True, "stabs": True, "fx": True}},
        {"name": "Drop", "slot": start_slot + 4, "prompt": "Drop: return full energy. Full drums+bass+perc+stabs. Add sparse fx accents near bar end.", "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": True}},
        {"name": "Outro", "slot": start_slot + 5, "prompt": "Outro: strip elements down. Keep kick and light hats, minimal bass, no stabs, no fx.", "include": {"drums": True, "bass": True, "perc": True, "stabs": False, "fx": False}},
    ]

    out_scenes = []
    for sc in scenes:
        slot = int(sc["slot"])
        inc = sc["include"]
        pair_req = GenerateAIPairRequest(
            style=style,
            bars=bars,
            drum_track_index=0,
            drum_clip_slot_index=slot,
            bass_track_index=1,
            bass_clip_slot_index=slot,
            perc_track_index=2,
            perc_clip_slot_index=slot,
            stabs_track_index=3,
            stabs_clip_slot_index=slot,
            fx_track_index=4,
            fx_clip_slot_index=slot,
            include_drums=bool(inc.get("drums")),
            include_bass=bool(inc.get("bass")),
            include_perc=bool(inc.get("perc")),
            include_stabs=bool(inc.get("stabs")),
            include_fx=bool(inc.get("fx")),
            prompt=str(sc.get("prompt") or ""),
            temperature=float(req.temperature),
        )

        resp = generate_ai_pair(pair_req)
        if not isinstance(resp, dict) or not resp.get("ok"):
            return {"ok": False, "error": "scene_generation_failed", "scene": sc, "detail": resp}

        out_scenes.append({
            "name": sc["name"],
            "slot": slot,
            "bars": bars,
            "tracks": {
                "drums": {"track": 0, "slot": slot, "included": bool(inc.get("drums"))},
                "bass": {"track": 1, "slot": slot, "included": bool(inc.get("bass"))},
                "perc": {"track": 2, "slot": slot, "included": bool(inc.get("perc"))},
                "stabs": {"track": 3, "slot": slot, "included": bool(inc.get("stabs"))},
                "fx": {"track": 4, "slot": slot, "included": bool(inc.get("fx"))},
            },
            "prompt": sc.get("prompt"),
        })

    arrangement = [
        {"scene": "Intro", "repeats": 2},
        {"scene": "Main A", "repeats": 4},
        {"scene": "Main B", "repeats": 4},
        {"scene": "Break", "repeats": 2},
        {"scene": "Drop", "repeats": 4},
        {"scene": "Outro", "repeats": 2},
    ]

    return {
        "ok": True,
        "style": style,
        "bars_per_scene": bars,
        "start_slot_index": start_slot,
        "scene_count": len(out_scenes),
        "scenes": out_scenes,
        "suggested_arrangement": arrangement,
        "note": "These are scene slots. Launch scenes in this order, repeating as suggested, to perform a full track. You can also swap Main A/Main B live for variation.",
    }


def _export_scene_from_cache(style: str | None, bars_per_scene: int | None, scene: SceneExportSpec):
    slot = int(scene.slot)
    inc = scene.include or {}
    include_drums = bool(inc.get("drums", True))
    include_bass = bool(inc.get("bass", True))
    include_perc = bool(inc.get("perc", True))
    include_stabs = bool(inc.get("stabs", True))
    include_fx = bool(inc.get("fx", True))

    drums = GEN_CACHE.get_drums(0, slot) if include_drums else None
    bass = GEN_CACHE.get_bass(1, slot) if include_bass else None
    perc = GEN_CACHE.get_perc(2, slot) if include_perc else None
    stabs = GEN_CACHE.get_stabs(3, slot) if include_stabs else None
    fx = GEN_CACHE.get_fx(4, slot) if include_fx else None

    # bars_per_scene is optional; when present, it can be used by the client for display.
    return {
        "name": scene.name,
        "slot": slot,
        "style": style,
        "bars": bars_per_scene,
        "include": {
            "drums": include_drums,
            "bass": include_bass,
            "perc": include_perc,
            "stabs": include_stabs,
            "fx": include_fx,
        },
        "data": {
            "drums": drums,
            "bass": bass,
            "perc": perc,
            "stabs": stabs,
            "fx": fx,
        },
    }


@app.post("/track/export_full")
def export_full_track(req: ExportFullTrackRequest):
    style = (req.style or "").strip().lower() or None
    bars = int(req.bars_per_scene) if req.bars_per_scene is not None else None

    out_scenes = []
    missing = []
    for sc in req.scenes:
        exported = _export_scene_from_cache(style, bars, sc)
        slot = int(exported["slot"])
        data = exported.get("data") or {}
        for k in ["drums", "bass", "perc", "stabs", "fx"]:
            if exported.get("include", {}).get(k) and data.get(k) is None:
                missing.append({"slot": slot, "part": k})
        out_scenes.append(exported)

    return {
        "ok": True,
        "export": {
            "format": "ableton_full_track_export",
            "version": 1,
            "style": style,
            "bars_per_scene": bars,
            "start_slot_index": req.start_slot_index,
            "scene_count": len(out_scenes),
            "scenes": out_scenes,
            "suggested_arrangement": req.suggested_arrangement or [],
        },
        "missing_from_cache": missing,
    }


@app.post("/track/apply_export")
def apply_full_track_export(req: ApplyFullTrackExportRequest):
    export = req.export
    if not isinstance(export, dict) or export.get("format") != "ableton_full_track_export":
        return {"ok": False, "error": "invalid_export_format"}

    scenes = export.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {"ok": False, "error": "missing_scenes"}

    applied = []
    for sc in scenes:
        if not isinstance(sc, dict):
            continue

        slot = int(sc.get("slot", -1))
        if slot < 0:
            continue

        inc = sc.get("include") or {}
        data = sc.get("data") or {}

        if bool(inc.get("drums")) and isinstance(data.get("drums"), dict):
            drums_valid, dmeta = _validate_pattern(data["drums"], int(data["drums"].get("bars", 1) or 1))
            if not dmeta.get("ok"):
                return {"ok": False, "error": "invalid_drums", "scene_slot": slot, "detail": dmeta}
            _write_pattern_to_ableton(0, slot, drums_valid)
            GEN_CACHE.set_drums(0, slot, drums_valid)

        if bool(inc.get("bass")) and isinstance(data.get("bass"), dict):
            bass_valid, bmeta = _validate_bassline(data["bass"], int(data["bass"].get("bars", 1) or 1))
            if not bmeta.get("ok"):
                return {"ok": False, "error": "invalid_bass", "scene_slot": slot, "detail": bmeta}
            _write_bassline_to_ableton(1, slot, bass_valid)
            GEN_CACHE.set_bass(1, slot, bass_valid)

        if bool(inc.get("perc")) and isinstance(data.get("perc"), dict):
            perc_valid, pmeta = _validate_pattern(data["perc"], int(data["perc"].get("bars", 1) or 1))
            if not pmeta.get("ok"):
                return {"ok": False, "error": "invalid_perc", "scene_slot": slot, "detail": pmeta}
            _write_pattern_to_ableton(2, slot, perc_valid)
            GEN_CACHE.set_perc(2, slot, perc_valid)

        if bool(inc.get("stabs")) and isinstance(data.get("stabs"), dict):
            stabs_valid, smeta = _validate_pattern(data["stabs"], int(data["stabs"].get("bars", 1) or 1))
            if not smeta.get("ok"):
                return {"ok": False, "error": "invalid_stabs", "scene_slot": slot, "detail": smeta}
            _write_pattern_to_ableton(3, slot, stabs_valid)
            GEN_CACHE.set_stabs(3, slot, stabs_valid)

        if bool(inc.get("fx")) and isinstance(data.get("fx"), dict):
            fx_valid, fxmeta = _validate_pattern(data["fx"], int(data["fx"].get("bars", 1) or 1))
            if not fxmeta.get("ok"):
                return {"ok": False, "error": "invalid_fx", "scene_slot": slot, "detail": fxmeta}
            _write_pattern_to_ableton(4, slot, fx_valid)
            GEN_CACHE.set_fx(4, slot, fx_valid)

        applied.append({"slot": slot, "name": sc.get("name")})

    return {"ok": True, "applied_scene_count": len(applied), "applied": applied}


@app.post("/bass/tweak")
def tweak_bass(req: TweakBassRequest):
    base = GEN_CACHE.get_bass(int(req.track_index), int(req.clip_slot_index))
    if base is None:
        return {"ok": False, "error": "no_cached_bass", "hint": "Generate a bassline first."}
    tweaked = _bass_apply_transpose_and_velocity(base, req.transpose, req.velocity_scale)
    _write_bassline_to_ableton(int(req.track_index), int(req.clip_slot_index), tweaked)
    GEN_CACHE.set_bass(int(req.track_index), int(req.clip_slot_index), tweaked)
    return {"ok": True, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index, "bassline": tweaked}


@app.post("/drums/tweak")
def tweak_drums(req: TweakDrumsRequest):
    base = GEN_CACHE.get_drums(int(req.track_index), int(req.clip_slot_index))
    if base is None:
        return {"ok": False, "error": "no_cached_drums", "hint": "Generate a drum pattern first."}
    tweaked = _drums_apply_velocity_scale(base, req.velocity_scale)
    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), tweaked)
    GEN_CACHE.set_drums(int(req.track_index), int(req.clip_slot_index), tweaked)
    return {"ok": True, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index, "pattern": tweaked}


@app.post("/devices/load")
def load_device(req: LoadDeviceRequest):
    name = (req.device_name or "").strip()
    if not name:
        return {"ok": False, "error": "missing_device_name"}
    ctrl.load_device(int(req.track_index), name)
    return {"ok": True, "track_index": req.track_index, "device_name": name}


@app.post("/kits/load_test")
def load_test_kits():
    """Best-effort test load of one drum kit and one bass instrument/preset by name."""
    ctrl.load_device(0, DRUM_KIT_TEST_NAME)
    ctrl.load_device(1, BASS_DEVICE_TEST_NAME)
    return {
        "ok": True,
        "drum": {"track_index": 0, "device_name": DRUM_KIT_TEST_NAME},
        "bass": {"track_index": 1, "device_name": BASS_DEVICE_TEST_NAME},
    }


@app.post("/device/set_param")
def set_device_param(req: SetDeviceParamRequest):
    ctrl.send("/live/device/set/parameter/value", [
        int(req.track_index),
        int(req.device_index),
        int(req.param_index),
        float(req.value),
    ])
    return {
        "ok": True,
        "track_index": req.track_index,
        "device_index": req.device_index,
        "param_index": req.param_index,
        "value": req.value,
    }


@app.post("/macros/set")
def set_macro(req: SetMacroRequest):
    macro = int(req.macro)
    if macro not in MACRO_PARAM_BY_MACRO:
        return {"ok": False, "error": "unknown_macro", "available": sorted(MACRO_PARAM_BY_MACRO.keys())}
    param = MACRO_PARAM_BY_MACRO[macro]
    value = float(req.value)
    if value < 0.0:
        value = 0.0
    if value > 1.0:
        value = 1.0

    ctrl.send("/live/device/set/parameter/value", [
        int(MACRO_TRACK_INDEX),
        int(MACRO_DEVICE_INDEX),
        int(param),
        float(value),
    ])
    return {
        "ok": True,
        "macro": macro,
        "value": value,
        "track_index": MACRO_TRACK_INDEX,
        "device_index": MACRO_DEVICE_INDEX,
        "param_index": param,
    }


def _set_rack_macro(track_index: int, device_index: int, macro: int, value: float):
    if macro not in MACRO_PARAM_BY_MACRO:
        return {"ok": False, "error": "unknown_macro", "available": sorted(MACRO_PARAM_BY_MACRO.keys())}
    param = MACRO_PARAM_BY_MACRO[macro]
    value = float(value)
    if value < 0.0:
        value = 0.0
    if value > 1.0:
        value = 1.0

    ctrl.send("/live/device/set/parameter/value", [
        int(track_index),
        int(device_index),
        int(param),
        float(value),
    ])
    return {
        "ok": True,
        "macro": macro,
        "value": value,
        "track_index": track_index,
        "device_index": device_index,
        "param_index": param,
    }


@app.post("/bass/knobs/set")
def set_bass_knob(req: SetNamedKnobRequest):
    name = (req.name or "").strip().upper()
    if name not in BASS_KNOB_TO_MACRO:
        return {"ok": False, "error": "unknown_knob", "available": sorted(BASS_KNOB_TO_MACRO.keys())}
    macro = int(BASS_KNOB_TO_MACRO[name])
    return _set_rack_macro(BASS_RACK_TRACK_INDEX, BASS_RACK_DEVICE_INDEX, macro, req.value) | {"knob": name}


@app.post("/drums/knobs/set")
def set_drums_knob(req: SetNamedKnobRequest):
    name = (req.name or "").strip().upper()
    if name not in DRUMS_KNOB_TO_MACRO:
        return {"ok": False, "error": "unknown_knob", "available": sorted(DRUMS_KNOB_TO_MACRO.keys())}
    macro = int(DRUMS_KNOB_TO_MACRO[name])
    return _set_rack_macro(DRUMS_FX_RACK_TRACK_INDEX, DRUMS_FX_RACK_DEVICE_INDEX, macro, req.value) | {"knob": name}


def _openai_generate_pattern(style: str, bars: int, prompt: str | None, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    style = (style or "").strip()
    if not style:
        return None, {"ok": False, "error": "missing_style"}

    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    steps_per_bar = 16
    total_steps = bars * steps_per_bar

    schema = {
        "bars": bars,
        "step_division": "1/16",
        "lanes": {
            "kick": [0] * total_steps,
            "snare": [0] * total_steps,
            "ch": [0] * total_steps,
            "oh": [0] * total_steps,
            "perc1": [0] * total_steps,
            "perc2": [0] * total_steps,
        },
    }

    system = (
        "You generate drum patterns as strict JSON only (no markdown, no prose). "
        "Return a single JSON object with keys: bars, step_division, lanes. "
        "step_division must be '1/16'. bars must be an integer 1..4. "
        "lanes is an object mapping lane names to arrays of length bars*16. "
        "Each array element is an integer velocity 0..127 (0 means no hit). "
        "Valid lanes: kick, snare, clap, ch, oh, perc1, perc2. "
        "Keep it musical and stylistically correct. Prefer sparse patterns for beginners."
    )

    user = (
        f"Generate a {bars}-bar drum pattern in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        "Return ONLY JSON.\n\n"
        f"If you need a template, follow this shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return None, {"ok": False, "error": "openai_http_error", "status": e.code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, {"ok": False, "error": "openai_bad_response", "raw": data}

    try:
        pattern = json.loads(content)
    except Exception:
        return None, {"ok": False, "error": "openai_non_json", "content": content}

    return pattern, {"ok": True}


def _openai_generate_fx_pattern(style: str, bars: int, prompt: str | None, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    style = (style or "").strip()
    if not style:
        return None, {"ok": False, "error": "missing_style"}

    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    total_steps = bars * 16

    schema = {
        "bars": bars,
        "step_division": "1/16",
        "lanes": {
            "fx": [0] * total_steps,
        },
    }

    system = (
        "You generate FX swoosh/riser hit patterns as strict JSON only (no markdown, no prose). "
        "Return a single JSON object with keys: bars, step_division, lanes. "
        "step_division must be '1/16'. bars must be an integer 1..4. "
        "lanes is an object mapping lane names to arrays of length bars*16. "
        "Each array element is an integer velocity 0..127 (0 means no hit). "
        "Valid lanes: fx. "
        "Keep it sparse: 0-3 hits per bar, usually near the end of the bar for transitions."
    )

    user = (
        f"Generate a {bars}-bar FX swoosh/riser pattern in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        "Put most hits late in the bar (e.g., steps 12-15) and keep it sparse. "
        "Return ONLY JSON.\n\n"
        f"Template shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return None, {"ok": False, "error": "openai_http_error", "status": e.code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, {"ok": False, "error": "openai_bad_response", "raw": data}

    try:
        pattern = json.loads(content)
    except Exception:
        return None, {"ok": False, "error": "openai_non_json", "content": content}

    return pattern, {"ok": True}


def _openai_generate_stabs_pattern(style: str, bars: int, prompt: str | None, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    style = (style or "").strip()
    if not style:
        return None, {"ok": False, "error": "missing_style"}

    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    total_steps = bars * 16

    schema = {
        "bars": bars,
        "step_division": "1/16",
        "lanes": {
            "stab": [0] * total_steps,
        },
    }

    system = (
        "You generate rhythmic stab/chord patterns as strict JSON only (no markdown, no prose). "
        "Return a single JSON object with keys: bars, step_division, lanes. "
        "step_division must be '1/16'. bars must be an integer 1..4. "
        "lanes is an object mapping lane names to arrays of length bars*16. "
        "Each array element is an integer velocity 0..127 (0 means no hit). "
        "Valid lanes: stab. "
        "Make it supportive and beginner-friendly: short rhythmic stabs, not constant notes."
    )

    user = (
        f"Generate a {bars}-bar stab/chord rhythm pattern in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        "Use syncopation/offbeats appropriate for the style; leave space for kick and bass. "
        "Return ONLY JSON.\n\n"
        f"Template shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return None, {"ok": False, "error": "openai_http_error", "status": e.code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, {"ok": False, "error": "openai_bad_response", "raw": data}

    try:
        pattern = json.loads(content)
    except Exception:
        return None, {"ok": False, "error": "openai_non_json", "content": content}

    return pattern, {"ok": True}


def _openai_generate_perc_pattern(style: str, bars: int, prompt: str | None, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    style = (style or "").strip()
    if not style:
        return None, {"ok": False, "error": "missing_style"}

    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    total_steps = bars * 16

    schema = {
        "bars": bars,
        "step_division": "1/16",
        "lanes": {
            "ch": [0] * total_steps,
            "oh": [0] * total_steps,
            "perc1": [0] * total_steps,
            "perc2": [0] * total_steps,
        },
    }

    system = (
        "You generate percussion/hat patterns as strict JSON only (no markdown, no prose). "
        "Return a single JSON object with keys: bars, step_division, lanes. "
        "step_division must be '1/16'. bars must be an integer 1..4. "
        "lanes is an object mapping lane names to arrays of length bars*16. "
        "Each array element is an integer velocity 0..127 (0 means no hit). "
        "Valid lanes: ch, oh, perc1, perc2. "
        "Keep it beginner-friendly: emphasize groove and clarity, avoid filling every step."
    )

    user = (
        f"Generate a {bars}-bar percussion/hat pattern in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        "Keep it supportive of the kick/bass: mostly offbeats and light syncopation. "
        "Return ONLY JSON.\n\n"
        f"Template shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return None, {"ok": False, "error": "openai_http_error", "status": e.code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, {"ok": False, "error": "openai_bad_response", "raw": data}

    try:
        pattern = json.loads(content)
    except Exception:
        return None, {"ok": False, "error": "openai_non_json", "content": content}

    return pattern, {"ok": True}


def _openai_generate_pair(style: str, bars: int, drum_lanes: list[str], bass_root_midi: int, prompt: str | None, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    style = (style or "").strip()
    if not style:
        return None, {"ok": False, "error": "missing_style"}

    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    total_steps = bars * 16
    bass_profile = BASS_STYLE_PROFILE.get(style.lower()) or BASS_STYLE_PROFILE["techno"]

    drum_schema = {
        "bars": bars,
        "step_division": "1/16",
        "lanes": {lane: [0] * total_steps for lane in drum_lanes},
    }

    bass_schema = {
        "bars": bars,
        "step_division": "1/16",
        "root_midi": bass_root_midi,
        "steps": [0] * total_steps,
        "velocities": [0] * total_steps,
    }

    schema = {
        "style": style,
        "drums": drum_schema,
        "bass": bass_schema,
        "applied": {
            "bass_profile": bass_profile,
            "summary": "1-2 sentences describing the overall groove and what makes it match the style.",
            "why_drums": [
                "bullet points explaining key drum choices (kick placement, snare/clap placement, hats, syncopation) in plain language",
            ],
            "why_bass": [
                "bullet points explaining bass rhythm and note choices relative to the style and drums",
            ],
            "listening_tips": [
                "short, actionable listening tips (what to listen for when turning macros or changing density/transpose)",
            ],
            "suggested_settings": {
                "drums": {
                    "filter_cutoff": 0.0,
                    "filter_resonance": 0.0,
                    "drive": 0.0,
                    "eq_low_cut": 0.0,
                    "eq_presence": 0.0,
                    "space": 0.0,
                },
                "bass": {
                    "filter_cutoff": 0.0,
                    "filter_resonance": 0.0,
                    "drive": 0.0,
                    "eq_low_cut": 0.0,
                    "eq_presence": 0.0,
                    "space": 0.0,
                },
                "note": "All values are normalized 0..1 suggestions. They are for learning and can be mapped to Ableton rack macros later.",
            },
        },
    }

    system = (
        "You generate a complementary DRUM pattern and BASSLINE as strict JSON only (no markdown, no prose). "
        "Return exactly one JSON object with keys: style, drums, bass, applied. "
        "drums must match: {bars:int, step_division:'1/16', lanes:{lane:[velocities...]}}. "
        "Each lane array length bars*16; each value integer 0..127 (0 means rest). "
        "bass must match: {bars:int, step_division:'1/16', root_midi:int, steps:[midi...], velocities:[vel...]}. "
        "steps length bars*16; each entry is 0 (rest) or MIDI note 0..127. velocities length bars*16, 0..127, use 0 when step is 0. "
        "Keep bass monophonic (one note per step). Keep patterns beginner-friendly. "
        "The 'applied' object must contain teaching commentary fields: summary (string), why_drums (array of strings), why_bass (array of strings), listening_tips (array of strings). "
        "The 'applied' object must also include suggested_settings with nested drums and bass objects containing normalized 0..1 values for: filter_cutoff, filter_resonance, drive, eq_low_cut, eq_presence, space."
    )

    user = (
        f"Generate a {bars}-bar drum pattern and a complementary bassline in the style '{style}'. "
        f"For bass use root MIDI {bass_root_midi} as tonal center. "
        f"Bass style profile: scale={bass_profile['scale']}, octave_range={bass_profile['octave_range']}, density={bass_profile['density']}, rhythm={bass_profile['rhythm']}, note_pool={bass_profile['note_pool']}. "
        "Return ONLY JSON.\n\n"
        f"Template: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return None, {"ok": False, "error": "openai_http_error", "status": e.code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, {"ok": False, "error": "openai_bad_response", "raw": data}

    try:
        obj = json.loads(content)
    except Exception:
        return None, {"ok": False, "error": "openai_non_json", "content": content}

    return obj, {"ok": True}


def _query_with_timeout(address: str, args: list, timeout_s: float = 0.6):
    ctrl.send(address, args)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        item = OSC_STATE.get(address)
        if item is not None:
            ts, payload = item
            if ts >= deadline - timeout_s:
                return {"ok": True, "address": address, "args": payload}
        time.sleep(0.03)
    return {"ok": False, "address": address, "error": "timeout"}


@app.get("/setup/status")
def setup_status():
    """Guidance for the recommended template set: Track 0 = DRUMS, Track 1 = BASS."""
    status = {
        "ok": True,
        "listener": {"ok": OSC_LISTENER_ERROR is None, "error": OSC_LISTENER_ERROR},
        "recommended_template": {
            "tracks": [
                {"index": 0, "role": "DRUMS", "expected": "MIDI track with Drum Rack/kit loaded"},
                {"index": 1, "role": "BASS", "expected": "MIDI track with bass instrument loaded"},
            ]
        },
        "checks": [],
        "style_recommendations": STYLE_RECOMMENDATIONS,
    }

    if OSC_LISTENER_ERROR is not None:
        status["ok"] = False
        status["checks"].append({"ok": False, "error": "osc_listener_failed", "detail": OSC_LISTENER_ERROR})
        return status

    status["checks"].append(_query_with_timeout("/live/song/get/track_names", []))
    for t in [0, 1]:
        status["checks"].append(_query_with_timeout("/live/track/get/name", [t]))
        status["checks"].append(_query_with_timeout("/live/track/get/num_devices", [t]))

    return status


def _openai_generate_bassline(style: str, bars: int, root_midi: int, prompt: str | None, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    style = (style or "").strip()
    if not style:
        return None, {"ok": False, "error": "missing_style"}

    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    if root_midi < 0:
        root_midi = 0
    if root_midi > 127:
        root_midi = 127

    total_steps = bars * 16

    profile = BASS_STYLE_PROFILE.get(style.lower())
    if profile is None:
        profile = {
            "scale": "minor",
            "octave_range": [2, 3],
            "density": "medium",
            "rhythm": "straight_16",
            "note_pool": "root_fifth_octave",
            "explain": "Default style profile (generic club bassline).",
        }

    schema = {
        "bars": bars,
        "step_division": "1/16",
        "root_midi": root_midi,
        "steps": [0] * total_steps,
        "velocities": [0] * total_steps,
    }

    system = (
        "You generate bassline patterns as strict JSON only (no markdown, no prose). "
        "Return a single JSON object with keys: bars, step_division, root_midi, steps, velocities. "
        "step_division must be '1/16'. bars must be an integer 1..4. root_midi must be 0..127. "
        "steps is an array length bars*16. Each element is either 0 (rest) or a MIDI note number 0..127. "
        "velocities is an array length bars*16. Each element is 0..127; use 0 when step is 0. "
        "Keep it monophonic (at most one note per step) and beginner-friendly."
    )

    user = (
        f"Generate a {bars}-bar bassline in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        f"Use root note MIDI {root_midi} as the tonal center. "
        f"Style settings: scale={profile['scale']}, octave_range={profile['octave_range']}, density={profile['density']}, rhythm={profile['rhythm']}, note_pool={profile['note_pool']}. "
        "Prefer a simple, groovy, repetitive pattern appropriate for the style. "
        "Return ONLY JSON.\n\n"
        f"Template shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }

    req = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return None, {"ok": False, "error": "openai_http_error", "status": e.code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, {"ok": False, "error": "openai_bad_response", "raw": data}

    try:
        pattern = json.loads(content)
    except Exception:
        return None, {"ok": False, "error": "openai_non_json", "content": content}

    return pattern, {"ok": True}


def _validate_pattern(pattern: dict, bars: int):
    if not isinstance(pattern, dict):
        return None, {"ok": False, "error": "pattern_not_object"}

    if pattern.get("step_division") != "1/16":
        return None, {"ok": False, "error": "bad_step_division"}

    p_bars = pattern.get("bars")
    if not isinstance(p_bars, int) or p_bars < 1 or p_bars > 4:
        return None, {"ok": False, "error": "bad_bars"}

    if p_bars != bars:
        bars = p_bars

    lanes = pattern.get("lanes")
    if not isinstance(lanes, dict):
        return None, {"ok": False, "error": "lanes_missing"}

    total_steps = bars * 16
    clean_lanes: dict[str, list[int]] = {}

    for lane_name, values in lanes.items():
        lane = str(lane_name).strip().lower()
        if lane not in LANE_TO_MIDI_NOTE:
            continue
        if not isinstance(values, list):
            return None, {"ok": False, "error": "bad_lane_type", "lane": lane}

        # Models sometimes return the wrong array length for multi-bar patterns.
        # Normalize by truncating/padding to the expected length.
        if len(values) < total_steps:
            values = list(values) + ([0] * (total_steps - len(values)))
        elif len(values) > total_steps:
            values = list(values)[:total_steps]
        clean: list[int] = []
        for v in values:
            if isinstance(v, bool):
                v = 127 if v else 0
            if not isinstance(v, (int, float)):
                v = 0
            v = int(round(float(v)))
            if v < 0:
                v = 0
            if v > 127:
                v = 127
            clean.append(v)
        clean_lanes[lane] = clean

    if not clean_lanes:
        return None, {"ok": False, "error": "no_valid_lanes"}

    return {"bars": bars, "step_division": "1/16", "lanes": clean_lanes}, {"ok": True}


def _validate_bassline(pattern: dict, bars: int):
    if not isinstance(pattern, dict):
        return None, {"ok": False, "error": "pattern_not_object"}

    if pattern.get("step_division") != "1/16":
        return None, {"ok": False, "error": "bad_step_division"}

    p_bars = pattern.get("bars")
    if not isinstance(p_bars, int) or p_bars < 1 or p_bars > 4:
        return None, {"ok": False, "error": "bad_bars"}

    if p_bars != bars:
        bars = p_bars

    root_midi = pattern.get("root_midi")
    if not isinstance(root_midi, int):
        root_midi = 43
    if root_midi < 0:
        root_midi = 0
    if root_midi > 127:
        root_midi = 127

    total_steps = bars * 16

    steps = pattern.get("steps")
    velocities = pattern.get("velocities")

    # Auto-correct lengths
    if isinstance(steps, list):
        if len(steps) < total_steps:
            steps = steps + [0] * (total_steps - len(steps))
        elif len(steps) > total_steps:
            steps = steps[:total_steps]
            
    if isinstance(velocities, list):
        if len(velocities) < total_steps:
            velocities = velocities + [0] * (total_steps - len(velocities))
        elif len(velocities) > total_steps:
            velocities = velocities[:total_steps]

    if not isinstance(steps, list) or len(steps) != total_steps:
        return None, {"ok": False, "error": "bad_steps_length", "expected": total_steps, "got": len(steps) if isinstance(steps, list) else "not_list"}
    if not isinstance(velocities, list) or len(velocities) != total_steps:
        return None, {"ok": False, "error": "bad_velocities_length", "expected": total_steps, "got": len(velocities) if isinstance(velocities, list) else "not_list"}

    clean_steps: list[int] = []
    clean_vels: list[int] = []

    for n, v in zip(steps, velocities):
        if isinstance(n, bool):
            n = 0
        if not isinstance(n, (int, float)):
            n = 0
        n = int(round(float(n)))
        if n < 0:
            n = 0
        if n > 127:
            n = 127

        if isinstance(v, bool):
            v = 0
        if not isinstance(v, (int, float)):
            v = 0
        v = int(round(float(v)))
        if v < 0:
            v = 0
        if v > 127:
            v = 127

        if n == 0:
            v = 0
        if n > 0 and v == 0:
            v = 90

        clean_steps.append(n)
        clean_vels.append(v)

    return {
        "bars": bars,
        "step_division": "1/16",
        "root_midi": root_midi,
        "steps": clean_steps,
        "velocities": clean_vels,
    }, {"ok": True}


def _write_pattern_to_ableton(track_index: int, clip_slot_index: int, pattern: dict):
    bars = int(pattern["bars"])
    length_beats = float(bars * 4)
    ctrl.create_clip(track_index, clip_slot_index, length_beats)

    step = 0.25
    for lane, steps in pattern["lanes"].items():
        pitch = LANE_TO_MIDI_NOTE[lane]
        for i, vel in enumerate(steps):
            if vel <= 0:
                continue
            start = float(i) * step
            duration = 0.10
            if lane in {"kick", "snare", "clap"}:
                duration = 0.20
            ctrl.add_note(track_index, clip_slot_index, pitch, start, duration, vel)


def _write_bassline_to_ableton(track_index: int, clip_slot_index: int, bass: dict):
    bars = int(bass["bars"])
    length_beats = float(bars * 4)
    ctrl.create_clip(track_index, clip_slot_index, length_beats)

    step = 0.25
    steps = bass["steps"]
    vels = bass["velocities"]

    swing = float(bass.get("swing", 0.0) or 0.0)
    if swing < 0.0:
        swing = 0.0
    if swing > 0.3:
        swing = 0.3

    for i, (note, vel) in enumerate(zip(steps, vels)):
        if note <= 0 or vel <= 0:
            continue
        start = float(i) * step
        # Simple 16th swing: delay off-16ths slightly.
        if swing > 0.0 and (i % 2 == 1):
            start += swing * 0.08
        duration = 0.22
        ctrl.add_note(track_index, clip_slot_index, int(note), start, duration, int(vel))


def _apply_style_to_bassline(bass: dict, style: str):
    """Best-effort post-processing to make the bass feel more style-correct.

    This does not try to be 'perfect music theory'—it's a teaching-friendly set of defaults.
    """

    style = (style or "").strip().lower()
    bars = int(bass.get("bars", 1) or 1)
    total_steps = bars * 16
    steps = list(bass.get("steps", []))
    vels = list(bass.get("velocities", []))

    if len(steps) != total_steps or len(vels) != total_steps:
        return bass, {"ok": False, "error": "bad_length"}

    def clamp_midi(n: int):
        if n < 1:
            return 1
        if n > 127:
            return 127
        return n

    def set_step(i: int, note: int, vel: int):
        if note <= 0:
            steps[i] = 0
            vels[i] = 0
            return
        steps[i] = clamp_midi(note)
        if vel < 1:
            vel = 1
        if vel > 127:
            vel = 127
        vels[i] = vel

    # Defaults
    swing = 0.0
    density_keep = 1.0
    accent_steps: list[int] = []
    octave_jump_steps: list[int] = []

    if style == "house":
        # Offbeat feel: keep notes mostly on the "and" of each beat.
        # Steps per bar: 0..15; offbeats for 8ths are 2, 6, 10, 14.
        allowed = {2, 6, 10, 14}
        for b in range(bars):
            base = b * 16
            for i in range(16):
                idx = base + i
                if idx < len(steps) and steps[idx] > 0 and i not in allowed:
                    set_step(idx, 0, 0)
        accent_steps = [2, 10]
        density_keep = 0.9

    elif style == "garage":
        # Swingy / bouncy: allow 16ths but apply swing.
        swing = 0.18
        accent_steps = [3, 7, 11, 15]
        density_keep = 0.85

    elif style == "dnb":
        # Denser + syncopated: keep more 16ths, add occasional octave pops.
        swing = 0.0
        accent_steps = [0, 5, 10, 14]
        octave_jump_steps = [7, 15]
        density_keep = 1.0

    elif style == "breakbeat":
        swing = 0.08
        accent_steps = [0, 6, 12]
        density_keep = 0.95

    elif style == "tribal":
        # Leave space for percussion.
        allowed = {0, 4, 8, 12}
        for b in range(bars):
            base = b * 16
            for i in range(16):
                idx = base + i
                if idx < len(steps) and steps[idx] > 0 and i not in allowed:
                    set_step(idx, 0, 0)
        accent_steps = [0, 8]
        density_keep = 0.8

    elif style == "acid_techno":
        # Acid: more syncopated accents + occasional octave pops.
        swing = 0.0
        accent_steps = [0, 3, 7, 10, 14]
        octave_jump_steps = [7, 15]
        density_keep = 0.95

    elif style == "psytrance":
        # Psy: steady 16ths feel, very consistent, subtle accent for pulse.
        swing = 0.0
        accent_steps = [0, 4, 8, 12]
        density_keep = 1.0

    else:  # techno/default
        # Driving but not busy.
        swing = 0.0
        accent_steps = [0, 8, 12]
        density_keep = 0.9

    # Density shaping (probabilistic dropouts on non-accented steps)
    if density_keep < 1.0:
        for i in range(total_steps):
            if steps[i] <= 0:
                continue
            if (i % 16) in accent_steps:
                continue
            if random.random() > density_keep:
                set_step(i, 0, 0)

    # Accents
    for b in range(bars):
        base = b * 16
        for s in accent_steps:
            idx = base + s
            if 0 <= idx < total_steps and steps[idx] > 0:
                vels[idx] = min(127, int(round(vels[idx] * 1.15)))

    # Octave pops
    for b in range(bars):
        base = b * 16
        for s in octave_jump_steps:
            idx = base + s
            if 0 <= idx < total_steps and steps[idx] > 0:
                steps[idx] = clamp_midi(steps[idx] + 12)
                vels[idx] = min(127, int(round(vels[idx] * 1.05)))

    out = dict(bass)
    out["steps"] = steps
    out["velocities"] = vels
    out["swing"] = swing

    return out, {
        "ok": True,
        "style": style,
        "swing": swing,
        "accent_steps": accent_steps,
        "octave_jump_steps": octave_jump_steps,
        "density_keep": density_keep,
        "note": "These are post-processing defaults applied after AI generation to better match common feel for the style.",
    }


def _bass_apply_transpose_and_velocity(bass: dict, transpose: int, velocity_scale: float):
    transpose = int(transpose)
    velocity_scale = float(velocity_scale)
    if velocity_scale < 0.0:
        velocity_scale = 0.0
    if velocity_scale > 2.0:
        velocity_scale = 2.0

    steps = []
    vels = []
    for n, v in zip(bass["steps"], bass["velocities"]):
        n2 = int(n)
        v2 = int(v)
        if n2 > 0:
            n2 = n2 + transpose
            if n2 < 1:
                n2 = 1
            if n2 > 127:
                n2 = 127
            v2 = int(round(v2 * velocity_scale))
            if v2 < 1:
                v2 = 1
            if v2 > 127:
                v2 = 127
        else:
            v2 = 0
        steps.append(n2)
        vels.append(v2)

    out = dict(bass)
    out["steps"] = steps
    out["velocities"] = vels
    return out


def _drums_apply_velocity_scale(drums: dict, velocity_scale: float):
    velocity_scale = float(velocity_scale)
    if velocity_scale < 0.0:
        velocity_scale = 0.0
    if velocity_scale > 2.0:
        velocity_scale = 2.0

    lanes = {}
    for lane, steps in drums["lanes"].items():
        out_steps = []
        for v in steps:
            v2 = int(v)
            if v2 > 0:
                v2 = int(round(v2 * velocity_scale))
                if v2 < 1:
                    v2 = 1
                if v2 > 127:
                    v2 = 127
            out_steps.append(v2)
        lanes[lane] = out_steps

    out = dict(drums)
    out["lanes"] = lanes
    return out


@app.post("/patterns/generate_ai")
def generate_ai_pattern(req: GenerateAIPatternRequest):
    pattern, meta = _openai_generate_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_drums(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/perc/generate_ai")
def generate_ai_perc(req: GenerateAIPercPatternRequest):
    pattern, meta = _openai_generate_perc_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_perc(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/stabs/generate_ai")
def generate_ai_stabs(req: GenerateAIStabsPatternRequest):
    pattern, meta = _openai_generate_stabs_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_stabs(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/fx/generate_ai")
def generate_ai_fx(req: GenerateAIFxPatternRequest):
    pattern, meta = _openai_generate_fx_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_fx(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/bass/generate_ai")
def generate_ai_bassline(req: GenerateAIBasslineRequest):
    style = (req.style or "").strip().lower()
    root = int(req.root_midi)
    if style in BASS_STYLE_DEFAULTS:
        root = int(BASS_STYLE_DEFAULTS[style]["root"])

    applied = BASS_STYLE_PROFILE.get(style, BASS_STYLE_PROFILE["techno"])

    pattern, meta = _openai_generate_bassline(style, req.bars, root, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_bassline(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_bassline", "detail": vmeta, "raw": pattern}

    styled, sp = _apply_style_to_bassline(validated, style)

    _write_bassline_to_ableton(int(req.track_index), int(req.clip_slot_index), styled)
    GEN_CACHE.set_bass(int(req.track_index), int(req.clip_slot_index), styled)
    return {
        "ok": True,
        "bassline": styled,
        "track_index": req.track_index,
        "clip_slot_index": req.clip_slot_index,
        "applied": {
            "style": style,
            "root_midi": root,
            "scale": applied["scale"],
            "octave_range": applied["octave_range"],
            "density": applied["density"],
            "rhythm": applied["rhythm"],
            "note_pool": applied["note_pool"],
            "explain": applied["explain"],
            "post": sp,
        },
    }
