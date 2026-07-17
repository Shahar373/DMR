"""הקלטות per-call: watcher, sweep retention, /api/activity."""


def test_scan_new_recordings(paths):
    app = paths
    app.REC_DIR.mkdir(parents=True, exist_ok=True)
    (app.REC_DIR / "call_1.wav").write_bytes(b"x" * 100)
    rows, newest = app._scan_new_recordings(0)
    assert len(rows) == 1 and rows[0]["file"] == "call_1.wav" and rows[0]["bytes"] == 100
    assert newest > 0


def test_scan_new_recordings_incremental(paths):
    app = paths
    app.REC_DIR.mkdir(parents=True, exist_ok=True)
    (app.REC_DIR / "old.wav").write_bytes(b"x")
    _, newest = app._scan_new_recordings(0)
    # שנייה שקראנו כבר => אין חדשות
    rows, _ = app._scan_new_recordings(newest)
    assert rows == []


def test_sweep_retention(paths, monkeypatch):
    app = paths
    app.REC_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app, "REC_MAX_FILES", 2)
    for i in range(5):
        (app.REC_DIR / f"c{i}.wav").write_bytes(b"x")
    app._sweep_recordings()
    remaining = list(app.REC_DIR.glob("*.wav"))
    assert len(remaining) == 2


def test_api_activity(paths):
    app = paths
    app.REC_DIR.mkdir(parents=True, exist_ok=True)
    (app.REC_DIR / "call_1.wav").write_bytes(b"x")
    app._append_activity([{"ts": 100.0, "file": "call_1.wav", "bytes": 1}])
    body = app.app.test_client().get("/api/activity").get_json()
    assert body["ok"] and body["events"][0]["file"] == "call_1.wav"
    assert body["events"][0]["exists"] is True


# --- multi mode: per-channel WAV subdirs (Phase 2 bug #1) --------------------
def test_scan_finds_recordings_in_channel_subdirs(paths):
    """מצב multi כותב הקלטות ל-recordings/lcnN/. rglob (לא glob) חייב לתפוס
    אותן, אחרת ה-retention לא רואה אותן והדיסק מתמלא. השדה 'file' נשמר כנתיב
    יחסי (lcnN/foo.wav) כדי ש-/recordings וקישור התמלול ימצאו את הקובץ."""
    app = paths
    (app.REC_DIR / "lcn2").mkdir(parents=True, exist_ok=True)
    (app.REC_DIR / "lcn2" / "call_x.wav").write_bytes(b"x" * 50)
    rows, newest = app._scan_new_recordings(0)
    assert len(rows) == 1
    assert rows[0]["file"] == "lcn2/call_x.wav"   # נתיב יחסי, לא רק p.name
    assert newest > 0


def test_sweep_retention_across_channel_subdirs(paths, monkeypatch):
    """ה-retention חייב לספור הקלטות בכל תת-תיקיות הערוצים יחד (אחרת כל ערוץ
    ב-multi צובר בלי גבול)."""
    app = paths
    monkeypatch.setattr(app, "REC_MAX_FILES", 2)
    for lcn in (1, 2, 3):
        d = app.REC_DIR / f"lcn{lcn}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "c.wav").write_bytes(b"x")
    app._sweep_recordings()
    remaining = list(app.REC_DIR.rglob("*.wav"))
    assert len(remaining) == 2   # 3 across subdirs -> capped to 2


def test_api_activity_serves_subdir_recording(paths):
    app = paths
    (app.REC_DIR / "lcn5").mkdir(parents=True, exist_ok=True)
    (app.REC_DIR / "lcn5" / "call_y.wav").write_bytes(b"data")
    app._append_activity([{"ts": 100.0, "file": "lcn5/call_y.wav", "bytes": 4}])
    c = app.app.test_client()
    body = c.get("/api/activity").get_json()
    assert body["events"][0]["exists"] is True   # (REC_DIR / "lcn5/call_y.wav")
    # /recordings/<path:name> serves the subdir file
    r = c.get("/recordings/lcn5/call_y.wav")
    assert r.status_code == 200 and r.data == b"data"
