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


def test_compute_wideband_plan_center_and_rate():
    center_hz, iq_rate = dsd_pty.compute_wideband_plan(
        [461_037_500, 461_062_500, 461_087_500, 461_112_500], guard_hz=25_000)
    assert center_hz == (461_037_500 + 461_112_500) // 2
    span = 461_112_500 - 461_037_500
    assert iq_rate >= span + 2 * 25_000
    assert iq_rate % 48_000 == 0   # rsp_fm's NfmDemodulator requirement


def test_compute_wideband_plan_rejects_span_too_wide():
    try:
        dsd_pty.compute_wideband_plan([100_000_000, 105_000_000], max_rate=2_000_000)
    except ValueError as error:
        assert "MHz" in str(error)
    else:
        raise AssertionError("expected ValueError for span exceeding max_rate")


def test_compute_wideband_plan_rejects_when_rounding_exceeds_ceiling():
    """Bug #3: a span+guard just under max_rate (1.99MHz < 2.0MHz) that rounds
    UP past it (2.016MHz) must be rejected — the ceiling check is on the
    ROUNDED iq_rate, not the raw span, so the value actually fed to rsp_tcp
    can never exceed max_rate."""
    lo = 100_000_000
    hi = lo + 1_940_000   # span+2*25k guard = 1.99MHz (passes a naive pre-round check)
    try:
        dsd_pty.compute_wideband_plan([lo, hi], guard_hz=25_000, max_rate=2_000_000)
    except ValueError as error:
        assert "MHz" in str(error)
    else:
        raise AssertionError("expected ValueError: rounded iq_rate exceeds max_rate")


def test_compute_wideband_plan_return_rate_never_exceeds_max():
    """Whatever it returns must be <= max_rate (the contract the ceiling
    guards). Sweep a range of spans near the boundary."""
    for extra in range(0, 60_000, 7_000):
        lo = 100_000_000
        hi = lo + (2_000_000 - 2 * 25_000 - extra)
        try:
            _c, rate = dsd_pty.compute_wideband_plan([lo, hi], guard_hz=25_000,
                                                     max_rate=2_000_000)
        except ValueError:
            continue   # rejected is fine
        assert rate <= 2_000_000, f"returned {rate} > max_rate for span extra={extra}"


def test_compute_wideband_plan_matches_rsp_fm_copy():
    """dsd_pty and rsp_fm each carry their own copy of this pure function
    (dsd_pty stays stdlib-only, rsp_fm needs numpy for other things) -- they
    must agree bit-for-bit on the same input, since rsp_tcp and rsp_fm.py are
    two independent subprocesses that both need the exact same center/rate."""
    import rsp_fm
    channelmap = [461_037_500, 461_062_500, 461_087_500, 461_112_500]
    assert dsd_pty.compute_wideband_plan(channelmap) == rsp_fm.compute_wideband_plan(channelmap)


def test_parse_channelmap_hz(tmp_path):
    path = tmp_path / "channelmap.csv"
    path.write_text("1,461037500\n2,461062500\n\n3,461087500\n")
    assert dsd_pty.parse_channelmap_hz(str(path)) == [
        {"lcn": 1, "freq_hz": 461037500},
        {"lcn": 2, "freq_hz": 461062500},
        {"lcn": 3, "freq_hz": 461087500},
    ]


def test_build_multi_rsp_tcp_command_argv():
    command = dsd_pty.build_multi_rsp_tcp_command(_runtime_env(), 461_075_000, 336_000)
    assert command[0].endswith("rsp_tcp")
    assert command[command.index("-s") + 1] == "336000"
    assert command[command.index("-f") + 1] == "461075000"


def test_build_multi_bridge_command_argv():
    command = dsd_pty.build_multi_bridge_command(_runtime_env(), 461_075_000, 336_000)
    assert command[1] == "-u"
    assert command[command.index("--rtl") + 1] == "127.0.0.1:1234"
    assert command[command.index("--rigctl") + 1] == "127.0.0.1:4532"
    assert command[command.index("--multi-channelmap") + 1] == "/etc/dmr/channelmap.csv"
    assert command[command.index("--frequency") + 1] == "461075000"
    assert command[command.index("--iq-rate") + 1] == "336000"
    assert "--audio-tcp-base" in command


def test_build_multi_bridge_command_requires_channelmap():
    env = dict(_runtime_env())
    del env["DSD_CHANNELMAP"]
    try:
        dsd_pty.build_multi_bridge_command(env, 461_075_000, 336_000)
    except ValueError as error:
        assert "DSD_CHANNELMAP" in str(error)
    else:
        raise AssertionError("missing channel map was accepted")


def test_build_channel_dsd_command_no_trunk_flags():
    """Fixed-frequency per-channel decode: no -T/-U (no per-channel retuning
    -- there is only one shared LO, see compute_wideband_plan). -7 must
    precede -P, matching DSD-FME's argv parser (same order as build_command's
    single-channel per-call recording)."""
    command = dsd_pty.build_channel_dsd_command(
        _runtime_env(), lcn=2, audio_port=17356, wav_root="/var/lib/dmr/recordings")
    assert command[:5] == [dsd_pty.DSD_BIN, "-i", "tcp:127.0.0.1:17356", "-o", "null"]
    assert "-T" not in command and "-U" not in command and "-C" not in command
    assert command.index("-7") + 1 == command.index("-P") - 1
    assert command[command.index("-7") + 1] == "/var/lib/dmr/recordings/lcn2"


def test_build_channel_dsd_command_no_wav_root():
    command = dsd_pty.build_channel_dsd_command(_runtime_env(), lcn=1, audio_port=7355)
    assert "-7" not in command and "-P" not in command


def test_tag_event_stamps_phys_lcn_and_freq():
    event = {"type": "voice_call", "tg": 3, "src": 2120}
    tagged = dsd_pty.tag_event(event, lcn=2, freq_hz=461_062_500)
    assert tagged is event   # mutates in place, returns same dict
    assert tagged["phys_lcn"] == 2
    assert tagged["phys_freq_hz"] == 461_062_500


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


def test_normalize_dsd_uses_phys_freq_hz_when_present(paths):
    """dsd_pty._run_multi תגית ground-truth (phys_lcn/phys_freq_hz, נקבעת
    ב-spawn) — כשקיימת, מחליפה את _channelmap_freq(lcn) (ניחוש מטקסט/
    מערכת-פעילה יחידה), לא רק משלימה אותה."""
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 1, "call_type": "group",
        "lcn": 99, "phys_lcn": 2, "phys_freq_hz": 461_062_500,
    })
    assert card["freq"] == 461.0625
    assert card["lcn"] == 2          # phys_lcn גובר על ה-lcn המנוחש (99)
    assert card["phys_lcn"] == 2


def test_normalize_dsd_phys_lcn_none_preserves_existing_behavior(paths):
    """אירועי חד-ערוצי (dmr/scan) לעולם לא נושאים phys_lcn/phys_freq_hz —
    ההתנהגות זהה בדיוק ל-Phase 2 (fallback ל-_channelmap_freq)."""
    paths.SYSTEMS_PATH.write_text(
        '[{"id":"s1","name":"T","control":461.0,"color_code":1,'
        '"channelmap":[{"lcn":5,"freq":461.0625}]}]'
    )
    paths.save_state({"app_mode": "dmr", "system": "s1"})
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 1, "call_type": "group", "lcn": 5,
    })
    assert card["freq"] == 461.0625
    assert card["lcn"] == 5
    assert card["phys_lcn"] is None


def test_rf_quality_snapshot_per_channel_filters_correctly(paths):
    app = paths
    app._rf_ticks.clear()
    app._rf_quality_tick("CSBK_CRC", phys_lcn=1)
    app._rf_quality_tick("SLCO_CRC", phys_lcn=2)
    app._rf_quality_tick("CSBK_CRC", phys_lcn=1)
    assert app._rf_quality_snapshot()["total_errors"] == 3        # גלובלי — כל הערוצים
    assert app._rf_quality_snapshot(phys_lcn=1)["total_errors"] == 2
    assert app._rf_quality_snapshot(phys_lcn=2)["total_errors"] == 1
    assert app._rf_quality_snapshot(phys_lcn=3)["total_errors"] == 0
    by_channel = {d["phys_lcn"]: d["total_errors"] for d in app._rf_quality_by_channel()}
    assert by_channel == {1: 2, 2: 1}


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


def test_listener_dedup_keys_on_phys_lcn(paths, monkeypatch):
    """שתי שיחות בו-זמנית, אותם tg+src+slot, על שני ערוצים פיזיים שונים
    (multi mode) => שני כרטיסים נפרדים, לא ממוזגים לאחד (בלי phys_lcn
    ב-dedup key זה היה מתמזג בטעות — ר' CLAUDE.md §8 סיכון בין-ערוצי)."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15561)
    with app._dmr_lock:
        app._dmr_msgs.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    now = time.time()
    _send_udp(15561, {
        "type": "voice_call", "slot": 1, "tg": 3, "src": 2120, "call_type": "group",
        "phys_lcn": 1, "phys_freq_hz": 461_037_500, "t": now,
    })
    time.sleep(0.1)
    _send_udp(15561, {
        "type": "voice_call", "slot": 1, "tg": 3, "src": 2120, "call_type": "group",
        "phys_lcn": 2, "phys_freq_hz": 461_062_500, "t": now + 0.2,
    })
    time.sleep(0.3)
    with app._dmr_lock:
        messages = list(app._dmr_msgs)
    assert len(messages) == 2
    freqs = {m["freq"] for m in messages}
    assert freqs == {461.0375, 461.0625}


def test_listener_encryption_correlates_per_channel(paths, monkeypatch):
    """תג הצפנה על ערוץ אחד לא נדבק בטעות לשיחה הפתוחה על ערוץ אחר, גם אם
    שתיהן פתוחות באותו slot בו-זמנית (multi mode)."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15562)
    with app._dmr_lock:
        app._dmr_msgs.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    now = time.time()
    _send_udp(15562, {
        "type": "voice_call", "slot": 1, "tg": 3, "src": 111, "call_type": "group",
        "phys_lcn": 1, "phys_freq_hz": 461_037_500, "t": now,
    })
    _send_udp(15562, {
        "type": "voice_call", "slot": 1, "tg": 5, "src": 222, "call_type": "group",
        "phys_lcn": 2, "phys_freq_hz": 461_062_500, "t": now,
    })
    time.sleep(0.2)
    # הצפנה מגיעה מ-phys_lcn=2 בלבד -- חייבת להישאר על השיחה של ערוץ 2
    _send_udp(15562, {"type": "encryption", "slot": 1, "phys_lcn": 2, "t": now + 0.3})
    time.sleep(0.3)
    with app._dmr_lock:
        messages = {(m["phys_lcn"]): m for m in app._dmr_msgs}
    assert messages[1]["encrypted"] is False   # ערוץ 1: לא נגע בו
    assert messages[2]["encrypted"] is True    # ערוץ 2: תואם


def test_emit_status_off_leaves_parsing_unchanged():
    """ברירת מחדל (emit_status=False): sync/channel_status נקיים => None (כמו קודם)."""
    assert dsd_pty.parse_dsd_line(
        "Capacity Plus Channel Status - FL: 1 TS: 1 RS: 0 - Rest LSN: 6 - Final Block"
    ) is None
    assert dsd_pty.parse_dsd_line(
        "Sync: +DMR  [slot1] slot2 | Color Code=01 | IDLE"
    ) is None


def test_emit_status_positive_sync_event():
    event = dsd_pty.parse_dsd_line(
        "Sync: +DMR  [slot1] slot2 | Color Code=01 | IDLE", emit_status=True)
    assert event == {"type": "sync", "proto": "dmr", "cc": 1, "slot": 1, "state": "IDLE"}
    grant = dsd_pty.parse_dsd_line(
        "Sync: +DMR  [SLOT1] slot2 | Color Code=00 | CSBK Voice Channel Grant",
        emit_status=True)
    assert grant["cc"] == 0 and grant["state"] == "CSBK Voice Channel Grant"


def test_emit_status_channel_status_event():
    event = dsd_pty.parse_dsd_line(
        "Capacity Plus Channel Status - FL: 1 TS: 1 RS: 0 - Rest LSN: 6 - Final Block",
        emit_status=True)
    assert event == {"type": "channel_status", "rest_lsn": 6}
    with_states = dsd_pty.parse_dsd_line(
        "Capacity Plus Channel Status - Rest LSN: 1 - LSN 01: Rest; LSN 02: Idle;",
        emit_status=True)
    assert with_states["rest_lsn"] == 1
    assert with_states["lsn_states"] == [
        {"lsn": 1, "state": "Rest"}, {"lsn": 2, "state": "Idle"}]


def test_emit_status_error_sync_stays_quality():
    """שורת sync עם שגיאה נשארת quality (קדימות) גם עם emit_status."""
    event = dsd_pty.parse_dsd_line(
        "21:39:14 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK (CRC ERR)",
        emit_status=True)
    assert event == {"type": "quality", "error_type": "CSBK_CRC", "cc": 2}


def test_emit_status_replay_reclassifies_channel_status():
    """עם emit_status, שורות ה-channel_status של הפיקסצ'ר נעשות אירועי channel_status,
    ושורות ה-quality נשארות quality (שגיאה קודמת ל-sync)."""
    rows = _load_fixture()
    for row in rows:
        event = dsd_pty.parse_dsd_line(row["raw_line"], emit_status=True)
        if row["orig_type"] == "channel_status":
            assert event and event["type"] == "channel_status"
        elif row["orig_type"] == "quality":
            assert event and event["type"] == "quality"
