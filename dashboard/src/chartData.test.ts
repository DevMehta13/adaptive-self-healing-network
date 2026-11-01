import { describe, expect, it } from 'vitest';
import type { ChangeEvent, ChartReading, TimeMap } from './liveData';
import { SERIES, axisTime, buildChartData, gapThresholdMs, markersFrom, seriesKey, visibleRange, yRange } from './chartData';

const r = (seq: number, atSec: number, via: string | null = 'B', replayed = false, value = 20): ChartReading =>
  ({ key: `A/1/${seq}`, origin: 'A', session: 1, seq, value, via, path: ['A', via, 'C'], at: atSec * 1000, reachedAt: atSec * 1000, replayed, delayMs: 0 });
const series = (name: string) => SERIES.findIndex(s => s.key === name);

describe('gapThresholdMs', () => {
  it('is three reading intervals, at least 5 s', () => {
    expect(gapThresholdMs(Array.from({ length: 10 }, (_, i) => i * 2000))).toBe(6000);
    expect(gapThresholdMs(Array.from({ length: 10 }, (_, i) => i * 500))).toBe(5000);
  });
  it('has a default while there are few readings', () => expect(gapThresholdMs([0, 2000])).toBe(10_000));
});

describe('buildChartData', () => {
  it('splits by relay and by live / replayed', () => {
    const data = buildChartData([r(1, 0, 'B'), r(2, 2, 'D'), r(3, 4, 'B', true, 18), r(4, 6, null)]);
    expect(data.values[series('B-live')]).toEqual([20, null, null, null]);
    expect(data.values[series('D-live')]).toEqual([null, 20, null, null]);
    expect(data.values[series('B-replay')]).toEqual([null, null, 18, null]);
    expect(data.values[series('O-live')]).toEqual([null, null, null, 20]);
    expect(seriesKey({ via: 'D', replayed: true })).toBe('D-replay');
  });
  it('puts a break row where live readings have a gap, so no line crosses it', () => {
    const live = Array.from({ length: 6 }, (_, i) => r(i, i * 2));
    const after = [...live, r(10, 60)];
    const data = buildChartData(after);
    expect(data.rows).toHaveLength(8);
    const breakAt = data.rows.indexOf(null);
    expect(breakAt).toBe(6);
    expect(data.values.every(v => v[breakAt] === null)).toBe(true);
    expect(data.xs[breakAt]).toBeGreaterThan(10);
    expect(data.xs[breakAt]).toBeLessThan(60);
  });
  it('does not break the line for a gap that replayed readings filled in', () => {
    const data = buildChartData([...Array.from({ length: 6 }, (_, i) => r(i, i * 2)), r(20, 60), r(30, 20, 'B', true), r(31, 22, 'B', true)]);
    // the live line still has a break row, but the replayed points sit inside the gap on their own series
    expect(data.values[series('B-replay')].filter(v => v !== null)).toHaveLength(2);
    const rowsInGap = data.rows.filter(x => x && x.replayed);
    expect(rowsInGap).toHaveLength(2);
  });
  it('keeps x strictly increasing even for readings at the same instant', () => {
    const data = buildChartData([r(1, 5), r(2, 5), r(3, 5)]);
    expect(data.xs[1]).toBeGreaterThan(data.xs[0]);
    expect(data.xs[2]).toBeGreaterThan(data.xs[1]);
  });
  it('handles no readings', () => expect(buildChartData([]).xs).toEqual([]));
});

describe('yRange', () => {
  it('pads, and widens to at least a span of 2', () => {
    const [lo, hi] = yRange(20, 20);
    expect(hi - lo).toBeGreaterThanOrEqual(2);
    expect((lo + hi) / 2).toBeCloseTo(20);
    const [a, b] = yRange(10, 30);
    expect(a).toBeLessThan(10); expect(b).toBeGreaterThan(30);
  });
  it('has a default without data', () => expect(yRange(null, null)).toEqual([0, 2]));
});

describe('visibleRange', () => {
  it('ends at the newest data or clock for a window', () => {
    expect(visibleRange({ windowMs: 60_000, zoom: null, dataMin: 0, dataMax: 100, nowAt: 110_000 })).toEqual([50, 110]);
    expect(visibleRange({ windowMs: 60_000, zoom: null, dataMin: 0, dataMax: 100, nowAt: null })).toEqual([40, 100]);
  });
  it('spans everything for All and obeys a zoom', () => {
    expect(visibleRange({ windowMs: null, zoom: null, dataMin: 10, dataMax: 100, nowAt: null })).toEqual([10, 100]);
    expect(visibleRange({ windowMs: 60_000, zoom: [5, 6], dataMin: 0, dataMax: 100, nowAt: null })).toEqual([5, 6]);
  });
  it('copes with no data', () => expect(visibleRange({ windowMs: null, zoom: null, dataMin: null, dataMax: null, nowAt: null })).toEqual([0, 60]));
});

describe('markersFrom', () => {
  const ev = (id: number, type: string, atSec: number, data: Record<string, unknown> = {}): ChangeEvent => ({ key: `1:${id}`, id, type, at: atSec * 1000, data });
  it('keeps the five kinds the chart shows and ignores the rest', () => {
    const markers = markersFrom([
      ev(1, 'node_seen', 1, { node: 'B' }), ev(2, 'environment', 2), ev(3, 'node_lost_contact', 5, { node: 'B' }),
      ev(4, 'local_failover', 6, { node: 'A', to: 'D' }), ev(5, 'node_regained_contact', 20, { node: 'B' }),
      ev(6, 'gateway_link_lost', 30), ev(7, 'gateway_link_restored', 40), ev(8, 'policy_changed', 41, { new: 'x' }),
    ]);
    expect(markers.map(m => m.label)).toEqual(['B lost', 'failover A→D', 'B back', 'link lost', 'link back']);
  });
  it('marks a route only when the path differs from the previous active route', () => {
    const markers = markersFrom([
      ev(1, 'route_active', 1, { path: ['A', 'B', 'C'] }), ev(2, 'route_active', 5, { path: ['A', 'B', 'C'] }),
      ev(3, 'route_active', 9, { path: ['A', 'D', 'C'] }), ev(4, 'route_active', 12, { path: ['A', 'D', 'C'] }),
    ]);
    expect(markers).toEqual([{ at: 9, kind: 'route', label: 'route A→D→C' }]);
  });
  it('uses seconds on the axis', () => expect(markersFrom([ev(1, 'gateway_link_lost', 12.5)])[0].at).toBe(12.5));
});

describe('axisTime', () => {
  it('shows simulated time as mm:ss', () => {
    const sim: TimeMap = { mode: 'simulation', offsetMs: 0 };
    expect(axisTime(sim, 125_900)).toBe('02:05');
  });
  it('shows wall-clock time as HH:MM:SS', () => {
    const hw: TimeMap = { mode: 'hardware', offsetMs: 0 };
    const at = new Date(2026, 10, 1, 9, 5, 7).getTime();
    expect(axisTime(hw, at)).toBe('09:05:07');
  });
});
