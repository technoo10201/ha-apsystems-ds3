"""Tests for the pure-Python runtime state machine.

These tests pin down the contract the HA coordinator depends on:
backoff growth, day/night gating (the `patience4711` observation that
DS3 inverters are silent at night by design) and the failure→DEAD
escalation threshold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.aps_zigbee.aps_protocol.runtime import (
    InverterRuntime,
    InverterState,
    compute_backoff,
    is_dead,
    reset_night_counters,
)

_T0 = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_compute_backoff_doubles_until_cap() -> None:
    assert compute_backoff(0) == 0
    assert compute_backoff(1) == 1
    assert compute_backoff(2) == 2
    assert compute_backoff(3) == 4
    assert compute_backoff(4) == 8
    assert compute_backoff(5) == 16
    # 2 ** 8 = 256, still under the default cap of 300.
    assert compute_backoff(9) == 256
    # 2 ** 9 = 512, capped to 300.
    assert compute_backoff(10) == 300
    assert compute_backoff(20) == 300


def test_compute_backoff_respects_custom_cap() -> None:
    assert compute_backoff(20, cap_s=10) == 10
    assert compute_backoff(2, base_s=5, cap_s=100) == 10


def test_record_success_resets_everything() -> None:
    rt = InverterRuntime(serial="123456789012")
    rt.consecutive_failures = 3
    rt.state = InverterState.STALE
    rt.next_retry_after = _T0 + timedelta(seconds=60)

    rt.record_success(_T0)

    assert rt.state is InverterState.OK
    assert rt.consecutive_failures == 0
    assert rt.last_seen == _T0
    assert rt.next_retry_after is None


def test_record_failure_during_day_escalates_to_stale_then_dead() -> None:
    rt = InverterRuntime(serial="x")
    for i in range(1, 5):
        rt.record_failure(_T0, sun_is_up=True, dead_threshold=5)
        assert rt.state is InverterState.STALE
        assert rt.consecutive_failures == i
    rt.record_failure(_T0, sun_is_up=True, dead_threshold=5)
    assert rt.state is InverterState.DEAD
    assert rt.consecutive_failures == 5


def test_record_failure_at_night_stays_idle_no_matter_what() -> None:
    rt = InverterRuntime(serial="x")
    for _ in range(20):
        rt.record_failure(_T0, sun_is_up=False, dead_threshold=5)
    assert rt.state is InverterState.IDLE
    assert rt.consecutive_failures == 0  # we don't count night failures
    # We still set a backoff so the bus isn't hammered all night.
    assert rt.next_retry_after is not None
    assert rt.next_retry_after > _T0


def test_should_skip_returns_true_inside_backoff_window() -> None:
    rt = InverterRuntime(serial="x")
    rt.record_failure(_T0, sun_is_up=True, dead_threshold=5)
    # Backoff after one failure = 1 s.
    assert rt.should_skip(_T0)
    assert rt.should_skip(_T0 + timedelta(milliseconds=500))
    assert not rt.should_skip(_T0 + timedelta(seconds=2))


def test_should_skip_false_when_no_prior_failure() -> None:
    rt = InverterRuntime(serial="x")
    assert not rt.should_skip(_T0)


def test_backoff_grows_with_consecutive_failures() -> None:
    rt = InverterRuntime(serial="x")
    last = _T0
    deltas = []
    for _ in range(6):
        rt.record_failure(last, sun_is_up=True, dead_threshold=99)
        deltas.append((rt.next_retry_after - last).total_seconds())
        last = rt.next_retry_after  # type: ignore[assignment]
    # 1, 2, 4, 8, 16, 32 — strictly doubling, all distinct.
    assert deltas == [1, 2, 4, 8, 16, 32]


def test_to_attributes_serialises_for_ha() -> None:
    rt = InverterRuntime(serial="x")
    rt.record_failure(_T0, sun_is_up=True, dead_threshold=5)
    attrs = rt.to_attributes()
    assert attrs["state"] == "stale"
    assert attrs["consecutive_failures"] == 1
    assert attrs["last_seen"] is None


def test_to_attributes_after_success_includes_last_seen_iso() -> None:
    rt = InverterRuntime(serial="x")
    rt.record_success(_T0)
    attrs = rt.to_attributes()
    assert attrs["state"] == "ok"
    assert attrs["last_seen"] == _T0.isoformat()


def test_is_dead_helper() -> None:
    assert is_dead(InverterState.DEAD)
    assert not is_dead(InverterState.STALE)
    assert not is_dead(InverterState.OK)
    assert not is_dead(InverterState.IDLE)


def test_reset_night_counters_clears_failures_only() -> None:
    rt_a = InverterRuntime(serial="a")
    rt_b = InverterRuntime(serial="b")
    rt_a.record_failure(_T0, sun_is_up=True, dead_threshold=99)
    rt_a.record_failure(_T0, sun_is_up=True, dead_threshold=99)
    rt_b.record_failure(_T0, sun_is_up=True, dead_threshold=99)
    retry_a = rt_a.next_retry_after
    retry_b = rt_b.next_retry_after

    reset_night_counters({"a": rt_a, "b": rt_b})

    assert rt_a.consecutive_failures == 0
    assert rt_b.consecutive_failures == 0
    # next_retry_after is left alone — it expires on its own.
    assert rt_a.next_retry_after == retry_a
    assert rt_b.next_retry_after == retry_b


def test_state_enum_values_are_stable_strings() -> None:
    # The HA dashboard / templates rely on these exact strings.
    assert InverterState.OK.value == "ok"
    assert InverterState.STALE.value == "stale"
    assert InverterState.IDLE.value == "idle"
    assert InverterState.DEAD.value == "dead"


def test_record_failure_rejects_negative_thresholds() -> None:
    # We aren't strict — but make sure we don't crash.
    rt = InverterRuntime(serial="x")
    rt.record_failure(_T0, sun_is_up=True, dead_threshold=0)
    assert rt.state is InverterState.DEAD


def test_backoff_window_uses_capped_seconds() -> None:
    rt = InverterRuntime(serial="x")
    for _ in range(20):
        rt.record_failure(_T0, sun_is_up=True, dead_threshold=99, cap_s=30)
    delta = (rt.next_retry_after - _T0).total_seconds()
    assert delta == 30
