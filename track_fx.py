"""
Per-track processing for Pulse Studio: the effect chain each role gets for a style, how the
chosen kick/kit is tuned from its audio analysis, and the master limiter.

Pure logic: returns device specs; the server loads them through PulseBridge.

A device spec is {"device": <stock device name>, "params": {<name or "A|B" aliases>: <setting>}}
where a setting is
    0.5                      normalized 0..1 across the parameter's range (styles.json convention)
    {"display": -0.3}        a value in the units Live shows (dB, Hz, ms, %; needs PulseBridge 0.3)
    {"item": "Analog Clip"}  an option of a switch/menu parameter (needs PulseBridge 0.3)
    {"value": 1}             Live's raw parameter value
"""
from __future__ import annotations

import math

# Styles grouped by how hard their processing should push.
STYLE_FAMILY = {
    "house": "clean", "happy_house": "clean", "detroit_house": "clean", "garage": "clean", "folk_step": "clean",
    "techno": "dark", "acid_techno": "dark", "tribal": "dark", "techno_balkan": "dark", "techno_balkan_2": "dark",
    "hard_techno": "hard", "tekno": "hard", "gabber": "hard",
    "psytrance": "tight",
    "dnb": "break", "breakbeat": "break",
}

# EQ Eight filter types (raw values of "<n> Filter Type A").
LOW_CUT_48, LOW_CUT_12, LOW_SHELF, BELL, NOTCH, HIGH_SHELF, HIGH_CUT_12, HIGH_CUT_48 = range(8)


def eq(low_cut_hz: float | None = None, high_cut_hz: float | None = None, low_cut_type: int = LOW_CUT_48,
       bell: tuple[float, float, float] | None = None) -> dict:
    """EQ Eight with a low cut on band 1, an optional bell on band 3 and a high cut on band 8."""
    p: dict = {}
    if low_cut_hz:
        p.update({"1 Filter On A": {"value": 1}, "1 Filter Type A": {"value": low_cut_type}, "1 Frequency A": {"display": low_cut_hz}})
    if bell:
        hz, gain, q = bell
        p.update({"3 Filter On A": {"value": 1}, "3 Filter Type A": {"value": BELL}, "3 Frequency A": {"display": hz},
                  "3 Gain A": {"value": gain}, "3 Q A": {"display": q}})
    if high_cut_hz:
        p.update({"8 Filter On A": {"value": 1}, "8 Filter Type A": {"value": HIGH_CUT_12}, "8 Frequency A": {"display": high_cut_hz}})
    return {"device": "EQ Eight", "params": p}


def glue(threshold_db: float, ratio: int = 0, attack: int = 3, release: int = 6, makeup_db: float = 0.0, mix: float = 1.0) -> dict:
    """Glue Compressor. ratio 0/1/2 = 2:1/4:1/10:1; attack 0..6 = 0.01..30 ms (3 = 1 ms);
    release 0..6 = 0.1..1.2 s, 6 = Auto."""
    return {"device": "Glue Compressor", "params": {
        "Threshold": {"value": threshold_db}, "Ratio": {"value": ratio}, "Attack": {"value": attack},
        "Release": {"value": release}, "Output": {"value": makeup_db}, "Dry/Wet": mix}}


def saturator(drive_db: float, kind: str = "Analog Clip", mix: float = 1.0) -> dict:
    # Output pulls back about half the drive so switching it on doesn't just make the track louder.
    return {"device": "Saturator", "params": {
        "Drive": {"display": round(drive_db, 1)}, "Type": {"item": kind}, "Output": {"display": round(-drive_db / 2, 1)},
        "Dry/Wet": mix}}


def bass_mono(hz: float = 120) -> dict:
    return {"device": "Utility", "params": {"Bass Mono": {"value": 1}, "Bass Freq": {"display": hz}}}


# role -> chain, per family. Low cuts keep everything but the kick and bass out of the sub, so
# the low end stays clean and the master limiter isn't pumped by rumble.
ROLE_CHAINS: dict[str, dict[str, list[dict]]] = {
    "clean": {
        "drums": [eq(low_cut_hz=28, low_cut_type=LOW_CUT_12)],
        "bass": [eq(low_cut_hz=30), bass_mono(), glue(-12, ratio=0, attack=3, makeup_db=2)],
        "perc": [eq(low_cut_hz=180), glue(-10, ratio=0, attack=4, makeup_db=1.5)],
        "stabs": [eq(low_cut_hz=180, high_cut_hz=14000)],
        "chords": [eq(low_cut_hz=200, high_cut_hz=14000)],
        "pad": [eq(low_cut_hz=250, high_cut_hz=12000)],
        "fx": [eq(low_cut_hz=200)],
    },
    "dark": {
        "drums": [eq(low_cut_hz=30, low_cut_type=LOW_CUT_12)],
        "bass": [eq(low_cut_hz=30), bass_mono(), saturator(4), glue(-14, ratio=1, attack=3, makeup_db=2)],
        "perc": [eq(low_cut_hz=200, high_cut_hz=15000), glue(-12, ratio=0, attack=4, makeup_db=2)],
        "stabs": [eq(low_cut_hz=200, high_cut_hz=9000), saturator(3, mix=0.5)],
        "chords": [eq(low_cut_hz=220, high_cut_hz=8000)],
        "pad": [eq(low_cut_hz=250, high_cut_hz=7000)],
        "fx": [eq(low_cut_hz=250)],
    },
    "hard": {
        "drums": [eq(low_cut_hz=32, low_cut_type=LOW_CUT_12)],
        "bass": [eq(low_cut_hz=35), bass_mono(), saturator(8, kind="Hard Curve"), glue(-16, ratio=1, attack=2, makeup_db=3)],
        "perc": [eq(low_cut_hz=250), saturator(6, kind="Hard Curve", mix=0.6), glue(-14, ratio=1, attack=4, makeup_db=2)],
        "stabs": [eq(low_cut_hz=250, high_cut_hz=10000), saturator(6, kind="Hard Curve", mix=0.6)],
        "chords": [eq(low_cut_hz=250, high_cut_hz=8000)],
        "pad": [eq(low_cut_hz=300, high_cut_hz=7000)],
        "fx": [eq(low_cut_hz=300)],
    },
    "tight": {
        "drums": [eq(low_cut_hz=30, low_cut_type=LOW_CUT_12)],
        "bass": [eq(low_cut_hz=35), bass_mono(150), glue(-14, ratio=1, attack=1, makeup_db=2)],
        "perc": [eq(low_cut_hz=250, high_cut_hz=16000), glue(-12, ratio=0, attack=3, makeup_db=1.5)],
        "stabs": [eq(low_cut_hz=250, high_cut_hz=12000)],
        "chords": [eq(low_cut_hz=250, high_cut_hz=10000)],
        "pad": [eq(low_cut_hz=300, high_cut_hz=9000)],
        "fx": [eq(low_cut_hz=250)],
    },
    "break": {
        "drums": [eq(low_cut_hz=30, low_cut_type=LOW_CUT_12)],
        "bass": [eq(low_cut_hz=30), bass_mono(), saturator(3), glue(-14, ratio=1, attack=3, makeup_db=2)],
        # Breaks carry the groove, so the perc bus keeps more body and gets punchier compression.
        "perc": [eq(low_cut_hz=120), glue(-14, ratio=1, attack=4, makeup_db=2.5)],
        "stabs": [eq(low_cut_hz=200, high_cut_hz=12000)],
        "chords": [eq(low_cut_hz=220, high_cut_hz=11000)],
        "pad": [eq(low_cut_hz=250, high_cut_hz=10000)],
        "fx": [eq(low_cut_hz=250)],
    },
}

# Last device on the master: a brickwall limiter so the mix can't clip.
MASTER_CHAIN = [{"device": "Limiter", "params": {"Ceiling": {"display": -0.3}, "Auto": {"value": 1}}}]


def family(style: str) -> str:
    s = (style or "").strip().lower().replace(" ", "_").replace("-", "_")
    if s in STYLE_FAMILY:
        return STYLE_FAMILY[s]
    if "hard" in s or "tekno" in s or "gabber" in s:
        return "hard"
    return next((f for k, f in sorted(STYLE_FAMILY.items(), key=lambda kv: -len(kv[0])) if k in s), "dark")


def role_chain(style: str, role: str, override: dict | None = None) -> list[dict]:
    """The style's chain for a role; styles.json "track_fx": {role: [specs]} replaces it."""
    if isinstance(override, dict) and isinstance(override.get(role), list):
        return [c for c in override[role] if isinstance(c, dict) and c.get("device")]
    return [dict(c) for c in ROLE_CHAINS[family(style)].get(role, [])]


# ---------------------------------------------------------------- analysis-driven tuning

def note_hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def kick_target_hz(kick_target: dict, song_root: int | None) -> float:
    """The kick note to aim for: the song's root in the style's pitch range when it fits, so the
    kick sits in key with the bass; otherwise the middle of the range."""
    lo, hi = kick_target.get("pitch_hz") or (45, 60)
    mid = math.sqrt(lo * hi)
    if song_root is None:
        return mid
    notes = [note_hz(12 * octave + 12 + int(song_root)) for octave in range(0, 3)]
    inside = [f for f in notes if lo <= f <= hi]
    pool = inside or notes
    return min(pool, key=lambda f: abs(math.log(f / mid)))


def _mid(rng, default):
    return (rng[0] + rng[1]) / 2 if isinstance(rng, tuple) else default


def instrument_settings(role: str, device_name: str, class_name: str, feat: dict | None,
                        targets: dict, song_root: int | None) -> tuple[dict, str | None]:
    """
    (param specs for the track's instrument, a note on what they do). The kick is pulled toward the
    style's measured target: DS Kick gets pitch/decay/drive/click; a sampled kick in Simpler is
    transposed to the target note and its tail trimmed when the analysis says it rings too long.
    """
    kick = targets.get("kick") if role == "drums" else None
    if not kick:
        return {}, None
    hz = kick_target_hz(kick, song_root)
    decay = _mid(kick.get("decay_ms"), 300)
    dirt = _mid(kick.get("dirt"), 0.2)
    if device_name.startswith("DS Kick"):
        # DS Kick's Pitch shows Hz on a curved internal scale, so it is set by its display.
        params = {
            "Pitch": {"display": round(hz, 1)},
            "Decay": {"display": round(max(15.0, min(90.0, decay / 10)), 1)},
            "overdrive|Drive": {"value": round(dirt * 100)},
            "Click": {"value": 1 if (kick.get("click_hz") or (0, 0))[0] >= 250 else 0},
        }
        return params, f"DS Kick tuned to {hz:.0f} Hz, ~{decay:.0f} ms, drive {dirt * 100:.0f}%"
    if class_name in ("OriginalSimpler", "MultiSampler") and feat and feat.get("pitch_hz"):
        params, notes = {}, []
        semis = max(-7, min(7, round(12 * math.log2(hz / feat["pitch_hz"]))))
        if semis:
            params["Transpose"] = {"value": semis}
            notes.append(f"transposed {semis:+d} st to ~{hz:.0f} Hz")
        hi = (kick.get("decay_ms") or (0, 10000))[1]
        if feat.get("decay_ms", 0) > hi * 1.15:
            params["Fade Out|Ve Release"] = {"display": hi}
            notes.append(f"tail trimmed to {hi:.0f} ms")
        return params, ("kick " + ", ".join(notes)) if notes else None
    return {}, None


def build_chain(extra: list[dict], chain: list[dict]) -> list[dict]:
    """Analysis-driven devices first, then the style chain minus any device the analysis already added."""
    have = {c["device"] for c in extra}
    return extra + [c for c in chain if c["device"] not in have]


def analysis_fx(role: str, feat: dict | None, targets: dict, device_name: str = "") -> list[dict]:
    """Extra processing when the measured sound falls short of the style: e.g. drive on a clean
    kick or kit for a style that wants grit. DS Kick handles its own drive."""
    if not feat:
        return []
    if role == "drums" and not device_name.startswith("DS Kick"):
        want = (targets.get("kick") or {}).get("dirt")
        have = feat.get("dirt")
    elif role == "perc":
        want = (targets.get("kit") or {}).get("snare_dirt")
        have = feat.get("snare_dirt")
    else:
        return []
    if not isinstance(want, tuple) or have is None or have >= want[0]:
        return []
    gap = want[0] - have
    return [saturator(4 + 14 * gap, kind="Hard Curve" if want[0] >= 0.5 else "Analog Clip")]
