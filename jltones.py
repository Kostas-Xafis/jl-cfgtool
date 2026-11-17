#!/usr/bin/env python3
"""Work with the prompt tones inside a JieLi flash dump.

  extract IMAGE DIR         save every file of the 'tone' directory
  decode  FILE.wts OUT.wav  decode a .wts tone (SILK) to a WAV file
  quiet   IMAGE NEW_IMAGE   write a copy of the image with the tones made quieter

'quiet' keeps every file at its exact size and data CRC, so no file table has
to change, and it leaves alone any tone that shares a flash sector with
program code. Flash the result with:  jlcfg.py flash IMAGE NEW_IMAGE
"""
import argparse
import array
import ctypes
import ctypes.util
import os
import random
import subprocess
import sys
import tempfile
import wave

from jlcfg import SECTOR, crc16, directory, safe_region

MAX_FRAME = 78          # largest frame in the stock tones; bigger ones break playback
TOC_SILK_WB_20MS = 0x48
SILENT_8 = bytes.fromhex("80022614bb9c84")       # silent frames taken from a stock tone:
SILENT_9 = bytes.fromhex("8009008f3512ef98")     # 8 and 9 bytes long with their length byte


# --------------------------------------------------------------------------
# .wts = 1 header byte + length-prefixed Opus SILK frames (no TOC byte)
# --------------------------------------------------------------------------

def wts_frames(data):
    frames, i = [], 1
    while i < len(data):
        n = data[i]
        if i + 1 + n > len(data):
            raise ValueError("truncated frame")
        frames.append(data[i + 1:i + 1 + n])
        i += 1 + n
    return frames


def wts_pack(header_byte, frames):
    return bytes([header_byte]) + b"".join(bytes([len(f)]) + f for f in frames)


class Opus:
    def __init__(self, path=None):
        path = path or ctypes.util.find_library("opus")
        if not path:
            sys.exit("libopus not found; pass --opus-lib")
        self.lib = lib = ctypes.CDLL(path)
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
        lib.opus_packet_unpad.argtypes = [ctypes.c_char_p, ctypes.c_int32]

    def decode(self, frames):
        """Decode SILK wideband frames to 16 kHz mono samples."""
        err = ctypes.c_int()
        dec = self.lib.opus_decoder_create(16000, 1, ctypes.byref(err))
        pcm, buf = array.array("h"), (ctypes.c_int16 * 960)()
        for f in frames:
            pkt = bytes([TOC_SILK_WB_20MS]) + f
            n = self.lib.opus_decode(dec, pkt, len(pkt), buf, 960, 0)
            if n < 0:
                raise ValueError(f"opus_decode error {n}")
            pcm.extend(buf[:n])
        self.lib.opus_decoder_destroy(ctypes.c_void_p(dec))
        return pcm

    def encode(self, pcm, nframes, bitrate, vbr):
        """Encode to SILK-only wideband 20 ms frames of at most MAX_FRAME bytes, TOC removed."""
        err = ctypes.c_int()
        enc = self.lib.opus_encoder_create(16000, 1, 2048, ctypes.byref(err))  # VOIP
        for request, value in ((4002, bitrate), (4006, vbr), (4020, 0),   # bitrate, VBR, unconstrained
                               (4008, 1103), (4004, 1103),                # (max) bandwidth = wideband
                               (11002, 1000),                             # force mode = SILK only
                               (4016, 0), (4012, 0), (4010, 10)):         # no DTX, no FEC, complexity
            if self.lib.opus_encoder_ctl(ctypes.c_void_p(enc), ctypes.c_int(request), ctypes.c_int(value)) != 0:
                raise ValueError(f"opus_encoder_ctl({request}) failed")
        frames, buf = [], ctypes.create_string_buffer(400)
        try:
            for k in range(nframes):
                chunk = array.array("h", pcm[k * 320:(k + 1) * 320])
                chunk.extend([0] * (320 - len(chunk)))
                n = self.lib.opus_encode(enc, chunk.buffer_info()[0], 320, buf, MAX_FRAME + 1)
                if n <= 1:
                    return None
                n = self.lib.opus_packet_unpad(buf, n)
                if n <= 1 or buf.raw[0] != TOC_SILK_WB_20MS or n - 1 > MAX_FRAME:
                    return None
                frames.append(buf.raw[1:n])
        finally:
            self.lib.opus_encoder_destroy(ctypes.c_void_p(enc))
        return frames


def fillers(gap):
    """Fewest silent frames (8 and/or 9 bytes each) that fill exactly `gap` bytes, at least one."""
    best = None
    for nines in range(gap // 9 + 1):
        if (gap - 9 * nines) % 8 == 0:
            eights = (gap - 9 * nines) // 8
            if eights + nines >= 1 and (best is None or eights + nines < sum(best)):
                best = (eights, nines)
    return best


def force_crc(data, target):
    """Set the last two bytes so that crc16(data) == target."""
    data = bytearray(data)
    state = crc16(bytes(data[:-2]))
    for v in range(65536):
        tail = bytes([v >> 8, v & 0xFF])
        if crc16(tail, state) == target:
            data[-2:] = tail
            return bytes(data)
    raise ValueError("no solution")


def quiet_wts(opus, orig, gain_db):
    """Re-encode a .wts tone quieter, at the same size and CRC16. Returns (data, note)."""
    frames = wts_frames(orig)
    pcm = opus.decode(frames)
    gain = 10 ** (gain_db / 20)
    src = array.array("h", (int(round(s * gain)) for s in pcm))
    src.extend([0] * (len(frames) * 320 - len(src)))
    rnd = random.Random(1)
    # A quieter signal needs fewer bytes, and the file size must not change. The gap is closed
    # with a few frames of very faint noise (they encode large) plus silent stock frames.
    noise = array.array("h", (rnd.randint(-12, 12) for _ in range(320 * 40)))
    best = None
    for extra in range(41):
        for vbr in (0, 1):
            for bitrate in range(MAX_FRAME * 400, 9999, -400):
                enc = opus.encode(src + noise[:320 * extra], len(frames) + extra, bitrate, vbr)
                if enc is None:
                    continue
                gap = len(orig) - len(wts_pack(orig[0], enc))
                if gap < 8:
                    continue
                f = fillers(gap)
                if f and (best is None or extra + sum(f) < best[0]):
                    best = (extra + sum(f), enc, f)
                break
        if best and best[0] <= extra + 1:
            break
    if best is None:
        return None, "no encoding fits"
    added, enc, (eights, nines) = best
    new = force_crc(wts_pack(orig[0], enc + [SILENT_9] * nines + [SILENT_8] * eights), crc16(orig))
    out = wts_frames(new)
    if any(not 0x80 <= f[0] < 0xC0 for f in out):
        return None, ("the encoder marked frames as inactive; the device does not play those. "
                      "Use a libopus built with patches/opus-1.5.2-silk-always-active.patch (--opus-lib)")
    assert len(new) == len(orig) and max(map(len, out)) <= MAX_FRAME
    opus.decode(out)
    return new, f"+{added * 20} ms"


# --------------------------------------------------------------------------
# .sbc = raw SBC frames
# --------------------------------------------------------------------------

def sbc_frame_size(hdr):
    blocks = (4, 8, 12, 16)[(hdr[1] >> 4) & 3]
    mode = (hdr[1] >> 2) & 3
    subbands = 8 if hdr[1] & 1 else 4
    channels = 1 if mode == 0 else 2
    bitpool = hdr[2]
    if mode in (0, 1):
        bits = blocks * channels * bitpool
    else:
        bits = (mode == 3) * subbands + blocks * bitpool
    return 4 + (4 * subbands * channels) // 8 + -(-bits // 8)


def quiet_sbc(orig, gain_db):
    """Re-encode an SBC tone quieter with ffmpeg, at the same size and CRC16."""
    size = sbc_frame_size(orig)
    rate = (16000, 32000, 44100, 48000)[orig[1] >> 6]
    blocks = (4, 8, 12, 16)[(orig[1] >> 4) & 3]
    subbands = 8 if orig[1] & 1 else 4
    bitrate = size * 8 * rate // (blocks * subbands)
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.sbc"), os.path.join(tmp, "out.sbc")
        with open(src, "wb") as f:
            f.write(orig)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "sbc", "-i", src,
                            "-af", f"volume={gain_db}dB", "-c:a", "sbc", "-b:a", str(bitrate), "-f", "sbc", dst],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, "ffmpeg failed: " + r.stderr.strip()[:200]
        new = open(dst, "rb").read()
    if len(new) != len(orig) or sbc_frame_size(new) != size or new[2] != orig[2]:
        return None, "ffmpeg did not reproduce the original frame size"
    # The CRC is fixed up in the last two bytes, which are sample bits of the final frame.
    # That is inaudible as long as the tone ends in silence (small scale factors).
    if max(new[-size + 4:-size + 8]) > 0x40:
        return None, "the tone does not end in silence; cannot fix up its CRC inaudibly"
    return force_crc(new, crc16(orig)), "ok"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_extract(args):
    image = open(args.image, "rb").read()
    _, files = directory(image, "tone")
    os.makedirs(args.dir, exist_ok=True)
    for e, addr in files:
        data = image[addr:addr + e.size]
        with open(os.path.join(args.dir, e.name), "wb") as f:
            f.write(data)
        print(f"{e.name:16s} {e.size:7d} bytes at {addr:#x}  crc {'ok' if crc16(data) == e.data_crc else 'BAD'}")


def cmd_decode(args):
    pcm = Opus(args.opus_lib).decode(wts_frames(open(args.wts, "rb").read()))
    with wave.open(args.wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    print(f"{len(pcm) / 16000:.2f} s, peak {max(map(abs, pcm), default=0)}")


def cmd_quiet(args):
    image = bytearray(open(args.image, "rb").read())
    _, files = directory(image, "tone")
    start, end = safe_region(image)
    opus = None
    for e, addr in files:
        if (args.only and e.name not in args.only) or e.name in (args.skip or ()):
            continue
        ext = e.name.rsplit(".", 1)[-1]
        if ext not in ("wts", "sbc") or e.name.startswith("null"):
            continue
        orig = bytes(image[addr:addr + e.size])
        if crc16(orig) != e.data_crc:
            print(f"{e.name:16s} skipped: its CRC does not match (already modified some other way?)")
            continue
        if addr // SECTOR * SECTOR < start or addr + e.size > end:
            print(f"{e.name:16s} skipped: shares a flash sector with program code or a file table")
            continue
        if ext == "sbc":
            new, note = quiet_sbc(orig, args.gain)
        else:
            opus = opus or Opus(args.opus_lib)
            new, note = quiet_wts(opus, orig, args.gain)
        if new is None:
            print(f"{e.name:16s} skipped: {note}")
            continue
        image[addr:addr + e.size] = new
        print(f"{e.name:16s} {args.gain:+.0f} dB  ({note})")
    with open(args.out, "wb") as f:
        f.write(image)
    print(f"wrote {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opus-lib", help="path to libopus (a patched build is needed for 'quiet')")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract")
    p.add_argument("image")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("decode")
    p.add_argument("wts")
    p.add_argument("wav")
    p.set_defaults(fn=cmd_decode)
    p = sub.add_parser("quiet")
    p.add_argument("image", help="ORIGINAL dump (tones are always re-encoded from the stock audio)")
    p.add_argument("out")
    p.add_argument("--gain", type=float, default=-16.0, help="level change in dB (default -16)")
    p.add_argument("--only", nargs="+", metavar="NAME", help="only these tone files")
    p.add_argument("--skip", nargs="+", metavar="NAME", help="leave these tone files alone")
    p.set_defaults(fn=cmd_quiet)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
