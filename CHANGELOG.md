# Changelog

כל השינויים המהותיים לפרויקט מתועדים כאן. הפורמט מבוסס על
[Keep a Changelog](https://keepachangelog.com/he/1.0.0/), והגרסאות עוקבות אחר
[SemVer](https://semver.org/lang/he/).

## [Unreleased]

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

[Unreleased]: https://github.com/Shahar373/DMR/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Shahar373/DMR/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Shahar373/DMR/releases/tag/v0.1.0
