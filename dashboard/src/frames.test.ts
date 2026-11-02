import { describe, expect, it } from 'vitest';
import { FRAME_CAP, FRAME_TYPES, afterSnapshot, isFrameRecord, mergeFrames, toFrameRow } from './frames';
import { framesRows, framesRun } from './framesFixture';
import { LiveStore, timeMapFor } from './liveData';

describe('the recorded frames run', () => {
  it('has every kind of record the Packets page has to show', () => {
    const rows = framesRows();
    expect(new Set(rows.map(r => r.header?.type_name))).toEqual(new Set(FRAME_TYPES));
    expect(rows.some(r => r.dup === true)).toBe(true);
    expect(rows.filter(r => r.replayed).length).toBeGreaterThanOrEqual(3);
    expect(rows.some(r => r.replayed && r.header?.type_name === 'LOCAL_FAILOVER_REPORT')).toBe(true);
  });
  it('is made of records the dashboard accepts', () => {
    for (const record of framesRun.records) expect(isFrameRecord(record)).toBe(true);
  });
});

describe('isFrameRecord', () => {
  const good = framesRun.records[0];
  it('rejects shapes it cannot show instead of half-showing them', () => {
    expect(isFrameRecord({ ...good, hex: 'zz' })).toBe(false);
    expect(isFrameRecord({ ...good, size: good.size + 1 })).toBe(false);
    expect(isFrameRecord({ ...good, header: null })).toBe(false); // neither decoded nor rejected
    expect(isFrameRecord({ ...good, id: 0 })).toBe(false);
    expect(isFrameRecord(null)).toBe(false);
  });
  it('accepts a rejected record with no header', () => {
    const refused = { ...good, header: null, payload: null, dup: null, rejected: { reason: 'bad_version' }, summary: 'rejected: bad_version' };
    expect(isFrameRecord(refused)).toBe(true);
  });
});

describe('time on the shared axis', () => {
  it('puts a replayed frame at the time C got it and says when the controller did', () => {
    const late = framesRows().find(r => r.replayed)!;
    expect(late.at).toBe(late.received_ms);
    expect(late.reachedAt).toBe(late.received_ms + late.delay_ms);
  });
  it('hardware: a negative received_ms (after a backend restart) is still a wall-clock time', () => {
    const map = timeMapFor({ mode: 'hardware', server_time: '2026-11-02T10:00:10.000Z', clock: { sim_ms: 10_000 } });
    const row = toFrameRow({ ...framesRun.records[0], received_ms: -4764, replayed: true, delay_ms: 5904 }, map, 1);
    expect(new Date(row.at).toISOString()).toBe('2026-11-02T09:59:55.236Z');
    expect(row.reachedAt - row.at).toBe(5904);
  });
});

describe('frame buffer', () => {
  const rows = framesRows();
  it('keeps one copy per (instance, id)', () => {
    const first = mergeFrames([], rows.slice(0, 5));
    expect(mergeFrames(first, rows.slice(0, 5))).toBe(first);
    expect(mergeFrames(first, rows.slice(3, 8)).map(r => r.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });
  it('treats the same id from another backend start as another frame', () => {
    const merged = mergeFrames(framesRows(1).slice(0, 3), framesRows(2).slice(0, 3));
    expect(merged).toHaveLength(6);
    expect(merged.map(r => r.instance)).toEqual([1, 1, 1, 2, 2, 2]);
  });
  it('keeps the order the controller learned of the frames', () => {
    const merged = mergeFrames(rows.slice(5, 10), rows.slice(0, 5));
    expect(merged.map(r => r.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });
  it('drops the oldest beyond the cap', () => {
    const merged = mergeFrames(rows.slice(0, 5), rows.slice(5, 10), 6);
    expect(merged.map(r => r.id)).toEqual([5, 6, 7, 8, 9, 10]);
    expect(FRAME_CAP).toBe(1000);
  });
});

describe('the ordering rule', () => {
  it('discards records the snapshot already contains', () => {
    const after = afterSnapshot(framesRun.records, framesRun.mid.frames.last_id);
    expect(after[0].id).toBe(framesRun.mid.frames.last_id + 1);
    expect(afterSnapshot(framesRun.records, 0)).toHaveLength(framesRun.records.length);
  });
});

describe('the store', () => {
  const snapshot = (over = {}) => ({ ...framesRun.mid, ...over }) as never;
  it('is seeded by the connect-time snapshot, extended by messages, and does not double-count the overlap', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot());
    expect(store.getState().frames).toHaveLength(framesRun.mid.frames.recent.length);
    store.ingestFrames(framesRun.records); // includes the records the snapshot has
    expect(store.getState().frames.map(r => r.id)).toEqual(framesRun.records.map(r => r.id));
  });
  it('a poll without frames leaves the buffer alone', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot());
    const before = store.getState().frames;
    const poll: Record<string, unknown> = { ...framesRun.mid };
    delete poll.frames;
    store.ingestSnapshot(poll as never);
    expect(store.getState().frames).toBe(before);
  });
  it('ignores things that are not records', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot({ frames: { last_id: 0, recent: [] } }));
    store.ingestFrames([{ id: 'x' }, null, 5]);
    expect(store.getState().frames).toEqual([]);
  });
  it('simulation: a reset clears the buffer', () => {
    const store = new LiveStore();
    store.ingestSnapshot(snapshot());
    store.ingestEvent({ type: 'reset', event_id: 99, data: { run_id: 2 } });
    expect(store.getState().frames).toEqual([]);
  });
  it('hardware: a backend restart keeps the buffer, and ids that start again are different frames', () => {
    const store = new LiveStore();
    const hardware = (server_time: string, sim_ms: number) => ({ ...framesRun.mid, mode: 'hardware', server_time, clock: { sim_ms } }) as never;
    store.ingestSnapshot(hardware('2026-11-02T10:00:20.000Z', 20_000));
    const kept = store.getState().frames.length;
    store.ingestSnapshot(hardware('2026-11-02T10:05:00.000Z', 3_000)); // the backend restarted: its clock starts again
    expect(store.getState().frames).toHaveLength(kept * 2);
    expect(new Set(store.getState().frames.map(r => r.instance)).size).toBe(2);
  });
});
