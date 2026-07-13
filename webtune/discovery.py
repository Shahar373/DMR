#!/usr/bin/env python3
# ============================================================================
#  DMR  -  גילוי רשתות (Frequency Discovery): לוגיקה טהורה
# ----------------------------------------------------------------------------
#  סריקה חכמה: (1) סורקים טווח RF ומוצאים תדרים חשודים כ-DMR (גילוי אנרגיה FFT),
#  ואז (2) בודקים כל מועמד עם DSD-FME ומגלים פרמטרי רשת (color code, סוג ערוץ,
#  LSN-ים, TG-ים). כאן חיה כל הלוגיקה הטהורה (בלי חומרה, בלי Flask) => נבדקת
#  ישירות ב-CI. התזמור (thread, SDR, REST) חי ב-app.py; הגשר (FFT) ב-rsp_fm.py.
#
#  ⚠ עקרון הפרויקט: לעולם לא ממציאים מדד. שדה שלא נצפה בפועל => None/"לא ידוע".
#    מיפוי LCN↔תדר מלא אינו ניתן לגילוי אוטומטי (Cap+ משדר LSN לוגי, לא תדר;
#    SDR יחיד לא יכול לצפות בבקרה ובקול בו-זמנית) => best-effort/ידני. ר' CLAUDE.md.
# ============================================================================
from __future__ import annotations

import re

import numpy as np

# תחום RSP1B (VHF/UHF), זהה ל-_validate_systems ב-app.py.
FREQ_MIN_MHZ, FREQ_MAX_MHZ = 24.0, 1300.0
SPAN_MAX_MHZ = 100.0                       # תקרת רוחב-טווח לסריקה אחת (חוסם זמן סריקה)
DEFAULT_START_MHZ, DEFAULT_END_MHZ = 450.0, 470.0   # ברירת מחדל: פס עסקי UHF (Cap+)

DMR_CHANNEL_HZ = 12500                      # רוחב תפוס של נושא DMR (4FSK/FDMA)
DEFAULT_IQ_RATE = 2_000_000                 # קצב סריקה רחב (‎~11 קפיצות ל-20MHz)
DEFAULT_NFFT = 2048
DEFAULT_DWELL_MS = 300
DEFAULT_THRESHOLD_MAD = 6.0                 # סף אדפטיבי: median + k·(1.4826·MAD)
DEFAULT_PROBE_SEC = 6
DEFAULT_MIN_SYNC = 3                        # שורות sync רצופות ל"זה DMR" (נגד false-sync)
DEFAULT_GAIN_INDEX = 14
DEFAULT_MAX_CANDIDATES = 20                 # תקרת מועמדים לבדיקה (חוסם restart-ים)

USABLE_FRAC = 0.9                           # חלון שמיש מתוך הפס (crop לקצוות ה-IF)
EDGE_FRAC = 0.08                            # דילוג בינים בקצוות כל snapshot (rolloff)
NOTCH_HZ = 15000                            # מסיכת ה-DC spike המרכזי (±)
MIN_MARGIN_DB = 8.0                         # מרווח מינימלי מעל רצפת הרעש (יחסי, לא dBFS מוחלט)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_sweep_plan(raw):
    """מנרמל ומאמת בקשת סריקה. מחזיר dict מלא (עם ברירות-מחדל) או None.

    קלט מינימלי: {start_mhz, end_mhz}. שדות מתקדמים אופציונליים (iq_rate, nfft,
    dwell_ms, threshold_mad, probe_sec, min_sync, gain_index, max_candidates)."""
    if not isinstance(raw, dict):
        return None
    start = _as_number(raw.get("start_mhz", DEFAULT_START_MHZ))
    end = _as_number(raw.get("end_mhz", DEFAULT_END_MHZ))
    if start is None or end is None:
        return None
    start, end = round(start, 6), round(end, 6)
    if not (FREQ_MIN_MHZ <= start < end <= FREQ_MAX_MHZ):
        return None
    if (end - start) > SPAN_MAX_MHZ:
        return None

    def _int_field(key, default, low, high):
        try:
            return int(_clamp(int(raw.get(key, default)), low, high))
        except (TypeError, ValueError):
            return default

    def _float_field(key, default, low, high):
        val = _as_number(raw.get(key, default))
        return round(_clamp(val, low, high), 4) if val is not None else default

    return {
        "start_mhz": start,
        "end_mhz": end,
        "iq_rate": _int_field("iq_rate", DEFAULT_IQ_RATE, 240_000, 3_000_000),
        "nfft": _int_field("nfft", DEFAULT_NFFT, 256, 8192),
        "dwell_ms": _int_field("dwell_ms", DEFAULT_DWELL_MS, 100, 3000),
        "threshold_mad": _float_field("threshold_mad", DEFAULT_THRESHOLD_MAD, 2.0, 30.0),
        "probe_sec": _int_field("probe_sec", DEFAULT_PROBE_SEC, 2, 30),
        "min_sync": _int_field("min_sync", DEFAULT_MIN_SYNC, 1, 20),
        "gain_index": _int_field("gain_index", DEFAULT_GAIN_INDEX, 0, 28),
        "max_candidates": _int_field("max_candidates", DEFAULT_MAX_CANDIDATES, 1, 50),
    }


def build_freq_grid(start_mhz, end_mhz, iq_rate, usable_frac=USABLE_FRAC):
    """תדרי-מרכז (Hz) לצעידה על הטווח, בצעד של החלון השמיש (חפיפה בין קפיצות
    כדי לכסות תפרים וקצוות ה-IF). מחזיר לפחות מרכז אחד."""
    start_hz = start_mhz * 1e6
    end_hz = end_mhz * 1e6
    usable = max(1.0, iq_rate * usable_frac)
    centers = []
    center = start_hz + usable / 2.0
    while center - usable / 2.0 < end_hz:
        centers.append(int(round(center)))
        center += usable
    if not centers:
        centers.append(int(round((start_hz + end_hz) / 2.0)))
    return centers


def _snapshot_points(snapshot, notch_hz=NOTCH_HZ, edge_frac=EDGE_FRAC):
    """מחזיר (freqs_hz, power_db) לבינים התקפים ב-snapshot יחיד: מדלג על ה-DC
    notch המרכזי ועל בינים בקצוות (rolloff). fftshifted => אינדקס האמצע הוא DC."""
    power = np.asarray(snapshot.get("power_db") or [], dtype=np.float64)
    n = power.size
    if n < 4:
        return np.empty(0), np.empty(0)
    center_hz = float(snapshot.get("center_hz") or 0)
    bin_hz = float(snapshot.get("bin_hz") or 0)
    if bin_hz <= 0:
        return np.empty(0), np.empty(0)
    idx = np.arange(n)
    offset = (idx - n // 2) * bin_hz            # תדר יחסי למרכז
    freqs = center_hz + offset
    edge = int(n * edge_frac)
    keep = np.ones(n, dtype=bool)
    if edge > 0:
        keep[:edge] = False
        keep[n - edge:] = False
    keep &= np.abs(offset) >= notch_hz          # מסכת DC
    return freqs[keep], power[keep]


def detect_candidates(snapshots, threshold_mad=DEFAULT_THRESHOLD_MAD,
                      chan_hz=DMR_CHANNEL_HZ, max_candidates=DEFAULT_MAX_CANDIDATES,
                      notch_hz=NOTCH_HZ, min_margin_db=MIN_MARGIN_DB):
    """מוציא תדרים חשודים ממפת ההספק (רשימת snapshots של הסריקה).

    סף אדפטיבי: median + max(k·1.4826·MAD, min_margin_db). הכול *יחסי* לרצפת
    הרעש הנמדדת — לעולם לא קבוע dBFS מוחלט (rsp_tcp נותן dBFS יחסי בלבד). מרווח
    ה-min מגן על המקרה של רצפה שטוחה מדי (MAD=0) שבה std מנופח דווקא ע"י הנושאים
    שאנחנו מחפשים. מיזוג בינים חמים סמוכים (≤ רוחב ערוץ) למועמד; מיון לפי עוצמה."""
    all_freqs, all_power = [], []
    for snap in snapshots or []:
        freqs, power = _snapshot_points(snap, notch_hz=notch_hz)
        if freqs.size:
            all_freqs.append(freqs)
            all_power.append(power)
    if not all_freqs:
        return []
    freqs = np.concatenate(all_freqs)
    power = np.concatenate(all_power)

    median = float(np.median(power))
    mad = float(np.median(np.abs(power - median)))
    threshold = median + max(threshold_mad * 1.4826 * mad, min_margin_db)

    hot = power >= threshold
    if not np.any(hot):
        return []
    hot_freqs = freqs[hot]
    hot_power = power[hot]
    order = np.argsort(hot_freqs)
    hot_freqs = hot_freqs[order]
    hot_power = hot_power[order]

    # מיזוג בינים חמים סמוכים (פער ≤ רוחב ערוץ) למועמד אחד; תדר = הבין החזק ביותר.
    candidates = []
    cluster_f = [hot_freqs[0]]
    cluster_p = [hot_power[0]]
    for f, p in zip(hot_freqs[1:], hot_power[1:]):
        if f - cluster_f[-1] <= chan_hz:
            cluster_f.append(f)
            cluster_p.append(p)
        else:
            candidates.append(_make_candidate(cluster_f, cluster_p, median))
            cluster_f, cluster_p = [f], [p]
    candidates.append(_make_candidate(cluster_f, cluster_p, median))

    candidates.sort(key=lambda c: c["power_db"], reverse=True)
    return candidates[:max_candidates]


def _make_candidate(cluster_f, cluster_p, floor_db):
    peak = int(np.argmax(cluster_p))
    freq_hz = int(round(float(cluster_f[peak])))
    power_db = round(float(cluster_p[peak]), 2)
    return {
        "freq_hz": freq_hz,
        "freq_mhz": round(freq_hz / 1e6, 6),
        "power_db": power_db,
        "snr_db": round(power_db - floor_db, 2),
    }


def _mode_int(values):
    """הערך השכיח (mode) ברשימת ints, או None אם ריקה."""
    counts = {}
    for v in values:
        if v is None:
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def aggregate_probe(freq_mhz, events, min_sync=DEFAULT_MIN_SYNC):
    """מסכם את אירועי ה-UDP שנאספו בזמן בדיקת מועמד לרשומת רשת מגולה.

    events = רשימת ה-dict-ים הגולמיים (sync/channel_status/voice_call/quality/
    encryption/data_header) שנאספו בחלון הבדיקה. לעולם לא ממציא — שדה שלא נצפה
    נשאר None/ריק."""
    events = [e for e in (events or []) if isinstance(e, dict)]
    sync = [e for e in events if e.get("type") == "sync"]
    chan_status = [e for e in events if e.get("type") == "channel_status"]
    voice = [e for e in events if e.get("type") == "voice_call"]
    data = [e for e in events if e.get("type") == "data_header"]
    quality = [e for e in events if e.get("type") == "quality"]
    encryption = [e for e in events if e.get("type") == "encryption"]

    ccs = [e.get("cc") for e in (sync + chan_status + quality) if e.get("cc") is not None]
    cc = _mode_int(ccs)

    if chan_status:
        channel_type = "control"          # CSBK Channel Status + Rest LSN = ערוץ בקרה
    elif voice:
        channel_type = "voice"            # פריימי קול = ערוץ נושא
    elif sync:
        channel_type = "conventional"     # sync בלי CSBK trunking = קונבנציונלי/idle
    else:
        channel_type = None

    is_dmr = bool(len(sync) >= min_sync or voice or chan_status)

    rest_lsns = sorted({int(e["rest_lsn"]) for e in chan_status if e.get("rest_lsn") is not None}
                       | {int(e["lcn"]) for e in voice if e.get("lcn") is not None})
    talkgroups = sorted({int(e["tg"]) for e in voice if e.get("tg") is not None})
    rids = sorted({int(e["src"]) for e in (voice + data) if e.get("src") is not None})
    encrypted = bool(encryption)

    # ביטחון (0..1): ראיות חזקות (קול/כמה channel_status) => גבוה; sync מתמשך => בינוני.
    if voice or len(chan_status) >= 2:
        confidence = 0.9
    elif len(sync) >= min_sync or chan_status:
        confidence = 0.6
    elif sync:
        confidence = 0.3
    else:
        confidence = 0.0

    return {
        "freq_mhz": round(float(freq_mhz), 6),
        "is_dmr": is_dmr,
        "cc": cc,
        "channel_type": channel_type,
        "rest_lsns": rest_lsns,
        "talkgroups": talkgroups,
        "rids": rids,
        "encrypted": encrypted,
        "confidence": round(confidence, 2),
        "sync_count": len(sync),
        "voice_count": len(voice),
        "sample_count": len(events),
    }


def _system_id(freq_mhz):
    """id יציב וחוקי (^[A-Za-z0-9_\\-]{1,32}$) מתדר: 461.0375 => disc_461_037500."""
    khz = int(round(float(freq_mhz) * 1000))
    return re.sub(r"[^A-Za-z0-9_\-]", "_", f"disc_{khz}")[:32]


def discovery_to_system(record, name=None):
    """הופך רשומת גילוי לאובייקט מערכת (schema של app._validate_systems).

    ⚠ channelmap נשאר ריק במכוון: מיפוי LCN↔תדר אינו ניתן לגילוי אוטומטי מלא
    (ר' ראש הקובץ) => המשתמש משלים ידנית. color_code חסר => 0 (ניטרלי)."""
    freq = round(float(record["freq_mhz"]), 6)
    cc = record.get("cc")
    if cc is None or not (0 <= int(cc) <= 15):
        cc = 0
    return {
        "id": _system_id(freq),
        "name": (name or f"גילוי {freq:.4f} MHz").strip()[:48],
        "control": freq,
        "color_code": int(cc),
        "channelmap": [],
    }
