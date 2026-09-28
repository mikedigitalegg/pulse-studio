"""
Hand-picked instrument swaps: a role's sounds grouped by style fit, and loading one onto
the role's track. The bridge is faked.

    python -m pytest tests/test_instrument_swap.py -q
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ableton_web_poc_server as srv  # noqa: E402
import instrument_palette as ip  # noqa: E402
from test_palette_restyle import FakeBridge, env, INDEX  # noqa: E402,F401

BASS = [
    {"name": "Dark Rumble Bass.adv", "path": "sounds/Bass", "uri": "a", "is_loadable": True},
    {"name": "Deep House Organ Bass.adv", "path": "sounds/Bass", "uri": "b", "is_loadable": True},
    {"name": "Plain Bass.adv", "path": "sounds/Bass", "uri": "c", "is_loadable": True},
    {"name": "Grainy Thing.adv", "path": "sounds/Bass", "uri": "d", "is_loadable": True},
    {"name": "Fingered Bass.adv", "path": "sounds/Bass", "uri": "e", "is_loadable": True},
]


def _groups(style, prompt=None, query=None):
    return {g["key"]: [i["name"] for i in g["items"]] for g in ip.group_candidates(BASS, "bass", style, prompt, query=query)}


def test_groups_follow_style():
    g = _groups("techno")
    assert list(g) == ["style", "role", "other", "clash"]
    assert g["style"] == ["Dark Rumble Bass"]
    assert "Fingered Bass" in g["clash"]  # "fingered" never suits a synth bass role
    assert g["other"] == ["Grainy Thing"]
    h = _groups("house")
    assert h["style"] == ["Deep House Organ Bass"]


def test_prompt_group_and_filter():
    g = _groups("techno", prompt="grainy")
    assert list(g)[0] == "prompt" and g["prompt"] == ["Grainy Thing"]
    assert _groups("house", query="organ") == {"style": ["Deep House Organ Bass"]}


def test_group_labels_and_tags():
    groups = ip.group_candidates(BASS, "bass", "techno")
    assert groups[0]["label"] == "Best for techno"
    assert "dark" in groups[0]["items"][0]["tags"]


def test_swap_loads_the_picked_sound(env):
    bridge = FakeBridge({1: "Dark Rumble Bass"})
    env(bridge)
    req = srv.InstrumentSwapRequest(style="house", role="bass", uri="u3")
    res = asyncio.run(srv.instruments_swap(req))
    assert res["ok"] and res["previous"] == "Dark Rumble Bass"
    assert res["result"]["name"] == "Deep House Organ Bass"
    assert bridge.deleted == [1] and bridge.loaded == [(1, "Deep House Organ Bass")]
    assert srv._read_palette_state()["1"] == {"style": "house", "device": "Deep House Organ Bass"}


def test_swap_rejects_sounds_outside_the_role(env):
    bridge = FakeBridge({1: "Dark Rumble Bass"})
    env(bridge)
    res = asyncio.run(srv.instruments_swap(srv.InstrumentSwapRequest(style="house", role="bass", uri="u1")))
    assert res["error"] == "not_in_role_pool" and bridge.deleted == []


def test_candidates_endpoint_reports_current(env):
    env(FakeBridge({1: "Dark Rumble Bass"}))
    res = asyncio.run(srv.instruments_candidates(srv.InstrumentCandidatesRequest(style="techno", role="bass")))
    assert res["ok"] and res["current"] == "Dark Rumble Bass" and res["track_index"] == 1
    assert res["groups"][0]["items"][0]["name"] == "Dark Rumble Bass"


def _fake_ai(reply, seen=None):
    async def call(system, user, temperature):
        if seen is not None:
            seen.append(user)
        return reply, {"ok": True}
    return call


def test_ai_top_picks_keeps_valid_unique_ids_and_skips_clashes():
    groups = ip.group_candidates(BASS, "bass", "techno")
    seen = []
    reply = {"picks": [{"id": 2, "reason": "warm"}, {"id": 2, "reason": "dup"}, {"id": 99}, {"id": 1, "reason": "dark"},
                       {"id": 3}, {"id": 4}]}
    picks, meta = asyncio.run(ip.ai_top_picks(_fake_ai(reply, seen), groups=groups, role="bass", style="techno",
                                              prompt=None, others={"drums": "909 Core Kit"}, current="Plain Bass"))
    assert [p["reason"] for p in picks[:2]] == ["warm", "dark"] and len(picks) == 3 and meta["ok"]
    assert "Fingered Bass" not in seen[0]  # clashes aren't offered to the AI
    assert "909 Core Kit" in seen[0] and "Plain Bass" in seen[0]


def test_ai_top_picks_reports_bad_replies():
    groups = ip.group_candidates(BASS, "bass", "techno")
    picks, meta = asyncio.run(ip.ai_top_picks(_fake_ai({"nope": 1}), groups=groups, role="bass", style="techno", prompt=None))
    assert picks == [] and meta["error"] == "bad_ai_response"


def test_suggest_endpoint_sends_other_tracks(env, monkeypatch):
    bridge = FakeBridge({0: "909 Core Kit", 1: "Dark Rumble Bass"})
    env(bridge)
    monkeypatch.setattr(srv, "PULSE_TRACK_PLAN", [{"track_index": 0, "role": "drums"}, {"track_index": 1, "role": "bass"}])
    monkeypatch.setattr(srv, "_ai_configured", lambda: True)
    seen = []
    monkeypatch.setattr(srv, "_call_ai_async", _fake_ai({"picks": [{"id": 1, "reason": "fits"}]}, seen))
    res = asyncio.run(srv.instruments_suggest(srv.InstrumentCandidatesRequest(style="house", role="bass")))
    assert res["ok"] and res["others"] == {"drums": "909 Core Kit"}
    assert res["picks"][0]["reason"] == "fits" and res["picks"][0]["uri"]
    assert "Dark Rumble Bass" in seen[0]  # the current bass is passed as context


def test_suggest_needs_an_ai_provider(env, monkeypatch):
    env(FakeBridge({1: None}))
    monkeypatch.setattr(srv, "_ai_configured", lambda: False)
    res = asyncio.run(srv.instruments_suggest(srv.InstrumentCandidatesRequest(style="house", role="bass")))
    assert res["error"] == "ai_not_configured"


def test_current_reports_each_track_and_who_picked_it(env, monkeypatch):
    bridge = FakeBridge({0: None, 1: None})
    env(bridge)
    monkeypatch.setattr(srv, "PULSE_TRACK_PLAN", [{"track_index": 0, "role": "drums"}, {"track_index": 1, "role": "bass"}])
    asyncio.run(srv.instruments_swap(srv.InstrumentSwapRequest(style="techno", role="bass", uri="u2")))
    rows = asyncio.run(srv.instruments_current())["results"]
    assert rows[0] == {"track_index": 0, "role": "drums", "action": "current", "name": None, "track_name": None, "picked_for": None}
    assert rows[1]["name"] == "Dark Rumble Bass" and rows[1]["picked_for"] == "techno"
    bridge.devices[1] = "My Own Bass"  # swapped by hand in Live
    assert asyncio.run(srv.instruments_current())["results"][1]["picked_for"] is None
