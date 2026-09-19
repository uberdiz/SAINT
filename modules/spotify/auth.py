"""Spotify Authorization Code + PKCE authentication for the desktop app."""

import base64
import hashlib
import http.server
import json
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
            pass

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
            if self._token and self._token.refresh_token:
                self._refresh()
                return self._token.access_token
            return None

    def _refresh(self):
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._token.refresh_token,
                "client_id": self.client_id,
            },
            timeout=15,
        )
        if response.status_code >= 400:
            self.clear()
            raise RuntimeError(f"Spotify token refresh failed ({response.status_code})")
        data = response.json()
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

        result = {}
        done = threading.Event()

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

        server = http.server.ThreadingHTTPServer((parsed.hostname, parsed.port or 80), CallbackHandler)
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
            })
            webbrowser.open(f"{AUTH_URL}?{query}")
            deadline = time.time() + 180
            while time.time() < deadline and not done.wait(0.5):
                pass
            if not result:
                raise RuntimeError("Spotify authentication timed out or was canceled.")
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
            server.shutdown()
            server.server_close()
