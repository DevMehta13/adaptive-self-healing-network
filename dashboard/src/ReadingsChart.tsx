import { useEffect, useMemo, useRef, useState } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { ChangeEvent, ChartReading, TimeMap } from './liveData';
import { SERIES, axisCaption, axisTime, buildChartData, markersFrom, visibleRange, yRange } from './chartData';
import type { ChartData, Marker, WindowChoice } from './chartData';
import { seconds } from './overview';
import type { Theme } from './theme';

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const HEIGHT = 340;

const windows: { label: string; value: WindowChoice }[] = [
  { label: '1 min', value: 60_000 }, { label: '5 min', value: 300_000 }, { label: '15 min', value: 900_000 }, { label: 'All', value: null },
];

interface Shown { readings: ChartReading[]; changes: ChangeEvent[]; nowAt: number | null }
interface View { data: ChartData; markers: Marker[]; map: TimeMap; unit: string; windowMs: WindowChoice; zoom: [number, number] | null; nowAt: number | null }

const pathText = (path: (string | null)[]) => path.map(n => n ?? '?').join(' → ');

/** Fills the tooltip with text nodes only: reading fields come from the backend. */
function fillTooltip(el: HTMLElement, reading: ChartReading, view: View) {
  const lines = [
    `${reading.value.toFixed(view.unit === 'units' ? 2 : 1)} ${view.unit}`,
    `via ${reading.via ?? '?'} · ${pathText(reading.path)}`,
    reading.replayed ? `replayed · held ${seconds(reading.delayMs)}` : 'live',
    reading.replayed ? `reached C ${axisTime(view.map, reading.at)} · controller ${axisTime(view.map, reading.reachedAt)}` : `at C ${axisTime(view.map, reading.at)}`,
  ];
  el.replaceChildren(...lines.map((text, i) => { const line = document.createElement(i === 0 ? 'strong' : 'span'); line.textContent = text; return line; }));
}

export function ReadingsChart({ readings, changes, map, nowAt, theme, unit }: { readings: ChartReading[]; changes: ChangeEvent[]; map: TimeMap; nowAt: number | null; theme: Theme; unit: string }) {
  const [windowMs, setWindowMs] = useState<WindowChoice>(300_000);
  const [frozen, setFrozen] = useState<Shown | null>(null);
  const [zoom, setZoom] = useState<[number, number] | null>(null);
  const shown: Shown = frozen ?? { readings, changes, nowAt };
  const data = useMemo(() => buildChartData(shown.readings), [shown.readings]);
  const markers = useMemo(() => markersFrom(shown.changes), [shown.changes]);

  const viewRef = useRef<View>({ data, markers, map, unit, windowMs, zoom, nowAt: shown.nowAt });
  viewRef.current = { data, markers, map, unit, windowMs, zoom, nowAt: shown.nowAt };
  const host = useRef<HTMLDivElement>(null);
  const tooltip = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);

  const aligned = (d: ChartData): uPlot.AlignedData => [d.xs, ...d.values];

  // The chart object is rebuilt when the theme or the clock kind changes (its colours and axis type are fixed at creation).
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const ink = css('--muted'), grid = css('--line-soft'), surface = css('--surface');
    const colours: Record<string, string> = { B: css('--series-b'), D: css('--series-d'), other: css('--series-other') };
    const markerColours = { failover: css('--warn-stroke'), route: css('--accent-text'), node: css('--bad-stroke'), link: css('--muted-2') };
    const hardware = viewRef.current.map.mode === 'hardware';
    const opts: uPlot.Options = {
      width: Math.max(el.clientWidth, 280), height: HEIGHT, legend: { show: false }, padding: [14, 12, 0, 0],
      cursor: { drag: { x: true, y: false, setScale: false }, points: { size: 9 } },
      scales: {
        x: { time: hardware, range: () => { const v = viewRef.current; const xs = v.data.xs; return visibleRange({ windowMs: v.windowMs, zoom: v.zoom, dataMin: xs.length ? xs[0] : null, dataMax: xs.length ? xs[xs.length - 1] : null, nowAt: v.nowAt }); } },
        y: { range: (_u, min, max) => yRange(Number.isFinite(min) ? min : null, Number.isFinite(max) ? max : null) },
      },
      axes: [
        { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, values: (_u: uPlot, ticks: number[]) => ticks.map(t => t < 0 ? '' : axisTime(viewRef.current.map, t * 1000)) },
        { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, label: unit, labelSize: 22, size: 54 },
      ],
      series: [{}, ...SERIES.map(s => {
        const colour = colours[s.via];
        return s.replayed
          ? { label: `via ${s.via} (replayed)`, stroke: colour, paths: () => null, points: { show: true, size: 9, width: 2, stroke: colour, fill: surface }, spanGaps: false }
          : { label: `via ${s.via}`, stroke: colour, width: 2, points: { show: true, size: 5, stroke: colour, fill: colour }, spanGaps: false };
      })],
      hooks: {
        setSelect: [u => {
          if (u.select.width < 6) return;
          const lo = u.posToVal(u.select.left, 'x'), hi = u.posToVal(u.select.left + u.select.width, 'x');
          u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          if (hi > lo) setZoom([lo, hi]);
        }],
        setCursor: [u => {
          const tip = tooltip.current;
          if (!tip) return;
          const idx = u.cursor.idx;
          const near = idx != null && (u.cursor.left ?? -1) >= 0 && Math.abs(u.valToPos(viewRef.current.data.xs[idx], 'x') - (u.cursor.left ?? 0)) < 36;
          const reading = near && idx != null ? viewRef.current.data.rows[idx] : null;
          if (!reading) { tip.hidden = true; return; }
          fillTooltip(tip, reading, viewRef.current);
          tip.hidden = false;
          const left = (u.cursor.left ?? 0) + u.bbox.left / devicePixelRatio;
          const flip = left > u.over.clientWidth * 0.6;
          tip.style.left = `${flip ? left - tip.offsetWidth - 12 : left + 14}px`;
          tip.style.top = `${Math.max(4, (u.cursor.top ?? 0) - 10)}px`;
        }],
        draw: [u => {
          const { ctx, bbox } = u;
          const dpr = devicePixelRatio;
          ctx.save();
          ctx.beginPath(); ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height); ctx.clip();
          ctx.font = `${11 * dpr}px -apple-system, 'Segoe UI', sans-serif`;
          ctx.lineWidth = dpr; ctx.setLineDash([4 * dpr, 3 * dpr]); ctx.textBaseline = 'top';
          viewRef.current.markers.forEach((marker, i) => {
            const x = Math.round(u.valToPos(marker.at, 'x', true));
            if (x < bbox.left || x > bbox.left + bbox.width) return;
            ctx.strokeStyle = ctx.fillStyle = markerColours[marker.kind];
            ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke();
            const width = ctx.measureText(marker.label).width;
            const textX = x + 4 * dpr + width > bbox.left + bbox.width ? x - 4 * dpr - width : x + 4 * dpr;
            ctx.fillText(marker.label, textX, bbox.top + (3 + (i % 3) * 14) * dpr);
          });
          ctx.restore();
        }],
      },
    };
    const chart = new uPlot(opts, aligned(viewRef.current.data), el);
    plot.current = chart;
    const observer = new ResizeObserver(() => chart.setSize({ width: Math.max(el.clientWidth, 280), height: HEIGHT }));
    observer.observe(el);
    return () => { observer.disconnect(); chart.destroy(); plot.current = null; };
  }, [theme, map.mode, unit]);

  useEffect(() => { plot.current?.setData(aligned(data), true); }, [data, windowMs, zoom, shown.nowAt, theme, map.mode, unit]);

  const pause = () => setFrozen({ readings, changes, nowAt });
  const present = new Set(shown.readings.map(r => r.via === 'B' || r.via === 'D' ? r.via : 'other'));

  return <section className="chart-card" aria-labelledby="chart-title">
    <div className="chart-toolbar">
      <h2 id="chart-title">Temperature over time</h2>
      <div className="chart-controls">
        <div className="segmented" role="group" aria-label="Time window">
          {windows.map(w => <button type="button" key={w.label} aria-pressed={windowMs === w.value} onClick={() => { setWindowMs(w.value); setZoom(null); }}>{w.label}</button>)}
        </div>
        <div className="segmented" role="group" aria-label="Follow new data">
          <button type="button" aria-pressed={frozen === null} onClick={() => setFrozen(null)}>Live</button>
          <button type="button" aria-pressed={frozen !== null} onClick={pause}>Paused</button>
        </div>
        <button type="button" className="plain-button" disabled={zoom === null} onClick={() => setZoom(null)}>Reset zoom</button>
      </div>
    </div>
    <ul className="chart-legend" aria-label="Legend">
      <li><span className="swatch b" />via B</li>
      <li><span className="swatch d" />via D</li>
      {present.has('other') && <li><span className="swatch other" />via a relay C could not name</li>}
      <li><span className="swatch hollow" />hollow: held at C and replayed</li>
      <li><span className="swatch marker" />dashed line: failover, route change, node or link event</li>
    </ul>
    <div className="chart-frame">
      <div ref={host} className="chart-host" role="img" aria-label={`Line chart of ${shown.readings.length} temperature readings in ${unit}. The table below lists the same readings.`} />
      <div ref={tooltip} className="chart-tooltip" hidden />
      {shown.readings.length === 0 && <p className="chart-empty">No readings have reached C yet.</p>}
    </div>
    <p className="chart-caption">{axisCaption(map)}. {frozen ? 'Paused: the chart is frozen; press Live to follow new data. ' : ''}Drag across the chart to zoom.</p>
  </section>;
}
