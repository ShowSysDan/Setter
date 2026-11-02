"""
The Setter control string protocol.

One grammar for every way in: UDP datagrams, the dashboard's "send a control
string" box, and GET/POST /api/v1/cmd. Case-insensitive, whitespace
separated, several commands per message split on newlines or semicolons.

    PING                          -> PONG
    STATUS                        -> connection, program and preview
    PGM <src> [MEn]               cut <src> straight to program
    PVW <src> [MEn]               put <src> on preview
    CUT [MEn]                     cut preview to program
    AUTO [MEn]                    auto transition with the current style/rate
    FADE <src> [rate] [MEn]       preview <src>, mix transition, optional rate
    STYLE <mix|dip|wipe|dve|sting> [MEn]
    RATE <rate> [MEn]             set the mix rate
    FTB [MEn]                     toggle fade to black
    FTBRATE <rate> [MEn]
    AUX <n> <src>                 route <src> to aux output n
    DSK <n> <on|off|auto|toggle>  downstream keyer n
    KEY <n> <on|off> [MEn]        upstream keyer n
    MACRO <n>                     run macro n
    RAW <NAME> <hex>              send a raw 4-character ATEM command

<src>  1..N for the numbered inputs (N is the configured input count), or one
       of BLACK, BARS, COL1, COL2, MP1, MP1K, MP2, MP2K, SS, CF1, CF2, PGM,
       PVW, AUXn, or #<id> for any raw ATEM source id.
<rate> frames (25), or seconds with an s suffix (1s, 0.5s) converted using
       the switcher's current video mode.
<MEn>  ME1..ME4; defaults to ME1. For CUT, AUTO and FTB a bare number is
       also accepted as the ME.
"""
from __future__ import annotations

import re

from .atem_client import NotConnected, STYLE_NAMES

SOURCE_ALIASES = {
    "BLACK": 0, "BLK": 0,
    "BARS": 1000,
    "COL1": 2001, "COLOR1": 2001, "COLOUR1": 2001,
    "COL2": 2002, "COLOR2": 2002, "COLOUR2": 2002,
    "MP1": 3010, "MP1K": 3011, "MP2": 3020, "MP2K": 3021,
    "MP3": 3030, "MP3K": 3031, "MP4": 3040, "MP4K": 3041,
    "SS": 6000, "SS1": 6000, "SS2": 6001,
    "CF1": 7001, "CF2": 7002, "CF3": 7003, "CF4": 7004,
    "PGM": 10010, "PVW": 10011,
    "ME1PGM": 10010, "ME1PVW": 10011, "ME2PGM": 10020, "ME2PVW": 10021,
    "ME3PGM": 10030, "ME3PVW": 10031, "ME4PGM": 10040, "ME4PVW": 10041,
}

STYLES = {"MIX": 0, "DIP": 1, "WIPE": 2, "DVE": 3, "STING": 4, "STINGER": 4}

HELP = [
    "PING | STATUS | HELP",
    "PGM <src> [MEn] | PVW <src> [MEn] | CUT [MEn] | AUTO [MEn]",
    "FADE <src> [rate] [MEn] | STYLE <mix|dip|wipe|dve|sting> [MEn] | RATE <rate> [MEn]",
    "FTB [MEn] | FTBRATE <rate> [MEn] | AUX <n> <src> | DSK <n> <on|off|auto|toggle>",
    "KEY <n> <on|off> [MEn] | MACRO <n> | RAW <NAME> <hex>",
]


class CommandError(ValueError):
    pass


def split_commands(text: str) -> list:
    return [part.strip() for part in re.split(r"[\n;]+", text or "") if part.strip()]


def parse_source(token: str, input_count: int) -> int:
    """Turn a source token into an ATEM source id."""
    u = token.upper()
    if u.startswith("#"):
        try:
            return int(u[1:])
        except ValueError:
            raise CommandError(f"'{token}' is not a valid raw source id")
    m = re.fullmatch(r"(?:IN|INPUT|CAM)?(\d+)", u)
    if m:
        n = int(m.group(1))
        if 1 <= n <= input_count:
            return n
        raise CommandError(f"input {n} is outside the configured inputs 1..{input_count} "
                           f"(change it in Settings, or use #{n} to force a raw source id)")
    if u in SOURCE_ALIASES:
        return SOURCE_ALIASES[u]
    m = re.fullmatch(r"AUX(\d+)", u)
    if m:
        return 8000 + int(m.group(1))
    raise CommandError(f"unknown source '{token}'")


def parse_rate(token: str, fps: float) -> int:
    t = token.lower()
    try:
        if t.endswith("s"):
            frames = round(float(t[:-1]) * fps)
        elif t.endswith("f"):
            frames = int(t[:-1])
        else:
            frames = int(t)
    except ValueError:
        raise CommandError(f"'{token}' is not a valid rate (use frames, e.g. 25, or seconds, e.g. 1s)")
    if not 1 <= frames <= 250:
        raise CommandError("rate must be between 1 and 250 frames")
    return frames


def _parse_index(token: str, what: str, count: int) -> int:
    """1-based user number -> 0-based index, validated against the configured count."""
    m = re.fullmatch(rf"(?:{what})?(\d+)", token.upper())
    if not m:
        raise CommandError(f"'{token}' is not a valid {what.lower()} number")
    n = int(m.group(1))
    if not 1 <= n <= count:
        raise CommandError(f"{what} {n} is outside the configured 1..{count} (change it in Settings)")
    return n - 1


def _extract_me(args: list, me_count: int, bare_number_is_me: bool = False):
    """Pull an ME1..ME4 token (or a bare number if allowed) out of args."""
    me = 0
    rest = []
    for a in args:
        u = a.upper()
        if re.fullmatch(r"ME\d+", u) or (bare_number_is_me and u.isdigit()):
            me = _parse_index(u, "ME", me_count)
        else:
            rest.append(a)
    return me, rest


def _need(args: list, n: int, usage: str):
    if len(args) < n:
        raise CommandError(f"usage: {usage}")


class CommandRunner:
    """Executes control strings against a Controller."""

    def __init__(self, controller):
        self.controller = controller

    def execute(self, text: str, via: str = "udp", sender: str | None = None) -> list:
        replies = []
        for line in split_commands(text):
            try:
                replies.append(self.run_one(line, via=via, sender=sender))
            except CommandError as exc:
                replies.append(f"ERR {line}: {exc}")
                self.controller.audit.warning("Command rejected", command=line, reason=str(exc), via=via, sender=sender)
            except NotConnected as exc:
                replies.append(f"ERR {line}: {exc}")
                self.controller.audit.warning("Command dropped", command=line, reason=str(exc), via=via, sender=sender)
        if not replies:
            replies.append("ERR empty command")
        return replies

    def run_one(self, line: str, via: str = "udp", sender: str | None = None) -> str:
        ctl = self.controller
        s = ctl.settings
        tokens = line.split()
        cmd = tokens[0].upper()
        args = tokens[1:]
        me_count = s.get("me_count")
        input_count = s.get("input_count")
        fps = ctl.fps()

        if cmd == "PING":
            return "PONG"
        if cmd == "HELP":
            return "OK " + " | ".join(HELP)
        if cmd == "STATUS":
            return ctl.status_line()

        if cmd in ("PGM", "PROGRAM"):
            _need(args, 1, "PGM <src> [MEn]")
            me, rest = _extract_me(args, me_count)
            src = parse_source(rest[0], input_count)
            acked = ctl.program(src, me, via=via, sender=sender)
            return _reply(acked, f"PGM {rest[0].upper()} ME{me + 1}")

        if cmd in ("PVW", "PREVIEW"):
            _need(args, 1, "PVW <src> [MEn]")
            me, rest = _extract_me(args, me_count)
            src = parse_source(rest[0], input_count)
            acked = ctl.preview(src, me, via=via, sender=sender)
            return _reply(acked, f"PVW {rest[0].upper()} ME{me + 1}")

        if cmd == "CUT":
            me, _ = _extract_me(args, me_count, bare_number_is_me=True)
            return _reply(ctl.cut(me, via=via, sender=sender), f"CUT ME{me + 1}")

        if cmd == "AUTO":
            me, _ = _extract_me(args, me_count, bare_number_is_me=True)
            return _reply(ctl.auto(me, via=via, sender=sender), f"AUTO ME{me + 1}")

        if cmd in ("FADE", "MIX"):
            _need(args, 1, "FADE <src> [rate] [MEn]")
            me, rest = _extract_me(args, me_count)
            src = parse_source(rest[0], input_count)
            frames = parse_rate(rest[1], fps) if len(rest) > 1 else None
            acked = ctl.fade(src, me, frames, via=via, sender=sender)
            return _reply(acked, f"FADE {rest[0].upper()} {frames or 'current-rate'} ME{me + 1}")

        if cmd == "STYLE":
            _need(args, 1, "STYLE <mix|dip|wipe|dve|sting> [MEn]")
            me, rest = _extract_me(args, me_count)
            style = STYLES.get(rest[0].upper())
            if style is None:
                raise CommandError(f"unknown transition style '{rest[0]}'")
            acked = ctl.style(style, me, via=via, sender=sender)
            return _reply(acked, f"STYLE {STYLE_NAMES[style].upper()} ME{me + 1}")

        if cmd == "RATE":
            _need(args, 1, "RATE <rate> [MEn]")
            me, rest = _extract_me(args, me_count)
            frames = parse_rate(rest[0], fps)
            return _reply(ctl.rate(frames, me, via=via, sender=sender), f"RATE {frames} ME{me + 1}")

        if cmd == "FTB":
            me, _ = _extract_me(args, me_count, bare_number_is_me=True)
            return _reply(ctl.ftb(me, via=via, sender=sender), f"FTB ME{me + 1}")

        if cmd == "FTBRATE":
            _need(args, 1, "FTBRATE <rate> [MEn]")
            me, rest = _extract_me(args, me_count)
            frames = parse_rate(rest[0], fps)
            return _reply(ctl.ftb_rate(frames, me, via=via, sender=sender), f"FTBRATE {frames} ME{me + 1}")

        if cmd == "AUX":
            _need(args, 2, "AUX <n> <src>")
            aux = _parse_index(args[0], "AUX", s.get("aux_count"))
            src = parse_source(args[1], input_count)
            return _reply(ctl.aux(aux, src, via=via, sender=sender), f"AUX {aux + 1} {args[1].upper()}")

        if cmd == "DSK":
            _need(args, 2, "DSK <n> <on|off|auto|toggle>")
            idx = _parse_index(args[0], "DSK", s.get("dsk_count"))
            action = args[1].upper()
            if action not in ("ON", "OFF", "AUTO", "TOGGLE"):
                raise CommandError("DSK action must be on, off, auto or toggle")
            return _reply(ctl.dsk(idx, action.lower(), via=via, sender=sender), f"DSK {idx + 1} {action}")

        if cmd == "KEY":
            _need(args, 2, "KEY <n> <on|off> [MEn]")
            me, rest = _extract_me(args, me_count)
            idx = _parse_index(rest[0], "KEY", 4)
            action = rest[1].upper()
            if action not in ("ON", "OFF"):
                raise CommandError("KEY action must be on or off")
            return _reply(ctl.key(idx, action == "ON", me, via=via, sender=sender), f"KEY {idx + 1} {action} ME{me + 1}")

        if cmd == "MACRO":
            _need(args, 1, "MACRO <n>")
            idx = _parse_index(args[0], "MACRO", s.get("macro_count"))
            return _reply(ctl.macro(idx, via=via, sender=sender), f"MACRO {idx + 1}")

        if cmd == "RAW":
            _need(args, 1, "RAW <NAME> <hex>")
            name = args[0]
            if len(name) != 4:
                raise CommandError("raw command name must be exactly 4 characters")
            hexstr = "".join(args[1:]).replace(":", "").replace("-", "")
            try:
                payload = bytes.fromhex(hexstr)
            except ValueError:
                raise CommandError("payload must be hex bytes, e.g. 00 00 00 01")
            return _reply(ctl.raw(name, payload, via=via, sender=sender), f"RAW {name} {payload.hex()}")

        raise CommandError(f"unknown command '{cmd}' (send HELP for the list)")


def _reply(acked: bool, text: str) -> str:
    return f"OK {text}" if acked else f"ERR {text}: switcher did not acknowledge"
