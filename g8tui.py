#!/usr/bin/env python3
"""GameSir G8+ mod tool (terminal UI). Arrow keys, Enter and Esc only.

Start of a general controller mod app: the top row picks the controller type (Left/Right, or Enter for a list); it
starts on the G8+ MFi if one is plugged in over USB, else on the G8+ Bluetooth. Per model: base firmware, mod list,
build, and whether this app can flash it. G8+ Bluetooth: build + flash over Bluetooth LE (below). G8+ MFi
(model_mfi.py): build-only, the user flashes the saved zip with the GameSir phone app; the USB status and installed
mods are read from sysfs (the descriptor the kernel already has), nothing is sent to the controller.

What it does, step by step and visibly:
  base firmware  official GameSir 2.20: a file next to this script or in its firmware/ folder, a file you pick,
                 or - only if you choose it - a download from GameSir's update server (saved to firmware/)
  check          the base must be byte-identical to official 2.20 (sha256), or no mods are applied
  apply mods     our edit files (mods/edits_*.txt, lines "offset:bytes") + a build marker
  build image    CRCs, encryption, self-checks (g8fw.py)
  flash          g8ota.py over Bluetooth LE (run as a subprocess, progress shown here)
  check          read the controller's Report Map marker and compare with what was flashed
We never ship GameSir's firmware: only this script and our edit files.

Build marker: Report Map byte at file 0x2C124 (hat physical max, stock `46 3b 01`). Our builds write
0x20 | mask for masks below 0x10 (0x21-0x2f), 0x40 | mask for masks with bit 0x10 (0x50-0x5f; 0x30-0x3f is avoided
because 0x3a = 7.17 (mask 7, older marker) and 0x3b = stock or an unmarked test build).

Run:       python3 g8tui.py
Version:   python3 g8tui.py --version
Self-test: python3 g8tui.py --selftest   (builds every mod combination and reads its marker back; no network)
"""

import base64
import curses
import hashlib
import io
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
import urllib.request
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CR = TOOLS.parent / "claude-re"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(CR))
import g8fw  # noqa: E402
import g8ota  # noqa: E402
import model_mfi  # noqa: E402

CACHE = Path.home() / ".cache/g8tui"     # built images (temporary)
FW_DIR = TOOLS / "firmware"               # where GameSir firmware files go (git-ignored)
BASE = "2.20"
STOCK_NAME = "g8-bluetooth-firmware-2.20.zip"
# GameSir's own update server (base64, so the address isn't plain text in the source; nothing secret about it).
FW_SERVER = base64.b64decode("aHR0cHM6Ly9vdGEueGlhb2ppLmNvbS9kb3dubG9hZC8=").decode()
STOCK_URL = FW_SERVER + STOCK_NAME
REPO_STOCK_ZIP = CR / "v7.17/g8-bluetooth-firmware-2.22.zip"  # our saved copy (= official 2.20); self-test only
# App version, Semantic Versioning (semver.org). 0.y.z = beta: anything may still change. Bump MINOR for new mods or
# features, PATCH for fixes; 1.0.0 = first release we call stable. Git tag each release as v<version>.
VERSION = "0.2.0"
AUTHOR = "jun182"
REPO = "jun182/controller-mod-tool"  # GitHub; releases are tagged v<VERSION>

MARKER_OFFSET = 0x2C124
MARKER_STOCK, MARKER_717, MARKER_BASE, MARKER_BASE2, MARKER_SHOT, MARKER_BACK, MARKER_SHARE = 0x3B, 0x3A, 0x20, 0x40, 0x60, 0x80, 0xA0
IMAGE_SIZE = 393908            # every G8+ BT image built on 2.20 (stock and ours) has exactly this size
HAT_ITEM = re.compile(rb"\x35\x00\x46(.)\x01\x65\x14", re.S)  # hat: phys min 0, phys max 0x01xx, unit (Report Map)

PADDLES, RATE58, RATE115, STICKRAW, STICKRAW2, SHOTKEY, SHOTBACK, SHOTSHARE = 1, 2, 4, 8, 16, 32, 64, 128
MODS_DIRS = [TOOLS / "mods", CR / "v7.17"]  # (tools/mods also exists in the private repo, for newer mods)  # public layout: mods/ next to the app; private repo: claude-re/v7.17
MAX_FILE = 4_000_000  # bytes; firmware files are ~0.4 MB, anything much bigger is not one
EDITS = {  # mask bit -> edit files (offset:hex lines), the same files the tested builds were made from
    PADDLES: ["edits_7.1_paddles.txt"],
    RATE58: ["edits_interval.txt", "edits_accept.txt"],
    RATE115: ["edits_gate2.txt"],
    STICKRAW: ["edits_stick_raw.txt"],
    STICKRAW2: ["edits_stick_raw2.txt"],
    SHOTKEY: ["edits_kbshot.txt"],
    SHOTBACK: ["edits_shotback.txt"],
    SHOTSHARE: ["edits_shotshare.txt"],
}
# Masks whose code is byte-identical to a build tested on hardware (only the marker byte differs).
TESTED = {0: "stock", PADDLES: "7.1", PADDLES | RATE58: "7.13/7.15", PADDLES | RATE58 | RATE115: "7.17",
          PADDLES | RATE58 | RATE115 | STICKRAW: "mods 0xF (2026-11-22)",
          PADDLES | RATE58 | RATE115 | STICKRAW | STICKRAW2: "mods 0x1F (2026-11-22)",
          PADDLES | RATE58 | RATE115 | STICKRAW | STICKRAW2 | SHOTBACK: "7.93 (screenshot = Android Back, 2026-11-22)",
          PADDLES | RATE58 | RATE115 | STICKRAW | STICKRAW2 | SHOTSHARE: "7.94 (screenshot = Share, 2026-11-22)"}

# Rates measured on Linux: stock ~27 reports/s, 7.13 ~58/s, 7.17 ~115/s. Battery cost not measured: faster = the BLE
# radio wakes every 7.5 ms instead of every 18.75 ms, so it should cost battery.
FASTER_NOTE = ("Faster uses more battery (the Bluetooth radio wakes up more often; how much is not measured yet). "
               "Can disturb DS4 mode if your phone is paired in both modes.")
# Mods: (title, slider?, [(mask bits, description, note, meter 0-10 or None)]); level 0 = stock.
FEATURES = [
    ("Back buttons", False, [
        (0, "Stock back buttons", "copy a face button (programmable)", None),
        (PADDLES, "Independent back buttons", "L4 = BUTTON_C, R4 = BUTTON_Z; set both paddles to unmapped (null) in "
         "the GameSir app, or they still copy a face button", None),
    ]),
    ("Input speed", True, [
        (0, "Input speed: Standard (stock)", "~27 updates per second", 2),
        (RATE58, "Input speed: Fast", "~58 updates per second", 5),
        (RATE58 | RATE115, "Input speed: Fastest", "~115 updates per second", 10),
    ]),
    ("Stick smoothing", True, [
        (0, "Stick smoothing: Stock", "filter 1 (+~24 ms) + filter 2 (+~8 ms) on slow moves; hides stick jitter (centre ±3)", 3),
        (STICKRAW, "Stick smoothing: Light", "filter 1 off (-24 ms), filter 2 on (+~8 ms); centre jitter ±5 (measured)", 7),
        (STICKRAW | STICKRAW2, "Stick smoothing: Off",
         "both off (-32 ms; filter 2 only in Android mode); no idle jitter measured on our unit", 10),
    ]),
    ("Screenshot button", False, [
        (0, "Screenshot button: phone screenshot (stock)",
         "sends Volume Down + Power: Android takes a screenshot (a PC may open its power/logout menu instead)", None),
        (SHOTBACK, "Screenshot button: Android Back", "the system Back key (like the phone's back gesture); apps such "
         "as Nightjar/Moonlight can bind it to their menu or exit", None),
        (SHOTSHARE, "Screenshot button: Share", "the media Record key (Android MEDIA_RECORD): Nightjar shows it as "
         "'Share' and sends it to the PC as the controller's Share button", None),
        (SHOTKEY, "Screenshot button: Print Screen key", "a keyboard key: PCs take a screenshot; bind it in your apps", None),
        (SHOTKEY, "Screenshot button: F13 key", "a keyboard key no PC or phone uses by default: bind it in Steam, "
         "Nightjar etc. (Linux desktops treat F13 as 'Tools')", None),
        (SHOTKEY, "Screenshot button: custom key", "any key or combo you type, e.g. ctrl+shift+f12 (Enter to change)",
         None),
    ]),
]
SHOT_FEATURE = len(FEATURES) - 1
SHOT_PRESETS = {3: (0, 0x46, "Print Screen"), 4: (0, 0x68, "F13")}  # level -> (modifiers, HID key, label)
SHOT_CUSTOM = 5  # level index of "custom key"
SHOT_TEMPLATE = bytes.fromhex("c2ff00006800")  # 'r2 = 0x00680000' (F13) in edits_kbshot.txt: u32 = mods | key << 16
SHOT_NOTE = ("Keyboard keys work in Android mode (green light); the phone-screenshot combo is then gone. The "
             "controller doesn't report which key it has, only that it sends one.")
HID_KEYS = {**{chr(ord("a") + i): 0x04 + i for i in range(26)}, **{str((i + 1) % 10): 0x1E + i for i in range(10)},
            **{f"f{i}": 0x3A + i - 1 for i in range(1, 13)}, **{f"f{i}": 0x68 + i - 13 for i in range(13, 25)},
            "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "minus": 0x2D, "equal": 0x2E,
            "printscreen": 0x46, "prtsc": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49, "home": 0x4A,
            "pageup": 0x4B, "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E, "right": 0x4F, "left": 0x50, "down": 0x51,
            "up": 0x52, "menu": 0x65}
HID_MODS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08, "super": 0x08, "meta": 0x08,
            "rctrl": 0x10, "rshift": 0x20, "altgr": 0x40, "ralt": 0x40, "rwin": 0x80}


def parse_combo(text: str) -> tuple[int, int, str]:
    """'ctrl+shift+f12' -> (modifier bits, HID key, tidy label). One key plus any modifiers. Raises ValueError."""
    parts = [p for p in text.lower().replace(" ", "").split("+") if p]
    mods, keys = 0, []
    for p in parts:
        if p in HID_MODS:
            mods |= HID_MODS[p]
        elif p in HID_KEYS:
            keys.append(p)
        else:
            raise ValueError(f"unknown key '{p}'")
    if len(keys) != 1:
        raise ValueError("give exactly one key (plus any of ctrl, shift, alt, win, altgr)")
    names = [m for m in ("ctrl", "shift", "alt", "win", "rctrl", "rshift", "altgr", "rwin") if mods & HID_MODS[m]]
    label = "+".join(n.capitalize() if len(n) > 3 else n.upper() if n in ("alt", "win") else n.capitalize()
                     for n in names + keys)
    return mods, HID_KEYS[keys[0]], label

ACTIONS = {  # name -> one-line description of what happens, shown under the focused action
    "Mod and flash controller": "check base firmware -> apply mods -> build image -> flash -> check controller",
    "Save modded firmware to a file": "check base firmware -> apply mods -> build image -> save a .zip (no flashing)",
    "Flash a firmware file as-is": "pick a file -> check it -> flash -> check controller (no mods applied)",
    "Reset controller to stock firmware": "check official 2.20 -> flash it unchanged -> check controller",
    "Read controller again": "connect and read which mods the controller has (changes nothing)",
    "Remove laptop pairing": "forget the controller in the laptop's Bluetooth settings (needed before flashing)",
    "Scan for controllers": "look for GameSir controllers nearby (about 6 s)",
    "Check for updates": "ask GitHub for the newest release and tell you (downloads and installs nothing)",
    "Quit": "",
}
ACTIONS_MFI = {  # descriptions that differ for the MFi (wired, build-only)
    "Read controller again": "look at the USB connection again and read which mods it has (sends nothing to it)",
    "Save modded firmware to a file": "check base firmware -> apply mods -> build image -> save a .zip for the phone "
                                      "app (no flashing)",
}


def short_path(p: Path) -> str:
    """Path relative to the app's folder, else with ~ for the home folder."""
    try:
        return str(p.relative_to(TOOLS.parent))
    except ValueError:
        home = str(Path.home())
        return "~" + str(p)[len(home):] if str(p).startswith(home) else str(p)


def run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# -- mods <-> mask ----------------------------------------------------------------------------------------------

def levels_of(mask: int, features=None) -> list[int]:
    """Mask -> chosen level per mod (highest level whose bits are all set; of levels with the same bits, the first)."""
    out = []
    for _title, _slider, levels in features or FEATURES:
        lvl = 0
        for i, level in enumerate(levels):
            if level[0] and level[0] & mask == level[0] and level[0] != levels[lvl][0]:
                lvl = i
        out.append(lvl)
    return out


def mask_of(levels: list[int], features=None) -> int:
    m = 0
    for (_title, _slider, lv), i in zip(features or FEATURES, levels):
        m |= lv[i][0]
    return m


def mods_text(mask: int, features=None, shot_label: str | None = None) -> list[str]:
    """Mod descriptions for a mask. The screenshot key itself isn't in the mask (nor stored on the controller):
    pass shot_label to name it, else it reads 'a keyboard key'."""
    features = features or FEATURES
    out = [levels[lvl][1] for (_t, _s, levels), lvl in zip(features, levels_of(mask, features))]
    if features is FEATURES and mask & SHOTKEY:
        out[SHOT_FEATURE] = f"Screenshot button: {shot_label or 'a keyboard key'}"
    return out


def mask_from_report_map(report_map: bytes) -> tuple[int | None, str]:
    """Returns (installed mods mask, or None if unknown; firmware description)."""
    m = HAT_ITEM.search(report_map)
    if not m:
        return None, "unknown (no hat descriptor found)"
    b = m.group(1)[0]
    if b == MARKER_STOCK:
        return None, f"GameSir stock {BASE} (or an early test build without a marker)"
    if b == MARKER_717:
        return PADDLES | RATE58 | RATE115, f"Modified firmware (GameSir {BASE} + mods)"
    mask = (b & 0x0F if b & 0xF0 == MARKER_BASE else b & 0x1F if b & 0xF0 == MARKER_BASE2 | 0x10
            else SHOTKEY | b & 0x1F if b & 0xE0 == MARKER_SHOT else SHOTBACK | b & 0x1F if b & 0xE0 == MARKER_BACK
            else SHOTSHARE | b & 0x1F if b & 0xE0 == MARKER_SHARE else 0)
    if mask and mask == mask_of(levels_of(mask)):
        return mask, f"Modified firmware (GameSir {BASE} + mods)"
    return None, f"Unknown firmware (marker 0x{b:02x}, newer than this tool?)"


# -- firmware files -----------------------------------------------------------------------------------------------

def read_image(path: Path) -> bytes:
    """ota.bin from a zip, or a raw .bin."""
    if path.stat().st_size > MAX_FILE:
        raise ValueError("file too big to be a G8+ firmware")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [i for i in z.infolist() if Path(i.filename).name == "ota.bin"]
            if len(names) != 1:
                raise ValueError("the zip must contain exactly one ota.bin" if names else "the zip has no ota.bin")
            if names[0].file_size > MAX_FILE:
                raise ValueError("ota.bin in the zip is too big to be a G8+ firmware")
            return z.read(names[0])
    return path.read_bytes()


def is_stock(enc: bytes) -> bool:
    return hashlib.sha256(enc).hexdigest() == g8fw.STOCK_SHA256


def describe_image(enc: bytes) -> tuple[bool, str, int | None]:
    """(valid G8+ BT image?, description, mods mask or None)."""
    if is_stock(enc):
        return True, f"official GameSir {BASE}, unmodified", None
    if len(enc) != IMAGE_SIZE:
        return False, (f"not a G8+ Bluetooth firmware based on {BASE} (size {len(enc)}, expected {IMAGE_SIZE}); "
                       "this tool only flashes official 2.20 and builds made from it"), None
    try:
        plain = bytes(g8fw.crypt(enc))
        if not g8fw.check(plain, quiet=True):
            return False, "not a valid G8+ Bluetooth firmware (layout or checksums don't match)", None
    except Exception:
        return False, "not a G8+ Bluetooth firmware file", None
    hat = plain[MARKER_OFFSET - 3:MARKER_OFFSET + 4]
    if not HAT_ITEM.fullmatch(hat):
        return False, f"not a G8+ Bluetooth firmware based on {BASE} (Report Map not where expected)", None
    mask, desc = mask_from_report_map(hat)
    return True, desc, mask


def find_local_firmware(model=None) -> list[Path]:
    """Official base firmware files (2.20 for the BT model) next to this script, in its firmware/ folder, or in the
    current folder."""
    stock = model.is_stock if model else is_stock
    found = []
    for d in dict.fromkeys([TOOLS, FW_DIR, Path.cwd()]):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.zip")) + sorted(d.glob("*.bin")):
            try:
                if p.stat().st_size < 2_000_000 and stock(read_image(p)):
                    found.append(p)
            except (OSError, ValueError, zipfile.BadZipFile):
                pass
    return found


def download_stock(model=None) -> Path:
    """Official base firmware from GameSir's update server, checked and saved to FW_DIR. Returns the saved path."""
    model = model or BT
    req = urllib.request.Request(model.stock_url, headers={"User-Agent": "g8tui"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("the download is far bigger than the expected firmware zip")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("ota.bin")
        if info.file_size > MAX_FILE:
            raise ValueError("ota.bin in the download is too big")
        enc = z.read(info)
    if not model.is_stock(enc):
        raise ValueError(f"the downloaded file is not the expected official {model.base} (GameSir changed it?)")
    FW_DIR.mkdir(parents=True, exist_ok=True)
    out = FW_DIR / model.stock_name
    out.write_bytes(data)
    return out


def not_test_unit(addr: str) -> bool:
    """True if the user named a test controller ("spare"/"test" in known_controllers.json) and this isn't it."""
    tests = {a for a, n in g8ota.KNOWN.items() if n.lower() in ("spare", "test")}
    return bool(tests) and addr.upper() not in tests


def write_cache(name: str, data: bytes) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    path.write_bytes(data)
    return path


def edit_file(name: str) -> str:
    for d in MODS_DIRS:
        if (d / name).is_file():
            return str(d / name)
    raise FileNotFoundError(f"mod file {name} not found (looked in {', '.join(map(str, MODS_DIRS))})")


def mod_edits(mask: int) -> list[tuple[int, bytes]]:
    files = [edit_file(f) for bit, fs in EDITS.items() if mask & bit for f in fs]
    return g8fw.read_edits(files) if files else []


def version_key(v: str) -> tuple:
    """'v0.2.0' / '0.2.0-beta.1' -> comparable tuple (a pre-release sorts before its release)."""
    core, _, pre = v.strip().lstrip("v").partition("-")
    nums = tuple(int(x) for x in core.split("."))
    return nums + ((0, pre) if pre else (1, ""))


def latest_release() -> str:
    """Tag of the newest GitHub release (one GET to api.github.com, nothing downloaded or installed)."""
    import json
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "g8tui", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        tag = json.load(resp)["tag_name"]
    version_key(tag)  # raises on a tag we can't compare
    return tag.lstrip("v")


def marker_of(mask: int) -> int:
    if mask & SHOTKEY:
        return MARKER_SHOT | mask & 0x1F
    if mask & SHOTBACK:
        return MARKER_BACK | mask & 0x1F
    if mask & SHOTSHARE:
        return MARKER_SHARE | mask & 0x1F
    return (MARKER_BASE2 if mask & 0x10 else MARKER_BASE) | mask


def build_bytes(stock_enc: bytes, mask: int, marker: int | None, step=lambda name: None,
                shot: tuple[int, int] = (0, 0x68)) -> bytes:
    """Stock 2.20 + edits for `mask` (+ marker byte unless None). Calls step('build') between the stages.
    shot = (modifiers, HID key) for the screenshot-key mod (default F13)."""
    if not is_stock(stock_enc):
        raise ValueError("base is not the official 2.20")
    edits = mod_edits(mask)
    if mask & SHOTKEY:
        mods, key = shot
        if not (0 <= mods <= 0xFF and 0x04 <= key <= 0xA4):
            raise ValueError("screenshot key out of range")
        slot = SHOT_TEMPLATE[:2] + bytes([mods, 0, key, 0])
        fixed = []
        for off, data in edits:
            if SHOT_TEMPLATE in data:
                if data.count(SHOT_TEMPLATE) != 1:
                    raise ValueError("edits_kbshot.txt: key template not unique")
                data = data.replace(SHOT_TEMPLATE, slot)
            fixed.append((off, data))
        if fixed == edits and slot != SHOT_TEMPLATE:
            raise ValueError("edits_kbshot.txt: key template not found")
        edits = fixed
    if marker is not None:
        edits = sorted(edits + [(MARKER_OFFSET, bytes([marker]))])
    plain = g8fw.crypt(stock_enc)
    patched = g8fw.apply_edits(plain, edits)
    step("build")
    return bytes(g8fw.seal(plain, patched, edits))


def selftest() -> int:
    local = find_local_firmware()
    if not local and not REPO_STOCK_ZIP.is_file():
        print(f"no official {BASE} file: put {STOCK_NAME} in {FW_DIR} (see FIRMWARE.md)")
        return 1
    stock = read_image(local[0] if local else REPO_STOCK_ZIP)
    ok = True
    for mask, marker, ref in ((PADDLES, None, "v7.1/ota.bin"), (PADDLES | RATE58, None, "v7.13/ota.bin"),
                              (PADDLES | RATE58 | RATE115, MARKER_717, "v7.17/ota.bin")):
        if not (CR / ref).is_file():
            print(f"mods {mask:x}: skipped, {ref} not here (firmware files aren't in git)")
            continue
        same = build_bytes(stock, mask, marker) == (CR / ref).read_bytes()
        print(f"mods {mask:x} -> {ref}: {'identical' if same else 'DIFFERENT'}")
        ok &= same
    for mask in sorted({mask_of([a, b, c, d]) for a in range(2) for b in range(3) for c in range(3)
                        for d in (0, 1, 2, 3)} - {0}):
        valid, _desc, back = describe_image(build_bytes(stock, mask, marker_of(mask)))
        print(f"mods {mask:x}: valid {valid}, marker reads back as {back}")
        ok &= valid and back == mask
    for combo, want in (("f13", "c2ff00006800"), ("PrtSc", "c2ff00004600"), ("ctrl+shift+f12", "c2ff03004500")):
        mods, key, _label = parse_combo(combo)
        plain = g8fw.crypt(build_bytes(stock, SHOTKEY, None, shot=(mods, key)))
        got = plain[0x178D0:0x178D6].hex()
        print(f"screenshot key {combo}: {got}")
        ok &= got == want
    mfi_local = find_local_firmware(MFI) + [p for p in (TOOLS.parent / "mfi/downloads/8900/ota.bin",) if p.is_file()]
    if mfi_local:
        ok &= model_mfi.selftest(read_image(mfi_local[0]))
    else:
        print(f"MFi: skipped, no official {MFI.base} file (put {MFI.stock_name} in {FW_DIR})")
    print("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


# -- controller models --------------------------------------------------------------------------------------------
# What the UI needs per model. The BT model's code is in this file; other models live in model_<key>.py.

def bt_save_name(mask: int) -> str:
    return f"g8-bluetooth-firmware-7.5{mask}.zip"  # name drives the phone app's "newer version" check; 7.5x > 7.1x tests


BT = types.SimpleNamespace(
    key="bt", name="G8+ Bluetooth", blurb="Bluetooth model. Flashes over Bluetooth LE from this laptop (Android "
    "mode, green light).", base=BASE, stock_name=STOCK_NAME, stock_url=STOCK_URL, features=FEATURES, tested=TESTED,
    faster_note=FASTER_NOTE, is_stock=is_stock, describe_image=describe_image, build_bytes=build_bytes,
    marker_of=marker_of, save_name=bt_save_name, can_flash=True, marker_tested=True)
MFI = types.SimpleNamespace(
    key="mfi", name=model_mfi.NAME, blurb=model_mfi.BLURB, base=model_mfi.BASE, stock_name=model_mfi.STOCK_NAME,
    stock_url=model_mfi.STOCK_URL, features=model_mfi.FEATURES, tested=model_mfi.TESTED,
    faster_note=model_mfi.FASTER_NOTE, is_stock=model_mfi.is_stock, describe_image=model_mfi.describe_image,
    build_bytes=model_mfi.build_bytes, marker_of=model_mfi.marker_of, save_name=model_mfi.save_name,
    can_flash=model_mfi.CAN_FLASH, marker_tested=model_mfi.MARKER_TESTED)
MODELS = [BT, MFI]


# -- controller state ---------------------------------------------------------------------------------------------

class Link:
    """An ATT connection the app keeps open while it runs, so the controller isn't left waiting for a host (and
    falling asleep). A background thread answers the controller's own ATT requests. Closed before flashing."""

    def __init__(self, addr: str):
        self.att = g8ota.Att(addr, False, False)
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while self.alive:
            with self.lock:
                if not self.alive:
                    break
                try:
                    self.att.pump(0.3)
                    self.att.notifications.clear()
                except Exception:
                    self.alive = False
                    try:
                        self.att.close()
                    except OSError:
                        pass
            time.sleep(0.2)

    def close(self) -> None:
        self.alive = False
        with self.lock:
            try:
                self.att.close()
            except OSError:
                pass
        if self in LINKS:
            LINKS.remove(self)


LINKS: list[Link] = []


class Controller:
    def __init__(self, addr: str, name: str = ""):
        self.addr, self.name = addr, name
        self.advertising = self.paired = self.connected = False
        self.firmware: str | None = None
        self.mask: int | None = None  # installed mods, None = unknown / not read
        self.dev_info = ""
        self.battery: int | None = None  # % from the BLE Battery Service, if the controller offers it
        self.read_note = ""
        self.link: Link | None = None

    @property
    def label(self) -> str:
        return g8ota.label(self.addr)

    @property
    def linked(self) -> bool:
        link = self.link  # read once: a worker thread may reset it
        return link is not None and link.alive

    @property
    def os_connected(self) -> bool:
        """Connected through the laptop's Bluetooth stack (our own ATT link also shows up as 'Connected')."""
        return self.connected and not self.linked

    def drop_link(self) -> None:
        link, self.link = self.link, None
        if link is not None:
            link.close()

    def mode(self) -> str:
        if self.name == g8ota.DEVICE_NAME:
            return "Android mode (can be flashed)"
        if self.name == g8ota.DEVICE_NAME + "_G":
            return "G-Touch recovery mode (use the phone app)"
        return "other mode (switch to Android mode to flash)" if self.name else "?"

    def refresh_bt(self) -> None:
        out = run(["bluetoothctl", "info", self.addr], 5)
        self.paired = bool(re.search(r"^\s*Paired: yes", out, re.M))
        self.connected = bool(re.search(r"^\s*Connected: yes", out, re.M))
        m = re.search(r"^\s*Name: (.*)$", out, re.M)
        if m and m.group(1).strip():
            self.name = m.group(1).strip()


def hidraw_descriptor(addr: str) -> bytes | None:
    for dev in Path("/sys/class/hidraw").glob("hidraw*"):
        try:
            if f"HID_UNIQ={addr.lower()}" in (dev / "device/uevent").read_text().lower():
                return (dev / "device/report_descriptor").read_bytes()
        except OSError:
            continue
    return None


def read_over_att(att) -> tuple[bytes | None, str, int | None]:
    """Report Map, Device Information revisions and the Battery Level if the controller has one (read-only)."""
    report_map, info, battery = None, [], None
    for start, end, suuid in att.services():
        u = g8ota.uuid16(suuid)
        if u not in (0x1812, 0x180A, 0x180F):
            continue
        for _decl, _props, value, cuuid in att.characteristics(start, end):
            c = g8ota.uuid16(cuuid)
            if u == 0x1812 and c == 0x2A4B:
                report_map = g8ota.read_long(att, value)
            elif u == 0x180A and c in (0x2A26, 0x2A28):
                try:
                    text = att.read(value).decode(errors="replace").strip("\0 ")
                    info.append(f"{'firmware' if c == 0x2A26 else 'software'} rev {text!r}")
                except g8ota.AttError:
                    pass
            elif u == 0x180F and c == 0x2A19:
                try:
                    level = att.read(value)
                    battery = level[0] if level and level[0] <= 100 else None
                except g8ota.AttError:
                    pass
    return report_map, ", ".join(info), battery


def read_build(ctl: Controller, attempts: int = 3) -> bool:
    """Read the installed mods. Returns True on success; on failure only ctl.read_note changes."""
    ctl.refresh_bt()
    if ctl.os_connected:
        report_map, source = hidraw_descriptor(ctl.addr), "hidraw (paired + connected)"
        if report_map is None:
            ctl.read_note = "connected to the laptop but no hidraw device found"
            return False
    else:
        last = None
        for _attempt in range(attempts):  # a controller that just woke up or rebooted often misses the first try
            try:
                if not ctl.linked:
                    ctl.drop_link()
                    ctl.link = Link(ctl.addr)
                    LINKS.append(ctl.link)
                with ctl.link.lock:
                    report_map, ctl.dev_info, ctl.battery = read_over_att(ctl.link.att)
                source = "Bluetooth"
                break
            except Exception as exc:
                last = exc
                ctl.drop_link()
                if attempts > 1:
                    time.sleep(2)
        else:
            ctl.read_note = f"couldn't reach the controller ({last}). Is it on and in Android mode?"
            return False
    if report_map is None:
        ctl.read_note = "no HID Report Map found"
        return False
    ctl.mask, ctl.firmware = mask_from_report_map(report_map)
    ctl.read_note = f"read {time.strftime('%H:%M:%S')} via {source}" + (
        "; the app stays connected so the controller stays awake" if ctl.linked else "")
    return True


# -- UI -----------------------------------------------------------------------------------------------------------

MARKS = {"todo": "[ ]", "run": "[>]", "done": "[x]", "fail": "[!]", "skip": "[-]"}


class Steps:
    """Checklist shown while an action runs."""

    def __init__(self, names: list[str]):
        self.items = [[n, "todo", ""] for n in names]  # name, state (todo/run/done/fail/skip), detail

    def set(self, i: int, state: str, detail: str = "") -> None:
        self.items[i][1] = state
        if detail:
            self.items[i][2] = detail

    def fail_running(self, msg: str) -> None:
        for item in self.items:
            if item[1] == "run":
                item[1], item[2] = "fail", msg


class App:
    """Arrow keys move, Left/Right/Enter change a value, Enter runs an action, Esc quits / cancels."""

    def __init__(self, scr):
        self.scr = scr
        self.ctls: list[Controller] = []
        self.sel = 0   # selected controller
        self.row = 0   # focused row in self.rows()
        self.log: list[str] = []
        self.usb: list[dict] = []  # GameSir USB devices (MFi), from sysfs
        self.m = BT                # controller model shown; MFi if one is plugged in at start
        self.usb_scan(quiet=True)
        if self.usb:
            self.m = MFI
        self.choice = [0] * len(self.m.features)  # selected level per mod
        self.sources: list[tuple[str, Path | None]] = []  # base firmware options: ("file", path) / ("none"|"choose")
        self.src = 0
        self.busy_action: str | None = None
        self.busy_label = ""
        self.dots = ""
        self.greyed = False
        self.steps: Steps | None = None
        self.accepted = False    # flash disclaimer accepted in this session
        self.shot_custom: tuple[int, int, str] | None = None  # (modifiers, HID key, label) for "custom key"
        self.scroll = 0          # first body line shown (the body scrolls so the focused row stays visible)
        self.body = False        # True while drawing the scrolling body
        self.focus_span = (0, 0)  # body lines of the focused row (+ its description)
        self.refresh_sources()
        curses.curs_set(0)
        curses.set_escdelay(25)
        curses.use_default_colors()
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN), 1):
            curses.init_pair(i, c, -1)
        self.green, self.yellow, self.red, self.cyan = (curses.color_pair(i) for i in range(1, 5))

    # -- helpers -------------------------------------------------------------------------------------------
    def put(self, y: int, x: int, text: str, attr: int = 0, keep: bool = False) -> None:
        if self.greyed and not keep:
            attr = curses.A_DIM
        h, w = self.scr.getmaxyx()
        if self.body:  # body lines scroll and must stay between the header (rows 0-1) and the footer (last 2 rows)
            y -= self.scroll
            if not 2 <= y < h - 2:
                return
        if 0 <= y < h and x < w - 1:
            try:
                self.scr.addnstr(y, x, text, w - 1 - x, attr)
            except curses.error:
                pass

    def wrapped(self, y: int, x: int, text: str, attr: int = 0, indent: int = 2, keep: bool = False) -> int:
        width = max(self.scr.getmaxyx()[1] - x - 2, 20)
        for part in textwrap.wrap(text, width, subsequent_indent=" " * indent):
            self.put(y, x, part, attr, keep)
            y += 1
        return y

    def say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.log = self.log[-50:]

    @property
    def ctl(self) -> Controller | None:
        return self.ctls[self.sel] if self.ctls else None

    def refresh_sources(self, select: Path | None = None) -> None:
        files = [p for kind, p in self.sources if kind == "file" and p.is_file()]
        for p in find_local_firmware(self.m):
            if p not in files:
                files.append(p)
        if select is not None and select not in files:
            files.append(select)
        self.sources = [("file", p) for p in files] or [("none", None)]
        self.sources.append(("choose", None))
        if select is not None:
            self.src = self.sources.index(("file", select))
        self.src = min(self.src, len(self.sources) - 1)

    def source_text(self) -> tuple[str, str]:
        kind, path = self.sources[self.src]
        if kind == "file":
            return f"{path.name}", f"official GameSir {self.m.base}, in {short_path(path.parent)}"
        if kind == "none":
            return ("No firmware file found", f"put {self.m.stock_name} in {short_path(FW_DIR)}, or you'll be offered a download "
                    "from GameSir when you start")
        return "Choose a file...", "press Enter to pick the official GameSir firmware (.zip or .bin)"

    def rows(self) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = [("model", None)]
        if self.m is not BT:
            out.append(("src", None))
            out += [("mod", i) for i in range(len(self.m.features))]
            out += [("act", "Save modded firmware to a file"), ("act", "Read controller again"),
                    ("act", "Check for updates"), ("act", "Quit")]
            return out
        c = self.ctl
        if len(self.ctls) > 1:
            out.append(("ctl", None))
        out.append(("src", None))
        out += [("mod", i) for i in range(len(FEATURES))]
        if c:
            out.append(("act", "Mod and flash controller"))
        out.append(("act", "Save modded firmware to a file"))
        if c:
            out += [("act", "Flash a firmware file as-is"), ("act", "Reset controller to stock firmware"),
                    ("act", "Read controller again")]
            if c.paired:
                out.append(("act", "Remove laptop pairing"))
        out += [("act", "Scan for controllers"), ("act", "Check for updates"), ("act", "Quit")]
        return out

    # -- dialogs -------------------------------------------------------------------------------------------
    def dialog(self, title: str, lines: list[tuple[str, int]], options: list[str], default: int = 0) -> int | None:
        """Box with text and buttons. Left/Right pick, Enter confirms, Esc = None. y/n also work for 2 buttons.
        Text taller than the window scrolls with Up/Down (Up/Down pick buttons only when everything fits)."""
        idx, off = default, 0
        while True:
            self.draw()
            h, w = self.scr.getmaxyx()
            bw = max(min(w - 4, 96), 20)
            body: list[tuple[str, int]] = []
            for text, attr in lines:
                body += [(part, attr) for part in (textwrap.wrap(text, bw - 4) or [""])]
            buttons: list[list[tuple[int, str]]] = [[]]  # wrap buttons onto several lines if needed
            x = 2
            for i, opt in enumerate(options):
                label = f"[ {opt} ]"
                if x + len(label) > bw - 2 and buttons[-1]:
                    buttons.append([])
                    x = 2
                buttons[-1].append((i, label))
                x += len(label) + 2
            bh = min(len(body) + 6 + len(buttons), h)
            avail = max(1, bh - 6 - len(buttons))  # text rows: border, blank, text..., blank, hint, buttons, border
            scrolls = len(body) > avail
            off = max(0, min(off, len(body) - avail))
            keys = "Left/Right choose   Enter confirm   Esc cancel" if len(options) > 1 else "Enter = OK"
            if scrolls:
                keys = "Up/Down scroll   " + keys
            top, left = max((h - bh) // 2, 0), max((w - bw) // 2, 0)
            win = curses.newwin(bh, bw, top, left)
            win.erase()
            win.box()
            try:
                win.addnstr(0, 2, f" {title} ", bw - 4, curses.A_BOLD)
                for i, (text, attr) in enumerate(body[off:off + avail]):
                    win.addnstr(2 + i, 2, text, bw - 4, attr)
                if scrolls and off > 0:
                    win.addnstr(1, bw - 10, " more ^ ", 8, curses.A_REVERSE)
                if scrolls and off + avail < len(body):
                    win.addnstr(2 + avail, bw - 10, " more v ", 8, curses.A_REVERSE)
                win.addnstr(bh - 2 - len(buttons), 2, keys, bw - 4, curses.A_DIM)
                for r, line in enumerate(buttons):
                    x = 2
                    for i, label in line:
                        win.addnstr(bh - 1 - len(buttons) + r, x, label, bw - x - 1,
                                    curses.A_REVERSE if i == idx else 0)
                        x += len(label) + 2
            except curses.error:
                pass
            win.refresh()
            k = self.scr.getch()
            if scrolls and k == curses.KEY_UP:
                off = max(0, off - 1)
            elif scrolls and k == curses.KEY_DOWN:
                off += 1
            elif k in (curses.KEY_LEFT, curses.KEY_UP):
                idx = (idx - 1) % len(options)
            elif k in (curses.KEY_RIGHT, curses.KEY_DOWN, 9):
                idx = (idx + 1) % len(options)
            elif k in (10, 13, curses.KEY_ENTER):
                return idx
            elif k == 27:
                return None
            elif k in (ord("y"), ord("Y")) and len(options) == 2:
                return 1
            elif k in (ord("n"), ord("N")):
                return None

    def shot(self) -> tuple[int, int, str] | None:
        """(modifiers, HID key, label) of the chosen screenshot key, or None (stock, or custom not set yet)."""
        if self.m is not BT:
            return None
        lvl = self.choice[SHOT_FEATURE]
        return SHOT_PRESETS.get(lvl) or (self.shot_custom if lvl == SHOT_CUSTOM else None)

    def shot_label(self) -> str | None:
        k = self.shot()
        return k[2] if k else None

    def ask_combo(self) -> bool:
        """Ask for the custom screenshot key. Returns True if one is set."""
        text, err = (self.shot_custom[2].lower() if self.shot_custom else ""), ""
        while True:
            self.draw()
            h, w = self.scr.getmaxyx()
            bw = max(min(w - 4, 76), 30)
            lines = ["Type a key, optionally with modifiers, joined with +.",
                     "Examples: f13   ctrl+shift+f12   alt+f9   printscreen   ctrl+alt+p",
                     "Keys: a-z 0-9 f1-f24 printscreen insert home end pageup pagedown delete pause tab space ...",
                     "Modifiers: ctrl shift alt win altgr (rctrl rshift rwin)", ""]
            win = curses.newwin(len(lines) + 7, bw, max((h - len(lines) - 7) // 2, 0), max((w - bw) // 2, 0))
            win.erase()
            win.box()
            try:
                win.addnstr(0, 2, " Screenshot button: custom key ", bw - 4, curses.A_BOLD)
                for i, line in enumerate(lines):
                    win.addnstr(2 + i, 2, line, bw - 4, curses.A_DIM if i else 0)
                win.addnstr(2 + len(lines), 2, f"> {text}_", bw - 4, curses.A_BOLD)
                if err:
                    win.addnstr(3 + len(lines), 2, err, bw - 4, self.red)
                win.addnstr(len(lines) + 5, 2, "Enter = OK   Esc = cancel", bw - 4, self.cyan)
            except curses.error:
                pass
            win.refresh()
            k = self.scr.getch()
            if k == 27:
                return self.shot_custom is not None
            if k in (10, 13, curses.KEY_ENTER):
                try:
                    self.shot_custom = parse_combo(text)
                    self.say(f"screenshot key: {self.shot_custom[2]}")
                    return True
                except ValueError as exc:
                    err = str(exc)
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                text, err = text[:-1], ""
            elif 32 <= k < 127 and len(text) < 40:
                text, err = text + chr(k), ""

    def info(self, title: str, text: str) -> None:
        self.dialog(title, [(text, 0)], ["OK"])

    def pick_file(self) -> Path | None:
        """Arrow-key file browser for .zip/.bin files. Enter opens a folder / picks a file, Esc cancels."""
        d = Path.home() / "Downloads"
        d = d if d.is_dir() else Path.home()
        idx = 0
        while True:
            try:
                entries = sorted((p for p in d.iterdir() if not p.name.startswith(".")),
                                 key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                entries = []
            items = [d.parent] + [p for p in entries if p.is_dir() or p.suffix.lower() in (".zip", ".bin")]
            idx = min(idx, len(items) - 1)
            self.scr.erase()
            h, _w = self.scr.getmaxyx()
            self.put(0, 1, "Choose a firmware file (.zip or .bin)", curses.A_BOLD)
            self.put(1, 1, str(d), curses.A_DIM)
            top = max(0, idx - (h - 6))
            for i, p in enumerate(items[top:top + h - 5], top):
                label = ".. (up one folder)" if i == 0 else (p.name + "/" if p.is_dir() else p.name)
                self.put(3 + i - top, 1, f"{'>' if i == idx else ' '} {label}", curses.A_REVERSE if i == idx else 0)
            self.put(h - 1, 1, "Up/Down move   Enter open/choose   Left up one folder   Esc cancel", self.cyan)
            self.scr.refresh()
            k = self.scr.getch()
            if k == curses.KEY_UP:
                idx = max(idx - 1, 0)
            elif k == curses.KEY_DOWN:
                idx = min(idx + 1, len(items) - 1)
            elif k == curses.KEY_LEFT:
                d, idx = d.parent, 0
            elif k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                p = items[idx]
                if p.is_dir():
                    d, idx = p, 0
                elif k != curses.KEY_RIGHT:
                    return p
            elif k == 27:
                return None

    # -- drawing -------------------------------------------------------------------------------------------
    def draw(self, busy: str = "", _again: bool = True) -> None:
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        rows = self.rows()
        self.row = min(self.row, len(rows) - 1)
        focus = rows[self.row]
        self.put(0, 1, f"GameSir G8+ mod tool {VERSION} (beta) · by {AUTHOR} · github.com/{REPO}", curses.A_BOLD, keep=True)
        if busy:
            self.put(1, 1, f"{busy}   (please wait)", curses.A_BOLD | self.cyan, keep=True)
        self.body = True
        try:
            end = self.draw_body(rows, focus)
        finally:
            self.body = False
        # keep the focused row (and its description) inside the visible body rows 2 .. h-3
        top, bottom = self.focus_span
        view = h - 4
        scroll = self.scroll
        if bottom - scroll > h - 3:
            scroll = bottom - (h - 3)
        if top - scroll < 2:
            scroll = top - 2
        scroll = max(0, min(scroll, max(0, end - 2 - view)))
        if scroll != self.scroll and _again:
            self.scroll = scroll
            self.draw(busy, _again=False)
            return
        if self.scroll > 0:
            self.put(2, w - 9, " more ^ ", curses.A_DIM | curses.A_REVERSE, keep=True)
        if end - self.scroll > h - 2:
            self.put(h - 3, w - 9, " more v ", curses.A_DIM | curses.A_REVERSE, keep=True)
        hint = "Up/Down move   Left/Right or Enter change   Enter run   Esc quit"
        self.put(h - 2, 1, hint, self.cyan)
        if self.log:
            self.put(h - 1, 1, self.log[-1], curses.A_DIM)
        s.refresh()
        if self.steps:
            self.draw_steps()

    def mark_focus(self, top: int, bottom: int) -> None:
        self.focus_span = (top, bottom)

    def draw_body(self, rows, focus) -> int:
        """Draw the scrolling part; returns the first unused body line."""
        y = 2
        f = focus[0] == "model"
        self.put(y, 1, f"{'>' if f else ' '} Controller type: < {self.m.name} >",
                 curses.A_REVERSE if f else curses.A_BOLD)
        if f:
            self.mark_focus(y, y + 1)
            y = self.wrapped(y + 1, 6, "Left/Right switch, Enter shows all types. " + self.m.blurb, curses.A_DIM, 0)
        else:
            y += 1
        y += 1
        if self.m is MFI:
            y = self.draw_mfi(y)
        elif not self.ctls:
            for line in ("No controller found yet.",
                         "1. Turn the controller on in Android mode (A + Home, green light).",
                         "2. If it connects to your phone, turn the phone's Bluetooth off for now:",
                         "   while it's connected to the phone, the laptop can't see it.",
                         "3. Choose Scan for controllers below.",
                         "You can also just save a modded firmware file without a controller."):
                y = self.wrapped(y, 1, line, curses.A_BOLD if line.startswith("No") else 0, 3)
            y += 1
        else:
            c = self.ctl
            ctl_line = f"Controller: {c.label}  {c.name or '?'}"
            if len(self.ctls) > 1:
                ctl_line = f"Controller: < {c.label}  {c.name or '?'} >  ({self.sel + 1} of {len(self.ctls)})"
            self.put(y, 1, ctl_line, curses.A_REVERSE if focus[0] == "ctl" else curses.A_BOLD)
            if focus[0] == "ctl":
                self.mark_focus(y, y)
            y += 1
            self.put(y, 3, f"Mode:      {c.mode()}")
            y += 1
            if c.battery is not None:
                low = c.battery < 30
                self.put(y, 3, f"Battery:   {c.battery} %" + ("  (charge it before flashing)" if low else ""),
                         self.red | curses.A_BOLD if low else 0)
                y += 1
            if c.paired or c.os_connected:
                conn = ("paired with this laptop as a gamepad" + (", connected" if c.os_connected else "")
                        + " (removed automatically before flashing)")
                attr = self.yellow
            elif c.linked:
                conn, attr = "connected to this app", self.green
            elif c.advertising:
                conn, attr = "not connected (controller is on and waiting)", 0
            else:
                conn, attr = "not connected (not seen in the last scan)", curses.A_DIM
            y = self.wrapped(y, 3, f"Bluetooth: {conn}", attr, 11)
            if c.linked and not (c.paired or c.os_connected):
                y = self.wrapped(y, 14, "Your laptop's Bluetooth menu shows it as connected too: that's this app's "
                                 "link. It isn't paired, so the laptop doesn't use it as a gamepad. The link "
                                 "closes when you quit.", curses.A_DIM, 0)
            y = self.wrapped(y, 3, f"Firmware:  {c.firmware or 'not read yet'}",
                             (self.cyan if c.firmware else curses.A_DIM) | curses.A_BOLD, 11)
            if c.read_note:
                bad = "couldn't" in c.read_note or c.read_note.startswith(("no ", "connected to the laptop"))
                y = self.wrapped(y, 14, c.read_note, self.red if bad else curses.A_DIM, 0)
            y += 1
        # base firmware
        title, sub = self.source_text()
        f = focus == ("src", None)
        kind = self.sources[self.src][0]
        arrows = len(self.sources) > 1
        text = f"Base firmware: {'< ' if arrows else ''}{title}{' >' if arrows else ''}"
        self.put(y, 1, f"{'>' if f else ' '} {text}",
                 curses.A_REVERSE if f else (self.yellow | curses.A_BOLD if kind == "none" else curses.A_BOLD))
        top = y
        y = self.wrapped(y + 1, 6, sub, curses.A_DIM, 0)
        if f:
            self.mark_focus(top, y - 1)
        y += 1
        y = self.draw_mods(y, focus)
        y += 1
        for kind, name in rows:
            if kind != "act":
                continue
            focused = (kind, name) == focus
            if name == self.busy_action and self.dots:
                self.put(y, 1, f"{'>' if focused else ' '} {self.busy_label}{self.dots}", curses.A_BOLD, keep=True)
            else:
                self.put(y, 1, f"{'>' if focused else ' '} {name}", curses.A_REVERSE if focused else 0)
            top = y
            y += 1
            desc = (ACTIONS_MFI.get(name) if self.m is MFI else None) or ACTIONS.get(name)
            if focused and desc and not self.greyed:
                y = self.wrapped(y, 5, desc, curses.A_DIM, 0)
            if focused:
                self.mark_focus(top, y - 1)
        return y

    def draw_mfi(self, y: int) -> int:
        status, firmware, _mask = model_mfi.usb_status(self.usb)
        self.put(y, 1, "Controller: G8+ MFi", curses.A_BOLD)
        y = self.wrapped(y + 1, 3, f"USB:       {status}", self.green if firmware else curses.A_DIM, 11)
        if firmware:
            y = self.wrapped(y, 3, f"Firmware:  {firmware}", self.cyan | curses.A_BOLD, 11)
            y = self.wrapped(y, 14, "read from what the laptop already knows about the controller; nothing is sent "
                             "to it", curses.A_DIM, 0)
        y = self.wrapped(y, 3, "Flashing: this app only builds the file. Copy it to your phone and flash it with the "
                         "GameSir app (local file), with the controller connected to the phone.", 0, 11)
        return y + 1

    def installed(self) -> tuple[bool, int | None]:
        """(a controller of the current model is there, its mods mask or None if unknown)."""
        if self.m is MFI:
            _status, firmware, mask = model_mfi.usb_status(self.usb)
            return firmware is not None, mask
        c = self.ctl
        return c is not None, c.mask if c else None

    def draw_mods(self, y: int, focus) -> int:
        features = self.m.features
        have, inst = self.installed()
        known = inst is not None
        cur = levels_of(inst or 0, features) if have else [0] * len(features)
        c = have
        head = "Mods"
        if c and not known:
            head += "  (controller's mods unknown: compared with stock)"
        elif not c:
            head += "  (* = differs from stock)"
        self.put(y, 1, head, curses.A_BOLD)
        y += 1
        for i, (_t, slider, levels) in enumerate(features):
            lvl = self.choice[i]
            _bits, desc, note, meter = levels[lvl]
            if self.m is BT and i == SHOT_FEATURE and lvl == SHOT_CUSTOM:
                desc = f"Screenshot button: custom key ({self.shot_custom[2] if self.shot_custom else 'not set'})"
            if lvl != cur[i]:
                text, attr = f"* {desc}", curses.A_BOLD | self.yellow   # differs from the controller / stock
            elif lvl == 0:
                text, attr = f"  {desc}", curses.A_DIM                    # stock behaviour
            else:
                text, attr = f"  {desc}", self.green                      # mod already installed
            focused = focus == ("mod", i)
            box = "<->" if slider else ("[x]" if lvl else "[ ]")
            self.put(y, 1, f"{'>' if focused else ' '} {box}", curses.A_REVERSE if focused else 0)
            top = y
            y = self.wrapped(y, 8, text, attr, 2)
            if meter is not None:
                self.put(y, 10, "Slow ")
                self.put(y, 15, "█" * meter, attr if lvl != cur[i] else self.cyan)
                self.put(y, 15 + meter, "░" * (10 - meter), curses.A_DIM)
                self.put(y, 26, "Fast")
                self.put(y, 32, note, curses.A_DIM)
                y += 1
                if lvl and self.m.faster_note:
                    y = self.wrapped(y, 10, self.m.faster_note, self.yellow, 0)
            elif note:
                y = self.wrapped(y, 10, note, curses.A_DIM, 0)
            if self.m is BT and i == SHOT_FEATURE and levels[lvl][0] == SHOTKEY:
                y = self.wrapped(y, 10, SHOT_NOTE, self.yellow, 0)
            if focused:
                self.mark_focus(top, y - 1)
        mask = mask_of(self.choice, features)
        tested = self.m.tested
        if known and mask == inst:
            y = self.wrapped(y + 1, 3, "This is what the controller has now.", curses.A_DIM, 0)
        else:
            msg = ("* = changes when you flash. " if c else "") + (
                "Tested combination." if mask in tested else "This combination is not tested on a controller yet.")
            y = self.wrapped(y + 1, 3, msg, self.green if mask in tested else self.red, 0)
        return y

    def steps_lines(self, width: int) -> list[tuple[str, int]]:
        lines = []
        for name, state, detail in self.steps.items:
            text = f"{MARKS[state]} {name}" + (self.dots if state == "run" else "")
            attr = {"todo": curses.A_DIM, "run": curses.A_BOLD | self.cyan, "done": self.green,
                    "fail": curses.A_BOLD | self.red, "skip": curses.A_DIM}[state]
            lines += [(part, attr) for part in textwrap.wrap(text, width, subsequent_indent="    ")]
            if detail:
                lines += [(part, self.red if state == "fail" else curses.A_DIM)
                          for part in textwrap.wrap(detail, width - 4, initial_indent="    ", subsequent_indent="    ")]
        return lines

    def draw_steps(self) -> None:
        h, w = self.scr.getmaxyx()
        bw = min(w - 4, 76)
        lines = self.steps_lines(bw - 4)
        bh = min(len(lines) + 4, h)
        win = curses.newwin(bh, bw, max((h - bh) // 2, 0), max((w - bw) // 2, 0))
        win.erase()
        win.box()
        try:
            win.addnstr(0, 2, " Working ", bw - 4, curses.A_BOLD)
            for i, (text, attr) in enumerate(lines[:bh - 3]):
                win.addnstr(2 + i, 2, text, bw - 4, attr)
        except curses.error:
            pass
        win.refresh()

    # -- running work ----------------------------------------------------------------------------------------
    def working(self, action: str | None, msg: str, fn):
        """Run fn() in a thread (no curses calls inside) and animate dots until it finishes. Re-raises."""
        res: dict = {}

        def target():
            try:
                res["v"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                res["e"] = exc

        th = threading.Thread(target=target, daemon=True)
        th.start()
        prev, self.busy_action, i = self.busy_action, action or self.busy_action, 0
        self.busy_label, self.greyed = msg, True
        try:
            while th.is_alive():
                self.dots = "." * (i % 3 + 1)
                self.draw(f"{msg}{self.dots:<3}")
                th.join(0.4)
                i += 1
        finally:
            self.busy_action, self.dots, self.greyed = prev, "", False
            curses.flushinp()
        if "e" in res:
            raise res["e"]
        return res.get("v")

    def step(self, steps: Steps, i: int, msg: str, fn, detail: str = ""):
        """Run one checklist step; marks it done or failed (and re-raises)."""
        steps.set(i, "run")
        try:
            out = self.working(None, msg, fn)
        except BaseException as exc:
            steps.set(i, "fail", str(exc) or exc.__class__.__name__)
            raise
        steps.set(i, "done", detail)
        return out

    def stopped(self, steps: Steps, exc: BaseException, text: str) -> None:
        """An action failed before flashing: mark the running step failed and always show the error."""
        msg = str(exc) or exc.__class__.__name__
        if not any(item[1] == "run" for item in steps.items):
            text = f"{msg}. {text}"
        steps.fail_running(msg)
        self.finish(steps, "Stopped", text, False)

    def finish(self, steps: Steps, title: str, text: str, ok: bool) -> None:
        """Show the final checklist with a message until the user presses Enter."""
        self.steps = steps
        lines = self.steps_lines(70)
        self.steps = None
        lines += [("", 0), (text, curses.A_BOLD if ok else self.red)]
        self.dialog(title, lines, ["OK"])

    # -- base firmware -----------------------------------------------------------------------------------------
    def plan_base(self) -> tuple[str, Path | None] | None:
        """Decide where the base firmware comes from, asking the user if there is no file. None = cancelled."""
        self.refresh_sources()
        kind, path = self.sources[self.src]
        if kind == "file":
            return ("file", path)
        if kind == "choose":
            self.choose_source_file()
            kind, path = self.sources[self.src]
            return ("file", path) if kind == "file" else None
        while True:
            ans = self.dialog("Firmware file needed", [
                (f"The mods are applied to GameSir's official firmware {self.m.base}, and there's no copy of it here yet. "
                 "We don't include it with this app because it's GameSir's.", 0),
                ("", 0),
                (f"Download: gets {self.m.stock_name} from GameSir's own update server, checks it and saves it "
                 f"in {FW_DIR} so you're not asked again.", 0),
                (f"Add it yourself: put {self.m.stock_name} (or its ota.bin) in {FW_DIR} or next to this app.", 0),
            ], ["Download from GameSir", "Choose a file...", "I'll add it myself", "Cancel"], default=0)
            if ans == 0:
                return ("download", None)
            if ans == 1:
                self.choose_source_file()
                kind, path = self.sources[self.src]
                if kind == "file":
                    return ("file", path)
                continue
            if ans == 2:
                try:
                    FW_DIR.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                self.info("Add the firmware file", f"Put {self.m.stock_name} in this folder:  {FW_DIR}   "
                          "(or next to this app). Then start again; the app finds it by itself.")
            return None

    def base_step_name(self, plan) -> str:
        if plan[0] == "file":
            return f"Check base firmware {plan[1].name} (must be official GameSir {self.m.base})"
        return f"Download official GameSir {self.m.base} from GameSir's update server, check it and save it to {FW_DIR}"

    def get_base(self, plan) -> bytes:
        """Bytes of the base firmware; must be official 2.20 (runs in the worker thread)."""
        if plan[0] == "file":
            enc = read_image(plan[1])
            if not self.m.is_stock(enc):
                raise ValueError(f"{plan[1].name} is not the official GameSir {self.m.base} firmware")
            return enc
        try:
            path = download_stock(self.m)
        except Exception as exc:
            raise RuntimeError(f"download failed ({exc}). GameSir may have moved the file: put a copy of "
                               f"{self.m.stock_name} in {FW_DIR} instead.") from exc
        plan[1] = path
        return read_image(path)

    def choose_source_file(self) -> None:
        p = self.pick_file()
        if p is None:
            return
        try:
            enc = read_image(p)
        except Exception as exc:
            self.info("Can't use this file", f"{p.name}: {exc}")
            return
        if self.m.is_stock(enc):
            self.refresh_sources(select=p)
            self.say(f"base firmware: {p.name} (official {self.m.base})")
            return
        valid, desc, _mask = self.m.describe_image(enc)
        self.info("Can't mod this file",
                  f"{p.name} is {f'a {self.m.name} firmware, but ' + desc if valid else desc}. Mods can only be "
                  f"applied to the official GameSir {self.m.base} firmware."
                  + (" To flash this file without changes, use 'Flash a firmware file as-is'." if self.m.can_flash
                     else ""))

    def base_and_build(self, steps: Steps, plan, mask: int) -> bytes:
        """Steps 0-2: base firmware, apply mods, build image. Returns the image; raises on failure."""
        base = self.step(steps, 0, "Checking base firmware", lambda: self.get_base(plan), f"official {self.m.base}, OK")
        if plan[0] == "download":
            self.refresh_sources(select=plan[1])

        def mark(name):
            if name == "build":
                steps.set(1, "done")
                steps.set(2, "run")
        steps.set(1, "run")
        extra = {"shot": self.shot()[:2]} if self.m is BT and mask & SHOTKEY else {}
        image = self.working(None, "Applying mods and building",
                             lambda: self.m.build_bytes(base, mask, self.m.marker_of(mask), mark, **extra))
        steps.set(2, "done", f"{len(image)} bytes, checksums OK")
        return image

    # -- actions -----------------------------------------------------------------------------------------------
    def scan(self) -> None:
        try:
            seen = self.working("Scan for controllers", "Scanning", g8ota.scan)
        except Exception as exc:
            self.say(f"scan failed: {exc}")
            return
        known = {}
        for line in run(["bluetoothctl", "devices"]).splitlines():
            m = re.match(r"Device ([0-9A-F:]{17}) (.*)", line.strip())
            if m and m.group(2).startswith("GameSir"):
                known[m.group(1)] = m.group(2)
        old = {c.addr: c for c in self.ctls}
        linked = {c.addr for c in self.ctls if c.linked}
        ctls = []
        for addr in sorted(set(known) | linked | {a for a, n in seen.items() if n.startswith("GameSir")}):
            c = old.get(addr) or Controller(addr)
            c.name = seen.get(addr) or known.get(addr) or c.name
            c.advertising = addr in seen and seen[addr].startswith("GameSir")
            c.refresh_bt()
            ctls.append(c)
        self.ctls = ctls
        self.sel = min(self.sel, max(len(ctls) - 1, 0))
        self.say(f"scan: {len(ctls)} GameSir controller(s)")
        for c in ctls:
            if c.firmware is None and not c.linked and (c.os_connected or c.name == g8ota.DEVICE_NAME):
                self.read(c)

    def read(self, c: Controller) -> None:
        ok = self.working(None, "Reading controller", lambda: read_build(c))
        if not ok:
            self.say(f"{c.label}: {c.read_note}")
            return
        self.say(f"{c.label}: {c.firmware}" + (f" (mods 0x{c.mask:x})" if c.mask is not None else ""))
        if c is self.ctl and c.mask is not None:
            self.choice = levels_of(c.mask)

    def unpair(self, c: Controller, ask: bool = True) -> bool:
        if ask and self.dialog("Remove pairing", [(f"Remove the laptop's Bluetooth pairing with {c.label}?", 0)],
                               ["Cancel", "Remove"]) != 1:
            return False
        c.drop_link()
        out = run(["bluetoothctl", "remove", c.addr]).strip().splitlines()
        self.say(f"remove pairing: {out[-1] if out else 'no output'}")
        c.refresh_bt()
        ok = not c.paired and not c.connected
        if not ok:
            self.info("Couldn't remove the pairing", "Remove the controller in the laptop's Bluetooth settings, "
                      "then try again.")
        return ok

    def can_flash(self, c: Controller) -> bool:
        """Mode check before the confirm dialog (nothing is changed here)."""
        c.refresh_bt()
        if c.name != g8ota.DEVICE_NAME:
            self.info("Can't flash", f"The controller isn't in Android mode ({c.name or 'name unknown'}). "
                      "Turn it on with A + Home (green light) and scan again.")
            return False
        return True

    def pairing_note(self, c: Controller) -> list[tuple[str, int]]:
        if c.paired or c.os_connected:
            return [("First the laptop's Bluetooth pairing with the controller is removed (needed for flashing).",
                     curses.A_DIM)]
        return []

    def unpair_if_needed(self, c: Controller) -> bool:
        """After the user confirmed: remove a laptop pairing / OS connection that would block the flash."""
        if c.paired or c.os_connected:
            return self.unpair(c, ask=False)
        return True

    DISCLAIMER = [
        "USE AT YOUR OWN RISK. This is an unofficial, experimental tool, not made or supported by GameSir. Flashing "
        "modified firmware can make the controller unusable (\"bricked\") and may void its warranty.",
        "The software comes WITHOUT ANY WARRANTY. Its authors are NOT responsible for any damage or loss, whatever the "
        "cause: mistakes in this tool, a flat controller battery, a laptop that sleeps, loses power or Bluetooth, or a "
        "flash that is interrupted for any other reason.",
        "Before flashing: charge the controller, plug the laptop in, keep both close together and don't touch the "
        "controller until the app says Done.",
    ]

    def confirm(self, title: str, steps: list[str], lines: list[tuple[str, int]], warn: list[str],
                flashing: bool = True, battery: int | None = None) -> bool:
        if battery is not None and battery < 30:
            warn = [f"The controller battery is low ({battery} %). Charge it first."] + warn
        body = [(w, curses.A_BOLD | self.red) for w in warn] + ([("", 0)] if warn else [])
        if flashing and not self.accepted:
            body += [(t, curses.A_BOLD if i < 2 else 0) for i, t in enumerate(self.DISCLAIMER)] + [("", 0)]
        body += [("This will:", curses.A_BOLD)] + [(f"  {i}. {s}", 0) for i, s in enumerate(steps, 1)]
        body += ([("", 0)] + lines) if lines else []
        start = "I accept the risk, start" if flashing and not self.accepted else "Start"
        ok = self.dialog(title, body, ["Cancel", start], default=0) == 1
        if ok and flashing:
            self.accepted = True  # shown once per session
        return ok

    def flash_and_check(self, c: Controller, steps: Steps, i: int, path: Path, expect: int | None) -> bool:
        """Steps i (flash) and i+1 (check). expect = mods mask, or None for stock. Returns True if all OK."""
        c.drop_link()  # g8ota opens its own connection

        def do_flash():
            time.sleep(1)
            env = dict(os.environ, G8OTA_PROGRESS_STEP="1")
            proc = subprocess.Popen([sys.executable, str(TOOLS / "g8ota.py"), "flash", str(path), "--address",
                                     c.addr, "--yes"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    env=env)
            tail: list[str] = []
            for line in proc.stdout:
                tail = (tail + [line.strip()])[-3:]
                m = re.match(r"\s*(\d+)%", line)
                if m:
                    steps.items[i][2] = f"{m.group(1)} %  (about 2 min; keep the controller on and close by)"
            if proc.wait() != 0:
                raise RuntimeError(" / ".join(t for t in tail if t) or f"exit {proc.returncode}")

        steps.set(i, "run", "connecting")
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C must not cut a flash short (the child inherits it)
        try:
            self.working(None, "Flashing", do_flash)
        except Exception as exc:
            steps.set(i, "fail", str(exc))
            steps.set(i + 1, "skip")
            return False
        finally:
            signal.signal(signal.SIGINT, old)
        steps.set(i, "done", "100 %")
        steps.set(i + 1, "run", "waiting for the controller to restart")
        c.firmware, c.mask = None, None
        for n in range(1, 9):  # up to ~2.5 min
            steps.items[i + 1][2] = f"waiting for the controller to restart (try {n} of 8)"
            self.working(None, "Controller restarting", lambda: time.sleep(6))
            if self.working(None, "Reading controller", lambda: read_build(c, attempts=1)):
                break
        if not c.firmware:
            steps.set(i + 1, "fail", "the controller didn't answer; turn it on in Android mode and choose Scan")
            return False
        if c.mask is not None:
            self.choice = levels_of(c.mask)
        if expect == -1:  # file without our marker: just report
            steps.set(i + 1, "done", f"controller reports: {c.firmware}")
            return True
        good = (c.mask == expect) if expect is not None else c.firmware.startswith("GameSir stock")
        if not good:
            steps.set(i + 1, "fail", f"controller reports: {c.firmware}"
                      + (f" with mods 0x{c.mask:x}" if c.mask is not None else ""))
            return False
        steps.set(i + 1, "done", "controller reports: " + ("; ".join(mods_text(expect, shot_label=self.shot_label())) if expect is not None
                                                             else f"stock {BASE}"))
        return True

    RECOVERY = ("If the controller still turns on in Android mode (A + Home), flash again from here. If not: hold "
                "screenshot + Start for ~1 s (recovery mode, blinking light blue) and update it with the GameSir phone "
                "app.")

    def act_mod_and_flash(self, c: Controller) -> None:
        mask = mask_of(self.choice)
        if mask == 0:
            self.act_reset(c)
            return
        if c.mask is not None and mask == c.mask:
            if self.dialog("Already installed", [
                    ("The controller already has exactly these mods, so a flash isn't needed.", 0),
                    ("You can flash it again anyway.", curses.A_DIM)], ["Cancel", "Flash again"]) != 1:
                return
        plan = self.plan_base()
        if plan is None:
            return
        plan = list(plan)
        names = [self.base_step_name(plan), "Apply mods: " + "; ".join(mods_text(mask, shot_label=self.shot_label())),
                 "Build firmware image (checksums, encryption, self-check)", f"Flash to controller {c.label}",
                 "Check that the controller reports the new mods"]
        warn = []
        if mask not in TESTED:
            warn.append("This combination has not been tested on a controller yet.")
        if not_test_unit(c.addr):
            warn.append("This is not your test controller (see known_controllers.json).")
        if (not self.can_flash(c) or not self.confirm("Mod and flash", names, self.pairing_note(c), warn, battery=c.battery)
                or not self.unpair_if_needed(c)):
            return
        steps = self.steps = Steps(names)
        try:
            image = self.base_and_build(steps, plan, mask)
            path = write_cache(f"g8-bluetooth-firmware-{BASE}-mods-{mask:x}.bin", image)
        except Exception as exc:
            self.stopped(steps, exc, "Nothing was flashed; the controller is unchanged.")
            return
        ok = self.flash_and_check(c, steps, 3, path, mask)
        self.say(f"mod and flash 0x{mask:x}: {'OK' if ok else 'FAILED'}")
        self.finish(steps, "Done" if ok else "Problem", "The controller has the new mods." if ok else self.RECOVERY, ok)

    def act_reset(self, c: Controller) -> None:
        plan = self.plan_base()
        if plan is None:
            return
        plan = list(plan)
        names = [self.base_step_name(plan), f"Flash it unchanged to controller {c.label}",
                 "Check that the controller reports stock firmware"]
        warn = ["This is not your test controller (see known_controllers.json)."] if not_test_unit(c.addr) else []
        if (not self.can_flash(c) or not self.confirm("Reset to stock", names, self.pairing_note(c), warn, battery=c.battery)
                or not self.unpair_if_needed(c)):
            return
        steps = self.steps = Steps(names)
        try:
            base = self.step(steps, 0, "Checking base firmware", lambda: self.get_base(plan), f"official {BASE}, OK")
            path = write_cache(f"g8-bluetooth-firmware-{BASE}-stock.bin", base)
        except Exception as exc:
            self.stopped(steps, exc, "Nothing was flashed; the controller is unchanged.")
            return
        if plan[0] == "download":
            self.refresh_sources(select=plan[1])
        ok = self.flash_and_check(c, steps, 1, path, None)
        if ok:
            self.choice = [0] * len(FEATURES)
        self.finish(steps, "Done" if ok else "Problem",
                    "The controller runs stock GameSir firmware." if ok else self.RECOVERY, ok)

    def act_save(self) -> None:
        m = self.m
        mask = mask_of(self.choice, m.features)
        if mask == 0:
            self.info("No mods selected", "Select at least one mod first; an unmodified file is just GameSir's own.")
            return
        plan = self.plan_base()
        if plan is None:
            return
        plan = list(plan)
        out_dir = Path.home() / "Downloads"
        out_dir = out_dir if out_dir.is_dir() else Path.cwd()
        out = out_dir / m.save_name(mask)
        names = [self.base_step_name(plan),
                 "Apply mods: " + "; ".join(mods_text(mask, m.features, shot_label=self.shot_label())),
                 "Build firmware image (checksums, encryption, self-check)", f"Save as {out}"]
        warn = [] if mask in m.tested else ["This combination has not been tested on a controller yet."]
        if not m.marker_tested:
            warn.append("New: the file carries a build marker (one descriptor byte, so this app can later see which "
                        "mods are installed). The mods are the tested code, but no marked build has run on this "
                        "model yet: check the buttons after flashing.")
        if out.exists():
            warn.append(f"{out.name} already exists in {short_path(out_dir)} and will be replaced.")
        if not self.confirm("Save modded firmware", names, [("Nothing is flashed.", curses.A_DIM)], warn, flashing=False):
            return
        steps = self.steps = Steps(names)
        try:
            image = self.base_and_build(steps, plan, mask)

            def save():
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("ota.bin", image)
            self.step(steps, 3, "Saving", save)
        except Exception as exc:
            self.stopped(steps, exc, "No file was saved.")
            return
        self.say(f"saved {out}")
        how = ("Flash it with 'Flash a firmware file as-is', or with the GameSir phone app" if m.can_flash
               else "Copy it to your phone and flash it with the GameSir phone app")
        self.finish(steps, "Saved", f"Saved {out}. {how} ('local file'; the version in the name makes the app "
                    "accept it).", True)

    def act_flash_file(self, c: Controller) -> None:
        p = self.pick_file()
        if p is None:
            return
        try:
            enc = read_image(p)
        except Exception as exc:
            self.info("Can't use this file", f"{p.name}: {exc}")
            return
        valid, desc, mask = describe_image(enc)
        if not valid:
            self.info("Can't flash this file", f"{p.name}: {desc}.")
            return
        lines = [(f"File: {p.name}", curses.A_BOLD), (f"Contents: {desc}", 0)]
        if mask is not None:
            lines += [(f"  {t}", curses.A_DIM) for t in mods_text(mask)]
        warn = []
        if mask is not None and mask not in TESTED:
            warn.append("This combination of mods has not been tested on a controller yet.")
        if re.search(r"(^|[^0-9.])5\.\d", p.name):
            warn.append("Version 5.x from GameSir REMOVES DS4 mode for good. Only continue if you want that.")
        if not_test_unit(c.addr):
            warn.append("This is not your test controller (see known_controllers.json).")
        names = ["Check the file: valid G8+ Bluetooth firmware", f"Flash it unchanged to controller {c.label}",
                 "Check the controller afterwards"]
        if (not self.can_flash(c) or not self.confirm("Flash a file", names, lines + self.pairing_note(c), warn, battery=c.battery)
                or not self.unpair_if_needed(c)):
            return
        steps = self.steps = Steps(names)
        steps.set(0, "done", desc)
        expect = mask if mask is not None else (None if is_stock(enc) else -1)
        try:
            path = write_cache("flash-as-is.bin", enc)
        except OSError as exc:
            self.stopped(steps, exc, "Nothing was flashed; the controller is unchanged.")
            return
        ok = self.flash_and_check(c, steps, 1, path, expect)
        self.finish(steps, "Done" if ok else "Problem", "Flashed." if ok else self.RECOVERY, ok)

    def set_model(self, model) -> None:
        if model is self.m:
            return
        self.m = model
        self.sources, self.src = [], 0
        self.refresh_sources()
        _have, inst = self.installed()
        self.choice = levels_of(inst, model.features) if inst is not None else [0] * len(model.features)
        self.say(f"controller type: {model.name}")

    def pick_model(self) -> None:
        lines = []
        for mdl in MODELS:
            lines += [(mdl.name, curses.A_BOLD), (f"  {mdl.blurb}", 0)]
            if mdl is MFI:
                lines.append((f"  USB: {model_mfi.usb_status(self.usb)[0]}", curses.A_DIM))
            elif self.ctls:
                lines.append((f"  Bluetooth: {len(self.ctls)} found in the last scan", curses.A_DIM))
            lines.append(("", 0))
        ans = self.dialog("Controller type", lines, [mdl.name for mdl in MODELS], MODELS.index(self.m))
        if ans is not None:
            self.set_model(MODELS[ans])

    def usb_scan(self, quiet: bool = False) -> None:
        self.usb = model_mfi.usb_devices()
        if not quiet:
            status, _fw, mask = model_mfi.usb_status(self.usb)
            self.say(f"USB: {status}")
            if mask is not None:
                self.choice = levels_of(mask, MFI.features)

    def change(self, kind, idx, step: int) -> None:
        if kind == "model":
            if step == 2:
                self.pick_model()
            else:
                self.set_model(MODELS[(MODELS.index(self.m) + (1 if step > 0 else -1)) % len(MODELS)])
        elif kind == "ctl" and self.ctls:
            self.sel = (self.sel + (1 if step > 0 else -1)) % len(self.ctls)
            if self.ctl.mask is not None:
                self.choice = levels_of(self.ctl.mask)
        elif kind == "src":
            if step == 2 and self.sources[self.src][0] == "choose":
                self.choose_source_file()
            else:
                self.src = (self.src + (1 if step > 0 else -1)) % len(self.sources)
        elif kind == "mod":
            n = len(self.m.features[idx][2])
            if self.m.features[idx][1]:  # slider: stops at the ends; Enter wraps around
                self.choice[idx] = (max(0, min(n - 1, self.choice[idx] + step)) if abs(step) == 1
                                    else (self.choice[idx] + 1) % n)
            else:
                was = self.choice[idx]
                shot_row = self.m is BT and idx == SHOT_FEATURE
                if shot_row and step == 2 and was == SHOT_CUSTOM:
                    self.ask_combo()  # Enter on "custom key" edits the key; Left/Right move on
                    return
                self.choice[idx] = (self.choice[idx] + (1 if step > 0 else -1)) % n
                if shot_row and self.choice[idx] == SHOT_CUSTOM and self.shot_custom is None and not self.ask_combo():
                    self.choice[idx] = was

    def check_updates(self) -> None:
        try:
            latest = self.working("Check for updates", "Asking GitHub", latest_release)
        except Exception as exc:  # offline, private repo, rate limit, bad reply
            self.info("Check for updates", f"Couldn't check ({exc}). You have {VERSION}. "
                      f"Releases: https://github.com/{REPO}/releases")
            return
        if version_key(latest) > version_key(VERSION):
            self.info("Update available", f"Version {latest} is available (you have {VERSION}). Get it with "
                      f"'git pull' in the app's folder, or from https://github.com/{REPO}/releases")
        else:
            self.info("Check for updates", f"You have the newest version ({VERSION}).")

    def run_action(self, name: str) -> bool:
        """Returns False to quit."""
        c = self.ctl
        if name == "Quit":
            return self.dialog("Quit", [("Quit the mod tool?", 0)], ["Cancel", "Quit"]) != 1
        self.busy_action = name
        try:
            if name == "Scan for controllers":
                self.scan()
            elif name == "Check for updates":
                self.check_updates()
            elif name == "Save modded firmware to a file":
                self.act_save()
            elif self.m is MFI and name == "Read controller again":
                self.usb_scan()
            elif c and name == "Read controller again":
                self.read(c)
            elif c and name == "Remove laptop pairing":
                self.unpair(c)
            elif c and name == "Mod and flash controller":
                self.act_mod_and_flash(c)
            elif c and name == "Reset controller to stock firmware":
                self.act_reset(c)
            elif c and name == "Flash a firmware file as-is":
                self.act_flash_file(c)
        finally:
            self.busy_action, self.steps = None, None
        return True

    def loop(self) -> None:
        first = ("act", "Scan for controllers") if self.m is BT else ("act", "Save modded firmware to a file")
        self.row = self.rows().index(first)
        while True:
            self.draw()
            rows = self.rows()
            kind, what = rows[self.row]
            k = self.scr.getch()
            if k == 27:
                if self.dialog("Quit", [("Quit the mod tool?", 0)], ["Cancel", "Quit"]) == 1:
                    return
                continue
            if k == curses.KEY_UP:
                self.row = (self.row - 1) % len(rows)
            elif k == curses.KEY_DOWN:
                self.row = (self.row + 1) % len(rows)
            elif k == curses.KEY_LEFT:
                self.change(kind, what, -1)
            elif k == curses.KEY_RIGHT:
                self.change(kind, what, +1)
            elif k in (10, 13, curses.KEY_ENTER):
                if kind == "act":
                    if not self.run_action(what):
                        return
                    rows = self.rows()  # keep the focus on the same row when rows appear / disappear
                    if (kind, what) in rows:
                        self.row = rows.index((kind, what))
                else:
                    self.change(kind, what, 2)  # Enter: next value, wrapping (or open the file picker)


def main() -> int:
    if "--version" in sys.argv:
        print(f"g8tui {VERSION} (beta)")
        return 0
    if "--selftest" in sys.argv:
        return selftest()
    try:
        curses.wrapper(lambda scr: App(scr).loop())
    finally:
        for link in LINKS:
            link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
