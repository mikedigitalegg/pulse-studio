import os
import json
import threading
import time
import random
import hashlib
import io
import wave
import math
import asyncio
import httpx
import re
import shutil
import socket
import urllib.parse
from collections import deque

from fastapi import FastAPI
from fastapi import Query
from fastapi import UploadFile
from fastapi import File
from fastapi import Form
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from dotenv import load_dotenv

try:
    import instrument_palette
    from live_link import LiveLink
    from pulse_bridge_client import BridgeError, PulseBridgeClient
except ModuleNotFoundError as e:
    raise RuntimeError(
        f"Could not import {e.name!r}. If it is 'ableton_track_creator', run the server from the "
        "project folder; otherwise install dependencies: python -m pip install -r requirements.txt"
    ) from e


app = FastAPI(title="Ableton Web POC")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Load repo-local env before reading OPENAI_API_KEY, ABLETON_MCP_*, etc.
load_dotenv(os.path.join(APP_DIR, ".env"))
RECORDINGS_DIR = os.path.join(APP_DIR, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)
app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")
AUDIO_DIR = os.path.join(APP_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
app.mount("/audio_files", StaticFiles(directory=AUDIO_DIR), name="audio_files")

PULSE_CANVAS_DIR = os.path.join(APP_DIR, "pulse_canvas")
if os.path.isdir(PULSE_CANVAS_DIR):
    app.mount(
        "/pulse_canvas",
        StaticFiles(directory=PULSE_CANVAS_DIR, html=True),
        name="pulse_canvas",
    )


@app.get("/audio/wav/{filename}")
def get_wav_file(filename: str):
    name = os.path.basename(filename or "")
    if not name.lower().endswith(".wav"):
        return {"ok": False, "error": "unsupported_format"}
    path = os.path.join(AUDIO_DIR, name)
    if not os.path.exists(path):
        path = os.path.join(RECORDINGS_DIR, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "not_found"}
    return FileResponse(path, media_type="audio/wav", filename=name)

VOLCA_DRUM_TRACK_INDEX = 0
PERC_TRACK_INDEX = 2
STABS_TRACK_INDEX = 3
FX_TRACK_INDEX = 4
CHORDS_TRACK_INDEX = 5
PAD_TRACK_INDEX = 6
KNOWLEDGE_FILE = os.path.join(APP_DIR, "knowledge", "styles.json")

_AUTOPLAY_LOCK = threading.Lock()
_AUTOPLAY_THREAD: threading.Thread | None = None
_AUTOPLAY_STOP: threading.Event | None = None
_AUTOPLAY_STATE: dict = {"running": False, "current_step": None, "last_scene_index": None}

PULSE_TRACK_PLAN: list[dict] = [
    {"name": "Drums", "track_index": 0, "role": "drums", "q": "909 kit"},
    {"name": "Bass", "track_index": 1, "role": "bass", "q": "bass"},
    {"name": "Perc", "track_index": PERC_TRACK_INDEX, "role": "perc", "q": "perc"},
    {"name": "Stabs", "track_index": STABS_TRACK_INDEX, "role": "stabs", "q": "stab"},
    {"name": "FX", "track_index": FX_TRACK_INDEX, "role": "fx", "q": "pad"},
    {"name": "Chords", "track_index": CHORDS_TRACK_INDEX, "role": "chords", "q": "pad"},
    {"name": "Pad", "track_index": PAD_TRACK_INDEX, "role": "pad", "q": "pad"},
]
# Roles understood by the legacy (pre-PulseBridge) keyword picker.
_LEGACY_PICKER_ROLE = {"perc": "drums", "fx": "pads", "chords": "pads", "pad": "pads"}


def _pulse_required_track_count() -> int:
    # Track count is derived from the highest referenced track_index.
    mx = 0
    for item in PULSE_TRACK_PLAN:
        try:
            mx = max(mx, int(item.get("track_index")))
        except Exception:
            continue
    return int(mx) + 1


def _pulse_default_track_indices() -> list[int]:
    out: list[int] = []
    for item in PULSE_TRACK_PLAN:
        try:
            out.append(int(item.get("track_index")))
        except Exception:
            pass
    # Preserve order but remove duplicates.
    seen: set[int] = set()
    uniq: list[int] = []
    for i in out:
        if i in seen:
            continue
        seen.add(i)
        uniq.append(i)
    return uniq

ABLETON_MCP_HOST = os.environ.get("ABLETON_MCP_HOST", "127.0.0.1")
ABLETON_MCP_PORT = int(os.environ.get("ABLETON_MCP_PORT", "9877"))

BROWSER_INDEX_FILE = os.path.join(APP_DIR, "knowledge", "ableton_browser_index.json")
TTS_RECIPES_FILE = os.path.join(APP_DIR, "knowledge", "tts_recipes.json")


class GenerateTTSRequest(BaseModel):
    text: str
    voice: str = "alloy"
    fx: str = "clean"
    cache: bool = True


class SaveTTSRecipeRequest(BaseModel):
    name: str
    text: str
    voice: str = "alloy"
    fx: str = "clean"
    filename: str | None = None


class GenerateVoicePackLinesRequest(BaseModel):
    style: str
    pack_id: str
    count: int = 12
    seed: int | None = None


class AutoplayStep(BaseModel):
    scene_index: int
    repeats: int = 1


class AutoplayStartRequest(BaseModel):
    steps: list[AutoplayStep] = Field(default_factory=list)
    bpm: float = 140.0
    clip_bars: int = 8
    loop: bool = True


class ProcessAudioRequest(BaseModel):
    source_url: str
    fx: str = "clean"
    label: str | None = None


def _safe_text_label(text: str, *, max_words: int = 5, max_len: int = 32) -> str:
    s = str(text or "").strip().lower()
    if not s:
        return ""
    # Keep only a few words so filenames stay short.
    words = re.findall(r"[a-z0-9]+", s)
    if not words:
        return ""
    words = words[: int(max_words)]
    label = "_".join(words)
    label = re.sub(r"_+", "_", label).strip("_")
    if len(label) > int(max_len):
        label = label[: int(max_len)].rstrip("_")
    return label


_TTS_RECIPES_LOCK = threading.Lock()


def _read_tts_recipes() -> list[dict]:
    try:
        if not os.path.exists(TTS_RECIPES_FILE):
            return []
        with open(TTS_RECIPES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                out.append(item)
        return out
    except Exception:
        return []


def _write_tts_recipes(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(TTS_RECIPES_FILE), exist_ok=True)
    with open(TTS_RECIPES_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def _make_recipe_id(*, name: str, text: str, voice: str, fx: str) -> str:
    raw = f"v1|{(name or '').strip()}|{(voice or '').strip()}|{(fx or '').strip()}|{(text or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class VoiceToMidiRequest(BaseModel):
    source_url: str
    track_index: int = 6
    clip_slot_index: int | None = None  # None: next free slot on the track (PulseBridge only)
    bpm: float = 140.0
    bars: int = 1
    base_pitch: int | None = None  # None: first filled pad on a Drum Rack, else 60
    velocity: int = 110
    fire: bool = True


UPLOAD_AUDIO_EXTS = {".wav", ".webm", ".ogg", ".mp3", ".m4a"}
UPLOAD_MAX_BYTES = 100 * 1024 * 1024


@app.post("/audio/upload")
async def upload_audio(file: UploadFile = File(...), label: str = Form("")):
    try:
        original = (file.filename or "audio.webm").strip() or "audio.webm"
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", original)
        safe_label = re.sub(r"[^a-zA-Z0-9._-]", "_", (label or "").strip())
        ts = int(time.time() * 1000)
        name_root, ext = os.path.splitext(safe_name)
        ext = (ext or ".webm").lower()
        if ext not in UPLOAD_AUDIO_EXTS:
            return {"ok": False, "error": "unsupported_format", "hint": f"Allowed: {', '.join(sorted(UPLOAD_AUDIO_EXTS))}"}
        if safe_label:
            out_name = f"rec_{ts}_{safe_label}{ext}"
        else:
            out_name = f"rec_{ts}_{name_root}{ext}"

        out_path = os.path.join(RECORDINGS_DIR, out_name)
        data = await file.read(UPLOAD_MAX_BYTES + 1)
        if len(data) > UPLOAD_MAX_BYTES:
            return {"ok": False, "error": "file_too_large", "hint": f"Max upload size is {UPLOAD_MAX_BYTES // (1024 * 1024)} MB."}

        # If the user uploaded something named .wav, validate the header.
        if out_name.lower().endswith(".wav") and data[:4] != b"RIFF":
            return {"ok": False, "error": "invalid_wav", "hint": "Uploaded file does not look like a WAV (missing RIFF header)."}
        with open(out_path, "wb") as f:
            f.write(data)

        return {
            "ok": True,
            "filename": out_name,
            "label": safe_label,
            "bytes": len(data),
            "url": f"/recordings/{out_name}",
            "file_path": out_path,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


_VOICE_PACKS_BASE: list[dict] = [
    {
        "id": "chants",
        "name": "Chants",
        "templates": [
            "{word}.",
            "{word}. {word}.",
            "{word} now.",
            "{word} it.",
        ],
        "words": [
            "Move",
            "Push",
            "Go",
            "Jump",
            "Hands up",
            "Heads down",
            "One more",
            "Again",
            "No sleep",
            "No mercy",
        ],
    },
    {
        "id": "rave_commands",
        "name": "Rave Commands",
        "templates": [
            "Turn it up.",
            "Bring it back.",
            "Hold that groove.",
            "Lock it in.",
            "Open the filter.",
            "Close the filter.",
            "Feel the pressure.",
            "Straight to the floor.",
        ],
    },
    {
        "id": "machine_talk",
        "name": "Machine Talk",
        "templates": [
            "Machine rhythm.",
            "Analog heat.",
            "Digital sweat.",
            "Underground signal.",
            "Concrete heartbeat.",
            "System online.",
            "Protocol engaged.",
        ],
    },
]


def _voice_packs_for_style(style: str) -> list[dict]:
    s = (style or "").strip().lower()
    packs = list(_VOICE_PACKS_BASE)

    if s in {"techno"}:
        packs.append(
            {
                "id": "techno_long",
                "name": "Techno Long",
                "templates": [
                    "Lock into the grid and let the room do the talking. Keep the kick steady, keep the pressure rising, and don’t break eye contact with the groove.",
                    "This is your signal to simplify: strip it back to the skeleton, then rebuild with intent. Every small change is a big move at this tempo.",
                    "No melody needed—just tension, release, and discipline. Ride the filter like it’s a lever, and watch the crowd breathe in sync.",
                    "Stay underground. Stay relentless. If it feels too clean, add grit; if it feels too loud, add space; and if it feels too safe, push.",
                ],
            }
        )

    if "acid" in s:
        packs.append(
            {
                "id": "acid_talk",
                "name": "Acid Talk",
                "templates": [
                    "Acid line.",
                    "303 on.",
                    "Resonance up.",
                    "Cutoff down.",
                    "Squelch it.",
                ],
            }
        )

        packs.append(
            {
                "id": "acid_long",
                "name": "Acid Long",
                "templates": [
                    "Turn the resonance until it bites, then ease the cutoff like you’re steering a live wire. The line should wobble, argue, and finally confess.",
                    "Let the 303 talk in circles until it becomes a spiral. Accents are punctuation—use them to make the machine sound like it has a secret.",
                    "When the squelch opens up, don’t rush it. Hold the tension, tease the peak, and let the groove feel like it’s melting through concrete.",
                    "Keep it raw but controlled: one hand on the filter, one hand on the crowd. If it’s too polite, drive it harder.",
                ],
            }
        )

    if s in {"dnb", "drum_and_bass", "drumandbass"}:
        packs.append(
            {
                "id": "fast_system",
                "name": "Fast System",
                "templates": [
                    "Double time.",
                    "Switch the pattern.",
                    "Reload.",
                    "Run it.",
                    "Back to the drop.",
                ],
            }
        )

        packs.append(
            {
                "id": "dnb_long",
                "name": "DnB Long",
                "templates": [
                    "Keep the low-end clean and let the drums do violence politely. Tighten the sub, sharpen the snare, and leave space for the break to breathe.",
                    "This is forward motion: roll the hats, snap the ghost notes, and let the bassline move like a shadow under streetlights.",
                    "If the groove feels lost, cut the noise and bring the kick back as a compass. Then reload with purpose.",
                    "Hold the energy on a knife edge—too much chaos is a blur, too much order is a loop. Find the sweet spot and sprint.",
                ],
            }
        )

    if s in {"house", "garage"}:
        packs.append(
            {
                "id": "groove_talk",
                "name": "Groove Talk",
                "templates": [
                    "Keep it groovy.",
                    "Feel that swing.",
                    "Let it breathe.",
                    "One more time.",
                ],
            }
        )

        packs.append(
            {
                "id": "house_long",
                "name": "House Long",
                "templates": [
                    "Let it swing without forcing it. Keep the kick warm, the clap honest, and the hats shimmering like light on water.",
                    "This is about space and smile. Bring the bass in softly, let the chord breathe, and make the groove feel like a conversation.",
                    "Don’t rush the lift—build it with tiny changes, then let the drop arrive like a door opening, not a wall collapsing.",
                    "If it feels stiff, push the shuffle. If it feels messy, simplify. The groove will tell you what it needs.",
                ],
            }
        )

    if "psy" in s:
        packs.append(
            {
                "id": "trance_signal",
                "name": "Trance Signal",
                "templates": [
                    "Follow the pulse.",
                    "Stay in the tunnel.",
                    "Keep it moving.",
                    "Open your mind.",
                ],
            }
        )

        packs.append(
            {
                "id": "psy_mystic_paragraphs",
                "name": "Mystic Paragraphs",
                "templates": [
                    "Breathe with the kick. Let the bassline braid itself around your spine like a luminous vine. Every bar is a small doorway, every hat a reminder that time is only a polite suggestion.",
                    "You are not hearing a pattern, you are remembering it. The loop is a mirror: it shows you the part of yourself that never stops moving, the part that can’t be named but can be danced.",
                    "Close your eyes and watch the colors arrive on schedule. The rhythm is an ancient machine that runs on attention. Feed it focus, and it will turn fear into velocity.",
                    "Let the melody dissolve into geometry. Let the geometry dissolve into breath. When the drop arrives, it’s not a change—it's a revelation you were already prepared to understand.",
                    "In the tunnel of sound, the mind becomes a lantern. The reverb is the room where old stories echo until they finally soften. Step forward, and let the pulse choose you.",
                    "You don’t chase the trance; you allow it. The groove is a compass that points inward. Follow it past language, past memory, until only movement remains.",
                ],
            }
        )

    if s in {"breakbeat", "breaks"}:
        packs.append(
            {
                "id": "breakbeat_long",
                "name": "Breakbeat Long",
                "templates": [
                    "Chop the rhythm until it stutters with intention. Keep the kick grounded, let the snare swing wide, and make the gaps feel dangerous.",
                    "This is controlled chaos: slice the loop, flip the accents, and let the groove zig-zag without losing the floor.",
                    "If it’s too straight, break it. If it’s too broken, anchor it. The best breaks feel like balance on moving pavement.",
                    "Build tension with syncopation, not volume. Let the rhythm do the talking, then hit them with a clean downbeat.",
                ],
            }
        )

    if s in {"tribal"}:
        packs.append(
            {
                "id": "tribal_long",
                "name": "Tribal Long",
                "templates": [
                    "Let the drums tell a story in layers. Start with the heartbeat, add the hands, then let the shakers draw circles around the fire.",
                    "Keep it hypnotic, not busy. One groove, many textures—every hit should feel like a footstep in a procession.",
                    "When the rhythm locks, open the space around it. Reverb like air, delay like distance, and a pulse that never apologizes.",
                    "If you need energy, add call-and-response. If you need focus, strip to the core. The ritual is in the repetition.",
                ],
            }
        )

    return packs


def _generate_voice_pack_lines(*, style: str, pack_id: str, count: int, seed: int | None) -> list[str]:
    packs = _voice_packs_for_style(style)
    pick = None
    for p in packs:
        if p.get("id") == pack_id:
            pick = p
            break
    if not isinstance(pick, dict):
        raise ValueError("unknown_pack")

    c = int(count)
    if c < 1:
        c = 1
    if c > 50:
        c = 50

    rng = random.Random(seed) if seed is not None else random.Random()

    templates = pick.get("templates")
    if not isinstance(templates, list) or not templates:
        templates = []
    words = pick.get("words")
    if not isinstance(words, list) or not words:
        words = []

    out: list[str] = []
    for _ in range(c * 3):
        if len(out) >= c:
            break

        if templates:
            t = str(rng.choice(templates))
        else:
            t = ""

        if "{word}" in t:
            w = str(rng.choice(words)) if words else "Move"
            line = t.replace("{word}", w)
        else:
            line = t

        line = (line or "").strip()
        if not line:
            continue
        if line not in out:
            out.append(line)

    if len(out) < c:
        fallback = [
            "Pulse check.",
            "Lock the groove.",
            "Hold that tension.",
            "Let it drop.",
        ]
        for s in fallback:
            if len(out) >= c:
                break
            if s not in out:
                out.append(s)

    return out[:c]


@app.get("/voice_packs")
def list_voice_packs(style: str = Query("techno")):
    try:
        packs = _voice_packs_for_style(style)
        out = [{"id": p.get("id"), "name": p.get("name")} for p in packs if isinstance(p, dict)]
        return {"ok": True, "style": (style or "").strip().lower(), "packs": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/voice_packs/generate")
def generate_voice_pack_lines(req: GenerateVoicePackLinesRequest):
    try:
        style = (req.style or "").strip().lower()
        pack_id = (req.pack_id or "").strip()
        if not style:
            return {"ok": False, "error": "missing_style"}
        if not pack_id:
            return {"ok": False, "error": "missing_pack_id"}

        lines = _generate_voice_pack_lines(style=style, pack_id=pack_id, count=req.count, seed=req.seed)
        return {"ok": True, "style": style, "pack_id": pack_id, "lines": lines}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _clamp16(x: float) -> int:
    if x > 32767:
        return 32767
    if x < -32768:
        return -32768
    return int(x)


def _fx_process_pcm(samples: list[int], *, fx: str, sample_rate: int, channels: int) -> list[int]:
    fx = (fx or "clean").strip().lower()
    if fx == "clean":
        return samples

    def _varispeed_resample(interleaved: list[int], *, ratio: float, ch: int) -> list[int]:
        r = float(ratio)
        if r <= 0.0:
            return interleaved

        frames_in = int(len(interleaved) // max(1, ch))
        if frames_in < 2:
            return interleaved

        frames_out = int(frames_in / r)
        if frames_out < 2:
            frames_out = 2

        out: list[int] = [0] * (frames_out * ch)
        for n in range(frames_out):
            src = n * r
            i0 = int(src)
            if i0 >= frames_in - 1:
                i0 = frames_in - 2
            i1 = i0 + 1
            t = src - float(i0)

            base0 = i0 * ch
            base1 = i1 * ch
            baseo = n * ch
            for c in range(ch):
                s0 = float(interleaved[base0 + c])
                s1 = float(interleaved[base1 + c])
                y = s0 + (s1 - s0) * t
                out[baseo + c] = _clamp16(y)
        return out

    out: list[int] = []

    ch = int(channels) if int(channels) > 0 else 1
    if ch > 8:
        ch = 8

    if fx in {"chipmunk", "chipmunk_house"}:
        ratio = 2.0 ** (7.0 / 12.0)
        samples = _varispeed_resample(samples, ratio=ratio, ch=ch)

    # Simple 1-pole high-pass and low-pass filters for "telephone" band-limiting.
    # These are intentionally lightweight and stable.
    hp_cut = 350.0
    lp_cut = 3200.0
    hp_alpha = math.exp(-2.0 * math.pi * hp_cut / max(1, sample_rate))
    lp_alpha = math.exp(-2.0 * math.pi * lp_cut / max(1, sample_rate))

    hp_prev_x = [0.0] * ch
    hp_prev_y = [0.0] * ch
    lp_prev_y = [0.0] * ch

    def highpass(x: float, c: int) -> float:
        y = hp_alpha * (hp_prev_y[c] + x - hp_prev_x[c])
        hp_prev_x[c] = x
        hp_prev_y[c] = y
        return y

    def lowpass(x: float, c: int) -> float:
        y = (1.0 - lp_alpha) * x + lp_alpha * lp_prev_y[c]
        lp_prev_y[c] = y
        return y

    def soft_clip(x: float, drive: float) -> float:
        return math.tanh(x * drive)

    def add_noise(x: float, amount: float) -> float:
        if amount <= 0.0:
            return x
        n = (random.random() * 2.0 - 1.0) * amount
        return x + n

    delay_rate = max(1, int(sample_rate))
    def make_delay(delay_ms: float, feedback: float, mix: float, lp_hz: float | None = None):
        d = int((delay_ms / 1000.0) * delay_rate)
        if d < 1:
            d = 1
        max_len = d + 2
        buf = [[0.0] * max_len for _ in range(ch)]
        idx = [0] * ch
        if lp_hz is None or lp_hz <= 0:
            lp_a = 0.0
        else:
            lp_a = math.exp(-2.0 * math.pi * float(lp_hz) / delay_rate)
        fb_lp = [0.0] * ch

        def process(x: float, c: int) -> float:
            j = idx[c]
            y = buf[c][j]
            fb = y
            if lp_a > 0.0:
                fb_lp[c] = (1.0 - lp_a) * fb + lp_a * fb_lp[c]
                fb = fb_lp[c]
            buf[c][j] = x + fb * float(feedback)
            idx[c] = (j + 1) % max_len
            return x * (1.0 - float(mix)) + y * float(mix)

        return process

    def make_reverb(mix: float):
        comb_ms = [29.7, 37.1, 41.1, 43.7]
        comb_fb = 0.78
        combs = [make_delay(ms, comb_fb, 1.0, lp_hz=6000.0) for ms in comb_ms]
        ap1 = make_delay(5.0, 0.55, 1.0, lp_hz=None)
        ap2 = make_delay(1.7, 0.55, 1.0, lp_hz=None)

        def process(x: float, c: int) -> float:
            wet = 0.0
            for comb in combs:
                wet += comb(x, c)
            wet *= 0.25
            wet = ap1(wet, c)
            wet = ap2(wet, c)
            return x * (1.0 - float(mix)) + wet * float(mix)

        return process

    chorus_delay = make_delay(18.0, 0.0, 0.35, lp_hz=None)
    slapback = make_delay(120.0, 0.0, 0.35, lp_hz=None)
    dub = make_delay(320.0, 0.55, 0.45, lp_hz=1800.0)
    verb = make_reverb(0.35)
    warehouse_verb = make_reverb(0.55)

    plate_verb = make_reverb(0.25)
    room_verb = make_reverb(0.18)
    ducked_delay = make_delay(240.0, 0.35, 0.22, lp_hz=2400.0)
    tape_delay = make_delay(190.0, 0.25, 0.18, lp_hz=3200.0)

    trem_phase = [0.0] * ch
    def tremolo(x: float, c: int, *, rate_hz: float, depth: float) -> float:
        r = float(rate_hz)
        if r <= 0.0:
            return x
        d = float(depth)
        if d < 0.0:
            d = 0.0
        if d > 1.0:
            d = 1.0
        trem_phase[c] += (2.0 * math.pi * r) / max(1.0, float(sample_rate))
        if trem_phase[c] > 2.0 * math.pi:
            trem_phase[c] -= 2.0 * math.pi
        lfo = 0.5 * (1.0 + math.sin(trem_phase[c]))
        gain = (1.0 - d) + d * lfo
        return x * gain

    for i, s in enumerate(samples):
        c = i % ch
        x = float(s) / 32768.0

        if fx in {"telephone", "radio"}:
            x = highpass(x, c)
            x = lowpass(x, c)
            x = soft_clip(x, 2.4)
            x *= 0.9
        elif fx in {"lofi", "bitcrush"}:
            x = highpass(x, c)
            x = lowpass(x, c)
            x = soft_clip(x, 2.0)
            steps = 48.0
            x = round(x * steps) / steps
            x *= 0.95
        elif fx in {"distort", "distortion"}:
            x = soft_clip(x, 4.2)
            x *= 0.85
        elif fx in {"hype", "bright"}:
            x = highpass(x, c)
            x = soft_clip(x, 1.9)
            x *= 0.95
        elif fx in {"megaphone", "mega"}:
            x = highpass(x, c)
            x = lowpass(x, c)
            x = add_noise(x, 0.008)
            x = soft_clip(x, 5.0)
            x = slapback(x, c)
            x *= 0.9
        elif fx in {"slapback", "slap"}:
            x = soft_clip(x, 2.2)
            x = slapback(x, c)
            x *= 0.9
        elif fx in {"dub", "dub_delay", "delay"}:
            x = highpass(x, c)
            x = dub(x, c)
            x = soft_clip(x, 1.6)
            x *= 0.95
        elif fx in {"warehouse", "rave", "hall"}:
            x = highpass(x, c)
            x = warehouse_verb(x, c)
            x = soft_clip(x, 1.5)
            x *= 0.95
        elif fx in {"chorus", "wide"}:
            x = chorus_delay(x, c)
            x = verb(x, c)
            x *= 0.95

        elif fx in {"plate", "in_mix_plate"}:
            x = plate_verb(x, c)
            x = soft_clip(x, 1.2)
            x *= 0.98

        elif fx in {"room", "in_mix_room"}:
            x = room_verb(x, c)
            x *= 0.99

        elif fx in {"ducked", "ducked_delay", "in_mix_ducked"}:
            x = ducked_delay(x, c)
            x = soft_clip(x, 1.25)
            x *= 0.98

        elif fx in {"tape", "tape_delay", "in_mix_tape"}:
            x = tape_delay(x, c)
            x = add_noise(x, 0.0015)
            x = soft_clip(x, 1.35)
            x *= 0.98

        elif fx in {"air", "in_mix_air"}:
            x = highpass(x, c)
            x = soft_clip(x, 1.15)
            x *= 0.99

        elif fx in {"chipmunk", "chipmunk_house"}:
            x = highpass(x, c)
            x = soft_clip(x, 1.55)
            x = room_verb(x, c)
            x = ducked_delay(x, c)
            x *= 0.96

        elif fx in {"deep", "monster", "sub", "demon"}:
            x = lowpass(x, c)
            x = soft_clip(x, 2.6)
            x = warehouse_verb(x, c)
            x *= 0.9

        elif fx in {"robot", "ringmod", "ring"}:
            x = highpass(x, c)
            x = lowpass(x, c)
            x = soft_clip(x, 2.2)
            x = chorus_delay(x, c)
            x *= 0.92

        elif fx in {"tremolo", "stutter", "gate"}:
            x = highpass(x, c)
            x = tremolo(x, c, rate_hz=10.0, depth=0.85)
            x = soft_clip(x, 1.7)
            x *= 0.95

        out.append(_clamp16(x * 32768.0))

    return out


def _wav_from_pcm(samples: list[int], *, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(int(channels))
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return buf.getvalue()


def _wav_read_pcm(wav_bytes: bytes) -> tuple[list[int], int, int]:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = int(wf.getnchannels())
            sample_rate = int(wf.getframerate())
            sampwidth = int(wf.getsampwidth())
            if sampwidth != 2:
                raise ValueError("unsupported_sample_width")
            frames = wf.readframes(wf.getnframes())
    except wave.Error as e:
        raise ValueError(str(e))
    # Interpret as signed int16 little-endian
    samples = [int.from_bytes(frames[i:i+2], "little", signed=True) for i in range(0, len(frames), 2)]
    return samples, sample_rate, channels


async def _openai_tts_wav_bytes(*, text: str, voice: str) -> tuple[bytes | None, dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

    text = (text or "").strip()
    if not text:
        return None, {"ok": False, "error": "missing_text"}

    voice = (voice or "alloy").strip()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "gpt-4o-mini-tts",
        "voice": voice,
        "response_format": "wav",
        "input": text,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post("https://api.openai.com/v1/audio/speech", json=payload, headers=headers)
            resp.raise_for_status()

            # Guard against unexpected non-audio responses.
            ctype = (resp.headers.get("content-type") or "").lower()
            data = resp.content or b""
            if data[:4] != b"RIFF":
                # Sometimes errors come back as JSON/text. Return a trimmed body.
                body_preview = ""
                try:
                    body_preview = resp.text[:800]
                except Exception:
                    body_preview = str(data[:200])
                return None, {"ok": False, "error": "tts_not_wav", "content_type": ctype, "body": body_preview}

            return data, {"ok": True, "content_type": ctype}
    except httpx.HTTPStatusError as e:
        try:
            body = e.response.text
        except Exception:
            body = str(e)
        return None, {"ok": False, "error": "openai_http_error", "status": e.response.status_code, "body": body}
    except Exception as e:
        return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}


@app.post("/tts/generate")
async def generate_tts(req: GenerateTTSRequest):
    try:
        text = (req.text or "").strip()
        voice = (req.voice or "alloy").strip()
        fx = (req.fx or "clean").strip().lower()

        label = _safe_text_label(text)

        cache_key = hashlib.sha1(f"v1|{voice}|{fx}|{text}".encode("utf-8")).hexdigest()
        # Include a human-friendly label for Ableton browsing; keep cache key to avoid collisions.
        if label:
            out_name = f"tts_{label}_{cache_key[:12]}.wav"
        else:
            out_name = f"tts_{cache_key[:12]}.wav"
        out_path = os.path.join(AUDIO_DIR, out_name)

        if bool(req.cache) and os.path.exists(out_path):
            return {"ok": True, "cached": True, "filename": out_name, "clip_label": label, "url": f"/audio_files/{out_name}", "file_path": out_path}

        wav_bytes, meta = await _openai_tts_wav_bytes(text=text, voice=voice)
        if not meta.get("ok") or wav_bytes is None:
            return {"ok": False, "error": meta.get("error"), "detail": meta}

        samples, sr, ch = _wav_read_pcm(wav_bytes)
        processed = _fx_process_pcm(samples, fx=fx, sample_rate=sr, channels=ch)
        out_wav = _wav_from_pcm(processed, sample_rate=sr, channels=ch)

        with open(out_path, "wb") as f:
            f.write(out_wav)

        return {
            "ok": True,
            "cached": False,
            "filename": out_name,
            "voice": voice,
            "fx": fx,
            "clip_label": label,
            "url": f"/audio_files/{out_name}",
            "file_path": out_path,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- voice -> Live

VOICE_TRACK_NAME = "PS-VOX"
VOICE_SIMPLER_TRACK_PREFIX = "PS-VOX-SMP"
VOICE_USER_LIBRARY_SUBDIR = ("Samples", "Pulse Voices")


def _prepare_voice_pcm(
    samples: list[int],
    channels: int,
    sample_rate: int,
    *,
    threshold: float = 0.01,
    preroll_ms: float = 5.0,
    tail_ms: float = 40.0,
    peak_target: float = 0.89,
    max_gain: float = 8.0,
) -> list[int]:
    """Make a TTS phrase land on the beat and at a consistent level.

    Trims leading/trailing silence (TTS adds ~100-300 ms up front, which makes the phrase late),
    normalizes the peak to about -1 dBFS, and adds short fades so the cut points don't click.
    """
    ch = max(1, int(channels))
    frames = len(samples) // ch
    if frames == 0:
        return list(samples)
    thr = int(32767 * threshold)

    def frame_peak(i: int) -> int:
        base = i * ch
        return max(abs(samples[base + c]) for c in range(ch))

    start = next((i for i in range(frames) if frame_peak(i) > thr), None)
    if start is None:
        return list(samples)
    end = next(i for i in range(frames - 1, -1, -1) if frame_peak(i) > thr) + 1

    sr = max(1, int(sample_rate))
    start = max(0, start - int(sr * preroll_ms / 1000.0))
    end = min(frames, end + int(sr * tail_ms / 1000.0))
    seg = samples[start * ch:end * ch]

    peak = max(abs(v) for v in seg) or 1
    gain = min(max_gain, (peak_target * 32767.0) / peak)
    n = len(seg) // ch
    fade_in = max(1, int(sr * 0.003))
    fade_out = max(1, int(sr * 0.010))
    out: list[int] = []
    for i in range(n):
        env = 1.0
        if i < fade_in:
            env = i / fade_in
        elif i >= n - fade_out:
            env = (n - 1 - i) / fade_out
        g = gain * env
        for c in range(ch):
            out.append(max(-32768, min(32767, int(round(seg[i * ch + c] * g)))))
    return out


def _resolve_voice_source(filename: str | None, source_url: str | None) -> str | None:
    """Local path of a generated WAV from its filename or /audio_files|/recordings URL (no directory escapes)."""
    name = os.path.basename((filename or "").strip() or (source_url or "").strip().split("?")[0])
    if not name.lower().endswith(".wav"):
        return None
    for d in (AUDIO_DIR, RECORDINGS_DIR):
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return path
    return None


def _prepare_voice_file(src_path: str) -> tuple[str, float]:
    """Write a trimmed/normalized copy (vox_*.wav) next to the source; returns (path, duration_s)."""
    with open(src_path, "rb") as f:
        samples, sr, ch = _wav_read_pcm(f.read())
    base = os.path.basename(src_path)
    if base.startswith("vox_"):
        return src_path, (len(samples) // max(1, ch)) / float(sr or 1)
    processed = _prepare_voice_pcm(samples, ch, sr)
    out_path = os.path.join(AUDIO_DIR, "vox_" + base)
    with open(out_path, "wb") as f:
        f.write(_wav_from_pcm(processed, sample_rate=sr, channels=ch))
    return out_path, (len(processed) // max(1, ch)) / float(sr or 1)


def _voice_label_from_filename(path: str) -> str:
    """tts_hands_up_3a748ffc975b.wav -> "hands up" (drops the prefix and the cache hash)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^(vox_)?(fx_\d+_)?(tts_)?", "", stem)
    stem = re.sub(r"_?[0-9a-f]{12,40}$", "", stem)
    return stem.replace("_", " ").strip() or "voice"


def _wav_duration_s(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate() or 1)


def _live_version_tuple(version: str | None) -> tuple[int, int, int]:
    try:
        parts = [int(x) for x in str(version or "").split(".")[:3]]
        return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]
    except Exception:
        return (0, 0, 0)


def _user_library_dir() -> str | None:
    env = os.environ.get("PULSE_USER_LIBRARY")
    candidates = [env] if env else []
    candidates.append(os.path.join(os.path.expanduser("~"), "Documents", "Ableton", "User Library"))
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


async def _voice_track_index(*, audio: bool, name: str) -> tuple[int, bool]:
    """Index of the named voice track, creating it (audio or MIDI) at the end of the set if missing."""
    song = await _bridge_call("get_song", {})
    names = list((song or {}).get("track_names") or [])
    if name in names:
        return names.index(name), False
    created = await _bridge_call("create_audio_track" if audio else "create_midi_track", {"index": -1})
    idx = int((created or {}).get("index", len(names)))
    await _bridge_call("set_track", {"track_index": idx, "name": name})
    return idx, True


async def _verify_voice_plays(track_index: int, clip_slot_index: int, duration_s: float, timeout_s: float) -> dict:
    """Fire the clip and watch the track's output meter until signal shows up (or explain why not)."""
    song = await _bridge_call("get_song", {})
    tempo = float((song or {}).get("tempo") or 120.0)
    bar_s = 240.0 / max(20.0, tempo)  # default launch quantization is one bar
    wait_s = min(max(0.5, float(timeout_s)), bar_s + min(duration_s, 4.0) + 0.75)

    await _bridge_call("fire_clip", {"track_index": track_index, "clip_slot_index": clip_slot_index})
    t0 = time.time()
    best = 0.0
    last = {}
    while time.time() - t0 < wait_s:
        last = await _bridge_call("get_track_meter", {"track_index": track_index}) or {}
        best = max(best, float(last.get("peak") or 0.0))
        if best > 0.02:
            return {"playing": True, "peak": round(best, 3), "waited_s": round(time.time() - t0, 2)}
        await asyncio.sleep(0.1)

    reasons = []
    if last.get("mute"):
        reasons.append("the voice track is muted")
    if last.get("soloed_elsewhere"):
        reasons.append("another track is soloed")
    if float(last.get("volume") or 0.0) < 0.05:
        reasons.append("the voice track fader is down")
    if float(last.get("master_volume") if last.get("master_volume") is not None else 1.0) < 0.05:
        reasons.append("the master fader is down")
    if not last.get("is_playing"):
        reasons.append("Live's transport didn't start")
    if not reasons:
        reasons.append("no signal on the track meter; check the track's output routing and your audio device")
    return {"playing": False, "peak": round(best, 3), "waited_s": round(time.time() - t0, 2), "reasons": reasons}


def _copy_to_user_library(user_library: str, path: str, subdir: tuple[str, ...]) -> tuple[str, str]:
    """Copy a file into the User Library so Live's browser can load it; returns (dest, browser folder path)."""
    dest_dir = os.path.join(user_library, *subdir)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    if not os.path.isfile(dest):
        shutil.copyfile(path, dest)
    return dest, "user_library/" + "/".join(subdir)


async def _bridge_load_with_retry(cmd: str, params: dict, tries: int = 10) -> str | None:
    """Run a browser-load command, retrying while Live indexes new User Library files. Returns the last error, or None."""
    last_err = None
    for _ in range(max(1, tries)):
        try:
            await _bridge_call(cmd, params, 5.0)
            return None
        except Exception as e:
            last_err = str(e)
            # Only a missing item is worth waiting for; anything else won't fix itself.
            if "not_found" not in last_err:
                return last_err
            await asyncio.sleep(0.5)
    return last_err


async def _make_track_audible(track_index: int, what: str, warnings: list[str]) -> None:
    """Unmute the track and raise a low fader, so a freshly sent clip can actually be heard."""
    state = await _bridge_call("get_track_meter", {"track_index": track_index}) or {}
    if state.get("mute"):
        await _bridge_call("set_track", {"track_index": track_index, "mute": False})
        warnings.append(f"Unmuted the {what} track.")
    if float(state.get("volume") or 0.0) < 0.5:
        await _bridge_call("set_track", {"track_index": track_index, "volume": 0.85})
        warnings.append(f"{what.capitalize()} track fader was low; set it to 0 dB.")


async def _send_voice_as_simpler(path: str, duration_s: float, label: str, req: "VoiceSendRequest") -> dict:
    """Fallback for Live before 12.0.5: Simpler on its own MIDI track, triggered by one note."""
    ul = _user_library_dir()
    if not ul:
        return {
            "ok": False,
            "error": "no_user_library",
            "hint": "Live before 12.0.5 can't create audio clips from a script. Set PULSE_USER_LIBRARY to your Ableton User Library folder, or drag the file in by hand.",
        }
    dest, browser_path = _copy_to_user_library(ul, path, VOICE_USER_LIBRARY_SUBDIR)

    track_name = f"{VOICE_SIMPLER_TRACK_PREFIX} {label or os.path.splitext(os.path.basename(path))[0]}"[:40]
    ti, _ = await _voice_track_index(audio=False, name=track_name)

    last_err = await _bridge_load_with_retry("load_item_at_path", {"track_index": ti, "path": browser_path, "name": os.path.basename(dest)})
    if last_err:
        return {"ok": False, "error": "sample_not_in_browser", "detail": last_err, "file_path": dest, "track_index": ti}

    song = await _bridge_call("get_song", {})
    tempo = float((song or {}).get("tempo") or 120.0)
    beats = max(0.25, duration_s * tempo / 60.0)
    clip_len = float(max(4, int(-(-beats // 4)) * 4))
    slot = int(req.clip_slot_index) if req.clip_slot_index is not None else 0
    await _bridge_call("write_clip", {
        "track_index": ti,
        "clip_slot_index": slot,
        "length": clip_len,
        "name": label or "voice",
        "notes": [{"pitch": 60, "start_time": 0.0, "duration": beats, "velocity": 110}],
    })
    return {"ok": True, "method": "simpler", "track_index": ti, "clip_slot_index": slot, "file_path": dest}


class VoiceSendRequest(BaseModel):
    filename: str | None = None     # a WAV in audio/ or recordings/ (e.g. /tts/generate's filename)
    source_url: str | None = None   # or its /audio_files/... or /recordings/... URL
    clip_slot_index: int | None = None  # default: first free slot on the voice track
    track_index: int | None = None  # default: the PS-VOX audio track (created if missing)
    name: str | None = None
    prepare: bool = True            # trim silence + normalize
    fire: bool = True               # launch it and confirm it's audible
    verify_timeout_s: float = 8.0


@app.post("/voice/send_to_live")
async def send_voice_to_live(req: VoiceSendRequest):
    src = _resolve_voice_source(req.filename, req.source_url)
    if not src:
        return {"ok": False, "error": "voice_file_not_found", "hint": "Pass the filename or /audio_files/... URL of a generated WAV."}

    try:
        path, duration_s = _prepare_voice_file(src) if req.prepare else (src, _wav_duration_s(src))
    except Exception as e:
        return {"ok": False, "error": "voice_prepare_failed", "detail": str(e)}
    path = os.path.abspath(path)

    if not BRIDGE.connected:
        return {
            "ok": False,
            "error": "bridge_not_connected",
            "hint": "Enable the PulseBridge control surface in Live to send voices automatically, or drag the file in by hand.",
            "file_path": path,
        }

    label = (req.name or "").strip() or _voice_label_from_filename(src)
    warnings: list[str] = []

    try:
        hello = await _bridge_call("hello", {})
        live_version = str((hello or {}).get("live_version") or "")
        can_audio_clip = _live_version_tuple(live_version) >= (12, 0, 5)

        result = None
        if can_audio_clip:
            if req.track_index is not None:
                ti = int(req.track_index)
            else:
                ti, created = await _voice_track_index(audio=True, name=VOICE_TRACK_NAME)
                if created:
                    warnings.append(f"Created audio track '{VOICE_TRACK_NAME}' for voices.")
            if req.clip_slot_index is not None:
                slot = int(req.clip_slot_index)
            else:
                free = await _bridge_call("find_free_slot", {"track_index": ti})
                slot = int((free or {}).get("slot", 0))
            try:
                clip = await _bridge_call("load_audio_clip", {
                    "track_index": ti,
                    "clip_slot_index": slot,
                    "file_path": path,
                    "name": label,
                    "warping": False,
                    "looping": False,
                })
                result = {"ok": True, "method": "audio_clip", "track_index": ti, "clip_slot_index": slot, "clip": clip, "file_path": path}
            except Exception as e:
                if "unsupported" not in str(e):
                    return {"ok": False, "error": "load_audio_clip_failed", "detail": str(e), "file_path": path}
                warnings.append("This Live build can't create audio clips from a script; used a Simpler instead.")

        if result is None:
            result = await _send_voice_as_simpler(path, duration_s, label, req)
            if not result.get("ok"):
                result.setdefault("file_path", path)
                result["live_version"] = live_version
                return result

        ti, slot = int(result["track_index"]), int(result["clip_slot_index"])

        # Make sure nothing on the track itself stops it being heard.
        await _make_track_audible(ti, "voice", warnings)

        result["verified"] = await _verify_voice_plays(ti, slot, duration_s, req.verify_timeout_s) if req.fire else None
    except Exception as e:
        return {"ok": False, "error": "bridge_error", "detail": str(e), "file_path": path}

    result.update({"live_version": live_version, "duration_s": round(duration_s, 3), "warnings": warnings})
    return result


# ---------------------------------------------------------------- samples -> Live
#
# Imported samples live in samples/ as 16-bit WAV (the page decodes MP3/AIFF/mic recordings
# and re-encodes them before upload, so the server never needs ffmpeg). Live's own library
# samples come from the browser index and are loaded by browser path instead of by file.

SAMPLES_DIR = os.path.join(APP_DIR, "samples")
os.makedirs(SAMPLES_DIR, exist_ok=True)
app.mount("/sample_files", StaticFiles(directory=SAMPLES_DIR), name="sample_files")

SAMPLE_TRACK_NAME = "PS-SMP"
SAMPLE_USER_LIBRARY_SUBDIR = ("Samples", "Pulse Samples")
SAMPLE_MAX_BYTES = 100 * 1024 * 1024
SAMPLE_TARGETS = ("audio_clip", "simpler", "drum_pad")
SAMPLE_LIBRARY_EXTS = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
_SAMPLE_FILE_RE = re.compile(r"^smp_[a-z0-9_]*[0-9a-f]{12}\.wav$")


def _sample_slug(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or ""))[0].lower()
    return re.sub(r"[^a-z0-9]+", "_", stem).strip("_")[:40]


def _sample_label(filename: str) -> str:
    """smp_deep_kick_3a748ffc975b.wav -> "deep kick"."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"^smp_", "", stem)
    stem = re.sub(r"_?[0-9a-f]{12}$", "", stem)
    return stem.replace("_", " ").strip() or "sample"


def _sample_path(filename: str | None) -> str | None:
    """Local path of an imported sample by filename (no directory escapes)."""
    name = os.path.basename((filename or "").strip().split("?")[0])
    if not _SAMPLE_FILE_RE.match(name):
        return None
    path = os.path.join(SAMPLES_DIR, name)
    return path if os.path.isfile(path) else None


def _sample_info(path: str) -> dict:
    name = os.path.basename(path)
    with wave.open(path, "rb") as wf:
        frames, sr, ch = wf.getnframes(), wf.getframerate(), wf.getnchannels()
    return {
        "filename": name,
        "name": _sample_label(name),
        "url": f"/sample_files/{name}",
        "duration_s": round(frames / float(sr or 1), 3),
        "sample_rate": sr,
        "channels": ch,
        "bytes": os.path.getsize(path),
        "modified": os.path.getmtime(path),
    }


@app.post("/samples/import")
async def import_sample(file: UploadFile = File(...), name: str = Form(""), trim: bool = Form(False)):
    """Save an uploaded 16-bit WAV as an imported sample. trim: cut silence and normalize (good for mic takes)."""
    data = await file.read(SAMPLE_MAX_BYTES + 1)
    if len(data) > SAMPLE_MAX_BYTES:
        return {"ok": False, "error": "file_too_large", "hint": f"Max sample size is {SAMPLE_MAX_BYTES // (1024 * 1024)} MB."}
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return {"ok": False, "error": "invalid_wav", "hint": "Send a WAV file; the page converts other formats before uploading."}
    try:
        samples, sr, ch = _wav_read_pcm(data)
    except ValueError as e:
        return {"ok": False, "error": "unsupported_wav", "detail": str(e), "hint": "Only 16-bit PCM WAV is accepted; the page converts other formats before uploading."}
    if not samples:
        return {"ok": False, "error": "empty_audio"}
    if trim:
        samples = _prepare_voice_pcm(samples, ch, sr)
        data = _wav_from_pcm(samples, sample_rate=sr, channels=ch)

    slug = _sample_slug(name or file.filename or "sample")
    digest = hashlib.sha1(data).hexdigest()[:12]
    out_name = f"smp_{slug}_{digest}.wav" if slug else f"smp_{digest}.wav"
    out_path = os.path.join(SAMPLES_DIR, out_name)
    existed = os.path.isfile(out_path)
    if not existed:
        with open(out_path, "wb") as f:
            f.write(data)
    return {"ok": True, "duplicate": existed, "file_path": out_path, **_sample_info(out_path)}


@app.get("/samples/list")
def list_samples():
    items = []
    for name in os.listdir(SAMPLES_DIR):
        if not _SAMPLE_FILE_RE.match(name):
            continue
        try:
            items.append(_sample_info(os.path.join(SAMPLES_DIR, name)))
        except (wave.Error, EOFError, OSError):
            continue
    items.sort(key=lambda x: x["modified"], reverse=True)
    return {"ok": True, "samples": items}


class SampleDeleteRequest(BaseModel):
    filename: str


@app.post("/samples/delete")
def delete_sample(req: SampleDeleteRequest):
    """Remove an imported sample from Pulse. Copies already in the User Library stay, since Live sets may use them."""
    path = _sample_path(req.filename)
    if not path:
        return {"ok": False, "error": "sample_not_found"}
    os.remove(path)
    return {"ok": True, "filename": os.path.basename(path)}


@app.get("/samples/library")
def search_sample_library(q: str = Query(""), limit: int = Query(60, ge=1, le=500)):
    """Search the samples in Live's browser index (Core Library and User Library)."""
    terms = [t for t in (q or "").lower().split() if t]
    items = _browser_index_items()
    out = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        path = str(it.get("path") or "")
        name = str(it.get("name") or "")
        root = path.split("/", 1)[0]
        if root not in ("samples", "user_library") or not name.lower().endswith(SAMPLE_LIBRARY_EXTS):
            continue
        hay = f"{name} {path}".lower()
        if any(t not in hay for t in terms):
            continue
        key = (path, name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "path": path, "uri": it.get("uri")})
        if len(out) >= limit:
            break
    return {"ok": True, "items": out, "indexed": bool(items)}


class SampleSendRequest(BaseModel):
    filename: str | None = None        # an imported sample (see /samples/list)
    library_path: str | None = None    # or a Live library sample: its browser folder path...
    library_name: str | None = None    # ...and item name (from /samples/library)
    library_uri: str | None = None
    target: str = "audio_clip"         # audio_clip | simpler | drum_pad
    track_index: int | None = None     # audio_clip/simpler: default a PS-SMP track; drum_pad: required
    clip_slot_index: int | None = None # default: first free slot
    pad_note: int | None = None        # drum_pad: default the first empty pad from C1 (36)
    loop: bool = False                 # audio_clip: warp to tempo and loop, instead of a one-shot
    name: str | None = None
    fire: bool = True                  # launch the clip and confirm it's audible (not for drum pads)
    verify_timeout_s: float = 8.0


def _sample_browser_source(req: SampleSendRequest, path: str | None) -> tuple[dict | None, dict | None]:
    """(browser path/name to load the sample through Live's browser, error result)."""
    if path is None:
        return {"path": req.library_path, "name": req.library_name, "uri": req.library_uri}, None
    ul = _user_library_dir()
    if not ul:
        return None, {
            "ok": False,
            "error": "no_user_library",
            "hint": "Simpler and Drum Rack pads load samples through Live's browser. Set PULSE_USER_LIBRARY to your Ableton User Library folder.",
            "file_path": path,
        }
    dest, browser_path = _copy_to_user_library(ul, path, SAMPLE_USER_LIBRARY_SUBDIR)
    return {"path": browser_path, "name": os.path.basename(dest)}, None


async def _sample_slot(req: SampleSendRequest, track_index: int) -> int:
    if req.clip_slot_index is not None:
        return int(req.clip_slot_index)
    free = await _bridge_call("find_free_slot", {"track_index": track_index})
    return int((free or {}).get("slot", 0))


async def _sample_to_drum_pad(req: SampleSendRequest, source: dict, label: str, warnings: list[str]) -> dict:
    if req.track_index is None:
        return {"ok": False, "error": "missing_track", "hint": "Pick the track with the Drum Rack to load the sample onto."}
    ti = int(req.track_index)
    pads = await _bridge_call("get_drum_pads", {"track_index": ti}) or {}
    if not pads.get("rack"):
        track = await _bridge_call("get_track", {"track_index": ti}) or {}
        if track.get("devices"):
            return {"ok": False, "error": "no_drum_rack", "hint": "That track has an instrument but no Drum Rack. Pick a drum track, or an empty MIDI track."}
        await _bridge_call("load_device", {"track_index": ti, "device_name": "Drum Rack"}, 10.0)
        warnings.append("Added an empty Drum Rack to the track.")
        pads = {"pads": []}
    filled = {int(p["note"]) for p in (pads.get("pads") or []) if p.get("note") is not None}
    if req.pad_note is not None:
        note = int(req.pad_note)
        if note in filled:
            warnings.append("Replaced the sample already on that pad.")
    else:
        note = next((n for n in range(36, 52) if n not in filled), 36)
        if note in filled:
            warnings.append("The 16 main pads are full; replaced the sample on C1.")

    params = {"track_index": ti, "pad_note": note, **{k: v for k, v in source.items() if v}}
    err = await _bridge_load_with_retry("load_item_to_drum_pad", params)
    if err:
        code = "sample_not_in_browser" if "not_found" in err else "drum_pad_load_failed"
        hint = "Reinstall PulseBridge (python pulse_bridge/install.py) and restart Live." if "unknown_command" in err else None
        return {"ok": False, "error": code, "detail": err, "hint": hint, "track_index": ti, "pad_note": note}
    after = await _bridge_call("get_drum_pads", {"track_index": ti}) or {}
    pad = next((p for p in (after.get("pads") or []) if int(p.get("note", -1)) == note), None)
    if pad is None:
        warnings.append("Live didn't report the pad as filled; check the Drum Rack.")
    return {"ok": True, "method": "drum_pad", "track_index": ti, "pad_note": note, "pad_name": (pad or {}).get("name") or label}


@app.post("/samples/send_to_live")
async def send_sample_to_live(req: SampleSendRequest):
    target = (req.target or "audio_clip").strip().lower()
    if target not in SAMPLE_TARGETS:
        return {"ok": False, "error": "bad_target", "hint": f"target is one of: {', '.join(SAMPLE_TARGETS)}"}

    path = None
    duration_s = None
    if req.filename:
        path = _sample_path(req.filename)
        if not path:
            return {"ok": False, "error": "sample_not_found", "hint": "Pass the filename of an imported sample (see /samples/list)."}
        path = os.path.abspath(path)
        duration_s = _wav_duration_s(path)
        label = (req.name or "").strip() or _sample_label(path)
    elif req.library_path and req.library_name:
        if target == "audio_clip":
            return {"ok": False, "error": "library_not_audio_clip", "hint": "Live's library samples load through the browser; send them to a Simpler or a Drum Rack pad."}
        label = (req.name or "").strip() or os.path.splitext(req.library_name)[0]
    else:
        return {"ok": False, "error": "missing_sample", "hint": "Pass filename (imported) or library_path + library_name (Live library)."}

    if not BRIDGE.connected:
        return {
            "ok": False,
            "error": "bridge_not_connected",
            "hint": "Enable the PulseBridge control surface in Live to send samples automatically, or drag the file in by hand.",
            "file_path": path,
        }

    warnings: list[str] = []
    try:
        hello = await _bridge_call("hello", {})
        live_version = str((hello or {}).get("live_version") or "")

        if target == "audio_clip":
            if _live_version_tuple(live_version) < (12, 0, 5):
                return {"ok": False, "error": "unsupported", "hint": "Live before 12.0.5 can't create audio clips from a script; send to a Simpler instead.", "live_version": live_version, "file_path": path}
            if req.track_index is not None:
                ti = int(req.track_index)
            else:
                ti, created = await _voice_track_index(audio=True, name=SAMPLE_TRACK_NAME)
                if created:
                    warnings.append(f"Created audio track '{SAMPLE_TRACK_NAME}' for samples.")
            slot = await _sample_slot(req, ti)
            clip = await _bridge_call("load_audio_clip", {
                "track_index": ti,
                "clip_slot_index": slot,
                "file_path": path,
                "name": label,
                "warping": bool(req.loop),
                "looping": bool(req.loop),
            })
            result = {"ok": True, "method": "audio_clip", "track_index": ti, "clip_slot_index": slot, "clip": clip}
        else:
            source, err = _sample_browser_source(req, path)
            if err:
                return err
            if target == "drum_pad":
                result = await _sample_to_drum_pad(req, source, label, warnings)
                result.update({"live_version": live_version, "warnings": warnings, "file_path": path, "verified": None})
                return result

            # Simpler: loading a sample onto an empty MIDI track makes one; a single note plays it.
            if req.track_index is not None:
                ti = int(req.track_index)
            else:
                ti, _ = await _voice_track_index(audio=False, name=f"{SAMPLE_TRACK_NAME} {label}"[:40])
            err = await _bridge_load_with_retry("load_item_at_path", {"track_index": ti, **{k: v for k, v in source.items() if v}})
            if err:
                code = "sample_not_in_browser" if "not_found" in err else "simpler_load_failed"
                return {"ok": False, "error": code, "detail": err, "track_index": ti, "file_path": path}
            song = await _bridge_call("get_song", {})
            tempo = float((song or {}).get("tempo") or 120.0)
            beats = max(0.25, duration_s * tempo / 60.0) if duration_s else 1.0
            slot = await _sample_slot(req, ti)
            await _bridge_call("write_clip", {
                "track_index": ti,
                "clip_slot_index": slot,
                "length": float(max(4, int(-(-beats // 4)) * 4)),
                "name": label,
                "notes": [{"pitch": 60, "start_time": 0.0, "duration": beats, "velocity": 110}],
            })
            result = {"ok": True, "method": "simpler", "track_index": ti, "clip_slot_index": slot}

        await _make_track_audible(result["track_index"], "sample", warnings)
        result["verified"] = await _verify_voice_plays(result["track_index"], result["clip_slot_index"], duration_s or 1.0, req.verify_timeout_s) if req.fire else None
    except Exception as e:
        return {"ok": False, "error": "bridge_error", "detail": str(e), "file_path": path}

    result.update({"live_version": live_version, "duration_s": round(duration_s, 3) if duration_s else None, "warnings": warnings, "file_path": path})
    return result


@app.get("/tts/recipes")
def list_tts_recipes():
    try:
        with _TTS_RECIPES_LOCK:
            items = _read_tts_recipes()
        trimmed = [
            {
                "id": str(x.get("id")),
                "name": str(x.get("name")),
                "voice": str(x.get("voice")),
                "fx": str(x.get("fx")),
                "text": str(x.get("text")),
                "filename": x.get("filename"),
                "created_at": x.get("created_at"),
            }
            for x in items
            if isinstance(x, dict) and x.get("id")
        ]
        return {"ok": True, "recipes": trimmed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/tts/recipes/get")
def get_tts_recipe(recipe_id: str = Query("")):
    try:
        rid = (recipe_id or "").strip()
        if not rid:
            return {"ok": False, "error": "missing_recipe_id"}
        with _TTS_RECIPES_LOCK:
            items = _read_tts_recipes()
        hit = next((x for x in items if isinstance(x, dict) and str(x.get("id")) == rid), None)
        if not isinstance(hit, dict):
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "recipe": hit}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/tts/recipes/save")
def save_tts_recipe(req: SaveTTSRecipeRequest):
    try:
        name = (req.name or "").strip()
        text = (req.text or "").strip()
        voice = (req.voice or "alloy").strip()
        fx = (req.fx or "clean").strip().lower()

        if not name:
            return {"ok": False, "error": "missing_name"}
        if not text:
            return {"ok": False, "error": "missing_text"}

        rid = _make_recipe_id(name=name, text=text, voice=voice, fx=fx)
        item = {
            "id": rid,
            "name": name,
            "text": text,
            "voice": voice,
            "fx": fx,
            "filename": (req.filename or None),
            "created_at": int(time.time()),
        }

        with _TTS_RECIPES_LOCK:
            items = _read_tts_recipes()
            # Upsert by id
            out: list[dict] = []
            replaced = False
            for x in items:
                if isinstance(x, dict) and str(x.get("id")) == rid:
                    out.append(item)
                    replaced = True
                else:
                    out.append(x)
            if not replaced:
                out.insert(0, item)
            _write_tts_recipes(out[:200])

        return {"ok": True, "recipe": item}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/audio/process")
def process_audio(req: ProcessAudioRequest):
    try:
        src = (req.source_url or "").strip()
        if not src:
            return {"ok": False, "error": "missing_source_url"}

        # Only allow local static URLs we serve.
        if not (src.startswith("/recordings/") or src.startswith("/audio_files/")):
            return {"ok": False, "error": "unsupported_source_url", "hint": "Use a /recordings/* or /audio_files/* URL"}

        if src.startswith("/recordings/"):
            src_path = os.path.join(RECORDINGS_DIR, os.path.basename(src))
        else:
            src_path = os.path.join(AUDIO_DIR, os.path.basename(src))

        if not os.path.exists(src_path):
            return {"ok": False, "error": "not_found", "path": src_path}

        # Current FX pipeline supports WAV only.
        if not src_path.lower().endswith(".wav"):
            return {"ok": False, "error": "unsupported_format", "hint": "FX processing currently supports WAV only."}

        with open(src_path, "rb") as f:
            wav_bytes = f.read()
        try:
            samples, sr, ch = _wav_read_pcm(wav_bytes)
        except Exception as e:
            return {"ok": False, "error": str(e), "hint": "Source file is not a valid 16-bit PCM WAV."}
        fx = re.sub(r"[^a-z0-9_-]", "_", (req.fx or "clean").strip().lower()) or "clean"
        processed = _fx_process_pcm(samples, fx=fx, sample_rate=sr, channels=ch)
        out_wav = _wav_from_pcm(processed, sample_rate=sr, channels=ch)

        label = re.sub(r"[^a-zA-Z0-9._-]", "_", (req.label or "").strip())
        base = os.path.splitext(os.path.basename(src_path))[0]
        ts = int(time.time() * 1000)
        if label:
            out_name = f"fx_{ts}_{base}_{label}_{fx}.wav"
        else:
            out_name = f"fx_{ts}_{base}_{fx}.wav"
        out_path = os.path.join(AUDIO_DIR, out_name)
        with open(out_path, "wb") as f:
            f.write(out_wav)

        return {"ok": True, "filename": out_name, "fx": fx, "url": f"/audio_files/{out_name}", "file_path": out_path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _mono_rms_envelope(samples: list[int], *, sample_rate: int, channels: int, frame: int = 1024, hop: int = 512) -> tuple[list[float], float]:
    ch = int(channels) if int(channels) > 0 else 1
    if ch < 1:
        ch = 1
    # Convert interleaved int16 to mono float [-1,1]
    frames = len(samples) // ch
    mono = [0.0] * frames
    for i in range(frames):
        acc = 0
        base = i * ch
        for c in range(ch):
            acc += samples[base + c]
        mono[i] = (acc / ch) / 32768.0

    env: list[float] = []
    t_step = float(hop) / float(max(1, sample_rate))
    i = 0
    while i + frame <= frames:
        s = 0.0
        for j in range(frame):
            v = mono[i + j]
            s += v * v
        env.append(math.sqrt(s / frame))
        i += hop
    return env, t_step


def _pick_onsets_from_env(env: list[float], *, t_step: float) -> list[float]:
    if not env:
        return []

    # Basic threshold based on median.
    sorted_env = sorted(env)
    med = sorted_env[len(sorted_env) // 2]
    thr = max(med * 2.5, 0.02)

    # Smooth slightly (moving average over 3 frames)
    smooth: list[float] = []
    for i in range(len(env)):
        a = env[i - 1] if i - 1 >= 0 else env[i]
        b = env[i]
        c = env[i + 1] if i + 1 < len(env) else env[i]
        smooth.append((a + b + c) / 3.0)

    onsets: list[float] = []
    min_gap_s = 0.08
    last_t = -999.0
    for i in range(1, len(smooth) - 1):
        v = smooth[i]
        if v < thr:
            continue
        if v >= smooth[i - 1] and v >= smooth[i + 1]:
            t = i * t_step
            if t - last_t >= min_gap_s:
                onsets.append(t)
                last_t = t
    return onsets


def _voice_midi_target(track_index: int, clip_slot_index: int | None, base_pitch: int | None) -> dict:
    """Check a voice->MIDI destination before writing: it must exist, be MIDI, and have a sounding instrument.

    Without PulseBridge nothing can be checked, so the request's values are used as given.
    """
    warnings: list[str] = []
    if not BRIDGE.connected:
        return {
            "ok": True,
            "track_index": track_index,
            "track_name": None,
            "clip_slot_index": int(clip_slot_index) if clip_slot_index is not None else 0,
            "base_pitch": int(base_pitch) if base_pitch is not None else 60,
            "instrument": None,
            "warnings": ["PulseBridge not connected: the target track wasn't checked."],
        }

    try:
        info = BRIDGE.request("get_track", {"track_index": int(track_index)}, timeout_s=3.0)
    except Exception as e:
        if "out_of_range" in str(e):
            return {"ok": False, "error": "no_such_track", "track_index": track_index}
        return {"ok": False, "error": "bridge_error", "detail": str(e)}
    if not info.get("is_midi"):
        return {"ok": False, "error": "not_midi_track", "track_index": track_index, "track_name": info.get("name"),
                "hint": "Voice to MIDI writes notes; pick a MIDI track with an instrument."}
    playable, _ = _track_sound_state(info)
    if not playable:
        return {"ok": False, "error": "no_instrument", "track_index": track_index, "track_name": info.get("name"),
                "hint": "This track has no instrument (or only an empty Drum Rack), so the notes would be silent. Load a kit or synth first."}
    instrument = playable[0]

    if base_pitch is None:
        base_pitch = 60
        if instrument.get("is_drum_rack"):
            try:
                pads = (BRIDGE.request("get_drum_pads", {"track_index": int(track_index)}, timeout_s=3.0) or {}).get("pads") or []
                if pads:
                    base_pitch = int(pads[0]["note"])
            except Exception:
                warnings.append("Couldn't read the Drum Rack pads; using note 60.")

    if clip_slot_index is None:
        try:
            free = BRIDGE.request("find_free_slot", {"track_index": int(track_index)}, timeout_s=3.0) or {}
            clip_slot_index = int(free.get("slot", 0))
        except Exception:
            clip_slot_index = 0
            warnings.append("Couldn't find a free slot; wrote to slot 1.")

    return {
        "ok": True,
        "track_index": int(track_index),
        "track_name": info.get("name"),
        "clip_slot_index": int(clip_slot_index),
        "base_pitch": int(base_pitch),
        "instrument": instrument.get("name"),
        "warnings": warnings,
    }


@app.post("/voice_to_midi/apply")
def voice_to_midi_apply(req: VoiceToMidiRequest):
    try:
        src = (req.source_url or "").strip()
        if not src:
            return {"ok": False, "error": "missing_source_url"}
        if not (src.startswith("/recordings/") or src.startswith("/audio_files/")):
            return {"ok": False, "error": "unsupported_source_url"}

        if src.startswith("/recordings/"):
            src_path = os.path.join(RECORDINGS_DIR, os.path.basename(src))
        else:
            src_path = os.path.join(AUDIO_DIR, os.path.basename(src))
        if not os.path.exists(src_path):
            return {"ok": False, "error": "not_found", "path": src_path}
        if not src_path.lower().endswith(".wav"):
            return {"ok": False, "error": "unsupported_format", "hint": "voice_to_midi currently supports WAV only."}

        with open(src_path, "rb") as f:
            wav_bytes = f.read()
        try:
            samples, sr, ch = _wav_read_pcm(wav_bytes)
        except Exception as e:
            return {"ok": False, "error": str(e), "hint": "Source file is not a valid 16-bit PCM WAV."}

        bpm = float(req.bpm)
        if bpm <= 20:
            bpm = 20.0
        if bpm > 250:
            bpm = 250.0

        bars = int(req.bars)
        if bars < 1:
            bars = 1
        if bars > 8:
            bars = 8

        clip_len_beats = float(bars) * 4.0

        target = _voice_midi_target(int(req.track_index), req.clip_slot_index, req.base_pitch)
        if not target.get("ok"):
            return target

        env, t_step = _mono_rms_envelope(samples, sample_rate=sr, channels=ch)
        onsets_s = _pick_onsets_from_env(env, t_step=t_step)

        # Convert onsets to beats and quantize to 1/16
        notes: list[tuple[int, float, float, int]] = []
        base_pitch = int(target["base_pitch"])
        vel = int(req.velocity)
        if vel < 1:
            vel = 1
        if vel > 127:
            vel = 127

        step = 0.25  # 1/16 note in beats
        for t in onsets_s:
            beat = (t * bpm) / 60.0
            if beat < 0.0:
                continue
            if beat >= clip_len_beats:
                continue
            q = round(beat / step) * step
            if q < 0.0:
                q = 0.0
            if q >= clip_len_beats:
                continue
            notes.append((base_pitch, float(q), float(step), vel))

        # De-dupe start times
        seen = set()
        uniq: list[tuple[int, float, float, int]] = []
        for p, st, dur, v in notes:
            key = int(round(st * 1000))
            if key in seen:
                continue
            seen.add(key)
            uniq.append((p, st, dur, v))

        track = int(target["track_index"])
        slot = int(target["clip_slot_index"])

        # Create clip and add notes
        ctrl.create_clip(track, slot, length_beats=clip_len_beats)
        if uniq:
            ctrl.add_notes(track, slot, uniq)

        if bool(req.fire):
            ctrl.fire_clip(track, slot)

        return {
            "ok": True,
            "source_url": src,
            "source_path": src_path,
            "track_index": track,
            "track_name": target.get("track_name"),
            "clip_slot_index": slot,
            "base_pitch": base_pitch,
            "instrument": target.get("instrument"),
            "warnings": target.get("warnings", []),
            "bpm": bpm,
            "bars": bars,
            "notes": len(uniq),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _load_styles_from_disk():
    if not os.path.exists(KNOWLEDGE_FILE):
        print(f"WARNING: Knowledge file not found at {KNOWLEDGE_FILE}")
        return {}, {}, {}, {}, {}, {}, {}

    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load knowledge file: {e}")
        return {}, {}, {}, {}, {}, {}, {}
        
    raw_styles = data.get("styles", {})
    bass_defaults = data.get("bass_defaults", {})
    instrument_catalog = data.get("instrument_catalog", {})
    
    bass_profiles = {}
    harmony_profiles = {}
    style_config = {}
    recommendations = {}
    style_to_slot = {}
    
    for s, info in raw_styles.items():
        # Bass Profile
        if "bass_profile" in info:
            bass_profiles[s] = info["bass_profile"]

        # Harmony Profile
        if "harmony_profile" in info:
            harmony_profiles[s] = info["harmony_profile"]
            
        # Style Config
        style_config[s] = {
            "tempo": info.get("tempo", 120.0),
            "clip_slot": info.get("clip_slot", 0),
            "kit": info.get("kit", "Core Kit"),
            "swing": info.get("swing"),
            "groove": info.get("groove") if isinstance(info.get("groove"), dict) else {},
            "drum_bus": info.get("drum_bus") if isinstance(info.get("drum_bus"), dict) else None,
        }

        # Recommendations
        if "recommendations" in info:
            recommendations[s] = info["recommendations"]
            
        # Slot mapping
        if "clip_slot" in info:
            style_to_slot[s] = info["clip_slot"]

    return bass_defaults, bass_profiles, harmony_profiles, style_config, recommendations, style_to_slot, instrument_catalog


BASS_STYLE_DEFAULTS, BASS_STYLE_PROFILE, HARMONY_STYLE_PROFILE, STYLE_CONFIG, STYLE_RECOMMENDATIONS, VOLCA_STYLE_TO_CLIP_SLOT, INSTRUMENT_CATALOG = _load_styles_from_disk()

DRUM_KIT_TEST_NAME = "909 Core Kit"
BASS_DEVICE_TEST_NAME = "Basic 303 Bass"


def _style_name_blob(display_name: str, description: str, key: str) -> str:
    return f"{display_name or ''} {description or ''} {key or ''}".lower()


def _is_hard_dance_name(display_name: str, description: str, key: str) -> bool:
    b = _style_name_blob(display_name, description, key)
    if "liquid" in b and ("dnb" in b or "drum" in b):
        return False
    if any(w in b for w in ("gabber", "gabba", "hakkuh", "rotterdam", "frenchcore", "speedcore")):
        return True
    if "uptempo" in b and "hardcore" in b:
        return True
    if "hardcore" in b and "liquid" not in b and "melodic" not in b:
        return True
    if "happy hardcore" in b or "happycore" in b:
        return True
    return False


def _is_dnb_name(display_name: str, description: str, key: str) -> bool:
    b = _style_name_blob(display_name, description, key)
    if "gabba" in b or "gabber" in b:
        return False
    return bool(re.search(r"\bdnb\b|drum\s+and\s+bass|drum\s*&\s*bass|\bjungle\b", b))


def _style_cues_for_generation(style: str) -> str:
    """Extra producer cues from knowledge base — passed to OpenAI drum/bass prompts."""
    style_l = (style or "").strip().lower()
    parts: list[str] = []
    rec = STYLE_RECOMMENDATIONS.get(style_l) if isinstance(STYLE_RECOMMENDATIONS, dict) else None
    if isinstance(rec, dict):
        d = rec.get("drums")
        b = rec.get("bass")
        if d:
            parts.append(f"Drum / groove cues (follow closely): {d}")
    groove = _groove_cues(style_l)
    if groove:
        parts.append(groove)
    if isinstance(rec, dict):
        if b:
            parts.append(f"Bass / low-end cues (follow closely): {b}")
    bp = BASS_STYLE_PROFILE.get(style_l) if isinstance(BASS_STYLE_PROFILE, dict) else None
    if isinstance(bp, dict):
        ex = bp.get("explain")
        if ex:
            parts.append(f"Style bass profile intent: {ex}")
    cfg = STYLE_CONFIG.get(style_l) if isinstance(STYLE_CONFIG, dict) else None
    if isinstance(cfg, dict):
        try:
            t = float(cfg.get("tempo", 0) or 0)
            if t > 0:
                parts.append(f"Reference tempo from style definition: ~{t:.0f} BPM (inform energy; Live tempo may differ).")
        except Exception:
            pass
    return "\n".join(parts)


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


def _await_osc(address: str, *, since: float, timeout_s: float) -> tuple[float, tuple] | None:
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        got = OSC_STATE.get(address)
        if got is not None:
            ts, args = got
            if ts >= since:
                return ts, args
        time.sleep(0.01)
    return None


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

MACRO_VALUE_MAX = 127.0

# The UI is served by this server, so it never needs cross-origin access.
# Reject requests from other sites (Origin check) and DNS-rebinding hosts (Host
# check); CORS alone would not stop cross-site form posts such as uploads.
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}


def _hostname(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


@app.middleware("http")
async def _local_only_guard(request, call_next):
    if _hostname(request.headers.get("host", "")) not in _LOCAL_HOSTNAMES:
        return JSONResponse({"ok": False, "error": "forbidden_host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None:
        origin_host = _hostname(urllib.parse.urlsplit(origin).netloc)
        if origin_host not in _LOCAL_HOSTNAMES:
            return JSONResponse({"ok": False, "error": "forbidden_origin"}, status_code=403)
    return await call_next(request)


# PulseBridge (our Remote Script) is preferred; AbletonOSC is the fallback transport.
BRIDGE = PulseBridgeClient().start()
ctrl = LiveLink(BRIDGE)


def _live_query_unavailable() -> str | None:
    """Reason Live can't be queried right now, or None if the bridge or OSC listener works."""
    if BRIDGE.connected or OSC_LISTENER_ERROR is None:
        return None
    return OSC_LISTENER_ERROR


class _BrowserIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.data: dict | None = None

    def set(self, data: dict):
        with self.lock:
            self.data = data

    def get(self) -> dict | None:
        with self.lock:
            return self.data


BROWSER_INDEX = _BrowserIndex()


class TempoRequest(BaseModel):
    bpm: float


class FireClipRequest(BaseModel):
    track_index: int
    clip_slot_index: int
    fire_once: bool = False
    once_bars: int = 1


class FireSceneRequest(BaseModel):
    scene_index: int


class EnsureScenesRequest(BaseModel):
    min_scenes: int


class LabelScenesRequest(BaseModel):
    start_index: int = 0
    count: int = 8
    prefix: str = "PS"
    ensure_min_scenes: bool = True


class EnsureTracksRequest(BaseModel):
    min_tracks: int = Field(default_factory=_pulse_required_track_count)
    insert_at_start: bool = False
    prefix: str = "PS-TRK"


class LabelTracksRequest(BaseModel):
    start_index: int = 0
    count: int = Field(default_factory=_pulse_required_track_count)
    prefix: str = "PS-TRK"
    ensure_min_tracks: bool = False


class FadeOutRequest(BaseModel):
    duration_ms: int = 4000
    target_tracks: list[int] = Field(default_factory=_pulse_default_track_indices)


class WashDropRequest(BaseModel):
    duration_bars: int = 4


class LaunchStyleRequest(BaseModel):
    style: str


class LoadDeviceRequest(BaseModel):
    track_index: int
    device_name: str


class MCPBrowserTreeRequest(BaseModel):
    category_type: str = "all"


class MCPBrowserItemsRequest(BaseModel):
    path: str


class MCPLoadBrowserItemRequest(BaseModel):
    track_index: int
    item_uri: str


class MCPRebuildBrowserIndexRequest(BaseModel):
    category_type: str = "all"
    max_paths: int = 2000
    max_items: int = 20000
    include_items: bool = False


class MCPSearchBrowserIndexRequest(BaseModel):
    q: str
    limit: int = 50


class RecommendFromBrowserIndexRequest(BaseModel):
    style: str
    role: str
    track_index: int
    q: str | None = None
    limit: int = 80
    prompt: str | None = None
    temperature: float = 0.2


class RecommendInstrumentRequest(BaseModel):
    style: str
    role: str
    candidates: list[str]
    prompt: str | None = None
    temperature: float = 0.2


class RecommendAndApplyInstrumentRequest(BaseModel):
    style: str
    role: str
    track_index: int
    candidates: list[str]
    prompt: str | None = None
    temperature: float = 0.2


class SetDeviceParamRequest(BaseModel):
    track_index: int
    device_index: int
    param_index: int
    value: float


class SetMacroRequest(BaseModel):
    macro: int
    value: float


class GenerateAIPairRequest(BaseModel):
    style: str
    bars: int = 1
    clip_bars: int | None = None
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


class GenerateFullTrackRequest(BaseModel):
    style: str
    start_slot_index: int = 0
    bars_per_scene: int = 1
    clip_bars: int = 8
    temperature: float = 0.7
    apply_instruments: bool = True
    replace_instruments: bool = False  # default: keep instruments already on the Pulse tracks
    instrument_candidate_limit: int = 120  # legacy picker only (PulseBridge not connected)
    instrument_prompt: str | None = None
    prompt: str | None = None


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


class ClearTrackRangeRequest(BaseModel):
    start_slot_index: int = 0
    scene_count: int = 6
    tracks: list[int] | None = None
    stop_transport: bool = True


class NextFreeClipSlotRequest(BaseModel):
    track_index: int
    start_slot_index: int = 0
    max_slots: int = 256


@app.post("/track/clear_range")
def clear_track_range(req: ClearTrackRangeRequest):
    try:
        if bool(req.stop_transport):
            try:
                ctrl.stop()
            except Exception:
                pass

        start = int(req.start_slot_index)
        if start < 0:
            start = 0
        count = int(req.scene_count)
        if count < 1:
            count = 1
        if count > 64:
            count = 64

        tracks = req.tracks if isinstance(req.tracks, list) and req.tracks else _pulse_default_track_indices()

        cleared = 0
        for slot in range(start, start + count):
            for trk in tracks:
                try:
                    ctrl.send("/live/clip_slot/delete_clip", [int(trk), int(slot)])
                    cleared += 1
                    time.sleep(0.01)
                except Exception:
                    pass

        return {"ok": True, "start_slot_index": start, "scene_count": count, "tracks": tracks, "cleared": cleared}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/clip_slots/next_free")
def next_free_clip_slot(req: NextFreeClipSlotRequest):
    try:
        track = int(req.track_index)
        start = int(req.start_slot_index)
        if start < 0:
            start = 0

        max_slots = int(req.max_slots)
        if max_slots < 1:
            max_slots = 1
        if max_slots > 2048:
            max_slots = 2048

        if BRIDGE.connected:
            try:
                res = BRIDGE.request(
                    "find_free_slot",
                    {"track_index": track, "start_slot_index": start, "max_slots": max_slots},
                    timeout_s=2.0,
                )
                return {"ok": True, "track_index": track, "slot": int(res["slot"])}
            except BridgeError as e:
                if "no_free_slot_found" in str(e):
                    return {"ok": False, "error": "no_free_slot_found", "track_index": track, "start_slot_index": start, "max_slots": max_slots}

        # AbletonOSC provides: /live/clip_slot/get/has_clip [track, slot]
        # and replies (track, slot, has_clip), so the value is the last arg.
        for slot in range(start, start + max_slots):
            res = _query_with_timeout("/live/clip_slot/get/has_clip", [track, int(slot)], timeout_s=0.8)
            if not isinstance(res, dict) or not res.get("ok"):
                # If we can't query reliably, fail soft by returning current slot.
                return {"ok": True, "track_index": track, "slot": int(slot), "note": "query_failed_fallback"}

            args = res.get("args")
            has_clip = None
            try:
                if isinstance(args, tuple) and len(args) > 0:
                    has_clip = bool(int(args[-1]))
            except Exception:
                has_clip = None

            if has_clip is False:
                return {"ok": True, "track_index": track, "slot": int(slot)}

        return {"ok": False, "error": "no_free_slot_found", "track_index": track, "start_slot_index": start, "max_slots": max_slots}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/clips/find_free_slot")
def clips_find_free_slot(track_index: int = 0, start_slot: int = 0, max_slots: int = 256):
    # Backwards-compatible route (older UI used GET /clips/find_free_slot?track_index=0&start_slot=0)
    return next_free_clip_slot(
        NextFreeClipSlotRequest(
            track_index=int(track_index),
            start_slot_index=int(start_slot),
            max_slots=int(max_slots),
        )
    )


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/knowledge/styles")
def get_knowledge_styles():
    if os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _slugify_style_key(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[\s-]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def _next_clip_slot(styles: dict) -> int:
    if not isinstance(styles, dict) or not styles:
        return 0
    slots: list[int] = []
    for _, info in styles.items():
        if not isinstance(info, dict):
            continue
        try:
            slots.append(int(info.get("clip_slot", 0)))
        except Exception:
            continue
    return (max(slots) + 1) if slots else 0


def _weighted_choice(a, b, t: float):
    try:
        return (float(a) * (1.0 - t)) + (float(b) * t)
    except Exception:
        return a if t < 0.5 else b


def _infer_tempo(description: str, fallback: float):
    desc = (description or "").lower()
    m = re.search(r"\b(\d{2,3}(?:\.\d+)?)\s*bpm\b", desc)
    if m:
        try:
            bpm = float(m.group(1))
            if 40.0 <= bpm <= 240.0:
                return bpm
        except Exception:
            pass

    if any(k in desc for k in ["speedcore", "frenchcore", "uptempo hardcore", "uptempo"]):
        return min(220.0, max(200.0, fallback + 40.0))
    if any(k in desc for k in ["gabber", "gabba", "hakkuh", "rotterdam hardcore"]):
        return min(210.0, max(185.0, fallback + 45.0))
    if "hardcore" in desc and "liquid" not in desc and "melodic hardcore" not in desc:
        return min(200.0, max(150.0, fallback + 25.0))
    if any(k in desc for k in ["drum and bass", "drum & bass", " dnb", "dnb ", "jungle"]):
        return min(180.0, max(165.0, fallback - 5.0 if fallback > 150 else 174.0))

    # Very light heuristic
    if any(k in desc for k in ["fast", "hard", "peak time", "rave"]):
        return min(180.0, max(128.0, fallback + 10.0))
    if any(k in desc for k in ["slow", "downtempo", "chill"]):
        return max(80.0, min(120.0, fallback - 15.0))
    return fallback


def _extract_first_float(val, fallback: float | None = None) -> float | None:
    try:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return fallback
            return float(s)
        if isinstance(val, (list, tuple)):
            for item in val:
                f = _extract_first_float(item, None)
                if f is not None:
                    return f
        if isinstance(val, dict):
            for _, item in val.items():
                f = _extract_first_float(item, None)
                if f is not None:
                    return f
    except Exception:
        return fallback
    return fallback


def _current_tempo_fallback(default_bpm: float = 140.0) -> float:
    tempo_raw = _query_with_timeout("/live/song/get/tempo", [])
    bpm = _extract_first_float(tempo_raw, None)
    if bpm is None:
        return float(default_bpm)
    if bpm < 20.0:
        return 20.0
    if bpm > 300.0:
        return 300.0
    return float(bpm)


def _schedule_stop_clip_after_bars(*, track_index: int, clip_slot_index: int, bars: int, bpm_default: float = 140.0):
    b = int(bars)
    if b < 1:
        b = 1
    if b > 64:
        b = 64

    bpm = _current_tempo_fallback(float(bpm_default))
    seconds = (float(b) * 4.0 * 60.0) / max(1.0, float(bpm))

    def _worker():
        try:
            # Sleep for the intended musical duration, then stop the clip slot.
            time.sleep(max(0.05, float(seconds)))
            ctrl.stop_clip(int(track_index), int(clip_slot_index))
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _infer_bass_rhythm(description: str, fallback: str):
    desc = (description or "").lower()
    if any(k in desc for k in ["gabber", "gabba", "hardcore", "hakkuh", "frenchcore", "speedcore"]):
        return "steady_16"
    if any(k in desc for k in ["drum and bass", "drum & bass", "dnb", "jungle"]):
        return "syncopated_16"
    if "swing" in desc or "shuffle" in desc:
        return "swingy_16"
    if "syncop" in desc:
        return "syncopated_16"
    if "offbeat" in desc:
        return "offbeat_8"
    if "steady" in desc or "rolling" in desc:
        return "steady_16"
    return fallback


def _infer_density(description: str, fallback: str):
    desc = (description or "").lower()
    if any(k in desc for k in ["gabber", "gabba", "hardcore", "frenchcore", "speedcore", "hakkuh"]):
        return "high"
    if any(k in desc for k in ["minimal", "sparse", "space"]):
        return "low"
    if any(k in desc for k in ["busy", "dense", "complex", "intricate"]):
        return "high"
    return fallback


def _infer_scale(description: str, fallback: str):
    desc = (description or "").lower()
    if any(k in desc for k in ["major", "happy", "bright"]):
        return "major"
    if any(k in desc for k in ["minor", "dark", "moody"]):
        return "minor"
    # Balkan-influenced usually reads "minor" in this simplified system.
    if "balkan" in desc or "gypsy" in desc:
        return "minor"
    return fallback


def _apply_subgenre_heuristics_to_style_object(
    obj: dict,
    *,
    display_name: str,
    description: str,
    key: str,
) -> dict:
    """Nudge tempo, bass profile, harmony, and recommendations toward real genre norms (esp. hard dance)."""
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    hard = _is_hard_dance_name(display_name, description, key)
    dnb = _is_dnb_name(display_name, description, key) and not hard

    try:
        t0 = float(out.get("tempo", 140.0))
    except Exception:
        t0 = 140.0

    if hard:
        floor = 200.0 if any(w in _style_name_blob(display_name, description, key) for w in ("speedcore", "frenchcore")) else 185.0
        out["tempo"] = min(220.0, max(floor, t0))
        bp = dict(out.get("bass_profile") or {})
        bp["density"] = "high"
        bp["rhythm"] = "steady_16"
        bp["note_pool"] = "root_fifth_octave"
        bp["explain"] = (
            "Hard dance / gabber: bass is secondary to the kick—short sub tails, roots and fifths only, "
            "no melodic wandering; leave headroom for distorted kick transients."
        )
        out["bass_profile"] = bp
        rec = dict(out.get("recommendations") or {})
        rec["drums"] = (
            "Hard distorted 909-style kick as the main musical event; relentless 4/4 or classic gabber offbeat stomp; "
            "fast 16th closed hats; aggressive snare/clap; industrial percussion—not house or techno swing."
        )
        rec["bass"] = (
            "Minimal monotone sub under kicks; distorted or saturated; very few pitches—rumble on root, "
            "no jazzy walks or busy acid lines."
        )
        out["recommendations"] = rec
        hp = dict(out.get("harmony_profile") or {})
        hp["progressions"] = [
            {"name": "tonic-stomp", "degrees": [1, 1, 1, 1]},
            {"name": "i-bVII-stomp", "degrees": [1, 7, 1, 7]},
        ]
        out["harmony_profile"] = hp
    elif dnb:
        out["tempo"] = min(180.0, max(165.0, t0 if 165.0 <= t0 <= 180.0 else 174.0))
        bp = dict(out.get("bass_profile") or {})
        bp["density"] = "high"
        bp["rhythm"] = "syncopated_16"
        ex = str(bp.get("explain") or "").strip()
        bp["explain"] = ex or "DnB: rolling syncopated bass weight; anchor tonic; work with breakbeat drums."
        out["bass_profile"] = bp
        rec = dict(out.get("recommendations") or {})
        if not str(rec.get("drums") or "").strip():
            rec["drums"] = "Breakbeat-influenced kicks; snare placements typical of DnB; space for sub."
        if not str(rec.get("bass") or "").strip():
            rec["bass"] = "Reese or sub bass; syncopation; keep harmonic movement minimal."
        out["recommendations"] = rec

    return out


def _openai_extra_instructions_for_new_style(display_name: str, description: str, key: str) -> str:
    parts: list[str] = [
        "The recommendations.drums and recommendations.bass strings are used as explicit cues for later drum/bass AI steps—"
        "write concrete production detail (kick type, distortion, hat rate, snare character, sub vs mid bass), not vague genre labels.",
        "bass_profile.explain must describe mix role and rhythmic intent for this genre (not generic text).",
    ]
    if _is_hard_dance_name(display_name, description, key):
        parts.append(
            "Hard dance / gabber / hardcore: prefer tempo 185–220 unless the user gave a different BPM in the description. "
            "Emphasize distorted 909-style kicks, fast 16th hats, minimal harmony, and very simple bass that supports kicks."
        )
    elif _is_dnb_name(display_name, description, key):
        parts.append(
            "Drum & bass / jungle: prefer tempo ~170–180 unless the user specified otherwise. "
            "Emphasize breakbeat-leaning drums and syncopated, sub-heavy bass."
        )
    return "\n".join(parts)


_BASS_SHAPE_KEYS = ("allowed_steps", "fill_steps", "avoid_kick", "accent_steps", "octave_jump_steps", "density_keep")
_HARMONY_FEEL_KEYS = ("voicing", "register", "rhythm", "harmonic_rhythm", "velocity")


def _inherit_feel_from_bases(obj: dict, base_a: dict | None, base_b: dict | None) -> dict:
    """Fill a new style's groove, bass shaping and chord treatment from its base styles when it has none."""
    if not isinstance(obj, dict):
        return obj
    bases = [b for b in (base_a, base_b) if isinstance(b, dict)]
    out = dict(obj)
    if not isinstance(out.get("groove"), dict):
        g = next((b["groove"] for b in bases if isinstance(b.get("groove"), dict)), None)
        if g:
            out["groove"] = json.loads(json.dumps(g))
    for section, keys in (("bass_profile", _BASS_SHAPE_KEYS), ("harmony_profile", _HARMONY_FEEL_KEYS)):
        prof = dict(out.get(section) or {})
        for k in keys:
            if k in prof:
                continue
            for b in bases:
                src = b.get(section)
                if isinstance(src, dict) and k in src:
                    prof[k] = src[k]
                    break
        out[section] = prof
    return out


def _generate_style_object(*, key: str, display_name: str, description: str, base_a: dict | None, base_b: dict | None, clip_slot: int):
    base_a = base_a if isinstance(base_a, dict) else {}
    base_b = base_b if isinstance(base_b, dict) else {}

    # Blend tempo
    tempo_a = float(base_a.get("tempo", 128.0))
    tempo_b = float(base_b.get("tempo", tempo_a))
    tempo_blend = _weighted_choice(tempo_a, tempo_b, 0.5)
    tempo = float(_infer_tempo(description, tempo_blend))

    kit = str(base_a.get("kit") or base_b.get("kit") or "909 Core Kit")

    bp_a = base_a.get("bass_profile") if isinstance(base_a.get("bass_profile"), dict) else {}
    bp_b = base_b.get("bass_profile") if isinstance(base_b.get("bass_profile"), dict) else {}

    scale = _infer_scale(description, str(bp_a.get("scale") or bp_b.get("scale") or "minor"))
    density = _infer_density(description, str(bp_a.get("density") or bp_b.get("density") or "medium"))
    rhythm = _infer_bass_rhythm(description, str(bp_a.get("rhythm") or bp_b.get("rhythm") or "straight_16"))
    octave_range = bp_a.get("octave_range") if isinstance(bp_a.get("octave_range"), list) else (bp_b.get("octave_range") if isinstance(bp_b.get("octave_range"), list) else [2, 3])

    note_pool = str(bp_a.get("note_pool") or bp_b.get("note_pool") or "root_fifth_flat7_octave")
    if "balkan" in (description or "").lower() and "minor2" not in note_pool:
        # A tiny nod to balkan-ish flavour (phrygian-ish colour) without changing harmony engine.
        note_pool = "root_minor2_" + note_pool

    hp_a = base_a.get("harmony_profile") if isinstance(base_a.get("harmony_profile"), dict) else {}
    hp_b = base_b.get("harmony_profile") if isinstance(base_b.get("harmony_profile"), dict) else {}
    harmony_profile = {
        "harmonic_rhythm": str(hp_a.get("harmonic_rhythm") or hp_b.get("harmonic_rhythm") or "one_chord_per_bar"),
        "progressions": hp_a.get("progressions") if isinstance(hp_a.get("progressions"), list) and hp_a.get("progressions") else (hp_b.get("progressions") if isinstance(hp_b.get("progressions"), list) else []),
        "voicing": str(hp_a.get("voicing") or hp_b.get("voicing") or "triads"),
    }
    if not harmony_profile["progressions"]:
        harmony_profile["progressions"] = [
            {"name": "i-VI-III-VII", "degrees": [1, 6, 3, 7]},
            {"name": "i-iv-VII", "degrees": [1, 4, 7]},
        ]

    bass_profile = {
        "scale": scale,
        "octave_range": octave_range,
        "density": density,
        "rhythm": rhythm,
        "note_pool": note_pool,
        "explain": f"Generated style '{display_name}'. Base blend + user description: {description}",
    }

    recommendations = {
        "drums": str((base_a.get("recommendations") or {}).get("drums") or (base_b.get("recommendations") or {}).get("drums") or "909/808 style kit"),
        "bass": str((base_a.get("recommendations") or {}).get("bass") or (base_b.get("recommendations") or {}).get("bass") or "mono bass / simple sub"),
    }

    return {
        "tempo": tempo,
        "clip_slot": int(clip_slot),
        "kit": kit,
        "harmony_profile": harmony_profile,
        "bass_profile": bass_profile,
        "recommendations": recommendations,
    }


class GenerateStyleFromDescriptionRequest(BaseModel):
    name: str
    description: str | None = None
    base_style_a: str | None = None
    base_style_b: str | None = None


def _validate_style_object(obj: dict) -> tuple[dict | None, dict]:
    if not isinstance(obj, dict):
        return None, {"ok": False, "error": "style_not_object"}

    # Required keys
    for k in ["tempo", "clip_slot", "kit", "harmony_profile", "bass_profile", "recommendations"]:
        if k not in obj:
            return None, {"ok": False, "error": "style_missing_key", "key": k}

    # tempo
    try:
        tempo = float(obj.get("tempo"))
    except Exception:
        return None, {"ok": False, "error": "bad_tempo"}
    if tempo < 40.0 or tempo > 240.0:
        return None, {"ok": False, "error": "tempo_out_of_range"}

    # clip_slot
    try:
        clip_slot = int(obj.get("clip_slot"))
    except Exception:
        return None, {"ok": False, "error": "bad_clip_slot"}
    if clip_slot < 0 or clip_slot > 2048:
        return None, {"ok": False, "error": "clip_slot_out_of_range"}

    kit = str(obj.get("kit") or "")
    if not kit:
        return None, {"ok": False, "error": "missing_kit"}

    hp = obj.get("harmony_profile")
    if not isinstance(hp, dict):
        return None, {"ok": False, "error": "bad_harmony_profile"}
    if "progressions" not in hp or not isinstance(hp.get("progressions"), list):
        return None, {"ok": False, "error": "bad_harmony_progressions"}

    bp = obj.get("bass_profile")
    if not isinstance(bp, dict):
        return None, {"ok": False, "error": "bad_bass_profile"}
    for k in ["scale", "octave_range", "density", "rhythm", "note_pool", "explain"]:
        if k not in bp:
            return None, {"ok": False, "error": "bass_profile_missing_key", "key": k}
    if not isinstance(bp.get("octave_range"), list) or len(bp.get("octave_range")) != 2:
        return None, {"ok": False, "error": "bad_octave_range"}

    rec = obj.get("recommendations")
    if not isinstance(rec, dict):
        return None, {"ok": False, "error": "bad_recommendations"}
    if "drums" not in rec or "bass" not in rec:
        return None, {"ok": False, "error": "recommendations_missing"}

    # Normalize
    out = dict(obj)
    out["tempo"] = tempo
    out["clip_slot"] = clip_slot
    out["kit"] = kit
    out["harmony_profile"] = hp
    out["bass_profile"] = bp
    out["recommendations"] = rec
    return out, {"ok": True}


async def _openai_generate_style_from_description(
    *,
    key: str,
    display_name: str,
    description: str,
    base_a_key: str | None,
    base_b_key: str | None,
    base_a: dict | None,
    base_b: dict | None,
    clip_slot: int,
):
    # Provide a strict schema as a template (helps with valid JSON)
    schema = {
        "tempo": 135.0,
        "clip_slot": int(clip_slot),
        "kit": "909 Core Kit",
        "harmony_profile": {
            "harmonic_rhythm": "one_chord_per_bar",
            "progressions": [
                {"name": "i-VI-III-VII", "degrees": [1, 6, 3, 7]},
                {"name": "i-iv-VII", "degrees": [1, 4, 7]},
            ],
            "voicing": "triads",
        },
        "bass_profile": {
            "scale": "minor",
            "octave_range": [2, 3],
            "density": "medium",
            "rhythm": "straight_16",
            "note_pool": "root_fifth_flat7_octave",
            "explain": "...",
        },
        "recommendations": {
            "drums": "...",
            "bass": "...",
        },
    }

    system = (
        "You are an expert electronic music producer and music theorist. "
        "Generate a musical style definition as STRICT JSON only (no markdown, no prose). "
        "Return ONE JSON object matching this schema exactly: tempo, clip_slot, kit, harmony_profile, bass_profile, recommendations. "
        "Rules: tempo must be 40..240, clip_slot must be an integer, kit must be a short Ableton device name string. "
        "harmony_profile.progressions must be a non-empty list of objects each with: name (string) and degrees (array of ints 1..7). "
        "bass_profile.octave_range must be a 2-element array like [2,3]. "
        "bass_profile.density must be one of: low, medium, high. "
        "bass_profile.rhythm must be one of: straight_16, syncopated_16, swingy_16, offbeat_8, steady_16, straight_8. "
        "bass_profile.scale should be 'minor' or 'major'. "
        "Match real club genres: hard dance uses high BPM and kick-driven grooves; DnB uses breakbeat feel and rolling bass; "
        "house/techno use club tempos and appropriate hat swing. "
        "Do not invent extra keys."
    )

    base_a_json = base_a if isinstance(base_a, dict) else None
    base_b_json = base_b if isinstance(base_b, dict) else None
    extra = _openai_extra_instructions_for_new_style(display_name, description, key)

    user = (
        f"Create a NEW style definition.\n"
        f"Style key: {key}\n"
        f"Display name: {display_name}\n"
        f"User description: {description}\n\n"
        f"Base style A key: {base_a_key or '(none)'}\n"
        f"Base style B key: {base_b_key or '(none)'}\n\n"
        "If base styles are provided, blend them in a sensible way, but prioritize the user description. "
        "Apply music theory rules appropriate to the described genre fusion. "
        "Output should be compatible with a minor/major degree-based harmony engine (degrees 1..7).\n\n"
        f"Additional instructions:\n{extra}\n\n"
        f"If you need a template, follow this shape: {json.dumps(schema)}\n\n"
        f"Base style A JSON (optional): {json.dumps(base_a_json) if base_a_json else 'null'}\n\n"
        f"Base style B JSON (optional): {json.dumps(base_b_json) if base_b_json else 'null'}"
    )

    obj, meta = await _call_ai_async(system, user, 0.4)
    if not meta.get("ok"):
        return None, meta

    valid, vmeta = _validate_style_object(obj)
    if not vmeta.get("ok"):
        return None, {"ok": False, "error": "ai_invalid_style", "detail": vmeta, "raw": obj}

    return valid, {"ok": True}


@app.post("/knowledge/styles/generate")
async def generate_style_from_description(req: GenerateStyleFromDescriptionRequest):
    data = get_knowledge_styles()
    styles = data.get("styles") if isinstance(data, dict) else None
    if not isinstance(styles, dict):
        styles = {}

    display_name = (req.name or "").strip()
    if not display_name:
        return {"ok": False, "error": "missing_name"}

    key = _slugify_style_key(display_name)
    if not key:
        return {"ok": False, "error": "invalid_name"}

    base_a_key = (req.base_style_a or "").strip().lower() or None
    base_b_key = (req.base_style_b or "").strip().lower() or None
    base_a = styles.get(base_a_key) if base_a_key and isinstance(styles.get(base_a_key), dict) else None
    base_b = styles.get(base_b_key) if base_b_key and isinstance(styles.get(base_b_key), dict) else None

    clip_slot = _next_clip_slot(styles)
    desc = str(req.description or "").strip()

    # Prefer AI generation when available; fall back to heuristic generator.
    obj = None
    source = "heuristic"
    ai_obj, ai_meta = await _openai_generate_style_from_description(
        key=key,
        display_name=display_name,
        description=desc,
        base_a_key=base_a_key,
        base_b_key=base_b_key,
        base_a=base_a,
        base_b=base_b,
        clip_slot=clip_slot,
    )
    if ai_meta.get("ok") and isinstance(ai_obj, dict):
        obj = ai_obj
        source = "ai"
    else:
        obj = _generate_style_object(
            key=key,
            display_name=display_name,
            description=desc,
            base_a=base_a,
            base_b=base_b,
            clip_slot=clip_slot,
        )

    obj = _apply_subgenre_heuristics_to_style_object(
        obj,
        display_name=display_name,
        description=desc,
        key=key,
    )
    obj = _inherit_feel_from_bases(obj, base_a, base_b)

    # Ensure final validity
    valid, vmeta = _validate_style_object(obj)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "generated_style_invalid", "detail": vmeta, "raw": obj, "source": source}

    return {
        "ok": True,
        "key": key,
        "display_name": display_name,
        "style": valid,
        "suggested_clip_slot": clip_slot,
        "source": source,
        "ai": ai_meta if source != "ai" else {"ok": True},
    }


class AddGeneratedStyleRequest(BaseModel):
    key: str
    style: dict
    bass_default_root: int | None = None


@app.post("/knowledge/styles/add")
def add_generated_style(req: AddGeneratedStyleRequest):
    key = _slugify_style_key(req.key)
    if not key:
        return {"ok": False, "error": "invalid_key"}
    if not isinstance(req.style, dict):
        return {"ok": False, "error": "invalid_style"}

    data = get_knowledge_styles()
    if not isinstance(data, dict):
        data = {}

    styles = data.get("styles")
    if not isinstance(styles, dict):
        styles = {}
        data["styles"] = styles

    if key in styles:
        return {"ok": False, "error": "style_exists", "key": key}

    # Ensure clip_slot is present and sane
    style_obj = dict(req.style)
    if "clip_slot" not in style_obj:
        style_obj["clip_slot"] = _next_clip_slot(styles)
    else:
        try:
            style_obj["clip_slot"] = int(style_obj.get("clip_slot"))
        except Exception:
            style_obj["clip_slot"] = _next_clip_slot(styles)

    styles[key] = style_obj

    bass_defaults = data.get("bass_defaults")
    if not isinstance(bass_defaults, dict):
        bass_defaults = {}
        data["bass_defaults"] = bass_defaults

    root = req.bass_default_root
    if root is None:
        # If we can infer from existing defaults of similar style, try, else 43
        root = 43
    try:
        root = int(root)
    except Exception:
        root = 43
    if root < 0:
        root = 0
    if root > 127:
        root = 127
    bass_defaults[key] = {"root": root, "octave": 0}

    try:
        with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        global BASS_STYLE_DEFAULTS, BASS_STYLE_PROFILE, HARMONY_STYLE_PROFILE, STYLE_CONFIG, STYLE_RECOMMENDATIONS, VOLCA_STYLE_TO_CLIP_SLOT, INSTRUMENT_CATALOG
        BASS_STYLE_DEFAULTS, BASS_STYLE_PROFILE, HARMONY_STYLE_PROFILE, STYLE_CONFIG, STYLE_RECOMMENDATIONS, VOLCA_STYLE_TO_CLIP_SLOT, INSTRUMENT_CATALOG = _load_styles_from_disk()
        return {"ok": True, "key": key, "clip_slot": int(styles[key].get("clip_slot", 0))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/knowledge/styles")
def save_knowledge_styles(data: dict):
    # minimal validation
    if "styles" not in data:
        return {"ok": False, "error": "missing_styles_key"}
        
    try:
        with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            
        # Reload in-memory globals
        global BASS_STYLE_DEFAULTS, BASS_STYLE_PROFILE, HARMONY_STYLE_PROFILE, STYLE_CONFIG, STYLE_RECOMMENDATIONS, VOLCA_STYLE_TO_CLIP_SLOT, INSTRUMENT_CATALOG
        BASS_STYLE_DEFAULTS, BASS_STYLE_PROFILE, HARMONY_STYLE_PROFILE, STYLE_CONFIG, STYLE_RECOMMENDATIONS, VOLCA_STYLE_TO_CLIP_SLOT, INSTRUMENT_CATALOG = _load_styles_from_disk()
        
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class GenerateChordProgressionRequest(BaseModel):
    style: str
    bars: int = 4
    root_midi: int | None = None
    chord_track_index: int = CHORDS_TRACK_INDEX
    chord_clip_slot_index: int = 0
    chord_octave: int = 4
    velocity: int | None = None  # default: the style's harmony_profile velocity


SCALE_INTERVALS = {
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "major": [0, 2, 4, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
}

# Scale steps above the chord root (0 = root, 2 = third, 4 = fifth, 6 = seventh, 8 = ninth).
# "ninth" drops the fifth so the voicing stays 4 notes and doesn't clog up.
CHORD_VOICINGS = {
    "triad": [0, 2, 4],
    "seventh": [0, 2, 4, 6],
    "ninth": [0, 2, 6, 8],
    "power": [0, 4, 7],   # root, fifth, octave
    "drone": [0, 4],      # root + fifth
}
_ROOT_POSITION_VOICINGS = {"power", "drone"}

# Rhythm per bar for the chord track: (1/16 step, length in steps, velocity scale).
CHORD_RHYTHMS = {
    "sustain": [(0, 16, 1.0)],
    "halfbar": [(0, 7, 1.0), (8, 7, 0.85)],
    "offbeat": [(2, 1, 1.0), (6, 1, 0.9), (10, 1, 1.0), (14, 1, 0.9)],
    "house_stab": [(0, 1, 0.9), (3, 2, 1.0), (6, 1, 0.8), (10, 2, 1.0), (13, 1, 0.85)],
    "two_step": [(3, 1, 1.0), (6, 2, 0.85), (11, 1, 1.0), (14, 2, 0.85)],
    "rave_stab": [(0, 2, 1.0), (3, 2, 0.9), (6, 3, 0.95), (10, 1, 0.85), (12, 2, 0.9)],
    "dub": [(2, 1, 1.0), (5, 1, 0.5), (10, 1, 0.95), (13, 1, 0.45)],
    "gate16": [(i, 1, 1.0 if i % 4 == 0 else (0.75 if i % 2 == 0 else 0.55)) for i in range(16)],
}


def _style_harmony(style: str) -> dict:
    s = (style or "").strip().lower()
    hp = HARMONY_STYLE_PROFILE.get(s) if isinstance(HARMONY_STYLE_PROFILE, dict) else None
    hp = dict(hp) if isinstance(hp, dict) else {}
    if not hp.get("scale"):
        bp = BASS_STYLE_PROFILE.get(s) if isinstance(BASS_STYLE_PROFILE, dict) else None
        hp["scale"] = (bp or {}).get("scale") or "minor"
    return hp


def _scale_intervals(scale: str) -> list[int]:
    return SCALE_INTERVALS.get((scale or "minor").strip().lower(), SCALE_INTERVALS["minor"])


def _minor_scale_pitch_classes(root_pc: int):
    return [((root_pc + i) % 12) for i in SCALE_INTERVALS["minor"]]


def _chord_offsets(degree: int, scale: str, voicing: str) -> list[int]:
    """Semitones above the key root for a diatonic chord on `degree`, stacked in thirds within the scale."""
    iv = _scale_intervals(scale)
    d = (int(degree) - 1) % 7
    if voicing == "power":
        root = iv[d]
        fifth = iv[(d + 4) % 7] + 12 * ((d + 4) // 7)
        return [root, fifth, root + 12]
    out = []
    for k in CHORD_VOICINGS.get(voicing, CHORD_VOICINGS["triad"]):
        idx = d + k
        out.append(iv[idx % 7] + 12 * (idx // 7))
    return out


def _chord_quality(degree: int, scale: str, voicing: str) -> str:
    """Label like min, maj, dim, min7, maj7, 5 for display."""
    if voicing in _ROOT_POSITION_VOICINGS:
        return "5"
    t = _chord_offsets(degree, scale, "seventh")
    third, fifth, seventh = t[1] - t[0], t[2] - t[0], t[3] - t[0]
    base = "dim" if (third == 3 and fifth == 6) else ("min" if third == 3 else "maj")
    if voicing in {"seventh", "ninth"}:
        base += {10: "7", 11: "maj7"}.get(seventh, "")
    return base


def _voice_chord(root_pc: int, offsets: list[int], register: int, prev: list[int] | None, root_position: bool) -> list[int]:
    """Place the chord near `register`, choosing the inversion that moves least from the previous chord."""
    pcs = [(root_pc + o) % 12 for o in offsets]
    rotations = [0] if root_position else range(len(pcs))
    candidates = []
    for r in rotations:
        order = pcs[r:] + pcs[:r]
        if root_position:
            order = [root_pc + o for o in offsets]
        for base_oct in range(2, 8):
            if root_position:
                notes = [base_oct * 12 + n for n in order]
            else:
                notes = [base_oct * 12 + order[0]]
                for pc in order[1:]:
                    n = notes[-1] + ((pc - notes[-1]) % 12 or 12)
                    notes.append(n)
            if 0 <= min(notes) and max(notes) <= 127:
                candidates.append(notes)

    def score(c):
        center = sum(c) / len(c)
        s = abs(center - register) * (0.35 if prev else 1.0)
        if prev:
            a, b = sorted(c), sorted(prev)
            if len(a) == len(b):
                s += sum(abs(x - y) for x, y in zip(a, b))
            else:
                s += abs(center - sum(prev) / len(prev)) * len(a)
        return s

    return min(candidates, key=score)


def _pick_progression_degrees(style: str, bars: int):
    style = (style or "").strip().lower()
    bars_i = int(bars)
    if bars_i < 1:
        bars_i = 1
    if bars_i > 16:
        bars_i = 16

    hp = _style_harmony(style)
    if hp.get("harmonic_rhythm") == "drone":
        return [1] * bars_i

    progs = hp.get("progressions")
    if not isinstance(progs, list) or not progs:
        # Safe default: i - VI - III - VII
        base = [1, 6, 3, 7]
    else:
        pick = random.choice(progs)
        base = pick.get("degrees") if isinstance(pick, dict) else None
        if not isinstance(base, list) or not base:
            base = [1, 6, 3, 7]

    per_chord = 2 if hp.get("harmonic_rhythm") == "two_bars" else 1
    out: list[int] = []
    while len(out) < bars_i:
        for x in base:
            out.extend([int(x)] * per_chord)
    return out[:bars_i]


def _generate_chords(style: str, bars: int, root_midi: int, chord_octave: int):
    """Voice-led progression shaped by the style's harmony_profile (scale, voicing, register, rhythm)."""
    root_pc = int(root_midi) % 12
    hp = _style_harmony(style)
    scale = str(hp.get("scale") or "minor").lower()
    voicing = str(hp.get("voicing") or "triad").lower().rstrip("s")  # legacy "triads"
    if voicing not in CHORD_VOICINGS:
        voicing = "triad"
    try:
        register = int(hp.get("register"))
    except Exception:
        register = int(chord_octave) * 12 + 9
    rhythm = str(hp.get("rhythm") or "sustain")
    if rhythm not in CHORD_RHYTHMS:
        rhythm = "sustain"
    try:
        velocity = int(hp.get("velocity", 70))
    except Exception:
        velocity = 70

    degrees = _pick_progression_degrees(style, bars)
    chords: list[dict] = []
    prev = None
    for i, deg in enumerate(degrees):
        offsets = _chord_offsets(deg, scale, voicing)
        chord_root_pc = (root_pc + offsets[0]) % 12
        rel = [o - offsets[0] for o in offsets]
        notes = _voice_chord(chord_root_pc, rel, register, prev, voicing in _ROOT_POSITION_VOICINGS)
        prev = notes
        chords.append({
            "bar": i,
            "degree": int(deg),
            "quality": _chord_quality(deg, scale, voicing),
            "notes": notes,
        })
    return {
        "bars": int(bars),
        "root_midi": int(root_midi),
        "scale": scale,
        "voicing": voicing,
        "rhythm": rhythm,
        "velocity": velocity,
        "swing": _style_swing(style),
        "harmonic_rhythm": str(hp.get("harmonic_rhythm") or "one_chord_per_bar"),
        "chords": chords,
    }, {"ok": True}


def _write_chords_to_ableton(track_index: int, clip_slot_index: int, chord_prog: dict, velocity: int | None = None):
    bars = int(chord_prog.get("bars", 1) or 1)
    length_beats = float(bars * 4)
    ctrl.create_clip(track_index, clip_slot_index, length_beats)

    vel = int(velocity if velocity is not None else chord_prog.get("velocity", 70))
    vel = max(1, min(127, vel))

    chords = chord_prog.get("chords")
    if not isinstance(chords, list):
        return {"ok": False, "error": "chords_missing"}

    rhythm = CHORD_RHYTHMS.get(str(chord_prog.get("rhythm") or "sustain"), CHORD_RHYTHMS["sustain"])
    swing = _clamp_swing(chord_prog.get("swing"))

    def _notes(ch):
        out = []
        for n in ch.get("notes") or []:
            try:
                p = int(n)
            except Exception:
                continue
            if 0 <= p <= 127:
                out.append(p)
        return out

    valid = sorted([ch for ch in chords if isinstance(ch, dict)], key=lambda c: int(c.get("bar", 0) or 0))

    if rhythm == CHORD_RHYTHMS["sustain"]:
        # Held chords: tie repeated bars into one long note instead of retriggering every bar.
        i = 0
        while i < len(valid):
            notes = _notes(valid[i])
            start_bar = int(valid[i].get("bar", 0) or 0)
            j = i + 1
            while j < len(valid) and _notes(valid[j]) == notes and int(valid[j].get("bar", 0) or 0) == start_bar + (j - i):
                j += 1
            for p in notes:
                ctrl.add_note(track_index, clip_slot_index, p, float(start_bar * 4), float((j - i) * 4), vel)
            i = j
        return {"ok": True}

    for ch in valid:
        bar = int(ch.get("bar", 0) or 0)
        notes = _notes(ch)
        for step, length, vscale in rhythm:
            start = _swung_start(bar * 16 + step, swing)
            dur = max(0.1, length * 0.25 * 0.9)
            hit_vel = max(1, min(127, int(round(vel * vscale))))
            for p in notes:
                ctrl.add_note(track_index, clip_slot_index, p, start, dur, hit_vel)

    return {"ok": True}


@app.post("/chords/generate")
def generate_chord_progression(req: GenerateChordProgressionRequest):
    style = (req.style or "").strip().lower()
    if not style:
        return {"ok": False, "error": "missing_style"}

    bars = int(req.bars)
    if bars < 1:
        bars = 1
    if bars > 8:
        bars = 8

    root = req.root_midi
    if root is None:
        if style in BASS_STYLE_DEFAULTS:
            root = int(BASS_STYLE_DEFAULTS[style].get("root", 43))
        else:
            root = 43
    root = int(root)
    if root < 0:
        root = 0
    if root > 127:
        root = 127

    chord_octave = int(req.chord_octave)
    if chord_octave < 0:
        chord_octave = 0
    if chord_octave > 8:
        chord_octave = 8

    prog, meta = _generate_chords(style, bars, root, chord_octave)
    if not meta.get("ok"):
        return meta
    return {"ok": True, "style": style, "progression": prog}


@app.post("/chords/generate_and_write")
def generate_and_write_chord_progression(req: GenerateChordProgressionRequest):
    gen = generate_chord_progression(req)
    if not isinstance(gen, dict) or not gen.get("ok"):
        return gen

    prog = gen.get("progression")
    if not isinstance(prog, dict):
        return {"ok": False, "error": "bad_progression"}

    write_meta = _write_chords_to_ableton(
        int(req.chord_track_index),
        int(req.chord_clip_slot_index),
        prog,
        int(req.velocity) if req.velocity is not None else None,
    )
    if not write_meta.get("ok"):
        return write_meta

    return {
        "ok": True,
        "style": (req.style or "").strip().lower(),
        "track_index": int(req.chord_track_index),
        "clip_slot_index": int(req.chord_clip_slot_index),
        "progression": prog,
    }


@app.get("/")
def root():
    return RedirectResponse(url="/pulse_studio.html")


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
        "bridge": {"connected": BRIDGE.connected},
    }


@app.post("/clips/fire")
def fire_clip(req: FireClipRequest):
    ctrl.fire_clip(req.track_index, req.clip_slot_index)
    if bool(getattr(req, "fire_once", False)):
        try:
            bars = int(getattr(req, "once_bars", 1) or 1)
        except Exception:
            bars = 1
        _schedule_stop_clip_after_bars(
            track_index=int(req.track_index),
            clip_slot_index=int(req.clip_slot_index),
            bars=bars,
            bpm_default=140.0,
        )
    return {"ok": True, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/scenes/fire")
def fire_scene(req: FireSceneRequest):
    ctrl.fire_scene(req.scene_index)
    return {"ok": True, "scene_index": req.scene_index}


class TrackMuteRequest(BaseModel):
    track_index: int = Field(ge=0)
    mute: bool


@app.post("/tracks/mute")
def tracks_mute(req: TrackMuteRequest):
    ctrl.send("/live/track/set/mute", [int(req.track_index), bool(req.mute)])
    return {"ok": True, "track_index": int(req.track_index), "mute": bool(req.mute)}


@app.post("/clips/stop_all")
def clips_stop_all():
    """Stop every playing clip but keep the transport running (a panic that stays in time)."""
    if not BRIDGE.connected:
        ctrl.send("/live/song/stop_all_clips", [])
        return {"ok": True, "via": "osc"}
    try:
        BRIDGE.request("stop_all_clips", timeout_s=2.0)
        return {"ok": True, "via": "bridge"}
    except BridgeError as e:
        if "unknown_command" not in str(e):
            return {"ok": False, "error": str(e)}
    # PulseBridge before 0.2.0: stop each playing clip instead.
    snap = BRIDGE.request("get_snapshot", timeout_s=3.0) or {}
    stopped = 0
    for t in snap.get("tracks") or []:
        slot = t.get("playing_slot_index")
        if isinstance(slot, int) and slot >= 0:
            BRIDGE.send("stop_clip", {"track_index": int(t["index"]), "clip_slot_index": slot})
            stopped += 1
    return {"ok": True, "via": "per_track", "stopped": stopped}


@app.get("/tracks/plan")
def tracks_plan():
    """The tracks Pulse writes to, so pages can label them by role."""
    return {"ok": True, "tracks": [{"track_index": int(p["track_index"]), "role": p["role"], "name": p["name"]} for p in PULSE_TRACK_PLAN]}


def _autoplay_worker(*, steps: list[dict], bpm: float, clip_bars: int, loop: bool, stop_event: threading.Event):
    bpm_f = float(bpm or 140.0)
    if bpm_f <= 1.0:
        bpm_f = 140.0
    clip_bars_i = int(clip_bars or 1)
    if clip_bars_i < 1:
        clip_bars_i = 1
    if clip_bars_i > 128:
        clip_bars_i = 128

    sec_per_bar = (4.0 * 60.0) / bpm_f
    scene_dur_s = max(0.25, sec_per_bar * float(clip_bars_i))

    try:
        ctrl.play()
    except Exception:
        pass

    while not stop_event.is_set():
        for idx, st in enumerate(steps):
            if stop_event.is_set():
                break
            try:
                scene_index = int(st.get("scene_index"))
            except Exception:
                continue
            try:
                repeats = int(st.get("repeats", 1) or 1)
            except Exception:
                repeats = 1
            if repeats < 1:
                repeats = 1
            if repeats > 256:
                repeats = 256

            with _AUTOPLAY_LOCK:
                _AUTOPLAY_STATE["current_step"] = int(idx)
                _AUTOPLAY_STATE["last_scene_index"] = int(scene_index)

            try:
                ctrl.fire_scene(scene_index)
            except Exception:
                pass

            sleep_s = min(3600.0, scene_dur_s * float(repeats))
            end_at = time.time() + sleep_s
            while (time.time() < end_at) and (not stop_event.is_set()):
                time.sleep(0.1)

        if not bool(loop):
            break

    with _AUTOPLAY_LOCK:
        _AUTOPLAY_STATE["running"] = False
        _AUTOPLAY_STATE["current_step"] = None


@app.post("/autoplay/start")
def autoplay_start(req: AutoplayStartRequest):
    steps_in = req.steps or []
    steps: list[dict] = []
    for st in steps_in:
        try:
            scene_index = int(st.scene_index)
        except Exception:
            continue
        try:
            repeats = int(st.repeats or 1)
        except Exception:
            repeats = 1
        if repeats < 1:
            repeats = 1
        if repeats > 256:
            repeats = 256
        steps.append({"scene_index": scene_index, "repeats": repeats})

    if not steps:
        return {"ok": False, "error": "empty_steps"}

    global _AUTOPLAY_THREAD, _AUTOPLAY_STOP
    with _AUTOPLAY_LOCK:
        if _AUTOPLAY_STOP is not None:
            _AUTOPLAY_STOP.set()
        _AUTOPLAY_STOP = threading.Event()
        _AUTOPLAY_STATE["running"] = True
        _AUTOPLAY_STATE["current_step"] = 0
        _AUTOPLAY_STATE["last_scene_index"] = None

        th = threading.Thread(
            target=_autoplay_worker,
            kwargs={
                "steps": steps,
                "bpm": float(req.bpm or 140.0),
                "clip_bars": int(req.clip_bars or 8),
                "loop": bool(req.loop),
                "stop_event": _AUTOPLAY_STOP,
            },
            daemon=True,
        )
        _AUTOPLAY_THREAD = th
        th.start()

    return {"ok": True, "running": True, "step_count": len(steps)}


@app.post("/autoplay/stop")
def autoplay_stop():
    global _AUTOPLAY_STOP
    with _AUTOPLAY_LOCK:
        if _AUTOPLAY_STOP is not None:
            _AUTOPLAY_STOP.set()
        _AUTOPLAY_STATE["running"] = False
        _AUTOPLAY_STATE["current_step"] = None
    return {"ok": True, "running": False}


@app.get("/autoplay/status")
def autoplay_status():
    with _AUTOPLAY_LOCK:
        return {"ok": True, **_AUTOPLAY_STATE}


@app.post("/scenes/ensure")
def ensure_scenes(req: EnsureScenesRequest):
    try:
        return _ensure_scene_count(int(req.min_scenes))
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/scenes/label")
def label_scenes(req: LabelScenesRequest):
    try:
        start = int(req.start_index)
        if start < 0:
            start = 0
        count = int(req.count)
        if count < 1:
            count = 1
        if count > 128:
            count = 128
        prefix = str(req.prefix or "PS").strip() or "PS"

        ensure_res = None
        if bool(req.ensure_min_scenes):
            ensure_res = _ensure_scene_count(start + count)

        labeled = []
        for i in range(start, start + count):
            try:
                name = f"{prefix}-{(i + 1):02d}"
                ctrl.send("/live/scene/set/name", [int(i), str(name)])
                time.sleep(0.02)
                labeled.append({"scene_index": int(i), "name": name})
            except Exception:
                labeled.append({"scene_index": int(i), "error": "set_name_failed"})

        return {"ok": True, "ensure": ensure_res, "labeled": labeled}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _ensure_track_count(min_tracks: int):
    existing = None
    try:
        res = _query_with_timeout("/live/song/get/num_tracks", [], timeout_s=0.8)
        if isinstance(res, dict) and res.get("ok") and isinstance(res.get("args"), tuple) and len(res.get("args")) > 0:
            existing = int(res["args"][0])
    except Exception:
        existing = None

    if existing is None:
        to_create = int(min_tracks)
    else:
        to_create = max(0, int(min_tracks) - int(existing))

    if to_create > 64:
        to_create = 64

    for _ in range(to_create):
        try:
            ctrl.send("/live/song/create_midi_track", [-1])
            time.sleep(0.05)
        except Exception:
            pass

    after = None
    try:
        res2 = _query_with_timeout("/live/song/get/num_tracks", [], timeout_s=0.8)
        if isinstance(res2, dict) and res2.get("ok") and isinstance(res2.get("args"), tuple) and len(res2.get("args")) > 0:
            after = int(res2["args"][0])
    except Exception:
        after = None

    return {"ok": True, "requested": int(min_tracks), "existing": existing, "attempted_create": int(to_create), "after": after}


def _ensure_midi_tracks_prefix(count: int, *, prefix: str):
    # Best-effort: insert MIDI tracks at the start to guarantee track indices 0..count-1 are MIDI.
    if count < 1:
        count = 1
    if count > 16:
        count = 16

    created = 0
    pref = str(prefix or "").strip()

    # If tracks already appear to be prepared (named with prefix), don't insert more.
    try:
        names_res = _query_with_timeout("/live/song/get/track_names", [], timeout_s=0.8)
        if isinstance(names_res, dict) and names_res.get("ok") and isinstance(names_res.get("args"), tuple):
            names = [str(x) for x in names_res.get("args")]
            if len(names) >= int(count) and pref:
                first = names[: int(count)]
                if all(str(n).startswith(pref) for n in first):
                    return {"ok": True, "created": 0, "requested": int(count), "note": "Tracks already prepared (prefixed)."}
    except Exception:
        pass

    for _ in range(int(count)):
        try:
            ctrl.send("/live/song/create_midi_track", [0])
            time.sleep(0.05)
            created += 1
        except Exception:
            pass
    return {"ok": True, "created": int(created), "requested": int(count), "note": "Inserted MIDI tracks at index 0 (may push existing audio tracks to the right)."}


@app.post("/tracks/ensure")
def ensure_tracks(req: EnsureTracksRequest):
    try:
        print(f"/tracks/ensure min_tracks={int(req.min_tracks)} insert_at_start={bool(req.insert_at_start)}")
        if bool(req.insert_at_start):
            # Ensure indices are stable for Pulse Studio (0..N-1 should be MIDI).
            res = _ensure_midi_tracks_prefix(int(req.min_tracks), prefix=str(req.prefix or "PS-TRK"))
            if isinstance(res, dict):
                res["insert_at_start"] = True
                res["min_tracks"] = int(req.min_tracks)
            return res
        res = _ensure_track_count(int(req.min_tracks))
        if isinstance(res, dict):
            res["insert_at_start"] = False
            res["min_tracks"] = int(req.min_tracks)
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/tracks/label")
def label_tracks(req: LabelTracksRequest):
    try:
        start = int(req.start_index)
        if start < 0:
            start = 0
        count = int(req.count)
        if count < 1:
            count = 1
        if count > 64:
            count = 64
        prefix = str(req.prefix or "PS-TRK").strip() or "PS-TRK"

        ensure_res = None
        if bool(req.ensure_min_tracks):
            ensure_res = _ensure_track_count(start + count)

        labeled = []
        for i in range(start, start + count):
            try:
                name = f"{prefix}-{(i + 1):02d}"
                ctrl.send("/live/track/set/name", [int(i), str(name)])
                time.sleep(0.02)
                labeled.append({"track_index": int(i), "name": name})
            except Exception:
                labeled.append({"track_index": int(i), "error": "set_name_failed"})

        return {"ok": True, "ensure": ensure_res, "labeled": labeled}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fade_thread(duration_ms: int, tracks: list[int]):
    steps = 20
    interval = (duration_ms / 1000.0) / steps
    
    # Get current volumes (assume 1.0 or get from cache if we had one, but for now linear fade from 0.85 to 0)
    # Ideally we'd read current volume, but OSC query is slow. We'll fade from "current" conceptual max (0.85) to 0.
    start_vol = 0.85
    
    for i in range(steps):
        t = (i + 1) / steps
        vol = start_vol * (1.0 - t)
        for trk in tracks:
            ctrl.set_track_volume(trk, vol)
        time.sleep(interval)
        
    # Ensure zero
    for trk in tracks:
        ctrl.set_track_volume(trk, 0.0)
        
    # Stop playback after fade
    time.sleep(0.5)
    ctrl.stop()
    
    # Reset volumes (optional, but good for next play)
    time.sleep(0.5)
    for trk in tracks:
        ctrl.set_track_volume(trk, 0.85)


@app.post("/transport/fade_out")
def fade_out(req: FadeOutRequest):
    # Run in background to not block
    th = threading.Thread(target=_fade_thread, args=(req.duration_ms, req.target_tracks))
    th.start()
    return {"ok": True, "status": "fading_out", "duration_ms": req.duration_ms}


def _wash_drop_thread(bars: int):
    # Start on the next bar and drop on the downbeat, when Live's clock can be read.
    clock = _song_clock()
    drop_beat = None
    if clock is not None and clock["playing"]:
        tempo = clock["tempo"]
        start = _next_boundary(clock, clock["beats_per_bar"])
        drop_beat = start + bars * clock["beats_per_bar"]
        _wait_for_beat(start)
    else:
        tempo_res = _query_with_timeout("/live/song/get/tempo", [])
        tempo = 120.0
        if tempo_res["ok"] and tempo_res["args"]:
            tempo = float(tempo_res["args"][0])

    sec_per_beat = 60.0 / tempo
    total_time = bars * 4 * sec_per_beat
    steps = 40
    interval = total_time / steps
    
    # Target Macros
    # Drums (Track 0, Dev 1): Space (7), Low Cut (5)
    # Bass (Track 1, Dev 0): Space (8), Cutoff (1)
    
    # Store initial values (assumed defaults for now, or read? Reading is slow)
    # Ideally we should read them. For this POC, we ramp from "assumed dry" to "wet/filtered"
    # and then reset to "default dry".
    
    # Ramp up
    for i in range(steps):
        t = (i + 1) / steps
        
        # Drums: Space 0.1 -> 0.6, Low Cut 0.0 -> 0.7
        d_space = 0.1 + (t * 0.5)
        d_cut = 0.0 + (t * 0.7)
        _set_rack_macro(0, 1, 7, d_space) # Space
        _set_rack_macro(0, 1, 5, d_cut)   # Low Cut
        
        # Bass: Space 0.1 -> 0.5, Cutoff 0.4 -> 0.2 (closing)
        b_space = 0.1 + (t * 0.4)
        b_cut = 0.4 - (t * 0.2)
        _set_rack_macro(1, 0, 8, b_space) # Space
        _set_rack_macro(1, 0, 1, b_cut)   # Cutoff

        if i < steps - 1 or drop_beat is None:
            time.sleep(interval)

    if drop_beat is not None:
        _wait_for_beat(drop_beat)  # the sweep's sleeps drift; the drop itself lands on the bar

    # THE DROP - Reset instantly
    _set_rack_macro(0, 1, 7, 0.1) # Drums Space
    _set_rack_macro(0, 1, 5, 0.0) # Drums Low Cut
    _set_rack_macro(1, 0, 8, 0.1) # Bass Space
    _set_rack_macro(1, 0, 1, 0.35) # Bass Cutoff (default-ish)
    
    # Optional: Fire the "Drop" scene if we knew which one it was? 
    # For now, just effect automation.


@app.post("/transport/wash_drop")
def wash_drop(req: WashDropRequest):
    th = threading.Thread(target=_wash_drop_thread, args=(req.duration_bars,))
    th.start()
    return {"ok": True, "status": "washing", "bars": req.duration_bars}


# ---------------------------------------------------------------- bar snap
# Live quantizes clip and scene launches, but not mutes or sends. Moves built from those wait on
# Live's own clock instead of the moment the button was pressed, so they land on the grid.
GRID_LEAD_S = 0.012  # send a hair early: the command still has to reach Live
GRID_LATE_BEATS = 0.06  # pressed just after a boundary counts as on it, not a whole bar late


def _song_clock() -> dict | None:
    """Live's position now: {playing, beat, tempo, beats_per_bar}. None when the bridge can't answer.

    beat is in quarter notes like Live's song time, adjusted for half the round trip.
    """
    if not BRIDGE.connected:
        return None
    t0 = time.monotonic()
    try:
        s = BRIDGE.request("get_song", timeout_s=1.0) or {}
    except BridgeError:
        return None
    tempo = float(s.get("tempo") or 120.0)
    num = max(1, int(s.get("signature_numerator") or 4))
    den = max(1, int(s.get("signature_denominator") or 4))
    beat = float(s.get("current_song_time") or 0.0)
    playing = bool(s.get("is_playing"))
    if playing:
        beat += (time.monotonic() - t0) / 2.0 * tempo / 60.0
    return {"playing": playing, "beat": beat, "tempo": tempo, "beats_per_bar": num * 4.0 / den,
            "launch_q": s.get("clip_trigger_quantization")}


def _next_boundary(clock: dict, unit_beats: float) -> float:
    """The next multiple of unit_beats (a bar, a beat) at or just behind the clock."""
    beat = clock["beat"]
    k = math.floor(beat / unit_beats)
    if beat - k * unit_beats <= GRID_LATE_BEATS:
        return k * unit_beats
    return (k + 1) * unit_beats


def _wait_for_beat(target: float, interrupt: threading.Event | None = None) -> bool:
    """Sleep until Live reaches beat target. False if interrupt was set first.

    The clock is read again as the target nears, so tempo nudges during a long wait stay in time.
    Returns at once when the transport stops or Live can't be asked; the caller then just acts.
    """
    while True:
        clock = _song_clock()
        if clock is None or not clock["playing"]:
            return True
        left = (target - clock["beat"]) * 60.0 / clock["tempo"] - GRID_LEAD_S
        if left <= 0:
            return True
        # Long waits check back 0.5 s out; the last stretch is one sleep.
        step = min(left - 0.5, 2.0) if left > 0.75 else left
        if interrupt is not None:
            if interrupt.wait(step):
                return False
        else:
            time.sleep(step)
        if step == left:
            return True


def _grid_or_error() -> tuple[dict | None, dict | None]:
    """(clock, None) when a move can run on the grid, or (None, error response)."""
    if not BRIDGE.connected:
        return None, {"ok": False, "error": "bridge_not_connected", "hint": "Moves need PulseBridge enabled in Live's Control Surface settings."}
    clock = _song_clock()
    if clock is None:
        return None, {"ok": False, "error": "no_clock", "hint": "Live didn't answer; try again."}
    if not clock["playing"]:
        return None, {"ok": False, "error": "not_playing", "hint": "Press play first: moves land on the next bar."}
    return clock, None


# Live's launch quantization menu (Song.clip_trigger_quantization) as beats; ("bars", n) scales
# with the time signature. 0 is None: clips launch the moment they're fired.
_LAUNCH_Q = {0: 0.0, 1: ("bars", 8), 2: ("bars", 4), 3: ("bars", 2), 4: ("bars", 1), 5: 2.0, 6: 4 / 3,
             7: 1.0, 8: 2 / 3, 9: 0.5, 10: 1 / 3, 11: 0.25, 12: 1 / 6, 13: 0.125}


def _launch_quantum(clock: dict) -> float:
    """Launch quantization in beats. A bridge that doesn't report it is taken as Live's default, 1 bar."""
    q = _LAUNCH_Q.get(clock.get("launch_q"), ("bars", 1))
    return q[1] * clock["beats_per_bar"] if isinstance(q, tuple) else float(q)


def _launch_warning(clock: dict) -> str | None:
    if _launch_quantum(clock) > clock["beats_per_bar"]:
        return "Live's launch quantization is longer than a bar, so the launch may land a bar or more late. Set it to 1 Bar in Live's control bar."
    return None


def _launch_on(target: float, fire) -> None:
    """Fire a clip or scene so Live launches it on beat target.

    Live snaps a launch to its launch quantization, so the fire goes out just ahead of target and
    Live places it; with quantization off it goes out on target itself.
    """
    clock = _song_clock()
    quantum = _launch_quantum(clock) if clock else 4.0
    _wait_for_beat(target - (min(0.25, quantum / 2) if quantum > 0 else 0.0))
    if _is_playing():
        fire()


def _is_playing() -> bool:
    clock = _song_clock()
    return bool(clock and clock["playing"])


def _launch_boundary(clock: dict) -> float:
    """The next bar a launch can still be sent ahead of. A press just after a downbeat is too late for it."""
    bpb = clock["beats_per_bar"]
    target = math.floor(clock["beat"] / bpb) * bpb + bpb
    if target - clock["beat"] < 0.35:  # the clip write and the fire need a moment
        target += bpb
    return target


def _return_index(key: str) -> int | None:
    """The return carrying one of Pulse's devices for key ("reverb", "delay"), or Live's own A-Reverb/B-Delay."""
    spec = next(s for s in PULSE_RETURNS if s["key"] == key)
    returns = (BRIDGE.request("get_return_tracks", timeout_s=2.0) or {}).get("returns") or []
    for r in returns:
        names = {d.get("name") for d in r.get("devices") or []}
        if r.get("name") == spec["name"] or names & set(spec["chain"]):
            return int(r["return_index"])
    return None


def _role_tracks(roles: list[str]) -> list[int]:
    wanted = {r.strip().lower() for r in roles}
    return [int(p["track_index"]) for p in PULSE_TRACK_PLAN if p["role"] in wanted]


# ---------------------------------------------------------------- Breakdown → Drop
# Drums and bass drop out on the next bar and come back on the downbeat N bars later. Only parts
# that were playing are muted, and only those are brought back, so mutes set by hand stay put.
# Pressing again during the breakdown drops on the next bar instead of waiting it out.

class BreakdownRequest(BaseModel):
    bars: int = Field(default=4, ge=1, le=32)
    roles: list[str] = Field(default_factory=lambda: ["drums", "bass"])


_BREAKDOWN_LOCK = threading.Lock()
_BREAKDOWN: dict = {"running": False, "drop_now": None, "drop_beat": None}


def _set_mutes(tracks: list[int], mute: bool):
    for ti in tracks:
        try:
            BRIDGE.request("set_track", {"track_index": ti, "mute": mute}, timeout_s=1.0)
        except BridgeError as e:
            print(f"[breakdown] mute {ti} -> {mute} failed: {e}")


def _breakdown_thread(start_beat: float, bars: int, beats_per_bar: float, tracks: list[int], drop_now: threading.Event):
    muted: list[int] = []
    playing: list[int] = []
    for ti in tracks:
        try:
            if not (BRIDGE.request("get_track", {"track_index": ti}, timeout_s=1.0) or {}).get("mute"):
                playing.append(ti)
        except BridgeError:
            continue
    try:
        _wait_for_beat(start_beat)
        muted = playing
        _set_mutes(muted, True)
        drop_beat = start_beat + bars * beats_per_bar
        with _BREAKDOWN_LOCK:
            _BREAKDOWN["drop_beat"] = drop_beat
        if not _wait_for_beat(drop_beat, interrupt=drop_now):
            clock = _song_clock()
            if clock is not None and clock["playing"]:
                drop_beat = _next_boundary(clock, clock["beats_per_bar"])
                with _BREAKDOWN_LOCK:
                    _BREAKDOWN["drop_beat"] = drop_beat
                _wait_for_beat(drop_beat)
    finally:
        _set_mutes(muted, False)
        with _BREAKDOWN_LOCK:
            _BREAKDOWN.update({"running": False, "drop_now": None, "drop_beat": None})


@app.post("/moves/breakdown")
def move_breakdown(req: BreakdownRequest):
    with _BREAKDOWN_LOCK:
        if _BREAKDOWN["running"]:
            _BREAKDOWN["drop_now"].set()
            return {"ok": True, "status": "dropping", "drop_beat": _BREAKDOWN["drop_beat"]}
    clock, err = _grid_or_error()
    if err:
        return err
    tracks = _role_tracks(req.roles)
    if not tracks:
        return {"ok": False, "error": "no_tracks", "hint": "None of those roles are Pulse tracks: " + ", ".join(req.roles)}
    bpb = clock["beats_per_bar"]
    start = _next_boundary(clock, bpb)
    drop_now = threading.Event()
    with _BREAKDOWN_LOCK:
        _BREAKDOWN.update({"running": True, "drop_now": drop_now, "drop_beat": start + req.bars * bpb})
    threading.Thread(target=_breakdown_thread, args=(start, req.bars, bpb, tracks, drop_now), daemon=True).start()
    return {"ok": True, "status": "breaking_down", "start_beat": start, "drop_beat": start + req.bars * bpb, "tracks": tracks}


@app.get("/moves/status")
def moves_status():
    with _BREAKDOWN_LOCK:
        bd = {"running": _BREAKDOWN["running"], "drop_beat": _BREAKDOWN["drop_beat"]}
    with _THROW_LOCK:
        th = {"running": _THROW["running"]}
    with _TAIL_LOCK:
        tail = {"running": _TAIL["running"]}
    with _FILL_LOCK:
        fill = {"running": _FILL["running"]}
    return {"ok": True, "breakdown": bd, "delay_throw": th, "tail_out": tail, "fill": fill}


# ---------------------------------------------------------------- Delay Throw
# Opens the delay send on the melodic parts for a beat, then puts each send back where it was, so
# the echoes ring on after the dry sound. Lands on the next beat rather than the next bar: a
# throw is played in time with a phrase, not with the arrangement.

class DelayThrowRequest(BaseModel):
    beats: float = Field(default=1.0, gt=0.0, le=16.0)
    level: float = Field(default=1.0, ge=0.0, le=1.0)
    roles: list[str] = Field(default_factory=lambda: ["stabs", "chords"])
    track_indices: list[int] = Field(default_factory=list)  # extra tracks, such as the vox track


_THROW_LOCK = threading.Lock()
_THROW: dict = {"running": False}


def _return_or_error(key: str) -> tuple[int | None, dict | None]:
    try:
        ri = _return_index(key)
    except BridgeError as e:
        if "unknown_command" in str(e):
            return None, {"ok": False, "error": "bridge_outdated", "hint": _BRIDGE_UPDATE_HINT}
        return None, {"ok": False, "error": str(e)}
    if ri is None:
        return None, {"ok": False, "error": f"no_{key}_return", "hint": f"No {key} return in this set yet: load a sound palette in Compose to add one."}
    return ri, None


def _read_sends(tracks: list[int], ri: int) -> dict[int, float]:
    """Each track's current level on send ri, so a move can put it back exactly."""
    saved: dict[int, float] = {}
    for ti in tracks:
        try:
            sends = (BRIDGE.request("get_track", {"track_index": ti}, timeout_s=1.0) or {}).get("sends") or []
        except BridgeError:
            continue
        if ri < len(sends):
            saved[ti] = float(sends[ri])
    return saved


def _set_sends(levels: dict[int, float], ri: int, tag: str) -> list[int]:
    done = []
    for ti, value in levels.items():
        try:
            BRIDGE.request("set_send", {"track_index": ti, "send_index": ri, "value": value}, timeout_s=1.0)
            done.append(ti)
        except BridgeError as e:
            print(f"[{tag}] send {ti} failed: {e}")
    return done


def _throw_thread(start_beat: float, end_beat: float, saved: dict[int, float], ri: int, level: float):
    raised: list[int] = []
    try:
        _wait_for_beat(start_beat)
        raised = _set_sends({ti: level for ti in saved}, ri, "delay throw")
        _wait_for_beat(end_beat)
    finally:
        _set_sends({ti: saved[ti] for ti in raised}, ri, "delay throw")
        with _THROW_LOCK:
            _THROW["running"] = False


@app.post("/moves/delay_throw")
def move_delay_throw(req: DelayThrowRequest):
    with _THROW_LOCK:
        if _THROW["running"]:
            return {"ok": True, "status": "throwing"}  # a second press would save the raised levels
    clock, err = _grid_or_error()
    if err:
        return err
    ri, err = _return_or_error("delay")
    if err:
        return err
    tracks = list(dict.fromkeys(_role_tracks(req.roles) + [int(t) for t in req.track_indices if int(t) >= 0]))
    saved = _read_sends(tracks, ri)
    if not saved:
        return {"ok": False, "error": "no_tracks", "hint": "None of those tracks has a send to the delay return."}
    start = _next_boundary(clock, 1.0)
    end = start + float(req.beats)
    with _THROW_LOCK:
        _THROW["running"] = True
    threading.Thread(target=_throw_thread, args=(start, end, saved, ri, float(req.level)), daemon=True).start()
    return {"ok": True, "status": "throwing", "start_beat": start, "end_beat": end, "return_index": ri, "tracks": sorted(saved)}


# ---------------------------------------------------------------- Reverb Tail Out
# A softer way into the next scene than a hard cut. From the next bar every playing part feeds the
# reverb at full for feed_beats, then the dry parts drop out and only the tail rings. The next
# scene lands `bars` bars after the start, the parts come back with it and every send goes back.

class TailOutRequest(BaseModel):
    bars: int = Field(default=2, ge=1, le=8)  # from the next bar to the next scene
    feed_beats: float = Field(default=2.0, gt=0.0, le=16.0)  # how long the parts feed the reverb
    scene_index: int | None = Field(default=None, ge=0)  # None: the same clips come back
    track_indices: list[int] = Field(default_factory=list)  # extra tracks, such as the vox track


_TAIL_LOCK = threading.Lock()
_TAIL: dict = {"running": False}


def _tail_thread(start: float, cut: float, land: float, saved: dict[int, float], playing: list[int], ri: int, scene_index: int | None):
    raised: list[int] = []
    muted: list[int] = []
    try:
        _wait_for_beat(start)
        if not _is_playing():
            return
        raised = _set_sends({ti: 1.0 for ti in saved}, ri, "tail out")
        _wait_for_beat(cut)
        if not _is_playing():
            return
        muted = playing
        _set_mutes(muted, True)
        if scene_index is not None:
            _launch_on(land, lambda: BRIDGE.request("fire_scene", {"scene_index": scene_index}, timeout_s=1.0))
        _wait_for_beat(land)
    finally:
        # Sends go back as the parts return; the tail already in the reverb rings on regardless.
        _set_mutes(muted, False)
        _set_sends({ti: saved[ti] for ti in raised}, ri, "tail out")
        with _TAIL_LOCK:
            _TAIL["running"] = False


@app.post("/moves/tail_out")
def move_tail_out(req: TailOutRequest):
    with _TAIL_LOCK:
        if _TAIL["running"]:
            return {"ok": True, "status": "tailing"}
    clock, err = _grid_or_error()
    if err:
        return err
    ri, err = _return_or_error("reverb")
    if err:
        return err
    tracks = list(dict.fromkeys([int(p["track_index"]) for p in PULSE_TRACK_PLAN] + [int(t) for t in req.track_indices if int(t) >= 0]))
    playing = []
    for ti in tracks:
        try:
            if not (BRIDGE.request("get_track", {"track_index": ti}, timeout_s=1.0) or {}).get("mute"):
                playing.append(ti)
        except BridgeError:
            continue
    saved = _read_sends(playing, ri)
    if not saved:
        return {"ok": False, "error": "no_tracks", "hint": "No playing part has a send to the reverb return."}
    bpb = clock["beats_per_bar"]
    start = _next_boundary(clock, bpb)
    land = start + req.bars * bpb
    cut = start + min(float(req.feed_beats), land - start - 1.0)  # at least a beat of tail
    with _TAIL_LOCK:
        _TAIL["running"] = True
    threading.Thread(target=_tail_thread, args=(start, cut, land, saved, playing, ri, req.scene_index), daemon=True).start()
    out = {"ok": True, "status": "tailing", "start_beat": start, "cut_beat": cut, "land_beat": land,
           "return_index": ri, "tracks": sorted(saved), "scene_index": req.scene_index}
    if req.scene_index is not None and _launch_warning(clock):
        out["warning"] = _launch_warning(clock)
    return out


# ---------------------------------------------------------------- Fill → Next
# A one-bar roll written into a free slot on the drums track, launched on the next bar, then the
# next scene on the bar after. With no next scene the drums go back to the clip they were
# playing. The fill clip is deleted once it's done, so the set isn't left with one per press.

FILL_CLIP_NAME = "PS-FILL"


class FillRequest(BaseModel):
    kind: str = Field(default="snare", pattern="^(snare|hats)$")
    scene_index: int | None = Field(default=None, ge=0)


_FILL_LOCK = threading.Lock()
_FILL: dict = {"running": False}


def _fill_notes(kind: str, bar_beats: float) -> list[dict]:
    """A roll that doubles in speed through the bar and swells in velocity: 8ths, 16ths, 32nds."""
    pitch = LANE_TO_MIDI_NOTE["snare" if kind == "snare" else "ch"]
    lo, hi = (70, 127) if kind == "snare" else (60, 120)
    notes = []
    for seg_start, seg_end, step in ((0.0, 0.5, 0.5), (0.5, 0.75, 0.25), (0.75, 1.0, 0.125)):
        t = seg_start * bar_beats
        while t < seg_end * bar_beats - 1e-6:
            notes.append({"pitch": pitch, "start_time": round(t, 4), "duration": step * 0.9,
                          "velocity": round(lo + (hi - lo) * t / bar_beats)})
            t += step
    notes.append({"pitch": LANE_TO_MIDI_NOTE["kick"], "start_time": 0.0, "duration": 0.25, "velocity": 110})
    if kind == "hats":
        notes.append({"pitch": LANE_TO_MIDI_NOTE["oh"], "start_time": bar_beats - 0.5, "duration": 0.45, "velocity": 115})
    return notes


def _fill_thread(drums: int, slot: int, start: float, bpb: float, scene_index: int | None, prev_slot: int):
    try:
        _launch_on(start, lambda: BRIDGE.request("fire_clip", {"track_index": drums, "clip_slot_index": slot}, timeout_s=1.0))
        after = start + bpb
        if scene_index is not None:
            _launch_on(after, lambda: BRIDGE.request("fire_scene", {"scene_index": scene_index}, timeout_s=1.0))
        elif prev_slot >= 0:
            _launch_on(after, lambda: BRIDGE.request("fire_clip", {"track_index": drums, "clip_slot_index": prev_slot}, timeout_s=1.0))
        else:
            _launch_on(after, lambda: BRIDGE.request("stop_clip", {"track_index": drums, "clip_slot_index": slot}, timeout_s=1.0))
        # Delete once something else has taken over; a long launch quantization can take a few bars.
        for n in range(8):
            _wait_for_beat(after + 1.0 + n * bpb)
            if not _is_playing():
                break
            try:
                playing = (BRIDGE.request("get_track", {"track_index": drums}, timeout_s=1.0) or {}).get("playing_slot_index")
            except BridgeError:
                break
            if playing != slot:
                break
    finally:
        try:
            BRIDGE.request("delete_clip", {"track_index": drums, "clip_slot_index": slot}, timeout_s=1.0)
        except BridgeError as e:
            print(f"[fill] deleting the fill clip failed: {e}")
        with _FILL_LOCK:
            _FILL["running"] = False


@app.post("/moves/fill")
def move_fill(req: FillRequest):
    with _FILL_LOCK:
        if _FILL["running"]:
            return {"ok": True, "status": "filling"}
    clock, err = _grid_or_error()
    if err:
        return err
    drums = _role_tracks(["drums"])[0]
    bpb = clock["beats_per_bar"]
    try:
        prev_slot = int((BRIDGE.request("get_track", {"track_index": drums}, timeout_s=1.0) or {}).get("playing_slot_index", -1))
        slot = int(BRIDGE.request("find_free_slot", {"track_index": drums}, timeout_s=1.0)["slot"])
        BRIDGE.request("write_clip", {"track_index": drums, "clip_slot_index": slot, "length": bpb,
                                      "notes": _fill_notes(req.kind, bpb), "name": FILL_CLIP_NAME}, timeout_s=2.0)
    except BridgeError as e:
        return {"ok": False, "error": str(e)}
    clock = _song_clock() or clock  # writing took a moment
    start = _launch_boundary(clock)
    with _FILL_LOCK:
        _FILL["running"] = True
    threading.Thread(target=_fill_thread, args=(drums, slot, start, bpb, req.scene_index, prev_slot), daemon=True).start()
    out = {"ok": True, "status": "filling", "start_beat": start, "next_beat": start + bpb, "slot": slot, "scene_index": req.scene_index}
    if _launch_warning(clock):
        out["warning"] = _launch_warning(clock)
    return out


@app.post("/patterns/launch")
def launch_style(req: LaunchStyleRequest):
    style = (req.style or "").strip().lower()
    if style not in VOLCA_STYLE_TO_CLIP_SLOT:
        return {"ok": False, "error": "unknown_style", "available": sorted(VOLCA_STYLE_TO_CLIP_SLOT.keys())}
    slot = VOLCA_STYLE_TO_CLIP_SLOT[style]
    ctrl.fire_clip(VOLCA_DRUM_TRACK_INDEX, slot)
    return {"ok": True, "style": style, "track_index": VOLCA_DRUM_TRACK_INDEX, "clip_slot_index": slot}


def _style_root_midi(style: str, fallback: int = 43) -> int:
    s = (style or "").strip().lower()
    try:
        if s in BASS_STYLE_DEFAULTS:
            return int(BASS_STYLE_DEFAULTS[s].get("root", fallback))
    except Exception:
        pass
    return int(fallback)


def _tonic_chord_prog(root_midi: int, bars: int, chord_octave: int = 4, style: str | None = None) -> dict:
    """Tonic triad (in the style's scale) on every bar: keeps stabs in key when no progression is being written."""
    scale = str(_style_harmony(style).get("scale") or "minor") if style else "minor"
    base = (int(chord_octave) * 12) + (int(root_midi) % 12)
    notes = [base + o for o in _chord_offsets(1, scale, "triad")]
    return {
        "bars": int(bars),
        "root_midi": int(root_midi),
        "scale": scale,
        "harmonic_rhythm": "drone",
        "chords": [{"bar": b, "degree": 1, "quality": "min", "notes": list(notes)} for b in range(int(bars))],
    }


def _stab_voicings(chord_prog: dict | None, bars: int, octave_shift: int = 12) -> list[list[int]] | None:
    """One chord (list of MIDI notes) per bar, an octave above the chord track so stabs don't muddy the pad."""
    if not isinstance(chord_prog, dict):
        return None
    by_bar: dict[int, list[int]] = {}
    for ch in chord_prog.get("chords") or []:
        if not isinstance(ch, dict) or not isinstance(ch.get("notes"), list):
            continue
        notes = [int(n) + octave_shift for n in ch["notes"] if isinstance(n, (int, float)) and 0 <= int(n) + octave_shift <= 127]
        if notes:
            by_bar[int(ch.get("bar", 0) or 0)] = notes
    if not by_bar:
        return None
    first = by_bar[min(by_bar)]
    n_prog = max(by_bar) + 1
    return [by_bar.get(b % n_prog, first) for b in range(max(1, int(bars)))]


def _hit_steps(values: list | None, limit: int) -> list[int]:
    return [i for i, v in enumerate((values or [])[:limit]) if isinstance(v, (int, float)) and v > 0]


def _groove_context(drums: dict | None, bass: dict | None, bars: int) -> str:
    """Describe where the kick/snare/hats/bass already hit so perc, stabs and fx can interlock instead of collide."""
    n = max(1, int(bars)) * 16
    lines: list[str] = []
    lanes = drums.get("lanes") if isinstance(drums, dict) else None
    if isinstance(lanes, dict):
        for lane in ("kick", "snare", "clap", "ch", "oh"):
            hits = _hit_steps(lanes.get(lane), n)
            if hits:
                lines.append(f"- {lane}: steps {hits}")
    if isinstance(bass, dict):
        hits = _hit_steps(bass.get("steps"), n)
        if hits:
            lines.append(f"- bass notes: steps {hits}")
    if not lines:
        return ""
    return (
        f"Existing groove in this section (0-based 1/16 steps across {max(1, int(bars))} bar(s)):\n"
        + "\n".join(lines)
        + "\nWrite your part to interlock with this: fill the gaps, avoid doubling the kick and snare/clap hits, "
        "and keep the same rhythmic feel so everything sounds like one groove."
    )


def _groove_context_block(context: str | None) -> str:
    return f"\n\n{context.strip()}\n\n" if context and context.strip() else ""


def _declash_perc(perc: dict | None, drums: dict | None) -> dict | None:
    """Remove percussion hits that stack on the drum anchors: low perc on the kick, high perc on the snare/clap."""
    if not isinstance(perc, dict) or not isinstance(drums, dict):
        return perc
    dl = drums.get("lanes") or {}
    pl = dict(perc.get("lanes") or {})
    kick = dl.get("kick") or []
    snare = dl.get("snare") or []
    clap = dl.get("clap") or []
    backbeat = [max(snare[i] if i < len(snare) else 0, clap[i] if i < len(clap) else 0) for i in range(max(len(snare), len(clap)))]

    def _mask(values, anchor):
        return [0 if (i < len(anchor) and anchor[i] > 0) else v for i, v in enumerate(values)]

    if isinstance(pl.get("perc2"), list) and kick:
        pl["perc2"] = _mask(pl["perc2"], kick)
    if isinstance(pl.get("perc1"), list) and backbeat:
        pl["perc1"] = _mask(pl["perc1"], backbeat)
    return {**perc, "lanes": pl}


async def _generate_pair_parts(req: "GenerateAIPairRequest", *, lock: dict | None = None):
    """Generate drums+bass, then perc/stabs/fx with the groove as context. Nothing is written to Live.

    lock: optional {"kick": [...], "bass": {...}} (clip-length) to keep a section tied to the track's core groove.
    """
    style = (req.style or "").strip().lower()
    drum_lanes = ["kick", "snare", "clap", "ch", "oh", "perc1", "perc2"]
    root = int(req.bass_root_midi)
    if style in BASS_STYLE_DEFAULTS:
        root = int(BASS_STYLE_DEFAULTS[style]["root"])

    clip_bars = int(req.clip_bars) if req.clip_bars is not None else int(req.bars)
    if clip_bars < 1:
        clip_bars = 1
    if clip_bars > 16:
        clip_bars = 16

    obj, meta = await _openai_generate_pair(style, req.bars, drum_lanes, root, req.prompt, req.temperature)
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

    # Lock in the style's drum skeleton first, then shape the bass around that kick.
    # Done before repeating so every bar of the loop is identical.
    drums_valid = _apply_groove_to_drums(drums_valid, style)
    bass_styled, _ = _apply_style_to_bassline(bass_valid, style, drums_valid)
    gen_bars = int(drums_valid.get("bars", req.bars) or req.bars)

    drums_rep, _ = _repeat_lane_pattern(drums_valid, clip_bars)
    bass_rep, _ = _repeat_bassline(bass_styled, clip_bars)
    if isinstance(drums_rep, dict):
        drums_valid = drums_rep
    if isinstance(bass_rep, dict):
        bass_styled = bass_rep

    if isinstance(lock, dict):
        kick = lock.get("kick")
        if isinstance(kick, list) and len(kick) == clip_bars * 16:
            drums_valid = {**drums_valid, "lanes": {**drums_valid["lanes"], "kick": list(kick)}}
        if isinstance(lock.get("bass"), dict):
            bass_styled = lock["bass"]

    # Second round: the top layers see the finished drums+bass so they interlock instead of colliding.
    context = _groove_context(drums_valid, bass_styled, gen_bars)
    layer_calls = {
        "perc": (req.include_perc, _openai_generate_perc_pattern),
        "stabs": (req.include_stabs, _openai_generate_stabs_pattern),
        "fx": (req.include_fx, _openai_generate_fx_pattern),
    }
    tasks = {
        name: asyncio.create_task(fn(style, req.bars, req.prompt, req.temperature, context))
        for name, (included, fn) in layer_calls.items()
        if bool(included)
    }

    layers: dict[str, dict | None] = {"perc": None, "stabs": None, "fx": None}
    for name, task in tasks.items():
        pattern, pmeta = await task
        if not pmeta.get("ok"):
            return pmeta
        valid, vmeta = _validate_pattern(pattern, req.bars)
        if not vmeta.get("ok"):
            return {"ok": False, "error": f"invalid_{name}", "detail": vmeta, "raw": pattern}
        rep, _ = _repeat_lane_pattern(valid, clip_bars)
        layers[name] = rep if isinstance(rep, dict) else valid

    layers["perc"] = _declash_perc(layers["perc"], drums_valid)

    return {
        "ok": True,
        "style": style,
        "bars": int(req.bars),
        "clip_bars": clip_bars,
        "drums": drums_valid,
        "bass": bass_styled,
        **layers,
    }


def _finalize_pair_parts(parts: dict, style: str, chord_prog: dict | None) -> dict:
    """Stamp one shared swing on every layer and pitch the stabs to the chords. Returns new dicts."""
    swing = _style_swing(style)
    out = dict(parts)
    for key in ("drums", "bass", "perc", "stabs", "fx"):
        if isinstance(out.get(key), dict):
            out[key] = {**out[key], "swing": swing}
    stabs = out.get("stabs")
    if isinstance(stabs, dict):
        voicings = _stab_voicings(chord_prog, int(stabs.get("bars", 1) or 1))
        if voicings:
            out["stabs"] = {**stabs, "voicings": voicings}
    return out


def _write_pair_parts(req: "GenerateAIPairRequest", parts: dict):
    if bool(req.include_drums) and isinstance(parts.get("drums"), dict):
        _write_pattern_to_ableton(int(req.drum_track_index), int(req.drum_clip_slot_index), parts["drums"])
        GEN_CACHE.set_drums(int(req.drum_track_index), int(req.drum_clip_slot_index), parts["drums"])

    if bool(req.include_bass) and isinstance(parts.get("bass"), dict):
        _write_bassline_to_ableton(int(req.bass_track_index), int(req.bass_clip_slot_index), parts["bass"])
        GEN_CACHE.set_bass(int(req.bass_track_index), int(req.bass_clip_slot_index), parts["bass"])

    if bool(req.include_perc) and isinstance(parts.get("perc"), dict):
        _write_pattern_to_ableton(int(req.perc_track_index), int(req.perc_clip_slot_index), parts["perc"])
        GEN_CACHE.set_perc(int(req.perc_track_index), int(req.perc_clip_slot_index), parts["perc"])

    if bool(req.include_stabs) and isinstance(parts.get("stabs"), dict):
        _write_pattern_to_ableton(int(req.stabs_track_index), int(req.stabs_clip_slot_index), parts["stabs"])
        GEN_CACHE.set_stabs(int(req.stabs_track_index), int(req.stabs_clip_slot_index), parts["stabs"])

    if bool(req.include_fx) and isinstance(parts.get("fx"), dict):
        _write_pattern_to_ableton(int(req.fx_track_index), int(req.fx_clip_slot_index), parts["fx"])
        GEN_CACHE.set_fx(int(req.fx_track_index), int(req.fx_clip_slot_index), parts["fx"])


@app.post("/pair/generate_ai")
async def generate_ai_pair(req: GenerateAIPairRequest):
    parts = await _generate_pair_parts(req)
    if not parts.get("ok"):
        return parts

    style = parts["style"]
    # No progression is written here, so pitch stabs to the tonic chord of the style's key (matches the bass root).
    chord_prog = _tonic_chord_prog(_style_root_midi(style, int(req.bass_root_midi)), parts["clip_bars"], style=style)
    parts = _finalize_pair_parts(parts, style, chord_prog)
    _write_pair_parts(req, parts)

    return {
        "ok": True,
        "drums": parts["drums"],
        "bass": parts["bass"],
        "perc": parts["perc"],
        "stabs": parts["stabs"],
        "fx": parts["fx"],
        "style": style,
        "bars": int(req.bars),
        "clip_bars": parts["clip_bars"],
    }


def _build_suggested_arrangement(style: str) -> list[dict]:
    s = (style or "").strip().lower()

    # Goal: longer playthrough without requiring infinite looping.
    # Constraints: Intro/Outro once; other sections repeat and appear in varied order.
    if "psy" in s:
        return [
            {"scene": "Intro", "repeats": 1},
            {"scene": "Main A", "repeats": 4},
            {"scene": "Main B", "repeats": 4},
            {"scene": "Break", "repeats": 2},
            {"scene": "Main A", "repeats": 4},
            {"scene": "Drop", "repeats": 4},
            {"scene": "Break", "repeats": 1},
            {"scene": "Main B", "repeats": 4},
            {"scene": "Drop", "repeats": 4},
            {"scene": "Outro", "repeats": 1},
        ]

    if "techno" in s or "tekno" in s:
        return [
            {"scene": "Intro", "repeats": 1},
            {"scene": "Build 1", "repeats": 2},
            {"scene": "Build 2", "repeats": 2},
            {"scene": "Peak", "repeats": 4},
            {"scene": "Drop", "repeats": 2},
            {"scene": "Breakdown", "repeats": 2},
            {"scene": "Climax", "repeats": 4},
            {"scene": "Drop", "repeats": 2},
            {"scene": "Outro", "repeats": 1},
        ]

    return [
        {"scene": "Intro", "repeats": 1},
        {"scene": "Main A", "repeats": 4},
        {"scene": "Main B", "repeats": 4},
        {"scene": "Break", "repeats": 2},
        {"scene": "Drop", "repeats": 4},
        {"scene": "Main A", "repeats": 2},
        {"scene": "Drop", "repeats": 2},
        {"scene": "Outro", "repeats": 1},
    ]


# How each full-track scene is built from the track's core groove.
#   mode "core":   the core groove itself
#   mode "derive": core groove transformed in code (same kick/bass/stab identity, parts muted or thinned)
#   mode "vary":   a fresh AI top layer (hats/perc/stabs) locked to the core kick + bassline
# Lane ops: "keep", "drop", "thin" (every other hit), "sparse" (first hit of every other bar),
#           a number (velocity scale), or [op, scale]. "*" is the default for unlisted lanes.
# Bass ops: "keep", "roots" (first note of each beat), "eighths" (no off-16ths).
# FX ops:   "core", "end" (one hit late in the last bar), "start" (one hit on the downbeat).
FULL_TRACK_RECIPES = {
    "techno": {
        "Intro": {"mode": "derive", "drums": {"kick": "keep", "ch": 0.8, "*": "drop"}, "perc": {"perc1": 0.7, "*": "drop"}},
        "Build 1": {"mode": "derive", "drums": {"oh": "drop"}, "bass": "roots", "perc": {"*": 0.85}, "fx": "end"},
        "Build 2": {"mode": "derive", "bass": "eighths", "stabs": {"*": "sparse"}, "fx": "end"},
        "Peak": {"mode": "core"},
        "Drop": {"mode": "derive", "drums": {"kick": "keep", "ch": "thin", "*": "drop"}, "perc": {"perc2": "keep", "*": "drop"}, "fx": "start"},
        "Breakdown": {"mode": "derive", "drums": {"kick": "drop", "*": 0.85}, "perc": {"*": 0.8}, "stabs": {"*": "sparse"}, "fx": "end"},
        "Climax": {"mode": "vary", "prompt": "Push the intensity above the core groove: busier hats and percussion, more insistent stab rhythm."},
        "Outro": {"mode": "derive", "drums": {"kick": "keep", "ch": 0.75, "*": "drop"}, "bass": "roots", "perc": {"*": ["thin", 0.8]}},
    },
    "default": {
        "Intro": {"mode": "derive", "drums": {"kick": "keep", "ch": 0.7, "*": "drop"}, "perc": {"perc1": 0.7, "*": "drop"}},
        "Main A": {"mode": "core"},
        "Main B": {"mode": "vary", "prompt": "A variation of the core groove: change the hat, percussion and stab rhythms, keep the energy level."},
        "Break": {"mode": "derive", "drums": {"kick": "drop", "*": 0.85}, "perc": {"*": 0.8}, "stabs": {"*": "sparse"}, "fx": "end"},
        "Drop": {"mode": "derive", "fx": "core"},
        "Outro": {"mode": "derive", "drums": {"kick": "keep", "ch": 0.75, "*": "drop"}, "bass": "roots", "perc": {"*": ["thin", 0.8]}},
    },
}


def _full_track_recipe(style: str, scene_name: str) -> dict:
    s = (style or "").strip().lower()
    family = "techno" if ("techno" in s or "tekno" in s) else "default"
    return FULL_TRACK_RECIPES[family].get(scene_name) or {"mode": "core"}


def _apply_lane_op(values: list[int], op) -> list[int]:
    kind, scale = "keep", 1.0
    if isinstance(op, (int, float)) and not isinstance(op, bool):
        scale = float(op)
    elif isinstance(op, (list, tuple)) and op:
        kind = str(op[0])
        scale = float(op[1]) if len(op) > 1 else 1.0
    elif isinstance(op, str):
        kind = op

    if kind == "drop":
        return [0] * len(values)

    out = list(values)
    if kind == "thin":
        for k, i in enumerate(_hit_steps(out, len(out))):
            if k % 2 == 1:
                out[i] = 0
    elif kind == "sparse":
        for bar in range(len(out) // 16):
            hits = _hit_steps(out[bar * 16:(bar + 1) * 16], 16)
            keep = hits[0] if (hits and bar % 2 == 0) else None
            for h in hits:
                if h != keep:
                    out[bar * 16 + h] = 0

    if scale != 1.0:
        out = [max(1, min(127, int(round(v * scale)))) if v > 0 else 0 for v in out]
    return out


def _derive_lanes(pattern: dict | None, ops: dict | None) -> dict | None:
    if not isinstance(pattern, dict) or not ops:
        return pattern
    default = ops.get("*", "keep")
    lanes = {lane: _apply_lane_op(vals, ops.get(lane, default)) for lane, vals in (pattern.get("lanes") or {}).items()}
    return {**pattern, "lanes": lanes}


def _derive_bass(bass: dict | None, op: str | None) -> dict | None:
    if not isinstance(bass, dict) or op in (None, "keep"):
        return bass
    steps = list(bass.get("steps") or [])
    vels = list(bass.get("velocities") or [])
    if op == "roots":
        keep = set()
        for beat in range(len(steps) // 4):
            hits = [i for i in range(beat * 4, beat * 4 + 4) if steps[i] > 0]
            if hits:
                keep.add(hits[0])
    elif op == "eighths":
        keep = {i for i, n in enumerate(steps) if n > 0 and i % 2 == 0}
        if not keep:
            return bass
    else:
        return bass
    for i in range(len(steps)):
        if i not in keep:
            steps[i] = 0
            vels[i] = 0
    return {**bass, "steps": steps, "velocities": vels}


def _derive_fx(fx: dict | None, op: str | None, clip_bars: int) -> dict | None:
    if op in (None, "core"):
        if isinstance(fx, dict):
            return fx
        op = "end"
    total = max(1, int(clip_bars)) * 16
    lane = [0] * total
    if op == "start":
        lane[0] = 110
    else:
        lane[total - 4] = 100
    return {"bars": max(1, int(clip_bars)), "step_division": "1/16", "lanes": {"fx": lane}}


def _derive_scene_parts(core: dict, recipe: dict, clip_bars: int) -> dict:
    return {
        **core,
        "drums": _derive_lanes(core.get("drums"), recipe.get("drums")),
        "bass": _derive_bass(core.get("bass"), recipe.get("bass")),
        "perc": _derive_lanes(core.get("perc"), recipe.get("perc")),
        "stabs": _derive_lanes(core.get("stabs"), recipe.get("stabs")),
        "fx": _derive_fx(core.get("fx"), recipe.get("fx"), clip_bars),
    }


def _core_reference_prompt(core: dict, bars: int) -> str:
    return (
        "This section belongs to a track whose core groove is below. Keep its identity: the kick placement and "
        "bassline stay the same (they will be locked to the core), so write drums, hats and top parts that sit on them.\n"
        + _groove_context(core.get("drums"), core.get("bass"), bars)
    )


# The Pad track holds the progression an octave above the Chords track, where the arrangement
# thins out most. level scales the chord velocity; "tonic" holds the first chord only, "top" keeps
# the upper two notes so the pad sits behind busy sections. Scenes not listed (Drop) have no pad.
PAD_SCENES = {
    "Intro": {"level": 0.8, "voicing": "tonic"},
    "Outro": {"level": 0.8, "voicing": "tonic"},
    "Break": {"level": 1.0, "voicing": "full"},
    "Breakdown": {"level": 1.0, "voicing": "full"},
    "Build 1": {"level": 0.6, "voicing": "full"},
    "Build 2": {"level": 0.65, "voicing": "full"},
    "Main A": {"level": 0.55, "voicing": "top"},
    "Main B": {"level": 0.55, "voicing": "top"},
    "Peak": {"level": 0.5, "voicing": "top"},
    "Climax": {"level": 0.55, "voicing": "top"},
}
PAD_MAX_PITCH = 96


def _pad_voicing(notes: list, mode: str) -> list[int]:
    """Chord notes an octave up, root left to the bass and chords, kept below PAD_MAX_PITCH."""
    pitches = sorted(int(n) for n in notes if isinstance(n, (int, float)) and 0 <= int(n) <= 127)
    if not pitches:
        return []
    upper = pitches[1:] if len(pitches) > 2 else pitches
    lifted = [p + 12 if p + 12 <= PAD_MAX_PITCH else p for p in upper]
    return lifted[-2:] if mode == "top" else lifted


def _pad_part(chord_prog: dict | None, scene_name: str, clip_bars: int, chord_velocity: int) -> tuple[dict | None, int]:
    """(held progression for the Pad track, velocity), or (None, 0) when the scene has no pad."""
    spec = PAD_SCENES.get(scene_name)
    chords = [c for c in (chord_prog or {}).get("chords") or [] if isinstance(c, dict)]
    if not spec or not chords:
        return None, 0
    if spec["voicing"] == "tonic":
        chords = [{**chords[0], "bar": b} for b in range(int(clip_bars))]
    voiced = []
    for ch in chords:
        notes = _pad_voicing(ch.get("notes") or [], spec["voicing"])
        if notes:
            voiced.append({**ch, "notes": notes})
    if not voiced:
        return None, 0
    velocity = max(30, min(127, int(round(chord_velocity * spec["level"]))))
    return {**chord_prog, "bars": int(clip_bars), "chords": voiced, "rhythm": "sustain"}, velocity


@app.post("/track/generate_full")
async def generate_full_track(req: GenerateFullTrackRequest):
    style = (req.style or "").strip().lower()
    if not style:
        return {"ok": False, "error": "missing_style"}

    def _build_full_track_scenes(style_key: str, start_slot_index: int):
        s = (style_key or "").strip().lower()

        if "techno" in s or "tekno" in s:
            return [
                {
                    "name": "Intro",
                    "slot": start_slot_index + 0,
                    "prompt": "Intro: start with kick + subtle hats only. No bass. Add light perc texture. No stabs. No fx. Very steady groove.",
                    "include": {"drums": True, "bass": False, "perc": True, "stabs": False, "fx": False},
                },
                {
                    "name": "Build 1",
                    "slot": start_slot_index + 1,
                    "prompt": "Build 1: introduce claps/snares and a bit more hat movement. Add bass very minimal (simple root pattern). Keep stabs off. Add a gentle rising noise sweep near the end.",
                    "include": {"drums": True, "bass": True, "perc": True, "stabs": False, "fx": True},
                },
                {
                    "name": "Build 2",
                    "slot": start_slot_index + 2,
                    "prompt": "Build 2: increase tension. Add busier hats/perc and slightly stronger bass. Introduce very sparse stabs. Use build-up fx: rising filter feel, noise sweep, and a small reverb lift toward the end.",
                    "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": True},
                },
                {
                    "name": "Peak",
                    "slot": start_slot_index + 3,
                    "prompt": "Peak: maximum groove energy. Full drums + bass + perc + stabs. Keep it hypnotic and driving. Use very sparse fx accents (not constant).",
                    "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": True},
                },
                {
                    "name": "Drop",
                    "slot": start_slot_index + 4,
                    "prompt": "Drop: contrast after peak. Strip it back briefly. Keep kick and bass, reduce hats/perc, stabs minimal or off, one short fx hit at the start.",
                    "include": {"drums": True, "bass": True, "perc": True, "stabs": False, "fx": True},
                },
                {
                    "name": "Breakdown",
                    "slot": start_slot_index + 5,
                    "prompt": "Breakdown: remove kick. Keep clap/snare/hat texture and airy perc. No bass. Very sparse stabs. One fx swoosh near the end to lead into the next section.",
                    "include": {"drums": True, "bass": False, "perc": True, "stabs": True, "fx": True},
                },
                {
                    "name": "Climax",
                    "slot": start_slot_index + 6,
                    "prompt": "Climax: highest tension and energy. Full drums + bass + perc + stabs. Slightly more intense rhythms than Peak. Add 1-2 well-placed fx accents.",
                    "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": True},
                },
                {
                    "name": "Outro",
                    "slot": start_slot_index + 7,
                    "prompt": "Outro: slowly wind down. Keep kick and light hats, reduce perc, minimal bass, no stabs, no fx.",
                    "include": {"drums": True, "bass": True, "perc": True, "stabs": False, "fx": False},
                },
            ]

        return [
            {"name": "Intro", "slot": start_slot_index + 0, "prompt": "Intro: sparse drums, no bass, light hats, no stabs, no fx. Minimal energy.", "include": {"drums": True, "bass": False, "perc": True, "stabs": False, "fx": False}},
            {"name": "Main A", "slot": start_slot_index + 1, "prompt": "Main groove A: full drums + bass + perc. Add simple stabs sparsely. No fx.", "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": False}},
            {"name": "Main B", "slot": start_slot_index + 2, "prompt": "Main groove B: variation of the main groove. Change hats/perc and add a different stab rhythm. No fx.", "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": False}},
            {"name": "Break", "slot": start_slot_index + 3, "prompt": "Break: remove kick, reduce drums to clap/snare/hat texture, no bass, airy perc, very sparse stabs, add 1 fx swoosh near end.", "include": {"drums": True, "bass": False, "perc": True, "stabs": True, "fx": True}},
            {"name": "Drop", "slot": start_slot_index + 4, "prompt": "Drop: return full energy. Full drums+bass+perc+stabs. Add sparse fx accents near bar end.", "include": {"drums": True, "bass": True, "perc": True, "stabs": True, "fx": True}},
            {"name": "Outro", "slot": start_slot_index + 5, "prompt": "Outro: strip elements down. Keep kick and light hats, minimal bass, no stabs, no fx.", "include": {"drums": True, "bass": True, "perc": True, "stabs": False, "fx": False}},
        ]

    # Merge style recommendations + sidebar prompt with per-scene prompt.
    # Note: STYLE_RECOMMENDATIONS comes from knowledge/styles.json (recommendations section).
    rec = STYLE_RECOMMENDATIONS.get(style) if isinstance(STYLE_RECOMMENDATIONS, dict) else None
    style_hint = ""
    if isinstance(rec, dict):
        # Keep it short and plain: these hints help guide both patterns and instrument choices.
        drums_hint = str(rec.get("drums") or "").strip()
        bass_hint = str(rec.get("bass") or "").strip()
        parts = []
        if drums_hint:
            parts.append(f"Drums recommendation: {drums_hint}")
        if bass_hint:
            parts.append(f"Bass recommendation: {bass_hint}")
        style_hint = "\n".join(parts)

    sidebar_prompt = str(req.prompt or "").strip()

    # Ensure clip slots exist in a fresh project.
    # Scenes in Live correspond to rows of clip slots.
    ensure_scenes_res = None
    try:
        start_slot = int(req.start_slot_index)
        if start_slot < 0:
            start_slot = 0
        template = _build_full_track_scenes(style, start_slot)
        needed_scenes = start_slot + len(template)
        ensure_scenes_res = _ensure_scene_count(needed_scenes)
    except Exception:
        pass

    ensure_tracks_res = None
    try:
        needed_tracks = _pulse_required_track_count()
        # Ensure tracks 0..needed_tracks-1 are MIDI (fresh Live sets often start with audio tracks).
        ensure_tracks_res = _ensure_midi_tracks_prefix(int(needed_tracks), prefix="PS-TRK")
        # Auto-label first tracks so /tracks/ensure becomes idempotent (prefix check).
        for i in range(0, int(needed_tracks)):
            try:
                ctrl.send("/live/track/set/name", [int(i), f"PS-TRK-{(i + 1):02d}"])
                time.sleep(0.01)
            except Exception:
                pass
    except Exception:
        pass

    instrument_applied = None
    drum_bus_applied = None
    returns_applied = None
    if bool(req.apply_instruments):
        # New tracks/renames need a moment before load_device / browser loads reliably.
        await asyncio.sleep(0.45)
        if BRIDGE.connected:
            palette = await _apply_instrument_palette(
                style, prompt=req.instrument_prompt, replace=bool(req.replace_instruments),
            )
            instrument_applied = palette["results"]
            drum_bus_applied = palette.get("drum_bus")
            returns_applied = palette.get("returns")
        else:
            instrument_applied = await _choose_and_apply_instruments_for_full_track(
                style,
                prompt=req.instrument_prompt,
                limit=int(req.instrument_candidate_limit),
            )

    bars = int(req.bars_per_scene)
    if bars < 1:
        bars = 1
    if bars > 4:
        bars = 4

    clip_bars = int(req.clip_bars)
    if clip_bars < 1:
        clip_bars = 1
    if clip_bars > 16:
        clip_bars = 16

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
    scenes = _build_full_track_scenes(style, start_slot)

    # Generate one harmonic plan for the whole track (shared key + progression).
    # This keeps harmony consistent across all scenes.
    chord_root = 43
    if style in BASS_STYLE_DEFAULTS:
        try:
            chord_root = int(BASS_STYLE_DEFAULTS[style].get("root", 43))
        except Exception:
            chord_root = 43
    chord_root = int(chord_root)
    if chord_root < 0:
        chord_root = 0
    if chord_root > 127:
        chord_root = 127

    chord_octave = 4
    chord_prog, chord_meta = _generate_chords(style, clip_bars, chord_root, chord_octave)
    if not chord_meta.get("ok"):
        chord_prog = None
    chord_velocity = int(chord_prog.get("velocity", 70)) if isinstance(chord_prog, dict) else 70

    def _pair_req_for_scene(sc: dict, *, include_all: bool = False, extra_prompt: str = "") -> GenerateAIPairRequest:
        slot = int(sc["slot"])
        inc = sc["include"]
        scene_prompt = str(sc.get("prompt") or "").strip()
        combined = "\n\n".join([p for p in [style_hint, sidebar_prompt, scene_prompt, extra_prompt] if p])
        return GenerateAIPairRequest(
            style=style,
            bars=bars,
            clip_bars=clip_bars,
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
            include_drums=include_all or bool(inc.get("drums")),
            include_bass=include_all or bool(inc.get("bass")),
            include_perc=include_all or bool(inc.get("perc")),
            include_stabs=include_all or bool(inc.get("stabs")),
            include_fx=include_all or bool(inc.get("fx")),
            prompt=combined,
            temperature=float(req.temperature),
        )

    # 1) One core groove for the whole track. Every layer is generated (even ones the core scene
    #    doesn't play) so the other scenes have material to derive from.
    recipes = [_full_track_recipe(style, str(sc.get("name") or "")) for sc in scenes]
    core_idx = next((i for i, r in enumerate(recipes) if r.get("mode") == "core"), 0)
    core = await _generate_pair_parts(_pair_req_for_scene(scenes[core_idx], include_all=True))
    if not core.get("ok"):
        return {"ok": False, "error": "scene_generation_failed", "scene": scenes[core_idx], "detail": core}

    # Snap the core bass to the progression once, so every scene shares the same chord-aware bassline.
    if isinstance(chord_prog, dict) and isinstance(core.get("bass"), dict):
        try:
            snapped, _ = _snap_bass_to_chords(core["bass"], chord_prog)
            if isinstance(snapped, dict):
                core["bass"] = snapped
        except Exception:
            pass

    # 2) "vary" scenes get fresh top layers, locked to the core kick + bassline.
    core_lock = {"kick": ((core.get("drums") or {}).get("lanes") or {}).get("kick"), "bass": core.get("bass")}
    vary_idx = [i for i, r in enumerate(recipes) if r.get("mode") == "vary" and i != core_idx]
    vary_results = await asyncio.gather(*[
        _generate_pair_parts(
            _pair_req_for_scene(scenes[i], extra_prompt="\n\n".join([str(recipes[i].get("prompt") or ""), _core_reference_prompt(core, bars)])),
            lock=core_lock,
        )
        for i in vary_idx
    ])
    vary_by_idx = dict(zip(vary_idx, vary_results))

    warnings: list[str] = []
    out_scenes = []
    for i, sc in enumerate(scenes):
        recipe = recipes[i]
        if i == core_idx:
            parts = core
        elif i in vary_by_idx and vary_by_idx[i].get("ok"):
            parts = vary_by_idx[i]
        elif i in vary_by_idx:
            # A failed variation shouldn't sink the whole track: build this scene from the core groove.
            detail = vary_by_idx[i]
            warnings.append(f"{sc.get('name')}: AI variation failed ({detail.get('error')}); built from the core groove instead.")
            recipe = {**recipe, "mode": "derive_fallback"}
            parts = _derive_scene_parts(core, recipe, clip_bars)
        else:
            parts = _derive_scene_parts(core, recipe, clip_bars)

        slot = int(sc["slot"])
        inc = sc["include"]
        parts = _finalize_pair_parts(parts, style, chord_prog or _tonic_chord_prog(chord_root, clip_bars, style=style))
        _write_pair_parts(_pair_req_for_scene(sc), parts)

        # Write chords (best-effort; failures shouldn't kill the whole track).
        # Musical defaults:
        # - Intro/Outro: tonic drone (first chord only), held
        # - Main/Drop: full progression in the style's chord rhythm
        # - Break: thinner voicing (2 notes), held so the pad swells instead of chopping
        if isinstance(chord_prog, dict):
            try:
                scene_name = str(sc.get("name") or "")
                if scene_name in {"Intro", "Outro"}:
                    tonic = chord_prog.get("chords")[0] if isinstance(chord_prog.get("chords"), list) and chord_prog.get("chords") else None
                    if isinstance(tonic, dict) and isinstance(tonic.get("notes"), list) and tonic.get("notes"):
                        drone = {
                            "bars": clip_bars,
                            "root_midi": chord_prog.get("root_midi"),
                            "scale": chord_prog.get("scale"),
                            "harmonic_rhythm": "drone",
                            "chords": [{"bar": b, "degree": tonic.get("degree", 1), "quality": tonic.get("quality", "min"), "notes": tonic.get("notes")} for b in range(clip_bars)],
                        }
                        _write_chords_to_ableton(CHORDS_TRACK_INDEX, slot, drone, chord_velocity)
                elif scene_name in {"Break", "Breakdown"}:
                    # Thin the chord voicing: root + third only (first two notes).
                    thin_chords = []
                    for ch in (chord_prog.get("chords") or []):
                        if not isinstance(ch, dict):
                            continue
                        notes = ch.get("notes")
                        if not isinstance(notes, list) or len(notes) < 2:
                            continue
                        thin_chords.append({**ch, "notes": [notes[0], notes[1]]})
                    thin = {**chord_prog, "chords": thin_chords, "rhythm": "sustain"}
                    _write_chords_to_ableton(CHORDS_TRACK_INDEX, slot, thin, max(45, int(chord_velocity * 0.85)))
                else:
                    _write_chords_to_ableton(CHORDS_TRACK_INDEX, slot, chord_prog, chord_velocity)
            except Exception:
                pass

        pad_prog, pad_velocity = _pad_part(chord_prog, str(sc.get("name") or ""), clip_bars, chord_velocity)
        if pad_prog is not None:
            try:
                _write_chords_to_ableton(PAD_TRACK_INDEX, slot, pad_prog, pad_velocity)
            except Exception:
                pass

        out_scenes.append({
            "name": sc["name"],
            "slot": slot,
            "bars": bars,
            "mode": recipe.get("mode"),
            "tracks": {
                "drums": {"track": 0, "slot": slot, "included": bool(inc.get("drums"))},
                "bass": {"track": 1, "slot": slot, "included": bool(inc.get("bass"))},
                "perc": {"track": 2, "slot": slot, "included": bool(inc.get("perc"))},
                "stabs": {"track": 3, "slot": slot, "included": bool(inc.get("stabs"))},
                "fx": {"track": 4, "slot": slot, "included": bool(inc.get("fx"))},
                "chords": {"track": CHORDS_TRACK_INDEX, "slot": slot, "included": True},
                "pad": {"track": PAD_TRACK_INDEX, "slot": slot, "included": pad_prog is not None},
            },
            "prompt": sc.get("prompt"),
        })

    arrangement = _build_suggested_arrangement(style)

    return {
        "ok": True,
        "style": style,
        "bars_per_scene": bars,
        "clip_bars": clip_bars,
        "start_slot_index": start_slot,
        "ensure_scenes": ensure_scenes_res,
        "ensure_tracks": ensure_tracks_res,
        "apply_instruments": {
            "requested": bool(req.apply_instruments),
            "candidate_limit": int(req.instrument_candidate_limit),
        },
        "instruments": {
            "attempted": bool(req.apply_instruments),
            "applied": instrument_applied,
            "drum_bus": drum_bus_applied,
            "returns": returns_applied,
            "hints": _instrument_index_hints_from_applied(instrument_applied) if bool(req.apply_instruments) else [],
        },
        "chords": {"track_index": CHORDS_TRACK_INDEX, "root_midi": chord_root, "progression": chord_prog},
        "warnings": warnings,
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


@app.get("/cache/scene")
def get_cached_scene(slot: int = 0):
    s = int(slot)
    if s < 0:
        return {"ok": False, "error": "bad_slot"}
    return {
        "ok": True,
        "slot": s,
        "data": {
            "drums": GEN_CACHE.get_drums(0, s),
            "bass": GEN_CACHE.get_bass(1, s),
            "perc": GEN_CACHE.get_perc(2, s),
            "stabs": GEN_CACHE.get_stabs(3, s),
            "fx": GEN_CACHE.get_fx(4, s),
        },
    }


@app.post("/devices/load")
def load_device(req: LoadDeviceRequest):
    name = (req.device_name or "").strip()
    if not name:
        return {"ok": False, "error": "missing_device_name"}
    ctrl.load_device(int(req.track_index), name)
    return {"ok": True, "track_index": req.track_index, "device_name": name}


def _normalize_candidates(candidates: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates or []:
        s = str(c or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


class _AbletonMCPSocketClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)

    def _receive_full_json(self, sock: socket.socket, buffer_size: int = 8192, timeout_s: float = 15.0) -> dict:
        sock.settimeout(float(timeout_s))
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(buffer_size)
            if not chunk:
                break
            chunks.append(chunk)
            data = b"".join(chunks)
            try:
                return json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
        if not chunks:
            raise RuntimeError("No data received from AbletonMCP socket")
        data = b"".join(chunks)
        return json.loads(data.decode("utf-8"))

    # PulseBridge implements these with the same names and result shapes.
    BRIDGE_COMMANDS = {"get_session_info", "get_browser_tree", "get_browser_items_at_path", "load_browser_item"}

    def send_command(self, command_type: str, params: dict | None = None, timeout_s: float = 15.0) -> dict:
        if command_type in self.BRIDGE_COMMANDS and BRIDGE.connected:
            return BRIDGE.request(command_type, params or {}, timeout_s=timeout_s) or {}
        cmd = {"type": str(command_type), "params": params or {}}
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((self.host, self.port))
            s.sendall(json.dumps(cmd).encode("utf-8"))
            resp = self._receive_full_json(s, timeout_s=timeout_s)
        status = resp.get("status")
        if status == "error":
            raise RuntimeError(str(resp.get("message") or "unknown_ableton_mcp_error"))
        return resp.get("result", {})


MCP_SOCKET = _AbletonMCPSocketClient(ABLETON_MCP_HOST, ABLETON_MCP_PORT)


def _load_browser_index_from_disk() -> dict | None:
    if not os.path.exists(BROWSER_INDEX_FILE):
        return None
    try:
        with open(BROWSER_INDEX_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


_disk_index = _load_browser_index_from_disk()
if isinstance(_disk_index, dict):
    BROWSER_INDEX.set(_disk_index)


@app.get("/mcp/session")
def mcp_get_session_info():
    try:
        result = MCP_SOCKET.send_command("get_session_info", {}, timeout_s=10.0)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "host": ABLETON_MCP_HOST, "port": ABLETON_MCP_PORT}


@app.get("/mcp/browser/tree")
def mcp_get_browser_tree(category_type: str = "all"):
    try:
        result = MCP_SOCKET.send_command("get_browser_tree", {"category_type": str(category_type)}, timeout_s=15.0)
        return {"ok": True, "category_type": category_type, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "host": ABLETON_MCP_HOST, "port": ABLETON_MCP_PORT}


@app.get("/mcp/browser/items")
def mcp_get_browser_items(path: str):
    try:
        result = MCP_SOCKET.send_command("get_browser_items_at_path", {"path": str(path)}, timeout_s=15.0)
        return {"ok": True, "path": path, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "host": ABLETON_MCP_HOST, "port": ABLETON_MCP_PORT}


@app.get("/mcp/debug/browser_tree")
def mcp_debug_browser_tree(category_type: str = "all"):
    try:
        result = MCP_SOCKET.send_command("get_browser_tree", {"category_type": str(category_type)}, timeout_s=25.0)
        return {"ok": True, "category_type": category_type, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "host": ABLETON_MCP_HOST, "port": ABLETON_MCP_PORT}


@app.post("/mcp/browser/load")
def mcp_load_browser_item(req: MCPLoadBrowserItemRequest):
    uri = (req.item_uri or "").strip()
    if not uri:
        return {"ok": False, "error": "missing_item_uri"}
    try:
        result = MCP_SOCKET.send_command(
            "load_browser_item",
            {"track_index": int(req.track_index), "item_uri": uri},
            timeout_s=20.0,
        )
        return {"ok": True, "track_index": int(req.track_index), "item_uri": uri, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "track_index": int(req.track_index), "item_uri": uri}


def _collect_paths_from_tree(tree_result: dict) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    def add_path(p: str):
        s = str(p or "").strip()
        if not s:
            return
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        paths.append(s)

    def walk(node: dict | None):
        if not isinstance(node, dict):
            return
        if node.get("path"):
            add_path(str(node.get("path")))
        children = node.get("children")
        if isinstance(children, list):
            for ch in children:
                if isinstance(ch, dict):
                    walk(ch)

    cats = tree_result.get("categories") if isinstance(tree_result, dict) else None
    if isinstance(cats, list):
        for c in cats:
            if isinstance(c, dict):
                walk(c)

    # Some Ableton setups return no categories tree but do return available top-level categories.
    # Filter aggressively to avoid enqueueing non-path API helper methods.
    avail = tree_result.get("available_categories") if isinstance(tree_result, dict) else None
    if isinstance(avail, list) and not paths:
        allow = {
            "instruments",
            "sounds",
            "drums",
            "audio_effects",
            "midi_effects",
            "clips",
            "current_project",
            "max_for_live",
            "packs",
            "plugins",
            "samples",
            "user_library",
            "user_folders",
            "legacy_libraries",
        }
        for a in avail:
            if not isinstance(a, str):
                continue
            s = a.strip()
            if not s:
                continue
            k = s.lower()
            if k in allow:
                add_path(s)
    paths.sort(key=lambda x: x.lower())
    return paths


def _build_browser_index(category_type: str, *, max_paths: int, max_items: int) -> dict:
    tree = MCP_SOCKET.send_command("get_browser_tree", {"category_type": str(category_type)}, timeout_s=25.0)
    seed_paths = _collect_paths_from_tree(tree if isinstance(tree, dict) else {})

    # Crawl folders to discover deeper paths and loadable items.
    queue = deque(seed_paths)
    visited_paths: set[str] = set()
    items_out: list[dict] = []

    # Safety limits so we don't explode on huge libraries.
    if max_paths < 100:
        max_paths = 100
    if max_items < 1000:
        max_items = 1000

    while queue and len(visited_paths) < max_paths and len(items_out) < max_items:
        p = queue.popleft()
        p_norm = str(p or "").strip()
        if not p_norm:
            continue
        key = p_norm.lower()
        if key in visited_paths:
            continue
        visited_paths.add(key)

        try:
            res = MCP_SOCKET.send_command("get_browser_items_at_path", {"path": p_norm}, timeout_s=25.0)
        except Exception:
            continue

        its = res.get("items") if isinstance(res, dict) else None
        if not isinstance(its, list):
            continue

        for it in its:
            if not isinstance(it, dict):
                continue

            name = it.get("name")
            uri = it.get("uri")
            is_loadable = bool(it.get("is_loadable"))
            is_folder = bool(it.get("is_folder"))

            if isinstance(name, str) and name.strip() and is_folder:
                # If the remote script provides an explicit path for the folder/item, prefer it.
                it_path = it.get("path")
                if isinstance(it_path, str) and it_path.strip():
                    queue.append(it_path.strip())
                else:
                    # Fallback: build a child path from the current path + name.
                    queue.append(f"{p_norm}/{name.strip()}")

            # Only store loadable items (URIs can be huge; storing folders blows up size)
            if uri and name and is_loadable:
                row = {
                    "name": str(name),
                    "uri": str(uri),
                    "is_loadable": is_loadable,
                    "path": p_norm,
                }
                items_out.append(row)

    # Dedup by uri (keep first)
    dedup: dict[str, dict] = {}
    for it in items_out:
        u = it.get("uri")
        if not isinstance(u, str) or not u:
            continue
        if u in dedup:
            continue
        dedup[u] = it

    out_items = list(dedup.values())
    out_items.sort(key=lambda x: (str(x.get("name") or "").lower(), str(x.get("path") or "").lower()))

    # Assign stable numeric ids (stable for a given index build) so we don't send long URIs to the AI.
    for i, it in enumerate(out_items):
        if isinstance(it, dict):
            it["id"] = int(i + 1)

    return {
        "ok": True,
        "generated_at": time.time(),
        "host": ABLETON_MCP_HOST,
        "port": ABLETON_MCP_PORT,
        "category_type": str(category_type),
        "path_count": len(visited_paths),
        "item_count": len(out_items),
        "loadable_count": int(sum(1 for x in out_items if x.get("is_loadable"))),
        "items": out_items,
    }


@app.get("/mcp/index/status")
def mcp_index_status():
    data = BROWSER_INDEX.get()
    if not isinstance(data, dict):
        return {"ok": False, "error": "index_not_built", "file": BROWSER_INDEX_FILE}
    return {
        "ok": True,
        "generated_at": data.get("generated_at"),
        "category_type": data.get("category_type"),
        "item_count": data.get("item_count"),
        "loadable_count": data.get("loadable_count"),
        "file": BROWSER_INDEX_FILE,
    }


@app.post("/mcp/index/rebuild")
def mcp_index_rebuild(req: MCPRebuildBrowserIndexRequest):
    try:
        data = _build_browser_index(req.category_type, max_paths=int(req.max_paths), max_items=int(req.max_items))
        if not data.get("ok"):
            return data
        BROWSER_INDEX.set(data)
        try:
            with open(BROWSER_INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            out = {
                "ok": True,
                "generated_at": data.get("generated_at"),
                "host": data.get("host"),
                "port": data.get("port"),
                "category_type": data.get("category_type"),
                "path_count": data.get("path_count"),
                "item_count": data.get("item_count"),
                "loadable_count": data.get("loadable_count"),
                "persisted": False,
                "persist_error": str(e),
                "file": BROWSER_INDEX_FILE,
            }
            if bool(req.include_items):
                return data | out
            return out

        out = {
            "ok": True,
            "generated_at": data.get("generated_at"),
            "host": data.get("host"),
            "port": data.get("port"),
            "category_type": data.get("category_type"),
            "path_count": data.get("path_count"),
            "item_count": data.get("item_count"),
            "loadable_count": data.get("loadable_count"),
            "persisted": True,
            "file": BROWSER_INDEX_FILE,
        }
        if bool(req.include_items):
            return data | out
        return out
    except Exception as e:
        return {"ok": False, "error": str(e), "hint": "Make sure Ableton is running and AbletonMCP Remote Script is loaded."}


def _default_osc_instrument_name_for_track(track_index: int) -> str:
    """
    When the browser index can't pick a preset, load a stock device by name.
    PulseBridge reports which devices this Live edition has (e.g. Drift instead of
    Wavetable on Intro/Lite); without it we assume Standard/Suite.

    Env (optional): PULSE_OSC_DEFAULT_DRUM, PULSE_OSC_DEFAULT_MELODIC
    """
    defaults = (BRIDGE.capabilities or {}).get("defaults") or {}
    t = int(track_index)
    if t in (0, 2):
        return str(os.environ.get("PULSE_OSC_DEFAULT_DRUM") or defaults.get("drums") or "Drum Rack")
    return str(os.environ.get("PULSE_OSC_DEFAULT_MELODIC") or defaults.get("melodic") or "Wavetable")


async def _choose_and_apply_instruments_for_full_track(style: str, *, prompt: str | None, limit: int):
    # Map Pulse Studio tracks -> roles. Primary: MCP + browser index. Fallback: OSC class names.
    # Notes:
    # - Track 0 is drums
    # - Track 1 is bass
    # - Track 2 is perc (treated as drums-ish)
    # - Track 3 is stabs (treated as stabs)
    # - Track 4 is fx (treated as pads/noise)
    # - Track 5 is chords (treated as pads)
    # - Track 6 is the pad (treated as pads)
    plan = [{"track_index": int(x["track_index"]), "role": x["role"], "q": x.get("q")} for x in PULSE_TRACK_PLAN]

    applied: list[dict] = []
    for item in plan:
        resp: dict | None = None
        err: str | None = None
        try:
            resp = await recommend_apply_from_index(
                RecommendFromBrowserIndexRequest(
                    style=style,
                    role=_LEGACY_PICKER_ROLE.get(item["role"], item["role"]),
                    track_index=int(item["track_index"]),
                    q=item.get("q"),
                    limit=int(limit),
                    prompt=prompt,
                    temperature=0.25,
                )
            )
        except Exception as e:
            err = str(e)
            resp = {"ok": False, "error": err}

        if isinstance(resp, dict) and resp.get("ok"):
            applied.append(
                {
                    "track_index": item["track_index"],
                    "role": item["role"],
                    "ok": True,
                    "detail": resp,
                }
            )
            continue

        try:
            dev = _default_osc_instrument_name_for_track(int(item["track_index"]))
            ctrl.load_device(int(item["track_index"]), dev)
            time.sleep(0.2)
            applied.append(
                {
                    "track_index": item["track_index"],
                    "role": item["role"],
                    "ok": True,
                    "detail": {
                        "ok": True,
                        "source": "osc_class_fallback",
                        "device": dev,
                        "after_failure": resp if isinstance(resp, dict) else None,
                        "after_error": err,
                    },
                }
            )
        except Exception as e2:
            applied.append(
                {
                    "track_index": item["track_index"],
                    "role": item["role"],
                    "ok": False,
                    "detail": resp,
                    "error": err,
                    "osc_fallback_error": str(e2),
                }
            )
    # MCP may have returned ok with nothing on the device chain; fill any still-empty track.
    _osc_fill_instruments_on_empty_tracks(plan, applied)
    return applied


# ---------------------------------------------------------------- instrument palette (PulseBridge)


async def _bridge_call(cmd: str, params: dict | None = None, timeout_s: float = 5.0):
    """BRIDGE.request without blocking the event loop."""
    return await asyncio.to_thread(BRIDGE.request, cmd, params or {}, timeout_s)


def _browser_index_items() -> list[dict]:
    data = BROWSER_INDEX.get()
    if not isinstance(data, dict):
        data = _load_browser_index_from_disk()
        if isinstance(data, dict):
            BROWSER_INDEX.set(data)
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _track_sound_state(info: dict) -> tuple[list[dict], list[dict]]:
    """(playable instruments, silent empty Drum Racks) on a track."""
    inst = [d for d in info.get("devices") or [] if d.get("type") == "instrument"]
    empty = [d for d in inst if d.get("is_drum_rack") and not d.get("filled_pads")]
    return [d for d in inst if d not in empty], empty


PALETTE_STATE_FILE = os.path.join(APP_DIR, ".pulse_palette.json")
_PALETTE_STATE_LOCK = threading.Lock()


def _read_palette_state() -> dict:
    """{track_index: {"style", "device"}} for instruments Pulse loaded itself."""
    with _PALETTE_STATE_LOCK:
        try:
            with open(PALETTE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _record_palette_load(track_index: int, style: str, device: str | None, *, slot: str = ""):
    """Remember a device Pulse loaded. slot "" is the instrument; "bus" is the drum bus effect."""
    state = _read_palette_state()
    key = str(int(track_index)) + (f":{slot}" if slot else "")
    if device:
        state[key] = {"style": style, "device": device}
    else:
        state.pop(key, None)
    _write_palette_state(state)


def _write_palette_state(state: dict):
    with _PALETTE_STATE_LOCK:
        try:
            with open(PALETTE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass


def _loaded_for_other_style(state: dict, track_index: int, playable: list[dict], style: str) -> str | None:
    """The style a track's current instrument was picked for, if Pulse loaded it for a different style."""
    rec = state.get(str(int(track_index)))
    if not isinstance(rec, dict) or not playable:
        return None
    if str(playable[0].get("name") or "") != str(rec.get("device") or ""):
        return None  # the user swapped in their own instrument; leave it alone
    other = str(rec.get("style") or "")
    return other if other and other != style else None


async def _load_and_verify(track_index: int, item: dict) -> tuple[bool, str | None]:
    """(True, loaded device name) or (False, error)."""
    try:
        await _bridge_call(
            "load_item_at_path",
            {"track_index": track_index, "path": item.get("path"), "name": item.get("name"), "uri": item.get("uri")},
            timeout_s=20.0,
        )
    except BridgeError as e:
        return False, str(e)
    # The device appears on the chain within a tick or two of the load.
    for _ in range(4):
        info = await _bridge_call("get_track", {"track_index": track_index})
        playable, _ = _track_sound_state(info)
        if playable:
            return True, str(playable[0].get("name") or "")
        await asyncio.sleep(0.15)
    return False, "loaded but no playable instrument on the track"


async def _apply_instrument_palette(
    style: str, *, prompt: str | None = None, replace: bool = False, roles: list[str] | None = None,
) -> dict:
    """
    Choose and load a coherent set of instruments for the Pulse tracks.
    Tracks that already have a playable instrument are kept unless replace=True, except
    instruments Pulse itself picked for a different style: those are swapped for this style.
    Empty Drum Racks never count as an instrument and are always replaced.
    """
    rng = random.Random()
    state = _read_palette_state()
    restyled: dict[str, str] = {}
    plan = [p for p in PULSE_TRACK_PLAN if roles is None or p["role"] in roles]
    results: list[dict] = []
    need: dict[str, tuple[int, list[dict]]] = {}

    for p in plan:
        ti, role = int(p["track_index"]), p["role"]
        try:
            info = await _bridge_call("get_track", {"track_index": ti})
        except BridgeError as e:
            results.append({"track_index": ti, "role": role, "action": "failed", "error": str(e)})
            continue
        playable, empty = _track_sound_state(info)
        other_style = _loaded_for_other_style(state, ti, playable, style)
        if other_style:
            restyled[role] = other_style
        if playable and not replace and not other_style:
            kept = {"track_index": ti, "role": role, "action": "kept", "name": playable[0]["name"]}
            if role in ("drums", "perc") and not any(d.get("is_drum_rack") for d in playable):
                kept["warning"] = "Not a drum kit: drum patterns will play as pitched notes. Use Re-pick to swap in a kit."
            results.append(kept)
            continue
        need[role] = (ti, empty + (playable if (replace or other_style) else []))

    items = _browser_index_items()
    pools = {r: instrument_palette.role_pool(items, r) for r in need}
    cands = {r: instrument_palette.rank_candidates(pools[r], r, style, prompt, rng=rng) for r in need}
    cands = {r: c for r, c in cands.items() if c}

    picks: dict[str, dict] = {}
    ai_meta = None
    if cands and _ai_configured():
        picks, ai_meta = await instrument_palette.ai_palette(
            _call_ai_async, style=style, prompt=prompt, candidates=cands,
        )
    for role, pick in instrument_palette.heuristic_palette(cands).items():
        picks.setdefault(role, pick)

    defaults = (BRIDGE.capabilities or {}).get("defaults") or {}
    for role, (ti, remove) in need.items():
        for d in sorted(remove, key=lambda d: d["index"], reverse=True):
            try:
                await _bridge_call("delete_device", {"track_index": ti, "device_index": d["index"]})
            except BridgeError:
                pass

        pick = picks.get(role)
        order: list[dict] = []
        if pick:
            order.append(pick["item"])
        order += [c for c in cands.get(role, []) if not pick or c is not pick["item"]][:2]
        if role in ("drums", "perc"):
            kit = instrument_palette.default_kit(pools.get(role) or [])
            if kit is not None and kit not in order:
                order.append(kit)

        entry = {"track_index": ti, "role": role}
        attempts = []
        for item in order:
            ok, detail = await _load_and_verify(ti, item)
            if ok:
                _record_palette_load(ti, style, detail)
                first = item is (pick or {}).get("item")
                entry.update({
                    "action": "loaded" if first else "fallback",
                    "name": instrument_palette.display_name(item),
                    "folder": item.get("path"),
                    "reason": pick["reason"] if first else "first choice didn't load; next best candidate",
                    "source": pick["source"] if first else "fallback",
                })
                break
            attempts.append({"name": instrument_palette.display_name(item), "error": detail})
        else:
            # Library unavailable (no index) or every candidate failed: use a stock device.
            dev = defaults.get("drums" if role in ("drums", "perc") else "melodic") or "Drift"
            try:
                await _bridge_call("load_device", {"track_index": ti, "device_name": dev}, timeout_s=10.0)
                entry.update({"action": "fallback", "name": dev, "source": "stock_device",
                              "reason": "no library preset could be loaded" if order else "browser index not built yet"})
                _record_palette_load(ti, style, dev)
            except BridgeError as e:
                entry.update({"action": "failed", "error": str(e)})
        if attempts:
            entry["attempts"] = attempts
        if role in restyled:
            entry["replaced_style"] = restyled[role]
        _DRUM_MAPS.invalidate(ti)
        results.append(entry)

    results.sort(key=lambda r: r["track_index"])

    drum_bus = None
    drums_plan = next((p for p in plan if p["role"] == "drums"), None)
    if drums_plan is not None:
        try:
            drum_bus = await _apply_drum_bus(style, int(drums_plan["track_index"]))
        except BridgeError as e:
            drum_bus = {"track_index": int(drums_plan["track_index"]), "role": "drum_bus", "action": "failed", "error": str(e)}

    # Reverb/delay returns belong to the whole set, so a re-pick of a few roles leaves them alone.
    returns = None
    if roles is None:
        try:
            returns = await _apply_returns(style)
        except BridgeError as e:
            returns = {"action": "failed", "error": str(e)}

    return {
        "ok": all(r.get("action") != "failed" for r in results),
        "style": style,
        "results": results,
        "drum_bus": drum_bus,
        "returns": returns,
        "ai": ai_meta,
        "index_items": len(items),
    }


def _style_drum_bus(style: str) -> dict | None:
    cfg = STYLE_CONFIG.get((style or "").strip().lower()) if isinstance(STYLE_CONFIG, dict) else None
    bus = cfg.get("drum_bus") if isinstance(cfg, dict) else None
    return bus if isinstance(bus, dict) and isinstance(bus.get("chain"), list) else None


def _match_param(params: list[dict], key: str) -> dict | None:
    """Find a device parameter by name; key may list alternatives ("Boom Amt|Boom")."""
    aliases = [a.strip().lower() for a in key.split("|") if a.strip()]
    for a in aliases:
        hit = next((p for p in params if str(p.get("name") or "").lower() == a), None)
        if hit:
            return hit
    for a in aliases:
        hit = next((p for p in params if str(p.get("name") or "").lower().startswith(a)), None)
        if hit:
            return hit
    return None


async def _apply_drum_bus(style: str, track_index: int) -> dict:
    """Put the style's drum bus (e.g. Drum Buss for harder techno kicks) after the kit.

    The first device in the style's chain that this Live edition has is used (Drum Buss isn't in
    every edition; Saturator is the fallback). A bus Pulse added for another style is removed.
    Parameter values in styles.json are normalized 0..1 across each parameter's range.
    """
    state = _read_palette_state()
    rec = state.get(f"{int(track_index)}:bus")
    info = await _bridge_call("get_track", {"track_index": track_index})
    devices = info.get("devices") or []
    out: dict = {"track_index": track_index, "role": "drum_bus"}

    if isinstance(rec, dict) and rec.get("style") != style:
        idx = next((d["index"] for d in reversed(devices) if d.get("name") == rec.get("device")), None)
        if idx is not None:
            await _bridge_call("delete_device", {"track_index": track_index, "device_index": idx})
            out["removed"] = rec.get("device")
        _record_palette_load(track_index, style, None, slot="bus")
        rec = None
        info = await _bridge_call("get_track", {"track_index": track_index})
        devices = info.get("devices") or []

    spec = _style_drum_bus(style)
    if not spec:
        out["action"] = "removed" if out.get("removed") else "none"
        return out

    available = set(((BRIDGE.capabilities or {}).get("devices") or {}).get("audio_effects") or [])
    chain = [c for c in spec["chain"] if isinstance(c, dict) and c.get("device")]
    choice = next((c for c in chain if not available or c["device"] in available), None)
    if choice is None:
        out.update({"action": "skipped", "reason": "none of " + ", ".join(c["device"] for c in chain) + " is in this Live edition"})
        return out
    name = choice["device"]

    idx = next((d["index"] for d in reversed(devices) if d.get("name") == name), None)
    inst_idx = next((d["index"] for d in devices if d.get("type") == "instrument"), None)
    if idx is not None and inst_idx is not None and idx < inst_idx:
        # The kit was reloaded after the bus; the bus must come after the kit to process it.
        await _bridge_call("delete_device", {"track_index": track_index, "device_index": idx})
        idx = None
    if idx is None:
        await _bridge_call("load_device", {"track_index": track_index, "device_name": name}, timeout_s=10.0)
        info = await _bridge_call("get_track", {"track_index": track_index})
        idx = next((d["index"] for d in reversed(info.get("devices") or []) if d.get("name") == name), None)
        if idx is None:
            out.update({"action": "failed", "error": f"{name} didn't appear on the track"})
            return out
        out["action"] = "loaded"
    else:
        out["action"] = "updated"

    params = (await _bridge_call("get_device_params", {"track_index": track_index, "device_index": idx}) or {}).get("parameters") or []
    set_params, missing = {}, []
    for key, norm in (choice.get("params") or {}).items():
        p = _match_param(params, key)
        if p is None:
            missing.append(key)
            continue
        lo, hi = float(p.get("min", 0.0)), float(p.get("max", 1.0))
        value = lo + max(0.0, min(1.0, float(norm))) * (hi - lo)
        if p.get("is_quantized"):
            value = float(round(value))
        await _bridge_call("set_device_param", {"track_index": track_index, "device_index": idx, "param_index": p["index"], "value": value})
        set_params[p["name"]] = round(value, 3)

    _record_palette_load(track_index, style, name, slot="bus")
    out.update({"device": name, "params": set_params})
    if missing:
        out["unmatched_params"] = missing
    return out


# Return tracks Pulse sets up for space. The first device in each chain that this Live edition has
# is used. A return that already carries one of those devices (Live's default set has "A-Reverb"
# and "B-Delay") is reused as it is. Params are normalized 0..1 like the drum bus.
PULSE_RETURNS = [
    {"key": "reverb", "name": "PS-Reverb", "chain": ["Reverb", "Hybrid Reverb"], "params": {"Dry/Wet": 1.0}},
    {"key": "delay", "name": "PS-Delay", "chain": ["Delay", "Echo", "Simple Delay", "Ping Pong Delay"], "params": {"Dry/Wet": 1.0}},
]
# Send levels per role (Live's send range, 1.0 = 0 dB). Kick and bass stay dry to keep the low end
# tight. A style can override any of these in styles.json: "sends": {"reverb": {"pad": 0.7}}.
DEFAULT_SENDS = {
    "reverb": {"drums": 0.0, "bass": 0.0, "perc": 0.35, "stabs": 0.45, "fx": 0.6, "chords": 0.45, "pad": 0.6},
    "delay": {"drums": 0.0, "bass": 0.0, "perc": 0.25, "stabs": 0.45, "fx": 0.45, "chords": 0.2, "pad": 0.15},
}
_BRIDGE_UPDATE_HINT = (
    "PulseBridge needs updating for return tracks: run python pulse_bridge/install.py, "
    "then reselect PulseBridge in Live's Control Surface settings."
)


def _style_sends(style: str) -> dict[str, dict[str, float]]:
    cfg = STYLE_CONFIG.get((style or "").strip().lower()) if isinstance(STYLE_CONFIG, dict) else None
    override = cfg.get("sends") if isinstance(cfg, dict) else None
    out = {k: dict(v) for k, v in DEFAULT_SENDS.items()}
    if isinstance(override, dict):
        for key, levels in override.items():
            if key in out and isinstance(levels, dict):
                out[key].update({r: float(v) for r, v in levels.items() if isinstance(v, (int, float))})
    return out


async def _ensure_return(spec: dict, returns: list[dict], available: set[str]) -> dict:
    """Find or create the return for spec: {key, return_index, name, device, action}."""
    out: dict = {"key": spec["key"]}
    for r in returns:
        names = [d.get("name") for d in r.get("devices") or []]
        dev = next((d for d in spec["chain"] if d in names), None)
        if dev:
            out.update({"return_index": r["return_index"], "name": r.get("name"), "device": dev, "action": "reused"})
            return out
    chain = [d for d in spec["chain"] if not available or d in available]
    if not chain:
        out.update({"action": "skipped", "reason": "none of " + ", ".join(spec["chain"]) + " is in this Live edition"})
        return out

    ours = next((r for r in returns if r.get("name") == spec["name"]), None)
    if ours is not None:
        ri = int(ours["return_index"])  # our return lost its device; put it back
        out["action"] = "repaired"
    else:
        try:
            ri = int((await _bridge_call("create_return_track", {"name": spec["name"]}) or {})["return_index"])
        except BridgeError as e:
            if "return_track_limit" in str(e):
                out.update({"action": "skipped", "reason": "this Live edition allows no more return tracks"})
                return out
            raise
        out["action"] = "created"
    name = chain[0]
    await _bridge_call("load_device", {"return_index": ri, "device_name": name}, timeout_s=10.0)
    info = await _bridge_call("get_track", {"return_index": ri})
    di = next((d["index"] for d in reversed(info.get("devices") or []) if d.get("name") == name), None)
    if di is None:
        out.update({"return_index": ri, "action": "failed", "error": f"{name} didn't appear on the return track"})
        return out
    params = (await _bridge_call("get_device_params", {"return_index": ri, "device_index": di}) or {}).get("parameters") or []
    for key, norm in spec["params"].items():
        p = _match_param(params, key)
        if p is not None:
            lo, hi = float(p.get("min", 0.0)), float(p.get("max", 1.0))
            await _bridge_call("set_device_param", {"return_index": ri, "device_index": di, "param_index": p["index"], "value": lo + norm * (hi - lo)})
    out.update({"return_index": ri, "name": spec["name"], "device": name})
    return out


async def _apply_returns(style: str) -> dict:
    """Make sure the set has a reverb and a delay return, then set each Pulse track's sends.

    A send is only changed while it is still at zero or at the level Pulse last set, so sends the
    producer has moved by hand are left alone.
    """
    try:
        returns = (await _bridge_call("get_return_tracks") or {}).get("returns") or []
    except BridgeError as e:
        if "unknown_command" in str(e):
            return {"action": "skipped", "reason": _BRIDGE_UPDATE_HINT}
        raise
    available = set(((BRIDGE.capabilities or {}).get("devices") or {}).get("audio_effects") or [])
    rets = []
    for spec in PULSE_RETURNS:
        r = await _ensure_return(spec, returns, available)
        rets.append(r)
        if r["action"] == "created":
            returns.append({"return_index": r["return_index"], "name": r["name"], "devices": [{"name": r["device"]}]})

    levels = _style_sends(style)
    state = _read_palette_state()
    sends: list[dict] = []
    for p in PULSE_TRACK_PLAN:
        ti, role = int(p["track_index"]), p["role"]
        try:
            current = (await _bridge_call("get_track", {"track_index": ti}) or {}).get("sends") or []
        except BridgeError:
            continue
        row = {"track_index": ti, "role": role, "set": {}, "kept": {}}
        for r in rets:
            ri = r.get("return_index")
            if ri is None or r["action"] == "failed" or ri >= len(current):
                continue
            key = f"{ti}:send:{r['key']}"
            want = float(levels.get(r["key"], {}).get(role, 0.0))
            prior = (state.get(key) or {}).get("value")
            now = float(current[ri])
            if now > 0.001 and (prior is None or abs(now - float(prior)) > 0.01):
                row["kept"][r["key"]] = round(now, 3)
                continue
            if abs(now - want) > 0.001:
                await _bridge_call("set_send", {"track_index": ti, "send_index": ri, "value": want})
            state[key] = {"style": style, "value": want}
            row["set"][r["key"]] = want
        sends.append(row)
    _write_palette_state(state)

    failed = any(r["action"] == "failed" for r in rets)
    done = [r for r in rets if r["action"] not in ("failed", "skipped")]
    return {"action": "failed" if failed else ("applied" if done else "skipped"), "returns": rets, "sends": sends}


class PaletteRequest(BaseModel):
    style: str
    prompt: str | None = None
    replace: bool = False
    roles: list[str] | None = None


@app.post("/instruments/palette")
async def instruments_palette(req: PaletteRequest):
    """Pick and load instruments for the Pulse tracks (keeps existing ones unless replace)."""
    if not BRIDGE.connected:
        return {"ok": False, "error": "bridge_not_connected", "bridge": BRIDGE.status()}
    style = (req.style or "").strip().lower()
    if not style:
        return {"ok": False, "error": "missing_style"}
    roles = [r for r in (req.roles or []) if r in instrument_palette.ROLE_POOLS] or None
    return await _apply_instrument_palette(style, prompt=req.prompt, replace=bool(req.replace), roles=roles)


def _plan_for_role(role: str) -> dict | None:
    return next((p for p in PULSE_TRACK_PLAN if p["role"] == role), None)


class InstrumentCandidatesRequest(BaseModel):
    style: str
    role: str
    prompt: str | None = None
    query: str | None = None
    per_group: int = 40


@app.post("/instruments/candidates")
async def instruments_candidates(req: InstrumentCandidatesRequest):
    """A Pulse role's library sounds grouped by fit with the style, plus what's on its track now."""
    role = (req.role or "").strip().lower()
    plan = _plan_for_role(role)
    if plan is None or role not in instrument_palette.ROLE_POOLS:
        return {"ok": False, "error": "unknown_role", "roles": [p["role"] for p in PULSE_TRACK_PLAN]}
    style = (req.style or "").strip().lower()
    items = _browser_index_items()
    if not items:
        return {"ok": False, "error": "index_not_built", "hint": "Build the browser index first (Library tab)."}
    pool = instrument_palette.role_pool(items, role)
    groups = instrument_palette.group_candidates(
        pool, role, style, req.prompt, per_group=max(1, min(200, int(req.per_group))), query=req.query,
    )
    current = None
    if BRIDGE.connected:
        try:
            playable, _ = _track_sound_state(await _bridge_call("get_track", {"track_index": int(plan["track_index"])}))
            current = playable[0].get("name") if playable else None
        except BridgeError:
            pass
    return {"ok": True, "style": style, "role": role, "track_index": int(plan["track_index"]),
            "current": current, "pool_size": len(pool), "groups": groups}


@app.get("/instruments/current")
async def instruments_current():
    """What's on each Pulse track now, and the style Pulse picked it for (if Pulse loaded it)."""
    if not BRIDGE.connected:
        return {"ok": False, "error": "bridge_not_connected", "bridge": BRIDGE.status()}
    state = _read_palette_state()
    results = []
    for p in PULSE_TRACK_PLAN:
        ti, role = int(p["track_index"]), p["role"]
        row = {"track_index": ti, "role": role, "action": "current"}
        try:
            info = await _bridge_call("get_track", {"track_index": ti})
        except BridgeError as e:
            results.append({**row, "action": "failed", "error": str(e)})
            continue
        playable, _ = _track_sound_state(info)
        name = str(playable[0].get("name") or "") if playable else None
        rec = state.get(str(ti))
        picked_for = rec.get("style") if isinstance(rec, dict) and name and rec.get("device") == name else None
        row.update({"name": name, "track_name": info.get("name"), "picked_for": picked_for})
        if role in ("drums", "perc") and playable and not any(d.get("is_drum_rack") for d in playable):
            row["warning"] = "Not a drum kit: drum patterns will play as pitched notes."
        results.append(row)
    return {"ok": True, "results": results}


@app.post("/instruments/suggest")
async def instruments_suggest(req: InstrumentCandidatesRequest):
    """The AI's top 3 sounds for one Pulse role, given the style and what's on the other tracks."""
    if not _ai_configured():
        return {"ok": False, "error": "ai_not_configured", "hint": "Set up an AI provider on the System page."}
    listing = await instruments_candidates(req)
    if not listing.get("ok"):
        return listing
    others: dict[str, str] = {}
    if BRIDGE.connected:
        for p in PULSE_TRACK_PLAN:
            if p["role"] == listing["role"]:
                continue
            try:
                playable, _ = _track_sound_state(await _bridge_call("get_track", {"track_index": int(p["track_index"])}))
            except BridgeError:
                continue
            if playable:
                others[p["role"]] = str(playable[0].get("name") or "")
    picks, meta = await instrument_palette.ai_top_picks(
        _call_ai_async, groups=listing["groups"], role=listing["role"], style=listing["style"],
        prompt=req.prompt, others=others, current=listing.get("current"),
    )
    if not picks:
        return {"ok": False, "error": meta.get("error") or "ai_failed", "hint": meta.get("hint"), "ai": meta}
    return {"ok": True, "role": listing["role"], "style": listing["style"], "picks": picks, "others": others, "ai": meta}


class InstrumentSwapRequest(BaseModel):
    style: str
    role: str
    uri: str


@app.post("/instruments/swap")
async def instruments_swap(req: InstrumentSwapRequest):
    """Replace the instrument on a Pulse role's track with one hand-picked from /instruments/candidates."""
    if not BRIDGE.connected:
        return {"ok": False, "error": "bridge_not_connected", "bridge": BRIDGE.status()}
    role = (req.role or "").strip().lower()
    plan = _plan_for_role(role)
    if plan is None:
        return {"ok": False, "error": "unknown_role"}
    item = next((it for it in instrument_palette.role_pool(_browser_index_items(), role) if it.get("uri") == req.uri), None)
    if item is None:
        return {"ok": False, "error": "not_in_role_pool", "hint": "Pick a sound from the list for this role."}
    style = (req.style or "").strip().lower()
    ti = int(plan["track_index"])

    info = await _bridge_call("get_track", {"track_index": ti})
    playable, empty = _track_sound_state(info)
    previous = playable[0].get("name") if playable else None
    for d in sorted(playable + empty, key=lambda d: d["index"], reverse=True):
        try:
            await _bridge_call("delete_device", {"track_index": ti, "device_index": d["index"]})
        except BridgeError:
            pass
    ok, detail = await _load_and_verify(ti, item)
    _DRUM_MAPS.invalidate(ti)
    if not ok:
        return {"ok": False, "error": "load_failed", "detail": detail, "previous": previous,
                "hint": "The old instrument was removed; pick another sound or Re-pick."}
    _record_palette_load(ti, style, detail)
    result = {"track_index": ti, "role": role, "action": "loaded", "name": instrument_palette.display_name(item),
              "folder": item.get("path"), "reason": f"hand-picked (was {previous})" if previous else "hand-picked",
              "source": "user"}
    drum_bus = None
    if role == "drums":
        # A reloaded kit lands after the drum bus; this puts the bus back after the kit.
        try:
            drum_bus = await _apply_drum_bus(style, ti)
        except BridgeError as e:
            drum_bus = {"track_index": ti, "role": "drum_bus", "action": "failed", "error": str(e)}
    return {"ok": True, "result": result, "previous": previous, "drum_bus": drum_bus}


class _DrumMapCache:
    """Per-track lane -> pad note maps, dropped whenever a track's devices change."""

    def __init__(self):
        self.lock = threading.Lock()
        self.maps: dict[int, dict[str, int]] = {}

    def invalidate(self, track_index: int | None = None):
        with self.lock:
            if track_index is None:
                self.maps.clear()
            else:
                self.maps.pop(int(track_index), None)

    def on_event(self, event: str, data: dict):
        if event == "track.devices":
            self.invalidate(data.get("track_index"))
        elif event in ("song.tracks", "bridge.connected", "bridge.disconnected"):
            self.invalidate()

    def get(self, track_index: int) -> dict[str, int]:
        with self.lock:
            if track_index in self.maps:
                return self.maps[track_index]
        if not BRIDGE.connected:
            return {}
        try:
            res = BRIDGE.request("get_drum_pads", {"track_index": int(track_index)}, timeout_s=2.0)
            mapping = instrument_palette.map_drum_pads(res.get("pads") or [])
        except BridgeError:
            return {}
        with self.lock:
            self.maps[int(track_index)] = mapping
        return mapping


_DRUM_MAPS = _DrumMapCache()
BRIDGE.on_event(_DRUM_MAPS.on_event)


@app.get("/mcp/index/search")
def mcp_index_search(q: str, limit: int = 50, include_clips: bool = False):
    data = BROWSER_INDEX.get()
    if not isinstance(data, dict):
        return {"ok": False, "error": "index_not_built"}
    items = data.get("items")
    if not isinstance(items, list):
        return {"ok": False, "error": "index_bad_format"}

    qq = str(q or "").strip().lower()
    if not qq:
        return {"ok": False, "error": "missing_q"}

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").lower()
        path = str(it.get("path") or "").lower()
        uri = str(it.get("uri") or "")

        # Exclude MIDI clip content by default (not loadable via load_browser_item).
        if not bool(include_clips):
            if str(it.get("name") or "").lower().endswith(".alc"):
                continue
            if "midi clips" in path:
                continue
            if "midi%20clips" in uri.lower():
                continue

        if not bool(include_clips):
            if path.startswith("clips"):
                continue
            if uri.startswith("query:Clips"):
                continue
        if qq in name or qq in path:
            out.append(it)
        if len(out) >= int(limit):
            break

    return {"ok": True, "q": q, "count": len(out), "items": out}


async def _openai_choose_item_from_index(*, style: str, role: str, candidates: list[dict], prompt: str | None, temperature: float):
    schema = {"choice_id": 123, "reason": "..."}

    # Keep token usage down: send limited fields and avoid long URIs.
    compact = [{"id": c.get("id"), "name": c.get("name"), "path": c.get("path")} for c in candidates]

    system = (
        "You are an expert music producer. "
        "Pick EXACTLY ONE browser item from the provided list that best fits the requested style and role. "
        "Return STRICT JSON only (no markdown). "
        "Schema: {choice_id: number, reason: string}. "
        "Rules: choice_id must exactly match one of the candidate id values."
    )

    style_key = (style or "").strip().lower()
    rec = STYLE_RECOMMENDATIONS.get(style_key) if isinstance(STYLE_RECOMMENDATIONS, dict) else None
    timbre = _style_timbre_hints(style_key)
    timbre_text = timbre.get("text") if isinstance(timbre, dict) else ""

    user = (
        f"Style: {style_key}\n"
        f"Role: {role}\n"
        f"Style recommendations (if known): {json.dumps(rec) if isinstance(rec, dict) else 'null'}\n"
        f"Timbre notes (if any): {timbre_text}\n\n"
        f"Candidates: {json.dumps(compact)}\n\n"
        f"Extra prompt: {prompt or ''}\n\n"
        f"Return JSON like: {json.dumps(schema)}"
    )

    obj, meta = await _call_ai_async(system, user, float(temperature))
    if not meta.get("ok"):
        return None, meta
    if not isinstance(obj, dict):
        return None, {"ok": False, "error": "ai_bad_json"}
    choice_id = obj.get("choice_id")
    reason = obj.get("reason")

    try:
        choice_id_int = int(choice_id)
    except Exception:
        return None, {"ok": False, "error": "ai_missing_choice_id", "raw": obj}
    allowed = {int(c.get("id")) for c in candidates if isinstance(c, dict) and isinstance(c.get("id"), int)}
    if choice_id_int not in allowed:
        return None, {"ok": False, "error": "ai_choice_not_in_candidates", "choice_id": choice_id, "raw": obj}
    if not isinstance(reason, str) or not reason:
        reason = "AI choice"
    return {"choice_id": choice_id_int, "reason": reason}, {"ok": True}


def _heuristic_choose_item_from_index(style: str, role: str, candidates: list[dict]) -> tuple[dict | None, str]:
    s = (style or "").strip().lower()
    r = (role or "").strip().lower()
    # Extremely simple fallback
    if not candidates:
        return None, "heuristic: empty candidates"

    def find_name_contains(subs: list[str]):
        for c in candidates:
            if not isinstance(c, dict):
                continue
            nm = str(c.get("name") or "").lower()
            if any(x in nm for x in subs):
                return c
        return None

    if r in {"drums", "kit"}:
        pick = find_name_contains(["909", "808", "drum", "kit", "rack"])
        if pick:
            return pick, "heuristic: drums keyword match"
    if r in {"bass", "sub"}:
        if "acid" in s:
            pick = find_name_contains(["303", "acid"])
            if pick:
                return pick, "heuristic: acid -> 303/acid"
        pick = find_name_contains(["bass", "sub", "mono"])
        if pick:
            return pick, "heuristic: bass keyword match"

    return (candidates[0] if candidates else None), "heuristic: first candidate"


def _style_timbre_hints(style: str) -> dict:
    s = (style or "").strip().lower()
    # Keep this lightweight: it's used for both heuristic scoring and AI prompting.
    if "tekno" in s:
        return {
            "positive": [
                "dark",
                "industrial",
                "raw",
                "dist",
                "distortion",
                "drive",
                "satur",
                "rumble",
                "mono",
                "hard",
                "noise",
            ],
            "negative": [
                "bright",
                "shiny",
                "sparkle",
                "happy",
                "pop",
                "bell",
                "pluck",
                "plucky",
                "tropical",
                "future",
            ],
            "text": (
                "Tekno: dark/industrial/raw timbre. Prefer heavy/dirty/rumble/mono sounds. "
                "Avoid bright/shiny/plucky/bell-like presets."
            ),
        }

    if "hard" in s and "techno" in s:
        return {
            "positive": ["dark", "hard", "industrial", "mono", "drive", "dist", "rumble"],
            "negative": ["bright", "pluck", "bell", "happy"],
            "text": "Hard techno: darker/industrial/mono, avoid bright/plucky/bell-like presets.",
        }

    return {"positive": [], "negative": [], "text": ""}


def _rank_candidates_for_role(items: list[dict], *, style: str, role: str, q: str | None, limit: int) -> list[dict]:
    role = (role or "").strip().lower()
    qq = (q or "").strip().lower()
    q_tokens = [t for t in re.split(r"\s+", qq) if t] if qq else []
    out: list[tuple[int, dict]] = []

    def score_item(it: dict) -> int:
        name = str(it.get("name") or "").lower()
        path = str(it.get("path") or "").lower()
        uri = str(it.get("uri") or "")
        uri_l = uri.lower()

        s = 0
        # Prefer Ableton device library over clips/presets where possible
        if "packs/core library/devices" in path or path.startswith("audio_effects/") or path.startswith("midi_effects/"):
            s += 25
        if path.startswith("clips"):
            s -= 50
        if "plugins" in path:
            s -= 10

        # Prefer short query URIs (FileId) over long LivePacks URIs which sometimes fail to resolve.
        if uri.startswith("query:Drums#") or uri.startswith("query:Synths#") or uri.startswith("query:Sounds#"):
            s += 30
        if uri.startswith("query:AudioFx#") or uri.startswith("query:MidiFx#"):
            s += 15
        if uri.startswith("query:LivePacks#"):
            s -= 25
        if "midi%20clips" in uri_l:
            s -= 80

        if q_tokens:
            hits = 0
            for tok in q_tokens:
                if tok in name or tok in path:
                    hits += 1
            s += hits * 15

        # Role hints
        if role in {"drums", "kit"}:
            if "drum" in name or "drum" in path:
                s += 25
            if "rack" in name or "rack" in path:
                s += 15
            if "909" in name:
                s += 10
            if "808" in name:
                s += 5
        elif role in {"bass", "sub"}:
            if "bass" in name or "bass" in path:
                s += 25
            if "sub" in name:
                s += 10
            if "303" in name or "acid" in name:
                s += 10
        elif role in {"pads", "pad"}:
            if "pad" in name or "pad" in path:
                s += 25
            if "poly" in name:
                s += 8
        elif role in {"stabs", "chords"}:
            if "chord" in name or "keys" in name or "stab" in name:
                s += 25
        elif role in {"lead", "melody"}:
            if "lead" in name or "pluck" in name:
                s += 20

        # Mild generic preference for Ableton synth devices
        if name in {"wavetable", "operator", "analog"}:
            s += 12

        # Style timbre bias (mainly for Tekno): prefer dark/industrial/dirty, avoid bright/plucky.
        style_hints = _style_timbre_hints(style)
        pos = style_hints.get("positive") if isinstance(style_hints, dict) else None
        neg = style_hints.get("negative") if isinstance(style_hints, dict) else None
        if isinstance(pos, list):
            if any(tok in name or tok in path for tok in pos):
                s += 12
        if isinstance(neg, list):
            if any(tok in name or tok in path for tok in neg):
                s -= 18

        return s

    for it in items:
        if not isinstance(it, dict):
            continue
        if not it.get("is_loadable"):
            continue
        if not isinstance(it.get("id"), int):
            continue

        # Exclude clip-like content; load_browser_item is aimed at devices/presets.
        nm0 = str(it.get("name") or "").lower()
        p0 = str(it.get("path") or "").lower()
        u0 = str(it.get("uri") or "").lower()
        if nm0.endswith(".alc"):
            continue
        if p0.startswith("clips"):
            continue
        if "midi clips" in p0:
            continue
        if "midi%20clips" in u0:
            continue
        if u0.startswith("query:clips"):
            continue
        # Pre-filter by query tokens to cut work
        if q_tokens:
            name = str(it.get("name") or "").lower()
            path = str(it.get("path") or "").lower()
            if not any(tok in name or tok in path for tok in q_tokens):
                continue
        out.append((score_item(it), it))

    out.sort(key=lambda t: t[0], reverse=True)
    hard_cap = min(int(limit), 120)
    return [it for _, it in out[:hard_cap]]


@app.post("/instruments/recommend_apply_from_index")
async def recommend_apply_from_index(req: RecommendFromBrowserIndexRequest):
    data = BROWSER_INDEX.get()
    if not isinstance(data, dict):
        # Best-effort lazy load (useful after server restarts)
        disk = _load_browser_index_from_disk()
        if isinstance(disk, dict):
            BROWSER_INDEX.set(disk)
            data = disk
    if not isinstance(data, dict):
        return {"ok": False, "error": "index_not_built", "hint": "Call POST /mcp/index/rebuild first."}
    items = data.get("items")
    if not isinstance(items, list):
        return {"ok": False, "error": "index_bad_format"}

    id_to_item: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = it.get("id")
        if isinstance(iid, int) and iid not in id_to_item:
            id_to_item[iid] = it

    role = (req.role or "").strip().lower()
    style = (req.style or "").strip().lower()
    qq = (req.q or "").strip().lower()

    cand = _rank_candidates_for_role(items, style=style, role=role, q=qq or None, limit=int(req.limit))

    if not cand:
        return {"ok": False, "error": "no_candidates", "hint": "Try rebuilding index with category_type=all and/or adjust role/q."}

    # Prefer AI if configured
    if _ai_configured():
        pick, meta = await _openai_choose_item_from_index(
            style=style,
            role=role,
            candidates=cand,
            prompt=req.prompt,
            temperature=req.temperature,
        )
        if meta.get("ok") and isinstance(pick, dict):
            choice_id = pick.get("choice_id")
            if not isinstance(choice_id, int):
                return {"ok": False, "error": "ai_missing_choice_id"}
            chosen = id_to_item.get(int(choice_id))
            if not isinstance(chosen, dict):
                chosen = next((x for x in cand if isinstance(x, dict) and int(x.get("id") or 0) == int(choice_id)), None)
            if not isinstance(chosen, dict):
                return {"ok": False, "error": "chosen_not_found"}
            choice_uri = str(chosen.get("uri"))
            # Apply (retry with safer candidates if the chosen URI is not resolvable)
            try:
                load_res = MCP_SOCKET.send_command(
                    "load_browser_item",
                    {"track_index": int(req.track_index), "item_uri": str(choice_uri)},
                    timeout_s=25.0,
                )
            except Exception as e:
                msg = str(e)
                if "not found" in msg.lower():
                    fallback = next(
                        (x for x in cand if isinstance(x, dict) and str(x.get("uri") or "").startswith(("query:Drums#", "query:Synths#", "query:Sounds#"))),
                        None,
                    )
                    if isinstance(fallback, dict) and fallback.get("uri") and str(fallback.get("uri")) != choice_uri:
                        choice_uri = str(fallback.get("uri"))
                        chosen = fallback
                        load_res = MCP_SOCKET.send_command(
                            "load_browser_item",
                            {"track_index": int(req.track_index), "item_uri": str(choice_uri)},
                            timeout_s=25.0,
                        )
                    else:
                        raise
                else:
                    raise
            return {
                "ok": True,
                "source": "ai",
                "style": style,
                "role": role,
                "track_index": int(req.track_index),
                "choice": {"name": chosen.get("name"), "uri": choice_uri, "path": chosen.get("path")},
                "reason": pick.get("reason"),
                "load_result": load_res,
                "candidate_count": len(cand),
            }

    chosen, reason = _heuristic_choose_item_from_index(style, role, cand)
    if not isinstance(chosen, dict) or not chosen.get("uri"):
        return {"ok": False, "error": "no_choice"}
    choice_uri = str(chosen.get("uri"))

    # Apply with a fallback if the first URI can't be resolved.
    try:
        load_res = MCP_SOCKET.send_command(
            "load_browser_item",
            {"track_index": int(req.track_index), "item_uri": choice_uri},
            timeout_s=25.0,
        )
    except Exception as e:
        msg = str(e)
        if "not found" in msg.lower():
            fallback = next(
                (x for x in cand if isinstance(x, dict) and str(x.get("uri") or "").startswith(("query:Drums#", "query:Synths#", "query:Sounds#"))),
                None,
            )
            if isinstance(fallback, dict) and fallback.get("uri") and str(fallback.get("uri")) != choice_uri:
                chosen = fallback
                choice_uri = str(fallback.get("uri"))
                load_res = MCP_SOCKET.send_command(
                    "load_browser_item",
                    {"track_index": int(req.track_index), "item_uri": choice_uri},
                    timeout_s=25.0,
                )
            else:
                raise
        else:
            raise
    return {
        "ok": True,
        "source": "heuristic",
        "style": style,
        "role": role,
        "track_index": int(req.track_index),
        "choice": {"name": chosen.get("name"), "uri": choice_uri, "path": chosen.get("path")},
        "reason": reason,
        "load_result": load_res,
        "candidate_count": len(cand),
    }


def _heuristic_choose_instrument(style: str, role: str, candidates: list[str]) -> tuple[str | None, str]:
    s = (style or "").strip().lower()
    r = (role or "").strip().lower()

    def _first_contains(substrs: list[str]) -> str | None:
        for cand in candidates:
            cl = cand.lower()
            if any(sub in cl for sub in substrs):
                return cand
        return None

    # Role-specific hints
    if r in {"bass", "sub"}:
        if "acid" in s:
            pick = _first_contains(["303", "acid"]) 
            if pick:
                return pick, "heuristic: acid style -> 303/acid bass"
        if "psy" in s or "psytrance" in s:
            pick = _first_contains(["psy", "tight", "sub"]) 
            if pick:
                return pick, "heuristic: psy style -> tight/sub bass"
        pick = _first_contains(["bass", "sub", "mono"]) 
        if pick:
            return pick, "heuristic: role bass -> first candidate containing bass/sub/mono"

    if r in {"drums", "kit", "drum_rack"}:
        pick = _first_contains(["drum", "rack", "kit", "909", "808"]) 
        if pick:
            return pick, "heuristic: role drums -> drum rack/kit"

    if r in {"stabs", "chords", "pads"}:
        pick = _first_contains(["pad", "chord", "stab", "poly", "keys"]) 
        if pick:
            return pick, "heuristic: role stabs/pads -> pad/chord/stab/poly"

    if r in {"lead", "melody"}:
        pick = _first_contains(["lead", "pluck", "mono", "synth"]) 
        if pick:
            return pick, "heuristic: role lead -> lead/pluck/mono/synth"

    return (candidates[0] if candidates else None), "heuristic: default first candidate"


async def _openai_choose_instrument_from_list(
    *,
    style: str,
    role: str,
    candidates: list[str],
    prompt: str | None,
    temperature: float,
):
    style_key = (style or "").strip().lower()
    role_key = (role or "").strip().lower()
    cfg = STYLE_CONFIG.get(style_key) if isinstance(STYLE_CONFIG, dict) else None
    rec = STYLE_RECOMMENDATIONS.get(style_key) if isinstance(STYLE_RECOMMENDATIONS, dict) else None

    schema = {
        "choice": "...",
        "reason": "short reason",
    }

    system = (
        "You are an expert music producer. "
        "Pick EXACTLY ONE instrument/preset name from the provided candidates list. "
        "Return STRICT JSON ONLY (no markdown, no prose). "
        "Output schema: {choice: string, reason: string}. "
        "Rules: choice must exactly match one entry from candidates. "
        "If unsure, pick the safest, most generic candidate for the requested role."
    )

    user = (
        f"Style: {style_key}\n"
        f"Role: {role_key}\n"
        f"Style tempo (if known): {cfg.get('tempo') if isinstance(cfg, dict) else 'unknown'}\n"
        f"Style recommendations (if known): {json.dumps(rec) if isinstance(rec, dict) else 'null'}\n\n"
        f"Timbre notes (if any): {_style_timbre_hints(style_key).get('text', '')}\n\n"
        f"Candidates: {json.dumps(candidates)}\n\n"
        f"Extra prompt: {prompt or ''}\n\n"
        f"Return JSON like: {json.dumps(schema)}"
    )

    obj, meta = await _call_ai_async(system, user, temperature)
    if not meta.get("ok"):
        return None, meta
    if not isinstance(obj, dict):
        return None, {"ok": False, "error": "ai_bad_json"}
    choice = obj.get("choice")
    reason = obj.get("reason")
    if not isinstance(choice, str) or not choice:
        return None, {"ok": False, "error": "ai_missing_choice", "raw": obj}
    if choice not in candidates:
        return None, {"ok": False, "error": "ai_choice_not_in_candidates", "choice": choice, "raw": obj}
    if not isinstance(reason, str) or not reason:
        reason = "AI choice"
    return {"choice": choice, "reason": reason}, {"ok": True}


@app.post("/instruments/recommend")
async def recommend_instrument(req: RecommendInstrumentRequest):
    style = (req.style or "").strip()
    role = (req.role or "").strip()
    if not style:
        return {"ok": False, "error": "missing_style"}
    if not role:
        return {"ok": False, "error": "missing_role"}

    candidates = _normalize_candidates(req.candidates)
    if not candidates:
        # If you don't pass candidates, allow lookup from knowledge base catalog.
        catalog = INSTRUMENT_CATALOG if isinstance(INSTRUMENT_CATALOG, dict) else {}
        by_role = catalog.get(role.lower())
        if isinstance(by_role, list):
            candidates = _normalize_candidates([str(x) for x in by_role])

    if not candidates:
        # Final fallback: use cached Ableton browser index (names only).
        # This is useful for UI flows that just want "a good pick" without providing candidates.
        data = BROWSER_INDEX.get()
        if not isinstance(data, dict):
            disk = _load_browser_index_from_disk()
            if isinstance(disk, dict):
                BROWSER_INDEX.set(disk)
                data = disk
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            ranked = _rank_candidates_for_role(items, style=style.strip().lower(), role=role.strip().lower(), q=None, limit=80)
            seen: set[str] = set()
            out: list[str] = []
            for it in ranked:
                if not isinstance(it, dict):
                    continue
                nm = str(it.get("name") or "").strip()
                if not nm:
                    continue
                if nm in seen:
                    continue
                seen.add(nm)
                out.append(nm)
            candidates = _normalize_candidates(out)

    if not candidates:
        return {"ok": False, "error": "no_candidates", "hint": "Pass candidates[] or define instrument_catalog.<role> in knowledge/styles.json"}

    # Prefer AI when configured, otherwise use heuristic.
    if _ai_configured():
        pick, meta = await _openai_choose_instrument_from_list(
            style=style,
            role=role,
            candidates=candidates,
            prompt=req.prompt,
            temperature=req.temperature,
        )
        if meta.get("ok") and isinstance(pick, dict):
            return {"ok": True, "style": style, "role": role, "choice": pick["choice"], "reason": pick.get("reason"), "candidates": candidates, "source": "ai"}

    choice, reason = _heuristic_choose_instrument(style, role, candidates)
    if not choice:
        return {"ok": False, "error": "no_choice"}
    return {"ok": True, "style": style, "role": role, "choice": choice, "reason": reason, "candidates": candidates, "source": "heuristic"}


@app.post("/instruments/recommend_apply")
async def recommend_and_apply_instrument(req: RecommendAndApplyInstrumentRequest):
    # If the UI didn't provide candidates, prefer the Ableton browser index path.
    # This loads by browser URI (more reliable than name matching across presets/packs).
    if not _normalize_candidates(req.candidates):
        return await recommend_apply_from_index(RecommendFromBrowserIndexRequest(
            style=req.style,
            role=req.role,
            track_index=req.track_index,
            q=None,
            limit=80,
            prompt=req.prompt,
            temperature=req.temperature,
        ))

    rec = await recommend_instrument(RecommendInstrumentRequest(
        style=req.style,
        role=req.role,
        candidates=req.candidates,
        prompt=req.prompt,
        temperature=req.temperature,
    ))
    if not isinstance(rec, dict) or not rec.get("ok"):
        return rec

    choice = str(rec.get("choice") or "").strip()
    if not choice:
        return {"ok": False, "error": "recommendation_missing_choice"}

    ctrl.load_device(int(req.track_index), choice)
    return rec | {"applied": True, "track_index": int(req.track_index)}


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


@app.get("/device/params")
def get_device_params(
    track_index: int = Query(..., ge=0),
    device_index: int = Query(..., ge=0),
    timeout_s: float = Query(1.2, gt=0.0, le=10.0),
):
    unavailable = _live_query_unavailable()
    if unavailable:
        return {"ok": False, "error": "osc_listener_error", "detail": unavailable}

    if BRIDGE.connected:
        try:
            dev = BRIDGE.request(
                "get_device_params",
                {"track_index": int(track_index), "device_index": int(device_index)},
                timeout_s=max(1.0, float(timeout_s)),
            )
        except BridgeError as e:
            return {"ok": False, "error": str(e), "source": "bridge"}
        params = dev.get("parameters") or []
        return {
            "ok": True,
            "received_at": time.time(),
            "track_index": int(track_index),
            "device_index": int(device_index),
            "device_name": dev.get("name"),
            "names": [p["name"] for p in params],
            "parameters": params,
            "count": len(params),
            "source": "bridge",
        }

    since = time.time()
    ctrl.send("/live/device/get/parameters/name", [int(track_index), int(device_index)])
    got = _await_osc("/live/device/get/parameters/name", since=since, timeout_s=float(timeout_s))
    if got is None:
        return {"ok": False, "error": "timeout", "address": "/live/device/get/parameters/name"}

    ts, args = got
    # AbletonOSC replies with (track_index, device_index, <name0>, <name1>, ...)
    out_track = int(args[0]) if len(args) > 0 else int(track_index)
    out_dev = int(args[1]) if len(args) > 1 else int(device_index)
    names = [str(x) for x in args[2:]] if len(args) > 2 else []
    return {
        "ok": True,
        "received_at": ts,
        "track_index": out_track,
        "device_index": out_dev,
        "names": names,
        "count": len(names),
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
    value = value * MACRO_VALUE_MAX

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
    value = value * MACRO_VALUE_MAX

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


# ---------------------------------------------------------------- AI provider settings
# Every text-generation call goes through _call_ai_async, which uses whichever provider the
# user picked on the System page. Keys live in .env (the page can write them there); the
# chosen provider/model live in ai_settings.json. Voices (TTS) stay on OpenAI.

AI_SETTINGS_FILE = os.path.join(APP_DIR, "ai_settings.json")
ENV_FILE = os.path.join(APP_DIR, ".env")

AI_PROVIDERS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "key_required": True,
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": [
            {"id": "gpt-4o-mini", "label": "GPT-4o mini (fast, cheap)"},
            {"id": "gpt-4o", "label": "GPT-4o"},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini"},
            {"id": "gpt-4.1", "label": "GPT-4.1"},
            {"id": "gpt-5-mini", "label": "GPT-5 mini"},
            {"id": "gpt-5", "label": "GPT-5"},
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "key_env": "ANTHROPIC_API_KEY",
        "key_required": True,
        "default_model": "claude-opus-5",
        "models": [
            {"id": "claude-opus-5", "label": "Claude Opus 5"},
            {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5 (faster, cheaper)"},
            {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (fastest)"},
            {"id": "claude-fable-5-1", "label": "Claude Fable 5.1 (most capable, slowest)"},
        ],
    },
    "local": {
        "label": "Local (Ollama, LM Studio, ...)",
        "key_env": "LOCAL_AI_API_KEY",
        "key_required": False,
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3.1",
        "models": [],  # whatever the local server has pulled; see /ai/models
    },
}


def _ai_load_settings() -> dict:
    s: dict = {}
    try:
        with open(AI_SETTINGS_FILE, "r", encoding="utf-8") as f:
            s = json.load(f) or {}
    except (OSError, ValueError):
        s = {}
    provider = str(s.get("provider") or os.environ.get("PULSE_AI_PROVIDER") or "openai").lower()
    if provider not in AI_PROVIDERS:
        provider = "openai"
    models = s.get("models") if isinstance(s.get("models"), dict) else {}
    if not s.get("provider") and os.environ.get("PULSE_AI_MODEL"):
        models = {**models, provider: os.environ["PULSE_AI_MODEL"]}
    base = s.get("local_base_url") or os.environ.get("LOCAL_AI_BASE_URL") or AI_PROVIDERS["local"]["base_url"]
    return {"provider": provider, "models": models, "local_base_url": str(base).rstrip("/")}


def _ai_save_settings(s: dict) -> None:
    tmp = AI_SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, AI_SETTINGS_FILE)


def _ai_config(provider: str | None = None) -> dict:
    """Resolved settings for one provider (the selected one by default)."""
    s = _ai_load_settings()
    p = provider if provider in AI_PROVIDERS else s["provider"]
    info = AI_PROVIDERS[p]
    return {
        "provider": p,
        "model": str(s["models"].get(p) or info["default_model"]),
        "base_url": s["local_base_url"] if p == "local" else info.get("base_url"),
        "api_key": os.environ.get(info["key_env"]) or "",
        "key_env": info["key_env"],
        "key_required": info["key_required"],
    }


def _ai_configured() -> bool:
    cfg = _ai_config()
    return bool(cfg["api_key"]) or not cfg["key_required"]


def _ai_http_error(provider: str, status: int, body: str) -> dict:
    return {"ok": False, "error": "ai_http_error", "provider": provider, "status": status, "body": body}


def _anthropic_takes_effort(model: str) -> bool:
    # Haiku 4.5, Sonnet 4.5 and the Claude 3 family reject output_config.effort.
    m = model.lower()
    return not ("haiku" in m or "sonnet-4-5" in m or m.startswith("claude-3"))


async def _anthropic_complete(cfg: dict, system: str, user: str) -> tuple[str | None, dict]:
    import anthropic

    kwargs: dict = {
        "model": cfg["model"],
        "max_tokens": 16000,
        "system": system + "\n\nRespond with a single JSON object and nothing else.",
        "messages": [{"role": "user", "content": user}],
    }
    # Patterns are short, well-specified jobs: low effort keeps them quick. Newer Claude models
    # take no sampling temperature, so effort is the only dial here.
    if _anthropic_takes_effort(cfg["model"]):
        kwargs["output_config"] = {"effort": "low"}
    client = anthropic.AsyncAnthropic(api_key=cfg["api_key"], timeout=180.0)
    try:
        try:
            resp = await client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if "output_config" not in kwargs or "effort" not in str(e.message).lower():
                raise
            kwargs.pop("output_config")  # a model we don't know about yet
            resp = await client.messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        return None, _ai_http_error("anthropic", e.status_code, str(e.message))
    except anthropic.APIConnectionError as e:
        return None, {"ok": False, "error": "ai_request_failed", "provider": "anthropic", "detail": str(e)}
    finally:
        await client.close()
    if resp.stop_reason == "refusal":
        return None, {"ok": False, "error": "ai_refused", "provider": "anthropic"}
    return "".join(b.text for b in resp.content if b.type == "text"), {"ok": True}


async def _openai_compat_complete(cfg: dict, system: str, user: str, temperature: float) -> tuple[str | None, dict]:
    """OpenAI's Chat Completions, which Ollama, LM Studio, vLLM etc. also speak."""
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    payload: dict = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
        # JSON mode: the API guarantees one syntactically valid JSON object.
        "response_format": {"type": "json_object"},
    }
    url = f"{cfg['base_url']}/chat/completions"
    timeout = 60.0 if cfg["provider"] == "openai" else 300.0  # local models can be slow
    for _ in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"], {"ok": True}
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text
            except Exception:
                body = str(e)
            # Some models only allow the default temperature, and some local servers have no
            # JSON mode: drop whichever one the server complained about and try again.
            low = body.lower()
            if e.response.status_code == 400 and "temperature" in low and "temperature" in payload:
                payload.pop("temperature")
                continue
            if e.response.status_code in (400, 422) and "response_format" in low and "response_format" in payload:
                payload.pop("response_format")
                continue
            return None, _ai_http_error(cfg["provider"], e.response.status_code, body)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return "", {"ok": True, "detail": f"unexpected_response_shape: {e}"}
        except Exception as e:
            return None, {"ok": False, "error": "ai_request_failed", "provider": cfg["provider"], "detail": str(e)}
    return None, {"ok": False, "error": "ai_request_failed", "provider": cfg["provider"], "detail": "retries_exhausted"}


async def _ai_complete(cfg: dict, system: str, user: str, temperature: float) -> tuple[str | None, dict]:
    if cfg["provider"] == "anthropic":
        return await _anthropic_complete(cfg, system, user)
    return await _openai_compat_complete(cfg, system, user, temperature)


async def _ai_list_models(cfg: dict) -> tuple[list[str], dict]:
    if cfg["key_required"] and not cfg["api_key"]:
        return [], {"ok": False, "error": "missing_ai_api_key", "provider": cfg["provider"], "key_env": cfg["key_env"]}
    if cfg["provider"] == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=cfg["api_key"], timeout=20.0)
        try:
            return [m.id async for m in client.models.list()], {"ok": True}
        except anthropic.APIStatusError as e:
            return [], _ai_http_error("anthropic", e.status_code, str(e.message))
        except anthropic.APIConnectionError as e:
            return [], {"ok": False, "error": "ai_request_failed", "provider": "anthropic", "detail": str(e)}
        finally:
            await client.close()

    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{cfg['base_url']}/models", headers=headers)
            resp.raise_for_status()
            ids = [str(m.get("id")) for m in (resp.json().get("data") or []) if m.get("id")]
    except httpx.HTTPStatusError as e:
        return [], _ai_http_error(cfg["provider"], e.response.status_code, e.response.text)
    except Exception as e:
        return [], {"ok": False, "error": "ai_request_failed", "provider": cfg["provider"], "detail": str(e)}
    if cfg["provider"] == "openai":
        # The list includes embeddings, audio, image models etc.; keep the chat ones.
        skip = ("embed", "tts", "whisper", "audio", "realtime", "transcribe", "image", "dall-e",
                "moderation", "search", "davinci", "babbage", "codex", "computer-use")
        ids = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")) and not any(s in i for s in skip)]
    return sorted(ids), {"ok": True}


def _ai_public_settings() -> dict:
    s = _ai_load_settings()
    providers = []
    for pid, info in AI_PROVIDERS.items():
        key = os.environ.get(info["key_env"]) or ""
        providers.append({
            "id": pid,
            "label": info["label"],
            "models": info["models"],
            "default_model": info["default_model"],
            "model": s["models"].get(pid) or info["default_model"],
            "key_env": info["key_env"],
            "key_required": info["key_required"],
            "key_set": bool(key),
            "key_hint": f"...{key[-4:]}" if len(key) >= 8 else "",
        })
    return {"ok": True, "provider": s["provider"], "local_base_url": s["local_base_url"], "providers": providers}


class AISettingsRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    local_base_url: str | None = None
    # Written to .env under the provider's key variable. Never sent back to the page.
    api_key: str | None = None


@app.get("/ai/settings")
def get_ai_settings():
    return _ai_public_settings()


@app.post("/ai/settings")
def set_ai_settings(req: AISettingsRequest):
    s = _ai_load_settings()
    provider = (req.provider or s["provider"]).strip().lower()
    if provider not in AI_PROVIDERS:
        return {"ok": False, "error": "unknown_provider", "available": list(AI_PROVIDERS)}
    s["provider"] = provider
    if req.model is not None:
        model = req.model.strip()
        if model:
            s["models"][provider] = model
        else:
            s["models"].pop(provider, None)
    if req.local_base_url is not None:
        base = req.local_base_url.strip().rstrip("/")
        if base and not re.match(r"^https?://", base):
            return {"ok": False, "error": "bad_base_url", "hint": "Start it with http:// or https://"}
        s["local_base_url"] = base or AI_PROVIDERS["local"]["base_url"]
    if req.api_key is not None and req.api_key.strip():
        from dotenv import set_key

        key_env = AI_PROVIDERS[provider]["key_env"]
        if not os.path.exists(ENV_FILE):
            open(ENV_FILE, "a", encoding="utf-8").close()
        set_key(ENV_FILE, key_env, req.api_key.strip(), quote_mode="never")
        os.environ[key_env] = req.api_key.strip()
    _ai_save_settings(s)
    return _ai_public_settings()


@app.get("/ai/models")
async def list_ai_models(provider: str | None = Query(None)):
    cfg = _ai_config(provider)
    ids, meta = await _ai_list_models(cfg)
    return meta | {"provider": cfg["provider"], "models": ids}


@app.post("/ai/test")
async def test_ai():
    cfg = _ai_config()
    t0 = time.time()
    obj, meta = await _call_ai_async(
        'Reply with JSON: {"ok": true, "say": "<a five-word techno slogan>"}', "Go.", 0.7
    )
    return meta | {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "seconds": round(time.time() - t0, 2),
        "reply": obj,
    }


async def _call_ai_async(system: str, user: str, temperature: float):
    cfg = _ai_config()
    if cfg["key_required"] and not cfg["api_key"]:
        return None, {
            "ok": False,
            "error": "missing_ai_api_key",
            "provider": cfg["provider"],
            "hint": f"Add {cfg['key_env']} on the System page (AI Provider) or in .env.",
        }

    def _extract_json_text(s: str) -> str:
        txt = str(s or "").strip()
        if not txt:
            return ""
        if txt.startswith("```"):
            parts = txt.split("```")
            if len(parts) >= 3:
                txt = "```".join(parts[1:-1])
            txt = txt.strip()
            if txt.lower().startswith("json"):
                txt = txt[4:].strip()
        start = txt.find("{")
        end = txt.rfind("}")
        if start != -1 and end != -1 and end > start:
            return txt[start : end + 1]
        return txt

    def _try_parse_json(content: str):
        candidate = _extract_json_text(content)
        if not candidate:
            raise ValueError("empty_content")
        try:
            return json.loads(candidate), candidate
        except json.JSONDecodeError:
            # Models sometimes split one object into several: {"drums":...},{"bass":...}.
            # Parse them as a list and merge back into one object.
            try:
                parts = json.loads("[" + candidate + "]")
            except json.JSONDecodeError:
                raise
            if not parts or not all(isinstance(x, dict) for x in parts):
                raise
            merged: dict = {}
            for x in parts:
                merged.update(x)
            return merged, candidate

    attempts = 0
    last_content = ""
    last_err: str | None = None

    while attempts < 2:
        attempts += 1
        sys_msg = system
        temp = float(temperature)
        if attempts == 2:
            sys_msg = (
                str(system).rstrip()
                + "\n\nIMPORTANT: Return STRICT JSON ONLY. No trailing text, no markdown, no explanations."
            )
            temp = min(0.4, float(temperature))

        content, meta = await _ai_complete(cfg, sys_msg, user, temp)
        if content is None:
            return None, meta

        try:
            last_content = content
            obj, _ = _try_parse_json(last_content)
            return obj, {"ok": True}
        except Exception as e:
            last_err = str(e)
            continue

    snippet = str(_extract_json_text(last_content))
    if len(snippet) > 800:
        snippet = snippet[:800] + "..."

    return None, {
        "ok": False,
        "error": "ai_bad_response",
        "provider": cfg["provider"],
        "detail": last_err or "json_parse_failed",
        "content_snippet": snippet,
    }


async def _openai_generate_fx_pattern(style: str, bars: int, prompt: str | None, temperature: float, context: str | None = None):
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
        f"{_groove_context_block(context)}"
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_ai_async(system, user, temperature)


async def _openai_generate_stabs_pattern(style: str, bars: int, prompt: str | None, temperature: float, context: str | None = None):
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
        f"{_groove_context_block(context)}"
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_ai_async(system, user, temperature)


async def _openai_generate_perc_pattern(style: str, bars: int, prompt: str | None, temperature: float, context: str | None = None):
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
            "perc1": [0] * total_steps,
            "perc2": [0] * total_steps,
        },
    }

    system = (
        "You generate percussion patterns as strict JSON only (no markdown, no prose). "
        "Return a single JSON object with keys: bars, step_division, lanes. "
        "step_division must be '1/16'. bars must be an integer 1..4. "
        "lanes is an object mapping lane names to arrays of length bars*16. "
        "Each array element is an integer velocity 0..127 (0 means no hit). "
        "Valid lanes: perc1, perc2. "
        "Keep it supportive and beginner-friendly: syncopation is ok, but avoid constant hits."
    )

    user = (
        f"Generate a {bars}-bar percussion pattern in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        "Use syncopation appropriate for the style; leave space for kick and snare/clap. "
        "Use perc1 for higher percussion and perc2 for lower percussion. "
        "Return ONLY JSON.\n\n"
        f"Template shape: {json.dumps(schema)}\n\n"
        f"{_groove_context_block(context)}"
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_ai_async(system, user, temperature)


async def _openai_generate_pair(style: str, bars: int, drum_lanes: list[str], bass_root_midi: int, prompt: str | None, temperature: float):
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
        "Keep bass monophonic (one note per step). "
        "Match the density and aggression implied by the style and cues: hard dance may use pounding kicks and very fast hats; "
        "minimal techno may stay sparse. Do not default to generic house if the style is hardcore or gabber. "
        "The 'applied' object must contain teaching commentary fields: summary (string), why_drums (array of strings), why_bass (array of strings), listening_tips (array of strings). "
        "The 'applied' object must also include suggested_settings with nested drums and bass objects containing normalized 0..1 values for: filter_cutoff, filter_resonance, drive, eq_low_cut, eq_presence, space."
    )

    cues = _style_cues_for_generation(style)
    cues_block = f"\n\nProduction cues from the style definition (obey these):\n{cues}\n" if cues else ""

    user = (
        f"Generate a {bars}-bar drum pattern and a complementary bassline in the style '{style}'. "
        f"For bass use root MIDI {bass_root_midi} as tonal center. "
        f"Bass style profile: scale={bass_profile['scale']}, octave_range={bass_profile['octave_range']}, density={bass_profile['density']}, rhythm={bass_profile['rhythm']}, note_pool={bass_profile['note_pool']}. "
        f"Bass profile notes: {bass_profile.get('explain', '')}\n"
        f"{cues_block}"
        "Return ONLY JSON.\n\n"
        f"Template: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_ai_async(system, user, temperature)


def _query_with_timeout(address: str, args: list, timeout_s: float = 0.6):
    via_bridge = ctrl.query(address, args, timeout_s=timeout_s)
    if via_bridge is not None:
        return via_bridge
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


def _parse_osc_num_devices_args(args) -> int | None:
    """
    ideoforms AbletonOSC: /live/track/get/num_devices reply is (track_id, num_devices),
    not a single int. Using args[0] alone breaks track 0 (always read as 0 devices).
    """
    if not isinstance(args, tuple) or len(args) < 1:
        return None
    try:
        if len(args) >= 2:
            return int(args[1])
        return int(args[0])
    except Exception:
        return None


def _get_track_num_devices_osc(track_index: int, *, timeout_s: float = 1.0) -> int | None:
    """Query /live/track/get/num_devices; None if the query failed."""
    if _live_query_unavailable():
        return None
    res = _query_with_timeout("/live/track/get/num_devices", [int(track_index)], timeout_s=float(timeout_s))
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    return _parse_osc_num_devices_args(res.get("args"))


def _instrument_index_hints_from_applied(applied: list | None) -> list[str]:
    """If MCP failed with 'not found' on browser URIs, point users at rebuilding the on-disk index."""
    if not applied:
        return []
    for entry in applied:
        if not isinstance(entry, dict):
            continue
        d = entry.get("detail")
        s = ""
        if isinstance(d, dict):
            s = str(d.get("after_error") or d.get("error") or "")
        sl = s.lower()
        if "not found" in sl and ("uri" in sl or "browser" in sl or "query:" in s):
            return [
                "MCP: browser URI not found (stale or different Live/library). Rebuild: AbletonMCP on, Live open, then POST /mcp/index/rebuild to refresh knowledge/ableton_browser_index.json on this machine."
            ]
    return []


def _osc_fill_instruments_on_empty_tracks(plan: list[dict], applied: list[dict]) -> None:
    """
    AbletonMCP can report success while a track still has an empty device chain. After the main
    MCP/OSC pass, any Pulse track with num_devices==0 gets a built-in class device via /live/track/load_device.
    """
    time.sleep(0.55)
    for item in plan:
        ti = int(item["track_index"])
        n = _get_track_num_devices_osc(ti, timeout_s=1.0)
        if n is None:
            n = _get_track_num_devices_osc(ti, timeout_s=1.5)
        if n is not None and n > 0:
            continue
        if n is None:
            # Still unknown (OSC listener/timeout) — do not add a second load_device blindly.
            continue
        try:
            dev = _default_osc_instrument_name_for_track(ti)
            ctrl.load_device(ti, dev)
            time.sleep(0.2)
            applied.append(
                {
                    "track_index": ti,
                    "role": item.get("role"),
                    "ok": True,
                    "detail": {
                        "ok": True,
                        "source": "osc_fill_zero_devices",
                        "device": dev,
                        "num_devices_before": 0,
                    },
                }
            )
        except Exception as e:
            applied.append(
                {
                    "track_index": ti,
                    "role": item.get("role"),
                    "ok": False,
                    "detail": {"source": "osc_fill_zero_devices", "error": str(e), "num_devices_before": 0},
                }
            )


@app.get("/bridge/status")
def bridge_status(refresh: bool = False):
    """PulseBridge connection plus what this Live install supports (edition, version, devices)."""
    out = {"ok": True, **BRIDGE.status(), "capabilities": BRIDGE.capabilities}
    if refresh and BRIDGE.connected:
        try:
            BRIDGE.capabilities = BRIDGE.request("get_capabilities", {"refresh": True}, timeout_s=10.0)
            out["capabilities"] = BRIDGE.capabilities
        except BridgeError as e:
            out["refresh_error"] = str(e)
    return out


@app.get("/live/snapshot")
def live_snapshot():
    """Song, scenes and per-track state in one round trip (bridge only)."""
    try:
        return {"ok": True, "result": BRIDGE.request("get_snapshot", timeout_s=3.0)}
    except BridgeError as e:
        return {"ok": False, "error": str(e), "bridge": BRIDGE.status()}


class _EventFanout:
    """
    Thread-safe hand-off from the bridge client thread to per-request asyncio queues.
    Subscribers pick a mode: "state" (no meters), "all", or "meters" (meters only).
    Live is only asked to stream meters while at least one subscriber wants them.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue, str]] = set()

    def add(self, sub):
        with self.lock:
            self.subscribers.add(sub)
        self._sync_meters()

    def discard(self, sub):
        with self.lock:
            self.subscribers.discard(sub)
        self._sync_meters()

    def _sync_meters(self):
        with self.lock:
            wanted = any(mode != "state" for _, _, mode in self.subscribers)
        if BRIDGE.connected:
            try:
                BRIDGE.send("set_meters", {"enabled": wanted})
            except BridgeError:
                pass

    def publish(self, event: str, data: dict):
        if event == "bridge.connected":
            self._sync_meters()  # new bridge connection starts with meters off
        item = (event, data)
        with self.lock:
            subs = list(self.subscribers)
        for loop, q, mode in subs:
            if (event == "meters") != (mode == "meters") and mode != "all":
                continue
            try:
                loop.call_soon_threadsafe(self._offer, q, item)
            except RuntimeError:
                pass  # loop closed

    @staticmethod
    def _offer(q: asyncio.Queue, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass  # slow browser tab: drop rather than buffer forever


EVENT_FANOUT = _EventFanout()
BRIDGE.on_event(EVENT_FANOUT.publish)


@app.get("/live/events")
async def live_events(meters: str = "0"):
    """
    Server-Sent Events stream of Live changes (tempo, transport, tracks, devices, beats).
    meters=1 adds ~10 Hz output meter frames; meters=only sends just those.
    """
    mode = {"1": "all", "only": "meters"}.get(meters, "state")
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    sub = (loop, q, mode)
    EVENT_FANOUT.add(sub)

    async def stream():
        try:
            hello = {"connected": BRIDGE.connected, "hello": BRIDGE.hello}
            if mode != "meters":
                yield f"event: bridge.status\ndata: {json.dumps(hello)}\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
        finally:
            EVENT_FANOUT.discard(sub)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/song/track_names")
def song_track_names(timeout_s: float = Query(0.8, gt=0.0, le=10.0)):
    if _live_query_unavailable():
        return {"ok": False, "error": "osc_listener_failed", "detail": OSC_LISTENER_ERROR}

    res = _query_with_timeout("/live/song/get/track_names", [], timeout_s=float(timeout_s))
    if not isinstance(res, dict) or not res.get("ok"):
        return {"ok": False, "error": "timeout", "address": "/live/song/get/track_names"}

    args = res.get("args")
    names = [str(x) for x in args] if isinstance(args, tuple) else []
    return {"ok": True, "names": names, "count": len(names)}


@app.get("/track/num_devices")
def track_num_devices(track_index: int = Query(..., ge=0), timeout_s: float = Query(0.8, gt=0.0, le=10.0)):
    if _live_query_unavailable():
        return {"ok": False, "error": "osc_listener_failed", "detail": OSC_LISTENER_ERROR}

    res = _query_with_timeout("/live/track/get/num_devices", [int(track_index)], timeout_s=float(timeout_s))
    if not isinstance(res, dict) or not res.get("ok"):
        return {"ok": False, "error": "timeout", "address": "/live/track/get/num_devices"}

    n = _parse_osc_num_devices_args(res.get("args"))
    return {"ok": True, "track_index": int(track_index), "num_devices": n}


def _ensure_scene_count(min_scenes: int):
    # Best-effort: if AbletonOSC exposes num_scenes, only create missing.
    existing = None
    errors: list[str] = []
    try:
        res = _query_with_timeout("/live/song/get/num_scenes", [], timeout_s=0.8)
        if isinstance(res, dict) and res.get("ok") and isinstance(res.get("args"), tuple) and len(res.get("args")) > 0:
            existing = int(res["args"][0])
    except Exception:
        existing = None

    if existing is None:
        # Unknown: create up to min_scenes (may append extra; acceptable for blank projects).
        to_create = int(min_scenes)
    else:
        to_create = max(0, int(min_scenes) - int(existing))

    # Safety cap
    if to_create > 128:
        to_create = 128

    for _ in range(to_create):
        try:
            ctrl.send("/live/song/create_scene", [-1])
            time.sleep(0.02)
        except Exception:
            errors.append("create_scene_failed")

    # If AbletonOSC supports querying num_scenes, re-check for visibility.
    after = None
    try:
        res2 = _query_with_timeout("/live/song/get/num_scenes", [], timeout_s=0.8)
        if isinstance(res2, dict) and res2.get("ok") and isinstance(res2.get("args"), tuple) and len(res2.get("args")) > 0:
            after = int(res2["args"][0])
    except Exception:
        after = None

    return {
        "ok": True,
        "requested": int(min_scenes),
        "existing": existing,
        "attempted_create": int(to_create),
        "after": after,
        "errors": errors,
    }


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

    out = {"bars": bars, "step_division": "1/16", "lanes": clean_lanes}
    # Optional feel/pitch metadata survives export -> re-apply.
    swing = pattern.get("swing")
    if isinstance(swing, (int, float)) and not isinstance(swing, bool):
        out["swing"] = _clamp_swing(swing)
    voicings = pattern.get("voicings")
    if isinstance(voicings, list) and voicings:
        clean_voicings = []
        for chord in voicings:
            if not isinstance(chord, list):
                continue
            notes = [int(n) for n in chord if isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= int(n) <= 127]
            if notes:
                clean_voicings.append(notes)
        if clean_voicings:
            out["voicings"] = clean_voicings
    return out, {"ok": True}


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


def _repeat_lane_pattern(pattern: dict, target_bars: int):
    if not isinstance(pattern, dict):
        return None, {"ok": False, "error": "pattern_not_object"}
    if pattern.get("step_division") != "1/16":
        return None, {"ok": False, "error": "bad_step_division"}
    lanes = pattern.get("lanes")
    if not isinstance(lanes, dict):
        return None, {"ok": False, "error": "lanes_missing"}

    src_bars = int(pattern.get("bars", 1) or 1)
    if src_bars < 1:
        src_bars = 1
    if src_bars > 16:
        src_bars = 16

    tb = int(target_bars)
    if tb < 1:
        tb = 1
    if tb > 16:
        tb = 16

    if tb == src_bars:
        return pattern, {"ok": True}

    src_steps = src_bars * 16
    tgt_steps = tb * 16
    out_lanes: dict[str, list[int]] = {}

    for lane, values in lanes.items():
        if not isinstance(values, list):
            continue
        src = list(values)
        if len(src) < src_steps:
            src = src + ([0] * (src_steps - len(src)))
        elif len(src) > src_steps:
            src = src[:src_steps]

        out: list[int] = []
        while len(out) < tgt_steps:
            out.extend(src)
        out_lanes[str(lane)] = out[:tgt_steps]

    return {"bars": tb, "step_division": "1/16", "lanes": out_lanes}, {"ok": True}


def _repeat_bassline(bass: dict, target_bars: int):
    if not isinstance(bass, dict):
        return None, {"ok": False, "error": "pattern_not_object"}
    if bass.get("step_division") != "1/16":
        return None, {"ok": False, "error": "bad_step_division"}

    src_bars = int(bass.get("bars", 1) or 1)
    if src_bars < 1:
        src_bars = 1
    if src_bars > 16:
        src_bars = 16

    tb = int(target_bars)
    if tb < 1:
        tb = 1
    if tb > 16:
        tb = 16

    if tb == src_bars:
        return bass, {"ok": True}

    src_steps = src_bars * 16
    tgt_steps = tb * 16

    steps = bass.get("steps")
    vels = bass.get("velocities")
    if not isinstance(steps, list) or not isinstance(vels, list):
        return None, {"ok": False, "error": "bad_bass_shape"}

    ssrc = list(steps)
    vsrc = list(vels)
    if len(ssrc) < src_steps:
        ssrc = ssrc + ([0] * (src_steps - len(ssrc)))
    elif len(ssrc) > src_steps:
        ssrc = ssrc[:src_steps]

    if len(vsrc) < src_steps:
        vsrc = vsrc + ([0] * (src_steps - len(vsrc)))
    elif len(vsrc) > src_steps:
        vsrc = vsrc[:src_steps]

    sout: list[int] = []
    vout: list[int] = []
    while len(sout) < tgt_steps:
        sout.extend(ssrc)
        vout.extend(vsrc)

    out = {
        **bass,
        "bars": tb,
        "steps": sout[:tgt_steps],
        "velocities": vout[:tgt_steps],
    }
    return out, {"ok": True}


def _clamp_swing(swing) -> float:
    try:
        s = float(swing or 0.0)
    except Exception:
        return 0.0
    return max(0.0, min(0.33, s))


def _style_groove(style: str) -> dict:
    """The style's groove block from knowledge/styles.json: swing, drum anchors, forbidden steps, accents."""
    s = (style or "").strip().lower()
    cfg = STYLE_CONFIG.get(s) if isinstance(STYLE_CONFIG, dict) else None
    g = cfg.get("groove") if isinstance(cfg, dict) else None
    return g if isinstance(g, dict) else {}


def _style_swing(style: str) -> float:
    """16th swing: fraction of a 16th that off-16ths are delayed (0.18 ~= 59% swing).

    Read from the style's groove.swing (or a top-level "swing") in knowledge/styles.json.
    """
    g = _style_groove(style)
    if g.get("swing") is not None:
        return _clamp_swing(g.get("swing"))
    s = (style or "").strip().lower()
    cfg = STYLE_CONFIG.get(s) if isinstance(STYLE_CONFIG, dict) else None
    return _clamp_swing(cfg.get("swing") if isinstance(cfg, dict) else 0.0)


def _anchor_on(spec: dict, step_index: int) -> bool:
    cycle = int(spec.get("cycle", 16) or 16)
    return (step_index % max(1, cycle)) in set(int(x) for x in spec.get("steps") or [])


def _groove_kick_steps(style: str, total_steps: int) -> list[int]:
    spec = (_style_groove(style).get("anchors") or {}).get("kick")
    if not isinstance(spec, dict):
        return []
    return [i for i in range(total_steps) if _anchor_on(spec, i)]


_ANCHOR_DEFAULT_VELOCITY = {"kick": 118, "snare": 105, "clap": 100, "oh": 85, "ch": 80}


def _apply_groove_to_drums(drums: dict | None, style: str) -> dict | None:
    """Enforce the style's defining drum skeleton on a generated pattern.

    anchors: {lane: {steps, mode: "exact"|"require", cycle, velocity?}} - "require" adds missing hits,
             "exact" also removes hits off the anchor steps (e.g. a strict four-on-the-floor kick);
             velocity pins every anchor hit (e.g. full-strength techno kicks).
    forbid:  {lane: [steps]} - never hit these steps (e.g. no kick under the dnb snare).
    accents: {lane: [steps]} - push these steps up and the rest down, so hats/perc have a pulse.
    kick_room: scale hats/perc hits that land on a kick (e.g. 0.75) so the kick transient cuts through.
    Steps are 1/16 indices within a bar (or within `cycle` steps for 2-bar anchors).
    """
    g = _style_groove(style)
    if not g or not isinstance(drums, dict) or not isinstance(drums.get("lanes"), dict):
        return drums
    total = int(drums.get("bars", 1) or 1) * 16
    lanes = {k: (list(v) + [0] * total)[:total] for k, v in drums["lanes"].items()}

    for lane, spec in (g.get("anchors") or {}).items():
        if lane not in LANE_TO_MIDI_NOTE or not isinstance(spec, dict):
            continue
        vals = lanes.get(lane, [0] * total)
        hits = [v for v in vals if v > 0]
        vel = int(sorted(hits)[len(hits) // 2]) if hits else _ANCHOR_DEFAULT_VELOCITY.get(lane, 100)
        fixed = spec.get("velocity")
        pinned = isinstance(fixed, (int, float)) and not isinstance(fixed, bool)
        if pinned:
            vel = max(1, min(127, int(fixed)))
        exact = spec.get("mode") == "exact"
        for i in range(total):
            if _anchor_on(spec, i):
                if vals[i] <= 0 or pinned:
                    vals[i] = vel
            elif exact:
                vals[i] = 0
        lanes[lane] = vals

    for lane, steps in (g.get("forbid") or {}).items():
        if lane in lanes:
            banned = set(int(x) for x in steps)
            lanes[lane] = [0 if (i % 16) in banned else v for i, v in enumerate(lanes[lane])]

    for lane, steps in (g.get("accents") or {}).items():
        if lane in lanes:
            strong = set(int(x) for x in steps)
            lanes[lane] = [
                (min(127, int(round(v * 1.15))) if (i % 16) in strong else max(1, int(round(v * 0.8)))) if v > 0 else 0
                for i, v in enumerate(lanes[lane])
            ]

    room = g.get("kick_room")
    kick = lanes.get("kick")
    if isinstance(room, (int, float)) and not isinstance(room, bool) and kick:
        for lane in ("ch", "oh", "perc1", "perc2"):
            if lane in lanes:
                lanes[lane] = [max(1, int(round(v * room))) if (v > 0 and kick[i] > 0) else v for i, v in enumerate(lanes[lane])]

    return {**drums, "lanes": lanes}


def _groove_cues(style: str) -> str:
    """Plain-language version of the groove skeleton for the model, so the rest of the pattern is built around it."""
    g = _style_groove(style)
    parts = []
    for lane, spec in (g.get("anchors") or {}).items():
        if not isinstance(spec, dict):
            continue
        cycle = int(spec.get("cycle", 16) or 16)
        where = f"steps {list(spec.get('steps') or [])}" + (" of every bar" if cycle == 16 else f" of every {cycle // 16}-bar cycle")
        parts.append(f"{lane} {'exactly on' if spec.get('mode') == 'exact' else 'always on'} {where}")
    for lane, steps in (g.get("forbid") or {}).items():
        parts.append(f"never put {lane} on steps {list(steps)}")
    for lane, steps in (g.get("accents") or {}).items():
        parts.append(f"accent {lane} on steps {list(steps)}")
    if not parts:
        return ""
    return "Groove skeleton for this style (0-based 1/16 steps; build the rest of the pattern around it): " + "; ".join(parts) + "."


def _swung_start(step_index: int, swing: float) -> float:
    """Start time in beats for a 1/16 step; every track uses this so drums, perc, stabs and bass swing together."""
    start = float(step_index) * 0.25
    if swing > 0.0 and (step_index % 2 == 1):
        start += swing * 0.25
    return start


def _write_pattern_to_ableton(track_index: int, clip_slot_index: int, pattern: dict):
    bars = int(pattern["bars"])
    length_beats = float(bars * 4)
    ctrl.create_clip(track_index, clip_slot_index, length_beats)

    swing = _clamp_swing(pattern.get("swing"))
    voicings = pattern.get("voicings") if isinstance(pattern.get("voicings"), list) else None
    # On a Drum Rack, hit the pads by name (kick -> the kit's "Kick" pad) rather than fixed GM notes.
    pad_notes = _DRUM_MAPS.get(track_index)
    for lane, steps in pattern["lanes"].items():
        pitch = pad_notes.get(lane, LANE_TO_MIDI_NOTE[lane])
        for i, vel in enumerate(steps):
            if vel <= 0:
                continue
            start = _swung_start(i, swing)
            duration = 0.10
            if lane in {"kick", "snare", "clap"}:
                duration = 0.20
            if lane == "stab" and voicings:
                # Pitched stabs: play the chord for this bar so stabs sit in key with bass and chords.
                for note in voicings[(i // 16) % len(voicings)]:
                    ctrl.add_note(track_index, clip_slot_index, int(note), start, 0.20, vel)
                continue
            ctrl.add_note(track_index, clip_slot_index, pitch, start, duration, vel)


def _write_bassline_to_ableton(track_index: int, clip_slot_index: int, bass: dict):
    bars = int(bass["bars"])
    length_beats = float(bars * 4)
    ctrl.create_clip(track_index, clip_slot_index, length_beats)

    steps = bass["steps"]
    vels = bass["velocities"]
    swing = _clamp_swing(bass.get("swing"))

    for i, (note, vel) in enumerate(zip(steps, vels)):
        if note <= 0 or vel <= 0:
            continue
        start = _swung_start(i, swing)
        duration = 0.22
        ctrl.add_note(track_index, clip_slot_index, int(note), start, duration, int(vel))


def _snap_bass_to_chords(bass: dict, chord_prog: dict):
    """Best-effort: keep the bass rhythm, but snap note pitches to chord tones per bar."""
    if not isinstance(bass, dict) or not isinstance(chord_prog, dict):
        return bass, {"ok": False, "error": "bad_input"}

    if bass.get("step_division") != "1/16":
        return bass, {"ok": False, "error": "bad_step_division"}

    bars = int(bass.get("bars", 1) or 1)
    total_steps = bars * 16

    steps = bass.get("steps")
    vels = bass.get("velocities")
    if not isinstance(steps, list) or not isinstance(vels, list):
        return bass, {"ok": False, "error": "bad_shape"}
    if len(steps) != total_steps or len(vels) != total_steps:
        return bass, {"ok": False, "error": "bad_length"}

    chords = chord_prog.get("chords")
    if not isinstance(chords, list) or not chords:
        return bass, {"ok": False, "error": "no_chords"}

    chord_by_bar: dict[int, dict] = {}
    for ch in chords:
        if not isinstance(ch, dict):
            continue
        try:
            b = int(ch.get("bar", 0) or 0)
        except Exception:
            continue
        chord_by_bar[b] = ch

    def _candidate_notes_for_pcs(base_note: int, pcs: set[int]):
        # Search around the current note to find the closest note matching allowed pitch classes.
        candidates: list[int] = []
        base = int(base_note)
        if base < 0:
            base = 0
        if base > 127:
            base = 127
        for delta in range(0, 25):
            up = base + delta
            dn = base - delta
            if up <= 127 and (up % 12) in pcs:
                candidates.append(up)
            if dn >= 0 and (dn % 12) in pcs:
                candidates.append(dn)
            if candidates:
                break
        return candidates

    out_steps = list(steps)
    for i, n in enumerate(steps):
        if not isinstance(n, (int, float)):
            continue
        n = int(n)
        if n <= 0:
            continue

        bar = i // 16
        ch = chord_by_bar.get(bar)
        if not isinstance(ch, dict):
            # fallback: first chord
            ch = chord_by_bar.get(0)
        if not isinstance(ch, dict):
            continue

        notes = ch.get("notes")
        if not isinstance(notes, list) or not notes:
            continue

        pcs = set()
        for cn in notes:
            try:
                pcs.add(int(cn) % 12)
            except Exception:
                continue
        if not pcs:
            continue

        cands = _candidate_notes_for_pcs(n, pcs)
        if cands:
            out_steps[i] = int(cands[0])

    out = {**bass, "steps": out_steps}
    return out, {"ok": True}


def _apply_style_to_bassline(bass: dict, style: str, drums: dict | None = None):
    """Best-effort post-processing to make the bass feel more style-correct.

    Rules come from the style's bass_profile in knowledge/styles.json, so every style (including
    generated ones) is covered:
      allowed_steps      only keep notes on these 1/16 steps of each bar (house offbeats, tribal quarters)
      avoid_kick         drop notes that land on a kick hit (uses `drums`, else the style's kick anchor)
      fill_steps         fill empty steps with the previous note (psytrance rolling bass, tekno offbeats)
      density_keep       probability of keeping non-accented notes
      accent_steps       velocity push on these steps
      octave_jump_steps  octave pops on these steps
    """

    style = (style or "").strip().lower()
    bars = int(bass.get("bars", 1) or 1)
    total_steps = bars * 16
    steps = list(bass.get("steps", []))
    vels = list(bass.get("velocities", []))

    if len(steps) != total_steps or len(vels) != total_steps:
        return bass, {"ok": False, "error": "bad_length"}

    bp = BASS_STYLE_PROFILE.get(style) if isinstance(BASS_STYLE_PROFILE, dict) else None
    bp = bp if isinstance(bp, dict) else {}

    def _steps_list(key: str, default: list[int] | None):
        v = bp.get(key)
        return [int(x) % 16 for x in v] if isinstance(v, list) else default

    allowed = _steps_list("allowed_steps", None)
    fill_steps = _steps_list("fill_steps", [])
    accent_steps = _steps_list("accent_steps", [0, 8, 12])
    octave_jump_steps = _steps_list("octave_jump_steps", [])
    try:
        density_keep = float(bp.get("density_keep", 0.9))
    except Exception:
        density_keep = 0.9
    avoid_kick = bool(bp.get("avoid_kick", False))
    swing = _style_swing(style)

    def clamp_midi(n: int):
        return max(1, min(127, int(n)))

    def clear(i: int):
        steps[i] = 0
        vels[i] = 0

    original = (list(steps), list(vels))

    if allowed is not None:
        allowed_set = set(allowed)
        for i in range(total_steps):
            if steps[i] > 0 and (i % 16) not in allowed_set:
                clear(i)

    if avoid_kick:
        kick = ((drums or {}).get("lanes") or {}).get("kick") if isinstance(drums, dict) else None
        kick_steps = {i for i, v in enumerate(kick or []) if v > 0} if kick else set(_groove_kick_steps(style, total_steps))
        if kick_steps and any(steps[i] > 0 and i not in kick_steps for i in range(total_steps)):
            for i in kick_steps:
                if i < total_steps and steps[i] > 0:
                    clear(i)

    if fill_steps:
        root = int(bass.get("root_midi", 43) or 43)
        fill_set = set(fill_steps)
        for b in range(bars):
            base = b * 16
            first = next((steps[i] for i in range(base, base + 16) if steps[i] > 0), 0) or next(
                (original[0][i] for i in range(base, base + 16) if original[0][i] > 0), root
            )
            last = first
            for i in range(base, base + 16):
                if steps[i] > 0:
                    last = steps[i]
                elif (i % 16) in fill_set:
                    steps[i] = clamp_midi(last)
                    vels[i] = 88

    # Never shape a bassline into silence: fall back to what the model wrote.
    if not any(steps) and any(original[0]):
        steps, vels = list(original[0]), list(original[1])

    # Density shaping (probabilistic dropouts on non-accented steps)
    if density_keep < 1.0:
        accent_set = set(accent_steps)
        for i in range(total_steps):
            if steps[i] <= 0 or (i % 16) in accent_set:
                continue
            if random.random() > density_keep:
                clear(i)

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
        "allowed_steps": allowed,
        "fill_steps": fill_steps,
        "avoid_kick": avoid_kick,
        "accent_steps": accent_steps,
        "octave_jump_steps": octave_jump_steps,
        "density_keep": density_keep,
        "note": "These are post-processing defaults from the style's bass_profile, applied after AI generation to better match common feel for the style.",
    }

