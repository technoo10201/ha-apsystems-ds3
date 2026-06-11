"""Diagnostic CLI for the APS DS3 / CC2530 hardware stack.

Lets the user validate the wiring (USB-TTL bridge + CC2530+CC2591 module +
inverters) **before** installing the integration into Home Assistant. Six
sub-commands, all built on `argparse` (stdlib) so the CLI has no dependency
beyond `pyserial-asyncio`, which the protocol layer already needs.

Run via the package:

    python -m custom_components.aps_zigbee.aps_protocol.cli <subcommand> [...]

or via the `scripts/aps-cli` shell wrapper bundled in this repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .coordinator import check_coordinator, init_coordinator
from .decode_ds3 import DS3DecodeError, DS3Reading, decode_ds3_frame, derive_power
from .frames import DEFAULT_ECU_ID
from .pairing import PairingError, pair_inverter
from .polling import PollError, poll_inverter, reboot_inverter
from .znp import ZNP, ZNPError

_LOGGER = logging.getLogger("aps_zigbee.cli")


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", help="emit JSON instead of a human-readable view"
    )
    common.add_argument("--debug", action="store_true", help="verbose protocol logging on stderr")

    port_parent = argparse.ArgumentParser(add_help=False, parents=[common])
    port_parent.add_argument(
        "--port",
        required=True,
        help="serial device (e.g. /dev/serial/by-id/usb-...CH340E...)",
    )
    port_parent.add_argument(
        "--ecu-id",
        default=DEFAULT_ECU_ID,
        help=f"12 hex chars advertised by the coordinator (default: {DEFAULT_ECU_ID})",
    )

    parser = argparse.ArgumentParser(
        prog="aps-cli",
        description="Offline diagnostic CLI for the APS DS3 / CC2530 stack",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "list-ports",
        parents=[common],
        help="list USB serial ports, prefer /dev/serial/by-id paths",
    )

    sub.add_parser(
        "doctor",
        parents=[port_parent],
        help="open the port, bring up the coordinator, ping it — verdict pass/fail",
    )

    p_pair = sub.add_parser(
        "pair", parents=[port_parent], help="pair an inverter and print its invID"
    )
    p_pair.add_argument("serial", help="12-digit serial printed on the inverter")
    p_pair.add_argument(
        "--save",
        type=Path,
        help="append the {serial, inv_id, ecu_id, port} mapping to this JSON file",
    )

    p_poll = sub.add_parser("poll", parents=[port_parent], help="poll one inverter")
    p_poll.add_argument("inv_id", help="4-char hex inverter short address returned by `pair`")
    p_poll.add_argument(
        "--loop",
        type=float,
        nargs="?",
        const=60.0,
        default=None,
        metavar="SECONDS",
        help="poll every SECONDS until Ctrl-C (default 60 s when --loop has no value)",
    )

    p_reboot = sub.add_parser(
        "reboot", parents=[port_parent], help="send the proprietary reboot command"
    )
    p_reboot.add_argument("inv_id", help="4-char hex inverter short address")

    p_decode = sub.add_parser(
        "decode",
        parents=[common],
        help="offline — decode a hex burst without opening any port",
    )
    p_decode.add_argument(
        "hex",
        nargs="?",
        help="hex string of the raw burst (use --from-file or stdin to avoid shell limits)",
    )
    p_decode.add_argument(
        "--from-file",
        type=Path,
        help="read the hex burst from a file instead of an argument",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.debug)
    json_out: bool = args.json

    try:
        if args.command == "list-ports":
            return cmd_list_ports(json_out)
        if args.command == "decode":
            return cmd_decode(args, json_out)
        # everything below needs the event loop
        return asyncio.run(_async_dispatch(args, json_out))
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


async def _async_dispatch(args: argparse.Namespace, json_out: bool) -> int:
    if args.command == "doctor":
        return await cmd_doctor(args, json_out)
    if args.command == "pair":
        return await cmd_pair(args, json_out)
    if args.command == "poll":
        return await cmd_poll(args, json_out)
    if args.command == "reboot":
        return await cmd_reboot(args, json_out)
    raise AssertionError(f"unhandled command {args.command!r}")


# ---------------------------------------------------------------------------
# sub-commands
# ---------------------------------------------------------------------------


def cmd_list_ports(json_out: bool) -> int:
    ports = _enumerate_ports()
    if json_out:
        _emit_json(ports)
    else:
        if not ports:
            print("No USB serial ports detected.")
        else:
            _emit_table(
                ports,
                columns=("by_id", "device", "description", "vid_pid"),
                headers=("/dev/serial/by-id", "device", "description", "vid:pid"),
            )
    return 0


async def cmd_doctor(args: argparse.Namespace, json_out: bool) -> int:
    result: dict[str, Any] = {
        "port": args.port,
        "ecu_id": args.ecu_id,
        "port_open": False,
        "init_ok": False,
        "ping_ok": False,
    }
    znp = ZNP(args.port)
    try:
        await znp.open()
        result["port_open"] = True
        result["init_ok"] = await init_coordinator(znp, args.ecu_id)
        if result["init_ok"]:
            result["ping_ok"] = await check_coordinator(znp)
    except ZNPError as err:
        result["error"] = str(err)
    finally:
        await znp.close()

    verdict_ok = result["port_open"] and result["init_ok"] and result["ping_ok"]
    if json_out:
        _emit_json({**result, "verdict": "ok" if verdict_ok else "fail"})
    else:
        _emit_kv(result)
        print(f"\nverdict: {'OK' if verdict_ok else 'FAIL'}")
    return 0 if verdict_ok else 1


async def cmd_pair(args: argparse.Namespace, json_out: bool) -> int:
    znp = ZNP(args.port)
    try:
        await znp.open()
        if not await init_coordinator(znp, args.ecu_id):
            print("coordinator init failed; cannot pair", file=sys.stderr)
            return 1
        try:
            inv_id = await pair_inverter(znp, args.serial, args.ecu_id)
        except PairingError as err:
            print(f"pairing failed: {err}", file=sys.stderr)
            return 1
    finally:
        await znp.close()

    payload = {"serial": args.serial, "inv_id": inv_id, "ecu_id": args.ecu_id, "port": args.port}
    if args.save is not None:
        _append_pair_record(args.save, payload)
    if json_out:
        _emit_json(payload)
    else:
        _emit_kv(payload)
    return 0


async def cmd_poll(args: argparse.Namespace, json_out: bool) -> int:
    znp = ZNP(args.port)
    try:
        await znp.open()
        if not await init_coordinator(znp, args.ecu_id):
            print("coordinator init failed; cannot poll", file=sys.stderr)
            return 1
        if args.loop is None:
            return await _poll_once(znp, args.inv_id, args.ecu_id, json_out)
        return await _poll_loop(znp, args.inv_id, args.ecu_id, args.loop, json_out)
    finally:
        await znp.close()


async def _poll_once(
    znp: ZNP, inv_id: str, ecu_id: str, json_out: bool, previous: DS3Reading | None = None
) -> int:
    try:
        result = await poll_inverter(znp, inv_id, ecu_id)
    except PollError as err:
        print(f"poll failed: {err}", file=sys.stderr)
        return 1
    reading = result.reading
    p1, p2, ptot = derive_power(previous, reading)
    payload = _reading_payload(reading, p1, p2, ptot)
    if result.relays is not None:
        payload["mesh_hops"] = len(result.relays)
        payload["route"] = " -> ".join(result.relays) or "(direct)"
    if json_out:
        _emit_json(payload)
    else:
        _emit_kv(payload)
    return 0


async def _poll_loop(znp: ZNP, inv_id: str, ecu_id: str, interval_s: float, json_out: bool) -> int:
    previous: DS3Reading | None = None
    while True:
        try:
            result = await poll_inverter(znp, inv_id, ecu_id)
        except PollError as err:
            print(f"[poll] {err}", file=sys.stderr)
        else:
            reading = result.reading
            p1, p2, ptot = derive_power(previous, reading)
            previous = reading
            payload = _reading_payload(reading, p1, p2, ptot)
            if json_out:
                _emit_json(payload)
                sys.stdout.flush()
            else:
                print(f"--- {reading.serial} @ ts={reading.timestamp_raw} ---")
                _emit_kv(payload)
        await asyncio.sleep(interval_s)


async def cmd_reboot(args: argparse.Namespace, json_out: bool) -> int:
    znp = ZNP(args.port)
    try:
        await znp.open()
        if not await init_coordinator(znp, args.ecu_id):
            print("coordinator init failed; cannot reboot", file=sys.stderr)
            return 1
        try:
            await reboot_inverter(znp, args.inv_id, args.ecu_id)
        except PollError as err:
            print(f"reboot failed: {err}", file=sys.stderr)
            return 1
    finally:
        await znp.close()

    payload = {"inv_id": args.inv_id, "sent": True}
    if json_out:
        _emit_json(payload)
    else:
        _emit_kv(payload)
    return 0


def cmd_decode(args: argparse.Namespace, json_out: bool) -> int:
    if args.from_file is not None:
        hex_str = args.from_file.read_text().strip()
    elif args.hex is not None:
        hex_str = args.hex
    elif not sys.stdin.isatty():
        hex_str = sys.stdin.read().strip()
    else:
        print("decode needs a hex string (positional, --from-file, or stdin)", file=sys.stderr)
        return 2
    try:
        reading = decode_ds3_frame(hex_str)
    except DS3DecodeError as err:
        print(f"decode failed: {err}", file=sys.stderr)
        return 1
    payload = _reading_payload(reading, None, None, None)
    if json_out:
        _emit_json(payload)
    else:
        _emit_kv(payload)
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _enumerate_ports() -> list[dict[str, str]]:
    from serial.tools import list_ports  # type: ignore[import-untyped]

    out: list[dict[str, str]] = []
    for port in list_ports.comports():
        out.append(
            {
                "device": port.device,
                "by_id": _by_id_for(port.device),
                "description": port.description or "",
                "vid_pid": _format_vid_pid(port),
                "manufacturer": port.manufacturer or "",
            }
        )
    return out


def _by_id_for(device: str) -> str:
    by_id_dir = Path("/dev/serial/by-id")
    if not by_id_dir.is_dir():
        return ""
    try:
        target = Path(device).resolve()
    except OSError:
        return ""
    for link in by_id_dir.iterdir():
        try:
            if link.resolve() == target:
                return str(link)
        except OSError:
            continue
    return ""


def _format_vid_pid(port: Any) -> str:
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    if vid is None or pid is None:
        return ""
    return f"{vid:04x}:{pid:04x}"


def _reading_payload(
    reading: DS3Reading,
    p1: float | None,
    p2: float | None,
    ptot: float | None,
) -> dict[str, Any]:
    payload = asdict(reading)
    # Match the HA coordinator's sensor key names so the JSON line matches.
    payload["energy_p1_kwh"] = reading.energy_p1_wh / 1000.0
    payload["energy_p2_kwh"] = reading.energy_p2_wh / 1000.0
    if p1 is not None and p2 is not None and ptot is not None:
        payload["power_p1_w"] = p1
        payload["power_p2_w"] = p2
        payload["power_total_w"] = ptot
    else:
        payload["power_p1_w"] = None
        payload["power_p2_w"] = None
        payload["power_total_w"] = None
    return payload


def _emit_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _emit_kv(payload: dict[str, Any]) -> None:
    width = max((len(k) for k in payload), default=0)
    for key, value in payload.items():
        if isinstance(value, float):
            formatted = f"{value:.3f}"
        else:
            formatted = str(value)
        print(f"  {key.ljust(width)} : {formatted}")


def _emit_table(
    rows: list[dict[str, str]],
    *,
    columns: tuple[str, ...],
    headers: tuple[str, ...],
) -> None:
    widths = [
        max(len(headers[i]), max((len(row.get(columns[i], "")) for row in rows), default=0))
        for i in range(len(columns))
    ]
    line = "  ".join(headers[i].ljust(widths[i]) for i in range(len(columns)))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        cells = [row.get(columns[i], "").ljust(widths[i]) for i in range(len(columns))]
        print("  ".join(cells))


def _append_pair_record(path: Path, payload: dict[str, str]) -> None:
    """Persist a pairing result to a JSON file (list of records)."""
    records: list[dict[str, str]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, list):
                records = [r for r in existing if r.get("serial") != payload["serial"]]
        except (json.JSONDecodeError, OSError):
            pass
    records.append(payload)
    path.write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
