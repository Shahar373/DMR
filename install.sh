#!/usr/bin/env bash
# ============================================================================
#  DMR  -  התקנה מלאה ל-Raspberry Pi (Pi 5 / Pi 4, Raspberry Pi OS 64-bit)
# ----------------------------------------------------------------------------
#  מתקין הכל אוטומטית: SDRplay API, SoapySDR + SoapySDRPlay3, mbelib, dsd-neo,
#  גשר rsp_tcp (SDRplay→rtl_tcp), שרת הבקרה הוובי, ושירותי systemd.
#
#  ⚠️ הרץ *על ה-Pi עצמו*:   chmod +x install.sh && sudo ./install.sh
#  דגלים:  INSTALL_DMR_WHISPER=1  => תמלול אופציונלי (בנייה ארוכה)
#          עדכון SDRPLAY_VER / DSD_NEO_VER בראש הקובץ לגרסה חדשה.
# ============================================================================
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${SUDO_USER:-}" ]]; then BUILD_DIR="/home/$SUDO_USER/dmr-build"; else BUILD_DIR="/root/dmr-build"; fi
SDRPLAY_VER="3.15.2"        # אם יצא עדכון: עדכן כאן (ודא שהקובץ קיים באתר sdrplay)
DSD_NEO_VER="v2.3.0"        # arancormonk/dsd-neo — ⚠ *לא* lwvmobile/dsd-fme (אין לו
                             # קלט rtltcp; ראה CHANGELOG — מעבר נדרש אחרי כשל בחומרה אמיתית)

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[X] %s\033[0m\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "יש להריץ עם sudo (root)."

mkdir -p "$BUILD_DIR"

# ----------------------------------------------------------------------------
# 1. תלויות מערכת (כולל תלויות DSD-FME: ncurses, sndfile, rtl-sdr, pulse)
# ----------------------------------------------------------------------------
log "מתקין תלויות (apt)..."
apt-get update
apt-get install -y \
  git cmake ninja-build build-essential pkg-config curl usbutils \
  libusb-1.0-0-dev \
  libsoapysdr-dev soapysdr-tools \
  librtlsdr-dev \
  libsndfile1-dev libncurses-dev libncursesw5-dev \
  libpulse-dev libitpp-dev \
  libssl-dev libfftw3-dev libblas-dev liblapack-dev gfortran libcurl4-openssl-dev \
  python3 python3-flask

# ----------------------------------------------------------------------------
# 2. SDRplay API  (הורדה + חילוץ + התקנה אוטומטית, ללא אישור רישיון אינטראקטיבי)
# ----------------------------------------------------------------------------
SDRPLAY_MARK="/usr/local/share/dmr/sdrplay-api.version"
if ldconfig -p | grep -q libsdrplay_api && [[ "$(cat "$SDRPLAY_MARK" 2>/dev/null)" == "$SDRPLAY_VER" ]]; then
  log "SDRplay API v${SDRPLAY_VER} כבר מותקן - מדלג."
else
  log "מתקין SDRplay API v${SDRPLAY_VER}..."
  case "$(uname -m)" in
    aarch64) APIARCH="arm64" ;;
    armv7l)  APIARCH="armv7l" ;;
    x86_64)  APIARCH="x86_64" ;;
    *)       die "ארכיטקטורה לא נתמכת: $(uname -m). נתמכות: aarch64 / armv7l / x86_64." ;;
  esac
  RUN="/tmp/sdrplay.run"; EXT="/tmp/sdrplay_api"
  curl -fSL --retry 4 -o "$RUN" \
    "https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-${SDRPLAY_VER}.run" \
    || die "הורדת SDRplay API נכשלה. בדוק רשת או עדכן SDRPLAY_VER בראש הסקריפט."
  chmod +x "$RUN"
  rm -rf "$EXT"
  # חילוץ ללא הרצה (makeself) => עוקפים את אישור הרישיון האינטראקטיבי
  "$RUN" --noexec --target "$EXT"
  if [[ ! -d "$EXT/$APIARCH" ]]; then
    case "$APIARCH" in
      arm64)  ALT="$(find "$EXT" -maxdepth 1 -type d \( -name '*aarch64*' -o -name '*arm64*' \) | head -1)" ;;
      armv7l) ALT="$(find "$EXT" -maxdepth 1 -type d -name '*armv7*' | head -1)" ;;
      x86_64) ALT="$(find "$EXT" -maxdepth 1 -type d \( -name '*x86_64*' -o -name '*amd64*' \) | head -1)" ;;
    esac
    [[ -n "${ALT:-}" ]] && APIARCH="$(basename "$ALT")"
  fi
  [[ -n "$APIARCH" && -d "$EXT/$APIARCH" ]] || die "לא נמצאה ספריית ארכיטקטורה בתוך ה-API."
  LIB="$(ls "$EXT/$APIARCH"/libsdrplay_api.so.*.* 2>/dev/null | head -1)"
  [[ -n "$LIB" ]] || LIB="$(ls "$EXT/$APIARCH"/libsdrplay_api.so.* 2>/dev/null | head -1)"
  [[ -n "$LIB" ]] || die "לא נמצאה ספריית libsdrplay_api ב-API שחולץ."
  cp -f "$LIB" /usr/local/lib/
  BASE="$(basename "$LIB")"
  ln -sf "/usr/local/lib/$BASE" /usr/local/lib/libsdrplay_api.so.3
  ln -sf /usr/local/lib/libsdrplay_api.so.3 /usr/local/lib/libsdrplay_api.so
  SDR_HDR="$(find "$EXT" -name sdrplay_api.h -print -quit)"
  [[ -n "$SDR_HDR" ]] || die "sdrplay_api.h לא נמצא ב-API שחולץ ($EXT)."
  cp -f "$(dirname "$SDR_HDR")"/*.h /usr/local/include/
  cp -f "$EXT/$APIARCH/sdrplay_apiService" /usr/local/bin/
  chmod 755 /usr/local/bin/sdrplay_apiService
  cp -f "$EXT"/*.rules /etc/udev/rules.d/ 2>/dev/null || true
  udevadm control --reload-rules 2>/dev/null || true
  ldconfig
  mkdir -p "$(dirname "$SDRPLAY_MARK")"
  printf '%s' "$SDRPLAY_VER" > "$SDRPLAY_MARK"
fi

# ----------------------------------------------------------------------------
# 3. SoapySDRPlay3  (דרייבר SoapySDR ל-RSP1B — נדרש ל-rsp_tcp)
# ----------------------------------------------------------------------------
if SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
  log "SoapySDRPlay3 כבר מותקן - מדלג."
else
  log "בונה SoapySDRPlay3..."
  cd "$BUILD_DIR"
  [[ -d SoapySDRPlay3 ]] || git clone https://github.com/pothosware/SoapySDRPlay3.git
  cd SoapySDRPlay3 && rm -rf build && mkdir build && cd build
  cmake .. && make -j"$(nproc)" && make install && ldconfig
fi

# ----------------------------------------------------------------------------
# 4. mbelib  (ספריית פענוח ה-vocoder — תלות בנייה של DSD-FME)
# ----------------------------------------------------------------------------
if [[ -f /usr/local/lib/libmbe.so ]] || ldconfig -p | grep -q libmbe; then
  log "mbelib כבר מותקן - מדלג."
else
  log "בונה mbelib..."
  cd "$BUILD_DIR"
  [[ -d mbelib ]] || git clone https://github.com/szechyjs/mbelib.git
  cd mbelib && rm -rf build && mkdir build && cd build
  cmake .. && make -j"$(nproc)" && make install && ldconfig
fi

# ----------------------------------------------------------------------------
# 5. dsd-neo  (מפענח ה-DMR; arancormonk/dsd-neo)  -- החליף את rtl_airband/acars/vdl2
# ----------------------------------------------------------------------------
# ⚠ *לא* lwvmobile/dsd-fme: אומת (מול תיעוד CLI רשמי) שאין לו קלט ‎-i rtltcp: —
# רק ‎-i tcp: (פרוטוקול קנייני של SDR++, לא rtl_tcp) ו-‎-i rtl: (librtlsdr USB
# מקומי בלבד). dsd-neo תומך ‎-i rtltcp:host:port במפורש, שמתאים בדיוק לגשר
# rsp_tcp שכבר בנוי בשלב 6. נתפס בהרצה ראשונה על חומרה אמיתית — ר' CHANGELOG.
# חתימת בנייה פר-רכיב (כמו ב-AIR-AM): שינוי גרסה/flags => בנייה מחדש בעדכון הבא.
DSD_CMAKE_FLAGS="-G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DDSD_WARNINGS_AS_ERRORS=OFF"
DSD_BUILD_SIG="$(printf '%s' "$DSD_NEO_VER $DSD_CMAKE_FLAGS" | sha256sum | awk '{print $1}')"
DSD_MARK="/usr/local/share/dmr/dsd-neo.build-sig"
if command -v dsd-neo >/dev/null 2>&1 && [[ "$(cat "$DSD_MARK" 2>/dev/null)" == "$DSD_BUILD_SIG" ]]; then
  log "dsd-neo (${DSD_NEO_VER}) כבר מותקן - מדלג."
else
  log "בונה dsd-neo (${DSD_NEO_VER})..."
  cd "$BUILD_DIR"
  if [[ -d dsd-neo/.git ]]; then
    git -C dsd-neo fetch --depth 1 origin "tag" "$DSD_NEO_VER" && \
      git -C dsd-neo checkout -f "$DSD_NEO_VER" || true
  else
    rm -rf dsd-neo
    git clone --depth 1 --branch "$DSD_NEO_VER" https://github.com/arancormonk/dsd-neo.git
  fi
  cd dsd-neo && rm -rf build
  cmake -S . -B build $DSD_CMAKE_FLAGS -DCMAKE_INSTALL_PREFIX=/usr/local
  cmake --build build -j"$(nproc)"
  cmake --install build
  ldconfig
  command -v dsd-neo >/dev/null 2>&1 || die "בניית dsd-neo נכשלה (בדוק mbelib/ncurses/sndfile/fftw)."
  mkdir -p "$(dirname "$DSD_MARK")"
  printf '%s' "$DSD_BUILD_SIG" > "$DSD_MARK"
fi

# ----------------------------------------------------------------------------
# 6. rsp_tcp  (גשר SDRplay→rtl_tcp — DSD-FME מתחבר אליו כלקוח rtl_tcp)
# ----------------------------------------------------------------------------
# DSD-FME אינו תומך native ב-SDRplay; rsp_tcp מגיש שרת תואם-rtl_tcp מעל ה-RSP1B,
# ו-DSD-FME מתכוונן דרכו (טראנקינג). dsd_pty מריץ אותו כתהליך-בן.
if command -v rsp_tcp >/dev/null 2>&1; then
  log "rsp_tcp כבר מותקן - מדלג."
else
  log "בונה rsp_tcp (SDRplay RSP TCP server)..."
  cd "$BUILD_DIR"
  [[ -d rsp_tcp ]] || git clone https://github.com/SDRplay/RSPTCPServer.git rsp_tcp
  cd rsp_tcp && rm -rf build && mkdir build && cd build
  cmake .. && make -j"$(nproc)" && make install && ldconfig \
    || warn "בניית rsp_tcp נכשלה — בדוק שה-SDRplay API v${SDRPLAY_VER} מותקן. מצב DMR ידרוש אותה."
fi

# ----------------------------------------------------------------------------
# 7. תיקיות state + הגדרות התחלתיות
# ----------------------------------------------------------------------------
log "מתקין הגדרות התחלתיות..."
mkdir -p /etc/dmr /var/lib/dmr /var/lib/dmr/recordings /run/dmr
[[ -f /etc/dmr/dmr.env ]]        || cp "$REPO_DIR/config/dmr.env" /etc/dmr/dmr.env
[[ -f /etc/dmr/channelmap.csv ]] || cp "$REPO_DIR/config/channelmap.csv" /etc/dmr/channelmap.csv
# קובצי אליאסים (נזרעים ריקים; המשתמש מייבא RadioID.net user.csv ל-rid.csv)
[[ -f /etc/dmr/rid.csv ]] || printf 'RADIO_ID,CALLSIGN,NAME\n' > /etc/dmr/rid.csv
[[ -f /etc/dmr/tg.csv ]]  || printf 'TGID,NAME\n' > /etc/dmr/tg.csv

# ----------------------------------------------------------------------------
# 7b. חיזוק אבטחה: משתמש לא-root לשרת הווב + sudoers ממוקד
# ----------------------------------------------------------------------------
log "מגדיר משתמש 'dmr' לשרת הווב (הרצה ללא root)..."
id -u dmr >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin dmr
# הבעלות מאפשרת ל-dmr לכתוב env/channelmap/state/systems/aliases והקלטות
chown -R dmr:dmr /etc/dmr /var/lib/dmr
for grp in systemd-journal video; do
  getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" dmr || true
done
# sudoers: מתיר ל-dmr *רק* את restart/stop של dmr-dsdfme (NOPASSWD), לא יותר.
cat > /etc/sudoers.d/dmr <<'EOF'
dmr ALL=(root) NOPASSWD: /usr/bin/systemctl restart dmr-dsdfme
dmr ALL=(root) NOPASSWD: /usr/bin/systemctl stop dmr-dsdfme
EOF
chmod 440 /etc/sudoers.d/dmr
visudo -cf /etc/sudoers.d/dmr >/dev/null || die "קובץ sudoers לא תקין (/etc/sudoers.d/dmr)."
# קובץ environment לשרת הווב (PIN אופציונלי + תמלול). כבוי כברירת מחדל.
if [[ ! -f /etc/dmr/dmr-web.env ]]; then
  cat > /etc/dmr/dmr-web.env <<'EOF'
# DMR web control - משתני סביבה.
# כדי לדרוש PIN לשינוי מצב/מערכת, בטל את ההערה (ואז: systemctl restart dmr-web):
# DMR_PIN=1234
EOF
fi
chown -R dmr:dmr /etc/dmr

# ----------------------------------------------------------------------------
# 8. שרת הבקרה (web) + סקריפטים + udev
# ----------------------------------------------------------------------------
log "מתקין את שרת הווב ל-/opt/dmr ..."
mkdir -p /opt/dmr/webtune
cp -r "$REPO_DIR/webtune/." /opt/dmr/webtune/
[[ -f "$REPO_DIR/VERSION" ]] && cp "$REPO_DIR/VERSION" /opt/dmr/webtune/VERSION
cp "$REPO_DIR/scripts/dmr-wait-sdrplay" /usr/local/bin/
chmod 755 /usr/local/bin/dmr-wait-sdrplay
cp "$REPO_DIR/udev/99-dmr.rules" /etc/udev/rules.d/
udevadm control --reload-rules 2>/dev/null || true

# ----------------------------------------------------------------------------
# 8b. תמלול (אופציונלי) - whisper.cpp + מודל base (רב-לשוני)
#     הפעלה:  INSTALL_DMR_WHISPER=1 sudo ./install.sh
# ----------------------------------------------------------------------------
if [[ "${INSTALL_DMR_WHISPER:-0}" == "1" ]]; then
  log "מתקין תמלול (whisper.cpp + base) - עשוי לקחת כמה דקות ..."
  WHISPER_SRC="$BUILD_DIR/whisper.cpp"
  [[ -d "$WHISPER_SRC" ]] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$WHISPER_SRC"
  cmake -S "$WHISPER_SRC" -B "$WHISPER_SRC/build" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$WHISPER_SRC/build" -j"$(nproc)" --target whisper-cli
  install -m755 "$WHISPER_SRC/build/bin/whisper-cli" /usr/local/bin/whisper-cli
  mkdir -p /opt/dmr/models
  MODEL="/opt/dmr/models/ggml-base.bin"
  [[ -f "$MODEL" ]] || curl -fL --retry 3 -o "$MODEL" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
  chown -R dmr:dmr /opt/dmr/models
  grep -q '^DMR_TRANSCRIBE=' /etc/dmr/dmr-web.env 2>/dev/null || \
    printf 'DMR_TRANSCRIBE=1\n' >> /etc/dmr/dmr-web.env
  log "תמלול הופעל (לכבות: ערוך /etc/dmr/dmr-web.env והסר DMR_TRANSCRIBE)."
else
  log "תמלול לא הותקן (להפעלה: INSTALL_DMR_WHISPER=1 sudo ./install.sh)."
fi

# ----------------------------------------------------------------------------
# 9. שירותי systemd
# ----------------------------------------------------------------------------
log "מתקין שירותי systemd ..."
cp "$REPO_DIR/systemd/sdrplay.service"     /etc/systemd/system/
cp "$REPO_DIR/systemd/dmr-dsdfme.service"  /etc/systemd/system/
cp "$REPO_DIR/systemd/dmr-web.service"     /etc/systemd/system/
systemctl daemon-reload
# dmr-dsdfme אינו enabled בכוונה: dmr-web (המתזמר, enabled) משחזר את המצב השמור
# באתחול (dmr/off/scan). אין "מצב ראשי"; off שורד reboot.
systemctl enable sdrplay.service dmr-web.service
# restart (לא enable --now שהוא no-op לשירות שכבר רץ) => בעדכון הקוד/units נטענים החדשים.
systemctl restart sdrplay.service || warn "sdrplay.service לא עלה - בדוק חיבור ה-RSP1B."
sleep 2
systemctl restart dmr-web.service || warn "dmr-web לא עלה - בדוק journalctl -u dmr-web"

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
log "ההתקנה הסתיימה ✅"
cat <<EOF

  📻 פתח בטלפון את לוח הבקרה של DMR:
        http://${IP:-<IP-של-ה-Pi>}:8080

  שם עורכים את מערכת ה-Cap+ (תדר בקרה + color code + מפת LCN), לוחצים "התחל",
  ורואים את פיד השיחות החי. מאזינים לא צריכים סיסמה.

  לוגים:  sudo journalctl -u dmr-dsdfme -f
          sudo journalctl -u dmr-web -f
EOF
