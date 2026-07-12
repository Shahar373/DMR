# Changelog

כל השינויים המהותיים לפרויקט מתועדים כאן. הפורמט מבוסס על
[Keep a Changelog](https://keepachangelog.com/he/1.0.0/), והגרסאות עוקבות אחר
[SemVer](https://semver.org/lang/he/).

## [Unreleased]
### Fixed — מעבר ל-dsd-neo (נתפס בהרצה ראשונה על חומרה אמיתית, Pi5+RSP1B)
ההרצה הראשונה על חומרה אמיתית חשפה שתי שכבות של תקלות ב-`lwvmobile/dsd-fme`:

- **שלב 1 (נתפס):** `DSD_FME_BRANCH` הצביע ל-`main`, שלא קיים בכלל בריפו
  (הענף הפעיל האמיתי הוא `audio_work`) — `git clone` נכשל מיד בשלב 5.
- **שלב 2 (נתפס אחרי תיקון שלב 1, מ-journalctl של `dmr-dsdfme.service`):**
  גם עם הענף הנכון, `dmr-dsdfme.service` קרס מיד (`exit 1`). בדיקה של הפקודה
  שבאמת רצה חשפה `build_command()` שבור: דגל `‎-C` כפול (בלוק `DSD_COLOR_CODE`
  ישן הזריק `‎-C` בטעות — ל-DMR *אין* דגל CLI לצבע-קוד, הוא מזוהה מהסנכרון
  בפועל) ודגל `‎-c <תדר>` שלא קיים בכלל באף fork (אומת מול תיעוד CLI מלא).
  חקירה נוספת גילתה בעיה עמוקה יותר: `lwvmobile/dsd-fme` **לא תומך כלל** בקלט
  `‎-i rtltcp:` (רק `‎-i tcp:` הקנייני של SDR++ ו-`‎-i rtl:` USB מקומי) — כל
  ארכיטקטורת הגשר `rsp_tcp` מ-Phase 1 נשענה על הנחה שגויה.
  **הפתרון:** מעבר ל-`arancormonk/dsd-neo` (v2.3.0) — fork פעיל ומתועד היטב
  שתומך `‎-i rtltcp:host:port` במפורש (מתאים בדיוק לגשר `rsp_tcp` הקיים,
  שנשמר ללא שינוי) וגם SoapySDR ישיר (לא מנוצל כרגע — שיפור ארכיטקטוני עתידי
  מתועד, לא יושם תחת לחץ זמן). תוקן: `install.sh` (בלוק בנייה חדש עם
  `ninja-build`/`libfftw3-dev`/`libblas-dev`/`liblapack-dev`/`gfortran`/
  `libssl-dev`/`libcurl4-openssl-dev` שדסק-נאו דורש, `cmake --install`),
  `dsd_pty.build_command` (הוסר `‎-C` הכפול ו-`‎-c`; `‎-6 <dir>` (WAV רציף)
  הוחלף ב-`‎-7 <dir> -P` הנכון (WAV per-call), `DSD_BIN` ברירת מחדל `dsd-neo`).
  תדר ערוץ הבקרה כבר לא דגל CLI — `app.render_channelmap` מזריק אותו כשורת
  "בוא לכאן קודם" ראשונה בקובץ ה-channelmap עצמו (ערוץ-דמה `999`, לפי מוסכמת
  התיעוד הרשמי של dsd-neo).
- **נוסף:** `dsd_pty._run` היה בולע את הפלט הגולמי של DSD-FME/`rsp_tcp` (רק
  אירועים מפורסרים נשלחו הלאה) — שגיאות/קריסה של המפענח לא הגיעו ל-
  `journalctl -u dmr-dsdfme` כלל, מה שהקשה מאוד על האבחון בפועל. כעת כל שורת
  פלט גולמית מהודהדת ל-stderr (=> journal), ו-`rsp_tcp` יורש stdout/stderr
  במקום `DEVNULL`.

## [0.3.0] - 2026-07-12
### Changed — פרסור DSD-FME מבוסס קליטה אמיתית (לא ניחוש)
עד גרסה זו `dsd_pty.parse_dsd_line` היה בנוי על ניחוש סביר של פורמט הפלט של
DSD-FME. בגרסה זו הוא **נכתב מחדש מול קליטה אמיתית** — 20,000 שורות מרשת
Motorola Capacity Plus רב-אתרית (SLCO) — ואומת ב-replay מלא: **כל אחת מ-68
הצורות הייחודיות** בקליטה (`tests/fixtures/capplus_slco_sample.csv`) מסווגת
נכון (100% התאמה, כולל housekeeping שנופל).

- **ממצא מרכזי:** ~80% מהפלט האמיתי הוא רעש תפעולי (`lsn_status`/
  `channel_status`/`site_info`/`ip_mapping`/`bank_call`/`preamble_csbk`) —
  `parse_dsd_line` כעת **מטיל את אלה החוצה במקור** (מחזיר `None`, לא נשלח
  ב-UDP כלל) במקום להציף את `dmr.jsonl`.
- **תיקון סמנטי:** אין שדה TG נפרד בפרוטוקול DMR — בשיחת קבוצה `TGT` *הוא*
  ה-talkgroup עצמו; בשיחה פרטית `TGT` נשאר יעד. `_normalize_dsd` תוקן בהתאם.
- **הצפנה:** DSD-FME לא הדפיס בקליטה שנבדקה שם אלגוריתם/מזהה מפתח (FLCO/FID
  הם routing fields, לא ALG/KEY) — אירוע `encryption` **מתואם** (לא כרטיס
  עצמאי) לשיחת הקול הפתוחה באותו slot (`_slot_open_call`, חלון 15ש'), ומסמן
  `encrypted=True` בלבד. לעולם לא ממציאים שם אלגוריתם.
- **תדר:** DSD-FME לא מדפיס תדר בקליטה אמיתית — `freq` על כרטיס נגזר כעת
  מ-channelmap המערכת הפעילה (`_channelmap_freq`, לפי LCN/Rest-LSN), לא מטקסט.

### Added — איכות RF (תדירות שגיאות) + נוד-רווח חי
- **`_rf_quality_tick`/`_rf_quality_snapshot`:** DSD-FME לא נותן שום SNR/RSSI/
  dBFS רציף — רק אירועי CRC/FEC בודדים (`SLCO_CRC`/`CACH_BURST_FEC`/
  `CSBK_CRC`/`CSBK_FEC`, וגם פריימי-קול עם `(CRC ERR)`). חלון נגלל של 60
  שניות סופר תדירות שגיאות אמיתית (`errors_per_min` + פילוח לפי סוג) —
  **לא ממציאים יחס/SNR**. `GET /api/rf`.
- **נוד-רווח חי (`dsd_pty.send_gain_nudge`):** DSD-FME תומך בהקשות מקלדת
  `g`/`G` ("Manually decrease/increase RTL gain") בזמן ריצה, שנשלחות דרך
  ה-unix socket הקיים (`DSD_CTRL_SOCK`) — **בלי לעצור את DSD-FME ובלי פטצ'
  קוד C**. מונה יחסי best-effort ב-state (`gain_nudge`, מתאפס בכל
  `_enter_dmr`) — אין readback אמיתי מ-DSD-FME. `POST /api/gain
  {direction: up|down}`.
- **UI:** כרטיס "📶 איכות RF ובקרת רווח" בבית — תדירות שגיאות + פילוח לפי
  סוג + כפתורי נוד-רווח, עם הבהרה מפורשת שאין מד dBFS/SNR רציף.
- **נדחה במכוון (Phase עוקב):** מד dBFS עצמאי מה-SDR עצמו דורש פטצ' קוד C על
  `rsp_tcp` (SDRplay API הוא single-owner; אין side-channel telemetry
  בגרסה הסטנדרטית) — לא ניתן לאמת בלי חומרה אמיתית (RSP1B). ר' CLAUDE.md §8.

### Tests
- `tests/fixtures/capplus_slco_sample.csv` — 68 הצורות הייחודיות מהקליטה
  האמיתית (type+pattern), עם replay-test שמוודא 68/68 סיווג מדויק.
- `tests/test_dsd_normalize.py` נכתב מחדש (32 בדיקות) מול הפורמט האמיתי.
- `tests/test_rf_gain.py` (7 בדיקות) — שכבת ה-HTTP של `/api/rf`/`/api/gain`.
- סה"כ 106 בדיקות (היו 85).

## [0.2.0] - 2026-07-12
### Added — Phase 2: אנליטיקה (הצפנה + תעבורה)
- `_encryption_stats`: היסטוגרמת אלגוריתמי הצפנה (ALG) + %מוצפן פר-talkgroup —
  לעולם לא מפענח, רק מסכם את התג (`encrypted`/`enc.alg_name`) הקיים בכל כרטיס.
- `_traffic_stats`: air-time + מספר שיחות פר-TG, והתפלגות שעתית (0–23, שעון
  מקומי) לזיהוי שעות עומס.
- `GET /api/analytics/encryption` ו-`GET /api/analytics/traffic` — אותם
  פרמטרים כמו `/api/dmr` (`?day=`/`?all=1`, ברירת מחדל: היום).
- UI: כרטיסיית **📊 ניתוח** חדשה — היסטוגרמת ALG, בר מוערם ברור/מוצפן פר-TG
  (עם legend), heatmap שעתי (24 תאים, גוון accent יחיד — sequential), ורשימת
  TG-ים מובילים לפי זמן-שידור. טוקני `--st-good`/`--st-critical` עברו ולידציה
  מול `validate_palette.js` (skill dataviz) לפני שימוש.

### Added — Phase 3: רשת ומיקום (גרף RID↔TG + מפת LRRP)
- `_rid_tg_graph`: צירי source-RID→talkgroup ממושקלים במספר שיחות (who-talks-
  to-whom), רק שיחות קבוצה. `GET /api/analytics/graph`.
- `_lrrp_snapshot`: מיקום אחרון-ידוע פר-RID מאירועי LRRP שבזיכרון (בהשראת
  `adsb.aircraft_snapshot` ב-AIR-AM — "עכשיו" בלבד, לא ארכיון). `GET /api/positions`.
- UI: רשימת גרף RID↔TG מדורגת לפי משקל, ומפת Leaflet (vendored, lazy-load)
  שמציגה marker לכל RID עם מיקום LRRP ידוע. ריק בשקט כשהרשת לא שולחת LRRP
  סטנדרטי (Motorola proprietary אינו מפוענח ע"י DSD-FME).

### Fixed
- **RTL:** ה-UI כולו היה חסר `<html lang="he" dir="rtl">` (נשמט ב-Phase 1) —
  התגלה בבדיקת Playwright חזותית של מסך האנליטיקה. תוקן; משפיע על כל התצוגות.
- **מעבר לתצוגת שיחות** לא רינדר מיידית הודעות שכבר נטענו לזיכרון — המתין
  לטיק ה-polling הבא (עד 3 שניות). `showView("calls")` מרנדר+מרענן כעת מיידית
  (אותה תבנית שכבר הייתה קיימת למעבר לתצוגת אנליטיקה).

### Tests
- `tests/test_analytics.py` (14 בדיקות): `_encryption_stats`/`_traffic_stats`/
  `_rid_tg_graph`/`_lrrp_snapshot` + כל 4 ה-routes החדשים, כולל ולידציית `?day=`.
  סה"כ 85 בדיקות.

## [0.1.0] - 2026-07-12
### Added — Phase 1: יסוד עובד קצה-לקצה
הפרויקט נוצר משכפול ארכיטקטורת **AIR-AM** והחלתה על פענוח **רשתות DMR** (במיוחד
Motorola Capacity Plus) באמצעות **DSD-FME**, על Raspberry Pi 5 + SDRplay RSP1B,
בשליטה מלאה מהטלפון בדפדפן.

- **מתאם DSD-FME → JSON (`webtune/dsd_pty.py`):** מריץ את DSD-FME תחת PTY ואת גשר
  `rsp_tcp` (SDRplay→rtl_tcp) כתהליך-בן; מפרסר את הפלט הטקסטואלי (`parse_dsd_line`)
  ושולח כל אירוע כ-JSON ב-UDP — הופך את DSD-FME ל"API-first" כמו `acarsdec -j`.
- **שרת הבקרה (`webtune/app.py`):** מצב `dmr` (DSD-FME) / `off` (standby) / `scan`
  (סבב בין מערכות) — SDR אחד בהחלפה, מתזמר-web, boot-restore, כישלון-נופל-ל-off.
  `_normalize_dsd` ממיר אירוע DSD-FME לכרטיס שיחה אחיד (TG/RID/slot/CC/enc/BER);
  listener על UDP 5555 → `dmr.jsonl` (retention + ארכיון יומי + ייצוא CSV/JSON).
- **מערכות DMR (`systems`):** פריסטים של רשתות (תדר בקרה + color code + מפת LCN),
  נערכים מהטלפון, נשמרים ב-`systems.json`.
- **אליאסים (`webtune/aliases.py`):** שמות ל-TG/RID מייבוא CSV (RadioID.net user.csv)
  + עריכות ידניות מהטלפון; join אוטומטי בכל שיחה.
- **רוסטר מאוחד:** היתוך שיחות לפי RID/TG (בסיס לגרף RID↔TG של Phase 3), חי בכל מצב.
- **הקלטות per-call:** DSD-FME כותב WAV לכל שיחה; watcher + retention + נגן בדפדפן;
  תמלול whisper אופציונלי (כבוי כברירת מחדל, `INSTALL_DMR_WHISPER=1`).
- **פריסה:** `install.sh` (SDRplay API, SoapySDRPlay3, mbelib, DSD-FME, rsp_tcp,
  משתמש `dmr` לא-root + sudoers ממוקד), יחידות systemd (`sdrplay`/`dmr-dsdfme`/
  `dmr-web`), `dmr-wait-sdrplay`, `udev/99-dmr.rules`.
- **UI (`static/index.html`):** PWA בעברית/RTL — בית (מצבים, עורך מערכות, אליאסים,
  רוסטר, בריאות "האם אני מפענח") + תצוגת שיחות (פיד חי, פילטרים, ארכיון, ייצוא, נגן).
- **בדיקות (`tests/`):** 71 בדיקות, SDR/systemd ממוקפים — נרמול (parse_dsd_line +
  `_normalize_dsd`), מעברי מצב, boot-restore, סריקה, אליאסים, הקלטות, אבטחה, ארכיון.
  CI: pytest (Python 3.11) + `bash -n` על install.sh ו-dmr-wait-sdrplay.

### שלבים עוקבים (מתוכננים)
- **Phase 2:** ניתוח הצפנה (היסטוגרמת ALG, %מוצפן פר-TG) + אנליטיקת תעבורה (heatmap).
- **Phase 3:** גרף RID↔TG (who-talks-to-whom) + מפת GPS/LRRP (Leaflet).

[Unreleased]: https://github.com/Shahar373/DMR/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Shahar373/DMR/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Shahar373/DMR/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Shahar373/DMR/releases/tag/v0.1.0
