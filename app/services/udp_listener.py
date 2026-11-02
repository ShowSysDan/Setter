"""
UDP control listener.

Any device that can send a UDP datagram (Q-SYS, Crestron, AMX, a Stream Deck
plugin, `echo "FADE 2 25" | nc -u ...`) can drive the switcher through this.
Each datagram is plain text; several commands can be sent in one datagram
separated by newlines or semicolons. The parsed result goes through the
same command runner the web UI and /api/v1/ use.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time

log = logging.getLogger("setter.udp")

MAX_DATAGRAM = 2048          # a control string is a few dozen bytes; anything bigger is not one
REBIND_DELAY = 1.0           # seconds between attempts to re-create a broken socket
REFUSED_LOG_INTERVAL = 60.0  # log a refused sender at most this often


class UdpListener:
    def __init__(self, host: str, port: int, handler, reply: bool = True, allowed=None, on_refused=None):
        """
        handler(text, source_addr) -> list[str] of reply lines.
        allowed: list of ipaddress networks; empty / None = any sender.
        on_refused(addr) is called (rate limited) when a datagram is dropped.
        """
        self.host = host
        self.port = port
        self.handler = handler
        self.reply = reply
        self.allowed = list(allowed or [])
        self.on_refused = on_refused
        self.received = 0
        self.refused = 0
        self.last = None            # {"ts", "from", "text", "reply"}
        self.error = None
        self._refused_logged = {}   # ip -> last time we logged it
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.host, self.port))   # raises OSError if the port is taken
        sock.settimeout(0.5)
        return sock

    def sender_allowed(self, ip: str) -> bool:
        if not self.allowed:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.allowed)

    def start(self):
        if self.running:
            return
        self._sock = self._open()
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
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                # the socket broke underneath us; keep trying to get it back
                try:
                    self._sock = self._open()
                    self.error = None
                    log.info("UDP listener re-bound to %s:%s", self.host, self.port)
                except OSError as exc:
                    self.error = str(exc)
                    self._stop.wait(REBIND_DELAY)
                continue
            try:
                data, addr = sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                log.warning("UDP listener socket error: %s; re-binding", exc)
                self.error = str(exc)
                try:
                    sock.close()
                except OSError:
                    pass
                self._sock = None
                self._stop.wait(REBIND_DELAY)
                continue
            if not self.sender_allowed(addr[0]):
                self.refused += 1
                now = time.monotonic()
                if now - self._refused_logged.get(addr[0], 0) > REFUSED_LOG_INTERVAL:
                    self._refused_logged[addr[0]] = now
                    if self.on_refused:
                        self.on_refused(addr)
                continue   # no reply either: an unknown sender learns nothing
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
