"""Tests for the pairing decisions.

These pin down what a given failure is allowed to mean. Getting that wrong is
not a cosmetic problem: one branch decides whether a machine the flow has just
bound is reported as belonging to somebody else, which throws away the only
copy of its pairing key.

Run with:  python3 -m pytest tests/ -q      (needs bleak installed)
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

_PKG_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nespresso_smart"
)
_pkg = types.ModuleType("nespresso_smart")
_pkg.__path__ = [str(_PKG_DIR)]  # type: ignore[attr-defined]
sys.modules.setdefault("nespresso_smart", _pkg)

_REASON = "bleak is not installed"
pairing = pytest.importorskip("nespresso_smart.pairing", reason=_REASON)
device = pytest.importorskip("nespresso_smart.device", reason=_REASON)
protocol = pytest.importorskip("nespresso_smart.protocol", reason=_REASON)


class FakeDevice:
    """A VertuoDevice stand-in that records the pairing conversation."""

    def __init__(
        self,
        *,
        state: protocol.PairingKeyState = protocol.PairingKeyState.NONE,
        connect_error: Exception | None = None,
        authenticate_error: Exception | None = None,
        update_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.connect_error = connect_error
        self.authenticate_error = authenticate_error
        self.update_error = update_error
        self.calls: list[str] = []
        self.disconnects = 0

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error

    async def connect_unauthenticated(self) -> None:
        self.calls.append("connect_unauthenticated")
        if self.connect_error:
            raise self.connect_error

    async def read_pairing_state(self) -> protocol.PairingKeyState:
        self.calls.append("read_pairing_state")
        return self.state

    async def onboard(self) -> None:
        self.calls.append("onboard")
        self.state = protocol.PairingKeyState.FINAL

    async def authenticate(self) -> None:
        self.calls.append("authenticate")
        if self.authenticate_error:
            raise self.authenticate_error

    async def update(self) -> None:
        self.calls.append("update")
        if self.update_error:
            raise self.update_error

    async def disconnect(self) -> None:
        self.disconnects += 1


NOT_BONDED = device.VertuoNotBondedError("not paired")


def test_machine_bound_by_our_own_failed_attempt_is_claimed_not_disowned() -> None:
    """The seed-losing case.

    Attempt 1 writes the key and then fails to verify it, so attempt 2 finds a
    machine that is already bound -- to the seed the flow is still holding.
    Reporting "already_paired" would discard that seed and leave the machine
    on a key nobody has, fixable only by a factory reset.
    """
    dev = FakeDevice(state=protocol.PairingKeyState.NONE)
    # Verification fails the first time only, exactly as an unbonded link does.
    errors = [NOT_BONDED, None]

    async def update() -> None:
        dev.calls.append("update")
        err = errors.pop(0) if errors else None
        if err:
            raise err

    dev.update = update  # type: ignore[method-assign]

    result = asyncio.run(pairing.attempt_twice(dev, pairing.onboard))

    assert result is None, f"expected success, got {result!r}"
    assert "onboard" in dev.calls
    # Attempt 2 saw a bound machine and proved it was ours instead of bailing.
    assert dev.calls.count("read_pairing_state") == 2


def test_machine_bound_to_another_key_is_still_reported() -> None:
    """The genuine case must keep working: our key is refused, so it is theirs."""
    dev = FakeDevice(
        state=protocol.PairingKeyState.FINAL,
        update_error=device.VertuoAuthError("rejected"),
    )

    result = asyncio.run(pairing.attempt_twice(dev, pairing.onboard))

    assert result == "already_paired"


def test_connect_failure_is_not_blamed_on_the_seed() -> None:
    """A machine asleep or held by the phone app is not a wrong key."""
    from bleak.exc import BleakError

    dev = FakeDevice(connect_error=BleakError("Failed to connect after 4 attempts"))

    result = asyncio.run(pairing.attempt_twice(dev, pairing.authenticate_with_seed))

    assert result == "cannot_connect"


def test_rejected_key_is_reported_as_an_auth_failure() -> None:
    """A genuine rejection still says so."""
    dev = FakeDevice(connect_error=device.VertuoAuthError("machine said no"))

    result = asyncio.run(pairing.attempt_twice(dev, pairing.authenticate_with_seed))

    assert result == "auth_failed"


def test_unbonded_link_is_retried_once() -> None:
    """The expected first-attempt refusal must produce a second attempt."""
    attempts = []

    async def step(dev: FakeDevice, attempt: int) -> str | None:
        attempts.append(attempt)
        if attempt == 1:
            raise NOT_BONDED
        return None

    dev = FakeDevice()
    result = asyncio.run(pairing.attempt_twice(dev, step))

    assert result is None
    assert attempts == [1, 2]
    assert dev.disconnects == 1  # the dead link was dropped between attempts


def test_each_attempt_gets_its_own_deadline() -> None:
    """A shared budget would starve attempt 2, the one meant to succeed.

    Attempt 1 burns most of a per-attempt budget before failing unbonded; with
    one budget spanning both, attempt 2 would be cancelled on entry.
    """
    seen = []

    async def step(dev: FakeDevice, attempt: int) -> str | None:
        seen.append(attempt)
        await asyncio.sleep(0.06)
        if attempt == 1:
            raise NOT_BONDED
        return None

    monkey = pairing.ATTEMPT_TIMEOUT
    pairing.ATTEMPT_TIMEOUT = 0.1
    try:
        result = asyncio.run(pairing.attempt_twice(FakeDevice(), step))
    finally:
        pairing.ATTEMPT_TIMEOUT = monkey

    assert seen == [1, 2]
    assert result is None


def test_timeout_is_reported_as_timeout() -> None:
    """A step that overruns its deadline reports timeout, not a pairing verdict."""

    async def step(dev: FakeDevice, attempt: int) -> str | None:
        await asyncio.Event().wait()
        return None

    monkey = pairing.ATTEMPT_TIMEOUT
    pairing.ATTEMPT_TIMEOUT = 0.05
    try:
        result = asyncio.run(pairing.attempt_twice(FakeDevice(), step))
    finally:
        pairing.ATTEMPT_TIMEOUT = monkey

    assert result == "timeout"


def test_unknown_pairing_state_is_a_connection_problem() -> None:
    """An unreadable CMIDType means the link is bad, not that the key is."""
    dev = FakeDevice(state=protocol.PairingKeyState.UNKNOWN)

    result = asyncio.run(pairing.attempt_twice(dev, pairing.onboard))

    assert result == "cannot_connect"
