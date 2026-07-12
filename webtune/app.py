#!/usr/bin/env python3
# ============================================================================
#  DMR  -  שרת בקרה (web) לתחנת האזנה ל-DMR עם DSD-FME
# ----------------------------------------------------------------------------
#  ממשק וובי לשליטה מלאה מהטלפון בתחנת פענוח DMR (Motorola Capacity Plus וכו').
#  בכל בחירת מערכת/מצב:
#   1. כותב /etc/dmr/dmr.env (תדר בקרה, color code, נתיב channel-map) + channelmap.csv.
#   2. מפעיל מחדש את שירות dmr-dsdfme (DSD-FME תחת PTY דרך dsd_pty.py).
#   3. dsd_pty מפרסר את פלט DSD-FME ושולח כל אירוע כ-JSON ב-UDP ל-listener כאן;
#      הדף מושך את פיד השיחות מ-/api/dmr, ואת ההקלטות מ-/recordings.
#
#  מיועד לרשת פרטית מהימנה בלבד. רץ כמשתמש לא-root (dmr) עם sudoers ממוקד
#  ל-restart/stop של המצבים בלבד; אימות PIN אופציונלי (DMR_PIN), כבוי כברירת מחדל.
#
#  ⚠ הארכיטקטורה משוכפלת מ-AIR-AM (SDR-אחד-בהחלפה, מתזמר-web, boot-restore,
#    listener→jsonl, scan, roster). ההבדל המהותי: DSD-FME אינו API-first — הפלט
#    שלו טקסטואלי (dsd_pty ממיר ל-JSON), והשליטה בהקשות מקלדת (PTY).
# ============================================================================
import collections
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, abort

import aliases as aliasdb   # ניהול אליאסים (TG/RID) — טעינת CSV + חנות נערכת-מהטלפון
import dsd_export           # ייצוא CSV/JSON (BOM ל-Excel)
import dsd_pty              # build_command וכו', וגם send_gain_nudge (נוד-רווח חי דרך PTY)

# stdout => journald (השירות רץ תחת systemd); journalctl -u dmr-web מציג הכל
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("dmr")

# --- קבועים: נתיבים ומצב --------------------------------------------------
STATE_PATH = Path("/var/lib/dmr/state.json")
DMR_ENV_PATH = Path("/etc/dmr/dmr.env")
CHANNELMAP_PATH = Path("/etc/dmr/channelmap.csv")   # LCN→תדר, נכתב ע"י app.py בכל מעבר
DMR_SERVICE = "dmr-dsdfme"

# --- פיד השיחות: DSD-FME → dsd_pty → UDP JSON → listener כאן ----------------
# dsd_pty.py (ExecStart של dmr-dsdfme.service) מריץ את DSD-FME תחת PTY, מפרסר
# כל שורת אירוע ל-dict, ושולח כ-JSON ב-UDP לכאן — בדיוק כמו "acarsdec -j" ב-AIR-AM.
DMR_UDP_HOST = "127.0.0.1"
DMR_UDP_PORT = 5555                   # חייב להתאים ל-DMR_UDP ב-dmr.env / dsd_pty
DMR_BUF_MAX = 800                     # שיחות אחרונות בזיכרון (נטענות בעלייה, היום בלבד)
DMR_LOG_PATH = Path("/var/lib/dmr/dmr.jsonl")
DMR_LOG_KEEP = 8000                   # retention בדיסק (זנב נשמר; ייצוא לניתוח)

# מיפוי אלגוריתם הצפנה (ALG id → שם קריא). DSD-FME מדפיס hex; אנו ממפים בלבד,
# לעולם *לא* מפענחים בלי מפתח. ערכים נפוצים (RadioReference / קהילת DSD-FME):
DMR_ALG_NAMES = {
    0x00: "Clear", 0x01: "RC4/BP", 0x21: "RC4/BP", 0x02: "DES-OFB", 0x22: "DES",
    0x04: "AES-128", 0x24: "AES-128", 0x05: "AES-256", 0x25: "AES-256",
    0x06: "AES-256", 0x26: "AES-256",
}
# סוגי שיחה מנורמלים (call_type). ערך = (תווית עברית, קבוצת-צבע ל-UI/ייצוא):
#   group(כחול) · private(סגול) · data(אפור) · control(אפור) · reg(ירוק)
DMR_CALL_TYPES = {
    "group": ("שיחת קבוצה", "group"),
    "private": ("שיחה פרטית", "private"),
    "data": ("נתונים", "data"),
    "sms": ("הודעת טקסט (SMS)", "data"),
    "lrrp": ("מיקום (LRRP/GPS)", "position"),
    "control": ("ערוץ בקרה (CSBK)", "control"),
    "reg": ("רישום/שיוך (registration)", "reg"),
}

# --- הקלטות: DSD-FME כותב per-call WAV לתיקייה; watcher מקשר לפיד --------------
REC_DIR = Path("/var/lib/dmr/recordings")
REC_MAX_FILES = 400
REC_MAX_BYTES = 400 * 1024 * 1024
ACTIVITY_PATH = Path("/var/lib/dmr/activity.jsonl")
ACTIVITY_KEEP = 800
ACTIVITY_RETURN = 60
WATCH_INTERVAL = 10.0

# תמלול (אופציונלי, כבוי כברירת מחדל): whisper.cpp מקומי על ה-WAV. פעיל רק אם
# DMR_TRANSCRIBE=1 וגם הבינארי+המודל קיימים (install.sh בונה רק עם INSTALL_DMR_WHISPER=1).
# הערה: תועלת נמוכה מ-ATC (DMR קצר/רב-לשוני/לעיתים מוצפן) — נשאר opt-in.
TRANSCRIBE = os.environ.get("DMR_TRANSCRIBE", "").strip().lower() in ("1", "true", "yes", "on")
WHISPER_BIN = os.environ.get("DMR_WHISPER_BIN", "/usr/local/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("DMR_WHISPER_MODEL", "/opt/dmr/models/ggml-base.bin")
WHISPER_LANG = os.environ.get("DMR_WHISPER_LANG", "auto")
TRANSCRIBE_TIMEOUT = 120.0

APP_DIR = Path(__file__).resolve().parent
_FREQ_RE = re.compile(r"^\d{2,4}\.\d{1,6}$")   # ולידציית תדר MHz (400.xxx / 136.xxx וכו')


def _read_version():
    for p in (APP_DIR / "VERSION", APP_DIR.parent / "VERSION"):
        try:
            return p.read_text().strip()
        except OSError:
            continue
    return "dev"


VERSION = _read_version()
app = Flask(__name__, static_folder=str(APP_DIR / "static"))

# מעבר-מצב אחד בכל רגע: שני POST מקבילים => שני restart שלובים. serialized תחת נעילה.
TUNE_LOCK = threading.Lock()

# הרצה כמשתמש לא-root (חיזוק אבטחה): restart/stop עוברים דרך sudoers ממוקד.
SUDO = [] if os.geteuid() == 0 else ["sudo", "-n"]

# אימות אופציונלי: פעיל אך ורק אם DMR_PIN הוגדר ב-environment של השירות.
DMR_PIN = os.environ.get("DMR_PIN", "").strip()


@app.before_request
def _guard():
    """הגנות קלות על בקשות משנות-מצב (POST/PUT/DELETE):
      1. CSRF / DNS-rebinding: אם נשלח Origin/Referer הוא חייב להתאים ל-Host.
      2. אימות אופציונלי: אם DMR_PIN הוגדר, נדרש header X-DMR-PIN תואם.
    בקשות GET (פיד/health/מדדים) לא מושפעות."""
    if request.method not in ("POST", "PUT", "DELETE"):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify(ok=False, error="מקור הבקשה לא תואם (Origin)"), 403
    if DMR_PIN and request.headers.get("X-DMR-PIN", "") != DMR_PIN:
        return jsonify(ok=False, error="נדרש PIN", auth=True), 401
    return None


# --- מערכות DMR (systems): פריסטים של רשתות טראנקינג -----------------------
# מערכת = {id, name, control (MHz), color_code, channelmap: [{lcn, freq}]}.
# זו הזריעה הראשונית; מרגע עריכה בממשק האמת היא /var/lib/dmr/systems.json.
# ⚠ החלף בערכי הרשת שלך (control freq + color code + מפת LCN→תדר).
DEFAULT_SYSTEMS = [
    {"id": "capplus1", "name": "Cap+ לדוגמה", "control": 461.0375, "color_code": 1,
     "channelmap": [
         {"lcn": 1, "freq": 461.0375},
         {"lcn": 2, "freq": 461.0625},
         {"lcn": 3, "freq": 461.0875},
         {"lcn": 4, "freq": 461.1125},
     ]},
]
SYSTEMS_PATH = Path("/var/lib/dmr/systems.json")
SYSTEMS_MAX = 30
CHANNELMAP_MAX = 64
COLOR_CODE_MIN, COLOR_CODE_MAX = 0, 15


def _validate_systems(lst):
    """(ok, cleaned) - מנרמל ומאמת רשימת מערכות DMR מהלקוח/מהדיסק."""
    if not isinstance(lst, list) or len(lst) > SYSTEMS_MAX:
        return False, None
    out = []
    seen_ids = set()
    for s in lst:
        if not isinstance(s, dict):
            return False, None
        sid = str(s.get("id", "")).strip()
        name = str(s.get("name", "")).strip()
        if not re.match(r"^[A-Za-z0-9_\-]{1,32}$", sid) or sid in seen_ids:
            return False, None
        if not name or len(name) > 48:
            return False, None
        seen_ids.add(sid)
        try:
            control = round(float(s.get("control")), 6)
        except (TypeError, ValueError):
            return False, None
        if not (24.0 <= control <= 1300.0):   # תחום SDRplay/RSP1B הרחב (VHF/UHF)
            return False, None
        try:
            cc = int(s.get("color_code", 1))
        except (TypeError, ValueError):
            return False, None
        if not (COLOR_CODE_MIN <= cc <= COLOR_CODE_MAX):
            return False, None
        raw_map = s.get("channelmap") or []
        if not isinstance(raw_map, list) or len(raw_map) > CHANNELMAP_MAX:
            return False, None
        cmap = []
        for ch in raw_map:
            if not isinstance(ch, dict):
                return False, None
            try:
                lcn = int(ch.get("lcn"))
                freq = round(float(ch.get("freq")), 6)
            except (TypeError, ValueError):
                return False, None
            if not (1 <= lcn <= 4096) or not (24.0 <= freq <= 1300.0):
                return False, None
            cmap.append({"lcn": lcn, "freq": freq})
        out.append({"id": sid, "name": name, "control": control,
                    "color_code": cc, "channelmap": cmap})
    return True, out


def load_systems():
    try:
        ok, cleaned = _validate_systems(json.loads(SYSTEMS_PATH.read_text()))
        if ok:
            return cleaned
    except Exception:
        pass
    return [json.loads(json.dumps(s)) for s in DEFAULT_SYSTEMS]


def _find_system(systems, sid):
    """מחזיר את המערכת עם ה-id הנתון, או None."""
    for s in systems:
        if s["id"] == sid:
            return s
    return None


DEFAULT_STATE = {
    # "dmr" (DSD-FME פעיל) | "off" (standby — הצרכן עצור, ה-SDR פנוי) | "scan" (סבב).
    # ברירת המחדל ניטרלית (off): התקנה טרייה נוחתת במסך הבית, המצב שורד reboot.
    "app_mode": "off",
    "system": None,          # id המערכת הפעילה (או None => הראשונה ב-load_systems)
    "gain_nudge": 0,         # מונה יחסי best-effort (g/G) — מתאפס בכל כניסה למצב DMR
}


def _atomic_write(path, text):
    """כתיבה אטומית (tmp + rename): dmr-dsdfme יכול לעלות בכל רגע (Restart=always /
    udev) ואסור שיקרא env/channelmap חצי-כתוב. tmp ייחודי לפר-thread => שתי בקשות
    מקבילות לא דורסות זו את קובץ ה-tmp של זו; ה-rename האחרון מנצח (last-write-wins)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_state():
    try:
        st = json.loads(STATE_PATH.read_text())
        return {**DEFAULT_STATE, **st}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(st):
    _atomic_write(STATE_PATH, json.dumps(st))


# --- זיהוי SDR + systemctl (מוקפים בבדיקות) --------------------------------
def _sdr_present():
    """בדיקת USB מהירה (vendor 1df7 = SDRplay) בלי לפתוח את המכשיר."""
    try:
        return subprocess.run(["lsusb", "-d", "1df7:"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return True   # אין lsusb / ספק => מניחים שמחובר (עדיף רולבק מיותר מאף-פעם)


def _journal_tail(service=DMR_SERVICE, lines=8):
    return subprocess.run(["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                          capture_output=True, text=True).stdout


def _is_active(service):
    """is-active הוא קריאת-קריאה => לא דורש sudo (עובד לכל משתמש)."""
    try:
        r = subprocess.run(["systemctl", "is-active", service],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _sysctl(action, service, timeout=45):
    """systemctl פעולה משנת-מצב => דרך SUDO (sudoers ממוקד מתיר בדיוק
    restart/stop של dmr-dsdfme ל-dmr)."""
    return subprocess.run([*SUDO, "systemctl", action, service],
                          capture_output=True, text=True, timeout=timeout)


# --- כתיבת קונפיג DSD-FME (env + channelmap) --------------------------------
def _sanitize_freq(val, default=None):
    """מנרמל תדר יחיד (MHz) למחרוזת נקייה (רק ספרות ונקודה), או default אם לא תקין."""
    s = str(val).strip()
    return s if _FREQ_RE.match(s) else default


def render_channelmap(channelmap):
    """בונה את תוכן channelmap.csv (LCN,FREQ_HZ) ל-DSD-FME (‎-C). התדרים ב-Hz.
    פורמט DSD-FME: כל שורה 'lcn,freq_hz'. פונקציה טהורה => נבדקת בלי חומרה."""
    lines = []
    for ch in channelmap or []:
        try:
            lcn = int(ch["lcn"])
            hz = int(round(float(ch["freq"]) * 1e6))
        except (KeyError, TypeError, ValueError):
            continue
        lines.append(f"{lcn},{hz}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_channelmap(channelmap):
    _atomic_write(CHANNELMAP_PATH, render_channelmap(channelmap))


def render_dmr_env(system):
    """בונה את תוכן dmr.env (EnvironmentFile של systemd, KEY=VALUE מנותח בבטחה).
    dsd_pty קורא את המשתנים ובונה מהם את שורת הפקודה של DSD-FME.
    ⚠ DSD_CONTROL_FREQ ב-Hz (DSD-FME/rigctl); ה-state/UI עובדים ב-MHz — ההמרה כאן."""
    control_hz = int(round(float(system["control"]) * 1e6))
    cc = int(system.get("color_code", 1))
    lines = [
        '# נכתב אוטומטית ע"י DMR web (מעבר למצב DMR). שינויים ידניים נדרסים.',
        f"# מערכת: {system.get('name', system.get('id', ''))}",
        f"DSD_CONTROL_FREQ={control_hz}",   # Hz — ערוץ הבקרה (CC) של Cap+
        f"DSD_COLOR_CODE={cc}",
        f"DSD_CHANNELMAP={CHANNELMAP_PATH}",
        f"DSD_UDP={DMR_UDP_HOST}:{DMR_UDP_PORT}",   # יעד פיד ה-JSON (dsd_pty → app.py)
        f"DSD_WAV_DIR={REC_DIR}",                    # per-call WAV לתיקיית ההקלטות
        "DSD_TRUNK=1",                               # מעקב טראנקינג (Cap+)
        "",
    ]
    return "\n".join(lines)


def write_dmr_env(system):
    _atomic_write(DMR_ENV_PATH, render_dmr_env(system))


# --- מצב DMR: כניסה + standby ----------------------------------------------
def _enter_dmr(system):
    """כותב env + channelmap ומריץ את dmr-dsdfme (DSD-FME תחת PTY). מחזיר
    (error, detail). מבנה זהה ל-_enter_acars ב-AIR-AM: write-env → restart → poll
    לקריסה מאוחרת (השירות יכול לעלות ואז לקרוס על תדר/מפה רעים ~2ש' אחר-כך)."""
    write_channelmap(system.get("channelmap"))
    write_dmr_env(system)
    try:
        r = _sysctl("restart", DMR_SERVICE, timeout=45)
    except subprocess.TimeoutExpired:
        return "הפעלת DMR נתקעה — בדוק שה-SDR מחובר", None
    if r.returncode != 0:
        return (r.stderr or "dsd-fme failed").strip(), _journal_tail(DMR_SERVICE)
    for _ in range(7):
        time.sleep(0.5)
        if not _is_active(DMR_SERVICE):
            return "DSD-FME נכשל לעלות — בדוק journalctl -u dmr-dsdfme", _journal_tail(DMR_SERVICE)
    # restart אמיתי של DSD-FME => מרווח ברירת-המחדל שלו מחדש; מונה נוד-הרווח
    # היחסי שלנו (g/G, ר' _dmr_gain_nudge) לא רלוונטי יותר, בכל נקודת כניסה
    # (UI/scan/boot-restore) — לא רק דרך api_mode.
    try:
        save_state({**load_state(), "gain_nudge": 0})
    except Exception:
        pass
    return None, None


def _enter_standby():
    """מצב כיבוי (standby): עוצר את dmr-dsdfme => משחרר את ה-RSP1B ליישום SDR אחר,
    בעוד dmr-web/הדף נשארים פעילים. sdrplay.service נשאר חי בכוונה (ה-API daemon
    מאפשר לאפליקציה אחרת להתחבר מיד; ה-sudoers ממילא אינו מתיר לעצור אותו).
    מחזיר (error, detail). serialized תחת TUNE_LOCK ע"י הקורא."""
    try:
        _sysctl("stop", DMR_SERVICE, timeout=30)
    except Exception:
        pass
    for _ in range(7):
        time.sleep(0.3)
        if not _is_active(DMR_SERVICE):
            return None, None
    return "כיבוי המקלט נכשל — השירות עדיין פעיל", _journal_tail(DMR_SERVICE)


def _fail_to_off(st, err, detail, log_prefix):
    """כישלון כניסה למצב => נפילה ל-off (standby). עוצר את הצרכן (best-effort),
    שומר state עם off + prev_mode, ומחזיר (payload, 500) בחוזה שה-UI מכיר."""
    log.warning("%s failed: %s — falling to standby", log_prefix, err)
    try:
        _enter_standby()
    except Exception:
        pass
    new_state = {**st, "app_mode": "off", "prev_mode": st.get("app_mode", "off")}
    save_state(new_state)
    return {"ok": False, "error": err, "detail": detail,
            "app_mode": "off", "state": new_state}, 500


MODE_SERVICE = {"dmr": DMR_SERVICE}


def _live_mode():
    """המצב שרץ בפועל (לפי השירות), או None כשהצרכן לא פעיל."""
    return "dmr" if _is_active(DMR_SERVICE) else None


# --- פיד השיחות: נרמול, התמדה, listener ------------------------------------
_dmr_lock = threading.Lock()
_dmr_msgs = collections.deque(maxlen=DMR_BUF_MAX)
_dmr_seq = 0                    # מזהה רץ גלובלי (cursor ל-UI)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(str(v).strip(), 0)   # תומך גם ב-"0x21"
        except (TypeError, ValueError):
            return None


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_CARD_EVENT_TYPES = frozenset({"voice_call", "data_header", "lrrp_position", "lrrp_request"})


def _channelmap_freq(lcn):
    """תדר (MHz) לפי LCN/Rest-LSN, מתוך ה-channelmap של המערכת הפעילה כרגע
    (state.system). best-effort — None אם אין LCN/מערכת/ערוץ תואם; DSD-FME
    (בקליטה אמיתית שנבדקה) לא מדפיס תדר בטקסט כלל, אז זה המקור האמין היחיד."""
    if lcn is None:
        return None
    try:
        system = _find_system(load_systems(), load_state().get("system"))
        if not system:
            return None
        for ch in system.get("channelmap") or []:
            if int(ch.get("lcn", -1)) == int(lcn):
                return float(ch["freq"])
    except Exception:
        return None
    return None


def _normalize_dsd(m):
    """★ הלב: ממיר אירוע DSD-FME מוקלד (dict מ-dsd_pty.parse_dsd_line, שדה
    'type') לכרטיס שיחה אחיד. **quality/encryption לא הופכים לכרטיס** —
    quality מוזן ל-_rf_quality_tick (מד תדירות-שגיאות), encryption מתואם
    ל-slot ע"י ה-listener (_dmr_correlate_encryption). מחזיר None לכל type
    אחר (כולל housekeeping — אך dsd_pty כבר לא שולח אותם כלל).
    לעולם *לא* ממציא מדד: ber/level נשארים None כי DSD-FME לא מדפיס אותם
    בקליטה אמיתית שנבדקה (אין "לרמות" למספר — ר' CLAUDE.md §8)."""
    if not isinstance(m, dict) or m.get("type") not in _CARD_EVENT_TYPES:
        return None
    typ = m["type"]
    t = _float_or_none(m.get("t")) or time.time()

    slot = _int_or_none(m.get("slot"))
    tg = _int_or_none(m.get("tg"))
    src = _int_or_none(m.get("src"))
    tgt = _int_or_none(m.get("tgt"))
    lcn = _int_or_none(m.get("lcn"))
    ct = str(m.get("call_type") or "data").strip().lower()
    if ct not in DMR_CALL_TYPES:
        ct = "data"
    category, group = DMR_CALL_TYPES[ct]

    lat = _float_or_none(m.get("lat"))
    lon = _float_or_none(m.get("lon"))
    if lat is not None and lon is not None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            lat = lon = None
        else:
            group = "position"

    card = {
        "t": round(t, 3), "proto": "DMR",
        "freq": _channelmap_freq(lcn), "slot": slot, "cc": None, "lcn": lcn,
        "tg": tg, "tg_alias": aliasdb.tg_name(tg),
        "src": src, "src_alias": aliasdb.rid_name(src),
        "tgt": tgt, "tgt_alias": aliasdb.rid_name(tgt),
        "call_type": ct, "category": category, "group": group,
        "encrypted": False, "enc": None,   # מתואם בהמשך ע"י ה-listener אם רלוונטי
        "ber": None, "level": None,        # DSD-FME לא מדפיס — אף פעם לא ממציאים
        "dur": None, "event": typ,
        "lat": round(lat, 5) if lat is not None else None,
        "lon": round(lon, 5) if lon is not None else None,
        "text": None, "wav": None,
        "delivery": m.get("delivery"),   # אופציונלי (data_header בלבד)
    }
    return card


def _append_jsonl_log(path, rec):
    """מוסיף רשומה לקובץ JSONL (append; thread ה-listener הוא הכותב היחיד).
    נכשל בשקט (דיסק מלא וכו') => הפיד החי ממשיך לפעול."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("jsonl log append (%s)", path)


def _trim_jsonl_log(path, keep):
    """קיצוץ ל-keep שורות (rewrite אטומי). נקרא מדי פעם מ-thread ה-listener."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > keep:
        _atomic_write(path, "\n".join(lines[-keep:]) + "\n")


def _append_dmr_log(rec):
    _append_jsonl_log(DMR_LOG_PATH, rec)


def _trim_dmr_log():
    _trim_jsonl_log(DMR_LOG_PATH, DMR_LOG_KEEP)


# --- איכות RF: תדירות שגיאות (לא dBFS/SNR) + נוד-רווח חי ---------------------
# DSD-FME (בקליטה אמיתית שנבדקה מול רשת Cap+/SLCO) לא נותן שום SNR/RSSI/dBFS
# רציף — רק אירועי CRC/FEC בודדים (quality) ו"Protected LC" (encryption).
# **לעולם לא ממציאים יחס/dB** — רק סופרים תדירות שגיאות אמיתית בחלון נגלל.
# מד dBFS עצמאי מה-SDR עצמו נדחה במכוון (דורש פטצ' rsp_tcp; ר' CLAUDE.md §8).
RF_WINDOW_SEC = 60.0
_rf_lock = threading.Lock()
_rf_ticks = collections.deque()   # (t, error_type) — נגזם לחלון RF_WINDOW_SEC


def _rf_quality_tick(error_type):
    now = time.time()
    with _rf_lock:
        _rf_ticks.append((now, error_type))
        cutoff = now - RF_WINDOW_SEC
        while _rf_ticks and _rf_ticks[0][0] < cutoff:
            _rf_ticks.popleft()


def _rf_quality_snapshot():
    """תדירות שגיאות CRC/FEC אמיתית ב-RF_WINDOW_SEC האחרונות. פונקציה טהורה
    (קוראת מהחלון הנגלל בלבד) => נבדקת בלי חומרה."""
    now = time.time()
    with _rf_lock:
        cutoff = now - RF_WINDOW_SEC
        while _rf_ticks and _rf_ticks[0][0] < cutoff:
            _rf_ticks.popleft()
        ticks = list(_rf_ticks)
    by_type = collections.Counter(t for _, t in ticks)
    total = len(ticks)
    return {"window_sec": RF_WINDOW_SEC, "total_errors": total,
            "errors_per_min": round(total * 60.0 / RF_WINDOW_SEC, 1),
            "by_type": [{"error_type": k, "count": v} for k, v in by_type.most_common()]}


# נוד-רווח חי: g/G דרך dsd_pty.send_gain_nudge (הקשה ל-DSD-FME, בלי לעצור אותו).
# יחסי בלבד — אין readback מ-DSD-FME, אז אין מספר dB אמיתי לעקוב אחריו; מונה
# best-effort ב-state, מתאפס בכל כניסה חדשה למצב DMR (ההנחה: DSD-FME מתחיל
# מרווח ברירת-מחדל משלו בכל restart).
GAIN_NUDGE_MIN, GAIN_NUDGE_MAX = -30, 30


def _dmr_gain_nudge(direction):
    """שולח הקשת נוד-רווח בודדת ומעדכן מונה יחסי. מחזיר (ok, gain_nudge_value)."""
    ok = dsd_pty.send_gain_nudge(direction)
    st = load_state()
    cur = int(st.get("gain_nudge", 0))
    if ok:
        cur = max(GAIN_NUDGE_MIN, min(GAIN_NUDGE_MAX, cur + (1 if direction == "up" else -1)))
        save_state({**st, "gain_nudge": cur})
    return ok, cur


def _today_start():
    """epoch של חצות מקומי (שעון ה-Pi) — רצפת-זמן ל"היום בלבד" בפיד החי."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _day_bounds(date_str):
    """גבולות היום המקומי [start, end) עבור 'YYYY-MM-DD' (לארכיון החיפוש), או None.
    ⚠ end מחושב עם mktime על tm_mday+1 (לא +86400) => עמיד לשעון קיץ/חורף."""
    try:
        lt = time.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return start, end


def _read_dmr_log():
    """כל השיחות מהדיסק, ממוינות לפי זמן (t עולה). סובל שורות פגומות."""
    try:
        lines = DMR_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    out.sort(key=lambda r: r.get("t") or 0)
    return out


def _load_dmr_history():
    """טוען את זנב dmr.jsonl ל-ring buffer בעלייה (היום בלבד). נקרא *לפני*
    הפעלת thread ה-listener (אין מרוץ)."""
    global _dmr_seq
    try:
        lines = DMR_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    recs = []
    for ln in lines[-DMR_BUF_MAX:]:
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue
    floor = _today_start()
    recs = [r for r in recs if (r.get("t") or 0) >= floor]
    recs.sort(key=lambda r: r.get("t") or 0)
    with _dmr_lock:
        for r in recs:
            _dmr_seq += 1
            r["id"] = _dmr_seq
            _dmr_msgs.append(r)
    if recs:
        log.info("DMR: נטענו %d שיחות מההיסטוריה", len(recs))


def _dmr_listener():
    """thread רקע: מאזין ל-UDP מ-dsd_pty (DSD-FME), שומר ל-dmr.jsonl ומכניס
    ל-ring buffer. רץ תמיד (גם ב-standby) — פשוט לא מגיעות דאטהגרמות כש-DSD-FME כבוי.
    dedup: אירועי המשך של אותה שיחה (voice frames) מתאחדים לכרטיס אחד (8ש').
    quality/encryption *לא* הופכים לכרטיס: quality מוזן ל-_rf_quality_tick
    (מד תדירות-שגיאות), encryption מתואם לשיחה הפתוחה באותו slot (_slot_open_call,
    חלון 15ש' — best-effort; אם אין שיחה פתוחה מתאימה, מדולג בשקט)."""
    global _dmr_seq
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((DMR_UDP_HOST, DMR_UDP_PORT))
    except OSError:
        log.warning("DMR listener: port %d busy - /api/dmr יחזיר ריק", DMR_UDP_PORT)
        return
    seen = 0
    _dedup: dict = {}           # (tg, src, slot, call_type) → (timestamp, rec) — איחוד המשך-שיחה
    _slot_open_call: dict = {}  # slot → (timestamp, rec) — לקורלציית encryption
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except OSError:
            continue
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(msg, dict):
            continue

        mtype = msg.get("type")
        if mtype == "quality":
            _rf_quality_tick(msg.get("error_type") or "UNKNOWN")
            continue
        if mtype == "encryption":
            entry = _slot_open_call.get(msg.get("slot"))
            ts = msg.get("t") or time.time()
            if entry is not None and ts - entry[0] < 15:
                with _dmr_lock:   # open_rec חי גם ב-_dmr_msgs => מוטציה תחת הנעילה
                    entry[1]["encrypted"] = True
                    entry[1]["enc"] = entry[1].get("enc") or {"alg": None, "alg_name": "מוצפן", "key_id": None}
            continue

        try:
            rec = _normalize_dsd(msg)
        except Exception:
            log.exception("DMR: נרמול נכשל על דאטהגרם — מדולג")
            continue
        if mtype == "voice_call" and msg.get("crc_err"):
            _rf_quality_tick("VOICE_CRC")   # פריים קול שנכשל => גם מד ה-RF, גם (אם יש) הכרטיס
        if rec is None:
            continue

        # dedup: אותה שיחה (tg+src+slot) בתוך 8ש' => עדכון הכרטיס הקיים (משך/wav),
        # לא כרטיס חדש. שיחות voice ב-DMR משדרות מסגרות רבות לאורך השיחה.
        key = (rec.get("tg"), rec.get("src"), rec.get("slot"), rec.get("call_type"))
        ts = rec.get("t") or time.time()
        is_voice = rec.get("call_type") in ("group", "private") and (rec.get("tg") or rec.get("src"))
        if is_voice:
            prev_ts, prev_rec = _dedup.get(key, (0, None))
            if prev_rec is not None and ts - prev_ts < 8:
                with _dmr_lock:   # prev_rec חי גם ב-_dmr_msgs => מוטציה תחת הנעילה
                    prev_rec["dur"] = round(ts - prev_rec.get("_start", prev_ts), 1)
                    if rec.get("wav"):
                        prev_rec["wav"] = rec["wav"]
                    prev_rec["frames"] = prev_rec.get("frames", 1) + 1
                _dedup[key] = (ts, prev_rec)
                if rec.get("slot") is not None:
                    _slot_open_call[rec["slot"]] = (ts, prev_rec)
                continue
            rec["_start"] = ts
            _dedup[key] = (ts, rec)
            if len(_dedup) > 500:
                cutoff = ts - 30
                for k in [k for k, (t0, _) in _dedup.items() if t0 < cutoff]:
                    del _dedup[k]

        _append_dmr_log(rec)
        with _dmr_lock:
            _dmr_seq += 1
            rec["id"] = _dmr_seq
            _dmr_msgs.append(rec)
        if is_voice and rec.get("slot") is not None:
            _slot_open_call[rec["slot"]] = (ts, rec)
            if len(_slot_open_call) > 8:   # רק 2 slots אפשריים בפועל — הגנה בכל זאת
                cutoff = ts - 15
                for k in [k for k, (t0, _) in _slot_open_call.items() if t0 < cutoff]:
                    del _slot_open_call[k]
        seen += 1
        if seen % 200 == 0:
            _trim_dmr_log()


# --- מצב סריקה/סבב: מחזור אוטומטי בין מערכות DMR ----------------------------
# "רגל" (leg) = {"system": <system id>, "dwell_sec": int, "active_from"?, "active_to"?}.
# thread נפרד מסתובב בין הרגלים; נועל TUNE_LOCK רק בזמן מעבר. כשל ברגל => דילוג;
# כשל של *כל* הרגלים ברצף => off (אין fallback). מחזור בין מערכות טראנקינג שונות.
SCAN_DWELL_MIN, SCAN_DWELL_MAX = 15, 3600
SCAN_LEGS_MAX = 8
SCAN_WINDOW_RECHECK_SEC = 30
_HHMM_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def _leg_active_now(leg):
    """האם הרגל בחלון השעות שלה כרגע (שעון מקומי). בלי active_from/active_to =>
    תמיד פעילה. תומך בחלון שחוצה חצות. from==to => 24 שעות (תמיד פעילה)."""
    frm, to = leg.get("active_from"), leg.get("active_to")
    if not frm or not to:
        return True
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    fh, fm = (int(x) for x in frm.split(":"))
    th, tm = (int(x) for x in to.split(":"))
    f, t = fh * 60 + fm, th * 60 + tm
    if f == t:
        return True
    return (f <= cur < t) if f <= t else (cur >= f or cur < t)


_scan_lock = threading.Lock()
_scan_thread = None
_scan_thread_stop = None
_scan_status = {"idx": -1, "leg": None, "next_switch_at": None, "plan": []}


def _validate_scan_plan(raw):
    """מוודא לוח סריקה: 1..SCAN_LEGS_MAX רגלים, כל רגל system (id קיים) +
    dwell_sec בטווח + (אופציונלי) חלון שעות "HH:MM"-"HH:MM" (שניהם ביחד).
    מחזיר לוח מנורמל או None."""
    if not isinstance(raw, list) or not (1 <= len(raw) <= SCAN_LEGS_MAX):
        return None
    systems = load_systems()
    plan = []
    for leg in raw:
        if not isinstance(leg, dict):
            return None
        sid = str(leg.get("system", "")).strip()
        if not _find_system(systems, sid):
            return None
        try:
            dwell = int(leg.get("dwell_sec"))
        except (TypeError, ValueError):
            return None
        if not (SCAN_DWELL_MIN <= dwell <= SCAN_DWELL_MAX):
            return None
        clean = {"system": sid, "dwell_sec": dwell}
        frm, to = leg.get("active_from"), leg.get("active_to")
        if frm or to:
            if not (isinstance(frm, str) and isinstance(to, str)
                    and _HHMM_RE.match(frm) and _HHMM_RE.match(to)):
                return None
            clean["active_from"], clean["active_to"] = frm, to
        plan.append(clean)
    return plan


def _scan_enter_leg(leg):
    """נכנס לרגל בודדת (מערכת). *לא* נועל TUNE_LOCK — הקורא אחראי. מחזיר (error, detail)."""
    system = _find_system(load_systems(), leg["system"])
    if system is None:
        return "מערכת לא נמצאה: " + str(leg.get("system")), None
    return _enter_dmr(system)


def _scan_stop_thread():
    """עוצר את thread הסריקה הפעיל (אם יש) ומחכה שיסיים. אין-אופ אם לא רץ סבב."""
    global _scan_thread, _scan_thread_stop
    with _scan_lock:
        thread, stop_evt = _scan_thread, _scan_thread_stop
        _scan_thread = _scan_thread_stop = None
        if stop_evt:
            stop_evt.set()
    if thread and thread.is_alive():
        thread.join(timeout=15)
    if thread:
        with _scan_lock:
            _scan_status.update(idx=-1, leg=None, next_switch_at=None)


def _scan_loop(stop_evt, plan, start_idx, first_dwell, consumer_active=False):
    """thread: ממתין first_dwell על הרגל שכבר הוכנסה, ואז מסתובב בין שאר הרגלים.
    stop_evt ייחודי-לקריאה-הזו. רגל מחוץ לחלון מדולגת (לא כשל); סבב שלם בלי אף
    רגל בחלון => מכבים את הצרכן וממתינים SCAN_WINDOW_RECHECK_SEC. רגל זהה לרגל
    שכבר רצה (אותה מערכת) => רק מרעננים טיימר (בלי restart מיותר). כשל של *כל*
    הרגלים ברצף => off."""
    idx = start_idx
    remaining = first_dwell
    consecutive_fail = 0
    consecutive_skip = 0
    last_system = plan[(start_idx - 1) % len(plan)]["system"] if consumer_active else None
    while not stop_evt.is_set():
        while remaining > 0 and not stop_evt.is_set():
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step
        if stop_evt.is_set():
            break
        leg = plan[idx % len(plan)]
        if not _leg_active_now(leg):
            consecutive_skip += 1
            idx += 1
            with _scan_lock:
                _scan_status.update(idx=-1, leg=None, next_switch_at=None)
            if consecutive_skip >= len(plan):
                if consumer_active:
                    log.info("scan: אף רגל לא בחלון השעות — מכבה את הצרכן הפעיל")
                    _enter_standby()
                    consumer_active = False
                    last_system = None
                remaining = SCAN_WINDOW_RECHECK_SEC
                consecutive_skip = 0
            else:
                remaining = 0
            continue
        consecutive_skip = 0
        if last_system is not None and last_system == leg["system"]:
            with _scan_lock:
                _scan_status.update(idx=idx % len(plan), leg=leg,
                                    next_switch_at=time.time() + leg["dwell_sec"])
            remaining = leg["dwell_sec"]
            idx += 1
            continue
        if not TUNE_LOCK.acquire(timeout=5):
            remaining = 1
            continue
        try:
            err, detail = _scan_enter_leg(leg)
        finally:
            TUNE_LOCK.release()
        if err:
            log.warning("scan: leg %d (%s) failed: %s", idx % len(plan), leg["system"], err)
            consecutive_fail += 1
            if consecutive_fail >= len(plan):
                log.warning("scan: כל הרגלים נכשלו בסבב — נופל ל-off")
                _enter_standby()
                if stop_evt.is_set():
                    return
                cur = load_state()
                save_state({**cur, "app_mode": "off", "prev_mode": "scan"})
                with _scan_lock:
                    _scan_status.update(idx=-1, leg=None, next_switch_at=None)
                return
            idx += 1
            remaining = 1
            continue
        consecutive_fail = 0
        consumer_active = True
        last_system = leg["system"]
        if stop_evt.is_set():
            return
        with _scan_lock:
            _scan_status.update(idx=idx % len(plan), leg=leg,
                                next_switch_at=time.time() + leg["dwell_sec"])
        remaining = leg["dwell_sec"]
        idx += 1


def _scan_activate(plan):
    """מפעיל סבב סריקה: נכנס לרגל הראשונה שבחלון שלה כרגע (הקורא מחזיק TUNE_LOCK).
    אם אף רגל לא בחלון — לא כשל: ה-SDR נשאר כבוי ומתחיל thread שממתין לחלון הבא.
    מחזיר (error, detail) — error רק על כשל אמיתי בכניסה לרגל."""
    global _scan_thread, _scan_thread_stop
    active_idx = next((i for i, leg in enumerate(plan) if _leg_active_now(leg)), None)
    if active_idx is None:
        stop_evt = threading.Event()
        thread = threading.Thread(target=_scan_loop, args=(stop_evt, plan, 0, 0, False), daemon=True)
        with _scan_lock:
            _scan_status.update(idx=-1, leg=None, next_switch_at=None, plan=plan)
            _scan_thread, _scan_thread_stop = thread, stop_evt
        thread.start()
        return None, None
    err, detail = _scan_enter_leg(plan[active_idx])
    if err:
        return err, detail
    stop_evt = threading.Event()
    thread = threading.Thread(target=_scan_loop,
                              args=(stop_evt, plan, active_idx + 1, plan[active_idx]["dwell_sec"], True),
                              daemon=True)
    with _scan_lock:
        _scan_status.update(idx=active_idx, leg=plan[active_idx],
                            next_switch_at=time.time() + plan[active_idx]["dwell_sec"], plan=plan)
        _scan_thread, _scan_thread_stop = thread, stop_evt
    thread.start()
    return None, None


# --- רוסטר רדיו-IDs / talkgroups מאוחד --------------------------------------
ROSTER_MAX = 300


def _dmr_identity(m):
    """מפתח זהות מרשומה מנורמלת: source RID קודם, אחרת talkgroup."""
    if m.get("src") is not None:
        return ("rid", int(m["src"]))
    if m.get("tg") is not None:
        return ("tg", int(m["tg"]))
    return None


def _build_roster():
    """רוסטר מאוחד: היתוך שיחות DMR (בזיכרון) לפי זהות (RID/TG) — חי בכל מצב,
    כי ה-listener רץ תמיד ברקע. עבור RID מחזיר גם עם אילו TG-ים דיבר (בסיס
    לגרף RID↔TG של Phase 3)."""
    craft = {}
    with _dmr_lock:
        snapshot = list(_dmr_msgs)
    for m in snapshot:
        key = _dmr_identity(m)
        if key is None:
            continue
        c = craft.setdefault(key, {
            "kind": key[0], "id": key[1], "alias": None,
            "count": 0, "last_t": None, "first_t": None,
            "last_tg": None, "last_category": None, "last_group": None,
            "encrypted_seen": False, "tgs": set(),
        })
        c["count"] += 1
        t = m.get("t") or 0
        if c["first_t"] is None or t < c["first_t"]:
            c["first_t"] = t
        if c["last_t"] is None or t >= c["last_t"]:
            c["last_t"] = t
            c["last_tg"] = m.get("tg")
            c["last_category"] = m.get("category")
            c["last_group"] = m.get("group")
        if m.get("encrypted"):
            c["encrypted_seen"] = True
        if key[0] == "rid":
            c["alias"] = c["alias"] or m.get("src_alias")
            if m.get("tg") is not None:
                c["tgs"].add(int(m["tg"]))
        else:
            c["alias"] = c["alias"] or m.get("tg_alias")
    out = []
    for c in craft.values():
        c["tgs"] = sorted(c["tgs"])
        out.append(c)
    out.sort(key=lambda c: c["last_t"] or 0, reverse=True)
    return out[:ROSTER_MAX]


# --- Phase 2/3: אנליטיקה (הצפנה, תעבורה, גרף RID↔TG, מפת LRRP) --------------
# כל הפונקציות כאן טהורות (מקבלות רשימת רשומות מנורמלות) => נבדקות בלי חומרה.
# מקור הנתונים תמיד dmr.jsonl/_dmr_msgs — שום אינדוקציה, שום המצאת מדד.
ANALYTICS_TOP_N = 50
GRAPH_TOP_N = 300


def _analytics_source(day=None, show_all=False):
    """(records, error) — מקור אחיד לאנליטיקה: ?day=YYYY-MM-DD (ארכיון מהדיסק),
    ?all=1 (כל מה שבזיכרון), אחרת *היום* בלבד (כמו /api/dmr). error=None כשתקין."""
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return None, "תאריך לא תקין (פורמט: YYYY-MM-DD)"
        start, end = bounds
        return [r for r in _read_dmr_log() if start <= (r.get("t") or 0) < end], None
    if show_all:
        with _dmr_lock:
            return list(_dmr_msgs), None
    floor = _today_start()
    with _dmr_lock:
        return [m for m in _dmr_msgs if (m.get("t") or 0) >= floor], None


def _encryption_stats(recs):
    """ניתוח הצפנה: היסטוגרמת ALG + %מוצפן פר-TG. לעולם לא מפענח — רק מסכם את
    התג (encrypted/alg_name) שכבר קיים בכל כרטיס מנורמל."""
    by_alg = collections.Counter()
    tg_total, tg_enc, tg_alias = collections.Counter(), collections.Counter(), {}
    total = encrypted_total = 0
    for r in recs:
        if r.get("call_type") not in ("group", "private"):
            continue
        total += 1
        tg = r.get("tg")
        if tg is not None:
            tg_total[tg] += 1
            tg_alias.setdefault(tg, r.get("tg_alias"))
        if r.get("encrypted"):
            encrypted_total += 1
            enc = r.get("enc") or {}
            by_alg[enc.get("alg_name") or "מוצפן"] += 1
            if tg is not None:
                tg_enc[tg] += 1
    by_tg = [{"tg": tg, "tg_alias": tg_alias.get(tg), "total": tot,
              "encrypted": tg_enc.get(tg, 0), "clear": tot - tg_enc.get(tg, 0),
              "pct": round(100 * tg_enc.get(tg, 0) / tot, 1) if tot else 0.0}
             for tg, tot in tg_total.items()]
    by_tg.sort(key=lambda x: x["total"], reverse=True)
    return {
        "total": total, "encrypted_total": encrypted_total,
        "encrypted_pct": round(100 * encrypted_total / total, 1) if total else 0.0,
        "by_alg": [{"alg_name": k, "count": v} for k, v in by_alg.most_common()],
        "by_tg": by_tg[:ANALYTICS_TOP_N],
    }


def _traffic_stats(recs):
    """אנליטיקת תעבורה: air-time+שיחות פר-TG, והתפלגות שעתית (0–23, שעון מקומי)
    לזיהוי שעות עומס. dur מגיע מה-listener (dedup המשך-שיחה); None => 0 (שיחה
    בודדת שלא נצפו לה מסגרות המשך — לא מדד שהומצא, פשוט חוסר מידע)."""
    by_tg = {}
    hourly = [0] * 24
    for r in recs:
        if r.get("call_type") not in ("group", "private"):
            continue
        tg = r.get("tg")
        if tg is not None:
            e = by_tg.setdefault(tg, {"tg": tg, "tg_alias": r.get("tg_alias"),
                                      "calls": 0, "airtime": 0.0})
            e["calls"] += 1
            e["airtime"] += r.get("dur") or 0.0
            e["tg_alias"] = e["tg_alias"] or r.get("tg_alias")
        t = r.get("t")
        if t:
            hourly[time.localtime(t).tm_hour] += 1
    out = [{**e, "airtime": round(e["airtime"], 1)} for e in by_tg.values()]
    out.sort(key=lambda x: x["airtime"], reverse=True)
    return {"by_tg": out[:ANALYTICS_TOP_N], "hourly": hourly,
            "total_calls": sum(hourly)}


def _rid_tg_graph(recs):
    """גרף RID↔TG (who-talks-to-whom): צירי source-RID→talkgroup ממושקלים
    במספר שיחות. רק שיחות קבוצה (ל-private אין TG בעל משמעות רשתית)."""
    edges = collections.Counter()
    rid_alias, tg_alias = {}, {}
    for r in recs:
        if r.get("call_type") != "group":
            continue
        rid, tg = r.get("src"), r.get("tg")
        if rid is None or tg is None:
            continue
        edges[(rid, tg)] += 1
        rid_alias.setdefault(rid, r.get("src_alias"))
        tg_alias.setdefault(tg, r.get("tg_alias"))
    out = [{"rid": rid, "rid_alias": rid_alias.get(rid), "tg": tg,
            "tg_alias": tg_alias.get(tg), "weight": w}
           for (rid, tg), w in edges.items()]
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out[:GRAPH_TOP_N]


def _lrrp_snapshot():
    """מיקום אחרון-ידוע פר-RID מאירועי LRRP שבזיכרון (לא מהדיסק — "עכשיו" בלבד,
    כמו adsb.aircraft_snapshot ב-AIR-AM). {rid: {lat, lon, t, alias}}."""
    out = {}
    with _dmr_lock:
        snapshot = list(_dmr_msgs)
    for m in snapshot:
        if m.get("lat") is None or m.get("src") is None:
            continue
        rid, t = m["src"], m.get("t") or 0
        if rid not in out or t >= out[rid]["t"]:
            out[rid] = {"lat": m["lat"], "lon": m["lon"], "t": t, "alias": m.get("src_alias")}
    return out


@app.route("/api/analytics/encryption")
def api_analytics_encryption():
    """ניתוח הצפנה: ?day=YYYY-MM-DD ארכיון | ?all=1 הכל-בזיכרון | ברירת מחדל היום."""
    recs, err = _analytics_source(request.args.get("day"),
                                  request.args.get("all") in ("1", "true", "yes"))
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, **_encryption_stats(recs))


@app.route("/api/analytics/traffic")
def api_analytics_traffic():
    """אנליטיקת תעבורה: air-time/TG + heatmap שעתי. אותם פרמטרים כמו הצפנה."""
    recs, err = _analytics_source(request.args.get("day"),
                                  request.args.get("all") in ("1", "true", "yes"))
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, **_traffic_stats(recs))


@app.route("/api/analytics/graph")
def api_analytics_graph():
    """גרף RID↔TG (who-talks-to-whom). אותם פרמטרים כמו הצפנה/תעבורה."""
    recs, err = _analytics_source(request.args.get("day"),
                                  request.args.get("all") in ("1", "true", "yes"))
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, edges=_rid_tg_graph(recs))


@app.route("/api/positions")
def api_positions():
    """מיקום LRRP אחרון-ידוע פר-RID (Phase 3). ריק כשהרשת לא שולחת LRRP סטנדרטי
    (Motorola proprietary לא מפוענח ע"י DSD-FME — ר' CLAUDE.md §8)."""
    return jsonify(ok=True, positions=_lrrp_snapshot())


@app.route("/api/rf")
def api_rf():
    """איכות RF: תדירות שגיאות CRC/FEC אמיתית מ-DSD-FME (חלון RF_WINDOW_SEC).
    **אין dBFS/SNR** — נדחה במכוון (ר' CLAUDE.md §8: דורש פטצ' rsp_tcp)."""
    st = load_state()
    return jsonify(ok=True, gain_nudge=int(st.get("gain_nudge", 0)), **_rf_quality_snapshot())


@app.route("/api/gain", methods=["POST"])
def api_gain():
    """נוד-רווח חי (הקשת g/G דרך dsd_pty, בלי לעצור את DSD-FME). יחסי בלבד —
    אין readback אמיתי מ-DSD-FME, ר' _dmr_gain_nudge. דרך _guard (POST)."""
    data = request.get_json(silent=True) or {}
    direction = str(data.get("direction", "")).lower()
    if direction not in ("up", "down"):
        return jsonify(ok=False, error="direction חייב להיות up/down"), 400
    ok, val = _dmr_gain_nudge(direction)
    if not ok:
        return jsonify(ok=False, error="שליחת הפקודה נכשלה — dmr-dsdfme רץ?",
                       gain_nudge=val), 500
    return jsonify(ok=True, gain_nudge=val)


# --- הקלטות + יומן + תמלול אופציונלי ----------------------------------------
def _append_activity(rows):
    try:
        lines = ACTIVITY_PATH.read_text().splitlines()
    except OSError:
        lines = []
    lines += [json.dumps(r, ensure_ascii=False) for r in rows]
    if len(lines) > ACTIVITY_KEEP * 2:
        lines = lines[-ACTIVITY_KEEP:]
    _atomic_write(ACTIVITY_PATH, "\n".join(lines) + "\n")


def _last_logged_ts():
    try:
        for ln in reversed(ACTIVITY_PATH.read_text().splitlines()):
            try:
                return float(json.loads(ln)["ts"])
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return 0.0


def _transcript_path(wav):
    return wav.parent / (wav.name + ".txt")


def _transcribe_file(wav):
    """מריץ whisper.cpp על ה-WAV (16kHz מונו מ-DSD-FME). מחזיר טקסט או None."""
    try:
        out = subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(wav),
                              "-l", WHISPER_LANG, "-nt"],
                             capture_output=True, text=True,
                             timeout=TRANSCRIBE_TIMEOUT, check=True)
        return " ".join(out.stdout.split()).strip() or None
    except Exception:
        log.exception("transcribe %s", wav.name)
        return None


def _transcribe_worker():
    if not (Path(WHISPER_BIN).exists() and Path(WHISPER_MODEL).exists()):
        log.warning("transcription on, but whisper missing (%s / %s) - מדלג",
                    WHISPER_BIN, WHISPER_MODEL)
        return
    log.info("transcription worker started (model=%s)", WHISPER_MODEL)
    while True:
        try:
            recs = sorted(REC_DIR.glob("*.wav"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for wav in recs:
                txt = _transcript_path(wav)
                if txt.exists():
                    continue
                _atomic_write(txt, (_transcribe_file(wav) or "") + "\n")
        except Exception:
            log.exception("transcribe worker")
        time.sleep(WATCH_INTERVAL)


def _sweep_recordings():
    """retention: עד REC_MAX_FILES / REC_MAX_BYTES (חדש=>ישן). קובץ-צד תמלול
    (.txt) נמחק יחד עם ההקלטה."""
    try:
        recs = sorted(REC_DIR.glob("*.wav"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    total = 0
    for i, p in enumerate(recs):
        try:
            total += p.stat().st_size
            if i >= REC_MAX_FILES or total > REC_MAX_BYTES:
                p.unlink()
                _transcript_path(p).unlink(missing_ok=True)
        except OSError:
            pass


def _scan_new_recordings(last_seen):
    """(rows, newest) - הקלטות WAV חדשות מ-last_seen (חדש=>ישן לפי mtime)."""
    rows, newest = [], last_seen
    try:
        recs = sorted(REC_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    except OSError:
        recs = []
    for p in recs:
        try:
            stat = p.stat()
        except OSError:
            continue
        ts = round(stat.st_mtime, 1)
        if ts > last_seen:
            rows.append({"ts": ts, "file": p.name, "bytes": stat.st_size})
            newest = max(newest, ts)
    return rows, newest


def _activity_watcher():
    last_seen = _last_logged_ts()
    while True:
        try:
            rows, newest = _scan_new_recordings(last_seen)
            if rows:
                _append_activity(rows)
                last_seen = newest
            _sweep_recordings()
        except Exception:
            log.exception("activity watcher")
        time.sleep(WATCH_INTERVAL)


# --- נתיבים ----------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


_ROOT_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@app.route("/<path:fname>")
def root_asset(fname):
    mimetype = _ROOT_ASSETS.get(fname)
    if mimetype is None:
        abort(404)
    resp = send_from_directory(app.static_folder, fname, mimetype=mimetype)
    if fname == "sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/state")
def api_state():
    st = load_state()
    live = _live_mode()
    saved = st.get("app_mode", "off")
    if saved == "scan":
        plan = st.get("scan_plan") or []
        any_due = any(_leg_active_now(leg) for leg in plan) if plan else True
        st["app_mode"] = "scan"
        st["mode_ok"] = (live is not None) or not any_due
    else:
        st["app_mode"] = live or saved
        st["mode_ok"] = (live is not None) or (saved == "off")
    st.update(systems=load_systems(), version=VERSION,
              alg_names={f"0x{k:02X}": v for k, v in DMR_ALG_NAMES.items()})
    return jsonify(st)


@app.route("/api/systems", methods=["GET", "PUT"])
def api_systems():
    """PUT מחליף את רשימת המערכות כולה (עריכה בממשק על הסט המלא)."""
    if request.method == "GET":
        return jsonify(ok=True, systems=load_systems())
    data = request.get_json(silent=True)
    ok, cleaned = _validate_systems(data)
    if not ok:
        return jsonify(ok=False, error="רשימת מערכות לא תקינה", systems=load_systems()), 400
    _atomic_write(SYSTEMS_PATH, json.dumps(cleaned, ensure_ascii=False))
    log.info("systems updated (%d items, from %s)", len(cleaned), request.remote_addr)
    return jsonify(ok=True, systems=cleaned)


@app.route("/api/aliases", methods=["GET", "PUT"])
def api_aliases():
    """אליאסים TG/RID. GET מחזיר את המיזוג (CSV מיובא + עריכות ידניות).
    PUT מחליף את מפת העריכות הידניות (aliases.json)."""
    if request.method == "GET":
        return jsonify(ok=True, aliases=aliasdb.export_all())
    data = request.get_json(silent=True)
    ok, err = aliasdb.replace_manual(data)
    if not ok:
        return jsonify(ok=False, error=err), 400
    log.info("aliases updated (from %s)", request.remote_addr)
    return jsonify(ok=True, aliases=aliasdb.export_all())


@app.route("/api/health")
def api_health():
    """סטטוס המערכת — מאפשר ל-UI להבדיל בין "אין תעבורה" ל"משהו נפל"."""
    services = {}
    for svc in ("sdrplay", DMR_SERVICE):
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=5)
            services[svc] = (r.stdout.strip() or "unknown")
        except Exception:
            services[svc] = "unknown"
    saved_state = load_state()
    saved = saved_state.get("app_mode", "off")
    dmr_active = services[DMR_SERVICE] == "active"
    off_ok = (saved == "off" and not dmr_active)
    if saved == "scan":
        plan = saved_state.get("scan_plan") or []
        any_due = any(_leg_active_now(leg) for leg in plan) if plan else True
        mode, ok = "scan", dmr_active or not any_due
    else:
        mode = ("dmr" if dmr_active else saved)
        ok = dmr_active if mode == "dmr" else off_ok
    # מדדי פיד: מספר שיחות היום + זמן השיחה האחרונה (חי חושף "האם אני מפענח")
    with _dmr_lock:
        floor = _today_start()
        today = [m for m in _dmr_msgs if (m.get("t") or 0) >= floor]
        calls_today = len(today)
        last_call = max((m.get("t") or 0 for m in _dmr_msgs), default=0) or None
    return jsonify(ok=ok, app_mode=mode, services=services,
                   sdr_present=_sdr_present(), calls_today=calls_today,
                   last_call_at=last_call)


# --- פיד DMR + ייצוא + ארכיון -----------------------------------------------
@app.route("/api/dmr")
def api_dmr():
    """שיחות DMR אחרונות. ?since=<id> => רק חדשות מאותו cursor (פולינג יעיל).
    כברירת מחדל רק *היום*; ?all=1 => כל מה שבזיכרון; ?day=YYYY-MM-DD => ארכיון מהדיסק."""
    day = request.args.get("day")
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return jsonify(ok=False, error="תאריך לא תקין (פורמט: YYYY-MM-DD)"), 400
        start, end = bounds
        msgs = [r for r in _read_dmr_log() if start <= (r.get("t") or 0) < end]
        return jsonify(ok=True, day=day, messages=msgs)
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    show_all = request.args.get("all") in ("1", "true", "yes")
    floor = 0 if show_all else _today_start()
    with _dmr_lock:
        msgs = [dict(m) for m in _dmr_msgs
                if m["id"] > since and (m.get("t") or 0) >= floor]
        cursor = _dmr_seq
    return jsonify(ok=True, active=_is_active(DMR_SERVICE), cursor=cursor, messages=msgs)


DMR_EXPORT_COLS = ["time_iso", "timestamp", "proto", "freq", "slot", "cc", "lcn",
                   "call_type", "category", "tg", "tg_alias", "src", "src_alias",
                   "tgt", "encrypted", "alg_name", "ber", "level", "dur",
                   "lat", "lon", "text"]


@app.route("/api/dmr/export")
def api_dmr_export():
    """ייצוא כל שיחות ה-DMR השמורות (dmr.jsonl). ?format=csv (BOM) | json."""
    return dsd_export.export_response(app, request, _read_dmr_log(), DMR_EXPORT_COLS, "dmr")


@app.route("/api/aircraft")
@app.route("/api/roster")
def api_roster():
    """רוסטר רדיו-IDs / talkgroups מאוחד — חי בכל מצב (הנתונים בזיכרון, לא תלוי
    SDR הפעיל). (‎/api/aircraft alias לתאימות עם תבנית ה-UI המשוכפלת)."""
    return jsonify(ok=True, roster=_build_roster())


@app.route("/api/activity")
def api_activity():
    """הקלטות אחרונות, חדש=>ישן. exists=False כשההקלטה כבר נמחקה ב-retention."""
    try:
        lines = ACTIVITY_PATH.read_text().splitlines()
    except OSError:
        lines = []
    events = []
    for ln in reversed(lines):
        if len(events) >= ACTIVITY_RETURN:
            break
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        ev["exists"] = bool(ev.get("file")) and (REC_DIR / ev["file"]).is_file()
        ev["text"] = None
        if ev.get("file"):
            try:
                ev["text"] = (REC_DIR / (ev["file"] + ".txt")).read_text().strip() or None
            except OSError:
                pass
        events.append(ev)
    return jsonify(ok=True, events=events)


@app.route("/recordings/<name>")
def recordings(name):
    return send_from_directory(str(REC_DIR), name)


def _vcgencmd(*args):
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


@app.route("/api/power")
def api_power():
    """מצב אספקת המתח והטמפרטורה של ה-Pi (get_throttled / pmic_read_adc / measure_temp)."""
    out = _vcgencmd("get_throttled")
    if out is None:
        return jsonify(ok=False)
    flags = 0
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if m:
        flags = int(m.group(1), 16)
    volts_in = None
    adc = _vcgencmd("pmic_read_adc")
    if adc:
        mv = re.search(r"EXT5V_V\s+volt\([^)]*\)=([0-9.]+)", adc)
        if mv:
            volts_in = round(float(mv.group(1)), 2)
    temp = None
    mt = re.search(r"=([0-9.]+)", _vcgencmd("measure_temp") or "")
    if mt:
        temp = round(float(mt.group(1)), 1)
    return jsonify(ok=True, throttled=hex(flags),
                   undervolt_now=bool(flags & 0x1), throttle_now=bool(flags & 0x4),
                   undervolt_ever=bool(flags & 0x10000), throttle_ever=bool(flags & 0x40000),
                   volts_in=volts_in, temp=temp)


@app.route("/api/mode", methods=["POST"])
def api_mode():
    """מעבר בין המצבים: dmr (DSD-FME) / off (standby) / scan (סבב בין מערכות).
    SDR אחד בהחלפה. כישלון כניסה => off (בלי fallback). POST => עובר דרך _guard."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).lower()
    if mode not in ("dmr", "off", "scan"):
        return jsonify(ok=False, error="mode לא תקין (dmr/off/scan)"), 400

    # ולידציה סטטית קודם (לא תלוית-נעילה) — בקשה עם פרמטרים לא-תקינים (400) לא
    # נוגעת בסבב סריקה פעיל (מניעת "scan זומבי"). _scan_stop_thread נקרא רק אחרי
    # שתפסנו את TUNE_LOCK.
    st = load_state()
    plan = system = None
    if mode == "scan":
        plan = _validate_scan_plan(data.get("plan") or st.get("scan_plan"))
        if plan is None:
            return jsonify(ok=False, error="לוח סריקה לא תקין (1-8 רגלים, "
                           "כל רגל מערכת+זמן שהייה תקין)", state=st), 400
    elif mode == "dmr":
        sid = data.get("system") or st.get("system")
        systems = load_systems()
        system = _find_system(systems, sid) if sid else (systems[0] if systems else None)
        if system is None:
            return jsonify(ok=False, error="לא נבחרה מערכת DMR תקינה", state=st), 400

    if not TUNE_LOCK.acquire(timeout=0.5):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב",
                       state=load_state()), 409
    try:
        _scan_stop_thread()
        st = load_state()
        if mode == "off":
            log.info("mode -> OFF (standby) (from %s)", request.remote_addr)
            err, detail = _enter_standby()
            if err:
                log.warning("enter standby failed: %s", err)
                return jsonify(ok=False, error=err, detail=detail, state=st), 500
            new_state = {**st, "app_mode": "off", "prev_mode": st.get("app_mode", "off")}
            save_state(new_state)
            return jsonify(ok=True, app_mode="off")

        if mode == "scan":
            log.info("mode -> SCAN plan=%s (from %s)", plan, request.remote_addr)
            err, detail = _scan_activate(plan)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter scan (leg 0)")
                return jsonify(payload), status
            new_state = {**st, "app_mode": "scan", "scan_plan": plan}
            save_state(new_state)
            return jsonify(ok=True, app_mode="scan", scan_plan=plan)

        # mode == "dmr"
        log.info("mode -> DMR system=%s (from %s)", system["id"], request.remote_addr)
        err, detail = _enter_dmr(system)
        if err:
            payload, status = _fail_to_off(st, err, detail, "enter dmr")
            return jsonify(payload), status
        # gain_nudge כבר אופס בתוך _enter_dmr (restart אמיתי) — טוענים state עדכני
        new_state = {**load_state(), "app_mode": "dmr", "system": system["id"]}
        save_state(new_state)
        return jsonify(ok=True, app_mode="dmr", system=system["id"])
    finally:
        TUNE_LOCK.release()


@app.route("/api/scan")
def api_scan():
    """סטטוס סבב הסריקה החי: רגל נוכחית, אינדקס, ומועד המעבר הבא."""
    with _scan_lock:
        status = dict(_scan_status)
        active = _scan_thread is not None and _scan_thread.is_alive()
    return jsonify(ok=True, active=active, **status)


# --- שחזור מצב באתחול: dmr-web הוא המתזמר -----------------------------------
BOOT_SDR_WAIT_SEC = 90


def _boot_restore():
    """אורקסטרציית אתחול: dmr-dsdfme אינו enabled ב-systemd — dmr-web (שעולה תמיד)
    קורא את state.json ומחזיר את המצב השמור (dmr/off/scan). רץ ב-thread daemon;
    כל כישלון => off + לוג, לעולם לא מפיל את שרת הווב."""
    try:
        st = load_state()
        mode = st.get("app_mode", "off")
        live = _live_mode()
        if live == mode:
            return
        if mode == "off":
            if live:
                _enter_standby()
            return
        for _ in range(BOOT_SDR_WAIT_SEC // 2):
            if _sdr_present():
                break
            time.sleep(2)
        if not TUNE_LOCK.acquire(blocking=False):
            return
        try:
            st2 = load_state()
            if st2.get("app_mode", "off") != mode:
                log.info("boot restore: המצב השמור השתנה בזמן ההמתנה ל-SDR — מוותרים")
                return
            st = st2
            if mode == "dmr":
                systems = load_systems()
                system = _find_system(systems, st.get("system")) or (systems[0] if systems else None)
                if system is None:
                    err, _detail = "אין מערכת DMR שמורה", None
                else:
                    err, _detail = _enter_dmr(system)
            else:   # scan
                plan = _validate_scan_plan(st.get("scan_plan"))
                if plan is None:
                    err, _detail = "לוח סריקה שמור לא תקין", None
                else:
                    err, _detail = _scan_activate(plan)
            if err:
                log.warning("boot restore -> %s failed: %s — falling to off", mode, err)
                _enter_standby()
                save_state({**st, "app_mode": "off", "prev_mode": mode})
            else:
                log.info("boot restore -> %s", mode)
        finally:
            TUNE_LOCK.release()
    except Exception:
        log.exception("boot restore crashed (ignored)")


if __name__ == "__main__":
    aliasdb.load()   # טעינת אליאסים (CSV מיובא + עריכות ידניות) לזיכרון
    threading.Thread(target=_boot_restore, daemon=True).start()
    REC_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    _load_dmr_history()                                            # היסטוריית היום שורדת restart (לפני ה-listener)
    threading.Thread(target=_dmr_listener, daemon=True).start()    # פיד UDP מ-dsd_pty (שקט ב-standby)
    if TRANSCRIBE:
        threading.Thread(target=_transcribe_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
