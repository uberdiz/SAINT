from modules.spotify.auth import SpotifyAuth
from modules.spotify.client import SpotifyAPIError


def test_spotify_auth_requires_client_id():
    auth = SpotifyAuth()
    assert not auth.is_configured()


def test_spotify_api_error():
    exc = SpotifyAPIError("bad", 403)
    assert str(exc) == "bad"
    assert exc.status == 403
