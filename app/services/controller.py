"""
The Controller ties everything together: it owns the ATEM client and the UDP
listener, applies settings changes (reconnecting as needed), performs the
high-level actions the routes and the command parser call, and builds the
status document the dashboard polls.
"""
from __future__ import annotations

import logging
import threading

from .atem_client import AtemClient, NotConnected, STYLE_NAMES
from .command_parser import CommandRunner, SOURCE_ALIASES
from .settings import parse_networks
from .udp_listener import UdpListener

# Internal sources shown on the dashboard buses, with the token the UI sends
INTERNAL_SOURCES = [
    (0, "BLACK", "Black"), (1000, "BARS", "Bars"),
    (2001, "COL1", "Col 1"), (2002, "COL2", "Col 2"),
    (3010, "MP1", "MP 1"), (3020, "MP2", "MP 2"),
]


class Controller:
    def __init__(self, settings, audit):
        self.settings = settings
        self.audit = audit
        self.client: AtemClient | None = None
        self.udp: UdpListener | None = None
        self.runner = CommandRunner(self)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self.settings.get("auto_connect"):
            self.connect()
        if self.settings.get("udp_enabled"):
            self.start_udp()

    def stop(self):
        self.disconnect()
        self.stop_udp()
        self.audit.close()

    def configure_syslog(self):
        cfg = self.settings.syslog_config()
        self.audit.configure_remote(**cfg)
        if cfg["enabled"]:
            self.audit.info("Remote syslog target set", host=cfg["host"], port=cfg["port"],
                            protocol=cfg["protocol"], facility=cfg["facility"])

    def connect(self):
        with self._lock:
            if self.client and self.client.running:
                return
            host = self.settings.get("switcher_ip")
            port = self.settings.get("switcher_port")
            self.audit.info("Connecting to switcher", host=host, port=port)
            self.client = AtemClient(host, port, on_event=self._client_event)
            self.client.start()

    def disconnect(self):
        with self._lock:
            client, self.client = self.client, None
        if client:
            client.stop()
            self.audit.info("Disconnected from switcher", host=client.host)

    def reconnect(self):
        self.disconnect()
        self.connect()

    def start_udp(self):
        with self._lock:
            if self.udp and self.udp.running:
                return
            host = self.settings.get("udp_host")
            port = self.settings.get("udp_port")
            allowed = parse_networks(self.settings.get("udp_allowed_senders"))
            self.udp = UdpListener(host, port, self._udp_handler, reply=self.settings.get("udp_reply"),
                                   allowed=allowed, on_refused=self._udp_refused)
            try:
                self.udp.start()
                self.audit.info("UDP control listener started", host=host, port=port,
                                allowed_senders=self.settings.get("udp_allowed_senders") or "any")
            except OSError as exc:
                self.udp.error = str(exc)
                self.audit.error("UDP control listener could not start", host=host, port=port, reason=str(exc))

    def stop_udp(self):
        with self._lock:
            udp, self.udp = self.udp, None
        if udp:
            udp.stop()
            self.audit.info("UDP control listener stopped", port=udp.port)

    def apply_settings(self, changed: dict):
        """Called after Settings.update(); restarts whatever the change affects."""
        if not changed:
            return
        self.audit.info("Settings changed", **{k: v for k, v in changed.items()})
        if {"switcher_ip", "switcher_port"} & set(changed):
            if self.client:
                self.reconnect()
        if "auto_connect" in changed:
            if changed["auto_connect"] and not self.client:
                self.connect()
        if {"udp_enabled", "udp_host", "udp_port", "udp_reply", "udp_allowed_senders"} & set(changed):
            self.stop_udp()
            if self.settings.get("udp_enabled"):
                self.start_udp()
        if any(k.startswith("syslog_") for k in changed):
            self.configure_syslog()

    # ------------------------------------------------------------------ callbacks
    def _client_event(self, level, message):
        self.audit.log(level, message, via="switcher")

    def _udp_refused(self, addr):
        self.audit.warning("UDP control string refused: sender not in allowed list",
                           sender=f"{addr[0]}:{addr[1]}", via="udp")

    def _udp_handler(self, text, addr):
        sender = f"{addr[0]}:{addr[1]}"
        self.audit.info("UDP control string received", text=text, sender=sender, via="udp")
        return self.runner.execute(text, via="udp", sender=sender)

    # ------------------------------------------------------------------ helpers
    def _client(self) -> AtemClient:
        client = self.client
        if not client or not client.connected:
            raise NotConnected("not connected to switcher")
        return client

    def fps(self) -> float:
        return self.client.state.fps if self.client else 25.0

    def source_label(self, source_id) -> str:
        if source_id is None:
            return "-"
        if self.client:
            return self.client.state.source_name(source_id)
        return str(source_id)

    def run(self, text: str, via: str = "ui", sender: str | None = None) -> list:
        return self.runner.execute(text, via=via, sender=sender)

    def _log_action(self, message: str, acked: bool, **fields):
        if acked:
            self.audit.info(message, **fields)
        else:
            self.audit.warning(message + " (not acknowledged)", **fields)

    # ------------------------------------------------------------------ actions
    def program(self, source, me=0, via="ui", **meta) -> bool:
        acked = self._client().set_program(me, source)
        self._log_action("Program changed", acked, me=me + 1, input=self.source_label(source), via=via, **meta)
        return acked

    def preview(self, source, me=0, via="ui", **meta) -> bool:
        acked = self._client().set_preview(me, source)
        self._log_action("Preview changed", acked, me=me + 1, input=self.source_label(source), via=via, **meta)
        return acked

    def cut(self, me=0, via="ui", **meta) -> bool:
        acked = self._client().cut(me)
        self._log_action("Cut", acked, me=me + 1, via=via, **meta)
        return acked

    def auto(self, me=0, via="ui", **meta) -> bool:
        acked = self._client().auto(me)
        self._log_action("Auto transition", acked, me=me + 1, via=via, **meta)
        return acked

    def fade(self, source, me=0, frames=None, via="ui", **meta) -> bool:
        acked = self._client().fade_to(me, source, frames)
        self._log_action("Fade", acked, me=me + 1, input=self.source_label(source),
                         frames=frames or "current", via=via, **meta)
        return acked

    def style(self, style, me=0, via="ui", **meta) -> bool:
        acked = self._client().set_transition_style(me, style)
        self._log_action("Transition style set", acked, me=me + 1, style=STYLE_NAMES.get(style, style), via=via, **meta)
        return acked

    def rate(self, frames, me=0, via="ui", **meta) -> bool:
        acked = self._client().set_mix_rate(me, frames)
        self._log_action("Mix rate set", acked, me=me + 1, frames=frames, via=via, **meta)
        return acked

    def ftb(self, me=0, via="ui", **meta) -> bool:
        acked = self._client().fade_to_black(me)
        self._log_action("Fade to black toggled", acked, me=me + 1, via=via, **meta)
        return acked

    def ftb_rate(self, frames, me=0, via="ui", **meta) -> bool:
        acked = self._client().set_ftb_rate(me, frames)
        self._log_action("Fade to black rate set", acked, me=me + 1, frames=frames, via=via, **meta)
        return acked

    def aux(self, aux, source, via="ui", **meta) -> bool:
        acked = self._client().set_aux(aux, source)
        self._log_action("Aux routed", acked, aux=aux + 1, input=self.source_label(source), via=via, **meta)
        return acked

    def dsk(self, index, action, via="ui", **meta) -> bool:
        client = self._client()
        if action == "auto":
            acked = client.dsk_auto(index)
        else:
            if action == "toggle":
                current = client.state.dsk.get(index, {}).get("on_air", False)
                action = "off" if current else "on"
            acked = client.set_dsk_on_air(index, action == "on")
        self._log_action("Downstream key", acked, dsk=index + 1, action=action, via=via, **meta)
        return acked

    def key(self, index, on_air, me=0, via="ui", **meta) -> bool:
        acked = self._client().set_keyer_on_air(me, index, on_air)
        self._log_action("Upstream key", acked, me=me + 1, key=index + 1, action="on" if on_air else "off", via=via, **meta)
        return acked

    def macro(self, index, via="ui", **meta) -> bool:
        acked = self._client().run_macro(index)
        self._log_action("Macro run", acked, macro=index + 1, via=via, **meta)
        return acked

    def raw(self, name, payload: bytes, via="ui", **meta) -> bool:
        acked = self._client().raw(name, payload)
        self._log_action("Raw command sent", acked, command=name, payload=payload.hex(), via=via, **meta)
        return acked

    # ------------------------------------------------------------------ status
    def connection_state(self) -> str:
        if not self.client:
            return "disabled"
        if not self.client.connected:
            return "connecting"
        return "connected" if self.client.state.initialized else "loading"

    def status_line(self) -> str:
        state = self.connection_state()
        if state not in ("connected", "loading"):
            return f"OK {state.upper()}"
        st = self.client.state
        pgm = self.source_label(st.program.get(0))
        pvw = self.source_label(st.preview.get(0))
        return f"OK CONNECTED {st.product or 'ATEM'} PGM={pgm} PVW={pvw}"

    def status(self) -> dict:
        s = self.settings.all()
        client = self.client
        st = client.state if client else None
        inputs = []
        for n in range(1, s["input_count"] + 1):
            info = st.inputs.get(n) if st else None
            inputs.append({
                "id": n, "token": str(n),
                "label": (info["short_name"] if info and info["short_name"] else str(n)),
                "name": (info["long_name"] if info and info["long_name"] else f"Input {n}"),
            })
        internal = []
        for sid, token, label in INTERNAL_SOURCES:
            info = st.inputs.get(sid) if st else None
            internal.append({"id": sid, "token": token,
                             "label": info["short_name"] if info and info["short_name"] else label,
                             "name": info["long_name"] if info and info["long_name"] else label})
        udp = self.udp
        return {
            "connection": {
                "state": self.connection_state(),
                "host": s["switcher_ip"], "port": s["switcher_port"],
                "product": st.product if st else None,
                "protocol_version": st.protocol_version if st else None,
                "last_error": client.last_error if client else None,
            },
            "switcher": st.snapshot() if st else None,
            "settings": s,
            "inputs": inputs,
            "internal_sources": internal,
            "aliases": SOURCE_ALIASES,
            "udp": {
                "enabled": s["udp_enabled"], "host": s["udp_host"], "port": s["udp_port"],
                "running": bool(udp and udp.running), "error": udp.error if udp else None,
                "received": udp.received if udp else 0, "refused": udp.refused if udp else 0,
                "last": udp.last if udp else None,
            },
            "syslog": self.audit.remote_status(),
        }
