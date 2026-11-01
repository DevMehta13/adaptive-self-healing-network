"""Feeds the run history from the live runtime's event stream.

The recorder is a subscriber like a WebSocket client: it sees exactly the events the dashboard
sees and nothing else. A new stored run begins at start-up and whenever the live run changes
(a reset to another scenario), judged by the run id on each event so no event lands in the
wrong run.
"""

from typing import Any

from iot_backend.live import DashboardModel, LiveEvent
from iot_backend.storage.store import RunStore


class RunRecorder:
    def __init__(self, store: RunStore, live: DashboardModel) -> None:
        self._store = store
        self._live = live
        self._live_run_id: int | None = None
        self.run_id: int | None = None
        self._unsubscribe = live.subscribe(self._on_event)
        self._begin()

    def _begin(self) -> None:
        live = self._live
        self.run_id = self._store.start_run(
            live.mode, live.scenario, live.run_id, getattr(live, "run_config", {})
        )
        self._live_run_id = live.run_id

    def _on_event(self, event: LiveEvent) -> None:
        if self._live.run_id != self._live_run_id:
            if self.run_id is not None:
                self._store.end_run(self.run_id)
            self._begin()
        assert self.run_id is not None
        data: dict[str, Any] = event.data
        if event.type == "reading_received":
            self._store.add_reading(self.run_id, event.event_id, data)
        else:
            self._store.add_event(self.run_id, event.event_id, data["sim_ms"], event.type, data)

    def close(self) -> None:
        self._unsubscribe()
        if self.run_id is not None:
            self._store.end_run(self.run_id)
        self._store.flush()
