"""DSD-FME parsing, normalization and SDRplay bridge command tests."""
import csv
import socket
import threading
import time
from pathlib import Path

import dsd_pty

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "capplus_slco_sample.csv"
_KEPT_TYPES = {
    "voice_call", "data_header", "lrrp_position", "lrrp_request",
    "encryption", "quality",
}


def _load_fixture():
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_fixture_replay_matches_reality():
    rows = _load_fixture()
    assert len(rows) == 68
    mismatches = []
    for row in rows:
        event = dsd_pty.parse_dsd_line(row["raw_line"])
        parsed = event["type"] if event else "DROPPED"
        expected = row["orig_type"] if row["orig_type"] in _KEPT_TYPES else "DROPPED"
        if parsed != expected:
            mismatches.append((row["orig_type"], row["raw_line"], expected, parsed))
    assert not mismatches


def test_fixture_has_all_expected_types():
    seen = {row["orig_type"] for row in _load_fixture()}
    assert _KEPT_TYPES <= seen
    assert {
        "lsn_status", "channel_status", "site_info", "ip_mapping",
        "bank_call", "preamble_csbk",
    } <= seen


def test_parse_voice_call_group():
    event = dsd_pty.parse_dsd_line(
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5"
    )
    assert event == {
        "type": "voice_call", "slot": 1, "src": 2120,
        "call_type": "group", "crc_err": False, "tg": 3, "lcn": 5,
    }


def test_parse_voice_call_variants():
    event = dsd_pty.parse_dsd_line("SLOT 1 TGT=3 SRC=26 Group TXI Call")
    assert event["tg"] == 3 and event["src"] == 26 and "lcn" not in event
    event = dsd_pty.parse_dsd_line(
        "SLOT 1 TGT=199 SRC=4723398 Group TXI Call   (CRC ERR)"
    )
    assert event["crc_err"] is True and event["tg"] == 199
    event = dsd_pty.parse_dsd_line(
        "SLOT 2 TGT=3140001 SRC=3141592 Private Call"
    )
    assert event["call_type"] == "private" and event["tgt"] == 3140001
    assert "tg" not in event


def test_parse_data_and_lrrp():
    event = dsd_pty.parse_dsd_line(
        "Slot 1 Data Header - Indiv - Confirmed Delivery - Response Requested - "
        "Source: 191 Target: 64250"
    )
    assert event == {
        "type": "data_header", "slot": 1, "src": 191, "tgt": 64250,
        "call_type": "data", "delivery": "Confirmed Delivery",
    }
    assert dsd_pty.parse_dsd_line(
        "LRRP SRC: 199; Response to TGT: 64250;"
    ) == {
        "type": "lrrp_request", "src": 199, "tgt": 64250,
        "call_type": "lrrp",
    }
    position = dsd_pty.parse_dsd_line(
        "Lat: 32.09302 Lon: 34.86757 (32.09302, 34.86757) (CRC ERR)"
    )
    assert position["lat"] == 32.09302 and position["lon"] == 34.86757


def test_parse_encryption_and_quality():
    event = dsd_pty.parse_dsd_line(
        "SLOT 1 Protected LC  FLCO=0x0C FID=0x00  SLOT 1 FLCO FEC ERR  (FEC ERR)"
    )
    assert event == {"type": "encryption", "slot": 1, "encrypted": True}
    event = dsd_pty.parse_dsd_line(
        "21:39:14 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK (CRC ERR)"
    )
    assert event == {"type": "quality", "error_type": "CSBK_CRC", "cc": 2}
    assert dsd_pty.parse_dsd_line("SLCO CRC ERR") == {
        "type": "quality", "error_type": "SLCO_CRC",
    }


def test_housekeeping_and_ansi_are_handled():
    for line in [
        "LSN 01:  Idle;  LSN 02:  Idle;  LSN 03: 64250;  LSN 04:  Idle;",
        "Capacity Plus Channel Status - FL: 2 TS: 1 RS: 0 - Rest LSN: 6 - Initial Block",
        "SLCO Capacity Plus Site: 2 - Rest LSN: 5 - RS: 00",
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;",
        "Bank One F80 Private or Data Call(s) -  LSN 03: TGT 64250;",
        "Preamble CSBK - Individual Data - Source: 191 - Target: 64250 - Rest LSN: 4",
    ]:
        assert dsd_pty.parse_dsd_line(line) is None
    colored = "\x1b[31mSLOT 1 TGT=3 SRC=2120 Cap+ Group Call\x1b[0m"
    assert dsd_pty.parse_dsd_line(colored)["tg"] == 3
    assert dsd_pty.parse_dsd_line("") is None


def _runtime_env():
    return {
        "DSD_CONTROL_FREQ": "461037500",
        "DSD_COLOR_CODE": "1",
        "DSD_CHANNELMAP": "/etc/dmr/channelmap.csv",
        "DSD_TRUNK": "1",
        "DSD_WAV_DIR": "/var/lib/dmr/recordings",
        "DSD_RTLTCP": "127.0.0.1:1234",
        "DSD_AUDIO_TCP": "127.0.0.1:7355",
        "DSD_RIGCTL": "127.0.0.1:4532",
    }


def test_build_dsd_command_uses_supported_interfaces():
    command = dsd_pty.build_command(_runtime_env())
    assert command[:5] == [
        dsd_pty.DSD_BIN, "-i", "tcp:127.0.0.1:7355", "-o", "null"
    ]
    assert command.count("-C") == 1
    assert command[command.index("-C") + 1] == "/etc/dmr/channelmap.csv"
    assert command[command.index("-U") + 1] == "4532"
    assert command[command.index("-7") + 1] == "/var/lib/dmr/recordings"
    assert "-P" in command and "-T" in command
    assert "-c" not in command and "-6" not in command
    assert not any(value.startswith("rtltcp:") for value in command)


def test_build_rsp_and_fm_bridge_commands():
    environment = _runtime_env()
    rsp = dsd_pty.build_rsp_tcp_command(environment)
    assert rsp[0].endswith("rsp_tcp")
    assert rsp[rsp.index("-s") + 1] == "240000"
    assert rsp[rsp.index("-f") + 1] == "461037500"

    bridge = dsd_pty.build_bridge_command(environment)
    assert bridge[1] == "-u"
    assert bridge[bridge.index("--rtl") + 1] == "127.0.0.1:1234"
    assert bridge[bridge.index("--audio") + 1] == "127.0.0.1:7355"
    assert bridge[bridge.index("--rigctl") + 1] == "127.0.0.1:4532"
    assert bridge[bridge.index("--frequency") + 1] == "461037500"


def test_trunking_configuration_is_validated():
    try:
        dsd_pty.build_command({"DSD_TRUNK": "1", "DSD_CONTROL_FREQ": "1"})
    except ValueError as error:
        assert "DSD_CHANNELMAP" in str(error)
    else:
        raise AssertionError("missing channel map was accepted")


def test_send_gain_nudge(tmp_path):
    socket_path = str(tmp_path / "ctrl.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(socket_path)
    server.settimeout(2)
    assert dsd_pty.send_gain_nudge("up", sock_path=socket_path)
    assert server.recvfrom(64)[0] == b"G"
    assert dsd_pty.send_gain_nudge("down", sock_path=socket_path)
    assert server.recvfrom(64)[0] == b"g"
    server.close()


def test_send_gain_nudge_no_listener_returns_false(tmp_path):
    assert not dsd_pty.send_gain_nudge(
        "up", sock_path=str(tmp_path / "nobody.sock")
    )


def test_normalize_voice_call_group(paths):
    app = paths
    card = app._normalize_dsd({
        "type": "voice_call", "t": 1000.0, "slot": 1,
        "tg": 3, "src": 2120, "call_type": "group", "lcn": 5,
    })
    assert card["tg"] == 3 and card["src"] == 2120 and card["slot"] == 1
    assert card["call_type"] == "group" and card["group"] == "group"
    assert card["category"] == "שיחת קבוצה"
    assert card["encrypted"] is False and card["enc"] is None


def test_normalize_data_header(paths):
    app = paths
    card = app._normalize_dsd({
        "type": "data_header", "slot": 1, "src": 191, "tgt": 64250,
        "call_type": "data", "delivery": "Confirmed Delivery",
    })
    assert card["call_type"] == "data" and card["tgt"] == 64250
    assert card["delivery"] == "Confirmed Delivery"


def test_normalize_never_invents_metric(paths):
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 1, "src": 2, "call_type": "group",
    })
    assert card["ber"] is None and card["level"] is None


def test_normalize_alias_join(paths):
    import aliases
    aliases._tg_manual[3] = "מוקד"
    aliases._rid_manual[2120] = "יחידה 1"
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 2120, "call_type": "group",
    })
    assert card["tg_alias"] == "מוקד" and card["src_alias"] == "יחידה 1"


def test_normalize_lrrp_and_invalid_position(paths):
    card = paths._normalize_dsd({
        "type": "lrrp_position", "src": 18, "lat": 32.09265,
        "lon": 34.86761, "call_type": "lrrp",
    })
    assert card["lat"] == 32.09265 and card["group"] == "position"
    card = paths._normalize_dsd({
        "type": "lrrp_position", "lat": 0, "lon": 0, "call_type": "lrrp",
    })
    assert card["lat"] is None and card["lon"] is None


def test_housekeeping_never_becomes_cards(paths):
    assert paths._normalize_dsd({"type": "quality", "error_type": "SLCO_CRC"}) is None
    assert paths._normalize_dsd({"type": "encryption", "slot": 1}) is None
    assert paths._normalize_dsd({"type": "channel_status"}) is None
    assert paths._normalize_dsd({}) is None
    assert paths._normalize_dsd("not a dict") is None


def test_normalize_freq_from_channelmap(paths):
    paths.SYSTEMS_PATH.write_text(
        '[{"id":"s1","name":"T","control":461.0,"color_code":1,'
        '"channelmap":[{"lcn":5,"freq":461.0625}]}]'
    )
    paths.save_state({"app_mode": "dmr", "system": "s1"})
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 1,
        "call_type": "group", "lcn": 5,
    })
    assert card["freq"] == 461.0625


def test_normalize_freq_none_when_lcn_unknown(paths):
    paths.save_state({"app_mode": "off", "system": None})
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 1,
        "call_type": "group", "lcn": 99,
    })
    assert card["freq"] is None


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
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15551, {"type": "quality", "error_type": "CSBK_CRC", "t": time.time()})
    time.sleep(0.3)
    snapshot = app._rf_quality_snapshot()
    assert snapshot["total_errors"] == 1
    assert snapshot["by_type"] == [{"error_type": "CSBK_CRC", "count": 1}]
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 0


def test_listener_encryption_correlates_into_open_call(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15552)
    with app._dmr_lock:
        app._dmr_msgs.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    now = time.time()
    _send_udp(15552, {
        "type": "voice_call", "slot": 1, "tg": 3, "src": 2120,
        "call_type": "group", "t": now,
    })
    time.sleep(0.2)
    _send_udp(15552, {
        "type": "encryption", "slot": 1, "encrypted": True, "t": now + 0.5,
    })
    time.sleep(0.3)
    with app._dmr_lock:
        messages = list(app._dmr_msgs)
    assert len(messages) == 1 and messages[0]["encrypted"] is True
    assert messages[0]["enc"]["alg_name"] == "מוצפן"


def test_listener_voice_crc_err_feeds_rf_window(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15553)
    app._rf_ticks.clear()
    with app._dmr_lock:
        app._dmr_msgs.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15553, {
        "type": "voice_call", "slot": 1, "tg": 199, "src": 4723398,
        "call_type": "group", "crc_err": True, "t": time.time(),
    })
    time.sleep(0.3)
    snapshot = app._rf_quality_snapshot()
    assert snapshot["total_errors"] == 1
    assert snapshot["by_type"][0]["error_type"] == "VOICE_CRC"
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 1
