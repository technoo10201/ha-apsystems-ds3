# hacs-aps-ds3

Home Assistant custom component for **APsystems DS3** micro-inverters, talking
directly to a **CC2530 USB Zigbee dongle** flashed with the proprietary Kadsol
firmware (`CC2530ZNP-with-SBL.hex`). No ECU, no cloud, no ESP32 bridge.

## Status

Work in progress. The `develop` branch contains the live code. `main` is kept
empty (this README only) until the first stable release is squash-merged.

## Why

APsystems' DS3 micro-inverters use a proprietary Zigbee protocol that ZHA and
Zigbee2MQTT can't speak. The existing
[`patience4711/ESP32-read-APS-inverters`](https://github.com/patience4711/ESP32-read-APS-inverters)
firmware solves this on an ESP32 + CC2530 combo. This project ports that
protocol stack to pure Python so the CC2530 dongle can be plugged straight into
the host running Home Assistant.

## Hardware

- A CC2530 USB stick flashed with `CC2530ZNP-with-SBL.hex` (Kadsol build).
- One or more APsystems DS3 micro-inverters (up to two PV inputs each).
- Home Assistant (Container / OS / Core). Tested on HA Container (Docker).

## Architecture (high level)

- `aps_protocol/` — async Python implementation of the CC2530 ZNP framing,
  the APS-specific coordinator init, pairing handshake, polling request and
  DS3 frame decoder. Reusable outside of Home Assistant.
- `custom_components/aps_zigbee/` — Home Assistant integration wired on top
  of `aps_protocol` via a `DataUpdateCoordinator` and a `sensor` platform.
  Config flow lets the user select the serial port and pair inverters by
  their 12-digit serial number.

## Sensors exposed (per inverter)

Power & energy (per PV input and total), DC voltage & current (per input),
AC voltage, grid frequency, inverter temperature, Zigbee signal quality.

## Roadmap

See the `develop` branch.

## Credits

- Protocol reverse-engineering and reference firmware:
  [patience4711/ESP32-read-APS-inverters](https://github.com/patience4711/ESP32-read-APS-inverters).
- CC2530 firmware: Kadsol (distributed via the zigbee2mqtt community).

## License

MIT (TBD — license file added with the first develop merge).
