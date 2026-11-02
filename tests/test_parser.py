"""Unit tests for the control string parser. Run with: python -m unittest"""
import unittest

from app.services.command_parser import CommandRunner, parse_rate, parse_source, CommandError


class StubSettings:
    def __init__(self, **kw):
        self.data = {"me_count": 2, "input_count": 8, "aux_count": 3, "dsk_count": 2, "macro_count": 10}
        self.data.update(kw)

    def get(self, key):
        return self.data[key]


class StubAudit:
    def __init__(self):
        self.entries = []

    def info(self, msg, **f): self.entries.append(("info", msg, f))
    def warning(self, msg, **f): self.entries.append(("warning", msg, f))


class StubController:
    """Records every action; returns acked=True."""
    def __init__(self):
        self.settings = StubSettings()
        self.audit = StubAudit()
        self.calls = []

    def fps(self): return 25.0
    def status_line(self): return "OK CONNECTED Stub PGM=1 PVW=2"

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, {k: v for k, v in kwargs.items() if k not in ("via", "sender")}))
            return True
        return record


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.ctl = StubController()
        self.runner = CommandRunner(self.ctl)

    def run_cmd(self, text):
        return self.runner.execute(text, via="test")

    def test_sources(self):
        self.assertEqual(parse_source("3", 8), 3)
        self.assertEqual(parse_source("cam3", 8), 3)
        self.assertEqual(parse_source("BARS", 8), 1000)
        self.assertEqual(parse_source("mp1k", 8), 3011)
        self.assertEqual(parse_source("#3010", 8), 3010)
        self.assertEqual(parse_source("aux2", 8), 8002)
        with self.assertRaises(CommandError):
            parse_source("9", 8)
        with self.assertRaises(CommandError):
            parse_source("banana", 8)

    def test_rates(self):
        self.assertEqual(parse_rate("25", 25), 25)
        self.assertEqual(parse_rate("1s", 25), 25)
        self.assertEqual(parse_rate("0.5s", 29.97), 15)
        self.assertEqual(parse_rate("12f", 25), 12)
        with self.assertRaises(CommandError):
            parse_rate("300", 25)

    def test_pgm_and_me(self):
        self.assertEqual(self.run_cmd("pgm 2"), ["OK PGM 2 ME1"])
        self.assertEqual(self.ctl.calls[-1], ("program", (2, 0), {}))
        self.assertEqual(self.run_cmd("PGM BARS ME2"), ["OK PGM BARS ME2"])
        self.assertEqual(self.ctl.calls[-1], ("program", (1000, 1), {}))
        self.assertEqual(self.run_cmd("PGM 1 ME3")[0][:3], "ERR")

    def test_fade(self):
        self.assertEqual(self.run_cmd("FADE 2 25"), ["OK FADE 2 25 ME1"])
        self.assertEqual(self.ctl.calls[-1], ("fade", (2, 0, 25), {}))
        self.assertEqual(self.run_cmd("fade 3 1s me2"), ["OK FADE 3 25 ME2"])
        self.assertEqual(self.ctl.calls[-1], ("fade", (3, 1, 25), {}))
        self.assertEqual(self.run_cmd("FADE 4"), ["OK FADE 4 current-rate ME1"])
        self.assertEqual(self.ctl.calls[-1], ("fade", (4, 0, None), {}))

    def test_cut_auto_ftb_bare_me(self):
        self.run_cmd("CUT 2")
        self.assertEqual(self.ctl.calls[-1], ("cut", (1,), {}))
        self.run_cmd("AUTO")
        self.assertEqual(self.ctl.calls[-1], ("auto", (0,), {}))
        self.run_cmd("FTB ME2")
        self.assertEqual(self.ctl.calls[-1], ("ftb", (1,), {}))

    def test_aux_dsk_key_macro(self):
        self.assertEqual(self.run_cmd("AUX 2 MP1"), ["OK AUX 2 MP1"])
        self.assertEqual(self.ctl.calls[-1], ("aux", (1, 3010), {}))
        self.assertEqual(self.run_cmd("AUX 4 1")[0][:3], "ERR")
        self.run_cmd("DSK 1 auto")
        self.assertEqual(self.ctl.calls[-1], ("dsk", (0, "auto"), {}))
        self.run_cmd("KEY 2 on ME2")
        self.assertEqual(self.ctl.calls[-1], ("key", (1, True, 1), {}))
        self.run_cmd("MACRO 3")
        self.assertEqual(self.ctl.calls[-1], ("macro", (2,), {}))

    def test_raw(self):
        self.assertEqual(self.run_cmd("RAW CPgI 00 00 00 01"), ["OK RAW CPgI 00000001"])
        self.assertEqual(self.ctl.calls[-1], ("raw", ("CPgI", b"\x00\x00\x00\x01"), {}))
        self.assertEqual(self.run_cmd("RAW CPg 00")[0][:3], "ERR")

    def test_multiple_and_errors(self):
        replies = self.run_cmd("PVW 3; CUT\nPING")
        self.assertEqual(replies, ["OK PVW 3 ME1", "OK CUT ME1", "PONG"])
        self.assertEqual(self.run_cmd("WIBBLE")[0][:3], "ERR")
        self.assertEqual(self.run_cmd(""), ["ERR empty command"])
        self.assertTrue(self.run_cmd("HELP")[0].startswith("OK PING"))


if __name__ == "__main__":
    unittest.main()
