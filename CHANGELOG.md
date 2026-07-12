# Changelog

כל השינויים המהותיים לפרויקט מתועדים כאן. הפורמט מבוסס על
[Keep a Changelog](https://keepachangelog.com/he/1.0.0/), והגרסאות עוקבות אחר
[SemVer](https://semver.org/lang/he/).

## [Unreleased]

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

[Unreleased]: https://github.com/Shahar373/DMR/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Shahar373/DMR/releases/tag/v0.1.0
