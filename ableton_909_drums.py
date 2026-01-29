from pythonosc.udp_client import SimpleUDPClient
import time

client = SimpleUDPClient("127.0.0.1", 11000)


def send(address, args=None, wait=0.05):
    if args is None:
        args = []
    print(f"Sending: {address} {args}")
    client.send_message(address, args)
    time.sleep(wait)


def create_909_pattern(track_index, clip_slot_index, length_beats=4.0):
    send("/live/clip_slot/delete_clip", [track_index, clip_slot_index])
    send("/live/clip_slot/create_clip", [track_index, clip_slot_index, float(length_beats)])
    time.sleep(0.1)

    kick = 36
    snare = 38
    clap = 39
    ch = 42
    oh = 46

    for b in range(4):
        send("/live/clip/add/notes", [track_index, clip_slot_index, kick, float(b), 0.20, 115.0, 0])

    send("/live/clip/add/notes", [track_index, clip_slot_index, snare, 1.0, 0.20, 110.0, 0])
    send("/live/clip/add/notes", [track_index, clip_slot_index, snare, 3.0, 0.20, 110.0, 0])

    send("/live/clip/add/notes", [track_index, clip_slot_index, clap, 1.0, 0.20, 90.0, 0])
    send("/live/clip/add/notes", [track_index, clip_slot_index, clap, 3.0, 0.20, 90.0, 0])

    for i in range(16):
        t = i * 0.25
        v = 84.0 if i % 2 == 0 else 70.0
        send("/live/clip/add/notes", [track_index, clip_slot_index, ch, t, 0.08, v, 0])

    send("/live/clip/add/notes", [track_index, clip_slot_index, oh, 1.75, 0.25, 80.0, 0])
    send("/live/clip/add/notes", [track_index, clip_slot_index, oh, 3.75, 0.25, 80.0, 0])

    send("/live/clip/set/name", [track_index, clip_slot_index, "909 Groove"])


if __name__ == "__main__":
    DRUM_TRACK_INDEX = 1
    CLIP_SLOT_INDEX = 0

    create_909_pattern(DRUM_TRACK_INDEX, CLIP_SLOT_INDEX)
    print("Done. Fire the clip in Ableton Session View.")
