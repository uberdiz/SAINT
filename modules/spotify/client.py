"""Small Spotify Web API client with normalized, structured errors."""

import logging
import time

import requests

from modules.spotify.auth import SpotifyAuth

BASE_URL = "https://api.spotify.com/v1"

logger = logging.getLogger("saint.spotify")


class SpotifyAPIError(RuntimeError):
    def __init__(self, message, status=None, reason=None, code=None):
        super().__init__(message)
        self.status = status
        self.reason = reason               # Spotify's error.reason, e.g. NO_ACTIVE_DEVICE
        self.code = code or self._derive_code()

    def _derive_code(self):
        if self.reason:
            return str(self.reason)
        if self.status == 401:
            return "AUTH_EXPIRED"
        if self.status == 404:
            return "NOT_FOUND"
        if self.status == 429:
            return "RATE_LIMITED"
        if self.status == 403:
            return "FORBIDDEN"
        if self.status is None:
            return "NOT_CONNECTED"
        return "API_ERROR"


# error_code -> natural, speakable message. The AI/router should surface these
# instead of a raw Python exception string.
_FRIENDLY = {
    "NOT_CONNECTED": "Spotify isn't connected.",
    "AUTH_EXPIRED": "Your Spotify session expired — please reconnect Spotify.",
    "NO_ACTIVE_DEVICE": "Spotify isn't playing on any device right now.",
    "NOT_FOUND": "Spotify isn't playing anything right now.",
    "RATE_LIMITED": "Spotify is rate-limiting requests; please try again in a moment.",
    "FORBIDDEN": "Spotify wouldn't allow that action right now.",
    "PREMIUM_REQUIRED": "That Spotify action needs a Premium account.",
    "NO_PLAYBACK": "Spotify isn't currently playing anything.",
}


def friendly_error(exc) -> str:
    """Map a SpotifyAPIError (or any exception) to a clean, speakable message."""
    code = getattr(exc, "code", None)
    if code in _FRIENDLY:
        return _FRIENDLY[code]
    # Premium restriction often comes back as 403 with a hint in the message.
    msg = str(exc)
    if "premium" in msg.lower():
        return _FRIENDLY["PREMIUM_REQUIRED"]
    return "Spotify couldn't complete that request."


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth):
        self.auth = auth

    def request(self, method, path, **kwargs):
        token = self.auth.get_access_token()
        if not token:
            raise SpotifyAPIError("Spotify is not connected.", status=None)
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        started = time.perf_counter()
        try:
            response = requests.request(method, BASE_URL + path, headers=headers, timeout=15, **kwargs)
        except requests.RequestException as e:
            raise SpotifyAPIError(f"Network error contacting Spotify: {e}", status=None, code="NETWORK") from e
        elapsed_ms = (time.perf_counter() - started) * 1000

        # Log status + a safe, truncated body. Never log the Authorization
        # header / access token.
        body_preview = (response.text or "")[:200]
        logger.debug("spotify.api %s %s -> %s (%.0fms) body=%r",
                     method, path, response.status_code, elapsed_ms, body_preview)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise SpotifyAPIError(
                f"Spotify rate limit reached; retry after {retry_after} seconds.",
                status=429, code="RATE_LIMITED",
            )
        if response.status_code == 401:
            self.auth.clear()
            raise SpotifyAPIError("Spotify authorization expired. Please reconnect.",
                                  status=401, code="AUTH_EXPIRED")
        if response.status_code >= 400:
            detail, reason = self._parse_error_body(response)
            logger.warning("spotify.api.error status=%s reason=%s detail=%s",
                           response.status_code, reason, detail)
            raise SpotifyAPIError(
                f"Spotify API error ({response.status_code}): {detail}",
                status=response.status_code, reason=reason,
            )

        # Success. Many player endpoints return 204 No Content — do NOT call
        # response.json() on an empty body (that raised the
        # "Expecting value: line 1 column 1 (char 0)" JSONDecodeError).
        if not response.content:
            return None, elapsed_ms
        try:
            return response.json(), elapsed_ms
        except ValueError:
            # 200/2xx with a non-JSON body: treat as empty rather than crashing.
            logger.warning("spotify.api non-JSON 2xx body on %s %s: %r",
                           method, path, body_preview)
            return None, elapsed_ms

    @staticmethod
    def _parse_error_body(response):
        """Extract (detail_message, reason) from a Spotify error body safely."""
        detail = (response.text or "")[:200]
        reason = None
        if response.content:
            try:
                err = response.json().get("error", {})
                if isinstance(err, dict):
                    detail = err.get("message", detail)
                    reason = err.get("reason")
            except ValueError:
                pass
        return detail, reason

    def search(self, query, types="track,artist,album,playlist", limit=10):
        return self.request("GET", "/search", params={"q": query, "type": types, "limit": limit})[0]

    def playback(self):
        return self.request("GET", "/me/player")[0]

    def devices(self):
        return self.request("GET", "/me/player/devices")[0]

    def playlists(self, limit=50):
        return self.request("GET", "/me/playlists", params={"limit": max(1, min(50, int(limit)))})[0]


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

    def shuffle(self, state: bool, device_id=None):
        params = {"state": "true" if state else "false"}
        if device_id:
            params["device_id"] = device_id
        self.request("PUT", "/me/player/shuffle", params=params)

    def repeat(self, state: str, device_id=None):
        # state: "off" | "context" | "track"
        if state not in ("off", "context", "track"):
            state = "context"
        params = {"state": state}
        if device_id:
            params["device_id"] = device_id
        self.request("PUT", "/me/player/repeat", params=params)

    def queue(self, uri, device_id=None):
        params = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        self.request("POST", "/me/player/queue", params=params)

    def add_to_playlist(self, playlist_id, uris):
        self.request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris})
