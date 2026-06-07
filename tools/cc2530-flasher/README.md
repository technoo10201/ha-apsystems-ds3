# `tools/cc2530-flasher/` — standalone CC2530 flasher on an ESP32-C3

Flash a **CC2530 + CC2591** module from a cheap ESP32-C3 dev board
(~ 4–5 € on AliExpress), with the **Kadsol** firmware
`CC2530ZNP_2591-with-SBL.hex` embedded directly in the ESP32 binary
(~ 256 KB in PROGMEM): erase → write → verify → green LED → done. No
host-side TI tools required, no CC Debugger, no Raspberry Pi.

> **Armed mode.** At boot the DD / DC / RST pins are left high-impedance
> — the CC2530 keeps running normally even if the ESP32 stays wired in
> permanently. Flashing only starts when you type `FLASH` + Enter on the
> ESP32's serial console (115 200 baud). An accidental ESP32 reboot can
> never erase the CC2530.

> **Use case.** Leave the ESP32-C3 wired alongside the CC2530 in your
> permanent install. The next time you need to re-flash the CC2530 (new
> firmware, recovery), you do it from your Home Assistant host over SSH
> by opening `pio device monitor` against the ESP32's USB-CDC — no
> physical access to the dongle required.

> ⚠️ **Permanent-install wiring.** Connect **only DD, DC, RST and GND**
> between the ESP32 and the CC2530 — **never the 3 V3 rail**. The CC2530
> stays powered by the USB-TTL bridge that talks to your HA host; two
> 3.3 V regulators in parallel will fight each other. Before sending
> `FLASH`, stop anything else that's talking to the CC2530 over UART
> (the HA container, ad-hoc scripts) to avoid bus collisions.

LED on the ESP32-C3 Mini reference build (WS2812 on GPIO 8): **blue
pulse** during write / verify, **solid green** on success, **red blink**
on failure (resets to armed mode after 30 s).

PlatformIO project. Build with `pio run`, flash with `pio run -t upload`.

## Project layout

```
tools/cc2530-flasher/
├── platformio.ini              # PlatformIO config, two envs (esp32-c3, esp32-classic)
├── src/
│   └── main.cpp                # main sketch (CCLoader-derived, MIT)
├── include/
│   └── firmware_data.h         # Kadsol firmware as PROGMEM array (~256 KB)
└── README.md                   # this file
```

The low-level CC2530 protocol code is taken from
[`RedBearLab/CCLoader`](https://github.com/RedBearLab/CCLoader) (MIT
license), kept functionally unchanged. The embedded firmware
`CC2530ZNP_2591-with-SBL.hex` is the standard community mirror of the
Kadsol build, published by
[`patience4711/read-APSystems-YC600-QS1-DS3`](https://github.com/patience4711/read-APSystems-YC600-QS1-DS3/blob/main/cc25xx_firmware.zip)
and re-mirrored in the Zigbee2MQTT community.

## Hardware

- **1 ESP32-C3 board** — recommended. Native 3.3 V GPIOs match the
  CC2530 logic levels exactly, so no level shifter. Built-in USB-CDC,
  so no separate USB-UART adapter for upload. Tested variants:
  DevKit M, C3 Super Mini, Seeed XIAO ESP32-C3. A classic ESP32
  (WROOM-32) also works, via the `esp32-classic` env in `platformio.ini`.
- **5 dupont wires** — female-female or female-male depending on the
  connectors on each side.
- **The CC2530 + CC2591 module to flash.**
- **1 USB cable** to connect the ESP32 to your computer (only for the
  initial upload of this sketch to the ESP32).

> Why not an Arduino Uno / Nano? Their GPIOs are 5 V — you would risk
> damaging the CC2530 without a level shifter. The ESP32-C3 saves you
> that part.

## Wiring (default env `esp32-c3`)

```
ESP32-C3                   CC2530 + CC2591
========                   ===============
GPIO 3   ───── 5 cm ─────► DD   (Debug Data)
GPIO 4   ───── 5 cm ─────► DC   (Debug Clock)
GPIO 5   ───── 5 cm ─────► RST  (Reset)
3V3      ───────────────► VCC  (3.3 V only — NEVER 5 V)
GND      ───────────────► GND
```

Keep wires under 10 cm to limit parasitics during the debug-clock pulses.

Two pre-configured environments live in `platformio.ini`:

| env             | board                  | DD | DC | RST | LED | LED active |
| --------------- | ---------------------- | -- | -- | --- | --- | ---------- |
| `esp32-c3` def. | `esp32-c3-devkitm-1`   |  3 |  4 |   5 |   8 | WS2812     |
| `esp32-classic` | `esp32dev`             |  5 | 18 |  19 |   2 | active-HIGH |

To pin-out a different board (S2, S3, custom), edit `build_flags` inside
`platformio.ini`:

```ini
build_flags =
    -DCC2530_PIN_DD=3
    -DCC2530_PIN_DC=4
    -DCC2530_PIN_RST=5
    -DCC2530_PIN_LED=8
    -DCC2530_LED_ACTIVE_LOW=1
```

⚠️ During the flash, the CC2530 **must** be powered from a single 3.3 V
source. Disconnect the USB-TTL bridge or any other 3.3 V rail before
hitting `FLASH`.

## Build + upload (PlatformIO Core)

```bash
# Install PlatformIO Core if you don't have it already:
#   pip install platformio
# or:
#   pipx install platformio

cd tools/cc2530-flasher

# Compile (default env is esp32-c3)
pio run

# Flash the ESP32 over USB-CDC
pio run -t upload

# Open the serial monitor (115200 baud) — prompt:
#   cc2530-flasher> waiting for FLASH command...
pio device monitor

# All-in-one: compile + upload + monitor
pio run -t upload -t monitor

# Target the classic ESP32 env instead of C3
pio run -e esp32-classic -t upload
```

On Linux, the ESP32-C3 typically enumerates as `/dev/ttyACM0`. On
Windows it shows up as `COM3` (or similar). PlatformIO auto-detects the
port; no `--upload-port` flag needed in most cases.

First upload takes ~30 s (the binary is ~280 KB including the embedded
firmware) — well within the C3's 4 MB flash.

### VSCode + PlatformIO extension

1. Open `tools/cc2530-flasher/` as a folder in VSCode.
2. The PlatformIO extension auto-detects the project.
3. PlatformIO sidebar → *Project Tasks* → `esp32-c3` → *Upload*, then
   *Monitor* to watch the logs.

## Flashing procedure (armed mode)

1. **Wire the CC2530** to the ESP32 (DD / DC / RST / GND, and 3V3 only if
   the CC2530 has no other power source).
2. **Plug the ESP32** into USB. Boot is passive — pins are high-impedance,
   the CC2530 keeps running normally.
3. **Open the serial monitor** at 115 200 baud. You'll see the prompt:
   ```
   cc2530-flasher> waiting for FLASH command...
   ```
4. **Type `FLASH` + Enter.** Watch the LED:
   - **Blue pulse** during write + verify (heartbeat).
   - **Solid green** when done and verified → ✅ success.
   - **Red blink** for 30 s, then back to armed mode → ❌ failure, check
     the serial output.

Total flash time: **~2–4 minutes** (256 KB in 512-byte blocks plus
byte-by-byte verify). At the end the debug pins go back to high-Z and
the CC2530 reboots into the fresh firmware — no need to unwire anything
if you left it permanently installed.

Other accepted commands at the prompt: `STATUS` (chip ID, sketch
version, last result).

## Verify after the flash

From any host with the CC2530 reachable via USB-TTL (the same host
where your Home Assistant runs the `aps_zigbee` integration), confirm
the dongle answers a ZNP ping and pair-handshake with the example test
runner shipped in this project:

```bash
docker run --rm -it \
    --device=/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0:/dev/ttyUSB0 \
    -v "$(pwd):/app" -w /app \
    python:3.13-slim \
    bash -c "pip install --quiet pyserial && python main.py 704000XXXXXX"
```

Replace `704000XXXXXX` with one of your inverter serial numbers. On the
new Kadsol firmware you should see:

```
=== PAIR SUCCESS ===
  serial = 704000XXXXXX
  invID  = 0xXXXX
```

If you still see `Inverter NOT paired`, the firmware probably didn't
take — check `STATUS` on the flasher's serial console for the chip ID
and last-result code, then re-flash.

## Troubleshooting

### `ERROR: no CC2530 detected. Check wiring + 3.3 V on VCC + GND common.`

Serial output right after boot. Common causes:

- Wrong GPIOs: re-check `-DCC2530_PIN_*` in `platformio.ini`, rebuild,
  re-upload.
- No common ground between ESP32 and CC2530.
- CC2530 VCC not powered (multimeter test: should read ≈ 3.3 V).
- CC2530 VCC at 5 V (DANGER, potential damage).
- Wires too long / electrical noise — stay under 10 cm.
- ESP32 in a weird state → unplug / replug.

### LED stays dark

The default LED pin (GPIO 8 on the C3) is not universal. On some
boards the onboard LED is elsewhere (GPIO 2, 7, 21, …) or simply not
present. Check your board's pinout and adjust `CC2530_PIN_LED` /
`CC2530_LED_ACTIVE_LOW` in `platformio.ini`. The serial monitor
(`pio device monitor`) tells you everything the LED would — flash from
there if the LED is unreliable.

### Nothing on the serial console

On ESP32-C3 with native USB-Serial-JTAG, make sure
`-DARDUINO_USB_CDC_ON_BOOT=1` is set in `build_flags`. If the C3 has
an external CH340 (clone "C3 Mini" boards), keep that flag at `0` so
`Serial` is routed to the hardware UART pins instead of the native
USB-CDC.

### `Upload failed: A fatal error occurred...`

On an ESP32-C3 with native USB-Serial-JTAG, try holding the BOOT button
while you click upload, or briefly short GPIO 9 to GND at boot to force
the bootloader.

### Edited `firmware_data.h`, the change isn't picked up

PlatformIO doesn't always re-compile sources when only an `include/`
header changes. Force a clean build:

```bash
pio run -t clean
pio run -t upload
```

## Regenerating `firmware_data.h` from a different `.hex`

If you want to flash a different CC2530 image — a different Kadsol
revision, an experimental build, or your own custom firmware —
regenerate the embedded PROGMEM array:

```bash
pip install intelhex
python3 - <<'PY' > include/firmware_data.h
from intelhex import IntelHex
ih = IntelHex('/path/to/your/CC2530.hex')
mn, mx = ih.minaddr(), ih.maxaddr()
total = mx - mn + 1
if total % 4: total += 4 - (total % 4)
data = bytearray([0xFF] * total)
for addr, value in ih.todict().items():
    if isinstance(addr, int):
        data[addr] = value
out = ['// Auto-generated', '#pragma once', '#include <Arduino.h>', '',
       f'#define KADSOL_FIRMWARE_BYTES {total}u', '',
       f'const uint8_t KADSOL_FIRMWARE[KADSOL_FIRMWARE_BYTES] PROGMEM = {{']
for i in range(0, total, 16):
    out.append('  ' + ', '.join(f'0x{b:02X}' for b in data[i:i+16]) + ',')
out.append('};\n')
print('\n'.join(out))
PY

pio run -t clean
pio run -t upload
```

The macro `KADSOL_FIRMWARE_BYTES` and the array `KADSOL_FIRMWARE` are
the contract `main.cpp` expects — keep those names.

## Credits & license

- Low-level CC2530 flashing protocol:
  [`RedBearLab/CCLoader`](https://github.com/RedBearLab/CCLoader) — MIT.
- Embedded firmware: `CC2530ZNP_2591-with-SBL.hex` from
  [`patience4711/read-APSystems-YC600-QS1-DS3`](https://github.com/patience4711/read-APSystems-YC600-QS1-DS3)
  (community mirror, see `cc25xx_firmware.zip`).
- This sketch: MIT (same license as the parent `hacs-aps-ds3` repo).
