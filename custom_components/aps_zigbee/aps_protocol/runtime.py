"""Pure-Python runtime state machine for the polling loop.

This is the **HA-free** part of the robustness layer: tracking the consecutive
failures of each inverter, deciding when to back off, and translating the
combination of (failures, time of day) into one of four states that the
coordinator and the sensor platform can both consume.

Why pure Python? Two reasons:
1. The decision logic is intricate enough that we want unit tests on it, and
   `pytest-homeassistant-custom-component` is a heavy dep we don't want to
   pull in just for that.
2. Separating the state machine from the I/O makes the coordinator's
   `_async_update_data` straight-line and easy to read.

`patience4711` observed that APsystems DS3 micro-inverters are exclusively
powered by their PV panels — no sun, no Zigbee chatter. The night-mode
guarding in `record_failure` exists for that reason: we don't escalate failures
to `DEAD` while the sun is below the horizon, because the silence is expected
and harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class InverterState(str, Enum):
    """High-level health of one paired inverter.

    The ordering reflects increasing severity in daytime; `IDLE` is the
    catch-all "expected silence" — at night, or while we're backing off after
    a daytime failure.
    """

    OK = "ok"
    STALE = "stale"
    IDLE = "idle"
    DEAD = "dead"


def compute_backoff(failures: int, *, base_s: int = 1, cap_s: int = 300) -> int:
    """Return the number of seconds to wait before retrying.

    Doubles with each consecutive failure (1, 2, 4, 8, 16, …) and saturates
    at `cap_s` (default 5 min). `failures` is the number of failures recorded
    **including the current one**.
    """
    if failures < 1:
        return 0
    return min(base_s * (2 ** (failures - 1)), cap_s)


@dataclass(slots=True)
class InverterRuntime:
    """Per-inverter mutable state owned by the coordinator.

    Not thread-safe — assumed to live inside the asyncio loop.
    """

    serial: str
    state: InverterState = InverterState.IDLE
    consecutive_failures: int = 0
    last_seen: datetime | None = None
    next_retry_after: datetime | None = None

    def should_skip(self, now: datetime) -> bool:
        """True if we are still inside the backoff window."""
        return self.next_retry_after is not None and now < self.next_retry_after

    def record_success(self, now: datetime) -> None:
        """Reset everything on a successful poll."""
        self.state = InverterState.OK
        self.consecutive_failures = 0
        self.last_seen = now
        self.next_retry_after = None

    def record_failure(
        self,
        now: datetime,
        *,
        sun_is_up: bool,
        dead_threshold: int,
        base_s: int = 1,
        cap_s: int = 300,
    ) -> None:
        """Update state after a failed poll attempt.

        At night we don't count toward the `DEAD` threshold and collapse the
        state to `IDLE` — *unless the inverter is already DEAD*, in which case
        the DEAD state latches through the night.  Failure counters are reset
        at sunrise by `reset_night_counters` so the inverter gets a fresh set
        of chances once the sun is back up.
        """
        if not sun_is_up:
            if self.state is not InverterState.DEAD:
                self.state = InverterState.IDLE
            # We still set a backoff so we don't hammer the bus all night.
            self.next_retry_after = now + timedelta(seconds=min(cap_s, 60))
            return

        self.consecutive_failures += 1
        backoff_s = compute_backoff(self.consecutive_failures, base_s=base_s, cap_s=cap_s)
        self.next_retry_after = now + timedelta(seconds=backoff_s)
        if self.consecutive_failures >= dead_threshold:
            self.state = InverterState.DEAD
        else:
            self.state = InverterState.STALE

    def to_attributes(self) -> dict[str, object]:
        """Snapshot exposed to Home Assistant as `extra_state_attributes`."""
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


def is_dead(state: InverterState) -> bool:
    """Helper for the sensor `available` property."""
    return state is InverterState.DEAD


def reset_night_counters(runtimes: dict[str, InverterRuntime]) -> None:
    """Clear `consecutive_failures` for every inverter.

    Called at sunrise so inverters that didn't talk overnight get a fresh
    chance before being escalated to `DEAD`.
    """
    for rt in runtimes.values():
        rt.consecutive_failures = 0
        # Don't touch next_retry_after — it expires on its own and the
        # coordinator's first daytime tick will retry.
