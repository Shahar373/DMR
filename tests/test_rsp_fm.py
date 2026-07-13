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
