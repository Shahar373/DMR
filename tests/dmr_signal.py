"""מחולל אות DMR סינתטי (עוזר-בדיקות, לא קוד-ריצה) + כלי-מדידה, להרצה דרך שרשרת ה-DSP האמיתית של rsp_fm.

DMR (ETSI TS 102 361-1): 4FSK, 4800 סמלים/ש', סטיות ±1944/±648 Hz,
עיצוב-פולס raised-cosine (alpha≈0.2). 48kHz פלט = 10 דגימות/סמל.
"""
import numpy as np

SYMBOL_RATE = 4800
DEVIATIONS = np.array([1944.0, 648.0, -648.0, -1944.0])   # dibits 01,00,10,11


def raised_cosine(sps, span=10, alpha=0.2):
    """Raised-cosine (לא RRC): מקיים את קריטריון Nyquist — אפס ISI **במרכזי
    הסמלים** גם בלי פילטר מותאם בצד הקליטה, וכך מרכזי-הסמלים שווים בדיוק
    לסטיות המבוקשות. מנורמל לשיא 1."""
    t = np.arange(-span * sps / 2, span * sps / 2 + 1, dtype=np.float64) / sps
    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.sinc(t) * np.cos(np.pi * alpha * t) / (1 - (2 * alpha * t) ** 2)
    h[np.isclose(np.abs(2 * alpha * t), 1)] = (np.pi / 4) * np.sinc(1 / (2 * alpha))
    return h / h.max()


def make_dmr_iq(symbols, iq_rate, offset_hz=0.0, amplitude=0.9,
                snr_db=None, seed=0):
    """סדרת סמלים -> IQ מרוכב בקצב iq_rate (עם עיצוב-פולס ומודולציית FM)."""
    sps = iq_rate // SYMBOL_RATE
    assert sps * SYMBOL_RATE == iq_rate, "iq_rate חייב להיות כפולה של 4800"
    dev = DEVIATIONS[symbols]
    up = np.zeros(len(dev) * sps)
    up[::sps] = dev
    shaped = np.convolve(up, raised_cosine(sps), mode="same")
    phase = 2 * np.pi * np.cumsum(shaped) / iq_rate
    if offset_hz:
        phase += 2 * np.pi * offset_hz * np.arange(len(phase)) / iq_rate
    iq = amplitude * np.exp(1j * phase)
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        n = (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq))) / np.sqrt(2)
        iq = iq + n * (amplitude / (10 ** (snr_db / 20.0)))
    return iq


def to_u8(iq):
    """IQ מרוכב -> בתים u8 משורשרים (בדיוק מה ש-rsp_tcp שולח)."""
    i = np.clip(np.round(iq.real * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q = np.clip(np.round(iq.imag * 127.5 + 127.5), 0, 255).astype(np.uint8)
    out = np.empty(len(i) * 2, dtype=np.uint8)
    out[0::2], out[1::2] = i, q
    return out.tobytes()


def pcm_to_deviation(pcm_i16, audio_gain=4.0, audio_rate=48_000):
    """הופך PCM בחזרה לסטיית-תדר ב-Hz (היפוך הנוסחה ב-NfmDemodulator)."""
    return pcm_i16.astype(np.float64) / (32767.0 / np.pi) / audio_gain \
        * audio_rate / (2 * np.pi)


def best_slice(dev_hz, n_symbols, sps=10):
    """מוצא את פאזת-הדגימה הטובה ביותר ומחזיר (סמלים משוחזרים, פאזה, שגיאת-RMS)."""
    best = None
    for phase in range(sps):
        taken = dev_hz[phase::sps][:n_symbols]
        if len(taken) < n_symbols:
            continue
        idx = np.argmin(np.abs(taken[:, None] - DEVIATIONS[None, :]), axis=1)
        err = np.sqrt(np.mean((taken - DEVIATIONS[idx]) ** 2))
        if best is None or err < best[2]:
            best = (idx, phase, err, taken)
    return best


def mix(*signals, peak=0.8):
    """מחבר כמה אותות IQ ומנרמל את **הסכום** לשיא נתון.

    כך יחס-העוצמות בין האותות נשמר בדיוק כפי שנקבע ב-`amplitude` של כל אחד,
    בלי שחיתוך ב-`to_u8` יחתוך את הניסוי — הפרדה מכוונת בין *סלקטיביות*
    (מה שנמדד כאן) לבין *חיתוך* (שנמדד בנפרד, ר' `peak>1`)."""
    total = sum(signals)
    scale = np.max(np.abs(total))
    return total / scale * peak if scale else total


def demodulate(iq, iq_rate, chunk=24_000, **demod_kwargs):
    """מריץ IQ דרך שרשרת ה-DSP **האמיתית** (`rsp_fm.NfmDemodulator`) ומחזיר
    `(סטייה ב-Hz, המדמודלטור)`. ה-chunking מכוון: כל באגי-המצב שנתפסו עד
    היום (overlap/DC/פאזת-מיקסר/פאזת-דצימציה) מתגלים רק בגבולות-chunk."""
    import rsp_fm                      # מיובא כאן כדי ש-conftest יסדר sys.path
    raw = to_u8(iq)
    demod = rsp_fm.NfmDemodulator(iq_rate=iq_rate, audio_rate=48_000,
                                  **demod_kwargs)
    pcm = b"".join(demod.process(raw[i:i + chunk * 2])
                   for i in range(0, len(raw), chunk * 2))
    return pcm_to_deviation(np.frombuffer(pcm, dtype="<i2")), demod


def symbol_error_rate(symbols, dev_hz, max_lag=60):
    """שיעור שגיאות-סמל מול הסדרה ששודרה. השהיית הפילטר לא ידועה מראש,
    ולכן נבחר ה-lag הטוב ביותר (כמו סנכרון-סמלים אמיתי בצד הקליטה)."""
    n = min(len(symbols) - 40, len(dev_hz) // 10 - 20)
    recovered, _phase, _err, _taken = best_slice(dev_hz[200:], n)
    return min(
        float(np.mean(recovered[:min(len(recovered), len(symbols) - lag)]
                      != symbols[lag:lag + min(len(recovered),
                                               len(symbols) - lag)]))
        for lag in range(max_lag))


def random_symbols(n=4000, seed=7):
    return np.random.default_rng(seed).integers(0, 4, n)
