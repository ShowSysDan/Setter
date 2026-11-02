"""
End-to-end test: start the fake ATEM, start Setter against it, drive it over
UDP and HTTP, and check the switcher state follows. Run with: python -m unittest
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from fake_atem import FakeAtem  # noqa: E402

from app import create_app  # noqa: E402
from tests.test_syslog import UdpSink  # noqa: E402


def free_ports(n):
    """n distinct free UDP ports. The sockets stay open until all are chosen
    so the OS cannot hand the same port out twice."""
    socks = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        socks.append(s)
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


class TestConfig:
    TESTING = True
    SECRET_KEY = "test"
    LOG_LEVEL = "WARNING"
    API_KEY = None
    SETTER_AUTOSTART = True
    SETTINGS_FILE = None


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.atem_port, cls.udp_port = free_ports(2)
        cls.fake = FakeAtem("127.0.0.1", cls.atem_port, inputs=8, fps=25, mes=1, aux=2, name="ATEM Test")
        cls.fake.log = lambda msg: None
        threading.Thread(target=cls.fake.run, daemon=True).start()

        cls.tmp = tempfile.mkdtemp()
        settings_file = os.path.join(cls.tmp, "settings.json")
        with open(settings_file, "w") as fh:
            json.dump({"switcher_ip": "127.0.0.1", "switcher_port": cls.atem_port,
                       "udp_host": "127.0.0.1", "udp_port": cls.udp_port,
                       "input_count": 8, "aux_count": 2, "default_fade_frames": 5}, fh)
        TestConfig.SETTINGS_FILE = settings_file
        cls.app = create_app(TestConfig)
        cls.client = cls.app.test_client()
        cls.ctl = cls.app.extensions["setter"]
        deadline = time.time() + 5
        while time.time() < deadline and cls.ctl.connection_state() != "connected":
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.ctl.stop()

    def udp(self, text):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(text.encode(), ("127.0.0.1", self.udp_port))
        data, _ = s.recvfrom(4096)
        s.close()
        return data.decode().strip().split("\n")

    def wait_for(self, predicate, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_01_connected_and_state_loaded(self):
        self.assertEqual(self.ctl.connection_state(), "connected")
        st = self.ctl.client.state
        self.assertEqual(st.product, "ATEM Test")
        self.assertEqual(st.inputs[3]["long_name"], "Camera 3")
        self.assertEqual(st.program[0], 1)
        self.assertEqual(st.video_mode_name, "1080p25")
        self.assertEqual(st.topology["aux"], 2)

    def test_02_http_command_program(self):
        r = self.client.post("/api/command", json={"text": "PGM 3"})
        self.assertEqual(r.get_json()["replies"], ["OK PGM 3 ME1"])
        self.assertTrue(self.wait_for(lambda: self.fake.program[0] == 3))
        self.assertTrue(self.wait_for(lambda: self.ctl.client.state.program[0] == 3))

    def test_03_udp_fade(self):
        self.assertEqual(self.udp("PVW 5"), ["OK PVW 5 ME1"])
        self.assertTrue(self.wait_for(lambda: self.fake.preview[0] == 5))
        self.assertEqual(self.udp("FADE 2 5"), ["OK FADE 2 5 ME1"])
        self.assertEqual(self.fake.rate[0], 5)
        self.assertTrue(self.wait_for(lambda: self.fake.program[0] == 2, timeout=3.0))
        self.assertTrue(self.wait_for(lambda: self.ctl.client.state.program[0] == 2))

    def test_04_udp_multiple_and_aux(self):
        replies = self.udp("AUX 2 BARS; PING; AUX 9 1")
        self.assertEqual(replies[0], "OK AUX 2 BARS")
        self.assertEqual(replies[1], "PONG")
        self.assertTrue(replies[2].startswith("ERR"))
        self.assertTrue(self.wait_for(lambda: self.fake.aux[1] == 1000))

    def test_05_external_api_text(self):
        r = self.client.get("/api/v1/cmd?c=CUT&format=text")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(as_text=True).strip(), "OK CUT ME1")
        r = self.client.get("/api/v1/status?format=text")
        self.assertTrue(r.get_data(as_text=True).startswith("OK CONNECTED ATEM Test"))

    def test_06_raw_and_status(self):
        r = self.client.post("/api/raw", json={"name": "CPgI", "hex": "00 00 00 04"})
        self.assertTrue(r.get_json()["acked"])
        self.assertTrue(self.wait_for(lambda: self.fake.program[0] == 4))
        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["connection"]["state"], "connected")
        self.assertEqual(status["inputs"][0]["label"], "Cam1")
        self.assertEqual(len(status["inputs"]), 8)

    def test_07_settings_change_reconnects(self):
        r = self.client.put("/api/settings", json={"input_count": 4})
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(self.udp("PGM 6")[0].startswith("ERR"))
        self.assertEqual(self.udp("PGM #6"), ["OK PGM #6 ME1"])
        self.client.put("/api/settings", json={"input_count": 8})

    def test_08_events_logged(self):
        events = self.client.get("/api/events").get_json()["events"]
        messages = [e["message"] for e in events]
        self.assertIn("UDP control string received", messages)
        self.assertIn("Fade", messages)

    def test_09_remote_syslog_via_settings(self):
        sink = UdpSink()
        try:
            r = self.client.put("/api/settings", json={"syslog_enabled": True, "syslog_host": "127.0.0.1",
                                                      "syslog_port": sink.port, "syslog_protocol": "udp",
                                                      "syslog_facility": "local5"})
            self.assertTrue(r.get_json()["ok"], r.get_json())
            self.assertEqual(self.udp("PGM 7"), ["OK PGM 7 ME1"])
            self.assertTrue(self.wait_for(lambda: any("Program changed" in l and "via=udp" in l for l in sink.lines)))
            self.assertTrue(any(l.startswith("<174>") for l in sink.lines))   # local5 (21*8) + info (6)
            r = self.client.post("/api/syslog/test")
            self.assertTrue(r.get_json()["ok"], r.get_json())
            self.assertTrue(self.wait_for(lambda: any("Test message" in l for l in sink.lines)))
            self.assertEqual(self.client.get("/api/status").get_json()["syslog"]["failed"], 0)
            # enabling without a host is refused
            r = self.client.put("/api/settings", json={"syslog_host": ""})
            self.assertEqual(r.status_code, 400)
        finally:
            self.client.put("/api/settings", json={"syslog_enabled": False})
            sink.close()


if __name__ == "__main__":
    unittest.main()
