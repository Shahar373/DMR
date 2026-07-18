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

> **הבינארי בפועל: `lwvmobile/dsd-fme` (ענף `audio_work`)** — ר' §2 לעדכון
> הארכיטקטורה. בדרך נבדקה גם חלופה זמנית (`arancormonk/dsd-neo`, שנוסה ואז
> **הוחזר** — ר' CHANGELOG) ואומת ש-`dsd-fme` אכן חסר קלט `‎-i rtltcp:` ישיר;
> הפתרון הסופי אינו חוזר לחיבור rtltcp הישיר, אלא מזין את `dsd-fme` דרך גשר
> IQ→PCM+rigctl עצמאי (`webtune/rsp_fm.py`) — אומת על חומרה אמיתית.

---

## 2. הממצא המכריע: DSD-FME אינו "API-first"

בניגוד ל-acarsdec/dumpvdl2 (AIR-AM) שפולטים JSON נקי על UDP, **DSD-FME הוא מפענח
TUI/מקלדת** — הפלט טקסטואלי והשליטה בהקשות. הפתרון: `webtune/dsd_pty.py` הוא **מתאם**
שהופך אותו ל-API-first.

**⚠ עדכון ארכיטקטורה (v0.4.0):** ניסיון מוקדם חיבר את DSD-FME **ישירות** ל-`rsp_tcp`
כלקוח rtl_tcp (`-i rtltcp:...`) — זה קרס בפועל: `rsp_tcp` נפל תוך שניות מהתחברות
DSD-FME (חוסר-תאימות ידוע בין לקוח ה-rtl_tcp של DSD-FME לבין ה-emulator של
SDRplay). DSD-FME **אינו לקוח rtl_tcp אמין מול rsp_tcp** — הפתרון הנוכחי מסיר את
התלות הזו לגמרי ומזין את DSD-FME בדרך הנתמכת הרשמית שלו לרדיו שלא עושה טראנקינג
ברמת IQ: קלט אודיו (discriminator-style, כמו מסורק אנלוגי) + rigctl לכיוונון.
`webtune/rsp_fm.py` (מודול חדש, תלות NumPy) הוא הגשר השלישי בשרשרת:

```
אנטנה ─►RSP1B─USB─► sdrplay.service (SDRplay API)
                          │
        ┌─────────────────────── dmr-dsdfme.service ───────────────────────┐
        │  dsd_pty.py (ExecStart) מפקח על 3 תהליכי-בן:                     │
        │    ├─ rsp_tcp    (שרת IQ תואם-rtl_tcp)         ◄── מחזיק את ה-SDR │
        │    ├─ rsp_fm.py  (דמודולציית NFM ל-PCM 48kHz    │                │
        │    │              + שרת rigctl לכיוונון טראנקינג)                │
        │    └─ DSD-FME    (תחת PTY; קלט: tcp PCM, כיוונון: rigctl -U)     │
        │         │ טקסט → parse_dsd_line() → JSON                        │
        └─────────┼──────────────── UDP 5555 ─────────────────────────────┘
                  ▼
        dmr-web.service :8080  (Flask, המתזמר)  ── REST/JSON ──► דף הבקרה (PWA)
                  │  _dmr_listener → _normalize_dsd → dmr.jsonl
```

DSD-FME נבנה מענף `audio_work` של `lwvmobile/dsd-fme` (ר' `install.sh`) — שם חיות
תכונות ה-`-i tcp`/`-U`/`-7`/`-P` שהשרשרת הזו תלויה בהן; ל-upstream **אין ענף `main`**
יותר, `audio_work` הוא ברירת המחדל שלו בפועל.

**SDR אחד, בהחלפה:** ל-RSP1B ניגש תהליך אחד בכל רגע. `dmr-dsdfme` הוא צרכן ה-SDR
**היחיד** (rsp_tcp ו-rsp_fm.py רצים כתהליכי-בן שלו, לכן יחידת systemd אחת = צרכן
אחד, כמו rtl_airband ב-AIR-AM — ר' עדכון §2 למעלה). **אף צרכן אינו enabled** —
`dmr-web` (שעולה תמיד) קורא `state.json` באתחול
ומשחזר את המצב השמור (`_boot_restore`) => המצב שורד reboot, כולל `off`. כישלון כניסה
נופל ל-`off` (`_fail_to_off`), **לעולם** לא "מצב ברירת מחדל".

**⚠ `parse_dsd_line` מבוסס קליטה אמיתית, לא ניחוש:** התבניות (`SLOT N TGT=N SRC=N
Cap+ Group Call`, `Slot N Data Header - Indiv - ...`, `Sync: +DMR ... CSBK (CRC ERR)`
וכו') אומתו ב-replay מלא מול 20,000 שורות אמיתיות מרשת Motorola Capacity Plus
רב-אתרית (SLCO) — ר' `tests/fixtures/capplus_slco_sample.csv` ו-§7. ממצא מרכזי:
**~80% מהפלט האמיתי הוא רעש תפעולי** (lsn_status/channel_status/site_info/
ip_mapping/bank_call/preamble_csbk — עדכוני מצב פנימי של הטראנקינג) —
`parse_dsd_line` מטיל אותו החוצה **במקור** (מחזיר `None`, לא נשלח ב-UDP כלל),
לא ב-`app.py`. שנה תבניות **רק** לפי דגימות אמיתיות חדשות, לא ניחוש.

---

## 3. מבנה המאגר (file-by-file)

```
install.sh                  # מתקין-על יחיד. אידמפוטנטי (build-signature פר-רכיב).
VERSION · CHANGELOG.md · README.md · CLAUDE.md

webtune/
  app.py                    # ★ הליבה: Flask. מצבים (dmr/off/scan), listener, נרמול,
                            #   מערכות, אליאסים(join), רוסטר, REST, boot-restore, הקלטות.
  dsd_pty.py                # ★ המתאם/המפקח: מפעיל rsp_tcp+rsp_fm.py+DSD-FME (תחת PTY),
                            #   parse_dsd_line → UDP JSON. הרצה ידנית: --selftest
  rsp_fm.py                 # ★ הגשר: IQ (מ-rsp_tcp) → דמודולציית NFM ל-PCM 48kHz + שרת
                            #   rigctl לכיוונון טראנקינג. NumPy. ר' §2/§8.
  aliases.py                # שמות TG/RID: ייבוא CSV (RadioID.net) + עריכות ידניות (join).
  discovery.py              # ★ גילוי רשתות (טהור): ולידציית טווח, גריד, זיהוי מועמדים
                            #   (סף אדפטיבי FFT), סיכום בדיקה, רשומה→מערכת. ר' §5/§10.
  dsd_export.py             # ייצוא CSV(BOM)/JSON לפיד.
  static/
    index.html              # ה-UI כולו (HTML+CSS+JS inline). PWA. 4 תצוגות: 🏠 בית +
                            #   📻 שיחות + 📊 ניתוח (הצפנה/תעבורה/גרף/LRRP) + 🔎 גילוי.
    manifest.webmanifest · sw.js · icon-*.png · apple-touch-icon.png
    vendor/leaflet/         # Leaflet vendored (למפת LRRP ב-Phase 3; בלי CDN).

config/
  dmr.env                   # ברירת-מחדל ל-DSD-FME (EnvironmentFile). ⚠ נדרס ע"י app.py.
  channelmap.csv            # מפת LCN→תדר (Hz) לדוגמה. ⚠ נדרס ע"י app.py בכל מעבר.
  systems.survey.json       # 19 מערכות אמיתיות ממדידת-שדה עצמאית (IQ Surveyor,
                            #   17.07.2026, VHF 162–167MHz): 16 ערוצים בודדים +
                            #   3 אשכולות ל-multi (162/164/165MHz). *לא* נטען
                            #   אוטומטית — הפעלה: cp אל /var/lib/dmr/systems.json
                            #   בפי. ר' §10 Phase 7 להקשר.

systemd/
  sdrplay.service           # שירות SDRplay API. enabled.
  dmr-dsdfme.service        # צרכן ה-SDR (DSD-FME+גשר). Requires+PartOf=sdrplay. *לא* enabled. root.
  dmr-web.service           # שרת הבקרה + המתזמר. enabled. User=dmr (לא-root).

scripts/dmr-wait-sdrplay    # שער מוכנות (ExecStartPre): מחכה שה-API יענה, מרים sdrplay אם תקוע.
udev/99-dmr.rules           # חיבור RSP1B (Vendor 1df7) → restart אוטומטי ל-sdrplay.
tests/                      # pytest (SDR/systemd/rsp_fm ממוקפים). 177 בדיקות. ראה §7.
  fixtures/capplus_slco_sample.csv  # 68 צורות אמיתיות (מקליטת Cap+/SLCO) ל-replay-test.
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
| `/var/lib/dmr/discovery.json` | דוח הגילוי האחרון (מועמדים + רשתות שהתגלו) | _discover_loop |
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
  `render_dmr_env` דורס את **כל** `/etc/dmr/dmr.env` בכל קריאה — פרמטרי הגשר הקבועים
  (`DMR_BRIDGE_RTLTCP`/`_AUDIO_TCP`/`_RIGCTL`/`_IQ_RATE`/`_AUDIO_GAIN`) נכתבים כאן
  כקבועים לצד השדות הפר-מערכת (תדר/CC/מפה); ר' גוצ'ה ב-§8.
- **★ `_normalize_dsd(m)`:** הלב — ממיר אירוע DSD-FME **מוקלד** (dict מ-dsd_pty, שדה
  `type`) לכרטיס שיחה אחיד: `{t, proto, freq, slot, cc, lcn, tg, tg_alias, src, src_alias,
  tgt, tgt_alias, call_type, category, group, encrypted, enc{alg,alg_name,key_id}, ber,
  level, dur, event, lat, lon, text, wav, delivery}`. **רק** ל-`voice_call`/`data_header`/
  `lrrp_position`/`lrrp_request` (`_CARD_EVENT_TYPES`) — `quality`/`encryption`/כל השאר
  מחזירים `None` (לא כרטיס; ר' bullet הבא). **לעולם לא ממציא מדד:** `ber`/`level` תמיד
  `None` (DSD-FME לא מדפיס אותם בקליטה אמיתית — זה תקין, לא חוסר-מימוש). `freq` נגזר
  מ-`_channelmap_freq(lcn)` (חיפוש ב-channelmap של המערכת הפעילה לפי Rest-LSN), **לא**
  מטקסט DSD-FME. אליאסים ב-join מ-`aliases.py`. `enc.alg`/`enc.key_id` נשארים `None` תמיד
  (DSD-FME לא מדפיס ALG/KEY בקליטה שנבדקה — `DMR_ALG_NAMES` שמור כטבלת-מיפוי לעתיד
  אם קליטה אחרת כן תחשוף אותם).
- **★ `_dmr_listener` (thread, UDP 5555) — dispatch לפי `type`:**
  `voice_call`/`data_header`/`lrrp_position`/`lrrp_request` → `_normalize_dsd` + dedup
  המשך-שיחה (tg+src+slot, חלון 8ש', מצטבר ל-`dur`) → `dmr.jsonl`+`_dmr_msgs`.
  `quality` → **לא** כרטיס — מוזן ל-`_rf_quality_tick` (מד תדירות-שגיאות, ר' בהמשך).
  `encryption` → **לא** כרטיס — מתואם ל-`_slot_open_call[slot]` (השיחה הפתוחה כרגע
  באותו slot, חלון 15ש') ומסמן `encrypted=True` עליה (מוטציה על הרשומה החיה, כמו merge
  ה-dedup). `voice_call` עם `crc_err=True` מזין גם הוא את מד ה-RF (`VOICE_CRC`), בנוסף
  לכרטיס עצמו. housekeeping לא מגיע לכאן בכלל — `dsd_pty` כבר סינן במקור (§2).
  `_append_jsonl_log`/`_trim_jsonl_log`/`_read_dmr_log`/`_load_dmr_history`, `_today_start`/
  `_day_bounds` (ארכיון יומי, עמיד DST). רץ תמיד ברקע (גם ב-standby).
- **★ איכות RF (לא dBFS/SNR!) + נוד-רווח:** `_rf_quality_tick`/`_rf_quality_snapshot`
  (חלון נגלל `RF_WINDOW_SEC`=60s של אירועי CRC/FEC אמיתיים — `errors_per_min` + פילוח
  `by_type`; **לעולם לא ממציאים dB/SNR**, רק סופרים תדירות אמיתית). `_dmr_gain_nudge`
  (שולח `g`/`G` דרך `dsd_pty.send_gain_nudge` — הקשה חיה ל-DSD-FME, **בלי לעצור אותו
  ובלי פטצ' קוד C**; מונה `state.gain_nudge` **יחסי בלבד**, אין readback אמיתי,
  מתאפס בכל `_enter_dmr`). מד dBFS עצמאי מה-SDR **נדחה במכוון** — ר' §8.
- **מערכות DMR:** `DEFAULT_SYSTEMS`/`_validate_systems`/`load_systems`/`_find_system`.
  מערכת = `{id, name, control(MHz), color_code, channelmap:[{lcn,freq(MHz)}]}`.
- **scan (סבב בין מערכות):** `_validate_scan_plan` (רגל = `{system, dwell_sec, active_from?,
  active_to?}`), `_leg_active_now`, `_scan_enter_leg`, `_scan_loop`/`_scan_activate`/
  `_scan_stop_thread` — thread שמסתובב, נועל TUNE_LOCK רק במעבר; כשל-כל-הרגלים ⇒ off.
- **גילוי (discover, Phase 6):** מצב חולף בזיכרון (**לא** מַתמיד ב-state, לא משוחזר
  ב-boot). `_discover_activate` (מרים קונפיג-sweep, מחזיק TUNE_LOCK ל-bring-up),
  `_discover_loop` (שלב1: `discmod.build_freq_grid`→`_sweep_read` דרך rigctl F+SPECTRUM,
  **בלי** TUNE_LOCK; שלב2: `_probe_candidate` per-מועמד עם TUNE_LOCK per-step כמו scan,
  `_enter_dmr(_probe_system)` non-trunk), `_discover_stop_thread`, `_finish_discovery`
  (דוח→`discovery.json`+standby+off). `_discover_collect` = side-tap ב-`_dmr_listener`
  (מתויג-epoch, לא נוגע ב-dedup). `_discover_active` נבדק **ראשון** ב-`api_state`/
  `api_health`. הלוגיקה הטהורה ב-`discovery.py`; שרשרת האותות (`_sweep_read`/rsp_fm
  sweep) `pragma: no cover` (חומרה). ר' §8 ו-§10 Phase 6.
- **★ multi (Phase 7): פענוח כל ערוצי ה-channelmap בו-זמנית.** מצב `app_mode`
  חדש, אותה יחידת systemd בדיוק כמו `dmr` (`MODE_SERVICE["multi"]=DMR_SERVICE` —
  **לא** unit נפרד, שומר על "SDR אחד בהחלפה"; `_live_mode` מבדיל dmr/multi לפי
  `state.json` כי systemctl לא יכול). `_validate_multi_feasible` (טהורה: ≥2
  ערוצים, `MULTI_CHANNELS_MAX`, נכנס ב-`MULTI_MAX_SPAN_HZ` דרך
  `dsd_pty.compute_wideband_plan`) נבדק ב-`api_mode` **לפני** תפיסת `TUNE_LOCK`.
  `render_dmr_env(system, multi=True)` מוסיף `DSD_MULTI=1`+
  `DSD_MULTI_GUARD_HZ`/`DSD_MULTI_MAX_RATE_HZ` (מהקבועים `MULTI_GUARD_HZ`/
  `MULTI_MAX_SPAN_HZ` — **אותם ערכים** ש-`_validate_multi_feasible` כבר אימת
  מולם, כדי ש-`dsd_pty._run_multi` לא יחשב עם ברירות-מחדל שסוטות) + `DSD_AUDIO_TCP_BASE`
  (בלתי-תלוי-מצב, כמו שאר קבועי הגשר — ר' §8). `dsd_pty._run_multi` מריץ
  `rsp_tcp` **רחב-פס אחד** (מכוון פעם אחת ל-center_hz שחושב) + `rsp_fm.py` עם N
  מדמודלטורי NFM מוסטים (`offset_hz` לכל ערוץ; אין retune פר-ערוץ — LO משותף
  יחיד) + N מפענחי DSD-FME (כל אחד `-i tcp:...:port_i`, בלי `-T`/`-U` — אין
  trunking-follow פר-ערוץ). כל אירוע מתויג `tag_event(phys_lcn, phys_freq_hz)`
  ב-dsd_pty (ground-truth מ-spawn, לא ניחוש טקסט) — `_normalize_dsd` מעדיף אותו
  על `_channelmap_freq(lcn)`, ו-`_dmr_listener`'s dedup/`_slot_open_call`/RF
  ticks כולם מורחבים במפתח `phys_lcn` (בחד-ערוצי תמיד `None` ⇒ ללא שינוי
  התנהגות). כשל בכל מפענח יחיד ⇒ כשל כל השירות (כמו חד-ערוצי; רסטרט חלקי
  פר-ערוץ **לא** ממומש). **שרשרת האותות `pragma: no cover`, לא אומתה על
  חומרה** (ר' §10 Phase 7) — הלוגיקה הטהורה (`compute_wideband_plan`,
  `parse_channelmap_hz`, `tag_event`, בוני-הפקודות) כן נבדקת ב-CI.
- **רוסטר:** `_dmr_identity` (RID קודם, אחרת TG) + `_build_roster` (היתוך, כולל אילו
  TG-ים כל RID דיבר — בסיס לגרף RID↔TG). חי בכל מצב.
- **אנליטיקה (Phase 2/3):** `_analytics_source(day, show_all)` — מקור אחיד (היום/
  ארכיון/הכל-בזיכרון), אותם פרמטרים כמו `/api/dmr`. `_encryption_stats` (היסטוגרמת
  ALG + %מוצפן פר-TG — **לעולם לא מפענח**, רק מסכם את התג הקיים). `_traffic_stats`
  (air-time+שיחות פר-TG + heatmap שעתי 0–23). `_rid_tg_graph` (who-talks-to-whom,
  צירי RID→TG ממושקלים, רק שיחות קבוצה). `_lrrp_snapshot` (מיקום אחרון-ידוע פר-RID
  מהזיכרון — "עכשיו" בלבד, כמו `adsb.aircraft_snapshot` ב-AIR-AM; ריק אם הרשת לא
  שולחת LRRP סטנדרטי — Motorola proprietary לא מפוענח ע"י DSD-FME).
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
| POST | `/api/mode` | **מעבר מצב** dmr/off/scan/discover/multi. דרך `_guard`. כישלון ⇒ off + 500 |
| GET | `/api/scan` | סטטוס סבב (רגל, ספירה לאחור) |
| GET | `/api/discover` | סטטוס גילוי חי (שלב/התקדמות/מועמדים) + הדוח האחרון |
| POST | `/api/discover/save` | שומר רשת מגולה כמערכת (מיזוג ל-systems דרך `_validate_systems`) |
| GET | `/api/dmr` | שיחות (היום; `?all=1`; `?day=YYYY-MM-DD` ארכיון; `?since=` cursor) |
| GET | `/api/dmr/export?format=csv\|json` | ייצוא (CSV עם BOM) |
| GET | `/api/roster` (·`/api/aircraft`) | רוסטר RID/TG מאוחד — חי בכל מצב |
| GET | `/api/analytics/encryption` | ניתוח הצפנה: היסטוגרמת ALG + %מוצפן פר-TG (`?day=`/`?all=1`) |
| GET | `/api/analytics/traffic` | אנליטיקת תעבורה: air-time/TG + heatmap שעתי (`?day=`/`?all=1`) |
| GET | `/api/analytics/graph` | גרף RID↔TG (who-talks-to-whom) (`?day=`/`?all=1`) |
| GET | `/api/positions` | מיקום LRRP אחרון-ידוע פר-RID (מהזיכרון בלבד, "עכשיו") |
| GET | `/api/rf` | איכות RF: תדירות שגיאות CRC/FEC אמיתית (60ש') + `gain_nudge` + `by_channel` (Phase 7, `multi` בלבד). **אין dBFS/SNR** |
| POST | `/api/gain` | נוד-רווח חי (`{direction: up\|down}`) — הקשת g/G, בלי לעצור את DSD-FME |
| GET | `/api/activity` | הקלטות אחרונות |
| GET | `/recordings/<name>` | קובץ WAV |
| GET | `/api/power` | מתח/טמפ' ה-Pi |

**כלל:** כל route משנה-מצב = `POST` + `_guard`. מעברי-מצב (`/api/mode`) גם נועלים
`TUNE_LOCK`; **`/api/gain` לא** — הקשת נוד-רווח לא מפעילה restart ולא מתחרה על
משאב ה-SDR, אז אין סיבה לחסום אותה מאחורי אותה נעילה.

---

## 7. בדיקות (ללא חומרה)

`python -m pytest tests/ -v` (177 בדיקות). SDR/systemd/rsp_fm ממוקפים דרך fixtures ב-`conftest.py`:
`paths` (מפנה נתיבי-מודול ל-`tmp_path`), `sysctl` (Recorder ל-`_sysctl` + מוקי
`_is_active`/`_sdr_present`), `no_sleep`. פונקציות טהורות (`parse_dsd_line`, `_normalize_dsd`,
`render_dmr_env`, `_validate_*`, `_encryption_stats`, `_traffic_stats`, `_rid_tg_graph`,
`_lrrp_snapshot`, `_rf_quality_snapshot`) נבדקות ישירות; Flask דרך `app.app.test_client()`.

**★ `tests/fixtures/capplus_slco_sample.csv`:** 68 הצורות הייחודיות (type+pattern) מקליטה
אמיתית של רשת Cap+/SLCO רב-אתרית (20,000 שורות מקור). `test_fixture_replay_matches_reality`
מריץ את כל 68 דרך `parse_dsd_line` ומוודא סיווג מדויק (housekeeping⇒None, שיחה⇒type נכון)
— זו בדיקת ה-regression המרכזית של הפרויקט. **בכל שינוי ב-`parse_dsd_line`, הרץ אותה
ראשון.** אם מגיעה דגימה אמיתית חדשה (רשת/גרסת DSD-FME אחרת) — הוסף לפיקסצ'ר, אל תמציא.

קבצים: `test_dsd_normalize` (הלב — parse + normalize + replay + listener e2e, וגם
argv טהור של `build_command`/`build_rsp_tcp_command`/`build_bridge_command`), `test_rsp_fm`
(הגשר IQ→PCM: דמודולטור, DC-blocker stateful, timeout על `RtlTcpClient`, `AudioSender`,
`RigctlServer`), `test_mode`, `test_boot`, `test_scan`, `test_aliases`, `test_recordings`,
`test_security`, `test_archive`, `test_analytics` (הצפנה/תעבורה/גרף/LRRP), `test_rf_gain`
(שכבת ה-HTTP של `/api/rf`/`/api/gain`), `test_discovery` (גילוי: `validate_sweep_plan`/
`build_freq_grid`/`detect_candidates`/`aggregate_probe`/`discovery_to_system` הטהורים +
שכבת Flask `/api/discover[/save]` + `_discover_loop` e2e ממוקף + collector-via-listener).
**הוסף בדיקה לכל שינוי backend.** CI: pytest (Python 3.11, כולל NumPy) + `bash -n`.

**UI (`static/index.html`) — ללא סוויטת בדיקות (כמו AIR-AM: אין build step, אין JS
tests).** אימות שינויי UI: `node --check` על ה-JS המחולץ מ-`<script>` + הרצת השרת
עם נתונים מדומים ובדיקה ויזואלית (Playwright headless) — כך נתפסה ותוקנה בפועל
חסרת `dir="rtl"` על ה-`<html>` (Phase 2). **בדוק חזותית כל שינוי UI מהותי, אל
תסתפק ב-syntax check.**

---

## 8. מוסכמות וגוצ'אות (קרא לפני שינוי)

- **SDR אחד בהחלפה:** צרכן אחד בכל רגע. `off` משחרר; אף צרכן לא enabled; `_boot_restore` משחזר.
- **⚠ MHz בכל מקום חוץ מ-env/channelmap:** state/UI/systems/API עובדים ב-**MHz**;
  `render_dmr_env`/`render_channelmap` הם **המקומות היחידים** שממירים ל-**Hz** (DSD-FME/rigctl).
  אל תערבב (בדיוק כמו כלל ה-VDL2-Hz ב-AIR-AM).
- **לעולם לא ממציאים מדד:** `ber`/`level` על כרטיס תמיד `None` — DSD-FME לא מדפיס אותם
  בקליטה אמיתית שנבדקה (זה **תקין**, לא חוסר-מימוש; אל תמלא ערך משוער). הצפנה = **תג
  בלבד** (`encrypted=True`, `enc.alg_name` גנרי "מוצפן") — DSD-FME לא הדפיס ALG/KEY
  בקליטה שנבדקה (FLCO/FID הם routing fields, לא אלגוריתם); `DMR_ALG_NAMES` שמור למקרה
  שגרסה/רשת אחרת כן תחשוף שם אלגוריתם — אל תניח שהוא תמיד ריק.
- **⚠ אין SNR/RSSI/dBFS רציף מ-DSD-FME:** המדד היחיד הוא **תדירות שגיאות CRC/FEC**
  (`_rf_quality_tick`/`RF_WINDOW_SEC`=60s, `errors_per_min`) — לא יחס, לא dB. מד dBFS
  עצמאי מה-SDR עצמו **נדחה במכוון**: SDRplay API הוא single-owner (תהליך אחד מחזיק את
  ה-RSP1B בכל רגע — `rsp_tcp` כבר תופס אותו), ואין side-channel telemetry ב-rsp_tcp
  הסטנדרטי (`RSPTCPServer`). המימוש היחיד האפשרי דורש **פטצ' קוד C** על `rsp_tcp` (hook
  בתוך ה-IQ callback שכבר מחזיק את המכשיר, סטטס-פייל תקופתי בסגנון `channel_dbfs_*`
  של rtl_airband ב-AIR-AM) — **לא מומש כרגע**, כי לא ניתן לאמת בלי חומרת RSP1B אמיתית.
  אם מיישמים בעתיד: ודאו בדיקה על חומרה אמיתית לפני merge, ותעדו כאן.
- **⚠ נוד-רווח (gain) עכשיו מגיע ל-SDR האמיתי, לא ל-DSD-FME:** מ-v0.4.0, DSD-FME
  כבר לא נוגע ב-SDR בכלל (הוא צרכן אודיו/rigctl בלבד). לחיצת `g`/`G` מ-`app.py`
  (`dsd_pty.send_gain_nudge` → `DSD_CTRL_SOCK`) מועברת ע"י `dsd_pty._run()` דרך
  `_send_bridge_control` ל-`rsp_fm.py` (`DSD_BRIDGE_CTRL_SOCK`, unix socket), ששולח
  משם פקודות rtl_tcp אמיתיות (`SET_GAIN_MODE`/`SET_GAIN_BY_INDEX`) ל-`rsp_tcp`
  (`RtlTcpClient.nudge_gain`). עדיין **בלי readback אמיתי** — `state.gain_nudge`
  ב-`app.py` וה-`gain_index` (0–28) ב-`rsp_fm.py` שניהם מונים יחסיים best-effort,
  לא dB; מתאפסים בכל `_enter_dmr` (restart אמיתי = ברירת-מחדל מחדש בשני הצדדים).
- **DSD-FME הוא ה"מתאם" היחיד תלוי-פורמט לטקסט הפלט:** אם גרסת DSD-FME משנה ניסוח
  פלט — מתקנים **רק** ב-`dsd_pty.parse_dsd_line` (ונבדק ב-`test_dsd_normalize`, כולל
  replay מול `tests/fixtures/capplus_slco_sample.csv`). שאר הקוד צורך אירועים מוקלדים
  נקיים. **לעומת זאת** — שרשרת האותות (rsp_tcp→rsp_fm.py→DSD-FME) היא תלוית-hardware
  אמיתית ולא נבדקת ב-CI (`dsd_pty._run`/`rsp_fm.run` הם `pragma: no cover`); שינוי בה
  דורש בדיקה על RSP1B אמיתי, לא רק pytest ירוק.
- **rsp_tcp + rsp_fm.py כתהליכי-בן:** dsd_pty מריץ את שניהם (ובנוסף את DSD-FME עצמו
  תחת PTY) => יחידת systemd אחת = צרכן-SDR אחד (מודל ה-standby/PartOf של AIR-AM
  נשמר). כל שלושת התהליכים מקבלים `PR_SET_PDEATHSIG` (`dsd_pty._pdeathsig_term`) כדי
  שלא יישארו יתומים אם המפקח עצמו נופל (למשל OOM-kill) — בלעדי זה, תהליך יתום ממשיך
  להחזיק את ה-SDR/פורטים והריצה הבאה (`Restart=always`) נכשלת באותה צורה. אל תפצל
  ל-unit נפרד בלי לעדכן את `_enter_standby`.
- **⚠ גילוי-אנרגיה = קוד sweep תלוי-חומרה; מיפוי LCN↔תדר לא ניתן לגילוי מלא:**
  מצב `discover` מוסיף מצב sweep ל-`rsp_fm.py`/`dsd_pty.py` (FFT על ה-IQ הגולמי,
  gain ידני קבוע, verb `SPECTRUM` ב-rigctl) + לקוח rigctl/spectrum ב-`app.py` —
  כל אלה `pragma: no cover`, מתאמתים רק על RSP1B אמיתי (כמו הגשר של v0.4.0). רק
  `discovery.py` (טהור) + `compute_power_spectrum` + שינויי ה-parser נבדקים ב-CI.
  **הסף בזיהוי מועמדים הוא יחסי בלבד** (median+k·MAD עם מרווח-מינימום מעל רצפת
  הרעש) — לעולם לא dBFS מוחלט (rsp_tcp נותן dBFS יחסי בלבד). **מיפוי LCN↔תדר מלא
  אינו בר-גילוי אוטומטי:** Cap+ משדר LSN לוגי (לא תדר), ו-SDR יחיד לא יכול לצפות
  בבקרה ובקול בו-זמנית — הדוח נותן תדר-בקרה+CC+LSN-ים-שנצפו; מפת הערוצים המלאה ידנית.
- **⚠ אירועי `sync`/`channel_status` ב-`parse_dsd_line` הם opt-in (`emit_status`):**
  ברירת המחדל (dmr/scan רגיל) משאירה אותם `None` — שומר על "סינון housekeeping
  במקור" (§2) ועל ה-fixture replay (68/68). רק בדיקת גילוי (`_probe_system` מגדיר
  `DSD_EMIT_STATUS=1`) מפעילה אותם. שורת sync **עם שגיאה** נשארת `quality` (קדימות).
- **⚠ `render_dmr_env`/`write_dmr_env` דורסים את `/etc/dmr/dmr.env` בכל מעבר מצב:**
  כל מפתח env שהגשר (`rsp_tcp`/`rsp_fm.py`) צריך (`DSD_RTLTCP`/`DSD_AUDIO_TCP`/
  `DSD_RIGCTL`/`DSD_IQ_RATE`/`DSD_AUDIO_GAIN`) **חייב** להופיע כקבוע קשיח בתוך
  `render_dmr_env` עצמו (`DMR_BRIDGE_*` ב-`app.py`) — אחרת הוא נעלם מהקובץ החי בכל
  `_enter_dmr`/מעבר-רגל-סריקה, ו-`dsd_pty`/`rsp_fm` נופלים בשקט על ברירות-המחדל
  שלהם-עצמם (מזל שהן זהות היום ל-`config/dmr.env` — אל תסמכו על זה בעתיד).
- **gain של SDRplay הפוך:** ערך קטן = רווח גדול (רלוונטי לבקרת gain עתידית ברמת
  IFGR/RFGR עצמאיים — כרגע נוד-הרווח הוא אינדקס יחסי 0–28 דרך `RtlTcpClient`, לא
  ערך ישיר).
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
- **Phase 2 (הושלם):** ניתוח הצפנה (היסטוגרמת ALG, %מוצפן פר-TG) + אנליטיקת תעבורה
  (heatmap שעתי, air-time/TG). נגזר מ-`dmr.jsonl`/`_dmr_msgs` הקיימים —
  `/api/analytics/encryption`+`/api/analytics/traffic`, כרטיסייה 📊 ניתוח ב-UI.
- **Phase 3 (הושלם):** גרף RID↔TG (who-talks-to-whom, `/api/analytics/graph`) + מפת
  GPS/LRRP (`/api/positions` + Leaflet vendored, lazy-load). מוצג ריק בשקט כשהרשת
  לא שולחת LRRP סטנדרטי (Motorola proprietary אינו מפוענח ע"י DSD-FME).
- **Phase 4 (הושלם):** `parse_dsd_line` נכתב מחדש מול **קליטה אמיתית** (20,000 שורות,
  רשת Cap+/SLCO רב-אתרית) במקום ניחוש — replay מלא מאמת 68/68 צורות (§7). תוקנו:
  סינון housekeeping במקור (~80% מהפלט), סמנטיקת tg/tgt (group call: tgt=tg עצמו),
  קורלציית encryption ל-slot, תדר מ-channelmap במקום מטקסט. נוסף: איכות RF (תדירות
  שגיאות CRC/FEC, `/api/rf`) + נוד-רווח חי דרך הקשות DSD-FME (`/api/gain`) — שניהם
  בלי פטצ' קוד C. כרטיס UI "📶 איכות RF ובקרת רווח".
- **Phase 5 (v0.4.0, הושלם — אומת על חומרה אמיתית):** תוקן קריסת `dmr-dsdfme`
  (`rsp_tcp` נופל תוך שניות מהתחברות DSD-FME — חוסר-תאימות ידוע בין לקוח ה-rtl_tcp
  של DSD-FME ל-emulator של SDRplay) ע"י הסרת החיבור הישיר לגמרי: `webtune/rsp_fm.py`
  (מודול חדש) מבצע דמודולציית NFM עצמאית (IQ→PCM 48kHz, NumPy) ומריץ שרת rigctl;
  DSD-FME עבר מ-`-i rtltcp:...` ל-`-i tcp:...` (קלט אודיו) + `-U` (rigctl), בהתאם
  לדפוס השימוש הרשמי של lwvmobile/dsd-fme לרדיו בלי טראנקינג IQ נטיבי (ענף
  `audio_work`, שהוא כעת ברירת המחדל של ה-upstream). תוקן גם באג ותיק בהקלטות
  per-call (`-6` השגוי → `-7 ... -P` הנכון). ר' §2/§8 לפרטי הארכיטקטורה החדשה.
  **בדרך נוסה גם מעבר ל-`arancormonk/dsd-neo`** (fork אחר שתומך `‎-i rtltcp:`
  ישיר, בלי צורך בגשר) — **הוחזר**: הוחלט להישאר עם `lwvmobile/dsd-fme` + הגשר
  העצמאי, ר' CHANGELOG. **אומת בפועל** על Pi 5 + RSP1B: `dmr-dsdfme.service`
  נשאר `active (running)` יציב (בעבר קרס תוך שניות), כל תהליכי-הבן חיים
  ב-cgroup, ו-DSD-FME מתחבר בהצלחה ל-audio socket ("TCP Connection Success!")
  ומתחיל תהליך פענוח/טראנקינג. איכות הפענוח בפועל מול תעבורה חיה (נעילה על
  ערוץ בקרה, המשך שיחות) ממשיכה להיבדק בשטח.
- **Phase 6 (v0.5.0, הושלם — CI ירוק; שרשרת הסריקה טרם אומתה על חומרה):** גילוי
  רשתות (frequency discovery) — מצב `discover` שסורק טווח RF (סריקת ספקטרום FFT
  ב-`rsp_fm.py` מצב sweep, צעד דרך rigctl F, קריאת `SPECTRUM`), מזהה תדרים חשודים
  כ-DMR (`discovery.detect_candidates`, סף אדפטיבי), ובודק כל מועמד עם DSD-FME
  (`_probe_candidate`, non-trunk) לגילוי תדר-בקרה/CC/סוג-ערוץ/LSN/TG. נוסף:
  `webtune/discovery.py` (טהור), `compute_power_spectrum`, אירועי `sync`/
  `channel_status` opt-in ב-`parse_dsd_line`, נקודות `/api/discover[/save]`, תצוגת
  "🔎 גילוי" עם "שמור כמערכת". **מיפוי LCN↔תדר best-effort/ידני** (Cap+ = LSN לוגי,
  SDR יחיד; ר' §8). הלוגיקה הטהורה + Flask נבדקים ב-CI (140 בדיקות); מצב ה-sweep
  ולקוח ה-rigctl/spectrum הם `pragma: no cover` — לאימות על Pi 5 + RSP1B.
- **Phase 7 (v0.6.0, קוד+CI ירוקים; שרשרת הפענוח הרב-ערוצית טרם אומתה על
  חומרה):** מיזוג עם `DMR-DECREP-SHAHAR` — מצב `multi` חדש: פענוח **כל** ערוצי
  ה-channelmap של מערכת בו-זמנית, לא רק תדר-בקרה יחיד (§5 "multi"). קליטה
  רחבת-פס אחת (`rsp_tcp` מכוון פעם אחת) → N מדמודלטורי NFM מוסטים ב-`rsp_fm.py`
  (`offset_hz` פר-ערוץ, גרסה כללית של המדמודלטור החד-ערוצי הקיים — לא retune
  פר-ערוץ, יש LO משותף יחיד) → N מפענחי DSD-FME תחת PTY, כל אחד תג-מזוהה
  (`dsd_pty.tag_event`) עם `phys_lcn`/`phys_freq_hz` אמיתיים. `_normalize_dsd`
  ו-`_dmr_listener` (dedup/הצפנה/RF-quality) מורחבים במפתח `phys_lcn` —
  בחד-ערוצי הוא תמיד `None` ⇒ **אפס שינוי התנהגות** לקוד הקיים (140 הבדיקות
  המקוריות + 68/68 ה-fixture replay נשארו ירוקים ללא שינוי). `/api/rf` מחזיר
  גם `by_channel` (פירוט איכות-RF פר-ערוץ). **החלטות-מוצר של Phase 7:** יום-1
  מפעיל את **כל** ה-channelmap (בלי בחירת-ערוצים חלקית — `channel_ids`/
  `--follow-traffic` נדחו ל-Phase הבא), איכות-RF פר-ערוץ ביום-1 (לא נדחתה),
  שכבת ה-UI/`channels.json` (טבלת קונפיגורציה+live status) נדחתה ל-Phase 8,
  אחרי שהמנוע יאומת על חומרה. **קליטת ה-IQ הרחבה-פס (`rsp_tcp` בקצב מעל
  240kHz הקיים) היא הסיכון הטכני המרכזי הפתוח** — לא אומתה על RSP1B אמיתי;
  אם רוחב-הפס לא יציב שם, החלופה היא Soapy-direct כמו ב-DECREP (ר' spike
  script בריפו DMR-DECREP-SHAHAR). ⚠ **הספייק של DECREP בודק את ה-channelizer
  שלו, לא את `rsp_fm.run_multi` של DMR** — נתיב DSP שונה. לאימות מנוע ה-DMR
  עצמו יש `scripts/spike-dmr-multi` (v0.6.2): מריץ ישירות את `dsd_pty._run_multi`
  (rsp_tcp רחב-פס + rsp_fm + N×dsd-fme) ומודד שרידות rsp_tcp + נעילת-sync
  פר-ערוץ (phys_lcn ב-UDP) + CPU. הלוגיקה הטהורה (`compute_wideband_plan`,
  `parse_channelmap_hz`, `tag_event`, בוני-פקודות `dsd_pty`, `_validate_multi_feasible`)
  נבדקת מלאה ב-CI (184 בדיקות); `dsd_pty._run_multi`/`rsp_fm.run_multi` הם
  `pragma: no cover` — דורשים אימות על Pi 5 + RSP1B אמיתי לפני שהמצב ייחשב מוכן-לשטח.
  **תיקוני v0.6.2 (code review לפני חומרה):** הקלטות multi ב-תת-תיקיות
  (`recordings/lcnN/`) נתפסות ע"י `rglob` (אחרת retention עיוור→דיסק מתמלא);
  `/api/gain` עובד ב-multi (ctrl-sock ב-`_run_multi`); `compute_wideband_plan`
  בודק תקרה אחרי עיגול-48kHz; `_validate_multi_feasible` דוחה LCN כפול.
  **בעלות פורטים (שני הריפואים על אותו Pi):** `dmr-web.service` הוא **8080 קבוע**
  (`app.run(..., port=8080)`, `webtune/app.py`) — זה משטח-הבקרה היחיד שעולה
  תמיד ב-boot, ואסור שיזוז. `DMR-DECREP-SHAHAR` (שהפך לריפו-רפרנס/מקור-מנוע
  למיזוג הזה) שינה את ברירת-המחדל של `--port` מ-8080 ל-**8081** (`backend/cli.py`
  v0.26.4) — כדי שהרצה מקומית שלו (למשל `scripts/spike_multichannel.sh`, או
  `python -m backend.cli --serve` ידני) לעולם לא תתנגש עם dmr-web גם בלי
  `--port` מפורש.
  **⚠ תקרית אמיתית בשטח (18.07.2026):** למרות ברירת-המחדל שתוקנה ב-0.26.4,
  פורט 8080 בכל זאת נתפס בפועל ע"י `dmr-monitor` (ה-service של DECREP) — כנראה
  יחידת systemd שנפרסה על ה-Pi **לפני** תיקון ברירת-המחדל, ולכן לא התעדכנה
  אוטומטית (יחידת systemd שהותקנה היא עותק סטטי בדיסק — לא מסתנכרנת עם שינויים
  בריפו המקור בלי הפעלה מחדש של `install-service.sh`). זוהה כש-`http://<pi>:8080`
  הציג את ה-UI האנגלי של DECREP במקום UI העברי של DMR. שוחזר ידנית (`kill` על
  התהליך התוקע + `systemctl disable dmr-monitor`). **תוקן בקוד** (לא רק
  בברירת-מחדל) ב-DECREP v0.26.5: `backend.cli.main` **דוחה** עכשיו `--serve
  --port 8080` ישירות (`FATAL`, exit 2) — אכיפה שלא תלויה בתוכן של יחידת
  systemd כלשהי, גם אם היא נסחפה/מיושנת. **אם התקרית חוזרת:** ודאו
  שה-`dmr-monitor.service` הפרוס בפועל תואם את הריפו העדכני (הרץ מחדש
  `scripts/install-service.sh` מ-checkout טרי של DMR-DECREP-SHAHAR).
- **Phase A (v0.7.0, הושלם) — שילוב UI ל-multi + תיקוני-UI קטנים.** נתפס
  ונבנה דרך אימות חזותי אמיתי (Playwright מול `app.py` אמיתי + מוק-נתונים,
  לפי §7) — לא syntax-check בלבד. חלק מ"שכבת ה-UI" שנדחתה ב-Phase 7 סעיף
  לעיל **נשלחה כאן** (הלוח הייעודי פר-ערוץ; `channels.json`/live-status
  מלא **עדיין** נדחה קדימה). כלול: `renderChannelsBoard()` בבית (שורה לכל
  ערוץ: LCN+תדר+קריאה-אחרונה+תדירות-שגיאות `by_channel`, לחיצה→פיד מסונן
  `lcn:N`), תג `LCNn` על כרטיסי שיחה, כרטיס-מצב "📡 רב-ערוצי" (כפתור כבוי
  אלא אם למערכת ≥2 ערוצים — משקף `_validate_multi_feasible`), `modeLabel`/
  כותרת-משנה/פיל-בריאות עם ענף אמיתי ל-`multi`, טוגל ערכת-נושא (🌓)+`localStorage`,
  כרטיס מתח/טמפ' (`/api/power`). **שני באגי-backend אמיתיים נתפסו רק דרך
  צילום-מסך** (לא code review): `api_health()` קרס `multi`→`"dmr"` — עותק
  שני, עצמאי, של אותו דפוס-באג שכבר תוקן פעם אחת ב-`_live_mode()` (§5) —
  ו-`/api/dmr/export` התעלם מ-`?day=` (ייצוא-CSV בארכיון יומי ייצא תמיד
  הכל). גם תוקן: בלוקי `:root[data-theme=...]` (קוד-מת קודם — שום דבר לא
  קבע `data-theme` לפני הטוגל הזה) עדכנו רק 3/10 טוקנים → רינדור חצי-כהה/
  חצי-בהיר שבור בפועל ברגע הראשון שהופעלו; תוקן ל-10/10 + לוגיקת JS יציבה
  (משתנה, לא `matchMedia` מחדש בכל קליק). 187 בדיקות ירוקות (184→187).
- **`config/systems.survey.json` — 19 מערכות אמיתיות ממדידת-שדה (17.07.2026):**
  ייבוא מ-inventory Excel של סקר IQ עצמאי (SoapySDR+SDRconnect, decoder+SQLite,
  `integrity_check` תקין). 16 ערוצי DMR מאומתים (VHF 162.14–167.14MHz, כל אחד
  color-code משלו — **לא** אתר Cap+ טראנקינג אחיד, אשכול מקלטים עצמאיים) +
  3 מערכות-אשכול ל-multi mode (162MHz/7 ערוצים, 164MHz/6 ערוצים — כולל שני
  הערוצים עם ראיות TG/Radio ID החזקות ביותר בסקר, 165MHz/2 ערוצים), כל אחת
  אומתה בפועל מול `_validate_systems`/`_validate_multi_feasible`. **לא נטען
  אוטומטית** — `cp config/systems.survey.json /var/lib/dmr/systems.json` בפי
  כדי להפעיל. מועמד-הספייק הראשון המומלץ ל-Phase 7: `multi_164cluster` (תעבורה
  אמיתית מאומתת, לא תדרים בדויים).
- **נדחה במכוון (דורש חומרה לאימות):** מד dBFS/SNR רציף עצמאי מה-SDR — דורש פטצ'
  קוד C על `rsp_tcp` (RSPTCPServer), לא ניתן לממש/לבדוק בלי RSP1B אמיתי. ר' §8.
- **הבא (לא מתוכנן עדיין):** רעיונות שעלו בסיעור-המוחות המקורי ולא נכנסו ל-scope —
  lockout/hold/whitelist דרך הזרקת מקשים ל-PTY (`dsd_pty` כבר תומך בערוץ `DSD_CTRL_SOCK`,
  אותו ערוץ ששמש לנוד-הרווח — ניתן להרחיב), Web Push להתראות watchlist, ייבוא/ייצוא
  מערכת כ-QR, מעקב multi-site (שדה `site` נצפה בקליטה אמיתית אך לא מומש).
