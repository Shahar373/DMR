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
#    - ★ מיפוי LSN↔תדר אמיתי (record_rest_channel) — ר' הבלוק הבא.
#
#  ★ מיפוי LSN↔תדר (v0.12.0) — למה זה עובד עכשיו וקודם לא:
#  טלמטריית ערוץ-הבקרה של Cap+ (lsn_status/site_info/preamble_csbk) מודיעה
#  איזה LSN הוא ה-Rest כרגע, אבל **לא** באיזה תדר הוא. עד multi mode לא היה
#  איך לדעת: SDR יחיד שומע ערוץ אחד, ואם הוא שומע את הבקרה הוא לא שומע את
#  הקול (ר' CLAUDE.md §8 — הכלל ההיסטורי "מיפוי LCN↔תדר אינו בר-גילוי").
#  ב-multi mode זה נשבר לטובה: לכל ערוץ פיזי יש מפענח נפרד, ו-dsd_pty.tag_event
#  מחתים **כל** אירוע ב-phys_freq_hz של המפענח שהפיק אותו (ground-truth מזמן
#  spawn, לא ניחוש-טקסט). וטלמטריית CSBK יכולה להגיע **רק** מהערוץ הפיזי שנושא
#  את ה-Rest LSN => phys_freq_hz של אירוע lsn_status *הוא* התדר של ה-Rest LSN
#  שמסומן בתוכו. אין כאן ניחוש — רק צירוף שני נתונים שכבר היו לנו (אחד מהם
#  נזרק). נצבר כהצבעות ולא כאמת-מדגם-בודד, כי אנומליה יחידה לא צריכה לקבע מפה.
#
#  ומכיוון שב-Cap+ כל תדר נושא **שני** LSN-ים (LSN 1+2 = תדר אחד, 3+4 = הבא,
#  וכן הלאה — אומת מול קוד ה-C של DSD-FME: dmr_csbk.c מאנדקס
#  trunk_chan_map[LSN] ומחשב slot לפי זוגיות ה-LSN, ומול תיעוד ה-upstream) —
#  כל תצפית-Rest מקבעת **זוג** LSN-ים לתדר אחד. לכן derive_lsn_map מסיק גם את
#  ה-LSN השותף (source="pair"), ומסמן סתירת-זוג אם שני חצאי-הזוג נצפו על
#  תדרים שונים (=> הרשת אינה Cap+ סטנדרטי, או שהקליטה מזוהמת).
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
LSN_MAX = 32                 # DSD-FME עצמו מטפל ב-16 LSN-ים (dmr_csbk.c) — פי-2 מרווח
LSN_FREQ_MIN_VOTES = 3       # מתחת לזה לא מכריזים על מיפוי (תצפית בודדת אינה מפה)
LSN_FREQ_DOMINANCE = 0.75    # שיעור ההצבעות שחייב להיות לתדר המוביל

_lock = threading.Lock()
_intel: dict = {}            # system_id -> profile
_dirty = False
_last_flush = 0.0


def _blank_profile():
    return {"sites": {}, "lsn_directory": {}, "cc": None, "private_calls": [],
            "lsn_freq": {}, "lsn_freq_seen": None}


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
    """פרופיל המערכת, עם השלמת-סכימה קדימה: system_intel.json שנכתב ע"י גרסה
    קודמת (לפני lsn_freq) נטען כמו-שהוא, ולכן כל מפתח חדש חייב להיווצר כאן
    ולא רק ב-_blank_profile — אחרת record_* קורס על KeyError בשדרוג."""
    p = _intel.setdefault(system_id, _blank_profile())
    for key, blank in _blank_profile().items():
        p.setdefault(key, blank)
    return p


def record_site(system_id, site, t=None):
    """site_info: RID-ים "מתגלגלים" בין אתרים לאורך זמן => שביל-תנועה גס
    בלי GPS/LRRP (רוב הרשתות לא משדרות LRRP סטנדרטי ממילא, ר' CLAUDE.md §5)."""
    global _dirty
    if system_id is None or site is None:
        return
    t = t if t is not None else time.time()
    try:
        key = str(int(site))
    except (TypeError, ValueError):
        return          # ⚠ אין int() חשוף בנתיב-הקליטה: הוא הרג את ה-listener (v0.13.0)
    with _lock:
        p = _profile(system_id)
        entry = p["sites"].setdefault(key, {"first_seen": t, "last_seen": t, "count": 0})
        entry["last_seen"] = t
        entry["count"] += 1
        if len(p["sites"]) > SITES_MAX:
            oldest = min(p["sites"], key=lambda k: p["sites"][k]["last_seen"])
            if oldest != key:
                del p["sites"][oldest]
        _dirty = True
    maybe_flush()


def record_lsn_status(system_id, channels, t=None, phys_freq_hz=None):
    """channels: {lsn:int -> 'idle'|'rest'|occupant_id:int} מ-dsd_pty.
    ⚠ 'occupant_id' הוא המספר כפי-שהוא מהשידור -- לא ניתן להבחין כאן אם זה
    TG או RID-יעד של שיחת-יחיד (שני הסוגים נצפו תופסים LSN באותה צורה
    בקליטה אמיתית); לא ממציאים סיווג שלא אומת. שומר רק שינוי-מצב אמיתי
    פר-LSN (occupant השתנה) — לא כותב last_change בכל דגימה חוזרת.

    phys_freq_hz (multi mode בלבד): התדר הפיזי של המפענח שהפיק את השורה. ה-LSN
    שמסומן 'rest' בתוכה הוא, בהכרח, ה-LSN שיושב על התדר הזה — ר' record_rest_channel."""
    global _dirty
    if system_id is None or not channels:
        return
    t = t if t is not None else time.time()
    if phys_freq_hz is not None:
        for lsn, occ in channels.items():
            if occ == "rest":
                record_rest_channel(system_id, lsn, phys_freq_hz, t=t)
    with _lock:
        p = _profile(system_id)
        for lsn, occ in channels.items():
            try:
                key = str(int(lsn))
            except (TypeError, ValueError):
                continue    # ⚠ ר' record_site: קלט פגום מדולג, לא מפיל את ה-thread
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


def record_rest_channel(system_id, rest_lsn, phys_freq_hz, t=None):
    """★ הצבעה אחת ל-"LSN ה-Rest הזה יושב על התדר הפיזי הזה" (ר' הבלוק בראש
    הקובץ). נקרא רק כשיש phys_freq_hz אמיתי — כלומר **רק ב-multi mode**, שבו
    dsd_pty.tag_event מחתים כל אירוע בתדר המפענח שהפיק אותו. בחד-ערוצי
    phys_freq_hz הוא None ⇒ יוצאים מיד ואין שינוי התנהגות (אין ממה להסיק:
    מפענח יחיד שקורא בקרה לא יודע לומר יותר ממה שכתוב בטקסט).

    צבירה ולא קביעה: אנומליה בודדת (הדלפת-אות משכן, פענוח שגוי) לא צריכה
    לקבע מפה — ההכרעה נעשית ב-derive_lsn_map לפי מכסת-הצבעות ורוב."""
    global _dirty
    if system_id is None or rest_lsn is None or phys_freq_hz is None:
        return
    try:
        lsn = int(rest_lsn)
        freq_hz = int(phys_freq_hz)
    except (TypeError, ValueError):
        return
    if lsn < 1 or lsn > LSN_MAX or freq_hz <= 0:
        return
    t = t if t is not None else time.time()
    with _lock:
        p = _profile(system_id)
        votes = p["lsn_freq"].setdefault(str(lsn), {})
        votes[str(freq_hz)] = int(votes.get(str(freq_hz), 0)) + 1
        p["lsn_freq_seen"] = t
        _dirty = True
    maybe_flush()


def _pair_lsn(lsn):
    """ה-LSN השותף — החצי השני של אותו תדר פיזי ב-Cap+ (1↔2, 3↔4, 5↔6 ...)."""
    return lsn + 1 if lsn % 2 else lsn - 1


def lsn_to_physical_channel(lsn):
    """מספר הערוץ הפיזי (1-based) שנושא את ה-LSN הנתון: LSN 1,2→1; 3,4→2 ..."""
    return (int(lsn) + 1) // 2


def derive_lsn_map(lsn_freq, min_votes=LSN_FREQ_MIN_VOTES,
                   dominance=LSN_FREQ_DOMINANCE):
    """★ טהורה: הצבעות → מיפוי LSN↔תדר מוכרע. אין כאן I/O ואין נעילה, כדי
    שההכרעה עצמה תיבדק ב-CI בלי חומרה (כמו discovery.py).

    קלט: {str(lsn): {str(freq_hz): votes}} (הצורה שנשמרת בפרופיל).
    פלט: {lsn:int -> {freq_hz, votes, total, confidence, source, physical_channel,
                      pair_lsn, pair_conflict}}
      source="rest"  — נצפה ישירות כ-Rest על התדר הזה.
      source="pair"  — לא נצפה בעצמו; הוסק מה-LSN השותף (אותו תדר פיזי,
                       עובדת-מבנה של Cap+ — ר' הבלוק בראש הקובץ).
    LSN שלא עבר min_votes/dominance נשמט (אין "מיפוי חלש" — או שיודעים או שלא).
    pair_conflict=True: שני חצאי הזוג הוכרעו לתדרים שונים ⇒ אחת ההנחות שבורה
    (לא Cap+ סטנדרטי / זיהום-קליטה). לא "מתקנים" את זה בשקט — מסמנים."""
    decided = {}
    for lsn_s, votes in (lsn_freq or {}).items():
        try:
            lsn = int(lsn_s)
        except (TypeError, ValueError):
            continue          # מפתחות לא-מספריים (מטא) — מדולגים
        if not isinstance(votes, dict) or not votes:
            continue
        clean = {}
        for freq_s, count in votes.items():
            try:
                clean[int(freq_s)] = int(count)
            except (TypeError, ValueError):
                continue
        total = sum(clean.values())
        if total < min_votes:
            continue
        freq_hz, top = max(clean.items(), key=lambda kv: (kv[1], kv[0]))
        confidence = top / total
        if confidence < dominance:
            continue          # אין רוב ברור ⇒ לא מכריעים
        decided[lsn] = {"freq_hz": freq_hz, "votes": top, "total": total,
                        "confidence": round(confidence, 3), "source": "rest",
                        "physical_channel": lsn_to_physical_channel(lsn),
                        "pair_lsn": _pair_lsn(lsn), "pair_conflict": False}

    # הסקת השותף: LSN שלא נצפה כ-Rest לעולם (למשל תמיד slot-הקול) יורש את
    # התדר מהחצי השני של הזוג. מסומן source="pair" — הסקה, לא תצפית.
    inferred = {}
    for lsn, entry in decided.items():
        pair = entry["pair_lsn"]
        if pair in decided:
            if decided[pair]["freq_hz"] != entry["freq_hz"]:
                entry["pair_conflict"] = True
        elif 1 <= pair <= LSN_MAX and pair not in inferred:
            inferred[pair] = {"freq_hz": entry["freq_hz"], "votes": 0,
                              "total": entry["total"], "confidence": entry["confidence"],
                              "source": "pair",
                              "physical_channel": lsn_to_physical_channel(pair),
                              "pair_lsn": lsn, "pair_conflict": False}
    decided.update(inferred)
    return dict(sorted(decided.items()))


def lsn_map_to_channelmap(lsn_map):
    """★ טהורה: מיפוי LSN מוכרע → channelmap פיזי (הצורה של systems.json:
    [{lcn, freq(MHz)}], ר' app.py:_validate_systems). כאן ה-lcn מפסיק להיות
    אינדקס-שרירותי ונהיה **מספר הערוץ הפיזי האמיתי** שנגזר מה-LSN — וזה בדיוק
    מה ש-app.py:render_channelmap(lsn_pairs=True) יודע להרחיב חזרה לזוגות
    LSN בשביל ‎-C של DSD-FME. זוג עם pair_conflict נשמט (לא מנחשים איזה חצי
    נכון). MHz כאן, לא Hz — כל systems.json ב-MHz (CLAUDE.md §8)."""
    by_channel = {}
    for lsn, entry in (lsn_map or {}).items():
        if entry.get("pair_conflict"):
            continue
        ch = int(entry.get("physical_channel") or lsn_to_physical_channel(lsn))
        freq_hz = int(entry["freq_hz"])
        # אותו ערוץ פיזי משני חצאי-הזוג => אותו תדר; הראשון קובע (זהים ממילא
        # אחרי סינון pair_conflict).
        by_channel.setdefault(ch, freq_hz)
    return [{"lcn": ch, "freq": round(by_channel[ch] / 1e6, 6)}
            for ch in sorted(by_channel)]


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


def lsn_map_for(system_id):
    """המיפוי המוכרע (derive_lsn_map) למערכת נתונה, מהסנפשוט החי."""
    with _lock:
        p = _intel.get(system_id) or {}
        votes = json.loads(json.dumps(p.get("lsn_freq") or {}))
    return derive_lsn_map(votes)


def export_for(system_id):
    """מבט לתצוגה: פרופיל מערכת יחיד, private_calls מקוצר (20 אחרונים,
    החדש קודם). מחזיר פרופיל ריק (לא None) למערכת שעדיין לא נצפתה.
    lsn_map הוא **נגזרת** (derive_lsn_map) ולא state — מחושב בזמן התצוגה כדי
    שסף-ההכרעה יוכל להשתנות בלי מיגרציית-קובץ."""
    with _lock:
        p = _intel.get(system_id)
        snapshot = json.loads(json.dumps(p)) if p is not None else _blank_profile()
    for key, blank in _blank_profile().items():
        snapshot.setdefault(key, blank)
    snapshot["private_calls"] = list(reversed(snapshot["private_calls"][-20:]))
    lsn_map = derive_lsn_map(snapshot.get("lsn_freq") or {})
    snapshot["lsn_map"] = {str(lsn): entry for lsn, entry in lsn_map.items()}
    snapshot["lsn_channelmap"] = lsn_map_to_channelmap(lsn_map)
    return snapshot
