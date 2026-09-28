"""
Check PulseBridge against a running Live.

    python pulse_bridge/smoke_test.py            # read-only: handshake, capabilities, snapshot
    python pulse_bridge/smoke_test.py --write    # also adds a MIDI track with a test clip (undo with Ctrl+Z)
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pulse_bridge_client import PulseBridgeClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PULSE_BRIDGE_PORT", "9880")))
    args = ap.parse_args()

    events = []
    c = PulseBridgeClient(port=args.port)
    c.on_event(lambda e, d: events.append(e))
    c.start()
    if not c.wait_connected(5):
        sys.exit("Not connected: %s\nIs PulseBridge selected as a Control Surface in Live?" % c.last_error)
    deadline = time.time() + 10
    while c.capabilities is None and time.time() < deadline:
        time.sleep(0.05)

    t0 = time.perf_counter()
    c.request("ping")
    print("round trip: %.0f ms" % ((time.perf_counter() - t0) * 1000))
    print("hello:", json.dumps(c.hello))
    caps = dict(c.capabilities or {})
    devices = caps.pop("devices", {})
    print("capabilities:", json.dumps(caps, indent=2))
    print("instruments:", ", ".join(devices.get("instruments", [])))
    snap = c.request("get_snapshot")
    print("song: %.1f BPM, %d tracks, %d scenes, key=%s %s" % (
        snap["tempo"], snap["num_tracks"], snap["num_scenes"], snap.get("root_note"), snap.get("scale_name")))

    if args.write:
        res = c.request("create_midi_track", {"index": -1})
        ti = res["index"]
        c.request("set_track", {"track_index": ti, "name": "PulseBridge test"})
        c.request("load_device", {"track_index": ti, "device_name": caps["defaults"]["melodic"]})
        notes = [{"pitch": 48 + (i % 4) * 3, "start_time": i * 0.5, "duration": 0.4, "velocity": 100,
                  "probability": 0.75 if i % 2 else 1.0} for i in range(8)]
        w = c.request("write_clip", {"track_index": ti, "clip_slot_index": 0, "length": 4, "name": "smoke", "notes": notes})
        print("wrote clip:", w, "->", c.request("get_track", {"track_index": ti})["devices"])
        time.sleep(0.5)
        print("events seen:", sorted(set(events)))
    c.close()


if __name__ == "__main__":
    main()
