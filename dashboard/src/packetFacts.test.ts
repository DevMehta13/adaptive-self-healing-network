import { describe, expect, it } from 'vitest';
import { FRAME_TYPES } from './frames';
import type { FrameRow } from './frames';
import { framesRows } from './framesFixture';
import { ackEvidence, frameFacts, matchesFilter, summarize, transmissions } from './packetFacts';

const rows = framesRows();
const row = (over: Partial<FrameRow> & { session?: number; seq?: number; src?: string; ttl?: number; type?: string }, id: number): FrameRow => {
  const base = rows.find(r => r.header?.type_name === (over.type ?? 'DATA'))!;
  return {
    ...base, id, key: `1:${id}`, at: id * 1000, reachedAt: id * 1000, replayed: false, dup: false, ...over,
    header: { ...base.header!, session: over.session ?? base.header!.session, seq: over.seq ?? id, src: over.src ?? 'A', ttl: over.ttl ?? 7 },
  };
};

describe('summary', () => {
  const s = summarize(rows);
  it('counts every frame once, by type', () => {
    expect(s.total).toBe(rows.length);
    expect(Object.values(s.byType).reduce((a, b) => a + b, 0) + s.rejected).toBe(s.total);
    expect(Object.keys(s.byType)).toEqual([...FRAME_TYPES]);
    for (const type of FRAME_TYPES) expect(s.byType[type]).toBe(rows.filter(r => r.header?.type_name === type).length);
  });
  it('counts repeats, replays and rejects', () => {
    expect(s.duplicates).toBe(rows.filter(r => r.dup === true).length);
    expect(s.duplicates).toBeGreaterThanOrEqual(1);
    expect(s.replayed).toBe(rows.filter(r => r.replayed).length);
    expect(s.rejected).toBe(0);
    const refused: FrameRow = { ...rows[0], header: null, payload: null, dup: null, rejected: { reason: 'bad_version' } };
    expect(summarize([refused, rows[1]])).toMatchObject({ total: 2, rejected: 1 });
    expect(summarize([refused]).byType.DATA).toBe(0);
  });
  it('lists the TTL on arrival, highest first, over the decoded frames', () => {
    expect(s.ttl.map(t => t.ttl)).toEqual([...s.ttl.map(t => t.ttl)].sort((a, b) => b - a));
    expect(s.ttl.reduce((n, t) => n + t.count, 0)).toBe(s.total - s.rejected);
  });
});

describe('facts', () => {
  const facts = frameFacts(rows);
  it('TTL on arrival and the transmissions it implies (data TTL 8: one relay is TTL 7, two transmissions)', () => {
    expect(transmissions(8)).toBe(1);
    expect(transmissions(7)).toBe(2);
    expect(facts.dataFrames).toBe(rows.filter(r => r.header?.type_name === 'DATA').length);
    const seven = facts.arrival.find(a => a.ttl === 7);
    expect(seven).toMatchObject({ transmissions: 2 });
    expect(seven!.via.every(v => v === 'B' || v === 'D')).toBe(true); // the relay C could name
    expect(facts.arrival.reduce((n, a) => n + a.count, 0)).toBe(facts.dataFrames);
  });
  it('finds the reading C received twice by its identity (src, session, seq)', () => {
    expect(facts.duplicates.length).toBeGreaterThanOrEqual(1);
    const d = facts.duplicates[0];
    expect(d.flagged).toBeGreaterThanOrEqual(1);
    const copies = rows.filter(r => r.header?.type_name === 'DATA' && r.header.src === d.src && r.header.session === d.session && r.header.seq === d.seq);
    expect(copies.map(r => r.id)).toEqual(d.ids);
    expect(copies.length).toBeGreaterThanOrEqual(2);
  });
  it('lists the replayed frames with the longest delay', () => {
    const late = rows.filter(r => r.replayed);
    expect(facts.replayed.count).toBe(late.length);
    expect(facts.replayed.maxDelayMs).toBe(Math.max(...late.map(r => r.delay_ms)));
    expect(facts.replayed.longest!.delay_ms).toBe(facts.replayed.maxDelayMs);
    expect(facts.replayed.types.LOCAL_FAILOVER_REPORT).toBeGreaterThanOrEqual(1);
  });
  it('sees no reboot in a run where no board restarted', () => {
    expect(facts.reboots).toEqual([]);
  });
  it('a new session from the same src is a reboot, ordered by when C got the frames', () => {
    const list = [row({}, 1), row({}, 2), row({ session: 999 }, 3), row({ session: 999 }, 4)];
    expect(frameFacts(list).reboots).toEqual([{ src: 'A', from: list[1].header!.session, to: 999, at: 3000, id: 3 }]);
    // an old-session frame replayed late (handed up last, received earlier) is not a second reboot
    const lateOld = { ...row({}, 5), at: 1500, replayed: true, delay_ms: 20_000 };
    expect(frameFacts([...list, lateOld]).reboots).toHaveLength(1);
    // another board’s session is its own
    expect(frameFacts([row({}, 1), row({ src: 'B', session: 5 }, 2)]).reboots).toEqual([]);
  });
  it('is quiet about what it cannot say', () => {
    const none = frameFacts([]);
    expect(none).toMatchObject({ dataFrames: 0, arrival: [], reboots: [], duplicates: [] });
    expect(none.replayed).toMatchObject({ count: 0, longest: null });
  });
});

describe('A’s acknowledgement counter', () => {
  const acks = ackEvidence(rows, 100);
  it('comes only from A’s TELEMETRY frames, newest first', () => {
    const fromA = rows.filter(r => r.header?.type_name === 'TELEMETRY' && r.header.src === 'A');
    expect(acks).toHaveLength(fromA.length);
    expect(acks.every(a => a.row.header!.src === 'A' && a.row.header!.type_name === 'TELEMETRY')).toBe(true);
    expect(acks.map(a => a.row.at)).toEqual([...acks.map(a => a.row.at)].sort((x, y) => y - x));
  });
  it('reads the counter from the payload and the step since the previous one', () => {
    const newest = acks[0], older = acks[1];
    const counters = newest.row.payload!.counters as Record<string, number>;
    expect(newest.acknowledged).toBe(counters.acknowledged);
    expect(newest.created).toBe(counters.created);
    expect(newest.change).toBe(newest.acknowledged - older.acknowledged);
    expect(acks.at(-1)!.change).toBeNull(); // nothing earlier to compare with
  });
  it('does not compare across a reboot', () => {
    const t = (id: number, session: number, acknowledged: number) => ({ ...row({ type: 'TELEMETRY', session }, id), payload: { counters: { created: acknowledged, acknowledged } } });
    const list = ackEvidence([t(1, 1, 10), t(2, 1, 14), t(3, 2, 2)]);
    expect(list.map(a => a.change)).toEqual([null, 4, null]);
  });
  it('honours the limit', () => { expect(ackEvidence(rows, 3)).toHaveLength(3); });
});

describe('filter', () => {
  it('matches a type, all, or the rejected frames', () => {
    const telemetry = rows.find(r => r.header?.type_name === 'TELEMETRY')!;
    expect(matchesFilter(telemetry, 'all')).toBe(true);
    expect(matchesFilter(telemetry, 'TELEMETRY')).toBe(true);
    expect(matchesFilter(telemetry, 'DATA')).toBe(false);
    expect(matchesFilter(telemetry, 'rejected')).toBe(false);
    const refused: FrameRow = { ...telemetry, header: null, payload: null, dup: null, rejected: { reason: 'bad_version' } };
    expect(matchesFilter(refused, 'rejected')).toBe(true);
    expect(matchesFilter(refused, 'TELEMETRY')).toBe(false);
  });
});
