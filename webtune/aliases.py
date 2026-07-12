#!/usr/bin/env python3
# ============================================================================
#  DMR  -  ניהול אליאסים (Talkgroup / Radio-ID)
# ----------------------------------------------------------------------------
#  "TG 2451 / RID 3141592" חסר משמעות; "מוקד / KJ4XYZ" זהב. מודול זה ממזג שני
#  מקורות שמות ומחזיר שם לכל TG/RID דרך tg_name()/rid_name() (join ב-_normalize_dsd):
#    1. ייבוא CSV (למשל user.csv של RadioID.net ל-RID, ו-tg.csv ל-talkgroups) —
#       /etc/dmr/rid.csv ו-/etc/dmr/tg.csv (נזרעים ריקים ע"י install.sh).
#    2. עריכות ידניות מהטלפון — /var/lib/dmr/aliases.json (גובר על הייבוא).
#
#  בטיחות: הכל קריאה בלבד מקבצים שבבעלות dmr; אין הרצת קוד. נכשל בשקט (בלי שמות
#  => מוצג המספר הגולמי, כמו ב-AIR-AM: "לעולם לא ממציאים ערך").
# ============================================================================
import csv
import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("dmr")

RID_CSV = Path(os.environ.get("DMR_RID_CSV", "/etc/dmr/rid.csv"))
TG_CSV = Path(os.environ.get("DMR_TG_CSV", "/etc/dmr/tg.csv"))
MANUAL_PATH = Path("/var/lib/dmr/aliases.json")
ALIAS_MAX = 20000        # תקרה שפויה (RadioID.net user.csv ענק => טוענים עד כאן)
NAME_MAX = 64

_lock = threading.Lock()
_tg_import: dict = {}    # int -> name (מ-CSV)
_rid_import: dict = {}   # int -> name (מ-CSV)
_tg_manual: dict = {}    # int -> name (עריכות ידניות, גובר)
_rid_manual: dict = {}

# עמודות אפשריות בקובצי CSV (RadioID.net user.csv, chirp, וכו') — best-effort.
# הסדר הוא *עדיפות*: הראשון שנמצא בכותרת נבחר (callsign עדיף על name ל-RID).
_ID_COLS = ("radio_id", "dmr_id", "rid", "tgid", "talkgroup", "tg", "id", "number")
_NAME_COLS = ("callsign", "alias", "name", "call", "label", "fname", "description")


def _clean_name(s):
    s = str(s or "").strip()
    return s[:NAME_MAX] or None


def _load_csv(path, target):
    """טוען קובץ CSV (עם/בלי כותרת) ל-target[int]=name. best-effort — סובל
    פורמטים שונים ושורות פגומות. מזהה עמודות ID/שם לפי כותרת, אחרת 2 ראשונות."""
    target.clear()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        # זיהוי כותרת: אם השורה הראשונה מכילה שם-עמודה מוכר
        first = text.splitlines()[:1]
        has_header = bool(first) and any(
            c in first[0].lower() for c in _ID_COLS + _NAME_COLS)
        reader = csv.reader(text.splitlines())
        rows = list(reader)
    except Exception:
        return
    if not rows:
        return
    id_idx, name_idx = 0, 1
    start = 0
    if has_header:
        header = [h.strip().lower() for h in rows[0]]
        start = 1
        # בוחרים לפי *עדיפות* העמודה (הסדר ב-_ID_COLS/_NAME_COLS), לא לפי מיקום —
        # כך callsign עדיף על name ל-RID גם כשהוא לא ראשון בכותרת.
        for col in _ID_COLS:
            if col in header:
                id_idx = header.index(col)
                break
        for col in _NAME_COLS:
            if col in header:
                name_idx = header.index(col)
                break
    for row in rows[start:]:
        if len(row) <= max(id_idx, name_idx):
            continue
        try:
            rid = int(str(row[id_idx]).strip())
        except (ValueError, TypeError):
            continue
        name = _clean_name(row[name_idx])
        if name:
            target[rid] = name
            if len(target) >= ALIAS_MAX:
                break


def _load_manual():
    global _tg_manual, _rid_manual
    _tg_manual, _rid_manual = {}, {}
    try:
        data = json.loads(MANUAL_PATH.read_text())
    except Exception:
        return
    for kind, target in (("tg", _tg_manual), ("rid", _rid_manual)):
        d = data.get(kind) if isinstance(data, dict) else None
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            try:
                num = int(k)
            except (ValueError, TypeError):
                continue
            name = _clean_name(v)
            if name:
                target[num] = name


def load():
    """טוען/מרענן את כל מקורות האליאסים לזיכרון. נקרא בעלייה וכל עדכון."""
    with _lock:
        _load_csv(RID_CSV, _rid_import)
        _load_csv(TG_CSV, _tg_import)
        _load_manual()
    log.info("aliases: %d RID + %d TG מיובאים, %d+%d ידניים",
             len(_rid_import), len(_tg_import), len(_rid_manual), len(_tg_manual))


def tg_name(tg):
    if tg is None:
        return None
    try:
        n = int(tg)
    except (ValueError, TypeError):
        return None
    with _lock:
        return _tg_manual.get(n) or _tg_import.get(n)


def rid_name(rid):
    if rid is None:
        return None
    try:
        n = int(rid)
    except (ValueError, TypeError):
        return None
    with _lock:
        return _rid_manual.get(n) or _rid_import.get(n)


def _validate_manual(data):
    """(ok, err) — מאמת מפת עריכות ידניות {'tg':{...},'rid':{...}}."""
    if not isinstance(data, dict):
        return False, "פורמט לא תקין"
    for kind in ("tg", "rid"):
        d = data.get(kind, {})
        if d is None:
            continue
        if not isinstance(d, dict):
            return False, f"שדה {kind} חייב להיות אובייקט"
        if len(d) > ALIAS_MAX:
            return False, "יותר מדי אליאסים"
        for k, v in d.items():
            try:
                int(k)
            except (ValueError, TypeError):
                return False, f"מזהה לא-מספרי: {k}"
            if not _clean_name(v):
                return False, "שם ריק"
    return True, None


def replace_manual(data):
    """מחליף את מפת העריכות הידניות (aliases.json) ומרענן. מחזיר (ok, err)."""
    ok, err = _validate_manual(data)
    if not ok:
        return False, err
    clean = {"tg": {}, "rid": {}}
    for kind in ("tg", "rid"):
        for k, v in (data.get(kind) or {}).items():
            clean[kind][str(int(k))] = _clean_name(v)
    MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANUAL_PATH.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(clean, ensure_ascii=False))
    os.replace(tmp, MANUAL_PATH)
    load()
    return True, None


def export_all():
    """מחזיר מבט מלא לעריכה בטלפון: {'tg':{id:name}, 'rid':{id:name},
    'counts':{...}}. מציג רק את הידניים למען עריכה (הייבוא ענק) + ספירות."""
    with _lock:
        return {
            "tg": {str(k): v for k, v in _tg_manual.items()},
            "rid": {str(k): v for k, v in _rid_manual.items()},
            "counts": {"tg_import": len(_tg_import), "rid_import": len(_rid_import),
                       "tg_manual": len(_tg_manual), "rid_manual": len(_rid_manual)},
        }
