"""Unit tests for the pure parts of provision.py.  No hardware, no subprocesses.

    cd backend && uv run pytest ../tools
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provision as p  # noqa: E402

MACS = {
    "A": "AA:AA:AA:AA:AA:01",
    "B": "BB:BB:BB:BB:BB:02",
    "C": "CC:CC:CC:CC:CC:03",
    "D": "DD:DD:DD:DD:DD:04",
}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ---- new-port detection ----------------------------------------------------------------

def test_single_new_port_is_found():
    assert p.single_new_port(["COM1", "COM3"], ["COM1", "COM3", "COM7"]) == "COM7"


def test_no_new_port_fails_clearly():
    with pytest.raises(p.ProvisionError, match="no new serial port"):
        p.single_new_port(["COM1"], ["COM1"])


def test_two_new_ports_fail_clearly():
    with pytest.raises(p.ProvisionError, match="more than one new serial port.*COM7.*COM8"):
        p.single_new_port(["COM1"], ["COM1", "COM8", "COM7"])


def test_removed_port_is_not_new():
    assert p.new_ports(["COM1", "COM3"], ["COM1"]) == []


def test_wait_for_new_port_polls_until_it_appears():
    clock = FakeClock()
    calls = []

    def listing():
        calls.append(clock.now)
        return ["COM1", "COM5"] if clock.now >= 1.0 else ["COM1"]

    port = p.wait_for_new_port(listing, ["COM1"], timeout=10, settle=0.5,
                               clock=clock, sleep=clock.sleep)
    assert port == "COM5"
    assert len(calls) > 2


def test_wait_for_new_port_times_out():
    clock = FakeClock()
    with pytest.raises(p.ProvisionError, match="no new serial port"):
        p.wait_for_new_port(lambda: ["COM1"], ["COM1"], timeout=3, settle=0.5,
                            clock=clock, sleep=clock.sleep)


def test_wait_for_new_port_catches_a_second_port_during_settling():
    clock = FakeClock()

    def listing():
        ports = ["COM1", "COM5"]
        if clock.now >= 0.4:
            ports.append("COM6")
        return ports

    with pytest.raises(p.ProvisionError, match="more than one"):
        p.wait_for_new_port(listing, ["COM1"], timeout=5, settle=1.0,
                            clock=clock, sleep=clock.sleep)


# ---- F3_READY parsing ------------------------------------------------------------------

READY = "# F3_READY role=2 mac=24:6f:28:AB:cd:01 channel=1 peers_restored=2 free_heap=249312"


def test_parse_ready():
    info = p.parse_ready(READY)
    assert info == {"role": "B", "mac": "24:6F:28:AB:CD:01", "channel": 1,
                    "peers_restored": 2, "free_heap": 249312}


def test_parse_ready_ignores_surrounding_noise():
    assert p.parse_ready("ets Jun 8 2016\n" + READY + "\r")["role"] == "B"


@pytest.mark.parametrize("line", [
    "", "# F3C_READY controller-installed routes",
    "# F3_READY role=2 mac=bad channel=1 peers_restored=2 free_heap=1",
    "# F3_READY role=2 mac=24:6f:28:ab:cd:01 channel=1 free_heap=1",
])
def test_parse_ready_rejects_other_lines(line):
    assert p.parse_ready(line) is None


def test_parse_ready_unknown_role_number():
    assert p.parse_ready(READY.replace("role=2", "role=9"))["role"] is None


def test_check_ready_rules():
    good = p.parse_ready(READY)
    p.check_ready(good, "B")
    with pytest.raises(p.ProvisionError, match="role=B"):
        p.check_ready(good, "A")
    with pytest.raises(p.ProvisionError, match="peers_restored=1"):
        p.check_ready({**good, "peers_restored": 1}, "B")
    with pytest.raises(p.ProvisionError, match="no F3_READY"):
        p.check_ready(None, "B")


def test_parse_esptool_mac():
    out = "Chip is ESP32-D0WD-V3\nFeatures: WiFi\nMAC: 24:6f:28:ab:cd:01\nUploading stub..."
    assert p.parse_esptool_mac(out) == "24:6F:28:AB:CD:01"
    with pytest.raises(p.ProvisionError):
        p.parse_esptool_mac("no mac here")


# ---- peer lines (lab package table) ----------------------------------------------------

def test_peer_lines_match_the_lab_table():
    assert p.peer_lines("A", MACS) == ["peer 2 BB:BB:BB:BB:BB:02", "peer 4 DD:DD:DD:DD:DD:04"]
    assert p.peer_lines("B", MACS) == ["peer 1 AA:AA:AA:AA:AA:01", "peer 3 CC:CC:CC:CC:CC:03"]
    assert p.peer_lines("C", MACS) == ["peer 2 BB:BB:BB:BB:BB:02", "peer 4 DD:DD:DD:DD:DD:04"]
    assert p.peer_lines("D", MACS) == ["peer 1 AA:AA:AA:AA:AA:01", "peer 3 CC:CC:CC:CC:CC:03"]


def test_full_plan_covers_every_board_with_two_lines():
    plan = p.peer_plan(None, MACS)
    assert list(plan) == ["A", "B", "C", "D"]
    assert all(len(lines) == 2 for lines in plan.values())


# ---- --only replacement logic ----------------------------------------------------------

def test_only_b_resends_b_fully_and_repoints_a_and_c():
    plan = p.peer_plan(["B"], MACS)
    assert set(plan) == {"A", "B", "C"}  # D has no peer entry for B
    assert plan["B"] == ["peer 1 AA:AA:AA:AA:AA:01", "peer 3 CC:CC:CC:CC:CC:03"]
    assert plan["A"] == ["peer 2 BB:BB:BB:BB:BB:02"]
    assert plan["C"] == ["peer 2 BB:BB:BB:BB:BB:02"]


def test_only_a_repoints_b_and_d():
    plan = p.peer_plan(["A"], MACS)
    assert set(plan) == {"A", "B", "D"}
    assert plan["B"] == ["peer 1 AA:AA:AA:AA:AA:01"]
    assert plan["D"] == ["peer 1 AA:AA:AA:AA:AA:01"]


def test_only_two_adjacent_boards_does_not_duplicate_lines():
    plan = p.peer_plan(["A", "B"], MACS)
    assert plan["A"] == ["peer 2 BB:BB:BB:BB:BB:02", "peer 4 DD:DD:DD:DD:DD:04"]
    assert plan["C"] == ["peer 2 BB:BB:BB:BB:BB:02"]
    assert plan["D"] == ["peer 1 AA:AA:AA:AA:AA:01"]


def test_merge_macs_replaces_only_the_named_roles():
    saved = dict(MACS)
    merged = p.merge_macs(["B"], saved, {"B": "11:22:33:44:55:66"})
    assert merged["B"] == "11:22:33:44:55:66"
    assert merged["A"] == MACS["A"] and merged["D"] == MACS["D"]


def test_merge_macs_needs_saved_macs_for_the_others():
    with pytest.raises(p.ProvisionError, match="saved MAC of role A"):
        p.merge_macs(["B"], {"C": MACS["C"], "D": MACS["D"]}, {"B": MACS["B"]})


def test_merge_macs_full_run_uses_only_fresh_macs():
    assert p.merge_macs(None, {"A": "stale"}, MACS) == MACS


def test_match_roles_finds_ports_by_mac_not_by_name():
    observed = {"COM9": MACS["C"], "COM4": MACS["A"], "COM2": "00:00:00:00:00:00"}
    assert p.match_roles(observed, MACS) == {"C": "COM9", "A": "COM4"}


# ---- peer send / retry -----------------------------------------------------------------

class FakeSerial:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.sent.append(data.decode())

    def readline(self):
        return (self.replies.pop(0) if self.replies else "").encode()


def test_send_peer_line_accepts_peer_set():
    ser = FakeSerial(["noise\n", "# peer_set\n"])
    p.send_peer_line(ser, "peer 2 BB:BB:BB:BB:BB:02")
    assert ser.sent == ["peer 2 BB:BB:BB:BB:BB:02\n"]


def test_send_peer_line_retries_once_then_succeeds():
    ser = FakeSerial(["# peer_failed\n", "# peer_set\n"])
    p.send_peer_line(ser, "peer 2 X")
    assert len(ser.sent) == 2


def test_send_peer_line_stops_after_one_retry():
    ser = FakeSerial(["# peer_failed\n", "# peer_failed\n", "# peer_set\n"])
    with pytest.raises(p.ProvisionError, match="after 2 attempts"):
        p.send_peer_line(ser, "peer 2 X")
    assert len(ser.sent) == 2


def test_send_peer_line_times_out_without_reply():
    clock = FakeClock()
    ser = FakeSerial([])
    ser.readline = lambda: (clock.sleep(0.5), b"")[1]
    with pytest.raises(p.ProvisionError):
        p.send_peer_line(ser, "peer 2 X", timeout=1.0, clock=clock)
    assert len(ser.sent) == 2


# ---- misc ------------------------------------------------------------------------------

def test_env_selection():
    assert p.env_for("A", no_sensor=False) == "f3c_a_dht11"
    assert p.env_for("A", no_sensor=True) == "f3c_a"
    assert [p.env_for(r, False) for r in "BCD"] == ["f3c_b", "f3c_c", "f3c_d"]


def test_normalize_mac_rejects_garbage():
    with pytest.raises(p.ProvisionError):
        p.normalize_mac("not a mac")


def test_lab_record_and_table():
    record = p.lab_record(MACS, {"A": "COM3", "B": "COM4"})
    assert record["boards"]["A"] == {"mac": MACS["A"], "port": "COM3"}
    assert record["boards"]["C"]["port"] is None
    table = p.format_table(MACS, {"A": "COM3"})
    assert "Role" in table and MACS["D"] in table and "COM3" in table


def test_dry_run_changes_nothing_and_exits_zero(capsys):
    assert p.main(["--dry-run", "--only", "b"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "role B" in out and "f3c_b" in out
    assert "role A" not in out.split("Peer lines")[0]


def test_unknown_role_is_rejected():
    with pytest.raises(SystemExit):
        p.main(["--dry-run", "--only", "Z"])


def test_merge_ports_drops_a_stale_port_now_used_by_another_board():
    saved = {"A": "COM3", "D": "COM6"}
    assert p.merge_ports(saved, {"B": "COM6", "A": "COM3"}) == {"B": "COM6", "A": "COM3"}


def test_merge_ports_keeps_saved_ports_for_unseen_boards():
    assert p.merge_ports({"D": "COM7", "A": "COM3"}, {"A": "COM4"}) == {"D": "COM7", "A": "COM4"}
