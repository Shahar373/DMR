"""Phase 2/3: ניתוח הצפנה, אנליטיקת תעבורה, גרף RID↔TG, מפת LRRP.
כל הפונקציות טהורות (מקבלות רשימת רשומות) => נבדקות בלי חומרה."""
import json
import time


def _c(app):
    return app.app.test_client()


def _write_log(app, recs):
    app.DMR_LOG_PATH.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


# --- _encryption_stats -------------------------------------------------------
def test_encryption_stats_basic(paths):
    app = paths
    recs = [
        {"call_type": "group", "tg": 100, "tg_alias": "A", "encrypted": False},
        {"call_type": "group", "tg": 100, "tg_alias": "A", "encrypted": True,
         "enc": {"alg_name": "AES-256"}},
        {"call_type": "group", "tg": 200, "encrypted": True, "enc": {"alg_name": "AES-256"}},
        {"call_type": "control"},   # לא נספר (לא group/private)
    ]
    stats = app._encryption_stats(recs)
    assert stats["total"] == 3 and stats["encrypted_total"] == 2
    assert stats["encrypted_pct"] == round(200 / 3, 1)
    assert {"alg_name": "AES-256", "count": 2} in stats["by_alg"]
    tg100 = next(t for t in stats["by_tg"] if t["tg"] == 100)
    assert tg100["total"] == 2 and tg100["encrypted"] == 1 and tg100["clear"] == 1
    assert tg100["pct"] == 50.0 and tg100["tg_alias"] == "A"


def test_encryption_stats_empty(paths):
    app = paths
    stats = app._encryption_stats([])
    assert stats["total"] == 0 and stats["encrypted_pct"] == 0.0 and stats["by_alg"] == []


def test_encryption_stats_never_invents_alg(paths):
    """הודעה מוצפנת בלי enc => עדיין נספרת, בלי שם אלגוריתם מומצא (fallback גנרי)."""
    app = paths
    stats = app._encryption_stats([{"call_type": "group", "tg": 1, "encrypted": True}])
    assert stats["by_alg"] == [{"alg_name": "מוצפן", "count": 1}]


def test_api_analytics_encryption(paths):
    app = paths
    _write_log(app, [{"t": time.time(), "call_type": "group", "tg": 1, "encrypted": True,
                      "enc": {"alg_name": "DES"}}])
    body = _c(app).get("/api/analytics/encryption?all=1").get_json()
    assert body["ok"] and body["encrypted_total"] == 0   # all=1 קורא מ-_dmr_msgs (זיכרון), לא מהדיסק


def test_api_analytics_encryption_day(paths):
    app = paths
    b = app._day_bounds("2026-07-12")
    mid = (b[0] + b[1]) / 2
    _write_log(app, [{"t": mid, "call_type": "group", "tg": 1, "encrypted": True,
                      "enc": {"alg_name": "DES"}}])
    body = _c(app).get("/api/analytics/encryption?day=2026-07-12").get_json()
    assert body["ok"] and body["encrypted_total"] == 1
    assert body["by_alg"] == [{"alg_name": "DES", "count": 1}]


def test_api_analytics_bad_day(paths):
    assert _c(paths).get("/api/analytics/encryption?day=bad").status_code == 400
    assert _c(paths).get("/api/analytics/traffic?day=bad").status_code == 400
    assert _c(paths).get("/api/analytics/graph?day=bad").status_code == 400


# --- _traffic_stats -----------------------------------------------------------
def test_traffic_stats_by_tg_and_hourly(paths, monkeypatch):
    app = paths
    lt9 = time.struct_time((2026, 7, 12, 9, 0, 0, 6, 193, -1))
    monkeypatch.setattr(app.time, "localtime", lambda t=None: lt9)
    recs = [
        {"call_type": "group", "tg": 1, "tg_alias": "Ops", "dur": 5.0, "t": 1000},
        {"call_type": "group", "tg": 1, "dur": 3.0, "t": 1001},
        {"call_type": "private", "tg": None, "dur": 2.0, "t": 1002},   # לא נספר ב-by_tg (בלי tg)
    ]
    stats = app._traffic_stats(recs)
    assert stats["by_tg"][0]["tg"] == 1 and stats["by_tg"][0]["calls"] == 2
    assert stats["by_tg"][0]["airtime"] == 8.0 and stats["by_tg"][0]["tg_alias"] == "Ops"
    assert stats["hourly"][9] == 3 and stats["total_calls"] == 3


def test_traffic_stats_missing_dur_is_zero_not_invented(paths):
    app = paths
    stats = app._traffic_stats([{"call_type": "group", "tg": 5, "t": 100}])
    assert stats["by_tg"][0]["airtime"] == 0.0


def test_api_analytics_traffic(paths):
    app = paths
    b = app._day_bounds("2026-07-12")
    mid = (b[0] + b[1]) / 2
    _write_log(app, [{"t": mid, "call_type": "group", "tg": 42, "dur": 10.0}])
    body = _c(app).get("/api/analytics/traffic?day=2026-07-12").get_json()
    assert body["ok"] and body["by_tg"][0]["tg"] == 42 and body["by_tg"][0]["airtime"] == 10.0
    assert len(body["hourly"]) == 24


# --- _rid_tg_graph -------------------------------------------------------------
def test_rid_tg_graph_weights(paths):
    app = paths
    recs = [
        {"call_type": "group", "src": 5, "tg": 100, "src_alias": "Alice", "tg_alias": "Ops"},
        {"call_type": "group", "src": 5, "tg": 100},
        {"call_type": "group", "src": 5, "tg": 200},
        {"call_type": "private", "src": 5, "tgt": 6},   # לא נספר (לא group)
        {"call_type": "group", "src": None, "tg": 300},  # בלי RID => לא נספר
    ]
    edges = app._rid_tg_graph(recs)
    assert edges[0] == {"rid": 5, "rid_alias": "Alice", "tg": 100, "tg_alias": "Ops", "weight": 2}
    assert {"rid": 5, "rid_alias": "Alice", "tg": 200, "tg_alias": None, "weight": 1} in edges
    assert len(edges) == 2


def test_api_analytics_graph(paths):
    app = paths
    with app._dmr_lock:
        app._dmr_msgs.append({"id": 1, "call_type": "group", "src": 7, "tg": 1})
    body = _c(app).get("/api/analytics/graph?all=1").get_json()
    assert body["ok"] and body["edges"][0]["rid"] == 7


# --- _lrrp_snapshot / positions -------------------------------------------------
def test_lrrp_snapshot_latest_wins(paths):
    app = paths
    with app._dmr_lock:
        app._dmr_msgs.clear()
        app._dmr_msgs.append({"id": 1, "src": 9, "lat": 32.0, "lon": 34.8, "t": 100, "src_alias": "X"})
        app._dmr_msgs.append({"id": 2, "src": 9, "lat": 32.1, "lon": 34.9, "t": 200})
    snap = app._lrrp_snapshot()
    assert snap[9]["lat"] == 32.1 and snap[9]["t"] == 200


def test_lrrp_snapshot_ignores_no_position(paths):
    app = paths
    with app._dmr_lock:
        app._dmr_msgs.clear()
        app._dmr_msgs.append({"id": 1, "src": 9, "tg": 1})
    assert app._lrrp_snapshot() == {}


def test_api_positions(paths):
    app = paths
    with app._dmr_lock:
        app._dmr_msgs.clear()
        app._dmr_msgs.append({"id": 1, "src": 9, "lat": 32.0, "lon": 34.8, "t": 100})
    body = _c(app).get("/api/positions").get_json()
    assert body["ok"] and body["positions"]["9"]["lat"] == 32.0
