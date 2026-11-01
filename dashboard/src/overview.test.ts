import { describe, expect, it } from 'vitest';
import type { NodeView, Reading } from './fixtures';
import type { ChangeEvent, TimeMap } from './liveData';
import { ageText, latestReading, nodeLines, readingAgeMs, recentChanges } from './overview';

const reading = (seq: number, received_ms: number, extra: Partial<Reading> = {}): Reading =>
  ({ origin: 'A', session: 1, seq, value: 20, via: 'B', observed_path: ['A', 'B', 'C'], received_ms, replayed: false, delay_ms: 0, ...extra });
const node = (id: NodeView['id'], status: string, extra: Partial<NodeView> = {}) =>
  ({ id, status, session: 1, last_report_age_ms: null, ...extra }) as NodeView;

describe('latestReading', () => {
  it('is undefined without readings', () => expect(latestReading([])).toBeUndefined());
  it('picks the newest by receive time, not by arrival order', () => {
    const late = reading(5, 1000, { replayed: true, delay_ms: 9000 });
    expect(latestReading([reading(9, 20_000), late])?.seq).toBe(9);
  });
  it('lets the later entry win a tie', () => expect(latestReading([reading(1, 500), reading(2, 500)])?.seq).toBe(2));
});

describe('readingAgeMs / ageText', () => {
  it('never goes negative', () => expect(readingAgeMs(1000, reading(1, 4000))).toBe(0));
  it('is the clock difference', () => expect(readingAgeMs(10_000, reading(1, 4000))).toBe(6000));
  it('words ages', () => {
    expect(ageText(300)).toBe('just now');
    expect(ageText(6000)).toBe('6 s ago');
    expect(ageText(125_000)).toBe('2 min 5 s ago');
  });
});

describe('nodeLines', () => {
  const lines = nodeLines([node('A', 'reachable', { last_report_age_ms: 1200 }),
    node('B', 'lost_contact', { source: { observed_by: ['A', 'C'], silent_for_ms: 4632 } }), node('C', 'stale'), node('D', 'unknown')]);
  it('keeps the API wording and roles', () => {
    expect(lines.map(l => `${l.id} ${l.role}: ${l.status}`)).toEqual(['A Source: reachable', 'B Relay: lost contact', 'C USB gateway: stale', 'D Relay: unknown (never heard)']);
  });
  it('explains lost contact with who saw it and never says powered off', () => {
    expect(lines[1].detail).toBe('seen as silent by A, C for 4.6 s');
    expect(lines.some(l => /power/i.test(l.status + l.detail))).toBe(false);
  });
  it('shows report age', () => expect(lines[0].detail).toBe('last report 1.2 s old'));
});

describe('recentChanges', () => {
  const sim: TimeMap = { mode: 'simulation', offsetMs: 0 };
  const change = (id: number, type: string, at: number, data: Record<string, unknown> = {}): ChangeEvent => ({ key: `1:${id}`, id, type, at, data });
  const changes = [
    change(1, 'node_seen', 1000, { node: 'B' }), change(2, 'telemetry', 2000),
    change(3, 'environment', 3000, { label: 'Stop B (injected by presenter, simulation)' }),
    change(4, 'local_failover', 4000, { to: 'D' }),
  ];
  it('skips untitled events and is newest first', () => {
    const out = recentChanges(changes, 10_000, sim);
    expect(out.map(c => c.id)).toEqual(['1:4', '1:3', '1:1']);
    expect(out[0]).toMatchObject({ title: 'A switched locally to D', time: '00:04', ago: '6 s ago' });
    expect(out[1].injected).toBe(true);
  });
  it('respects the limit', () => expect(recentChanges(changes, 10_000, sim, 2)).toHaveLength(2));
  it('has no age without a clock', () => expect(recentChanges(changes, null, sim)[0].ago).toBe(''));
});
