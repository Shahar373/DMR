"""אפיון RF של שרשרת ה-DSP: רעש, שגיאת-תדר, ערוץ-שכן, רמת-כניסה וחיתוך.

כל בדיקה כאן מזינה אות DMR **סינתטי תקני** (`dmr_signal.py`: 4FSK, 4800
סמלים/ש', ±1944/±648 Hz) לשרשרת ה-DSP **האמיתית** של `rsp_fm.py` ומודדת
שיעור שגיאות-סמל (SER). זו הדרך היחידה לענות "עד כמה התחנה עמידה" בלי
חומרה — `test_rsp_fm.py` מוכיח שהפענוח **עובד** בתנאים נקיים, והקובץ הזה
ממפה את **הגבולות** שבהם הוא מפסיק לעבוד, ומקבע אותם כרגרסיה.

⚠ **מה SER אומר ומה לא:** DMR מגן על עצמו ב-FEC, ולכן SER של כמה עשיריות
האחוז עדיין מפוענח בשלמות, ו-SER של אחוזים בודדים כבר מייצר את `CACH/Burst
FEC ERR` שדווח מהשטח. הספים כאן שמרניים במכוון: הם מקבעים **סדר-גודל
נמדד**, לא הבטחת-פענוח מדויקת (רק תדירות שגיאות ה-CRC/FEC האמיתית מהאוויר
אומרת את זה — ר' CLAUDE.md §8).

זמן-ריצה: ~40ש' (הקובץ הכבד ביותר בסוויטה). ר' §7.
"""
import re
from pathlib import Path

import numpy as np
import pytest

import dmr_signal
import rsp_fm

SINGLE_RATE = 240_000          # חד-ערוצי (dmr/scan) — המנוע שאומת על חומרה
WIDE_RATE = 672_000            # multi (multi_164cluster: פריסת 431kHz)
WIDE_TAPS = rsp_fm.scaled_taps(WIDE_RATE)      # 339


def ser(iq, iq_rate, symbols, **demod_kwargs):
    dev, _demod = dmr_signal.demodulate(iq, iq_rate, **demod_kwargs)
    return dmr_signal.symbol_error_rate(symbols, dev)


def two_channel_iq(desired, neighbour, iq_rate, spacing_hz, neighbour_db):
    """אות רצוי + ערוץ-שכן בהיסט נתון ובעוצמה יחסית נתונה, מנורמל בסכום
    כדי שהניסוי יבודד **סלקטיביות** ולא יערבב פנימה חיתוך."""
    return dmr_signal.mix(
        dmr_signal.make_dmr_iq(desired, iq_rate, amplitude=1.0),
        dmr_signal.make_dmr_iq(neighbour, iq_rate, offset_hz=spacing_hz,
                               amplitude=10 ** (neighbour_db / 20.0), seed=3))


def ui_constant(name):
    """קורא קבוע-סף מה-UI. הבדיקות כאן מכיילות אותו — אם הוא ישתנה בלי
    מדידה, הבדיקה תיפול (ר' ההערה מעל `LEVEL_TARGET_LO` ב-index.html)."""
    html = (Path(__file__).resolve().parent.parent
            / "webtune" / "static" / "index.html").read_text(encoding="utf-8")
    match = re.search(rf"\b{name}\s*=\s*(-?[\d.]+)", html)
    assert match, f"{name} לא נמצא ב-index.html"
    return float(match.group(1))


# --- רעש (SNR) ---------------------------------------------------------------

def test_ser_falls_monotonically_as_snr_improves():
    """עקומת SER↔SNR: חייבת לרדת בעקביות. עקומה לא-מונוטונית מסגירה באג-מצב
    ב-DSP (הזרם מתפרק מסיבה שאינה רעש), לא 'קליטה גרועה'."""
    syms = dmr_signal.random_symbols()
    curve = [ser(dmr_signal.make_dmr_iq(syms, SINGLE_RATE, amplitude=0.9,
                                        snr_db=snr, seed=5), SINGLE_RATE, syms)
             for snr in (4, 6, 8, 10, 12)]
    assert all(a >= b - 1e-9 for a, b in zip(curve, curve[1:])), curve
    assert curve[-1] == 0.0, curve                 # 12dB — נקי לחלוטין
    assert curve[3] < 0.005, curve                 # 10dB — נמדד 0.08%
    assert curve[0] > 0.01, curve                  # 4dB — הבדיקה לא ריקה


def test_wideband_path_is_not_worse_than_single_channel_under_noise():
    """★ רגרסיה למחלקת-הבאגים של v0.14.0 (פאזת-דצימציה): באג-מצב שתלוי
    ב-iq_rate נראה בדיוק כמו 'רעש' בשטח. אות נקי כבר נבדק ב-test_rsp_fm;
    כאן נבדק שגם **תחת רעש** נתיב ה-multi אינו גרוע מהחד-ערוצי."""
    syms = dmr_signal.random_symbols()
    for snr in (6, 8):
        single = ser(dmr_signal.make_dmr_iq(syms, SINGLE_RATE, amplitude=0.9,
                                            snr_db=snr, seed=5),
                     SINGLE_RATE, syms)
        wide = ser(dmr_signal.make_dmr_iq(syms, WIDE_RATE, amplitude=0.9,
                                          snr_db=snr, seed=5), WIDE_RATE, syms)
        assert wide <= single + 0.002, (snr, single, wide)


# --- שגיאת-תדר ---------------------------------------------------------------

@pytest.mark.parametrize("offset_hz,limit", [(250, 0.001), (500, 0.001),
                                             (1_000, 0.02)])
def test_frequency_error_budget_holds_to_1khz(offset_hz, limit):
    """סחיפת TCXO/משדר. ב-164MHz: 1kHz ≈ 6ppm — מעבר לכל מתנד סביר, ולכן
    זהו התקציב שמצדיק את תדר-החתך הצר (6kHz) שנקבע ב-v0.16.0."""
    syms = dmr_signal.random_symbols()
    measured = ser(dmr_signal.make_dmr_iq(syms, SINGLE_RATE,
                                          offset_hz=offset_hz),
                   SINGLE_RATE, syms)
    assert measured < limit, (offset_hz, measured)


def test_frequency_error_beyond_budget_does_degrade():
    """התאום של הבדיקה שמעליה: מוודא שהיא לא עוברת בגלל שהניסוי חסין-מדי.
    3kHz (18ppm) כבר מוציא חלק מהאות מהפילטר הצר, וזה **חייב** להיראות."""
    syms = dmr_signal.random_symbols()
    assert ser(dmr_signal.make_dmr_iq(syms, SINGLE_RATE, offset_hz=3_000),
               SINGLE_RATE, syms) > 0.01


# --- ערוץ-שכן (סלקטיביות) ----------------------------------------------------

def test_adjacent_12k5_channel_destroys_decode_at_wideband_rate():
    """★★ ממצא חדש (נמדד, לא הערכה): ב-672kHz עם 121 taps, שכן במרווח
    **12.5kHz** הורס את הפענוח כבר בעוצמה **שווה** — SER ~23%. באותה
    פריסה בדיוק בקצב החד-ערוצי (240kHz) התוצאה 0%.

    זה לא 'רעש' ולא 'קליטה חלשה': רוחב-המעבר של הפילטר הוא ~3.3·fs/taps,
    כלומר ~6.5kHz ב-240kHz מול ~18kHz ב-672kHz — אותו מספר taps נותן
    סלקטיביות גרועה פי-2.8 בקצב הרחב. ⚠ ההערה ב-CLAUDE.md §8 ש'`scaled_taps`
    נבדק ונמצא מיותר' נשענה על מדידת-דחייה בהיסט 21kHz בלבד; ב-12.5kHz —
    המרווח התקני של DMR — המסקנה מתהפכת."""
    desired, neighbour = (dmr_signal.random_symbols(),
                          dmr_signal.random_symbols(seed=99))
    iq_wide = two_channel_iq(desired, neighbour, WIDE_RATE, 12_500, 0)
    iq_single = two_channel_iq(desired, neighbour, SINGLE_RATE, 12_500, 0)

    assert ser(iq_wide, WIDE_RATE, desired) > 0.10          # נמדד 23.1%
    assert ser(iq_single, SINGLE_RATE, desired) == 0.0
    assert ser(iq_wide, WIDE_RATE, desired, taps=WIDE_TAPS) == 0.0


@pytest.mark.parametrize("neighbour_db", [10, 20])
def test_scaled_taps_restores_single_channel_selectivity(neighbour_db):
    """`scaled_taps(672k)` (=339) מחזיר את הסלקטיביות של המנוע החד-ערוצי:
    מול שכן ב-12.5kHz התוצאה זהה (בשבריר האחוז) לזו של 121 taps ב-240kHz,
    בכל עוצמת-שכן שנבדקה. זו ההצדקה המדידה להפוך אותו לברירת-מחדל."""
    desired, neighbour = (dmr_signal.random_symbols(),
                          dmr_signal.random_symbols(seed=99))
    reference = ser(two_channel_iq(desired, neighbour, SINGLE_RATE,
                                   12_500, neighbour_db), SINGLE_RATE, desired)
    scaled = ser(two_channel_iq(desired, neighbour, WIDE_RATE,
                                12_500, neighbour_db), WIDE_RATE, desired,
                 taps=WIDE_TAPS)
    assert abs(scaled - reference) < 0.01, (neighbour_db, reference, scaled)


@pytest.mark.parametrize("spacing_hz", [25_000, 50_000])
def test_deployed_25khz_spacing_survives_a_strong_neighbour(spacing_hz):
    """המרווח המינימלי בפועל בסקר-השדה הוא 25kHz (`multi_164cluster`/
    `multi_165cluster`). שם ברירת-המחדל הקיימת (121 taps) עומדת גם בשכן
    חזק ב-30dB ⇒ הבאג שמעל אינו מסביר את מה שנצפה באשכולות האלה, ואסור
    לתלות בו כשל-פענוח בפריסות הפרוסות היום."""
    desired, neighbour = (dmr_signal.random_symbols(),
                          dmr_signal.random_symbols(seed=99))
    assert ser(two_channel_iq(desired, neighbour, WIDE_RATE, spacing_hz, 30),
               WIDE_RATE, desired) < 0.001


# --- רמת-כניסה (רצפת-הכימות) -------------------------------------------------

def _level_and_ser(amplitude, rate=SINGLE_RATE):
    syms = dmr_signal.random_symbols()
    iq = dmr_signal.make_dmr_iq(syms, rate, amplitude=amplitude)
    front = rsp_fm.iq_level_dbfs(dmr_signal.to_u8(iq))
    dev, demod = dmr_signal.demodulate(iq, rate)
    return front, demod.level_dbfs, dmr_signal.symbol_error_rate(syms, dev)


def test_quantisation_floor_bounds_the_usable_channel_level():
    """★ רלוונטי ישירות לדיווח מהשטח (‎−36..−39 dBFS פר-ערוץ ב-gain מקסימלי):
    ה-IQ מ-rsp_tcp הוא 8 ביט, ולכן לאות חלש **לא נשאר מספיק רזולוציה** גם
    כשאין רעש כלל. נמדד: ‎−26 dBFS נקי לחלוטין, ‎−34 עדיין שולי (0.03%),
    ‎−38 כבר 10% שגיאות-סמל. כלומר טווח ה-‎−36..−39 שדווח יושב **בדיוק על
    הצוק** — מסקנה שאין ממנה מנוס: צריך יותר אות (אנטנה/מיקום/מגבר), לא
    כוונון-תוכנה."""
    _f, clean_level, clean_ser = _level_and_ser(0.05)
    _f, marginal_level, marginal_ser = _level_and_ser(0.02)
    _f, broken_level, broken_ser = _level_and_ser(0.012)

    assert -27 < clean_level < -25 and clean_ser == 0.0
    assert -35 < marginal_level < -33 and marginal_ser < 0.005
    assert -39 < broken_level < -37 and broken_ser > 0.05


def test_ui_low_level_warning_fires_while_decoding_is_still_perfect():
    """סף האזהרה ב-UI (`LEVEL_TARGET_LO`) חייב להישאר **מעל** רצפת-הכימות
    הנמדדת — אחרת הוא מתריע רק אחרי שכבר איבדנו פענוח. ברמת-הסף עצמה
    הפענוח חייב להיות מושלם."""
    threshold = ui_constant("LEVEL_TARGET_LO")
    _front, level, measured = _level_and_ser(0.056)     # ≈ הסף עצמו
    assert threshold - 1 <= level <= threshold + 1, (threshold, level)
    assert measured == 0.0
    assert threshold > -34 + 5, threshold              # שוליים מעל הצוק הנמדד


# --- חיתוך (over-gain) -------------------------------------------------------

def test_constant_envelope_fm_tolerates_moderate_clipping():
    """FM הוא מעטפת-קבועה, ולכן חיתוך פוגע בו הרבה פחות מהאינטואיציה:
    `clip_frac` של 0.2 עדיין 0% שגיאות. הסף האדום ב-UI (0.25) שמרני
    במכוון — הוא מתריע לפני שיש נזק, לא אחריו."""
    syms = dmr_signal.random_symbols()
    front, _lvl, mild = _clip_case(syms, 1.05)
    assert front["clip_frac"] > 0.15 and mild == 0.0, (front, mild)

    front, _lvl, heavy = _clip_case(syms, 2.0)
    assert front["clip_frac"] > 0.6 and heavy > 0.10, (front, heavy)


def _clip_case(syms, amplitude, rate=SINGLE_RATE):
    iq = dmr_signal.make_dmr_iq(syms, rate, amplitude=amplitude)
    front = rsp_fm.iq_level_dbfs(dmr_signal.to_u8(iq))
    dev, demod = dmr_signal.demodulate(iq, rate)
    return front, demod.level_dbfs, dmr_signal.symbol_error_rate(syms, dev)


def test_clipping_hurts_wideband_multi_earlier_than_a_single_channel():
    """★ הבחנה שלא הייתה מתועדת: הסובלנות-לחיתוך שמעל תקפה ל**ערוץ אחד**.
    ב-multi ה-ADC רואה את **סכום** הערוצים, וחיתוך של סכום יוצר מכפלי-
    אינטרמודולציה שנופלים בתוך ערוצים אחרים. נמדד: 6 ערוצים שווי-עוצמה
    (פריסת `multi_164cluster`) כבר ב-`clip_frac`≈0.26 מייצרים ~2% שגיאות,
    בעוד שערוץ בודד באותו clip_frac נשאר ב-0%. ⇒ הסף האדום 0.25 ב-UI הוא
    הסף הנכון **ל-multi**, ואסור להרפות אותו על סמך מדידת ערוץ-בודד."""
    offsets = [-200_000, -6_200, 18_800, 231_300, 131_300, 43_800]
    desired = dmr_signal.random_symbols()
    total = dmr_signal.make_dmr_iq(desired, WIDE_RATE, offset_hz=offsets[0])
    for k, off in enumerate(offsets[1:], 1):
        total = total + dmr_signal.make_dmr_iq(
            dmr_signal.random_symbols(seed=100 + k), WIDE_RATE,
            offset_hz=off, seed=k)

    clean = dmr_signal.mix(total, peak=0.9)
    clipped = dmr_signal.mix(total, peak=3.0)
    front = rsp_fm.iq_level_dbfs(dmr_signal.to_u8(clipped))

    assert ser(clean, WIDE_RATE, desired, offset_hz=offsets[0]) == 0.0
    assert 0.2 < front["clip_frac"] < 0.35, front
    assert ser(clipped, WIDE_RATE, desired, offset_hz=offsets[0]) > 0.005
    assert front["clip_frac"] > ui_constant("CLIP_BAD")
