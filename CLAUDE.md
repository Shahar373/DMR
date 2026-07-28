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
  watchlist.py              # מעקב RID/TG: match() טהורה, נצרך ע"י _normalize_dsd.
                            #   התראה מקומית בלבד ב-UI (Notification API, לא Web Push — ר' §8).
  system_intel.py           # ★ מודיעין-מערכת (Phase 8): אתרים/מפת-תפוסה-LSN/CDR
                            #   שיחות-יחיד/סחיפת-CC, נצבר מ-lsn_status/bank_call/
                            #   preamble_csbk/site_info. record_* טהורות; flush debounced
                            #   (lsn_status תכוף מדי לכתיבה בכל אירוע). ר' §5/§8/§10.
                            #   + ★ v0.12.0: גילוי מיפוי LSN↔תדר (multi בלבד) —
                            #   record_rest_channel/derive_lsn_map (טהורה). ר' §5/§8.
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
tests/                      # pytest (SDR/systemd/rsp_fm ממוקפים). 360 בדיקות. ראה §7.
  fixtures/capplus_slco_sample.csv  # 68 צורות אמיתיות (מקליטת Cap+/SLCO) ל-replay-test.
  fixtures/dsdfme_source_shapes.csv # ★ 16 צורות שנגזרו מקוד-המקור של DSD-FME
                            #   (מצבים שהרשת לא שידרה), כל שורה עם provenance.
                            #   ⚠ קובץ נפרד במוצהר — אל תערבבו עם הקליטה. ר' §7.
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
| `/var/lib/dmr/watchlist.json` | רשימת RID/TG במעקב (התראה מקומית) | watchlist.py |
| `/var/lib/dmr/system_intel.json` | מודיעין-מערכת נצבר (אתרים/LSN/CDR/CC + הצבעות LSN↔תדר), debounced | system_intel.py |
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
  אם קליטה אחרת כן תחשוף אותם). `watchlist: {kind,id}|None` נגזר מ-`watchlist.match(tg,
  src, tgt)` (v0.10.0) — אותו עקרון כמו aliasdb: העשרת-מזהים בזמן נרמול, לא בזמן-תצוגה.
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
  **⚠ מבנה (v0.13.0):** הלופ עצמו עושה רק recv+json, וכל הטיפול עבר ל-
  `_handle_datagram(msg, ctx)` שעטוף ב-`try/except` — דאטהגרם בודד **אסור** שיפיל
  את ה-thread (בעבר `{"type":"site_info","site":"abc"}` עשה בדיוק את זה, ר' §8).
  `ctx` = `{dedup, slot_open, pending, seen}`, מודולרי (`_listener_ctx`) כדי
  ש-`_close_stale_calls` ירוץ גם מ-`_activity_watcher`. `_listener_watchdog`
  מקים את ה-thread מחדש אם בכל זאת מת. **אל תוסיפו לוגיקה לתוך הלופ עצמו** —
  היא לא תהיה עטופה ולא תהיה נבדקת.
- **★ כתיבה לארכיון בסגירת-שיחה (v0.13.0) — לא בפריים הראשון:** `dur`/`frames`/
  `encrypted`/`id` נקבעים **אחרי** יצירת הכרטיס, כמוטציה על האובייקט החי. לכן
  כתיבה מוקדמת הקפיאה את הרשומה בדיסק בלעדיהם, ו-`?day=`/CSV דיווחו airtime 0
  ו-0% מוצפן לנצח. שיחות-קול נכנסות ל-`ctx["pending"]` ונכתבות ב-
  `_close_stale_calls` אחרי `CALL_CLOSE_SEC` (20ש') **מהפריים האחרון**; כרטיסי
  data/lrrp נכתבים מיד (אינם משתנים). ⚠ `CALL_CLOSE_SEC` חייב להישאר גדול
  **משני** החלונות שממתתים כרטיס — dedup (8ש') וקורלציית-הצפנה (15ש'); אם
  משנים אחד מהם, עדכנו גם אותו. מחיר מודע: קריסה מאבדת שיחות פתוחות (≤20ש').
- **★ חיוניות: "רשת שקטה" מול "שרשרת מתה" (v0.13.0):** `_feed_tick` נרשם על
  **כל** דאטהגרם — לפני הדיספאץ' ולפני כל פרסור, כדי שגם דאטהגרם בעייתי
  ייספר. `_feed_snapshot` מחזיר עובדות בלבד (חלון `FEED_WINDOW_SEC`=60s פר
  טיפוס + `last_datagram_at`/`last_voice_at`), ו-`_decode_state` (טהורה)
  מכריעה `decoding`/`chain_alive`/`silent`/`listener_down`/`standby`.
  **שמרנית במכוון:** `silent` ולא "שבור" — בחד-ערוצי non-trunk דממה מלאה היא
  מצב לגיטימי ואין ראיה להאשים את השרשרת (§8 "לא ממציאים"). האות שמאפשר את
  ההבחנה הוא `lsn_status` שזורם רציף ב-Cap+ גם בלי שיחות.
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
  התנהגות). **⚠ רסטרט פר-ערוץ (v0.9.0, ✅ אומת על חומרה 25.07.2026):** מפענח
  DSD-FME בודד שקורס נקם-מחדש **במקומו** (אותו audio port; `rsp_fm` כבר סובל
  חיבור-לקוח חדש) עד `CHANNEL_RESTART_MAX` ניסיונות ב-`CHANNEL_RESTART_WINDOW_SEC`
  (ברירת מחדל 3/300ש', `dsd_pty._channel_restart_decision` טהורה) — **לא**
  מפיל את שאר הערוצים. חורג מהמכסה ⇒ הערוץ מוסר מ-`dsd_procs` וממשיכים
  בלעדיו; רק אם *כולם* ויתרו ⇒ כשל כל השירות (כמו קודם). כל מעבר
  (`restarting`/`down`) מדווח כ-`decoder_status` (`build_decoder_status_event`,
  טהורה) → `app.py`'s `_channel_status_tick` → `/api/rf`'s `by_channel`
  (`status`/`restart_count`, **גלוי גם בלי טיקי-RF נוספים** — לעולם לא נעלם
  בשקט) → תג בלוח-הערוצים. **אומת בפועל** על Pi 5 + RSP1B: `sudo kill -9` על
  תהליכי-בן `dsd-fme` בודדים (multi_164cluster, 6 ערוצים חיים) → journalctl
  הראה `respawning (attempt 1/3)` לכל אחד, שני המפענחים קמו על אותו audio
  port והתחברו-מחדש בהצלחה (`TCP Connection Success!`, `rsp_fm` קיבל לקוח
  חדש), `/api/rf` דיווח `status=restarting, restart_count=1` לשניהם, ו-
  `systemctl status dmr-dsdfme` נשאר `active (running)` **ללא הפרעה** לאורך
  כל הבדיקה (4 הערוצים האחרים המשיכו לפענח ללא שינוי). הלוגיקה הטהורה
  (`compute_wideband_plan`, `parse_channelmap_hz`, `tag_event`,
  `_channel_restart_decision`, `build_decoder_status_event`, בוני-הפקודות)
  נבדקת גם ב-CI.
- **★ רדאר-מערכת (Phase 8, v0.11.0):** `_dmr_listener` מדספ'ץ `lsn_status`/
  `site_info`/`preamble_csbk`/`bank_call` (תמיד פעילים — ר' §8) ישירות ל-
  `system_intel.record_*` (**לא** הופכים לכרטיס-שיחה). `quality`'s `cc`
  (שכבר מפוענח, קודם נזרק) מוזן ל-`system_intel.record_cc` מול
  `_active_color_code`. `_intel_system_id()` מסנן standby/מערכות-גילוי
  חולפות (`__probe__`/`__sweep__`) — לא מסאבים את בנק-התדרים. `_active_
  system_id`/`_active_color_code` (מטמון בזיכרון, נקבע ב-`_enter_dmr`) —
  ה-listener **לעולם לא** קורא `load_state()`/`load_systems()` (דיסק) פר-
  אירוע UDP, כי `lsn_status` תכוף מדי לזה. `GET /api/system-intel` חושף
  את הפרופיל הנצבר (אין PUT — זה לא config נערך-ידנית).
- **★ מיפוי LSN↔תדר (v0.12.0) — הנתון היה שם ונזרק:** טלמטריית CSBK
  (`lsn_status`/`site_info`/`preamble_csbk`) אומרת איזה LSN הוא ה-**Rest**,
  אבל לא באיזה תדר. `dsd_pty.tag_event` כבר מחתים **כל** אירוע ב-multi
  ב-`phys_freq_hz` של המפענח שהפיק אותו, וטלמטריית בקרה יכולה להגיע **רק**
  מהערוץ הפיזי שנושא את ה-Rest LSN ⇒ `(rest_lsn, phys_freq_hz)` הוא
  ground-truth. עד v0.11.0 ה-listener זרק את `phys_freq_hz` בענפים האלה.
  `system_intel.record_rest_channel` צובר **הצבעות** (לא קובע מתצפית בודדת),
  `derive_lsn_map` (טהורה) מכריעה לפי `LSN_FREQ_MIN_VOTES`/`_DOMINANCE`,
  ומסיקה את ה-LSN השותף (`source="pair"`, כי זוג LSN חולק ערוץ פיזי) או
  מסמנת `pair_conflict` אם שני החצאים נצפו על תדרים שונים — **לא מתקנים
  הנחה שבורה בשקט**. `POST /api/system-intel/apply-lsn` הוא **המקום היחיד**
  שבו מודיעין-מערכת נוגע ב-`systems.json`, ורק בפעולה יזומה-אנושית.
  ⚠ עובד **רק ב-multi** — בחד-ערוצי `phys_freq_hz` הוא None ואין ממה להסיק.
- **רוסטר:** `_dmr_identity` (RID קודם, אחרת TG) + `_build_roster` (היתוך, כולל אילו
  TG-ים כל RID דיבר — בסיס לגרף RID↔TG). חי בכל מצב.
- **אנליטיקה (Phase 2/3):** `_analytics_source(day, show_all)` — מקור אחיד (היום/
  ארכיון/הכל-בזיכרון), אותם פרמטרים כמו `/api/dmr`. `_encryption_stats` (היסטוגרמת
  ALG + %מוצפן פר-TG — **לעולם לא מפענח**, רק מסכם את התג הקיים). `_traffic_stats`
  (air-time+שיחות פר-TG + heatmap שעתי 0–23). `_rid_tg_graph` (who-talks-to-whom,
  צירי RID→TG ממושקלים, רק שיחות קבוצה). `_lrrp_snapshot` (מיקום אחרון-ידוע פר-RID
  מהזיכרון — "עכשיו" בלבד, כמו `adsb.aircraft_snapshot` ב-AIR-AM; ריק אם הרשת לא
  שולחת LRRP סטנדרטי — Motorola proprietary לא מפוענח ע"י DSD-FME).
  `_unknown_aliases` (worklist: RID-source+target ו-TG שנצפו בתעבורה אך `aliasdb`
  לא פותר **כרגע** — ממוין לפי count; `/api/aliases/unknown`, פאנל ב-UI).
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
| GET | `/api/aliases/unknown` | תור לא-מזוהים: RID/TG שנצפו בתעבורה אך בלי שם, ממוין לפי count (`?day=`/`?all=1`) |
| GET/PUT | `/api/watchlist` | מעקב RID/TG להתראה מקומית (GET=רשימה, PUT=החלפה מלאה) |
| GET | `/api/system-intel` | מודיעין-מערכת נצבר: אתרים/מפת-LSN/CDR/סחיפת-CC + `lsn_map`/`lsn_channelmap` (מיפוי LSN↔תדר שהתגלה) (`?system=<id>`, ברירת מחדל: הפעילה) |
| POST | `/api/system-intel/apply-lsn` | אימוץ המיפוי שהתגלה כ-`channelmap` של המערכת (יזום-אנושית; הפעולה היחידה שבה intel כותב לקונפיג) |
| GET | `/api/health` | בריאות + `calls_today`/`last_call_at` + ★ `listener_alive`/`feed`/`decode_state` (מבדיל "רשת שקטה" מ"שרשרת מתה") |
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
| GET | `/api/rf` | איכות RF: תדירות שגיאות CRC/FEC אמיתית (60ש') + `by_channel` (Phase 7, `multi` בלבד) + `parser_miss`/`handler_errors` + ★ **`level`/`level_by_channel` (dBFS נמדד + `clip_frac`) ו-`gain` (מצב AGC/אינדקס, `readback:false`)** — `null` כשהגשר לא רץ, לא ערך מומצא |
| POST | `/api/gain` | בקרת רווח חיה, בלי לעצור את DSD-FME: `{direction: up\|down}` (נוד יחסי), ★ `{agc: true\|false}` (מצב מפורש — החזרה ל-AGC לא הייתה אפשרית לפני v0.14.0), ★ `{index: 0–28}` (רווח ידני מוחלט) |
| GET | `/api/activity` | הקלטות אחרונות |
| GET | `/recordings/<name>` | קובץ WAV |
| GET | `/api/power` | מתח/טמפ' ה-Pi |

**כלל:** כל route משנה-מצב = `POST` + `_guard`. מעברי-מצב (`/api/mode`) גם נועלים
`TUNE_LOCK`; **`/api/gain` לא** — הקשת נוד-רווח לא מפעילה restart ולא מתחרה על
משאב ה-SDR, אז אין סיבה לחסום אותה מאחורי אותה נעילה.

---

## 7. בדיקות (ללא חומרה)

`python -m pytest tests/ -v` (360 בדיקות, ~19ש'). SDR/systemd/rsp_fm ממוקפים דרך fixtures ב-`conftest.py`:
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
argv טהור של `build_command`/`build_rsp_tcp_command`/`build_bridge_command`, וגם דיספאץ'
רדאר-המערכת: `lsn_status`/`site_info`/`preamble_csbk`/`bank_call`/CC-drift → `system_intel`),
`test_rsp_fm` (הגשר IQ→PCM: דמודולטור, DC-blocker stateful, timeout על `RtlTcpClient`,
`AudioSender`, `RigctlServer`), `test_mode`, `test_boot`, `test_scan`, `test_aliases`,
`test_watchlist` (מעקב RID/TG + `_normalize_dsd` tagging), `test_system_intel` (record_*
טהורות: אתרים/LSN/CDR/CC-drift/debounced-flush + ★ הכרעת מיפוי LSN↔תדר
`derive_lsn_map`/`lsn_map_to_channelmap`: מכסת-הצבעות, רוב, הסקת-זוג,
סתירת-זוג — בלי UDP/Flask), `test_recordings`,
`test_security`, `test_archive`, `test_analytics` (הצפנה/תעבורה/גרף/LRRP), `test_rf_gain`
(שכבת ה-HTTP של `/api/rf`/`/api/gain`), `test_discovery` (גילוי: `validate_sweep_plan`/
`build_freq_grid`/`detect_candidates`/`aggregate_probe`/`discovery_to_system` הטהורים +
שכבת Flask `/api/discover[/save]` + `_discover_loop` e2e ממוקף + collector-via-listener).
**★ `tests/fixtures/dsdfme_source_shapes.csv` (v0.13.0) — פיקסצ'ר שני, מופרד
במוצהר:** 16 צורות שנגזרו מ**קוד-המקור** של DSD-FME (`audio_work`) עבור מצבים
שהרשת שנקלטה לא שידרה — כל שורה עם עמודת `provenance` (`source:dmr_flco.c:545`)
ש-`test_source_fixture_provenance_is_explicit` אוכף. ⚠ **אל תערבבו בין השניים:**
`capplus_slco_sample.csv` הוא קליטה אמיתית ונשאר קודש; כשמגיעה דגימה אמיתית של
מצב שיושב כאן, מעבירים אותה לשם. ר' §8 להסבר למה הכלל הורחב.

**★ `tests/dmr_signal.py` (v0.16.0) — מחולל אות DMR סינתטי:** 4FSK תקני
(4800 סמלים/ש', ±1944/±648 Hz, raised-cosine) שמוזן לשרשרת ה-DSP **האמיתית**
ומודד שיעור שגיאות-סמל. זו הבדיקה היחידה שמוכיחה שהדמודולטור **מפענח DMR**
ולא רק "מייצר PCM סביר". ⚠ **חובה להריץ אותה בכל שינוי ב-`rsp_fm.py`** —
באג הדצימציה (v0.14.0) עבר 300+ בדיקות בירוק בזמן שהתחנה לא פענחה כלום
בשטח, ובדיוק הבדיקה הזאת הייתה תופסת אותו (SER 53% מול 0%).
**v0.17.0** הוציא ממנו את הכלים המשותפים — `mix()` (חיבור אותות עם שמירת
יחס-עוצמות), `demodulate()` (הרצה דרך `NfmDemodulator` **האמיתי**, ב-chunks),
`symbol_error_rate()` (יישור-lag אוטומטי) — כדי ששתי סוויטות ישתמשו באותה
מדידה, ולא בשתי גרסאות שנסחפות.

**★ `tests/test_rf_characterization.py` (v0.17.0) — אפיון RF: איפה זה
נשבר, לא רק שזה עובד.** 15 בדיקות שממפות את **גבולות** השרשרת: עקומת
SER↔SNR (חייבת להיות מונוטונית — עקומה קופצנית = באג-מצב, לא רעש), תקציב
שגיאת-תדר (עד 1kHz), סלקטיביות מול ערוץ-שכן (12.5/25/50 kHz × עוצמה
יחסית 0–30dB, ב-121 taps מול `scaled_taps`), רצפת-הכימות של IQ ב-8 ביט,
וחיתוך — כולל ההבחנה שחיתוך פוגע ב-multi הרבה לפני ערוץ-בודד. **שתי
בדיקות קוראות את קבועי-הסף מ-`index.html` עצמו** (`LEVEL_TARGET_LO`/
`CLIP_BAD`) ומוודאות שהם עדיין מכוילים למדידה — שינוי סף בלי מדידה יפיל
אותן. הקובץ מכוון להיות **הכבד ביותר** (~6ש'); אל תוסיפו לו מקרים בלי
לבדוק זמן-ריצה.

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
- **★ dBFS *כן* נמדד — מ-v0.14.0 (הטענה ההפוכה כאן הייתה מיושנת):** הבולט
  הזה נהג לומר "אין dBFS, נדרש פטצ' קוד C על `rsp_tcp`". הנימוק נכתב
  לארכיטקטורה של **לפני** v0.4.0, כשהגשר עוד לא היה שלנו. היום
  `webtune/rsp_fm.py` מחזיק כל דגימת IQ ב-NumPy, ולכן המדידה היא תוספת
  קטנה **במודול שאנחנו כותבים**, בלי לגעת ב-C:
  - `iq_level_dbfs(raw)` (טהורה) — עוצמת ה-**front-end** מה-IQ הגולמי:
    `rms_dbfs`/`peak_dbfs` (0 dBFS = מלוא סקאלת ה-u8) + **`clip_frac`**,
    שהוא מדד ה-over-gain האמיתי (שיעור הבתים על מסילת 0/255).
  - `NfmDemodulator._measure_level` — עוצמה **פר-ערוץ**, אחרי הפילטר.
  - שניהם נחשפים ב-verb `LEVEL` של rigctl (אותו דפוס pull כמו `SPECTRUM`),
    ומשם ל-`/api/rf` (`level`/`level_by_channel`) ולכרטיס-UI עם מד ואזור-יעד.
  **הכלל של §8 לא נשבר — הוא נאכף:** האיסור היה על **המצאת** מדד, לא על
  מדידתו. באותה מנה נמחק הקבוע המומצא `-50.0` שה-verb `l` החזיר (הפרה
  שהמתינה כאן מאז v0.13.0) — עכשיו הוא מחזיר מדידה אמיתית, או `RPRT 1`
  כשאין מדידה. **תדירות שגיאות CRC/FEC נשארת מדד עצמאי ומשלים** — עוצמה
  גבוהה לא מעידה על פענוח תקין (ר' באג הדצימציה ב-v0.14.0).
  ⚠ **הכיול המוחלט טרם אומת מול מד-עוצמה חיצוני** — הערכים יחסיים-לסקאלה
  ואמינים להשוואה (בין ערוצים, ולפני/אחרי כיוון אנטנה), לא כ-dBm מוחלט.
- **⚠ נוד-רווח (gain) מגיע ל-SDR האמיתי, לא ל-DSD-FME:** מ-v0.4.0, DSD-FME
  כבר לא נוגע ב-SDR בכלל (הוא צרכן אודיו/rigctl בלבד). פקודת רווח מ-`app.py`
  (`dsd_pty.send_gain_command` → `DSD_CTRL_SOCK`) מועברת ע"י `dsd_pty._run()`
  דרך `_send_bridge_control` ל-`rsp_fm.py` (`DSD_BRIDGE_CTRL_SOCK`, unix
  socket), ששולח משם פקודות rtl_tcp אמיתיות (`SET_GAIN_MODE`/
  `SET_GAIN_BY_INDEX`) ל-`rsp_tcp`. **v0.14.0 הוסיף מצב מפורש:** מלבד
  הנודים `g`/`G` יש עכשיו `agc:on`/`agc:off`/`gain:N`, ו-`RtlTcpClient.agc`
  עוקב אחרי המצב. **הבאג שתוקן:** `connect()` מדליק AGC, והנוד הראשון-אי-פעם
  היה מפיל אותו ל-ידני **לצמיתות** — בלי שום פקודת-חזרה (רק restart מלא
  לשירות) ובלי שהמשתמש ידע שזה קרה. עדיין **בלי readback אמיתי**: פרוטוקול
  ה-rtl_tcp הוא כתיבה-בלבד לרווח, ולכן `gain_state()` מחזיר
  `readback: false` **במפורש** — ה-UI אומר "מה שפקדנו", לא מתחזה למדידה.
- **⚠⚠ ה-control-socket חוצה גבול-הרשאות (root↔`dmr`) — חובה `chmod` אחרי
  `bind` (v0.16.1, נתפס בשטח מיד אחרי v0.16.0):** `dmr-dsdfme.service` רץ
  כ-`root`; `dmr-web.service` (מאיפה `/api/gain` שולח) רץ כמשתמש `dmr`
  הלא-פריווילגי. `bind()` על socket חדש משאיר אותו בהרשאות לפי umask של
  היוצר — בדרך כלל `0755` (ל"אחרים" רק read+execute, **בלי write**).
  `sendto()` ממשתמש לא-בעלים לנתיב הזה נכשל תמיד ב-`EACCES`, **בשני
  הכיוונים באופן זהה** (גם `agc:on` וגם `agc:off`), בלי שום קריסה
  שתסביר את זה — ה-journal הראה שירות יציב לחלוטין. `dsd_pty._bind_
  control_socket()` (משותף לחד-ערוצי ול-multi) עושה `os.chmod(path,
  0o666)` מיד אחרי ה-bind. **הלקח:** כל socket/קובץ שנוצר ע"י התהליך
  root ונקרא/נכתב ע"י `dmr-web` (המשתמש הלא-פריווילגי) חייב בדיקת-הרשאות
  מפורשת — אל תסתמכו על umask ברירת-מחדל לחצות את הגבול הזה.
- **⚠ רוחב-הפס לפני הדיסקרימינטור הוא פרמטר-ביצועים, לא פרט טכני
  (v0.16.0):** `DEFAULT_CUTOFF_HZ` היה 10 kHz — רוחב-פס של 20 kHz לערוץ
  DMR של 12.5 kHz שתופס ~7.7 kHz בפועל. העודף הוא **רעש בלבד**, ורעש-FM
  גדל עם התדר (ספקטרום משולש) ⇒ הנזק גדול מיחס-הרוחב. הורד ל-**6 kHz**:
  ב-SNR=10dB שיעור שגיאות-הסמל ירד מ-5.79% ל-0.06% (240kHz) ומ-0.32%
  ל-0.02% (672kHz). **אל תרחיבו אותו בלי למדוד** — ההצרה לא עלתה בכלום
  (אות נקי: 0% שגיאות בכל ערך שנבדק), והשוליים מול שגיאת-תדר נשמרים עד
  ~1 kHz סטייה (ב-164MHz זה 6ppm — הרבה מעבר לכל TCXO סביר).
- **⚠ עוצמה (dBFS) איננה איכות-פענוח — שני מדדים בלתי-תלויים (v0.16.0):**
  נמדד שערוץ ב-‎−1 dBFS נותן SER של 0% ב-SNR אינסופי ו-11% ב-SNR=8dB —
  **אותה קריאה בדיוק במד-העוצמה**. העוצמה עונה רק על "האם כיוון הרווח
  נכון" (רעש-כימות מלמטה, חיתוך מלמעלה); רק **תדירות שגיאות ה-CRC/FEC**
  מעידה אם הפענוח עובד. אל תציגו את המד בשום מקום כאילו ירוק=מפענח.
  הספים מכוילים בסימולציה: ‎−25 dBFS מלמטה (‎−26 נמדד כגבול), clip_frac
  0.05/0.25 מלמעלה (FM הוא מעטפת-קבועה, ולכן 0.06 עדיין 0% שגיאות).
  **★ v0.17.0 — שני חידודים נמדדים:** (א) **רצפת-הכימות** של ה-IQ ב-8 ביט
  היא הגבול התחתון האמיתי, גם **בלי רעש כלל**: ‎−26 dBFS נקי, ‎−34 שולי
  (0.03%), ‎−38 כבר 10% שגיאות-סמל ⇒ הדיווח מהשטח על ‎−36..−39 dBFS פר-ערוץ
  יושב **בדיוק על הצוק**, וזו בעיית-אות (אנטנה/מיקום/מגבר) שאין לה פתרון
  בתוכנה. (ב) **סובלנות-החיתוך תלוית-מצב:** ערוץ בודד סופג `clip_frac`
  0.38 ב-0% שגיאות, אבל ב-multi ה-ADC חותך את **סכום** הערוצים ומייצר
  אינטרמודולציה — 6 ערוצים שווי-עוצמה כבר ב-0.26 נותנים ~2% שגיאות. הסף
  האדום 0.25 ב-UI נכון **ל-multi**; אל תרפו אותו לפי מדידת ערוץ-בודד.
- **⚠⚠ `scaled_taps` תלוי-מרווח — הטענה "נבדק ונמצא מיותר" (v0.16.0) הייתה
  נכונה למרווח שנמדד בלבד (v0.17.0):** המדידה הקודמת בדקה **היסט 21 kHz**
  בלבד (‎−53 dB דחייה) והסיקה ממנה על הכלל. במרווח **12.5 kHz** — המרווח
  התקני של DMR — המסקנה **מתהפכת**: ב-672kHz עם 121 taps שכן ב-12.5 kHz
  הורס את הפענוח כבר בעוצמה **שווה** (SER 23%, נמדד), ועם שכן חזק ב-10dB
  זה 62%. עם `scaled_taps(672k)`=339 אותו מקרה הוא **0%**, זהה לתוצאת
  המנוע החד-ערוצי (240kHz/121 taps). הסיבה פשוטה: רוחב-המעבר הוא
  ~3.3·fs/taps ⇒ 6.5kHz ב-240kHz מול 18kHz ב-672kHz.
  **מה זה לא אומר:** זה **לא** ההסבר לכשלי ה-FEC של v0.14.0 (שם הסיבה
  הייתה פאזת הדצימציה, ונשארת נכונה), ו**לא** רלוונטי לפריסות הפרוסות
  היום — המרווח המינימלי בסקר-השדה הוא 25 kHz, ושם 121 taps עומדים גם
  בשכן חזק ב-30dB (נמדד). הדגל נשאר opt-in כברירת-מחדל **עד החלטה
  מפורשת**; מי שמגדיר מערכת multi עם מרווח <25 kHz חייב להדליק אותו.
  נבדק ב-`tests/test_rf_characterization.py`.
- **⚠⚠ מצב ה-DSP חייב לחצות גבולות-chunk — כל מצב, לא רק ה-overlap
  (v0.14.0):** `NfmDemodulator` נושא כבר מזמן `overlap`/`previous`/DC-blocker/
  `_mix_phase` בין קריאות `process()`, אבל **פאזת הדצימציה** נשכחה:
  `filtered[::D]` התחיל תמיד מאינדקס 0 בכל chunk. זה נכון **רק** כשאורך
  ה-chunk הוא כפולה שלמה של `D` — והחד-ערוצי עמד בזה **במקרה**
  (240000/48000=5, ו-`DEFAULT_CHUNK_SAMPLES`=24000 מתחלק ב-5). **ב-multi
  זה נשבר:** ב-672kHz `D=14`, ו-24000 % 14 = 4 ⇒ בכל גבול-chunk רשת-הדגימה
  החליקה 10 דגימות (~15µs מתוך סמל של 208µs) והזרם יצא ב-**48,020Hz במקום
  48,000** (+417ppm). התוצאה בשטח: DSD-FME מצא `Sync: +DMR` ואז **נכשל
  ב-100% מבדיקות ה-CACH/Burst FEC**, עם `Color Code=XX` — כלומר "יש אות,
  אין פענוח", התסמין המדויק שדווח (27.07.2026). **הלקח הכללי:** כל מצב
  ב-DSP הזה חייב לעבור בין chunks; ובפרט — **אל תניחו ש-chunk מתחלק
  בפרמטר כלשהו**, כי החד-ערוצי יסתיר את הבאג ורק ה-multi יחשוף אותו.
  נבדק ב-`test_decimation_phase_carries_across_chunks_at_multi_rate`.
- **DSD-FME הוא ה"מתאם" היחיד תלוי-פורמט לטקסט הפלט:** אם גרסת DSD-FME משנה ניסוח
  פלט — מתקנים **רק** ב-`dsd_pty.parse_dsd_line` (ונבדק ב-`test_dsd_normalize`, כולל
  replay מול `tests/fixtures/capplus_slco_sample.csv`). שאר הקוד צורך אירועים מוקלדים
  נקיים. **לעומת זאת** — שרשרת האותות (rsp_tcp→rsp_fm.py→DSD-FME) היא תלוית-hardware
  אמיתית ולא נבדקת ב-CI (`dsd_pty._run`/`rsp_fm.run` הם `pragma: no cover`); שינוי בה
  דורש בדיקה על RSP1B אמיתי, לא רק pytest ירוק.
- **⚠ הפיקסצ'ר מוכיח מה *יש*, לא מה *יבוא* (v0.13.0) — הרחבה של כלל "לא מנחשים":**
  הכלל המקורי ("שנה תבניות **רק** לפי דגימות אמיתיות") נכון, ובכל זאת הוא הוליד
  באג חמור: ה-regex של שורת-השיחה הותאם ל-68 הצורות שנקלטו, ובהן היו רק
  `SO=0x00` ו-`SO=0x20` — אז **8 מתוך 9** וריאציות ה-Service Option הפילו את
  הכרטיס כולו בשקט, כולל `Emergency`. לכן הכלל עודכן: תבנית מותר לתקן לפי
  **קליטה אמיתית או קוד-המקור של DSD-FME**, ובמקרה השני חובה (א) פיקסצ'ר
  **נפרד** — `tests/fixtures/dsdfme_source_shapes.csv`, עם עמודת `provenance`
  (`source:dmr_flco.c:545`) שנאכפת בבדיקה, ו-(ב) לא לזהם את
  `capplus_slco_sample.csv`, שנשאר קליטה-בלבד. כשמגיעה דגימה אמיתית של אחד
  המצבים — היא **עוברת** לפיקסצ'ר הקליטה.
- **⚠ ולעולם לא ליפול בשקט: `voice_miss` (v0.13.0).** שורה שנראית כמו שיחה
  (`_RE_VOICE_CALL_LOOSE`) ולא נתפסה ע"י ה-regex המדויק מייצרת אירוע-אבחון
  שנספר ומופיע ב-`/api/rf` (`parser_miss`). נבדק **אחרון** ב-`parse_dsd_line`
  כדי לא לגנוב שורות מתבניות אמיתיות. אם מוסיפים תבנית חדשה — שמרו על
  הסדר הזה, והוסיפו גלאי מקביל אם התבנית קריטית. הלקח מהבאג הוא לא "להוסיף
  טוקנים" אלא שהחמצה תהיה **גלויה**.
- **⚠ `scaled_taps` ב-multi הוא opt-in (`DSD_MULTI_SCALED_TAPS`, כבוי כברירת-מחדל,
  v0.7.3):** רוחב-המעבר של פילטר ה-anti-alias הוא ~3.3·fs/taps, אז 121 taps קבוע
  נותן סלקטיביות גרועה יותר ככל ש-iq_rate גדל (ב-672kHz של multi זה חשוד ל"רק
  2/6 ערוצים נעלו"). `rsp_fm.scaled_taps(iq_rate)` מתאים אותם (339 @ 672kHz)
  לשמור רוחב-מעבר קבוע — **אבל זה ×~2.8 עומס-קונבולוציה על תהליך `rsp_fm.run_multi`
  היחיד, ולא אומת על RSP1B**. לכן `MultiChannelBridge` משתמש ב-121 קבוע (המנוע
  שאומת ב-v0.7.1) **אלא אם** `DSD_MULTI_SCALED_TAPS` דלוק. חד-ערוצי (240kHz) זהה
  byte-for-byte בכל מקרה (`scaled_taps(240k)==121`; `NfmDemodulator` משתמש ב-`taps`
  כפי-שהוא). אימות: A/B בשדה דרך הספייק — `sudo bash scripts/spike-dmr-multi
  multi_164cluster 120 scaled` (ארגומנט-מיקום `scaled`; **לא** `DSD_MULTI_SCALED_TAPS=1
  sudo ...` — sudo מנקה משתני-סביבה!) מול הרצה רגילה — משווים ערוצים-עם-אירועים
  (צריך חלון-תעבורה עמוס) + CPU שיא (מדיד תמיד). **✅ A/B נמדד על חומרה (24.07):
  scaled 339 taps → 139% שיא מול 121 taps → 148% שיא — עלות CPU זניחה (הקונבולוציה
  אינה צוואר-הבקבוק; dsd-fme+atan2 שולטים).** נותר רק לאשר שיפור-פענוח בחלון עמוס
  (כל ההרצות עד כה היו דממת-תעבורה: 0–2 אירועים) → ואז הופכים לברירת-מחדל.
  **★ v0.17.0 — הראיה החסרה נמדדה בסימולציה** (`test_rf_characterization.py`):
  ההבדל אמיתי ומכריע במרווח 12.5 kHz (23%→0% SER), ואפסי במרווח 25 kHz
  ומעלה. כלומר החשד המקורי ("רק 2/6 ערוצים נעלו") **אינו** מוסבר בזה
  ב-`multi_164cluster` (מרווח מינימלי 25 kHz) — ר' הגוצ'ה המפורטת למעלה.
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
  הרעש) — לעולם לא dBFS מוחלט (rsp_tcp נותן dBFS יחסי בלבד). **מיפוי LCN↔תדר
  אינו בר-גילוי ממצב `discover` עצמו:** Cap+ משדר LSN לוגי (לא תדר), ו-SDR יחיד
  במצב סריקה לא יכול לצפות בבקרה ובקול בו-זמנית — הדוח נותן תדר-בקרה+CC+
  LSN-ים-שנצפו, בלי מפת-ערוצים. ⚠ **אבל זה כבר לא נכון לגבי המערכת כולה** —
  מ-v0.12.0 מצב `multi` **כן** מגלה את המיפוי (מפענח לכל ערוץ ⇒ בקרה וקול
  יחד), ר' הגוצ'ה הבאה ו-§5 "מיפוי LSN↔תדר".
- **⚠ מפת הערוצים ל-DSD-FME מאונדקסת ב-LSN, ושורת-הכותרת בה היא חובה
  (v0.12.0):** שתי עובדות שאומתו מול קוד-המקור של DSD-FME (`audio_work`), לא
  מול ניחוש, ושתיהן היו שגויות אצלנו:
  1. `csvChanImport` (`src/dsd_import.c`) עושה `if (row_count == 1) continue;`
     — **מדלג על השורה הראשונה תמיד**, בלי לבדוק אם היא כותרת. בלי
     `CHANNELMAP_HEADER` הערוץ הראשון נזרק בשקט בכל הרצת-טראנקינג.
  2. ב-Cap+ כל תדר נושא **שני** LSN-ים (1+2 = ערוץ פיזי ראשון, 3+4 = השני...)
     — `dmr_csbk.c` מאנדקס `trunk_chan_map[LSN]` וגוזר את ה-slot מזוגיות
     ה-LSN. לכן `render_channelmap(lsn_pairs=True)` מרחיב ערוץ פיזי n לשתי
     שורות. **`lsn_pairs` תלוי-מצב ואסור להפעילו ב-multi:** שם אותו קובץ הוא
     רשימת הערוצים לדמודולציה, והכפלה תייצר מדמודלטורים/מפענחים כפולים.
     `_enter_dmr` מעביר `lsn_pairs=not multi` — אל תשנו את זה בלי לקרוא את
     שתי הסמנטיקות של הקובץ.
  ההרחבה מתקנת את ה**פורמט**; את ה**סדר** (איזה תדר הוא הערוץ הפיזי הראשון)
  מגלה `system_intel.derive_lsn_map` בפועל — עד אז ה-`lcn` שב-`systems.json`
  הוא השערה (בסקר-השדה: מספור לפי סדר-תדרים עולה, לא LSN אמיתי).
- **⚠ אירועי `sync`/`channel_status` ב-`parse_dsd_line` הם opt-in (`emit_status`):**
  ברירת המחדל (dmr/scan רגיל) משאירה אותם `None` — שומר על "סינון housekeeping
  במקור" (§2) ועל ה-fixture replay (68/68). רק בדיקת גילוי (`_probe_system` מגדיר
  `DSD_EMIT_STATUS=1`) מפעילה אותם. שורת sync **עם שגיאה** נשארת `quality` (קדימות).
- **⚠ רדאר-המערכת (`lsn_status`/`bank_call`/`preamble_csbk`/`site_info`,
  v0.11.0) הוא **תמיד פעיל**, בניגוד ל-`sync`/`channel_status` שמעל —
  כי הוא מזין את `system_intel` הרציף (§5), לא רק בדיקת-גילוי חד-פעמית.
  **חובה debounce בכל צרכן-חדש:** `lsn_status` לבדו הוא ~חצי מהפלט האמיתי
  (34/68 בפיקסצ'ר) — צרכן שכותב-לדיסק על כל אירוע ישחק כרטיס-SD (בדיוק
  כמו `system_intel.maybe_flush`). אל תוסיפו צרכן ל-4 הטיפוסים האלה בלי
  debounce/צבירה-בזיכרון. `lsn_status`'s occupant-id **לא מסווג** group/
  private (שני הסוגים תופסים LSN באותה צורה בקליטה שנבדקה) — אל תניחו סיווג.
- **⚠ `render_dmr_env`/`write_dmr_env` דורסים את `/etc/dmr/dmr.env` בכל מעבר מצב:**
  כל מפתח env שהגשר (`rsp_tcp`/`rsp_fm.py`) צריך (`DSD_RTLTCP`/`DSD_AUDIO_TCP`/
  `DSD_RIGCTL`/`DSD_IQ_RATE`/`DSD_AUDIO_GAIN`) **חייב** להופיע כקבוע קשיח בתוך
  `render_dmr_env` עצמו (`DMR_BRIDGE_*` ב-`app.py`) — אחרת הוא נעלם מהקובץ החי בכל
  `_enter_dmr`/מעבר-רגל-סריקה, ו-`dsd_pty`/`rsp_fm` נופלים בשקט על ברירות-המחדל
  שלהם-עצמם (מזל שהן זהות היום ל-`config/dmr.env` — אל תסמכו על זה בעתיד).
- **⚠ תוקן (v0.16.2) — ההערה הקודמת כאן הייתה מטעה:** "gain של SDRplay הפוך:
  ערך קטן = רווח גדול" נכון **רק** לפרמטר הגולמי `gRdB` (gain *reduction*)
  שה-SDRplay API מקבל — אבל **לא** לאינדקס 0–28 שהקוד שלנו חושף (`RtlTcpClient`/
  `GainControlServer`/מחוון ה-UI). אומת מול המקור האמיתי של `SDRplay/
  RSPTCPServer` (`rsp_tcp.c`, טבלאות `rsp1b_vhf_gains_*`): `case 0x0d` →
  `set_gain_by_index(index)` → `gain_index_to_gain(index, &if_gr, &lnastate)`
  → `gRdB=if_gr`. עבור RSP1B/VHF: **אינדקס 0** → `LNAstate=9, gRdB=59` (הכי
  הרבה החלשה, gain הכי נמוך); **אינדקס 28** → `LNAstate=0, gRdB=20` (הכי
  מעט החלשה, gain הכי גבוה שהטבלה מאפשרת). כלומר **האינדקס שלנו עולה עם
  ה-gain בפועל, בדיוק כמו שה-UI מניח — לא הפוך.** נבדק בשטח (27.07.2026):
  משתמש עם `gain=28` וקליטה חלשה מאוד (‎−36 עד ‎−39 dBFS פר-ערוץ) — ה-28
  הזה הוא באמת המקסימום, כך שהחשד "אולי 28 זה בעצם מינימום" נבדק ונפסל;
  המסקנה שהרווח כבר במקסימום ועדיין חלש מצביעה על אנטנה/מיקום, לא על גיין.
- **בידוד + כתיבה אטומית:** `_atomic_write` לכל env/state/channelmap. `threaded=True` ל-Flask.
- **עברית ב-RTL** ב-UI; CSV עם BOM ל-Excel.
- **⚠ מעקב (watchlist, v0.10.0) הוא התראה מקומית — במפורש לא Web Push:**
  Web Push API הסטנדרטי (גם self-hosted) עובר **תמיד** דרך שרת-relay של
  ספק-הדפדפן (Google FCM/Mozilla Autopush/Apple) — זו לא בחירת-מימוש, זו
  איך ה-API בנוי. זה סוטה מ"פרטי-מקומי (בלי ענן)" (§1). לכן: `Notification`
  API (לא Push API — אין subscription/VAPID/שרת), רטט, וצליל (Web Audio
  oscillator, בלי קובץ-מדיה) — הכל בתוך הדפדפן, בלי לצאת לרשת חיצונית.
  מגבלה מודעת: לא יעבוד אם ה-PWA סגור לגמרי (בניגוד ל-Push אמיתי) — זה
  המחיר של "בלי ענן" באמת. אם משנים בעתיד לכיוון Push אמיתי — עדכנו כאן
  ותעדו את הסטייה מהעיקרון במפורש, אל תחליקו את זה.

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
- **Phase 7 (v0.6.0 קוד+CI; v0.7.1 — ✅ מנוע ה-multi אומת על חומרה אמיתית):**
  ב-23.07.2026 הורץ `scripts/spike-dmr-multi multi_164cluster` על Pi 5 + RSP1B:
  **rsp_tcp שרד קליטה רחבת-פס 672kHz** (הסיכון הטכני המרכזי — נסגר), **6 מפענחי
  dsd-fme במקביל** חיו 120ש', **CPU 133% ממוצע/154% שיא מתוך 400%** (headroom),
  ותיוג `phys_lcn` הגיע נכון ב-UDP (1318 אירועים). **סייג:** רק 2/6 ערוצים
  ייצרו אירועים בחלון, רובם `quality`/`encryption` (לא `voice_call`) — **המנוע**
  הוכח, אך כיסוי-קול על *כל* הערוצים הוא שאלת תעבורה-בזמן/קליטה שתיבדק בחלון-שדה
  ארוך. ⚠ **לקח תפעולי:** ריצה שנקטעה משאירה יתומים שמחזיקים את ה-SDR והריצה
  הבאה מתה ב-bring-up (`t≈9s`, 0 אירועים) — הספייק מטאטא יתומים ב-preflight
  מ-v0.7.1 (ר' CHANGELOG). מיזוג עם `DMR-DECREP-SHAHAR` — מצב `multi` חדש: פענוח **כל** ערוצי
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
- **v0.8.0 — תור לא-מזוהים (`/api/aliases/unknown`):** worklist ל-RID/TG
  שנצפו בתעבורה אך `aliasdb` לא פותר כרגע, ממוין לפי פעילות + הקשר (TG-ים/
  מספר-רדיו). פאנל ב-UI עם קלט-שם מוטבע. פיצ'ר-מודיעין ללא-חומרה (§5).
- **v0.9.0 — restart פר-ערוץ ב-multi (✅ אומת על חומרה 25.07.2026):** תיקון
  פער-יציבות שנתפס בסקירה — מפענח DSD-FME בודד שקרס הפיל בעבר את **כל**
  שירות ה-multi (6 ערוצים). עכשיו נקם-מחדש במקומו עד מכסת-restart, אחרת
  הערוץ מוסר וממשיכים בלעדיו, בגלוי (`/api/rf` by_channel + תג ב-UI). ר' §5
  "multi" לפרטים המלאים ולראיות האימות (`sudo kill -9` על 2 מפענחים בזה
  אחר זה על multi_164cluster חי — שניהם קמו-מחדש, ה-service נשאר יציב,
  4 הערוצים האחרים לא הופרעו). מסלול-הוויתור (חריגה מ-3 ניסיונות/5 דק')
  עדיין לא נבדק בפועל — הלוגיקה שלו (`_channel_restart_decision`) כן נבדקת
  ב-CI (allow/deny/prune).
- **v0.10.0 — מעקב RID/TG עם התראה מקומית:** פיצ'ר-תפעול ללא-חומרה.
  `webtune/watchlist.py` (מודול חדש, מראה `aliases.py`) + תיוג ב-`_normalize_dsd`
  + כרטיס-UI + toast/רטט/צליל/`Notification`. **החלטת-ארכיטקטורה מפורשת:**
  לא Web Push (עובר תמיד דרך שרת-relay חיצוני, סותר "בלי ענן" §1) — התראה
  מקומית-בלבד בדף פתוח, ר' §8. תוקן בבדיקה-חזותית (לא code review): batch
  ההיסטוריה הראשון של `pollFeed` היה מציף בהתראות-שווא על שיחות ישנות בכל
  פתיחת-דף — נפתר עם `wlSeeded`. 220 בדיקות ירוקות (209→220).
- **v0.11.0 — רדאר-מערכת (Phase 8): מודיעין מערוץ-הבקרה, מעשיר את
  `systems.json` (קוד+CI ירוקים; טרם אומת על חומרה):** Cap+ הוא רשת-
  טראנקינג — ערוץ-הבקרה משדר טלמטריה על **כל** המערכת, לא רק על עצמו.
  4 טיפוסי-אירוע חדשים ב-`dsd_pty.parse_dsd_line` (**תמיד פעילים**, לא
  emit_status-מותנים כמו sync/channel_status — ר' §8): `lsn_status`
  (מפת-תפוסה חיה של כל הערוצים), `preamble_csbk`/`bank_call` (CDR לשיחות-
  יחיד — מי→מי, גם בלי לשמוע), `site_info` (זהות-אתר, Cap+ רב-אתרי).
  כל הארבעה אומתו מול **כל** הווריאציות האמיתיות בפיקסצ'ר (34 lsn_status
  שונים!, לא דוגמה אחת). `webtune/system_intel.py` (מודול חדש) צובר את זה
  ל-`/var/lib/dmr/system_intel.json` — קובץ-state **נפרד** מ-`systems.json`
  (לעולם לא נכתב חזרה לקונפיג עצמו). זיהוי-סחיפת Color-Code (`cc` שכבר
  מפוענח על `quality` ונזרק עד היום, מושווה מול `color_code` המוגדר).
  `GET /api/system-intel` + כרטיס-UI. **⚠ קריטי:** `lsn_status` לבדו הוא
  ~חצי מהפלט האמיתי — `maybe_flush()` debounced (15ש') + thread-גיבוי
  תקופתי מונעים שחיקת כרטיס-SD. 254 בדיקות ירוקות (220→254), כולל listener
  e2e מלא (UDP→app.py→system_intel). **שרשרת האותות `pragma`-דומה ל-
  `_run_multi`** — הפרסור עצמו נבדק ב-CI מול דגימות אמיתיות, אבל תדירות-
  האמת/וריאציות-נוספות של הטיפוסים החדשים בשטח (מעבר ל-68 דגימות
  הפיקסצ'ר) עדיין לא אומתו על חומרה. אם מגיעה דגימה חדשה שלא תואמת —
  מוסיפים לפיקסצ'ר, לא ממציאים.
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
  ⚠ **ה-`lcn` בקובץ הזה הוא אינדקס-סריקה, לא LSN אמיתי** (v0.12.0): הסקר מדד
  נוכחות-DMR + CC פר-תדר, הוא לא קרא Rest LSN מערוץ-בקרה מעולם — המספור הוא
  פשוט סדר-תדרים עולה. במצב `multi` זה לא מזיק (ה-LCN הוא תווית; התדרים הם
  מה שקובע, והם נמדדו), אבל במצב `dmr` עם טראנקינג זו מפת-LSN שגויה. תוקן ב-
  `multi_164cluster` גם `color_code` (8→10 — הסקר מדד CC=10 על תדר-הבקרה
  המוגדר). המיפוי האמיתי מתגלה בהרצת `multi` — ר' §5 "מיפוי LSN↔תדר".
- **v0.12.0 — מיפוי LSN↔תדר אמיתי + תיקון מפת-הערוצים של DSD-FME (קוד+CI
  ירוקים + אימות חזותי; תדירות-הגילוי בשטח טרם נמדדה):** התחיל משאלת-משתמש
  על ה-LCN בקונפיג-הסקר, והוליד שני תיקונים שאומתו מול **קוד-המקור של
  DSD-FME** (`audio_work`): (1) שורת-כותרת חסרה ⇒ הערוץ הראשון במפה נזרק
  בשקט תמיד (`csvChanImport`: `if (row_count == 1) continue;`); (2) מפת Cap+
  מאונדקסת ב-LSN וכל תדר נושא שני LSN-ים ⇒ `render_channelmap(lsn_pairs=True)`.
  ובנוסף הפיצ'ר עצמו: `system_intel.record_rest_channel`/`derive_lsn_map`
  מגלים את המיפוי מהצבעות ground-truth ב-multi (ר' §5/§8), עם
  `POST /api/system-intel/apply-lsn` לאימוץ יזום-אנושית. 275 בדיקות (254→275).
  **הצעד הבא אם הכיסוי יתגלה חלקי:** קורלטור שני — occupant-id של `lsn_status`
  מול TG/יעד של שיחה שנשמעה בו-זמנית על ערוץ פיזי ידוע (הנתונים כבר זורמים;
  לא מומש בכוונה כדי לא להוסיף מקור-אי-ודאות שני לפני שהראשון נמדד בשטח).
- **v0.13.0 — כשלים שקטים (מנת-תיקונים, לא פיצ'רים; CI+חזותי, 302 בדיקות):**
  נולד מסבב-חשיבה שבו ארבעה סוכנים נשלחו לטעון עמדות מנוגדות על "הפיצ'ר הבא",
  ושלושה מהם הגיעו **בנפרד** לאותה מסקנה: יש כאן באגים שמשמידים נתונים בשקט.
  כל ארבעתם שוחזרו בהרצה לפני התיקון והם עכשיו בדיקות-רגרסיה:
  (1) דאטהגרם פגום בודד הרג את ה-listener לנצח (`int()` חשוף בענף שלא היה
  עטוף) בזמן ש-`/api/health` דיווח `ok=True`; (2) 8/9 וריאציות SO של שורת-
  השיחה נדחו ⇒ **שיחות חירום לא הופיעו בשום מקום**; (3) `dur`/`frames`/
  `encrypted`/`id` לא הגיעו לדיסק אף פעם ⇒ `?day=` ו-CSV דיווחו אפס לנצח;
  (4) `systemctl restart dmr-web` השתיק את כל מודיעין-המערכת (v0.11+v0.12).
  נוסף גם מה שכל הסוכנים סימנו כפער החמור ביותר לתחנת-האזנה: **הבחנה בין
  "רשת שקטה" ל"שרשרת מתה"** (`_decode_state`, ר' §5), וגלאי-החמצה בפרסר
  (`voice_miss` → `/api/rf`) כדי שהבאג מסוג (2) לא יחזור בשקט.
  **⚠ נשאר פתוח בהחלטה (אומת בסבב, לא תוקן):** נגן ההקלטות ב-UI הוא קוד מת
  (`wav` לעולם לא מוצב, בניגוד להבטחה ב-README); `/api/positions` ריק מבנית
  (`_RE_LRRP_POS` — קבוצת `src` שלא יכולה להתאים, כי `dmr_pdu.c` מדפיס SRC רק
  כשאין lat; התיעוד תולה את זה בטעות ב"Motorola proprietary" — **הקואורדינטות
  כן מפוענחות**, יש דגימה בפיקסצ'ר); ה-UI לא שולח `X-DMR-PIN` ⇒ ה-PIN המתועד
  הופך את התחנה לקריאה-בלבד; ~~אין CRUD למערכות ב-UI~~ (נסגר ב-v0.15.0);
  ~~`rsp_fm.py:583` מחזיר `-50.0` **מומצא**~~ (נסגר ב-v0.14.0); `rsp_fm.py` מחזיר
  `-50.0` **מומצא**; ו-`index.html:1201` טוען אריחי-מפה מ-OSM — תלות-ענן חיה
  שסותרת §1. וריאציות `data_header` (UDT/Short Data) עדיין נדחות.
- **★ v0.14.0 — "יש אות, אין פענוח": באג הדצימציה ב-multi + סוף העיוורון
  (נתפס בשטח, 27.07.2026):** התחנה **מעולם לא פענחה שיחה אמיתית מקצה-לקצה**
  (v0.4.0 אימת שהשרשרת *עולה*, לא שהיא *מפענחת* — ר' Phase 5), והדיווח
  מהשטח היה `Sync: +DMR` ואחריו 100% כשלי `CACH/Burst FEC ERR` עם
  `Color Code=XX`, בכל 6 הערוצים — כולל ערוצים במרחק 531kHz זה מזה, מה
  שפסל את תיאוריית "דליפה מערוץ שכן"/`scaled_taps`. שלושה תיקונים:
  1. **פאזת הדצימציה לא נשמרה בין chunks** — הבאג עצמו. ר' הגוצ'ה ב-§8;
     ב-multi הזרם יצא ב-48,020Hz עם קפיצת-תזמון בכל chunk, וזה בדיוק
     "sync נתפס, FEC נכשל". **חד-ערוצי לא הושפע** (24000 מתחלק ב-5).
  2. **מדידת עוצמה אמיתית** (`iq_level_dbfs` + `_measure_level` → verb
     `LEVEL` → `/api/rf` → מד-UI עם אזור-יעד ואזהרת `clip_frac`). המשתמש
     כיוון אנטנה ורווח **בלי שום מחוון** — ר' §8; גם נמחק הקבוע המומצא
     `-50.0` שהופיע ברשימת-הפתוחים של v0.13.0.
  3. **בקרת AGC מפורשת** (`agc:on`/`agc:off`/`gain:N`) — עד כה הנוד הראשון
     הפיל את ה-AGC לצמיתות בלי דרך חזרה ובלי שהמשתמש ידע.
  ובנוסף, בעקבות שאלת-משתמש ("ה-LCN וה-CC מבלבלים ולא עוזרים"): **טראנקינג
  הוא עכשיו ברירת-מחדל רק ל-≥2 ערוצים** (`_default_trunk`/`_system_trunks`).
  מערכת חד-ערוצית — כמו 16 מערכות סקר-השדה — פשוט מכוונת ומפענחת, בלי
  `-T/-C/-U` ובלי `lsn_pairs` שהכפיל את הערוץ היחיד לשתי שורות-LSN מדומות.
  ⚠ **ה-Color Code ממילא לא הועבר מעולם ל-DSD-FME** (הוא מתגלה מהאוויר);
  `DSD_COLOR_CODE` משמש **רק** לזיהוי סחיפה ב-`system_intel`. 333 בדיקות.
  **טרם אומת על חומרה** — הפענוח עצמו הוא מה שצריך להיבדק עכשיו בשטח.
- **v0.15.0 — יצירת מערכות מהממשק + מחוון רוחב-פס:** נסגר הפער "אין CRUD
  למערכות ב-UI" (v0.13.0): "+ מערכת"/מחיקה/שם/"📋 הדבק תדרים". ה-`id` נגזר
  מחותמת-זמן ולא מהשם (סכימת `_validate_systems` אוסרת עברית ב-id). נוסף
  **מחוון פריסה⇒רוחב-חלון** שמריץ בצד-הלקוח את אותה נוסחה של
  `compute_wideband_plan` ומציע פיצול כשהמרווח הגדול מצדיק. ⚠ הרקע החשוב:
  **הרוחב נקבע מהפריסה, לא ממספר הערוצים** — 6 ערוצים צפופים זולים יותר
  מ-2 ערוצים מרוחקים. תוקן גם שטיוטה לא-מלאה מנעה שמירה של כל הסט.
- **הכיוונים שנבחרו להמשך (מסבב v0.13.0):** שכבת ה-Data (סיווג לפי פורט —
  4001=LRRP, 4005=ARS, 4007=הודעות טקסט; + תיקון המפה + מהירות/כיוון/גובה),
  **Talker Alias** (הרדיואים משדרים את שמם; `dsd_alias.c:744` — ⚠ אפס דגימות
  בקליטה שלנו, נוכחות דורשת חלון-שדה), ו"למצוא ולשמוע" (השמעת הקלטות בפועל,
  חיפוש חוצה-ימים בשרת, click-through מפאנלי הניתוח לשיחות).
- **⚠ תוקן בתיעוד (v0.13.0) — שתי טענות שהיו נכונות ואינן:**
  1. **מד dBFS *אינו* דורש עוד פטצ' קוד C.** הנימוק ב-§8 ("אין side-channel
     ב-`rsp_tcp`") נכתב לארכיטקטורה של **לפני** v0.4.0, כשהגשר לא היה שלנו.
     היום `webtune/rsp_fm.py` מחזיק כל דגימת IQ ב-NumPy (`baseband` ב-
     `NfmDemodulator.process`, שורות 283-291) ⇒ RMS→dBFS **פר-ערוץ** הוא
     תוספת קטנה במודול שאנחנו כותבים, ונבדקת ב-`test_rsp_fm` הקיים. מה
     שנשאר נכון: המספר חייב להיות **נמדד** ולא מומצא, וצריך אימות על חומרה
     לפני שסומכים על הכיול שלו.
     ⚠ ובינתיים יש **קבוע מומצא בקוד שלנו**: `rsp_fm.py:583` מחזיר
     `"-50.0\n"` ל-verb `l` של rigctl. כרגע אין לו קורא (`GetSignalLevel`
     ב-`dsd_rigctl.c` מוגדר ולא נקרא), אבל זו הפרה של §8 שממתינה — למחוק
     או להחליף במדידה אמיתית.
  2. **lockout/hold דרך הזרקת מקשים ל-PTY אינו אפשרי.** `ncurses_input_handler`
     נקרא **רק** מ-`dsd_ncurses_printer.c:1704`, וכל קריאה ל-`ncursesPrinter`
     מגודרת ב-`opts->use_ncurses_terminal == 1` — כלומר דורשת `-N`, שתהרוס
     את הפרסור השורתי שלנו. בלי `-N` אף מקש לא נקרא. (ערוץ ה-`DSD_CTRL_SOCK`
     שלנו ממילא מנותב מ-v0.4.0 ל-`rsp_fm.py` ולא ל-DSD-FME — ר' §8.) הדרך
     האמיתית למדיניות פר-TG היא דגל `-G` (group list עם allow/block) ו-`-I`
     (TG hold), כלומר קונפיג + restart.
- **נדחה במכוון (דורש חומרה לאימות):** כיול מד dBFS/SNR — המימוש עצמו כבר
  אפשרי בלי פטצ' C (ר' הסעיף שמעל), אבל הערך המדווח חייב אימות על RSP1B
  אמיתי לפני שמסתמכים עליו. ר' §8.
- **הבא (לא מתוכנן עדיין):** רעיונות שעלו בסיעור-המוחות המקורי ולא נכנסו ל-scope —
  מדיניות פר-TG דרך `-G`/`-I` (**לא** דרך הזרקת-מקשים, ר' התיקון שמעל),
  Web Push להתראות watchlist (סותר §1 — ר' §8), ייבוא/ייצוא מערכת כ-QR,
  מעקב multi-site (`Capacity Plus Adjacent Sites`, `dmr_csbk.c:1291` — מפת
  השכנים קיימת בפרוטוקול ולא נצפתה ב-68 הדגימות שלנו; דורש חלון-שדה).
