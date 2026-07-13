"""גילוי רשתות: לוגיקה טהורה (discovery.py) + שכבת ה-Flask/תזמור (app.py).
כל מה שתלוי חומרה (sweep/probe אמיתי) ממוקף — כמו שאר הבדיקות."""
import json
import threading
import time

import pytest

import discovery


# ------------------------------------------------------------------ טהור --
def test_validate_sweep_plan_defaults():
    plan = discovery.validate_sweep_plan({"start_mhz": 450, "end_mhz": 470})
    assert plan["start_mhz"] == 450.0 and plan["end_mhz"] == 470.0
    assert plan["iq_rate"] == discovery.DEFAULT_IQ_RATE
    assert plan["nfft"] == discovery.DEFAULT_NFFT
    assert plan["max_candidates"] == discovery.DEFAULT_MAX_CANDIDATES


def test_validate_sweep_plan_empty_uses_uhf_default():
    plan = discovery.validate_sweep_plan({})
    assert plan["start_mhz"] == 450.0 and plan["end_mhz"] == 470.0


def test_validate_sweep_plan_rejects():
    assert discovery.validate_sweep_plan({"start_mhz": 470, "end_mhz": 450}) is None
    assert discovery.validate_sweep_plan({"start_mhz": 10, "end_mhz": 20}) is None    # מתחת ל-24
    assert discovery.validate_sweep_plan({"start_mhz": 100, "end_mhz": 250}) is None  # רוחב>100
    assert discovery.validate_sweep_plan("nope") is None
    assert discovery.validate_sweep_plan({"start_mhz": "x", "end_mhz": 5}) is None


def test_validate_sweep_plan_clamps_advanced():
    plan = discovery.validate_sweep_plan(
        {"start_mhz": 450, "end_mhz": 451, "nfft": 999999, "probe_sec": 999,
         "gain_index": 99, "threshold_mad": 0.1})
    assert plan["nfft"] == 8192 and plan["probe_sec"] == 30
    assert plan["gain_index"] == 28 and plan["threshold_mad"] == 2.0


def test_build_freq_grid_covers_range():
    grid = discovery.build_freq_grid(450, 470, 2_000_000)
    assert len(grid) >= 11
    assert grid[0] < 451e6                       # החלון הראשון מתחיל בתחילת הטווח
    assert grid[-1] + 900_000 >= 470e6           # החלון האחרון מגיע לסוף
    # מונוטוני עולה
    assert all(b > a for a, b in zip(grid, grid[1:]))


def test_build_freq_grid_single_center_for_narrow():
    grid = discovery.build_freq_grid(461.0, 461.01, 2_000_000)
    assert len(grid) == 1


def _carrier_snapshot(center_hz, iq_rate, nfft, offset_hz, level_db=-60.0, floor=-120.0):
    bin_hz = iq_rate / nfft
    power = [floor] * nfft
    k = nfft // 2 + int(round(offset_hz / bin_hz))
    for b in range(k - 4, k + 5):
        if 0 <= b < nfft:
            power[b] = level_db
    return {"center_hz": center_hz, "bin_hz": bin_hz, "power_db": power}


def test_detect_candidates_finds_carrier():
    nfft, iq = 1024, 2_000_000
    snap = _carrier_snapshot(461_000_000, iq, nfft, offset_hz=200_000)
    cands = discovery.detect_candidates([snap])
    assert len(cands) == 1
    assert abs(cands[0]["freq_mhz"] - 461.2) < 0.01
    assert cands[0]["power_db"] == -60.0


def test_detect_candidates_masks_dc_spike():
    """spike ב-DC המרכזי לא נספר כמועמד (notch מרכזי)."""
    nfft, iq = 1024, 2_000_000
    snap = _carrier_snapshot(461_000_000, iq, nfft, offset_hz=0, level_db=-30.0)
    assert discovery.detect_candidates([snap]) == []


def test_detect_candidates_empty_and_flat():
    assert discovery.detect_candidates([]) == []
    flat = {"center_hz": 461_000_000, "bin_hz": 1953.0, "power_db": [-120.0] * 1024}
    assert discovery.detect_candidates([flat]) == []


def test_detect_candidates_respects_max():
    nfft, iq = 2048, 2_000_000
    # כמה נושאים מרוחקים
    snap = {"center_hz": 461_000_000, "bin_hz": iq / nfft, "power_db": [-120.0] * nfft}
    for off in range(-400_000, 400_001, 100_000):
        if off == 0:
            continue
        k = nfft // 2 + int(round(off / (iq / nfft)))
        for b in range(k - 3, k + 4):
            snap["power_db"][b] = -60.0
    cands = discovery.detect_candidates([snap], max_candidates=3)
    assert len(cands) == 3


def test_aggregate_probe_control_channel():
    events = ([{"type": "sync", "proto": "dmr", "cc": 2}] * 4
              + [{"type": "channel_status", "rest_lsn": 5, "cc": 2}]
              + [{"type": "voice_call", "tg": 3, "src": 2120, "lcn": 5, "call_type": "group"}])
    rec = discovery.aggregate_probe(461.0375, events)
    assert rec["is_dmr"] is True and rec["cc"] == 2
    assert rec["channel_type"] == "control"
    assert rec["rest_lsns"] == [5] and rec["talkgroups"] == [3] and rec["rids"] == [2120]
    assert rec["confidence"] >= 0.9


def test_aggregate_probe_conventional_and_encrypted():
    events = ([{"type": "sync", "proto": "dmr", "cc": 7}] * 3
              + [{"type": "encryption", "slot": 1, "encrypted": True}])
    rec = discovery.aggregate_probe(462.0, events)
    assert rec["channel_type"] == "conventional" and rec["cc"] == 7
    assert rec["is_dmr"] is True and rec["encrypted"] is True


def test_aggregate_probe_not_dmr():
    rec = discovery.aggregate_probe(455.0, [{"type": "sync", "proto": "dmr"}])   # sync בודד
    assert rec["is_dmr"] is False and rec["cc"] is None
    assert rec["channel_type"] == "conventional"
    empty = discovery.aggregate_probe(455.0, [])
    assert empty["is_dmr"] is False and empty["channel_type"] is None


def test_discovery_to_system_shape_and_validates(paths):
    app = paths
    rec = {"freq_mhz": 461.0375, "cc": 2, "channel_type": "control"}
    system = discovery.discovery_to_system(rec, name="רשת בדיקה")
    assert system["control"] == 461.0375 and system["color_code"] == 2
    assert system["channelmap"] == []
    ok, cleaned = app._validate_systems([system])
    assert ok and cleaned[0]["id"] == system["id"]


def test_discovery_to_system_null_cc_defaults_zero(paths):
    system = discovery.discovery_to_system({"freq_mhz": 461.0, "cc": None})
    assert system["color_code"] == 0
    ok, _ = paths._validate_systems([system])
    assert ok


# ------------------------------------------------------- Flask / תזמור --
@pytest.fixture(autouse=True)
def _reset_discover(paths):
    app = paths
    app._discover_active = False
    app._discover_report = None
    app._discover_thread = None
    app._discover_thread_stop = None
    with app._discover_acc_lock:
        app._discover_acc.clear()
    app._discover_status.update(stage="idle", progress=0.0, current_mhz=None,
                                candidates=0, probed=0, results=[])
    yield
    app._discover_active = False


def _send_udp(port, obj):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(json.dumps(obj).encode(), ("127.0.0.1", port))
    sock.close()


def test_api_mode_discover_starts(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    seen = []

    def fake_activate(plan):
        app._discover_active = True
        with app._discover_lock:
            app._discover_status.update(stage="sweep", plan=plan)
        seen.append(plan)
        return None, None

    monkeypatch.setattr(app, "_discover_activate", fake_activate)
    c = app.app.test_client()
    r = c.post("/api/mode", json={"mode": "discover",
                                  "plan": {"start_mhz": 450, "end_mhz": 470}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["app_mode"] == "discover"
    assert seen and seen[0]["start_mhz"] == 450.0
    # api_state/health משקפים discover (לא נשמר ל-state)
    st = c.get("/api/state").get_json()
    assert st["app_mode"] == "discover" and st["mode_ok"] is True
    assert st["discover"]["stage"] == "sweep"
    hp = c.get("/api/health").get_json()
    assert hp["app_mode"] == "discover"


def test_api_mode_discover_invalid_range(paths, sysctl):
    r = paths.app.test_client().post(
        "/api/mode", json={"mode": "discover", "plan": {"start_mhz": 470, "end_mhz": 450}})
    assert r.status_code == 400


def test_api_mode_off_stops_running_discover(paths, sysctl, no_sleep):
    app = paths
    app._discover_active = True
    stop = threading.Event()
    thread = threading.Thread(target=lambda: stop.wait(5), daemon=True)
    thread.start()
    with app._discover_lock:
        app._discover_thread, app._discover_thread_stop = thread, stop
    r = app.app.test_client().post("/api/mode", json={"mode": "off"})
    assert r.status_code == 200
    assert app._discover_active is False
    assert stop.is_set() and not thread.is_alive()


def test_discover_collect_via_listener(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15571)
    with app._dmr_lock:
        app._dmr_msgs.clear()
    app._discover_active = True
    epoch = app._discover_begin_probe()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    now = time.time()
    _send_udp(15571, {"type": "sync", "proto": "dmr", "cc": 2, "t": now})
    _send_udp(15571, {"type": "channel_status", "rest_lsn": 5, "cc": 2, "t": now})
    _send_udp(15571, {"type": "voice_call", "tg": 3, "src": 2120, "slot": 1,
                      "call_type": "group", "lcn": 5, "t": now})
    time.sleep(0.4)
    events = app._discover_take_events(epoch)
    types = {e["type"] for e in events}
    assert {"sync", "channel_status", "voice_call"} <= types
    rec = app.discmod.aggregate_probe(461.0, events)
    assert rec["is_dmr"] and rec["cc"] == 2 and rec["channel_type"] == "control"
    # sync/channel_status אינם הופכים לכרטיסים (רק ה-voice_call)
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 1


def test_discover_loop_end_to_end(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    nfft, iq = 512, 2_000_000
    snap = _carrier_snapshot(461_000_000, iq, nfft, offset_hz=200_000)
    monkeypatch.setattr(app, "_sweep_read", lambda center: snap)

    def fake_probe(stop_evt, cand, plan):
        return app.discmod.aggregate_probe(
            cand["freq_mhz"],
            [{"type": "sync", "proto": "dmr", "cc": 2}] * 4
            + [{"type": "channel_status", "rest_lsn": 5, "cc": 2}],
            min_sync=plan["min_sync"])

    monkeypatch.setattr(app, "_probe_candidate", fake_probe)
    plan = discovery.validate_sweep_plan({"start_mhz": 460, "end_mhz": 462, "nfft": nfft})
    app._discover_active = True
    app._discover_loop(threading.Event(), plan)
    report = app._load_discovery_report()
    assert report and report["candidate_count"] >= 1
    assert report["networks"] and report["networks"][0]["cc"] == 2
    assert app.load_state()["app_mode"] == "off"          # standby בסיום
    assert app._discover_active is False


def test_api_discover_status_and_save(paths, sysctl):
    app = paths
    app._discover_report = {
        "candidate_count": 1,
        "results": [{"freq_mhz": 461.0375, "is_dmr": True, "cc": 2,
                     "channel_type": "control", "rest_lsns": [5],
                     "talkgroups": [3], "rids": [2120]}],
        "networks": [{"freq_mhz": 461.0375, "cc": 2}],
    }
    c = app.app.test_client()
    status = c.get("/api/discover").get_json()
    assert status["ok"] and status["report"]["candidate_count"] == 1

    r = c.post("/api/discover/save", json={"freq_mhz": 461.0375, "name": "רשת א"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["system"]["control"] == 461.0375
    assert body["system"]["color_code"] == 2 and body["system"]["name"] == "רשת א"
    saved = json.loads(app.SYSTEMS_PATH.read_text())
    assert any(abs(s["control"] - 461.0375) < 1e-6 for s in saved)


def test_api_discover_save_no_report(paths, sysctl):
    r = paths.app.test_client().post("/api/discover/save", json={"freq_mhz": 461.0})
    assert r.status_code == 400


def test_api_discover_save_unknown_record(paths, sysctl):
    app = paths
    app._discover_report = {"results": [{"freq_mhz": 461.0, "is_dmr": True}]}
    r = app.app.test_client().post("/api/discover/save", json={"freq_mhz": 999.0})
    assert r.status_code == 400
