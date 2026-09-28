from __future__ import absolute_import, print_function, unicode_literals

import os
import traceback

from _Framework.ControlSurface import ControlSurface

from .commands import Commands, CommandError, BRIDGE_VERSION
from .events import EventHub
from .server import BridgeServer

HOST = "127.0.0.1"
DEFAULT_PORT = 9880
MAX_MESSAGES_PER_TICK = 500
READ_ONLY = frozenset((
    "ping", "hello", "get_capabilities", "get_song", "get_snapshot", "get_track", "has_clip",
    "find_free_slot", "get_notes", "get_device_params", "get_session_info", "get_browser_tree",
    "get_browser_items_at_path", "subscribe", "unsubscribe", "set_meters", "get_drum_pads",
    "get_track_meter",
))


class PulseBridge(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._commands = Commands(self)
        self._server = None
        self._events = None
        port = int(os.environ.get("PULSE_BRIDGE_PORT", DEFAULT_PORT))
        try:
            self._server = BridgeServer(HOST, port, log=self.log_message)
            self._server.start()
        except Exception as e:
            self._server = None
            self.log_message("PulseBridge: could not listen on port %d: %s" % (port, e))
            self.show_message("PulseBridge: port %d is busy (%s)" % (port, e))
            return
        self._events = EventHub(self.song(), self._emit, log=self.log_message)
        self._events.start()
        self.log_message("PulseBridge %s listening on %s:%d" % (BRIDGE_VERSION, HOST, port))
        self.show_message("PulseBridge %s ready on port %d" % (BRIDGE_VERSION, port))

    def disconnect(self):
        if self._events is not None:
            self._events.stop()
        if self._server is not None:
            self._server.stop()
        ControlSurface.disconnect(self)

    # Live calls this on the main thread roughly every 100 ms.
    def update_display(self):
        ControlSurface.update_display(self)
        if self._server is None:
            return
        self._drain()
        try:
            if self._server.has_subscribers():
                self._events.tick()
            if self._server.wants_meters():
                data = self._events.meters()
                if data is not None:
                    self._server.broadcast("meters", data, meters=True)
        except Exception:
            pass

    def _emit(self, event, data):
        if self._server is not None and self._server.has_subscribers():
            self._server.broadcast(event, data)

    def _drain(self):
        self._server.poll()
        batch = []
        inbox = self._server.inbox
        while len(batch) < MAX_MESSAGES_PER_TICK:
            try:
                batch.append(inbox.get_nowait())
            except Exception:
                break
        if not batch:
            return
        song = self.song()
        # Group every change made in this tick into one undo step (Live 11+).
        grouped = hasattr(song, "begin_undo_step") and any(m.get("cmd") not in READ_ONLY for _, m in batch)
        if grouped:
            song.begin_undo_step()
        try:
            for conn, msg in batch:
                reply = self._execute(conn, msg)
                if msg.get("id") is not None or not reply.get("ok"):
                    reply["id"] = msg.get("id")
                    conn.send(reply)
        finally:
            if grouped:
                song.end_undo_step()

    def _execute(self, conn, msg):
        cmd = str(msg.get("cmd") or "")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return {"ok": False, "cmd": cmd, "error": "bad_params"}
        try:
            if cmd == "ping":
                return {"ok": True, "result": {"pong": True}}
            if cmd == "subscribe":
                conn.subscribed = True
                return {"ok": True, "result": {"subscribed": True}}
            if cmd == "unsubscribe":
                conn.subscribed = False
                return {"ok": True, "result": {"subscribed": False}}
            if cmd == "set_meters":
                conn.meters = bool(params.get("enabled", True))
                return {"ok": True, "result": {"meters": conn.meters}}
            if cmd == "batch":
                return {"ok": True, "result": self._batch(conn, params)}
            handler = self._commands.handlers.get(cmd)
            if handler is None:
                return {"ok": False, "cmd": cmd, "error": "unknown_command: %s" % cmd}
            return {"ok": True, "result": handler(params)}
        except CommandError as e:
            return {"ok": False, "cmd": cmd, "error": str(e)}
        except Exception as e:
            self.log_message("PulseBridge: %s failed: %s\n%s" % (cmd, e, traceback.format_exc()))
            return {"ok": False, "cmd": cmd, "error": "%s: %s" % (type(e).__name__, e)}

    def _batch(self, conn, params):
        results = []
        stop_on_error = bool(params.get("stop_on_error", False))
        for sub in params.get("commands") or []:
            if not isinstance(sub, dict) or sub.get("cmd") == "batch":
                results.append({"ok": False, "error": "bad_batch_item"})
            else:
                results.append(self._execute(conn, sub))
            if stop_on_error and not results[-1].get("ok"):
                break
        return results
