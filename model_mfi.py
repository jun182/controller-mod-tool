"""GameSir G8+ MFi (wired USB) model for the mod tool: mods, build, file check and a read-only USB look.

Build-only (user decision 2026-11-22): the app saves G8-Plus-MFi-firmware-<ver>.zip and the user flashes it with the
GameSir phone app ("local file"). Nothing here sends anything to the controller.

Build marker: hat physical max in the Android-mode descriptor, area offset 0x30a36 (stock `46 3b 01`). Our builds
write 0x20 | mask. 0x3b = stock 8900 or a build without a marker (e.g. our 9106/9107 test builds).
"""

import hashlib
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent / "mfi"))  # private repo layout; in the release repo mfifw.py sits next to this
import mfifw  # noqa: E402

KEY = "mfi"
NAME = "G8+ MFi (wired)"
BLURB = "USB cable only. The app builds a firmware file; you flash it with the GameSir phone app."
BASE = "8900"
STOCK_NAME = "G8-Plus-MFi-firmware-8900.zip"
STOCK_URL = __import__("base64").b64decode("aHR0cHM6Ly9vdGEueGlhb2ppLmNvbS9kb3dubG9hZC8=").decode() + STOCK_NAME
IMAGE_SIZE = mfifw.IMAGE_SIZE
CAN_FLASH = False
MARKER_TESTED = True  # 9903 (mask 3, marker 0x23) flashed 2026-11-22: buttons OK on phone + Linux, marker read back over USB

MARKER_OFFSET = 0x30a36
MARKER_STOCK, MARKER_BASE = 0x3B, 0x20
HAT_ITEM = re.compile(rb"\x35\x00\x46(.)\x01\x65\x14", re.S)

PADDLES, HOME, STICKAVG = 1, 2, 4
MODS_DIRS = [TOOLS / "mods/mfi", TOOLS.parent / "mfi/v9106", TOOLS.parent / "mfi/v9107"]
EDITS = {PADDLES: ["edits.txt"], HOME: ["edits_home.txt"], STICKAVG: ["edits_stick_avg.txt"]}  # the files the tested 9106 / 9107 were built from
TESTED = {0: "stock", PADDLES: "9106", PADDLES | HOME: "9107, 9903 (marked, 2026-11-22)"}
FEATURES = [
    ("Back buttons", False, [
        (0, "Stock back buttons", "copy a face button (programmable)", None),
        (PADDLES, "Independent back buttons", "L4 = BUTTON_C, R4 = BUTTON_Z (Android/HID mode)", None),
    ]),
    ("Home button", False, [
        (0, "Stock Home button", "Home is held back ~0.8 s; Home + D-pad = volume", None),
        (HOME, "Remove Home button delay", "Home is sent at once; the Home + D-pad volume shortcut is removed", None),
    ]),
    ("Stick smoothing", False, [
        (0, "Stock stick smoothing", "filter 1 averages each stick move with the previous value (est. 1-3 ms lag)",
         None),
        (STICKAVG, "Stick averaging off", "filter 1 passes stick (and trigger) moves straight through (est. 1-3 ms "
         "faster); its tiny noise deadband and the later filters stay", None),
    ]),
]
FASTER_NOTE = ""
USB_VID = "3537"
USB_PID_ANDROID = "0112"
USB_PID_BOOT = "2001"
HID_ROOT = Path("/sys/bus/hid/devices")


def is_stock(enc: bytes) -> bool:
    return hashlib.sha256(enc).hexdigest() == mfifw.STOCK_SHA256


def marker_of(mask: int) -> int:
    return MARKER_BASE | mask


def all_masks() -> int:
    m = 0
    for _t, _s, levels in FEATURES:
        for bits, *_ in levels:
            m |= bits
    return m


def mask_from_report_map(report_map: bytes) -> tuple[int | None, str]:
    m = HAT_ITEM.search(report_map)
    if not m:
        return None, "unknown (no hat descriptor found)"
    b = m.group(1)[0]
    if b == MARKER_STOCK:
        return None, f"GameSir stock {BASE}, or a mod build without a marker (e.g. 9106/9107)"
    if b & 0xF0 == MARKER_BASE and b & 0x0F and not b & 0x0F & ~all_masks():
        return b & 0x0F, f"Modified firmware (GameSir {BASE} + mods)"
    return None, f"Unknown firmware (marker 0x{b:02x}, newer than this tool?)"


def describe_image(enc: bytes) -> tuple[bool, str, int | None]:
    """(valid G8+ MFi image based on 8900?, description, mods mask or None)."""
    if is_stock(enc):
        return True, f"official GameSir {BASE}, unmodified", None
    if len(enc) != IMAGE_SIZE:
        return False, f"not a G8+ MFi firmware based on {BASE} (size {len(enc)}, expected {IMAGE_SIZE})", None
    try:
        plain = mfifw.decode(enc)
        if not mfifw.check(plain, quiet=True):
            return False, "not a valid G8+ MFi firmware (checksums don't match)", None
    except Exception:
        return False, "not a G8+ MFi firmware file", None
    hat = bytes(plain[MARKER_OFFSET - 3:MARKER_OFFSET + 4])
    if not HAT_ITEM.fullmatch(hat):
        return False, f"not a G8+ MFi firmware based on {BASE} (descriptor not where expected)", None
    mask, desc = mask_from_report_map(hat)
    return True, desc, mask


def edit_file(name: str) -> str:
    for d in MODS_DIRS:
        if (d / name).is_file():
            return str(d / name)
    raise FileNotFoundError(f"mod file {name} not found (looked in {', '.join(map(str, MODS_DIRS))})")


def build_bytes(stock_enc: bytes, mask: int, marker: int | None, step=lambda name: None) -> bytes:
    files = [edit_file(f) for bit, fs in EDITS.items() if mask & bit for f in fs]
    edits = mfifw.read_edits(files) if files else []
    if marker is not None:
        edits = edits + [(MARKER_OFFSET, bytes([marker]))]
    step("build")
    return mfifw.build(stock_enc, edits)


def save_name(mask: int) -> str:
    # The phone app only takes a file whose version is above the installed one; 99xx is above stock 8900 and all of
    # our test builds (91xx, 9800).
    return f"G8-Plus-MFi-firmware-99{mask:02d}.zip"


def usb_devices() -> list[dict]:
    """GameSir USB HID devices, from the kernel's sysfs (no I/O to the controller at all). For Android/HID mode
    (3537:0112) the report descriptor the kernel already read is parsed for the build marker."""
    out = {}
    try:
        devs = sorted(HID_ROOT.iterdir())
    except OSError:
        return []
    for d in devs:
        parts = d.name.split(":")  # 0003:3537:0112.000A
        if len(parts) != 3 or parts[1].upper() != USB_VID:
            continue
        pid = parts[2].split(".")[0].upper()
        name = ""
        try:
            for line in (d / "uevent").read_text().splitlines():
                if line.startswith("HID_NAME="):
                    name = line[9:]
            desc = (d / "report_descriptor").read_bytes()
        except OSError:
            continue
        dev = out.setdefault(pid, {"pid": pid, "name": name, "mask": None, "firmware": None})
        if pid == USB_PID_ANDROID and HAT_ITEM.search(desc):  # the gamepad interface (the other one is vendor-only)
            dev["mask"], dev["firmware"] = mask_from_report_map(desc)
    return list(out.values())


def usb_status(devs: list[dict]) -> tuple[str, str | None, int | None]:
    """(one-line USB status, firmware description or None, mods mask or None)."""
    if not devs:
        return "no G8+ MFi on USB (plug it in; after ~5 s it switches to Android mode by itself)", None, None
    for d in devs:
        if d["pid"] == USB_PID_ANDROID:
            return f"connected in Android/HID mode ({USB_VID}:{d['pid']} {d['name']})", d["firmware"], d["mask"]
    d = devs[0]
    if d["pid"] == USB_PID_BOOT:
        return f"in update (bootloader) mode ({USB_VID}:{d['pid']}): finish or retry the update in the phone app", \
            None, None
    return (f"connected in another mode ({USB_VID}:{d['pid']} {d['name']}); switch to Android mode to read its "
            "firmware"), None, None


def selftest(stock: bytes) -> bool:
    ok = True
    root = TOOLS.parent / "mfi"
    for mask, ref in ((PADDLES, "v9106/ota.bin"), (PADDLES | HOME, "v9107/ota.bin")):
        if not (root / ref).is_file():
            print(f"MFi mods {mask:x}: skipped, mfi/{ref} not here (firmware files aren't in git)")
            continue
        same = build_bytes(stock, mask, None) == (root / ref).read_bytes()
        print(f"MFi mods {mask:x} -> mfi/{ref}: {'identical' if same else 'DIFFERENT'}")
        ok &= same
    for mask in range(1, all_masks() + 1):
        valid, _desc, back = describe_image(build_bytes(stock, mask, marker_of(mask)))
        print(f"MFi mods {mask:x}: valid {valid}, marker reads back as {back}")
        ok &= valid and back == mask
    valid, desc, back = describe_image(stock)
    ok &= valid and back is None
    return ok
