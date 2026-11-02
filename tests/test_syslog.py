"""
Remote syslog tests: a throwaway UDP and TCP "syslog server" receive what
the audit log sends, including application errors; an unreachable server
must not block the caller. Run with: python -m unittest
"""
import logging
import socket
import threading
import time
import unittest

from app.services.audit_log import AuditLog


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class UdpSink:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.lines = []
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(8192)
                self.lines.append(data.decode("utf-8", "replace"))
            except socket.timeout:
                continue
            except OSError:
                break

    def close(self):
        self._stop = True
        self.sock.close()


class TcpSink:
    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.srv.settimeout(0.2)
        self.port = self.srv.getsockname()[1]
        self.data = b""
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        conn = None
        while not self._stop:
            try:
                if conn is None:
                    conn, _ = self.srv.accept()
                    conn.settimeout(0.2)
                chunk = conn.recv(8192)
                if not chunk:
                    conn.close()
                    conn = None
                    continue
                self.data += chunk
            except socket.timeout:
                continue
            except OSError:
                break

    @property
    def lines(self):
        return [l for l in self.data.decode("utf-8", "replace").split("\n") if l]

    def close(self):
        self._stop = True
        self.srv.close()


class RemoteSyslogTests(unittest.TestCase):
    def test_udp_receives_actions_and_app_errors(self):
        sink = UdpSink()
        audit = AuditLog(tag="settertest")
        try:
            audit.configure_remote(True, "127.0.0.1", sink.port, "udp", "local3")
            audit.info("Program changed", me=1, input="Cam2", via="udp", sender="10.0.0.5:4000")
            logging.getLogger("setter.atem").warning("Switcher did not acknowledge packet 7")
            logging.getLogger("setter.atem").info("this INFO line must not be forwarded")
            self.assertTrue(wait_for(lambda: len(sink.lines) >= 2))
            joined = "\n".join(sink.lines)
            # <PRI> = facility local3 (19) * 8 + severity info (6) = 158
            self.assertIn("<158>", joined)
            self.assertIn("settertest[", joined)
            self.assertIn("INFO Program changed me=1 input=Cam2 via=udp sender=10.0.0.5:4000", joined)
            # local3 * 8 + warning (4) = 156
            self.assertIn("<156>", joined)
            self.assertIn("WARNING setter.atem: Switcher did not acknowledge packet 7", joined)
            self.assertNotIn("must not be forwarded", joined)
            self.assertEqual(audit.remote_status()["sent"], 2)
            self.assertEqual(audit.remote_status()["failed"], 0)
        finally:
            audit.close()
            sink.close()

    def test_tcp_newline_framing_and_reconfigure(self):
        sink = TcpSink()
        audit = AuditLog(tag="settertest")
        try:
            audit.configure_remote(True, "127.0.0.1", sink.port, "tcp", "local0")
            audit.info("Cut", me=1, via="ui")
            audit.error("UDP control listener could not start", reason="port busy")
            self.assertTrue(wait_for(lambda: len(sink.lines) >= 2))
            self.assertTrue(sink.lines[0].startswith("<134>"))     # local0 info
            self.assertTrue(sink.lines[1].startswith("<131>"))     # local0 error
            self.assertTrue(sink.data.endswith(b"\n"))
            # turning it off detaches the handlers; nothing more arrives
            audit.configure_remote(False)
            audit.info("after disable")
            time.sleep(0.2)
            self.assertNotIn("after disable", sink.data.decode())
            self.assertFalse(audit.remote_status()["running"])
        finally:
            audit.close()
            sink.close()

    def test_unreachable_server_does_not_block(self):
        audit = AuditLog(tag="settertest")
        try:
            # a port nobody is listening on
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            audit.configure_remote(True, "127.0.0.1", port, "tcp", "local0")
            t0 = time.monotonic()
            for i in range(20):
                audit.info("Fade", me=1, input=f"Cam{i}")
            self.assertLess(time.monotonic() - t0, 0.5, "logging must not wait on the socket")
            self.assertTrue(wait_for(lambda: audit.remote_status()["failed"] >= 1))
            st = audit.remote_status()
            self.assertEqual(st["sent"], 0)
            self.assertIsNotNone(st["last_error"])
            self.assertEqual(len(audit.since()), 20, "the activity feed still has everything")
        finally:
            audit.close()


if __name__ == "__main__":
    unittest.main()
