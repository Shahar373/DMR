import socket
import struct
import time

import numpy as np

import rsp_fm


def test_rtl_command_packet():
    assert rsp_fm.rtl_command(1, 168_500_000) == struct.pack(">BI", 1, 168_500_000)


def test_demodulator_output_rate_and_signal():
    sample_rate = 240_000
    sample_count = 24_000
    time_axis = np.arange(sample_count) / sample_rate
    deviation = 2_500 * np.sin(2 * np.pi * 1_000 * time_axis)
    phase = np.cumsum(2 * np.pi * deviation / sample_rate)
    iq = np.exp(1j * phase)
    raw = np.empty(sample_count * 2, dtype=np.uint8)
    raw[0::2] = np.clip(np.real(iq) * 127 + 128, 0, 255)
    raw[1::2] = np.clip(np.imag(iq) * 127 + 128, 0, 255)

    pcm = rsp_fm.NfmDemodulator().process(raw.tobytes())
    decoded = np.frombuffer(pcm, dtype="<i2")
    assert decoded.size == 4_800
    assert decoded.std() > 1_000


def test_dc_blocker_is_stateful_across_chunk_boundaries():
    """A block-wise mean subtraction (the pre-fix behavior) recomputes its own
    mean independently per process() call, so splitting one continuous stream
    into two calls would *not* match processing it as a single call. The
    stateful one-pole DC blocker must be chunking-transparent: the same
    samples produce the same output regardless of how they were split across
    process() calls."""
    sample_rate = 240_000
    total_samples = 48_000
    time_axis = np.arange(total_samples) / sample_rate
    deviation = 1_500 * np.sin(2 * np.pi * 800 * time_axis) + 300  # nonzero average
    phase = np.cumsum(2 * np.pi * deviation / sample_rate)
    iq = np.exp(1j * phase)
    raw = np.empty(total_samples * 2, dtype=np.uint8)
    raw[0::2] = np.clip(np.real(iq) * 127 + 128, 0, 255)
    raw[1::2] = np.clip(np.imag(iq) * 127 + 128, 0, 255)
    raw_bytes = raw.tobytes()
    half = len(raw_bytes) // 2  # stays IQ-sample- and decimation-aligned

    whole_pcm = np.frombuffer(
        rsp_fm.NfmDemodulator().process(raw_bytes), dtype="<i2"
    )

    split = rsp_fm.NfmDemodulator()
    first = np.frombuffer(split.process(raw_bytes[:half]), dtype="<i2")
    second = np.frombuffer(split.process(raw_bytes[half:]), dtype="<i2")
    split_pcm = np.concatenate([first, second])

    assert split_pcm.size == whole_pcm.size
    assert np.max(np.abs(split_pcm.astype(int) - whole_pcm.astype(int))) < 300


def test_dc_blocker_reset_clears_state():
    demod = rsp_fm.NfmDemodulator()
    demod._dc_x_prev = 123.0
    demod._dc_y_prev = 45.0
    demod.reset()
    assert demod._dc_x_prev == 0.0
    assert demod._dc_y_prev == 0.0


def test_rtl_tcp_client_recv_timeout_is_fatal():
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 168_500_000, 240_000)

    class _StubSocket:
        def recv(self, size):
            raise socket.timeout()

    tuner.sock = _StubSocket()
    try:
        tuner.recv(4096)
    except ConnectionError as error:
        assert "rsp_tcp" in str(error)
    else:
        raise AssertionError("expected ConnectionError on IQ read timeout")


def test_audio_sender_delivers_via_background_thread():
    server = rsp_fm.AudioServer("127.0.0.1", 0)
    server.start()
    port = server.listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    _wait_for_client(server)
    sender = rsp_fm.AudioSender(server)
    sender.start()
    try:
        sender.submit(b"\x01\x02")
        client.settimeout(2)
        assert client.recv(2) == b"\x01\x02"
    finally:
        sender.close()
        client.close()
        server.close()


def test_audio_sender_drops_oldest_under_backpressure():
    class _StubAudio:
        def send(self, pcm):
            raise AssertionError("must not be called: sender thread not started")

    sender = rsp_fm.AudioSender(_StubAudio(), maxsize=2)
    sender.submit(b"a")
    sender.submit(b"b")
    sender.submit(b"c")
    assert sender.queue.qsize() == 2
    assert sender.queue.get_nowait() == b"b"


def test_rigctl_commands(monkeypatch):
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 168_500_000, 240_000)
    tuned = []
    monkeypatch.setattr(tuner, "set_frequency", lambda value: tuned.append(value))
    server = rsp_fm.RigctlServer("127.0.0.1", 0, tuner)

    assert server.handle_command("f") == "168500000\n"
    assert server.handle_command("F 168863000") == "RPRT 0\n"
    assert tuned == [168_863_000]
    assert server.handle_command("M NFM 12000") == "RPRT 0\n"
    assert server.handle_command("unknown") == "RPRT 1\n"


def _wait_for_client(server, previous=None):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with server._lock:
            current = server.client
        if current is not None and current is not previous:
            return current
        time.sleep(0.01)
    raise AssertionError("audio client was not accepted")


def test_audio_server_accepts_replacement_clients():
    server = rsp_fm.AudioServer("127.0.0.1", 0)
    server.start()
    port = server.listener.getsockname()[1]
    first = socket.create_connection(("127.0.0.1", port))
    accepted_first = _wait_for_client(server)
    second = socket.create_connection(("127.0.0.1", port))
    _wait_for_client(server, accepted_first)
    try:
        server.send(b"\x01\x02")
        second.settimeout(2)
        assert second.recv(2) == b"\x01\x02"
    finally:
        first.close()
        second.close()
        server.close()


def test_compute_power_spectrum_peak_and_floor():
    """טון full-scale מרוכב => peak ~0 dBFS בבין הצפוי, רצפת רעש נמוכה."""
    nfft = 256
    k = nfft // 4                       # תדר +Fs/4
    n = np.arange(nfft * 8)
    tone = np.exp(2j * np.pi * (k / nfft) * n).astype(np.complex64)
    power = rsp_fm.compute_power_spectrum(tone, nfft)
    assert power.shape[0] == nfft
    assert int(np.argmax(power)) == nfft // 2 + k     # fftshift => מרכז=DC
    assert abs(float(power.max())) < 1.0              # ~0 dBFS
    assert float(np.median(power)) < -60.0            # רצפה נמוכה מהשיא


def test_compute_power_spectrum_none_when_short():
    assert rsp_fm.compute_power_spectrum(np.zeros(10, dtype=np.complex64), 256) is None


def test_compute_power_spectrum_u8_quantized():
    """אחרי קוונטיזציה ל-u8 (כמו rsp_tcp) ה-peak עדיין בבין הנכון."""
    nfft = 256
    k = -nfft // 8
    n = np.arange(nfft * 4)
    tone = np.exp(2j * np.pi * (k / nfft) * n)
    u8 = np.empty(tone.size * 2, dtype=np.uint8)
    u8[0::2] = np.clip(np.real(tone) * 127 + 127.5, 0, 255)
    u8[1::2] = np.clip(np.imag(tone) * 127 + 127.5, 0, 255)
    floats = (u8.astype(np.float32) - 127.5) / 128.0
    iq = floats[0::2] + 1j * floats[1::2]
    power = rsp_fm.compute_power_spectrum(iq, nfft)
    assert int(np.argmax(power)) == nfft // 2 + k


def test_set_fixed_gain_sends_manual_mode(monkeypatch):
    sent = []
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    monkeypatch.setattr(tuner, "send_command", lambda cmd, val: sent.append((cmd, val)))
    tuner.set_fixed_gain(20)
    assert (rsp_fm.RTL_CMD_SET_GAIN_MODE, 1) in sent
    assert (rsp_fm.RTL_CMD_SET_GAIN_BY_INDEX, 20) in sent
    assert tuner.gain_index == 20
    tuner.set_fixed_gain(999)               # נחתך ל-28
    assert tuner.gain_index == 28


def test_nfm_demodulator_offset_zero_matches_baseline():
    """offset_hz=0.0 (the default) must be byte-for-byte identical to the
    pre-multi-channel behavior -- zero risk to the existing dmr/scan path."""
    sample_rate = 240_000
    sample_count = 24_000
    time_axis = np.arange(sample_count) / sample_rate
    deviation = 2_500 * np.sin(2 * np.pi * 1_000 * time_axis)
    phase = np.cumsum(2 * np.pi * deviation / sample_rate)
    iq = np.exp(1j * phase)
    raw = np.empty(sample_count * 2, dtype=np.uint8)
    raw[0::2] = np.clip(np.real(iq) * 127 + 128, 0, 255)
    raw[1::2] = np.clip(np.imag(iq) * 127 + 128, 0, 255)
    raw_bytes = raw.tobytes()

    baseline = rsp_fm.NfmDemodulator().process(raw_bytes)
    explicit_zero = rsp_fm.NfmDemodulator(offset_hz=0.0).process(raw_bytes)
    assert baseline == explicit_zero


def test_nfm_demodulator_offset_recovers_shifted_tone():
    """A channel sitting offset_hz away from a wideband capture's tuned
    centre must demodulate to (approximately) the same audio as if it had
    been captured at DC -- proves the mixer stage is correct, the core of
    multi-channel decode (N offset-aware demodulators sharing one capture)."""
    sample_rate = 240_000
    sample_count = 24_000
    offset_hz = 40_000.0
    time_axis = np.arange(sample_count) / sample_rate
    deviation = 2_500 * np.sin(2 * np.pi * 1_000 * time_axis)
    phase = np.cumsum(2 * np.pi * deviation / sample_rate)
    baseband = np.exp(1j * phase)
    shifted = baseband * np.exp(2j * np.pi * offset_hz / sample_rate * np.arange(sample_count))

    def to_u8(iq):
        raw = np.empty(iq.size * 2, dtype=np.uint8)
        raw[0::2] = np.clip(np.real(iq) * 127 + 128, 0, 255)
        raw[1::2] = np.clip(np.imag(iq) * 127 + 128, 0, 255)
        return raw.tobytes()

    baseline_pcm = np.frombuffer(
        rsp_fm.NfmDemodulator(offset_hz=0.0).process(to_u8(baseband)), dtype="<i2")
    recovered_pcm = np.frombuffer(
        rsp_fm.NfmDemodulator(offset_hz=offset_hz).process(to_u8(shifted)), dtype="<i2")

    assert recovered_pcm.size == baseline_pcm.size
    assert recovered_pcm.std() > 1_000
    assert np.corrcoef(baseline_pcm.astype(float), recovered_pcm.astype(float))[0, 1] > 0.95


def test_nfm_demodulator_offset_reset_clears_mix_phase():
    demod = rsp_fm.NfmDemodulator(offset_hz=30_000.0)
    demod._mix_phase = 12345.0
    demod.reset()
    assert demod._mix_phase == 0.0


def test_scaled_taps_preserves_single_channel_reference():
    """At the single-channel reference rate (240kHz) scaled_taps returns the
    base count EXACTLY, so the field A/B flag can never perturb the hardware-
    validated dmr/scan path (240kHz -> 121 either way)."""
    assert rsp_fm.scaled_taps(rsp_fm.DEFAULT_IQ_RATE, 121) == 121
    # NfmDemodulator itself no longer auto-scales: it uses `taps` as-is, so the
    # default single-channel demod is exactly 121 regardless of the flag.
    demod = rsp_fm.NfmDemodulator(iq_rate=rsp_fm.DEFAULT_IQ_RATE)
    assert len(demod.taps) == 121


def test_scaled_taps_widens_for_multi_rate_and_stays_odd():
    """A ~2.8x wider capture (672kHz, the multi_164cluster rate) needs ~2.8x
    the taps to hold the transition width -- otherwise the adjacent Cap+
    channel bleeds through. Always odd (design_lowpass requires it). This is
    the value the OPT-IN DSD_MULTI_SCALED_TAPS path feeds each multi demod."""
    n = rsp_fm.scaled_taps(672_000, 121)
    assert n == 339                      # round(121 * 672000/240000) -> 338.8 -> 339
    assert n % 2 == 1
    assert rsp_fm.scaled_taps(240_000, 121) < n   # monotonic in rate
    # applied explicitly, the demod honors it
    assert len(rsp_fm.NfmDemodulator(iq_rate=672_000, taps=n).taps) == 339


def test_scaled_taps_is_capped_and_odd_at_extremes():
    capped = rsp_fm.scaled_taps(2_000_000, 121, cap=1023)
    assert capped <= 1023 and capped % 2 == 1


def test_compute_wideband_plan_center_and_rate():
    center_hz, iq_rate = rsp_fm.compute_wideband_plan(
        [461_037_500, 461_062_500, 461_087_500, 461_112_500], guard_hz=25_000)
    assert center_hz == (461_037_500 + 461_112_500) // 2
    span = 461_112_500 - 461_037_500
    assert iq_rate >= span + 2 * 25_000
    assert iq_rate % rsp_fm.DEFAULT_AUDIO_RATE == 0


def test_compute_wideband_plan_rounds_to_audio_rate_multiple():
    """A naive max(span+guard, floor) is not guaranteed to be a multiple of
    audio_rate -- NfmDemodulator requires iq_rate % audio_rate == 0 and
    raises ValueError otherwise. compute_wideband_plan must round up so no
    per-channel demodulator construction can ever hit that error."""
    center_hz, iq_rate = rsp_fm.compute_wideband_plan(
        [100_000_000, 100_037_000], guard_hz=1_000, audio_rate=48_000)
    assert iq_rate % 48_000 == 0
    assert iq_rate >= 37_000 + 2_000


def test_compute_wideband_plan_rejects_span_too_wide():
    try:
        rsp_fm.compute_wideband_plan([100_000_000, 105_000_000], max_rate=2_000_000)
    except ValueError as exc:
        assert "MHz" in str(exc)
    else:
        raise AssertionError("expected ValueError for span exceeding max_rate")


def test_compute_wideband_plan_rejects_empty():
    try:
        rsp_fm.compute_wideband_plan([])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty channelmap")


def test_parse_channelmap_hz_roundtrips_render_channelmap(tmp_path):
    path = tmp_path / "channelmap.csv"
    path.write_text("1,461037500\n2,461062500\n\n3,461087500\n")
    parsed = rsp_fm.parse_channelmap_hz(str(path))
    assert parsed == [
        {"lcn": 1, "freq_hz": 461037500},
        {"lcn": 2, "freq_hz": 461062500},
        {"lcn": 3, "freq_hz": 461087500},
    ]


def test_parse_channelmap_hz_skips_malformed_lines(tmp_path):
    path = tmp_path / "channelmap.csv"
    path.write_text("1,461037500\nnot,a,number\n2,461062500\n")
    parsed = rsp_fm.parse_channelmap_hz(str(path))
    assert parsed == [
        {"lcn": 1, "freq_hz": 461037500},
        {"lcn": 2, "freq_hz": 461062500},
    ]


def test_multi_channel_bridge_builds_offset_per_channel():
    config = rsp_fm.MultiChannelConfig(
        rtl_host="127.0.0.1", rtl_port=1234,
        channels=[{"lcn": 1, "freq_hz": 461_037_500}, {"lcn": 2, "freq_hz": 461_062_500}],
        center_hz=461_050_000, iq_rate=240_000,
        audio_host="127.0.0.1", audio_base_port=17355,
        rigctl_host="127.0.0.1", rigctl_port=14532,
        control_socket="/tmp/does-not-matter.sock",
    )
    bridge = rsp_fm.MultiChannelBridge(config)
    try:
        assert set(bridge.channels) == {1, 2}
        assert bridge.channels[1]["demod"].offset_hz == 461_037_500 - 461_050_000
        assert bridge.channels[2]["demod"].offset_hz == 461_062_500 - 461_050_000
        assert bridge.channels[1]["audio"].port == 17355
        assert bridge.channels[2]["audio"].port == 17356
    finally:
        bridge.close()


def _wideband_multi_config():
    return rsp_fm.MultiChannelConfig(
        rtl_host="127.0.0.1", rtl_port=1234,
        channels=[{"lcn": 1, "freq_hz": 164_100_000},
                  {"lcn": 2, "freq_hz": 164_700_000}],
        center_hz=164_400_000, iq_rate=672_000,
        audio_host="127.0.0.1", audio_base_port=17355,
        rigctl_host="127.0.0.1", rigctl_port=14532,
        control_socket="/tmp/does-not-matter.sock",
    )


def test_multi_bridge_uses_fixed_taps_by_default(monkeypatch):
    """Default (flag unset) MUST be the hardware-validated 121 taps, even at a
    wideband 672kHz rate -- merging to main never changes decode behavior until
    DSD_MULTI_SCALED_TAPS is validated on an RSP1B (CLAUDE.md §8)."""
    monkeypatch.delenv("DSD_MULTI_SCALED_TAPS", raising=False)
    bridge = rsp_fm.MultiChannelBridge(_wideband_multi_config())
    try:
        assert len(bridge.channels[1]["demod"].taps) == 121
    finally:
        bridge.close()


def test_multi_bridge_scales_taps_when_flag_set(monkeypatch):
    """The opt-in A/B flag widens every multi demod's anti-alias filter to hold
    selectivity at the wideband rate (672kHz -> 339 taps)."""
    monkeypatch.setenv("DSD_MULTI_SCALED_TAPS", "1")
    bridge = rsp_fm.MultiChannelBridge(_wideband_multi_config())
    try:
        assert len(bridge.channels[1]["demod"].taps) == 339
        assert len(bridge.channels[2]["demod"].taps) == 339
    finally:
        bridge.close()


def test_rigctl_spectrum_verb():
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    spectrum = rsp_fm.SpectrumState()
    spectrum.update(461_000_000, 976.5, [-100.0, -50.0, -110.0])
    server = rsp_fm.RigctlServer("127.0.0.1", 0, tuner, spectrum=spectrum)
    import json
    resp = json.loads(server.handle_command("SPECTRUM"))
    assert resp["center_hz"] == 461_000_000 and resp["power_db"][1] == -50.0
    # בלי spectrum => לא נתמך
    assert rsp_fm.RigctlServer("127.0.0.1", 0, tuner).handle_command("SPECTRUM") == "RPRT 1\n"


# --- ★ v0.14.0: פאזת דצימציה, מדידת עוצמה אמיתית, ובקרת AGC -----------------

def _u8_noise(complex_samples, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, complex_samples * 2, dtype=np.uint8).tobytes()


def test_decimation_phase_carries_across_chunks_at_multi_rate():
    """★ regression for the multi-mode FEC failure (27.07.2026, hardware).

    `filtered[::D]` used to restart at index 0 on every chunk, which is only
    correct when the chunk length is an exact multiple of D. Single-channel
    got away with it (240000/48000=5, chunk 24000 % 5 == 0); multi did not
    (672000/48000=14, chunk 24000 % 14 == 4), so the sampling grid slipped 10
    samples (~15us of a 208us symbol) at every chunk boundary and the stream
    ran at 48,020 Hz instead of 48,000 (+417 ppm). DSD-FME found `Sync: +DMR`
    and then failed every single CACH/Burst FEC. The output sample count must
    now track iq_rate/audio_rate exactly, with no accumulating drift.
    """
    chunk = 24_000                      # complex samples; 24000 % 14 == 4
    demod = rsp_fm.NfmDemodulator(iq_rate=672_000, audio_rate=48_000)
    produced = sum(len(demod.process(_u8_noise(chunk, seed=i))) // 2
                   for i in range(40))
    expected = 40 * chunk * 48_000 / 672_000
    assert abs(produced - expected) <= 1, (produced, expected)


def test_decimation_phase_is_noop_when_chunk_divides_evenly():
    """The single-channel path (240kHz, decimation 5, chunk 24000) was already
    phase-aligned, so the fix must not perturb it: phase stays 0 throughout."""
    demod = rsp_fm.NfmDemodulator(iq_rate=240_000, audio_rate=48_000)
    for i in range(5):
        assert len(demod.process(_u8_noise(24_000, seed=i))) // 2 == 4_800
        assert demod._decim_phase == 0


def test_decimation_phase_reset_clears_grid():
    demod = rsp_fm.NfmDemodulator(iq_rate=672_000, audio_rate=48_000)
    demod.process(_u8_noise(24_000))
    assert demod._decim_phase != 0
    demod.reset()
    assert demod._decim_phase == 0


def test_iq_level_dbfs_full_scale_is_about_zero_dbfs():
    """A carrier filling the u8 range measures ~0 dBFS -- the scale the UI
    shows must be anchored to something real, not an arbitrary offset."""
    n = 4_096
    phase = 2 * np.pi * np.arange(n) / 16.0
    iq = np.empty(n * 2, dtype=np.uint8)
    # amplitude 126 (not 127) so the waveform peaks just inside the 0/255
    # rails -- otherwise this "clean full-scale" reference would itself
    # register as clipping, which is exactly what clip_frac is meant to catch.
    iq[0::2] = np.round(np.cos(phase) * 126 + 127.5).astype(np.uint8)
    iq[1::2] = np.round(np.sin(phase) * 126 + 127.5).astype(np.uint8)
    level = rsp_fm.iq_level_dbfs(iq.tobytes())
    assert -1.0 < level["rms_dbfs"] < 0.5
    assert level["clip_frac"] == 0.0


def test_iq_level_dbfs_flags_clipping_as_over_gain():
    """clip_frac is the honest over-gain tell: bytes pinned at the 0/255 rail."""
    railed = np.full(4_096, 255, dtype=np.uint8)
    railed[1::2] = 0
    level = rsp_fm.iq_level_dbfs(railed.tobytes())
    assert level["clip_frac"] == 1.0


def test_iq_level_dbfs_quiet_input_is_far_below_full_scale():
    quiet = np.full(4_096, 128, dtype=np.uint8)
    level = rsp_fm.iq_level_dbfs(quiet.tobytes())
    assert level["rms_dbfs"] < -40.0
    assert level["clip_frac"] == 0.0


def test_iq_level_dbfs_handles_empty_input():
    assert rsp_fm.iq_level_dbfs(b"")["rms_dbfs"] is None


def test_demodulator_measures_channel_level():
    demod = rsp_fm.NfmDemodulator(iq_rate=240_000, audio_rate=48_000)
    assert demod.level_dbfs is None
    demod.process(_u8_noise(24_000))
    assert demod.level_dbfs is not None and demod.level_dbfs < 0.0


def test_gain_state_reports_agc_and_never_claims_readback(monkeypatch):
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    monkeypatch.setattr(tuner, "send_command", lambda cmd, val: None)
    assert tuner.gain_state() == {"agc": True, "index": 14,
                                  "index_max": 28, "readback": False}
    tuner.set_fixed_gain(20)
    assert tuner.gain_state()["agc"] is False
    assert tuner.gain_state()["index"] == 20


def test_set_agc_can_return_to_automatic(monkeypatch):
    """★ Before v0.14.0 the first gain nudge switched the SDR to manual gain
    permanently -- there was no command to re-enable AGC at all, only a full
    service restart."""
    sent = []
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    monkeypatch.setattr(tuner, "send_command", lambda cmd, val: sent.append((cmd, val)))
    tuner.nudge_gain(+1)
    assert tuner.agc is False
    sent.clear()
    tuner.set_agc(True)
    assert tuner.agc is True
    assert (rsp_fm.RTL_CMD_SET_GAIN_MODE, 0) in sent


def test_gain_control_server_applies_agc_and_absolute_gain(monkeypatch):
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    monkeypatch.setattr(tuner, "send_command", lambda cmd, val: None)
    server = rsp_fm.GainControlServer("/tmp/unused-dmr-test.sock", tuner)
    server._apply(b"agc:off")
    assert tuner.agc is False
    server._apply(b"gain:22")
    assert tuner.gain_index == 22
    server._apply(b"agc:on")
    assert tuner.agc is True
    server._apply(b"G")
    assert tuner.gain_index == 23 and tuner.agc is False


def test_rigctl_level_verb_reports_measured_values(monkeypatch):
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    monkeypatch.setattr(tuner, "send_command", lambda cmd, val: None)
    snapshot = {"frontend": {"rms_dbfs": -22.5, "peak_dbfs": -9.0, "clip_frac": 0.0},
                "channels": [{"lcn": 3, "freq_hz": 164_325_000,
                              "dbfs": -41.2, "peak_dbfs": -38.0}]}
    server = rsp_fm.RigctlServer("127.0.0.1", 0, tuner, levels=lambda: snapshot)
    import json
    body = json.loads(server.handle_command("LEVEL"))
    assert body["frontend"]["rms_dbfs"] == -22.5
    assert body["channels"][0]["lcn"] == 3
    assert body["gain"] == {"agc": True, "index": 14, "index_max": 28,
                            "readback": False}


def test_rigctl_l_verb_is_measured_not_the_invented_constant(monkeypatch):
    """`l` used to return a hardcoded "-50.0" -- an invented metric
    (CLAUDE.md §8). It must now be the measured front-end level, and must
    refuse rather than fabricate one when nothing has been measured."""
    tuner = rsp_fm.RtlTcpClient("127.0.0.1", 1234, 461_000_000, 240_000)
    monkeypatch.setattr(tuner, "send_command", lambda cmd, val: None)
    measured = rsp_fm.RigctlServer(
        "127.0.0.1", 0, tuner,
        levels=lambda: {"frontend": {"rms_dbfs": -33.25}})
    assert measured.handle_command("l") == "-33.2\n"
    assert rsp_fm.RigctlServer("127.0.0.1", 0, tuner).handle_command("l") == "RPRT 1\n"
    blank = rsp_fm.RigctlServer("127.0.0.1", 0, tuner,
                                levels=lambda: {"frontend": {"rms_dbfs": None}})
    assert blank.handle_command("l") == "RPRT 1\n"


# --- ★ v0.16.0: פענוח מקצה-לקצה מול אות DMR סינתטי --------------------------
# ‏`dmr_signal.py` מייצר אות 4FSK תקני (4800 סמלים/ש', ±1944/±648 Hz) ומריץ
# אותו דרך שרשרת ה-DSP **האמיתית**. זו הבדיקה היחידה שמוכיחה שהדמודולטור
# באמת מפענח DMR — כל השאר בודקות רכיבים. בלי זה, באג הדצימציה (v0.14.0)
# עבר את כל 300+ הבדיקות בירוק בזמן שהתחנה לא פענחה כלום בשטח.

import dmr_signal


def _decode_ser(iq_rate, symbols, chunk=24_000, demod_kwargs=None,
                signal_kwargs=None):
    """מריץ אות סינתטי דרך NfmDemodulator ומחזיר שיעור שגיאות-סמל."""
    iq = dmr_signal.make_dmr_iq(symbols, iq_rate, **(signal_kwargs or {}))
    raw = dmr_signal.to_u8(iq)
    demod = rsp_fm.NfmDemodulator(iq_rate=iq_rate, audio_rate=48_000,
                                  **(demod_kwargs or {}))
    pcm = b"".join(demod.process(raw[i:i + chunk * 2])
                   for i in range(0, len(raw), chunk * 2))
    dev = dmr_signal.pcm_to_deviation(np.frombuffer(pcm, dtype="<i2"))
    n = min(len(symbols) - 40, len(dev) // 10 - 20)
    recovered, _phase, _err, _ = dmr_signal.best_slice(dev[200:], n)
    # השהיית הפילטר לא ידועה מראש -> מיישרים מול הסדרה ששודרה
    return min(np.mean(recovered[:min(len(recovered), len(symbols) - lag)]
                       != symbols[lag:lag + min(len(recovered),
                                                len(symbols) - lag)])
               for lag in range(60))


def _symbols(n=4000, seed=7):
    return np.random.default_rng(seed).integers(0, 4, n)


def test_end_to_end_decodes_clean_dmr_single_channel():
    assert _decode_ser(240_000, _symbols()) == 0.0


def test_end_to_end_decodes_clean_dmr_at_multi_rate():
    """★ רגרסיה לבאג הדצימציה: כאן 24000 % 14 == 4, ולפני v0.14.0 שיעור
    שגיאות-הסמל היה ~53% (האות נהרס לגמרי) בעוד שכל שאר הבדיקות היו ירוקות."""
    assert _decode_ser(672_000, _symbols()) == 0.0


def test_end_to_end_decodes_offset_channel_at_multi_rate():
    """ערוץ שאינו במרכז החלון — המיקסר של multi חייב לשמור על הפענוח."""
    syms = _symbols()
    for offset in (+193_750, -212_500):
        ser = _decode_ser(672_000, syms,
                          demod_kwargs={"offset_hz": offset},
                          signal_kwargs={"offset_hz": offset})
        assert ser == 0.0, (offset, ser)


def test_end_to_end_survives_unaligned_chunk_sizes():
    """קריאה חלקית מ-TCP נותנת chunk באורך שרירותי. לפני v0.14.0 זה לבדו
    הרס את הפענוח (SER ~45%) גם בקצב החד-ערוצי."""
    assert _decode_ser(240_000, _symbols(), chunk=10_007) == 0.0


def test_narrow_cutoff_beats_the_old_10khz_under_noise():
    """★ v0.16.0: תדר-החתך ירד מ-10kHz ל-6kHz. 10kHz העביר רוחב-פס של 20kHz
    לערוץ של 12.5kHz — עודף שהוא רעש בלבד. הבדיקה מקבעת את השיפור הנמדד."""
    syms = _symbols(5000, seed=11)
    noisy = {"amplitude": 0.9, "snr_db": 10, "seed": 5}
    new = _decode_ser(240_000, syms, demod_kwargs={"cutoff_hz": 6_000},
                      signal_kwargs=noisy)
    old = _decode_ser(240_000, syms, demod_kwargs={"cutoff_hz": 10_000},
                      signal_kwargs=noisy)
    assert new < old / 10, (new, old)
    assert new < 0.005


def test_default_cutoff_is_the_narrow_one():
    assert rsp_fm.DEFAULT_CUTOFF_HZ == 6_000.0
    assert rsp_fm.NfmDemodulator(iq_rate=240_000).taps.size == 121


def test_end_to_end_tolerates_realistic_frequency_error():
    """סחיפת TCXO/משדר. ב-164MHz: 1ppm ≈ 164Hz, ולכן 500Hz הוא כבר תרחיש
    פסימי. תדר-החתך הצר חייב לעמוד בזה."""
    ser = _decode_ser(240_000, _symbols(), signal_kwargs={"offset_hz": 500.0})
    assert ser < 0.005, ser      # נמדד ~0.03% — הרחק בתוך יכולת ה-FEC של DMR
