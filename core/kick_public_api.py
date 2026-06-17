"""Kick Public API client.

Uses the official Kick OAuth client-credentials flow and public endpoints when
the user provides a Kick app client id/secret. All callers should treat this as
an optional source and keep fallbacks for browser/session-only endpoints.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from utils.helpers import debug_print


KICK_API_BASE = "https://api.kick.com"
KICK_OAUTH_TOKEN_URL = "https://id.kick.com/oauth/token"


class KickPublicAPI:
    def __init__(self, client_id="", client_secret="", access_token="", expires_at=0):
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.access_token = access_token or ""
        self.expires_at = float(expires_at or 0)

    def configured(self):
        return bool(self.client_id and self.client_secret)

    def token_valid(self):
        return bool(self.access_token and time.time() < self.expires_at - 60)

    def _headers(self):
        return {
            "Accept": "application/json",
            "User-Agent": "KickDropsMiner/1.0",
            "Authorization": f"Bearer {self.access_token}",
        }

    def refresh_app_token(self):
        if not self.configured():
            return False

        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            KICK_OAUTH_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        token = data.get("access_token")
        if not token:
            return False
        try:
            expires_in = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self.access_token = token
        self.expires_at = time.time() + expires_in
        return True

    def ensure_token(self):
        if self.token_valid():
            return True
        return self.refresh_app_token()

    def request_json(self, path, params=None):
        if not self.ensure_token():
            return None
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{KICK_API_BASE}{path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            debug_print(f"DEBUG: Kick public API HTTP {e.code} for {path}")
            if e.code == 401 and self.refresh_app_token():
                req = urllib.request.Request(url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.load(resp)
            return None
        except Exception as e:
            debug_print(f"DEBUG: Kick public API error for {path}: {e}")
            return None

    def get_channels_by_slug(self, slugs):
        if isinstance(slugs, str):
            slugs = [slugs]
        slugs = [slug for slug in slugs if slug]
        if not slugs:
            return []
        data = self.request_json("/public/v1/channels", {"slug": slugs})
        return _extract_data_list(data)

    def get_channel_by_slug(self, slug):
        channels = self.get_channels_by_slug(slug)
        return channels[0] if channels else None

    def get_livestreams(self, **params):
        clean = {key: value for key, value in params.items() if value not in (None, "", [])}
        data = self.request_json("/public/v1/livestreams", clean)
        return _extract_data_list(data)

    def get_livestreams_by_category(self, category_id, limit=24):
        return self.get_livestreams(category_id=category_id, limit=limit, sort="viewer_count")

    def get_livestreams_by_broadcaster(self, broadcaster_user_id):
        return self.get_livestreams(broadcaster_user_id=[broadcaster_user_id], limit=1)

    def get_categories(self, *, ids=None, names=None, tags=None, limit=100, cursor=None):
        params = {"limit": limit}
        if ids:
            params["id"] = ids if isinstance(ids, list) else [ids]
        if names:
            params["name"] = names if isinstance(names, list) else [names]
        if tags:
            params["tag"] = tags if isinstance(tags, list) else [tags]
        if cursor:
            params["cursor"] = cursor
        data = self.request_json("/public/v2/categories", params)
        return _extract_data_list(data)

    def get_category_by_name(self, name):
        categories = self.get_categories(names=[name], limit=10)
        lowered = (name or "").strip().lower()
        for category in categories:
            if str(category.get("name", "")).strip().lower() == lowered:
                return category
        return categories[0] if categories else None

    def live_status_by_slug(self, slug):
        channel = self.get_channel_by_slug(slug)
        if not channel:
            return None
        stream = channel.get("stream")
        if isinstance(stream, dict):
            return True
        broadcaster_id = channel.get("broadcaster_user_id")
        if broadcaster_id:
            return bool(self.get_livestreams_by_broadcaster(broadcaster_id))
        return False

    def category_id_by_slug(self, slug):
        channel = self.get_channel_by_slug(slug)
        if not channel:
            return None
        stream = channel.get("stream")
        if isinstance(stream, dict):
            category = stream.get("category") or channel.get("category")
        else:
            category = channel.get("category")
        if isinstance(category, dict):
            return category.get("id")
        return None


def _extract_data_list(response):
    if not isinstance(response, dict):
        return []
    data = response.get("data", [])
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("livestreams", "channels", "categories", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def livestream_to_streamer(stream):
    slug = stream.get("slug")
    if not slug:
        return None
    return {
        "url": f"https://kick.com/{slug}",
        "username": slug,
        "title": stream.get("stream_title", ""),
        "viewer_count": stream.get("viewer_count", 0),
        "profile_picture": stream.get("profile_picture", ""),
        "category_id": (stream.get("category") or {}).get("id") if isinstance(stream.get("category"), dict) else None,
    }
