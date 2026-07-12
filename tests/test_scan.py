"""מצב סריקה (סבב בין מערכות DMR): validate_scan_plan, leg_active_now, /api/mode scan."""
import json


def _seed(app):
    app.SYSTEMS_PATH.write_text(json.dumps([
        {"id": "s1", "name": "A", "control": 461.0, "color_code": 1, "channelmap": []},
        {"id": "s2", "name": "B", "control": 462.0, "color_code": 2, "channelmap": []},
    ]))


def test_validate_scan_plan_ok(paths):
    app = paths
    _seed(app)
    plan = app._validate_scan_plan([{"system": "s1", "dwell_sec": 60},
                                    {"system": "s2", "dwell_sec": 120}])
    assert plan and len(plan) == 2 and plan[0]["system"] == "s1"


def test_validate_scan_plan_unknown_system(paths):
    app = paths
    _seed(app)
    assert app._validate_scan_plan([{"system": "nope", "dwell_sec": 60}]) is None


def test_validate_scan_plan_bad_dwell(paths):
    app = paths
    _seed(app)
    assert app._validate_scan_plan([{"system": "s1", "dwell_sec": 1}]) is None       # קצר מדי
    assert app._validate_scan_plan([{"system": "s1", "dwell_sec": 99999}]) is None   # ארוך מדי


def test_validate_scan_plan_time_window(paths):
    app = paths
    _seed(app)
    plan = app._validate_scan_plan([{"system": "s1", "dwell_sec": 60,
                                     "active_from": "22:00", "active_to": "06:00"}])
    assert plan[0]["active_from"] == "22:00"
    # חלון חלקי (רק from) => לא תקין
    assert app._validate_scan_plan([{"system": "s1", "dwell_sec": 60,
                                     "active_from": "22:00"}]) is None


def test_validate_scan_plan_size_limits(paths):
    app = paths
    _seed(app)
    assert app._validate_scan_plan([]) is None
    assert app._validate_scan_plan([{"system": "s1", "dwell_sec": 60}] * 9) is None


def test_leg_active_now_no_window(paths):
    assert paths._leg_active_now({"system": "s1", "dwell_sec": 60}) is True


def test_leg_active_now_window(paths, monkeypatch):
    import time as _t
    app = paths
    # 12:00 מקומי
    monkeypatch.setattr(app.time, "localtime",
                        lambda *a: _t.struct_time((2026, 7, 12, 12, 0, 0, 6, 193, -1)))
    assert app._leg_active_now({"active_from": "08:00", "active_to": "16:00"}) is True
    assert app._leg_active_now({"active_from": "16:00", "active_to": "20:00"}) is False
    # חלון שחוצה חצות
    assert app._leg_active_now({"active_from": "22:00", "active_to": "06:00"}) is False


def test_api_mode_scan(paths, sysctl, no_sleep):
    app = paths
    _seed(app)
    r = _client(app).post("/api/mode", json={"mode": "scan",
                          "plan": [{"system": "s1", "dwell_sec": 60}]})
    assert r.status_code == 200
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "scan" and st["scan_plan"][0]["system"] == "s1"
    app._scan_stop_thread()   # ניקוי


def test_api_mode_scan_invalid_plan(paths, sysctl, no_sleep):
    app = paths
    _seed(app)
    r = _client(app).post("/api/mode", json={"mode": "scan",
                          "plan": [{"system": "nope", "dwell_sec": 60}]})
    assert r.status_code == 400


def _client(app):
    return app.app.test_client()
