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
    # Phase 8 system radar -- always-on control-channel telemetry (not
    # emit_status-gated like sync/channel_status).
    "lsn_status", "bank_call", "preamble_csbk", "site_info",
    # ★ v0.14.0 data layer: the SRC(24)/DST(24) lines are no longer dropped --
    # they carry the sender RID *and* the UDP port, and the port is the free
    # data-kind classifier (4001=LRRP, 4005=ARS, 4007=text, ...). Dropping them
    # is what made /api/positions structurally empty.
    "ip_mapping",
}
# הפיקסצ'ר תויג לפי **שם השורה** כשנבנה; שם האירוע שאנחנו פולטים מתאר את
# התפקיד. המפה הזאת מתרגמת בין השניים במקום לשנות תיוג-קליטה היסטורי.
_FIXTURE_TYPE_ALIAS = {"ip_mapping": "ip_data"}


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
        expected = _FIXTURE_TYPE_ALIAS.get(expected, expected)
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
        "emergency": False,
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


# --- Phase 8: system radar (control-channel telemetry, always-on) ----------
def test_parse_lsn_status_all_state_kinds():
    """idle/rest/numeric-occupant, and a line with a 3-digit + a 5-digit
    occupant id in the same line (real capture has both small TGs and the
    large 64250 special id side by side)."""
    event = dsd_pty.parse_dsd_line(
        "LSN 05:   223;  LSN 06: 64250;  LSN 07:  Idle;  LSN 08:  Idle;"
    )
    assert event == {"type": "lsn_status",
                     "channels": {5: 223, 6: 64250, 7: "idle", 8: "idle"}}


def test_parse_lsn_status_rest_state():
    event = dsd_pty.parse_dsd_line(
        "LSN 05:     1;  LSN 06:  Rest;  LSN 07:  Idle;  LSN 08:  Idle;"
    )
    assert event["channels"][6] == "rest"


def test_parse_lsn_status_does_not_swallow_other_lsn_mentions():
    """A voice-call line also contains 'Rest LSN: N' -- must NOT be
    misclassified as lsn_status (the full-line-shape regex requires the
    entire line to be LSN-state segments, nothing else)."""
    event = dsd_pty.parse_dsd_line(
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5"
    )
    assert event["type"] == "voice_call"


def test_parse_bank_call_with_entries():
    event = dsd_pty.parse_dsd_line(
        "Bank One F80 Private or Data Call(s) -  LSN 03: TGT 64250; LSN 05: TGT 64250;"
    )
    assert event == {"type": "bank_call", "bank": "One F80",
                     "entries": [{"lsn": 3, "tgt": 64250}, {"lsn": 5, "tgt": 64250}]}


def test_parse_bank_call_no_active_entries():
    """'Bank ... -' with nothing after it => no private/data calls active on
    that bank right now -- a legitimate, common real-capture shape (empty
    entries list, not None)."""
    event = dsd_pty.parse_dsd_line("Bank One F20 Private or Data Call(s) -")
    assert event == {"type": "bank_call", "bank": "One F20", "entries": []}


def test_parse_preamble_csbk_individual_csbk():
    event = dsd_pty.parse_dsd_line(
        "Preamble CSBK - Individual CSBK - Source: 64250 - Target: 2232 - Rest LSN: 5"
    )
    assert event == {"type": "preamble_csbk", "kind": "csbk",
                     "src": 64250, "tgt": 2232, "rest_lsn": 5}


def test_parse_preamble_csbk_individual_data():
    event = dsd_pty.parse_dsd_line(
        "Preamble CSBK - Individual Data - Source: 191 - Target: 64250 - Rest LSN: 4"
    )
    assert event == {"type": "preamble_csbk", "kind": "data",
                     "src": 191, "tgt": 64250, "rest_lsn": 4}


def test_parse_site_info():
    event = dsd_pty.parse_dsd_line(
        "SLCO Capacity Plus Site: 2 - Rest LSN: 5 - RS: 00"
    )
    assert event == {"type": "site_info", "site": 2, "rest_lsn": 5, "rs": 0}


def test_housekeeping_and_ansi_are_handled():
    for line in [
        # channel_status stays emit_status-gated (unchanged) -- only real housekeeping here
        "Capacity Plus Channel Status - FL: 2 TS: 1 RS: 0 - Rest LSN: 6 - Initial Block",
    ]:
        assert dsd_pty.parse_dsd_line(line) is None
    # ★ v0.14.0: ה-SRC(24)/DST(24) **אינם** housekeeping יותר — הם נושאים את
    # ה-RID והפורט (מסווג סוג-הדאטה). הבדיקה הזאת קודם קבעה את ההפוך.
    assert dsd_pty.parse_dsd_line(
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;"
    ) == {"type": "ip_data", "role": "src", "rid": 18,
          "ip": "012.000.000.018", "port": 4001, "kind": "lrrp"}
    # Phase 8 system radar: these four are now always-on typed events, not
    # housekeeping (see _KEPT_TYPES + test_fixture_replay_matches_reality for
    # full real-sample coverage). Confirm here too since this test used to
    # assert the opposite.
    assert dsd_pty.parse_dsd_line(
        "LSN 01:  Idle;  LSN 02:  Idle;  LSN 03: 64250;  LSN 04:  Idle;"
    )["type"] == "lsn_status"
    assert dsd_pty.parse_dsd_line(
        "SLCO Capacity Plus Site: 2 - Rest LSN: 5 - RS: 00"
    )["type"] == "site_info"
    assert dsd_pty.parse_dsd_line(
        "Bank One F80 Private or Data Call(s) -  LSN 03: TGT 64250;"
    )["type"] == "bank_call"
    assert dsd_pty.parse_dsd_line(
        "Preamble CSBK - Individual Data - Source: 191 - Target: 64250 - Rest LSN: 4"
    )["type"] == "preamble_csbk"
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


# --- Phase 7: restart פר-ערוץ ב-multi (מפענח בודד קורס => לא מפיל את כולם) --
def test_channel_restart_decision_allows_under_budget():
    should, kept = dsd_pty._channel_restart_decision([], now=1000.0)
    assert should is True and kept == []


def test_channel_restart_decision_denies_at_budget():
    """3 restarts תוך 5 דקות (ברירת מחדל) => הרביעי נדחה."""
    now = 1000.0
    times = [now - 10, now - 20, now - 30]
    should, kept = dsd_pty._channel_restart_decision(times, now)
    assert should is False
    assert kept == times   # כולם עדיין בתוך החלון, לא נגזמו


def test_channel_restart_decision_prunes_old_entries_outside_window():
    """restart ישן מ-window_sec לא נספר במכסה -- ערוץ שהתייצב מקבל מכסה טרייה."""
    now = 1000.0
    times = [now - 400, now - 350]   # שניהם ישנים מ-300ש' (ברירת מחדל)
    should, kept = dsd_pty._channel_restart_decision(times, now)
    assert should is True
    assert kept == []   # נגזמו לגמרי


def test_channel_restart_decision_custom_budget():
    should, kept = dsd_pty._channel_restart_decision(
        [1.0, 2.0], now=3.0, max_restarts=2, window_sec=100)
    assert should is False and kept == [1.0, 2.0]


def test_build_decoder_status_event_restarting():
    event = dsd_pty.build_decoder_status_event(3, 164_325_000, "restarting", 2, t=555.0)
    assert event == {"type": "decoder_status", "phys_lcn": 3, "phys_freq_hz": 164_325_000,
                     "status": "restarting", "restart_count": 2, "t": 555.0}


def test_build_decoder_status_event_down_defaults_time():
    event = dsd_pty.build_decoder_status_event(6, 164_725_000, "down", 3)
    assert event["status"] == "down" and event["restart_count"] == 3
    assert isinstance(event["t"], float)


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


def test_normalize_tags_watchlist_match(paths):
    import watchlist
    watchlist.replace({"tg": [3], "rid": []})
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 2120, "call_type": "group",
    })
    assert card["watchlist"] == {"kind": "tg", "id": 3}


def test_normalize_watchlist_none_when_no_match(paths):
    import watchlist
    watchlist.replace({"tg": [], "rid": []})
    card = paths._normalize_dsd({
        "type": "voice_call", "tg": 3, "src": 2120, "call_type": "group",
    })
    assert card["watchlist"] is None


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


# --- Phase 7: channel_status (restart/give-up) --> /api/rf by_channel ------
def test_channel_status_tick_records_and_appears_in_by_channel(paths):
    app = paths
    with app._channel_status_lock:
        app._channel_status.clear()
    app._rf_ticks.clear()
    app._channel_status_tick({"phys_lcn": 4, "status": "restarting", "restart_count": 2, "t": 111.0})
    by_lcn = {d["phys_lcn"]: d for d in app._rf_quality_by_channel()}
    assert by_lcn[4]["status"] == "restarting" and by_lcn[4]["restart_count"] == 2


def test_channel_status_survives_with_zero_rf_ticks(paths):
    """ערוץ שנפל ולא מייצר יותר טיקים עדיין מופיע ב-by_channel (לא נעלם בשקט) --
    זו בדיוק הנקודה: give-up היה שקוף, לא מוסתר."""
    app = paths
    with app._channel_status_lock:
        app._channel_status.clear()
    app._rf_ticks.clear()
    app._channel_status_tick({"phys_lcn": 6, "status": "down", "restart_count": 3, "t": 222.0})
    lcns = {d["phys_lcn"] for d in app._rf_quality_by_channel()}
    assert 6 in lcns


def test_channel_status_ignores_missing_phys_lcn(paths):
    app = paths
    with app._channel_status_lock:
        app._channel_status.clear()
    app._channel_status_tick({"status": "down", "restart_count": 1})
    assert app._channel_status == {}


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


def test_listener_decoder_status_updates_by_channel_not_feed(paths, monkeypatch):
    """decoder_status (Phase 7 partial-restart) לא הופך לכרטיס-שיחה -- רק
    מעדכן את סטטוס-הערוץ שנחשף דרך /api/rf by_channel."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15554)
    with app._channel_status_lock:
        app._channel_status.clear()
    with app._dmr_lock:
        app._dmr_msgs.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15554, {"type": "decoder_status", "phys_lcn": 5, "phys_freq_hz": 164_637_500,
                      "status": "restarting", "restart_count": 1, "t": time.time()})
    time.sleep(0.3)
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 0
    by_lcn = {d["phys_lcn"]: d for d in app._rf_quality_by_channel()}
    assert by_lcn[5]["status"] == "restarting" and by_lcn[5]["restart_count"] == 1


# --- Phase 8: system radar dispatch (UDP -> system_intel, not a card) -------
def test_listener_site_info_updates_intel_not_feed(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15560)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    with app._dmr_lock:
        app._dmr_msgs.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15560, {"type": "site_info", "site": 2, "rest_lsn": 5, "rs": 0, "t": time.time()})
    time.sleep(0.3)
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 0
    assert "2" in app.system_intel.export_for("s1")["sites"]


def test_listener_lsn_status_updates_intel(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15561)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15561, {"type": "lsn_status",
                      "channels": {"5": 223, "6": "idle"}, "t": time.time()})
    time.sleep(0.3)
    lsn_dir = app.system_intel.export_for("s1")["lsn_directory"]
    assert lsn_dir["5"]["occupant"] == 223 and lsn_dir["6"]["occupant"] == "idle"


def test_listener_preamble_csbk_updates_intel_cdr(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15562)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15562, {"type": "preamble_csbk", "kind": "csbk", "src": 64250,
                      "tgt": 2232, "rest_lsn": 5, "t": time.time()})
    time.sleep(0.3)
    calls = app.system_intel.export_for("s1")["private_calls"]
    assert calls[0] == {"t": calls[0]["t"], "src": 64250, "tgt": 2232, "kind": "csbk", "rest_lsn": 5}


def test_listener_bank_call_updates_intel_cdr(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15563)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15563, {"type": "bank_call", "bank": "One F80",
                      "entries": [{"lsn": 3, "tgt": 64250}], "t": time.time()})
    time.sleep(0.3)
    calls = app.system_intel.export_for("s1")["private_calls"]
    assert calls[0]["tgt"] == 64250 and calls[0]["src"] is None and calls[0]["kind"] == "bank"


def test_listener_quality_cc_feeds_cc_drift(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15564)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    monkeypatch.setattr(app, "_active_color_code", 1)
    app._rf_ticks.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15564, {"type": "quality", "error_type": "CSBK_CRC", "cc": 2, "t": time.time()})
    time.sleep(0.3)
    cc = app.system_intel.export_for("s1")["cc"]
    assert cc["observed"] == 2 and cc["configured"] == 1 and cc["mismatch"] is True


def test_listener_system_radar_ignores_discovery_probe_system(paths, monkeypatch):
    """__probe__/__sweep__ (Phase 6 discovery) לא מסאבים את בנק-התדרים."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15565)
    monkeypatch.setattr(app, "_active_system_id", "__probe__")
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    _send_udp(15565, {"type": "site_info", "site": 9, "rest_lsn": 1, "rs": 0, "t": time.time()})
    time.sleep(0.3)
    assert app.system_intel.export_for("__probe__")["sites"] == {}


def test_api_system_intel_defaults_to_active_system(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel.record_site("s1", 2, t=1.0)
    body = app.app.test_client().get("/api/system-intel").get_json()
    assert body["ok"] and body["system"] == "s1"
    assert body["intel"]["sites"]["2"]["count"] == 1


def test_api_system_intel_explicit_system_param(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel.record_site("other-sys", 4, t=1.0)
    body = app.app.test_client().get("/api/system-intel?system=other-sys").get_json()
    assert body["system"] == "other-sys"
    assert "4" in body["intel"]["sites"]


def test_listener_lsn_status_maps_rest_lsn_to_physical_freq(paths, monkeypatch):
    """★ v0.12.0 e2e: אירוע lsn_status ב-multi נושא phys_freq_hz (חתימת
    dsd_pty.tag_event). ה-LSN שמסומן 'rest' בתוכו מתמפה לתדר הזה — ground-truth,
    לא ניחוש. עד v0.11.0 phys_freq_hz נזרק כאן."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15571)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel._intel.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    for _ in range(3):
        _send_udp(15571, {"type": "lsn_status", "channels": {"5": "rest", "6": 3},
                          "phys_lcn": 3, "phys_freq_hz": 164_537_500, "t": time.time()})
    time.sleep(0.4)
    intel = app.system_intel.export_for("s1")
    assert intel["lsn_freq"] == {"5": {"164537500": 3}}
    assert intel["lsn_map"]["5"] == {
        "freq_hz": 164_537_500, "votes": 3, "total": 3, "confidence": 1.0,
        "source": "rest", "physical_channel": 3, "pair_lsn": 6, "pair_conflict": False}
    assert intel["lsn_channelmap"] == [{"lcn": 3, "freq": 164.5375}]


def test_listener_site_info_also_votes_for_rest_lsn(paths, monkeypatch):
    """site_info נושא rest_lsn גם הוא, ומגיע רק מערוץ-הבקרה => אותה הסקה."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15572)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel._intel.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    for _ in range(3):
        _send_udp(15572, {"type": "site_info", "site": 2, "rest_lsn": 1, "rs": 0,
                          "phys_lcn": 1, "phys_freq_hz": 164_106_250, "t": time.time()})
    time.sleep(0.4)
    intel = app.system_intel.export_for("s1")
    assert intel["sites"]["2"]["count"] == 3          # ההתנהגות הקיימת נשמרה
    assert intel["lsn_map"]["1"]["freq_hz"] == 164_106_250
    assert intel["lsn_map"]["2"]["source"] == "pair"  # LSN 1+2 = אותו ערוץ פיזי


def test_listener_single_channel_creates_no_lsn_map(paths, monkeypatch):
    """חד-ערוצי (בלי phys_freq_hz): אפס שינוי התנהגות מול v0.11.0 —
    מפת-התפוסה נצברת, מיפוי-תדר לא נוצר (אין ממה להסיק)."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15573)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel._intel.clear()
    threading.Thread(target=app._dmr_listener, daemon=True).start()
    time.sleep(0.3)
    for _ in range(5):
        _send_udp(15573, {"type": "lsn_status", "channels": {"5": "rest", "6": 3},
                          "t": time.time()})
    time.sleep(0.4)
    intel = app.system_intel.export_for("s1")
    assert intel["lsn_directory"]["5"]["occupant"] == "rest"
    assert intel["lsn_freq"] == {} and intel["lsn_map"] == {}


def test_api_apply_lsn_writes_discovered_channelmap(paths, monkeypatch):
    """כפתור "אמץ מיפוי": ה-lcn מפסיק להיות אינדקס-שרירותי ונהיה מספר-הערוץ-
    הפיזי שנגזר מה-LSN שנצפה. יזום-אנושית (POST) — לא כתיבה אוטומטית."""
    app = paths
    import json
    app.SYSTEMS_PATH.write_text(json.dumps(
        [{"id": "s1", "name": "T", "control": 164.10625, "color_code": 10,
          "channelmap": [{"lcn": 1, "freq": 164.10625}, {"lcn": 2, "freq": 164.3}]}]))
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel._intel.clear()
    for _ in range(4):
        app.system_intel.record_rest_channel("s1", 5, 164_537_500, t=1.0)
        app.system_intel.record_rest_channel("s1", 1, 164_106_250, t=1.0)
    body = app.app.test_client().post("/api/system-intel/apply-lsn", json={}).get_json()
    assert body["ok"] is True
    assert body["channelmap"] == [{"lcn": 1, "freq": 164.10625},
                                  {"lcn": 3, "freq": 164.5375}]
    assert app.load_systems()[0]["channelmap"] == body["channelmap"]


def test_api_apply_lsn_refuses_without_a_decided_map(paths, monkeypatch):
    """אין מיפוי מוכרע => 400, ולא כתיבה חלקית/ריקה על ה-channelmap הקיים."""
    app = paths
    import json
    app.SYSTEMS_PATH.write_text(json.dumps(
        [{"id": "s1", "name": "T", "control": 164.10625, "color_code": 10,
          "channelmap": [{"lcn": 1, "freq": 164.10625}]}]))
    monkeypatch.setattr(app, "_active_system_id", "s1")
    app.system_intel._intel.clear()
    resp = app.app.test_client().post("/api/system-intel/apply-lsn", json={})
    assert resp.status_code == 400
    assert app.load_systems()[0]["channelmap"] == [{"lcn": 1, "freq": 164.10625}]


def test_api_apply_lsn_unknown_system_is_404(paths, monkeypatch):
    app = paths
    import json
    app.SYSTEMS_PATH.write_text(json.dumps(
        [{"id": "s1", "name": "T", "control": 164.10625, "color_code": 10,
          "channelmap": []}]))
    app.system_intel._intel.clear()
    for _ in range(4):
        app.system_intel.record_rest_channel("nope", 1, 164_106_250, t=1.0)
    resp = app.app.test_client().post("/api/system-intel/apply-lsn",
                                      json={"system": "nope"})
    assert resp.status_code == 404


def test_api_system_intel_no_active_system_returns_blank(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_active_system_id", None)
    body = app.app.test_client().get("/api/system-intel").get_json()
    assert body["system"] is None
    assert body["intel"] == {"sites": {}, "lsn_directory": {}, "cc": None,
                             "private_calls": [], "lsn_freq": {}, "lsn_freq_seen": None,
                             "lsn_map": {}, "lsn_channelmap": []}


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


# ============================================================================
#  ★ v0.13.0 — כשלים שקטים: הפרסר, הארכיון, שרידות ה-listener, וחיוניות
# ============================================================================
SOURCE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dsdfme_source_shapes.csv"


def _load_source_fixture():
    """צורות שנגזרו מקוד-המקור של DSD-FME (לא מקליטה) — ר' הכותרת בקובץ.
    שורות ההסבר (#) מדולגות."""
    with SOURCE_FIXTURE.open(encoding="utf-8") as handle:
        return [r for r in csv.DictReader(handle)
                if r["provenance"] and not r["provenance"].startswith("#")]


def test_source_fixture_shapes_all_parse():
    """★ הבדיקה המרכזית של התיקון: כל 16 וריאציות ה-Service Option מייצרות
    כרטיס. לפני v0.13.0 שמונה מתוך תשע מהן נדחו ע"י ה-regex — כלומר שיחת
    חירום, שיחה מוצפנת ושיחת-עדיפות **לא הופיעו בשום מקום** במערכת."""
    rows = _load_source_fixture()
    assert len(rows) >= 16
    dropped = []
    for row in rows:
        event = dsd_pty.parse_dsd_line(row["raw_line"])
        if not event or event["type"] != row["orig_type"]:
            dropped.append((row["provenance"], row["raw_line"]))
    assert not dropped


def test_source_fixture_provenance_is_explicit():
    """כל שורה בפיקסצ'ר הזה חייבת לסמן מאיפה היא — כדי שלא תיווצר אשליה
    שמדובר בקליטה אמיתית (הפרדה מוצהרת מ-capplus_slco_sample.csv)."""
    for row in _load_source_fixture():
        assert row["provenance"].startswith("source:")
        assert ".c:" in row["provenance"]


def test_parse_voice_flags_extracts_service_options():
    """טהורה: רצף הטוקנים → שדות. סדר הטוקנים הוא סדר ההדפסה של dmr_flco.c."""
    flags = dsd_pty.parse_voice_flags("Emergency Encrypted TXI RPT Broadcast OVCM Priority 2 ")
    assert flags["emergency"] is True
    assert flags["encrypted"] is True
    assert flags["priority"] == 2
    assert flags["flags"] == ["Emergency", "Encrypted", "TXI", "RPT",
                              "Broadcast", "OVCM", "Priority 2"]


def test_parse_voice_flags_empty_run_is_not_emergency():
    """שיחה רגילה: emergency=False מפורש (bool ולא None — הוא מניע התראה),
    ובלי flags/priority/encrypted מומצאים."""
    flags = dsd_pty.parse_voice_flags("")
    assert flags == {"emergency": False}


def test_voice_miss_event_for_unknown_modifier():
    """★ הלקח האמיתי מהבאג: טוקן שלא מוכר לנו לא ייעלם בשקט. גרסת DSD-FME
    אחרת/וונדור אחר יפיקו מודיפיקטור חדש — ואז נדע, במקום לגלות בעוד שנה."""
    event = dsd_pty.parse_dsd_line("SLOT 1 TGT=3 SRC=2120 Cap+ Group Klingon Call")
    assert event == {"type": "voice_miss",
                     "text": "SLOT 1 TGT=3 SRC=2120 Cap+ Group Klingon Call"}


def test_voice_miss_does_not_shadow_real_shapes():
    """גלאי-ההחמצה נבדק אחרון => אינו גונב שורות שתבנית אמיתית תופסת.
    כל 68 צורות הקליטה האמיתית נשארות מסווגות בדיוק כמו קודם (ר' replay)."""
    for row in _load_fixture():
        event = dsd_pty.parse_dsd_line(row["raw_line"])
        assert not (event and event["type"] == "voice_miss"), row["raw_line"]


def test_normalize_carries_service_option_flags(paths):
    """הדגלים מגיעים לכרטיס, ו-encrypted נקבע מהמקור (SO) ולא מהקורלציה."""
    app = paths
    card = app._normalize_dsd(dsd_pty.parse_dsd_line(
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group Emergency Encrypted Priority 1 Call"))
    assert card["emergency"] is True
    assert card["priority"] == 1
    assert card["encrypted"] is True
    assert card["enc"]["alg_name"] == "מוצפן"
    assert card["enc"]["alg"] is None and card["enc"]["key_id"] is None   # §8


def test_normalize_plain_call_has_no_invented_flags(paths):
    app = paths
    card = app._normalize_dsd(dsd_pty.parse_dsd_line(
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5"))
    assert card["emergency"] is False
    assert card["priority"] is None and card["flags"] is None
    assert card["encrypted"] is False and card["enc"] is None


# --- שרידות ה-listener -------------------------------------------------------
def test_handler_survives_malformed_intel_datagram(paths):
    """★ regression לבאג שהרג את הפיד: site לא-מספרי הגיע ל-int() חשוף בענף
    שלא היה עטוף, ה-thread מת, ו-/api/health המשיך לדווח ok=True. עכשיו
    הדאטהגרם מדולג וההמשך נקלט."""
    app = paths
    app._active_system_id = "s1"
    ctx = app._new_listener_ctx()
    app._handle_datagram({"type": "site_info", "site": "abc", "rest_lsn": 5}, ctx)
    app._handle_datagram({"type": "lsn_status", "channels": {"x": "rest"}}, ctx)
    app._handle_datagram({"type": "voice_call", "slot": 1, "src": 2120, "tg": 3,
                          "call_type": "group", "t": time.time()}, ctx)
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 1


def test_listener_thread_survives_bad_datagram_end_to_end(paths, monkeypatch):
    """אותו תרחיש דרך ה-socket האמיתי: ה-thread נשאר חי והפיד ממשיך לקלוט."""
    app = paths
    monkeypatch.setattr(app, "DMR_UDP_PORT", 15981)
    monkeypatch.setattr(app, "_active_system_id", "s1")
    with app._dmr_lock:
        app._dmr_msgs.clear()
    thread = threading.Thread(target=app._dmr_listener, daemon=True)
    thread.start()
    time.sleep(0.3)
    _send_udp(15981, {"type": "site_info", "site": "not-a-number", "rest_lsn": 5})
    time.sleep(0.3)
    for i in range(3):
        _send_udp(15981, {"type": "voice_call", "slot": 1, "src": 900 + i, "tg": 7,
                          "call_type": "group", "t": time.time()})
    time.sleep(0.5)
    assert thread.is_alive()
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 3
    assert app._feed_stats["handler_errors"] == 0   # דולג בנקיון, לא חריגה


def test_voice_miss_is_counted_not_carded(paths):
    app = paths
    ctx = app._new_listener_ctx()
    app._handle_datagram({"type": "voice_miss", "text": "SLOT 1 TGT=1 SRC=2 Xx Call"}, ctx)
    with app._dmr_lock:
        assert len(app._dmr_msgs) == 0
    assert app._feed_stats["voice_miss"] == 1
    assert app._feed_stats["voice_miss_last"] == "SLOT 1 TGT=1 SRC=2 Xx Call"


# --- ארכיון: כתיבה בסגירת השיחה ---------------------------------------------
def _voice(app, ctx, t, **kw):
    msg = {"type": "voice_call", "slot": 1, "src": 2120, "tg": 3,
           "call_type": "group", "t": t}
    msg.update(kw)
    app._handle_datagram(msg, ctx)


def test_archive_keeps_dur_frames_encrypted_and_id(paths):
    """★ הבאג המרכזי: הרשומה נכתבה לדיסק בפריים הראשון, ולכן dur/frames/
    encrypted/id — שכולם נקבעים אחר-כך כמוטציה — לא הגיעו לארכיון אף פעם.
    התוצאה הייתה `?day=` עם airtime 0 ו-0% מוצפן, לנצח, וייצוא CSV ריק."""
    app = paths
    ctx = app._new_listener_ctx()
    t0 = 1000.0
    _voice(app, ctx, t0)
    _voice(app, ctx, t0 + 1.0)
    _voice(app, ctx, t0 + 2.5)
    app._handle_datagram({"type": "encryption", "slot": 1, "t": t0 + 3.0}, ctx)

    assert app._read_dmr_log() == []            # עדיין פתוחה — לא נכתבה
    assert app._close_stale_calls(ctx, now=t0 + 5) == 0     # בתוך חלון הסגירה
    # חלון הסגירה נמדד מהפריים האחרון (t0+2.5), לא מתחילת השיחה
    written = app._close_stale_calls(ctx, now=t0 + 2.5 + app.CALL_CLOSE_SEC + 1)
    assert written == 1

    disk = app._read_dmr_log()
    assert len(disk) == 1
    rec = disk[0]
    assert rec["dur"] == 2.5 and rec["frames"] == 3
    assert rec["encrypted"] is True and rec["enc"]["alg_name"] == "מוצפן"
    assert rec["id"] == 1                       # id הגיע לדיסק (היה null)
    assert "_start" not in rec                  # מפתח-עבודה פנימי לא נכתב


def test_archive_and_memory_agree_on_analytics(paths):
    """אותה שיחה, שני מקורות — חייבים לתת אותה תשובה. זו הבדיקה שהייתה
    נכשלת קודם: זיכרון החזיר airtime אמיתי, הדיסק החזיר 0."""
    app = paths
    ctx = app._new_listener_ctx()
    t0 = 2000.0
    _voice(app, ctx, t0)
    _voice(app, ctx, t0 + 4.0)
    app._handle_datagram({"type": "encryption", "slot": 1, "t": t0 + 4.5}, ctx)
    app._close_stale_calls(ctx, force=True)

    with app._dmr_lock:
        mem = [dict(m) for m in app._dmr_msgs]
    disk = app._read_dmr_log()
    assert app._traffic_stats(disk)["by_tg"] == app._traffic_stats(mem)["by_tg"]
    assert app._encryption_stats(disk)["encrypted_total"] == 1
    assert (app._encryption_stats(disk)["encrypted_total"]
            == app._encryption_stats(mem)["encrypted_total"])


def test_non_voice_cards_are_written_immediately(paths):
    """כרטיסי data/lrrp לא עוברים dedup ולא מקבלים תג-הצפנה => אין סיבה
    לעכב אותם עד סגירת-חלון."""
    app = paths
    ctx = app._new_listener_ctx()
    app._handle_datagram({"type": "data_header", "slot": 1, "src": 191, "tgt": 64250,
                          "call_type": "data", "delivery": "Confirmed Delivery",
                          "t": 3000.0}, ctx)
    assert len(app._read_dmr_log()) == 1


def test_close_stale_calls_force_flushes_everything(paths):
    app = paths
    ctx = app._new_listener_ctx()
    _voice(app, ctx, 4000.0)
    assert app._close_stale_calls(ctx, now=4000.0) == 0
    assert app._close_stale_calls(ctx, force=True) == 1
    assert ctx["pending"] == {}


def test_close_window_measured_from_last_frame(paths):
    """שיחה ארוכה לא נכתבת באמצע: כל פריים-המשך דוחה את הסגירה."""
    app = paths
    ctx = app._new_listener_ctx()
    t0 = 5000.0
    _voice(app, ctx, t0)
    for i in range(1, 6):
        _voice(app, ctx, t0 + i * 3.0)          # פריימים בתוך חלון dedup של 8ש'
    assert app._close_stale_calls(ctx, now=t0 + 16.0) == 0
    assert app._close_stale_calls(ctx, now=t0 + 15.0 + app.CALL_CLOSE_SEC) == 1
    assert app._read_dmr_log()[0]["frames"] == 6


def test_listener_restart_flushes_pending_calls(paths):
    """הקמה-מחדש של ה-listener (watchdog) לא מאבדת שיחה שנצפתה וטרם נכתבה."""
    app = paths
    _voice(app, app._listener_ctx, 6000.0)
    assert app._read_dmr_log() == []
    app._reset_listener_ctx()
    assert len(app._read_dmr_log()) == 1
    assert app._listener_ctx["pending"] == {}


# --- חיוניות: להבדיל דממה מחירשות ------------------------------------------
def test_feed_tick_and_snapshot_count_by_type(paths):
    app = paths
    now = 7000.0
    app._feed_tick({"type": "lsn_status"}, now=now)
    app._feed_tick({"type": "lsn_status"}, now=now + 1)
    app._feed_tick({"type": "voice_call"}, now=now + 2)
    snap = app._feed_snapshot(now=now + 3)
    assert snap["datagrams_window"] == 3
    assert dict((d["type"], d["count"]) for d in snap["by_type"]) == \
        {"lsn_status": 2, "voice_call": 1}
    assert snap["last_voice_at"] == now + 2
    assert snap["last_datagram_at"] == now + 2


def test_feed_snapshot_window_expires_old_ticks(paths):
    app = paths
    app._feed_tick({"type": "quality"}, now=8000.0)
    snap = app._feed_snapshot(now=8000.0 + app.FEED_WINDOW_SEC + 5)
    assert snap["datagrams_window"] == 0
    assert snap["last_datagram_at"] == 8000.0      # העובדה נשמרת גם מחוץ לחלון


def test_feed_tick_counts_datagram_that_would_crash_handler(paths):
    """המונה נרשם **לפני** הדיספאץ' => גם דאטהגרם בעייתי נספר, ולכן אי-אפשר
    "לאבד" זרם שלם בגלל אירוע אחד שנפל."""
    app = paths
    app._feed_tick({"type": "site_info"}, now=9000.0)
    assert app._feed_snapshot(now=9000.0)["datagrams_window"] == 1


def test_decode_state_distinguishes_silence_from_deafness(paths):
    """★ ההבחנה שלא הייתה קיימת. פונקציה טהורה => נבדקת בלי חומרה."""
    app = paths
    now = 10000.0
    voice = {"last_voice_at": now - 5, "datagrams_window": 4, "last_datagram_at": now - 1}
    assert app._decode_state("dmr", voice, now=now) == "decoding"

    telemetry = {"last_voice_at": None, "datagrams_window": 30, "last_datagram_at": now - 1}
    assert app._decode_state("dmr", telemetry, now=now) == "chain_alive"

    nothing = {"last_voice_at": None, "datagrams_window": 0, "last_datagram_at": None}
    assert app._decode_state("dmr", nothing, now=now) == "silent"

    stale = {"last_voice_at": None, "datagrams_window": 0,
             "last_datagram_at": now - app.DECODE_SILENT_SEC - 1}
    assert app._decode_state("dmr", stale, now=now) == "silent"

    # standby אינו כשל — אין מפענח שירוץ
    assert app._decode_state("off", nothing, now=now) == "standby"


def test_decode_state_reports_listener_down(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_listener_bound", False)
    assert app._decode_state("dmr", {"datagrams_window": 5}, now=1.0) == "listener_down"


def test_api_health_exposes_feed_and_decode_state(paths, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    body = app.app.test_client().get("/api/health").get_json()
    assert "feed" in body and "decode_state" in body and "listener_alive" in body
    assert body["feed"]["window_sec"] == int(app.FEED_WINDOW_SEC)


def test_api_rf_exposes_parser_miss(paths):
    app = paths
    ctx = app._new_listener_ctx()
    app._handle_datagram({"type": "voice_miss", "text": "SLOT 9 TGT=1 SRC=2 Zz Call"}, ctx)
    body = app.app.test_client().get("/api/rf").get_json()
    assert body["parser_miss"] == 1
    assert body["parser_miss_last"] == "SLOT 9 TGT=1 SRC=2 Zz Call"


# --- מטמון-המודיעין אחרי restart של dmr-web ---------------------------------
def test_restore_intel_cache_when_decoder_already_running(paths):
    """★ regression: אחרי `systemctl restart dmr-web` בזמן ש-dmr-dsdfme רץ,
    _boot_restore יצא מוקדם ו-_active_system_id נשאר None => כל
    v0.11.0+v0.12.0 צבר אפס, בשקט."""
    app = paths
    import json as _json
    app.SYSTEMS_PATH.write_text(_json.dumps(
        [{"id": "s1", "name": "T", "control": 164.10625, "color_code": 10,
          "channelmap": []}]))
    app._active_system_id = None
    app._active_color_code = None
    assert app._restore_intel_cache({"app_mode": "multi", "system": "s1"}) == "s1"
    assert app._intel_system_id() == "s1"
    assert app._active_color_code == 10


def test_restore_intel_cache_ignores_standby_and_unknown(paths):
    app = paths
    import json as _json
    app.SYSTEMS_PATH.write_text(_json.dumps([]))
    app._active_system_id = None
    assert app._restore_intel_cache({"app_mode": "off", "system": "s1"}) is None
    assert app._restore_intel_cache({"app_mode": "dmr", "system": "nope"}) is None
    assert app._intel_system_id() is None


def test_boot_restore_populates_intel_cache_on_early_return(paths, monkeypatch, no_sleep):
    """דרך _boot_restore עצמו: live == mode => יוצא מוקדם, אבל המטמון מאוכלס."""
    app = paths
    import json as _json
    app.SYSTEMS_PATH.write_text(_json.dumps(
        [{"id": "s1", "name": "T", "control": 164.10625, "color_code": 8,
          "channelmap": []}]))
    app.STATE_PATH.write_text(_json.dumps({"app_mode": "dmr", "system": "s1"}))
    monkeypatch.setattr(app, "_live_mode", lambda: "dmr")
    app._active_system_id = None
    app._boot_restore()
    assert app._intel_system_id() == "s1"


# ============================================================================
#  ★ v0.14.0 — שכבת ה-Data: /api/positions שהיה ריק מבנית, סיווג לפי פורט,
#  הודעות טקסט, ושדות LRRP נוספים.
# ============================================================================
def test_ip_data_carries_rid_and_port_kind():
    """השורות שנזרקו עד כה כ-housekeeping נושאות את ה-RID **ואת הפורט**,
    והפורט הוא מסווג סוג-הדאטה (dmr_pdu.c:345-484)."""
    assert dsd_pty.parse_dsd_line(
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;"
    ) == {"type": "ip_data", "role": "src", "rid": 18,
          "ip": "012.000.000.018", "port": 4001, "kind": "lrrp"}
    assert dsd_pty.parse_dsd_line(
        "DST(24): 00064250; IP: 013.000.250.250; Port: 4007;"
    )["kind"] == "text"
    assert dsd_pty.parse_dsd_line(
        "SRC(24): 00000210; IP: 012.000.000.210; Port: 4005;"
    )["kind"] == "ars"


def test_ip_data_unknown_port_has_no_invented_kind():
    """פורט שלא במפה => kind=None. לא ממציאים סיווג (§8)."""
    event = dsd_pty.parse_dsd_line("SRC(24): 00000018; IP: 012.000.000.018; Port: 9999;")
    assert event["port"] == 9999 and event["kind"] is None


def test_lrrp_position_regex_no_longer_expects_impossible_src():
    """★ שורש הבאג: dmr_pdu.c:844-858 בונה 'LRRP SRC: N; (lat,lon)' אבל מדפיס
    אותה רק ב-if(!lat) => שורה עם קואורדינטות לעולם לא נושאת SRC. הקבוצה
    האופציונלית שהייתה כאן לא יכלה להתאים, ולכן src היה None תמיד."""
    event = dsd_pty.parse_dsd_line("Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)")
    assert event == {"type": "lrrp_position", "lat": 32.09265,
                     "lon": 34.86761, "call_type": "lrrp"}


def test_lrrp_extra_and_text_shapes():
    assert dsd_pty.parse_dsd_line(" Time: 2026.07.25 10:15:30") == {
        "type": "lrrp_extra", "fix_time": "2026.07.25 10:15:30"}
    assert dsd_pty.parse_dsd_line(" Speed: 3.5000 m/s 12.6000 km/h 7.82 mph") == {
        "type": "lrrp_extra", "speed_kmh": 12.6}
    assert dsd_pty.parse_dsd_line(" Track: 271") == {"type": "lrrp_extra", "track_deg": 271}
    assert dsd_pty.parse_dsd_line(" Text: שלום") == {"type": "text_message", "text": "שלום"}
    assert dsd_pty.parse_dsd_line(" Text:   ") is None      # ריק => לא אירוע


def _feed_lines(app, ctx, lines, t0=1000.0):
    for i, line in enumerate(lines):
        event = dsd_pty.parse_dsd_line(line)
        if event:
            event["t"] = t0 + i * 0.05
            app._handle_datagram(event, ctx)


def test_position_gets_rid_from_preceding_ip_data(paths):
    """★ התיקון המרכזי: הרצף המדויק מהקליטה האמיתית שלנו (הפיקסצ'ר, שורות
    16-19) — SRC/DST ואז Lat/Lon — מייצר כרטיס מיקום **עם** RID."""
    app = paths
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, [
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;",
        "DST(24): 00064250; IP: 013.000.250.250; Port: 4001;",
        "Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)",
    ])
    with app._dmr_lock:
        cards = list(app._dmr_msgs)
    assert len(cards) == 1                      # לא כרטיס כפול מה-ip_data
    card = cards[0]
    assert card["src"] == 18 and card["tgt"] == 64250
    assert card["lat"] == 32.09265 and card["lon"] == 34.86761
    assert card["call_type"] == "lrrp" and card["data_kind"] == "lrrp"
    assert card["data_port"] == 4001


def test_api_positions_no_longer_structurally_empty(paths):
    """★ /api/positions החזיר {} תמיד — לא מחוסר LRRP אלא כי כל מיקום סוּנן
    על src=None (app.py:_lrrp_snapshot). עכשיו יש RID אמיתי."""
    app = paths
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, [
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;",
        "DST(24): 00064250; IP: 013.000.250.250; Port: 4001;",
        "Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)",
    ])
    snap = app._lrrp_snapshot()
    assert set(snap) == {18}                     # מפתחות int בפייתון
    assert snap[18]["lat"] == 32.09265 and snap[18]["lon"] == 34.86761
    body = app.app.test_client().get("/api/positions").get_json()
    assert body["ok"] is True and "18" in body["positions"]   # ב-JSON כמחרוזת


def test_lrrp_extras_mutate_the_open_position_and_reach_the_archive(paths):
    """Time מודפס לפני ה-Lat/Lon, והשאר אחריו => חלקם הקשר וחלקם מוטציה.
    שניהם חייבים להגיע לדיסק — אחרת חוזר הבאג של v0.13.0."""
    app = paths
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, [
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;",
        "DST(24): 00064250; IP: 013.000.250.250; Port: 4001;",
        " Time: 2026.07.25 10:15:30",
        "Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)",
        " Radius: 12m",
        " Altitude: 45m",
        " Speed: 3.5000 m/s 12.6000 km/h 7.82 mph",
        " Track: 271",
    ])
    assert app._read_dmr_log() == []            # מיקום נדחה עד סגירה (עוד מוטבל)
    app._close_stale_calls(ctx, force=True)
    rec = app._read_dmr_log()[0]
    assert rec["fix_time"] == "2026.07.25 10:15:30"
    assert rec["radius_m"] == 12 and rec["alt_m"] == 45
    assert rec["speed_kmh"] == 12.6 and rec["track_deg"] == 271


def test_text_message_becomes_an_sms_card(paths):
    app = paths
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, [
        "SRC(24): 00000191; IP: 012.000.000.191; Port: 4007;",
        "DST(24): 00000210; IP: 012.000.000.210; Port: 4007;",
        " Text: מגיע בעוד 5 דקות",
    ])
    with app._dmr_lock:
        card = list(app._dmr_msgs)[-1]
    assert card["call_type"] == "sms" and card["category"] == "הודעת טקסט (SMS)"
    assert card["src"] == 191 and card["tgt"] == 210
    assert card["text"] == "מגיע בעוד 5 דקות"
    assert app._read_dmr_log()[0]["text"] == "מגיע בעוד 5 דקות"   # נכתב מיד


def test_text_content_can_be_disabled(paths, monkeypatch):
    """DMR_CAPTURE_TEXT=0 => המטא-דאטה נשמרת, התוכן לא. דלוק כברירת מחדל."""
    app = paths
    monkeypatch.setattr(app, "CAPTURE_TEXT", False)
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, [
        "SRC(24): 00000191; IP: 012.000.000.191; Port: 4007;",
        "DST(24): 00000210; IP: 012.000.000.210; Port: 4007;",
        " Text: סודי",
    ])
    with app._dmr_lock:
        card = list(app._dmr_msgs)[-1]
    assert card["text"] is None
    assert card["src"] == 191 and card["call_type"] == "sms"   # ההקשר נשמר


def test_ars_registration_becomes_a_card(paths):
    """ARS/טלמטריה/OTAP לא מדפיסים payload שאנחנו מפרסרים => שורת ה-DST היא
    כל מה שנדע, והיא בכל זאת אירוע (איזה רדיו נרשם, מול מי)."""
    app = paths
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, [
        "SRC(24): 00000210; IP: 012.000.000.210; Port: 4005;",
        "DST(24): 00064250; IP: 013.000.250.250; Port: 4005;",
    ])
    with app._dmr_lock:
        card = list(app._dmr_msgs)[-1]
    assert card["call_type"] == "reg" and card["data_kind"] == "ars"
    assert card["src"] == 210 and card["tgt"] == 64250


def test_data_ctx_expires_and_does_not_leak_between_pdus(paths):
    """הקשר של PDU אחד לא זולג למיקום של PDU אחר: תחילת SRC חדש מאפסת,
    וגם חלון DATA_CTX_SEC פג."""
    app = paths
    ctx = app._new_listener_ctx()
    _feed_lines(app, ctx, ["SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;"], t0=1000.0)
    # מיקום מאוחר בהרבה — ההקשר פג
    event = dsd_pty.parse_dsd_line("Lat: 1.0 Lon: 2.0")
    event["t"] = 1000.0 + app.DATA_CTX_SEC + 5
    app._handle_datagram(event, ctx)
    with app._dmr_lock:
        assert list(app._dmr_msgs)[-1]["src"] is None
    # ו-SRC חדש מאפס את ה-tgt של הקודם
    ctx2 = app._new_listener_ctx()
    _feed_lines(app, ctx2, [
        "SRC(24): 00000018; IP: 012.000.000.018; Port: 4001;",
        "DST(24): 00064250; IP: 013.000.250.250; Port: 4001;",
        "SRC(24): 00000077; IP: 012.000.000.077; Port: 4001;",
        "Lat: 5.0 Lon: 6.0",
    ], t0=2000.0)
    with app._dmr_lock:
        card = list(app._dmr_msgs)[-1]
    assert card["src"] == 77 and card["tgt"] is None      # לא ירש את 64250


def test_position_cards_isolated_per_physical_channel(paths):
    """ב-multi שני ערוצים יכולים לשלוח מיקום בו-זמנית — ההקשר מופרד לפי
    phys_lcn, בדיוק כמו dedup/קורלציית-ההצפנה."""
    app = paths
    ctx = app._new_listener_ctx()
    for lcn, rid, lat in ((1, 18, 32.1), (2, 77, 31.5)):
        for line in ("SRC(24): %08d; IP: 012.000.000.001; Port: 4001;" % rid,
                     "Lat: %s Lon: 34.0" % lat):
            event = dsd_pty.parse_dsd_line(line)
            event.update(t=3000.0, phys_lcn=lcn, phys_freq_hz=164_000_000 + lcn)
            app._handle_datagram(event, ctx)
    with app._dmr_lock:
        cards = {c["phys_lcn"]: c for c in app._dmr_msgs}
    assert cards[1]["src"] == 18 and cards[1]["lat"] == 32.1
    assert cards[2]["src"] == 77 and cards[2]["lat"] == 31.5
