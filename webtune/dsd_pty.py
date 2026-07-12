#!/usr/bin/env python3
# ============================================================================
#  DMR  -  DSD-FME PTY harness (מתאם: טקסט → JSON UDP)
# ----------------------------------------------------------------------------
#  ExecStart של dmr-dsdfme.service. הבעיה: DSD-FME אינו API-first — הפלט שלו
#  טקסטואלי (ncurses/console) והשליטה בהקשות מקלדת. המודול הזה עוטף אותו:
#
#    1. build_command(env) — בונה את שורת הפקודה של dsd-fme מתוך משתני dmr.env
#       (תדר בקרה, color code, channel-map, טראנקינג, יעד WAV, קלט rsp_tcp).
#    2. מריץ את dsd-fme תחת pseudo-terminal (PTY) — כי DSD-FME מצפה ל-TTY.
#    3. parse_dsd_line(text) — הלב הטקסטואלי: מפרסר כל שורת פלט ל-dict אירוע.
#    4. שולח כל אירוע כ-JSON ב-UDP ל-app.py (DSD_UDP) — בדיוק כמו "acarsdec -j".
#    5. שליטה: unix-socket (DSD_CTRL_SOCK) לקבלת הקשות מ-app.py (lockout/hold —
#       Phase עוקב); כרגע ה-socket נפתח ומזריק את הבייטים כמות-שהם ל-PTY.
#
#  ⚠ parse_dsd_line כתוב מול פורמט הפלט הטקסטואלי של DSD-FME (lwvmobile fork).
#    זהו ה"מתאם" — הנקודה היחידה שתלויה בפורמט; אם גרסת DSD-FME משנה ניסוח,
#    מתקנים כאן בלבד (ונבדק ב-tests/test_dsd_normalize.py).
#
#  הרצה ידנית לבדיקה:  python3 dsd_pty.py --selftest   (בלי חומרה)
# ============================================================================
import json
import os
import re
import select
import socket
import sys
import time

# --- קבועים -----------------------------------------------------------------
DEFAULT_UDP = "127.0.0.1:5555"
DSD_BIN = os.environ.get("DSD_BIN", "dsd-fme")
CTRL_SOCK_PATH = os.environ.get("DSD_CTRL_SOCK", "/run/dmr/dsd-ctrl.sock")
# rsp_tcp: הגשר SoapySDR→rtl_tcp שמזין את DSD-FME (RSP1B אינו נתמך native ב-DSD-FME).
RSP_TCP_HOST = os.environ.get("DSD_RTLTCP", "127.0.0.1:1234")

# --- פרסור פלט DSD-FME (הלב הטקסטואלי) --------------------------------------
# כל תבנית מחפשת token בודד בשורה; אירוע נבנה מכל ה-tokens שנמצאו בשורה אחת.
_RE_SLOT = re.compile(r"\b(?:slot|ts)\s*[:=#]?\s*([12])\b", re.I)
_RE_TG = re.compile(r"\b(?:tg|talkgroup|tgid|group)\s*[:=]?\s*(\d{1,8})\b", re.I)
_RE_SRC = re.compile(r"\b(?:src|source|rid|from)\s*[:=]?\s*(\d{2,8})\b", re.I)
_RE_TGT = re.compile(r"\b(?:tgt|dst|target|dest|to)\s*[:=]?\s*(\d{2,8})\b", re.I)
_RE_CC = re.compile(r"\b(?:cc|color\s*code|colorcode)\s*[:=]?\s*(\d{1,2})\b", re.I)
_RE_LCN = re.compile(r"\b(?:lcn|lsn|lpcn|logical\s*ch\w*)\s*[:=]?\s*(\d{1,4})\b", re.I)
_RE_BER = re.compile(r"\bber\s*[:=]?\s*([0-9.]+)", re.I)
_RE_LEVEL = re.compile(r"\b(?:in(?:put)?\s*level|level|rms)\s*[:=]?\s*(-?[0-9.]+)", re.I)
_RE_ALG = re.compile(r"\balg(?:\s*id)?\s*[:=]?\s*(0x[0-9a-f]+|\d+)", re.I)
_RE_KEY = re.compile(r"\bkey(?:\s*id)?\s*[:=]?\s*(0x[0-9a-f]+|\d+)", re.I)
# תדר שהטראנקינג התכוונן אליו (Hz או MHz). DSD-FME מדפיס בעת grant/tune.
_RE_FREQ_HZ = re.compile(r"\b(?:freq\w*|tuned?)\s*[:=]?\s*(\d{7,10})\b", re.I)
_RE_FREQ_MHZ = re.compile(r"\b(?:freq\w*|tuned?)\s*[:=]?\s*(\d{2,4}\.\d{3,6})\b", re.I)
# מיקום LRRP (Phase 3): DSD-FME מדפיס lat/long בעת פענוח LRRP
_RE_LAT = re.compile(r"\b(?:lat|latitude)\s*[:=]?\s*(-?\d{1,3}\.\d+)", re.I)
_RE_LON = re.compile(r"\b(?:lon|long|longitude)\s*[:=]?\s*(-?\d{1,3}\.\d+)", re.I)
# קובץ WAV שנפתח לשיחה (DSD-FME מדפיס את שם הקובץ)
_RE_WAV = re.compile(r"([A-Za-z0-9._\-]+\.wav)\b")


def _num(s):
    try:
        return int(str(s), 0)   # תומך 0x..
    except (ValueError, TypeError):
        return None


def parse_dsd_line(text):
    """מפרסר שורת פלט של DSD-FME ל-dict אירוע חלקי (רק ה-tokens שנמצאו),
    או None אם אין בשורה שום מידע שימושי. פונקציה טהורה => נבדקת בלי חומרה.
    ה-app.py (_normalize_dsd) ממזג אירועים חלקיים לכרטיס שיחה מלא."""
    if not text or not text.strip():
        return None
    low = text.lower()
    ev = {}

    m = _RE_SLOT.search(text)
    if m:
        ev["slot"] = int(m.group(1))
    m = _RE_TG.search(text)
    if m:
        ev["tg"] = int(m.group(1))
    m = _RE_SRC.search(text)
    if m:
        ev["src"] = int(m.group(1))
    m = _RE_TGT.search(text)
    if m:
        ev["tgt"] = int(m.group(1))
    m = _RE_CC.search(text)
    if m:
        ev["cc"] = int(m.group(1))
    m = _RE_LCN.search(text)
    if m:
        ev["lcn"] = int(m.group(1))
    m = _RE_BER.search(text)
    if m:
        try:
            ev["ber"] = float(m.group(1))
        except ValueError:
            pass
    m = _RE_LEVEL.search(text)
    if m:
        try:
            ev["level"] = float(m.group(1))
        except ValueError:
            pass
    m = _RE_ALG.search(text)
    if m:
        ev["alg"] = _num(m.group(1))
    m = _RE_KEY.search(text)
    if m:
        ev["key_id"] = _num(m.group(1))
    m = _RE_FREQ_HZ.search(text)
    if m:
        ev["freq_hz"] = int(m.group(1))
    else:
        m = _RE_FREQ_MHZ.search(text)
        if m:
            ev["freq"] = float(m.group(1))
    m = _RE_LAT.search(text)
    m2 = _RE_LON.search(text)
    if m and m2:
        ev["lat"] = float(m.group(1))
        ev["lon"] = float(m2.group(1))
    m = _RE_WAV.search(text)
    if m:
        ev["wav"] = m.group(1)

    # סוג שיחה + אירוע מזוהים לפי מילות מפתח
    if "private call" in low or "unit to unit" in low:
        ev["call_type"] = "private"
    elif "group call" in low:
        ev["call_type"] = "group"
    elif "data call" in low or "data header" in low or "pdu" in low:
        ev["call_type"] = "data"
    elif "registration" in low or "affiliation" in low or "ahoy" in low:
        ev["call_type"] = "reg"
    elif "csbk" in low or "control" in low or "aloha" in low or "rest channel" in low:
        ev["call_type"] = "control"
        ev["event"] = "control"
    if "sms" in low or "short data" in low or "message:" in low:
        ev["call_type"] = "sms"
        # טקסט ההודעה אחרי "Message:" אם קיים
        mm = re.search(r"message\s*:\s*(.+)$", text, re.I)
        if mm:
            ev["text"] = mm.group(1).strip()
    if "encrypt" in low or "enc:" in low:
        ev["encrypted"] = True
    if "lrrp" in low and ev.get("lat") is not None:
        ev["call_type"] = "lrrp"

    if not ev:
        return None
    ev.setdefault("event", ev.get("call_type", "call"))
    ev["proto"] = "DMR"
    return ev


def build_rsp_tcp_command(env):
    """בונה את argv של rsp_tcp (הגשר SoapySDR/SDRplay→rtl_tcp). זהו התהליך
    ש*מחזיק* את ה-RSP1B; DSD-FME מתחבר אליו כלקוח rtl_tcp ושולח פקודות כיוונון
    (טראנקינג). dsd_pty מריץ אותו כתהליך-בן => יחידת systemd אחת = צרכן-SDR אחד
    (כמו rtl_airband ב-AIR-AM). פונקציה טהורה => נבדקת. תדר התחלתי = ערוץ הבקרה."""
    host, _, port = env.get("DSD_RTLTCP", RSP_TCP_HOST).partition(":")
    cmd = [os.environ.get("RSP_TCP_BIN", "rsp_tcp"),
           "-a", host or "127.0.0.1", "-p", port or "1234"]
    control = env.get("DSD_CONTROL_FREQ")
    if control:
        cmd += ["-f", str(control)]      # תדר התחלתי = ערוץ הבקרה (Hz)
    return cmd


def build_command(env):
    """בונה את argv של dsd-fme ממשתני הסביבה (dmr.env). פונקציה טהורה => נבדקת.
    קלט rsp_tcp (‎-i rtltcp), פלט per-call WAV (‎-6/‎-P), טראנקינג (‎-T) עם
    channel-map (‎-C) ותדר בקרה (‎-c). ‎-N מריץ בלי ncurses-input (headless)."""
    rtltcp = env.get("DSD_RTLTCP", RSP_TCP_HOST)
    cmd = [DSD_BIN, "-i", f"rtltcp:{rtltcp}", "-o", "null"]
    # מצב DMR + פענוח מטא-דאטה מלא
    cmd += ["-fs"]                       # -fs = DMR stereo (שני ה-slots)
    cc = env.get("DSD_COLOR_CODE")
    if cc not in (None, ""):
        cmd += ["-C", str(env.get("DSD_CHANNELMAP", ""))] if env.get("DSD_CHANNELMAP") else []
    control = env.get("DSD_CONTROL_FREQ")
    if env.get("DSD_TRUNK") in ("1", "true", "yes") and control:
        cmd += ["-T"]                    # מעקב טראנקינג
        if env.get("DSD_CHANNELMAP"):
            cmd += ["-C", str(env["DSD_CHANNELMAP"])]
        cmd += ["-c", str(control)]      # תדר ערוץ הבקרה (Hz)
    wav_dir = env.get("DSD_WAV_DIR")
    if wav_dir:
        cmd += ["-6", str(wav_dir)]      # per-call WAV לתיקייה
    return cmd


# --- לולאת הרצה (על חומרה) --------------------------------------------------
def _udp_target():
    host, _, port = os.environ.get("DSD_UDP", DEFAULT_UDP).partition(":")
    return (host or "127.0.0.1", int(port or 5555))


def _run():   # pragma: no cover  (רץ רק על חומרה, לא ב-CI)
    import pty
    import subprocess

    target = _udp_target()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    env = dict(os.environ)

    # גשר rsp_tcp (מחזיק את ה-RSP1B) כתהליך-בן — DSD-FME מתחבר אליו כ-rtl_tcp.
    rsp_cmd = build_rsp_tcp_command(env)
    sys.stderr.write("dsd_pty: exec (bridge) %s\n" % " ".join(rsp_cmd))
    rsp = subprocess.Popen(rsp_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(2)   # שהות לגשר לפתוח את ה-SDR ולהאזין לפני ש-DSD-FME מתחבר

    cmd = build_command(env)
    sys.stderr.write("dsd_pty: exec %s\n" % " ".join(cmd))

    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True)
    os.close(slave)

    # unix socket לשליטה (הקשות מ-app.py) — best-effort
    ctrl = None
    try:
        if os.path.exists(CTRL_SOCK_PATH):
            os.unlink(CTRL_SOCK_PATH)
        os.makedirs(os.path.dirname(CTRL_SOCK_PATH), exist_ok=True)
        ctrl = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        ctrl.bind(CTRL_SOCK_PATH)
        ctrl.setblocking(False)
    except OSError:
        ctrl = None

    buf = b""
    try:
        while proc.poll() is None:
            rlist = [master] + ([ctrl] if ctrl else [])
            r, _, _ = select.select(rlist, [], [], 1.0)
            if ctrl in r:
                try:
                    keys, _ = ctrl.recvfrom(64)
                    os.write(master, keys)   # הזרקת הקשות ל-DSD-FME
                except OSError:
                    pass
            if master in r:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace")
                    ev = parse_dsd_line(text)
                    if ev:
                        ev["t"] = time.time()
                        try:
                            sock.sendto(json.dumps(ev).encode("utf-8"), target)
                        except OSError:
                            pass
    finally:
        for p in (proc, rsp):
            try:
                p.terminate()
            except Exception:
                pass
        os.close(master)
    return proc.returncode or 0


def _selftest():
    """בדיקה מהירה ללא חומרה: מפרסר כמה שורות דוגמה ומדפיס את התוצאה."""
    samples = [
        "Sync: +DMR  Slot 1  Group Call  TG=2451  SRC=3141592  CC 1",
        "Slot 2 Private Call TGT 3140001 SRC 3141592 BER 0.5",
        "ALG ID: 0x21  KEY ID: 3  Encrypted",
        "CSBK  Aloha  Rest Channel LCN 3",
        "Tuned to frequency 461062500 Hz for LCN 2",
        "just some banner text with no fields",
    ]
    for s in samples:
        print(f"{s!r}\n   -> {parse_dsd_line(s)}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_run())
