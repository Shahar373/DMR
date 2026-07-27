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


# --- ★ v0.14.0: עוצמה נמדדת + בקרת AGC מפורשת -------------------------------

def test_api_rf_exposes_measured_level(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_bridge_levels", lambda: {
        "frontend": {"rms_dbfs": -21.4, "peak_dbfs": -6.0, "clip_frac": 0.0},
        "channels": [{"lcn": 3, "freq_hz": 164_325_000, "dbfs": -44.1,
                      "peak_dbfs": -41.0}],
        "gain": {"agc": True, "index": 14, "index_max": 28, "readback": False},
    })
    body = _c(app).get("/api/rf").get_json()
    assert body["level"]["rms_dbfs"] == -21.4
    assert body["level"]["clip_frac"] == 0.0
    assert body["level_by_channel"][0]["lcn"] == 3
    assert body["gain"]["agc"] is True
    # ⚠ המדד לא מתחזה למדוד-מהחומרה: הדגל חייב להישאר גלוי
    assert body["gain"]["readback"] is False


def test_api_rf_level_is_null_not_invented_when_bridge_is_down(paths, monkeypatch):
    """CLAUDE.md §8: מוטב None על פני מספר מומצא. זה בדיוק הבאג של הקבוע
    `-50.0` שהיה ב-rsp_fm.py — כשהגשר לא רץ אין עוצמה, ואומרים את זה."""
    app = paths
    monkeypatch.setattr(app, "_bridge_levels", lambda: None)
    body = _c(app).get("/api/rf").get_json()
    assert body["ok"] and body["level"] is None
    assert body["level_by_channel"] == [] and body["gain"] is None


def test_api_gain_enables_agc_and_clears_relative_counter(paths, monkeypatch):
    """★ החזרה ל-AGC לא הייתה אפשרית לפני v0.14.0 (רק restart מלא)."""
    app = paths
    sent = []
    monkeypatch.setattr(app.dsd_pty, "send_gain_nudge", lambda d: True)
    monkeypatch.setattr(app.dsd_pty, "send_gain_command",
                        lambda p, **kw: sent.append(p) or True)
    _c(app).post("/api/gain", json={"direction": "up"})
    r = _c(app).post("/api/gain", json={"agc": True})
    assert r.status_code == 200 and r.get_json()["agc"] is True
    assert sent == [b"agc:on"]
    st = json.loads(app.STATE_PATH.read_text())
    assert st["gain_nudge"] == 0        # המונה היחסי כבר לא רלוונטי תחת AGC


def test_api_gain_disables_agc(paths, monkeypatch):
    app = paths
    sent = []
    monkeypatch.setattr(app.dsd_pty, "send_gain_command",
                        lambda p, **kw: sent.append(p) or True)
    assert _c(app).post("/api/gain", json={"agc": False}).status_code == 200
    assert sent == [b"agc:off"]


def test_api_gain_sets_absolute_index(paths, monkeypatch):
    app = paths
    sent = []
    monkeypatch.setattr(app.dsd_pty, "send_gain_command",
                        lambda p, **kw: sent.append(p) or True)
    r = _c(app).post("/api/gain", json={"index": 22})
    assert r.status_code == 200 and r.get_json()["index"] == 22
    assert sent == [b"gain:22"]


def test_api_gain_rejects_index_out_of_range(paths):
    assert _c(paths).post("/api/gain", json={"index": 99}).status_code == 400
    assert _c(paths).post("/api/gain", json={"index": -1}).status_code == 400
    assert _c(paths).post("/api/gain", json={"index": "loud"}).status_code == 400


def test_api_gain_agc_failure_is_reported(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app.dsd_pty, "send_gain_command", lambda p, **kw: False)
    assert _c(app).post("/api/gain", json={"agc": True}).status_code == 500
