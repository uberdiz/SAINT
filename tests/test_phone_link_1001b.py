"""Phone <-> PC additions (2026-10-01, round 2): pairing links that carry the PC's
Tailscale address, the phone's activity log arriving in History, and the
ONNX Kokoro engine being chosen when PyTorch isn't there."""

import time

from modules.link.identity import PairingManager, parse_pair_uri
from modules.link.service import tailscale_address
from modules.link.sync import ActionLogAdapter, default_adapters


def test_tailscale_address_is_found_among_the_pcs_addresses():
    assert tailscale_address(["192.168.1.20", "100.101.5.7"]) == "100.101.5.7"
    assert tailscale_address(["192.168.1.20", "100.20.1.1"]) == ""        # 100.20 isn't Tailscale's range
    assert tailscale_address([]) == ""


def test_pair_link_carries_alternate_addresses():
    offer = PairingManager().create("own")
    uri = offer.uri("192.168.1.20", 8765, "My PC", alternates=["192.168.1.20", "100.101.5.7"])
    info = parse_pair_uri(uri)
    assert info["host"] == "192.168.1.20" and info["alternates"] == ["100.101.5.7"]
    plain = parse_pair_uri(offer.uri("192.168.1.20", 8765))
    assert plain["alternates"] == []


def test_phone_activity_lands_in_history_once(tmp_path, monkeypatch):
    from core import history as history_mod
    added = []
    monkeypatch.setattr(history_mod.history, "append", lambda rec: added.append(rec))
    a = ActionLogAdapter(path=str(tmp_path / "phone_activity.json"))
    entry = {"ts": time.time(), "request": "play blinding lights", "action": "Playing Blinding Lights.",
             "kind": "music", "status": "done", "source": "phone", "device": "iPhone"}
    assert a.apply_batch([("abc", entry)]) == []
    assert a.apply_batch([("abc", entry)]) == []                   # synced again: not a second History line
    assert len(added) == 1 and added[0]["source"] == "iphone" and added[0]["user"] == "play blinding lights"
    assert "abc" in a.snapshot()                                   # kept, so sync doesn't read it as deleted
    old = dict(entry, ts=time.time() - 40 * 86400)
    a.apply_batch([("old", old)])
    assert "old" not in a.snapshot()
    a.apply_batch([("abc", None)])
    assert a.snapshot() == {}


def test_actionlog_is_synced():
    assert "actionlog" in {ad.kind for ad in default_adapters()}


def test_onnx_engine_is_used_without_pytorch(monkeypatch):
    from modules.voice import kokoro_onnx, tts
    monkeypatch.setattr(tts, "_torch_available", lambda: False)
    monkeypatch.setattr(kokoro_onnx, "available", lambda: True)
    engine = tts.make_tts("kokoro", voice="af_heart", speed=1.0)
    assert type(engine).__name__ == "KokoroOnnxTTS"
