#!/usr/bin/env python3
"""SDRplay rsp_tcp -> 48 kHz NFM PCM bridge with a minimal rigctl server.

DSD-FME does not implement an rtl_tcp client. It does support a raw 48 kHz
mono PCM TCP input and can retune an external receiver through the Hamlib/GQRX
rigctl protocol. This helper bridges those two interfaces:

    rsp_tcp (u8 IQ) -> channel filter + FM demod -> PCM TCP -> DSD-FME
                                      ^
                                      +----------- rigctl retune commands

The bridge intentionally uses only Python's standard library and NumPy so it
can run headless on Raspberry Pi OS without GNU Radio or a desktop SDR app.
"""
from __future__ import annotations

import argparse
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

RTL_CMD_SET_FREQ = 0x01
RTL_CMD_SET_SAMPLE_RATE = 0x02
RTL_CMD_SET_GAIN_MODE = 0x03
RTL_CMD_SET_GAIN_BY_INDEX = 0x0D
DEFAULT_IQ_RATE = 240_000
DEFAULT_AUDIO_RATE = 48_000
DEFAULT_CHUNK_SAMPLES = 24_000


def _log(message: str) -> None:
    print(f"rsp_fm: {message}", file=sys.stderr, flush=True)


def rtl_command(command: int, value: int) -> bytes:
    """Return one standard rtl_tcp command packet (1-byte cmd + u32 BE)."""
    return struct.pack(">BI", command & 0xFF, value & 0xFFFFFFFF)


def scaled_taps(iq_rate: int, base_taps: int = 121,
                ref_rate: int = DEFAULT_IQ_RATE, cap: int = 1023) -> int:
    """Tap count that holds the low-pass transition width ~constant across
    sample rates. A windowed-sinc's transition width is ~3.3*fs/taps, so at a
    fixed `base_taps` a wider `iq_rate` gives a *wider* (worse) transition —
    exactly the multi-mode problem: the single-channel filter was tuned at
    240kHz (121 taps, ~6.5kHz transition), but multi runs one wideband capture
    at e.g. 672kHz where 121 taps balloon to ~18kHz, letting the adjacent Cap+
    channel (12.5-25kHz away) bleed through and hurting per-channel decode.
    Scaling taps ∝ iq_rate keeps selectivity constant. Pure/odd/CI-testable.
    At iq_rate==ref_rate this returns base_taps EXACTLY, so the hardware-
    validated single-channel path (240kHz) is byte-for-byte unchanged; only
    the wider multi rates get more taps (CPU re-measured by scripts/spike-dmr-
    multi -- there is headroom, 154%/400% at 6ch)."""
    n = round(base_taps * iq_rate / ref_rate)
    n = min(n, cap)
    if n % 2 == 0:
        n += 1
    return max(n, base_taps if iq_rate >= ref_rate else 3)


def design_lowpass(sample_rate: int, cutoff_hz: float, taps: int = 121) -> np.ndarray:
    """Windowed-sinc low-pass used before integer decimation."""
    if taps < 3 or taps % 2 == 0:
        raise ValueError("taps must be an odd integer >= 3")
    if not 0 < cutoff_hz < sample_rate / 2:
        raise ValueError("cutoff must be between 0 and Nyquist")
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    fc = cutoff_hz / sample_rate
    coefficients = 2 * fc * np.sinc(2 * fc * n)
    coefficients *= np.hamming(taps)
    coefficients /= np.sum(coefficients)
    return coefficients.astype(np.float32)


def compute_power_spectrum(iq, nfft: int) -> Optional[np.ndarray]:
    """Averaged power spectrum of complex IQ, in dBFS, fftshifted.

    Pure/testable: splits `iq` into consecutive `nfft`-sample frames, applies a
    Hann window (rectangular leaks strong carriers into neighbours), averages
    |FFT|^2 across frames (Welch, to knock down variance), and normalises so a
    full-scale complex tone reads ~0 dBFS. Index 0 is -Fs/2, the centre bin is
    DC. Returns None when there is not even one full frame. The scale is
    relative (rsp_tcp only offers 8-bit, gain-limited IQ) -- callers must use an
    adaptive, noise-floor-relative threshold, never a fixed dBFS constant.
    """
    iq = np.asarray(iq, dtype=np.complex64)
    if nfft < 2 or iq.size < nfft:
        return None
    frames = iq.size // nfft
    window = np.hanning(nfft).astype(np.float64)
    coherent_gain = float(np.sum(window))  # full-scale tone -> this peak magnitude
    accum = np.zeros(nfft, dtype=np.float64)
    for i in range(frames):
        block = iq[i * nfft:(i + 1) * nfft].astype(np.complex128) * window
        spectrum = np.fft.fftshift(np.fft.fft(block))
        accum += np.abs(spectrum) ** 2
    accum /= frames
    power_db = 10.0 * np.log10(accum / (coherent_gain ** 2) + 1e-20)
    return power_db.astype(np.float32)


def compute_wideband_plan(channelmap_hz, guard_hz: int = 25_000,
                           max_rate: int = 2_000_000,
                           audio_rate: int = DEFAULT_AUDIO_RATE) -> tuple:
    """(center_hz, iq_rate) for one wideband capture covering every frequency
    in `channelmap_hz` (Hz). Pure -- no hardware, testable without an RSP1B.
    Multi-channel decode (Phase 2): a single RtlTcpClient is tuned once to
    center_hz/iq_rate, and each physical channel gets its own offset-aware
    NfmDemodulator (offset_hz = freq_hz - center_hz) instead of retuning the
    one shared LO per channel (there is only one LO -- see CLAUDE.md §8).

    iq_rate is rounded UP to the nearest multiple of `audio_rate`:
    NfmDemodulator requires iq_rate % audio_rate == 0 for integer decimation,
    so this is computed once here rather than risking a ValueError at every
    one of the N per-channel NfmDemodulator construction sites.
    """
    channelmap_hz = list(channelmap_hz)
    if not channelmap_hz:
        raise ValueError("multi-channel plan needs at least one channel")
    lo, hi = min(channelmap_hz), max(channelmap_hz)
    span = hi - lo
    center_hz = (hi + lo) // 2
    floor_hz = max(span + 2 * guard_hz, audio_rate)
    iq_rate = -(-int(floor_hz) // audio_rate) * audio_rate  # ceil to multiple of audio_rate
    # Ceiling check on the ROUNDED iq_rate (not raw span+guard): a span that
    # just fits (e.g. 1.99MHz) could round up past 2MHz and slip through if
    # checked before rounding. Must match dsd_pty's copy exactly.
    if iq_rate > max_rate:
        raise ValueError(
            f"channel plan needs {iq_rate / 1e6:.4f} MHz IQ rate (span "
            f"{span / 1e6:.4f} MHz + guard, rounded to {audio_rate / 1e3:.0f}kHz) "
            f"-- exceeds {max_rate / 1e6:.1f} MHz max; narrow the plan or use "
            "fewer channels")
    return int(center_hz), int(iq_rate)


def parse_channelmap_hz(path) -> list:
    """[{'lcn': int, 'freq_hz': int}, ...] from the LCN,FREQ_HZ CSV that
    app.py's render_channelmap() already writes for DSD-FME's -C flag.
    Multi mode reuses that exact file/format -- no separate schema."""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lcn_s, _, hz_s = line.partition(",")
            try:
                out.append({"lcn": int(lcn_s), "freq_hz": int(hz_s)})
            except ValueError:
                continue
    return out


class SpectrumState:
    """Latest averaged power spectrum for the current tune, shared between the
    sweep IQ loop (writer) and the rigctl `SPECTRUM` reader thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.center_hz = 0
        self.bin_hz = 0.0
        self.power_db: Optional[list] = None

    def update(self, center_hz: int, bin_hz: float, power_db: list) -> None:
        with self._lock:
            self.center_hz = int(center_hz)
            self.bin_hz = float(bin_hz)
            self.power_db = power_db

    def clear(self) -> None:
        with self._lock:
            self.power_db = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "center_hz": self.center_hz,
                "bin_hz": self.bin_hz,
                "power_db": list(self.power_db) if self.power_db is not None else None,
            }


class NfmDemodulator:
    """Stateful u8 IQ -> signed 16-bit, 48 kHz mono NFM demodulator.

    `offset_hz` (default 0.0, byte-for-byte identical to the original
    single-channel behaviour) lets one wideband IQ stream be decoded at a
    frequency other than its tuned centre: a per-sample complex mixer shifts
    the target channel down to baseband before the existing filter/decimate/
    FM-discriminator/DC-block chain runs unchanged. This is the multi-channel
    (Phase 2) building block -- N of these, one per physical Cap+ channel,
    share one RtlTcpClient tuned once to a wideband centre (see
    MultiChannelBridge) instead of each retuning the single shared LO.
    """

    def __init__(self, iq_rate: int = DEFAULT_IQ_RATE,
                 audio_rate: int = DEFAULT_AUDIO_RATE,
                 cutoff_hz: float = 10_000.0,
                 audio_gain: float = 4.0,
                 taps: int = 121,
                 offset_hz: float = 0.0) -> None:
        if iq_rate % audio_rate:
            raise ValueError("iq_rate must be an integer multiple of audio_rate")
        self.iq_rate = iq_rate
        self.audio_rate = audio_rate
        self.decimation = iq_rate // audio_rate
        self.audio_gain = float(audio_gain)
        # `taps` is the reference count at DEFAULT_IQ_RATE (240kHz); scaled_taps
        # holds transition width constant at wider (multi) rates and returns it
        # UNCHANGED at 240kHz, so single-channel stays byte-identical. See
        # scaled_taps() for why fixed taps under-filter multi's wideband capture.
        self.taps = design_lowpass(iq_rate, cutoff_hz, scaled_taps(iq_rate, taps))
        self.overlap = np.zeros(len(self.taps) - 1, dtype=np.complex64)
        self.previous = np.complex64(1.0 + 0.0j)
        # DC-blocker state (single-pole IIR, y[n] = x[n] - x[n-1] + r*y[n-1]).
        # Must be carried across chunks like `overlap` above -- recomputing a
        # block-wise mean per ~100ms chunk instead would insert a step at
        # every chunk boundary, which is audible to DSD-FME as periodic noise.
        self._dc_r = 0.999
        self._dc_x_prev = 0.0
        self._dc_y_prev = 0.0
        self.offset_hz = float(offset_hz)
        # Running mixer phase (in samples), carried across process() calls for
        # the same reason `overlap`/DC-blocker state is: a fresh phase=0 every
        # chunk would insert an audible discontinuity at each chunk boundary.
        self._mix_phase = 0.0

    def reset(self) -> None:
        self.overlap.fill(0)
        self.previous = np.complex64(1.0 + 0.0j)
        self._dc_x_prev = 0.0
        self._dc_y_prev = 0.0
        self._mix_phase = 0.0

    def _dc_block(self, fm: np.ndarray) -> np.ndarray:
        """Remove DC/slow drift with a stateful one-pole filter (~8 Hz cutoff
        at 48 kHz), carrying x[n-1]/y[n-1] across calls so chunk boundaries
        don't produce a discontinuity."""
        out = np.empty_like(fm)
        x_prev = self._dc_x_prev
        y_prev = self._dc_y_prev
        r = self._dc_r
        for i in range(fm.shape[0]):
            x = fm[i]
            y = x - x_prev + r * y_prev
            out[i] = y
            x_prev = x
            y_prev = y
        self._dc_x_prev = x_prev
        self._dc_y_prev = y_prev
        return out

    def process(self, raw: bytes) -> bytes:
        values = np.frombuffer(raw, dtype=np.uint8)
        if values.size < 2:
            return b""
        if values.size & 1:
            values = values[:-1]
        floats = (values.astype(np.float32) - 127.5) / 128.0
        iq = (floats[0::2] + 1j * floats[1::2]).astype(np.complex64, copy=False)

        if self.offset_hz:
            n = np.arange(iq.size, dtype=np.float64)
            mixer = np.exp(-2j * np.pi * self.offset_hz / self.iq_rate
                           * (n + self._mix_phase))
            iq = (iq * mixer).astype(np.complex64)
            self._mix_phase = (self._mix_phase + iq.size) % (
                self.iq_rate / max(abs(self.offset_hz), 1e-9))

        extended = np.concatenate((self.overlap, iq))
        filtered = np.convolve(extended, self.taps, mode="valid")
        self.overlap = extended[-(len(self.taps) - 1):].copy()
        baseband = filtered[::self.decimation]
        if baseband.size == 0:
            return b""

        previous = np.empty_like(baseband)
        previous[0] = self.previous
        previous[1:] = baseband[:-1]
        self.previous = baseband[-1]
        fm = np.angle(baseband * np.conj(previous)).astype(np.float32)
        fm = self._dc_block(fm)
        pcm = np.clip(fm * (32767.0 / np.pi) * self.audio_gain,
                      -32768, 32767).astype("<i2")
        return pcm.tobytes()


DEFAULT_IQ_READ_TIMEOUT = 5.0


class RtlTcpClient:
    def __init__(self, host: str, port: int, frequency: int, sample_rate: int,
                 read_timeout: float = DEFAULT_IQ_READ_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.frequency = int(frequency)
        self.sample_rate = int(sample_rate)
        self.read_timeout = read_timeout
        self.sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.generation = 0
        self.gain_index = 14

    def connect(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: Optional[BaseException] = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=2.0)
                sock.settimeout(5.0)
                header = self._recv_exact(sock, 12)
                if header[:4] not in (b"RTL0", b"RSP0"):
                    raise RuntimeError(f"unexpected rtl_tcp header: {header[:4]!r}")
                # Keep a bounded read timeout (not None/blocking-forever): if
                # rsp_tcp stays connected but stops sending samples (SDR/USB
                # stall), recv() must eventually raise so the caller notices
                # instead of hanging the bridge indefinitely.
                sock.settimeout(self.read_timeout)
                self.sock = sock
                self.send_command(RTL_CMD_SET_SAMPLE_RATE, self.sample_rate)
                self.send_command(RTL_CMD_SET_FREQ, self.frequency)
                self.send_command(RTL_CMD_SET_GAIN_MODE, 0)
                _log(f"connected to rtl_tcp {self.host}:{self.port}; "
                     f"frequency={self.frequency} Hz, IQ={self.sample_rate} sps")
                return
            except (OSError, RuntimeError) as error:
                last_error = error
                time.sleep(0.25)
        raise RuntimeError(f"could not connect to rsp_tcp: {last_error}")

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("rtl_tcp closed during handshake")
            data.extend(chunk)
        return bytes(data)

    def send_command(self, command: int, value: int) -> None:
        if self.sock is None:
            raise RuntimeError("rtl_tcp is not connected")
        with self._send_lock:
            self.sock.sendall(rtl_command(command, value))

    def set_frequency(self, frequency: int) -> None:
        frequency = int(frequency)
        if frequency <= 0:
            raise ValueError("frequency must be positive")
        self.send_command(RTL_CMD_SET_FREQ, frequency)
        with self._state_lock:
            self.frequency = frequency
            self.generation += 1
        _log(f"tuned to {frequency} Hz")

    def get_frequency(self) -> int:
        with self._state_lock:
            return self.frequency

    def set_fixed_gain(self, index: int) -> None:
        """Force manual gain to a fixed index (disables AGC). The sweep needs a
        stable gain across retunes so power bins are comparable hop-to-hop; with
        AGC on, each hop settles differently and the map is meaningless."""
        self.gain_index = max(0, min(28, int(index)))
        self.send_command(RTL_CMD_SET_GAIN_MODE, 1)
        self.send_command(RTL_CMD_SET_GAIN_BY_INDEX, self.gain_index)
        _log(f"manual gain index {self.gain_index}/28")

    def nudge_gain(self, direction: int) -> None:
        self.set_fixed_gain(self.gain_index + direction)

    def recv(self, size: int) -> bytes:
        if self.sock is None:
            raise RuntimeError("rtl_tcp is not connected")
        try:
            return self.sock.recv(size)
        except socket.timeout as error:
            raise ConnectionError(
                f"rsp_tcp sent no IQ samples for {self.read_timeout}s"
            ) from error

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


class AudioServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.listener: Optional[socket.socket] = None
        self.client: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self.stop_event = threading.Event()

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(4)
        listener.settimeout(0.5)
        self.listener = listener
        threading.Thread(target=self._accept_loop, daemon=True).start()
        _log(f"PCM audio listening on {self.host}:{listener.getsockname()[1]}")

    def _accept_loop(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                client, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.settimeout(2.0)
            with self._lock:
                old, self.client = self.client, client
            if old is not None:
                old.close()
            _log(f"DSD-FME audio client connected from {address[0]}:{address[1]}")

    def send(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            client = self.client
        if client is None:
            return
        try:
            client.sendall(pcm)
        except OSError:
            with self._lock:
                if self.client is client:
                    self.client = None
            client.close()
            _log("DSD-FME audio client disconnected")

    def close(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            self.listener.close()
        with self._lock:
            client, self.client = self.client, None
        if client is not None:
            client.close()


class AudioSender:
    """Decouples PCM delivery from the IQ-reading thread. `AudioServer.send`
    can block for up to its client socket's timeout (2s) if DSD-FME stalls
    reading; doing that inline in the IQ loop would back up samples from
    rsp_tcp and delay retune-generation handling at the same time. This runs
    its own thread pulling off a small bounded queue, dropping the oldest
    chunk under sustained backpressure rather than blocking upstream."""

    def __init__(self, audio: "AudioServer", maxsize: int = 50) -> None:
        self.audio = audio
        self.queue: "queue.Queue[bytes]" = queue.Queue(maxsize=maxsize)
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, pcm: bytes) -> None:
        if not pcm:
            return
        try:
            self.queue.put_nowait(pcm)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(pcm)
            except queue.Full:
                pass

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                pcm = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.audio.send(pcm)

    def close(self) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class RigctlServer:
    def __init__(self, host: str, port: int, tuner: RtlTcpClient,
                 spectrum: "Optional[SpectrumState]" = None) -> None:
        self.host = host
        self.port = port
        self.tuner = tuner
        self.spectrum = spectrum
        self.listener: Optional[socket.socket] = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(4)
        listener.settimeout(0.5)
        self.listener = listener
        threading.Thread(target=self._accept_loop, daemon=True).start()
        _log(f"rigctl listening on {self.host}:{listener.getsockname()[1]}")

    def _accept_loop(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve_client, args=(client,), daemon=True).start()

    def _serve_client(self, client: socket.socket) -> None:
        with client:
            file = client.makefile("rwb", buffering=0)
            while not self.stop_event.is_set():
                line = file.readline()
                if not line:
                    return
                command = line.decode("ascii", "replace").strip()
                try:
                    response = self.handle_command(command)
                except Exception as error:
                    _log(f"rigctl command failed ({command!r}): {error}")
                    response = "RPRT 1\n"
                file.write(response.encode("ascii"))
                if command.lower() in ("q", "quit"):
                    return

    def handle_command(self, command: str) -> str:
        parts = command.split()
        if not parts:
            return "RPRT 0\n"
        verb = parts[0]
        if verb == "f":
            return f"{self.tuner.get_frequency()}\n"
        if verb == "F" and len(parts) >= 2:
            self.tuner.set_frequency(int(parts[1]))
            return "RPRT 0\n"
        # Discovery sweep extension: the frequency-discovery loop in app.py is
        # the only client during a sweep (DSD-FME is not running), so a custom
        # SPECTRUM verb on the same rigctl connection returns the current
        # averaged power spectrum as one JSON line. Harmless in decode mode --
        # DSD-FME never sends it.
        if verb == "SPECTRUM" and self.spectrum is not None:
            import json
            return json.dumps(self.spectrum.snapshot()) + "\n"
        if verb == "M":
            return "RPRT 0\n"
        if verb == "m":
            return "NFM\n12000\n"
        if verb == "l":
            return "-50.0\n"
        if verb == "L":
            return "RPRT 0\n"
        if verb in ("q", "quit"):
            return "RPRT 0\n"
        return "RPRT 1\n"

    def close(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            self.listener.close()


class GainControlServer:
    def __init__(self, path: str, tuner: RtlTcpClient) -> None:
        self.path = path
        self.tuner = tuner
        self.sock: Optional[socket.socket] = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(self.path)
        sock.settimeout(0.5)
        self.sock = sock
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        assert self.sock is not None
        while not self.stop_event.is_set():
            try:
                data = self.sock.recv(64)
            except socket.timeout:
                continue
            except OSError:
                return
            if data in (b"G", b"gain_up"):
                self.tuner.nudge_gain(+1)
            elif data in (b"g", b"gain_down"):
                self.tuner.nudge_gain(-1)

    def close(self) -> None:
        self.stop_event.set()
        if self.sock is not None:
            self.sock.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


@dataclass
class BridgeConfig:
    rtl_host: str
    rtl_port: int
    audio_host: str
    audio_port: int
    rigctl_host: str
    rigctl_port: int
    control_socket: str
    frequency: int
    iq_rate: int
    audio_rate: int
    audio_gain: float
    sweep: bool = False
    nfft: int = 2048
    sweep_frames: int = 64
    gain_index: int = 14


def run(config: BridgeConfig) -> int:
    tuner = RtlTcpClient(config.rtl_host, config.rtl_port,
                         config.frequency, config.iq_rate)
    audio = AudioServer(config.audio_host, config.audio_port)
    sender = AudioSender(audio)
    rigctl = RigctlServer(config.rigctl_host, config.rigctl_port, tuner)
    gain = GainControlServer(config.control_socket, tuner)
    demod = NfmDemodulator(iq_rate=config.iq_rate,
                           audio_rate=config.audio_rate,
                           audio_gain=config.audio_gain)
    stop_event = threading.Event()

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()
        tuner.close()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        tuner.connect()
        audio.start()
        sender.start()
        rigctl.start()
        gain.start()
        bytes_per_chunk = DEFAULT_CHUNK_SAMPLES * 2
        buffer = bytearray()
        generation = tuner.generation
        discard_chunks = 0
        while not stop_event.is_set():
            data = tuner.recv(max(4096, bytes_per_chunk - len(buffer)))
            if not data:
                raise ConnectionError("rsp_tcp closed the IQ connection")
            buffer.extend(data)
            while len(buffer) >= bytes_per_chunk:
                chunk = bytes(buffer[:bytes_per_chunk])
                del buffer[:bytes_per_chunk]
                if tuner.generation != generation:
                    generation = tuner.generation
                    demod.reset()
                    discard_chunks = 2
                pcm = demod.process(chunk)
                if discard_chunks:
                    discard_chunks -= 1
                else:
                    sender.submit(pcm)
    except (OSError, RuntimeError, ConnectionError, ValueError) as error:
        if not stop_event.is_set():
            _log(f"fatal: {error}")
            return 1
        return 0
    finally:
        gain.close()
        rigctl.close()
        sender.close()
        audio.close()
        tuner.close()
    return 0


def run_sweep(config: BridgeConfig) -> int:  # pragma: no cover - hardware runtime
    """Frequency-discovery sweep: hold the SDR, force fixed gain, and publish an
    averaged power spectrum for the current centre over the rigctl SPECTRUM verb.
    The NFM demod / audio path is skipped entirely -- only the FFT is needed.
    app.py drives the frequency grid via rigctl `F` and reads each `SPECTRUM`."""
    tuner = RtlTcpClient(config.rtl_host, config.rtl_port,
                         config.frequency, config.iq_rate)
    spectrum = SpectrumState()
    rigctl = RigctlServer(config.rigctl_host, config.rigctl_port, tuner,
                          spectrum=spectrum)
    stop_event = threading.Event()

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()
        tuner.close()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    nfft = max(2, int(config.nfft))
    frames_per_avg = max(1, int(config.sweep_frames))
    bytes_needed = nfft * frames_per_avg * 2
    bin_hz = config.iq_rate / float(nfft)
    try:
        tuner.connect()
        tuner.set_fixed_gain(config.gain_index)
        rigctl.start()
        buffer = bytearray()
        generation = tuner.generation
        while not stop_event.is_set():
            data = tuner.recv(max(4096, bytes_needed - len(buffer)))
            if not data:
                raise ConnectionError("rsp_tcp closed the IQ connection")
            if tuner.generation != generation:
                # A retune happened: drop samples straddling the boundary and
                # blank the published spectrum so app.py never reads stale bins.
                generation = tuner.generation
                buffer.clear()
                spectrum.clear()
                continue
            buffer.extend(data)
            while len(buffer) >= bytes_needed:
                block = bytes(buffer[:bytes_needed])
                del buffer[:bytes_needed]
                values = np.frombuffer(block, dtype=np.uint8)
                floats = (values.astype(np.float32) - 127.5) / 128.0
                iq = floats[0::2] + 1j * floats[1::2]
                power_db = compute_power_spectrum(iq, nfft)
                if power_db is not None:
                    spectrum.update(tuner.get_frequency(), bin_hz, power_db.tolist())
    except (OSError, RuntimeError, ConnectionError, ValueError) as error:
        if not stop_event.is_set():
            _log(f"sweep fatal: {error}")
            return 1
        return 0
    finally:
        rigctl.close()
        tuner.close()
    return 0


@dataclass
class MultiChannelConfig:
    """N-channel counterpart of BridgeConfig. One wideband RtlTcpClient tuned
    to center_hz/iq_rate feeds N offset-aware NfmDemodulator instances (one
    per `channels` entry), each serving its own AudioServer/AudioSender pair
    on `audio_host:audio_base_port + i`. rigctl/gain stay single and shared
    (there is one physical tuner regardless of how many channels are later
    demodulated in software -- see compute_wideband_plan)."""
    rtl_host: str
    rtl_port: int
    channels: list            # [{"lcn": int, "freq_hz": int}, ...]
    center_hz: int
    iq_rate: int
    audio_host: str
    audio_base_port: int
    rigctl_host: str
    rigctl_port: int
    control_socket: str
    audio_rate: int = DEFAULT_AUDIO_RATE
    audio_gain: float = 4.0


class MultiChannelBridge:
    """Owns one wideband RtlTcpClient + N (demod, AudioServer, AudioSender)
    triples, one per physical channel. `process_chunk` feeds the same raw IQ
    chunk through every channel's demodulator -- the wideband capture is read
    once per chunk regardless of N."""

    def __init__(self, config: MultiChannelConfig) -> None:
        self.config = config
        self.tuner = RtlTcpClient(config.rtl_host, config.rtl_port,
                                  config.center_hz, config.iq_rate)
        self.channels: "dict[int, dict]" = {}   # lcn -> {"demod":, "audio":, "sender":}
        for i, ch in enumerate(config.channels):
            demod = NfmDemodulator(iq_rate=config.iq_rate, audio_rate=config.audio_rate,
                                   audio_gain=config.audio_gain,
                                   offset_hz=ch["freq_hz"] - config.center_hz)
            audio = AudioServer(config.audio_host, config.audio_base_port + i)
            sender = AudioSender(audio)
            self.channels[int(ch["lcn"])] = {"demod": demod, "audio": audio,
                                             "sender": sender, "freq_hz": int(ch["freq_hz"])}

    def start(self) -> None:
        self.tuner.connect()
        for ch in self.channels.values():
            ch["audio"].start()
            ch["sender"].start()

    def process_chunk(self, raw: bytes) -> None:
        for ch in self.channels.values():
            pcm = ch["demod"].process(raw)
            ch["sender"].submit(pcm)

    def reset(self) -> None:
        for ch in self.channels.values():
            ch["demod"].reset()

    def close(self) -> None:
        for ch in self.channels.values():
            ch["sender"].close()
            ch["audio"].close()
        self.tuner.close()


def run_multi(config: MultiChannelConfig) -> int:  # pragma: no cover - hardware runtime
    """Multi-channel counterpart of run(): one wideband IQ read per chunk,
    fanned out to N channel demodulators via MultiChannelBridge. Same
    read/reconnect/generation-discard skeleton as run()/run_sweep()."""
    bridge = MultiChannelBridge(config)
    rigctl = RigctlServer(config.rigctl_host, config.rigctl_port, bridge.tuner)
    gain = GainControlServer(config.control_socket, bridge.tuner)
    stop_event = threading.Event()

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()
        bridge.tuner.close()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        bridge.start()
        rigctl.start()
        gain.start()
        bytes_per_chunk = DEFAULT_CHUNK_SAMPLES * 2
        buffer = bytearray()
        generation = bridge.tuner.generation
        discard_chunks = 0
        while not stop_event.is_set():
            data = bridge.tuner.recv(max(4096, bytes_per_chunk - len(buffer)))
            if not data:
                raise ConnectionError("rsp_tcp closed the IQ connection")
            buffer.extend(data)
            while len(buffer) >= bytes_per_chunk:
                chunk = bytes(buffer[:bytes_per_chunk])
                del buffer[:bytes_per_chunk]
                if bridge.tuner.generation != generation:
                    generation = bridge.tuner.generation
                    bridge.reset()
                    discard_chunks = 2
                if discard_chunks:
                    discard_chunks -= 1
                else:
                    bridge.process_chunk(chunk)
    except (OSError, RuntimeError, ConnectionError, ValueError) as error:
        if not stop_event.is_set():
            _log(f"multi fatal: {error}")
            return 1
        return 0
    finally:
        gain.close()
        rigctl.close()
        bridge.close()
    return 0


def parse_endpoint(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator:
        raise argparse.ArgumentTypeError("endpoint must be HOST:PORT")
    return host or "127.0.0.1", int(port)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtl", default="127.0.0.1:1234")
    parser.add_argument("--audio", default="127.0.0.1:7355")
    parser.add_argument("--rigctl", default="127.0.0.1:4532")
    parser.add_argument("--control-socket", default="/run/dmr/rsp-fm.sock")
    parser.add_argument("--frequency", type=int, default=None,
                        help="required unless --multi-channelmap is given")
    parser.add_argument("--iq-rate", type=int, default=DEFAULT_IQ_RATE)
    parser.add_argument("--audio-rate", type=int, default=DEFAULT_AUDIO_RATE)
    parser.add_argument("--audio-gain", type=float, default=4.0)
    parser.add_argument("--sweep", action="store_true",
                        help="frequency-discovery sweep mode (FFT power, no demod)")
    parser.add_argument("--nfft", type=int, default=2048)
    parser.add_argument("--sweep-frames", type=int, default=64)
    parser.add_argument("--gain-index", type=int, default=14)
    parser.add_argument("--multi-channelmap", default=None,
                        help="LCN,FREQ_HZ CSV path -> multi-channel decode mode "
                             "(one wideband capture, N offset-aware demodulators)")
    parser.add_argument("--audio-tcp-base", default=None,
                        help="HOST:PORT base for multi-channel mode; channel i "
                             "gets base_port + i (default: --audio's port)")
    args = parser.parse_args()
    rtl_host, rtl_port = parse_endpoint(args.rtl)
    audio_host, audio_port = parse_endpoint(args.audio)
    rigctl_host, rigctl_port = parse_endpoint(args.rigctl)

    if args.multi_channelmap:
        # center_hz/iq_rate are NOT recomputed here: rsp_tcp (a separate
        # process, driven by dsd_pty's own build_multi_rsp_tcp_command) must
        # tune to the *exact* same center/rate this bridge assumes, so the
        # orchestrator (dsd_pty._run_multi) computes both ONCE via
        # compute_wideband_plan and passes them explicitly as --frequency/
        # --iq-rate -- two independent computations from possibly-diverging
        # default constants would risk the two processes silently disagreeing.
        if args.frequency is None:
            parser.error("--frequency (wideband center Hz) is required with "
                         "--multi-channelmap -- compute it once with "
                         "compute_wideband_plan() and pass it explicitly")
        channels = parse_channelmap_hz(args.multi_channelmap)
        if not channels:
            parser.error(f"--multi-channelmap {args.multi_channelmap!r} has no channels")
        base_host, base_port = (parse_endpoint(args.audio_tcp_base)
                                if args.audio_tcp_base else (audio_host, audio_port))
        config = MultiChannelConfig(
            rtl_host=rtl_host, rtl_port=rtl_port,
            channels=channels, center_hz=args.frequency, iq_rate=args.iq_rate,
            audio_host=base_host, audio_base_port=base_port,
            rigctl_host=rigctl_host, rigctl_port=rigctl_port,
            control_socket=args.control_socket,
            audio_rate=args.audio_rate, audio_gain=args.audio_gain,
        )
        return run_multi(config)

    if args.frequency is None:
        parser.error("--frequency is required unless --multi-channelmap is given")
    config = BridgeConfig(
        rtl_host=rtl_host,
        rtl_port=rtl_port,
        audio_host=audio_host,
        audio_port=audio_port,
        rigctl_host=rigctl_host,
        rigctl_port=rigctl_port,
        control_socket=args.control_socket,
        frequency=args.frequency,
        iq_rate=args.iq_rate,
        audio_rate=args.audio_rate,
        audio_gain=args.audio_gain,
        sweep=args.sweep,
        nfft=args.nfft,
        sweep_frames=args.sweep_frames,
        gain_index=args.gain_index,
    )
    return run_sweep(config) if args.sweep else run(config)


if __name__ == "__main__":
    raise SystemExit(main())
