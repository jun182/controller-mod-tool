#!/usr/bin/env python3
"""GameSir G8+ MFi firmware tool: verify, decrypt and build patched ota.bin images.

  python3 -I mfifw.py verify  ota.bin
  python3 -I mfifw.py decrypt ota.bin area.bin          # decrypted program area (offset 0 = area start)
  python3 -I mfifw.py build   ota.bin out.bin [edits.txt ...]

Format (verified on 8900 and 9011, 2026-11-19):
- 0x00..0x20: plain header ("G8MF", "AC695X_TRANS"); it doesn't cover the program area.
- 0x20..0x5000: loader table (uboot.boot, isd_config.ini, app_dir_head). Entries are SFC-encrypted with a
  fixed seed of 0xFFFF per 32-byte block. Left untouched.
- 0x5000..: program area. SFC with key 0x3496, seed = key ^ (offset_from_area_start >> 2),
  LFSR poly 0x1021, 32-byte blocks. Entries {hdr_crc, data_crc, addr u32, len u32, ..., name}:
    app_area_head (data CRC over area[0x20:len], len = end of area), app.bin (+0x120), cfg_tool.bin, VM.
  CRC16-XMODEM over decrypted bytes; hdr_crc covers entry bytes 2..31.
- Runtime address = area offset + 0x1E00000 (chip BR23 / AC635x, pi32v2 CPU).
Edit lines are "<hex area offset>:<hex bytes>"; '#' starts a comment. `build` only accepts the official 8900
image (sha256 STOCK_SHA256): every edit file is written for it.
"""
import hashlib, struct, sys

POLY, KEY, BLK, AREA = 0x1021, 0x3496, 32, 0x5000


def crypt(data, key=KEY):
    out = bytearray(data)
    for o in range(0, len(out), BLK):
        s = (key ^ (o >> 2)) & 0xFFFF
        for i in range(o, min(o + BLK, len(out))):
            out[i] ^= s & 0xFF
            s = ((s << 1) & 0xFFFF) ^ (POLY if s & 0x8000 else 0)
    return out


def crc16(b, c=0):
    for x in b:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ POLY) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def entries(a):
    out = []
    for i in range(3):
        e = a[i * 32:(i + 1) * 32]
        hc, dc, addr, ln = struct.unpack('<HHII', e[:12])
        out.append(dict(i=i, hc=hc, dc=dc, addr=addr, len=ln, name=e[16:].split(b'\0')[0].decode()))
    return out


def rng(e):
    return (0x20, e['len']) if e['i'] == 0 else (e['addr'], e['addr'] + e['len'])


STOCK_SHA256 = "3c35f03453e97eedcc0e47bc59423f8c4c27d242e651918114737a98c0774fc5"  # official 8900 ota.bin
IMAGE_SIZE = 258048


def decode(d):
    """Encrypted ota.bin bytes -> decrypted program area. Raises ValueError if it isn't a G8+ MFi image."""
    if len(d) < AREA + 3 * 32 or d[4:8] != b'G8MF':
        raise ValueError('not a G8+ MFi ota.bin')
    a = crypt(d[AREA:])
    if entries(a)[0]['name'] != 'app_area_head':
        raise ValueError('G8+ MFi program area did not decrypt')
    return a


def load(path):
    d = open(path, 'rb').read()
    return d, decode(d)


def check(a, quiet=False):
    ok = True
    for e in entries(a):
        lo, hi = rng(e)
        h, c = crc16(a[e['i'] * 32 + 2:(e['i'] + 1) * 32]), crc16(a[lo:hi])
        good = h == e['hc'] and c == e['dc']
        ok &= good
        if not quiet:
            print('  %-14s data 0x%05x-0x%05x  hdr %04x/%04x  data %04x/%04x  %s'
                  % (e['name'], lo, hi, e['hc'], h, e['dc'], c, 'OK' if good else 'BAD'))
    return ok


def fix(a):
    ents = entries(a)
    for i in (1, 2, 0):          # app.bin and cfg data+hdr first, area last (it covers them)
        e = ents[i]; lo, hi = rng(e)
        struct.pack_into('<H', a, i * 32 + 2, crc16(a[lo:hi]))
        struct.pack_into('<H', a, i * 32, crc16(a[i * 32 + 2:(i + 1) * 32]))


def read_edits(paths):
    """Edit files -> sorted [(area offset, bytes)]. Raises ValueError on a bad line or overlapping edits."""
    edits = []
    for path in paths:
        for n, raw in enumerate(open(path), 1):
            line = raw.split('#')[0].strip()
            if not line:
                continue
            try:
                off, hx = line.split(':')
                edits.append((int(off, 16), bytes.fromhex(hx)))
            except ValueError:
                raise ValueError(f'{path} line {n}: expected "<hex offset>:<hex bytes>"') from None
    return check_edits(edits)


def check_edits(edits):
    edits = sorted(edits)
    for (a, da), (b, _) in zip(edits, edits[1:]):
        if a + len(da) > b:
            raise ValueError(f'edits overlap at {b:#x}')
    return edits


def build(d, edits):
    """Stock ota.bin bytes + edits -> new ota.bin bytes. Real checks (no asserts): stock sha256, edits inside app.bin
    and not overlapping, CRCs valid, round trip, and nothing changed outside the edits and the CRC fields."""
    if hashlib.sha256(d).hexdigest() != STOCK_SHA256:
        raise ValueError('base is not the official G8+ MFi 8900 firmware')
    plain = decode(d)
    edits = check_edits(edits)
    app = entries(plain)[1]
    a = bytearray(plain)
    for off, b in edits:
        if not (app['addr'] <= off and off + len(b) <= app['addr'] + app['len']):
            raise ValueError(f'edit at {off:#x} is outside app.bin')
        a[off:off + len(b)] = b
    fix(a)
    end = entries(a)[0]['len']
    out = d[:AREA] + bytes(crypt(a[:end])) + d[AREA + end:]
    if len(out) != len(d):
        raise RuntimeError('size changed')
    back = decode(out)
    if back != a or not check(back, quiet=True):
        raise RuntimeError('round trip or CRC check failed')
    allowed = {i * 32 + k for i in range(3) for k in range(4)}  # hdr_crc + data_crc of the 3 entries
    for off, b in edits:
        allowed |= set(range(off, off + len(b)))
    stray = [i for i in range(len(a)) if a[i] != plain[i] and i not in allowed]
    if stray:
        raise RuntimeError(f'unexpected changes at {stray[:5]}')
    return out


def main():
    cmd, src = sys.argv[1], sys.argv[2]
    try:
        d, a = load(src)
    except ValueError as exc:
        sys.exit(f'{src}: {exc}')
    if cmd == 'verify':
        sha = hashlib.sha256(d).hexdigest()
        print(src, 'sha256', sha, '(stock 8900)' if sha == STOCK_SHA256 else '')
        sys.exit(0 if check(a) else 1)
    if cmd == 'decrypt':
        open(sys.argv[3], 'wb').write(a); return
    if cmd == 'build':
        try:
            out = build(d, read_edits(sys.argv[4:]))
        except (ValueError, RuntimeError) as exc:
            sys.exit(str(exc))
        open(sys.argv[3], 'wb').write(out)
        print(sys.argv[3], 'sha256', hashlib.sha256(out).hexdigest())
        check(decode(out))
        return
    sys.exit(__doc__)


if __name__ == '__main__':
    main()
