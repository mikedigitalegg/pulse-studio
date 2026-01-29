"""\
List parameters for the Basic 303 Bass device on track 0.

Assumptions:
- Track 0 has device 0 = 'Basic 303 Bass'

Run:
  py ./ableton_list_303_params.py

This prints parameter names + current/min/max + quantized flags.
Use this to identify which parameter indices correspond to cutoff/resonance/drive/etc.
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
DEVICE = 0

client = SimpleUDPClient(HOST, SEND_PORT)

state = {
    "names": None,
    "values": None,
    "mins": None,
    "maxs": None,
    "quant": None,
}


def handler(address, *args):
    if address == "/live/device/get/parameters/name":
        state["names"] = list(args[2:])
    elif address == "/live/device/get/parameters/value":
        state["values"] = list(args[2:])
    elif address == "/live/device/get/parameters/min":
        state["mins"] = list(args[2:])
    elif address == "/live/device/get/parameters/max":
        state["maxs"] = list(args[2:])
    elif address == "/live/device/get/parameters/is_quantized":
        state["quant"] = list(args[2:])


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
    send("/live/device/get/name", [TRACK, DEVICE])
    send("/live/device/get/class_name", [TRACK, DEVICE])
    send("/live/device/get/num_parameters", [TRACK, DEVICE])

    send("/live/device/get/parameters/name", [TRACK, DEVICE])
    send("/live/device/get/parameters/value", [TRACK, DEVICE])
    send("/live/device/get/parameters/min", [TRACK, DEVICE])
    send("/live/device/get/parameters/max", [TRACK, DEVICE])
    send("/live/device/get/parameters/is_quantized", [TRACK, DEVICE])

    deadline = time.time() + 3
    while time.time() < deadline:
        if all(state[k] is not None for k in state):
            break
        time.sleep(0.05)

    names = state["names"] or []
    values = state["values"] or []
    mins = state["mins"] or []
    maxs = state["maxs"] or []
    quant = state["quant"] or []

    n = min(len(names), len(values), len(mins), len(maxs), len(quant))

    print(f"Track {TRACK}, device {DEVICE} parameter list (count={n})")
    print("idx\tname\tvalue\tmin\tmax\tquantized")
    for i in range(n):
        print(f"{i}\t{names[i]}\t{values[i]}\t{mins[i]}\t{maxs[i]}\t{quant[i]}")
