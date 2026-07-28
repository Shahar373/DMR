"""‏rf_probe — כיול ואימות של מדד פקיחת-העין ושל ההכרעה שנגזרת ממנו.

הכלי הזה נועד לשטח, ולכן הספים שלו **חייבים** להיות מכוילים למדידה ולא
לניחוש (CLAUDE.md §8). כל בדיקה כאן מייצרת תרחיש-אוויר ידוע (אות נקי,
אות רועש, אות מתחת לרצפת-הכימות, רעש-בלבד, נשא לא-מאופנן, חיתוך) ומוודאת
שההכרעה נכונה — כלומר שהכלי לא ישלח את המשתמש לתקן את הדבר הלא-נכון.
"""
import numpy as np

import dmr_signal
import rf_probe


def _analyse(iq, iq_rate=240_000, **kwargs):
    return rf_probe.analyse_iq(dmr_signal.to_u8(iq), iq_rate, **kwargs)


def _noise(n=600_000, amplitude=0.3, seed=1):
    rng = np.random.default_rng(seed)
    return (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2) * amplitude


# --- מדד פקיחת-העין ---------------------------------------------------------

def test_clean_dmr_has_a_wide_open_eye():
    """אות DMR נקי: הסמלים יושבים כמעט בדיוק על ארבע הסטיות התקניות."""
    report = _analyse(dmr_signal.make_dmr_iq(dmr_signal.random_symbols(),
                                             240_000))
    assert report["eye"]["eye_rms_hz"] < rf_probe.EYE_GOOD_HZ
    assert 2_000 < report["eye"]["dev_p95_hz"] < 3_000
    assert "תקין" in report["verdict"]


def test_eye_widens_monotonically_as_noise_grows():
    """המדד חייב להיות מונוטוני ברעש — אחרת אי-אפשר להשוות שתי הקלטות
    (למשל לפני ואחרי כיוון אנטנה), וזה כל הערך שלו בשטח."""
    curve = [_analyse(dmr_signal.make_dmr_iq(dmr_signal.random_symbols(),
                                             240_000, snr_db=snr, seed=5)
                      )["eye"]["eye_rms_hz"]
             for snr in (20, 15, 12, 10, 8, 6, 4)]
    assert all(a < b for a, b in zip(curve, curve[1:])), curve


def test_eye_threshold_agrees_with_the_measured_symbol_error_rate():
    """★ הכיול עצמו: הסף `EYE_GOOD_HZ` חייב להתאים ל-SER שנמדד באותו
    תרחיש בדיוק. אות ב-SNR=10dB (SER 0.08% — מפוענח) חייב לעבור, ואות
    ב-SNR=4dB (SER 4.2% — כשלי FEC) חייב להיכשל."""
    syms = dmr_signal.random_symbols()
    for snr, decodable in ((10, True), (4, False)):
        iq = dmr_signal.make_dmr_iq(syms, 240_000, snr_db=snr, seed=5)
        eye = _analyse(iq)["eye"]["eye_rms_hz"]
        ser = dmr_signal.symbol_error_rate(
            syms, dmr_signal.demodulate(iq, 240_000)[0])
        assert (eye <= rf_probe.EYE_GOOD_HZ) is decodable, (snr, eye, ser)
        assert (ser < 0.01) is decodable, (snr, eye, ser)


# --- הכרעה: איזו בעיה זו בעצם -----------------------------------------------

def test_noise_only_is_reported_as_no_signal_not_as_a_decode_problem():
    """הטעות היקרה ביותר בשטח היא לחפש באג-פענוח כשפשוט אין אות. רעש
    מרייל את הדיסקרימינטור (p95 גבוה מאוד) — וזה חייב להיאמר במפורש."""
    report = _analyse(_noise())
    assert report["eye"]["dev_p95_hz"] > rf_probe.DEV_MAX_HZ
    assert report["spectrum"]["peak_over_median_db"] < rf_probe.SIGNAL_PRESENT_DB
    assert "אין אות" in report["verdict"]
    assert "לא בעיית פענוח" in report["action"]


def test_unmodulated_carrier_is_not_mistaken_for_dmr():
    """נשא רציף (או ערוץ שקט לגמרי): יש עוצמה מצוינת ואפס מודולציה.
    בלי הבדיקה הזו 'עוצמה חזקה' הייתה נקראת בטעות כ'אות תקין'."""
    tone = 0.9 * np.exp(2j * np.pi * 2_000 * np.arange(600_000) / 240_000)
    report = _analyse(tone)
    assert report["eye"]["dev_p95_hz"] < rf_probe.DEV_MIN_HZ
    assert "אין מודולציית DMR" in report["verdict"]


def test_signal_under_the_quantisation_floor_points_at_the_antenna():
    """‏‎−38 dBFS פר-ערוץ (הטווח שדווח מהשטח ב-27.07): 8 ביט לא מספיקים,
    גם בלי רעש כלל. ההנחיה חייבת להיות אנטנה/מיקום — לא כוונון תוכנה."""
    report = _analyse(dmr_signal.make_dmr_iq(dmr_signal.random_symbols(),
                                             240_000, amplitude=0.012))
    assert report["channel_dbfs"] < rf_probe.LEVEL_FLOOR_DBFS
    assert "רצפת-הכימות" in report["verdict"]
    assert "אנטנה" in report["action"]


def test_weak_but_decodable_signal_is_not_called_broken():
    """‏‎−28 dBFS: שוליים דקים אבל 0% שגיאות-סמל. כלי שצועק 'תקלה' כאן
    ישלח לתקן דבר שלא שבור."""
    report = _analyse(dmr_signal.make_dmr_iq(dmr_signal.random_symbols(),
                                             240_000, amplitude=0.04))
    assert rf_probe.LEVEL_FLOOR_DBFS < report["channel_dbfs"] < rf_probe.LEVEL_WEAK_DBFS
    assert "תקין" in report["verdict"]
    assert "שוליים" in report["action"]


def test_heavy_clipping_is_diagnosed_before_anything_else():
    """חיתוך הוא הסיבה היחידה שבה ההנחיה הפוכה מהאינטואיציה ('הורידו
    רווח' כשנראה חלש), ולכן הוא נבדק ראשון בשרשרת-ההכרעה."""
    report = _analyse(dmr_signal.make_dmr_iq(dmr_signal.random_symbols(),
                                             240_000, amplitude=3.0))
    assert report["front"]["clip_frac"] > 0.25
    assert "חיתוך" in report["verdict"]
    assert "הורידו רווח" in report["action"]


def test_strong_adjacent_channel_shows_as_a_distorted_eye():
    """שכן חזק ב-12.5kHz בקצב הרחב: עוצמה מצוינת, אין חיתוך, ובכל זאת
    העין סגורה. זהו התסמין שמפריד 'הפרעה' מ'אות חלש'."""
    desired = dmr_signal.random_symbols()
    iq = dmr_signal.mix(
        dmr_signal.make_dmr_iq(desired, 672_000, amplitude=1.0),
        dmr_signal.make_dmr_iq(dmr_signal.random_symbols(seed=99), 672_000,
                               offset_hz=12_500, amplitude=10.0, seed=3))
    report = rf_probe.analyse_iq(dmr_signal.to_u8(iq), 672_000, taps=121)
    assert report["channel_dbfs"] > -25      # רחוק מרצפת-הכימות
    assert report["front"]["clip_frac"] < 0.05
    assert report["eye"]["eye_rms_hz"] > rf_probe.EYE_MARGINAL_HZ
    # ★ ההבחנה שהופכת את הדוח לשמיש: יש אנרגיה חזקה בפס (בניגוד לרעש),
    # והשיא מצביע על השכן ב-12.5kHz — כלומר "הפרעה", לא "אין אות".
    assert report["spectrum"]["peak_over_median_db"] > 20
    assert abs(report["spectrum"]["peak_offset_hz"] - 12_500) < 1_000
    assert "מעוות" in report["verdict"]
    assert "הפרעת-שכן" in report["action"]


# --- שכבת ה-CLI (הטהורה שבה) ------------------------------------------------

def test_multi_offsets_are_derived_from_the_capture_centre(tmp_path, capsys):
    """‏`analyse --freqs` על הקלטה רחבת-פס אחת: כל ערוץ מנותח בהיסט שלו,
    ורק הערוץ שבאמת משודר מדווח כתקין."""
    iq = dmr_signal.make_dmr_iq(dmr_signal.random_symbols(), 672_000,
                                offset_hz=-100_000)
    path = tmp_path / "iq.bin"
    path.write_bytes(dmr_signal.to_u8(iq))
    rf_probe.main(["analyse", str(path), "--iq-rate", "672000",
                   "--center", "164.4", "--freqs", "164.3,164.5"])
    out = capsys.readouterr().out
    assert "164.30000" in out and "164.50000" in out
    present, absent = out.split("164.50000")
    assert "תקין" in present
    assert "תקין" not in absent


def test_analyse_defaults_to_scaled_taps(tmp_path, capsys):
    """הכלי מנתח בסלקטיביות הטובה ביותר האפשרית, כדי שדוח 'העין סגורה'
    לעולם לא ייגרם מהפילטר של הכלי עצמו (ר' §8 — במרווח 12.5kHz זה
    ההבדל בין 23% שגיאות ל-0%)."""
    iq = dmr_signal.mix(
        dmr_signal.make_dmr_iq(dmr_signal.random_symbols(), 672_000),
        dmr_signal.make_dmr_iq(dmr_signal.random_symbols(seed=99), 672_000,
                               offset_hz=12_500, amplitude=3.0, seed=3))
    path = tmp_path / "iq.bin"
    path.write_bytes(dmr_signal.to_u8(iq))
    rf_probe.main(["analyse", str(path), "--iq-rate", "672000"])
    assert "תקין" in capsys.readouterr().out


def test_report_never_fabricates_a_number_when_the_capture_is_too_short():
    """§8: אין ערך מומצא. הקלטה קצרה מדי מחזירה None + הנחיה, לא הערכה."""
    report = rf_probe.analyse_iq(b"\x80\x80" * 100, 240_000)
    assert report["eye"]["eye_rms_hz"] is None
    assert report["verdict"] == "לא מספיק דגימות"


def test_an_empty_channel_in_a_busy_band_is_not_blamed_on_the_antenna():
    """★ נתפס בבדיקת-שפיות של הפלט, לא בסקירת-קוד: בניתוח multi רוב
    הערוצים שקטים ברוב הזמן. ההודעה "מתחת לרצפת-הכימות ⇒ תקנו אנטנה"
    נכונה רק כשה**פס כולו** חלש; כשיש בפס אות חזק והערוץ הזה פשוט ריק,
    הפעולה הנכונה הפוכה (לחכות לתעבורה / לבדוק את התדר במפה)."""
    iq = dmr_signal.make_dmr_iq(dmr_signal.random_symbols(), 672_000,
                                offset_hz=-100_000)
    raw = dmr_signal.to_u8(iq)

    busy = rf_probe.analyse_iq(raw, 672_000, offset_hz=-100_000)
    empty = rf_probe.analyse_iq(raw, 672_000, offset_hz=+150_000)

    assert "תקין" in busy["verdict"]
    assert empty["channel_dbfs"] < rf_probe.LEVEL_FLOOR_DBFS
    assert "הערוץ הזה ריק" in empty["verdict"]
    assert "אנטנה" not in empty["action"]
    assert "-249" in empty["action"]           # מצביע על היכן כן יש אות (רזולוציית FFT)


def test_a_uniformly_weak_band_still_points_at_the_antenna():
    """התאום: כשאין בפס שום אות חזק, אותה מדידה כן מצדיקה "אנטנה"."""
    report = rf_probe.analyse_iq(
        dmr_signal.to_u8(dmr_signal.make_dmr_iq(dmr_signal.random_symbols(),
                                                240_000, amplitude=0.012)),
        240_000)
    assert "רצפת-הכימות" in report["verdict"] and "אנטנה" in report["action"]


def test_session_refuses_to_start_when_the_sdr_port_is_taken():
    """★ נתפס בשטח (28.07): ריצה שנייה נכשלה ב-`Connection refused` כי
    ה-`rsp_tcp` הקודם עוד החזיק את הפורט ואת ה-SDR. עכשיו התפיסה מזוהה
    **לפני** ההרצה, עם ההנחיה המדויקת — במקום stack trace על connect."""
    import socket

    import pytest

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        session = rf_probe.RspTcpSession(164_300_000, 240_000, port=port)
        with pytest.raises(RuntimeError, match="כבר תפוס"):
            session.__enter__()
