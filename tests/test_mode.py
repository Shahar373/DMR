"""מעברי מצב DMR: _enter_dmr, /api/mode, נפילה-ל-off, /api/state, /api/health.
SDR/systemd ממוקפים דרך fixture sysctl."""
import json


def _client(app):
    return app.app.test_client()


def _seed_systems(app, systems):
    app.SYSTEMS_PATH.write_text(json.dumps(systems))


def test_enter_dmr_success(paths, sysctl, no_sleep):
    app = paths
    system = {"id": "s1", "name": "Test", "control": 461.0375, "color_code": 1,
              "channelmap": [{"lcn": 1, "freq": 461.0375}]}
    err, detail = app._enter_dmr(system)
    assert err is None
    assert ("restart", app.DMR_SERVICE) in sysctl.calls
    # env + channelmap נכתבו
    assert "DSD_CONTROL_FREQ=461037500" in app.DMR_ENV_PATH.read_text()
    assert "461037500" in app.CHANNELMAP_PATH.read_text()


def test_enter_dmr_crash_returns_error(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # השירות "קרס" מיד
    system = {"id": "s1", "name": "T", "control": 461.0, "color_code": 1, "channelmap": []}
    err, detail = app._enter_dmr(system)
    assert err is not None


def test_api_mode_dmr(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0375,
                         "color_code": 1, "channelmap": [{"lcn": 1, "freq": 461.0375}]}])
    r = _client(app).post("/api/mode", json={"mode": "dmr", "system": "s1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "dmr" and st["system"] == "s1"


def test_api_mode_off(paths, sysctl, no_sleep):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    r = _client(app).post("/api/mode", json={"mode": "off"})
    assert r.status_code == 200
    assert ("stop", app.DMR_SERVICE) in sysctl.calls
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "off" and st["prev_mode"] == "dmr"


def test_api_mode_invalid(paths, sysctl):
    r = _client(paths).post("/api/mode", json={"mode": "banana"})
    assert r.status_code == 400


def test_api_mode_dmr_no_system(paths, sysctl, no_sleep):
    app = paths
    _seed_systems(app, [])   # אין מערכות => 400, לא crash
    r = _client(app).post("/api/mode", json={"mode": "dmr"})
    assert r.status_code == 400


def test_api_mode_fail_to_off(paths, sysctl, no_sleep, monkeypatch):
    """כשל כניסה => נפילה ל-off (500), לעולם לא נשאר תקוע."""
    app = paths
    _seed_systems(app, [{"id": "s1", "name": "T", "control": 461.0,
                         "color_code": 1, "channelmap": []}])
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # תמיד "נכשל לעלות"
    r = _client(app).post("/api/mode", json={"mode": "dmr", "system": "s1"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False and body["app_mode"] == "off"
    st = json.loads(app.STATE_PATH.read_text())
    assert st["app_mode"] == "off"


def test_api_state_live_vs_saved(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    # שמור dmr אבל אף שירות לא רץ => mode_ok=False (תקלה מדווחת)
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    r = _client(app).get("/api/state")
    body = r.get_json()
    assert body["app_mode"] == "dmr" and body["mode_ok"] is False


def test_api_state_off_is_ok(paths, sysctl, monkeypatch):
    app = paths
    app.save_state({"app_mode": "off"})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    body = _client(app).get("/api/state").get_json()
    assert body["app_mode"] == "off" and body["mode_ok"] is True


def test_api_health_dmr(paths, monkeypatch):
    app = paths
    app.save_state({"app_mode": "dmr", "system": "s1"})
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            stdout="active", returncode=0))
    body = _client(app).get("/api/health").get_json()
    assert body["app_mode"] == "dmr" and body["ok"] is True


def test_systems_put_validation(paths):
    app = paths
    c = _client(app)
    # תקין
    good = [{"id": "s1", "name": "Sys", "control": 461.0, "color_code": 2,
             "channelmap": [{"lcn": 1, "freq": 461.0}]}]
    assert c.put("/api/systems", json=good).status_code == 200
    # color code מחוץ לטווח => 400
    bad = [{"id": "s1", "name": "Sys", "control": 461.0, "color_code": 99, "channelmap": []}]
    assert c.put("/api/systems", json=bad).status_code == 400


def test_render_dmr_env_hz_conversion(paths):
    app = paths
    system = {"id": "s1", "name": "T", "control": 461.0375, "color_code": 3, "channelmap": []}
    env = app.render_dmr_env(system)
    assert "DSD_CONTROL_FREQ=461037500" in env   # MHz → Hz
    assert "DSD_COLOR_CODE=3" in env


def test_render_channelmap(paths):
    app = paths
    txt = app.render_channelmap([{"lcn": 1, "freq": 461.0375}, {"lcn": 2, "freq": 461.0625}])
    assert "1,461037500" in txt and "2,461062500" in txt


def test_render_channelmap_control_hint_row(paths):
    # אין דגל -c ב-dsd-neo; תדר הבקרה מוזרק כשורה ראשונה (ערוץ-דמה 999) — סדר
    # השורות הוא ה"בוא לכאן קודם" היחיד שהפורמט תומך בו.
    app = paths
    txt = app.render_channelmap(
        [{"lcn": 1, "freq": 461.0375}, {"lcn": 2, "freq": 461.0625}], control_mhz=461.0375)
    lines = txt.strip().split("\n")
    assert lines[0] == f"{app.CONTROL_HINT_LCN},461037500,control"
    assert lines[1:] == ["1,461037500", "2,461062500"]


def test_render_channelmap_no_control(paths):
    app = paths
    txt = app.render_channelmap([{"lcn": 1, "freq": 461.0375}])
    assert txt.strip() == "1,461037500"
