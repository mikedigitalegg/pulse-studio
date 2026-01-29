"""\
Create a simple classic-acid "full track" structure in Ableton Session View.

Assumptions (based on your current set):
- Track 0: '1-Basic 303 Bass'
- Track 1: '2-909 Core Kit'

This script:
- Sets tempo
- Writes multiple scene-aligned clips (intro/A/B/break/outro)
- Names scenes
- Optionally performs an automated play-through by firing scenes in sequence

Run:
  py ./ableton_full_track.py
"""

from pythonosc.udp_client import SimpleUDPClient
import time

HOST = "127.0.0.1"
SEND_PORT = 11000

client = SimpleUDPClient(HOST, SEND_PORT)


def send(address, args=None, wait=0.05):
    if args is None:
        args = []
    client.send_message(address, args)
    time.sleep(wait)


BASS_TRACK = 0
DRUM_TRACK = 1
VOX_TRACK = 2

BASS_DEVICE = 0
P_CUTOFF = 1
P_RESO = 2
P_DRIVE = 4

TEMPO = 140

# Use higher slot indexes to avoid clobbering earlier experiments.
S_INTRO = 10
S_A = 11
S_B = 12
S_BREAK = 13
S_OUTRO = 14
S_BUILD = 15
S_FILL = 16
S_DROP = 17


def ensure_scene_count(min_scenes: int):
    # Query num_scenes by requesting and then waiting a moment. We can't easily capture without a server,
    # so we just create scenes up to a safe minimum.
    # Ableton will append scenes at the end.
    for _ in range(min_scenes):
        send("/live/song/create_scene", [-1])


def set_scene_name(scene_idx: int, name: str):
    send("/live/scene/set/name", [scene_idx, name])


def delete_clip(track: int, slot: int):
    send("/live/clip_slot/delete_clip", [track, slot])


def create_clip(track: int, slot: int, length_beats: float):
    delete_clip(track, slot)
    send("/live/clip_slot/create_clip", [track, slot, float(length_beats)])
    time.sleep(0.08)


def add_note(track: int, slot: int, pitch: int, start: float, duration: float, velocity: float):
    send("/live/clip/add/notes", [track, slot, int(pitch), float(start), float(duration), float(velocity), 0])


def set_param(track: int, device: int, param: int, value: float):
    send("/live/device/set/parameter/value", [track, device, param, float(value)])


def ramp_param(track: int, device: int, param: int, start: float, end: float, steps: int, seconds: float):
    if steps <= 0:
        set_param(track, device, param, end)
        return
    dt = seconds / steps
    for i in range(steps + 1):
        v = start + (end - start) * (i / steps)
        set_param(track, device, param, v)
        time.sleep(dt)


def apply_section_params(scene_idx: int):
    if scene_idx == S_INTRO:
        set_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 18)
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 92)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 70)
    elif scene_idx == S_A:
        set_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 34)
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 96)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 78)
    elif scene_idx == S_B:
        set_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 42)
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 100)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 84)
    elif scene_idx == S_BREAK:
        set_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 16)
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 90)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 68)
    elif scene_idx == S_BUILD:
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 104)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 86)
        ramp_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 22, 92, steps=24, seconds=6.0)
    elif scene_idx == S_FILL:
        set_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 28)
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 98)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 82)
    elif scene_idx == S_DROP:
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 105)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 92)
        ramp_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 26, 70, steps=10, seconds=0.9)
    elif scene_idx == S_OUTRO:
        set_param(BASS_TRACK, BASS_DEVICE, P_CUTOFF, 20)
        set_param(BASS_TRACK, BASS_DEVICE, P_RESO, 94)
        set_param(BASS_TRACK, BASS_DEVICE, P_DRIVE, 72)


def create_303_pattern(track: int, slot: int, root: int, variant: str):
    create_clip(track, slot, 4.0)

    step = 0.25
    if variant == "A":
        intervals = [0, 2, 3, 7, 10, 7, 3, 2, 0, 2, 3, 7, 8, 7, 3, 2]
        accents = {0, 3, 8, 11}
        slides = {3, 7, 11, 14}
    else:
        intervals = [0, 0, 3, 7, 8, 7, 5, 3, 2, 3, 7, 10, 7, 3, 2, 0]
        accents = {2, 4, 10, 11, 15}
        slides = {4, 10, 12}

    for i, interval in enumerate(intervals):
        start = i * step
        vel = 118 if i in accents else 88
        dur = 0.35 if i in slides else 0.22
        add_note(track, slot, root + interval, start, dur, vel)

    send("/live/clip/set/name", [track, slot, f"303 {variant}"])


def create_909_intro(track: int, slot: int):
    create_clip(track, slot, 4.0)

    ch = 42
    oh = 46

    for i in range(8):
        t = i * 0.5
        v = 72 if i % 2 else 82
        add_note(track, slot, ch, t, 0.08, v)

    add_note(track, slot, oh, 3.5, 0.25, 80)
    send("/live/clip/set/name", [track, slot, "909 Intro Hats"])


def create_909_groove(track: int, slot: int, variant: str):
    create_clip(track, slot, 4.0)

    kick = 36
    snare = 38
    clap = 39
    ch = 42
    oh = 46

    for b in range(4):
        add_note(track, slot, kick, float(b), 0.20, 115)

    add_note(track, slot, snare, 1.0, 0.20, 110)
    add_note(track, slot, snare, 3.0, 0.20, 110)
    add_note(track, slot, clap, 1.0, 0.20, 90)
    add_note(track, slot, clap, 3.0, 0.20, 90)

    if variant == "B":
        add_note(track, slot, kick, 2.5, 0.20, 95)
        add_note(track, slot, kick, 2.75, 0.20, 85)

    for i in range(16):
        t = i * 0.25
        v = 84 if i % 2 == 0 else 70
        add_note(track, slot, ch, t, 0.08, v)

    if variant == "A":
        add_note(track, slot, oh, 1.75, 0.25, 80)
        add_note(track, slot, oh, 3.75, 0.25, 80)
    else:
        add_note(track, slot, oh, 1.5, 0.25, 78)
        add_note(track, slot, oh, 3.5, 0.25, 78)

    send("/live/clip/set/name", [track, slot, f"909 Groove {variant}"])


def create_vox_chops(track: int, slot: int, variant: str):
    create_clip(track, slot, 4.0)

    # Assumes the Vox track has a Simpler/Drum Rack loaded and mapped so that MIDI note 60 triggers a vocal chop.
    chop = 60

    if variant == "A":
        hits = [
            (1.0, 0.10, 105),
            (1.5, 0.10, 95),
            (2.75, 0.10, 105),
            (3.0, 0.10, 98),
            (3.25, 0.10, 110),
        ]
    elif variant == "BUILD":
        hits = []
        for i in range(8):
            hits.append((2.0 + i * 0.25, 0.08, 70 + i * 4))
    elif variant == "FILL":
        hits = []
        for i in range(8):
            hits.append((3.0 + i * 0.125, 0.06, 92))
    else:
        hits = [
            (0.75, 0.10, 100),
            (1.25, 0.10, 105),
            (2.0, 0.10, 95),
            (2.5, 0.10, 105),
            (3.5, 0.10, 110),
        ]

    for t, d, v in hits:
        add_note(track, slot, chop, t, d, v)

    send("/live/clip/set/name", [track, slot, f"Vox {variant}"])


def create_909_break(track: int, slot: int):
    create_clip(track, slot, 4.0)

    kick = 36
    clap = 39
    ch = 42

    add_note(track, slot, kick, 0.0, 0.20, 115)
    add_note(track, slot, kick, 2.0, 0.20, 110)

    add_note(track, slot, clap, 3.0, 0.20, 95)

    for i in range(4):
        add_note(track, slot, ch, i * 1.0, 0.08, 70)

    send("/live/clip/set/name", [track, slot, "909 Break"])


def create_909_fill(track: int, slot: int):
    create_clip(track, slot, 4.0)

    kick = 36
    snare = 38
    clap = 39
    ch = 42
    oh = 46

    add_note(track, slot, kick, 0.0, 0.20, 115)
    add_note(track, slot, kick, 1.0, 0.20, 115)
    add_note(track, slot, kick, 2.0, 0.20, 115)
    add_note(track, slot, kick, 3.0, 0.20, 115)

    add_note(track, slot, snare, 1.0, 0.20, 110)
    add_note(track, slot, snare, 3.0, 0.20, 110)
    add_note(track, slot, clap, 1.0, 0.20, 90)
    add_note(track, slot, clap, 3.0, 0.20, 90)

    for i in range(16):
        t = i * 0.25
        v = 86 if i % 2 == 0 else 72
        add_note(track, slot, ch, t, 0.08, v)

    for i in range(8):
        t = 2.0 + i * 0.25
        add_note(track, slot, snare, t, 0.08, 92)

    add_note(track, slot, oh, 3.75, 0.25, 84)
    send("/live/clip/set/name", [track, slot, "909 Fill"])


def create_909_build(track: int, slot: int):
    create_clip(track, slot, 4.0)

    kick = 36
    clap = 39
    ch = 42

    add_note(track, slot, kick, 0.0, 0.20, 115)
    add_note(track, slot, kick, 2.0, 0.20, 115)
    add_note(track, slot, clap, 3.0, 0.20, 95)

    for i in range(16):
        t = i * 0.25
        v = 60 + int(i * 2)
        add_note(track, slot, ch, t, 0.08, v)

    send("/live/clip/set/name", [track, slot, "909 Build"])


def create_303_sparse(track: int, slot: int, root: int):
    create_clip(track, slot, 4.0)

    step = 0.25
    intervals = [0, None, 3, None, 7, None, 10, None, 0, None, 3, None, 7, None, 8, None]
    accents = {0, 4, 8, 12}

    for i, interval in enumerate(intervals):
        if interval is None:
            continue
        start = i * step
        vel = 118 if i in accents else 86
        add_note(track, slot, root + interval, start, 0.22, vel)

    send("/live/clip/set/name", [track, slot, "303 Sparse"])


def write_scenes():
    send("/live/song/set/tempo", [float(TEMPO)])

    # Ensure there are enough scenes to safely name the ones we use.
    ensure_scene_count(S_DROP + 1)

    set_scene_name(S_INTRO, "Intro")
    set_scene_name(S_A, "A")
    set_scene_name(S_B, "B")
    set_scene_name(S_BREAK, "Break")
    set_scene_name(S_OUTRO, "Outro")
    set_scene_name(S_BUILD, "Build")
    set_scene_name(S_FILL, "Fill")
    set_scene_name(S_DROP, "Drop")

    # Intro: hats only, no bass.
    delete_clip(BASS_TRACK, S_INTRO)
    create_909_intro(DRUM_TRACK, S_INTRO)
    delete_clip(VOX_TRACK, S_INTRO)

    # A: classic acid + groove
    create_303_pattern(BASS_TRACK, S_A, root=45, variant="A")
    create_909_groove(DRUM_TRACK, S_A, variant="A")
    create_vox_chops(VOX_TRACK, S_A, "A")

    # B: variation
    create_303_pattern(BASS_TRACK, S_B, root=45, variant="B")
    create_909_groove(DRUM_TRACK, S_B, variant="B")
    create_vox_chops(VOX_TRACK, S_B, "B")

    # Break: drums down, bass muted
    delete_clip(BASS_TRACK, S_BREAK)
    create_909_break(DRUM_TRACK, S_BREAK)
    delete_clip(VOX_TRACK, S_BREAK)

    # Outro: back to hats only
    delete_clip(BASS_TRACK, S_OUTRO)
    create_909_intro(DRUM_TRACK, S_OUTRO)
    delete_clip(VOX_TRACK, S_OUTRO)

    # Build: sparse bass + build-up hats
    create_303_sparse(BASS_TRACK, S_BUILD, root=45)
    create_909_build(DRUM_TRACK, S_BUILD)
    create_vox_chops(VOX_TRACK, S_BUILD, "BUILD")

    # Fill: no bass, drum fill
    delete_clip(BASS_TRACK, S_FILL)
    create_909_fill(DRUM_TRACK, S_FILL)
    create_vox_chops(VOX_TRACK, S_FILL, "FILL")

    # Drop: return to A
    create_303_pattern(BASS_TRACK, S_DROP, root=45, variant="A")
    create_909_groove(DRUM_TRACK, S_DROP, variant="A")
    create_vox_chops(VOX_TRACK, S_DROP, "A")


def bars_to_seconds(bars: int) -> float:
    beats = bars * 4
    return (60.0 / TEMPO) * beats


def perform_live(arrangement):
    send("/live/song/start_playing")
    time.sleep(0.1)

    for scene_idx, bars in arrangement:
        apply_section_params(scene_idx)
        send("/live/scene/fire", [scene_idx])
        time.sleep(bars_to_seconds(bars))


if __name__ == "__main__":
    write_scenes()

    # A simple full-track structure.
    arrangement = [
        (S_INTRO, 4),
        (S_A, 16),
        (S_BUILD, 4),
        (S_FILL, 1),
        (S_DROP, 16),
        (S_B, 16),
        (S_BREAK, 4),
        (S_FILL, 1),
        (S_DROP, 8),
        (S_OUTRO, 4),
    ]

    # Set to False if you only want the clips/scenes created (no autoplay).
    AUTO_PLAY = True

    if AUTO_PLAY:
        perform_live(arrangement)
