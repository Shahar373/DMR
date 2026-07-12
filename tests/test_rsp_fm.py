import socket
import struct

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


def test_audio_server_accepts_replacement_clients():
    server = rsp_fm.AudioServer("127.0.0.1", 0)
    server.start()
    port = server.listener.getsockname()[1]
    first = socket.create_connection(("127.0.0.1", port))
    second = socket.create_connection(("127.0.0.1", port))
    try:
        server.send(b"\x01\x02")
        second.settimeout(2)
        assert second.recv(2) == b"\x01\x02"
    finally:
        first.close()
        second.close()
        server.close()
