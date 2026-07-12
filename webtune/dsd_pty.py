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
#    3. parse_dsd_line(text) — הלב הטקסטואלי: מפרסר כל שורת פלט ל-dict אירוע
#       *מוקלד* (type), או None לשורת housekeeping (לא נשלח ל-UDP כלל — ~80%
#       מהפלט בפועל הוא lsn_status/channel_status/site_info/ip_mapping/
#       bank_call/preamble_csbk, סוננו כבר כאן במקור כדי לחסוך רוחב-פס).
#    4. שולח כל אירוע כ-JSON ב-UDP ל-app.py (DSD_UDP) — בדיוק כמו "acarsdec -j".
#    5. שליטה: unix-socket (DSD_CTRL_SOCK) לקבלת הקשות מ-app.py — נוד-רווח
#       חי (g/G) דרך send_gain_nudge(), בלי לעצור את DSD-FME.
#
#  ⚠ parse_dsd_line נכתב מחדש מול **קליטה אמיתית** של רשת Cap+/SLCO רב-אתרית
#    (לא ניחוש) — התבניות המדויקות (SLOT N TGT=N SRC=N Cap+ Group Call וכו')
#    אומתו מול 20,000 שורות אמיתיות. זהו ה"מתאם" — הנקודה היחידה שתלויה
#    בפורמט; אם גרסת DSD-FME משנה ניסוח, מתקנים כאן בלבד (ונבדק ב-
#    tests/test_dsd_normalize.py עם golden fixtures מאותה קליטה אמיתית).
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

# --- פרסור פלט DSD-FME: תבניות אמיתיות (לא ניחוש) ---------------------------
# מקור: קליטה אמיתית מרשת Motorola Capacity Plus רב-אתרית (SLCO). כל תבנית
# מעוגנת למילות-מפתח מדויקות שנצפו בפועל, לא token גנרי — עדיפות לדיוק על כיסוי.

# שיחת קול (voice_call): "SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5"
# וריאציות אמיתיות: בלי "Cap+", עם/בלי "TXI" (Cap+ TX Interrupt), עם/בלי
# "Rest LSN:", עם סיומת "(CRC ERR)" לפריים שנכשל. Group => tgt הוא ה-TG עצמו
# (אין שדה TG נפרד בפרוטוקול — זו הכתובת); Private/Unit to Unit => tgt נשאר יעד.
_RE_VOICE_CALL = re.compile(
    r"SLOT\s+(?P<slot>\d)\s+TGT=(?P<tgt>\d+)\s+SRC=(?P<src>\d+)\s+"
    r"(?:Cap\+\s+)?(?P<kind>Group|Private|Unit to Unit)(?:\s+TXI)?\s+Call"
    r"(?:\s+Rest LSN:\s*(?P<rest_lsn>\d+))?", re.I)

# כותרת נתונים (data_header): "Slot 1 Data Header - Indiv - Confirmed Delivery
# - Response Requested - Source: 191 Target: 64250". addressing תמיד Indiv
# בקליטה שנבדקה (Group data header לא נצפה, אך התבנית סובלת אותו).
_RE_DATA_HEADER = re.compile(
    r"Slot\s+(?P<slot>\d)\s+Data Header\s*-\s*(?P<addr>Indiv|Group)\s*-\s*"
    r"(?P<delivery>Confirmed Delivery|Unconfirmed Delivery|Response Packet)"
    r".*?Source:\s*(?P<src>\d+)\s+Target:\s*(?P<tgt>\d+)", re.I)

# בקשת/תגובת מיקום LRRP (בלי נ"צ עצמם): "LRRP SRC: 199; Response to TGT: 64250;"
_RE_LRRP_REQ = re.compile(r"LRRP\s+SRC:\s*(?P<src>\d+);\s*Response to TGT:\s*(?P<tgt>\d+);", re.I)

# מיקום LRRP בפועל: "Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)". ה-SRC
# בקליטה שנבדקה מגיע בעמודה נפרדת (לא באותה שורת טקסט) — לכן src אופציונלי
# כאן (תומך בפורמט עם "SRC: N" מקדים אם קיים בגרסה אחרת); אחרת src=None
# ו-app.py מנסה למתאם לפי proximity זמן/slot (לא ממציא).
_RE_LRRP_POS = re.compile(
    r"(?:SRC[:=]?\s*(?P<src>\d+)\D*?)?Lat:\s*(?P<lat>-?[0-9.]+)\s+Lon:\s*(?P<lon>-?[0-9.]+)", re.I)

# הצפנה (Protected LC): "SLOT 1 Protected LC  FLCO=0x0C FID=0x00 ...". שים לב:
# FLCO/FID הם routing fields של ה-Link Control, *לא* ALG/KEY — DSD-FME לא
# הדפיס כאן שם אלגוריתם/מזהה מפתח (נבדק בקליטה אמיתית) => alg/key_id נשארים
# None ("לעולם לא ממציאים ערך"); encrypted=True בלבד.
_RE_ENCRYPTION = re.compile(r"SLOT\s+(?P<slot>\d)\s+Protected LC\b", re.I)

# תקלות CRC/FEC (quality): 4 סוגים שנצפו בקליטה אמיתית. אלה *לא* הופכים
# לכרטיס שיחה — מוזנים למד ה-RF (error frequency) בלבד, ר' _rf_quality ב-app.py.
_RE_QUALITY_ERR = re.compile(
    r"(CACH/Burst FEC ERR|CSBK \(CRC ERR\)|CSBK \(FEC ERR\)|SLCO CRC ERR)", re.I)
_QUALITY_ERR_MAP = {
    "cach/burst fec err": "CACH_BURST_FEC", "csbk (crc err)": "CSBK_CRC",
    "csbk (fec err)": "CSBK_FEC", "slco crc err": "SLCO_CRC",
}
_RE_QUALITY_CC = re.compile(r"Color Code=(?P<cc>\d+)", re.I)


def parse_dsd_line(text):
    """מפרסר שורת פלט של DSD-FME לאירוע מוקלד (dict עם 'type'), או None
    לשורת housekeeping (lsn_status/channel_status/site_info/ip_mapping/
    bank_call/preamble_csbk/ncurses לא-מזוהה — לא נשלחות ל-UDP כלל).
    פונקציה טהורה => נבדקת בלי חומרה מול golden fixtures אמיתיים."""
    if not text or not text.strip():
        return None
    text = text.strip()

    m = _RE_VOICE_CALL.search(text)
    if m:
        kind = m.group("kind").lower()
        ev = {"type": "voice_call", "slot": int(m.group("slot")),
              "src": int(m.group("src")),
              "call_type": "group" if kind == "group" else "private",
              "crc_err": "(CRC ERR)" in text}
        if kind == "group":
            ev["tg"] = int(m.group("tgt"))
        else:
            ev["tgt"] = int(m.group("tgt"))
        if m.group("rest_lsn"):
            ev["lcn"] = int(m.group("rest_lsn"))
        return ev

    m = _RE_DATA_HEADER.search(text)
    if m:
        return {"type": "data_header", "slot": int(m.group("slot")),
                "src": int(m.group("src")), "tgt": int(m.group("tgt")),
                "call_type": "data", "delivery": m.group("delivery")}

    m = _RE_LRRP_REQ.search(text)
    if m:
        return {"type": "lrrp_request", "src": int(m.group("src")),
                "tgt": int(m.group("tgt")), "call_type": "lrrp"}

    if "lat:" in text.lower() and "lon:" in text.lower():
        m = _RE_LRRP_POS.search(text)
        if m:
            ev = {"type": "lrrp_position", "lat": float(m.group("lat")),
                  "lon": float(m.group("lon")), "call_type": "lrrp"}
            if m.group("src"):
                ev["src"] = int(m.group("src"))
            return ev

    m = _RE_ENCRYPTION.search(text)
    if m:
        return {"type": "encryption", "slot": int(m.group("slot")), "encrypted": True}

    m = _RE_QUALITY_ERR.search(text)
    if m:
        ev = {"type": "quality",
              "error_type": _QUALITY_ERR_MAP.get(m.group(1).lower(), m.group(1).upper())}
        cc = _RE_QUALITY_CC.search(text)
        if cc:
            ev["cc"] = int(cc.group("cc"))
        return ev

    return None   # housekeeping — לא נשלח


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


# --- נוד-רווח חי (g/G): DSD-FME תומך בהקשות מקלדת "Manually decrease/increase
# RTL gain" בזמן ריצה, שנשלחות דרך חיבור ה-rtl_tcp הקיים ל-rsp_tcp — בלי לעצור
# את DSD-FME ובלי לפצח פרוטוקול/לפצ'ץ' קוד C. זו רווח *יחסי* (rsp_tcp במצב
# תואם-RTL נותן אינדקס רווח מצורף אחד, לא IFGR/RFGR עצמאיים — מצב extended
# עם gain עצמאי שובר את פורמט ה-IQ שDSD-FME קורא). אין readback מ-DSD-FME =>
# app.py עוקב אחרי מספר הלחיצות בעצמו (best-effort, לא מספר dB אמיתי).
GAIN_UP_KEY, GAIN_DOWN_KEY = b"G", b"g"


def send_gain_nudge(direction, sock_path=None):
    """שולח הקשת נוד-רווח בודדת (g/G) ל-DSD_CTRL_SOCK של dsd_pty הרץ. direction
    = 'up'/'down'. מחזיר True אם הבייטים נשלחו (לא מאמת קליטה בפועל ע"י
    DSD-FME — best-effort, כמו כל התקשורת דרך ה-PTY). קורא: app.py."""
    key = GAIN_UP_KEY if direction == "up" else GAIN_DOWN_KEY
    path = sock_path or CTRL_SOCK_PATH
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(key, path)
        return True
    except OSError:
        return False


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
    """בדיקה מהירה ללא חומרה: מפרסר שורות דוגמה *אמיתיות* (מקליטת Cap+/SLCO
    רב-אתרית) ומדפיס את התוצאה. אותן שורות מדויקות ב-tests/test_dsd_normalize.py."""
    samples = [
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5",
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group TXI Call  Rest LSN: 5",
        "Slot 1 Data Header - Indiv - Confirmed Delivery - Response Requested - Source: 191 Target: 64250",
        "LRRP SRC: 199; Response to TGT: 64250;",
        "Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)",
        "SLOT 1 Protected LC  FLCO=0x0C FID=0x00  SLOT 1 FLCO FEC ERR  (FEC ERR)",
        "21:39:14 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK (CRC ERR)",
        "SLCO CRC ERR",
        "Bank One F80 Private or Data Call(s) -  LSN 03: TGT 64250;",   # housekeeping => None
        "LSN 01:  Idle;  LSN 02:  Idle;  LSN 03: 64250;  LSN 04:  Idle;",   # housekeeping => None
    ]
    for s in samples:
        print(f"{s!r}\n   -> {parse_dsd_line(s)}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(_run())
