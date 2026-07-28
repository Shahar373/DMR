# RUNBOOK — סשן שטח מודרך (Pi 5 + RSP1B)

**כל שלב הוא בלוק להעתקה-והדבקה.** המבנה הוא **חצייה בינארית**: כל שלב
שולל סיבה אחת, ומריצים את הבא רק אם הקודם ירוק. זה מכוון — התסמין "אין
שיחות בממשק" נגרם משלוש סיבות שנראות זהות מבחוץ (אין אות / האות לא DMR /
השרשרת שבורה), ובלי סדר מפרידים ביניהן בניחוש.

**רקע — מה כבר ידוע (נמדד בסימולציה, CHANGELOG v0.17.0):** IQ ב-8 ביט נותן
פענוח נקי עד ‎−26 dBFS, שולי ב-‎−34, ו-10% שגיאות-סמל ב-‎−38. הדיווח מהשטח
(‎−36..−39 dBFS) יושב **על הצוק**. שלב 1 מאשר או שולל את זה.

---

## שלב 0 — עדכון הפי והכנה

```bash
# --- 0.1 עדכון הקוד על הפי ---
cd ~/DMR
git fetch origin
git checkout claude/rf-decoding-stability-tests-igemcb || \
  git checkout -b claude/rf-decoding-stability-tests-igemcb origin/claude/rf-decoding-stability-tests-igemcb
git pull origin claude/rf-decoding-stability-tests-igemcb
sudo ./install.sh
cat /opt/dmr/webtune/VERSION          # חייב להראות 0.18.0
```

```bash
# --- 0.2 שחרור ה-SDR (חובה לפני rf_probe — "SDR אחד בהחלפה") ---
curl -s -X POST localhost:8080/api/mode -H 'Content-Type: application/json' -d '{"mode":"off"}'
sudo systemctl stop dmr-dsdfme
sudo pkill -f rsp_tcp ; sudo pkill -f rsp_fm ; sudo pkill -f dsd-fme ; sleep 2
```

```bash
# --- 0.3 ודא שהבסיס חי ---
systemctl is-active sdrplay dmr-web
lsusb | grep -i 1df7                  # RSP1B מחובר
pgrep -af "rsp_tcp|rsp_fm|dsd-fme"    # חייב להיות ריק (אין יתומים)
```

⚠ אם 0.3 מראה תהליכים — חזור ל-0.2. יתום מחזיק את ה-SDR, וכל ריצה הבאה
מתה ב-bring-up (t≈9s, 0 אירועים) בלי הודעת-שגיאה מובנת.

---

## שלב 1 — האם יש בכלל אות, ומה איכותו? ⬅ **הכי חשוב**

⚠ **אל תתחיל ב-multi.** הוא מוסיף שלושה משתנים (רוחב-פס, היסטים, N מפענחים)
לבעיה שעדיין לא בודדה.

```bash
# --- 1.1 הערוץ הבודד החזק ביותר בסקר ---
sudo python3 /opt/dmr/webtune/rf_probe.py capture \
     --freq 164.3 --seconds 8 --out /tmp/iq_164_3.bin
python3 /opt/dmr/webtune/rf_probe.py analyse /tmp/iq_164_3.bin | tee /tmp/probe_164_3.txt
```

```bash
# --- 1.2 עוד שני ערוצים, לוודא שזו לא בעיה של ערוץ בודד ---
for F in 164.5375 164.725 162.525; do
  sudo python3 /opt/dmr/webtune/rf_probe.py capture --freq $F --seconds 6 --out /tmp/iq_$F.bin
  python3 /opt/dmr/webtune/rf_probe.py analyse /tmp/iq_$F.bin | tee -a /tmp/probe_all.txt
done
```

**איך לקרוא את ההכרעה:**

| ההכרעה בפלט | מה זה אומר | הצעד הבא |
|---|---|---|
| `אין אות בפס כלל — רעש בלבד` | שיא-הספקטרום ≈ החציון. אין שם כלום. | אנטנה/חיבור/תדר. **אל תחפש באג-תוכנה.** |
| `אות מתחת לרצפת-הכימות` | ‏<‎−34 dBFS **וכל הפס חלש**. | אנטנה/מיקום/מגבר-קדם. אין פתרון בתוכנה. |
| `הערוץ הזה ריק — הפס עצמו פעיל` | יש אות בפס, לא כאן. הפלט אומר כמה kHz. | חכה לתעבורה, או בדוק שהתדר במפה נכון. |
| `אין מודולציית DMR בתדר הזה` | נשא בלי סמלים. | ערוץ שקט — חזור בשעת תעבורה. |
| `אות מעוות — האנרגיה החזקה אינה בערוץ` | הפלט אומר **כמה kHz** מהערוץ יושב השיא. | הפרעת-שכן, או שהתדר האמיתי הוא מקום-השיא. |
| `אות DMR שולי` | צפויות שגיאות FEC בודדות. | שפר אנטנה/רווח → שלב 2, ואז 3. |
| **`אות DMR תקין — העין פתוחה`** | ה-RF תקין. | ⇒ **שלב 3**. אם עדיין אין פענוח — הבעיה אינה RF. |

---

## שלב 2 — האם הרווח בכלל משפיע, ומה המקסימום האמיתי?

לפרוטוקול rtl_tcp **אין readback** (ולכן `/api/gain` מדווח `readback:false`
והממשק מציג "מה פקדנו", לא מה קרה). זו המדידה היחידה שסוגרת את זה:

```bash
sudo python3 /opt/dmr/webtune/rf_probe.py gain-sweep --freq 164.3 | tee /tmp/gain_sweep.txt
```

```bash
# רזולוציה גבוהה יותר סביב הקצה העליון, אם 28 נראה רווי
sudo python3 /opt/dmr/webtune/rf_probe.py gain-sweep --freq 164.3 \
     --indices 20,22,24,26,27,28 | tee -a /tmp/gain_sweep.txt
```

**מה מחפשים:** העוצמה עולה עם האינדקס (⇒ הכיוון תקין, כפי שנקבע ב-v0.16.2);
היכן היא מפסיקה לעלות (⇒ ה-gain האפקטיבי המקסימלי); ו-`clip` שלא יעבור 0.05.
אם העוצמה **לא זזה בכלל** — פקודות הרווח לא מגיעות (בדוק את הרשאות
ה-control-socket, הבאג של v0.16.1).

```bash
# קובעים את הרווח הנבחר לריצה החיה (החלף 28 במה שיצא הכי טוב)
curl -s -X POST localhost:8080/api/gain -H 'Content-Type: application/json' -d '{"index":28}'
```

---

## שלב 3 — האם התחנה מפענחת שיחה אמיתית?

זו השאלה שמעולם לא נענתה מקצה-לקצה. **הרץ רק אם שלב 1 חזר "תקין"/"שולי".**

```bash
# --- 3.1 מצב חד-ערוצי על הערוץ שנבדק ---
curl -s -X POST localhost:8080/api/mode -H 'Content-Type: application/json' \
     -d '{"mode":"dmr","system":"vhf164_3"}' ; echo
sleep 20
systemctl is-active dmr-dsdfme        # חייב active
```

```bash
# --- 3.2 טרמינל נפרד: ה-log הגולמי (מפריד "סנכרון בלי FEC" מ"אין סנכרון") ---
sudo journalctl -u dmr-dsdfme -f | grep --line-buffered -Ei "sync|FEC|Color Code|Voice|Slot|TGT"
```

```bash
# --- 3.3 טרמינל נפרד: מצב חי כל 10ש' ---
watch -n 10 'curl -s localhost:8080/api/health | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(\"decode_state:\", d.get(\"decode_state\"), \"| listener:\", d.get(\"listener_alive\"))
print(\"feed:\", d.get(\"feed\"))"; \
curl -s localhost:8080/api/rf | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(\"errors/min:\", d[\"errors_per_min\"], \"| parser_miss:\", d[\"parser_miss\"])
print(\"level:\", d[\"level\"]); print(\"gain:\", d[\"gain\"])"'
```

```bash
# --- 3.4 האם נכנסו כרטיסי שיחה? ---
curl -s localhost:8080/api/dmr | python3 -c "
import json,sys; d=json.load(sys.stdin); m=d.get('msgs',[])
print(len(m),'כרטיסים'); [print(x) for x in m[-5:]]"
```

**נקודות-הכרעה:**

| מה רואים | פירוש | הצעד הבא |
|---|---|---|
| `decode_state: silent`, journal ריק | רשת שקטה או אין נעילה | חכה לחלון תעבורה |
| `Sync: +DMR` ואז `FEC ERR` בכל פריים | "יש אות, אין פענוח" | שמור פלט + קובץ IQ, שלח אליי |
| `parser_miss` עולה | הפרסר מפספס שורות אמיתיות | שמור את השורות מה-journal |
| כרטיסים מופיעים ב-3.4 | ✅ **התחנה מפענחת** | ⇒ שלב 4 |

---

## שלב 4 — multi (רק אחרי ששלב 3 ירוק)

```bash
# --- 4.1 הפעלה ---
curl -s -X POST localhost:8080/api/mode -H 'Content-Type: application/json' \
     -d '{"mode":"multi","system":"multi_164cluster"}' ; echo
sleep 45
```

```bash
# --- 4.2 מצב פר-ערוץ ---
curl -s localhost:8080/api/rf | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('front level:', d['level'])
for c in d.get('level_by_channel',[]): print(' ערוץ', c)
for c in d.get('by_channel',[]):       print(' איכות', c)" | tee /tmp/multi_rf.txt
```

**מה לבדוק:** כל 6 הערוצים מעל ‎−34 dBFS? `clip_frac` מעל 0.25 ⇒ **הורד**
רווח (ב-multi ה-ADC חותך את סכום הערוצים, והאינטרמודולציה פוגעת הרבה לפני
שהיא פוגעת בערוץ בודד). `restarting` שחוזר על אותו ערוץ = בעיה אמיתית שם.

```bash
# --- 4.3 ניתוח offline של כל 6 הערוצים מהקלטה רחבת-פס אחת ---
curl -s -X POST localhost:8080/api/mode -H 'Content-Type: application/json' -d '{"mode":"off"}'
sudo systemctl stop dmr-dsdfme ; sleep 2
sudo python3 /opt/dmr/webtune/rf_probe.py capture --freq 164.415625 \
     --iq-rate 672000 --seconds 8 --out /tmp/iq_multi.bin
python3 /opt/dmr/webtune/rf_probe.py analyse /tmp/iq_multi.bin --iq-rate 672000 \
     --center 164.415625 \
     --freqs 164.10625,164.3,164.325,164.5375,164.6375,164.725 | tee /tmp/probe_multi.txt
```

זה נותן שורת-הכרעה **לכל ערוץ בנפרד** — תשובה ישירה לשאלה הפתוחה מ-Phase 7
("למה רק 2/6 ערוצים ייצרו אירועים"): אם 4 ערוצים חוזרים "ריק"/"מתחת לרצפה",
זו תעבורה/קליטה, לא באג ב-multi.

---

## שלב 5 — A/B של `scaled_taps` (עדיפות נמוכה)

הסימולציה כבר הכריעה: במרווח 25kHz (הפריסה שלך) **אין הבדל**; ההבדל מכריע
רק ב-12.5kHz. הרץ רק אם שלב 4.3 הראה ערוצים מעוותים עם שיא-אנרגיה של שכן קרוב.

```bash
sudo systemctl stop dmr-dsdfme ; sleep 2
sudo bash scripts/spike-dmr-multi multi_164cluster 120          # 121 taps
sudo bash scripts/spike-dmr-multi multi_164cluster 120 scaled   # 339 taps
```

⚠ הדגל עובר כארגומנט-מיקום `scaled` — **לא** כמשתנה-סביבה (sudo מנקה אותם).

---

## איסוף התוצאות לשליחה

```bash
# אורז את כל מה שנאסף לקובץ אחד
{ echo "=== VERSION ==="; cat /opt/dmr/webtune/VERSION
  echo "=== probe ==="; cat /tmp/probe_*.txt 2>/dev/null
  echo "=== gain sweep ==="; cat /tmp/gain_sweep.txt 2>/dev/null
  echo "=== multi rf ==="; cat /tmp/multi_rf.txt 2>/dev/null
  echo "=== api/rf ==="; curl -s localhost:8080/api/rf
  echo; echo "=== api/health ==="; curl -s localhost:8080/api/health
  echo; echo "=== journal ==="; sudo journalctl -u dmr-dsdfme -n 200 --no-pager
} > /tmp/dmr_field_report.txt 2>&1
ls -lh /tmp/dmr_field_report.txt /tmp/iq_*.bin
```

שלח את `/tmp/dmr_field_report.txt`, ואם משהו נראה חריג — גם את **קובץ ה-IQ**
(`/tmp/iq_164_3.bin`, ~4MB לשנייה ב-240kHz). זה הנכס היקר ביותר: ממנו אפשר
לשחזר את הבעיה offline, להוסיף אותה כפיקסצ'ר-רגרסיה, ולתקן בלי גישה לחומרה.

---

## חזרה למצב עבודה

```bash
curl -s -X POST localhost:8080/api/mode -H 'Content-Type: application/json' \
     -d '{"mode":"multi","system":"multi_164cluster"}' ; echo
systemctl is-active dmr-dsdfme dmr-web
```

## תקלות נפוצות

```bash
# "פעולה אחרת מתבצעת" (409) — סבב סריקה/גילוי תקוע
curl -s -X POST localhost:8080/api/mode -H 'Content-Type: application/json' -d '{"mode":"off"}'

# rf_probe נתקע ב-bring-up / 0 בתים — יתומים מחזיקים את ה-SDR
sudo pkill -f rsp_tcp ; sudo pkill -f rsp_fm ; sudo pkill -f dsd-fme ; sleep 2

# ה-API לא עונה
sudo systemctl restart dmr-web && sudo journalctl -u dmr-web -n 50 --no-pager

# ה-SDR לא נמצא
sudo systemctl restart sdrplay && lsusb | grep -i 1df7
```
