"""
Persistent settings, stored as JSON in the Flask instance folder.

Small enough that a database would be overkill; the file is rewritten
atomically on every save.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading

DEFAULTS = {
    "switcher_ip": "192.168.10.240",   # Blackmagic's factory default
    "switcher_port": 9910,
    "auto_connect": True,
    "me_count": 1,
    "input_count": 8,
    "aux_count": 1,
    "dsk_count": 2,
    "macro_count": 8,
    "default_fade_frames": 25,
    "udp_enabled": True,
    "udp_host": "0.0.0.0",
    "udp_port": 9990,
    "udp_reply": True,
    "udp_allowed_senders": "",         # "" = any address; else IPs / CIDRs, comma separated
    "raw_external": False,             # allow RAW <NAME> <hex> over UDP and /api/v1/ (UI always can)
    "syslog_enabled": False,
    "syslog_host": "",
    "syslog_port": 514,
    "syslog_protocol": "udp",
    "syslog_facility": "local0",
}

# (min, max) for the integer settings
LIMITS = {
    "switcher_port": (1, 65535),
    "me_count": (1, 4),
    "input_count": (1, 40),
    "aux_count": (0, 24),
    "dsk_count": (0, 4),
    "macro_count": (0, 100),
    "default_fade_frames": (1, 250),
    "udp_port": (1, 65535),
    "syslog_port": (1, 65535),
}
BOOLS = {"auto_connect", "udp_enabled", "udp_reply", "raw_external", "syslog_enabled"}
CHOICES = {
    "syslog_protocol": ("udp", "tcp"),
    "syslog_facility": ("local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7", "user", "daemon"),
}
STRINGS = {"switcher_ip", "udp_host", "syslog_host", "udp_allowed_senders"}

# RFC 1123 hostname (labels of letters, digits and hyphens) - used for the
# switcher and syslog targets, which may be names as well as addresses.
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")


def _is_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(value))


def parse_networks(spec: str) -> list:
    """'10.1.1.0/24, 192.168.10.5' -> [ip_network, ...]; raises SettingsError."""
    nets = []
    for part in (spec or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise SettingsError(f"'{part}' is not an IP address or CIDR network")
    return nets


class SettingsError(ValueError):
    pass


def _coerce(key: str, value):
    if key in BOOLS:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if key in LIMITS:
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key} must be a whole number")
        lo, hi = LIMITS[key]
        if not lo <= value <= hi:
            raise SettingsError(f"{key} must be between {lo} and {hi}")
        return value
    if key in CHOICES:
        value = str(value or "").strip().lower()
        if value not in CHOICES[key]:
            raise SettingsError(f"{key} must be one of: {', '.join(CHOICES[key])}")
        return value
    if key in STRINGS:
        value = str(value or "").strip()
        if len(value) > 1000:
            raise SettingsError(f"{key} is too long")
        if key == "switcher_ip":
            if not value:
                raise SettingsError("switcher_ip cannot be empty")
            if not _is_host(value):
                raise SettingsError("switcher_ip must be an IP address or hostname")
        elif key == "udp_host":
            if not value:
                return DEFAULTS[key]
            try:
                ipaddress.ip_address(value)
            except ValueError:
                raise SettingsError("udp_host must be an IP address of this machine (0.0.0.0 for all)")
        elif key == "syslog_host":
            if value and not _is_host(value):
                raise SettingsError("syslog_host must be an IP address or hostname")
        elif key == "udp_allowed_senders":
            value = ", ".join(str(n) for n in parse_networks(value))
        return value
    raise SettingsError(f"unknown setting '{key}'")


class Settings:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    stored = json.load(fh)
            except FileNotFoundError:
                return
            except (OSError, ValueError):
                return
            for key, value in stored.items():
                if key in DEFAULTS:
                    try:
                        self._data[key] = _coerce(key, value)
                    except SettingsError:
                        pass

    def save(self):
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self.path)

    def get(self, key: str):
        with self._lock:
            return self._data[key]

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict) -> dict:
        """Validate, apply and save. Returns the subset of keys whose value changed."""
        with self._lock:
            cleaned = {k: _coerce(k, v) for k, v in changes.items() if k in DEFAULTS}
            unknown = [k for k in changes if k not in DEFAULTS]
            if unknown:
                raise SettingsError(f"unknown setting(s): {', '.join(unknown)}")
            merged = {**self._data, **cleaned}
            if merged["syslog_enabled"] and not merged["syslog_host"]:
                raise SettingsError("syslog_host is required when remote syslog is enabled")
            changed = {k: v for k, v in cleaned.items() if self._data.get(k) != v}
            self._data.update(cleaned)
            self.save()
            return changed

    def syslog_config(self) -> dict:
        with self._lock:
            return {
                "enabled": self._data["syslog_enabled"],
                "host": self._data["syslog_host"],
                "port": self._data["syslog_port"],
                "protocol": self._data["syslog_protocol"],
                "facility": self._data["syslog_facility"],
            }
