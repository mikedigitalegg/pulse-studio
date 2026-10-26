"""
Instrument palette selection for Pulse Studio.

Picks one sound per Pulse role (drums, bass, perc, stabs, fx, chords) from the cached
browser index, as a set that fits together:

1. Each role draws from the matching browser category (real kits for drums, Sounds/Bass
   for bass, ...), not a keyword search over the whole library.
2. Candidates are ranked with a per-style timbre profile, with a little jitter so
   re-picks vary, and the top few per role go to the AI in a single request. Kicks and
   perc kits are also ranked by how they actually sound (SOUND_TARGETS vs. the audio
   analysis in sound_analysis.py), since their names rarely say.
3. Without an API key (or if the AI fails) the top-ranked candidate per role is used.

Also maps a Drum Rack's pad names to Pulse drum lanes, so patterns hit the right pads.
Pure logic: no I/O except the injected AI call.
"""
from __future__ import annotations

import json
import random
import re
from typing import Awaitable, Callable

# role -> browser folders it draws from (index "path" values, lowercase prefix match).
ROLE_POOLS: dict[str, dict] = {
    # One kick sound, not a kit: DS Kick, a synth kick preset or a kick one-shot (loads into Simpler).
    "drums": {"paths": ["instruments", "sounds/percussive", "drums/drum hits/kick"], "exact": True, "name_re": r"kick",
              "label": "kick: one kick instrument or sample"},
    "perc": {"paths": ["drums"], "exact": True, "ext": (".adg",), "label": "drum kit for everything but the kick: snare, clap, hats and percussion"},
    "bass": {"paths": ["sounds/bass"], "label": "bass"},
    "stabs": {"paths": ["sounds/synth rhythmic", "sounds/synth keys", "sounds/piano & keys"], "label": "stabs / rhythmic chords"},
    "fx": {"paths": ["sounds/effects", "sounds/ambient & evolving"], "label": "FX, risers and textures"},
    "chords": {"paths": ["sounds/pad", "sounds/ambient & evolving", "sounds/synth keys"], "label": "pads / sustained chords"},
    "pad": {"paths": ["sounds/pad", "sounds/ambient & evolving"], "label": "atmospheric pad held above the chords (differs from the chords sound)"},
}

# Words that suit (+) or clash with (-) a role regardless of style.
ROLE_WORDS: dict[str, tuple[list[str], list[str]]] = {
    "drums": (["kick"], ["tom", "reverse", "layer", "loop", "bpm"]),
    # Perc plays snare, clap and hats too, so full kits win; styles can still ask for hand percussion.
    "perc": (["core kit", "kit"], ["orchestral"]),
    "bass": (["sub", "bass", "mono"], ["upright", "acoustic", "fretless", "slap", "picked", "fingered", "guitar"]),
    "stabs": (["stab", "chord", "rhythm", "organ", "pluck", "keys"], ["lead", "arp", "solo"]),
    "fx": (["riser", "sweep", "noise", "impact", "swell", "whoosh", "texture", "atmos", "fx"], ["piano", "guitar"]),
    "chords": (["pad", "chord", "warm", "atmos", "strings"], ["lead", "bass", "pluck"]),
    "pad": (["pad", "atmos", "air", "evolv", "swell", "drone", "wash", "strings"], ["lead", "bass", "pluck", "stab", "keys", "piano", "arp"]),
}

# Style timbre profiles: + words per role (or "*" for all roles), and words to avoid.
STYLE_PROFILES: dict[str, dict] = {
    "techno": {
        "text": "Techno: dark, driving, hypnotic. Punchy 909-style drums, rumbling or mono bass, dubby stabs, dark evolving pads.",
        "+": {"drums": ["909", "techno", "industrial", "warehouse"], "bass": ["techno", "rumble", "dark", "reese", "dist", "mono"],
              "stabs": ["dub", "stab", "techno", "dark", "chord"], "chords": ["dark", "drone", "dub", "evolv"], "*": ["dark", "techno", "dub"]},
        "-": ["happy", "tropical", "pop", "country", "jazz", "orchestral", "acoustic", "ukulele", "kalimba"],
    },
    "acid_techno": {
        "text": "Acid techno: 303 squelch bass, raw 909/808 drums, sparse dark stabs.",
        "+": {"drums": ["909", "808", "acid", "techno"], "bass": ["303", "acid", "squelch", "reso", "mono"],
              "stabs": ["acid", "stab", "dark"], "*": ["acid", "raw"]},
        "-": ["happy", "tropical", "pop", "jazz", "orchestral", "acoustic"],
    },
    "house": {
        "text": "House: warm and groovy. 909/808 drums, round deep bass, organ/piano/Rhodes chord stabs, lush pads.",
        "+": {"drums": ["909", "808", "house", "chicago", "disco"], "bass": ["deep", "house", "organ", "round", "funk", "sub"],
              "stabs": ["organ", "piano", "rhodes", "house", "chord", "stab", "keys"], "chords": ["warm", "lush", "soft", "strings"],
              "*": ["house", "deep", "warm"]},
        "-": ["industrial", "dist", "hard", "metal", "orchestral"],
    },
    "dnb": {
        "text": "Drum & bass: tight breakbeat kits, reese/sub bass, rave stabs, atmospheric liquid pads.",
        "+": {"drums": ["break", "jungle", "dnb", "amen", "liquid"], "bass": ["reese", "sub", "wobble", "neuro", "growl", "dnb"],
              "stabs": ["rave", "hoover", "stab"], "chords": ["atmos", "liquid", "lush"], "*": ["dnb", "jungle"]},
        "-": ["country", "orchestral", "acoustic", "house"],
    },
    "breakbeat": {
        "text": "Breakbeat: funky broken kits, punchy sub or acid bass, rave stabs.",
        "+": {"drums": ["break", "funk", "808", "boom bap"], "bass": ["sub", "funk", "acid", "break"], "stabs": ["rave", "stab", "funk"]},
        "-": ["orchestral", "country"],
    },
    "garage": {
        "text": "UK garage: shuffling 2-step kits, bouncy sub/organ bass, organ and chord stabs, warm pads.",
        "+": {"drums": ["garage", "2 step", "ukg", "shuffle", "808"], "bass": ["garage", "sub", "organ", "bounce", "wobble"],
              "stabs": ["organ", "chord", "stab", "rhodes", "keys"], "chords": ["warm", "soft"]},
        "-": ["industrial", "metal", "orchestral"],
    },
    "tribal": {
        "text": "Tribal: organic percussion-heavy grooves, hand drums and world percussion, deep rumbling bass, sparse textures.",
        "+": {"drums": ["tribal", "techno", "909"], "perc": ["tribal", "conga", "bongo", "djembe", "tabla", "afro", "hand", "world"],
              "bass": ["deep", "sub", "rumble"], "chords": ["drone", "atmos", "dark"]},
        "-": ["pop", "tropical", "orchestral"],
    },
    "psytrance": {
        "text": "Psytrance: tight punchy kick, rolling mono sub bass, acidic zappy leads/stabs, big risers and lasers.",
        "+": {"drums": ["psy", "trance", "909", "techno"], "bass": ["psy", "rolling", "sub", "trance", "mono"],
              "stabs": ["acid", "psy", "zap", "stab"], "fx": ["riser", "zap", "laser", "sweep"]},
        "-": ["house", "jazz", "orchestral", "acoustic"],
    },
    "tekno": {
        "text": "Tekno: dark, industrial, raw. Distorted drums, heavy mono rumble bass; avoid bright, shiny or plucky sounds.",
        "+": {"*": ["dark", "industrial", "raw", "dist", "drive", "satur", "rumble", "mono", "hard", "noise"]},
        "-": ["bright", "shiny", "sparkle", "happy", "pop", "bell", "pluck", "tropical", "future"],
    },
    "happy_house": {
        "text": "Happy house: bright, uplifting and bouncy. Punchy 909 drums, round funky bass, piano/organ chord stabs, bright strings.",
        "+": {"drums": ["909", "house", "disco", "bright"], "bass": ["house", "funk", "round", "organ", "bounce"],
              "stabs": ["piano", "organ", "m1", "rhodes", "bright", "chord", "keys"], "chords": ["string", "bright", "lush", "warm"],
              "*": ["bright", "happy", "uplift", "house", "disco"]},
        "-": ["dark", "industrial", "dist", "drone", "horror", "metal", "noise", "evil"],
    },
    "detroit_house": {
        "text": "Detroit house: warm, soulful analog. Shuffling 909/808 drums, deep round analog bass, Rhodes/organ chords, string pads.",
        "+": {"drums": ["909", "808", "detroit", "house", "analog"], "bass": ["deep", "analog", "round", "sub", "soul"],
              "stabs": ["rhodes", "organ", "chord", "analog", "warm", "keys"], "chords": ["string", "warm", "analog", "lush", "soul"],
              "*": ["analog", "warm", "deep", "soul", "detroit"]},
        "-": ["industrial", "dist", "metal", "tropical", "pop", "harsh"],
    },
    "gabber": {
        "text": "Gabber: brutal distorted kick as the lead, hoover and rave stabs, harsh noisy energy; nothing soft or pretty.",
        "+": {"drums": ["gabber", "hardcore", "909", "dist", "hard", "kick"], "bass": ["dist", "hard", "hoover", "noise", "drive"],
              "stabs": ["hoover", "rave", "stab", "hard", "dist"], "fx": ["noise", "riser", "siren"],
              "*": ["hard", "dist", "hardcore", "rave", "drive", "noise", "gabber"]},
        "-": ["soft", "warm", "lush", "jazz", "acoustic", "pluck", "bell", "tropical", "chill", "ambient"],
    },
    "techno_balkan": {
        "text": "Balkan techno: driving dark techno with Eastern European colour: 909 drums, darbuka/hand percussion, eerie modal stabs and drones.",
        "+": {"drums": ["909", "techno"], "perc": ["darbuka", "tabla", "hand", "world", "ethnic", "tribal", "frame"],
              "bass": ["dark", "techno", "mono", "rumble"], "stabs": ["dark", "dub", "ethnic", "oud", "stab", "reed"],
              "chords": ["drone", "dark", "ethnic", "eerie"], "*": ["dark", "ethnic", "world", "techno"]},
        "-": ["happy", "pop", "tropical", "orchestral", "disco"],
    },
    "techno_balkan_2": {
        "text": "Balkan tribal techno: rolling hand percussion and folk colour over a steady kick, deep bass, modal drones.",
        "+": {"drums": ["tribal", "techno", "909"], "perc": ["darbuka", "tabla", "hand", "world", "ethnic", "tribal", "conga", "frame"],
              "bass": ["deep", "sub", "rumble"], "stabs": ["ethnic", "reed", "pluck", "oud", "dark"], "chords": ["drone", "ethnic", "atmos"],
              "*": ["ethnic", "world", "tribal", "organic"]},
        "-": ["pop", "tropical", "orchestral", "disco", "bright"],
    },
    "folk_step": {
        "text": "Folk step: organic folk textures over a 4/4 groove: acoustic and hand percussion, plucked and keyed melodic sounds, warm strings.",
        "+": {"drums": ["909", "808", "organic", "acoustic"], "perc": ["hand", "shaker", "world", "acoustic", "folk", "tambourine"],
              "bass": ["deep", "sub", "round", "warm"], "stabs": ["pluck", "guitar", "kalimba", "acoustic", "folk", "harp", "keys"],
              "chords": ["string", "warm", "acoustic", "organic", "folk"], "*": ["organic", "acoustic", "folk", "warm"]},
        "-": ["industrial", "dist", "metal", "hard", "noise", "harsh"],
    },
    "hard_techno": {
        "text": "Hard techno: dark, industrial, distorted and mono; avoid bright, plucky or bell-like sounds.",
        "+": {"*": ["dark", "hard", "industrial", "mono", "drive", "dist", "rumble"]},
        "-": ["bright", "pluck", "bell", "happy"],
    },
}

# How each style's kick and perc kit should *sound*, matched against audio analysis of the
# library (sound_analysis.py), like XO's sample space: a kick named "Kick Amber" can still be
# the right techno kick. Ranges are (lo, hi); a sound inside every range fits perfectly.
#   kick: pitch_hz body note, decay_ms length, click_hz attack brightness, dirt 0 clean .. 1 distorted
#   kit:  snare_decay_ms / hat_decay_ms (snare+clap, hats), snare_dirt, hand = hand drums wanted
SOUND_TARGETS: dict[str, dict] = {
    "techno": {"kick": {"pitch_hz": (42, 60), "decay_ms": (250, 500), "click_hz": (180, 600), "dirt": (0, 0.6)},
               "kit": {"snare_decay_ms": (120, 400), "hat_decay_ms": (30, 220), "snare_dirt": (0, 0.7)}},
    "acid_techno": {"kick": {"pitch_hz": (45, 62), "decay_ms": (220, 450), "click_hz": (200, 650), "dirt": (0, 0.5)},
                    "kit": {"snare_decay_ms": (100, 350), "hat_decay_ms": (30, 200)}},
    "house": {"kick": {"pitch_hz": (45, 65), "decay_ms": (200, 420), "click_hz": (180, 550), "dirt": (0, 0.3)},
              "kit": {"snare_decay_ms": (150, 450), "hat_decay_ms": (40, 260), "snare_dirt": (0, 0.4), "clap": True}},
    "happy_house": {"kick": {"pitch_hz": (48, 66), "decay_ms": (180, 360), "click_hz": (250, 750), "dirt": (0, 0.25)},
                    "kit": {"snare_decay_ms": (150, 400), "hat_decay_ms": (40, 220), "snare_dirt": (0, 0.3), "clap": True}},
    "detroit_house": {"kick": {"pitch_hz": (45, 62), "decay_ms": (220, 450), "click_hz": (150, 450), "dirt": (0, 0.3)},
                      "kit": {"snare_decay_ms": (150, 450), "hat_decay_ms": (40, 260), "snare_dirt": (0, 0.4), "clap": True}},
    "dnb": {"kick": {"pitch_hz": (45, 75), "decay_ms": (100, 250), "click_hz": (300, 1100), "dirt": (0, 0.4)},
            "kit": {"snare_decay_ms": (120, 320), "hat_decay_ms": (25, 160), "snare": True}},
    "breakbeat": {"kick": {"pitch_hz": (45, 80), "decay_ms": (120, 300), "click_hz": (250, 900), "dirt": (0, 0.5)},
                  "kit": {"snare_decay_ms": (120, 350), "hat_decay_ms": (30, 200), "snare": True}},
    "garage": {"kick": {"pitch_hz": (45, 70), "decay_ms": (120, 280), "click_hz": (250, 800), "dirt": (0, 0.3)},
               "kit": {"snare_decay_ms": (100, 300), "hat_decay_ms": (25, 150), "snare_dirt": (0, 0.3)}},
    "tribal": {"kick": {"pitch_hz": (42, 60), "decay_ms": (250, 500), "click_hz": (150, 450), "dirt": (0, 0.4)},
               "kit": {"hat_decay_ms": (30, 220), "hand": 2}},
    "psytrance": {"kick": {"pitch_hz": (40, 56), "decay_ms": (120, 230), "click_hz": (300, 900), "dirt": (0, 0.3)},
                  "kit": {"snare_decay_ms": (80, 250), "hat_decay_ms": (20, 120)}},
    "tekno": {"kick": {"pitch_hz": (40, 62), "decay_ms": (350, 900), "click_hz": (250, 900), "dirt": (0.4, 1)},
              "kit": {"snare_decay_ms": (100, 350), "hat_decay_ms": (20, 160), "snare_dirt": (0.3, 1)}},
    "hard_techno": {"kick": {"pitch_hz": (40, 62), "decay_ms": (300, 750), "click_hz": (250, 900), "dirt": (0.4, 1)},
                    "kit": {"snare_decay_ms": (100, 350), "hat_decay_ms": (20, 160), "snare_dirt": (0.3, 1)}},
    # The gabber kick is a tonal, distorted note, so its body sits higher than a sub kick.
    "gabber": {"kick": {"pitch_hz": (50, 95), "decay_ms": (350, 1000), "click_hz": (300, 1400), "dirt": (0.6, 1)},
               "kit": {"snare_decay_ms": (80, 300), "hat_decay_ms": (20, 140), "snare_dirt": (0.4, 1)}},
    "techno_balkan": {"kick": {"pitch_hz": (42, 60), "decay_ms": (250, 500), "click_hz": (180, 600), "dirt": (0, 0.6)},
                      "kit": {"snare_decay_ms": (120, 400), "hat_decay_ms": (30, 220), "hand": 1}},
    "techno_balkan_2": {"kick": {"pitch_hz": (42, 60), "decay_ms": (250, 500), "click_hz": (150, 500), "dirt": (0, 0.4)},
                        "kit": {"hat_decay_ms": (30, 220), "hand": 2}},
    "folk_step": {"kick": {"pitch_hz": (45, 75), "decay_ms": (180, 400), "click_hz": (150, 700), "dirt": (0, 0.3)},
                  "kit": {"snare_decay_ms": (100, 400), "snare_dirt": (0, 0.3), "hand": 1}},
}
# How far outside a range counts as one full miss.
_SOUND_SCALES = {"pitch_hz": 15.0, "decay_ms": 150.0, "click_hz": 250.0, "dirt": 0.3,
                 "snare_decay_ms": 120.0, "hat_decay_ms": 80.0, "snare_dirt": 0.3}
SOUND_FIT_MAX = 16.0

# Audio features by lowercase file name ("kick 909 1.aif", "909 core kit.adg"); the server
# loads them from the sound_analysis cache with set_sound_features().
SOUND_FEATURES: dict[str, dict] = {}


def set_sound_features(features: dict[str, dict] | None) -> None:
    SOUND_FEATURES.clear()
    SOUND_FEATURES.update(features or {})


CANDIDATES_PER_ROLE = 30
# Fallbacks when every ranked pick fails: DS Kick for the kick, a Core Kit for the perc.
DEFAULT_KIT_NAMES = ("DS Kick", "909 Core Kit.adg", "808 Core Kit.adg")


def style_key(style: str) -> str | None:
    s = (style or "").strip().lower().replace(" ", "_").replace("-", "_")
    if s in STYLE_PROFILES:
        return s
    if "tekno" in s:
        return "tekno"
    if "hard" in s and "techno" in s:
        return "hard_techno"
    if "acid" in s:
        return "acid_techno"
    # Most specific name wins, so "happy_house" doesn't fall back to plain "house".
    return next((key for key in sorted(STYLE_PROFILES, key=len, reverse=True) if key in s), None)


def style_profile(style: str) -> dict:
    key = style_key(style)
    if key is None:
        return {"text": "", "+": {}, "-": []}
    return {**STYLE_PROFILES[key], "sound": SOUND_TARGETS.get(key, {})}


def sound_features(item: dict) -> dict | None:
    return SOUND_FEATURES.get(str(item.get("name") or "").lower())


def features_for_device(device_name: str) -> dict | None:
    """Analysis for a device already on a track: Live names a Simpler after its sample and a
    Drum Rack after its preset, both without the file extension."""
    base = (device_name or "").strip().lower()
    return next((SOUND_FEATURES[base + ext] for ext in ("", ".wav", ".aif", ".aiff", ".adg") if base + ext in SOUND_FEATURES), None)


def _misses(values: dict, target: dict) -> float:
    total = 0.0
    for k, rng in target.items():
        v = values.get(k)
        if not isinstance(rng, tuple) or v is None:
            continue
        lo, hi = rng
        dist = lo - v if v < lo else v - hi if v > hi else 0.0
        total += min(2.0, dist / _SOUND_SCALES[k])
    return total


def sound_fit(item: dict, role: str, profile: dict) -> float | None:
    """How well a kick or kit *sounds* like the style: up to SOUND_FIT_MAX, negative for a poor fit,
    None when the sound wasn't analysed (synth presets, compressed Pack samples) or the style has no target."""
    feat = sound_features(item)
    targets = profile.get("sound") or {}
    if not feat:
        return None
    if role == "drums" and "kick" in targets and feat.get("pitch_hz"):
        return max(-12.0, SOUND_FIT_MAX - 7 * _misses(feat, targets["kick"]))
    if role == "perc" and feat.get("kit") and "kit" in targets:
        t, lanes = targets["kit"], set(feat.get("lanes") or [])
        ranged = [k for k, v in t.items() if isinstance(v, tuple)]
        measured = sum(1 for k in ranged if feat.get(k) is not None) / len(ranged) if ranged else 1.0
        # Kits built from Pack samples often can't be measured: only what was heard earns the full score.
        s = SOUND_FIT_MAX * (0.4 + 0.6 * measured) - 6 * _misses(feat, t)
        # Perc carries every drum but the kick, so a kit missing hats or a backbeat sound can't play the groove.
        if "hat" not in lanes:
            s -= 10
        if not lanes & {"snare", "clap"}:
            s -= 10
        for lane in ("clap", "snare"):
            if t.get(lane) and lane not in lanes:
                s -= 5
        if t.get("hand"):
            s += 4 * min(feat.get("hand_drums") or 0, 3) - (6 if (feat.get("hand_drums") or 0) < t["hand"] else 0)
        return max(-12.0, min(SOUND_FIT_MAX + 6, s))
    return None


def describe_sound(item: dict) -> str | None:
    """Short plain-language description of an analysed kick or kit, for the AI and the picker."""
    f = sound_features(item)
    if not f:
        return None
    if f.get("kit"):
        bits = [f"{f.get('pads')} pads"]
        if f.get("snare_decay_ms") is not None:
            bits.append(f"snare/clap {int(f['snare_decay_ms'])} ms")
        if f.get("hat_decay_ms") is not None:
            bits.append(f"hats {int(f['hat_decay_ms'])} ms")
        if f.get("snare_dirt") is not None:
            bits.append("gritty" if f["snare_dirt"] >= 0.4 else "clean")
        if f.get("hand_drums"):
            bits.append(f"{f['hand_drums']} hand drums")
        missing = [l for l in ("hat", "snare", "clap") if l not in (f.get("lanes") or [])]
        if missing:
            bits.append("no " + "/".join(missing))
        words = str(f.get("words") or "")
        return ", ".join(bits) + (f"; samples: {words[:90]}" if words else "")
    if not f.get("pitch_hz"):
        return None
    d = f.get("dirt") or 0
    tone = "distorted" if d >= 0.6 else "saturated" if d >= 0.3 else "clean"
    click = "hard click" if f["click_hz"] >= 600 else "punchy" if f["click_hz"] >= 250 else "soft attack"
    drop = ", pitch drop" if (f.get("attack_hz") or 0) > 2 * f["pitch_hz"] else ""
    return f"{int(f['pitch_hz'])} Hz body, {int(f['decay_ms'])} ms, {click}, {tone}{drop}"


def display_name(item: dict) -> str:
    return re.sub(r"\.(adg|adv|alc|amxd)$", "", str(item.get("name") or ""), flags=re.I)


def role_pool(items: list[dict], role: str) -> list[dict]:
    spec = ROLE_POOLS[role]
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict) or not it.get("is_loadable") or not it.get("uri"):
            continue
        path = str(it.get("path") or "").lower()
        name = str(it.get("name") or "")
        if spec.get("exact"):
            if path not in spec["paths"]:
                continue
        elif not any(path == p or path.startswith(p + "/") for p in spec["paths"]):
            continue
        if spec.get("ext") and not name.lower().endswith(spec["ext"]):
            continue
        if spec.get("name_re") and not re.search(spec["name_re"], name, re.I):
            continue
        key = (path, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _score(item: dict, role: str, profile: dict, prompt_words: list[str]) -> float:
    text = f"{item.get('name', '')} {item.get('path', '')}".lower()
    pos_role, neg_role = ROLE_WORDS.get(role, ([], []))
    plus = profile.get("+", {})
    s = 0.0
    s += 3 * sum(1 for w in pos_role if w in text)
    s -= 6 * sum(1 for w in neg_role if w in text)
    # The pad shares the chords' style words unless a profile names pad words of its own.
    s += 8 * sum(1 for w in plus.get(role, plus.get("chords", []) if role == "pad" else []) if w in text)
    s += 4 * sum(1 for w in plus.get("*", []) if w in text)
    s -= 10 * sum(1 for w in profile.get("-", []) if w in text)
    s += 12 * sum(1 for w in prompt_words if w in text)  # the producer's own words beat style defaults
    if role == "perc" and "core kit" in text:
        s += 2  # dependable, well-mapped kits
    if role == "drums" and display_name(item) == "DS Kick":
        s += 12  # the go-to kick: tunable and punchy, so it always reaches the shortlist
    if "mpe" in text:
        s -= 8  # MPE presets sound flat when played from plain MIDI clips
    feat = sound_features(item)
    if feat and feat.get("kit"):
        # Kit names rarely say much ("Abyss Kit"); the samples on its pads do ("Kick 909", "Conga Hi").
        pads = str(feat.get("words") or "").lower()
        s += 3 * sum(1 for w in plus.get(role, []) + plus.get("drums", []) + plus.get("*", []) if w in pads and w not in text)
        s -= 4 * sum(1 for w in profile.get("-", []) if w in pads and w not in text)
        s += 4 * sum(1 for w in prompt_words if w in pads and w not in text)
    fit = sound_fit(item, role, profile)
    return s + (fit or 0.0)


def rank_candidates(pool: list[dict], role: str, style: str, prompt: str | None = None, *,
                    limit: int = CANDIDATES_PER_ROLE, rng: random.Random | None = None,
                    exclude: set[str] | None = None) -> list[dict]:
    rng = rng or random.Random()
    profile = style_profile(style)
    words = [w for w in re.findall(r"[a-z0-9]+", (prompt or "").lower()) if len(w) > 2]
    scored = []
    for it in pool:
        if exclude and it.get("uri") in exclude:
            continue
        # Jitter reorders near-equal candidates so re-picks explore, without letting
        # a poor match jump a clearly better one.
        scored.append((_score(it, role, profile, words) + rng.uniform(0, 2.5), it))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [it for _, it in scored[:limit]]


def _matched_words(item: dict, role: str, profile: dict, prompt_words: list[str]) -> dict[str, list[str]]:
    """
    Which role/style/prompt words a preset's name hits, for grouping and display. Only the name
    counts (the folder already says the role, and "Piano & Keys" would make every preset in it
    a "keys" match), and words must start a word, so "arp" doesn't hit "Harpsichord".
    """
    name = display_name(item).lower()
    pos_role, neg_role = ROLE_WORDS.get(role, ([], []))
    plus = profile.get("+", {})
    style_words = plus.get(role, plus.get("chords", []) if role == "pad" else []) + plus.get("*", [])

    def hits(words):
        return sorted({w for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w), name)})

    return {
        "style": hits(style_words),
        "role": hits(pos_role),
        "prompt": hits(prompt_words),
        "clash": hits(neg_role + profile.get("-", []) + ["mpe"]),
    }


GROUP_LABELS = {
    "prompt": "Matches your notes",
    "style": "Best for {style}",
    "role": "Solid {role} sounds",
    "other": "Other {role} sounds",
    "clash": "Probably clashes with {style}",
}


def group_candidates(pool: list[dict], role: str, style: str, prompt: str | None = None, *,
                     per_group: int = 40, query: str | None = None) -> list[dict]:
    """
    A role's browser pool grouped by how well each sound fits the style, best first, for a
    hand-picked swap. Deterministic (no jitter). An item goes in its best group:
    prompt match > style match > role match > other; anything hitting a clash word goes last.
    """
    profile = style_profile(style)
    words = [w for w in re.findall(r"[a-z0-9]+", (prompt or "").lower()) if len(w) > 2]
    q = (query or "").strip().lower()
    buckets: dict[str, list[tuple[float, dict]]] = {k: [] for k in GROUP_LABELS}
    for it in pool:
        if q and q not in f"{it.get('name', '')} {it.get('path', '')}".lower():
            continue
        m = _matched_words(it, role, profile, words)
        fit = sound_fit(it, role, profile)
        # A sound whose audio matches the style counts as a style match even when its name says nothing.
        sounds_right = fit is not None and fit >= SOUND_FIT_MAX - 4
        key = ("clash" if m["clash"] else "prompt" if m["prompt"] else "style" if m["style"] or sounds_right
               else "role" if m["role"] else "other")
        row = {
            "name": display_name(it), "file": it.get("name"), "folder": it.get("path"), "uri": it.get("uri"),
            "tags": list(dict.fromkeys(m["prompt"] + m["style"] + (["sounds right"] if sounds_right else []) + m["role"])),
            "clash": m["clash"],
            "sound": describe_sound(it),
        }
        buckets[key].append((_score(it, role, profile, words), row))
    role_label = role if role != "fx" else "FX"
    out = []
    for key, rows in buckets.items():
        if not rows:
            continue
        rows.sort(key=lambda t: (-t[0], t[1]["name"].lower()))
        out.append({
            "key": key,
            "label": GROUP_LABELS[key].format(style=style.replace("_", " ") or "this style", role=role_label),
            "total": len(rows),
            "items": [r for _, r in rows[:per_group]],
        })
    return out


def heuristic_palette(candidates: dict[str, list[dict]]) -> dict[str, dict]:
    picks: dict[str, dict] = {}
    for role, cands in candidates.items():
        if not cands:
            continue
        choice = cands[0]
        other = {"perc": "drums", "pad": "chords"}.get(role)
        if other in picks and choice.get("uri") == picks[other]["item"].get("uri") and len(cands) > 1:
            choice = cands[1]
        picks[role] = {"item": choice, "reason": "best match for the style profile", "source": "heuristic"}
    return picks


AI_SYSTEM = (
    "You are an expert electronic music producer choosing a coherent sound palette in Ableton Live. "
    "For each role pick EXACTLY ONE candidate id from that role's list. The sounds must work together "
    "as one track: complementary frequency ranges, one clear character, nothing that clashes with the style. "
    "The drums role is a single kick sound; the perc kit plays every other drum (snare, clap, hats, percussion). "
    "The pad must differ from the chords sound. A candidate's 'sound' field is measured from its audio "
    "(body pitch, length, attack, distortion; a kit's snare/hat lengths and pad samples): trust it over the name. "
    "Return STRICT JSON only: "
    '{"choices": {"<role>": {"id": <number>, "reason": "<one short sentence>"}}}'
)


async def ai_palette(
    call_ai: Callable[[str, str, float], Awaitable[tuple]],
    *, style: str, prompt: str | None, candidates: dict[str, list[dict]], temperature: float = 0.6,
) -> tuple[dict[str, dict], dict]:
    """One AI request for the whole palette. Returns (picks, meta); invalid roles are omitted."""
    by_id: dict[str, dict[int, dict]] = {}
    lists = {}
    for role, cands in candidates.items():
        by_id[role] = {}
        rows = []
        for n, it in enumerate(cands, 1):
            by_id[role][n] = it
            row = {"id": n, "name": display_name(it), "folder": it.get("path")}
            if describe_sound(it):
                row["sound"] = describe_sound(it)
            rows.append(row)
        lists[role] = {"role": ROLE_POOLS[role]["label"], "candidates": rows}

    profile = style_profile(style)
    user = (
        f"Style: {style}\n"
        f"Style character: {profile.get('text') or 'use your judgement for this style'}\n"
        f"Producer's notes: {prompt or 'none'}\n\n"
        f"Roles and candidates: {json.dumps(lists)}"
    )
    obj, meta = await call_ai(AI_SYSTEM, user, float(temperature))
    if not meta.get("ok") or not isinstance(obj, dict):
        return {}, meta
    choices = obj.get("choices") if isinstance(obj.get("choices"), dict) else {}
    picks: dict[str, dict] = {}
    for role, table in by_id.items():
        c = choices.get(role)
        if not isinstance(c, dict):
            continue
        try:
            item = table.get(int(c.get("id")))
        except (TypeError, ValueError):
            item = None
        if item is not None:
            picks[role] = {"item": item, "reason": str(c.get("reason") or "AI choice"), "source": "ai"}
    return picks, {"ok": True, "missing": sorted(set(candidates) - set(picks))}


AI_TOP_PICKS_SYSTEM = (
    "You are an expert electronic music producer choosing a sound for one track in Ableton Live. "
    "Pick the 3 best candidates for this role and style, best first. They must sit well with the "
    "instruments already on the other tracks: complementary frequency range and one shared character. "
    "Pick 3 different-sounding options so the producer has a real choice. A candidate's 'sound' field is "
    "measured from its audio: trust it over the name. Return STRICT JSON only: "
    '{"picks": [{"id": <number>, "reason": "<one short sentence>"}]}'
)
AI_TOP_PICKS_CANDIDATES = 40


async def ai_top_picks(
    call_ai: Callable[[str, str, float], Awaitable[tuple]],
    *, groups: list[dict], role: str, style: str, prompt: str | None,
    others: dict[str, str] | None = None, current: str | None = None, count: int = 3, temperature: float = 0.5,
) -> tuple[list[dict], dict]:
    """
    Up to `count` AI picks for one role, drawn from group_candidates() output (clashes left out,
    best groups first). Returns (rows from the groups with a "reason" added, meta).
    """
    rows = [it for g in groups if g["key"] != "clash" for it in g["items"]][:AI_TOP_PICKS_CANDIDATES]
    if not rows:
        return [], {"ok": False, "error": "no_candidates"}
    table = {n: r for n, r in enumerate(rows, 1)}
    user = (
        f"Style: {style}\n"
        f"Style character: {style_profile(style).get('text') or 'use your judgement for this style'}\n"
        f"Role: {ROLE_POOLS[role]['label']}\n"
        f"Currently on this track: {current or 'nothing'}\n"
        f"Other tracks: {json.dumps(others or {})}\n"
        f"Producer's notes: {prompt or 'none'}\n\n"
        f"Candidates: {json.dumps([{'id': n, 'name': r['name'], 'folder': r['folder'], **({'sound': r['sound']} if r.get('sound') else {})} for n, r in table.items()])}"
    )
    obj, meta = await call_ai(AI_TOP_PICKS_SYSTEM, user, float(temperature))
    if not meta.get("ok") or not isinstance(obj, dict) or not isinstance(obj.get("picks"), list):
        return [], meta if not meta.get("ok") else {"ok": False, "error": "bad_ai_response"}
    out, seen = [], set()
    for p in obj["picks"]:
        try:
            n = int(p.get("id"))
        except (AttributeError, TypeError, ValueError):
            continue
        if n in table and n not in seen:
            seen.add(n)
            out.append({**table[n], "reason": str(p.get("reason") or "")})
        if len(out) >= count:
            break
    return out, {"ok": bool(out), "candidates": len(rows), **({} if out else {"error": "no_valid_picks"})}


def default_kit(pool: list[dict]) -> dict | None:
    by_name = {str(it.get("name")): it for it in pool}
    for n in DEFAULT_KIT_NAMES:
        if n in by_name:
            return by_name[n]
    return pool[0] if pool else None


# ---------------------------------------------------------------- drum pad mapping

# Lane -> pad-name patterns, most specific first. Matched against lowercase pad names.
LANE_PATTERNS: dict[str, list[str]] = {
    "kick": [r"\bkick\b", r"\bkik\b", r"\bbd\b", r"bass ?drum"],
    "snare": [r"\bsnare\b", r"\bsd\b", r"\bsnr\b"],
    "clap": [r"\bclap\b", r"\bcp\b", r"\bclp\b"],
    "ch": [r"closed", r"\bchh?\b", r"hi[- ]?hat(?!.*open)", r"\bhh\b", r"\bhat\b(?!.*open)"],
    "oh": [r"\bopen\b", r"\bohh?\b"],
    "perc1": [r"shaker", r"\brim\b", r"cowbell", r"\bperc", r"tamb", r"clave", r"block", r"conga hi", r"bongo"],
    "perc2": [r"\btom\b", r"conga", r"low perc", r"\bperc", r"djembe"],
    "fx": [r"\bfx\b", r"crash", r"ride", r"cymbal", r"noise"],
}
GM_DEFAULTS = {"kick": 36, "snare": 38, "clap": 39, "ch": 42, "oh": 46, "perc1": 50, "perc2": 45, "fx": 49}


def map_drum_pads(pads: list[dict]) -> dict[str, int]:
    """
    Map Pulse drum lanes to this kit's pad notes by pad name. Lanes with no matching pad
    keep the General MIDI note only if the kit has a pad there; otherwise they borrow the
    nearest unused filled pad, so every lane makes a sound.
    """
    names = {int(p["note"]): str(p.get("name") or "").lower() for p in pads if "note" in p}
    if not names:
        return {}
    used: set[int] = set()
    out: dict[str, int] = {}
    for lane, patterns in LANE_PATTERNS.items():
        for pat in patterns:
            hit = next((n for n in sorted(names) if n not in used and re.search(pat, names[n])), None)
            if hit is not None:
                out[lane] = hit
                used.add(hit)
                break
    for lane, gm in GM_DEFAULTS.items():
        if lane in out:
            continue
        if gm in names and gm not in used:
            out[lane] = gm
        else:
            free = [n for n in names if n not in used] or list(names)
            out[lane] = min(free, key=lambda n: abs(n - gm))
        used.add(out[lane])
    return out
