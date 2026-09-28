"""
Instrument palette selection for Pulse Studio.

Picks one sound per Pulse role (drums, bass, perc, stabs, fx, chords) from the cached
browser index, as a set that fits together:

1. Each role draws from the matching browser category (real kits for drums, Sounds/Bass
   for bass, ...), not a keyword search over the whole library.
2. Candidates are ranked with a per-style timbre profile, with a little jitter so
   re-picks vary, and the top few per role go to the AI in a single request.
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
    "drums": {"paths": ["drums"], "exact": True, "ext": (".adg",), "label": "main drum kit"},
    "perc": {"paths": ["drums"], "exact": True, "ext": (".adg",), "label": "percussion kit (complements the main kit)"},
    "bass": {"paths": ["sounds/bass"], "label": "bass"},
    "stabs": {"paths": ["sounds/synth rhythmic", "sounds/synth keys", "sounds/piano & keys"], "label": "stabs / rhythmic chords"},
    "fx": {"paths": ["sounds/effects", "sounds/ambient & evolving"], "label": "FX, risers and textures"},
    "chords": {"paths": ["sounds/pad", "sounds/ambient & evolving", "sounds/synth keys"], "label": "pads / sustained chords"},
    "pad": {"paths": ["sounds/pad", "sounds/ambient & evolving"], "label": "atmospheric pad held above the chords (differs from the chords sound)"},
}

# Words that suit (+) or clash with (-) a role regardless of style.
ROLE_WORDS: dict[str, tuple[list[str], list[str]]] = {
    "drums": (["core kit", "kit"], ["percussion", "perc kit", "hand", "ethnic", "orchestral"]),
    "perc": (["perc", "percussion", "conga", "bongo", "hand", "latin", "afro", "tribal", "shaker", "tabla", "world"], []),
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

CANDIDATES_PER_ROLE = 30
DEFAULT_KIT_NAMES = ("909 Core Kit.adg", "808 Core Kit.adg")


def style_profile(style: str) -> dict:
    s = (style or "").strip().lower().replace(" ", "_").replace("-", "_")
    if s in STYLE_PROFILES:
        return STYLE_PROFILES[s]
    if "tekno" in s:
        return STYLE_PROFILES["tekno"]
    if "hard" in s and "techno" in s:
        return STYLE_PROFILES["hard_techno"]
    if "acid" in s:
        return STYLE_PROFILES["acid_techno"]
    # Most specific name wins, so "happy_house" doesn't fall back to plain "house".
    for key in sorted(STYLE_PROFILES, key=len, reverse=True):
        if key in s:
            return STYLE_PROFILES[key]
    return {"text": "", "+": {}, "-": []}


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
    if role == "drums" and "core kit" in text:
        s += 2  # dependable, well-mapped kits
    if "mpe" in text:
        s -= 8  # MPE presets sound flat when played from plain MIDI clips
    return s


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
    "The perc kit must differ from the main drum kit, and the pad from the chords sound. Return STRICT JSON only: "
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
            rows.append({"id": n, "name": display_name(it), "folder": it.get("path")})
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
