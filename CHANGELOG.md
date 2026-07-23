# Changelog

כל השינויים המהותיים לפרויקט מתועדים כאן. הפורמט מבוסס על
[Keep a Changelog](https://keepachangelog.com/he/1.0.0/), והגרסאות עוקבות אחר
[SemVer](https://semver.org/lang/he/).

## [Unreleased]

## [0.7.1] - מנוע ה-multi אומת על חומרה + הקשחת ספייק נגד יתומים

**מצב `multi` הורץ בהצלחה על Pi 5 + RSP1B אמיתי** (מערכת `multi_164cluster`,
6 ערוצים, 120ש') — הסיכון הטכני המרכזי הפתוח מ-Phase 7 נסגר.

### אומת על חומרה (spike-dmr-multi)
- **rsp_tcp שרד קליטה רחבת-פס 672kHz** (span 618.75kHz + guard, פי ~2.8
  מ-240kHz החד-ערוצי) — זה היה החשש המרכזי (ר' CLAUDE.md §10 Phase 7).
- **6 מפענחי dsd-fme במקביל** נשארו חיים את כל 120 השניות.
- **CPU נוח:** 133% ממוצע / 154% שיא מתוך 400% (המון headroom).
- **תיוג phys_lcn עובד** — 1318 אירועים הגיעו מתויגים ב-UDP (phys_lcn 3/4).
- **סייג כן:** רק 2/6 ערוצים ייצרו אירועים בחלון הזה, ורובם `quality`/
  `encryption` (לא `voice_call`) — כיסוי-קול על *כל* הערוצים דורש חלון-שדה
  ארוך יותר. המנוע הוכח; שאלת התעבורה-בזמן נשארת לשטח.

### Fixed
- **`scripts/spike-dmr-multi`: טאטוא-יתומים ב-preflight.** ריצה שנקטעה
  (Ctrl+C/קריסה חלקית) השאירה `dsd_pty`/`rsp_tcp`/`rsp_fm`/`dsd-fme` חיים
  מחזיקים את ה-SDR, והריצה הבאה מתה ב-bring-up (`t≈9s`, 0 אירועים) כי
  rsp_tcp לא יכול לפתוח SDR תפוס — בדיוק כלל "SDR אחד בהחלפה" (§8). זה עלה
  שתי ריצות-סרק לפני שאובחן. עכשיו הספייק הורג יתומים (full-cmdline, כי
  rsp_fm/dsd_pty רצים כ-python3) לפני שהוא מתחיל.

## [0.7.0] - שילוב UI מלא למצב multi (Phase A) + תיקוני-UI קטנים

מ-בדיקה חזותית אמיתית (Playwright, לפי CLAUDE.md §7) של ה-UI מול נתונים
מדומים ב-multi — נמצאו ותוקנו 3 באגים אמיתיים (לא רק שיפורי-נראות) + נבנתה
תצוגה ייעודית פר-ערוץ שהמשתמש ביקש מפורשות. `multi` היה קיים ב-backend
מ-v0.6.0/v0.6.2 אבל ה-UI כמעט לא ידע להציג אותו (label שגוי, פיל-בריאות
אדום-שגוי, בלי לוח-ערוצים) — זה הפער שהפאזה הזו סוגרת.

### Fixed
- **`api_health()` קרס `multi` בחזרה ל-"dmr" (אותו דפוס-באג כמו ב-`_live_mode()`
  שתוקן קודם, ב-פונקציה נפרדת עם לוגיקה משוכפלת).** `dmr`/`multi` חולקות
  יחידת systemd אחת, כך ש-`systemctl is-active` לבדו לא יכול להבדיל ביניהן —
  `mode = "dmr" if dmr_active else saved` דרס כל `multi` בחזרה ל-`"dmr"` ברגע
  שהשירות פעיל. תוקן להשתמש ב-`MODE_SERVICE` כמקור-אמת (כמו `_live_mode`).
  נתפס ע"י צילום-מסך אמיתי שהראה פיל "תקלה" אדום במצב multi תקין.
- **`/api/dmr/export` התעלם מ-`?day=`** — לחיצה על ייצוא CSV בזמן צפייה בארכיון
  של יום מסוים ייצאה בפועל את *כל* ה-`dmr.jsonl`, לא רק את היום המוצג. עכשיו
  מסנן לפי `_day_bounds` בדיוק כמו `/api/dmr?day=` (בלי הפרמטר — ללא שינוי).
- **ערכת-נושא (theme toggle) חדשה עם רינדור חצי-כהה/חצי-בהיר שבור.**
  `:root[data-theme="dark"]`/`["light"]` (קוד קיים, לא-פעיל בפועל עד עכשיו כי
  שום דבר לא קבע `data-theme`) דרסו רק 3 מתוך ~10 המשתנים ש-`@media
  (prefers-color-scheme)` דורס — הרחבת הטוגל חשפה שהשאר ("יתומים") נשארו
  בערך הישן. תוקן: שני הבלוקים דורסים עכשיו את כל 10 המשתנים. גם תוקן
  הלוגיקה ב-JS: זיהוי "מצב נוכחי" באמצעות `matchMedia` בכל לחיצה במקום משתנה
  JS יציב — לא תאם תמיד את ברירת-המחדל האמיתית של הקאסקייד (כהה, לא "בהיר
  כשה-media-query הכהה לא תואם"). אומת חזותית: שני הכיוונים מרנדרים עכשיו
  עקבי לגמרי (Playwright, בדיקת פיקסלים ישירה — לא רק עין).

### Added
- **לוח-ערוצים ייעודי ב-multi (`renderChannelsBoard`) — שורה אחת לכל ערוץ**
  בבית: LCN+תדר, קריאה אחרונה (tg/alias, זמן), תדירות-שגיאות RF פר-ערוץ
  (`by_channel`, אף פעם לא dBFS/SNR). שורה לחיצה → קופצת לפיד השיחות מסונן
  לאותו ערוץ. זה מה שהמשתמש ביקש מפורשות (מעבר להצגת תדר על כרטיס שיחה בלבד).
- **תג `LCNn` על כרטיסי שיחה** (`phys_lcn`, רק ב-multi) + סינון `lcn:N`/`lcnN`
  בשדה החיפוש בפיד.
- **כרטיס-מצב "📡 רב-ערוצי"** בבית + כפתור "התחל רב-ערוצי" — כבוי כשלמערכת
  הנבחרת אין ≥2 ערוצי channelmap (אותו תנאי כמו `_validate_multi_feasible`).
- **`modeLabel()`/כותרת-משנה בבית** כוללים עכשיו ענף אמיתי ל-`multi`
  ("רב-ערוצי · N ערוצים בו-זמנית · {system}") — לפני כן נפלו לברירת-המחדל.
- **טוגל ערכת-נושא (🌓)** בסרגל-העליון + שמירה ל-`localStorage`.
- **כרטיס מתח/טמפ'** (`/api/power`, שכבר היה קיים ב-backend) — פיל אדום
  ב-undervolt/throttle *עכשיו*, כתום אם קרה בעבר, ירוק אחרת.

### בדיקות
- 187 בדיקות ירוקות (184→187): `test_api_health_multi`,
  `test_api_dmr_export_day_filter`, `test_api_dmr_export_bad_day`.
- UI: `node --check` על ה-JS המחולץ + אימות חזותי מלא (Playwright, מוק-שרת
  שמריץ את `app.py` האמיתי עם 19 מערכות הסקר + מצב multi מדומה) — צילומי-מסך
  בית/שיחות/כהה/בהיר, כולל בדיקת-פיקסלים ישירה לוודא עקביות ערכת-הנושא.

## [0.6.2] - תיקוני מצב multi (לפני אימות חומרה) + ספייק ייעודי ל-DMR

מ-code review מעמיק (3 סוכנים) לפני ההרצה הראשונה של multi על חומרה. כל
התיקונים נוגעים אך ורק במצב multi — חד-ערוצי (dmr/scan/discover) לא נגע.

### Fixed
- **#1 (גבוה) — הקלטות multi גרמו למילוי-דיסק בלי גבול והיו בלתי-נראות ב-UI.**
  DSD-FME כותב per-call WAV לתת-תיקיות פר-ערוץ (`recordings/lcnN/`), אבל כל
  ניהול-ההקלטות השתמש ב-`glob("*.wav")` לא-רקורסיבי → ה-retention (400 קבצים)
  לא ראה אותן, הן לא הופיעו ב-`/api/activity`, ולא ניתנו להגשה. תוקן: `rglob`
  בשלושת המקומות (retention/activity/תמלול), `_scan_new_recordings` שומר נתיב
  יחסי (`lcnN/foo.wav`), ו-`/recordings/<path:name>` מגיש תת-נתיבים.
- **#2 (בינוני) — `/api/gain` (נוד-רווח חי) היה מת ב-multi.** `_run_multi` לא
  פתח את `dsd-ctrl.sock` שה-`_run` החד-ערוצי פותח, אז הבקשה החזירה 500. נוסף
  ערוץ הבקרה (bind + select + forward ל-`DSD_BRIDGE_CTRL_SOCK`), זהה ל-`_run`.
- **#3 (נמוך) — `compute_wideband_plan` יכל להחזיר `iq_rate` מעל תקרת ה-2MHz.**
  בדיקת-התקרה הייתה על `span+guard` הגולמי *לפני* העיגול לכפולה של 48kHz, אז
  span שנכנס בקושי (1.99MHz) עוגל מעל התקרה (2.016MHz) ועבר בטעות. תוקן בשני
  העותקים (dsd_pty + rsp_fm): הבדיקה עכשיו על ה-iq_rate המעוגל.
- **#4 (נמוך) — LCN כפול ב-channelmap גרם לכשל bring-up אטום ב-multi.**
  `MultiChannelBridge` ממפתח מדמודלטורים לפי LCN → LCN כפול היה מפיל דמודלטור
  בשקט. `_validate_multi_feasible` דוחה עכשיו LCN כפול ב-400 עם הודעה ברורה
  (ממוקד ל-multi בלבד — לא מהדק את `_validate_systems` הגלובלי).
- **#5 (קוסמטי) — סף ניקוי `_slot_open_call`** הוגדל מ-8 ל-`2×MULTI_CHANNELS_MAX`
  (עד 2 slots × N ערוצים ב-multi), עם תיקון ההערה המטעה.

### Added
- **`scripts/spike-dmr-multi`** — ספייק חומרה שבודק את מנוע ה-multi של **DMR
  עצמו** (`dsd_pty._run_multi` + `rsp_fm.run_multi` + `rsp_tcp` רחב-פס), **לא**
  את ה-channelizer של DECREP (ש-`spike_multichannel.sh` בריפו DECREP בודק —
  נתיב DSP שונה לגמרי). מריץ את השרשרת ש-`/api/mode {"mode":"multi"}` מרים,
  ישירות ובמבודד, ומודד: שרידות `rsp_tcp` ברוחב-פס (הסיכון #1), נעילת-sync
  פר-ערוץ (אירועים מתויגי-`phys_lcn` ב-UDP), ו-CPU. זה האימות שהיה חסר.

### בדיקות
- 184 בדיקות ירוקות (177→184): הקלטות בתת-תיקיות, גבול תקרת wideband-plan,
  ו-LCN כפול. 68/68 fixture replay נשאר ירוק ללא שינוי.

## [0.6.1] - סקר-שדה אמיתי + בעלות פורטים סופית (8080/8081)

### Added
- **`config/systems.survey.json`** — 19 מערכות DMR אמיתיות ממדידת-שדה עצמאית
  (17.07.2026, VHF 162–167MHz): 16 ערוצים מאומתים (SoapySDR+SDRconnect,
  decoder+SQLite עם `integrity_check` תקין) + 3 מערכות-אשכול ל-`multi` mode.
  אומת בפועל מול `_validate_systems`/`_validate_multi_feasible`. לא נטען
  אוטומטית — `cp` ידני אל `/var/lib/dmr/systems.json`. ר' CLAUDE.md §3/§10.

### Fixed
- **בעלות פורט 8080 סוכמה סופית** בין שני הריפואים שרצים על אותו Pi:
  `dmr-web.service` נשאר 8080 (קבוע, `app.run(port=8080)` — אין מה לשנות, זה
  משטח-הבקרה שעולה תמיד ב-boot). `DMR-DECREP-SHAHAR` (ריפו-רפרנס למיזוג הזה)
  שינה את ברירת-המחדל שלו מ-8080 ל-**8081** ב-`backend/cli.py`/
  `scripts/dmr-monitor.service`/`README.md` (שם, v0.26.4) — כך שהרצה שלו לעולם
  לא תתנגש עם DMR גם בלי `--port` מפורש.

## [0.6.0] - מצב `multi`: פענוח רב-ערוצי בו-זמנית (מיזוג עם DMR-DECREP-SHAHAR)

### Added — מצב `multi`: כל ערוצי ה-channelmap בבת אחת, לא רק תדר-בקרה
מצב `app_mode` חדש (`dmr`/`off`/`scan`/`discover`/**`multi`**) שמפענח **את כל
ערוצי ה-channelmap של מערכת בו-זמנית** — לא רק את ערוץ הבקרה עם מעקב-טראנקינג
כמו `dmr`. תוצאה של מיזוג ארכיטקטורה עם `DMR-DECREP-SHAHAR` (שני פרויקטים
שוכפלו מאותו scaffold): המנוע הרב-ערוצי (channelizer דיגיטלי על קליטת IQ
רחבת-פס אחת) נשאב משם ונשתל לתוך המודל החד-SDR/PWA-עברי המוקשח של DMR.

- **`webtune/rsp_fm.py`:** `NfmDemodulator` קיבל `offset_hz` (ברירת מחדל 0.0,
  זהה byte-for-byte להתנהגות הקודמת) — מיקסר מרוכב שמסיט ערוץ מוסט מהמרכז
  המכוון חזרה ל-DC לפני שרשרת הפילטר/דצימציה/דיסקרימינטור-FM/DC-blocker
  הקיימת, בלי שינוי. `compute_wideband_plan` (טהורה): מרכז+קצב-IQ לקליטה
  רחבת-פס אחת שמכסה כל הערוצים, מעוגל **כלפי מעלה לכפולה של 48kHz** (אילוץ
  `NfmDemodulator`, לא היה מובטח בנוסחה נאיבית). `parse_channelmap_hz`,
  `MultiChannelConfig`/`MultiChannelBridge`/`run_multi` (N מדמודלטורים על
  קריאת-IQ אחת, N שרתי-אודיו). CLI: `--multi-channelmap`/`--audio-tcp-base`.
- **`webtune/dsd_pty.py`:** עותק stdlib-בלבד של `compute_wideband_plan`/
  `parse_channelmap_hz` (dsd_pty נשאר בלי תלות ב-numpy) — **חישוב יחיד** לפני
  spawn, מוזרם במפורש (`--frequency`/`--iq-rate`) גם ל-`rsp_tcp` וגם ל-
  `rsp_fm.py` (שני תהליכי-בן נפרדים שחייבים להסכים על אותו center/rate; לא
  נסמכים על שני חישובים עצמאיים). `build_multi_rsp_tcp_command`/
  `build_multi_bridge_command`/`build_channel_dsd_command` (בלי `-T`/`-U` —
  אין retune פר-ערוץ, LO משותף יחיד), `tag_event` (מתייג כל אירוע עם
  `phys_lcn`/`phys_freq_hz` אמיתיים — ground-truth מ-spawn, לא ניחוש טקסט),
  `_run_multi` (N מפענחי DSD-FME תחת PTY, כשל בכל אחד ⇒ כשל כל השירות, כמו
  חד-ערוצי; רסטרט חלקי פר-ערוץ נדחה). `_run()` מפצל ל-`_run_multi` לפי
  `DSD_MULTI=1`.
- **`webtune/app.py`:** `MULTI_GUARD_HZ`/`MULTI_MAX_SPAN_HZ`/`MULTI_CHANNELS_MAX`,
  `_validate_multi_feasible` (≥2 ערוצים, נכנס ברוחב-פס, נבדק **לפני**
  `TUNE_LOCK`). `render_dmr_env(system, multi=True)` מוסיף `DSD_MULTI=1` +
  `DSD_MULTI_GUARD_HZ`/`DSD_MULTI_MAX_RATE_HZ` (מהקבועים ש-
  `_validate_multi_feasible` כבר אימת מולם) + `DSD_AUDIO_TCP_BASE`
  (בלתי-תלוי-מצב, נכתב תמיד). `MODE_SERVICE["multi"]` = אותה יחידת systemd
  כמו `dmr` (**לא** unit נפרד — "SDR אחד בהחלפה" נשמר); `_live_mode` מבדיל
  dmr/multi לפי `state.json` (systemctl לא יכול). `api_mode`/`_boot_restore`
  מטפלים ב-`multi` באותה תבנית כמו `dmr`. `_normalize_dsd` מעדיף
  `phys_freq_hz`/`phys_lcn` (כשקיימים) על `_channelmap_freq(lcn)` — ground-truth
  לא ניחוש. `_dmr_listener`: מפתחות ה-dedup וה-`_slot_open_call` (קורלציית
  הצפנה) מורחבים ל-`(phys_lcn, ...)` — **בלי זה** שתי שיחות בו-זמנית על שני
  ערוצים שונים עם אותו slot היו מתמזגות/מקבלות תג-הצפנה בטעות מערוץ אחר.
  `_rf_ticks`/`_rf_quality_tick`/`_rf_quality_snapshot` מורחבים עם ממד
  `phys_lcn`; `/api/rf` מחזיר גם `by_channel` (החלטת-מוצר: פר-ערוץ **מיום-1**,
  לא נדחה). כל השדות/מפתחות החדשים `None`/ריקים בחד-ערוצי ⇒ **אפס שינוי
  התנהגות** לקוד הקיים.
- **בדיקות:** 37 בדיקות חדשות (140→177): offset-demod + wideband-plan +
  channelmap-parsing ב-`test_rsp_fm.py`; בוני-פקודות + `tag_event` ב-
  `test_dsd_normalize.py`; `render_dmr_env`/`_validate_multi_feasible`/
  `/api/mode multi`/`_live_mode` ב-`test_mode.py`; `phys_lcn` tagging/dedup/
  encryption-per-channel ב-`test_dsd_normalize.py`; `by_channel` RF ב-
  `test_rf_gain.py`. **68/68 ה-fixture replay של `parse_dsd_line` נשאר ירוק
  ללא שינוי אחד** — הפרסור עצמו לא נגע, התיוג קורה אחריו ב-`dsd_pty`.

### ⚠ טרם אומת על חומרה (חסימת-שטח לפני production)
שרשרת האותות הרב-ערוצית (`dsd_pty._run_multi`, `rsp_fm.run_multi`,
`RtlTcpClient`/`rsp_tcp` בקצב-IQ מעל 240kHz הקיים) היא `pragma: no cover` —
לא אומתה על RSP1B אמיתי. הסיכון הטכני המרכזי: קליטת IQ רחבת-פס (עד 2MHz)
מעל `rsp_tcp` (rtl_tcp emulation) לא נבדקה מעולם ביציבות ב-DMR (רק 240kHz
אומת, ר' Phase 5/CHANGELOG). ספייק-מדידה (`scripts/spike_multichannel.sh`
בריפו `DMR-DECREP-SHAHAR`) נכתב לבדיקה על חומרה אמיתית; אם `rsp_tcp` לא יציב
ברוחב-פס הזה, החלופה היא Soapy-direct (`rf/capture.py` ב-DECREP). ר' §5/§10
Phase 7.

### נדחה במכוון ל-Phase הבא
בחירת-ערוצים חלקית (`channel_ids`/`--follow-traffic` — יום-1 מפעיל את **כל**
ה-channelmap של המערכת), ושכבת ה-UI/`channels.json` (טבלת קונפיגורציה + live
status פר-ערוץ) — נדחו עד שהמנוע יאומת על חומרה אמיתית.

## [0.5.0] - גילוי רשתות DMR (frequency discovery)
### Added — מצב `discover`: סריקת תדרים חכמה + גילוי פרמטרים
מצב חדש שסורק טווח RF, מזהה תדרים חשודים כ-DMR (גילוי אנרגיה FFT), ואז בודק כל
מועמד עם DSD-FME ומגלה: תדר בקרה, color code, סוג ערוץ (בקרה/קול/קונבנציונלי),
LSN-ים ו-TG-ים פעילים. תוצאה: דוח גילוי + "שמור כמערכת" (מיזוג ל-systems.json).

- **`webtune/discovery.py` (מודול חדש, לוגיקה טהורה):** `validate_sweep_plan`
  (ברירת מחדל 450–470 MHz, תחום 24–1300, רוחב עד 100MHz), `build_freq_grid`
  (צעד לפי חלון שמיש ‎~1.8MHz), `detect_candidates` (סף אדפטיבי median+k·MAD עם
  מרווח-מינימום יחסי, מסכת DC spike מרכזי, crop קצוות, מיזוג בינים לפי רוחב ערוץ
  12.5kHz), `aggregate_probe` (אירועים→רשומת רשת: is_dmr/cc/channel_type/rest_lsns/
  talkgroups/rids/encrypted/confidence), `discovery_to_system`.
- **`webtune/rsp_fm.py`:** `compute_power_spectrum` (Hann→FFT→dBFS, טהור ונבדק),
  מצב `--sweep` (מדלג על ה-NFM demod, gain ידני קבוע, FFT על ה-IQ הגולמי עם
  איפוס ב-generation change), verb `SPECTRUM` בשרת ה-rigctl, `set_fixed_gain`.
- **`webtune/dsd_pty.py`:** `parse_dsd_line(..., emit_status=True)` מוסיף אירועי
  `sync` (‎`Sync: +DMR ... Color Code=NN | state`) ו-`channel_status` (Rest LSN +
  LSN states) — שדות ש-DSD-FME כבר מדפיס אך נזרקו. **ברירת המחדל (emit_status=False)
  ללא שינוי** — פרסור רגיל זהה byte-for-byte, ה-fixture replay (68/68) נשאר ירוק.
  מצב sweep ב-`_run` (רק rsp_tcp+rsp_fm, בלי DSD-FME).
- **`webtune/app.py`:** תזמור `_discover_activate`/`_discover_loop`/
  `_discover_stop_thread` (מודל scan; TUNE_LOCK per-step בלבד; job חולף בזיכרון —
  לא מַתמיד ב-state, לא משוחזר ב-boot), collector side-tap ב-`_dmr_listener`
  (מתויג-epoch, לא נוגע ב-dedup), `render_dmr_env` עם `trunk` פר-מערכת (במקום
  קבוע) + דגלי sweep/emit_status, `_probe_system` (non-trunk), `discovery.json`.
  נקודות REST: `POST /api/mode {mode:"discover"}`, `GET /api/discover`,
  `POST /api/discover/save`. `api_state`/`api_health` מדווחים discover כשפעיל.
- **UI:** תצוגת "🔎 גילוי" חדשה — טופס טווח, התקדמות חיה (שלב/progress/תדר),
  טבלת רשתות מגולות + "שמור כמערכת". הערה מפורשת על מיפוי-ערוצים חלקי.
- **בדיקות:** `tests/test_discovery.py` (33 בדיקות: טהור + Flask + e2e ממוקף),
  `compute_power_spectrum` ב-`test_rsp_fm`, אירועי sync/channel_status ב-
  `test_dsd_normalize`. סה"כ 140 בדיקות ירוקות.

> ⚠ **גבול אימות חומרה:** שרשרת האותות של הסריקה (מצב sweep ב-rsp_fm/dsd_pty,
> לקוח ה-rigctl/spectrum ב-app.py, בדיקת DSD-FME) היא `pragma: no cover` —
> מתאמתת רק על Pi 5 + RSP1B אמיתי, כמו הגשר של 0.4.0. CI מכסה את כל הלוגיקה
> הטהורה, `compute_power_spectrum`, שינויי ה-parser, ונקודות ה-Flask. הזמנים/
> הרגישות בשטח (התייצבות retune, יציבות gain, peaks אמיתיים) ייאומתו על חומרה.
>
> **מיפוי LCN↔תדר אינו ניתן לגילוי אוטומטי מלא:** Cap+ משדר LSN לוגי (לא תדר),
> ו-SDR יחיד לא יכול לצפות בבקרה ובקול בו-זמנית — הדוח מדווח LSN-ים שנצפו + CC +
> תדר בקרה; מפת הערוצים המלאה נשלמת ידנית.

## [0.3.1] - תיקון-ביניים: מעבר ל-dsd-neo (הוחזר ב-0.4.0, ר' למטה)
> ⚠ **המעבר ל-dsd-neo בגרסה הזו הוחזר ב-0.4.0** — הבינארי בפועל מ-0.4.0 ואילך
> הוא שוב `lwvmobile/dsd-fme`, מוזן דרך גשר IQ→PCM+rigctl עצמאי במקום לעבור
> binary. **הממצא המרכזי כאן נשאר נכון ותקף:** `lwvmobile/dsd-fme` אכן אינו
> תומך בקלט `‎-i rtltcp:` ישיר — זו הסיבה שהוחלט שלא לחזור לחיבור הישיר
> המקורי, אלא לפתור את זה בגשר עצמאי.

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

## [0.4.0] - תיקון קריסת dmr-dsdfme + גשר IQ→PCM עצמאי (✅ אומת על חומרה אמיתית)
### Fixed — קריסת `dmr-dsdfme` תוך שניות מהעלייה
`rsp_tcp` היה נופל (`strmHandlerThread`/`heartBeatThread`/`eventHandlerThread: Exit`,
`sdrplay_api_Close`) תוך שניות מהתחברות DSD-FME אליו כלקוח rtl_tcp ישיר
(`-i rtltcp:...`) — חוסר-תאימות ידוע בין לקוח ה-rtl_tcp של DSD-FME לבין ה-emulator
של SDRplay. הפתרון מסיר את החיבור הישיר לגמרי במקום להטליא אותו.

**מחליף את 0.3.1:** גרסה זו נוסתה גם עם `arancormonk/dsd-neo` (ר' 0.3.1 למעלה),
שנבחר כי הוא תומך `‎-i rtltcp:` ישיר — אך הוחלט להחזיר את `lwvmobile/dsd-fme`
ולפתור את חוסר-תמיכת ה-rtltcp בגשר עצמאי (`webtune/rsp_fm.py`) במקום להחליף
בינארי, כדי לשמר את עבודת הפרסור/הנרמול הקיימת (`parse_dsd_line`,
`tests/fixtures/capplus_slco_sample.csv`) שתלויה בפורמט הפלט הספציפי של
lwvmobile/dsd-fme.

### Changed — ארכיטקטורה: rsp_tcp → rsp_fm.py (IQ→PCM+rigctl) → DSD-FME
- **`webtune/rsp_fm.py` (חדש, תלות NumPy):** דמודולטור NFM עצמאי (IQ u8 240kHz →
  PCM signed-16 48kHz, FIR 121-taps + DC-blocker stateful חד-קוטבי) + שרת rigctl
  לכיוונון טראנקינג + שרת בקרת-רווח (unix socket).
- **`dsd_pty.py`:** מפקח כעת על 3 תהליכי-בן (rsp_tcp, rsp_fm.py, DSD-FME תחת PTY)
  במקום 2. DSD-FME עבר מ-`-i rtltcp:...` ל-`-i tcp:...` (קלט אודיו) + `-U` (rigctl)
  — דפוס השימוש הרשמי של lwvmobile/dsd-fme לרדיו בלי טראנקינג IQ נטיבי. תוקן גם
  באג ותיק בהקלטות per-call (`-6` השגוי → `-7 <dir> -P` הנכון).
- **`install.sh`:** `DSD_FME_BRANCH` עבר מ-`main` (שוב לא קיים ב-upstream) ל-`audio_work`
  (ברירת המחדל הנוכחית של lwvmobile/dsd-fme, שם חיות `-i tcp`/`-U`/`-7`/`-P`).
- **`app.py`:** `render_dmr_env` כולל כעת גם את קבועי הגשר (`DSD_RTLTCP`/`DSD_AUDIO_TCP`/
  `DSD_RIGCTL`/`DSD_IQ_RATE`/`DSD_AUDIO_GAIN`) — בלי זה הם נדרסים מתוך `/etc/dmr/dmr.env`
  בכל מעבר מצב (`_enter_dmr`/מעבר-רגל-סריקה).
- **נוד-רווח (`/api/gain`):** מגיע כעת בפועל ל-SDR (`rsp_fm.RtlTcpClient.nudge_gain`
  → פקודות rtl_tcp אמיתיות ל-`rsp_tcp`) במקום הקשה מדומה ל-DSD-FME, שכבר לא נוגע ב-SDR.

### Fixed — חיזוקי אמינות בגשר החדש
- `RtlTcpClient` היה בלי read-timeout אחרי handshake (`settimeout(None)`) — אם
  `rsp_tcp` נתקע בלי לסגור את הסוקט, הגשר היה נתקע לנצח וה-health-check לא תופס
  את זה. עכשיו timeout קבוע (5s) הופך תקיעה לשגיאה מטופלת (restart).
- דמודולטור ה-DC-blocking עבר מ"ממוצע בלוק" (מחושב מחדש בנפרד על כל chunk של
  ~100ms — קפיצה בכל גבול chunk) לפילטר IIR חד-קוטבי עם מצב הנשמר לרוחב chunks,
  כמו ה-FIR overlap הקיים.
- שליחת PCM ל-DSD-FME (`AudioSender`) עברה ל-thread נפרד עם תור חסום — במקום לחסום
  את ה-thread שקורא IQ מ-`rsp_tcp` (עד 2 שניות אם DSD-FME נתקע).
- שלושת תהליכי-הבן (`rsp_tcp`/`rsp_fm.py`/DSD-FME) מקבלים כעת `PR_SET_PDEATHSIG`
  — הגנה מפני תהליכים יתומים שממשיכים להחזיק את ה-SDR/פורטים אם המפקח עצמו נופל
  (למשל OOM-kill), מה שהיה גורם ל-restart הבא להיכשל באותה צורה.

### ✅ אומת על חומרה אמיתית (Pi 5 + SDRplay RSP1B)
כל שרשרת האותות (`rsp_tcp`→`rsp_fm.py`→DSD-FME) היא `pragma: no cover` — לא
נבדקת ב-CI; pytest ירוק (107/107) מוודא רק לוגיקה טהורה (argv, פרסור, דמודולטור
מול אות מסונתז). **אומת בנוסף בפועל** על Pi 5 + RSP1B: לאחר `sudo ./install.sh`,
`dmr-dsdfme.service` נשאר `active (running)` יציב (בעבר קרס תוך שניות), כל
תהליכי-הבן (`rsp_tcp`/`rsp_fm.py`/DSD-FME) חיים ב-cgroup אחד, ו-DSD-FME מתחבר
בהצלחה ל-audio socket ("TCP Connection Success!") ומתחיל תהליך פענוח/טראנקינג
מול רשת Cap+ אמיתית.

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
