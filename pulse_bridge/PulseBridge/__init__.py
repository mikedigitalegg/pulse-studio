"""
PulseBridge - Ableton Live Remote Script for Pulse Studio.

Works in every Live edition (Lite, Intro, Standard, Suite) because it is a
plain control-surface script: no Max for Live required.

Enable it in Live: Settings -> Link, Tempo & MIDI -> Control Surface -> PulseBridge
(Input/Output can stay "None").

Protocol: newline-delimited JSON over TCP on 127.0.0.1:9880 (see README.md).
Keep this package Python 3.7 compatible (Live 11 ships 3.7, Live 12 ships 3.11).
"""
from __future__ import absolute_import, print_function, unicode_literals

import importlib

from . import server, commands, events, surface


def create_instance(c_instance):
    # Live caches imported modules, so re-selecting the Control Surface would otherwise
    # keep running old code after an update. Reload in dependency order each time.
    for mod in (server, commands, events, surface):
        importlib.reload(mod)
    return surface.PulseBridge(c_instance)
