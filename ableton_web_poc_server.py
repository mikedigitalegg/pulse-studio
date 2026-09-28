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
import socket
from collections import deque

from fastapi import FastAPI
from fastapi import Query
from fastapi import UploadFile
from fastapi import File
from fastapi import Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from dotenv import load_dotenv

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
KNOWLEDGE_FILE = os.path.join(APP_DIR, "knowledge", "styles.json")

_AUTOPLAY_LOCK = threading.Lock()
_AUTOPLAY_THREAD: threading.Thread | None = None
_AUTOPLAY_STOP: threading.Event | None = None
_AUTOPLAY_STATE: dict = {"running": False, "current_step": None, "last_scene_index": None}

PULSE_TRACK_PLAN: list[dict] = [
    {"name": "Drums", "track_index": 0, "role": "drums", "q": "909 kit"},
    {"name": "Bass", "track_index": 1, "role": "bass", "q": "bass"},
    {"name": "Perc", "track_index": PERC_TRACK_INDEX, "role": "drums", "q": "perc"},
    {"name": "Stabs", "track_index": STABS_TRACK_INDEX, "role": "stabs", "q": "stab"},
    {"name": "FX", "track_index": FX_TRACK_INDEX, "role": "pads", "q": "pad"},
    {"name": "Chords", "track_index": CHORDS_TRACK_INDEX, "role": "pads", "q": "pad"},
]


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
    clip_slot_index: int = 0
    bpm: float = 140.0
    bars: int = 1
    base_pitch: int = 60
    velocity: int = 110
    fire: bool = True


@app.post("/audio/upload")
async def upload_audio(file: UploadFile = File(...), label: str = Form("")):
    try:
        original = (file.filename or "audio.webm").strip() or "audio.webm"
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", original)
        safe_label = re.sub(r"[^a-zA-Z0-9._-]", "_", (label or "").strip())
        ts = int(time.time() * 1000)
        name_root, ext = os.path.splitext(safe_name)
        if not ext:
            ext = ".webm"
        if safe_label:
            out_name = f"rec_{ts}_{safe_label}{ext}"
        else:
            out_name = f"rec_{ts}_{name_root}{ext}"

        out_path = os.path.join(RECORDINGS_DIR, out_name)
        data = await file.read()

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
        fx = (req.fx or "clean").strip().lower()
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

        env, t_step = _mono_rms_envelope(samples, sample_rate=sr, channels=ch)
        onsets_s = _pick_onsets_from_env(env, t_step=t_step)

        # Convert onsets to beats and quantize to 1/16
        notes: list[tuple[int, float, float, int]] = []
        base_pitch = int(req.base_pitch)
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

        track = int(req.track_index)
        slot = int(req.clip_slot_index)

        # Ensure the destination track exists without shifting existing track indices.
        try:
            ensure_tracks(EnsureTracksRequest(min_tracks=track + 1, insert_at_start=False))
        except Exception:
            pass

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
            "clip_slot_index": slot,
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
            "kit": info.get("kit", "Core Kit")
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

BASS_RACK_TRACK_INDEX = 1
BASS_RACK_DEVICE_INDEX = 0
DRUMS_FX_RACK_TRACK_INDEX = 0
DRUMS_FX_RACK_DEVICE_INDEX = 0

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

MACRO_VALUE_MAX = 127.0

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


ctrl = AbletonController()


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


class LoadStyleRequest(BaseModel):
    style: str
    launch: bool = True
    load_kit: bool = True
    launch_once: bool = False
    once_bars: int = 1


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
    clip_bars: int = 8
    temperature: float = 0.7
    apply_instruments: bool = True
    instrument_candidate_limit: int = 120
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


class ApplyPairExportRequest(BaseModel):
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

        # AbletonOSC provides: /live/clip_slot/get/has_clip [track, slot]
        # Response payload typically contains a single int/bool.
        for slot in range(start, start + max_slots):
            res = _query_with_timeout("/live/clip_slot/get/has_clip", [track, int(slot)], timeout_s=0.8)
            if not isinstance(res, dict) or not res.get("ok"):
                # If we can't query reliably, fail soft by returning current slot.
                return {"ok": True, "track_index": track, "slot": int(slot), "note": "query_failed_fallback"}

            args = res.get("args")
            has_clip = None
            try:
                if isinstance(args, tuple) and len(args) > 0:
                    has_clip = bool(int(args[0]))
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

    obj, meta = await _call_openai_async(system, user, 0.4)
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
    velocity: int = 70


def _minor_scale_pitch_classes(root_pc: int):
    # Natural minor: 1 2 b3 4 5 b6 b7
    intervals = [0, 2, 3, 5, 7, 8, 10]
    return [((root_pc + i) % 12) for i in intervals]


def _minor_degree_to_pc(root_pc: int, degree_1_to_7: int):
    deg = int(degree_1_to_7)
    if deg < 1:
        deg = 1
    if deg > 7:
        deg = ((deg - 1) % 7) + 1
    scale = _minor_scale_pitch_classes(root_pc)
    return scale[deg - 1]


def _minor_triads_quality(degree_1_to_7: int):
    # Natural minor triads:
    # i (min), ii° (dim), III (maj), iv (min), v (min), VI (maj), VII (maj)
    deg = int(degree_1_to_7)
    if deg < 1:
        deg = 1
    if deg > 7:
        deg = ((deg - 1) % 7) + 1
    if deg in {1, 4, 5}:
        return "min"
    if deg == 2:
        return "dim"
    return "maj"


def _triad_intervals(quality: str):
    q = (quality or "").strip().lower()
    if q == "dim":
        return [0, 3, 6]
    if q == "min":
        return [0, 3, 7]
    return [0, 4, 7]


def _pick_progression_degrees(style: str, bars: int):
    style = (style or "").strip().lower()
    bars_i = int(bars)
    if bars_i < 1:
        bars_i = 1
    if bars_i > 8:
        bars_i = 8

    hp = HARMONY_STYLE_PROFILE.get(style) if isinstance(HARMONY_STYLE_PROFILE, dict) else None
    progs = None
    if isinstance(hp, dict):
        progs = hp.get("progressions")

    if not isinstance(progs, list) or not progs:
        # Safe default: i - VI - III - VII
        base = [1, 6, 3, 7]
    else:
        pick = random.choice(progs)
        base = pick.get("degrees") if isinstance(pick, dict) else None
        if not isinstance(base, list) or not base:
            base = [1, 6, 3, 7]

    out: list[int] = []
    while len(out) < bars_i:
        out.extend([int(x) for x in base])
    return out[:bars_i]


def _generate_chords(style: str, bars: int, root_midi: int, chord_octave: int):
    # root_midi defines key center; we use its pitch class.
    root_pc = int(root_midi) % 12
    degrees = _pick_progression_degrees(style, bars)

    chords: list[dict] = []
    for i, deg in enumerate(degrees):
        q = _minor_triads_quality(deg)
        chord_root_pc = _minor_degree_to_pc(root_pc, deg)
        base = (int(chord_octave) * 12) + chord_root_pc
        notes = [base + iv for iv in _triad_intervals(q)]
        chords.append({
            "bar": i,
            "degree": int(deg),
            "quality": q,
            "notes": notes,
        })
    return {
        "bars": int(bars),
        "root_midi": int(root_midi),
        "scale": "minor",
        "harmonic_rhythm": "one_chord_per_bar",
        "chords": chords,
    }, {"ok": True}


def _write_chords_to_ableton(track_index: int, clip_slot_index: int, chord_prog: dict, velocity: int):
    bars = int(chord_prog.get("bars", 1) or 1)
    length_beats = float(bars * 4)
    ctrl.create_clip(track_index, clip_slot_index, length_beats)

    vel = int(velocity)
    if vel < 1:
        vel = 1
    if vel > 127:
        vel = 127

    chords = chord_prog.get("chords")
    if not isinstance(chords, list):
        return {"ok": False, "error": "chords_missing"}

    for ch in chords:
        if not isinstance(ch, dict):
            continue
        bar = int(ch.get("bar", 0) or 0)
        start = float(bar * 4)
        notes = ch.get("notes")
        if not isinstance(notes, list):
            continue
        for n in notes:
            try:
                pitch = int(n)
            except Exception:
                continue
            if pitch < 0 or pitch > 127:
                continue
            ctrl.add_note(track_index, clip_slot_index, pitch, start, 4.0, vel)

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
        int(req.velocity),
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
    # Calculate duration based on current tempo
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
        
        time.sleep(interval)
        
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

        if bool(getattr(req, "launch_once", False)):
            try:
                bars = int(getattr(req, "once_bars", 1) or 1)
            except Exception:
                bars = 1
            _schedule_stop_clip_after_bars(
                track_index=VOLCA_DRUM_TRACK_INDEX,
                clip_slot_index=clip_slot,
                bars=bars,
                bpm_default=float(cfg.get("tempo") or 140.0),
            )

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
async def generate_ai_pair(req: GenerateAIPairRequest):
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

    # Launch tasks in parallel
    pair_task = asyncio.create_task(_openai_generate_pair(style, req.bars, drum_lanes, root, req.prompt, req.temperature))
    
    perc_task = None
    if bool(req.include_perc):
        perc_task = asyncio.create_task(_openai_generate_perc_pattern(style, req.bars, req.prompt, req.temperature))

    stabs_task = None
    if bool(req.include_stabs):
        stabs_task = asyncio.create_task(_openai_generate_stabs_pattern(style, req.bars, req.prompt, req.temperature))

    fx_task = None
    if bool(req.include_fx):
        fx_task = asyncio.create_task(_openai_generate_fx_pattern(style, req.bars, req.prompt, req.temperature))

    # Await Core Pair
    obj, meta = await pair_task
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

    if clip_bars != int(req.bars):
        drums_rep, _ = _repeat_lane_pattern(drums_valid, clip_bars)
        bass_rep, _ = _repeat_bassline(bass_styled, clip_bars)
        if isinstance(drums_rep, dict):
            drums_valid = drums_rep
        if isinstance(bass_rep, dict):
            bass_styled = bass_rep

    # Await and Validate Perc
    perc_valid = None
    if perc_task:
        perc_pattern, pmeta = await perc_task
        if not pmeta.get("ok"):
            return pmeta
        perc_valid, pvmeta = _validate_pattern(perc_pattern, req.bars)
        if not pvmeta.get("ok"):
            return {"ok": False, "error": "invalid_perc", "detail": pvmeta, "raw": perc_pattern}

        if clip_bars != int(req.bars) and isinstance(perc_valid, dict):
            perc_rep, _ = _repeat_lane_pattern(perc_valid, clip_bars)
            if isinstance(perc_rep, dict):
                perc_valid = perc_rep

    # Await and Validate Stabs
    stabs_valid = None
    if stabs_task:
        stabs_pattern, smeta = await stabs_task
        if not smeta.get("ok"):
            return smeta
        stabs_valid, svmeta = _validate_pattern(stabs_pattern, req.bars)
        if not svmeta.get("ok"):
            return {"ok": False, "error": "invalid_stabs", "detail": svmeta, "raw": stabs_pattern}

        if clip_bars != int(req.bars) and isinstance(stabs_valid, dict):
            stabs_rep, _ = _repeat_lane_pattern(stabs_valid, clip_bars)
            if isinstance(stabs_rep, dict):
                stabs_valid = stabs_rep

    # Await and Validate FX
    fx_valid = None
    if fx_task:
        fx_pattern, fxmeta = await fx_task
        if not fxmeta.get("ok"):
            return fxmeta
        fx_valid, fxvmeta = _validate_pattern(fx_pattern, req.bars)
        if not fxvmeta.get("ok"):
            return {"ok": False, "error": "invalid_fx", "detail": fxvmeta, "raw": fx_pattern}

        if clip_bars != int(req.bars) and isinstance(fx_valid, dict):
            fx_rep, _ = _repeat_lane_pattern(fx_valid, clip_bars)
            if isinstance(fx_rep, dict):
                fx_valid = fx_rep

    # Write to Ableton (Sequential/Blocking but fast enough)
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

    return {
        "ok": True,
        "drums": drums_valid,
        "bass": bass_styled,
        "perc": perc_valid,
        "stabs": stabs_valid,
        "fx": fx_valid,
        "style": style,
        "bars": int(req.bars),
        "clip_bars": clip_bars,
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
    if bool(req.apply_instruments):
        # New tracks/renames need a moment before load_device / browser loads reliably.
        time.sleep(0.45)
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
    chord_velocity = 70
    chord_prog, chord_meta = _generate_chords(style, clip_bars, chord_root, chord_octave)
    if not chord_meta.get("ok"):
        chord_prog = None

    tasks = []
    for sc in scenes:
        slot = int(sc["slot"])
        inc = sc["include"]

        scene_prompt = str(sc.get("prompt") or "").strip()
        combined_scene_prompt = "\n\n".join([p for p in [style_hint, sidebar_prompt, scene_prompt] if p])

        pair_req = GenerateAIPairRequest(
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
            include_drums=bool(inc.get("drums")),
            include_bass=bool(inc.get("bass")),
            include_perc=bool(inc.get("perc")),
            include_stabs=bool(inc.get("stabs")),
            include_fx=bool(inc.get("fx")),
            prompt=combined_scene_prompt,
            temperature=float(req.temperature),
        )
        tasks.append(generate_ai_pair(pair_req))

    results = await asyncio.gather(*tasks)

    out_scenes = []
    for i, resp in enumerate(results):
        sc = scenes[i]
        if not isinstance(resp, dict) or not resp.get("ok"):
            return {"ok": False, "error": "scene_generation_failed", "scene": sc, "detail": resp}
        
        slot = int(sc["slot"])
        inc = sc["include"]

        # Make bass chord-aware (best-effort): rewrite bass clip to align pitch classes to chords.
        if isinstance(chord_prog, dict) and bool(inc.get("bass")):
            try:
                bass_obj = resp.get("bass") if isinstance(resp, dict) else None
                bassline = bass_obj.get("bassline") if isinstance(bass_obj, dict) else None
                if isinstance(bassline, dict):
                    snapped, _ = _snap_bass_to_chords(bassline, chord_prog)
                    if isinstance(snapped, dict):
                        _write_bassline_to_ableton(1, slot, snapped)
                        GEN_CACHE.set_bass(1, slot, snapped)
            except Exception:
                pass

        # Write chords (best-effort; failures shouldn't kill the whole track).
        # Musical defaults:
        # - Intro/Outro: tonic drone (first chord only)
        # - Main/Drop: full progression
        # - Break: thinner voicing (2 notes)
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
                    thin = {**chord_prog, "chords": thin_chords, "harmonic_rhythm": chord_prog.get("harmonic_rhythm", "one_chord_per_bar")}
                    _write_chords_to_ableton(CHORDS_TRACK_INDEX, slot, thin, max(45, int(chord_velocity * 0.85)))
                else:
                    _write_chords_to_ableton(CHORDS_TRACK_INDEX, slot, chord_prog, chord_velocity)
            except Exception:
                pass

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
                "chords": {"track": CHORDS_TRACK_INDEX, "slot": slot, "included": True},
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
            "hints": _instrument_index_hints_from_applied(instrument_applied) if bool(req.apply_instruments) else [],
        },
        "chords": {"track_index": CHORDS_TRACK_INDEX, "root_midi": chord_root, "progression": chord_prog},
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

    def send_command(self, command_type: str, params: dict | None = None, timeout_s: float = 15.0) -> dict:
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
    When AbletonMCP + browser index are unavailable, load stock device class names
    via AbletonOSC /live/track/load_device.

    Env (optional): PULSE_OSC_DEFAULT_DRUM, PULSE_OSC_DEFAULT_MELODIC
    """
    t = int(track_index)
    if t in (0, 2):
        return str(os.environ.get("PULSE_OSC_DEFAULT_DRUM") or "Drum Rack")
    return str(os.environ.get("PULSE_OSC_DEFAULT_MELODIC") or "Wavetable")


async def _choose_and_apply_instruments_for_full_track(style: str, *, prompt: str | None, limit: int):
    # Map Pulse Studio tracks -> roles. Primary: MCP + browser index. Fallback: OSC class names.
    # Notes:
    # - Track 0 is drums
    # - Track 1 is bass
    # - Track 2 is perc (treated as drums-ish)
    # - Track 3 is stabs (treated as stabs)
    # - Track 4 is fx (treated as pads/noise)
    # - Track 5 is chords (treated as pads)
    plan = [{"track_index": int(x["track_index"]), "role": x["role"], "q": x.get("q")} for x in PULSE_TRACK_PLAN]

    applied: list[dict] = []
    for item in plan:
        resp: dict | None = None
        err: str | None = None
        try:
            resp = await recommend_apply_from_index(
                RecommendFromBrowserIndexRequest(
                    style=style,
                    role=item["role"],
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

    obj, meta = await _call_openai_async(system, user, float(temperature))
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
    if os.environ.get("OPENAI_API_KEY"):
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

    obj, meta = await _call_openai_async(system, user, temperature)
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
    if os.environ.get("OPENAI_API_KEY"):
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
    if OSC_LISTENER_ERROR:
        return {"ok": False, "error": "osc_listener_error", "detail": OSC_LISTENER_ERROR}

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


async def _call_openai_async(system: str, user: str, temperature: float):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, {"ok": False, "error": "missing_openai_api_key"}

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
        return json.loads(candidate), candidate

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempts = 0
    last_data = None
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

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user},
            ],
            "temperature": float(temp),
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                last_data = resp.json()
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text
            except Exception:
                body = str(e)
            return None, {"ok": False, "error": "openai_http_error", "status": e.response.status_code, "body": body}
        except Exception as e:
            return None, {"ok": False, "error": "openai_request_failed", "detail": str(e)}

        try:
            last_content = last_data["choices"][0]["message"]["content"]
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
        "error": "openai_bad_response",
        "detail": last_err or "json_parse_failed",
        "content_snippet": snippet,
    }


async def _openai_generate_pattern(style: str, bars: int, prompt: str | None, temperature: float):
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
        "Match the style: hard dance / gabber / hardcore often needs dense kicks and fast hats; minimal techno can stay sparse."
    )

    rec = STYLE_RECOMMENDATIONS.get(style.lower()) if isinstance(STYLE_RECOMMENDATIONS, dict) else None
    drum_cues = ""
    if isinstance(rec, dict) and rec.get("drums"):
        drum_cues = f"\n\nDrum production cues (follow closely):\n{rec['drums']}\n"

    user = (
        f"Generate a {bars}-bar drum pattern in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        f"{drum_cues}"
        "Return ONLY JSON.\n\n"
        f"If you need a template, follow this shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_openai_async(system, user, temperature)


async def _openai_generate_fx_pattern(style: str, bars: int, prompt: str | None, temperature: float):
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

    return await _call_openai_async(system, user, temperature)


async def _openai_generate_stabs_pattern(style: str, bars: int, prompt: str | None, temperature: float):
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

    return await _call_openai_async(system, user, temperature)


async def _openai_generate_perc_pattern(style: str, bars: int, prompt: str | None, temperature: float):
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
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_openai_async(system, user, temperature)


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

    return await _call_openai_async(system, user, temperature)


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
    """Query /live/track/get/num_devices; None if the OSC query failed."""
    if OSC_LISTENER_ERROR is not None:
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
                {"index": CHORDS_TRACK_INDEX, "role": "CHORDS (optional)", "expected": "MIDI track with chord instrument (pads/piano/stabs)"},
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
    for t in [0, 1, CHORDS_TRACK_INDEX]:
        status["checks"].append(_query_with_timeout("/live/track/get/name", [t]))
        status["checks"].append(_query_with_timeout("/live/track/get/num_devices", [t]))

    return status


@app.get("/song/track_names")
def song_track_names(timeout_s: float = Query(0.8, gt=0.0, le=10.0)):
    if OSC_LISTENER_ERROR is not None:
        return {"ok": False, "error": "osc_listener_failed", "detail": OSC_LISTENER_ERROR}

    res = _query_with_timeout("/live/song/get/track_names", [], timeout_s=float(timeout_s))
    if not isinstance(res, dict) or not res.get("ok"):
        return {"ok": False, "error": "timeout", "address": "/live/song/get/track_names"}

    args = res.get("args")
    names = [str(x) for x in args] if isinstance(args, tuple) else []
    return {"ok": True, "names": names, "count": len(names)}


@app.get("/track/num_devices")
def track_num_devices(track_index: int = Query(..., ge=0), timeout_s: float = Query(0.8, gt=0.0, le=10.0)):
    if OSC_LISTENER_ERROR is not None:
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


async def _openai_generate_bassline(style: str, bars: int, root_midi: int, prompt: str | None, temperature: float):
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
        "Keep it monophonic (at most one note per step). Match genre: hard dance often uses fewer, shorter notes; "
        "house/techno can be more groovy and repetitive."
    )

    cues = _style_cues_for_generation(style)

    user = (
        f"Generate a {bars}-bar bassline in the style '{style}'. "
        "Use 16 steps per bar (1/16). "
        f"Use root note MIDI {root_midi} as the tonal center. "
        f"Style settings: scale={profile['scale']}, octave_range={profile['octave_range']}, density={profile['density']}, rhythm={profile['rhythm']}, note_pool={profile['note_pool']}. "
        f"Style profile intent: {profile.get('explain', '')}\n"
        f"{(cues + chr(10)) if cues else ''}"
        "Return ONLY JSON.\n\n"
        f"Template shape: {json.dumps(schema)}\n\n"
        f"Extra prompt: {prompt or ''}"
    )

    return await _call_openai_async(system, user, temperature)


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
async def generate_ai_pattern(req: GenerateAIPatternRequest):
    pattern, meta = await _openai_generate_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_drums(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/perc/generate_ai")
async def generate_ai_perc(req: GenerateAIPercPatternRequest):
    pattern, meta = await _openai_generate_perc_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_perc(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/stabs/generate_ai")
async def generate_ai_stabs(req: GenerateAIStabsPatternRequest):
    pattern, meta = await _openai_generate_stabs_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_stabs(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/fx/generate_ai")
async def generate_ai_fx(req: GenerateAIFxPatternRequest):
    pattern, meta = await _openai_generate_fx_pattern(req.style, req.bars, req.prompt, req.temperature)
    if not meta.get("ok"):
        return meta

    validated, vmeta = _validate_pattern(pattern, req.bars)
    if not vmeta.get("ok"):
        return {"ok": False, "error": "invalid_pattern", "detail": vmeta, "raw": pattern}

    _write_pattern_to_ableton(int(req.track_index), int(req.clip_slot_index), validated)
    GEN_CACHE.set_fx(int(req.track_index), int(req.clip_slot_index), validated)
    return {"ok": True, "pattern": validated, "track_index": req.track_index, "clip_slot_index": req.clip_slot_index}


@app.post("/bass/generate_ai")
async def generate_ai_bassline(req: GenerateAIBasslineRequest):
    style = (req.style or "").strip().lower()
    root = int(req.root_midi)
    if style in BASS_STYLE_DEFAULTS:
        root = int(BASS_STYLE_DEFAULTS[style]["root"])

    applied = BASS_STYLE_PROFILE.get(style, BASS_STYLE_PROFILE["techno"])

    pattern, meta = await _openai_generate_bassline(style, req.bars, root, req.prompt, req.temperature)
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
