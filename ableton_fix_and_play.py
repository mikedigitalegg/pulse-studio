"""
Diagnose and fix the track setup, then play.
"""

from pythonosc.udp_client import SimpleUDPClient
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.dispatcher import Dispatcher
import threading
import time

client = SimpleUDPClient("127.0.0.1", 11000)

def default_handler(address, *args):
    print(f"  Response: {address} -> {args}")

# Start listener
dispatcher = Dispatcher()
dispatcher.set_default_handler(default_handler)
server = ThreadingOSCUDPServer(("127.0.0.1", 11001), dispatcher)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
time.sleep(0.3)

def send(address, args=None):
    if args is None:
        args = []
    print(f"Sending: {address} {args}")
    client.send_message(address, args)
    time.sleep(0.2)

TRACK = 0

print("=" * 50)
print("DIAGNOSING TRACK 0")
print("=" * 50)

print("\n1. Check if clip exists in slot 0...")
send("/live/clip_slot/get/has_clip", [TRACK, 0])

print("\n2. Check devices on track 0...")
send("/live/track/get/num_devices", [TRACK])
send("/live/track/get/devices/name", [TRACK])

print("\n3. Check track output routing...")
send("/live/track/get/output_routing_type/available_output_routing_types", [TRACK])

time.sleep(1)

print("\n" + "=" * 50)
print("ATTEMPTING TO PLAY")
print("=" * 50)

print("\n4. Switch to Session View...")
send("/live/view/set/is_view_visible", ["Session", 1])

print("\n5. Fire clip on track 0, slot 0...")
send("/live/clip_slot/fire", [TRACK, 0])

print("\n6. Start song playback...")
send("/live/song/start_playing")

time.sleep(2)

print("\n" + "=" * 50)
print("If you still don't hear sound:")
print("  - Manually drag an instrument (e.g. 'Drift' or 'Analog') onto track 1 in Ableton")
print("  - Or double-click the clip to see if notes are there")
print("=" * 50)
