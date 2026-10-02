"""Spotify Authorization Code + PKCE authentication for the desktop app."""

import base64
import hashlib
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Optional

import requests

from core.config import config

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"

log = logging.getLogger("saint.spotify")

# Refresh errors that mean the saved login is really gone (revoked, or the
# refresh token was already used). Anything else — no network yet at boot,
# a Spotify outage, rate limiting — keeps the login and tries again later.
_DEAD_LOGIN = {"invalid_grant", "invalid_token", "unauthorized_client"}


@dataclass
class SpotifyToken:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float
    scope: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 60


class SpotifyAuth:
    def __init__(self):
        self._token: Optional[SpotifyToken] = None
        self._lock = threading.Lock()
        self._pending: Optional[threading.Event] = None      # set to abandon the sign-in that's waiting

    def cancel_login(self) -> bool:
        """Give up on a sign-in still waiting for the browser (wrong redirect, closed tab), so
        "Connect" can start a fresh one at once instead of after the 3-minute timeout."""
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.set()
            return True
        return False

    @property
    def client_id(self):
        return config.get("spotify.client_id", "").strip()

    @property
    def redirect_uri(self):
        return config.get("spotify.redirect_uri", DEFAULT_REDIRECT_URI).strip()

    def is_configured(self):
        return bool(self.client_id)

    def _load_saved(self):
        try:
            import keyring
            raw = keyring.get_password("SAINT", "spotify")
            if raw:
                data = json.loads(raw)
                self._token = SpotifyToken(**data)
        except Exception:
            self._token = None

    def _save(self):
        if not self._token:
            return
        try:
            import keyring
            keyring.set_password("SAINT", "spotify", json.dumps(self._token.__dict__))
        except Exception:
            log.warning("spotify.token.save_failed", exc_info=True)

    def clear(self):
        self._token = None
        try:
            import keyring
            keyring.delete_password("SAINT", "spotify")
        except Exception:
            pass

    def get_access_token(self):
        with self._lock:
            if self._token is None:
                self._load_saved()
            if self._token and not self._token.expired:
                return self._token.access_token
            if self._token and self._token.refresh_token and self.is_configured():
                self._refresh()
                return self._token.access_token
            return None

    def _refresh(self):
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._token.refresh_token,
                    "client_id": self.client_id,
                },
                timeout=15,
            )
        except requests.RequestException as e:
            log.warning("spotify.refresh.network %s", e)
            raise RuntimeError("Couldn't reach Spotify to renew the login.") from e
        if response.status_code >= 400:
            try:
                err = (response.json() or {}).get("error", "")
            except ValueError:
                err = ""
            log.warning("spotify.refresh.failed status=%s error=%s", response.status_code, err or "?")
            # Only a definite "this login is dead" answer forgets it; a 5xx or
            # 429 must not make the user reconnect.
            if response.status_code in (400, 401) and err in _DEAD_LOGIN:
                self.clear()
            raise RuntimeError(f"Spotify token refresh failed ({response.status_code})")
        try:
            data = response.json()
        except ValueError as e:
            # Never let an empty/non-JSON refresh body surface as a raw
            # "Expecting value" JSONDecodeError to the tool/AI layer.
            log.warning("spotify.refresh.bad_body")
            raise RuntimeError("Spotify token refresh returned an invalid response.") from e
        self._token = SpotifyToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self._token.refresh_token),
            expires_at=time.time() + int(data.get("expires_in", 3600)),
            scope=data.get("scope", self._token.scope),
        )
        self._save()

    def authenticate(self, scopes):
        if not self.is_configured():
            raise RuntimeError("Spotify Client ID is not configured in Settings → Integrations → Spotify.")

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        parsed = urllib.parse.urlparse(self.redirect_uri)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("Desktop PKCE requires a loopback redirect URI.")

        self.cancel_login()                         # a second "Connect" replaces a stuck first one
        result = {}
        done = threading.Event()
        cancelled = threading.Event()
        self._pending = cancelled

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result.update({k: v[0] for k, v in params.items() if v})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>SAINT Spotify login complete.</h2><p>You can close this window.</p></body></html>")
                done.set()

            def log_message(self, *_args):
                return

        server = None
        for _ in range(10):                         # the abandoned attempt may still be letting go of the port
            try:
                server = http.server.ThreadingHTTPServer((parsed.hostname, parsed.port or 80), CallbackHandler)
                break
            except OSError:
                time.sleep(0.3)
        if server is None:
            raise RuntimeError(f"Port {parsed.port} is busy, so Spotify can't send you back to SAINT. Close whatever "
                               f"is using it, or change the Redirect URI (and add the new one in your Spotify app).")
        server.timeout = 1
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            query = urllib.parse.urlencode({
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(scopes),
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": state,
                "show_dialog": "true",              # always show Spotify's page, so a wrong attempt can be redone
            })
            webbrowser.open(f"{AUTH_URL}?{query}")
            deadline = time.time() + 180
            while time.time() < deadline and not done.wait(0.5):
                if cancelled.is_set():
                    raise RuntimeError("Spotify sign-in cancelled.")
            if not result:
                raise RuntimeError("Spotify sign-in timed out. If Spotify showed an error (like “Invalid redirect "
                                   "URI”), add the Redirect URI from Settings to your Spotify app exactly, then "
                                   "press Connect again.")
            if result.get("state") != state:
                raise RuntimeError("Spotify authentication state validation failed.")
            if result.get("error"):
                raise RuntimeError(f"Spotify authorization failed: {result['error']}")

            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": result["code"],
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": verifier,
                },
                timeout=15,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Spotify token exchange failed ({response.status_code})")
            data = response.json()
            self._token = SpotifyToken(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_at=time.time() + int(data.get("expires_in", 3600)),
                scope=data.get("scope", ""),
            )
            self._save()
            return True
        finally:
            if self._pending is cancelled:
                self._pending = None
            server.shutdown()
            server.server_close()
