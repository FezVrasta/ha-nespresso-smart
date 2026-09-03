"""Pairing decisions, independent of Home Assistant.

The config flow is only glue: which form to show, which string to render. The
question of *what a given failure means* -- retry it, blame the key, blame the
link, or declare the machine someone else's -- is decided here, so it can be
tested against a fake device the way protocol.py and device.py are.

That matters most for one branch. Onboarding can write the pairing key
successfully and only then fail to verify it, which leaves the machine bound
to a seed the flow is still holding. Calling that "already paired" would throw
the seed away and strand the machine on a key nobody has, recoverable only by
a factory reset -- so the choice is worth pinning down in tests.

Every function here returns an error key for strings.json, or None on success.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .device import (
    VertuoAuthError,
    VertuoDevice,
    VertuoNotBondedError,
    VertuoTimeoutError,
)
from .protocol import BOUND_STATES, PairingKeyState

_LOGGER = logging.getLogger(__name__)

#: Deadline for a *single* pairing attempt, covering connect, bond and every
#: read and write within it. The individual operations are bounded too; this
#: is the backstop that guarantees the form always comes back with an answer
#: rather than leaving the user staring at a spinner. It must comfortably
#: exceed DEFAULT_TIMEOUT + BOND_TIMEOUT + a few GATT_TIMEOUTs, or it would
#: cut short an attempt that was still making progress.
ATTEMPT_TIMEOUT = 90.0

#: One pairing attempt: takes the device and the attempt number.
Step = Callable[[VertuoDevice, int], Awaitable[str | None]]


async def attempt_twice(device: VertuoDevice, step: Step) -> str | None:
    """Run a pairing step, retrying once if the link was not yet bonded.

    The deadline is *per attempt*, not shared between them. One attempt can
    legitimately spend the connect timeout plus the bond timeout plus several
    GATT timeouts; a single budget covering both would be spent before the
    second attempt -- the one this retry exists to reach -- ever started, and
    the user would be told "timeout" for a machine that was about to pair.
    """
    for attempt in (1, 2):
        try:
            async with asyncio.timeout(ATTEMPT_TIMEOUT):
                return await step(device, attempt)
        except (TimeoutError, VertuoTimeoutError) as err:
            _LOGGER.debug("Pairing attempt %d timed out: %s", attempt, err)
            return "timeout"
        except VertuoNotBondedError as err:
            _LOGGER.debug("Attempt %d needs a bond first: %s", attempt, err)
            await device.disconnect()
    return "not_bonded"


async def authenticate_with_seed(device: VertuoDevice, attempt: int) -> str | None:
    """Connect with an existing seed and read the machine once.

    On BlueZ the first attempt against a machine the host has not bonded with
    is *expected* to fail: the pairing-key write comes back "not paired", and
    it is that refusal which prompts the bond. attempt_twice retries it.
    """
    try:
        await device.connect()
    except (TimeoutError, VertuoTimeoutError, VertuoNotBondedError):
        raise  # classified by attempt_twice
    except VertuoAuthError as err:
        _LOGGER.debug("Seed attempt %d rejected: %s", attempt, err)
        return "auth_failed"
    except Exception as err:
        # Anything else at this stage is the link, not the key: the machine
        # asleep, out of range, or held by the phone app. Calling that
        # "auth_failed" tells someone with a perfectly good seed to
        # factory-reset their machine.
        _LOGGER.debug("Seed attempt %d could not connect: %s", attempt, err)
        return "cannot_connect"

    try:
        await device.update()
    except (TimeoutError, VertuoTimeoutError, VertuoNotBondedError):
        raise
    except Exception as err:
        _LOGGER.debug("Authenticating with supplied seed failed: %s", err)
        return "auth_failed"
    return None


async def onboard(device: VertuoDevice, attempt: int) -> str | None:
    """Bind an unbound machine to the device's seed."""
    try:
        # Connect without authenticating, so the pairing state can be read
        # before anything is written to the machine.
        await device.connect_unauthenticated()
    except (TimeoutError, VertuoTimeoutError, VertuoNotBondedError):
        raise  # classified by attempt_twice
    except Exception as err:
        _LOGGER.debug("Connecting for onboarding failed: %s", err)
        return "cannot_connect"

    try:
        state = await device.read_pairing_state()
        if state is PairingKeyState.UNKNOWN:
            return "cannot_connect"

        if state in BOUND_STATES:
            # Not automatically somebody else's machine -- see claim_bound.
            return await claim_bound(device, attempt)

        await device.onboard()
        await device.authenticate()
        await device.update()
    except (TimeoutError, VertuoTimeoutError, VertuoNotBondedError):
        raise  # classified by attempt_twice
    except Exception as err:
        _LOGGER.debug("Onboarding failed: %s", err)
        return "onboard_failed"
    return None


async def claim_bound(device: VertuoDevice, attempt: int) -> str | None:
    """Decide whether an already-bound machine is bound to *our* seed.

    Returns None if the machine accepts our key -- it is ours, most likely
    because an earlier attempt in this same flow bound it and then failed to
    verify. Otherwise it really does belong to another controller, and the
    user needs its original key or a factory reset.
    """
    try:
        await device.authenticate()
        await device.update()
    except (TimeoutError, VertuoTimeoutError, VertuoNotBondedError):
        raise  # classified by attempt_twice
    except Exception as err:
        _LOGGER.debug("Attempt %d: machine is bound elsewhere: %s", attempt, err)
        return "already_paired"
    _LOGGER.debug("Attempt %d: machine is already bound to our seed", attempt)
    return None
