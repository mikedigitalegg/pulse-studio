"""
Load an instrument and play the clips.
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

# Track 0 is "1 MIDI" in Ableton (0-indexed)
TRACK_INDEX = 0

print("1. Loading instrument onto track 0...")
# Try loading a built-in Ableton instrument
# Common names: "Wavetable", "Analog", "Operator", "Simpler", "Drift"
send("/live/track/load_device", [TRACK_INDEX, "Wavetable"])
time.sleep(1)  # Give it time to load

print("\n2. Starting playback...")
send("/live/song/start_playing")

print("\n3. Firing clip in slot 0 (Simple Melody)...")
send("/live/clip_slot/fire", [TRACK_INDEX, 0])

print("\nDone! You should hear the melody playing.")
print("\nTo stop: run this in Python:")
print('  client.send_message("/live/song/stop_playing", [])')
