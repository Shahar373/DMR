"""שחזור מצב באתחול (_boot_restore) — המתזמר. אף צרכן לא enabled ב-systemd."""
import json


def test_boot_restore_dmr(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.SYSTEMS_PATH.write_text(json.dumps(
        [{"id": "s1", "name": "T", "control": 461.0, "color_code": 1, "channelmap": []}]))
    app.save_state({"app_mode": "dmr", "system": "s1"})
    monkeypatch.setattr(app, "_live_mode", lambda: None)   # עוד לא רץ
    app._boot_restore()
    assert ("restart", app.DMR_SERVICE) in sysctl.calls


def test_boot_restore_off_is_noop(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.save_state({"app_mode": "off"})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    app._boot_restore()
    assert ("restart", app.DMR_SERVICE) not in sysctl.calls


def test_boot_restore_already_running(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    monkeypatch.setattr(app, "_live_mode", lambda: "dmr")   # כבר רץ (restart של web)
    app._boot_restore()
    assert ("restart", app.DMR_SERVICE) not in sysctl.calls


def test_boot_restore_off_stops_leftover(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.save_state({"app_mode": "off"})
    monkeypatch.setattr(app, "_live_mode", lambda: "dmr")   # צרכן שנשאר מסשן קודם
    app._boot_restore()
    assert ("stop", app.DMR_SERVICE) in sysctl.calls


def test_boot_restore_never_raises(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "load_state", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    app._boot_restore()   # בולע כל חריגה — לא מפיל את השרת


def test_boot_restore_dmr_fail_falls_to_off(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.SYSTEMS_PATH.write_text(json.dumps(
        [{"id": "s1", "name": "T", "control": 461.0, "color_code": 1, "channelmap": []}]))
    app.save_state({"app_mode": "dmr", "system": "s1"})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # לא עולה
    app._boot_restore()
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "off" and st["prev_mode"] == "dmr"
