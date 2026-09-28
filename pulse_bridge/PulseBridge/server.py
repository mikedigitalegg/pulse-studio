"""
TCP transport for PulseBridge. No Live imports here so it can be unit tested.

Wire format: one JSON object per line (UTF-8, '\n' terminated).
  request : {"id": 1, "cmd": "get_song", "params": {...}}   ("id" optional = fire-and-forget)
  response: {"id": 1, "ok": true, "result": {...}}  |  {"id": 1, "ok": false, "error": "..."}
  event   : {"event": "song.tempo", "data": {...}}   (only to connections that sent "subscribe")

Python threads inside Live only get to run around Live's ~100 ms ticks, so the latency-
critical path is done on the main thread with non-blocking sockets: each tick polls for
input, executes it, and writes replies straight away if the socket is ready. Only an
accept thread and a per-connection writer thread (for data the socket couldn't take
immediately) run in the background, so a slow or stuck client can never block Live.
"""
from __future__ import absolute_import, print_function, unicode_literals

import json
import select
import socket
import threading

try:
    import queue
except ImportError:  # pragma: no cover - Python 2
    import Queue as queue

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_OUTBOX = 5000


def _encode(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


class Connection(object):
    def __init__(self, server, sock, addr):
        self.server = server
        self.sock = sock
        self.addr = addr
        self.subscribed = False
        self.meters = False
        self.alive = True
        self._outbox = queue.Queue(maxsize=MAX_OUTBOX)
        self._lock = threading.Lock()
        self._pending = 0  # items handed to the writer thread and not yet sent
        self._buf = b""
        self._writer = threading.Thread(target=self._write_loop, name="PulseBridge-write")
        self._writer.daemon = True

    def start(self):
        self._writer.start()

    def send(self, obj):
        """
        Called on Live's main thread. Writes immediately when the socket can take the data
        without blocking. Background threads in Live's Python only get to run around Live's
        ticks, so handing every reply to the writer thread would cost an extra ~100 ms.
        Anything that can't go out now (or would jump ahead of queued data) goes to the writer.
        """
        if not self.alive:
            return
        data = _encode(obj)
        with self._lock:
            if self._pending == 0:
                data = self._write_ready(data)
                if not data:
                    return
            self._pending += 1
        try:
            self._outbox.put_nowait(data)
        except queue.Full:
            # Client is not reading; drop it rather than grow without bound.
            self.server.log("PulseBridge: dropping unresponsive client %s" % (self.addr,))
            self.close()

    def _write_ready(self, data):
        """Send as much as the socket accepts right now; return what's left."""
        try:
            while data:
                _, writable, _ = select.select([], [self.sock], [], 0)
                if not writable:
                    break
                data = data[self.sock.send(data):]
        except Exception:
            self.close()
            return b""
        return data

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.server._forget(self)

    def poll(self, max_bytes=4 * 1024 * 1024):
        """
        Called on Live's main thread each tick: read whatever has arrived, without blocking,
        and queue complete messages. Reading here rather than in a thread means a request
        is executed in the same tick it arrives, instead of the next.
        """
        read = 0
        try:
            while self.alive and read < max_bytes:
                readable, _, _ = select.select([self.sock], [], [], 0)
                if not readable:
                    break
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.close()
                    return
                read += len(chunk)
                self._buf += chunk
        except Exception:
            self.close()
            return
        if len(self._buf) > MAX_LINE_BYTES and b"\n" not in self._buf:
            self.send({"id": None, "ok": False, "error": "message_too_large"})
            self.close()
            return
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception as e:
                self.send({"id": None, "ok": False, "error": "bad_json: %s" % e})
                continue
            if not isinstance(msg, dict):
                self.send({"id": None, "ok": False, "error": "bad_message"})
                continue
            self.server.inbox.put((self, msg))

    def _write_loop(self):
        while True:
            data = self._outbox.get()
            if data is None or not self.alive:
                return
            try:
                self.sock.sendall(data)
            except Exception:
                self.close()
                return
            with self._lock:
                self._pending -= 1


class BridgeServer(object):
    def __init__(self, host, port, log=None):
        self.host = host
        self.port = int(port)
        self.log = log or (lambda msg: None)
        self.inbox = queue.Queue()
        self._lock = threading.Lock()
        self._connections = []
        self._sock = None
        self._running = False
        self._thread = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="PulseBridge-accept")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        for c in self.connections():
            c.close()
        if self._thread is not None:
            self._thread.join(1.0)

    def connections(self):
        with self._lock:
            return list(self._connections)

    def poll(self):
        for c in self.connections():
            c.poll()

    def has_subscribers(self):
        with self._lock:
            return any(c.subscribed for c in self._connections)

    def wants_meters(self):
        with self._lock:
            return any(c.meters for c in self._connections)

    def broadcast(self, event, data, meters=False):
        msg = {"event": event, "data": data}
        for c in self.connections():
            if (c.meters if meters else c.subscribed):
                c.send(msg)

    def _forget(self, conn):
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)

    def _accept_loop(self):
        while self._running:
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    continue
                return
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            sock.settimeout(None)
            conn = Connection(self, sock, addr)
            with self._lock:
                self._connections.append(conn)
            conn.start()
            self.log("PulseBridge: client connected %s" % (addr,))
