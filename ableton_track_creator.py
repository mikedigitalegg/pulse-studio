"""
AbletonOSC Track Creator
Demonstrates creating tracks, clips, and adding MIDI notes via OSC.

Requires:
- Ableton Live with AbletonOSC installed and running
- python-osc: pip install python-osc
"""

from pythonosc.udp_client import SimpleUDPClient
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.dispatcher import Dispatcher
import time
import threading

# OSC Configuration
ABLETON_IP = "127.0.0.1"
SEND_PORT = 11000  # Port to send to Ableton
RECEIVE_PORT = 11001  # Port to receive responses from Ableton

client = SimpleUDPClient(ABLETON_IP, SEND_PORT)


class AbletonController:
    def __init__(self):
        self.client = SimpleUDPClient(ABLETON_IP, SEND_PORT)
        self.last_response = None
        self.response_event = threading.Event()
        
    def send(self, address, args=None):
        """Send an OSC message to Ableton."""
        if args is None:
            args = []
        print(f"Sending: {address} {args}")
        self.client.send_message(address, args)
        time.sleep(0.01)  # Small delay to avoid overwhelming Ableton
    
    # ==================== Song Control ====================
    
    def play(self):
        """Start playback."""
        self.send("/live/song/start_playing")
    
    def stop(self):
        """Stop playback."""
        self.send("/live/song/stop_playing")
    
    def set_tempo(self, bpm):
        """Set the song tempo."""
        self.send("/live/song/set/tempo", [float(bpm)])
    
    # ==================== Track Management ====================
    
    def create_midi_track(self, index=-1):
        """
        Create a new MIDI track.
        index: Position to insert (-1 = end)
        """
        self.send("/live/song/create_midi_track", [index])
    
    def create_audio_track(self, index=-1):
        """
        Create a new audio track.
        index: Position to insert (-1 = end)
        """
        self.send("/live/song/create_audio_track", [index])
    
    def set_track_name(self, track_index, name):
        """Set the name of a track."""
        self.send("/live/track/set/name", [track_index, name])
    
    def set_track_volume(self, track_index, volume):
        """Set track volume (0.0 to 1.0)."""
        self.send("/live/track/set/volume", [track_index, float(volume)])
    
    def set_track_pan(self, track_index, pan):
        """Set track pan (-1.0 to 1.0)."""
        self.send("/live/track/set/panning", [track_index, float(pan)])
    
    def arm_track(self, track_index, armed=True):
        """Arm/disarm a track for recording."""
        self.send("/live/track/set/arm", [track_index, int(armed)])
    
    def mute_track(self, track_index, muted=True):
        """Mute/unmute a track."""
        self.send("/live/track/set/mute", [track_index, int(muted)])
    
    def solo_track(self, track_index, solo=True):
        """Solo/unsolo a track."""
        self.send("/live/track/set/solo", [track_index, int(solo)])
    
    # ==================== Clip Management ====================
    
    def create_clip(self, track_index, clip_slot_index, length_beats=4.0):
        """
        Create a new MIDI clip in a clip slot.
        length_beats: Length of the clip in beats
        """
        # Ensure we overwrite any existing clip created by earlier runs.
        # AbletonOSC accepts delete even if the slot is empty.
        self.send("/live/clip_slot/delete_clip", [track_index, clip_slot_index])
        self.send("/live/clip_slot/create_clip", [track_index, clip_slot_index, float(length_beats)])
    
    def set_clip_name(self, track_index, clip_slot_index, name):
        """Set the name of a clip."""
        self.send("/live/clip/set/name", [track_index, clip_slot_index, name])
    
    def fire_clip(self, track_index, clip_slot_index):
        """Fire (play) a clip."""
        self.send("/live/clip_slot/fire", [track_index, clip_slot_index])
    
    def stop_clip(self, track_index, clip_slot_index):
        """Stop a clip."""
        self.send("/live/clip_slot/stop", [track_index, clip_slot_index])
    
    def delete_clip(self, track_index, clip_slot_index):
        """Delete a clip."""
        self.send("/live/clip_slot/delete_clip", [track_index, clip_slot_index])
    
    # ==================== Scene Management ====================

    def fire_scene(self, scene_index):
        """Fire (play) a scene."""
        self.send("/live/scene/fire", [int(scene_index)])

    # ==================== MIDI Note Operations ====================
    
    def add_note(self, track_index, clip_slot_index, pitch, start_time, duration, velocity=100, mute=False):
        """
        Add a single MIDI note to a clip.
        pitch: MIDI note number (0-127, 60 = Middle C)
        start_time: Start position in beats
        duration: Note length in beats
        velocity: Note velocity (0-127)
        mute: Whether the note is muted
        """
        self.send("/live/clip/add/notes", [
            track_index, 
            clip_slot_index,
            pitch,
            float(start_time),
            float(duration),
            float(velocity),
            int(mute)
        ])
    
    def add_notes(self, track_index, clip_slot_index, notes):
        """
        Add multiple MIDI notes to a clip.
        notes: List of tuples (pitch, start_time, duration, velocity)
        """
        for note in notes:
            pitch, start_time, duration, velocity = note
            self.add_note(track_index, clip_slot_index, pitch, start_time, duration, velocity)
    
    def remove_notes(self, track_index, clip_slot_index, start_time=0.0, end_time=4.0, pitch_start=0, pitch_end=127):
        """Remove notes from a clip within a range."""
        self.send("/live/clip/remove/notes", [
            track_index, 
            clip_slot_index,
            float(start_time),
            float(end_time - start_time),
            pitch_start,
            pitch_end - pitch_start
        ])
    
    # ==================== Device Management ====================
    
    def load_device(self, track_index, device_name):
        """
        Load a device (instrument/effect) onto a track.
        device_name: Name of the device (e.g., "Wavetable", "Reverb")
        """
        self.send("/live/track/load_device", [track_index, device_name])


# ==================== Example Usage ====================

def create_simple_beat(controller, track_index=0, clip_slot=0):
    """Create a simple 4-beat drum pattern."""
    # Create a 4-beat clip
    controller.create_clip(track_index, clip_slot, 4.0)
    time.sleep(0.1)
    
    # Kick drum pattern (MIDI note 36)
    controller.add_note(track_index, clip_slot, 36, 0.0, 0.5, 100)
    controller.add_note(track_index, clip_slot, 36, 1.0, 0.5, 100)
    controller.add_note(track_index, clip_slot, 36, 2.0, 0.5, 100)
    controller.add_note(track_index, clip_slot, 36, 3.0, 0.5, 100)
    
    # Snare on 2 and 4 (MIDI note 38)
    controller.add_note(track_index, clip_slot, 38, 1.0, 0.5, 100)
    controller.add_note(track_index, clip_slot, 38, 3.0, 0.5, 100)
    
    # Hi-hat pattern (MIDI note 42)
    for i in range(8):
        controller.add_note(track_index, clip_slot, 42, i * 0.5, 0.25, 80)
    
    controller.set_clip_name(track_index, clip_slot, "Basic Beat")


def create_melody(controller, track_index=0, clip_slot=0):
    """Create a simple melody."""
    # Create an 8-beat clip
    controller.create_clip(track_index, clip_slot, 8.0)
    time.sleep(0.1)
    
    # C major scale melody
    notes = [
        (60, 0.0, 0.5, 100),   # C
        (62, 0.5, 0.5, 90),    # D
        (64, 1.0, 1.0, 100),   # E
        (65, 2.0, 0.5, 85),    # F
        (67, 2.5, 0.5, 90),    # G
        (69, 3.0, 1.0, 100),   # A
        (67, 4.0, 0.5, 85),    # G
        (65, 4.5, 0.5, 80),    # F
        (64, 5.0, 1.0, 100),   # E
        (62, 6.0, 0.5, 85),    # D
        (60, 6.5, 1.5, 100),   # C
    ]
    
    controller.add_notes(track_index, clip_slot, notes)
    controller.set_clip_name(track_index, clip_slot, "Simple Melody")


def create_chord_progression(controller, track_index=0, clip_slot=0):
    """Create a simple chord progression (C - Am - F - G)."""
    controller.create_clip(track_index, clip_slot, 16.0)
    time.sleep(0.1)
    
    # C major chord (beats 0-4)
    controller.add_note(track_index, clip_slot, 48, 0.0, 4.0, 80)  # C
    controller.add_note(track_index, clip_slot, 52, 0.0, 4.0, 75)  # E
    controller.add_note(track_index, clip_slot, 55, 0.0, 4.0, 75)  # G
    
    # A minor chord (beats 4-8)
    controller.add_note(track_index, clip_slot, 45, 4.0, 4.0, 80)  # A
    controller.add_note(track_index, clip_slot, 48, 4.0, 4.0, 75)  # C
    controller.add_note(track_index, clip_slot, 52, 4.0, 4.0, 75)  # E
    
    # F major chord (beats 8-12)
    controller.add_note(track_index, clip_slot, 41, 8.0, 4.0, 80)  # F
    controller.add_note(track_index, clip_slot, 45, 8.0, 4.0, 75)  # A
    controller.add_note(track_index, clip_slot, 48, 8.0, 4.0, 75)  # C
    
    # G major chord (beats 12-16)
    controller.add_note(track_index, clip_slot, 43, 12.0, 4.0, 80)  # G
    controller.add_note(track_index, clip_slot, 47, 12.0, 4.0, 75)  # B
    controller.add_note(track_index, clip_slot, 50, 12.0, 4.0, 75)  # D
    
    controller.set_clip_name(track_index, clip_slot, "Chord Progression")


def create_acid_303_bassline(controller, track_index=0, clip_slot=0, root=45):
    controller.create_clip(track_index, clip_slot, 4.0)
    time.sleep(0.1)

    step = 0.25
    pattern = [
        (0, 0.25, 0.20, 115),
        (2, 0.25, 0.20, 85),
        (3, 0.25, 0.20, 95),
        (7, 0.25, 0.20, 110),
        (10, 0.25, 0.20, 80),
        (7, 0.25, 0.20, 105),
        (3, 0.25, 0.20, 90),
        (2, 0.25, 0.20, 85),
        (0, 0.25, 0.20, 120),
        (2, 0.25, 0.20, 85),
        (3, 0.25, 0.20, 95),
        (7, 0.25, 0.20, 112),
        (8, 0.25, 0.20, 90),
        (7, 0.25, 0.20, 105),
        (3, 0.25, 0.20, 92),
        (2, 0.25, 0.20, 85),
    ]

    slide_steps = {3, 7, 11, 14}
    for i, (interval, dur, gate, vel) in enumerate(pattern):
        start = i * step
        duration = dur
        if i in slide_steps:
            duration = 0.35
        controller.add_note(track_index, clip_slot, root + interval, start, duration, vel)

    controller.set_clip_name(track_index, clip_slot, "303 Acid")


def create_acid_drums(controller, track_index=0, clip_slot=0):
    controller.create_clip(track_index, clip_slot, 4.0)
    time.sleep(0.1)

    kick = 36
    clap = 39
    hat = 42
    ohat = 46

    for b in range(4):
        controller.add_note(track_index, clip_slot, kick, float(b), 0.25, 110)

    controller.add_note(track_index, clip_slot, clap, 1.0, 0.25, 100)
    controller.add_note(track_index, clip_slot, clap, 3.0, 0.25, 100)

    for i in range(8):
        t = i * 0.5
        v = 82 if i % 2 == 0 else 72
        controller.add_note(track_index, clip_slot, hat, t, 0.10, v)

    controller.add_note(track_index, clip_slot, ohat, 1.5, 0.25, 85)
    controller.add_note(track_index, clip_slot, ohat, 3.5, 0.25, 85)

    controller.set_clip_name(track_index, clip_slot, "Acid Drums")


def create_909_groove(controller, track_index=0, clip_slot=0):
    controller.create_clip(track_index, clip_slot, 4.0)
    time.sleep(0.1)

    kick = 36
    snare = 38
    clap = 39
    ch = 42
    oh = 46

    for b in range(4):
        controller.add_note(track_index, clip_slot, kick, float(b), 0.20, 115)

    controller.add_note(track_index, clip_slot, snare, 1.0, 0.20, 110)
    controller.add_note(track_index, clip_slot, snare, 3.0, 0.20, 110)
    controller.add_note(track_index, clip_slot, clap, 1.0, 0.20, 90)
    controller.add_note(track_index, clip_slot, clap, 3.0, 0.20, 90)

    for i in range(16):
        t = i * 0.25
        v = 84 if i % 2 == 0 else 70
        controller.add_note(track_index, clip_slot, ch, t, 0.08, v)

    controller.add_note(track_index, clip_slot, oh, 1.75, 0.25, 80)
    controller.add_note(track_index, clip_slot, oh, 3.75, 0.25, 80)

    controller.set_clip_name(track_index, clip_slot, "909 Groove")


if __name__ == "__main__":
    print("AbletonOSC Track Creator")
    print("=" * 40)
    
    ctrl = AbletonController()
    
    print("\n1. Setting tempo to 140 BPM...")
    ctrl.set_tempo(140)

    bass_track_idx = 0
    drum_track_idx = 1

    print(f"\n2. Creating classic 303 acid variation on track {bass_track_idx}, slot 4...")
    create_acid_303_bassline(ctrl, bass_track_idx, 4)

    print(f"\n3. Creating classic 909 groove on track {drum_track_idx}, slot 1...")
    create_909_groove(ctrl, drum_track_idx, 1)
    
    print("\nDone! Check Ableton Live for the new clips.")
    print("\nTo play, fire clips in Ableton Session View:")
    print("  - 303 Acid (slot 4 on track 1-Basic 303 Bass)")
    print("  - 909 Groove (slot 1 on track 2-909 Core Kit)")
