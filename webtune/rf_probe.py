#!/usr/bin/env python3
"""‏rf_probe — כלי-אבחון RF לשטח: קולט IQ גולמי ומודד **איכות-אות אמיתית**.

למה זה קיים: כשהתחנה לא מפענחת יש שלוש סיבות אפשריות שנראות זהות מבחוץ —
(1) אין מספיק אות (רצפת-הכימות של 8 ביט), (2) יש אות אבל הוא לא DMR / לא
בתדר שחשבנו, (3) האות תקין והבעיה בשרשרת ה-DSP. `/api/rf` נותן עוצמה
(`level`) ותדירות-שגיאות (`errors_per_min`), ושניהם **לא** מפרידים בין
השלושה. הכלי הזה מפריד, כי הוא מודד את **פקיחת-העין** (eye) של הזרם
המפוענח: כמה רחוקים הסמלים בפועל מארבע הסטיות התקניות של DMR
(±1944/±648 Hz). זה מדד שלא דורש לדעת מה שודר — ולכן עובד על אוויר אמיתי.

⚠ **המדידה עוברת דרך `rsp_fm.NfmDemodulator` עצמו** — לא דרך עותק. מה
שהכלי מודד הוא בדיוק מה שה-DSD-FME מקבל.

⚠ **הספים כוילו בסימולציה** (`tests/test_rf_probe.py`, מול אות DMR סינתטי
תקני), לא באוויר. DMR אמיתי משתמש בעיצוב-פולס שונה מעט, ולכן `eye_rms_hz`
של שידור אמיתי-ותקין עשוי לשבת גבוה מעט מהערך הסינתטי. הסיווג נועד להפריד
בין **סדרי-גודל** ("אין אות" / "חלש" / "תקין"), לא לשמש מד-איכות מדויק —
לזה יש את תדירות שגיאות ה-CRC/FEC האמיתית מ-DSD-FME (CLAUDE.md §8).

שימוש בשטח (⚠ עוצרים קודם את הצרכן — SDR אחד בהחלפה):
    sudo systemctl stop dmr-dsdfme
    sudo python3 /opt/dmr/webtune/rf_probe.py capture --freq 164.3 \
        --seconds 6 --out /tmp/iq_164_3.bin
    python3 /opt/dmr/webtune/rf_probe.py analyse /tmp/iq_164_3.bin

    # סריקת-רווח: האם הרווח בכלל עושה משהו, ומה באמת המקסימום
    sudo python3 /opt/dmr/webtune/rf_probe.py gain-sweep --freq 164.3

    # multi: קליטה רחבה אחת, ניתוח של כל הערוצים ממנה
    sudo python3 /opt/dmr/webtune/rf_probe.py capture --freq 164.415625 \
        --iq-rate 672000 --seconds 6 --out /tmp/iq_multi.bin
    python3 /opt/dmr/webtune/rf_probe.py analyse /tmp/iq_multi.bin \
        --iq-rate 672000 --center 164.415625 \
        --freqs 164.10625,164.3,164.325,164.5375,164.6375,164.725
"""
from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from typing import Optional

import numpy as np

import rsp_fm

# ארבע סטיות-התדר של DMR (ETSI TS 102 361-1, §4.2.2) — הסמלים שהמפענח
# אמור לראות. כל סטייה מהן היא רעש/עיוות.
IDEAL_DEVIATIONS = np.array([1944.0, 648.0, -648.0, -1944.0])
SYMBOL_RATE = 4800

# --- ספים מכוילים (ר' tests/test_rf_probe.py — מדידה, לא ניחוש) -------------
# eye_rms_hz של אות סינתטי נקי הוא ~124Hz; הוא עולה עם הרעש ועם חולשת-האות:
# SNR=10dB → 206 (SER 0.08%), SNR=6dB → 284 (1.5%), SNR=4dB → 333 (4.2%),
# ‎−38dBFS (רצפת-כימות) → 318 (10%). רעש-בלבד → ~1860.
EYE_GOOD_HZ = 220.0        # מתחתיו: פענוח נקי בכל התרחישים שנמדדו
EYE_MARGINAL_HZ = 330.0    # מעליו: שיעור שגיאות-סמל דו-ספרתי
# p95 של |סטייה|: DMR תקין יושב סביב 2400Hz. נשא לא-מאופנן נותן ~0;
# רעש בלבד מרייל את הדיסקרימינטור (~6000Hz).
DEV_MIN_HZ = 800.0
DEV_MAX_HZ = 4000.0
# שיא-הספקטרום מעל החציון: פס ריק (רעש בלבד) נמדד ב-~1 dB; **כל** אות
# אמיתי נמדד ב-24 dB ומעלה (אפילו ‎−38 dBFS נותן 39). זה המפריד היחיד
# שעובד בין "אין אות" לבין "יש אות והוא מעוות".
SIGNAL_PRESENT_DB = 8.0
# מרחק שממנו ואילך שיא-האנרגיה כבר אינו הערוץ שביקשנו אלא שכן.
NEIGHBOUR_OFFSET_HZ = 5_000.0
# רצפת-הכימות של IQ ב-8 ביט (נמדד: ‎−26 נקי, ‎−34 שולי, ‎−38 שבור).
LEVEL_FLOOR_DBFS = -34.0
LEVEL_WEAK_DBFS = -26.0


def pcm_to_deviation(pcm: bytes, audio_gain: float = 4.0,
                     audio_rate: int = 48_000) -> np.ndarray:
    """PCM שיצא מ-`NfmDemodulator` → סטיית-תדר ב-Hz (היפוך נוסחת ה-scaling)."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    return samples / (32767.0 / math.pi) / audio_gain * audio_rate / (2 * math.pi)


def eye_metrics(deviation_hz: np.ndarray, audio_rate: int = 48_000) -> dict:
    """פקיחת-עין: מודד כמה קרובים הסמלים לארבע הסטיות התקניות.

    טהורה, ולא דורשת לדעת מה שודר — לכל פאזת-דגימה אפשרית (יש
    `audio_rate/4800` דגימות לסמל) נמדד מרחק-RMS מהסטייה התקנית הקרובה,
    ונבחרת הפאזה הטובה ביותר. זה בדיוק מה שמסנכרן-סמלים אמיתי היה עושה,
    ולכן המספר משקף את מה שמפענח אמיתי רואה."""
    sps = audio_rate // SYMBOL_RATE
    deviation_hz = np.asarray(deviation_hz, dtype=np.float64)
    if deviation_hz.size < sps * 50:
        return {"eye_rms_hz": None, "best_phase": None, "dev_p95_hz": None,
                "symbols": 0}
    best_rms, best_phase, best_n = None, None, 0
    for phase in range(sps):
        taken = deviation_hz[phase::sps]
        distance = np.min(np.abs(taken[:, None] - IDEAL_DEVIATIONS[None, :]),
                          axis=1)
        rms = float(np.sqrt(np.mean(distance ** 2)))
        if best_rms is None or rms < best_rms:
            best_rms, best_phase, best_n = rms, phase, taken.size
    return {"eye_rms_hz": round(best_rms, 1), "best_phase": best_phase,
            "dev_p95_hz": round(float(np.percentile(np.abs(deviation_hz), 95)), 0),
            "symbols": best_n}


def band_spectrum(raw: bytes, iq_rate: int, nfft: int = 4096,
                  max_samples: int = 1_000_000) -> dict:
    """האם יש בכלל אנרגיה בפס, ואיפה היא יושבת.

    ⚠ זה מה שמפריד בין "אין אות" ל"יש אות מעוות" — שני מצבים שנראים זהים
    לגמרי במדד-העין (בשניהם הדיסקרימינטור מרייל). נמדד: פס ריק נותן
    שיא-מעל-חציון של ~1 dB, ואילו אות אמיתי (גם חלש מאוד) נותן 24–70 dB.
    `peak_offset_hz` מצביע ישירות על מקור-האנרגיה — בהפרעת-שכן הוא יצא
    בדיוק במרווח-הערוץ, וזו האבחנה שאי-אפשר לקבל מ-`/api/rf`."""
    values = np.frombuffer(raw, dtype=np.uint8)[:max_samples * 2]
    if values.size < nfft * 2:
        return {"peak_over_median_db": None, "peak_offset_hz": None}
    floats = (values.astype(np.float32) - 127.5) / 128.0
    iq = floats[0::2] + 1j * floats[1::2]
    power_db = rsp_fm.compute_power_spectrum(iq, nfft)
    if power_db is None:
        return {"peak_over_median_db": None, "peak_offset_hz": None}
    peak_bin = int(np.argmax(power_db))
    return {"peak_over_median_db": round(float(np.max(power_db)
                                               - np.median(power_db)), 1),
            "peak_offset_hz": round((peak_bin - nfft // 2) * iq_rate / nfft)}


def classify(front: dict, channel_dbfs: Optional[float], eye: dict,
             spectrum: Optional[dict] = None,
             offset_hz: float = 0.0) -> dict:
    """הכרעה טהורה: מה בעצם רואים כאן, ומה הצעד הבא.

    הסדר מכוון — קודם נשללות הסיבות שאין להן פתרון בתוכנה (חיתוך, פס ריק,
    אין מודולציה, רצפת-כימות), ורק אחר כך נשפטת האיכות."""
    p95 = eye.get("dev_p95_hz")
    rms = eye.get("eye_rms_hz")
    clip = (front or {}).get("clip_frac") or 0.0
    spectrum = spectrum or {}
    contrast = spectrum.get("peak_over_median_db")
    peak_offset = spectrum.get("peak_offset_hz")

    if rms is None:
        return {"verdict": "לא מספיק דגימות", "action": "הקליטו קטע ארוך יותר"}
    if clip > 0.25:
        return {"verdict": f"חיתוך כבד ({clip:.0%})",
                "action": "הורידו רווח — ב-multi החיתוך מייצר אינטרמודולציה "
                          "בין הערוצים"}
    if contrast is not None and contrast < SIGNAL_PRESENT_DB:
        return {"verdict": "אין אות בפס כלל — רעש בלבד",
                "action": "ודאו תדר/אנטנה/חיבור; זו לא בעיית פענוח"}
    if p95 is not None and p95 < DEV_MIN_HZ:
        return {"verdict": "אין מודולציית DMR בתדר הזה",
                "action": "נשא רציף או ערוץ שקט — ודאו תדר, או הקליטו בזמן "
                          "שיש תעבורה"}
    # ⚠ שתי סיבות שונות לגמרי לאותה מדידה (ערוץ מתחת לרצפה), והפעולה
    # הנכונה הפוכה: פס חלש כולו = בעיית-אנטנה; פס חזק שבו **הערוץ הזה**
    # ריק = פשוט לא שודר כלום כאן (או שהתדר במפה שגוי). בלי ההפרדה הזו
    # ניתוח multi של 6 ערוצים שולח לתקן אנטנה בגלל 4 ערוצים שקטים.
    far_from_peak = (peak_offset is not None
                     and abs(peak_offset - offset_hz) > NEIGHBOUR_OFFSET_HZ)
    if channel_dbfs is not None and channel_dbfs < LEVEL_FLOOR_DBFS:
        if far_from_peak and contrast is not None and contrast >= SIGNAL_PRESENT_DB:
            return {"verdict": "הערוץ הזה ריק — הפס עצמו פעיל",
                    "action": f"האנרגיה בפס יושבת {(peak_offset - offset_hz)/1000:+.1f}"
                              " kHz מכאן. ערוץ שקט בזמן ההקלטה, או תדר שגוי במפה"}
        return {"verdict": f"אות מתחת לרצפת-הכימות ({channel_dbfs:.0f} dBFS)",
                "action": "אין פתרון בתוכנה — אנטנה/מיקום/מגבר-קדם. "
                          "8 ביט לא מספיקים מתחת ל-‎−34 dBFS"}
    if rms <= EYE_GOOD_HZ:
        weak = (channel_dbfs is not None and channel_dbfs < LEVEL_WEAK_DBFS)
        return {"verdict": "אות DMR תקין — העין פתוחה",
                "action": ("שוליים דקים בעוצמה, אבל מפוענח" if weak
                           else "אם DSD-FME בכל זאת לא מפענח — הבעיה אינה ב-RF")}
    if rms <= EYE_MARGINAL_HZ:
        return {"verdict": "אות DMR שולי — העין חלקית",
                "action": "צפויות שגיאות FEC בודדות. שפרו אנטנה/רווח, "
                          "ובדקו ערוץ-שכן חזק"}
    if far_from_peak:
        return {"verdict": "אות מעוות — האנרגיה החזקה בפס אינה בערוץ הזה",
                "action": f"השיא יושב {(peak_offset - offset_hz)/1000:+.1f} kHz "
                          "מהערוץ — הפרעת-שכן. ב-multi הדליקו "
                          "DSD_MULTI_SCALED_TAPS, ובדקו אם התדר הנכון הוא בכלל "
                          "מקום-השיא"}
    return {"verdict": "אות מעוות — העין סגורה",
            "action": "יש אנרגיה בערוץ אבל הסמלים לא במקומם: רווח שגוי, תדר "
                      "לא מדויק, או מודולציה שאינה DMR"}


def analyse_iq(raw: bytes, iq_rate: int, offset_hz: float = 0.0,
               taps: Optional[int] = None, cutoff_hz: Optional[float] = None,
               chunk_samples: int = 24_000) -> dict:
    """מריץ IQ גולמי (u8, בדיוק כפי ש-rsp_tcp שולח) דרך שרשרת ה-DSP האמיתית
    ומחזיר דוח מלא. ה-chunking מכוון: כך נבדק גם המצב שחוצה גבולות-chunk,
    בדיוק כמו בריצה חיה."""
    demod = rsp_fm.NfmDemodulator(
        iq_rate=iq_rate, audio_rate=48_000, offset_hz=offset_hz,
        taps=taps if taps is not None else rsp_fm.scaled_taps(iq_rate),
        cutoff_hz=cutoff_hz if cutoff_hz is not None else rsp_fm.DEFAULT_CUTOFF_HZ)
    step = chunk_samples * 2
    pcm = b"".join(demod.process(raw[i:i + step]) for i in range(0, len(raw), step))
    eye = eye_metrics(pcm_to_deviation(pcm))
    front = rsp_fm.iq_level_dbfs(raw)
    spectrum = band_spectrum(raw, iq_rate)
    report = {"offset_hz": offset_hz, "front": front,
              "channel_dbfs": (None if demod.level_dbfs is None
                               else round(demod.level_dbfs, 1)),
              "eye": eye, "spectrum": spectrum,
              "seconds": round(len(raw) / 2 / iq_rate, 2)}
    report.update(classify(front, demod.level_dbfs, eye, spectrum, offset_hz))
    return report


def format_report(report: dict, label: str = "") -> str:
    eye, front, spec = report["eye"], report["front"], report["spectrum"]
    return (f"{label:<14} עוצמה={report['channel_dbfs']!s:>7} dBFS  "
            f"front={front['rms_dbfs']!s:>6}  clip={front['clip_frac']!s:<9} "
            f"eye={eye['eye_rms_hz']!s:>7} Hz  p95={eye['dev_p95_hz']!s:>6} Hz  "
            f"שיא-פס={spec['peak_over_median_db']!s:>5} dB @ "
            f"{spec['peak_offset_hz']!s} Hz\n"
            f"{'':<14} ⇒ {report['verdict']} — {report['action']}")


# --- חומרה: קליטה בפועל (pragma: no cover — דורש RSP1B) ---------------------

def _spawn_rsp_tcp(host: str, port: int, iq_rate: int,
                   freq_hz: int):                      # pragma: no cover
    import os
    command = [os.environ.get("RSP_TCP_BIN", "rsp_tcp"), "-a", host,
               "-p", str(port), "-s", str(iq_rate), "-f", str(freq_hz)]
    print(f"[rf_probe] מפעיל: {' '.join(command)}", file=sys.stderr)
    return subprocess.Popen(command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)


def capture(freq_hz: int, iq_rate: int, seconds: float, host: str = "127.0.0.1",
            port: int = 1234, gain_index: Optional[int] = None,
            settle: float = 1.5) -> bytes:              # pragma: no cover
    """מקליט IQ גולמי מ-rsp_tcp (מפעיל אותו בעצמו). SDR אחד בהחלפה —
    `dmr-dsdfme` חייב להיות עצור."""
    server = _spawn_rsp_tcp(host, port, iq_rate, freq_hz)
    try:
        client = rsp_fm.RtlTcpClient(host, port, freq_hz, iq_rate)
        client.connect()
        if gain_index is not None:
            client.set_fixed_gain(gain_index)
        else:
            client.set_agc(True)
        deadline = time.monotonic() + settle          # מדלגים על ה-transient
        while time.monotonic() < deadline:
            client.recv(1 << 16)
        wanted = int(iq_rate * seconds) * 2
        chunks, got = [], 0
        while got < wanted:
            data = client.recv(min(1 << 18, wanted - got))
            if not data:
                break
            chunks.append(data)
            got += len(data)
        client.close()
        return b"".join(chunks)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def _cmd_capture(args) -> int:                          # pragma: no cover
    raw = capture(int(args.freq * 1e6), args.iq_rate, args.seconds,
                  gain_index=args.gain)
    with open(args.out, "wb") as handle:
        handle.write(raw)
    print(f"נשמרו {len(raw)/1e6:.1f} MB ({len(raw)/2/args.iq_rate:.1f}ש') → {args.out}")
    print(format_report(analyse_iq(raw, args.iq_rate), "מיידי"))
    return 0


def _cmd_gain_sweep(args) -> int:                       # pragma: no cover
    """★ עונה על שאלה שאי-אפשר לענות עליה מה-UI: האם הרווח בכלל משפיע,
    ובאיזה אינדקס העוצמה מפסיקה לעלות. `readback` לא קיים בפרוטוקול
    rtl_tcp, ולכן המדידה הזו היא הראיה היחידה."""
    print(f"{'gain':>5} {'front dBFS':>11} {'clip':>9} {'ערוץ dBFS':>10} {'eye Hz':>8}")
    for index in [int(x) for x in args.indices.split(",")]:
        raw = capture(int(args.freq * 1e6), args.iq_rate, args.seconds,
                      gain_index=index)
        report = analyse_iq(raw, args.iq_rate)
        print(f"{index:>5} {report['front']['rms_dbfs']!s:>11} "
              f"{report['front']['clip_frac']!s:>9} "
              f"{report['channel_dbfs']!s:>10} {report['eye']['eye_rms_hz']!s:>8}")
        time.sleep(0.5)
    return 0


def _cmd_analyse(args) -> int:
    with open(args.path, "rb") as handle:
        raw = handle.read()
    if args.freqs:
        center = args.center or (args.freqs[0])
        for freq in args.freqs:
            offset = (freq - center) * 1e6
            print(format_report(analyse_iq(raw, args.iq_rate, offset_hz=offset,
                                           taps=args.taps),
                                f"{freq:.5f}"))
    else:
        print(format_report(analyse_iq(raw, args.iq_rate, taps=args.taps),
                            "הערוץ"))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    grab = subparsers.add_parser("capture", help="הקלטת IQ גולמי לקובץ")
    grab.add_argument("--freq", type=float, required=True, help="תדר ב-MHz")
    grab.add_argument("--iq-rate", type=int, default=240_000)
    grab.add_argument("--seconds", type=float, default=6.0)
    grab.add_argument("--gain", type=int, default=None,
                      help="אינדקס רווח 0–28 (ברירת מחדל: AGC)")
    grab.add_argument("--out", required=True)
    grab.set_defaults(func=_cmd_capture)

    sweep = subparsers.add_parser("gain-sweep", help="מדידת עוצמה בכל אינדקס רווח")
    sweep.add_argument("--freq", type=float, required=True)
    sweep.add_argument("--iq-rate", type=int, default=240_000)
    sweep.add_argument("--seconds", type=float, default=2.0)
    sweep.add_argument("--indices", default="0,7,14,21,28")
    sweep.set_defaults(func=_cmd_gain_sweep)

    look = subparsers.add_parser("analyse", help="ניתוח קובץ IQ שהוקלט")
    look.add_argument("path")
    look.add_argument("--iq-rate", type=int, default=240_000)
    look.add_argument("--center", type=float, default=None,
                      help="תדר-המרכז של ההקלטה ב-MHz (ל-multi)")
    look.add_argument("--freqs", default=None,
                      help="רשימת תדרים ב-MHz מופרדת בפסיקים (ל-multi)")
    look.add_argument("--taps", type=int, default=None,
                      help="ברירת מחדל: scaled_taps(iq_rate)")
    look.set_defaults(func=_cmd_analyse)

    args = parser.parse_args(argv)
    if getattr(args, "freqs", None):
        args.freqs = [float(x) for x in args.freqs.split(",")]
    return args.func(args)


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
