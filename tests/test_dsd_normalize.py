"""נרמול פלט DSD-FME — הלב של הפרויקט (parse_dsd_line + _normalize_dsd).
כל התלות בחומרה ממוקפת; רק לוגיקה טהורה.

⚠ התבניות כאן *אינן ניחוש* — נאמתו מול קליטה אמיתית (20,000 שורות) של רשת
Motorola Capacity Plus רב-אתרית (SLCO). tests/fixtures/capplus_slco_sample.csv
מכיל את כל 68 הצורות השונות (type+pattern) שנצפו בפועל באותה קליטה — replay
מלא נגדו הוא בדיקת ה-regression המרכזית (test_fixture_replay_matches_reality)."""
import csv
import socket
import threading
import time
from pathlib import Path

import dsd_pty

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "capplus_slco_sample.csv"

# הטיפוסים שאנו *שומרים* (הופכים לאירוע מוקלד שנשלח ב-UDP); כל השאר —
# housekeeping תפעולי (lsn_status/channel_status/site_info/ip_mapping/
# bank_call/preamble_csbk) — מוטל החוצה כבר במקור (parse_dsd_line מחזיר None).
_KEPT_TYPES = {"voice_call", "data_header", "lrrp_position", "lrrp_request",
              "encryption", "quality"}


def _load_fixture():
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- replay מלא מול קליטה אמיתית --------------------------------------------
def test_fixture_replay_matches_reality():
    """מריץ את כל 68 הצורות האמיתיות דרך parse_dsd_line ומוודא שכל housekeeping
    נופל (None) וכל טיפוס-שיחה מסווג נכון. זו בדיקת ה-regression המרכזית —
    replay מלא מוודא ש-68/68 תואמים בדיוק (לא רק "משהו עובד")."""
    rows = _load_fixture()
    assert len(rows) == 68, "הפיקסצ'ר השתנה — עדכן את המספר אם בכוונה"
    mismatches = []
    for row in rows:
        orig, raw = row["orig_type"], row["raw_line"]
        ev = dsd_pty.parse_dsd_line(raw)
        parsed = ev["type"] if ev else "DROPPED"
        expected = orig if orig in _KEPT_TYPES else "DROPPED"
        if parsed != expected:
            mismatches.append((orig, raw, expected, parsed))
    assert not mismatches, f"אי-התאמות: {mismatches}"


def test_fixture_has_all_expected_types():
    """מוודא שהפיקסצ'ר עדיין מכסה את כל הטיפוסים שהקוד יודע לטפל בהם."""
    rows = _load_fixture()
    seen = {row["orig_type"] for row in rows}
    assert _KEPT_TYPES <= seen
    assert {"lsn_status", "channel_status", "site_info", "ip_mapping",
            "bank_call", "preamble_csbk"} <= seen


# --- parse_dsd_line: תבניות ספציפיות (שדות מדויקים) -------------------------
def test_parse_voice_call_group():
    """שיחת קבוצה: tgt הופך ל-tg (אין שדה TG נפרד בפרוטוקול — tgt *הוא* ה-TG)."""
    ev = dsd_pty.parse_dsd_line("SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5")
    assert ev == {"type": "voice_call", "slot": 1, "src": 2120, "call_type": "group",
                  "crc_err": False, "tg": 3, "lcn": 5}


def test_parse_voice_call_group_txi_no_rest_lsn():
    ev = dsd_pty.parse_dsd_line("SLOT 1 TGT=3 SRC=26 Group TXI Call")
    assert ev["tg"] == 3 and ev["src"] == 26 and "lcn" not in ev


def test_parse_voice_call_crc_err():
    ev = dsd_pty.parse_dsd_line("SLOT 1 TGT=199 SRC=4723398 Group TXI Call   (CRC ERR)")
    assert ev["crc_err"] is True and ev["tg"] == 199 and ev["src"] == 4723398


def test_parse_voice_call_private():
    """וריאנט Private (לא נצפה בקליטה עצמה, אך התבנית הפרוטוקולרית תומכת) —
    tgt *נשאר* tgt (לא הופך ל-tg), בניגוד לשיחת קבוצה."""
    ev = dsd_pty.parse_dsd_line("SLOT 2 TGT=3140001 SRC=3141592 Private Call")
    assert ev["call_type"] == "private" and ev["tgt"] == 3140001 and "tg" not in ev


def test_parse_data_header():
    ev = dsd_pty.parse_dsd_line(
        "Slot 1 Data Header - Indiv - Confirmed Delivery - Response Requested - "
        "Source: 191 Target: 64250")
    assert ev == {"type": "data_header", "slot": 1, "src": 191, "tgt": 64250,
                  "call_type": "data", "delivery": "Confirmed Delivery"}


def test_parse_lrrp_request():
    ev = dsd_pty.parse_dsd_line("LRRP SRC: 199; Response to TGT: 64250;")
    assert ev == {"type": "lrrp_request", "src": 199, "tgt": 64250, "call_type": "lrrp"}


def test_parse_lrrp_position():
    ev = dsd_pty.parse_dsd_line("Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)")
    assert ev == {"type": "lrrp_position", "lat": 32.09265, "lon": 34.86761, "call_type": "lrrp"}


def test_parse_lrrp_position_with_crc_err_suffix():
    """הסיומת (CRC ERR) לא שוברת את חילוץ הנ"צ (נצפה בקליטה אמיתית)."""
    ev = dsd_pty.parse_dsd_line("Lat: 32.09302 Lon: 34.86757 (32.09302, 34.86757) (CRC ERR)")
    assert ev["type"] == "lrrp_position" and ev["lat"] == 32.09302


def test_parse_encryption():
    """FLCO/FID הם routing fields, *לא* ALG/KEY — DSD-FME לא הדפיס אלגוריתם
    בקליטה אמיתית, אז אין ev['alg']/ev['key_id'] בכלל (לא ממציאים)."""
    ev = dsd_pty.parse_dsd_line(
        "SLOT 1 Protected LC  FLCO=0x0C FID=0x00  SLOT 1 FLCO FEC ERR  (FEC ERR)")
    assert ev == {"type": "encryption", "slot": 1, "encrypted": True}


def test_parse_quality_csbk_crc_with_cc():
    ev = dsd_pty.parse_dsd_line(
        "21:39:14 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK (CRC ERR)")
    assert ev == {"type": "quality", "error_type": "CSBK_CRC", "cc": 2}


def test_parse_quality_cach_burst_fec():
    ev = dsd_pty.parse_dsd_line("21:39:13 Sync: +DMR   slot1  [slot2] | CACH/Burst FEC ERR")
    assert ev == {"type": "quality", "error_type": "CACH_BURST_FEC"}


def test_parse_quality_slco_crc_bare():
    ev = dsd_pty.parse_dsd_line("SLCO CRC ERR")
    assert ev == {"type": "quality", "error_type": "SLCO_CRC"}


def test_parse_housekeeping_dropped():
    """~80% מהפלט האמיתי (lsn_status/channel_status/site_info/ip_mapping/
    bank_call/preamble_csbk) — לא נשלח ל-UDP כלל."""
    for line in [
        "LSN 01:  Idle;  LSN 02:  Idle;  LSN 03: 64250;  LSN 04:  Idle;",
        "Capacity Plus Channel Status - FL: 2 TS: 1 RS: 0 - Rest LSN: 6 - Initial Block",
        "SLCO Capacity Plus Site: 2 - Rest LSN: 5 - RS: 00",
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;",
        "Bank One F80 Private or Data Call(s) -  LSN 03: TGT 64250;",
        "Preamble CSBK - Individual Data - Source: 191 - Target: 64250 - Rest LSN: 4",
    ]:
        assert dsd_pty.parse_dsd_line(line) is None, line


def test_parse_noise_returns_none():
    assert dsd_pty.parse_dsd_line("just a banner, no fields here") is None
    assert dsd_pty.parse_dsd_line("") is None
    assert dsd_pty.parse_dsd_line("   ") is None


# --- build_command / build_rsp_tcp_command: argv טהור ----------------------
def test_build_command_trunking():
    env = {"DSD_CONTROL_FREQ": "461037500", "DSD_COLOR_CODE": "1",
           "DSD_CHANNELMAP": "/etc/dmr/channelmap.csv", "DSD_TRUNK": "1",
           "DSD_WAV_DIR": "/var/lib/dmr/recordings", "DSD_RTLTCP": "127.0.0.1:1234"}
    cmd = dsd_pty.build_command(env)
    assert "-T" in cmd                              # טראנקינג
    assert "461037500" in cmd                       # תדר בקרה
    assert "/etc/dmr/channelmap.csv" in cmd         # channel map
    assert cmd[cmd.index("-6") + 1] == "/var/lib/dmr/recordings"
    assert "rtltcp:127.0.0.1:1234" in cmd


def test_build_rsp_tcp_command():
    env = {"DSD_RTLTCP": "127.0.0.1:1234", "DSD_CONTROL_FREQ": "461037500"}
    cmd = dsd_pty.build_rsp_tcp_command(env)
    assert cmd[0].endswith("rsp_tcp")
    assert "127.0.0.1" in cmd and "1234" in cmd and "461037500" in cmd


def test_send_gain_nudge(tmp_path):
    """שולח הקשה ל-unix socket ומוודא שהיא מגיעה (מדמה dsd_pty בצד השני)."""
    sock_path = str(tmp_path / "ctrl.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2)
    assert dsd_pty.send_gain_nudge("up", sock_path=sock_path) is True
    data, _ = srv.recvfrom(64)
    assert data == b"G"
    assert dsd_pty.send_gain_nudge("down", sock_path=sock_path) is True
    data, _ = srv.recvfrom(64)
    assert data == b"g"
    srv.close()


def test_send_gain_nudge_no_listener_returns_false(tmp_path):
    assert dsd_pty.send_gain_nudge("up", sock_path=str(tmp_path / "nobody.sock")) is False


# --- _normalize_dsd: dict אירוע מוקלד → כרטיס שיחה אחיד ---------------------
def test_normalize_voice_call_group(paths):
    app = paths
    card = app._normalize_dsd({"type": "voice_call", "t": 1000.0, "slot": 1,
                               "tg": 3, "src": 2120, "call_type": "group", "lcn": 5})
    assert card["tg"] == 3 and card["src"] == 2120 and card["slot"] == 1
    assert card["call_type"] == "group" and card["group"] == "group"
    assert card["category"] == "שיחת קבוצה"
    assert card["encrypted"] is False and card["enc"] is None   # מתואם ע"י ה-listener, לא כאן


def test_normalize_data_header(paths):
    app = paths
    card = app._normalize_dsd({"type": "data_header", "slot": 1, "src": 191,
                               "tgt": 64250, "call_type": "data",
                               "delivery": "Confirmed Delivery"})
    assert card["call_type"] == "data" and card["tgt"] == 64250 and card["src"] == 191
    assert card["delivery"] == "Confirmed Delivery"


def test_normalize_never_invents_metric(paths):
    """ber/level *תמיד* None — DSD-FME לא מדפיס אותם בקליטה אמיתית שנבדקה.
    זה תקין, לא חוסר-מימוש (מוסכמת 'לעולם לא ממציאים' מ-AIR-AM)."""
    app = paths
    card = app._normalize_dsd({"type": "voice_call", "tg": 1, "src": 2, "call_type": "group"})
    assert card["ber"] is None and card["level"] is None


def test_normalize_alias_join(paths):
    app = paths
    import aliases
    aliases._tg_manual[3] = "מוקד"
    aliases._rid_manual[2120] = "יחידה 1"
    card = app._normalize_dsd({"type": "voice_call", "tg": 3, "src": 2120, "call_type": "group"})
    assert card["tg_alias"] == "מוקד" and card["src_alias"] == "יחידה 1"


def test_normalize_lrrp_position(paths):
    app = paths
    card = app._normalize_dsd({"type": "lrrp_position", "src": 18, "lat": 32.09265,
                               "lon": 34.86761, "call_type": "lrrp"})
    assert card["lat"] == 32.09265 and card["lon"] == 34.86761 and card["group"] == "position"


def test_normalize_bad_latlon_dropped(paths):
    app = paths
    card = app._normalize_dsd({"type": "lrrp_position", "lat": 0, "lon": 0, "call_type": "lrrp"})
    assert card["lat"] is None and card["lon"] is None


def test_normalize_quality_encryption_housekeeping_never_become_cards(paths):
    """quality/encryption/housekeeping לא הופכים לכרטיס — מטופלים בנפרד
    ב-_dmr_listener (מד RF / קורלציית הצפנה)."""
    app = paths
    assert app._normalize_dsd({"type": "quality", "error_type": "SLCO_CRC"}) is None
    assert app._normalize_dsd({"type": "encryption", "slot": 1, "encrypted": True}) is None
    assert app._normalize_dsd({"type": "channel_status"}) is None
    assert app._normalize_dsd({}) is None
    assert app._normalize_dsd("not a dict") is None


def test_normalize_freq_from_channelmap(paths):
    """תדר מגיע מ-channelmap של המערכת הפעילה (state.system), לא מטקסט DSD-FME
    (שלא מדפיס תדר בקליטה אמיתית) — היחיד מקור-אמת אמין."""
    app = paths
    app.SYSTEMS_PATH.write_text(
        '[{"id":"s1","name":"T","control":461.0,"color_code":1,'
        '"channelmap":[{"lcn":5,"freq":461.0625}]}]')
    app.save_state({"app_mode": "dmr", "system": "s1"})
    card = app._normalize_dsd({"type": "voice_call", "tg": 3, "src": 1,
                               "call_type": "group", "lcn": 5})
    assert card["freq"] == 461.0625


def test_normalize_freq_none_when_lcn_unknown(paths):
    app = paths
    app.save_state({"app_mode": "off", "system": None})
    card = app._normalize_dsd({"type": "voice_call", "tg": 3, "src": 1,
                               "call_type": "group", "lcn": 99})
    assert card["freq"] is None   # לא ממציאים תדר שלא במפה


# --- _dmr_listener: אינטגרציה מקצה-לקצה (UDP אמיתי, thread אמיתי) ----------
def _send_udp(port, obj):
    import json
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(json.dumps(obj).encode(), ("127.0.0.1", port))
    sock.close()


def test_listener_quality_feeds_rf_window_not_feed(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15551)
    app._rf_ticks.clear()
    with app._dmr_lock:
        app._dmr_msgs.clear()
    t = threading.Thread(target=app._dmr_listener, daemon=True)
    t.start()
    time.sleep(0.3)
    _send_udp(15551, {"type": "quality", "error_type": "CSBK_CRC", "t": time.time()})
    time.sleep(0.3)
    snap = app._rf_quality_snapshot()
    assert snap["total_errors"] == 1
    assert snap["by_type"] == [{"error_type": "CSBK_CRC", "count": 1}]
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 0   # quality לא נכנס לפיד


def test_listener_encryption_correlates_into_open_call(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15552)
    with app._dmr_lock:
        app._dmr_msgs.clear()
    t = threading.Thread(target=app._dmr_listener, daemon=True)
    t.start()
    time.sleep(0.3)
    now = time.time()
    _send_udp(15552, {"type": "voice_call", "slot": 1, "tg": 3, "src": 2120,
                      "call_type": "group", "t": now})
    time.sleep(0.2)
    _send_udp(15552, {"type": "encryption", "slot": 1, "encrypted": True, "t": now + 0.5})
    time.sleep(0.3)
    with app._dmr_lock:
        msgs = list(app._dmr_msgs)
    assert len(msgs) == 1
    assert msgs[0]["encrypted"] is True
    assert msgs[0]["enc"]["alg_name"] == "מוצפן"   # לא ממציאים שם אלגוריתם


def test_listener_voice_crc_err_feeds_rf_window(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15553)
    app._rf_ticks.clear()
    with app._dmr_lock:
        app._dmr_msgs.clear()
    t = threading.Thread(target=app._dmr_listener, daemon=True)
    t.start()
    time.sleep(0.3)
    _send_udp(15553, {"type": "voice_call", "slot": 1, "tg": 199, "src": 4723398,
                      "call_type": "group", "crc_err": True, "t": time.time()})
    time.sleep(0.3)
    snap = app._rf_quality_snapshot()
    assert snap["total_errors"] == 1 and snap["by_type"][0]["error_type"] == "VOICE_CRC"
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 1   # הכרטיס עצמו עדיין נכנס לפיד
