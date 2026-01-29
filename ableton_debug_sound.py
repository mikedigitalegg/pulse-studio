"""\
Debug why a playing Session clip has no sound.
- Confirms the clip exists
- Reads notes from the clip
- Reads track output meter level

Run:
  py ./ableton_debug_sound.py
"""

from pythonosc.udp_client import SimpleUDPClient
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.dispatcher import Dispatcher
import threading
import time

HOST = "127.0.0.1"
SEND_PORT = 11000
RECV_PORT = 11001

TRACK = 0
SLOT = 0

client = SimpleUDPClient(HOST, SEND_PORT)


def handler(address, *args):
    print(f"Received: {address}")
    for i, a in enumerate(args):
        print(f"  [{i}]: {a}")
    print()


dispatcher = Dispatcher()
dispatcher.set_default_handler(handler)
server = ThreadingOSCUDPServer((HOST, RECV_PORT), dispatcher)
th = threading.Thread(target=server.serve_forever, daemon=True)
th.start()

time.sleep(0.3)


def send(addr, args=None, wait=0.2):
    if args is None:
        args = []
    print(f"Sending: {addr} {args}")
    client.send_message(addr, args)
    time.sleep(wait)


if __name__ == "__main__":
    print("=" * 60)
    print(f"Debugging track={TRACK} slot={SLOT}")
    print("=" * 60)

    send("/live/clip_slot/get/has_clip", [TRACK, SLOT])

    # Notes: this will print a long list if notes exist.
    send("/live/clip/get/notes", [TRACK, SLOT])

    # If notes exist and the instrument is generating sound, this should be > -inf while playing.
    send("/live/track/get/output_meter_level", [TRACK])

    print("Waiting 2s for any late responses...")
    time.sleep(2)

    print("\nIf /live/clip/get/notes returned only (track, slot) and no pitch/start_time entries,")
    print("the clip has no MIDI notes and will play silently.")
