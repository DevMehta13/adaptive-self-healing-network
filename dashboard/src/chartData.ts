import type { ChangeEvent, ChartReading, TimeMap } from './liveData';

/** Series in a fixed order: relay B, relay D, anything else; each live (line + filled point) or replayed (hollow point). */
export const SERIES = [
  { key: 'B-live', via: 'B', replayed: false }, { key: 'B-replay', via: 'B', replayed: true },
  { key: 'D-live', via: 'D', replayed: false }, { key: 'D-replay', via: 'D', replayed: true },
  { key: 'O-live', via: 'other', replayed: false }, { key: 'O-replay', via: 'other', replayed: true },
] as const;

export const seriesKey = (r: Pick<ChartReading, 'via' | 'replayed'>) =>
  `${r.via === 'B' || r.via === 'D' ? r.via : 'O'}-${r.replayed ? 'replay' : 'live'}`;

const median = (values: number[]) => {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};

/** About three reading intervals, from the live readings' own spacing; never below 5 s so a slow sensor does not break the line. */
export function gapThresholdMs(liveAts: number[]): number {
  if (liveAts.length < 4) return 10_000;
  const diffs: number[] = [];
  for (let i = 1; i < liveAts.length; i++) diffs.push(liveAts[i] - liveAts[i - 1]);
  return Math.max(5000, 3 * median(diffs));
}

export interface ChartData {
  /** Seconds on the axis, strictly increasing. */
  xs: number[];
  /** One array per entry of SERIES. */
  values: (number | null)[][];
  /** The reading behind each x, or null for a row that only breaks the line. */
  rows: (ChartReading | null)[];
}

/** Aligned arrays for uPlot. A break row (all null) goes after a live reading that is followed by a gap, so no line crosses it. */
export function buildChartData(readings: ChartReading[]): ChartData {
  const sorted = [...readings].sort((a, b) => a.at - b.at);
  const live = sorted.filter(r => !r.replayed);
  const gap = gapThresholdMs(live.map(r => r.at));
  const entries: { x: number; reading: ChartReading | null }[] = sorted.map(reading => ({ x: reading.at / 1000, reading }));
  for (let i = 1; i < live.length; i++) {
    if (live[i].at - live[i - 1].at > gap) entries.push({ x: live[i - 1].at / 1000 + 0.0005, reading: null });
  }
  entries.sort((a, b) => a.x - b.x);
  const xs: number[] = [];
  for (const entry of entries) {
    const previous = xs.at(-1);
    entry.x = previous !== undefined && entry.x <= previous ? previous + 0.001 : entry.x;
    xs.push(entry.x);
  }
  const values = SERIES.map(() => new Array<number | null>(entries.length).fill(null));
  entries.forEach((entry, row) => {
    if (!entry.reading) return;
    const index = SERIES.findIndex(s => s.key === seriesKey(entry.reading!));
    values[index][row] = entry.reading.value;
  });
  return { xs, values, rows: entries.map(e => e.reading) };
}

/** Padded y range, never narrower than `minSpan`, so one-degree sensor steps do not look like spikes. */
export function yRange(min: number | null, max: number | null, minSpan = 2): [number, number] {
  if (min === null || max === null) return [0, minSpan];
  let lo = min, hi = max;
  if (hi - lo < minSpan) { const mid = (hi + lo) / 2; lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
  const pad = (hi - lo) * 0.1;
  return [lo - pad, hi + pad];
}

export type WindowChoice = 60_000 | 300_000 | 900_000 | null;

/** The x range in seconds. A zoom wins; otherwise the window ends at the newest data (or `nowAt`) and, for All, spans everything. */
export function visibleRange(opts: { windowMs: WindowChoice; zoom: [number, number] | null; dataMin: number | null; dataMax: number | null; nowAt: number | null }): [number, number] {
  if (opts.zoom) return opts.zoom;
  const newest = Math.max(opts.dataMax ?? -Infinity, (opts.nowAt ?? -Infinity) / 1000);
  if (!Number.isFinite(newest)) return [0, 60];
  if (opts.windowMs === null) {
    const oldest = opts.dataMin ?? newest - 60;
    return oldest === newest ? [oldest - 30, newest + 30] : [oldest, newest];
  }
  return [newest - opts.windowMs / 1000, newest];
}

export interface Marker { at: number; label: string; kind: 'failover' | 'route' | 'node' | 'link' }

const nodePath = (value: unknown) => Array.isArray(value) ? value.map(String).join('→') : '';

/** Chart markers from the change buffer: failover, a route change to a new path, node lost / regained, controller link lost / restored. `at` is in seconds. */
export function markersFrom(changes: ChangeEvent[]): Marker[] {
  const out: Marker[] = [];
  let lastPath: string | null = null;
  for (const change of [...changes].sort((a, b) => a.at - b.at || a.id - b.id)) {
    const d = change.data;
    const at = change.at / 1000;
    switch (change.type) {
      case 'local_failover': out.push({ at, kind: 'failover', label: `failover ${d.node ?? '?'}→${d.to ?? 'none'}` }); break;
      case 'route_active': {
        const path = nodePath(d.path);
        if (lastPath !== null && path && path !== lastPath) out.push({ at, kind: 'route', label: `route ${path}` });
        if (path) lastPath = path;
        break;
      }
      case 'node_lost_contact': out.push({ at, kind: 'node', label: `${d.node ?? '?'} lost` }); break;
      case 'node_regained_contact': out.push({ at, kind: 'node', label: `${d.node ?? '?'} back` }); break;
      case 'gateway_link_lost': out.push({ at, kind: 'link', label: 'link lost' }); break;
      case 'gateway_link_restored': out.push({ at, kind: 'link', label: 'link back' }); break;
    }
  }
  return out;
}

const pad2 = (n: number) => String(n).padStart(2, '0');

/** Hardware: wall-clock HH:MM:SS. Simulation: simulated mm:ss. */
export function axisTime(map: TimeMap, at: number): string {
  if (map.mode === 'hardware') {
    const d = new Date(at);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }
  const total = Math.max(0, Math.floor(at / 1000));
  return `${pad2(Math.floor(total / 60))}:${pad2(total % 60)}`;
}

export const axisCaption = (map: TimeMap) => map.mode === 'hardware'
  ? 'Time: the laptop’s clock, when C received each reading'
  : 'Time: simulated clock (mm:ss), when C received each reading';
