#!/usr/bin/env python3
# ============================================================================
#  DMR - DSD-FME PTY harness and SDRplay audio bridge supervisor
# ----------------------------------------------------------------------------
# DSD-FME has no rtl_tcp IQ client. rsp_tcp therefore cannot be passed directly
# to `dsd-fme -i`. The runtime chain is:
#
#   RSP1B -> rsp_tcp (u8 IQ) -> rsp_fm.py (NFM/48k PCM + rigctl) -> DSD-FME
#
# This module supervises all three children, keeps DSD-FME under a PTY, parses
# recognized events to UDP JSON, and mirrors every raw DSD-FME line to journald.
# ============================================================================
from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import sys
import time
from pathlib import Path

DEFAULT_UDP = "127.0.0.1:5555"
DSD_BIN = os.environ.get("DSD_BIN", "dsd-fme")
CTRL_SOCK_PATH = os.environ.get("DSD_CTRL_SOCK", "/run/dmr/dsd-ctrl.sock")
BRIDGE_CTRL_SOCK = os.environ.get("DSD_BRIDGE_CTRL_SOCK", "/run/dmr/rsp-fm.sock")
RSP_TCP_HOST = os.environ.get("DSD_RTLTCP", "127.0.0.1:1234")
AUDIO_TCP_HOST = os.environ.get("DSD_AUDIO_TCP", "127.0.0.1:7355")
AUDIO_TCP_BASE_HOST = os.environ.get("DSD_AUDIO_TCP_BASE", "127.0.0.1:7355")
RIGCTL_HOST = os.environ.get("DSD_RIGCTL", "127.0.0.1:4532")
RSP_FM_BIN = os.environ.get(
    "RSP_FM_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rsp_fm.py")
)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# --- DSD-FME output parsing -------------------------------------------------
_RE_VOICE_CALL = re.compile(
    r"SLOT\s+(?P<slot>\d)\s+TGT=(?P<tgt>\d+)\s+SRC=(?P<src>\d+)\s+"
    r"(?:Cap\+\s+)?(?P<kind>Group|Private|Unit to Unit)(?:\s+TXI)?\s+Call"
    r"(?:\s+Rest LSN:\s*(?P<rest_lsn>\d+))?", re.I)
_RE_DATA_HEADER = re.compile(
    r"Slot\s+(?P<slot>\d)\s+Data Header\s*-\s*(?P<addr>Indiv|Group)\s*-\s*"
    r"(?P<delivery>Confirmed Delivery|Unconfirmed Delivery|Response Packet)"
    r".*?Source:\s*(?P<src>\d+)\s+Target:\s*(?P<tgt>\d+)", re.I)
_RE_LRRP_REQ = re.compile(
    r"LRRP\s+SRC:\s*(?P<src>\d+);\s*Response to TGT:\s*(?P<tgt>\d+);", re.I)
_RE_LRRP_POS = re.compile(
    r"(?:SRC[:=]?\s*(?P<src>\d+)\D*?)?Lat:\s*(?P<lat>-?[0-9.]+)\s+"
    r"Lon:\s*(?P<lon>-?[0-9.]+)", re.I)
_RE_ENCRYPTION = re.compile(r"SLOT\s+(?P<slot>\d)\s+Protected LC\b", re.I)
_RE_QUALITY_ERR = re.compile(
    r"(CACH/Burst FEC ERR|CSBK \(CRC ERR\)|CSBK \(FEC ERR\)|SLCO CRC ERR)", re.I)
_QUALITY_ERR_MAP = {
    "cach/burst fec err": "CACH_BURST_FEC",
    "csbk (crc err)": "CSBK_CRC",
    "csbk (fec err)": "CSBK_FEC",
    "slco crc err": "SLCO_CRC",
}
_RE_QUALITY_CC = re.compile(r"Color Code=(?P<cc>\d+)", re.I)
# Discovery-only patterns (see parse_dsd_line emit_status). A clean "Sync: +DMR"
# line (no error) is the most reliable positive "this frequency carries DMR"
# signal, and the Capacity Plus Channel Status line is the trunk control-channel
# fingerprint (periodic CSBK + Rest LSN). Both are printed by DSD-FME but dropped
# in normal operation to keep the UDP feed quiet (~80% of output is housekeeping).
_RE_SYNC = re.compile(r"Sync:\s*\+DMR", re.I)
_RE_SYNC_SLOT = re.compile(r"\[\s*slot\s*(?P<slot>\d)\s*\]", re.I)
_RE_CHAN_STATUS = re.compile(
    r"Channel Status\b.*?Rest LSN:\s*(?P<rest_lsn>\d+)", re.I)
_RE_LSN_STATE = re.compile(r"LSN\s*(?P<lsn>\d+):\s*(?P<state>Rest|Idle|\d+)", re.I)


def clean_dsd_line(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "").strip()


def parse_dsd_line(text, emit_status=False):
    """Parse one DSD-FME line into a typed event, or None for housekeeping.

    `emit_status` is enabled only during a discovery probe: it additionally
    surfaces a positive `sync` event (proto + color code + active slot) from a
    clean `Sync: +DMR` line and a `channel_status` event (Rest LSN + per-LSN
    states) from a Capacity Plus Channel Status line. In normal dmr/scan
    operation `emit_status` stays False, so parsing is byte-for-byte identical
    to before (those lines return None and never hit the UDP feed)."""
    if not text or not text.strip():
        return None
    text = clean_dsd_line(text)

    match = _RE_VOICE_CALL.search(text)
    if match:
        kind = match.group("kind").lower()
        event = {
            "type": "voice_call",
            "slot": int(match.group("slot")),
            "src": int(match.group("src")),
            "call_type": "group" if kind == "group" else "private",
            "crc_err": "(CRC ERR)" in text,
        }
        if kind == "group":
            event["tg"] = int(match.group("tgt"))
        else:
            event["tgt"] = int(match.group("tgt"))
        if match.group("rest_lsn"):
            event["lcn"] = int(match.group("rest_lsn"))
        return event

    match = _RE_DATA_HEADER.search(text)
    if match:
        return {
            "type": "data_header",
            "slot": int(match.group("slot")),
            "src": int(match.group("src")),
            "tgt": int(match.group("tgt")),
            "call_type": "data",
            "delivery": match.group("delivery"),
        }

    match = _RE_LRRP_REQ.search(text)
    if match:
        return {
            "type": "lrrp_request",
            "src": int(match.group("src")),
            "tgt": int(match.group("tgt")),
            "call_type": "lrrp",
        }

    if "lat:" in text.lower() and "lon:" in text.lower():
        match = _RE_LRRP_POS.search(text)
        if match:
            event = {
                "type": "lrrp_position",
                "lat": float(match.group("lat")),
                "lon": float(match.group("lon")),
                "call_type": "lrrp",
            }
            if match.group("src"):
                event["src"] = int(match.group("src"))
            return event

    match = _RE_ENCRYPTION.search(text)
    if match:
        return {"type": "encryption", "slot": int(match.group("slot")), "encrypted": True}

    match = _RE_QUALITY_ERR.search(text)
    if match:
        event = {
            "type": "quality",
            "error_type": _QUALITY_ERR_MAP.get(match.group(1).lower(), match.group(1).upper()),
        }
        cc = _RE_QUALITY_CC.search(text)
        if cc:
            event["cc"] = int(cc.group("cc"))
        return event

    if emit_status:
        # Error'd sync lines were already returned as `quality` above, so only
        # clean sync lines reach here.
        if _RE_SYNC.search(text):
            event = {"type": "sync", "proto": "dmr"}
            cc = _RE_QUALITY_CC.search(text)
            if cc:
                event["cc"] = int(cc.group("cc"))
            slot = _RE_SYNC_SLOT.search(text)
            if slot:
                event["slot"] = int(slot.group("slot"))
            if "|" in text:
                state = text.rsplit("|", 1)[-1].strip()
                if state:
                    event["state"] = state
            return event
        match = _RE_CHAN_STATUS.search(text)
        if match:
            event = {"type": "channel_status",
                     "rest_lsn": int(match.group("rest_lsn"))}
            cc = _RE_QUALITY_CC.search(text)
            if cc:
                event["cc"] = int(cc.group("cc"))
            states = _RE_LSN_STATE.findall(text)
            if states:
                event["lsn_states"] = [
                    {"lsn": int(lsn), "state": state} for lsn, state in states
                ]
            return event

    return None


# --- Command generation -----------------------------------------------------
def _split_endpoint(value: str, default_port: int) -> tuple[str, str]:
    host, separator, port = value.rpartition(":")
    if not separator:
        return value or "127.0.0.1", str(default_port)
    return host or "127.0.0.1", port or str(default_port)


def build_rsp_tcp_command(env):
    host, port = _split_endpoint(env.get("DSD_RTLTCP", RSP_TCP_HOST), 1234)
    command = [
        os.environ.get("RSP_TCP_BIN", "rsp_tcp"),
        "-a", host,
        "-p", port,
        "-s", str(env.get("DSD_IQ_RATE", "240000")),
    ]
    control = env.get("DSD_CONTROL_FREQ")
    if control:
        command += ["-f", str(control)]
    return command


def build_bridge_command(env):
    control = env.get("DSD_CONTROL_FREQ")
    if not control:
        raise ValueError("DSD_CONTROL_FREQ is required")
    command = [
        sys.executable,
        "-u",
        os.environ.get("RSP_FM_BIN", RSP_FM_BIN),
        "--rtl", env.get("DSD_RTLTCP", RSP_TCP_HOST),
        "--audio", env.get("DSD_AUDIO_TCP", AUDIO_TCP_HOST),
        "--rigctl", env.get("DSD_RIGCTL", RIGCTL_HOST),
        "--control-socket", env.get("DSD_BRIDGE_CTRL_SOCK", BRIDGE_CTRL_SOCK),
        "--frequency", str(control),
        "--iq-rate", str(env.get("DSD_IQ_RATE", "240000")),
        "--audio-gain", str(env.get("DSD_AUDIO_GAIN", "4.0")),
    ]
    if env.get("DSD_SWEEP", "").lower() in ("1", "true", "yes"):
        command += ["--sweep",
                    "--nfft", str(env.get("DSD_SWEEP_NFFT", "2048")),
                    "--sweep-frames", str(env.get("DSD_SWEEP_FRAMES", "64")),
                    "--gain-index", str(env.get("DSD_SWEEP_GAIN", "14"))]
    return command


def compute_wideband_plan(channelmap_hz, guard_hz=25_000, max_rate=2_000_000,
                          audio_rate=48_000):
    """(center_hz, iq_rate) for one wideband capture covering every channel.
    Pure -- stdlib only, no numpy dependency (unlike rsp_fm's copy, which
    exists for that module's own standalone-CLI convenience). This is the
    ONE authoritative computation for multi mode: rsp_tcp and rsp_fm.py are
    two independent subprocesses (see _run_multi) that must tune to the
    exact same center/rate, so dsd_pty computes it once here and passes the
    result explicitly to both build_multi_rsp_tcp_command and
    build_multi_bridge_command -- never recomputed downstream.

    iq_rate is rounded UP to the nearest multiple of audio_rate (rsp_fm's
    NfmDemodulator requires iq_rate % audio_rate == 0 for integer decimation)."""
    channelmap_hz = list(channelmap_hz)
    if not channelmap_hz:
        raise ValueError("multi-channel plan needs at least one channel")
    lo, hi = min(channelmap_hz), max(channelmap_hz)
    span = hi - lo
    if span + 2 * guard_hz > max_rate:
        raise ValueError(
            f"channel plan spans {span / 1e6:.4f} MHz + guard "
            f"({2 * guard_hz / 1e6:.4f} MHz) -- exceeds {max_rate / 1e6:.1f} "
            "MHz max; narrow the plan or use fewer channels")
    center_hz = (hi + lo) // 2
    floor_hz = max(span + 2 * guard_hz, audio_rate)
    iq_rate = -(-int(floor_hz) // audio_rate) * audio_rate  # ceil to multiple of audio_rate
    return int(center_hz), int(iq_rate)


def parse_channelmap_hz(path):
    """[{'lcn': int, 'freq_hz': int}, ...] from the LCN,FREQ_HZ CSV app.py's
    render_channelmap() writes for DSD-FME's -C flag -- multi mode reuses
    that exact file/format (dsd_pty already reads DSD_CHANNELMAP for -C in
    build_command; this just needs it structured for orchestration)."""
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


def build_multi_rsp_tcp_command(env, center_hz, iq_rate):
    """rsp_tcp tuned to the wideband centre (not DSD_CONTROL_FREQ -- that key
    is unused in multi mode, see app.py's render_dmr_env multi branch)."""
    host, port = _split_endpoint(env.get("DSD_RTLTCP", RSP_TCP_HOST), 1234)
    return [
        os.environ.get("RSP_TCP_BIN", "rsp_tcp"),
        "-a", host,
        "-p", port,
        "-s", str(int(iq_rate)),
        "-f", str(int(center_hz)),
    ]


def build_multi_bridge_command(env, center_hz, iq_rate):
    channel_map = env.get("DSD_CHANNELMAP")
    if not channel_map:
        raise ValueError("DSD_CHANNELMAP is required for multi mode")
    return [
        sys.executable,
        "-u",
        os.environ.get("RSP_FM_BIN", RSP_FM_BIN),
        "--rtl", env.get("DSD_RTLTCP", RSP_TCP_HOST),
        "--audio-tcp-base", env.get("DSD_AUDIO_TCP_BASE", AUDIO_TCP_BASE_HOST),
        "--rigctl", env.get("DSD_RIGCTL", RIGCTL_HOST),
        "--control-socket", env.get("DSD_BRIDGE_CTRL_SOCK", BRIDGE_CTRL_SOCK),
        "--multi-channelmap", str(channel_map),
        "--frequency", str(int(center_hz)),
        "--iq-rate", str(int(iq_rate)),
        "--audio-gain", str(env.get("DSD_AUDIO_GAIN", "4.0")),
    ]


def build_channel_dsd_command(env, lcn, audio_port, wav_root=None):
    """One dsd-fme instance for one physical channel: fixed-frequency, no
    -T/-U (no per-channel retuning -- Cap+ TDMA carries both logical slots
    on one physical frequency, and there is only one shared LO; see
    compute_wideband_plan). -7 before -P is required by DSD-FME's argv
    parser (same order as the single-channel build_command)."""
    audio_host, _ = _split_endpoint(env.get("DSD_AUDIO_TCP_BASE", AUDIO_TCP_BASE_HOST), 7355)
    command = [DSD_BIN, "-i", f"tcp:{audio_host}:{int(audio_port)}", "-o", "null", "-fs"]
    if wav_root:
        command += ["-7", str(Path(wav_root) / f"lcn{lcn}"), "-P"]
    return command


def tag_event(event, lcn, freq_hz):
    """Stamp a parse_dsd_line() result with ground-truth channel identity.
    Pulled out as a pure helper (rather than inlined in _run_multi, which is
    pragma: no cover hardware runtime) so this specific step -- the one that
    fixes the "which physical channel did this line come from" problem -- is
    unit-testable without pty/subprocess machinery. Only ever called on a
    non-None parse_dsd_line() result. phys_lcn/phys_freq_hz are additive:
    single-channel dmr/scan events never carry them, so
    _normalize_dsd/_dmr_listener treat their absence as today's behavior."""
    event["phys_lcn"] = int(lcn)
    event["phys_freq_hz"] = int(freq_hz)
    return event


def build_command(env):
    """Build a DSD-FME argv using supported PCM TCP input and rigctl tuning."""
    audio_host, audio_port = _split_endpoint(env.get("DSD_AUDIO_TCP", AUDIO_TCP_HOST), 7355)
    _rig_host, rig_port = _split_endpoint(env.get("DSD_RIGCTL", RIGCTL_HOST), 4532)
    command = [DSD_BIN, "-i", f"tcp:{audio_host}:{audio_port}", "-o", "null", "-fs"]

    control = env.get("DSD_CONTROL_FREQ")
    trunking = env.get("DSD_TRUNK", "").lower() in ("1", "true", "yes")
    channel_map = env.get("DSD_CHANNELMAP")
    if trunking:
        if not control:
            raise ValueError("DSD_CONTROL_FREQ is required for trunking")
        if not channel_map:
            raise ValueError("DSD_CHANNELMAP is required for trunking")
        command += ["-T", "-C", str(channel_map), "-U", str(rig_port)]

    wav_dir = env.get("DSD_WAV_DIR")
    if wav_dir:
        command += ["-7", str(wav_dir), "-P"]
    return command


GAIN_UP_KEY, GAIN_DOWN_KEY = b"G", b"g"


def send_gain_nudge(direction, sock_path=None):
    key = GAIN_UP_KEY if direction == "up" else GAIN_DOWN_KEY
    path = sock_path or CTRL_SOCK_PATH
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(key, path)
        return True
    except OSError:
        return False


def _send_bridge_control(keys: bytes, path: str) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(keys, path)
    except OSError as exc:
        sys.stderr.write(f"dsd_pty: bridge control failed: {exc}\n")
        sys.stderr.flush()


# --- Runtime ---------------------------------------------------------------
def _udp_target():
    host, port = _split_endpoint(os.environ.get("DSD_UDP", DEFAULT_UDP), 5555)
    return host, int(port)


def _wait_for_port(host: str, port: int, process, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _terminate(process) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def _pdeathsig_term():  # pragma: no cover - Linux-only, exercised post-fork
    """preexec_fn for the three supervised children: ask the kernel to send
    them SIGTERM the instant this process dies for *any* reason (including an
    OOM-kill that targets only the supervisor, which `finally`-block cleanup
    can't run for). Without this, a child can outlive dsd_pty and keep
    holding the SDR/ports, making the next `systemctl restart` fail the same
    way the original crash did. Linux-only; failure here is non-fatal."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _run():  # pragma: no cover - hardware runtime
    import pty
    import subprocess

    env = dict(os.environ)
    if env.get("DSD_MULTI", "").lower() in ("1", "true", "yes"):
        return _run_multi(env)

    target = _udp_target()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sweep = env.get("DSD_SWEEP", "").lower() in ("1", "true", "yes")
    emit_status = env.get("DSD_EMIT_STATUS", "").lower() in ("1", "true", "yes")
    processes = []
    master = None
    ctrl = None
    dsd = None

    try:
        rsp_command = build_rsp_tcp_command(env)
        sys.stderr.write("dsd_pty: exec (IQ server) %s\n" % " ".join(rsp_command))
        sys.stderr.flush()
        rsp = subprocess.Popen(rsp_command, preexec_fn=_pdeathsig_term)
        processes.append(rsp)

        bridge_command = build_bridge_command(env)
        sys.stderr.write("dsd_pty: exec (FM bridge) %s\n" % " ".join(bridge_command))
        sys.stderr.flush()
        bridge = subprocess.Popen(bridge_command, preexec_fn=_pdeathsig_term)
        processes.append(bridge)

        audio_host, audio_port = _split_endpoint(env.get("DSD_AUDIO_TCP", AUDIO_TCP_HOST), 7355)
        rig_host, rig_port = _split_endpoint(env.get("DSD_RIGCTL", RIGCTL_HOST), 4532)
        if not _wait_for_port(rig_host, int(rig_port), bridge):
            raise RuntimeError("rsp_fm rigctl port did not become ready")
        if sweep:
            # Discovery sweep: no DSD-FME. rsp_fm only serves the FFT spectrum +
            # retune over rigctl; app.py drives the frequency grid. Just keep the
            # two children alive until stopped (systemctl stop/restart) or a
            # child dies.
            sys.stderr.write("dsd_pty: sweep mode (rsp_tcp + rsp_fm only)\n")
            sys.stderr.flush()
            while True:
                if rsp.poll() is not None:
                    sys.stderr.write(f"dsd_pty: rsp_tcp exited with status {rsp.returncode}\n")
                    return 1
                if bridge.poll() is not None:
                    sys.stderr.write(f"dsd_pty: rsp_fm exited with status {bridge.returncode}\n")
                    return 1
                time.sleep(0.5)
        if not _wait_for_port(audio_host, int(audio_port), bridge):
            raise RuntimeError("rsp_fm audio port did not become ready")

        command = build_command(env)
        sys.stderr.write("dsd_pty: exec %s\n" % " ".join(command))
        sys.stderr.flush()
        master, slave = pty.openpty()
        dsd = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                               close_fds=True, preexec_fn=_pdeathsig_term)
        os.close(slave)
        processes.insert(0, dsd)

        try:
            if os.path.exists(CTRL_SOCK_PATH):
                os.unlink(CTRL_SOCK_PATH)
            os.makedirs(os.path.dirname(CTRL_SOCK_PATH), exist_ok=True)
            ctrl = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            ctrl.bind(CTRL_SOCK_PATH)
            ctrl.setblocking(False)
        except OSError as exc:
            sys.stderr.write(f"dsd_pty: control socket unavailable: {exc}\n")
            sys.stderr.flush()
            ctrl = None

        buffer = b""
        forced_failure = False
        while dsd.poll() is None:
            if rsp.poll() is not None:
                sys.stderr.write(f"dsd_pty: rsp_tcp exited with status {rsp.returncode}\n")
                forced_failure = True
                break
            if bridge.poll() is not None:
                sys.stderr.write(f"dsd_pty: rsp_fm exited with status {bridge.returncode}\n")
                forced_failure = True
                break

            readers = [master] + ([ctrl] if ctrl else [])
            ready, _, _ = select.select(readers, [], [], 1.0)
            if ctrl in ready:
                try:
                    keys, _ = ctrl.recvfrom(64)
                    _send_bridge_control(keys, env.get("DSD_BRIDGE_CTRL_SOCK", BRIDGE_CTRL_SOCK))
                except OSError:
                    pass
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    text = clean_dsd_line(raw_line.decode("utf-8", "replace"))
                    if text:
                        sys.stderr.write(f"dsd-fme: {text}\n")
                        sys.stderr.flush()
                    event = parse_dsd_line(text, emit_status=emit_status)
                    if event:
                        event["t"] = time.time()
                        try:
                            udp.sendto(json.dumps(event).encode("utf-8"), target)
                        except OSError:
                            pass

        if forced_failure:
            _terminate(dsd)
            return 1
        dsd.wait(timeout=3)
        sys.stderr.write(f"dsd_pty: dsd-fme exited with status {dsd.returncode}\n")
        sys.stderr.flush()
        return int(dsd.returncode or 0)
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"dsd_pty: fatal: {exc}\n")
        sys.stderr.flush()
        return 1
    finally:
        for process in processes:
            _terminate(process)
        if ctrl is not None:
            ctrl.close()
        try:
            os.unlink(CTRL_SOCK_PATH)
        except FileNotFoundError:
            pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        udp.close()


def _run_multi(env):  # pragma: no cover - hardware runtime
    """N-decoder counterpart of _run(): one wideband rsp_tcp + one rsp_fm.py
    bridge (N offset-aware demodulators, N audio ports) + N dsd-fme instances,
    one per physical channel in DSD_CHANNELMAP. Every parsed event is tagged
    with (phys_lcn, phys_freq_hz) via tag_event() before going out on UDP --
    ground truth app.py needs to disambiguate N simultaneous channels (see
    app.py's _normalize_dsd/_dmr_listener phys_lcn handling). Any single
    dsd-fme instance dying takes the whole service down (matches
    single-channel _run()'s Restart=always semantics -- partial per-channel
    respawn is a deliberate day-one simplification, not yet implemented)."""
    import pty
    import subprocess

    channel_map_path = env.get("DSD_CHANNELMAP")
    if not channel_map_path:
        sys.stderr.write("dsd_pty: DSD_MULTI=1 but DSD_CHANNELMAP is not set\n")
        return 1
    channels = parse_channelmap_hz(channel_map_path)
    if not channels:
        sys.stderr.write(f"dsd_pty: DSD_CHANNELMAP {channel_map_path!r} has no channels\n")
        return 1
    try:
        center_hz, iq_rate = compute_wideband_plan(
            [c["freq_hz"] for c in channels],
            guard_hz=int(env.get("DSD_MULTI_GUARD_HZ", "25000")),
            max_rate=int(env.get("DSD_MULTI_MAX_RATE_HZ", "2000000")))
    except ValueError as exc:
        sys.stderr.write(f"dsd_pty: multi channel plan invalid: {exc}\n")
        return 1

    target = _udp_target()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    emit_status = env.get("DSD_EMIT_STATUS", "").lower() in ("1", "true", "yes")
    wav_dir = env.get("DSD_WAV_DIR")
    processes = []
    dsd_procs = {}   # lcn -> {"proc":, "master":, "buffer": bytes, "freq_hz": int}

    try:
        rsp_command = build_multi_rsp_tcp_command(env, center_hz, iq_rate)
        sys.stderr.write("dsd_pty: exec (IQ server, multi) %s\n" % " ".join(rsp_command))
        sys.stderr.flush()
        rsp = subprocess.Popen(rsp_command, preexec_fn=_pdeathsig_term)
        processes.append(rsp)

        bridge_command = build_multi_bridge_command(env, center_hz, iq_rate)
        sys.stderr.write("dsd_pty: exec (FM bridge, multi) %s\n" % " ".join(bridge_command))
        sys.stderr.flush()
        bridge = subprocess.Popen(bridge_command, preexec_fn=_pdeathsig_term)
        processes.append(bridge)

        rig_host, rig_port = _split_endpoint(env.get("DSD_RIGCTL", RIGCTL_HOST), 4532)
        if not _wait_for_port(rig_host, int(rig_port), bridge):
            raise RuntimeError("rsp_fm rigctl port did not become ready")

        audio_host, base_port = _split_endpoint(
            env.get("DSD_AUDIO_TCP_BASE", AUDIO_TCP_BASE_HOST), 7355)
        for i, ch in enumerate(channels):
            port = int(base_port) + i
            if not _wait_for_port(audio_host, port, bridge):
                raise RuntimeError(f"rsp_fm audio port for lcn={ch['lcn']} did not become ready")
            command = build_channel_dsd_command(env, ch["lcn"], port, wav_dir)
            sys.stderr.write("dsd_pty: exec (lcn=%s) %s\n" % (ch["lcn"], " ".join(command)))
            sys.stderr.flush()
            master, slave = pty.openpty()
            proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                    close_fds=True, preexec_fn=_pdeathsig_term)
            os.close(slave)
            processes.insert(0, proc)
            dsd_procs[ch["lcn"]] = {"proc": proc, "master": master, "buffer": b"",
                                    "freq_hz": ch["freq_hz"]}

        while True:
            if rsp.poll() is not None:
                sys.stderr.write(f"dsd_pty: rsp_tcp exited with status {rsp.returncode}\n")
                break
            if bridge.poll() is not None:
                sys.stderr.write(f"dsd_pty: rsp_fm exited with status {bridge.returncode}\n")
                break
            dead = [lcn for lcn, c in dsd_procs.items() if c["proc"].poll() is not None]
            if dead:
                sys.stderr.write(f"dsd_pty: dsd-fme for lcn={dead} exited\n")
                break
            readers = [c["master"] for c in dsd_procs.values()]
            ready, _, _ = select.select(readers, [], [], 1.0)
            for lcn, c in dsd_procs.items():
                if c["master"] not in ready:
                    continue
                try:
                    chunk = os.read(c["master"], 4096)
                except OSError:
                    continue
                if not chunk:
                    continue
                c["buffer"] += chunk
                while b"\n" in c["buffer"]:
                    raw_line, c["buffer"] = c["buffer"].split(b"\n", 1)
                    text = clean_dsd_line(raw_line.decode("utf-8", "replace"))
                    if text:
                        sys.stderr.write(f"dsd-fme[lcn={lcn}]: {text}\n")
                        sys.stderr.flush()
                    event = parse_dsd_line(text, emit_status=emit_status)
                    if event:
                        event["t"] = time.time()
                        tag_event(event, lcn, c["freq_hz"])
                        try:
                            udp.sendto(json.dumps(event).encode("utf-8"), target)
                        except OSError:
                            pass
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"dsd_pty: multi fatal: {exc}\n")
        sys.stderr.flush()
        return 1
    finally:
        for process in processes:
            _terminate(process)
        for c in dsd_procs.values():
            try:
                os.close(c["master"])
            except OSError:
                pass
        udp.close()


def _selftest():
    samples = [
        "SLOT 1 TGT=3 SRC=2120 Cap+ Group Call  Rest LSN: 5",
        "Slot 1 Data Header - Indiv - Confirmed Delivery - Response Requested - Source: 191 Target: 64250",
        "LRRP SRC: 199; Response to TGT: 64250;",
        "Lat: 32.09265 Lon: 34.86761 (32.09265, 34.86761)",
        "SLOT 1 Protected LC  FLCO=0x0C FID=0x00",
        "21:39:14 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK (CRC ERR)",
    ]
    for sample in samples:
        print(f"{sample!r}\n   -> {parse_dsd_line(sample)}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(_run())
