#!/usr/bin/env python3
"""Client for the JieLi SDK "config tool" protocol (USB CDC ACM / UART).

Lets you identify a device, dump its flash, and rewrite sectors of the
resource area (prompt tones, config, EQ) on devices that ship with the
config tool channel enabled.

Usage: jlcfg.py [-p PORT] {info,dump,ls,verify,flash} ...
"""
import argparse
import os
import re
import select
import struct
import sys
import termios
import time

SECTOR = 4096
CHUNK = 32          # write chunk; keeps the firmware's 32-byte cipher grid
READ_CHUNK = 128

CMD_FILE_SIZE = 0x0B
CMD_FILE_READ = 0x0C
CMD_PREPARE_WRITE = 0x22
CMD_BASIC_INFO = 0x23
CMD_ERASE = 0x24
CMD_WRITE = 0x25
CMD_READ = 0x27


def crc16(data, crc=0):
    """CRC16-CCITT (poly 0x1021), as used all over JieLi firmware."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------

class CfgTool:
    def __init__(self, port="/dev/ttyACM0"):
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0                      # raw
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.sq = 1
        self._drain(0.2)

    def close(self):
        os.close(self.fd)

    def _drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                os.read(self.fd, 65536)

    def command(self, cmd, payload=b"", timeout=3.0):
        """Send one request, return the reply data (or None on timeout/bad reply)."""
        sq = self.sq
        self.sq = self.sq % 250 + 1
        body = bytes([6 + len(payload), 0x12, sq]) + struct.pack("<I", cmd) + payload
        os.write(self.fd, b"\x5a\xaa\xa5" + struct.pack("<H", crc16(body)) + body)
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            if select.select([self.fd], [], [], 0.02)[0]:
                buf += os.read(self.fd, 65536)
            i = buf.find(b"\x5a\xaa\xa5")
            if i >= 0 and len(buf) >= i + 6 and len(buf) >= i + 6 + buf[i + 5]:
                reply = buf[i + 5:i + 6 + buf[i + 5]]
                if crc16(reply) == struct.unpack("<H", buf[i + 3:i + 5])[0] and reply[2] == sq:
                    return reply[3:]
                return None
        return None

    def info(self):
        d = self.command(CMD_BASIC_INFO)
        if d is None or len(d) < 98:
            return None

        def text(b):
            return b.split(b"\0")[0].decode("latin1")
        return {"protocol": struct.unpack("<H", d[:2])[0], "prog_crc": text(d[2:34]),
                "sdk": text(d[34:66]), "pid": text(d[66:82]), "vid": text(d[82:98])}

    def file_size(self, file_id):
        d = self.command(CMD_FILE_SIZE, struct.pack("<I", file_id))
        return struct.unpack("<I", d)[0] if d and len(d) == 4 else None

    def prepare_write(self, file_id):
        """Returns (flash address, file size, erase unit) or None."""
        d = self.command(CMD_PREPARE_WRITE, struct.pack("<II", file_id, 0))
        return struct.unpack("<III", d) if d and len(d) == 12 else None

    def read(self, addr, size, progress=None):
        out = bytearray()
        while len(out) < size:
            n = min(READ_CHUNK, size - len(out))
            for _ in range(8):
                d = self.command(CMD_READ, struct.pack("<II", addr + len(out), n))
                if d is not None and len(d) == n:
                    break
                self._drain(0.3)
            else:
                raise IOError(f"read failed at {addr + len(out):#x}")
            out += d
            if progress and len(out) % 0x10000 < READ_CHUNK:
                progress(len(out), size)
        return bytes(out)

    def erase(self, addr, size):
        d = self.command(CMD_ERASE, struct.pack("<II", addr, size), timeout=5.0)
        return bool(d) and d[:2] == b"OK"

    def write(self, addr, data):
        d = self.command(CMD_WRITE, struct.pack("<II", addr, len(data)) + data)
        return bool(d) and d[:2] == b"OK"

    def write_sector(self, addr, data):
        """Erase one sector, program it in 32-byte chunks, read it back."""
        assert addr % SECTOR == 0 and len(data) == SECTOR
        if not self.erase(addr, SECTOR):
            return False
        for off in range(0, SECTOR, CHUNK):
            for _ in range(3):
                if self.write(addr + off, data[off:off + CHUNK]):
                    break
            else:
                return False
        return self.read(addr, SECTOR) == data


# --------------------------------------------------------------------------
# Flash image helpers
# --------------------------------------------------------------------------

class Entry:
    """A 32-byte file header: hdr_crc, data_crc, offset, size, attr, -, index, name[16]."""

    def __init__(self, image, pos):
        self.pos = pos
        self.valid = False
        if pos < 0 or pos + 32 > len(image):
            return
        self.hdr_crc, self.data_crc, self.offset, self.size, self.attr = struct.unpack("<HHIIB", image[pos:pos + 13])
        self.name = image[pos + 16:pos + 32].split(b"\0")[0].decode("latin1")
        self.valid = self.hdr_crc == crc16(image[pos + 2:pos + 32]) and bool(self.name)


def find_entries(image):
    """Find every valid file header in an image (they are not all in one table)."""
    found, seen = [], set()
    for m in re.finditer(rb"[\x21-\x7e]{2,15}\x00", image):
        for back in range(14):
            pos = m.start() - back - 16
            if pos < 0 or pos in seen:
                continue
            e = Entry(image, pos)
            if e.valid:
                seen.add(pos)
                found.append(e)
                break
    return found


def find_entry(image, name):
    for e in find_entries(image):
        if e.name == name:
            return e
    return None


def directory(image, name):
    """Files of a resource directory such as 'tone': list of (entry, absolute data address)."""
    d = find_entry(image, name)
    if d is None:
        raise ValueError(f"no '{name}' directory in this image")
    files, pos, end = [], d.pos + 32, None
    while end is None or pos < end:
        e = Entry(image, pos)
        if not e.valid:
            break
        if end is None:
            end = d.pos + e.offset          # the first file's data follows the header list
        files.append((e, d.pos + e.offset))
        pos += 32
    return d, files


def safe_region(image):
    """Sector range that holds only resources: after app.bin, before the VM area."""
    head = find_entry(image, "app_area_head")
    app = find_entry(image, "app.bin")
    if head is None or app is None:
        raise ValueError("cannot locate app.bin in this image")
    app_end = head.pos + app.offset + app.size
    start = -(-app_end // SECTOR) * SECTOR
    vm = find_entry(image, "VM")
    end = vm.offset // SECTOR * SECTOR if vm and vm.offset > start else len(image)
    return start, end


def changed_sectors(old, new):
    return [s for s in range(0, len(new), SECTOR) if old[s:s + SECTOR] != new[s:s + SECTOR]]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cmd_info(args):
    dev = CfgTool(args.port)
    info = dev.info()
    if info is None:
        sys.exit("no reply: this port does not speak the config tool protocol")
    for k, v in info.items():
        print(f"{k:9s} {v}")
    for fid, name in ((0, "cfg_tool.bin"), (1, "eq_cfg_hw.bin (old)"), (2, "effects_cfg.bin"), (3, "eq_cfg_hw.bin")):
        size = dev.file_size(fid)
        print(f"file {fid}    {name}: {'not present' if size is None else str(size) + ' bytes'}")


def cmd_dump(args):
    dev = CfgTool(args.port)
    size = int(args.size, 0)
    t = time.time()
    first = dev.read(0, size, lambda done, total: print(f"\r{done * 100 // total}%", end="", flush=True))
    print("\rread once, reading again to compare...")
    if dev.read(0, size) != first:
        sys.exit("the two reads differ; not saving")
    with open(args.out, "wb") as f:
        f.write(first)
    print(f"saved {size} bytes to {args.out} in {time.time() - t:.0f}s")


def cmd_ls(args):
    image = open(args.image, "rb").read()
    print(f"{'header':>9} {'offset':>10} {'size':>8} attr name")
    for e in find_entries(image):
        print(f"{e.pos:#9x} {e.offset:#10x} {e.size:8d} {e.attr:#04x} {e.name}")
    try:
        start, end = safe_region(image)
        print(f"\nresource-only sectors (safe to rewrite): {start:#x}..{end:#x}")
    except ValueError as err:
        print("\n" + str(err))


def cmd_verify(args):
    image = open(args.image, "rb").read()
    start, end = safe_region(image)
    dev = CfgTool(args.port)
    got = dev.read(start, end - start)
    bad = [hex(start + s) for s in changed_sectors(image[start:end], got)]
    print(f"resource area {start:#x}..{end:#x}:", "matches the image" if not bad else f"differs in sectors {bad}")
    sys.exit(1 if bad else 0)


def cmd_flash(args):
    cur = open(args.current, "rb").read()
    new = open(args.new, "rb").read()
    if len(cur) != len(new):
        sys.exit("the two images differ in size")
    sectors = changed_sectors(cur, new)
    if not sectors:
        sys.exit("the images are identical, nothing to do")
    start, end = safe_region(cur)
    outside = [hex(s) for s in sectors if not start <= s < end]
    if outside and not args.force:
        sys.exit(f"refusing: sectors {outside} are outside the resource-only area {start:#x}..{end:#x}\n"
                 "(they hold program code or file tables; a failed write there can brick the device)")
    print(f"{len(sectors)} sector(s) to rewrite between {sectors[0]:#x} and {sectors[-1] + SECTOR:#x}")
    if args.dry_run:
        return
    if not args.yes and input("This erases and rewrites flash on the device. Type 'yes' to go on: ") != "yes":
        sys.exit("aborted")

    def connect():
        for _ in range(60):
            try:
                if os.access(args.port, os.W_OK):
                    dev = CfgTool(args.port)
                    # Preparing a file other than cfg_tool.bin (id 0) sets the erase unit
                    # without arming the firmware's reset-after-write.
                    for fid in (3, 1, 2):
                        p = dev.prepare_write(fid)
                        if p:
                            if p[2] != SECTOR:
                                sys.exit(f"erase unit is {p[2]}, this tool only handles {SECTOR}")
                            return dev
                    sys.exit("no file other than cfg_tool.bin to prepare with; not going on")
            except OSError as err:
                log("waiting for the port:", err)
            time.sleep(2)
        sys.exit("could not reach the device")

    dev = connect()
    for n, s in enumerate(sectors, 1):
        want, was = new[s:s + SECTOR], cur[s:s + SECTOR]
        for attempt in range(4):
            try:
                have = dev.read(s, SECTOR)
                if have == want:
                    break
                if have != was and attempt == 0:
                    log(f"note: sector {s:#x} holds neither the current nor the new image")
                if dev.write_sector(s, want):
                    break
                log(f"sector {s:#x}: verify failed (attempt {attempt + 1})")
            except OSError as err:
                log(f"sector {s:#x}: {err}; reconnecting")
                time.sleep(3)
                dev = connect()
        else:
            log(f"giving up on sector {s:#x}; putting its previous content back")
            for _ in range(3):
                try:
                    if dev.write_sector(s, was):
                        log("restored")
                        break
                except OSError:
                    time.sleep(3)
                    dev = connect()
            sys.exit("FAILED")
        log(f"sector {s:#x} ok ({n}/{len(sectors)})")
    ok = dev.read(start, end - start) == new[start:end]
    log("resource area matches the new image" if ok else "MISMATCH after writing")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="identify the device").set_defaults(fn=cmd_info)
    p = sub.add_parser("dump", help="read the whole flash into a file (read-only)")
    p.add_argument("out")
    p.add_argument("--size", default="0x200000", help="flash size in bytes (default 2 MiB)")
    p.set_defaults(fn=cmd_dump)
    p = sub.add_parser("ls", help="list the files inside a dump")
    p.add_argument("image")
    p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("verify", help="compare the device's resource area with an image (read-only)")
    p.add_argument("image")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("flash", help="rewrite the sectors that differ between two images")
    p.add_argument("current", help="image of what is on the device now")
    p.add_argument("new", help="image to end up with")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--force", action="store_true", help="allow sectors outside the resource-only area")
    p.set_defaults(fn=cmd_flash)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
