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
    """At the single-channel reference rate (240kHz) scaled_taps must return
    the base count EXACTLY -- the hardware-validated dmr/scan path stays
    byte-for-byte unchanged; only wider multi rates get more taps."""
    assert rsp_fm.scaled_taps(rsp_fm.DEFAULT_IQ_RATE, 121) == 121
    demod = rsp_fm.NfmDemodulator(iq_rate=rsp_fm.DEFAULT_IQ_RATE)
    assert len(demod.taps) == 121


def test_scaled_taps_widens_for_multi_rate_and_stays_odd():
    """A ~2.8x wider capture (672kHz, the multi_164cluster rate) needs ~2.8x
    the taps to hold the transition width -- otherwise the adjacent Cap+
    channel bleeds through. Always odd (design_lowpass requires it)."""
    n = rsp_fm.scaled_taps(672_000, 121)
    assert n == 339                      # round(121 * 672000/240000) -> 338.8 -> 339
    assert n % 2 == 1
    demod = rsp_fm.NfmDemodulator(iq_rate=672_000)
    assert len(demod.taps) == 339
    assert rsp_fm.scaled_taps(240_000, 121) < n   # monotonic in rate


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
