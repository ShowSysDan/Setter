"""
Audit log.

Every switcher action, connection event and incoming control string is
written to:

  * the local syslog socket (LOCAL0, tag "setter"), or stderr where there
    is no syslog socket (Windows, some macOS);
  * an optional remote syslog server (UDP or TCP, RFC 3164 framing), which
    also receives every WARNING or ERROR raised anywhere in the application;
  * a short in-memory ring buffer that the dashboard activity feed reads.

Remote delivery runs on its own thread via a queue, so a slow DNS lookup or
a syslog server that is down never delays a cue.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import time
from collections import deque

FACILITIES = {
    "local0": logging.handlers.SysLogHandler.LOG_LOCAL0,
    "local1": logging.handlers.SysLogHandler.LOG_LOCAL1,
    "local2": logging.handlers.SysLogHandler.LOG_LOCAL2,
    "local3": logging.handlers.SysLogHandler.LOG_LOCAL3,
    "local4": logging.handlers.SysLogHandler.LOG_LOCAL4,
    "local5": logging.handlers.SysLogHandler.LOG_LOCAL5,
    "local6": logging.handlers.SysLogHandler.LOG_LOCAL6,
    "local7": logging.handlers.SysLogHandler.LOG_LOCAL7,
    "user": logging.handlers.SysLogHandler.LOG_USER,
    "daemon": logging.handlers.SysLogHandler.LOG_DAEMON,
}
PROTOCOLS = ("udp", "tcp")


class RemoteSyslogHandler(logging.handlers.SysLogHandler):
    """
    SysLogHandler that never blocks for long, never prints tracebacks to
    stderr when the server is unreachable, and counts what happened so the
    Settings page can show it. TCP connects are bounded by a timeout and
    re-established after a failure.
    """

    CONNECT_TIMEOUT = 5.0

    def __init__(self, address, protocol: str, facility: int):
        # Deliberately skip SysLogHandler.__init__: it resolves and connects
        # right away and raises if the server is down. We connect lazily on
        # the first emit, from the queue thread, with a timeout.
        logging.Handler.__init__(self)
        self.address = address
        self.facility = facility
        self.socktype = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
        self.unixsocket = False
        self.socket = None
        self.append_nul = False          # rsyslog / syslog-ng do not want the trailing NUL
        self.protocol = protocol
        self.attempted = 0
        self.delivered = 0
        self.failures = 0
        self.last_error = None
        self.last_error_at = None
        self._target = None

    def createSocket(self):
        host, port = self.address
        if self.socktype == socket.SOCK_STREAM:
            sock = socket.create_connection((host, port), timeout=self.CONNECT_TIMEOUT)
            sock.settimeout(self.CONNECT_TIMEOUT)
            self._target = (host, port)
        else:
            family, _, _, _, sockaddr = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)[0]
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(self.CONNECT_TIMEOUT)
            self._target = sockaddr
        self.socket = sock

    def emit(self, record):
        self.attempted += 1
        try:
            msg = self.format(record)
            prio = "<%d>" % self.encodePriority(self.facility, self.mapPriority(record.levelname))
            data = (prio + msg).encode("utf-8")
            if self.socket is None:
                self.createSocket()
            if self.socktype == socket.SOCK_DGRAM:
                self.socket.sendto(data, self._target)
            else:
                self.socket.sendall(data)
            self.delivered += 1
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        exc = sys.exc_info()[1]
        self.failures += 1
        self.last_error = (str(exc) or exc.__class__.__name__) if exc else "unknown error"
        self.last_error_at = time.time()
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None   # next emit reconnects

    @property
    def sent(self) -> int:
        # Counted on success rather than derived from attempted - failures,
        # so a status read taken while an emit is in flight is never off by one.
        return self.delivered


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that drops the record (instead of blocking or printing a
    traceback) if the remote queue is full, e.g. the server has been down for
    a long time."""

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass

    def handleError(self, record):
        pass


MAX_FIELD_LENGTH = 400


def _clean(value):
    """Make a field safe for a single syslog line: no control characters (a
    newline in a UDP control string must not become a forged log entry) and
    a bounded length."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text = str(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    if len(text) > MAX_FIELD_LENGTH:
        text = text[:MAX_FIELD_LENGTH] + "...(truncated)"
    return text


class AuditLog:
    def __init__(self, tag: str = "setter", maxlen: int = 300):
        self.tag = tag
        self.hostname = socket.gethostname().split(".")[0] or "setter"
        self.events = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

        self.logger = logging.getLogger(f"{tag}.audit")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            self.logger.addHandler(self._local_handler())

        # remote target (see configure_remote)
        self.remote = None            # RemoteSyslogHandler
        self.remote_config = {"enabled": False}
        self._queue = None
        self._listener = None
        self._queue_handlers = []     # (logger, handler) pairs we attached

    # ------------------------------------------------------------------ local
    def _local_handler(self) -> logging.Handler:
        for path in ("/dev/log", "/var/run/syslog"):
            if os.path.exists(path):
                try:
                    handler = logging.handlers.SysLogHandler(
                        address=path, facility=logging.handlers.SysLogHandler.LOG_LOCAL0)
                    handler.setFormatter(logging.Formatter(f"{self.tag}: %(levelname)s %(message)s"))
                    return handler
                except OSError:
                    pass
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(f"%(asctime)s {self.tag}.audit %(levelname)s %(message)s"))
        return handler

    # ------------------------------------------------------------------ remote
    def configure_remote(self, enabled: bool, host: str = "", port: int = 514,
                         protocol: str = "udp", facility: str = "local0"):
        """
        (Re)configure the remote syslog target. Safe to call at any time;
        tears down the previous target first. Audit entries go at INFO and
        above; everything else in the process goes at WARNING and above.
        """
        self._teardown_remote()
        self.remote_config = {"enabled": bool(enabled), "host": host, "port": port,
                              "protocol": protocol, "facility": facility}
        if not enabled or not host:
            return

        handler = RemoteSyslogHandler((host, int(port)), protocol,
                                      FACILITIES.get(facility, FACILITIES["local0"]))
        # RFC 3164: "<PRI>Mmm dd HH:MM:SS host tag[pid]: message" (+ newline framing on TCP)
        line = f"%(asctime)s {self.hostname} {self.tag}[{os.getpid()}]: %(levelname)s %(message)s"
        handler.setFormatter(logging.Formatter(line + ("\n" if protocol == "tcp" else ""),
                                               datefmt="%b %d %H:%M:%S"))

        self._queue = queue.Queue(maxsize=2000)
        self._listener = logging.handlers.QueueListener(self._queue, handler, respect_handler_level=True)
        self._listener.start()
        self.remote = handler

        audit_qh = _DroppingQueueHandler(self._queue)
        audit_qh.setLevel(logging.INFO)
        self.logger.addHandler(audit_qh)

        app_qh = _DroppingQueueHandler(self._queue)
        app_qh.setLevel(logging.WARNING)
        app_qh.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(app_qh)
        if root.level == logging.NOTSET or root.level > logging.WARNING:
            root.setLevel(logging.WARNING)

        self._queue_handlers = [(self.logger, audit_qh), (root, app_qh)]

    def _teardown_remote(self):
        for logger, handler in self._queue_handlers:
            logger.removeHandler(handler)
        self._queue_handlers = []
        listener, self._listener = self._listener, None
        if listener:
            try:
                listener.stop()
            except Exception:  # pragma: no cover
                pass
        remote, self.remote = self.remote, None
        if remote:
            try:
                remote.close()
            except Exception:  # pragma: no cover
                pass
        self._queue = None

    def remote_status(self) -> dict:
        cfg = dict(self.remote_config)
        remote = self.remote
        cfg.update({
            "running": remote is not None,
            "sent": remote.sent if remote else 0,
            "failed": remote.failures if remote else 0,
            "last_error": remote.last_error if remote else None,
            "last_error_at": remote.last_error_at if remote else None,
            "queued": self._queue.qsize() if self._queue else 0,
        })
        return cfg

    def close(self):
        self._teardown_remote()

    # ------------------------------------------------------------------ logging
    def log(self, level: int, message: str, **fields):
        clean = {k: _clean(v) for k, v in fields.items() if v is not None}
        extra = " ".join(f"{k}={v}" for k, v in clean.items())
        self.logger.log(level, f"{_clean(message)} {extra}".rstrip())
        with self._lock:
            self._seq += 1
            self.events.append({
                "id": self._seq,
                "ts": time.time(),
                "level": logging.getLevelName(level).lower(),
                "message": message,
                "fields": clean,
            })

    def info(self, message: str, **fields):
        self.log(logging.INFO, message, **fields)

    def warning(self, message: str, **fields):
        self.log(logging.WARNING, message, **fields)

    def error(self, message: str, **fields):
        self.log(logging.ERROR, message, **fields)

    def since(self, last_id: int = 0, limit: int = 100) -> list:
        with self._lock:
            items = [e for e in self.events if e["id"] > last_id]
        return items[-limit:]
