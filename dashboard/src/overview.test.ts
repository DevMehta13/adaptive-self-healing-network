import { describe, expect, it } from 'vitest';
import type { NodeView, Reading } from './fixtures';
import { latestReading, nodeLines, readingAgeMs } from './overview';

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
