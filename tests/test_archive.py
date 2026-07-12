"""ארכיון חיפוש רב-יומי + פיד + ייצוא + רוסטר."""
import json
import time


def _c(app):
    return app.app.test_client()


def _write_log(app, recs):
    app.DMR_LOG_PATH.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def test_day_bounds_valid():
    import app
    b = app._day_bounds("2026-07-12")
    assert b is not None and b[1] > b[0]
    assert app._day_bounds("bad") is None


def test_api_dmr_day_filter(paths):
    app = paths
    b = app._day_bounds("2026-07-12")
    mid = (b[0] + b[1]) / 2
    _write_log(app, [
        {"t": mid, "tg": 1, "src": 2, "call_type": "group"},
        {"t": b[0] - 100, "tg": 9, "src": 9, "call_type": "group"},   # יום קודם
    ])
    body = _c(app).get("/api/dmr?day=2026-07-12").get_json()
    assert body["ok"] and len(body["messages"]) == 1 and body["messages"][0]["tg"] == 1


def test_api_dmr_bad_day(paths):
    assert _c(paths).get("/api/dmr?day=nope").status_code == 400


def test_api_dmr_export_csv(paths):
    app = paths
    _write_log(app, [{"t": time.time(), "tg": 2451, "src": 3141592, "call_type": "group",
                      "enc": {"alg_name": "AES-256"}, "category": "שיחת קבוצה"}])
    r = _c(app).get("/api/dmr/export?format=csv")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert body.startswith("﻿")            # BOM ל-Excel
    assert "2451" in body and "AES-256" in body   # alg_name מחושב מתוך enc


def test_api_dmr_export_json(paths):
    app = paths
    _write_log(app, [{"t": time.time(), "tg": 1, "src": 2}])
    r = _c(app).get("/api/dmr/export?format=json")
    assert r.status_code == 200 and isinstance(r.get_json(), list)


def test_roster_identity_fusion(paths):
    app = paths
    with app._dmr_lock:
        app._dmr_msgs.clear()
        for m in [
            {"id": 1, "t": 100, "src": 5, "tg": 100, "src_alias": "Alice", "encrypted": False},
            {"id": 2, "t": 200, "src": 5, "tg": 200, "encrypted": True},
            {"id": 3, "t": 150, "tg": 300, "src": None},
        ]:
            app._dmr_msgs.append(m)
    roster = app._build_roster()
    rid5 = next(c for c in roster if c["kind"] == "rid" and c["id"] == 5)
    assert rid5["count"] == 2 and rid5["encrypted_seen"] is True
    assert set(rid5["tgs"]) == {100, 200} and rid5["alias"] == "Alice"


def test_api_roster_endpoint(paths):
    app = paths
    body = _c(app).get("/api/roster").get_json()
    assert body["ok"] and isinstance(body["roster"], list)
