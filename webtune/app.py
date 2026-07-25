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
import discovery as discmod # גילוי רשתות: ולידציה/גריד/זיהוי-מועמדים/סיכום-בדיקה (טהור)
import dsd_export           # ייצוא CSV/JSON (BOM ל-Excel)
import dsd_pty              # build_command וכו', וגם send_gain_nudge (נוד-רווח חי דרך PTY)
import watchlist            # מעקב RID/TG — תיוג כרטיס, התראה מקומית בלבד (ר' §8, בלי Push API)
import system_intel         # מודיעין-מערכת: אתרים/מפת-תפוסה/CDR/סחיפת-CC (Phase 8)

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

# גשר IQ→PCM (dsd_pty מריץ rsp_tcp + rsp_fm.py כתהליכי-בן; ר' CLAUDE.md §2).
# אלה נתיבי loopback/פרמטרים קבועים של התשתית (לא פר-מערכת) — זהים לברירות-
# המחדל במודולי dsd_pty/rsp_fm עצמם. render_dmr_env דורס את כל dmr.env בכל
# מעבר מצב, אז חובה לכלול אותם כאן — אחרת הם נעלמים מהקובץ החי בכל מעבר.
DMR_BRIDGE_RTLTCP = "127.0.0.1:1234"
DMR_BRIDGE_AUDIO_TCP = "127.0.0.1:7355"
DMR_BRIDGE_AUDIO_TCP_BASE = "127.0.0.1:7355"   # multi mode: instance i gets base_port+i
DMR_BRIDGE_RIGCTL = "127.0.0.1:4532"
DMR_BRIDGE_IQ_RATE = 240000
DMR_BRIDGE_AUDIO_GAIN = 4.0

# --- מצב 'multi' (Phase 2): פענוח כל ערוצי ה-channelmap בו-זמנית -----------
# קליטה רחבת-פס אחת (rsp_tcp) + N מדמודלטורים NFM מוסטים (rsp_fm.py) + N
# מפענחי DSD-FME, אחד לכל ערוץ פיזי. ר' dsd_pty._run_multi/compute_wideband_plan.
MULTI_GUARD_HZ = 25_000          # מרווח-שוליים בכל צד הטווח (Hz)
MULTI_MAX_SPAN_HZ = 2_000_000    # תקרת רוחב-פס לקליטה רחבת-פס אחת (RSP1B)
MULTI_CHANNELS_MAX = 8           # תקרה שמרנית עד מדידת CPU בפועל על Pi 5 (ר' CLAUDE.md)
MULTI_MIN_SEP_HZ = 12_500        # מרווח מינימלי בין ערוצים (raster DMR 12.5kHz): קרוב יותר = כפילות/טעות
DMR_BUF_MAX = 800                     # שיחות אחרונות בזיכרון (נטענות בעלייה, היום בלבד)
DMR_LOG_PATH = Path("/var/lib/dmr/dmr.jsonl")
DMR_LOG_KEEP = 8000                   # retention בדיסק (זנב נשמר; ייצוא לניתוח)

# --- גילוי רשתות (frequency discovery) --------------------------------------
# מצב 'discover': job חולף בזיכרון (לא מַתמיד ב-state) — סורק ספקטרום FFT (rsp_fm
# במצב sweep), מזהה תדרים חשודים כ-DMR, ואז בודק כל מועמד עם DSD-FME. ר' CLAUDE.md §2.
DISCOVERY_PATH = Path("/var/lib/dmr/discovery.json")   # דוח הגילוי האחרון
DISCOVERY_SETTLE_SEC = 0.25           # המתנת התייצבות אחרי retune לפני קריאת ספקטרום
DISCOVERY_SPECTRUM_TRIES = 8          # ניסיונות קריאת SPECTRUM לכל מרכז (עד שמתייצב)

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
# ★ v0.14.0 — תוכן הודעות טקסט (TMS, פורט 4007). דלוק כברירת מחדל: זו תעבורה
# שהרשת משדרת בגלוי ו-DSD-FME מפענח, וזה בדיוק סוג המודיעין שהתחנה בנויה לו.
# כיבוי: DMR_CAPTURE_TEXT=0 ב-/etc/dmr/dmr-web.env => המטא-דאטה (מי→מי, פורט,
# אורך) נשמר כרגיל וה**תוכן** לא נשמר ולא מוצג.
CAPTURE_TEXT = os.environ.get("DMR_CAPTURE_TEXT", "1").strip().lower() not in ("0", "false", "no", "off")
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


CHANNELMAP_HEADER = "# LCN,FREQ_HZ"


def render_channelmap(channelmap, lsn_pairs=False):
    """בונה את תוכן channelmap.csv (LCN,FREQ_HZ) ל-DSD-FME (‎-C). התדרים ב-Hz.
    פורמט DSD-FME: כל שורה 'lcn,freq_hz'. פונקציה טהורה => נבדקת בלי חומרה.

    ⚠ שורת-הכותרת אינה קוסמטית — היא **חובה**. csvChanImport של DSD-FME
    (src/dsd_import.c: `if (row_count == 1) continue; //don't want labels`)
    מדלג על השורה הראשונה **תמיד**, בלי לבדוק אם היא כותרת. בלעדיה הערוץ
    הראשון במפה נזרק בשקט בכל הרצת-טראנקינג. הכותרת מתחילה ב-'#' כדי
    שגם dsd_pty.parse_channelmap_hz/rsp_fm.parse_channelmap_hz (שקוראים את
    *אותו* קובץ ב-multi mode) ידלגו עליה — שם היא כן צריכה להיות מדולגת-במפורש.

    lsn_pairs=True (מצב טראנקינג חד-ערוצי): ב-Cap+ המפה של DSD-FME מאונדקסת
    ב-**LSN** ולא בערוץ פיזי, וכל תדר נושא שני LSN-ים — LSN 1+2 על הערוץ
    הפיזי הראשון, 3+4 על השני וכן הלאה (אומת מול dmr_csbk.c, שמאנדקס
    trunk_chan_map[LSN] וגוזר את ה-slot מזוגיות ה-LSN, ומול תיעוד ה-upstream).
    לכן ערוץ פיזי n מתפרש לשתי שורות: LSN 2n-1 ו-LSN 2n, שתיהן אותו תדר.
    בלי ההרחבה DSD-FME מכוון לתדר הלא-נכון בכל מעקב-שיחה (הוא קורא את
    השורה שמספרה = ה-LSN, ואצלנו שם ישב ערוץ פיזי אחר לגמרי).
    ⚠ ההרחבה מתקנת את ה**פורמט**; ה**סדר** (איזה תדר הוא הערוץ הפיזי הראשון)
    נשאר השערה עד שרדאר-המערכת מגלה אותו בפועל — ר' system_intel.derive_lsn_map.
    ב-multi mode ההרחבה **אסורה**: שם אותו קובץ הוא רשימת הערוצים הפיזיים
    שרוצים לדמודל, ושורות כפולות היו מייצרות מדמודלטורים כפולים."""
    lines = [CHANNELMAP_HEADER]
    for ch in channelmap or []:
        try:
            lcn = int(ch["lcn"])
            hz = int(round(float(ch["freq"]) * 1e6))
        except (KeyError, TypeError, ValueError):
            continue
        if lsn_pairs:
            lines.append(f"{lcn * 2 - 1},{hz}")
            lines.append(f"{lcn * 2},{hz}")
        else:
            lines.append(f"{lcn},{hz}")
    return "\n".join(lines) + "\n"


def write_channelmap(channelmap, lsn_pairs=False):
    _atomic_write(CHANNELMAP_PATH, render_channelmap(channelmap, lsn_pairs=lsn_pairs))


def render_dmr_env(system, multi=False):
    """בונה את תוכן dmr.env (EnvironmentFile של systemd, KEY=VALUE מנותח בבטחה).
    dsd_pty קורא את המשתנים ובונה מהם את שורת הפקודה של DSD-FME.
    ⚠ DSD_CONTROL_FREQ ב-Hz (DSD-FME/rigctl); ה-state/UI עובדים ב-MHz — ההמרה כאן.
    שדות אופציונליים פר-מערכת: trunk (ברירת מחדל 1), sweep (מצב סריקת FFT),
    emit_status (אירועי sync/channel_status לבדיקת גילוי), no_wav (בלי הקלטה).

    multi=True (Phase 2, app_mode='multi'): מפענח את כל ערוצי ה-channelmap
    בו-זמנית (dsd_pty._run_multi) במקום מעקב-טראנקינג -T של DSD-FME על תדר
    יחיד. DSD_IQ_RATE נשאר ברירת-המחדל החד-ערוצית (לא בשימוש ב-_run_multi —
    הוא גוזר קצב-IQ רחב-פס משלו מ-DSD_CHANNELMAP + DSD_MULTI_GUARD_HZ/
    DSD_MULTI_MAX_RATE_HZ, שני המפתחות האחרונים נכתבים כאן מהקבועים
    MULTI_GUARD_HZ/MULTI_MAX_SPAN_HZ של המודול הזה — **אותם קבועים** ש-
    _validate_multi_feasible אימת מולם; אחרת חישוב ה-compute_wideband_plan
    העצמאי של dsd_pty (הכרחי כדי להזין בדיוק אותם center/rate גם ל-rsp_tcp
    וגם ל-rsp_fm.py — שני תהליכי-בן נפרדים) עלול לסטות מברירות-מחדל אחרות)."""
    control_hz = int(round(float(system["control"]) * 1e6))
    cc = int(system.get("color_code", 1))
    trunk = "1" if str(system.get("trunk", 1)).lower() in ("1", "true", "yes") else "0"
    sweep = bool(system.get("sweep"))
    iq_rate = int(system.get("iq_rate", DMR_BRIDGE_IQ_RATE))
    lines = [
        '# נכתב אוטומטית ע"י DMR web (מעבר מצב). שינויים ידניים נדרסים.',
        f"# מערכת: {system.get('name', system.get('id', ''))}",
        f"DSD_CONTROL_FREQ={control_hz}",   # Hz — ערוץ הבקרה (CC) של Cap+ / מרכז התחלתי בסריקה
        f"DSD_COLOR_CODE={cc}",
        f"DSD_CHANNELMAP={CHANNELMAP_PATH}",
        f"DSD_UDP={DMR_UDP_HOST}:{DMR_UDP_PORT}",   # יעד פיד ה-JSON (dsd_pty → app.py)
        f"DSD_TRUNK={trunk}",                        # מעקב טראנקינג (Cap+); 0 בבדיקת גילוי
        f"DSD_RTLTCP={DMR_BRIDGE_RTLTCP}",           # rsp_tcp — IQ גולמי מה-RSP1B
        f"DSD_AUDIO_TCP={DMR_BRIDGE_AUDIO_TCP}",     # rsp_fm.py — PCM 48kHz ל-DSD-FME (חד-ערוצי)
        f"DSD_AUDIO_TCP_BASE={DMR_BRIDGE_AUDIO_TCP_BASE}",  # rsp_fm.py — בסיס-פורטים ל-multi (base+i)
        f"DSD_RIGCTL={DMR_BRIDGE_RIGCTL}",           # rsp_fm.py — rigctl לכיוונון/סריקה
        f"DSD_IQ_RATE={iq_rate}",                    # קצב IQ מבוקש מ-rsp_tcp (Hz; גבוה בסריקה)
        f"DSD_AUDIO_GAIN={DMR_BRIDGE_AUDIO_GAIN}",   # מכפיל רווח discriminator ב-rsp_fm.py
    ]
    if multi:
        lines += [
            "DSD_MULTI=1",                            # dsd_pty._run() -> _run_multi()
            f"DSD_MULTI_GUARD_HZ={MULTI_GUARD_HZ}",
            f"DSD_MULTI_MAX_RATE_HZ={MULTI_MAX_SPAN_HZ}",
        ]
    if not sweep and not system.get("no_wav"):
        lines.append(f"DSD_WAV_DIR={REC_DIR}")       # per-call WAV לתיקיית ההקלטות
    if sweep:
        lines += [
            "DSD_SWEEP=1",                            # מצב סריקת FFT (בלי DSD-FME/demod)
            f"DSD_SWEEP_NFFT={int(system.get('nfft', discmod.DEFAULT_NFFT))}",
            f"DSD_SWEEP_GAIN={int(system.get('gain_index', discmod.DEFAULT_GAIN_INDEX))}",
        ]
    if system.get("emit_status"):
        lines.append("DSD_EMIT_STATUS=1")            # אירועי sync/channel_status לגילוי
    lines.append("")
    return "\n".join(lines)


def write_dmr_env(system, multi=False):
    _atomic_write(DMR_ENV_PATH, render_dmr_env(system, multi=multi))


def _validate_multi_feasible(system):
    """(ok, error) — מערכת נכנסת ל-multi רק אם יש לה >=2 ערוצי channelmap
    והם נכנסים בקליטה רחבת-פס אחת (RSP1B, עד MULTI_MAX_SPAN_HZ). טהורה —
    נבדקת בלי חומרה, ומראה אותה שגיאה שדחיית compute_wideband_plan הייתה
    נותנת, אבל ב-/api/mode (400) ולא כקריסת dsd_pty מאוחרת יותר."""
    cmap = system.get("channelmap") or []
    if len(cmap) < 2:
        return False, "מצב רב-ערוצי דורש לפחות 2 ערוצים במפת המערכת"
    if len(cmap) > MULTI_CHANNELS_MAX:
        return False, f"מצב רב-ערוצי תומך עד {MULTI_CHANNELS_MAX} ערוצים"
    # ⚠ LCN חייב להיות ייחודי ב-multi: dsd_pty מריץ מפענח + פורט-אודיו לכל
    # רשומה, אבל rsp_fm.MultiChannelBridge ממפתח לפי LCN — LCN כפול היה
    # מפיל דמודלטור בשקט וגורם ל-bring-up להיכשל אטומית. נכשל כאן ב-400 עם
    # הודעה ברורה במקום כקריסה מאוחרת (לא מהדק את _validate_systems הגלובלי
    # כדי לא לדחות בשקט מערכות קיימות — הבעיה רלוונטית רק ל-multi).
    lcns = [int(ch["lcn"]) for ch in cmap]
    if len(set(lcns)) != len(lcns):
        return False, "מצב רב-ערוצי דורש LCN ייחודי לכל ערוץ (יש LCN כפול במפה)"
    chan_hz = [int(round(float(ch["freq"]) * 1e6)) for ch in cmap]
    # ⚠ תדרים חייבים להיות רחוקים ≥MULTI_MIN_SEP_HZ זה מזה: שני ערוצים באותו תדר
    # (או קרובים מ-raster DMR) הם כפילות/טעות — היו מריצים שני מדמודלטורי-NFM
    # חופפים (offset זהה) ושני מפענחים על אותו אות, אירועים כפולים בלי הפרדה
    # אמיתית. נכשל כאן ב-400 במקום להטעות בשקט. (כל אשכולות הסקר ≥25kHz — עוברים.)
    ordered = sorted(chan_hz)
    tight = min((b - a for a, b in zip(ordered, ordered[1:])), default=MULTI_MIN_SEP_HZ)
    if tight < MULTI_MIN_SEP_HZ:
        return False, (f"מצב רב-ערוצי דורש מרווח ≥{MULTI_MIN_SEP_HZ // 1000}kHz בין "
                       f"תדרים (יש זוג במרווח {tight} Hz — כפילות או טעות במפה)")
    try:
        dsd_pty.compute_wideband_plan(chan_hz, guard_hz=MULTI_GUARD_HZ,
                                      max_rate=MULTI_MAX_SPAN_HZ)
    except ValueError as exc:
        return False, str(exc)
    return True, None


# --- מצב DMR: כניסה + standby ----------------------------------------------
def _enter_dmr(system, multi=False):
    """כותב env + channelmap ומריץ את dmr-dsdfme (DSD-FME תחת PTY). מחזיר
    (error, detail). מבנה זהה ל-_enter_acars ב-AIR-AM: write-env → restart → poll
    לקריסה מאוחרת (השירות יכול לעלות ואז לקרוס על תדר/מפה רעים ~2ש' אחר-כך).
    multi=True: מצב רב-ערוצי (Phase 2) — ר' render_dmr_env.

    ⚠ אותו קובץ channelmap.csv משרת שתי סמנטיקות שונות, ולכן lsn_pairs תלוי-מצב:
    בחד-ערוצי הוא מפת ה-LSN של DSD-FME (זוגות — ר' render_channelmap), ב-multi
    הוא רשימת הערוצים הפיזיים לדמודולציה (שורה לערוץ, בלי הכפלה)."""
    write_channelmap(system.get("channelmap"), lsn_pairs=not multi)
    write_dmr_env(system, multi=multi)
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
    # restart אמיתי = כל מפענחי ה-multi עולים מ-0 => סטטוס-הערוצים (Phase 7
    # partial-restart) של הריצה הקודמת כבר לא רלוונטי.
    with _channel_status_lock:
        _channel_status.clear()
    # מטמון בזיכרון (לא load_systems()/load_state() בכל אירוע UDP — lsn_status
    # תכוף מדי, ר' _dmr_listener/system_intel). __probe__/__sweep__ (גילוי)
    # מסוננים בדיספאץ', לא כאן, כדי לשמור את _enter_dmr פשוט.
    global _active_system_id, _active_color_code
    _active_system_id = system.get("id")
    _active_color_code = system.get("color_code")
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


MODE_SERVICE = {"dmr": DMR_SERVICE, "multi": DMR_SERVICE}   # שתיהן אותה יחידת systemd (§0)
_active_system_id = None    # נקבע ב-_enter_dmr; _dmr_listener קורא אותו (לא load_state()
                            # בכל אירוע UDP — lsn_status תכוף מדי, ר' system_intel.py)
_active_color_code = None   # ה-color_code המוגדר למערכת הפעילה — להשוואה מול נצפה (CC-drift)


def _intel_system_id():
    """מזהה-המערכת הפעילה לצורך system_intel, או None (standby/אין מערכת/
    מערכת-גילוי חולפת __probe__/__sweep__ — לא נשמרת ולא מסמאת את הבנק)."""
    sid = _active_system_id
    if not sid or sid.startswith("__"):
        return None
    return sid


def _live_mode():
    """המצב שרץ בפועל (לפי השירות), או None כשהצרכן לא פעיל. dmr/multi חולקות
    את אותה יחידת systemd (dmr-dsdfme) — systemctl לא יכול להבדיל ביניהן,
    אז נעזרים במצב השמור (state.json) כדי לדווח את המצב הנכון."""
    if not _is_active(DMR_SERVICE):
        return None
    saved = load_state().get("app_mode")
    return saved if saved in MODE_SERVICE else "dmr"


# --- פיד השיחות: נרמול, התמדה, listener ------------------------------------
_dmr_lock = threading.Lock()
_dmr_msgs = collections.deque(maxlen=DMR_BUF_MAX)
_dmr_seq = 0                    # מזהה רץ גלובלי (cursor ל-UI)

# ★ v0.13.0 — מצב ה-listener וחיוניותו.
# CALL_CLOSE_SEC חייב להיות גדול **משני** החלונות שמְמַתְּתים כרטיס אחרי יצירתו:
# dedup (8ש', מצטבר ל-dur/frames) וקורלציית ההצפנה (15ש'). כתיבה מוקדמת מדי
# הייתה מקפיאה את הכרטיס בדיסק לפני שהמידע הזה נוסף — הבאג שתוקן כאן.
# המחיר המודע: קריסת-listener מאבדת שיחות שטרם נסגרו (≤ CALL_CLOSE_SEC).
CALL_CLOSE_SEC = 20.0
CLOSE_SWEEP_INTERVAL_SEC = 2.0    # מְמוּתָן — ר' הלופ ב-_dmr_listener
FEED_WINDOW_SEC = 60.0
_LISTENER_INTERNAL_KEYS = ("_start",)   # מפתחות-עבודה שלא נכתבים לארכיון


DATA_CTX_SEC = 5.0   # חלון קורלציית שכבת-הדאטה (ip_data → position/text)


def _new_listener_ctx():
    return {"dedup": {}, "slot_open": {}, "pending": {}, "seen": 0, "last_sweep": 0.0,
            # ★ v0.14.0 — הקשר PDU-דאטה פר-ערוץ פיזי: {phys_lcn: (t, {...})}.
            # שורות ה-SRC(24)/DST(24) מודפסות **לפני** ה-payload של אותו PDU
            # (dmr_pdu.c), ולכן ה-RID/פורט של המיקום או ההודעה שיגיעו מיד
            # אחריהם נמצא כאן. אותו דפוס בדיוק כמו _slot_open_call של ההצפנה,
            # עם חלון קצר יותר — זו סמיכות-שורות באותו PDU, לא חלון-שיחה.
            "data_ctx": {},
            # כרטיס המיקום הפתוח פר-ערוץ. שדות ה-LRRP הנוספים (רדיוס/גובה/
            # מהירות/כיוון) מודפסים **אחרי** שורת ה-Lat/Lon — כלומר אחרי שהכרטיס
            # נוצר => מוטציה, בדיוק כמו תג-ההצפנה על שיחה פתוחה. `Time:` לעומת
            # זאת מודפס **לפני**, ולכן הוא נכנס דרך data_ctx.
            "pos_open": {}}


_listener_ctx = _new_listener_ctx()   # מודולרי (לא לוקאלי ל-thread) כדי ש-
_listener_bound = None                # _close_stale_calls יוכל לרוץ גם מ-watcher
_listener_thread = None               # None=לא הופעל, False=bind נכשל
_feed_lock = threading.Lock()
_feed_stats = {"last_datagram_at": None, "last_voice_at": None,
               "handler_errors": 0, "voice_miss": 0, "voice_miss_last": None,
               "total": 0}
_feed_ticks = collections.deque()     # (t, type) — נגזם לחלון FEED_WINDOW_SEC


def _feed_tick(msg, now=None):
    """מונה-חיוניות: נרשם על **כל** דאטהגרם, לפני הדיספאץ' ולפני כל פרסור —
    כדי שגם דאטהגרם שמפיל את הטיפול ייספר. זה האות שמבדיל "הרשת שקטה"
    מ"שרשרת-הפענוח מתה", שעד כה לא היה קיים בשום נקודת-API (ר' §8)."""
    now = now if now is not None else time.time()
    mtype = str(msg.get("type") or "unknown") if isinstance(msg, dict) else "unknown"
    with _feed_lock:
        _feed_stats["total"] += 1
        _feed_stats["last_datagram_at"] = now
        if mtype == "voice_call":
            _feed_stats["last_voice_at"] = now
        _feed_ticks.append((now, mtype))
        cutoff = now - FEED_WINDOW_SEC
        while _feed_ticks and _feed_ticks[0][0] < cutoff:
            _feed_ticks.popleft()


def _feed_snapshot(now=None):
    """עובדות בלבד על זרם הדאטהגרמים — בלי לאבחן. ההבחנה הנגזרת
    (decode_state) נעשית ב-api_health, ואפילו שם היא שמרנית: "silent" ולא
    "שבור", כי בחד-ערוצי non-trunk דממה אמיתית היא מצב לגיטימי."""
    now = now if now is not None else time.time()
    with _feed_lock:
        cutoff = now - FEED_WINDOW_SEC
        by_type = collections.Counter(t for ts, t in _feed_ticks if ts >= cutoff)
        stats = dict(_feed_stats)
    return {
        "window_sec": int(FEED_WINDOW_SEC),
        "datagrams_window": int(sum(by_type.values())),
        "by_type": [{"type": t, "count": c} for t, c in by_type.most_common()],
        "last_datagram_at": stats["last_datagram_at"],
        "last_voice_at": stats["last_voice_at"],
        "handler_errors": stats["handler_errors"],
        "voice_miss": stats["voice_miss"],
        "voice_miss_last": stats["voice_miss_last"],
        "total": stats["total"],
    }


def _listener_alive():
    """True/False/None — None כשה-thread מעולם לא הופעל (למשל בבדיקות)."""
    if _listener_bound is False:
        return False
    if _listener_thread is None:
        return None
    return bool(_listener_thread.is_alive())


def _reset_listener_ctx():
    """מתקין ctx נקי ל-listener, אחרי שסגר (וכתב לדיסק) שיחות תלויות מהקודם.
    נקרא בעליית ה-listener: הקמה-מחדש ע"י ה-watchdog לא צריכה לירוש חלון-dedup
    ישן — ובוודאי לא לאבד שיחות שכבר נצפו אך טרם נכתבו."""
    global _listener_ctx
    try:
        _close_stale_calls(_listener_ctx, force=True)
    except Exception:
        log.exception("סגירת שיחות תלויות בעליית ה-listener נכשלה")
    _listener_ctx = _new_listener_ctx()
    return _listener_ctx


_DATA_CTX_FIELDS = ("kind", "port", "fix_time", "radius_m", "alt_m",
                    "speed_kmh", "track_deg")
_LRRP_EXTRA_FIELDS = ("fix_time", "radius_m", "alt_m", "speed_kmh", "track_deg")
# סוגי PDU שאין להם שורת-payload שאנחנו מפרסרים — שורת ה-DST היא האירוע.
_DATA_KINDS_NO_PAYLOAD = frozenset({"ars", "telemetry", "otap", "battery",
                                    "jobticket", "xcmp"})


def _apply_lrrp_extra(ctx, phys_lcn, msg, now=None):
    """מחיל שדה-לוואי של LRRP על כרטיס המיקום הפתוח באותו ערוץ. מחזיר True אם
    הוחל. מוטציה תחת _dmr_lock — הרשומה חיה גם ב-_dmr_msgs וגם ב-pending
    (ולכן היא תיכתב לארכיון עם השדות, ולא בלעדיהם)."""
    entry = ctx["pos_open"].get(phys_lcn)
    if entry is None:
        return False
    now = _float_or_none(msg.get("t")) or (now if now is not None else time.time())
    if now - entry[0] > DATA_CTX_SEC:
        return False
    applied = False
    with _dmr_lock:
        for key in _LRRP_EXTRA_FIELDS:
            if msg.get(key) is not None:
                entry[1][key] = msg[key]
                applied = True
    return applied


def _data_ctx_update(ctx, phys_lcn, msg):
    """צובר הקשר-PDU: `ip_data` (RID+פורט, role=src/dst) ו-`lrrp_extra`
    (זמן/רדיוס/גובה/מהירות/כיוון, כל אחד בשורה נפרדת). מתאפס בכל `ip_data`
    עם role=src — זו תחילת PDU חדש, ואסור שנתון מ-PDU קודם ידלוף פנימה."""
    now = _float_or_none(msg.get("t")) or time.time()
    entry = ctx["data_ctx"].get(phys_lcn)
    fresh = entry is None or now - entry[0] > DATA_CTX_SEC
    if msg.get("type") == "ip_data" and msg.get("role") == "src":
        fresh = True     # תחילת PDU חדש
    data = {} if fresh else dict(entry[1])
    if msg.get("type") == "ip_data":
        rid = _int_or_none(msg.get("rid"))
        if msg.get("role") == "dst":
            data["tgt"] = rid
        else:
            data["src"] = rid
        for key in ("kind", "port"):
            if msg.get(key) is not None:
                data[key] = msg[key]
    else:
        for key in _DATA_CTX_FIELDS:
            if msg.get(key) is not None:
                data[key] = msg[key]
    ctx["data_ctx"][phys_lcn] = (now, data)
    if len(ctx["data_ctx"]) > 2 * MULTI_CHANNELS_MAX:
        cutoff = now - DATA_CTX_SEC
        for key in [k for k, (t0, _) in ctx["data_ctx"].items() if t0 < cutoff]:
            del ctx["data_ctx"][key]


def _data_ctx_take(ctx, phys_lcn, now=None):
    """ההקשר הפעיל לערוץ, או {} אם אין/פג. לא מוחק — PDU יחיד יכול לייצר גם
    מיקום וגם שדות-לוואי, וכולם שייכים לאותו הקשר."""
    entry = ctx["data_ctx"].get(phys_lcn)
    if entry is None:
        return {}
    now = now if now is not None else time.time()
    if now - entry[0] > DATA_CTX_SEC:
        return {}
    return dict(entry[1])


def _archive_record(rec):
    """עותק לכתיבה לדיסק, בלי מפתחות-העבודה הפנימיים."""
    return {k: v for k, v in rec.items() if k not in _LISTENER_INTERNAL_KEYS}


def _close_stale_calls(ctx, now=None, force=False):
    """כותב ל-dmr.jsonl שיחות שכבר לא ישתנו (עבר CALL_CLOSE_SEC מהפריים
    האחרון), עם dur/frames/encrypted/id הסופיים. force=True סוגר את הכל
    (כיבוי נקי/בדיקות). מחזיר את מספר הרשומות שנכתבו.
    הסנפשוט נלקח תחת _dmr_lock (הרשומה חיה גם ב-_dmr_msgs) והכתיבה לדיסק
    נעשית **מחוץ** לנעילה, כדי שקורא-API לא ייחסם על I/O."""
    now = now if now is not None else time.time()
    pending = ctx["pending"]
    with _dmr_lock:
        due = [k for k, (ts, _) in pending.items() if force or now - ts >= CALL_CLOSE_SEC]
        records = [_archive_record(pending.pop(k)[1]) for k in due]
    for rec in records:
        _append_dmr_log(rec)
    return len(records)


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


_CARD_EVENT_TYPES = frozenset({"voice_call", "data_header", "lrrp_position",
                               "lrrp_request", "text_message",
                               # ip_data מגיע לכאן **רק** מהענף של
                               # _DATA_KINDS_NO_PAYLOAD ב-_handle_datagram
                               # (ARS/טלמטריה/וכו'); שאר ה-PDU-ים יוצאים
                               # מוקדם ומקבלים כרטיס מה-payload שלהם.
                               "ip_data"})
# ★ v0.14.0 — פורט ה-UDP של PDU-דאטה => סוג הכרטיס. הפורט מגיע מהשידור
# (dsd_pty.DATA_PORT_KINDS, מאומת על 4001 בקליטה שלנו), לא מניחוש תוכן.
_DATA_KIND_CALL_TYPES = {"lrrp": "lrrp", "text": "sms", "ars": "reg",
                         "cellocator": "lrrp"}
# תג-ההצפנה הגנרי. DSD-FME לא הדפיס ALG/KEY בקליטה שנבדקה => alg/key_id
# נשארים None (CLAUDE.md §8). מקור אחד לשני המסלולים: דגל ה-SO על שורת
# השיחה (_normalize_dsd) וקורלציית ה-Protected LC (_dmr_listener).
ENC_TAG = {"alg": None, "alg_name": "מוצפן", "key_id": None}


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
    בקליטה אמיתית שנבדקה (אין "לרמות" למספר — ר' CLAUDE.md §8).

    ★ Phase 2 (multi mode): dsd_pty._run_multi מתייג כל אירוע עם phys_lcn/
    phys_freq_hz — זהות-ערוץ ודאית (נקבעת בזמן spawn, לא ניחוש מטקסט DSD-FME)
    כי dsd_pty הוא היחיד שיודע בוודאות לאיזה תהליך dsd-fme (מחובר ל-audio
    port_i, שהוסט ל-freq_hz_i) שייכת שורה נתונה. כשקיים — הוא **מחליף** את
    _channelmap_freq(lcn) (חיפוש-ניחוש לפי Rest-LSN, רלוונטי רק ל-trunking
    חד-ערוצי) ולא רק משלים אותו. בחד-ערוצי (dmr/scan) phys_lcn/phys_freq_hz
    תמיד None => אותה התנהגות בדיוק כמו לפני Phase 2."""
    if not isinstance(m, dict) or m.get("type") not in _CARD_EVENT_TYPES:
        return None
    typ = m["type"]
    t = _float_or_none(m.get("t")) or time.time()

    slot = _int_or_none(m.get("slot"))
    tg = _int_or_none(m.get("tg"))
    src = _int_or_none(m.get("src"))
    tgt = _int_or_none(m.get("tgt"))
    lcn = _int_or_none(m.get("lcn"))
    phys_lcn = _int_or_none(m.get("phys_lcn"))
    phys_freq_hz = _int_or_none(m.get("phys_freq_hz"))
    if phys_freq_hz is not None:
        freq = round(phys_freq_hz / 1e6, 6)
        card_lcn = phys_lcn if phys_lcn is not None else lcn
    else:
        freq = _channelmap_freq(lcn)
        card_lcn = lcn
    # סוג הכרטיס: מה-payload עצמו אם הוא הצהיר, אחרת מפורט ה-UDP של ה-PDU
    # (שכבת ה-Data, v0.14.0) — שני המקורות מהשידור, אף אחד לא ניחוש.
    ct = str(m.get("call_type") or "").strip().lower()
    if not ct:
        ct = _DATA_KIND_CALL_TYPES.get(m.get("kind"), "data")
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
        "freq": freq, "slot": slot, "cc": None, "lcn": card_lcn,
        "phys_lcn": phys_lcn,   # None בחד-ערוצי; ערוץ-אמת ב-multi (dedup/RF פר-ערוץ)
        "tg": tg, "tg_alias": aliasdb.tg_name(tg),
        "src": src, "src_alias": aliasdb.rid_name(src),
        "tgt": tgt, "tgt_alias": aliasdb.rid_name(tgt),
        "call_type": ct, "category": category, "group": group,
        # ★ v0.13.0 — דגלי Service Option מ-dsd_pty.parse_voice_flags. `encrypted`
        # מגיע עכשיו מהמקור המדויק (SO bit) כשהוא נוכח; קורלציית ה-Protected LC
        # ב-_dmr_listener נשארת כ-fallback ויכולה רק להדליק, לא לכבות.
        "emergency": bool(m.get("emergency")),
        "priority": _int_or_none(m.get("priority")),
        "flags": list(m.get("flags") or ()) or None,
        "encrypted": bool(m.get("encrypted")), "enc": None,
        "ber": None, "level": None,        # DSD-FME לא מדפיס — אף פעם לא ממציאים
        "watchlist": watchlist.match(tg, src, tgt),   # None אם אין התאמה
        "dur": None, "event": typ,
        "lat": round(lat, 5) if lat is not None else None,
        "lon": round(lon, 5) if lon is not None else None,
        "text": (str(m.get("text"))[:500] if m.get("text") and CAPTURE_TEXT else None),
        "wav": None,
        "delivery": m.get("delivery"),   # אופציונלי (data_header בלבד)
        # ★ v0.14.0 שכבת ה-Data — כולם None אלא אם השידור נשא אותם בפועל.
        "data_kind": m.get("kind"),      # lrrp/text/ars/telemetry/... מפורט ה-UDP
        "data_port": _int_or_none(m.get("port")),
        "fix_time": m.get("fix_time"),
        "speed_kmh": _float_or_none(m.get("speed_kmh")),
        "track_deg": _int_or_none(m.get("track_deg")),
        "alt_m": _int_or_none(m.get("alt_m")),
        "radius_m": _int_or_none(m.get("radius_m")),
    }
    if card["encrypted"]:
        card["enc"] = dict(ENC_TAG)
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
_rf_ticks = collections.deque()   # (t, phys_lcn, error_type) — נגזם לחלון RF_WINDOW_SEC


def _rf_quality_tick(error_type, phys_lcn=None):
    """phys_lcn=None בחד-ערוצי (dmr/scan) — כמו לפני Phase 2. ב-multi mode
    dsd_pty מתייג כל אירוע UDP עם phys_lcn אמיתי (ר' _dmr_listener)."""
    now = time.time()
    with _rf_lock:
        _rf_ticks.append((now, phys_lcn, error_type))
        cutoff = now - RF_WINDOW_SEC
        while _rf_ticks and _rf_ticks[0][0] < cutoff:
            _rf_ticks.popleft()


def _rf_quality_snapshot(phys_lcn=None):
    """תדירות שגיאות CRC/FEC אמיתית ב-RF_WINDOW_SEC האחרונות. פונקציה טהורה
    (קוראת מהחלון הנגלל בלבד) => נבדקת בלי חומרה. phys_lcn=None (ברירת
    מחדל) => צובר גלובלי על פני כל הטיקים (חד-ערוצי: תמיד None => זהה
    להתנהגות המקורית). phys_lcn=<int> => מסונן לערוץ בודד (multi mode)."""
    now = time.time()
    with _rf_lock:
        cutoff = now - RF_WINDOW_SEC
        while _rf_ticks and _rf_ticks[0][0] < cutoff:
            _rf_ticks.popleft()
        ticks = list(_rf_ticks)
    if phys_lcn is not None:
        ticks = [t for t in ticks if t[1] == phys_lcn]
    by_type = collections.Counter(t[2] for t in ticks)
    total = len(ticks)
    return {"window_sec": RF_WINDOW_SEC, "total_errors": total,
            "errors_per_min": round(total * 60.0 / RF_WINDOW_SEC, 1),
            "by_type": [{"error_type": k, "count": v} for k, v in by_type.most_common()]}


_channel_status_lock = threading.Lock()
_channel_status: dict = {}   # phys_lcn -> {"status": "restarting"|"down", "restart_count": int, "t": float}


def _channel_status_tick(msg):
    """דוגרן חי מ-dsd_pty._run_multi (Phase 7 partial-restart): מפענח בודד
    שקרס וקם-מחדש (או ויתר עליו אחרי שחרג ממכסת-restart), בלי להפיל שאר
    הערוצים. **לעולם לא נבנה בשקט** — מדווח כאן כדי ש-/api/rf יציג אותו,
    ולא ייעלם סתם מ-by_channel (CLAUDE.md: לעולם לא מסתירים מדד)."""
    lcn = _int_or_none(msg.get("phys_lcn"))
    if lcn is None:
        return
    with _channel_status_lock:
        _channel_status[lcn] = {
            "status": msg.get("status") or "unknown",
            "restart_count": _int_or_none(msg.get("restart_count")) or 0,
            "t": msg.get("t") or time.time(),
        }


def _rf_quality_by_channel():
    """פירוט איכות-RF פר-ערוץ (multi mode, Phase 2/7). ריק בחד-ערוצי — שם כל
    הטיקים נושאים phys_lcn=None ואף ערוץ לא נספר כאן (הם כלולים בצובר
    הגלובלי של _rf_quality_snapshot()/None, לא כפולים). מוסיף status/
    restart_count לערוץ שדיווח אי-פעם decoder_status (Phase 7 partial-restart)
    — כך שערוץ שקרס ולא מייצר עוד טיקים עדיין מופיע (לא נעלם בשקט)."""
    with _rf_lock:
        now = time.time()
        cutoff = now - RF_WINDOW_SEC
        while _rf_ticks and _rf_ticks[0][0] < cutoff:
            _rf_ticks.popleft()
        channels = {t[1] for t in _rf_ticks if t[1] is not None}
    with _channel_status_lock:
        channels |= set(_channel_status.keys())
        status_snapshot = dict(_channel_status)
    out = []
    for lcn in sorted(channels):
        row = {"phys_lcn": lcn, **_rf_quality_snapshot(lcn)}
        st = status_snapshot.get(lcn)
        if st:
            row["status"] = st["status"]
            row["restart_count"] = st["restart_count"]
        out.append(row)
    return out


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
    חלון 15ש' — best-effort; אם אין שיחה פתוחה מתאימה, מדולג בשקט).

    ★ Phase 2 (multi mode): מפתחות ה-dedup/_slot_open_call/RF מורחבים עם ממד
    phys_lcn (תמיד None בחד-ערוצי => מתנוון בדיוק לאותה התנהגות כמו לפני
    Phase 2). בלי זה, שתי שיחות בו-זמנית על שני ערוצים שונים עם אותו slot
    (1/2 — יש רק 2 אפשריים לכל ערוץ) היו מתמזגות/מקבלות תג-הצפנה בטעות
    מערוץ אחר — לא רק קוסמטי, שחיתות-נתונים בין-ערוצית אמיתית."""
    global _listener_bound
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((DMR_UDP_HOST, DMR_UDP_PORT))
    except OSError:
        log.warning("DMR listener: port %d busy - /api/dmr יחזיר ריק", DMR_UDP_PORT)
        _listener_bound = False
        return
    _listener_bound = True
    ctx = _reset_listener_ctx()
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

        # ★ v0.13.0 — הלופ הזה חייב לשרוד **כל** דאטהגרם. עד כה ענפי
        # רדאר-המערכת לא היו עטופים, ו-`{"type":"site_info","site":"abc"}` בודד
        # (int() על טקסט) הרג את ה-thread לתמיד — בשקט, בזמן ש-/api/health
        # ממשיך לדווח ok=True. הטיפול בדאטהגרם עבר לפונקציה נפרדת שגם עטופה
        # וגם נבדקת ישירות ב-CI (השכבה ה-stateful הזאת לא הייתה בדיקה לפני).
        _feed_tick(msg)
        try:
            _handle_datagram(msg, ctx)
        except Exception:
            with _feed_lock:
                _feed_stats["handler_errors"] += 1
            log.exception("DMR listener: דאטהגרם הפיל את הטיפול — מדולג, ה-thread ממשיך")
        # סגירת שיחות מְמוּתָנת: `lsn_status` לבדו הוא ~חצי מהפלט האמיתי, ואין
        # סיבה לתפוס את _dmr_lock (שגם קוראי-API מחזיקים) על כל דאטהגרם.
        # ה-watcher התקופתי מכסה את המקרה שבו התעבורה נעצרת לגמרי.
        now = time.time()
        if now - ctx["last_sweep"] >= CLOSE_SWEEP_INTERVAL_SEC:
            ctx["last_sweep"] = now
            try:
                _close_stale_calls(ctx, now=now)
            except Exception:
                log.exception("DMR listener: סגירת שיחות נכשלה")


def _handle_datagram(msg, ctx):
    """טיפול בדאטהגרם בודד מ-dsd_pty. הופרד מ-_dmr_listener כדי שיהיה גם עטוף
    (כל חריגה נבלמת ב-listener) וגם נבדק-ישירות ב-CI. ctx מחזיק את המצב
    הנגלל: dedup, slot_open (קורלציית הצפנה), pending (שיחות פתוחות שטרם
    נכתבו לדיסק) ו-seen."""
    global _dmr_seq
    _dedup = ctx["dedup"]
    _slot_open_call = ctx["slot_open"]

    if _discover_active:   # side-tap: מזין את צובר הגילוי (כולל sync/channel_status)
        _discover_collect(msg)

    mtype = msg.get("type")
    msg_phys_lcn = _int_or_none(msg.get("phys_lcn"))
    if mtype == "decoder_status":   # Phase 7 partial-restart: מפענח בודד קם-מחדש/ויתרנו עליו
        _channel_status_tick(msg)
        return
    if mtype == "quality":
        _rf_quality_tick(msg.get("error_type") or "UNKNOWN", phys_lcn=msg_phys_lcn)
        cc = _int_or_none(msg.get("cc"))
        sid = _intel_system_id()
        if cc is not None and sid:
            system_intel.record_cc(sid, cc, _active_color_code, t=msg.get("t"))
        return
    # --- Phase 8: system radar (control-channel telemetry, always-on --
    # never cards, only enriches system_intel; §5 "multi"/§10). Filtered
    # to real user systems only (_intel_system_id() is None during
    # standby/discovery-probe __probe__/__sweep__).
    # ★ v0.12.0: phys_freq_hz (multi mode, מוחתם ע"י dsd_pty.tag_event) מועבר
    # הלאה במקום להיזרק. טלמטריית CSBK מגיעה רק מהערוץ הפיזי שנושא את
    # ה-Rest LSN => (rest_lsn, phys_freq_hz) הוא ground-truth למיפוי
    # LSN↔תדר. ר' system_intel.record_rest_channel. בחד-ערוצי זה None ⇒
    # אין שינוי התנהגות.
    msg_phys_freq_hz = _int_or_none(msg.get("phys_freq_hz"))
    if mtype == "lsn_status":
        sid = _intel_system_id()
        if sid:
            system_intel.record_lsn_status(sid, msg.get("channels") or {},
                                           t=msg.get("t"),
                                           phys_freq_hz=msg_phys_freq_hz)
        return
    if mtype == "site_info":
        sid = _intel_system_id()
        if sid:
            system_intel.record_site(sid, msg.get("site"), t=msg.get("t"))
            system_intel.record_rest_channel(sid, msg.get("rest_lsn"),
                                             msg_phys_freq_hz, t=msg.get("t"))
        return
    if mtype == "preamble_csbk":
        sid = _intel_system_id()
        if sid:
            system_intel.record_private_call(
                sid, src=msg.get("src"), tgt=msg.get("tgt"), kind=msg.get("kind"),
                rest_lsn=msg.get("rest_lsn"), t=msg.get("t"))
            system_intel.record_rest_channel(sid, msg.get("rest_lsn"),
                                             msg_phys_freq_hz, t=msg.get("t"))
        return
    if mtype == "bank_call":
        sid = _intel_system_id()
        if sid:
            for entry in (msg.get("entries") or []):
                system_intel.record_private_call(
                    sid, src=None, tgt=entry.get("tgt"), kind="bank",
                    rest_lsn=entry.get("lsn"), t=msg.get("t"))
        return
    if mtype == "encryption":
        entry = _slot_open_call.get((msg_phys_lcn, msg.get("slot")))
        ts = msg.get("t") or time.time()
        if entry is not None and ts - entry[0] < 15:
            with _dmr_lock:   # open_rec חי גם ב-_dmr_msgs => מוטציה תחת הנעילה
                entry[1]["encrypted"] = True
                entry[1]["enc"] = entry[1].get("enc") or dict(ENC_TAG)
        return

    # --- ★ v0.14.0 שכבת ה-Data: הקשר PDU (RID + פורט) ואז ה-payload ---------
    if mtype == "ip_data":
        _data_ctx_update(ctx, msg_phys_lcn, msg)
        # רוב סוגי ה-PDU (ARS/טלמטריה/OTAP/סוללה/משימות/XCMP) לא מדפיסים אחר-כך
        # שום שורה שאנחנו מפרסרים ⇒ שורת ה-DST היא כל מה שנדע עליהם אי-פעם,
        # והיא בכל זאת אירוע אמיתי (מי→מי, איזה שירות). ל-lrrp/text יש payload
        # שייצר את הכרטיס, ולכן הם **לא** מייצרים כרטיס כאן (אחרת כפול).
        if msg.get("role") == "dst" and msg.get("kind") in _DATA_KINDS_NO_PAYLOAD:
            msg = {**_data_ctx_take(ctx, msg_phys_lcn,
                                    now=_float_or_none(msg.get("t"))),
                   "type": "ip_data", "kind": msg.get("kind"),
                   "port": msg.get("port"), "t": msg.get("t")}
        else:
            return
    elif mtype == "lrrp_extra":
        # אחרי הכרטיס => מוטציה עליו; לפניו (Time:) => הקשר לכרטיס שיבוא.
        if not _apply_lrrp_extra(ctx, msg_phys_lcn, msg):
            _data_ctx_update(ctx, msg_phys_lcn, msg)
        return
    if mtype in ("lrrp_position", "text_message"):
        # מעשירים את האירוע מההקשר לפני הנרמול, כדי ש-_normalize_dsd יישאר
        # פונקציה טהורה של dict בודד (אפשר לבדוק אותה ישירות).
        msg = {**_data_ctx_take(ctx, msg_phys_lcn, now=_float_or_none(msg.get("t"))),
               **msg}

    if mtype == "voice_miss":
        # שורה שנראתה כמו שיחה ולא נתפסה ע"י ה-regex (ר' dsd_pty).
        # לא כרטיס — מונה גלוי ב-/api/rf, כדי שהמקרה לא ייעלם בשקט שוב.
        with _feed_lock:
            _feed_stats["voice_miss"] += 1
            _feed_stats["voice_miss_last"] = msg.get("text")
        log.warning("DMR: שורת-שיחה לא נתפסה ע\"י הפרסר: %s", msg.get("text"))
        return

    try:
        rec = _normalize_dsd(msg)
    except Exception:
        log.exception("DMR: נרמול נכשל על דאטהגרם — מדולג")
        return
    if mtype == "voice_call" and msg.get("crc_err"):
        _rf_quality_tick("VOICE_CRC", phys_lcn=msg_phys_lcn)   # פריים קול שנכשל => גם מד ה-RF, גם (אם יש) הכרטיס
    if rec is None:
        return

    # dedup: אותה שיחה (ערוץ+tg+src+slot) בתוך 8ש' => עדכון הכרטיס הקיים
    # (משך/wav), לא כרטיס חדש. שיחות voice ב-DMR משדרות מסגרות רבות.
    key = (rec.get("phys_lcn"), rec.get("tg"), rec.get("src"), rec.get("slot"), rec.get("call_type"))
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
            if key in ctx["pending"]:   # חלון הסגירה נמדד מהפריים האחרון, לא הראשון
                ctx["pending"][key] = (ts, prev_rec)
            if rec.get("slot") is not None:
                _slot_open_call[(rec.get("phys_lcn"), rec["slot"])] = (ts, prev_rec)
            return
        rec["_start"] = ts
        _dedup[key] = (ts, rec)
        if len(_dedup) > 500:
            cutoff = ts - 30
            for k in [k for k, (t0, _) in _dedup.items() if t0 < cutoff]:
                del _dedup[k]

    with _dmr_lock:
        _dmr_seq += 1
        rec["id"] = _dmr_seq
        _dmr_msgs.append(rec)
    # ★ v0.13.0 — כתיבה לדיסק **בסגירת השיחה**, לא בפריים הראשון.
    # קודם נכתב כאן מיד, ולכן dur/frames/encrypted/id — שכולם נקבעים אחר-כך
    # כמוטציה על האובייקט החי — לא הגיעו לארכיון **אף פעם**: `?day=` דיווח
    # airtime 0 ו-0% מוצפן לנצח, וה-CSV ייצא עמודות ריקות בסתירה למסך.
    # שיחות voice נכנסות ל-pending ונכתבות ב-_close_stale_calls; שאר הכרטיסים
    # (data/lrrp) אינם עוברים dedup ואינם מקבלים תג-הצפנה => נכתבים מיד.
    if is_voice:
        ctx["pending"][key] = (ts, rec)
    elif mtype == "lrrp_position":
        # ★ v0.14.0 — כרטיס מיקום עוד עשוי לקבל מהירות/כיוון/גובה/רדיוס
        # בשורות שאחרי ה-Lat/Lon, אז אסור לכתוב אותו מיד; אחרת חוזר בדיוק
        # הבאג של v0.13.0 (הארכיון קופא לפני שהמידע הצטבר).
        ctx["pending"][("pos", rec["id"])] = (ts, rec)
        ctx["pos_open"][rec.get("phys_lcn")] = (ts, rec)
    else:
        _append_dmr_log(_archive_record(rec))
    if is_voice and rec.get("slot") is not None:
        _slot_open_call[(rec.get("phys_lcn"), rec["slot"])] = (ts, rec)
        if len(_slot_open_call) > 2 * MULTI_CHANNELS_MAX:   # עד 2 slots × N ערוצים ב-multi
            cutoff = ts - 15
            for k in [k for k, (t0, _) in _slot_open_call.items() if t0 < cutoff]:
                del _slot_open_call[k]
    ctx["seen"] += 1
    if ctx["seen"] % 200 == 0:
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


# --- מצב גילוי (discover): סריקת ספקטרום + בדיקת מועמדים -------------------
# job חולף בזיכרון (לא מַתמיד ב-state, לא משוחזר ב-boot). שלב1: rsp_fm במצב sweep;
# app.py צועד את הגריד דרך rigctl F וקורא SPECTRUM (בלי TUNE_LOCK). שלב2: בודק כל
# מועמד ב-DSD-FME (per-step TUNE_LOCK כמו scan). ר' CLAUDE.md §2 ו-discovery.py.
_discover_lock = threading.Lock()
_discover_thread = None
_discover_thread_stop = None
_discover_active = False        # דגל: api_state/health מדווחים discover; ה-listener מזין collector
_discover_status = {"stage": "idle", "progress": 0.0, "current_mhz": None,
                    "candidates": 0, "probed": 0, "results": []}
_discover_report = None         # דוח אחרון (נשמר גם ל-DISCOVERY_PATH)
_discover_acc_lock = threading.Lock()
_discover_epoch = 0
_discover_acc = []              # [(epoch, msg)] — אירועי UDP גולמיים בזמן בדיקת מועמד


def _discover_collect(msg):
    """side-tap טהור מ-_dmr_listener: כשגילוי פעיל, צובר את ה-msg הגולמי עם ה-epoch
    הנוכחי (מזהה מועמד) — פריים מאחר לא ישויך למועמד הבא. *לא* נוגע ב-dedup/slot."""
    with _discover_acc_lock:
        _discover_acc.append((_discover_epoch, msg))
        if len(_discover_acc) > 5000:
            del _discover_acc[:len(_discover_acc) - 5000]


def _discover_begin_probe():
    """מתחיל epoch חדש לבדיקת מועמד ומנקה את הצובר. מחזיר את מספר ה-epoch."""
    global _discover_epoch
    with _discover_acc_lock:
        _discover_epoch += 1
        _discover_acc.clear()
        return _discover_epoch


def _discover_take_events(epoch):
    with _discover_acc_lock:
        return [m for (e, m) in _discover_acc if e == epoch]


def _discovery_status_snapshot():
    with _discover_lock:
        return dict(_discover_status)


def _probe_system(freq_mhz):
    """מערכת חולפת (לא נשמרת) לבדיקת מועמד: non-trunk (trunk+map ריק מקריס את
    המפקח), בלי הקלטה, עם emit_status (אירועי sync/channel_status לזיהוי)."""
    return {"id": "__probe__", "name": "probe", "control": round(float(freq_mhz), 6),
            "color_code": 0, "channelmap": [], "trunk": 0, "no_wav": True,
            "emit_status": True}


def _sweep_system(plan):
    """מערכת חולפת לקונפיג-סריקה: sweep=1, קצב IQ רחב, מרכז התחלתי = תחילת הטווח."""
    return {"id": "__sweep__", "name": "sweep", "control": round(plan["start_mhz"], 6),
            "color_code": 0, "channelmap": [], "sweep": True,
            "iq_rate": plan["iq_rate"], "nfft": plan["nfft"],
            "gain_index": plan["gain_index"], "no_wav": True}


def _rigctl_command(command, timeout=3.0):  # pragma: no cover - hardware runtime
    """שולח פקודת rigctl אחת ל-rsp_fm וקורא שורת תגובה (F לכיוונון, SPECTRUM לספקטרום)."""
    host, _sep, port = DMR_BRIDGE_RIGCTL.rpartition(":")
    with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((command + "\n").encode("ascii"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", "replace").strip()


def _sweep_read(center_hz):  # pragma: no cover - hardware runtime
    """מכוונן את הסורק למרכז נתון (rigctl F) וקורא את הספקטרום הממוצע (SPECTRUM),
    ממתין להתייצבות. מחזיר snapshot {center_hz, bin_hz, power_db} או None."""
    try:
        _rigctl_command(f"F {int(center_hz)}")
    except OSError:
        return None
    for _ in range(DISCOVERY_SPECTRUM_TRIES):
        time.sleep(DISCOVERY_SETTLE_SEC)
        try:
            snap = json.loads(_rigctl_command("SPECTRUM"))
        except (OSError, ValueError):
            continue
        if snap.get("power_db") and abs(int(snap.get("center_hz", 0)) - int(center_hz)) < 2000:
            return snap
    return None


def _save_discovery_report(report):
    global _discover_report
    _discover_report = report
    try:
        _atomic_write(DISCOVERY_PATH, json.dumps(report, ensure_ascii=False))
    except Exception:
        log.exception("discovery report save")


def _load_discovery_report():
    global _discover_report
    if _discover_report is not None:
        return _discover_report
    try:
        _discover_report = json.loads(DISCOVERY_PATH.read_text())
    except Exception:
        _discover_report = None
    return _discover_report


def _probe_candidate(stop_evt, cand, plan):
    """בודק מועמד יחיד: retune ל-DSD-FME (per-step TUNE_LOCK), dwell עם איסוף
    אירועי UDP דרך ה-collector, סיכום ל-aggregate_probe. מחזיר רשומה או None."""
    freq_mhz = cand["freq_mhz"]
    if not TUNE_LOCK.acquire(timeout=5):
        return None
    epoch = None
    try:
        err, _detail = _enter_dmr(_probe_system(freq_mhz))
        if err:
            log.warning("discover: probe %.4f MHz failed to tune: %s", freq_mhz, err)
            return None
        epoch = _discover_begin_probe()
    finally:
        TUNE_LOCK.release()
    if epoch is None or stop_evt.is_set():
        return None
    waited = 0.0
    while waited < plan["probe_sec"] and not stop_evt.is_set():
        time.sleep(0.25)
        waited += 0.25
    events = _discover_take_events(epoch)
    return discmod.aggregate_probe(freq_mhz, events, min_sync=plan["min_sync"])


def _finish_discovery(plan, candidates, results):
    """סוגר ריצת גילוי: שומר דוח, משחרר את ה-SDR (standby), state=off."""
    report = {"t": time.time(), "plan": plan, "candidate_count": len(candidates),
              "candidates": candidates,
              "networks": [r for r in results if r.get("is_dmr")], "results": results}
    _save_discovery_report(report)
    if TUNE_LOCK.acquire(timeout=10):
        try:
            _enter_standby()
        finally:
            TUNE_LOCK.release()
    save_state({**load_state(), "app_mode": "off", "prev_mode": "discover"})
    with _discover_lock:
        _discover_status.update(stage="done", current_mhz=None, progress=1.0,
                                results=results)


def _discover_loop(stop_evt, plan):
    """thread הגילוי. שלב1: סריקת ספקטרום על הגריד (בלי TUNE_LOCK, בודק stop_evt
    תדיר). שלב2: בדיקת כל מועמד (per-step TUNE_LOCK). בסיום: דוח + standby + off.
    הלולאה עצמה נבדקת e2e (עם `_sweep_read`/`_probe_candidate` ממוקפים); רק הקריאות
    לחומרה (`_rigctl_command`/`_sweep_read`) הן no-cover."""
    global _discover_active
    try:
        grid = discmod.build_freq_grid(plan["start_mhz"], plan["end_mhz"], plan["iq_rate"])
        snapshots = []
        for i, center in enumerate(grid):
            if stop_evt.is_set():
                return
            snap = _sweep_read(center)
            if snap:
                snapshots.append(snap)
            with _discover_lock:
                _discover_status.update(stage="sweep",
                                        progress=round((i + 1) / (len(grid) + 1), 3),
                                        current_mhz=round(center / 1e6, 4))
        if stop_evt.is_set():
            return
        candidates = discmod.detect_candidates(
            snapshots, threshold_mad=plan["threshold_mad"],
            max_candidates=plan["max_candidates"])
        with _discover_lock:
            _discover_status.update(stage="probe", candidates=len(candidates),
                                    current_mhz=None, progress=0.0)
        results = []
        for i, cand in enumerate(candidates):
            if stop_evt.is_set():
                return
            record = _probe_candidate(stop_evt, cand, plan)
            if record is not None:
                record["power_db"] = cand.get("power_db")
                results.append(record)
            with _discover_lock:
                _discover_status.update(
                    progress=round((i + 1) / (len(candidates) or 1), 3),
                    probed=i + 1, current_mhz=cand["freq_mhz"], results=list(results))
        if stop_evt.is_set():
            return
        _finish_discovery(plan, candidates, results)
    except Exception:
        log.exception("discover loop crashed")
        if TUNE_LOCK.acquire(timeout=5):
            try:
                _enter_standby()
            finally:
                TUNE_LOCK.release()
    finally:
        _discover_active = False


def _discover_activate(plan):
    """מפעיל גילוי: מרים קונפיג-סריקה (restart ל-sweep) ומתחיל thread. הקורא מחזיק
    TUNE_LOCK. מחזיר (error, detail)."""
    global _discover_thread, _discover_thread_stop, _discover_active
    err, detail = _enter_dmr(_sweep_system(plan))
    if err:
        return err, detail
    stop_evt = threading.Event()
    thread = threading.Thread(target=_discover_loop, args=(stop_evt, plan), daemon=True)
    with _discover_lock:
        _discover_active = True
        _discover_status.update(stage="sweep", progress=0.0, current_mhz=None,
                                candidates=0, probed=0, results=[],
                                started_at=time.time(), plan=plan)
        _discover_thread, _discover_thread_stop = thread, stop_evt
    thread.start()
    return None, None


def _discover_stop_thread():
    """עוצר את thread הגילוי (אם רץ) ומחכה שיסיים. אין-אופ אם לא רץ. הקורא
    (api_mode) מחזיק TUNE_LOCK — הצרכן ישוחרר מיד אחרי ע"י המעבר עצמו."""
    global _discover_thread, _discover_thread_stop, _discover_active
    with _discover_lock:
        thread, stop_evt = _discover_thread, _discover_thread_stop
        _discover_thread = _discover_thread_stop = None
        if stop_evt:
            stop_evt.set()
    if thread and thread.is_alive():
        thread.join(timeout=15)
    _discover_active = False
    with _discover_lock:
        if _discover_status.get("stage") not in ("idle", "done"):
            _discover_status.update(stage="idle", current_mhz=None)


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


UNKNOWN_MAX = 200


def _unknown_aliases(recs):
    """worklist של IDs שנצפו בתעבורה אך עדיין ללא שם — הדרך המהירה ביותר ממספר
    גולמי למשמעות. אוסף RID-ים (source + target) ו-TG-ים עם כמה/מתי נצפו; כל מה
    ש-aliasdb פותר **כרגע** (ייבוא או ידני) מוסלל החוצה, כך שמתן-שם מפיל את
    הרשומה מהתור בקריאה הבאה. ממוין לפי count יורד (קודם הפעילים — התשואה הגבוהה).
    טהור פרט לחיפוש-האליאס (aliasdb) => נבדק ישירות עם aliasdb מזורע."""
    rids, tgs = {}, {}
    for r in recs:
        t = r.get("t") or 0
        tg = r.get("tg")
        for rid in (r.get("src"), r.get("tgt")):
            if rid is None:
                continue
            s = rids.setdefault(int(rid), {"count": 0, "last_t": 0, "tgs": set()})
            s["count"] += 1
            s["last_t"] = max(s["last_t"], t)
            if tg is not None:
                s["tgs"].add(int(tg))
        if tg is not None:
            s = tgs.setdefault(int(tg), {"count": 0, "last_t": 0, "rids": set()})
            s["count"] += 1
            s["last_t"] = max(s["last_t"], t)
            if r.get("src") is not None:
                s["rids"].add(int(r["src"]))
    out = []
    for rid, s in rids.items():
        if aliasdb.rid_name(rid):
            continue
        out.append({"kind": "rid", "id": rid, "count": s["count"],
                    "last_t": s["last_t"] or None, "tgs": sorted(s["tgs"])[:8]})
    for tg, s in tgs.items():
        if aliasdb.tg_name(tg):
            continue
        out.append({"kind": "tg", "id": tg, "count": s["count"],
                    "last_t": s["last_t"] or None, "rid_count": len(s["rids"])})
    out.sort(key=lambda u: (u["count"], u["last_t"] or 0), reverse=True)
    return out[:UNKNOWN_MAX]


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


@app.route("/api/aliases/unknown")
def api_aliases_unknown():
    """תור IDs שנצפו בתעבורה אך עדיין ללא שם — worklist למתן שמות. אותם פרמטרים
    כמו האנליטיקה (?day=YYYY-MM-DD ארכיון | ?all=1 הכל | ברירת מחדל היום)."""
    recs, err = _analytics_source(request.args.get("day"),
                                  request.args.get("all") in ("1", "true", "yes"))
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, unknown=_unknown_aliases(recs))


@app.route("/api/positions")
def api_positions():
    """מיקום LRRP אחרון-ידוע פר-RID (Phase 3). ריק כשהרשת לא שולחת LRRP סטנדרטי
    (Motorola proprietary לא מפוענח ע"י DSD-FME — ר' CLAUDE.md §8)."""
    return jsonify(ok=True, positions=_lrrp_snapshot())


@app.route("/api/rf")
def api_rf():
    """איכות RF: תדירות שגיאות CRC/FEC אמיתית מ-DSD-FME (חלון RF_WINDOW_SEC).
    **אין dBFS/SNR** — נדחה במכוון (ר' CLAUDE.md §8: דורש פטצ' rsp_tcp).
    by_channel: פירוט פר-ערוץ ב-multi mode (Phase 2) — [] בחד-ערוצי."""
    st = load_state()
    feed = _feed_snapshot()
    return jsonify(ok=True, gain_nudge=int(st.get("gain_nudge", 0)),
                   by_channel=_rf_quality_by_channel(),
                   # ★ v0.13.0: שורות-שיחה שהפרסר לא תפס, ושגיאות-טיפול —
                   # שני מונים שקודם לא היו קיימים ולכן נפלו בשקט.
                   parser_miss=feed["voice_miss"],
                   parser_miss_last=feed["voice_miss_last"],
                   handler_errors=feed["handler_errors"],
                   **_rf_quality_snapshot())


@app.route("/api/system-intel")
def api_system_intel():
    """מודיעין-מערכת נצבר (Phase 8): אתרים (site_info), מפת-תפוסה-LSN חיה
    (lsn_status), CDR שיחות-יחיד (preamble_csbk/bank_call), סחיפת-CC. נצבר
    אוטומטית ב-_dmr_listener מתעבורה אמיתית — אין PUT (לא config נערך-ידנית,
    ר' CLAUDE.md §5/§8). `?system=<id>` לצפייה במערכת ספציפית (גם לא-פעילה
    כרגע); ברירת מחדל: המערכת הפעילה. ריק בשקט אם המערכת עוד לא נצפתה."""
    sid = request.args.get("system") or _active_system_id
    return jsonify(ok=True, system=sid, intel=system_intel.export_for(sid))


@app.route("/api/system-intel/apply-lsn", methods=["POST"])
def api_system_intel_apply_lsn():
    """★ אימוץ מיפוי ה-LSN שהתגלה (v0.12.0) כ-channelmap של המערכת.

    זו **הפעולה היחידה** שבה מודיעין-מערכת נוגע ב-systems.json, והיא יזומה-
    אנושית במפורש (POST דרך _guard, כפתור ב-UI) — system_intel עצמו לעולם לא
    כותב לקונפיג (CLAUDE.md §5). הערך המוסף: ה-lcn מפסיק להיות אינדקס-שרירותי
    (מספור לפי סדר-תדרים, כפי שהגיע מסקר-השדה) ונהיה מספר-הערוץ-הפיזי האמיתי
    שנגזר מה-LSN שנצפה בפועל — וזה מה שהופך את מצב הטראנקינג החד-ערוצי לנכון,
    כי render_channelmap(lsn_pairs=True) מרחיב אותו חזרה למפת-LSN אמיתית.

    שמרני במכוון: מחליף channelmap **רק** למערכת המבוקשת, ומסרב אם המיפוי
    ריק/סתור. שאר המערכות ושאר שדות המערכת נשארים כמו-שהם."""
    data = request.get_json(silent=True) or {}
    sid = data.get("system") or _active_system_id
    if not sid:
        return jsonify(ok=False, error="אין מערכת פעילה — ציין system"), 400
    channelmap = system_intel.lsn_map_to_channelmap(system_intel.lsn_map_for(sid))
    if not channelmap:
        return jsonify(ok=False, error="אין עדיין מיפוי LSN מוכרע למערכת הזו — "
                                      "הרץ multi על ערוץ-בקרה פעיל וחזור"), 400
    systems = load_systems()
    if not _find_system(systems, sid):
        return jsonify(ok=False, error=f"מערכת {sid} לא קיימת"), 404
    merged = [{**s, "channelmap": channelmap} if s.get("id") == sid else s
              for s in systems]
    ok, cleaned = _validate_systems(merged)
    if not ok:
        return jsonify(ok=False, error="המיפוי שהתגלה לא עבר ולידציה"), 400
    _atomic_write(SYSTEMS_PATH, json.dumps(cleaned, ensure_ascii=False))
    log.info("system-intel: channelmap של %s אומץ ממיפוי LSN (%d ערוצים, מ-%s)",
             sid, len(channelmap), request.remote_addr)
    return jsonify(ok=True, system=sid, channelmap=channelmap, systems=cleaned)


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
            recs = sorted(REC_DIR.rglob("*.wav"),
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
    (.txt) נמחק יחד עם ההקלטה. rglob (לא glob) => תופס גם הקלטות multi בתת-
    תיקיות פר-ערוץ (recordings/lcnN/), אחרת ה-retention לא רואה אותן והדיסק
    מתמלא בלי גבול (ר' CLAUDE.md §5 multi)."""
    try:
        recs = sorted(REC_DIR.rglob("*.wav"),
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
        recs = sorted(REC_DIR.rglob("*.wav"), key=lambda p: p.stat().st_mtime)
    except OSError:
        recs = []
    for p in recs:
        try:
            stat = p.stat()
        except OSError:
            continue
        ts = round(stat.st_mtime, 1)
        if ts > last_seen:
            # נתיב יחסי ל-REC_DIR: בחד-ערוצי זהה ל-p.name (קובץ שטוח); ב-multi
            # שומר את תת-התיקייה הפר-ערוצית (lcnN/foo.wav) כדי ש-/recordings
            # וקישור התמלול ימצאו את הקובץ.
            rows.append({"ts": ts, "file": str(p.relative_to(REC_DIR)),
                         "bytes": stat.st_size})
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
        try:
            # ★ v0.13.0: סגירת שיחות תלויות גם כשהתעבורה נעצרה. בלי טיק תקופתי,
            # השיחה האחרונה לפני דממה הייתה נשארת ב-pending ולא מגיעה לארכיון —
            # ה-listener מסיים סבב רק כשמגיע דאטהגרם נוסף.
            _close_stale_calls(_listener_ctx)
        except Exception:
            log.exception("סגירת שיחות תלויות נכשלה")
        time.sleep(WATCH_INTERVAL)


def _listener_watchdog():
    """★ v0.13.0 — חגורה-ובנוסף-שלייקס. אחרי ה-try/except ב-_dmr_listener מוות
    של ה-thread אמור להיות בלתי-אפשרי, אבל "אמור" הוא בדיוק מה שהיה נכון גם
    לפני שדאטהגרם אחד הרג אותו. אם ה-thread מת — מקימים אותו מחדש ומתעדים.
    ה-bind נכשל (פורט תפוס) הוא מצב אחר ולא מנסים אותו שוב בלופ."""
    global _listener_thread
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            if _listener_bound is False:
                continue     # הפורט תפוס — לא thread שמת; מדווח דרך /api/health
            thread = _listener_thread
            if thread is not None and not thread.is_alive():
                log.error("DMR listener מת — מקים מחדש")
                _listener_thread = threading.Thread(target=_dmr_listener, daemon=True)
                _listener_thread.start()
        except Exception:
            log.exception("listener watchdog")


def _intel_flush_watcher():
    """גיבוי לדיסק לכתיבת ה-debounce שב-system_intel.record_*: אם אירועים
    מפסיקים להגיע (מערכת נרגעת) בלי שיקרה עוד record_* שיפעיל flush, השינוי
    האחרון היה נשאר תקוע ב-dirty בזיכרון בלבד. טיק תקופתי מבטיח שמירה
    בסופו-של-דבר, בלי לכתוב על כל אירוע (ר' system_intel.FLUSH_MIN_INTERVAL_SEC)."""
    while True:
        try:
            system_intel.maybe_flush()
        except Exception:
            log.exception("system_intel flush watcher")
        time.sleep(system_intel.FLUSH_MIN_INTERVAL_SEC)


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
    if _discover_active:   # job חולף — לא מַתמיד ב-state; מדווח מהזיכרון בלבד
        st["app_mode"] = "discover"
        st["mode_ok"] = True
        st["discover"] = _discovery_status_snapshot()
        st.update(systems=load_systems(), version=VERSION,
                  alg_names={f"0x{k:02X}": v for k, v in DMR_ALG_NAMES.items()})
        return jsonify(st)
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


@app.route("/api/watchlist", methods=["GET", "PUT"])
def api_watchlist():
    """מעקב RID/TG להתראה מקומית (§8: לא Web Push — בלי שרת-relay חיצוני).
    GET מחזיר את הרשימה. PUT מחליף אותה במלואה (כמו /api/aliases)."""
    if request.method == "GET":
        return jsonify(ok=True, watchlist=watchlist.export_all())
    data = request.get_json(silent=True)
    ok, err = watchlist.replace(data)
    if not ok:
        return jsonify(ok=False, error=err), 400
    log.info("watchlist updated (from %s)", request.remote_addr)
    return jsonify(ok=True, watchlist=watchlist.export_all())


@app.route("/api/health")
def api_health():
    """סטטוס המערכת — מאפשר ל-UI להבדיל בין "אין תעבורה" ל"משהו נפל"."""
    if _discover_active:   # במהלך גילוי השירות פעיל (sweep/probe) — לא "dmr"
        with _dmr_lock:
            calls_today = len([m for m in _dmr_msgs if (m.get("t") or 0) >= _today_start()])
            last_call = max((m.get("t") or 0 for m in _dmr_msgs), default=0) or None
        return jsonify(ok=True, app_mode="discover", services={DMR_SERVICE: "active"},
                       sdr_present=_sdr_present(), calls_today=calls_today,
                       last_call_at=last_call, discover=_discovery_status_snapshot())
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
        # dmr/multi חולקות את אותה יחידת systemd (DMR_SERVICE) — systemctl
        # לבדו לא יכול להבדיל ביניהן, בדיוק כמו ב-_live_mode(). MODE_SERVICE
        # הוא מקור-האמת המשותף: "dmr" הוא רק ברירת-מחדל לשם לא-מוכר, לא
        # לקיצור-דרך שבולע multi (⚠ באג היה כאן: `mode = "dmr" if dmr_active
        # else saved` דרס כל multi בחזרה ל-"dmr" מיד כשהשירות פעיל).
        mode = (saved if saved in MODE_SERVICE else "dmr") if dmr_active else saved
        ok = dmr_active if mode in MODE_SERVICE else off_ok
    # מדדי פיד: מספר שיחות היום + זמן השיחה האחרונה (חי חושף "האם אני מפענח")
    with _dmr_lock:
        floor = _today_start()
        today = [m for m in _dmr_msgs if (m.get("t") or 0) >= floor]
        calls_today = len(today)
        last_call = max((m.get("t") or 0 for m in _dmr_msgs), default=0) or None
    feed = _feed_snapshot()
    return jsonify(ok=ok, app_mode=mode, services=services,
                   sdr_present=_sdr_present(), calls_today=calls_today,
                   last_call_at=last_call,
                   listener_alive=_listener_alive(), feed=feed,
                   decode_state=_decode_state(mode, feed))


DECODE_SILENT_SEC = 180.0   # בלי אף דאטהגרם כל-כך הרבה זמן במצב פעיל => "שקט"
DECODE_VOICE_SEC = 900.0    # שיחת-קול בטווח הזה => "מפענח" בוודאות


def _decode_state(mode, feed, now=None):
    """★ v0.13.0 — ההבחנה שלא הייתה קיימת: "הרשת שקטה" מול "השרשרת מתה".
    עד כה שלושת המצבים — רשת שקטה, listener מת, ושרשרת חיה-אך-חירשת — רונדרו
    בממשק **זהה** (פיל ירוק, "מאזין"), ו-errors_per_min=0 של מד ה-RF דו-משמעי
    בין הטוב ביותר לגרוע ביותר.

    פונקציה טהורה (mode + סנפשוט-פיד) => נבדקת ב-CI. שמרנית במכוון: מדווחת
    `silent` ולא "שבור", כי בחד-ערוצי non-trunk דממה מלאה היא מצב לגיטימי —
    אין לנו ראיה להאשים את השרשרת, ולא ממציאים אבחנה (§8)."""
    now = now if now is not None else time.time()
    if _listener_alive() is False:
        return "listener_down"
    if mode not in MODE_SERVICE:
        return "standby"
    last_voice = feed.get("last_voice_at")
    if last_voice and now - last_voice <= DECODE_VOICE_SEC:
        return "decoding"
    if feed.get("datagrams_window"):
        return "chain_alive"        # מגיעים אירועים (טלמטריה/איכות) אך לא קול
    last_dg = feed.get("last_datagram_at")
    if last_dg is None or now - last_dg > DECODE_SILENT_SEC:
        return "silent"             # אין שום נתון מהמפענח — דורש בדיקה
    return "chain_alive"


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
    """ייצוא שיחות DMR. ?format=csv (BOM) | json. ?day=YYYY-MM-DD => מסונן ליום
    הארכיון הזה בלבד (כמו /api/dmr) — בלי הפרמטר, כל ה-jsonl השמור (כברירת
    מחדל, זהה להתנהגות המקורית)."""
    recs = _read_dmr_log()
    day = request.args.get("day")
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return jsonify(ok=False, error="תאריך לא תקין (פורמט: YYYY-MM-DD)"), 400
        start, end = bounds
        recs = [r for r in recs if start <= (r.get("t") or 0) < end]
    return dsd_export.export_response(app, request, recs, DMR_EXPORT_COLS, "dmr")


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


@app.route("/recordings/<path:name>")
def recordings(name):
    # <path:> (לא <name>) => מגיש גם הקלטות multi בתת-תיקיות (lcnN/foo.wav).
    # send_from_directory מונע directory-traversal מעבר ל-REC_DIR.
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
    if mode not in ("dmr", "off", "scan", "discover", "multi"):
        return jsonify(ok=False, error="mode לא תקין (dmr/off/scan/discover/multi)"), 400

    # ולידציה סטטית קודם (לא תלוית-נעילה) — בקשה עם פרמטרים לא-תקינים (400) לא
    # נוגעת בסבב סריקה פעיל (מניעת "scan זומבי"). _scan_stop_thread נקרא רק אחרי
    # שתפסנו את TUNE_LOCK.
    st = load_state()
    plan = system = sweep_plan = None
    if mode == "scan":
        plan = _validate_scan_plan(data.get("plan") or st.get("scan_plan"))
        if plan is None:
            return jsonify(ok=False, error="לוח סריקה לא תקין (1-8 רגלים, "
                           "כל רגל מערכת+זמן שהייה תקין)", state=st), 400
    elif mode == "discover":
        sweep_plan = discmod.validate_sweep_plan(data.get("plan") or {})
        if sweep_plan is None:
            return jsonify(ok=False, error="טווח סריקה לא תקין (start<end, בתחום "
                           "24–1300MHz, רוחב עד 100MHz)", state=st), 400
    elif mode == "dmr":
        sid = data.get("system") or st.get("system")
        systems = load_systems()
        system = _find_system(systems, sid) if sid else (systems[0] if systems else None)
        if system is None:
            return jsonify(ok=False, error="לא נבחרה מערכת DMR תקינה", state=st), 400
    elif mode == "multi":
        sid = data.get("system") or st.get("system")
        systems = load_systems()
        system = _find_system(systems, sid) if sid else (systems[0] if systems else None)
        if system is None:
            return jsonify(ok=False, error="לא נבחרה מערכת DMR תקינה", state=st), 400
        ok, err = _validate_multi_feasible(system)
        if not ok:
            return jsonify(ok=False, error=err, state=st), 400

    if not TUNE_LOCK.acquire(timeout=0.5):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב",
                       state=load_state()), 409
    try:
        _scan_stop_thread()
        _discover_stop_thread()
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

        if mode == "discover":
            # ⚠ מצב חולף — לא נשמר ל-state (לא משוחזר ב-boot). ההתקדמות בזיכרון
            # בלבד דרך _discover_status; api_state/health מדווחים discover כל עוד
            # _discover_active. בסיום הריצה => standby + app_mode=off.
            log.info("mode -> DISCOVER %s-%s MHz (from %s)",
                     sweep_plan["start_mhz"], sweep_plan["end_mhz"], request.remote_addr)
            err, detail = _discover_activate(sweep_plan)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter discover")
                return jsonify(payload), status
            return jsonify(ok=True, app_mode="discover", plan=sweep_plan)

        if mode == "multi":
            log.info("mode -> MULTI system=%s (from %s)", system["id"], request.remote_addr)
            err, detail = _enter_dmr(system, multi=True)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter multi")
                return jsonify(payload), status
            new_state = {**load_state(), "app_mode": "multi", "system": system["id"]}
            save_state(new_state)
            return jsonify(ok=True, app_mode="multi", system=system["id"])

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


@app.route("/api/discover")
def api_discover():
    """סטטוס גילוי חי (שלב, התקדמות, תדר נוכחי, מועמדים/תוצאות) + הדוח האחרון."""
    with _discover_lock:
        status = dict(_discover_status)
        active = _discover_thread is not None and _discover_thread.is_alive()
    return jsonify(ok=True, active=active, status=status, report=_load_discovery_report())


@app.route("/api/discover/save", methods=["POST"])
def api_discover_save():
    """שומר רשת מגולה כמערכת DMR (מיזוג ל-systems.json דרך _validate_systems).
    בוחר רשומה לפי freq_mhz או index מהדוח. דרך _guard (POST)."""
    data = request.get_json(silent=True) or {}
    report = _load_discovery_report()
    if not report:
        return jsonify(ok=False, error="אין דוח גילוי זמין"), 400
    records = report.get("results") or []
    rec = None
    if data.get("freq_mhz") is not None:
        try:
            target = round(float(data["freq_mhz"]), 6)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            rec = next((r for r in records
                        if abs(float(r.get("freq_mhz", 0)) - target) < 1e-4), None)
    elif data.get("index") is not None:
        try:
            rec = records[int(data["index"])]
        except (TypeError, ValueError, IndexError):
            rec = None
    if rec is None:
        return jsonify(ok=False, error="רשומת גילוי לא נמצאה"), 400
    system = discmod.discovery_to_system(rec, name=data.get("name"))
    systems = [s for s in load_systems() if s["id"] != system["id"]] + [system]
    ok, cleaned = _validate_systems(systems)
    if not ok:
        return jsonify(ok=False, error="מערכת מגולה לא עברה ולידציה (אולי חריגה מ-30 מערכות)"), 400
    _atomic_write(SYSTEMS_PATH, json.dumps(cleaned, ensure_ascii=False))
    log.info("discovery: saved system %s (%.4f MHz, from %s)",
             system["id"], system["control"], request.remote_addr)
    return jsonify(ok=True, system=system, systems=cleaned)


# --- שחזור מצב באתחול: dmr-web הוא המתזמר -----------------------------------
BOOT_SDR_WAIT_SEC = 90


def _restore_intel_cache(st):
    """ממלא את מטמון-המודיעין (_active_system_id/_active_color_code) מ-state
    שמור, בלי לגעת בשרשרת האותות. נחוץ כשהמפענח כבר רץ ו-_enter_dmr לא
    ייקרא — אחרת ה-listener מסנן כל אירוע-מודיעין. פונקציה נפרדת (ולא שורה
    ב-_boot_restore) כדי שתהיה נבדקת ישירות."""
    global _active_system_id, _active_color_code
    mode = st.get("app_mode", "off")
    if mode not in MODE_SERVICE:
        return None
    system = _find_system(load_systems(), st.get("system"))
    if not system:
        return None
    _active_system_id = system.get("id")
    _active_color_code = system.get("color_code")
    log.info("boot-restore: מטמון-מודיעין שוחזר למערכת %s (מצב %s)",
             _active_system_id, mode)
    return _active_system_id


def _boot_restore():
    """אורקסטרציית אתחול: dmr-dsdfme אינו enabled ב-systemd — dmr-web (שעולה תמיד)
    קורא את state.json ומחזיר את המצב השמור (dmr/off/scan). רץ ב-thread daemon;
    כל כישלון => off + לוג, לעולם לא מפיל את שרת הווב."""
    try:
        st = load_state()
        mode = st.get("app_mode", "off")
        live = _live_mode()
        if live == mode:
            # ★ v0.13.0 — המצב הזה (dmr-dsdfme כבר רץ, למשל אחרי
            # `systemctl restart dmr-web` או Restart=always) יצא מכאן בלי
            # לאכלס את מטמון-המודיעין, ש-נקבע **רק** ב-_enter_dmr. התוצאה:
            # _intel_system_id() החזיר None לתמיד וכל v0.11.0+v0.12.0
            # (אתרים/LSN/CDR/CC/הצבעות-מיפוי) צבר אפס — בשקט מוחלט.
            _restore_intel_cache(st)
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
            elif mode == "multi":
                systems = load_systems()
                system = _find_system(systems, st.get("system")) or (systems[0] if systems else None)
                if system is None:
                    err, _detail = "אין מערכת DMR שמורה", None
                else:
                    ok, verr = _validate_multi_feasible(system)
                    err, _detail = (None, None) if ok else (verr, None)
                    if ok:
                        err, _detail = _enter_dmr(system, multi=True)
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
    watchlist.load()  # טעינת רשימת-המעקב (RID/TG להתראה מקומית) לזיכרון
    system_intel.load()  # טעינת מודיעין-המערכת הנצבר (אתרים/LSN/CDR/CC) לזיכרון
    threading.Thread(target=_boot_restore, daemon=True).start()
    REC_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    threading.Thread(target=_intel_flush_watcher, daemon=True).start()
    _load_dmr_history()                                            # היסטוריית היום שורדת restart (לפני ה-listener)
    _listener_thread = threading.Thread(target=_dmr_listener, daemon=True)
    _listener_thread.start()                                       # פיד UDP מ-dsd_pty (שקט ב-standby)
    threading.Thread(target=_listener_watchdog, daemon=True).start()
    if TRANSCRIBE:
        threading.Thread(target=_transcribe_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
