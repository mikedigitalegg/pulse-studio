"""
Audio analysis of drum one-shots and Drum Rack kits, so Pulse picks kicks and perc kits by how
they sound rather than only by their names (the idea behind XLN XO's sample space).

Reads WAV/AIFF one-shots straight from the Ableton library folders (no numpy) and describes them:

    pitch_hz   body pitch 60-160 ms after the hit (a kick's sub note)
    attack_hz  pitch in the first 30 ms (a high value means a pitch-dropping "zap" kick)
    decay_ms   time until the sound falls 30 dB below its peak
    click_hz   brightness of the first 15 ms (beater click / transient)
    drive      body brightness over body pitch: ~1 for a clean sine, 2+ for a distorted kick
    crest      body peak over RMS: ~1.4 for a sine, near 1 when clipped/squashed
    dirt       0 clean .. 1 distorted, from drive and crest
    bright_hz  brightness of the first 120 ms (hats, snares, claps)

Ableton Pack samples are stored in a compressed AIFF ("able") this reader can't decode; those
kicks keep name-based ranking, and kits still get their pad sample names.

Kits (.adg) are unzipped and their pads' samples analysed; a kit gets per-lane medians plus its
pad sample names, which carry words the kit name lacks ("Kick 909", "Conga Hi"...).
Results are cached in knowledge/sound_features.json, keyed by lowercase file name.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import struct
import time
from array import array

AUDIO_EXT = (".wav", ".aif", ".aiff")
MAX_SECONDS = 1.5
FEATURES_VERSION = 1


# ---------------------------------------------------------------- decoding

def _ieee_extended(b: bytes) -> float:
    exp = ((b[0] & 0x7F) << 8) | b[1]
    mant = int.from_bytes(b[2:10], "big")
    if exp == 0 and mant == 0:
        return 0.0
    return mant * 2.0 ** (exp - 16383 - 63)


def _pcm_to_mono(data: bytes, width: int, channels: int, big_endian: bool, is_float: bool, frames: int) -> list[float]:
    n = min(frames, len(data) // (width * channels))
    data = data[: n * width * channels]
    if is_float and width in (4, 8):
        a = array("f" if width == 4 else "d")
        a.frombytes(data)
        if big_endian:
            a.byteswap()
        vals, scale = a, 1.0
    elif width == 1:
        vals, scale = [(b - 128) if not big_endian else (b - 256 if b > 127 else b) for b in data], 128.0
    elif width in (2, 4):
        a = array("h" if width == 2 else "i")
        a.frombytes(data)
        if big_endian:
            a.byteswap()
        vals, scale = a, float(1 << (8 * width - 1))
    elif width == 3:
        order = "big" if big_endian else "little"
        vals = [int.from_bytes(data[i:i + 3], order, signed=True) for i in range(0, len(data), 3)]
        scale = float(1 << 23)
    else:
        return []
    if channels == 1:
        return [v / scale for v in vals]
    return [sum(vals[i:i + channels]) / (scale * channels) for i in range(0, len(vals), channels)]


def read_audio(path: str, max_seconds: float = MAX_SECONDS) -> tuple[list[float], int] | None:
    """(mono samples in -1..1, sample rate), or None for a format this reader doesn't handle."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12:
            return None
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            fmt = None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return None
                cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
                if cid == b"fmt ":
                    raw = f.read(size)
                    tag, chans, sr, _, _, bits = struct.unpack("<HHIIHH", raw[:16])
                    if tag == 0xFFFE and len(raw) >= 26:
                        tag = struct.unpack("<H", raw[24:26])[0]
                    fmt = (tag, chans, sr, bits)
                elif cid == b"data" and fmt:
                    tag, chans, sr, bits = fmt
                    if tag not in (1, 3):
                        return None
                    width = bits // 8
                    frames = int(sr * max_seconds)
                    data = f.read(min(size, frames * width * chans))
                    return _pcm_to_mono(data, width, chans, False, tag == 3, frames), sr
                else:
                    f.seek(size + (size & 1), 1)
        if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
            comm = None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return None
                cid, size = ch[:4], struct.unpack(">I", ch[4:])[0]
                if cid == b"COMM":
                    raw = f.read(size + (size & 1))
                    chans, _, bits = struct.unpack(">hIh", raw[:8])
                    sr = int(_ieee_extended(raw[8:18]))
                    comp = raw[18:22] if head[8:12] == b"AIFC" and len(raw) >= 22 else b"NONE"
                    comm = (chans, bits, sr, comp)
                elif cid == b"SSND" and comm:
                    chans, bits, sr, comp = comm
                    if comp not in (b"NONE", b"sowt", b"fl32", b"FL32", b"fl64", b"FL64"):
                        return None
                    offset = struct.unpack(">I", f.read(8)[:4])[0]
                    f.seek(offset, 1)
                    width = (bits + 7) // 8
                    frames = int(sr * max_seconds)
                    data = f.read(min(size - 8, frames * width * chans))
                    is_float = comp.lower() in (b"fl32", b"fl64")
                    width = 4 if comp.lower() == b"fl32" else 8 if comp.lower() == b"fl64" else width
                    return _pcm_to_mono(data, width, chans, comp != b"sowt", is_float, frames), sr
                else:
                    f.seek(size + (size & 1), 1)
    return None


# ---------------------------------------------------------------- features

def _rms(x: list[float]) -> float:
    return math.sqrt(sum(v * v for v in x) / len(x)) if x else 0.0


def _brightness(x: list[float], sr: int) -> float:
    """Spectral-centroid proxy: for a sine of frequency f, sr*rms(diff)/(2*pi*rms) == f."""
    if len(x) < 4:
        return 0.0
    r = _rms(x)
    if r <= 1e-6:
        return 0.0
    d = [x[i + 1] - x[i] for i in range(len(x) - 1)]
    return sr * _rms(d) / (2 * math.pi * r)


def _zc_pitch(x: list[float], sr: int) -> float:
    if len(x) < 8:
        return 0.0
    zc = sum(1 for i in range(1, len(x)) if (x[i - 1] < 0) != (x[i] < 0))
    return zc * sr / (2 * len(x))


def describe(samples: list[float], sr: int) -> dict | None:
    if not samples or sr <= 0:
        return None
    peak = max(abs(v) for v in samples)
    if peak < 1e-4:
        return None
    x = [v / peak for v in samples]
    onset = next(i for i, v in enumerate(x) if abs(v) >= 0.1)
    x = x[onset:]
    ms = sr // 1000

    def win(a_ms: int, b_ms: int) -> list[float]:
        return x[a_ms * ms: b_ms * ms]

    hop = 5 * ms
    env = [_rms(x[i:i + hop]) for i in range(0, max(1, len(x) - hop), hop)] or [0.0]
    top = max(range(len(env)), key=env.__getitem__)
    floor = env[top] * 10 ** (-30 / 20)
    end = next((i for i in range(top, len(env)) if env[i] < floor), len(env))
    body = win(60, 160)
    body_rms = _rms(body)
    pitch = _zc_pitch(body, sr) if body_rms > 0.02 else 0.0
    body_bright = _brightness(body, sr)
    feat = {
        "pitch_hz": round(pitch, 1),
        "attack_hz": round(_zc_pitch(win(3, 30), sr), 1),
        "decay_ms": end * 5,
        "click_hz": round(_brightness(win(0, 15), sr)),
        "drive": round(body_bright / pitch, 2) if pitch > 20 else None,
        "crest": round(max(abs(v) for v in body) / body_rms, 2) if body_rms > 0.02 else None,
        "bright_hz": round(_brightness(win(0, 120), sr)),
    }
    feat["dirt"] = _dirt(feat)
    return feat


def _dirt(feat: dict) -> float | None:
    """0 (clean) .. 1 (heavily distorted/saturated), from harmonic brightness and a squashed crest factor."""
    if feat.get("drive") is None:
        return None
    by_drive = (feat["drive"] - 1.1) / 0.8
    by_crest = (1.45 - feat["crest"]) / 0.35 if feat.get("crest") else 0.0
    return round(min(1.0, max(0.0, by_drive, by_crest)), 2)


def analyse_file(path: str) -> dict | None:
    try:
        audio = read_audio(path)
    except (OSError, struct.error, ValueError, ZeroDivisionError):
        return None
    return describe(*audio) if audio else None


# ---------------------------------------------------------------- kits

# Pad lanes for kit analysis; hand drums are counted separately since some styles want them.
_KIT_LANES = {
    "kick": r"kick|\bbd\b|bass ?drum",
    "snare": r"snare|\bsd\b|\bsnr\b",
    "clap": r"clap|\bcp\b",
    "hat": r"hat|\bhh\b|\bchh?\b|\bohh?\b",
    "perc": r"perc|shaker|rim|cowbell|tamb|clave|block|wood|bell|tom",
}
HAND_DRUM_RE = r"conga|bongo|djembe|tabla|darbuka|doumbek|frame ?drum|cajon|udu|talking|timbal|bata"


def kit_samples(adg_path: str) -> list[dict]:
    """[{"pad": pad name, "file": sample file path}] for each sampled pad of a Drum Rack preset."""
    try:
        with gzip.open(adg_path, "rb") as f:
            xml = f.read().decode("utf-8", "replace")
    except (OSError, EOFError):
        return []
    out = []
    # Each pad is a DrumBranchPreset; its name sits in the branch's <Name> and the sample in a FileRef <Path>.
    for branch in re.split(r"<DrumBranchPreset\b", xml)[1:]:
        name = re.search(r'<Name Value="([^"]*)"', branch)
        path = re.search(r'<Path Value="([^"]+\.(?:wav|aif|aiff))"', branch, re.I)
        if path:
            out.append({"pad": name.group(1) if name else "", "file": path.group(1)})
    return out


def _median(vals: list[float]) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def describe_kit(pads: list[dict], features: dict[str, dict]) -> dict:
    lanes: dict[str, list[dict]] = {k: [] for k in _KIT_LANES}
    present: set[str] = set()
    words, hand = [], 0
    for p in pads:
        fname = os.path.basename(p["file"])
        label = f"{p['pad']} {fname}".lower()
        words.append(re.sub(r"\.(wav|aiff?)$", "", fname, flags=re.I))
        if re.search(HAND_DRUM_RE, label):
            hand += 1
        lane = next((k for k, pat in _KIT_LANES.items() if re.search(pat, label)), None)
        if lane:
            present.add(lane)
        feat = features.get(fname.lower())
        if lane and feat:
            lanes[lane].append(feat)
    backbeat = lanes["snare"] + lanes["clap"]
    return {
        "kit": True,
        "pads": len(pads),
        "lanes": sorted(present),
        "hand_drums": hand,
        "snare_decay_ms": _median([f["decay_ms"] for f in backbeat]),
        "snare_dirt": _median([f.get("dirt") for f in backbeat]),
        "hat_decay_ms": _median([f["decay_ms"] for f in lanes["hat"]]),
        "hat_bright_hz": _median([f["bright_hz"] for f in lanes["hat"]]),
        "words": " ".join(dict.fromkeys(words)),
    }


# ---------------------------------------------------------------- library scan + cache

def library_roots() -> list[str]:
    """Ableton's Core Library, installed Packs and the User Library on this machine."""
    home = os.path.expanduser("~")
    cands = []
    for base in (os.environ.get("PROGRAMDATA", r"C:\ProgramData"),):
        abl = os.path.join(base, "Ableton")
        if os.path.isdir(abl):
            cands += [os.path.join(abl, d, "Resources", "Core Library") for d in os.listdir(abl)]
    for docs in ("Documents", "Dokument", os.path.join("OneDrive", "Documents"), os.path.join("OneDrive", "Dokument")):
        cands += [os.path.join(home, docs, "Ableton", "Factory Packs"), os.path.join(home, docs, "Ableton", "User Library")]
    cands += ["/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library",
              os.path.join(home, "Music", "Ableton", "Factory Packs"), os.path.join(home, "Music", "Ableton", "User Library")]
    return [c for c in dict.fromkeys(cands) if os.path.isdir(c)]


def scan_library(roots: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """(one-shot name -> path, kit name -> path) for drum one-shots and Drum Rack presets."""
    shots, kits = {}, {}
    for root in roots:
        for d, _, files in os.walk(root):
            low = d.lower()
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in AUDIO_EXT and ("drum" in low or "one shot" in low or "kick" in fn.lower()):
                    shots.setdefault(fn.lower(), os.path.join(d, fn))
                elif ext == ".adg" and "drum" in low:
                    kits.setdefault(fn.lower(), os.path.join(d, fn))
    return shots, kits


def build_features(roots: list[str] | None = None, *, kick_names: set[str] | None = None,
                   kit_names: set[str] | None = None, log=print) -> dict:
    """
    Analyse the kick one-shots and kits the browser offers (or every one found when no names are
    given), plus the samples those kits use. Returns the cache document.
    """
    t0 = time.time()
    roots = roots if roots is not None else library_roots()
    shots, kits = scan_library(roots)
    log(f"sound analysis: {len(shots)} one-shots, {len(kits)} kits under {len(roots)} library folders")
    feats: dict[str, dict] = {}

    def shot(name: str) -> dict | None:
        if name not in feats and name in shots:
            feats[name] = analyse_file(shots[name]) or {}
        return feats.get(name) or None

    for name in shots:
        if (kick_names is None and "kick" in name) or (kick_names and name in kick_names):
            shot(name)
    kit_docs = {}
    for name, path in kits.items():
        if kit_names is not None and name not in kit_names:
            continue
        pads = kit_samples(path)
        if not pads:
            continue
        for p in pads:
            # Packs store absolute paths from the pack author's machine; the file name is what matches.
            shot(os.path.basename(p["file"]).lower())
        kit_docs[name] = describe_kit(pads, feats)
    out = {k: v for k, v in feats.items() if v}
    out.update(kit_docs)
    log(f"sound analysis: {len(out)} sounds described in {time.time() - t0:.0f}s")
    return {"version": FEATURES_VERSION, "generated_at": int(time.time()), "roots": roots, "features": out}


def load_cache(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != FEATURES_VERSION:
        return {}
    return doc.get("features") or {}


def save_cache(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge", "sound_features.json")
    save_cache(target, build_features())
