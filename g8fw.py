#!/usr/bin/env python3
"""GameSir G8+ (G6470) firmware tool: decrypt, verify, and build patched images.

  python3 -I g8fw.py verify  stock.bin                 # check structure + all CRCs
  python3 -I g8fw.py decrypt stock.bin plain.bin
  python3 -I g8fw.py build   stock.bin out.bin [edits.txt ...]

Format (verified on v2.20, sha256 8e5d2d0c...):
- Whole file is Jieli SFC-encrypted in 32-byte blocks: LFSR poly 0x1021, seed = 0xA80F ^ (offset >> 2).
- Decrypted offset 0 holds 32-byte file entries {hdr_crc u16, data_crc u16, addr u32, len u32, attr..., name}.
  Entry 0 "app_area_head": data_crc = CRC16 over file[0x20 : len]  (len = end offset of the area).
  Entry 1 "app.bin" (file offset = addr), entry 2 "cfg_tool.bin": data_crc = CRC16 over their data.
  hdr_crc = CRC16 over the entry's bytes 2..31. CRC16 = XMODEM (poly 0x1021, init 0), over decrypted bytes.
- Runtime address = file offset + 0x1E00000 (flash XIP). Edits files use decrypted FILE offsets:
  lines "<hex offset>:<hex bytes>", '#' comments allowed.
Edits may only touch app.bin, outside the RAM-copied block unless deliberate (see RAM_COPY).
"""
import hashlib, struct, sys

POLY, KEY, BLK = 0x1021, 0xA80F, 32
STOCK_SHA256 = "8e5d2d0c49eecbc9e9bad78adf11e9e43a501c088693c9fdd4eea46978768160"
RAM_COPY = (0x51830, 0x51830 + 0x5FA4)   # flash range copied to RAM 0x0 at boot


def xor_block(block, off):
    out = bytearray(block); s = (KEY ^ (off >> 2)) & 0xFFFF
    for i in range(len(out)):
        out[i] ^= s & 0xFF
        s = ((s << 1) & 0xFFFF) ^ (POLY if s & 0x8000 else 0)
    return out


def crypt(data):
    out = bytearray(data)
    for off in range(0, len(data), BLK):
        out[off:off + BLK] = xor_block(data[off:off + BLK], off)
    return out


def crc16(b, c=0):
    for x in b:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ POLY) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def entries(p):
    out = []
    for i in range(3):
        e = p[i * 32:(i + 1) * 32]
        hc, dc, addr, ln = struct.unpack('<HHII', e[:12])
        out.append(dict(i=i, hc=hc, dc=dc, addr=addr, len=ln, name=e[16:].split(b'\0')[0].decode()))
    return out


def data_range(e):
    if e['i'] == 0:              # len is the end offset of the whole area, measured from 0
        return 0x20, e['len']
    return e['addr'], e['addr'] + e['len']


def check(p, quiet=False):
    """CRCs of all three entries, plus the expected layout (names, ranges inside the file, area length)."""
    if len(p) < 96:
        return False
    try:
        es = entries(p)
    except (UnicodeDecodeError, struct.error):
        return False
    # The area (entry 0) may end before the file does: stock 2.20 has 0x8770 bytes after it, outside every CRC.
    if [e['name'] for e in es] != ['app_area_head', 'app.bin', 'cfg_tool.bin'] or es[0]['len'] > len(p):
        return False
    for e in es:
        s, t = data_range(e)
        if not (0x20 <= s <= t <= len(p)):
            return False
    ok = True
    for e in entries(p):
        s, t = data_range(e)
        hc = crc16(p[e['i'] * 32 + 2:(e['i'] + 1) * 32]); dc = crc16(p[s:t])
        good = hc == e['hc'] and dc == e['dc']
        ok &= good
        if not quiet:
            print(f"  {e['name']:14s} data {s:#07x}-{t:#07x}  hdr {e['hc']:04x}/{hc:04x}  data {e['dc']:04x}/{dc:04x}  {'OK' if good else 'BAD'}")
    return ok


def fix_crcs(p):
    es = entries(p)
    for i in (2, 1, 0):          # inner entries first: entry 0's data range covers entries 1-2
        e = es[i]; s, t = data_range(e)
        struct.pack_into('<H', p, i * 32 + 2, crc16(p[s:t]))
        struct.pack_into('<H', p, i * 32, crc16(p[i * 32 + 2:(i + 1) * 32]))
        es = entries(p)


def read_edits(paths):
    edits = []
    for path in paths:
        for raw in open(path):
            line = raw.split('#')[0].strip()
            if not line:
                continue
            off, hexbytes = line.split(':')
            edits.append((int(off, 16), bytes.fromhex(hexbytes)))
    edits.sort()
    for (a, da), (b, _) in zip(edits, edits[1:]):
        if a + len(da) > b:
            sys.exit(f"edits overlap at {b:#x}")
    return edits


def apply_edits(plain, edits, note=lambda msg: None):
    """Step 1 of a build: copy of the decrypted stock image with the edits applied (app.bin only)."""
    edits = sorted(edits)
    for (a, da), (b, _) in zip(edits, edits[1:]):
        if a + len(da) > b:
            raise ValueError(f"edits overlap at {b:#x}")
    app = entries(plain)[1]; a0, a1 = data_range(app)
    p = bytearray(plain)
    for off, data in edits:
        if not (a0 <= off and off + len(data) <= a1):
            raise ValueError(f"edit at {off:#x} is outside app.bin ({a0:#x}-{a1:#x})")
        if off < RAM_COPY[1] and off + len(data) > RAM_COPY[0]:
            note(f"note: edit at {off:#x} is in the RAM-copied block (runs at RAM {off - RAM_COPY[0]:#x})")
        p[off:off + len(data)] = data
    return p


def seal(plain, p, edits):
    """Step 2 of a build: fix the CRCs, encrypt, and self-check (decrypts back, CRCs valid, nothing changed
    outside the edits + CRC fields). Returns the encrypted image."""
    fix_crcs(p)
    out = crypt(p)
    # Real checks, not asserts (python -O would strip asserts).
    if crypt(out) != p:
        raise RuntimeError("round trip failed")
    if not check(p, quiet=True):
        raise RuntimeError("CRC check failed")
    allowed = set(range(0, 4)) | set(range(0x20, 0x24))
    for off, data in edits:
        allowed |= set(range(off, off + len(data)))
    stray = [i for i in range(len(p)) if p[i] != plain[i] and i not in allowed]
    if stray:
        raise RuntimeError(f"unexpected changes at {stray[:5]}")
    return out


def main():
    cmd, src = sys.argv[1], sys.argv[2]
    enc = open(src, 'rb').read()
    sha = hashlib.sha256(enc).hexdigest()
    plain = crypt(enc)
    if cmd == 'verify':
        print(f"{src}: sha256 {sha}{' (stock v2.20)' if sha == STOCK_SHA256 else ''}")
        sys.exit(0 if check(plain) else 1)
    if cmd == 'decrypt':
        open(sys.argv[3], 'wb').write(plain); return
    if cmd != 'build':
        sys.exit(__doc__)
    if sha != STOCK_SHA256:
        sys.exit(f"{src} is not the stock v2.20 image (sha256 {sha})")
    out_path, edits = sys.argv[3], read_edits(sys.argv[4:])
    try:
        p = apply_edits(plain, edits, note=print)
        out = seal(plain, p, edits)
    except ValueError as exc:
        sys.exit(str(exc))
    open(out_path, 'wb').write(out)
    changed = sum(1 for x, y in zip(plain, p) if x != y)
    print(f"wrote {out_path}: {len(edits)} edits, {changed} plain bytes changed, sha256 {hashlib.sha256(out).hexdigest()}")
    check(p)


if __name__ == '__main__':
    main()
