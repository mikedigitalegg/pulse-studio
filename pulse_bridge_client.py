"""
Client for the PulseBridge Remote Script (pulse_bridge/PulseBridge).

Keeps one persistent TCP connection to Live, reconnects automatically, matches replies
to requests by id, and fans out pushed events to registered callbacks.

    bridge = PulseBridgeClient()
    bridge.start()
    bridge.request("get_song")                   # waits for the reply
    bridge.send("fire_scene", {"scene_index": 0})  # fire-and-forget, keeps order
"""
from __future__ import annotations

import itertools
import json
import os
import socket
import threading
from typing import Any, Callable

DEFAULT_HOST = os.environ.get("PULSE_BRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PULSE_BRIDGE_PORT", "9880"))


class BridgeError(RuntimeError):
    pass


class BridgeUnavailable(BridgeError):
    pass


class _Pending:
    __slots__ = ("event", "reply")

    def __init__(self):
        self.event = threading.Event()
        self.reply: dict | None = None


class PulseBridgeClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, reconnect_s: float = 2.0):
        self.host = host
        self.port = int(port)
        self.reconnect_s = float(reconnect_s)
        self.hello: dict | None = None
        self.capabilities: dict | None = None
        self.last_error: str | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._ids = itertools.count(1)
        self._listeners: list[Callable[[str, dict], None]] = []
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "PulseBridgeClient":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pulse-bridge-client", daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        self._drop_connection("closed")

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout_s: float) -> bool:
        return self._connected.wait(timeout_s)

    def on_event(self, callback: Callable[[str, dict], None]):
        self._listeners.append(callback)

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "hello": self.hello,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ requests

    def request(self, cmd: str, params: dict | None = None, timeout_s: float = 5.0) -> Any:
        if not self.connected:
            raise BridgeUnavailable(self.last_error or "PulseBridge not connected")
        rid = next(self._ids)
        pending = _Pending()
        with self._state_lock:
            self._pending[rid] = pending
        try:
            self._write({"id": rid, "cmd": cmd, "params": params or {}})
            if not pending.event.wait(timeout_s):
                raise BridgeError(f"timeout waiting for {cmd}")
        finally:
            with self._state_lock:
                self._pending.pop(rid, None)
        reply = pending.reply or {}
        if reply.get("disconnected"):
            raise BridgeUnavailable("PulseBridge disconnected")
        if not reply.get("ok"):
            raise BridgeError(str(reply.get("error") or "unknown_error"))
        return reply.get("result")

    def send(self, cmd: str, params: dict | None = None) -> None:
        """Fire-and-forget. Errors come back as id-less replies and are recorded in last_error."""
        if not self.connected:
            raise BridgeUnavailable(self.last_error or "PulseBridge not connected")
        self._write({"cmd": cmd, "params": params or {}})

    def _write(self, obj: dict):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise BridgeUnavailable("PulseBridge not connected")
            try:
                sock.sendall(data)
            except OSError as e:
                self._drop_connection(str(e))
                raise BridgeUnavailable(str(e)) from e

    # ------------------------------------------------------------------ connection loop

    def _run(self):
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=2.0)
            except OSError as e:
                self.last_error = f"cannot connect to PulseBridge on {self.host}:{self.port} ({e.strerror or e})"
                self._stop.wait(self.reconnect_s)
                continue
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._send_lock:
                self._sock = sock
            self._connected.set()
            self.last_error = None
            reader = threading.Thread(target=self._read_loop, args=(sock,), daemon=True)
            reader.start()
            try:
                self.hello = self.request("hello", timeout_s=3.0)
                self.request("subscribe", timeout_s=3.0)
                # Capabilities walk the browser's top level; allow Live a moment.
                self.capabilities = self.request("get_capabilities", timeout_s=10.0)
                self._dispatch("bridge.connected", {"hello": self.hello})
            except BridgeError as e:
                self.last_error = f"handshake failed: {e}"
            reader.join()
            self._dispatch("bridge.disconnected", {"error": self.last_error})
            if not self._stop.is_set():
                self._stop.wait(self.reconnect_s)

    def _read_loop(self, sock: socket.socket):
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle_line(line)
        except OSError as e:
            self.last_error = str(e)
        finally:
            self._drop_connection(self.last_error or "connection closed")

    def _handle_line(self, line: bytes):
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            return
        if "event" in msg:
            self._dispatch(str(msg["event"]), msg.get("data") or {})
            return
        rid = msg.get("id")
        if rid is None:
            if not msg.get("ok"):
                self.last_error = f"{msg.get('cmd') or 'command'} failed: {msg.get('error')}"
            return
        with self._state_lock:
            pending = self._pending.get(rid)
        if pending is not None:
            pending.reply = msg
            pending.event.set()

    def _drop_connection(self, reason: str):
        with self._send_lock:
            sock, self._sock = self._sock, None
        if sock is None:
            return
        self._connected.clear()
        if reason:
            self.last_error = reason
        try:
            sock.close()
        except OSError:
            pass
        with self._state_lock:
            pending = list(self._pending.values())
        for p in pending:
            p.reply = {"ok": False, "disconnected": True}
            p.event.set()

    def _dispatch(self, event: str, data: dict):
        for cb in list(self._listeners):
            try:
                cb(event, data)
            except Exception:
                pass
