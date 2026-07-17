"""שכבת ה-HTTP של איכות RF (/api/rf) ונוד-רווח (/api/gain). הלוגיקה הטהורה
(_rf_quality_snapshot/_dmr_gain_nudge) כבר מכוסה ב-test_dsd_normalize.py —
כאן רק ה-routes: guard, ולידציה, תגובות."""
import json


def _c(app):
    return app.app.test_client()


def test_api_rf_empty(paths):
    app = paths
    app._rf_ticks.clear()
    body = _c(app).get("/api/rf").get_json()
    assert body["ok"] and body["total_errors"] == 0 and body["gain_nudge"] == 0
    assert body["by_type"] == []


def test_api_rf_reports_ticks(paths):
    app = paths
    app._rf_ticks.clear()
    app._rf_quality_tick("CSBK_CRC")
    app._rf_quality_tick("CSBK_CRC")
    app._rf_quality_tick("SLCO_CRC")
    body = _c(app).get("/api/rf").get_json()
    assert body["total_errors"] == 3
    by_type = {d["error_type"]: d["count"] for d in body["by_type"]}
    assert by_type == {"CSBK_CRC": 2, "SLCO_CRC": 1}


def test_api_rf_by_channel_empty_in_single_channel_mode(paths):
    """חד-ערוצי: כל הטיקים phys_lcn=None => by_channel ריק (לא נספר פעמיים —
    הצובר הגלובלי כבר כולל אותם דרך _rf_quality_snapshot(None))."""
    app = paths
    app._rf_ticks.clear()
    app._rf_quality_tick("CSBK_CRC")
    body = _c(app).get("/api/rf").get_json()
    assert body["total_errors"] == 1
    assert body["by_channel"] == []


def test_api_rf_by_channel_breaks_down_multi_mode(paths):
    app = paths
    app._rf_ticks.clear()
    app._rf_quality_tick("CSBK_CRC", phys_lcn=1)
    app._rf_quality_tick("CSBK_CRC", phys_lcn=1)
    app._rf_quality_tick("SLCO_CRC", phys_lcn=2)
    body = _c(app).get("/api/rf").get_json()
    assert body["total_errors"] == 3   # הצובר הגלובלי כולל את כל הערוצים יחד
    by_lcn = {d["phys_lcn"]: d["total_errors"] for d in body["by_channel"]}
    assert by_lcn == {1: 2, 2: 1}


def test_api_gain_sends_and_tracks(paths, monkeypatch):
    app = paths
    sent = []
    monkeypatch.setattr(app.dsd_pty, "send_gain_nudge", lambda d: sent.append(d) or True)
    r = _c(app).post("/api/gain", json={"direction": "up"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["gain_nudge"] == 1
    assert sent == ["up"]
    # ירידה שנייה
    r2 = _c(app).post("/api/gain", json={"direction": "down"})
    assert r2.get_json()["gain_nudge"] == 0


def test_api_gain_invalid_direction(paths):
    r = _c(paths).post("/api/gain", json={"direction": "sideways"})
    assert r.status_code == 400


def test_api_gain_send_failure(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app.dsd_pty, "send_gain_nudge", lambda d: False)
    r = _c(app).post("/api/gain", json={"direction": "up"})
    assert r.status_code == 500
    st = json.loads(app.STATE_PATH.read_text()) if app.STATE_PATH.exists() else {}
    assert st.get("gain_nudge", 0) == 0   # לא עודכן — השליחה נכשלה


def test_api_gain_clamped_to_range(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app.dsd_pty, "send_gain_nudge", lambda d: True)
    app.save_state({**app.load_state(), "gain_nudge": app.GAIN_NUDGE_MAX})
    r = _c(app).post("/api/gain", json={"direction": "up"})
    assert r.get_json()["gain_nudge"] == app.GAIN_NUDGE_MAX   # לא חורג מהתקרה


def test_gain_nudge_resets_on_dmr_entry(paths, sysctl, no_sleep):
    app = paths
    app.SYSTEMS_PATH.write_text(json.dumps(
        [{"id": "s1", "name": "T", "control": 461.0, "color_code": 1, "channelmap": []}]))
    app.save_state({"app_mode": "off", "gain_nudge": 15})
    r = _c(app).post("/api/mode", json={"mode": "dmr", "system": "s1"})
    assert r.status_code == 200
    st = json.loads(app.STATE_PATH.read_text())
    assert st["gain_nudge"] == 0
