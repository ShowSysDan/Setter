"""
Configuration classes. Values come from the environment (or a .env file in
the project root). Operational settings - switcher IP, input counts, UDP
port - live in the Settings page / instance/settings.json, not here.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    SETTINGS_FILE = os.environ.get("SETTINGS_FILE")      # default: instance/settings.json
    API_KEY = os.environ.get("API_KEY")                  # for /api/v1/; unset = open on the LAN
    SETTER_AUTOSTART = True
    JSON_SORT_KEYS = False


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


def get_config():
    env = os.environ.get("FLASK_ENV", "development").lower()
    return ProductionConfig if env == "production" else DevelopmentConfig
