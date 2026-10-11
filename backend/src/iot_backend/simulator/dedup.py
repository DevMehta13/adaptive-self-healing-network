"""Bounded duplicate detection for received readings.

Sequence numbers are 32-bit and wrap from 4294967295 to 0. To compare two of them we use
*serial-number arithmetic*: look at the difference modulo 2**32. If the difference is less
than half the range, the first number is "ahead"; otherwise it is "behind". So 0 counts as
newer than 4294967295, which plain `>` would get wrong.

For each (origin, session) the filter remembers only the highest sequence number seen and a
bitmap of the `window` numbers just below it (a sliding window). Memory per session is fixed.
Only `max_sessions` sessions are tracked; the least recently used is forgotten first.
"""

from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

SEQ_MODULUS = 2**32
_HALF = 2**31


class Verdict(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    TOO_OLD = "too_old"  # older than the window: cannot tell, so it is not accepted


def seq_diff(a: int, b: int) -> int:
    """Signed distance from b to a with wraparound, in [-2**31, 2**31)."""
    diff = (a - b) % SEQ_MODULUS
    return diff - SEQ_MODULUS if diff >= _HALF else diff


@dataclass
class _Window:
    highest: int
    bits: int  # bit i set means sequence number (highest - i) has been seen


class DuplicateFilter:
    def __init__(self, window: int = 64, max_sessions: int = 8) -> None:
        if window < 1 or max_sessions < 1:
            raise ValueError("window and max_sessions must be positive")
        self._window = window
        self._mask = (1 << window) - 1
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[tuple[int, int], _Window] = OrderedDict()

    @property
    def tracked_sessions(self) -> int:
        return len(self._sessions)

    def check(self, origin: int, session: int, seq: int) -> Verdict:
        """Classify a received sequence number and remember it if it is new."""
        key = (origin, session)
        state = self._sessions.get(key)
        if state is None:
            self._sessions[key] = _Window(highest=seq, bits=1)
            if len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)  # forget the least recently used session
            return Verdict.NEW

        self._sessions.move_to_end(key)
        diff = seq_diff(seq, state.highest)
        if diff > 0:  # newer: slide the window forward
            state.bits = ((state.bits << diff) | 1) & self._mask if diff < self._window else 1
            state.highest = seq
            return Verdict.NEW
        if diff == 0:
            return Verdict.DUPLICATE
        age = -diff
        if age >= self._window:
            return Verdict.TOO_OLD
        bit = 1 << age
        if state.bits & bit:
            return Verdict.DUPLICATE
        state.bits |= bit  # late but never seen before
        return Verdict.NEW
