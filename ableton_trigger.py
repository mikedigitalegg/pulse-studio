"""
Properly trigger clip playback.
"""

from pythonosc.udp_client import SimpleUDPClient
import time

client = SimpleUDPClient("127.0.0.1", 11000)

def send(address, args=None):
    if args is None:
        args = []
    print(f"Sending: {address} {args}")
    client.send_message(address, args)
    time.sleep(0.1)

TRACK = 0
CLIP_SLOT = 0

print("1. Stop any current playback...")
send("/live/song/stop_playing")
time.sleep(0.3)

print("\n2. Set track output to Master...")
# Try setting output routing to Master
send("/live/track/set/output_routing_type", [TRACK, 0])  # 0 is usually Master

print("\n3. Unmute track and set volume...")
send("/live/track/set/mute", [TRACK, 0])  # Unmute
send("/live/track/set/volume", [TRACK, 0.85])  # Set volume

print("\n4. Fire the clip directly (this should trigger it)...")
send("/live/clip/fire", [TRACK, CLIP_SLOT])

print("\n5. Also try clip_slot/fire...")
send("/live/clip_slot/fire", [TRACK, CLIP_SLOT])

print("\n6. Start global playback...")
send("/live/song/start_playing")

print("\n7. Fire scene 0 (triggers all clips in row 0)...")
send("/live/scene/fire", [0])

print("\nDone! Check if the clip shows a green play triangle.")
print("\nIf still no sound, in Ableton:")
print("  1. Click directly on the 'Simple Melody' clip")
print("  2. Check Audio To dropdown on track 1 - change to 'Master'")
