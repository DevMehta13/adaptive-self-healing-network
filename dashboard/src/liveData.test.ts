import { describe, expect, it } from 'vitest';
import type { Reading } from './fixtures';
import { LiveStore, mapTime, mergeChanges, mergeReadings, sinceLastReset, timeMapFor, toChangeEvent, toChartReading } from './liveData';
import type { ChangeEvent, ChartReading, SnapshotLike } from './liveData';

const raw = (seq: number, received_ms: number, extra: Partial<Reading> = {}): Reading =>
  ({ origin: 'A', session: 7, seq, value: 20, via: 'B', observed_path: ['A', 'B', 'C'], received_ms, replayed: false, delay_ms: 0, ...extra });
const sim: SnapshotLike['mode'] = 'simulation';

const snapshot = (over: Partial<SnapshotLike> = {}): SnapshotLike => ({
  mode: 'hardware', run_id: 1, server_time: '2026-11-01T10:00:10.000Z', clock: { sim_ms: 10_000 }, readings: { recent: [] }, recent_events: [], ...over,
});

describe('time mapping', () => {
  it('hardware: wall clock = server_time - (clock - t)', () => {
    const map = timeMapFor(snapshot());
    const start = Date.parse('2026-11-01T10:00:00.000Z');
    expect(mapTime(map, 0)).toBe(start);
    expect(mapTime(map, 4000)).toBe(start + 4000);
    expect(mapTime(map, 10_000)).toBe(Date.parse('2026-11-01T10:00:10.000Z'));
  });
  it('simulation: the simulated clock itself', () => {
    expect(mapTime(timeMapFor(snapshot({ mode: sim })), 12_345)).toBe(12_345);
  });
  it('places a replayed reading when C received it and says when the controller got it', () => {
    const map = timeMapFor(snapshot({ mode: sim }));
    const r = toChartReading(raw(87, 12_034, { replayed: true, delay_ms: 28_986 }), map);
    expect(r.at).toBe(12_034);
    expect(r.reachedAt).toBe(41_020);
    expect(toChartReading(raw(1, 5000), map)).toMatchObject({ at: 5000, reachedAt: 5000, replayed: false });
  });
  it('places a late failover report at received_ms', () => {
    const map = timeMapFor(snapshot({ mode: sim }));
    const e = toChangeEvent({ type: 'local_failover', event_id: 9, data: { sim_ms: 41_140, received_ms: 20_966, replayed: true } }, map, 1);
    expect(e?.at).toBe(20_966);
    expect(toChangeEvent({ type: 'local_failover', event_id: 9, data: { sim_ms: 41_140, replayed: false } }, map, 1)?.at).toBe(41_140);
  });
});

describe('reading buffer', () => {
  const map = timeMapFor(snapshot({ mode: sim }));
  const many = (n: number, from = 0): ChartReading[] => Array.from({ length: n }, (_, i) => toChartReading(raw(from + i, (from + i) * 1000), map));
  it('keeps one copy per (origin, session, seq)', () => {
    const first = mergeReadings([], many(3));
    expect(mergeReadings(first, many(3))).toBe(first);
    expect(mergeReadings(first, many(5)).map(r => r.seq)).toEqual([0, 1, 2, 3, 4]);
  });
  it('treats another session as another reading', () => {
    const merged = mergeReadings(many(1), [toChartReading(raw(0, 0, { session: 8 }), map)]);
    expect(merged).toHaveLength(2);
  });
  it('is ordered by time even when a replayed reading arrives late', () => {
    const merged = mergeReadings(many(1, 10), [toChartReading(raw(3, 3000, { replayed: true, delay_ms: 9000 }), map)]);
    expect(merged.map(r => r.seq)).toEqual([3, 10]);
  });
  it('drops the oldest beyond the cap', () => {
    const merged = mergeReadings(many(5), many(5, 5), 6);
    expect(merged.map(r => r.seq)).toEqual([4, 5, 6, 7, 8, 9]);
  });
});

describe('change buffer', () => {
  const change = (id: number, at: number, instance = 1): ChangeEvent => ({ key: `${instance}:${id}`, id, type: 'node_lost_contact', at, data: {} });
  it('dedupes by instance and event id, so a restart with reused ids is kept', () => {
    const a = mergeChanges([], [change(1, 100), change(2, 200)]);
    expect(mergeChanges(a, [change(2, 200)])).toBe(a);
    expect(mergeChanges(a, [change(1, 900, 2)])).toHaveLength(3);
  });
  it('drops the oldest beyond the cap', () => {
    expect(mergeChanges([change(1, 1), change(2, 2)], [change(3, 3)], 2).map(c => c.id)).toEqual([2, 3]);
  });
  it('leaves readings and telemetry out of the changes', () => {
    const map = timeMapFor(snapshot());
    expect(toChangeEvent({ type: 'reading_received', event_id: 1, data: { sim_ms: 1 } }, map, 1)).toBeNull();
    expect(toChangeEvent({ type: 'telemetry', event_id: 2, data: { sim_ms: 1 } }, map, 1)).toBeNull();
    expect(toChangeEvent({ type: 'node_lost_contact', event_id: 3, data: { sim_ms: 1 } }, map, 1)).not.toBeNull();
    expect(toChangeEvent({ type: 'node_lost_contact', event_id: 4, data: {} }, map, 1)).toBeNull();
  });
});

describe('LiveStore clearing rules', () => {
  const event = (seq: number, ms: number) => ({ type: 'reading_received', event_id: seq, data: { ...raw(seq, ms) } as unknown as Record<string, unknown> });

  it('seeds from a snapshot and merges the same readings from a poll without duplicates', () => {
    const store = new LiveStore();
    const snap = snapshot({ readings: { recent: [raw(1, 1000), raw(2, 2000)] }, recent_events: [{ type: 'node_seen', event_id: 1, data: { sim_ms: 500, node: 'B' } }] });
    store.ingestSnapshot(snap);
    store.ingestEvent(event(2, 2000));
    store.ingestEvent(event(3, 3000));
    store.ingestSnapshot(snapshot({ readings: { recent: [raw(2, 2000), raw(3, 3000)] } }));
    const s = store.getState();
    expect(s.readings.map(r => r.seq)).toEqual([1, 2, 3]);
    expect(s.changes).toHaveLength(1);
    expect(s.nowAt).toBe(Date.parse('2026-11-01T10:00:10.000Z'));
  });

  it('simulation: clears on a new run_id and on reset', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot({ mode: sim, readings: { recent: [raw(1, 1000)] } }));
    store.ingestEvent({ type: 'node_lost_contact', event_id: 5, data: { sim_ms: 900, node: 'B' } });
    expect(store.getState().readings).toHaveLength(1);
    expect(store.getState().changes).toHaveLength(1);
    store.ingestSnapshot(snapshot({ mode: sim, run_id: 2, readings: { recent: [raw(1, 100)] } }));
    expect(store.getState().readings.map(r => r.at)).toEqual([100]);
    expect(store.getState().changes).toHaveLength(0);
    store.ingestEvent({ type: 'reset', event_id: 9, data: { run_id: 3 } });
    expect(store.getState().readings).toHaveLength(0);
    store.ingestSnapshot(snapshot({ mode: sim, run_id: 3 }));
    expect(store.getState().readings).toHaveLength(0);
  });

  it('hardware: keeps readings across a backend restart and places the new ones on the wall clock', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot({ readings: { recent: [raw(1, 8000)] }, recent_events: [{ type: 'node_lost_contact', event_id: 3, data: { sim_ms: 8000, node: 'B' } }] }));
    const before = store.getState().readings[0].at;
    // Backend restarted 40 s later: its clock starts again at ~0 and event ids restart.
    store.ingestSnapshot(snapshot({ run_id: 1, server_time: '2026-11-01T10:00:52.000Z', clock: { sim_ms: 2000 }, readings: { recent: [raw(2, 1500, { replayed: true, delay_ms: 30_000 })] }, recent_events: [{ type: 'node_lost_contact', event_id: 3, data: { sim_ms: 1500, node: 'C' } }] }));
    const s = store.getState();
    expect(s.readings).toHaveLength(2);
    expect(s.readings[0].at).toBe(before);
    expect(s.readings[1].at).toBe(Date.parse('2026-11-01T10:00:51.500Z'));
    expect(s.changes).toHaveLength(2);
    store.ingestSnapshot(snapshot({ run_id: 1, server_time: '2026-11-01T10:00:53.000Z', clock: { sim_ms: 3000 } }));
    expect(store.getState().readings).toHaveLength(2);
  });

  it('hardware: a reset event does not clear', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot({ readings: { recent: [raw(1, 1000)] } }));
    store.ingestEvent({ type: 'reset', event_id: 2, data: { run_id: 2 } });
    expect(store.getState().readings).toHaveLength(1);
  });

  it('clears everything when the backend comes back in the other mode', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot({ readings: { recent: [raw(1, 1000)] } }));
    store.ingestSnapshot(snapshot({ mode: sim, readings: { recent: [] } }));
    expect(store.getState().readings).toHaveLength(0);
    expect(store.getState().mode).toBe('simulation');
  });

  it('ignores events before the first snapshot and notifies subscribers', () => {
    const store = new LiveStore();
    let calls = 0;
    const off = store.subscribe(() => { calls++; });
    store.ingestEvent(event(1, 1000));
    expect(calls).toBe(0);
    store.ingestSnapshot(snapshot());
    expect(calls).toBe(1);
    off();
    store.ingestEvent(event(1, 1000));
    expect(calls).toBe(1);
  });
});

describe('sinceLastReset', () => {
  const e = (event_id: number, sim_ms?: number) => ({ event_id, data: sim_ms === undefined ? {} : { sim_ms } });
  it('drops the previous run that the backend still lists after a reset', () => {
    expect(sinceLastReset([e(1, 40_000), e(2, 50_000), e(3, 1000), e(4, 2000)]).map(x => x.event_id)).toEqual([3, 4]);
  });
  it('keeps everything in a single run, including events without a time', () => {
    expect(sinceLastReset([e(1, 10), e(2), e(3, 20), e(4, 20)]).map(x => x.event_id)).toEqual([1, 2, 3, 4]);
    expect(sinceLastReset([])).toEqual([]);
  });
  it('keeps only the newest run when there were several resets', () => {
    expect(sinceLastReset([e(1, 90), e(2, 10), e(3, 50), e(4, 5), e(5, 8)]).map(x => x.event_id)).toEqual([4, 5]);
  });
  it('is applied to simulation snapshots only', () => {
    const store = new LiveStore();
    const old = [{ type: 'node_lost_contact', event_id: 1, data: { sim_ms: 90_000, node: 'B' } }, { type: 'node_lost_contact', event_id: 2, data: { sim_ms: 2000, node: 'C' } }];
    store.ingestSnapshot(snapshot({ mode: sim, recent_events: old }));
    expect(store.getState().changes.map(c => c.id)).toEqual([2]);
    const hw = new LiveStore();
    hw.ingestSnapshot(snapshot({ recent_events: old }));
    expect(hw.getState().changes).toHaveLength(2);
  });
});
