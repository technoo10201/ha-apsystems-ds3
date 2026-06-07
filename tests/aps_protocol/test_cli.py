"""CLI tests that don't need a real serial port.

We cover the two fully offline subcommands (`decode` and `list-ports`) and the
argparse plumbing. The hardware-touching subcommands (doctor / pair / poll /
reboot) are exercised via integration in the user's hands; mocking the whole
ZNP transport here would buy little.
"""

from __future__ import annotations

import json

import pytest

from custom_components.aps_zigbee.aps_protocol import cli
from tests.aps_protocol.test_decode_ds3 import DS3_FIXTURE


def test_decode_human_output_lists_all_fields(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["decode", DS3_FIXTURE])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    for field in (
        "serial",
        "signal_quality_pct",
        "vdc1_v",
        "vdc2_v",
        "idc1_a",
        "idc2_a",
        "acv_v",
        "freq_hz",
        "temperature_c",
        "energy_p1_wh",
        "energy_p2_wh",
        "energy_p1_kwh",
        "energy_p2_kwh",
    ):
        assert field in out, field
    assert "703000021300" in out  # the fixture's serial


def test_decode_json_is_parseable_and_well_typed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["decode", "--json", DS3_FIXTURE])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["serial"] == "703000021300"
    assert isinstance(payload["vdc1_v"], float)
    assert payload["power_p1_w"] is None  # no previous reading provided
    assert pytest.approx(payload["energy_p1_kwh"], rel=1e-6) == payload["energy_p1_wh"] / 1000.0


def test_decode_from_file(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    fixture_file = tmp_path / "frame.hex"
    fixture_file.write_text(DS3_FIXTURE + "\n")
    rc = cli.main(["decode", "--from-file", str(fixture_file), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["serial"] == "703000021300"


def test_decode_bad_hex_returns_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["decode", "deadbeef"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "decode failed" in captured.err


def test_decode_with_no_input_explains_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Pretend stdin is a TTY — under pytest it's a pipe, which would otherwise
    # cause the CLI to consume an empty stdin and report a decode error instead.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    rc = cli.main(["decode"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "needs a hex string" in captured.err


def test_list_ports_handles_empty_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_enumerate_ports", lambda: [])
    rc = cli.main(["list-ports"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "No USB serial ports detected" in captured.out


def test_list_ports_renders_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "_enumerate_ports",
        lambda: [
            {
                "device": "/dev/ttyUSB0",
                "by_id": "/dev/serial/by-id/usb-CH340E_USB-C-if00",
                "description": "USB Serial",
                "vid_pid": "1a86:7522",
                "manufacturer": "QinHeng",
            }
        ],
    )
    rc = cli.main(["list-ports"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "/dev/ttyUSB0" in out
    assert "by-id" in out
    assert "CH340E" in out


def test_list_ports_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = [
        {
            "device": "/dev/ttyUSB0",
            "by_id": "",
            "description": "",
            "vid_pid": "",
            "manufacturer": "",
        }
    ]
    monkeypatch.setattr(cli, "_enumerate_ports", lambda: fake)
    rc = cli.main(["list-ports", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == fake


def test_build_parser_rejects_unknown_subcommand() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["does-not-exist"])


def test_build_parser_poll_loop_optional_value() -> None:
    parser = cli.build_parser()
    # No --loop → loop stays at None (single poll).
    ns = parser.parse_args(["poll", "--port", "/tmp/x", "ABCD"])
    assert ns.loop is None
    assert ns.inv_id == "ABCD"
    # --loop without value → defaults to 60.0 (positional must come first to
    # avoid argparse swallowing it as the loop value).
    ns = parser.parse_args(["poll", "--port", "/tmp/x", "ABCD", "--loop"])
    assert ns.loop == 60.0
    # Explicit value.
    ns = parser.parse_args(["poll", "--port", "/tmp/x", "ABCD", "--loop", "15"])
    assert ns.loop == 15.0


def test_decode_payload_includes_derived_power_when_provided() -> None:
    from custom_components.aps_zigbee.aps_protocol.decode_ds3 import (
        DS3Reading,
        derive_power,
    )

    a = DS3Reading(
        serial="000000000000",
        signal_quality_pct=50.0,
        vdc1_v=35.0,
        vdc2_v=35.0,
        idc1_a=1.0,
        idc2_a=1.0,
        acv_v=230.0,
        freq_hz=50.0,
        temperature_c=25.0,
        timestamp_raw=1000,
        energy_p1_wh=10.0,
        energy_p2_wh=5.0,
    )
    b = DS3Reading(
        serial="000000000000",
        signal_quality_pct=50.0,
        vdc1_v=35.0,
        vdc2_v=35.0,
        idc1_a=1.0,
        idc2_a=1.0,
        acv_v=230.0,
        freq_hz=50.0,
        temperature_c=25.0,
        timestamp_raw=1900,
        energy_p1_wh=12.5,
        energy_p2_wh=6.5,
    )
    p1, p2, total = derive_power(a, b)
    payload = cli._reading_payload(b, p1, p2, total)
    assert payload["power_p1_w"] == pytest.approx(10.0)
    assert payload["power_p2_w"] == pytest.approx(6.0)
    assert payload["power_total_w"] == pytest.approx(16.0)
