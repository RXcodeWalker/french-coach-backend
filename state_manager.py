"""
Exam state machine. Enforces strictly linear STATE_0 → STATE_5 transitions.
State skipping and regression are both prohibited.
"""
from __future__ import annotations

import time
from typing import Any

STATES: dict[int, str] = {
    0: "PREPARATION",
    1: "GREETING",
    2: "ROLEPLAY",
    3: "TOPIC_1",
    4: "TOPIC_2",
    5: "TERMINATED",
}

# Each state maps to the single valid next state
_NEXT_STATE: dict[int, int] = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}


def state_name(state: int) -> str:
    return STATES.get(state, f"UNKNOWN({state})")


def advance_state(session: dict[str, Any]) -> int:
    """Advance session to the next state. Raises ValueError if already terminal."""
    current = session["state"]
    if current not in _NEXT_STATE:
        raise ValueError(
            f"Cannot advance from terminal state {current} ({state_name(current)})"
        )
    next_s = _NEXT_STATE[current]
    session["state"] = next_s
    session["timestamps"][f"state_{next_s}"] = time.time()
    return next_s


def assert_state(session: dict[str, Any], expected: int) -> None:
    """Raise ValueError if session is not in the expected state."""
    if session["state"] != expected:
        raise ValueError(
            f"Expected state {expected} ({state_name(expected)}), "
            f"but session is in {session['state']} ({state_name(session['state'])})"
        )


def assert_state_in(session: dict[str, Any], allowed: list[int]) -> None:
    """Raise ValueError if session state is not in the allowed set."""
    if session["state"] not in allowed:
        raise ValueError(
            f"Session state {session['state']} ({state_name(session['state'])}) "
            f"not in allowed states {allowed}"
        )
