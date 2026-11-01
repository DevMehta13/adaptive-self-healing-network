import type { Reading } from './fixtures';

export type Mode = 'simulation' | 'hardware';

/** Hardware: the axis is the laptop's wall clock, `offsetMs + sim_ms`. Simulation: the axis is `sim_ms` itself. */
export interface TimeMap { mode: Mode; offsetMs: number }

export const mapTime = (map: TimeMap, simMs: number) => map.mode === 'hardware' ? map.offsetMs + simMs : simMs;

export function timeMapFor(snapshot: { mode: Mode; server_time: string; clock: { sim_ms: number } }): TimeMap {
  if (snapshot.mode === 'simulation') return { mode: 'simulation', offsetMs: 0 };
  const server = Date.parse(snapshot.server_time);
  return { mode: 'hardware', offsetMs: Number.isFinite(server) ? server - snapshot.clock.sim_ms : 0 };
}

export const READING_CAP = 2000;
export const CHANGE_CAP = 500;

/** A reading placed on the shared time axis. `at` is when it reached C; `reachedAt` is when the controller got it (later for a replayed reading). */
export interface ChartReading {
  key: string; origin: string; session: number; seq: number; value: number;
  via: string | null; path: (string | null)[]; at: number; reachedAt: number; replayed: boolean; delayMs: number;
}

export interface ChangeEvent { key: string; id: number; type: string; at: number; data: Record<string, unknown> }

/** Not network changes: the readings have their own buffer and telemetry only updates counters. */
const NOT_CHANGES = new Set(['reading_received', 'telemetry']);

export const readingKey = (r: { origin: string; session: number; seq: number }) => `${r.origin}/${r.session}/${r.seq}`;

/**
 * `received_ms` is when the reading reached C (the replayed example in docs/DASHBOARD_API.md: received 12034 ms,
 * held 28986 ms, event at 41020 ms). So the reading sits at `received_ms`, and `delay_ms` only says how much
 * later the controller got it. (The plan's `received_ms - delay_ms` would put it before it existed.)
 */
export function toChartReading(raw: Reading, map: TimeMap): ChartReading {
  const delayMs = raw.replayed ? raw.delay_ms : 0;
  const at = mapTime(map, raw.received_ms);
  return {
    key: readingKey(raw), origin: raw.origin, session: raw.session, seq: raw.seq, value: raw.value,
    via: raw.via, path: raw.observed_path, at, reachedAt: at + delayMs, replayed: raw.replayed, delayMs,
  };
}

/** A local failover that C relayed late belongs at `received_ms` (the API says so), everything else at its own `sim_ms`. */
export function toChangeEvent(event: { type: string; event_id?: number; data: Record<string, unknown> }, map: TimeMap, instance: number, index = 0): ChangeEvent | null {
  if (NOT_CHANGES.has(event.type)) return null;
  const d = event.data;
  const simMs = event.type === 'local_failover' && d.replayed === true && typeof d.received_ms === 'number' ? d.received_ms : d.sim_ms;
  if (typeof simMs !== 'number' || !Number.isFinite(simMs)) return null;
  const id = event.event_id ?? -(index + 1);
  return { key: `${instance}:${id}`, id, type: event.type, at: mapTime(map, simMs), data: d };
}

function mergeSorted<T extends { key: string; at: number }>(existing: T[], incoming: T[], cap: number): T[] {
  const seen = new Set(existing.map(item => item.key));
  const fresh: T[] = [];
  for (const item of incoming) if (!seen.has(item.key)) { seen.add(item.key); fresh.push(item); }
  if (!fresh.length) return existing;
  const merged = [...existing, ...fresh].sort((a, b) => a.at - b.at);
  return merged.length > cap ? merged.slice(merged.length - cap) : merged;
}

export const mergeReadings = (existing: ChartReading[], incoming: ChartReading[], cap = READING_CAP) => mergeSorted(existing, incoming, cap);
export const mergeChanges = (existing: ChangeEvent[], incoming: ChangeEvent[], cap = CHANGE_CAP) => mergeSorted(existing, incoming, cap);

export interface SnapshotLike {
  mode: Mode; run_id: number; server_time: string; clock: { sim_ms: number };
  readings?: { recent?: Reading[] };
  recent_events?: { type: string; event_id?: number; data: Record<string, unknown> }[];
}

export interface LiveState {
  mode: Mode | null; runId: number | null; timeMap: TimeMap | null;
  /** The newest controller time we know, on the axis. */
  nowAt: number | null;
  /** Counts each backend start (hardware) or run (simulation), because event ids restart. */
  instance: number;
  readings: ChartReading[];
  changes: ChangeEvent[];
}

const empty: LiveState = { mode: null, runId: null, timeMap: null, nowAt: null, instance: 0, readings: [], changes: [] };

/** App-level buffers shared by every page. Plain class so the connection can feed it outside React. */
export class LiveStore {
  private state: LiveState = empty;
  private listeners = new Set<() => void>();

  getState = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(next: LiveState) { this.state = next; this.listeners.forEach(listener => listener()); }

  /** Called with every snapshot and every poll. */
  ingestSnapshot(snapshot: SnapshotLike) {
    let s = this.state;
    const fresh = timeMapFor(snapshot);
    if (s.timeMap === null || s.mode !== snapshot.mode) {
      // First snapshot, or the backend came back in the other mode: nothing in the buffers belongs to this run.
      s = { ...empty, instance: s.instance + 1, mode: snapshot.mode, runId: snapshot.run_id, timeMap: fresh };
    } else if (snapshot.mode === 'simulation' && s.runId !== snapshot.run_id) {
      s = { ...empty, instance: s.instance + 1, mode: snapshot.mode, runId: snapshot.run_id, timeMap: fresh };
    } else if (snapshot.mode === 'hardware' && Math.abs(fresh.offsetMs - s.timeMap.offsetMs) > 2000) {
      // The backend restarted: keep what we have (its time is already wall-clock), but event ids start again.
      s = { ...s, instance: s.instance + 1, timeMap: fresh, runId: snapshot.run_id };
    }
    const map = s.timeMap!;
    const readings = (snapshot.readings?.recent ?? []).map(r => toChartReading(r, map));
    const changes = (snapshot.recent_events ?? []).map((e, i) => toChangeEvent(e, map, s.instance, i)).filter((e): e is ChangeEvent => e !== null);
    this.set({
      ...s, runId: snapshot.run_id, nowAt: mapTime(map, snapshot.clock.sim_ms),
      readings: mergeReadings(s.readings, readings), changes: mergeChanges(s.changes, changes),
    });
  }

  /** Called for each in-order state-changing event from the WebSocket. */
  ingestEvent(event: { type: string; event_id?: number; data: Record<string, unknown> }) {
    const s = this.state;
    if (!s.timeMap) return;
    if (event.type === 'reset') {
      if (s.mode === 'simulation') this.set({ ...empty, instance: s.instance + 1, mode: s.mode, timeMap: s.timeMap, runId: typeof event.data.run_id === 'number' ? event.data.run_id : s.runId });
      return;
    }
    if (event.type === 'reading_received') {
      const d = event.data;
      if (typeof d.origin !== 'string' || typeof d.session !== 'number' || typeof d.seq !== 'number' || typeof d.value !== 'number' || typeof d.received_ms !== 'number') return;
      const reading = toChartReading(d as unknown as Reading, s.timeMap);
      const readings = mergeReadings(s.readings, [reading]);
      if (readings !== s.readings) this.set({ ...s, readings });
      return;
    }
    const change = toChangeEvent(event, s.timeMap, s.instance);
    if (!change) return;
    const changes = mergeChanges(s.changes, [change]);
    if (changes !== s.changes) this.set({ ...s, changes });
  }
}
