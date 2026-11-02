"""
Persistent settings, stored as JSON in the Flask instance folder.

Small enough that a database would be overkill; the file is rewritten
atomically on every save.
"""
from __future__ import annotations

import json
import os
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
BOOLS = {"auto_connect", "udp_enabled", "udp_reply", "syslog_enabled"}
CHOICES = {
    "syslog_protocol": ("udp", "tcp"),
    "syslog_facility": ("local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7", "user", "daemon"),
}
STRINGS = {"switcher_ip", "udp_host", "syslog_host"}


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
        if key == "switcher_ip" and not value:
            raise SettingsError("switcher_ip cannot be empty")
        if key == "udp_host" and not value:
            return DEFAULTS[key]
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
