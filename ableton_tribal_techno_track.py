from ableton_track_creator import AbletonController
import time


def create_hard_tribal_drums(controller, track_index=1, clip_slot=0, length_beats=8.0, variation="A"):
    controller.create_clip(track_index, clip_slot, length_beats)
    time.sleep(0.1)

    kick = 36
    clap = 39
    ch = 42
    oh = 46
    rim = 37

    tom_low = 41
    tom_mid = 45
    tom_high = 50

    for b in range(int(length_beats)):
        controller.add_note(track_index, clip_slot, kick, float(b), 0.20, 118)

    if variation == "B":
        extra_kicks = [2.75, 6.75]
        for t in extra_kicks:
            if t < length_beats:
                controller.add_note(track_index, clip_slot, kick, t, 0.18, 105)
    elif variation == "C":
        extra_kicks = [3.75, 7.75]
        for t in extra_kicks:
            if t < length_beats:
                controller.add_note(track_index, clip_slot, kick, t, 0.18, 104)

    for bar in range(int(length_beats / 4)):
        controller.add_note(track_index, clip_slot, clap, bar * 4 + 1.0, 0.18, 104)
        controller.add_note(track_index, clip_slot, clap, bar * 4 + 3.0, 0.18, 104)

    steps = int(length_beats / 0.25)
    for i in range(steps):
        t = i * 0.25
        if variation == "B":
            v = 88 if i % 4 == 0 else (74 if i % 2 == 0 else 66)
        elif variation == "C":
            v = 84 if i % 3 == 0 else 70
        else:
            v = 86 if i % 2 == 0 else 72
        controller.add_note(track_index, clip_slot, ch, t, 0.08, v)

    for bar in range(int(length_beats / 4)):
        if variation == "B":
            controller.add_note(track_index, clip_slot, oh, bar * 4 + 0.5, 0.20, 78)
            controller.add_note(track_index, clip_slot, oh, bar * 4 + 2.5, 0.20, 78)
        else:
            controller.add_note(track_index, clip_slot, oh, bar * 4 + 1.5, 0.22, 82)
            controller.add_note(track_index, clip_slot, oh, bar * 4 + 3.5, 0.22, 82)

    if variation == "B":
        rim_hits = [0.75, 1.75, 2.75, 4.75, 5.75, 6.75]
    elif variation == "C":
        rim_hits = [1.25, 2.75, 4.25, 6.75]
    else:
        rim_hits = [0.75, 2.75, 4.75, 6.75]
    for t in rim_hits:
        if t < length_beats:
            controller.add_note(track_index, clip_slot, rim, t, 0.10, 78)

    if variation == "B":
        tom_pattern = [
            (tom_low, 0.50, 0.20, 100),
            (tom_mid, 0.75, 0.20, 95),
            (tom_high, 1.25, 0.20, 98),
            (tom_mid, 1.75, 0.20, 92),
            (tom_low, 2.25, 0.20, 102),
            (tom_mid, 2.75, 0.20, 94),
            (tom_high, 3.25, 0.20, 96),
            (tom_mid, 3.75, 0.20, 90),
            (tom_high, 4.25, 0.20, 98),
            (tom_mid, 4.75, 0.20, 95),
            (tom_low, 5.25, 0.20, 102),
            (tom_mid, 5.75, 0.20, 90),
            (tom_high, 6.25, 0.20, 96),
            (tom_low, 6.75, 0.20, 104),
            (tom_mid, 7.25, 0.20, 92),
            (tom_high, 7.50, 0.20, 94),
            (tom_mid, 7.75, 0.20, 88),
        ]
    elif variation == "C":
        tom_pattern = [
            (tom_low, 0.75, 0.20, 104),
            (tom_mid, 1.75, 0.20, 96),
            (tom_low, 2.75, 0.20, 102),
            (tom_high, 3.25, 0.20, 98),
            (tom_mid, 3.50, 0.20, 92),
            (tom_high, 3.75, 0.20, 90),
            (tom_high, 4.75, 0.20, 98),
            (tom_mid, 5.75, 0.20, 95),
            (tom_low, 6.75, 0.20, 104),
            (tom_mid, 7.25, 0.20, 92),
            (tom_high, 7.50, 0.20, 94),
            (tom_mid, 7.75, 0.20, 90),
        ]
    else:
        tom_pattern = [
            (tom_low, 0.75, 0.20, 102),
            (tom_mid, 1.25, 0.20, 96),
            (tom_high, 1.75, 0.20, 98),
            (tom_mid, 2.50, 0.20, 94),
            (tom_low, 2.75, 0.20, 100),
            (tom_high, 3.25, 0.20, 96),
            (tom_mid, 3.50, 0.20, 92),
            (tom_high, 4.25, 0.20, 98),
            (tom_mid, 4.75, 0.20, 95),
            (tom_low, 5.25, 0.20, 102),
            (tom_mid, 5.50, 0.20, 90),
            (tom_high, 6.25, 0.20, 96),
            (tom_low, 6.75, 0.20, 104),
            (tom_mid, 7.25, 0.20, 92),
            (tom_high, 7.50, 0.20, 90),
        ]

    for pitch, start, dur, vel in tom_pattern:
        if start < length_beats:
            controller.add_note(track_index, clip_slot, pitch, start, dur, vel)

    controller.set_clip_name(track_index, clip_slot, f"Hard Tribal Drums {variation}")


def create_hard_tribal_bass(controller, track_index=0, clip_slot=0, length_beats=8.0, root=43, variation="A"):
    controller.create_clip(track_index, clip_slot, length_beats)
    time.sleep(0.1)

    if variation == "B":
        notes = [
            (root, 0.5, 0.22, 96),
            (root, 1.5, 0.22, 96),
            (root + 7, 2.5, 0.20, 88),
            (root, 3.5, 0.22, 96),
            (root, 4.5, 0.22, 96),
            (root, 5.5, 0.22, 96),
            (root + 7, 6.5, 0.20, 88),
            (root, 7.5, 0.22, 96),
        ]
    elif variation == "C":
        notes = [
            (root, 0.5, 0.18, 92),
            (root, 1.0, 0.18, 90),
            (root, 1.5, 0.18, 92),
            (root, 3.5, 0.22, 96),
            (root, 4.5, 0.18, 92),
            (root, 5.0, 0.18, 90),
            (root, 5.5, 0.18, 92),
            (root, 7.5, 0.22, 96),
        ]
    else:
        notes = [
            (root, 0.5, 0.22, 96),
            (root, 1.5, 0.22, 96),
            (root, 2.5, 0.22, 96),
            (root, 3.5, 0.22, 96),
            (root, 4.5, 0.22, 96),
            (root, 5.5, 0.22, 96),
            (root, 6.5, 0.22, 96),
            (root, 7.5, 0.22, 96),
        ]

    for pitch, t, dur, vel in notes:
        if t < length_beats:
            controller.add_note(track_index, clip_slot, pitch, t, dur, vel)

    controller.set_clip_name(track_index, clip_slot, f"Tribal Bass {variation}")


def create_hard_tribal_stab(controller, track_index=2, clip_slot=0, length_beats=8.0, root=57, variation="A"):
    controller.create_clip(track_index, clip_slot, length_beats)
    time.sleep(0.1)

    if variation == "B":
        notes = [
            (root, 0.75, 0.28, 82),
            (root, 2.75, 0.28, 82),
            (root, 4.75, 0.28, 82),
            (root, 6.75, 0.28, 82),
        ]
    elif variation == "C":
        notes = [
            (root, 1.0, 0.30, 84),
            (root + 12, 3.0, 0.22, 76),
            (root, 5.0, 0.30, 84),
            (root + 12, 7.0, 0.22, 76),
        ]
    else:
        notes = [
            (root, 1.0, 0.30, 86),
            (root, 3.0, 0.30, 86),
            (root, 5.0, 0.30, 86),
            (root, 7.0, 0.30, 86),
        ]

    for pitch, t, dur, vel in notes:
        if t < length_beats:
            controller.add_note(track_index, clip_slot, pitch, t, dur, vel)

    controller.set_clip_name(track_index, clip_slot, f"Tribal Stab {variation}")


def create_hard_tribal_techno_track(bpm=142):
    ctrl = AbletonController()

    ctrl.set_tempo(bpm)
    time.sleep(0.1)

    bass_track_idx = 0
    drum_track_idx = 1
    stab_track_idx = 2

    ctrl.set_track_name(bass_track_idx, "Tribal Bass")
    ctrl.set_track_name(drum_track_idx, "Hard Tribal Kit")
    ctrl.set_track_name(stab_track_idx, "Tribal Stab")

    # Best-effort device loading (depends on your Ableton browser/device names)
    ctrl.load_device(bass_track_idx, "Operator")
    ctrl.load_device(drum_track_idx, "Drum Rack")
    ctrl.load_device(stab_track_idx, "Wavetable")

    variations = [("A", 0), ("B", 1), ("C", 2)]
    for variation, slot in variations:
        create_hard_tribal_bass(ctrl, bass_track_idx, slot, 8.0, root=43, variation=variation)
        create_hard_tribal_drums(ctrl, drum_track_idx, slot, 8.0, variation=variation)
        create_hard_tribal_stab(ctrl, stab_track_idx, slot, 8.0, root=57, variation=variation)


if __name__ == "__main__":
    create_hard_tribal_techno_track(bpm=142)
    print("Done. Fire the clips in Ableton Session View (slots 0-2 on each track).")
