# CLAUDE.md — מדריך הפרויקט ל-Claude (וכל מפתח)

מסמך זה הוא מקור-האמת לארכיטקטורה, למוסכמות ולזרימת העבודה של **DMR**. קרא אותו
לפני כל פיצ'ר או תיקון. כשמוסיפים יכולת מהותית או משנים ארכיטקטורה — **עדכן גם
את המסמך הזה** (וגם את `README.md` ו-`CHANGELOG.md`).

> שפה: הקוד, התיעוד והממשק בעברית (עם מונחים טכניים באנגלית). שמור על הסגנון הזה.
>
> **מוצא:** הפרויקט שוכפל מ-**AIR-AM** (האזנת תעופה). כל הסקאפולד (SDR-אחד-בהחלפה,
> מתזמר-web, boot-restore, listener→jsonl, scan, roster, PWA, מוקי-בדיקות) ירש
> ממנו כמעט מילה-במילה; רק לוגיקת התחום הוחלפה מ-תעופה ל-DMR/DSD-FME.

---

## 1. מהות הפרויקט

**DMR** הופך **Raspberry Pi 5 + SDRplay RSP1B** לתחנת פענוח **רשתות DMR** (במיוחד
**Motorola Capacity Plus / Cap+**) שנשלטת **כולה מהטלפון בדפדפן**. הפענוח ב-**DSD-FME**
מקומית. מטרות: (1) שליטה מלאה מהטלפון; (2) zero-config (`install.sh` יחיד); (3) headless
ועמיד (שורד reboot/ניתוק USB/קריסה); (4) פרטי-מקומי (בלי ענן); (5) בטיחות בלי חיכוך
(משתמש לא-root + sudoers ממוקד, PIN אופציונלי).

---

## 2. הממצא המכריע: DSD-FME אינו "API-first"

בניגוד ל-acarsdec/dumpvdl2 (AIR-AM) שפולטים JSON נקי על UDP, **DSD-FME הוא מפענח
TUI/מקלדת** — הפלט טקסטואלי והשליטה בהקשות. הפתרון: `webtune/dsd_pty.py` הוא **מתאם**
שהופך אותו ל-API-first:

```
אנטנה ─►RSP1B─USB─► sdrplay.service (SDRplay API)
                          │
        ┌─────────────── dmr-dsdfme.service ───────────────┐
        │  dsd_pty.py (ExecStart):                          │
        │    ├─ rsp_tcp   (גשר SDRplay→rtl_tcp) ← תהליך-בן  │   מחזיק את ה-SDR
        │    └─ DSD-FME   (תחת PTY, מעקב טראנקינג)          │   מתחבר ל-rsp_tcp
        │         │ טקסט → parse_dsd_line() → JSON          │
        └─────────┼─────── UDP 5555 ───────────────────────┘
                  ▼
        dmr-web.service :8080  (Flask, המתזמר)  ── REST/JSON ──► דף הבקרה (PWA)
                  │  _dmr_listener → _normalize_dsd → dmr.jsonl
```

**SDR אחד, בהחלפה:** ל-RSP1B ניגש תהליך אחד בכל רגע. `dmr-dsdfme` הוא צרכן ה-SDR
**היחיד** (rsp_tcp רץ כתהליך-בן שלו, לכן יחידת systemd אחת = צרכן אחד, כמו rtl_airband
ב-AIR-AM). **אף צרכן אינו enabled** — `dmr-web` (שעולה תמיד) קורא `state.json` באתחול
ומשחזר את המצב השמור (`_boot_restore`) => המצב שורד reboot, כולל `off`. כישלון כניסה
נופל ל-`off` (`_fail_to_off`), **לעולם** לא "מצב ברירת מחדל".

---

## 3. מבנה המאגר (file-by-file)

```
install.sh                  # מתקין-על יחיד. אידמפוטנטי (build-signature פר-רכיב).
VERSION · CHANGELOG.md · README.md · CLAUDE.md

webtune/
  app.py                    # ★ הליבה: Flask. מצבים (dmr/off/scan), listener, נרמול,
                            #   מערכות, אליאסים(join), רוסטר, REST, boot-restore, הקלטות.
  dsd_pty.py                # ★ המתאם: DSD-FME תחת PTY + rsp_tcp → parse_dsd_line → UDP JSON.
                            #   הרצה ידנית: python3 dsd_pty.py --selftest
  aliases.py                # שמות TG/RID: ייבוא CSV (RadioID.net) + עריכות ידניות (join).
  dsd_export.py             # ייצוא CSV(BOM)/JSON לפיד.
  static/
    index.html              # ה-UI כולו (HTML+CSS+JS inline). PWA. 2 תצוגות: 🏠 בית + 📻 שיחות.
    manifest.webmanifest · sw.js · icon-*.png · apple-touch-icon.png
    vendor/leaflet/         # Leaflet vendored (למפת LRRP ב-Phase 3; בלי CDN).

config/
  dmr.env                   # ברירת-מחדל ל-DSD-FME (EnvironmentFile). ⚠ נדרס ע"י app.py.
  channelmap.csv            # מפת LCN→תדר (Hz) לדוגמה. ⚠ נדרס ע"י app.py בכל מעבר.

systemd/
  sdrplay.service           # שירות SDRplay API. enabled.
  dmr-dsdfme.service        # צרכן ה-SDR (DSD-FME+גשר). Requires+PartOf=sdrplay. *לא* enabled. root.
  dmr-web.service           # שרת הבקרה + המתזמר. enabled. User=dmr (לא-root).

scripts/dmr-wait-sdrplay    # שער מוכנות (ExecStartPre): מחכה שה-API יענה, מרים sdrplay אם תקוע.
udev/99-dmr.rules           # חיבור RSP1B (Vendor 1df7) → restart אוטומטי ל-sdrplay.
tests/                      # pytest (SDR/systemd ממוקפים). 71 בדיקות. ראה §7.
.github/workflows/ci.yml    # pytest + bash -n על install.sh ו-dmr-wait-sdrplay.
```

---

## 4. נתיבי runtime על ה-Pi (לא במאגר)

| נתיב | תוכן | נכתב ע"י |
|------|------|----------|
| `/opt/dmr/webtune/` | הקוד הפרוס | install.sh |
| `/etc/dmr/dmr.env` | הגדרות DSD-FME חיות (תדר בקרה **ב-Hz**, CC, נתיב מפה) | app.py בכל מעבר DMR |
| `/etc/dmr/channelmap.csv` | מפת LCN→תדר (**Hz**) | app.py בכל מעבר DMR |
| `/etc/dmr/rid.csv` · `tg.csv` | ייבוא אליאסים (RadioID.net) | המשתמש |
| `/etc/dmr/dmr-web.env` | env אופציונלי (PIN, תמלול) — `EnvironmentFile=-` | install.sh / ידני |
| `/var/lib/dmr/state.json` | מצב אחרון (app_mode: dmr/off/scan, system, scan_plan) | app.py |
| `/var/lib/dmr/systems.json` | מערכות DMR (נערכות מה-UI) | app.py |
| `/var/lib/dmr/aliases.json` | עריכות אליאס ידניות | aliases.py |
| `/var/lib/dmr/dmr.jsonl` | היסטוריית שיחות (retention 8000) | _dmr_listener |
| `/var/lib/dmr/activity.jsonl` | יומן הקלטות | _activity_watcher |
| `/var/lib/dmr/recordings/` | per-call WAV (400 קבצים / 400MB) | DSD-FME, נמחק ע"י app.py |

---

## 5. `webtune/app.py` — מפת הקוד

- **`_guard` (before_request):** Origin==Host (CSRF/DNS-rebind) + PIN אופציונלי (`DMR_PIN`).
  כל route משנה-מצב עובר דרכו.
- **מצב DMR:** `render_dmr_env`/`write_dmr_env` (⚠ **MHz→Hz**) + `render_channelmap`/
  `write_channelmap` (⚠ **MHz→Hz**) → `_enter_dmr` (write → `systemctl restart dmr-dsdfme`
  → poll לקריסה מאוחרת). `_enter_standby` (עוצר את הצרכן, משאיר sdrplay). `_fail_to_off`
  (כישלון ⇒ standby + state off+prev_mode + payload 500). `MODE_SERVICE`/`_live_mode`.
- **★ `_normalize_dsd(m)`:** הלב — ממיר אירוע DSD-FME (dict מ-dsd_pty) לכרטיס שיחה אחיד:
  `{t, proto, freq, slot, cc, lcn, tg, tg_alias, src, src_alias, tgt, call_type, category,
  group, encrypted, enc{alg,alg_name,key_id}, ber, level, dur, event, lat, lon, text, wav}`.
  **לעולם לא ממציא מדד:** `ber`/`level` רק אם DSD-FME הדפיס. אליאסים ב-join מ-`aliases.py`.
  מיפוי ALG hex→שם דרך `DMR_ALG_NAMES` — **תג בלבד, לא פענוח**.
- **listener + jsonl:** `_dmr_listener` (thread, UDP 5555, dedup המשך-שיחה לפי tg+src+slot),
  `_append_jsonl_log`/`_trim_jsonl_log`/`_read_dmr_log`/`_load_dmr_history`, `_today_start`/
  `_day_bounds` (ארכיון יומי, עמיד DST). רץ תמיד ברקע (גם ב-standby).
- **מערכות DMR:** `DEFAULT_SYSTEMS`/`_validate_systems`/`load_systems`/`_find_system`.
  מערכת = `{id, name, control(MHz), color_code, channelmap:[{lcn,freq(MHz)}]}`.
- **scan (סבב בין מערכות):** `_validate_scan_plan` (רגל = `{system, dwell_sec, active_from?,
  active_to?}`), `_leg_active_now`, `_scan_enter_leg`, `_scan_loop`/`_scan_activate`/
  `_scan_stop_thread` — thread שמסתובב, נועל TUNE_LOCK רק במעבר; כשל-כל-הרגלים ⇒ off.
- **רוסטר:** `_dmr_identity` (RID קודם, אחרת TG) + `_build_roster` (היתוך, כולל אילו
  TG-ים כל RID דיבר — בסיס לגרף RID↔TG של Phase 3). חי בכל מצב.
- **הקלטות:** `_activity_watcher`/`_sweep_recordings` (retention), `_transcribe_worker`
  (whisper אופציונלי), `/recordings/<name>`.
- **`_boot_restore`** (thread ב-startup) + `__main__` (listener + watchers + `app.run(threaded=True)`).

---

## 6. REST API

| Method | Route | תיאור |
|--------|-------|------|
| GET | `/api/state` | מצב + `mode_ok` + systems + version + alg_names |
| GET/PUT | `/api/systems` | מערכות DMR (עריכה על הסט המלא) |
| GET/PUT | `/api/aliases` | אליאסים TG/RID (GET=מיזוג+ספירות, PUT=עריכות ידניות) |
| GET | `/api/health` | בריאות + `calls_today` + `last_call_at` ("האם אני מפענח") |
| POST | `/api/mode` | **מעבר מצב** dmr/off/scan. דרך `_guard`. כישלון ⇒ off + 500 |
| GET | `/api/scan` | סטטוס סבב (רגל, ספירה לאחור) |
| GET | `/api/dmr` | שיחות (היום; `?all=1`; `?day=YYYY-MM-DD` ארכיון; `?since=` cursor) |
| GET | `/api/dmr/export?format=csv\|json` | ייצוא (CSV עם BOM) |
| GET | `/api/roster` (·`/api/aircraft`) | רוסטר RID/TG מאוחד — חי בכל מצב |
| GET | `/api/activity` | הקלטות אחרונות |
| GET | `/recordings/<name>` | קובץ WAV |
| GET | `/api/power` | מתח/טמפ' ה-Pi |

**כלל:** כל route משנה-מצב = `POST` + `_guard` + `TUNE_LOCK`.

---

## 7. בדיקות (ללא חומרה)

`python -m pytest tests/ -v` (71 בדיקות). SDR/systemd ממוקפים דרך fixtures ב-`conftest.py`:
`paths` (מפנה נתיבי-מודול ל-`tmp_path`), `sysctl` (Recorder ל-`_sysctl` + מוקי
`_is_active`/`_sdr_present`), `no_sleep`. פונקציות טהורות (`parse_dsd_line`, `_normalize_dsd`,
`render_dmr_env`, `_validate_*`) נבדקות ישירות; Flask דרך `app.app.test_client()`.

קבצים: `test_dsd_normalize` (הלב — parse + normalize), `test_mode`, `test_boot`,
`test_scan`, `test_aliases`, `test_recordings`, `test_security`, `test_archive`.
**הוסף בדיקה לכל שינוי backend.** CI: pytest (Python 3.11) + `bash -n`.

---

## 8. מוסכמות וגוצ'אות (קרא לפני שינוי)

- **SDR אחד בהחלפה:** צרכן אחד בכל רגע. `off` משחרר; אף צרכן לא enabled; `_boot_restore` משחזר.
- **⚠ MHz בכל מקום חוץ מ-env/channelmap:** state/UI/systems/API עובדים ב-**MHz**;
  `render_dmr_env`/`render_channelmap` הם **המקומות היחידים** שממירים ל-**Hz** (DSD-FME/rigctl).
  אל תערבב (בדיוק כמו כלל ה-VDL2-Hz ב-AIR-AM).
- **לעולם לא ממציאים מדד:** `ber`/`level` רק אם DSD-FME הדפיס. הצפנה = **תג בלבד**
  (ALG/key-id) — אין פענוח בלי מפתח.
- **DSD-FME הוא ה"מתאם" היחיד תלוי-פורמט:** אם גרסת DSD-FME משנה ניסוח פלט — מתקנים
  **רק** ב-`dsd_pty.parse_dsd_line` (ונבדק ב-`test_dsd_normalize`). שאר הקוד צורך JSON נקי.
- **rsp_tcp כתהליך-בן:** dsd_pty מריץ אותו => יחידת systemd אחת = צרכן-SDR אחד (מודל
  ה-standby/PartOf של AIR-AM נשמר). אל תפצל ל-unit נפרד בלי לעדכן את `_enter_standby`.
- **gain של SDRplay הפוך:** ערך קטן = רווח גדול (רלוונטי אם מוסיפים בקרת gain).
- **בידוד + כתיבה אטומית:** `_atomic_write` לכל env/state/channelmap. `threaded=True` ל-Flask.
- **עברית ב-RTL** ב-UI; CSV עם BOM ל-Excel.

---

## 9. צ'קליסט: פיצ'ר / באג

1. הבן את ההקשר (§2 ארכיטקטורה + הבלוק הרלוונטי ב-§5).
2. שנה במקום הנכון: פרסור DSD-FME → `dsd_pty.py`; לוגיקת שרת/נרמול → `app.py`;
   אליאסים → `aliases.py`; UI → `static/index.html`; פריסה → `install.sh`+`systemd/`.
3. שמור על המודל: SDR-אחד, `_guard`/sudoers, כתיבה אטומית, MHz↔Hz רק ב-render_*.
4. הוסף/עדכן בדיקות (`tests/`, מקף SDR/systemd). ודא `pytest` ירוק.
5. עדכן `VERSION` (SemVer) + שורות ב-`CHANGELOG.md` תחת `[Unreleased]`.
6. עדכן `README.md`/`CLAUDE.md` אם ההתנהגות/הארכיטקטורה משתנות.
7. commit + push לענף המיועד (הודעות בעברית, תיאוריות).

---

## 10. מפת דרכים (שלבים)

- **Phase 1 (הושלם):** יסוד קצה-לקצה — מתאם DSD-FME, מצב DMR+טראנקינג, פיד+ארכיון,
  מערכות, אליאסים, רוסטר, הקלטות, בריאות, UI, install/systemd, בדיקות.
- **Phase 2:** ניתוח הצפנה (היסטוגרמת ALG, %מוצפן פר-TG) + אנליטיקת תעבורה (heatmap,
  air-time/TG). נגזר מ-`dmr.jsonl` הקיים.
- **Phase 3:** גרף RID↔TG (who-talks-to-whom, מעל הרוסטר) + מפת GPS/LRRP (Leaflet כבר
  vendored). מותנה בזמינות LRRP סטנדרטי ברשת (לא Motorola proprietary).
