from pythonosc.udp_client import SimpleUDPClient

client = SimpleUDPClient("127.0.0.1", 11000)

client.send_message("/live/song/start_playing", [])
# client.send_message("/live/song/stop_playing", [])
# client.send_message("/live/song/get/is_playing", [])

print("Sent")
