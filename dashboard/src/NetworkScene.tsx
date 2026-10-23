import type { ReactNode } from 'react';
import type { Count, LinkView, NetworkSnapshot, NodeId } from './fixtures';

const positions: Record<NodeId, [number, number]> = { A: [62, 170], B: [235, 55], C: [408, 170], D: [235, 285] };
const edges: [NodeId, NodeId][] = [['A', 'B'], ['B', 'C'], ['A', 'D'], ['D', 'C']];
const names: Record<NodeId, string> = { A: 'Source', B: 'Relay', C: 'USB gateway', D: 'Relay' };
const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;
const age = (ms: number | null | undefined) => ms == null ? 'age unknown' : `${seconds(ms)} old`;
const readable = (value: string) => value.replaceAll('_', ' ');
const routeText = (path?: (NodeId | null)[]) => path?.length ? path.map(n => n ?? '?').join(' → ') : 'None recorded';

function CountCard({ label, count }: { label: string; count: Count | null | undefined }) {
  return <div className="count-card"><span>{label}</span><strong>{count?.value ?? '—'}</strong><small>{count ? `${count.known_by}${count.age_ms != null ? ` · ${age(count.age_ms)}` : ''}` : 'Awaiting telemetry'}</small></div>;
}

function Sparkline({ values, unit }: { values: number[]; unit: string }) {
  if (values.length < 2) return null;
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 200},${34 - ((v - lo) / span) * 28}`).join(' ');
  return <figure className="sparkline"><svg viewBox="0 0 200 40" role="img" aria-label={`Last ${values.length} readings, ${lo.toFixed(1)} to ${hi.toFixed(1)} ${unit}`}><polyline points={pts} fill="none" stroke="#315d47" strokeWidth="2" /></svg><figcaption>Last {values.length} readings: {lo.toFixed(1)}–{hi.toFixed(1)} {unit}</figcaption></figure>;
}

export function NetworkScene({ view, timelineTitle, timeline, unit = 'units' }: { view: NetworkSnapshot; timelineTitle: string; timeline: ReactNode; unit?: string }) {
  const shown = (v: number) => unit === 'units' ? v.toFixed(2) : v.toFixed(1);
  const latest = view.readings.recent.at(-1);
  const activePath = view.route.state === 'active' && view.route.kind === 'route' ? view.route.path : undefined;
  const activeEdges = new Set((activePath ?? []).slice(0, -1).map((n, i) => [n, activePath?.[i + 1]].sort().join('-')));
  const linkFor = (a: NodeId, b: NodeId): LinkView | undefined => view.links.find(l => (l.a === a && l.b === b) || (l.a === b && l.b === a));

  return <>
    {view.gateway.state === 'lost' && <div className="gateway-alert" role="note"><strong>Network view stale.</strong> C has stopped reporting to the controller. The graph and counts show the last information it received; they do not reveal what the boards are doing now.</div>}
    <div className="network-grid">
      <div className="graph-panel"><div className="panel-heading"><div><span className="eyebrow">Configured diamond</span><h3>Four-board network</h3></div><span className="small-note">Controller view</span></div>
        <svg className="network-svg" viewBox="0 0 470 340" role="img" aria-label={`Four-node diamond. Planned route: ${activePath ? routeText(activePath) : 'not active'}. Gateway: ${view.gateway.state}.`}>
          {edges.map(([a, b]) => { const link = linkFor(a, b); const key = [a, b].sort().join('-'); const planned = activeEdges.has(key); return <g key={key}><line className={`network-edge ${link?.state ?? 'unknown'}`} x1={positions[a][0]} y1={positions[a][1]} x2={positions[b][0]} y2={positions[b][1]} /><text className="edge-label" x={(positions[a][0] + positions[b][0]) / 2} y={(positions[a][1] + positions[b][1]) / 2 - 8}>{a}–{b}: {link ? readable(link.state) : 'not reported'}</text>{planned && <text className="planned-label" x={(positions[a][0] + positions[b][0]) / 2} y={(positions[a][1] + positions[b][1]) / 2 + 16}>planned</text>}</g>; })}
          {view.nodes.map(n => <g key={n.id}><circle className={`network-node ${n.status}`} cx={positions[n.id][0]} cy={positions[n.id][1]} r="30" /><text className="node-letter" x={positions[n.id][0]} y={positions[n.id][1] + 7}>{n.id}</text></g>)}
        </svg>
        <div className="node-list">{view.nodes.map(n => <div key={n.id}><strong>{n.id} · {names[n.id]}</strong><span>{readable(n.status)}{n.source ? ` · reported by ${n.source.observed_by.join(', ')}` : ''}</span></div>)}</div>
        <p className="graph-note">Lines are configured logical links. Solid means both ends reported them up; dashed means one-way or stale. A grey line has no usable link evidence. “Planned” marks the active controller route; it does not prove a packet arrived.</p>
      </div>
      <div className="evidence-panel"><div><span className="eyebrow">Keep plan and evidence apart</span><h3>Route and delivery</h3></div>
        <div className="evidence-block"><span>Controller’s planned route</span><strong>{activePath ? routeText(activePath) : view.route.kind === 'no_route' ? 'No available route' : `No active route · ${readable(view.route.state)}`}</strong><small>{activePath ? `Version ${view.route.version ?? '—'} · ${readable(view.route.policy ?? 'unknown policy')}${view.route.cost_ms != null ? ` · ${view.route.cost_ms} ms link RTT sum` : ''}` : 'Only an acknowledged active route is shown as a plan.'}</small></div>
        <div className="evidence-block"><span>Last reading observed at C</span><strong>{latest ? routeText(latest.observed_path) : 'No reading received yet'}</strong><small>{latest ? `${shown(latest.value)} ${unit} · via ${latest.via ?? '?'}${latest.replayed ? ` · delayed ${seconds(latest.delay_ms)}` : ''}` : 'Observed paths come from C’s sender and TTL evidence.'}</small></div>
        <div className="evidence-block"><span>Gateway link</span><strong>{readable(view.gateway.state)}</strong><small>{view.gateway.network_view ?? 'No gateway message yet'}</small></div>
        <p className="evidence-note">A local failover can move packets through D before the controller confirms a new route. A replayed report describes an earlier event, received later.</p>
      </div>
    </div>
    <div className="counts-grid"><CountCard label="Created at A" count={view.counts.created} /><CountCard label="Delivered at C" count={view.counts.delivered} /><CountCard label="Acknowledged at A" count={view.counts.acknowledged} /><CountCard label="Lost at A" count={view.counts.lost} /><CountCard label="Lost at C" count={view.counts.lost_at_c} /><CountCard label="Lost in total" count={view.counts.lost_total} /><CountCard label="No route at A" count={view.counts.no_route} /><CountCard label="Delivered late" count={view.counts.delivered_late} /></div>
    <div className="details-grid"><div className="detail-panel"><h3>Recent readings</h3><Sparkline values={view.readings.recent.slice(-20).map(r => r.value)} unit={unit} />{view.readings.recent.length ? <ul className="reading-list">{[...view.readings.recent].slice(-5).reverse().map(r => <li key={`${r.origin}-${r.session}-${r.seq}`}><span>{shown(r.value)} {unit} received</span><strong>{routeText(r.observed_path)}</strong><small>{seconds(r.received_ms)} at C{r.replayed ? ` · delayed ${seconds(r.delay_ms)}` : ''}</small></li>)}</ul> : <p className="empty-state">No delivered reading has been recorded yet.</p>}</div>
      <div className="detail-panel"><h3>{timelineTitle}</h3>{timeline}</div></div>
  </>;
}
