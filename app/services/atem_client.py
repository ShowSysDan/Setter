"""
Pure-Python client for the Blackmagic ATEM UDP protocol (port 9910).

Handles the hello handshake, acknowledgements, retransmits and the initial
state dump, keeps a small live state model, and exposes the handful of
commands Setter needs. No third-party dependencies.

Packet header (12 bytes, big-endian):

    0-1   flags (upper 5 bits) + packet length (lower 11 bits)
    2-3   session id
    4-5   ack id        - id of the switcher packet this packet acknowledges
    6-7   retransmit id - "resend everything after this id" (switcher -> us)
    8-9   reserved
    10-11 packet id     - our own sequence number for packets that need an ack

Each command inside a packet is: uint16 length, 2 bytes padding, 4-char name,
then the payload (length includes the 8-byte command header).
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("setter.atem")

# Flag bits (the value is shifted left 11 bits into the first word)
FLAG_ACK_REQUEST = 0x01
FLAG_HELLO = 0x02
FLAG_RESEND = 0x04
FLAG_RETRANSMIT_REQUEST = 0x08
FLAG_ACK = 0x10

HELLO_SESSION = 0x53AB  # arbitrary client-chosen id used by the official software
MAX_PACKET_ID = 0x8000

HELLO_TIMEOUT = 3.0       # seconds to wait for the switcher's hello reply
RX_TIMEOUT = 5.0          # no packets for this long -> treat as disconnected
RECONNECT_DELAY = 2.0     # pause between reconnect attempts
RESEND_AFTER = 0.4        # resend an unacknowledged command after this long
MAX_ATTEMPTS = 5

# Transition styles for CTTp / TrSS
STYLE_MIX, STYLE_DIP, STYLE_WIPE, STYLE_DVE, STYLE_STINGER = 0, 1, 2, 3, 4
STYLE_NAMES = {0: "mix", 1: "dip", 2: "wipe", 3: "dve", 4: "stinger"}

# VidM video mode id -> (name, frames per second as the switcher counts them)
VIDEO_MODES = {
    0: ("525i59.94 NTSC", 29.97), 1: ("625i50 PAL", 25.0),
    2: ("525i59.94 NTSC 16:9", 29.97), 3: ("625i50 PAL 16:9", 25.0),
    4: ("720p50", 50.0), 5: ("720p59.94", 59.94),
    6: ("1080i50", 25.0), 7: ("1080i59.94", 29.97),
    8: ("1080p23.98", 23.98), 9: ("1080p24", 24.0), 10: ("1080p25", 25.0),
    11: ("1080p29.97", 29.97), 12: ("1080p50", 50.0), 13: ("1080p59.94", 59.94),
    14: ("2160p23.98", 23.98), 15: ("2160p24", 24.0), 16: ("2160p25", 25.0),
    17: ("2160p29.97", 29.97), 18: ("2160p50", 50.0), 19: ("2160p59.94", 59.94),
    26: ("1080p30", 30.0), 27: ("1080p60", 60.0), 28: ("2160p30", 30.0), 29: ("2160p60", 60.0),
}


class NotConnected(RuntimeError):
    """Raised when a command is attempted without a live switcher session."""


def _cstr(data: bytes) -> str:
    """Decode a NUL-terminated string field."""
    return data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def build_header(flags: int, length: int, session: int, ack_id: int = 0,
                 retransmit_id: int = 0, packet_id: int = 0) -> bytes:
    return struct.pack(">HHHHHH", (flags << 11) | (length & 0x7FF), session,
                       ack_id, retransmit_id, 0, packet_id)


def build_command(name: str, payload: bytes) -> bytes:
    if len(name) != 4:
        raise ValueError("command name must be 4 characters")
    return struct.pack(">HH", 8 + len(payload), 0) + name.encode("ascii") + payload


def build_hello() -> bytes:
    buf = bytearray(build_header(FLAG_HELLO, 20, HELLO_SESSION)) + bytearray(8)
    buf[9] = 0x3A   # as sent by the official client
    buf[12] = 0x01  # "connection request"
    return bytes(buf)


class AtemState:
    """Live model of the parts of the switcher Setter cares about."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.product = None
        self.protocol_version = None
        self.topology = {}
        self.video_mode = None
        self.inputs = {}          # source id -> {"id", "long_name", "short_name"}
        self.program = {}         # me index -> source id
        self.preview = {}
        self.aux = {}             # aux index -> source id
        self.transition = {}      # me index -> {style, selection, in_transition, frames_remaining, position}
        self.mix_rate = {}        # me index -> frames
        self.ftb = {}             # me index -> {full_black, in_transition, frames_remaining, rate}
        self.dsk = {}             # dsk index -> {on_air, in_transition, is_auto, frames_remaining}
        self.keyers = {}          # "me:key" -> on_air
        self.tally = {}           # source id -> {program, preview}
        self.macros = {}          # index -> {name, is_used}
        self.timecode = None
        self.initialized = False  # True once the switcher has sent InCm

    @property
    def fps(self) -> float:
        return VIDEO_MODES.get(self.video_mode, ("", 25.0))[1]

    @property
    def video_mode_name(self) -> str:
        if self.video_mode is None:
            return ""
        return VIDEO_MODES.get(self.video_mode, (f"mode {self.video_mode}", 25.0))[0]

    def source_name(self, source_id: int) -> str:
        inp = self.inputs.get(source_id)
        return inp["short_name"] if inp and inp["short_name"] else str(source_id)

    def snapshot(self) -> dict:
        return {
            "product": self.product,
            "protocol_version": self.protocol_version,
            "topology": dict(self.topology),
            "video_mode": {"id": self.video_mode, "name": self.video_mode_name, "fps": self.fps},
            "inputs": {str(k): v for k, v in self.inputs.items()},
            "program": {str(k): v for k, v in self.program.items()},
            "preview": {str(k): v for k, v in self.preview.items()},
            "aux": {str(k): v for k, v in self.aux.items()},
            "transition": {str(k): v for k, v in self.transition.items()},
            "mix_rate": {str(k): v for k, v in self.mix_rate.items()},
            "ftb": {str(k): v for k, v in self.ftb.items()},
            "dsk": {str(k): v for k, v in self.dsk.items()},
            "keyers": dict(self.keyers),
            "macros": {str(k): v for k, v in self.macros.items()},
            "timecode": self.timecode,
            "initialized": self.initialized,
        }


class AtemClient:
    """
    Background-threaded ATEM connection.

    on_event(level, message) is called for connect / disconnect / warnings so
    the application can log them; level is a logging level number.
    """

    def __init__(self, host: str, port: int = 9910, on_event=None):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.state = AtemState()
        self.connected = False      # handshake complete
        self.last_error = None
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._session = HELLO_SESSION
        self._local_id = 0
        self._inflight = {}         # packet id -> dict(body, sent, attempts, event)
        self._last_rx = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atem-client", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._set_connected(False)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _emit(self, level, message):
        if self.on_event:
            try:
                self.on_event(level, message)
            except Exception:  # never let a logging callback kill the loop
                log.exception("on_event callback failed")
        else:
            log.log(level, message)

    def _set_connected(self, value: bool):
        if value == self.connected:
            return
        self.connected = value
        if not value:
            self.state.initialized = False

    # ------------------------------------------------------------------ main loop
    def _run(self):
        while not self._stop.is_set():
            try:
                self._session_loop()
            except (OSError, TimeoutError, ConnectionError, NotConnected) as exc:
                if self._stop.is_set():
                    break
                self.last_error = str(exc) or exc.__class__.__name__
                if self.connected:
                    self._emit(logging.WARNING, f"Lost connection to {self.host}: {self.last_error}")
                else:
                    self._emit(logging.INFO, f"Could not reach {self.host}:{self.port} ({self.last_error}), retrying")
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("atem client loop crashed")
                self.last_error = str(exc)
            finally:
                self._set_connected(False)
                sock, self._sock = self._sock, None
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._stop.wait(RECONNECT_DELAY)

    def _session_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect((self.host, self.port))
        self._sock = sock
        self._session = HELLO_SESSION
        self._local_id = 0
        self._inflight.clear()
        self.state.reset()

        sock.send(build_hello())
        hello_sent = time.monotonic()
        self._last_rx = hello_sent

        while not self._stop.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                data = None
            now = time.monotonic()
            if data:
                self._handle_packet(data, now)
            if not self.connected and now - hello_sent > HELLO_TIMEOUT:
                raise TimeoutError("no reply to hello")
            if self.connected and now - self._last_rx > RX_TIMEOUT:
                raise TimeoutError("switcher stopped responding")
            self._service_inflight(now)

    # ------------------------------------------------------------------ receive
    def _handle_packet(self, data: bytes, now: float):
        if len(data) < 12:
            return
        word, session, ack_id, retransmit_id, _, remote_id = struct.unpack_from(">HHHHHH", data, 0)
        flags = word >> 11
        length = word & 0x7FF
        if length != len(data):
            log.debug("length mismatch: header says %d, got %d", length, len(data))
            length = min(length, len(data))
        self._last_rx = now
        self._session = session  # the switcher assigns the real id after hello; adopt whatever it uses

        if flags & FLAG_HELLO:
            status = data[12] if len(data) > 12 else 0
            if status == 3:
                raise ConnectionError("switcher refused the connection (no free client slots)")
            self._send_ack(remote_id)
            self._set_connected(True)
            self.last_error = None
            self._emit(logging.INFO, f"Connected to {self.host}:{self.port}, loading switcher state")
            return

        if flags & FLAG_ACK_REQUEST:
            self._send_ack(remote_id)
            if length > 12:
                self._parse_commands(data[12:length])

        if flags & FLAG_ACK:
            entry = self._inflight.pop(ack_id, None)
            if entry:
                entry["event"].set()

        if flags & FLAG_RETRANSMIT_REQUEST:
            for pid in sorted(self._inflight):
                if pid > retransmit_id:
                    self._send_inflight(pid, now, resend=True)

    def _parse_commands(self, payload: bytes):
        offset = 0
        total = len(payload)
        while offset + 8 <= total:
            length = struct.unpack_from(">H", payload, offset)[0]
            if length < 8 or offset + length > total:
                break
            name = payload[offset + 4:offset + 8].decode("ascii", "replace")
            self._apply(name, payload[offset + 8:offset + length])
            offset += length

    def _apply(self, name: str, d: bytes):
        st = self.state
        try:
            if name == "_ver":
                major, minor = struct.unpack_from(">HH", d, 0)
                st.protocol_version = f"{major}.{minor}"
            elif name == "_pin":
                st.product = _cstr(d)
            elif name == "_top":
                st.topology = {
                    "mes": d[0], "sources": d[1], "dsks": d[2], "aux": d[3],
                    "mix_minus": d[4], "media_players": d[5],
                }
            elif name == "VidM":
                st.video_mode = d[0]
            elif name == "InPr":
                sid = struct.unpack_from(">H", d, 0)[0]
                st.inputs[sid] = {"id": sid, "long_name": _cstr(d[2:22]), "short_name": _cstr(d[22:26])}
            elif name == "PrgI":
                st.program[d[0]] = struct.unpack_from(">H", d, 2)[0]
            elif name == "PrvI":
                st.preview[d[0]] = struct.unpack_from(">H", d, 2)[0]
            elif name == "AuxS":
                st.aux[d[0]] = struct.unpack_from(">H", d, 2)[0]
            elif name == "TrSS":
                st.transition.setdefault(d[0], {}).update(
                    style=d[1], selection=d[2], next_style=d[3], next_selection=d[4])
            elif name == "TrPs":
                st.transition.setdefault(d[0], {}).update(
                    in_transition=bool(d[1]), frames_remaining=d[2],
                    position=struct.unpack_from(">H", d, 4)[0])
            elif name == "TMxP":
                st.mix_rate[d[0]] = d[1]
            elif name == "FtbS":
                st.ftb.setdefault(d[0], {}).update(
                    full_black=bool(d[1]), in_transition=bool(d[2]), frames_remaining=d[3])
            elif name == "FtbP":
                st.ftb.setdefault(d[0], {})["rate"] = d[1]
            elif name == "DskS":
                st.dsk[d[0]] = {"on_air": bool(d[1]), "in_transition": bool(d[2]),
                                "is_auto": bool(d[3]), "frames_remaining": d[4]}
            elif name == "KeOn":
                st.keyers[f"{d[0]}:{d[1]}"] = bool(d[2])
            elif name == "TlSr":
                count = struct.unpack_from(">H", d, 0)[0]
                for i in range(count):
                    sid = struct.unpack_from(">H", d, 2 + 3 * i)[0]
                    f = d[4 + 3 * i]
                    st.tally[sid] = {"program": bool(f & 1), "preview": bool(f & 2)}
            elif name == "MPrp":
                idx = struct.unpack_from(">H", d, 0)[0]
                name_len = struct.unpack_from(">H", d, 4)[0]
                st.macros[idx] = {"index": idx, "is_used": d[2] == 1,
                                  "name": _cstr(d[8:8 + name_len])}
            elif name == "Time":
                st.timecode = "%02d:%02d:%02d:%02d" % (d[0], d[1], d[2], d[3])
            elif name == "InCm":
                st.initialized = True
                self._emit(logging.INFO, f"Switcher state loaded: {st.product or 'ATEM'} "
                                         f"({st.video_mode_name or 'unknown video mode'})")
        except (IndexError, struct.error) as exc:
            log.debug("could not parse %s (%d bytes): %s", name, len(d), exc)

    # ------------------------------------------------------------------ send
    def _raw_send(self, packet: bytes):
        sock = self._sock
        if not sock:
            raise NotConnected("not connected to switcher")
        with self._send_lock:
            sock.send(packet)

    def _send_ack(self, remote_id: int):
        self._raw_send(build_header(FLAG_ACK, 12, self._session, ack_id=remote_id))

    def _send_inflight(self, pid: int, now: float, resend: bool = False):
        entry = self._inflight.get(pid)
        if not entry:
            return
        flags = FLAG_ACK_REQUEST | (FLAG_RESEND if resend else 0)
        body = entry["body"]
        packet = build_header(flags, 12 + len(body), self._session, packet_id=pid) + body
        entry["sent"] = now
        entry["attempts"] += 1
        self._raw_send(packet)

    def _service_inflight(self, now: float):
        for pid, entry in list(self._inflight.items()):
            if now - entry["sent"] < RESEND_AFTER:
                continue
            if entry["attempts"] >= MAX_ATTEMPTS:
                self._inflight.pop(pid, None)
                self._emit(logging.WARNING, f"Switcher did not acknowledge packet {pid}")
                continue
            self._send_inflight(pid, now, resend=True)

    def send_commands(self, commands, wait: float = 0.5) -> bool:
        """
        Send one or more (name, payload) commands in a single packet.
        Returns True if the switcher acknowledged it within `wait` seconds.
        """
        if not self.connected or not self._sock:
            raise NotConnected("not connected to switcher")
        body = b"".join(build_command(n, p) for n, p in commands)
        with self._send_lock:
            self._local_id = (self._local_id + 1) % MAX_PACKET_ID or 1
            pid = self._local_id
            event = threading.Event()
            self._inflight[pid] = {"body": body, "sent": time.monotonic(), "attempts": 0, "event": event}
        self._send_inflight(pid, time.monotonic())
        if wait <= 0:
            return True
        return event.wait(wait)

    def send(self, name: str, payload: bytes, wait: float = 0.5) -> bool:
        return self.send_commands([(name, payload)], wait=wait)

    # ------------------------------------------------------------------ switcher commands
    def set_program(self, me: int, source: int) -> bool:
        return self.send("CPgI", struct.pack(">BBH", me, 0, source))

    def set_preview(self, me: int, source: int) -> bool:
        return self.send("CPvI", struct.pack(">BBH", me, 0, source))

    def cut(self, me: int) -> bool:
        return self.send("DCut", bytes([me, 0, 0, 0]))

    def auto(self, me: int) -> bool:
        return self.send("DAut", bytes([me, 0, 0, 0]))

    def set_transition_style(self, me: int, style: int) -> bool:
        return self.send("CTTp", bytes([0x01, me, style, 0]))

    def set_mix_rate(self, me: int, frames: int) -> bool:
        return self.send("CTMx", bytes([me, frames, 0, 0]))

    def fade_to(self, me: int, source: int, frames: int | None = None) -> bool:
        """Preview the source, force a Mix transition, optionally set the rate, then auto."""
        cmds = [("CPvI", struct.pack(">BBH", me, 0, source)),
                ("CTTp", bytes([0x01, me, STYLE_MIX, 0]))]
        if frames:
            cmds.append(("CTMx", bytes([me, frames, 0, 0])))
        cmds.append(("DAut", bytes([me, 0, 0, 0])))
        return self.send_commands(cmds)

    def fade_to_black(self, me: int) -> bool:
        return self.send("FtbA", bytes([me, 0, 0, 0]))

    def set_ftb_rate(self, me: int, frames: int) -> bool:
        return self.send("FtbC", bytes([0x01, me, frames, 0]))

    def set_aux(self, aux: int, source: int) -> bool:
        return self.send("CAuS", struct.pack(">BBH", 0x01, aux, source))

    def set_dsk_on_air(self, index: int, on_air: bool) -> bool:
        return self.send("CDsL", bytes([index, 1 if on_air else 0, 0, 0]))

    def dsk_auto(self, index: int) -> bool:
        return self.send("DDsA", bytes([index, 0, 0, 0]))

    def set_keyer_on_air(self, me: int, keyer: int, on_air: bool) -> bool:
        return self.send("CKOn", bytes([me, keyer, 1 if on_air else 0, 0]))

    def run_macro(self, index: int) -> bool:
        return self.send("MAct", struct.pack(">HBB", index, 0, 0))

    def raw(self, name: str, payload: bytes) -> bool:
        return self.send(name, payload)
