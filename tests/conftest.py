import sys
import types
from pathlib import Path

import pytest

# app.py יושב ב-webtune/ (לא חבילה מותקנת) => מוסיפים ל-path לפני import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webtune"))


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """מפנה את כל נתיבי-המודול ל-tmp_path => שום בדיקה לא כותבת ל-/etc או /var.
    מחזיר את app אחרי הפניית הנתיבים (כולל מודול האליאסים)."""
    import app
    import aliases

    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "DMR_ENV_PATH", tmp_path / "dmr.env")
    monkeypatch.setattr(app, "CHANNELMAP_PATH", tmp_path / "channelmap.csv")
    monkeypatch.setattr(app, "DMR_LOG_PATH", tmp_path / "dmr.jsonl")
    monkeypatch.setattr(app, "SYSTEMS_PATH", tmp_path / "systems.json")
    monkeypatch.setattr(app, "ACTIVITY_PATH", tmp_path / "activity.jsonl")
    monkeypatch.setattr(app, "REC_DIR", tmp_path / "recordings")

    monkeypatch.setattr(aliases, "MANUAL_PATH", tmp_path / "aliases.json")
    monkeypatch.setattr(aliases, "RID_CSV", tmp_path / "rid.csv")
    monkeypatch.setattr(aliases, "TG_CSV", tmp_path / "tg.csv")
    # מאתחל את מצב האליאסים לזיכרון נקי לכל בדיקה
    aliases._tg_import.clear(); aliases._rid_import.clear()
    aliases._tg_manual.clear(); aliases._rid_manual.clear()
    return app


@pytest.fixture
def no_sleep(monkeypatch):
    """מנטרל time.sleep => לולאות ה-poll (restart-verify וכו') רצות מיידית."""
    import app
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)


class Recorder:
    """מוקק ל-_sysctl: רושם (action, service) ומחזיר returncode מזויף."""
    def __init__(self):
        self.calls = []

    def __call__(self, action, service, timeout=45):
        self.calls.append((action, service))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")


@pytest.fixture
def sysctl(paths, monkeypatch):
    """מחליף את _sysctl ב-Recorder; ברירת מחדל: SDR נוכח, השירות מדווח active
    אחרי restart שנרשם. בדיקות מדייקות עוד יותר לפי צורך."""
    app = paths
    rec = Recorder()
    monkeypatch.setattr(app, "_sysctl", rec)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_journal_tail", lambda *a, **k: "")
    # active אם ראינו restart של השירות (ולא stop אחריו)
    def is_active(svc):
        last = None
        for act, s in rec.calls:
            if s == svc:
                last = act
        return last == "restart"
    monkeypatch.setattr(app, "_is_active", is_active)
    return rec
