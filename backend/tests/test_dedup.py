import pytest

from iot_backend.simulator.dedup import SEQ_MODULUS, DuplicateFilter, Verdict, seq_diff

TOP = SEQ_MODULUS - 1  # 4294967295


def test_seq_diff_uses_serial_number_arithmetic():
    assert seq_diff(5, 3) == 2
    assert seq_diff(3, 5) == -2
    assert seq_diff(0, TOP) == 1  # 0 is one step *after* 4294967295
    assert seq_diff(TOP, 0) == -1
    assert seq_diff(2**31 - 1, 0) == 2**31 - 1
    assert seq_diff(2**31, 0) == -(2**31)  # exactly half the range is "behind"


def test_new_then_duplicate():
    f = DuplicateFilter()
    assert f.check(1, 100, 10) is Verdict.NEW
    assert f.check(1, 100, 10) is Verdict.DUPLICATE
    assert f.check(1, 100, 11) is Verdict.NEW
    assert f.check(1, 100, 10) is Verdict.DUPLICATE  # still inside the window


def test_out_of_order_inside_window_is_new_once():
    f = DuplicateFilter(window=8)
    for seq in (10, 13):
        assert f.check(1, 1, seq) is Verdict.NEW
    assert f.check(1, 1, 11) is Verdict.NEW  # late arrival, never seen
    assert f.check(1, 1, 11) is Verdict.DUPLICATE
    assert f.check(1, 1, 12) is Verdict.NEW


def test_older_than_window_is_too_old():
    f = DuplicateFilter(window=8)
    assert f.check(1, 1, 100) is Verdict.NEW
    assert f.check(1, 1, 200) is Verdict.NEW  # jump of 100 clears the window
    assert f.check(1, 1, 100) is Verdict.TOO_OLD


def test_window_edge():
    f = DuplicateFilter(window=8)
    f.check(1, 1, 200)
    assert f.check(1, 1, 193) is Verdict.NEW  # age 7: last slot of the window
    assert f.check(1, 1, 192) is Verdict.TOO_OLD  # age 8: outside


def test_wraparound_is_handled():
    f = DuplicateFilter()
    for seq in (TOP - 1, TOP, 0, 1):
        assert f.check(7, 42, seq) is Verdict.NEW
    for seq in (TOP - 1, TOP, 0, 1):
        assert f.check(7, 42, seq) is Verdict.DUPLICATE
    assert f.check(7, 42, 2) is Verdict.NEW


def test_wraparound_late_arrival_across_the_boundary():
    f = DuplicateFilter()
    f.check(7, 42, TOP - 1)
    f.check(7, 42, 1)  # skipped TOP and 0
    assert f.check(7, 42, TOP) is Verdict.NEW  # late, from just before the wrap
    assert f.check(7, 42, 0) is Verdict.NEW
    assert f.check(7, 42, TOP) is Verdict.DUPLICATE


def test_sessions_and_origins_are_independent():
    f = DuplicateFilter()
    assert f.check(1, 100, 1) is Verdict.NEW
    assert f.check(1, 200, 1) is Verdict.NEW  # same seq, other session
    assert f.check(2, 100, 1) is Verdict.NEW  # same seq, other origin
    assert f.check(1, 100, 1) is Verdict.DUPLICATE


def test_tracked_sessions_are_bounded_and_least_recently_used_goes_first():
    f = DuplicateFilter(max_sessions=2)
    f.check(1, 1, 5)
    f.check(1, 2, 5)
    f.check(1, 1, 6)  # session 1 is now more recent than session 2
    f.check(1, 3, 5)  # evicts session 2
    assert f.tracked_sessions == 2
    assert f.check(1, 1, 5) is Verdict.DUPLICATE
    assert f.check(1, 2, 5) is Verdict.NEW  # forgotten: documented limit of a bounded filter


def test_invalid_sizes_are_refused():
    with pytest.raises(ValueError):
        DuplicateFilter(window=0)
    with pytest.raises(ValueError):
        DuplicateFilter(max_sessions=0)
