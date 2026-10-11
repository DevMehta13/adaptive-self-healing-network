"""The controller's topology view, driven by a fake gateway (no simulator involved)."""

import ast
from pathlib import Path

import pytest

import iot_backend.controller as controller_pkg
import iot_backend.transports as transports_pkg
from iot_backend.controller import TopologyConfig, TopologyView
from iot_backend.controller.topology import (
    LINK_DOWN,
    LINK_ONE_WAY,
    LINK_STALE,
    LINK_UP,
    LOST_CONTACT,
    REACHABLE,
    STALE,
    UNKNOWN,
)
from iot_backend.transports import NeighborObservation, TopologyReportEvent

A, B, C, D = 1, 2, 3, 4
EXPIRY = 15_000


class FakeGateway:
    """Implements the Gateway protocol with a manual clock."""

    def __init__(self):
        self.now = 0
        self._handlers = []
        self._timers = []

    def subscribe(self, handler):
        self._handlers.append(handler)

    def now_ms(self):
        return self.now

    def call_later(self, delay_ms, callback):
        self._timers.append((self.now + delay_ms, callback))

    def advance(self, ms):
        end = self.now + ms
        while True:
            due = sorted((t for t in self._timers if t[0] <= end), key=lambda t: t[0])
            if not due:
                break
            when, callback = due[0]
            self._timers.remove(due[0])
            self.now = when
            callback()
        self.now = end

    def report(self, origin, entries, session=None, seq=1):
        """entries: {neighbor: (state, session)}"""
        event = TopologyReportEvent(
            received_ms=self.now,
            origin=origin,
            session=session or origin * 1000,
            seq=seq,
            neighbors=tuple(
                NeighborObservation(n, state, sess, 100) for n, (state, sess) in entries.items()
            ),
        )
        for handler in self._handlers:
            handler(event)


@pytest.fixture
def world():
    gw = FakeGateway()
    return gw, TopologyView(gw, TopologyConfig(report_expiry_ms=EXPIRY))


def test_link_is_usable_only_when_both_ends_confirm_two_way_with_fresh_reports(world):
    gw, view = world
    gw.report(A, {B: ("two_way", 2000)})
    assert view.usable_links() == []  # B has not reported: one side only
    assert view.link_state("A", "B") == LINK_STALE
    gw.report(B, {A: ("two_way", 1000)})
    assert view.usable_links() == [("A", "B")]
    assert view.link_state("A", "B") == LINK_UP


def test_one_way_link_is_not_usable_and_names_the_working_direction(world):
    gw, view = world
    gw.report(A, {B: ("one_way", 2000)})  # A hears B ...
    gw.report(B, {A: ("lost", 1000)})  # ... but B no longer hears A
    assert view.link_state("A", "B") == LINK_ONE_WAY
    assert view.usable_links() == []
    (link,) = view.snapshot()["links"]
    assert link["usable"] is False
    assert link["directions"] == {"B->A": True, "A->B": False}  # frames from B reach A only


def test_node_lost_contact_names_observers_and_never_says_powered_off(world):
    gw, view = world
    gw.report(B, {A: ("two_way", 1000), C: ("two_way", 3000)})
    gw.report(A, {B: ("two_way", 2000)})
    gw.advance(4_000)
    gw.report(A, {B: ("lost", 2000)}, seq=2)  # A stopped hearing B
    assert view.node_status("B") == LOST_CONTACT
    event = [e for e in view.events if e.type == "node_lost_contact"][-1]
    assert event.data["node"] == "B"
    assert event.data["observed_by"] == ["A"]
    snapshot = view.snapshot()
    node_b = next(n for n in snapshot["nodes"] if n["id"] == "B")
    assert node_b["status"] == LOST_CONTACT
    assert node_b["source"]["observed_by"] == ["A"]
    assert view.link_state("A", "B") == LINK_DOWN
    assert "power" not in repr(snapshot).lower() and "power" not in repr(view.events).lower()


def test_returning_node_is_regained_and_flagged_as_restarted_by_its_new_session(world):
    gw, view = world
    gw.report(B, {A: ("two_way", 1000)}, session=2000)
    gw.report(A, {B: ("two_way", 2000)})
    gw.advance(4_000)
    gw.report(A, {B: ("lost", 2000)}, seq=2)
    assert view.node_status("B") == LOST_CONTACT
    gw.advance(2_000)
    gw.report(B, {A: ("one_way", 1000)}, session=2222, seq=1)  # B booted again
    assert view.node_status("B") == REACHABLE
    regained = [e for e in view.events if e.type == "node_regained_contact"]
    assert [(e.data["node"], e.data["restarted"], e.data["session"]) for e in regained] == [
        ("B", True, 2222)
    ]


def test_reports_expire_instead_of_staying_fresh(world):
    gw, view = world
    gw.report(A, {B: ("two_way", 2000)})
    gw.report(B, {A: ("two_way", 1000)})
    assert view.usable_links() == [("A", "B")]
    gw.advance(EXPIRY - 1)
    assert view.node_status("A") == REACHABLE  # still within the rule
    gw.advance(2)  # now older than the expiry
    assert view.node_status("A") == STALE and view.node_status("B") == STALE
    assert view.link_state("A", "B") == LINK_STALE
    assert view.usable_links() == []
    assert {e.type for e in view.events} >= {"node_stale", "link_state_changed"}


def test_refreshed_reports_keep_the_view_fresh(world):
    gw, view = world
    for i in range(5):
        gw.report(A, {B: ("two_way", 2000)}, seq=i + 1)
        gw.report(B, {A: ("two_way", 1000)}, seq=i + 1)
        gw.advance(10_000)
    assert view.node_status("A") == REACHABLE
    assert view.usable_links() == [("A", "B")]


def test_unknown_node_and_older_report_copies(world):
    gw, view = world
    assert view.node_status("D") == UNKNOWN
    gw.report(A, {B: ("two_way", 2000)}, seq=10)
    gw.report(A, {B: ("lost", 2000)}, seq=9)  # an older copy arriving late
    gw.report(B, {A: ("two_way", 1000)})
    assert view.link_state("A", "B") == LINK_UP


def test_events_have_consecutive_ids_and_reach_subscribers(world):
    gw, view = world
    seen = []
    view.subscribe(seen.append)
    gw.report(A, {B: ("two_way", 2000)})
    gw.report(B, {A: ("two_way", 1000)})
    ids = [e.event_id for e in view.events]
    assert ids == list(range(1, len(ids) + 1))
    assert seen == view.events
    assert view.snapshot()["last_event_id"] == ids[-1]


def test_event_log_is_bounded():
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(max_events=5))
    for i in range(20):
        state = "two_way" if i % 2 == 0 else "lost"
        gw.report(A, {B: (state, 2000)}, seq=i + 1)
    assert len(view.events) == 5
    assert view.last_event_id > 5


@pytest.mark.parametrize("package", [controller_pkg, transports_pkg])
def test_controller_and_gateway_interface_do_not_import_the_simulator(package):
    """The view is built from a gateway only, so it cannot see simulator internals."""
    for path in Path(package.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not [n for n in names if "simulator" in n], f"{path.name} imports {names}"


def test_link_needs_both_ends_to_report_two_way(world):
    gw, view = world
    gw.report(A, {B: ("two_way", 2000)})
    gw.report(B, {A: ("one_way", 1000)})  # B's latest report has not caught up yet
    assert view.link_state("A", "B") == LINK_ONE_WAY
    assert view.usable_links() == []
    gw.report(B, {A: ("two_way", 1000)}, seq=2)
    assert view.usable_links() == [("A", "B")]


def test_one_neighbors_claim_is_not_enough_while_another_still_hears_the_node(world):
    gw, view = world
    gw.report(A, {B: ("two_way", 2000)})
    gw.report(C, {B: ("two_way", 2000)})
    gw.report(B, {A: ("two_way", 1000), C: ("two_way", 3000)})
    gw.advance(1_000)
    gw.report(A, {B: ("lost", 2000)}, seq=2)  # only A lost it (say, one direction failed)
    assert view.node_status("B") == REACHABLE
    gw.report(C, {B: ("lost", 2000)}, seq=2)  # now every observer agrees
    assert view.node_status("B") == LOST_CONTACT
