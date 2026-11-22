#!/usr/bin/env python3
"""Flash a GameSir G8+ Bluetooth controller over BLE using its OTA protocol.

Protocol: the GameSir app's OTA protocol for this controller (simple, no authentication). Transport: our own ATT client on a raw L2CAP socket (fixed CID 4),
because BlueZ's gatt-client rejects the firmware's Service Changed characteristic and then exports no GATT
objects (bleak sees nothing). Needs Python >= 3.14 (LE address tuples for L2CAP sockets), no sudo, no bleak.
Unpair the controller from the laptop first, or bluetoothd may hold the ATT channel (connect -> EBUSY).
"""

import argparse
import re
import subprocess
import math
import os
import select
import shutil
import socket
import sys
import time
import zipfile
from pathlib import Path

SERVICE_UUID16 = 0xFF10
CONTROL_UUID16 = 0xFF11
DATA_UUID16 = 0xFF12
CCC_UUID16 = 0x2902
BASE_UUID_LE = bytes.fromhex("fb349b5f8000008000100000")  # Bluetooth base UUID, first 12 bytes little-endian
RESPONSE_TIMEOUT = 3
CONNECT_TIMEOUT = 10
MAX_SAME_BLOCK = 5
WRITE_SLICE = 20
ATT_CID = 4
ATT_MTU = 23
def _load_known() -> dict[str, str]:
    """Optional names for your own controllers: known_controllers.json next to this script, e.g.
    {"A0:5A:59:12:34:56": "spare"}. A controller named "spare" or "test" is treated as your test unit (the TUI warns
    before flashing any other one). The file is personal and is not published."""
    import json
    try:
        data = json.loads((Path(__file__).resolve().parent / "known_controllers.json").read_text())
        return {str(k).upper(): str(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


KNOWN = _load_known()  # green (BLE) mode addresses -> your own names
DEVICE_NAME = "GameSir-G8+"  # exact; "GameSir-G8+_G" is the G-Touch/recovery mode
SCAN_SECONDS = 6
RECOVERY_HINT = "Recovery is unknown; testing found the phone app can update a bad build via G-Touch = screenshot + Start for ~1 s."

# ATT opcodes
ERROR_RSP, MTU_REQ, MTU_RSP = 0x01, 0x02, 0x03
FIND_INFO_REQ, FIND_INFO_RSP = 0x04, 0x05
READ_BY_TYPE_REQ, READ_BY_TYPE_RSP = 0x08, 0x09
READ_REQ, READ_RSP = 0x0A, 0x0B
READ_BY_GROUP_REQ, READ_BY_GROUP_RSP = 0x10, 0x11
WRITE_REQ, WRITE_RSP = 0x12, 0x13
NOTIFY, INDICATE, CONFIRM = 0x1B, 0x1D, 0x1E
WRITE_CMD = 0x52
ERR_ATTR_NOT_FOUND, ERR_REQ_NOT_SUPPORTED = 0x0A, 0x06
# Requests a GATT server (the controller) might send us; we must answer each one.
SERVER_REQUESTS = {0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12, 0x16, 0x18, 0x20}


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM: poly 0x1021, init 0, MSB first, no final xor."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(cmd: int, payload: bytes = b"") -> bytes:
    if not 0 <= cmd <= 0xFF or len(payload) > 252:
        raise ValueError("command or payload is outside the frame format")
    return bytes((len(payload) + 3, cmd, 0)) + payload


def start_payload(data: bytes) -> bytes:
    if len(data) > 0xFFFFFF:
        raise ValueError("firmware exceeds the 24-bit size limit")
    checksum = crc16_xmodem(data)
    return checksum.to_bytes(2, "big") + len(data).to_bytes(3, "big")


def parse_notification(data: bytes) -> dict:
    """Parse one protocol response notification (commands 0x50..0x53)."""
    if len(data) < 3:
        raise ValueError("response is shorter than its command header")
    cmd = data[1]
    if cmd == 0x50 or cmd == 0x53:
        return {"cmd": cmd, "status": data[2]}
    if cmd == 0x51:
        if len(data) < 5:
            raise ValueError("0x51 response is too short for block size")
        return {"cmd": cmd, "status": data[2], "block_size": int.from_bytes(data[3:5], "big")}
    if cmd == 0x52:
        if len(data) < 8:
            raise ValueError("0x52 response is too short for next offset")
        return {"cmd": cmd, "status": data[3], "next_offset": int.from_bytes(data[4:8], "big")}
    raise ValueError(f"unexpected response command 0x{cmd:02x}")


def block_index(next_offset: int, block_size: int) -> int:
    if next_offset < 0 or block_size <= 0:
        raise ValueError("invalid next offset or block size")
    return (next_offset + block_size - 1) // block_size


def load_firmware(path: str) -> bytes:
    source = Path(path)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            try:
                info = archive.getinfo("ota.bin")
            except KeyError as exc:
                raise ValueError("zip does not contain ota.bin") from exc
            if info.file_size > 0xFFFFFF:
                raise ValueError("ota.bin exceeds 0xFFFFFF bytes")
            data = archive.read(info)
    else:
        if source.suffix.lower() != ".bin":
            raise ValueError("input must be a zip containing ota.bin or a .bin file")
        if source.stat().st_size > 0xFFFFFF:
            raise ValueError("firmware exceeds 0xFFFFFF bytes")
        data = source.read_bytes()
    if not data:
        raise ValueError("firmware file is empty")
    if len(data) > 0xFFFFFF:
        raise ValueError("firmware exceeds 0xFFFFFF bytes")
    return data


def uuid16(raw: bytes) -> int | None:
    """16-bit UUID from a little-endian 2- or 16-byte ATT UUID (None if not a Bluetooth-base UUID)."""
    if len(raw) == 2:
        return int.from_bytes(raw, "little")
    if len(raw) == 16 and raw[:12] == BASE_UUID_LE and raw[14:] == b"\0\0":
        return int.from_bytes(raw[12:14], "little")
    return None


def uuid_str(raw: bytes) -> str:
    short = uuid16(raw)
    return f"{short:04x}" if short is not None else raw[::-1].hex()


class Progress:
    """One-line progress bar (TTY) or a line per 10 % (pipe). The spinner moves on every controller ack, and a
    warning appears when no ack has arrived for a while, so a hang is visible before the 3 s timeout fires."""

    SPIN = "|/-\\"

    def __init__(self, total: int):
        self.total, self.done, self.acks = total, 0, 0
        self.started = self.last_ack = time.monotonic()
        self.tty = sys.stdout.isatty()
        self.step = max(1, int(os.environ.get("G8OTA_PROGRESS_STEP", "10")))  # pipe mode: a line every N %
        self.next_pct = self.step
        self.last_draw = 0.0

    def ack(self, done: int) -> None:
        self.done, self.acks, self.last_ack = min(done, self.total), self.acks + 1, time.monotonic()
        self.draw(force=True)
        if not self.tty and self.done * 100 >= self.next_pct * self.total:
            print(f"{self.done * 100 // self.total:3d}%  {self.done}/{self.total} bytes  "
                  f"{time.monotonic() - self.started:.0f}s", flush=True)
            self.next_pct = self.done * 100 // self.total // self.step * self.step + self.step

    def draw(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self.tty or (not force and now - self.last_draw < 0.2):
            return
        self.last_draw = now
        frac = self.done / self.total
        width = 30
        bar = "#" * int(frac * width) + "-" * (width - int(frac * width))
        elapsed = now - self.started
        rate = self.done / elapsed if elapsed > 0 else 0
        eta = f"{int((self.total - self.done) / rate) // 60}:{int((self.total - self.done) / rate) % 60:02d}" if rate else "?:??"
        quiet = now - self.last_ack
        state = f"waiting for controller {quiet:.1f}s" if quiet > 1.0 else f"{self.SPIN[self.acks % 4]} ok"
        line = (f"[{bar}] {frac * 100:5.1f}%  {self.done // 1024}/{self.total // 1024} KiB  "
                f"{rate / 1024:.1f} KiB/s  ETA {eta}  {state}")
        width = max(shutil.get_terminal_size((100, 24)).columns - 1, 20)  # never wider than the window, or it wraps
        print(f"\r{line[:width]:<{width}}", end="", flush=True)

    def finish(self) -> None:
        if self.tty:
            print()


class AttError(RuntimeError):
    def __init__(self, req: int, handle: int, code: int):
        super().__init__(f"ATT error 0x{code:02x} for request 0x{req:02x} handle 0x{handle:04x}")
        self.code = code


class Disconnected(RuntimeError):
    pass


class Att:
    """Minimal ATT client: one outstanding request at a time, notifications queued."""

    def __init__(self, address: str, random_addr: bool, verbose: bool):
        self.verbose = verbose
        self.notifications: list[tuple[int, bytes]] = []
        addr_type = socket.BDADDR_LE_RANDOM if random_addr else socket.BDADDR_LE_PUBLIC
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        try:
            self.sock.bind((socket.BDADDR_ANY, 0, ATT_CID, socket.BDADDR_LE_PUBLIC))
            self.sock.settimeout(CONNECT_TIMEOUT)
            self.sock.connect((address, 0, ATT_CID, addr_type))
            self.sock.settimeout(None)
        except BaseException:
            self.sock.close()
            raise

    def close(self) -> None:
        self.sock.close()

    def _log(self, direction: str, data: bytes) -> None:
        if self.verbose:
            print(f"{direction} {data.hex(' ')}", flush=True)

    def _send(self, pdu: bytes) -> None:
        self._log("ATT>", pdu)
        self.sock.send(pdu)

    def _recv(self, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self.sock], [], [], max(timeout, 0))
        if not ready:
            return None
        try:
            pdu = self.sock.recv(512)
        except OSError as exc:
            raise Disconnected(f"link lost: {exc}") from exc
        if not pdu:
            raise Disconnected("controller closed the link")
        self._log("ATT<", pdu)
        return pdu

    def _handle_unsolicited(self, pdu: bytes) -> bool:
        """Deal with server-initiated PDUs. Returns True if consumed."""
        op = pdu[0]
        if op in (NOTIFY, INDICATE):
            self.notifications.append((int.from_bytes(pdu[1:3], "little"), pdu[3:]))
            if op == INDICATE:
                self._send(bytes([CONFIRM]))
            return True
        if op == MTU_REQ:
            self._send(bytes([MTU_RSP]) + ATT_MTU.to_bytes(2, "little"))
            return True
        if op in SERVER_REQUESTS:
            self._send(bytes([ERROR_RSP, op, 0, 0, ERR_REQ_NOT_SUPPORTED]))
            return True
        if op & 0x40:  # commands need no answer
            return True
        return False

    def pump(self, timeout: float) -> None:
        """Process incoming PDUs for up to `timeout` seconds (returns early after the first one)."""
        pdu = self._recv(timeout)
        if pdu is not None and not self._handle_unsolicited(pdu):
            self._log("ATT? unexpected", pdu)

    def request(self, pdu: bytes, rsp_op: int) -> bytes:
        self._send(pdu)
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while True:
            got = self._recv(deadline - time.monotonic())
            if got is None:
                raise RuntimeError(f"ATT timeout waiting for opcode 0x{rsp_op:02x}")
            if self._handle_unsolicited(got):
                continue
            if got[0] == ERROR_RSP and len(got) >= 5:
                raise AttError(got[1], int.from_bytes(got[2:4], "little"), got[4])
            if got[0] == rsp_op:
                return got[1:]
            self._log("ATT? unexpected", got)

    def write_cmd(self, handle: int, value: bytes) -> None:
        self._send(bytes([WRITE_CMD]) + handle.to_bytes(2, "little") + value)

    def write_req(self, handle: int, value: bytes) -> None:
        self.request(bytes([WRITE_REQ]) + handle.to_bytes(2, "little") + value, WRITE_RSP)

    def read(self, handle: int) -> bytes:
        return self.request(bytes([READ_REQ]) + handle.to_bytes(2, "little"), READ_RSP)

    def _iterate(self, op: int, rsp_op: int, start: int, end: int, extra: bytes, parse):
        """Run a discovery procedure that continues after the last returned handle."""
        out = []
        while start <= end:
            try:
                body = self.request(bytes([op]) + start.to_bytes(2, "little") + end.to_bytes(2, "little") + extra, rsp_op)
            except AttError as exc:
                if exc.code == ERR_ATTR_NOT_FOUND:
                    break
                raise
            items, last = parse(body)
            if not items:
                break
            out.extend(items)
            if last >= end or last == 0xFFFF:
                break
            start = last + 1
        return out

    def services(self) -> list[tuple[int, int, bytes]]:
        def parse(body):
            size, items = body[0], []
            for i in range(1, len(body) - size + 1, size):
                e = body[i:i + size]
                items.append((int.from_bytes(e[0:2], "little"), int.from_bytes(e[2:4], "little"), e[4:]))
            return items, items[-1][1] if items else 0xFFFF
        return self._iterate(READ_BY_GROUP_REQ, READ_BY_GROUP_RSP, 1, 0xFFFF, (0x2800).to_bytes(2, "little"), parse)

    def characteristics(self, start: int, end: int) -> list[tuple[int, int, int, bytes]]:
        """(declaration handle, properties, value handle, uuid)."""
        def parse(body):
            size, items = body[0], []
            for i in range(1, len(body) - size + 1, size):
                e = body[i:i + size]
                items.append((int.from_bytes(e[0:2], "little"), e[2], int.from_bytes(e[3:5], "little"), e[5:]))
            return items, items[-1][0] if items else 0xFFFF
        return self._iterate(READ_BY_TYPE_REQ, READ_BY_TYPE_RSP, start, end, (0x2803).to_bytes(2, "little"), parse)

    def descriptors(self, start: int, end: int) -> list[tuple[int, bytes]]:
        def parse(body):
            size = 4 if body[0] == 1 else 18
            items = [(int.from_bytes(body[i:i + 2], "little"), body[i + 2:i + size])
                     for i in range(1, len(body) - size + 1, size)]
            return items, items[-1][0] if items else 0xFFFF
        return self._iterate(FIND_INFO_REQ, FIND_INFO_RSP, start, end, b"", parse)


PROPS = ["broadcast", "read", "write-without-response", "write", "notify", "indicate", "signed-write", "extended"]


def prop_names(props: int) -> str:
    return ", ".join(name for bit, name in enumerate(PROPS) if props & (1 << bit))


def discover_ota(att: Att, show: bool) -> dict:
    """Find FF11 value + CCC handles and FF12 value handle. Optionally print the whole GATT table."""
    found = {}
    for start, end, suuid in att.services():
        if show:
            print(f"Service {uuid_str(suuid)}  0x{start:04x}-0x{end:04x}")
        if not show and uuid16(suuid) != SERVICE_UUID16:
            continue
        chars = att.characteristics(start, end)
        for i, (decl, props, value, cuuid) in enumerate(chars):
            if show:
                print(f"  Char {uuid_str(cuuid)}  value 0x{value:04x}  props 0x{props:02x} ({prop_names(props)})")
            if uuid16(suuid) != SERVICE_UUID16:
                continue
            char_end = chars[i + 1][0] - 1 if i + 1 < len(chars) else end
            if uuid16(cuuid) == CONTROL_UUID16:
                found["control"], found["control_props"] = value, props
                for handle, duuid in att.descriptors(value + 1, char_end) if value < char_end else []:
                    if uuid16(duuid) == CCC_UUID16:
                        found["ccc"] = handle
            elif uuid16(cuuid) == DATA_UUID16:
                found["data"], found["data_props"] = value, props
    return found


def check_layout(found: dict) -> str | None:
    if "control" not in found or "data" not in found:
        return "FF10 service with FF11 and FF12 not found"
    if not found["control_props"] & 0x10 or "ccc" not in found:
        return "FF11 has no notify property or no CCC descriptor"
    return None


def scan(seconds: int = SCAN_SECONDS) -> dict[str, str]:
    """LE scan through bluetoothctl (no root). Returns {address: name} of every device seen."""
    try:
        out = subprocess.run(["bluetoothctl", "--timeout", str(seconds), "scan", "le"], capture_output=True,
                             text=True, timeout=seconds + 10).stdout
    except FileNotFoundError as exc:
        raise RuntimeError("bluetoothctl not found (package bluez-utils)") from exc
    out = re.sub(r"\x1b\[[0-9;]*m|\x01|\x02", "", out)
    seen: dict[str, str] = {}
    for line in out.splitlines():
        m = re.search(r"\[(NEW|CHG)\] Device ([0-9A-F:]{17})(?: (.*))?$", line)
        if not m:
            continue
        addr, rest = m.group(2), (m.group(3) or "").strip()
        if m.group(1) == "NEW":
            seen.setdefault(addr, rest)
        elif rest.startswith("Name: "):
            seen[addr] = rest[6:]
        else:
            seen.setdefault(addr, "")
    # A device bluetoothd already knows shows up only as "[CHG] ... RSSI", without its name: ask for it.
    for addr, name in seen.items():
        if not name or name.replace("-", ":") == addr:
            info = subprocess.run(["bluetoothctl", "info", addr], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"^\s*Name: (.*)$", info, re.M)
            seen[addr] = m.group(1).strip() if m else ""
    return seen


def label(addr: str) -> str:
    return f"{addr} ({KNOWN[addr]})" if addr in KNOWN else addr


def find_controller() -> str:
    print(f"Scanning {SCAN_SECONDS} s for {DEVICE_NAME!r} (controller on, green mode, not connected to the phone) ...",
          flush=True)
    found = [a for a, n in scan().items() if n == DEVICE_NAME]
    if not found:
        raise RuntimeError(f"no {DEVICE_NAME!r} found. Is it on in green (Android) mode and not connected to the phone?")
    if len(found) > 1:
        raise RuntimeError("several controllers found, pick one with --address: " + ", ".join(map(label, found)))
    print(f"Found {label(found[0])}")
    return found[0]


def _scan(args) -> int:
    try:
        seen = scan(args.seconds)
    except Exception as exc:
        print(f"Scan failed: {exc}", file=sys.stderr)
        return 1
    hits = {a: n for a, n in seen.items() if n.startswith("GameSir")}
    for addr, name in sorted(hits.items()):
        note = "" if name == DEVICE_NAME else "  (not the OTA mode; use green/Android mode)"
        print(f"{label(addr)}  {name}{note}")
    if not hits:
        print("No GameSir controllers found.")
    return 0 if hits else 1


def _connect(args) -> Att:
    if not args.address:
        args.address = find_controller()
    print(f"Connecting to {args.address} ...", flush=True)
    try:
        try:
            return Att(args.address, args.random, args.verbose)
        except OSError as exc:
            # Seen once right after a previous session closed: ENOSYS. A second try worked.
            if exc.errno == 16:
                raise
            print(f"connect failed ({exc}), retrying in 2 s", flush=True)
            time.sleep(2)
            return Att(args.address, args.random, args.verbose)
    except OSError as exc:
        hint = ""
        if getattr(exc, "errno", None) == 16:
            hint = " (EBUSY: bluetoothd holds the link; remove the pairing with bluetoothctl first)"
        raise RuntimeError(f"connect failed: {exc}{hint}") from exc


def read_or_error(att: Att, handle: int) -> str:
    """The G8+ firmware answers a CCC read with 'attribute not found' (0x0a) although writes work."""
    try:
        return att.read(handle).hex()
    except AttError as exc:
        return f"unreadable: {exc}"


def _info(args) -> int:
    att = None
    try:
        att = _connect(args)
        found = discover_ota(att, show=True)
        problem = check_layout(found)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print(f"OTA: FF11 value 0x{found['control']:04x} CCC 0x{found['ccc']:04x} ({read_or_error(att, found['ccc'])}), "
              f"FF12 value 0x{found['data']:04x}")
        if args.notify:
            att.write_req(found["ccc"], b"\x01\x00")
            print(f"FF11 notifications enabled (CCC read: {read_or_error(att, found['ccc'])}); listening 3 s (no OTA commands sent)")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                att.pump(deadline - time.monotonic())
            for handle, value in att.notifications:
                print(f"  notification 0x{handle:04x}: {value.hex(' ')}")
        return 0
    except Exception as exc:
        print(f"Info failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if att is not None:
            att.close()


def read_long(att: Att, handle: int) -> bytes:
    """Read + Read Blob until a short part (the G8+ rejects a Read Blob at offset 0)."""
    value = att.read(handle)
    while value and len(value) % (ATT_MTU - 1) == 0:
        try:
            part = att.request(bytes([0x0C]) + handle.to_bytes(2, "little") + len(value).to_bytes(2, "little"), 0x0D)
        except AttError:
            break
        if not part:
            break
        value += part
    return value


def _marker(args) -> int:
    """Print the HID Report Map and whether it holds our build marker (the app always shows 2.20)."""
    att = None
    try:
        att = _connect(args)
        report_map = None
        for start, end, suuid in att.services():
            if uuid16(suuid) == 0x1812:
                for _decl, _props, value, cuuid in att.characteristics(start, end):
                    if uuid16(cuuid) == 0x2A4B:
                        report_map = read_long(att, value)
        if report_map is None:
            print("No HID Report Map found", file=sys.stderr)
            return 1
        print(f"Report Map: {len(report_map)} bytes")
        if args.verbose:
            print(report_map.hex(" "))
        has = bytes.fromhex(args.marker) in report_map
        print(f"Marker {args.marker}: {'PRESENT' if has else 'absent'}")
        return 0
    except Exception as exc:
        print(f"Marker check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if att is not None:
            att.close()


def _flash(data: bytes, args) -> int:
    att = None
    started = time.monotonic()
    progress = None
    try:
        att = _connect(args)
        found = discover_ota(att, show=False)
        problem = check_layout(found)
        if problem:
            raise RuntimeError(problem)
        control, data_handle = found["control"], found["data"]
        att.write_req(found["ccc"], b"\x01\x00")
        # FF11/FF12 are write-without-response on the G8+; use write requests only if that's all they offer.
        control_cmd = bool(found["control_props"] & 0x04)
        data_cmd = bool(found["data_props"] & 0x04)
        print(f"FF11 writes: {'without' if control_cmd else 'with'} response, "
              f"FF12 writes: {'without' if data_cmd else 'with'} response")

        def write(handle: int, value: bytes, as_cmd: bool) -> None:
            if as_cmd:
                att.write_cmd(handle, value)
            else:
                att.write_req(handle, value)

        def command(cmd: int, payload: bytes = b"") -> None:
            write(control, frame(cmd, payload), control_cmd)

        def response(expected: int) -> dict:
            # Like the app: ignore notifications for other commands until the deadline.
            deadline = time.monotonic() + RESPONSE_TIMEOUT
            while True:
                while att.notifications:
                    handle, raw = att.notifications.pop(0)
                    if handle == control and len(raw) >= 2 and raw[1] == expected:
                        parsed = parse_notification(raw)
                        if parsed["status"] != 0:
                            raise RuntimeError(f"controller returned status {parsed['status']} for 0x{expected:02x}")
                        return parsed
                    if args.verbose:
                        print(f"IGNORED 0x{handle:04x}: {raw.hex(' ')}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(f"timed out waiting for 0x{expected:02x} response")
                att.pump(min(left, 0.2))
                if progress is not None:
                    progress.draw()

        command(0x50, start_payload(data))
        response(0x50)
        command(0x51)
        block_size = response(0x51)["block_size"]
        if block_size == 0:
            raise RuntimeError("controller reported a zero block size")
        print(f"Block size {block_size}, {math.ceil(len(data) / block_size)} blocks")
        progress = Progress(len(data))
        blocks = math.ceil(len(data) / block_size)
        index, last_index, same = 0, -1, 0
        while index < blocks:
            same = same + 1 if index == last_index else 0
            if same > MAX_SAME_BLOCK:
                raise RuntimeError(f"controller asked for block {index} more than {MAX_SAME_BLOCK} times")
            last_index = index
            piece = data[index * block_size:(index + 1) * block_size]
            for offset in range(0, len(piece), WRITE_SLICE):
                write(data_handle, piece[offset:offset + WRITE_SLICE], data_cmd)
                if args.delay_ms:
                    time.sleep(args.delay_ms / 1000)
            ack = response(0x52)
            index = block_index(ack["next_offset"], block_size)
            progress.ack(ack["next_offset"])
        progress.finish()
        time.sleep(0.1)
        command(0x53)
        response(0x53)
        print(f"Done in {time.monotonic() - started:.1f}s. Controller should reboot into the new firmware.")
        return 0
    except Exception as exc:
        print(f"\nTransfer failed: {exc}. {RECOVERY_HINT}", file=sys.stderr)
        return 1
    finally:
        if att is not None:
            att.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flash a GameSir G8+ Bluetooth controller (BLE, green mode)")
    sub = parser.add_subparsers(dest="action", required=True)
    flash_parser = sub.add_parser("flash", help="send a firmware zip (with ota.bin) or a .bin")
    flash_parser.add_argument("file")
    flash_parser.add_argument("--yes", action="store_true")
    flash_parser.add_argument("--delay-ms", type=float, default=0, help="pause between 20-byte writes (app: 0)")
    info_parser = sub.add_parser("info", help="list GATT and the OTA handles; read-only unless --notify")
    info_parser.add_argument("--notify", action="store_true", help="also enable FF11 notifications and listen 3 s")
    marker_parser = sub.add_parser("marker", help="read the HID Report Map and look for a build marker (read-only)")
    marker_parser.add_argument("--marker", default="463a01", help="hex bytes to look for (default: 7.17 marker)")
    scan_parser = sub.add_parser("scan", help="list nearby GameSir controllers")
    scan_parser.add_argument("--seconds", type=int, default=SCAN_SECONDS)
    for p in (flash_parser, info_parser, marker_parser):
        p.add_argument("--address", help="BLE address (default: scan; works if exactly one controller is on)")
        p.add_argument("--random", action="store_true", help="address is a random (not public) LE address")
        p.add_argument("-v", "--verbose", action="store_true", help="print every ATT PDU")
    args = parser.parse_args(argv)
    if args.action == "scan":
        return _scan(args)
    if args.action == "info":
        return _info(args)
    if args.action == "marker":
        return _marker(args)
    try:
        firmware = load_firmware(args.file)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Invalid firmware: {exc}", file=sys.stderr)
        return 2
    print(f"File size: {len(firmware)} bytes")
    print(f"CRC-16/XMODEM: 0x{crc16_xmodem(firmware):04X}")
    if not args.address:
        try:
            args.address = find_controller()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"Address: {label(args.address)}")
    if not args.yes:
        try:
            answer = input("Start firmware transfer? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    return _flash(firmware, args)


if __name__ == "__main__":
    raise SystemExit(main())
