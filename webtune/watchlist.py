#!/usr/bin/env python3
# ============================================================================
#  DMR  -  מעקב (watchlist) RID/TG להתראה מקומית
# ----------------------------------------------------------------------------
#  רשימת RID/TG שהמשתמש רוצה להתריע עליהם, נערכת מהטלפון (/api/watchlist).
#  match() נקרא מ-app.py's _normalize_dsd כדי לתייג כל כרטיס עם ההתאמה
#  הראשונה שנמצאה (אם יש) — התג נשמר על הכרטיס (dmr.jsonl כולל), וה-UI
#  מציג התראה **מקומית בלבד** (Notification API בדף פתוח + רטט/צליל) —
#  לא Web Push (שם ה-API הסטנדרטי מחייב שרת-relay חיצוני של ספק הדפדפן,
#  בסתירה ל"פרטי-מקומי/בלי ענן" של CLAUDE.md §1). ר' §8.
#
#  בטיחות: קריאה/כתיבה מקבצים שבבעלות dmr בלבד; אין הרצת קוד.
# ============================================================================
import json
import os
import threading
from pathlib import Path

WATCHLIST_PATH = Path("/var/lib/dmr/watchlist.json")
WATCHLIST_MAX = 500   # תקרה שפויה — זו רשימת-מעקב ידנית, לא ייבוא המוני כמו aliases

_lock = threading.Lock()
_tg: set = set()
_rid: set = set()


def _clean_ids(values):
    out = set()
    for v in (values or []):
        try:
            out.add(int(v))
        except (ValueError, TypeError):
            continue
        if len(out) >= WATCHLIST_MAX:
            break
    return out


def load():
    """טוען את רשימת-המעקב לזיכרון. נכשל בשקט (רשימה ריקה) אם הקובץ חסר/פגום —
    כמו aliases.py, לעולם לא קורס על קובץ-state לא-תקין."""
    global _tg, _rid
    with _lock:
        try:
            data = json.loads(WATCHLIST_PATH.read_text())
        except Exception:
            _tg, _rid = set(), set()
            return
        if not isinstance(data, dict):
            _tg, _rid = set(), set()
            return
        _tg = _clean_ids(data.get("tg"))
        _rid = _clean_ids(data.get("rid"))


def match(tg, src, tgt):
    """(kind, id) של ההתאמה הראשונה שנמצאה (סדר-עדיפות: tg, אז src, אז tgt —
    תג יחיד לכרטיס, לא רשימה), או None. טהורה למעט קריאת הזיכרון-הפנימי (בלי
    I/O) — נבדקת ישירות, בלי Flask/קובץ."""
    with _lock:
        tgs, rids = _tg, _rid
    if tg is not None:
        try:
            n = int(tg)
        except (ValueError, TypeError):
            n = None
        if n is not None and n in tgs:
            return {"kind": "tg", "id": n}
    for val in (src, tgt):
        if val is None:
            continue
        try:
            n = int(val)
        except (ValueError, TypeError):
            continue
        if n in rids:
            return {"kind": "rid", "id": n}
    return None


def _validate(data):
    """(ok, err) — מאמת מפת מעקב {'tg':[...], 'rid':[...]}."""
    if not isinstance(data, dict):
        return False, "פורמט לא תקין"
    for kind in ("tg", "rid"):
        vals = data.get(kind, [])
        if vals is None:
            continue
        if not isinstance(vals, list):
            return False, f"שדה {kind} חייב להיות רשימה"
        if len(vals) > WATCHLIST_MAX:
            return False, "יותר מדי ערכים במעקב"
        for v in vals:
            try:
                int(v)
            except (ValueError, TypeError):
                return False, f"ערך לא-מספרי: {v}"
    return True, None


def replace(data):
    """מחליף את רשימת-המעקב (watchlist.json) ומרענן. מחזיר (ok, err)."""
    ok, err = _validate(data)
    if not ok:
        return False, err
    clean = {"tg": sorted({int(v) for v in (data.get("tg") or [])}),
             "rid": sorted({int(v) for v in (data.get("rid") or [])})}
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_PATH.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(clean, ensure_ascii=False))
    os.replace(tmp, WATCHLIST_PATH)
    load()
    return True, None


def export_all():
    """מבט מלא לעריכה בטלפון: {'tg':[...], 'rid':[...]}."""
    with _lock:
        return {"tg": sorted(_tg), "rid": sorted(_rid)}
