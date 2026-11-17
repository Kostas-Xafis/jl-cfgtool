# jl-cfgtool

Make the startup, shutdown and Bluetooth prompt sounds of a **Xiaomi Sound Outdoor** speaker quieter,
over a plain USB-C cable, without opening it.

More generally: a small Python client for the "config tool" protocol that JieLi's SDK exposes on USB,
plus tools for the prompt tones stored in a JieLi flash image. It may work on other JieLi-based
speakers that ship with that channel enabled, but it has only been tried on one.

> **This rewrites flash memory on your device.** It worked on the one speaker it was developed on.
> A failed or interrupted write in the wrong place can leave a device that no longer boots, and
> there is no recovery short of JieLi's hardware programmer. Read the whole page first, keep your
> dump, and do not use it on something you cannot afford to lose.

## Why this exists

Xiaomi's speakers play their prompts at a fixed, loud level and offer no setting for it.
The speaker's volume, the phone's volume, and even the "tone volume" value in the device's own
configuration file make no difference.

The speaker does, however, leave the SDK's configuration channel open on its USB port:
it shows up as a serial port (`4c4a:4a55`, "Jieli Technology USB Composite Device") that
accepts unauthenticated commands to read, erase and write flash. The prompts are ordinary files
in that flash, so they can be read out, re-encoded at a lower level, and written back.

## What it does

`jlcfg.py` talks to the device:

| Command | What it does | Writes to the device? |
|---|---|---|
| `info` | SDK name, product ID, config files present | no |
| `dump FILE` | reads the whole flash twice, saves it if both reads agree | no |
| `ls IMAGE` | lists the files in a dump and the sector range that is safe to rewrite | no |
| `verify IMAGE` | compares the device's resource area with an image | no |
| `flash CURRENT NEW` | rewrites the 4 KiB sectors that differ between two images | **yes** |

`jltones.py` works on a dump:

| Command | What it does |
|---|---|
| `extract IMAGE DIR` | saves every prompt file |
| `decode FILE.wts OUT.wav` | decodes a `.wts` prompt to WAV |
| `quiet IMAGE NEW_IMAGE` | writes a copy of the image with the prompts made quieter |

## Requirements

- Linux and Python 3.8+ (no Python packages needed). macOS should work but is untested.
- `ffmpeg` (for the `.sbc` prompts)
- A **patched** libopus (for the `.wts` prompts), see below
- Access to the serial port, e.g. on Linux: `sudo setfacl -m u:$USER:rw /dev/ttyACM0`
  (the port is recreated every time the speaker restarts)

### Building the patched libopus

The speaker's decoder only plays SILK frames that are flagged "active". Stock libopus flags quiet
or tonal frames as inactive and has no switch for that, so one condition has to be patched out:

```bash
git clone --depth 1 --branch v1.5.2 https://github.com/xiph/opus.git
cd opus
git apply ../patches/opus-1.5.2-silk-always-active.patch
cmake -S . -B build -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

This gives `opus/build/libopus.so.0`. Nothing needs to be installed system-wide.

## Walkthrough

Plug the speaker into the computer with a data-capable USB-C cable and switch it on.

```bash
# 1. Is it the right kind of device?
./jlcfg.py info

# 2. Back up the whole flash. Keep this file somewhere safe: it is the only way back.
./jlcfg.py dump original.bin

# 3. See what is in it.
./jlcfg.py ls original.bin
./jltones.py extract original.bin tones/

# 4. Build an image with the prompts 16 dB quieter.
./jltones.py --opus-lib opus/build/libopus.so.0 quiet original.bin quiet.bin --gain -16

# 5. See what would be written, then write it.
./jlcfg.py flash original.bin quiet.bin --dry-run
./jlcfg.py flash original.bin quiet.bin
```

Then switch the speaker off and on. Bluetooth usually drops while flash is being written and
comes back after a power cycle.

To change the level later, always build from the **original** dump, and give `flash` the image that
is on the device now as its first argument:

```bash
./jltones.py --opus-lib opus/build/libopus.so.0 quiet original.bin quieter.bin --gain -20
./jlcfg.py flash quiet.bin quieter.bin
```

To go back to stock: `./jlcfg.py flash quiet.bin original.bin`.

`quiet` takes `--only NAME...` and `--skip NAME...` to pick prompts. On the Sound Outdoor the spoken
digits `0.wts` and `1.wts` cannot be changed (see below), so you may want to `--skip` `2.wts` … `9.wts`
as well to keep all ten at the same level.

## How it stays safe

- **Only sound data is rewritten.** `flash` refuses any sector outside the area between the end of
  the program code and the start of the settings storage (`ls` prints that range). Prompts that share
  a sector with program code or a file table are skipped by `quiet`.
- **No file table changes.** Every re-encoded prompt has exactly the original size and the original
  CRC16, so headers and directory checksums stay as they are.
- **Every sector is verified** after it is written. A sector that will not verify is put back to its
  previous content and the run stops.
- **Interrupted runs can be resumed**: run the same `flash` command again. Sectors already correct
  are skipped.
- **The device is not restarted mid-write.** The firmware restarts itself after a write to its config
  file; `flash` avoids arming that.

What it cannot protect against: a power cut or a firmware crash while a sector is erased, and firmware
that differs from the one this was developed on.

## Findings

Tested on a Xiaomi Sound Outdoor reporting SDK `JL701N_V1.4.2`, 2 MiB flash.

**Protocol.** Packets are `5A AA A5 | CRC16 | length | type | sequence | data`. Commands include
file size/read, raw flash read (`0x27`), erase (`0x24`) and write (`0x25`). Data is scrambled by the
firmware on write and reads return the unscrambled view, so reading and writing round-trip; writes
have to stay on a 32-byte grid. There is no authentication.

**Prompt formats.**

- `.sbc`: raw SBC frames, 48 kHz mono, bitpool 35.
- `.wts`: one header byte, then length-prefixed SILK frames: the payload of SILK-only wideband
  20 ms Opus packets with the TOC byte removed. Prepend `0x48` to each frame and any Opus decoder
  plays them.

**What the speaker's `.wts` decoder needs.**

- No frame longer than 78 bytes. With frames of 83-85 bytes a prompt got cut off; above 100 bytes
  it did not play at all.
- Frames flagged active. Every stock frame is, including silence.

**What does not work.** The `tone_vol` field in `cfg_tool.bin` can be changed over the same channel,
but this firmware ignores it.

The protocol and formats are written up in more detail in a pull request to the
[jielie](https://github.com/kagaimiq/jielie) documentation project:
[kagaimiq/jielie#15](https://github.com/kagaimiq/jielie/pull/15).

## What is not in this repository

No firmware dump and no prompt audio: those belong to Xiaomi. You make your own dump from your own
device.

## Credits

- [kagaimiq/jielie](https://github.com/kagaimiq/jielie) for the documentation of JieLi's flash layout
  and file headers.
- JieLi's publicly available SDK sources, which name the protocol's commands and the codec.

## License

MIT, see [LICENSE](LICENSE). The patch in `patches/` applies to Opus, which has its own (BSD) license.
