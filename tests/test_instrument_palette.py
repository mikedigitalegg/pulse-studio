import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import instrument_palette as ip  # noqa: E402


def item(name, path, uri=None):
    return {"name": name, "path": path, "uri": uri or f"query:{path}:{name}", "is_loadable": True}


INDEX = [
    item("909 Core Kit.adg", "drums"),
    item("Boom Bap Kit.adg", "drums"),
    item("Conga Percussion Kit.adg", "drums"),
    item("Kick 909.wav", "drums/Drum Hits/Kick"),  # a one-shot, not a kit
    item("Dark Rumble Bass.adv", "sounds/Bass"),
    item("Fingered Bass.adv", "sounds/Bass"),
    item("Deep House Organ Bass.adv", "sounds/Bass"),
    item("Dub Stab Chord.adv", "sounds/Synth Rhythmic"),
    item("Grand Piano.adv", "sounds/Piano & Keys"),
    item("Riser Sweep.adv", "sounds/Effects"),
    item("Dark Drone Pad.adv", "sounds/Pad"),
    item("Warm Lush Pad.adv", "sounds/Pad"),
    item("Wobble Lead.adv", "sounds/Synth Lead"),  # not in any pool
    {"name": "Broken.adv", "path": "sounds/Bass", "uri": "x", "is_loadable": False},
]


def test_role_pools_use_the_right_folders():
    assert [i["name"] for i in ip.role_pool(INDEX, "drums")] == ["909 Core Kit.adg", "Boom Bap Kit.adg", "Conga Percussion Kit.adg"]
    assert {i["name"] for i in ip.role_pool(INDEX, "bass")} == {"Dark Rumble Bass.adv", "Fingered Bass.adv", "Deep House Organ Bass.adv"}
    assert [i["name"] for i in ip.role_pool(INDEX, "fx")] == ["Riser Sweep.adv"]
    assert "Wobble Lead.adv" not in {i["name"] for r in ip.ROLE_POOLS for i in ip.role_pool(INDEX, r)}


def test_ranking_follows_style_profile():
    rng = random.Random(1)
    bass = ip.role_pool(INDEX, "bass")
    assert ip.rank_candidates(bass, "bass", "techno", rng=rng)[0]["name"] == "Dark Rumble Bass.adv"
    assert ip.rank_candidates(bass, "bass", "house", rng=rng)[0]["name"] == "Deep House Organ Bass.adv"
    assert ip.rank_candidates(bass, "bass", "techno", rng=rng)[-1]["name"] == "Fingered Bass.adv"
    kits = ip.role_pool(INDEX, "perc")
    assert ip.rank_candidates(kits, "perc", "tribal", rng=rng)[0]["name"] == "Conga Percussion Kit.adg"
    pads = ip.role_pool(INDEX, "chords")
    assert ip.rank_candidates(pads, "chords", "techno", rng=rng)[0]["name"] == "Dark Drone Pad.adv"
    assert ip.rank_candidates(pads, "chords", "house", rng=rng)[0]["name"] == "Warm Lush Pad.adv"
    # The producer's prompt counts too.
    assert ip.rank_candidates(pads, "chords", "techno", "warm lush", rng=rng)[0]["name"] == "Warm Lush Pad.adv"


def test_style_aliases():
    assert ip.style_profile("Hard Techno") is ip.STYLE_PROFILES["hard_techno"]
    assert ip.style_profile("acid") is ip.STYLE_PROFILES["acid_techno"]
    assert ip.style_profile("deep house") is ip.STYLE_PROFILES["house"]
    assert ip.style_profile("polka")["text"] == ""


def test_ai_palette_maps_ids_and_drops_invalid():
    cands = {
        "drums": ip.role_pool(INDEX, "drums"),
        "bass": ip.role_pool(INDEX, "bass"),
        "fx": ip.role_pool(INDEX, "fx"),
    }
    seen = {}

    async def fake_ai(system, user, temperature):
        seen["user"] = user
        return {"choices": {"drums": {"id": 2, "reason": "punchy"}, "bass": {"id": 99}, "fx": "bad"}}, {"ok": True}

    picks, meta = asyncio.run(ip.ai_palette(fake_ai, style="techno", prompt="dark", candidates=cands))
    assert picks["drums"]["item"]["name"] == "Boom Bap Kit.adg" and picks["drums"]["reason"] == "punchy"
    assert set(picks) == {"drums"} and meta["missing"] == ["bass", "fx"]
    assert "Boom Bap Kit" in seen["user"] and "query:" not in seen["user"]  # names, not URIs


def test_heuristic_palette_keeps_perc_different_from_drums():
    kits = ip.role_pool(INDEX, "drums")
    picks = ip.heuristic_palette({"drums": kits, "perc": kits})
    assert picks["drums"]["item"] is not picks["perc"]["item"]


def test_default_kit_prefers_909():
    assert ip.default_kit(ip.role_pool(INDEX, "drums"))["name"] == "909 Core Kit.adg"
    assert ip.default_kit([]) is None


def test_map_drum_pads_by_name():
    pads = [
        {"note": 36, "name": "Kick 909"},
        {"note": 37, "name": "Rim 909"},
        {"note": 38, "name": "Snare 909"},
        {"note": 39, "name": "Clap 909"},
        {"note": 40, "name": "Hihat Closed 909"},
        {"note": 41, "name": "Hihat Open 909"},
        {"note": 43, "name": "Tom Low 909"},
        {"note": 49, "name": "Crash 909"},
    ]
    m = ip.map_drum_pads(pads)
    assert m["kick"] == 36 and m["snare"] == 38 and m["clap"] == 39
    assert m["ch"] == 40 and m["oh"] == 41
    assert m["perc1"] == 37 and m["perc2"] == 43 and m["fx"] == 49


def test_map_drum_pads_live_909_core_kit_names():
    # Pad names as Live 12's 909 Core Kit reports them.
    names = {36: "Bass Drum", 37: "Rim Shot", 38: "Snare Drum", 39: "Hand Clap", 40: "Bass Drum", 41: "Snare Drum",
             42: "Closed Hi Hat", 43: "Snare Drum", 44: "Low Tom", 45: "Mid Tom", 46: "Open Hi Hat", 47: "Hi Tom",
             48: "Crash", 49: "Crash", 50: "Ride", 51: "Ride"}
    m = ip.map_drum_pads([{"note": n, "name": v} for n, v in names.items()])
    assert m == {"kick": 36, "snare": 38, "clap": 39, "ch": 42, "oh": 46, "perc1": 37, "perc2": 44, "fx": 48}


def test_map_drum_pads_unnamed_kit_uses_nearest_filled_pads():
    m = ip.map_drum_pads([{"note": 60, "name": "A"}, {"note": 61, "name": "B"}, {"note": 62, "name": "C"}])
    assert set(m.values()) <= {60, 61, 62}
    assert ip.map_drum_pads([]) == {}


def test_every_known_style_has_its_own_profile():
    import json

    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge", "styles.json"), encoding="utf-8") as f:
        styles = json.load(f)["styles"]
    for key in styles:
        assert ip.style_profile(key) is ip.STYLE_PROFILES[key], key


def test_most_specific_profile_wins():
    assert ip.style_profile("happy_house") is ip.STYLE_PROFILES["happy_house"]
    assert ip.style_profile("my_detroit_house_edit") is ip.STYLE_PROFILES["detroit_house"]
    assert ip.style_profile("deep house") is ip.STYLE_PROFILES["house"]


def test_happy_and_dark_styles_pick_different_pads():
    pads = ip.role_pool(INDEX, "chords")
    rng = random.Random(0)
    assert ip.display_name(ip.rank_candidates(pads, "chords", "techno", rng=rng)[0]) == "Dark Drone Pad"
    assert ip.display_name(ip.rank_candidates(pads, "chords", "happy_house", rng=rng)[0]) == "Warm Lush Pad"
