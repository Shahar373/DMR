"""נרמול פלט DSD-FME — הלב של הפרויקט (parse_dsd_line + _normalize_dsd).
כל התלות בחומרה ממוקפת; רק לוגיקה טהורה."""
import time

import dsd_pty


# --- parse_dsd_line: טקסט DSD-FME → dict אירוע חלקי ------------------------
def test_parse_group_call():
    ev = dsd_pty.parse_dsd_line("Sync: +DMR  Slot 1  Group Call  TG=2451  SRC=3141592  CC 1")
    assert ev["slot"] == 1 and ev["tg"] == 2451 and ev["src"] == 3141592
    assert ev["cc"] == 1 and ev["call_type"] == "group" and ev["proto"] == "DMR"


def test_parse_private_call():
    ev = dsd_pty.parse_dsd_line("Slot 2 Private Call TGT 3140001 SRC 3141592 BER 0.5")
    assert ev["slot"] == 2 and ev["tgt"] == 3140001 and ev["src"] == 3141592
    assert ev["ber"] == 0.5 and ev["call_type"] == "private"


def test_parse_encryption_hex():
    ev = dsd_pty.parse_dsd_line("ALG ID: 0x21  KEY ID: 3  Encrypted")
    assert ev["alg"] == 0x21 and ev["key_id"] == 3 and ev["encrypted"] is True


def test_parse_control_lcn():
    ev = dsd_pty.parse_dsd_line("CSBK  Aloha  Rest Channel LCN 3")
    assert ev["lcn"] == 3 and ev["call_type"] == "control" and ev["event"] == "control"


def test_parse_tune_freq_hz():
    ev = dsd_pty.parse_dsd_line("Tuned to frequency 461062500 Hz for LCN 2")
    assert ev["freq_hz"] == 461062500 and ev["lcn"] == 2


def test_parse_sms_text():
    ev = dsd_pty.parse_dsd_line("Short Data  Message: HELLO WORLD")
    assert ev["call_type"] == "sms" and ev["text"] == "HELLO WORLD"


def test_parse_lrrp():
    ev = dsd_pty.parse_dsd_line("LRRP  SRC 3141592  lat 32.0114 long 34.8867")
    assert ev["lat"] == 32.0114 and ev["lon"] == 34.8867 and ev["call_type"] == "lrrp"


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


# --- _normalize_dsd: dict אירוע → כרטיס שיחה אחיד --------------------------
def test_normalize_group_card(paths):
    app = paths
    card = app._normalize_dsd({"t": 1000.0, "slot": 1, "tg": 2451, "src": 3141592,
                               "cc": 1, "call_type": "group", "freq_hz": 461062500})
    assert card["tg"] == 2451 and card["src"] == 3141592 and card["slot"] == 1
    assert card["call_type"] == "group" and card["group"] == "group"
    assert card["freq"] == 461.0625        # Hz → MHz
    assert card["category"] == "שיחת קבוצה"
    assert card["encrypted"] is False and card["enc"] is None


def test_normalize_encryption_alg_name(paths):
    app = paths
    card = app._normalize_dsd({"tg": 100, "src": 5, "alg": 0x25, "key_id": 2})
    assert card["encrypted"] is True
    assert card["enc"]["alg_name"] == "AES-256" and card["enc"]["key_id"] == 2


def test_normalize_never_invents_metric(paths):
    """ber/level None כשלא סופקו — לעולם לא ממציאים מדד (מוסכמת AIR-AM)."""
    app = paths
    card = app._normalize_dsd({"tg": 1, "src": 2})
    assert card["ber"] is None and card["level"] is None
    card2 = app._normalize_dsd({"tg": 1, "src": 2, "ber": 1.5, "level": -42.0})
    assert card2["ber"] == 1.5 and card2["level"] == -42.0


def test_normalize_alias_join(paths):
    app = paths
    import aliases
    aliases._tg_manual[2451] = "מוקד"
    aliases._rid_manual[3141592] = "יחידה 1"
    card = app._normalize_dsd({"tg": 2451, "src": 3141592})
    assert card["tg_alias"] == "מוקד" and card["src_alias"] == "יחידה 1"


def test_normalize_lrrp_position(paths):
    app = paths
    card = app._normalize_dsd({"src": 5, "lat": 32.0, "lon": 34.8, "call_type": "lrrp"})
    assert card["lat"] == 32.0 and card["lon"] == 34.8 and card["group"] == "position"


def test_normalize_bad_latlon_dropped(paths):
    app = paths
    card = app._normalize_dsd({"tg": 1, "src": 2, "lat": 0, "lon": 0})
    assert card["lat"] is None and card["lon"] is None


def test_normalize_empty_event_none(paths):
    app = paths
    assert app._normalize_dsd({}) is None
    assert app._normalize_dsd({"proto": "DMR"}) is None
    assert app._normalize_dsd("not a dict") is None


def test_normalize_hex_string_ids(paths):
    """src/tg שהגיעו כמחרוזת hex (0x..) עדיין מתפרסים."""
    app = paths
    card = app._normalize_dsd({"tg": "0x10", "src": "42"})
    assert card["tg"] == 16 and card["src"] == 42
