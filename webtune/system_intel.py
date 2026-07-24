#!/usr/bin/env python3
# ============================================================================
#  DMR  -  מודיעין-מערכת (system intel): מעשיר את "בנק התדרים" (systems.json)
# ----------------------------------------------------------------------------
#  systems.json הוא config סטטי שהמשתמש עורך (id/name/control/color_code/
#  channelmap). המודול הזה הוא ה"צד השני" — ידע שנצבר לאורך זמן מטלמטריית
#  ערוץ-הבקרה שנצפית בפועל (ר' dsd_pty.py: lsn_status/bank_call/
#  preamble_csbk/site_info, כל אלה נצפו בקליטה אמיתית מ-Cap+/SLCO, לא ניחוש):
#    - אתרים (site_info) — לאילו אתרים RID-ים "מתגלגלים" לאורך זמן (§10).
#    - מפת-תפוסה חיה פר-LSN (lsn_status) — מה קורה על כל ערוץ, בלי לתפוס אותו.
#    - CDR לשיחות-יחיד (preamble_csbk/bank_call) — מי→מי, גם בלי לשמוע.
#    - זיהוי-סחיפת Color-Code (record_cc) — נגד קונפיג שגוי/הדלפת-רשת-שכנה.
#  שני-הכיוונים: config נקרא (color_code להשוואה), והמודיעין נצבר בחזרה —
#  אבל **אף פעם לא נכתב חזרה ל-systems.json עצמו** (אין החלפה-מלאה שתסכן
#  קונפיג-משתמש); זה קובץ-state נפרד, בדיוק כמו aliases.json/discovery.json.
#
#  ⚠ lsn_status הוא טלמטריה תכופה מאוד (ראה dsd_pty.py — ~חצי מהפלט האמיתי) —
#  כתיבה-לדיסק על כל אירוע הייתה שוחקת כרטיס-SD. in-memory תמיד מעודכן;
#  דיסק נכתב debounced (FLUSH_MIN_INTERVAL_SEC) דרך maybe_flush().
#
#  בטיחות: קריאה/כתיבה מקבצים שבבעלות dmr בלבד; כתיבה אטומית; אין הרצת קוד.
# ============================================================================
import json
import os
import threading
import time
from pathlib import Path

INTEL_PATH = Path("/var/lib/dmr/system_intel.json")
FLUSH_MIN_INTERVAL_SEC = 15.0
PRIVATE_CALLS_MAX = 200      # ring buffer, פר-מערכת
SITES_MAX = 50
LSN_DIRECTORY_MAX = 64       # תקרה שפויה — למערכת Cap+ אין יותר LSN-ים מזה בפועל

_lock = threading.Lock()
_intel: dict = {}            # system_id -> profile
_dirty = False
_last_flush = 0.0


def _blank_profile():
    return {"sites": {}, "lsn_directory": {}, "cc": None, "private_calls": []}


def load():
    """טוען מודיעין נצבר מהדיסק. נכשל בשקט (ריק) אם הקובץ חסר/פגום — כמו
    aliases.py/watchlist.py, לא קורס על state לא-תקין."""
    global _intel
    with _lock:
        try:
            data = json.loads(INTEL_PATH.read_text())
        except Exception:
            _intel = {}
            return
        _intel = data if isinstance(data, dict) else {}


def maybe_flush(force=False):
    """כתיבה debounced לדיסק. force=True עוקף את ה-debounce (למשל בכיבוי-שרת
    נקי). אינו נועל את הדיסק תחת ה-lock — רק את הסנפשוט/הדגלים."""
    global _dirty, _last_flush
    with _lock:
        if not _dirty:
            return
        now = time.time()
        if not force and now - _last_flush < FLUSH_MIN_INTERVAL_SEC:
            return
        snapshot = dict(_intel)
        _dirty = False
        _last_flush = now
    INTEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INTEL_PATH.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False))
    os.replace(tmp, INTEL_PATH)


def _profile(system_id):
    return _intel.setdefault(system_id, _blank_profile())


def record_site(system_id, site, t=None):
    """site_info: RID-ים "מתגלגלים" בין אתרים לאורך זמן => שביל-תנועה גס
    בלי GPS/LRRP (רוב הרשתות לא משדרות LRRP סטנדרטי ממילא, ר' CLAUDE.md §5)."""
    global _dirty
    if system_id is None or site is None:
        return
    t = t if t is not None else time.time()
    with _lock:
        p = _profile(system_id)
        key = str(int(site))
        entry = p["sites"].setdefault(key, {"first_seen": t, "last_seen": t, "count": 0})
        entry["last_seen"] = t
        entry["count"] += 1
        if len(p["sites"]) > SITES_MAX:
            oldest = min(p["sites"], key=lambda k: p["sites"][k]["last_seen"])
            if oldest != key:
                del p["sites"][oldest]
        _dirty = True
    maybe_flush()


def record_lsn_status(system_id, channels, t=None):
    """channels: {lsn:int -> 'idle'|'rest'|occupant_id:int} מ-dsd_pty.
    ⚠ 'occupant_id' הוא המספר כפי-שהוא מהשידור -- לא ניתן להבחין כאן אם זה
    TG או RID-יעד של שיחת-יחיד (שני הסוגים נצפו תופסים LSN באותה צורה
    בקליטה אמיתית); לא ממציאים סיווג שלא אומת. שומר רק שינוי-מצב אמיתי
    פר-LSN (occupant השתנה) — לא כותב last_change בכל דגימה חוזרת."""
    global _dirty
    if system_id is None or not channels:
        return
    t = t if t is not None else time.time()
    with _lock:
        p = _profile(system_id)
        for lsn, occ in channels.items():
            key = str(int(lsn))
            entry = p["lsn_directory"].get(key)
            if entry is None or entry.get("occupant") != occ:
                p["lsn_directory"][key] = {"occupant": occ, "last_seen": t, "last_change": t}
            else:
                entry["last_seen"] = t
        if len(p["lsn_directory"]) > LSN_DIRECTORY_MAX:
            oldest = min(p["lsn_directory"], key=lambda k: p["lsn_directory"][k]["last_seen"])
            del p["lsn_directory"][oldest]
        _dirty = True
    maybe_flush()


def record_private_call(system_id, src, tgt, kind, rest_lsn=None, t=None):
    """CDR-שורה: מי→מי בשיחת-יחיד. ממקורות: preamble_csbk (הקמת-שיחה, src+tgt
    מלאים, גם בלי לשמוע את השיחה בכלל) או bank_call (אישור-תפוסה מתמשך,
    target בלבד — src=None). ring buffer פר-מערכת, לא לוג-אינסופי."""
    global _dirty
    if system_id is None or tgt is None:
        return
    t = t if t is not None else time.time()
    with _lock:
        p = _profile(system_id)
        p["private_calls"].append({"t": t, "src": src, "tgt": tgt, "kind": kind,
                                   "rest_lsn": rest_lsn})
        if len(p["private_calls"]) > PRIVATE_CALLS_MAX:
            p["private_calls"] = p["private_calls"][-PRIVATE_CALLS_MAX:]
        _dirty = True
    maybe_flush()


def record_cc(system_id, observed_cc, configured_cc, t=None):
    """משווה CC שנצפה בפועל (מ-quality/sync events -- כבר מפוענח ב-dsd_pty
    אך נזרק היום, ר' CLAUDE.md §8) מול ה-color_code המוגדר במערכת.
    mismatch=True => קונפיג שגוי, או הדלפת-אות ממערכת שכנה על אותו תדר."""
    global _dirty
    if system_id is None or observed_cc is None:
        return
    t = t if t is not None else time.time()
    with _lock:
        p = _profile(system_id)
        p["cc"] = {"observed": observed_cc, "configured": configured_cc,
                   "mismatch": configured_cc is not None and observed_cc != configured_cc,
                   "last_seen": t}
        _dirty = True
    maybe_flush()


def export_for(system_id):
    """מבט לתצוגה: פרופיל מערכת יחיד, private_calls מקוצר (20 אחרונים,
    החדש קודם). מחזיר פרופיל ריק (לא None) למערכת שעדיין לא נצפתה."""
    with _lock:
        p = _intel.get(system_id)
        snapshot = json.loads(json.dumps(p)) if p is not None else _blank_profile()
    snapshot["private_calls"] = list(reversed(snapshot["private_calls"][-20:]))
    return snapshot
