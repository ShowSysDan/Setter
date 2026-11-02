"""
UDP control listener.

Any device that can send a UDP datagram (Q-SYS, Crestron, AMX, a Stream Deck
plugin, `echo "FADE 2 25" | nc -u ...`) can drive the switcher through this.
Each datagram is plain text; several commands can be sent in one datagram
separated by newlines or semicolons. The parsed result goes through the
same command runner the web UI and /api/v1/ use.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger("setter.udp")


class UdpListener:
    def __init__(self, host: str, port: int, handler, reply: bool = True):
        """
        handler(text, source_addr) -> list[str] of reply lines.
        """
        self.host = host
        self.port = port
        self.handler = handler
        self.reply = reply
        self.received = 0
        self.last = None            # {"ts", "from", "text", "reply"}
        self.error = None
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))   # raises OSError if the port is taken
        sock.settimeout(0.5)
        self._sock = sock
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="udp-listener", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _run(self):
        sock = self._sock
        while not self._stop.is_set() and sock:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace").strip()
            self.received += 1
            try:
                replies = self.handler(text, addr) or []
            except Exception as exc:  # the handler already logs its own errors
                log.exception("command handler failed")
                replies = [f"ERR internal error: {exc}"]
            self.last = {"ts": time.time(), "from": f"{addr[0]}:{addr[1]}",
                         "text": text, "reply": replies}
            if self.reply and replies:
                try:
                    sock.sendto(("\n".join(replies) + "\n").encode("utf-8"), addr)
                except OSError as exc:
                    log.debug("could not reply to %s: %s", addr, exc)
