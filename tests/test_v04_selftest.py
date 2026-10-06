"""SAINT.exe --selftest (core/selftest.py): the checks that don't need models or a window."""

import json

from core import selftest


def test_quick_checks_pass_from_source():
    r = selftest.Report()
    for name, fn in (("imports", selftest._imports), ("assets", selftest._assets), ("data", selftest._data),
                     ("qr", selftest._qr), ("agent", selftest._agent)):
        r.check(name, fn)
    failed = [c for c in r.checks if not c["ok"]]
    assert not failed, failed


def test_a_failing_required_check_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(selftest, "REQUIRED_IMPORTS", ["definitely_not_a_module_xyz"])
    monkeypatch.setenv("SAINT_SELFTEST_NO_VOICE", "1")
    monkeypatch.setattr(selftest, "_ui", lambda: "skipped in this test")
    out = tmp_path / "report.json"
    assert selftest.run(str(out)) == 1
    data = json.loads(out.read_text())
    assert not data["passed"]
    assert any(c["name"] == "imports" and not c["ok"] and "definitely_not_a_module_xyz" in c["detail"]
               for c in data["checks"])
