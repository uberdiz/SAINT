"""Small Spotify Web API client with normalized errors."""

import time
import requests

from modules.spotify.auth import SpotifyAuth

BASE_URL = "https://api.spotify.com/v1"


class SpotifyAPIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth):
        self.auth = auth

    def request(self, method, path, **kwargs):
        token = self.auth.get_access_token()
        if not token:
            raise SpotifyAPIError("Spotify is not connected.")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        started = time.perf_counter()
        response = requests.request(method, BASE_URL + path, headers=headers, timeout=15, **kwargs)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise SpotifyAPIError(f"Spotify rate limit reached; retry after {retry_after} seconds.", 429)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if response.status_code == 401:
            self.auth.clear()
            raise SpotifyAPIError("Spotify authorization expired. Please reconnect.", 401)
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            raise SpotifyAPIError(f"Spotify API error ({response.status_code}): {detail}", response.status_code)
        data = response.json() if response.content else None
        return data, elapsed_ms

    def search(self, query, types="track,artist,album,playlist", limit=10):
        return self.request("GET", "/search", params={"q": query, "type": types, "limit": limit})[0]

    def playback(self):
        return self.request("GET", "/me/player")[0]

    def devices(self):
        return self.request("GET", "/me/player/devices")[0]

    def play(self, context_uri=None, uris=None, device_id=None):
        params = {"device_id": device_id} if device_id else {}
        body = {}
        if context_uri:
            body["context_uri"] = context_uri
        if uris:
            body["uris"] = uris
        self.request("PUT", "/me/player/play", params=params, json=body)

    def pause(self, device_id=None):
        params = {"device_id": device_id} if device_id else {}
        self.request("PUT", "/me/player/pause", params=params)

    def next(self, device_id=None):
        params = {"device_id": device_id} if device_id else {}
        self.request("POST", "/me/player/next", params=params)

    def previous(self, device_id=None):
        params = {"device_id": device_id} if device_id else {}
        self.request("POST", "/me/player/previous", params=params)

    def volume(self, percent, device_id=None):
        params = {"volume_percent": max(0, min(100, int(percent)))}
        if device_id:
            params["device_id"] = device_id
        self.request("PUT", "/me/player/volume", params=params)

    def queue(self, uri, device_id=None):
        params = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        self.request("POST", "/me/player/queue", params=params)

    def add_to_playlist(self, playlist_id, uris):
        self.request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris})
