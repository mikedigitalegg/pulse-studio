"""
Query Ableton Live state via AbletonOSC.
Sets up a listener to receive responses from Ableton.
"""

from pythonosc.udp_client import SimpleUDPClient
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.dispatcher import Dispatcher
import threading
import time

ABLETON_IP = "127.0.0.1"
SEND_PORT = 11000
RECEIVE_PORT = 11001

client = SimpleUDPClient(ABLETON_IP, SEND_PORT)


def default_handler(address, *args):
    """Handle all incoming OSC messages."""
    print(f"Received: {address}")
    for i, arg in enumerate(args):
        print(f"  [{i}]: {arg}")
    print()


def start_server():
    """Start OSC server to receive responses."""
    dispatcher = Dispatcher()
    dispatcher.set_default_handler(default_handler)
    
    server = ThreadingOSCUDPServer((ABLETON_IP, RECEIVE_PORT), dispatcher)
    print(f"Listening for responses on port {RECEIVE_PORT}...")
    server.serve_forever()


if __name__ == "__main__":
    # Start listener in background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    
    time.sleep(0.5)  # Let server start
    
    print("Querying Ableton Live...")
    print("=" * 50)
    
    # Query song info
    print("\n1. Getting tempo...")
    client.send_message("/live/song/get/tempo", [])
    time.sleep(0.2)
    
    print("\n2. Getting track count...")
    client.send_message("/live/song/get/num_tracks", [])
    time.sleep(0.2)
    
    print("\n3. Getting track names...")
    client.send_message("/live/song/get/track_names", [])
    time.sleep(0.2)
    
    print("\n4. Checking if track 0 has a clip in slot 0...")
    client.send_message("/live/clip_slot/get/has_clip", [0, 0])
    time.sleep(0.2)
    
    print("\n5. Getting track 0 info...")
    client.send_message("/live/track/get/name", [0])
    time.sleep(0.2)
    
    print("\nWaiting for responses (3 seconds)...")
    time.sleep(3)
    print("\nDone.")
