"""
Configuration classes. Values come from the environment (or a .env file in
the project root). Operational settings - switcher IP, input counts, UDP
port - live in the Settings page / instance/settings.json, not here.

Security-related variables (all optional, all off by default so a fresh
install works on a closed show network; see README "Security"):

    PANEL_PASSWORD    HTTP Basic auth password for the web panel and /api/
    PANEL_USER        username for the above (default "setter")
    API_KEY           key required on the external /api/v1/ endpoints
    ALLOWED_NETWORKS  comma-separated IPs / CIDRs allowed to make HTTP requests
"""
import os
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

INSECURE_SECRET = "dev-insecure-change-me"


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


class Config:
    SECRET_KEY = _env("SECRET_KEY", INSECURE_SECRET)
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    SETTINGS_FILE = _env("SETTINGS_FILE")                # default: instance/settings.json
    API_KEY = _env("API_KEY")                            # for /api/v1/; unset = open on the LAN
    PANEL_USER = _env("PANEL_USER", "setter")
    PANEL_PASSWORD = _env("PANEL_PASSWORD")              # unset = panel and /api/ open on the LAN
    ALLOWED_NETWORKS = _env("ALLOWED_NETWORKS", "")      # "" = any address
    MAX_CONTENT_LENGTH = 64 * 1024                       # no request body is anywhere near this
    SETTER_AUTOSTART = True
    JSON_SORT_KEYS = False


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    # Nothing in Setter uses the session, but never run production on the
    # well-known default: use a fresh random key for this process instead.
    SECRET_KEY = Config.SECRET_KEY if Config.SECRET_KEY != INSECURE_SECRET else secrets.token_hex(32)
    SECRET_KEY_WAS_DEFAULT = Config.SECRET_KEY == INSECURE_SECRET


def get_config():
    env = os.environ.get("FLASK_ENV", "development").lower()
    return ProductionConfig if env == "production" else DevelopmentConfig
