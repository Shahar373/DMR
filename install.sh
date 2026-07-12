#!/usr/bin/env bash
# ============================================================================
#  DMR  -  התקנה מלאה ל-Raspberry Pi (Pi 5 / Pi 4, Raspberry Pi OS 64-bit)
# ----------------------------------------------------------------------------
#  מתקין הכל אוטומטית: SDRplay API, SoapySDR + SoapySDRPlay3, mbelib, DSD-FME,
#  rsp_tcp + גשר IQ→PCM/rigctl, שרת הבקרה הוובי, ושירותי systemd.
#
#  ⚠️ הרץ *על ה-Pi עצמו*:   chmod +x install.sh && sudo ./install.sh
#  דגלים:  INSTALL_DMR_WHISPER=1  => תמלול אופציונלי (בנייה ארוכה)
#          עדכון SDRPLAY_VER / DSD_FME_BRANCH בראש הקובץ לגרסה חדשה.
# ============================================================================
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${SUDO_USER:-}" ]]; then BUILD_DIR="/home/$SUDO_USER/dmr-build"; else BUILD_DIR="/root/dmr-build"; fi
SDRPLAY_VER="3.15.2"        # אם יצא עדכון: עדכן כאן (ודא שהקובץ קיים באתר sdrplay)
DSD_FME_BRANCH="audio_work" # ענף ברירת-המחדל הפעיל של lwvmobile/dsd-fme

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[X] %s\033[0m\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "יש להריץ עם sudo (root)."

mkdir -p "$BUILD_DIR"

# ----------------------------------------------------------------------------
# 1. תלויות מערכת (כולל DSD-FME + NumPy לגשר IQ→PCM)
# ----------------------------------------------------------------------------
log "מתקין תלויות (apt)..."
apt-get update
apt-get install -y \
  git cmake build-essential pkg-config curl usbutils \
  libusb-1.0-0-dev \
  libsoapysdr-dev soapysdr-tools \
  librtlsdr-dev \
  libsndfile1-dev libncurses-dev libncursesw5-dev \
  libpulse-dev libitpp-dev \
  python3 python3-flask python3-numpy

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
# 3. SoapySDRPlay3  (דרייבר SoapySDR ל-RSP1B — נשמר לכלי אבחון/תאימות)
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
# 5. DSD-FME  (מפענח ה-DMR; lwvmobile fork)
# ----------------------------------------------------------------------------
DSD_CMAKE_FLAGS="-DCMAKE_BUILD_TYPE=Release"
DSD_BUILD_SIG="$(printf '%s' "$DSD_FME_BRANCH $DSD_CMAKE_FLAGS" | sha256sum | awk '{print $1}')"
DSD_MARK="/usr/local/share/dmr/dsd-fme.build-sig"
if command -v dsd-fme >/dev/null 2>&1 && [[ "$(cat "$DSD_MARK" 2>/dev/null)" == "$DSD_BUILD_SIG" ]]; then
  log "DSD-FME (${DSD_FME_BRANCH}) כבר מותקן - מדלג."
else
  log "בונה DSD-FME (${DSD_FME_BRANCH})..."
  cd "$BUILD_DIR"
  if [[ -d dsd-fme/.git ]]; then
    git -C dsd-fme fetch --depth 1 origin "$DSD_FME_BRANCH" && \
      git -C dsd-fme checkout -f "$DSD_FME_BRANCH" && \
      git -C dsd-fme reset --hard "origin/$DSD_FME_BRANCH" || true
  else
    rm -rf dsd-fme
    git clone --depth 1 --branch "$DSD_FME_BRANCH" https://github.com/lwvmobile/dsd-fme.git
  fi
  cd dsd-fme && rm -rf build && mkdir build && cd build
  cmake $DSD_CMAKE_FLAGS .. && make -j"$(nproc)" && make install && ldconfig
  command -v dsd-fme >/dev/null 2>&1 || die "בניית DSD-FME נכשלה (בדוק mbelib/ncurses/sndfile)."
  mkdir -p "$(dirname "$DSD_MARK")"
  printf '%s' "$DSD_BUILD_SIG" > "$DSD_MARK"
fi

# ----------------------------------------------------------------------------
# 6. rsp_tcp (שרת IQ תואם rtl_tcp ל-SDRplay)
# ----------------------------------------------------------------------------
# DSD-FME אינו לקוח rtl_tcp. dsd_pty מפעיל rsp_fm.py שממיר את ה-IQ ל-NFM PCM
# ב-48 kHz ומספק rigctl לניתוב תדרי הטראנקינג אל rsp_tcp.
if command -v rsp_tcp >/dev/null 2>&1; then
  log "rsp_tcp כבר מותקן - מדלג."
else
  log "בונה rsp_tcp (SDRplay RSP TCP server)..."
  cd "$BUILD_DIR"
  [[ -d rsp_tcp ]] || git clone https://github.com/SDRplay/RSPTCPServer.git rsp_tcp
  cd rsp_tcp && rm -rf build && mkdir build && cd build
  cmake .. && make -j"$(nproc)" && make install && ldconfig \
    || die "בניית rsp_tcp נכשלה — בדוק שה-SDRplay API v${SDRPLAY_VER} מותקן."
fi

# ----------------------------------------------------------------------------
# 7. תיקיות state + הגדרות התחלתיות
# ----------------------------------------------------------------------------
log "מתקין הגדרות התחלתיות..."
mkdir -p /etc/dmr /var/lib/dmr /var/lib/dmr/recordings /run/dmr
[[ -f /etc/dmr/dmr.env ]] || cp "$REPO_DIR/config/dmr.env" /etc/dmr/dmr.env
[[ -f /etc/dmr/channelmap.csv ]] || cp "$REPO_DIR/config/channelmap.csv" /etc/dmr/channelmap.csv
# מיגרציה לא-הרסנית להתקנות קיימות. app.py שומר את תדר המערכת והמפה הפעילים.
grep -q '^DSD_AUDIO_TCP=' /etc/dmr/dmr.env || printf 'DSD_AUDIO_TCP=127.0.0.1:7355\n' >> /etc/dmr/dmr.env
grep -q '^DSD_RIGCTL=' /etc/dmr/dmr.env || printf 'DSD_RIGCTL=127.0.0.1:4532\n' >> /etc/dmr/dmr.env
grep -q '^DSD_IQ_RATE=' /etc/dmr/dmr.env || printf 'DSD_IQ_RATE=240000\n' >> /etc/dmr/dmr.env
grep -q '^DSD_AUDIO_GAIN=' /etc/dmr/dmr.env || printf 'DSD_AUDIO_GAIN=4.0\n' >> /etc/dmr/dmr.env
[[ -f /etc/dmr/rid.csv ]] || printf 'RADIO_ID,CALLSIGN,NAME\n' > /etc/dmr/rid.csv
[[ -f /etc/dmr/tg.csv ]]  || printf 'TGID,NAME\n' > /etc/dmr/tg.csv

# ----------------------------------------------------------------------------
# 7b. חיזוק אבטחה: משתמש לא-root לשרת הווב + sudoers ממוקד
# ----------------------------------------------------------------------------
log "מגדיר משתמש 'dmr' לשרת הווב (הרצה ללא root)..."
id -u dmr >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin dmr
chown -R dmr:dmr /etc/dmr /var/lib/dmr
for grp in systemd-journal video; do
  getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" dmr || true
done
cat > /etc/sudoers.d/dmr <<'SUDOEOF'
dmr ALL=(root) NOPASSWD: /usr/bin/systemctl restart dmr-dsdfme
dmr ALL=(root) NOPASSWD: /usr/bin/systemctl stop dmr-dsdfme
SUDOEOF
chmod 440 /etc/sudoers.d/dmr
visudo -cf /etc/sudoers.d/dmr >/dev/null || die "קובץ sudoers לא תקין (/etc/sudoers.d/dmr)."
if [[ ! -f /etc/dmr/dmr-web.env ]]; then
  cat > /etc/dmr/dmr-web.env <<'WEBEOF'
# DMR web control - משתני סביבה.
# כדי לדרוש PIN לשינוי מצב/מערכת, בטל את ההערה (ואז: systemctl restart dmr-web):
# DMR_PIN=1234
WEBEOF
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
chmod 755 /usr/local/bin/dmr-wait-sdrplay /opt/dmr/webtune/dsd_pty.py /opt/dmr/webtune/rsp_fm.py
cp "$REPO_DIR/udev/99-dmr.rules" /etc/udev/rules.d/
udevadm control --reload-rules 2>/dev/null || true

# ----------------------------------------------------------------------------
# 8b. תמלול (אופציונלי) - whisper.cpp + מודל base (רב-לשוני)
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
systemctl enable sdrplay.service dmr-web.service
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
