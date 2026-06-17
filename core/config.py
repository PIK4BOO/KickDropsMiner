"""Configuration management for KickDropsMiner"""
import os
from utils.helpers import CONFIG_FILE
from utils.json_utils import json_load, json_save_atomic


DEFAULT_CONFIG = {
    "items": [],
    "chromedriver_path": "",
    "extension_path": "",
    "mute": False,
    "hide_player": False,
    "mini_player": False,
    "background_browser": False,
    "force_160p": False,
    "dark_mode": True,
    "language": "es",
    "auto_start": False,
    "debug": False,
    "kick_api_client_id": "",
    "kick_api_client_secret": "",
    "kick_api_access_token": "",
    "kick_api_token_expires_at": 0,
}


DEFAULT_ITEM = {
    "url": "",
    "minutes": 0,
    "finished": False,
    "elapsed": 0,
    "campaign_id": None,
    "campaign_channels": [],
    "required_category_id": None,
    "is_global_drop": False,
    "cumulative_time": 0,
    "tried_channels": [],
    "priority": False,
}


class Config:
    """Manages application configuration and queue items"""
    
    def __init__(self):
        for key, value in DEFAULT_CONFIG.items():
            setattr(self, key, value)
        self.load()

    def _normalize_item(self, item):
        normalized = dict(DEFAULT_ITEM)
        if isinstance(item, dict):
            normalized.update(item)
        try:
            normalized["minutes"] = max(0, int(normalized.get("minutes") or 0))
        except (TypeError, ValueError):
            normalized["minutes"] = 0
        try:
            normalized["elapsed"] = max(0, int(normalized.get("elapsed") or 0))
        except (TypeError, ValueError):
            normalized["elapsed"] = 0
        try:
            normalized["cumulative_time"] = max(0, int(normalized.get("cumulative_time") or 0))
        except (TypeError, ValueError):
            normalized["cumulative_time"] = 0
        if not isinstance(normalized.get("campaign_channels"), list):
            normalized["campaign_channels"] = []
        if not isinstance(normalized.get("tried_channels"), list):
            normalized["tried_channels"] = []
        priority = normalized.get("priority", False)
        normalized["priority"] = bool(priority is True or str(priority).lower() == "high")
        normalized["finished"] = bool(normalized.get("finished"))
        normalized["is_global_drop"] = bool(normalized.get("is_global_drop"))
        return normalized

    def load(self):
        """Load configuration from file"""
        if os.path.exists(CONFIG_FILE):
            data = json_load(CONFIG_FILE, DEFAULT_CONFIG, merge=True)
        else:
            data = dict(DEFAULT_CONFIG)

        self.items = [self._normalize_item(item) for item in data.get("items", [])]
        self.chromedriver_path = data.get("chromedriver_path") or ""
        self.extension_path = data.get("extension_path") or ""
        self.mute = bool(data.get("mute", False))
        self.hide_player = bool(data.get("hide_player", False))
        self.mini_player = bool(data.get("mini_player", False))
        self.background_browser = bool(data.get("background_browser", False))
        self.force_160p = bool(data.get("force_160p", False))
        self.dark_mode = bool(data.get("dark_mode", True))
        self.language = data.get("language") or "es"
        self.auto_start = bool(data.get("auto_start", False))
        self.debug = bool(data.get("debug", False))
        self.kick_api_client_id = data.get("kick_api_client_id") or ""
        self.kick_api_client_secret = data.get("kick_api_client_secret") or ""
        self.kick_api_access_token = data.get("kick_api_access_token") or ""
        try:
            self.kick_api_token_expires_at = float(data.get("kick_api_token_expires_at") or 0)
        except (TypeError, ValueError):
            self.kick_api_token_expires_at = 0

    def save(self):
        """Save configuration to file"""
        data = {
            "items": self.items,
            "chromedriver_path": self.chromedriver_path,
            "extension_path": self.extension_path,
            "mute": self.mute,
            "hide_player": self.hide_player,
            "mini_player": self.mini_player,
            "background_browser": self.background_browser,
            "force_160p": self.force_160p,
            "dark_mode": self.dark_mode,
            "language": self.language,
            "auto_start": self.auto_start,
            "debug": self.debug,
            "kick_api_client_id": self.kick_api_client_id,
            "kick_api_client_secret": self.kick_api_client_secret,
            "kick_api_access_token": self.kick_api_access_token,
            "kick_api_token_expires_at": self.kick_api_token_expires_at,
        }
        json_save_atomic(CONFIG_FILE, data, indent=2)

    def add(self, url, minutes, campaign_id=None, campaign_channels=None, required_category_id=None, is_global_drop=False, priority=False):
        """Add item with optional campaign grouping"""
        item = {
            "url": url,
            "minutes": minutes,
            "campaign_id": campaign_id,
            "campaign_channels": campaign_channels or [],
            "required_category_id": required_category_id,
            "is_global_drop": is_global_drop,
            "cumulative_time": 0,  # Track cumulative time across all streamers in campaign
            "priority": bool(priority),
        }
        self.items.append(item)
        self.save()

    def remove(self, idx):
        """Remove item at index"""
        del self.items[idx]
        self.save()
