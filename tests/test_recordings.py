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
