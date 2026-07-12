# DMR — תחנת האזנה לרשתות DMR, בשליטה מהטלפון

**DMR** הופך **Raspberry Pi 5 + SDRplay RSP1B** לתחנת פענוח **רשתות DMR** (במיוחד
**Motorola Capacity Plus / Cap+**) שנשלטת **כולה מהטלפון דרך הדפדפן** — בלי אפליקציה
ובלי סיסמאות. הפענוח נעשה ב-**DSD-FME** מקומית על ה-Pi; שום דבר לא עולה לענן.

> אחיו של פרויקט **AIR-AM** (האזנת תעופה): אותה ארכיטקטורה — שליטה מהטלפון,
> zero-config, headless ועמיד, פרטי-מקומי — מוחלת כאן על DMR במקום תעופה.

---

## מה זה עושה

- **מעקב טראנקינג (Cap+):** שרשרת מקומית ממירה IQ מה-SDRplay ל-NFM PCM עבור
  DSD-FME, ושרת rigctl פנימי מעביר את פקודות הכיוון של DSD-FME חזרה ל-RSP1B.
- **פיד שיחות חי:** כרטיס לכל שיחה — Talkgroup, Radio ID (מקור), timeslot, סוג שיחה
  (קבוצה/פרטית/נתונים/מיקום), תג 🔒 להצפנה.
- **אליאסים:** שמות ל-TG/RID (ייבוא `user.csv` של RadioID.net + עריכה מהטלפון).
- **הקלטות:** DSD-FME מקליט WAV לכל שיחה; מתנגן ישירות בדפדפן (תמלול whisper אופציונלי).
- **רוסטר מאוחד:** מי פעיל, כמה, מתי, ועם אילו talkgroups (חי גם ב-standby).
- **מערכות מרובות + סריקה:** שומרים כמה רשתות Cap+ ומסובבים ביניהן אוטומטית לפי לוח.
- **ניתוח (📊):** היסטוגרמת אלגוריתמי הצפנה + %מוצפן פר-talkgroup, אנליטיקת תעבורה
  (heatmap שעתי + air-time לפי TG), גרף RID↔TG ("מי מדבר לאן"), ומפת מיקום LRRP.
- **איכות RF ובקרת רווח (📶):** תדירות שגיאות CRC/FEC אמיתית מ-DSD-FME וכפתורי
  נוד-רווח חי. **אין מד dBFS/SNR רציף** — DSD-FME לא מספק כזה.

> ⚠️ **מיועד לרשת פרטית מהימנה בלבד ולהאזנה חוקית.** אין פענוח תעבורה מוצפנת בלי
> מפתח — התוכנה רק מציינת שהשיחה מוצפנת.

---

## התקנה (פקודה אחת)

על ה-Pi (Raspberry Pi OS 64-bit, Pi 5/Pi 4):

```bash
git clone https://github.com/Shahar373/DMR.git
cd DMR
chmod +x install.sh
sudo ./install.sh
```

הסקריפט מתקין אוטומטית את SDRplay API,‏ SoapySDRPlay3,‏ mbelib,‏ DSD-FME,
`rsp_tcp`, גשר ה-IQ→PCM/rigctl, שרת הבקרה ושירותי systemd. עדכון:

```bash
git pull
sudo ./install.sh
```

תמלול אופציונלי (בנייה ארוכה):

```bash
INSTALL_DMR_WHISPER=1 sudo ./install.sh
```

---

## שימוש

פותחים בטלפון: **`http://<IP-של-ה-Pi>:8080`**

1. **בית → מערכת DMR:** מזינים תדר ערוץ בקרה (MHz), color code ומפת LCN→תדר.
2. לוחצים **התחל DMR**. לאחר מספר שניות מופיעות שיחות בתצוגת **📻 שיחות**.
3. מוסיפים שמות TG/RID או מייבאים `user.csv` של RadioID.net.
4. ניתן להגדיר סריקה בין מספר מערכות לפי לוח.
5. **כיבוי (⏻)** עוצר את כל שרשרת הקליטה ומשחרר את ה-SDR.

### ייבוא שמות רדיו (RadioID.net)

```bash
curl -L -o /etc/dmr/rid.csv "https://radioid.net/static/user.csv"
sudo systemctl restart dmr-web
```

---

## ארכיטקטורה

```text
אנטנה ─► RSP1B ─USB─► sdrplay.service
                          │
                       rsp_tcp                  IQ u8 @ 240 kHz
                          │
                       rsp_fm.py                NFM + decimation
                          ├────────► PCM TCP 48 kHz ─────► DSD-FME
                          └◄─────── rigctl retunes ◄──────┘
                                                           │
                                                     PTY text output
                                                           │
                         dsd_pty.py ── JSON/UDP 5555 ─► dmr-web :8080
```

DSD-FME **אינו** לקוח `rtl_tcp`; לכן אין להעביר אליו `-i rtltcp:...`. הגשר
`rsp_fm.py` הוא שכבת ההתאמה בין IQ של `rsp_tcp` לבין קלט ה-PCM ופקודות ה-rigctl
ש-DSD-FME תומך בהם בפועל.

**SDR אחד, בהחלפה:** `dmr-dsdfme` הוא צרכן ה-SDR היחיד ומפקח על כל תהליכי הבן.
מצב `off` עוצר את ה-cgroup ומשחרר את המכשיר. `dmr-web` משחזר את המצב לאחר reboot.

**הפרסור מבוסס קליטה אמיתית:** `parse_dsd_line` אומת מול 20,000 שורות אמיתיות
מרשת Cap+/SLCO רב-אתרית. כל פלט DSD-FME הגולמי משוקף גם ל-journald לצורך אבחון.

פרטים מלאים: ראו [`CLAUDE.md`](CLAUDE.md).

---

## לוגים ותחזוקה

```bash
sudo journalctl -u dmr-dsdfme -f
sudo journalctl -u dmr-web -f
```

בדיקות ללא חומרה:

```bash
python3 -m pytest tests/ -v
python3 webtune/dsd_pty.py --selftest
```

לאחר שינוי בשכבת ה-RF יש לבצע גם בדיקת חומרה על Pi + RSP1B: נעילה על ערוץ המנוחה,
מעבר לערוץ קול לפי grant, חזרה לערוץ המנוחה ויצירת WAV ב-`/var/lib/dmr/recordings`.

---

## אבטחה

- `dmr-web` רץ כמשתמש לא-root (`dmr`) עם sudoers ממוקד ל-restart/stop של
  `dmr-dsdfme` בלבד.
- הגנת CSRF/DNS-rebind לכל בקשה משנת-מצב; PIN אופציונלי (`DMR_PIN`).
- **אל תחשוף את 8080 לאינטרנט.** לגישה מרחוק — VPN/Tailscale.
