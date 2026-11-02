#!/usr/bin/env python3
"""
A small ATEM switcher simulator for testing Setter without hardware.

Speaks enough of the ATEM UDP protocol - the hello handshake, acks,
keepalive pings, the initial state dump and the commands Setter sends -
to let the dashboard and the UDP control path be exercised end to end.
Prints every command it receives. One client at a time, state in memory.

    python tools/fake_atem.py                      # 0.0.0.0:9910, 8 inputs, 25 fps
    python tools/fake_atem.py --port 9910 --inputs 20 --fps 29.97 --mes 2

Then point Setter at 127.0.0.1 (or this machine's IP) in Settings.
"""
from __future__ import annotations

import argparse
import random
import socket
import struct
import sys
import time

FLAG_ACK_REQUEST, FLAG_HELLO, FLAG_RESEND, FLAG_RETRANSMIT, FLAG_ACK = 1, 2, 4, 8, 16

VIDEO_MODE_FOR_FPS = {23.98: 8, 24: 9, 25: 10, 29.97: 11, 30: 26, 50: 12, 59.94: 13, 60: 27}


def header(flags, length, session, ack_id=0, packet_id=0):
    return struct.pack(">HHHHHH", (flags << 11) | length, session, ack_id, 0, 0, packet_id)


def cmd(name, payload):
    return struct.pack(">HH", 8 + len(payload), 0) + name.encode("ascii") + payload


def cstr(text, size):
    """Fixed-width string field: NUL-padded, and like the real hardware not
    NUL-terminated when the text fills the whole field (short names are 4/4)."""
    b = text.encode("utf-8")[:size]
    return b + b"\x00" * (size - len(b))


class FakeAtem:
    def __init__(self, host, port, inputs, fps, mes, aux, name):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.05)
        self.fps = fps
        self.name = name
        self.mes = mes
        self.aux_count = aux
        self.inputs = {0: ("Black", "Blk")}
        for n in range(1, inputs + 1):
            self.inputs[n] = (f"Camera {n}", f"Cam{n}")
        self.inputs.update({
            1000: ("Color Bars", "Bars"), 2001: ("Color 1", "Col1"), 2002: ("Color 2", "Col2"),
            3010: ("Media Player 1", "MP1"), 3011: ("Media Player 1 Key", "MP1K"),
            3020: ("Media Player 2", "MP2"), 3021: ("Media Player 2 Key", "MP2K"),
            7001: ("Clean Feed 1", "Cfd1"), 7002: ("Clean Feed 2", "Cfd2"),
            10010: ("Program", "Pgm"), 10011: ("Preview", "Pvw"),
        })
        for a in range(aux):
            self.inputs[8001 + a] = (f"Auxiliary {a + 1}", f"Aux{a + 1}")
        self.program = {m: 1 for m in range(mes)}
        self.preview = {m: 2 for m in range(mes)}
        self.aux = {a: 1 for a in range(aux)}
        self.style = {m: 0 for m in range(mes)}
        self.rate = {m: int(round(fps)) for m in range(mes)}
        self.ftb_rate = {m: int(round(fps)) for m in range(mes)}
        self.ftb_black = {m: False for m in range(mes)}
        self.ftb_until = {}
        self.dsk = {0: False, 1: False}
        self.dsk_until = {}
        self.transition_until = {}
        self.macros = {0: "Opening", 1: "Interval", 2: "Walk-in loop"}
        self.client = None
        self.last_ping = 0.0

    # ------------------------------------------------------------------ io
    def log(self, msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    def send_raw(self, packet):
        if self.client:
            self.sock.sendto(packet, self.client["addr"])

    def send_commands(self, cmds):
        """Pack commands into ack-request packets (keeping each under ~1400 bytes)."""
        if not self.client:
            return
        chunks, current = [], b""
        for c in cmds:
            if len(current) + len(c) > 1400:
                chunks.append(current)
                current = b""
            current += c
        if current or not chunks:
            chunks.append(current)
        for body in chunks:
            self.client["pkt_id"] = (self.client["pkt_id"] + 1) % 0x8000 or 1
            self.send_raw(header(FLAG_ACK_REQUEST, 12 + len(body), self.client["session"],
                                 packet_id=self.client["pkt_id"]) + body)

    # ------------------------------------------------------------------ state
    def state_dump(self):
        cmds = [
            cmd("_ver", struct.pack(">HH", 2, 30)),
            cmd("_pin", cstr(self.name, 44)),
            cmd("_top", bytes([self.mes, len(self.inputs), 2, self.aux_count, 0, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])),
            cmd("VidM", bytes([VIDEO_MODE_FOR_FPS.get(self.fps, 10), 0, 0, 0])),
        ]
        for sid, (long_name, short_name) in sorted(self.inputs.items()):
            cmds.append(cmd("InPr", struct.pack(">H", sid) + cstr(long_name, 20) + cstr(short_name, 4) + bytes(10)))
        for m in range(self.mes):
            cmds += self.me_state(m)
            cmds.append(cmd("FtbP", bytes([m, self.ftb_rate[m], 0, 0])))
        for a in range(self.aux_count):
            cmds.append(self.aux_state(a))
        for i in range(2):
            cmds.append(self.dsk_state(i))
        for idx, name in self.macros.items():
            n = cstr(name, len(name) + 1)
            cmds.append(cmd("MPrp", struct.pack(">HBBHH", idx, 1, 0, len(n), 1) + n + b"\x00"))
        cmds.append(cmd("InCm", bytes([1, 0, 0, 0])))
        return cmds

    def me_state(self, m):
        return [
            cmd("PrgI", struct.pack(">BBH", m, 0, self.program[m])),
            cmd("PrvI", struct.pack(">BBH", m, 0, self.preview[m]) + bytes([0, 0, 0, 0])),
            cmd("TrSS", bytes([m, self.style[m], 1, self.style[m], 1, 0, 0, 0])),
            cmd("TMxP", bytes([m, self.rate[m], 0, 0])),
            self.trans_state(m),
            self.ftb_state(m),
        ]

    def trans_state(self, m):
        active = m in self.transition_until
        remaining = max(0, int((self.transition_until.get(m, 0) - time.monotonic()) * self.fps)) if active else 0
        return cmd("TrPs", bytes([m, 1 if active else 0, remaining, 0]) + struct.pack(">H", 0 if not active else 5000) + bytes(2))

    def ftb_state(self, m):
        busy = m in self.ftb_until
        return cmd("FtbS", bytes([m, 1 if self.ftb_black[m] and not busy else 0, 1 if busy else 0, 0]))

    def aux_state(self, a):
        return cmd("AuxS", struct.pack(">BBH", a, 0, self.aux[a]))

    def dsk_state(self, i):
        busy = i in self.dsk_until
        return cmd("DskS", bytes([i, 1 if self.dsk[i] else 0, 1 if busy else 0, 1 if busy else 0, 0, 0, 0, 0]))

    # ------------------------------------------------------------------ commands from client
    def apply(self, name, d):
        updates = []
        src = lambda off: struct.unpack_from(">H", d, off)[0]
        if name == "CPgI":
            self.program[d[0]] = src(2); updates += [cmd("PrgI", struct.pack(">BBH", d[0], 0, src(2)))]
            self.log(f"  program ME{d[0] + 1} -> {self.label(src(2))}")
        elif name == "CPvI":
            self.preview[d[0]] = src(2); updates += [cmd("PrvI", struct.pack(">BBH", d[0], 0, src(2)) + bytes(4))]
            self.log(f"  preview ME{d[0] + 1} -> {self.label(src(2))}")
        elif name == "DCut":
            m = d[0]; self.program[m], self.preview[m] = self.preview[m], self.program[m]
            updates += self.me_state(m)
            self.log(f"  cut ME{m + 1}: program is now {self.label(self.program[m])}")
        elif name == "DAut":
            m = d[0]
            if m not in self.transition_until:
                self.transition_until[m] = time.monotonic() + self.rate[m] / self.fps
                updates.append(self.trans_state(m))
                self.log(f"  auto ME{m + 1}: {self.rate[m]} frames to {self.label(self.preview[m])}")
        elif name == "CTTp":
            if d[0] & 1:
                self.style[d[1]] = d[2]
                updates.append(cmd("TrSS", bytes([d[1], d[2], 1, d[2], 1, 0, 0, 0])))
                self.log(f"  style ME{d[1] + 1} -> {d[2]}")
        elif name == "CTMx":
            self.rate[d[0]] = d[1]; updates.append(cmd("TMxP", bytes([d[0], d[1], 0, 0])))
            self.log(f"  mix rate ME{d[0] + 1} -> {d[1]} frames")
        elif name == "FtbA":
            m = d[0]
            self.ftb_until[m] = time.monotonic() + self.ftb_rate[m] / self.fps
            updates.append(self.ftb_state(m))
            self.log(f"  fade to black ME{m + 1}: {'fading up' if self.ftb_black[m] else 'fading down'}")
        elif name == "FtbC":
            self.ftb_rate[d[1]] = d[2]; updates.append(cmd("FtbP", bytes([d[1], d[2], 0, 0])))
            self.log(f"  ftb rate ME{d[1] + 1} -> {d[2]} frames")
        elif name == "CAuS":
            self.aux[d[1]] = src(2); updates.append(self.aux_state(d[1]))
            self.log(f"  aux {d[1] + 1} -> {self.label(src(2))}")
        elif name == "CDsL":
            self.dsk[d[0]] = bool(d[1]); updates.append(self.dsk_state(d[0]))
            self.log(f"  dsk {d[0] + 1} -> {'on' if d[1] else 'off'}")
        elif name == "DDsA":
            i = d[0]
            if i not in self.dsk_until:
                self.dsk_until[i] = time.monotonic() + self.rate.get(0, 25) / self.fps
                updates.append(self.dsk_state(i))
                self.log(f"  dsk {i + 1} auto")
        elif name == "CKOn":
            updates.append(cmd("KeOn", bytes([d[0], d[1], d[2], 0])))
            self.log(f"  key {d[1] + 1} ME{d[0] + 1} -> {'on' if d[2] else 'off'}")
        elif name == "MAct":
            idx = struct.unpack_from(">H", d, 0)[0]
            self.log(f"  macro {idx + 1} action {d[2]}")
        else:
            self.log(f"  (ignored {name} {d.hex()})")
        return updates

    def label(self, sid):
        return self.inputs.get(sid, (str(sid),))[0]

    def tick(self):
        now = time.monotonic()
        if not self.client:
            return
        if now - self.client["last_seen"] > 5.0:
            self.log(f"client {self.client['addr']} timed out")
            self.client = None
            return
        updates = []
        for m, until in list(self.transition_until.items()):
            if now >= until:
                del self.transition_until[m]
                self.program[m], self.preview[m] = self.preview[m], self.program[m]
                updates += self.me_state(m)
                self.log(f"  transition done ME{m + 1}: program is now {self.label(self.program[m])}")
        for m, until in list(self.ftb_until.items()):
            if now >= until:
                del self.ftb_until[m]
                self.ftb_black[m] = not self.ftb_black[m]
                updates.append(self.ftb_state(m))
        for i, until in list(self.dsk_until.items()):
            if now >= until:
                del self.dsk_until[i]
                self.dsk[i] = not self.dsk[i]
                updates.append(self.dsk_state(i))
        if updates:
            self.send_commands(updates)
        if now - self.last_ping > 1.0:
            self.last_ping = now
            self.send_commands([])  # empty ack-request packet = keepalive

    def handle(self, data, addr):
        if len(data) < 12:
            return
        word, session, ack_id, _, _, remote_id = struct.unpack_from(">HHHHHH", data, 0)
        flags, length = word >> 11, word & 0x7FF
        now = time.monotonic()
        if flags & FLAG_HELLO:
            self.client = {"addr": addr, "session": random.randint(0x8001, 0x8FFF), "pkt_id": 0, "last_seen": now}
            self.log(f"hello from {addr}, session {self.client['session']:#06x}")
            self.sock.sendto(header(FLAG_HELLO, 20, session) + bytes([2, 0, 0, 0, 0, 0, 0, 0]), addr)
            self.send_commands(self.state_dump())
            return
        if not self.client or addr != self.client["addr"]:
            return
        self.client["last_seen"] = now
        if flags & FLAG_ACK_REQUEST:
            self.send_raw(header(FLAG_ACK, 12, self.client["session"], ack_id=remote_id))
            if length > 12:
                self.log(f"packet {remote_id} from client" + (" (resend)" if flags & FLAG_RESEND else ""))
                updates = []
                off = 12
                while off + 8 <= length:
                    clen = struct.unpack_from(">H", data, off)[0]
                    if clen < 8:
                        break
                    name = data[off + 4:off + 8].decode("ascii", "replace")
                    updates += self.apply(name, data[off + 8:off + clen])
                    off += clen
                if updates:
                    self.send_commands(updates)

    def run(self):
        self.log(f"fake ATEM '{self.name}' listening on {self.sock.getsockname()}  ({len([i for i in self.inputs if 0 < i < 1000])} inputs, {self.fps} fps)")
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
                self.handle(data, addr)
            except socket.timeout:
                pass
            self.tick()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9910)
    ap.add_argument("--inputs", type=int, default=8)
    ap.add_argument("--fps", type=float, default=25)
    ap.add_argument("--mes", type=int, default=1)
    ap.add_argument("--aux", type=int, default=3)
    ap.add_argument("--name", default="ATEM Fake 1 M/E")
    args = ap.parse_args()
    try:
        FakeAtem(args.host, args.port, args.inputs, args.fps, args.mes, args.aux, args.name).run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
