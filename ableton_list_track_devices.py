"""List device chains on each track via AbletonOSC.

Run:
  py ./ableton_list_track_devices.py
"""

from pythonosc.udp_client import SimpleUDPClient
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.dispatcher import Dispatcher
import threading
import time

HOST = "127.0.0.1"
SEND_PORT = 11000
RECV_PORT = 11001

client = SimpleUDPClient(HOST, SEND_PORT)


def handler(address, *args):
    if address.startswith("/live/track/get/"):
        print(f"{address}: {args}")
    else:
        print(f"{address}: {args}")


dispatcher = Dispatcher()
dispatcher.set_default_handler(handler)
server = ThreadingOSCUDPServer((HOST, RECV_PORT), dispatcher)
th = threading.Thread(target=server.serve_forever, daemon=True)
th.start()

time.sleep(0.3)


def send(addr, args=None, wait=0.15):
    if args is None:
        args = []
    client.send_message(addr, args)
    time.sleep(wait)


if __name__ == "__main__":
    # Ask for names once to get track count indirectly from user output.
    print("Requesting track names...")
    send("/live/song/get/track_names")

    # You have 8 tracks from your last query; scan 0..7
    print("\nScanning devices on tracks 0..7...")
    for t in range(8):
        send("/live/track/get/name", [t])
        send("/live/track/get/num_devices", [t])
        send("/live/track/get/devices/name", [t])

    print("\nDone. Look for anything like 'Drum Rack' / '909' / kit name on one of the tracks.")
    time.sleep(2)
