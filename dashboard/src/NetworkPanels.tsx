import { useEffect, useState } from 'react';
import type { LinkView, NodeId } from './fixtures';
import { policyName } from './live';
import type { LiveSnapshot } from './live';
import { along, directions, edgeKey, edges, geometryFor, labelPosition, linkText, nodeFacts } from './graphModel';
import type { ChangeEvent, TimeMap } from './liveData';
import { nodeRoles, seconds } from './overview';
import { PAGE_SIZE, buildTimeline, filterTimeline, visibleCount } from './timeline';
import type { Filter } from './timeline';
import type { Live } from './useLive';

const readable = (value: string) => value.replaceAll('_', ' ');
const pathText = (path?: (string | null)[]) => path?.length ? path.map(n => n ?? '?').join(' → ') : '';

function useNarrow(query = '(max-width: 600px)') {
  const [narrow, setNarrow] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia(query).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const list = window.matchMedia(query);
    const update = () => setNarrow(list.matches);
    update();
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, [query]);
  return narrow;
}

/** The diamond, larger than before. Link labels sit off the lines, the planned route has a halo, trouble has a badge. */
export function NetworkGraph({ view }: { view: LiveSnapshot }) {
  const g = geometryFor(useNarrow());
  const { positions, centre, radius } = g;
  const active = view.route.state === 'active' && view.route.kind === 'route' ? view.route.path : undefined;
  const planned = new Set((active ?? []).slice(0, -1).map((n, i) => edgeKey(n, active![i + 1])));
  const linkFor = (a: NodeId, b: NodeId): LinkView | undefined => view.links.find(l => (l.a === a && l.b === b) || (l.a === b && l.b === a));
  return <div className="graph-panel big-graph">
    <div className="panel-heading"><div><span className="eyebrow">Configured diamond</span><h3>Four-board network</h3></div><span className="small-note">Controller view</span></div>
    <svg className="network-svg big" viewBox={`0 0 ${g.width} ${g.height}`} role="img" aria-label={`Four-node diamond. Planned route: ${active ? pathText(active) : 'not active'}. Gateway: ${view.gateway.state}.`}>
      {edges.map(([a, b]) => {
        const link = linkFor(a, b);
        const isPlanned = planned.has(edgeKey(a, b));
        const text = linkText(link);
        const label = labelPosition(positions[a], positions[b], centre, g.labelOffset);
        const badge = along(positions[a], positions[b], 0.5);
        return <g key={edgeKey(a, b)}>
          {isPlanned && <line className="route-halo" x1={positions[a][0]} y1={positions[a][1]} x2={positions[b][0]} y2={positions[b][1]} />}
          <line className={`network-edge ${link?.state ?? 'unknown'}`} x1={positions[a][0]} y1={positions[a][1]} x2={positions[b][0]} y2={positions[b][1]} />
          {isPlanned && <g transform={`translate(${badge.x} ${badge.y})`}><rect className="planned-badge" x="-29" y="-10" width="58" height="20" rx="10" /><text className="planned-badge-text" y="4">planned</text></g>}
          <g transform={`translate(${label.x} ${label.y})`}>
            <rect className={`link-label ${text.tone}`} x={-g.labelW / 2} y={-g.labelH / 2} width={g.labelW} height={g.labelH} rx="9" />
            <text className="link-name" y="-5">{a}–{b}</text>
            <text className={`link-state ${text.tone}`} y="11">{text.state}{text.detail ? ` · ${text.detail}` : ''}</text>
          </g>
        </g>;
      })}
      {view.nodes.map(n => {
        const [x, y] = positions[n.id];
        const trouble = n.status === 'lost_contact' || n.status === 'stale' || n.status === 'unknown';
        const below = n.id !== 'B';
        return <g key={n.id}>
          <circle className={`network-node ${n.status}`} cx={x} cy={y} r={radius} />
          <text className="node-letter" x={x} y={y + 8}>{n.id}</text>
          {trouble && <g transform={`translate(${x + radius - 8} ${y - radius + 8})`}><circle className={`node-badge ${n.status}`} r="11" /><text className="node-badge-text" y="4">{n.status === 'unknown' ? '?' : '!'}</text></g>}
          <text className="node-caption" x={x} y={below ? y + radius + 18 : y - radius - 24}>{nodeRoles[n.id]}</text>
          <text className={`node-state ${n.status}`} x={x} y={below ? y + radius + 34 : y - radius - 8}>{readable(n.status)}</text>
        </g>;
      })}
    </svg>
    <ul className="graph-legend" aria-label="Legend">
      <li><svg width="34" height="10" aria-hidden="true"><line className="network-edge up" x1="0" y1="5" x2="34" y2="5" /></svg>up: both ends reported it</li>
      <li><svg width="34" height="10" aria-hidden="true"><line className="network-edge one_way" x1="0" y1="5" x2="34" y2="5" /></svg>dashed amber: one-way or stale</li>
      <li><svg width="34" height="10" aria-hidden="true"><line className="network-edge down" x1="0" y1="5" x2="34" y2="5" /></svg>dashed red: down</li>
      <li><svg width="34" height="10" aria-hidden="true"><line className="network-edge unknown" x1="0" y1="5" x2="34" y2="5" /></svg>grey: no link evidence</li>
      <li><svg width="34" height="10" aria-hidden="true"><line className="route-halo" x1="0" y1="5" x2="34" y2="5" /></svg>planned route (controller)</li>
    </ul>
    <p className="graph-note">Lines are configured logical links. “Planned” marks the controller’s active route; it does not prove a packet arrived. Link costs are simulated or measured round trips, not a promise.</p>
  </div>;
}

/** Planned route and last observed path side by side, plus the gateway link. */
export function RoutePanel({ view, unit }: { view: LiveSnapshot; unit: string }) {
  const active = view.route.state === 'active' && view.route.kind === 'route' ? view.route.path : undefined;
  const latest = view.readings.recent.at(-1);
  const shown = (v: number) => unit === 'units' ? v.toFixed(2) : v.toFixed(1);
  return <div className="evidence-panel route-panel">
    <div><span className="eyebrow">Keep plan and evidence apart</span><h3>Route and delivery</h3></div>
    <div className="route-pair">
      <div className="planned"><span className="label">Planned by the controller</span>
        <p className="path">{active ? pathText(active) : view.route.kind === 'no_route' ? 'No available route' : `No active route · ${readable(view.route.state)}`}</p>
        <small>{active ? `Version ${view.route.version ?? '—'} · ${readable(view.route.policy ?? 'unknown policy')}${view.route.cost_ms != null ? ` · ${view.route.cost_ms} ms link RTT sum` : ''}` : 'Only an acknowledged active route is shown as a plan.'}</small></div>
      <div className="observed"><span className="label">Last observed at C</span>
        <p className="path">{latest ? pathText(latest.observed_path) : 'No reading yet'}</p>
        <small>{latest ? `${shown(latest.value)} ${unit} · via ${latest.via ?? '?'}${latest.replayed ? ` · delayed ${seconds(latest.delay_ms)}` : ''}` : 'Observed paths come from C’s sender and TTL evidence.'}</small></div>
    </div>
    <div className="evidence-block"><span>Gateway link (controller to C)</span><strong>{readable(view.gateway.state)}</strong><small>{view.gateway.network_view ?? 'No gateway message yet'}</small></div>
    <p className="evidence-note">A local failover can move packets through D before the controller confirms a new route. A replayed report describes an earlier event, received later.</p>
  </div>;
}

export function NodeCards({ view, changes, map }: { view: LiveSnapshot; changes: ChangeEvent[]; map: TimeMap }) {
  return <ul className="node-cards" aria-label="Boards">
    {view.nodes.map(node => {
      const facts = nodeFacts(node, changes, map);
      return <li key={node.id} className={`node-card ${facts.tone}`}>
        <div className="node-card-head"><strong>{node.id} · {facts.role}</strong><span className={`state-pill ${facts.tone}`}>{facts.status}</span></div>
        <ul>{facts.facts.map(f => <li key={f}>{f}</li>)}</ul>
      </li>;
    })}
  </ul>;
}

function ActionButton({ live, label, path, body, children, disabled = false }: { live: Live; label: string; path: string; body: Record<string, unknown>; children?: string; disabled?: boolean }) {
  const busy = live.pending === label;
  return <button type="button" className={busy ? 'busy' : ''} aria-busy={busy} disabled={!live.controlsReady || disabled} onClick={() => void live.act(label, path, body)}>{busy ? 'Applying…' : children ?? label}</button>;
}

/** Routing policy (both modes) and the presenter controls (simulation only) in one panel. */
export function ControlsPanel({ view, live }: { view: LiveSnapshot; live: Live }) {
  const simulation = view.mode === 'simulation';
  const conditions = view.presenter.conditions;
  const stopped = conditions?.stopped_nodes ?? [];
  const linksDown = conditions?.links_down ?? [];
  const [direction, setDirection] = useState(directions[0]);
  const isDown = linksDown.includes(direction);
  const attached = conditions?.gateway_attached ?? true;
  const { pending, feedback, feedbackOk, controlsReady, act } = live;
  return <section className="control-panel" aria-labelledby="controls-title">
    <div className="control-heading"><div><span className="eyebrow">Controls</span><h3 id="controls-title">{simulation ? 'Change the demonstration' : 'Routing policy'}</h3></div>
      <span>{simulation ? 'Presenter actions change only the simulated environment; the controller learns of them through C' : 'Hardware mode: simulation controls unavailable'}</span></div>
    {!simulation && <p className="hardware-note" role="note">These controls act on the simulator, so they are turned off while real boards are connected (the backend would answer 409). To test failover on hardware, unplug relay B yourself; to test controller loss, stop the backend. The routing policy below still works.</p>}
    {simulation && <div className="control-groups four">
      <div className="control-group"><strong>Relays</strong><p>Stop a relay to see whether A fails over locally while the controller catches up.</p>
        <div className="relay-rows">{(['B', 'D'] as const).map(id => { const off = stopped.includes(id); return <div key={id} className="relay-row"><span>Relay {id} <em className={off ? 'bad' : 'ok'}>{off ? 'stopped' : 'running'}</em></span>
          <ActionButton live={live} label={off ? `Start ${id}` : `Stop ${id}`} path="/api/control/node" body={{ node: id, action: off ? 'start' : 'stop' }} /></div>; })}</div></div>
      <div className="control-group"><strong>One link direction</strong><p>Take one direction of a link down, then bring it back.</p>
        <label className="visually-hidden" htmlFor="link-direction">Link direction</label>
        <select id="link-direction" value={direction} onChange={e => setDirection(e.target.value)} disabled={!controlsReady}>{directions.map(d => <option key={d} value={d}>{d.replace('->', ' → ')}{linksDown.includes(d) ? ' (down)' : ''}</option>)}</select>
        <ActionButton live={live} label={isDown ? `Bring ${direction} up` : `Take ${direction} down`} path="/api/control/link" body={{ link: direction, down: !isDown }} /></div>
      <div className="control-group"><strong>C’s laptop link</strong><p>Detach C to make it hold reports, then attach it to watch the replay.</p>
        <ActionButton live={live} label={attached ? 'Detach C' : 'Attach C'} path="/api/control/gateway" body={{ attached: !attached }} /></div>
      <div className="control-group"><strong>Simulation run</strong><p>Pause to inspect the current state, or reset this scenario to its beginning.</p>
        <div className="control-actions"><ActionButton live={live} label={view.clock.paused ? 'Resume' : 'Pause'} path={view.clock.paused ? '/api/control/resume' : '/api/control/pause'} body={{}} />
          <ActionButton live={live} label="Reset run" path="/api/control/reset" body={{ scenario: view.scenario }} disabled={!view.scenario} /></div></div>
    </div>}
    <div className="policy-control"><label htmlFor="routing-policy">Routing policy</label>
      <select id="routing-policy" value={view.policy.current} disabled={!controlsReady} onChange={e => void act(`Policy: ${policyName(e.target.value)}`, '/api/policy', { policy: e.target.value })}>{view.policy.available.map(key => <option key={key} value={key}>{policyName(key)}</option>)}</select>
      <p>Policy changes affect future route choices. A better route must win 3 evaluations in a row (about 15 s) before it replaces an intact one; a broken route is replaced at once.</p></div>
    <div className="control-status" aria-live="polite">
      {pending && <p className="control-feedback" role="status">Applying {pending}…</p>}
      {!pending && feedback && <p className={`control-feedback ${feedbackOk ? '' : 'error'}`} role={feedbackOk ? 'status' : 'alert'}>{feedback}</p>}
    </div>
  </section>;
}

const filters: { id: Filter; label: string }[] = [
  { id: 'all', label: 'Everything' }, { id: 'controller', label: 'What the controller noticed' }, { id: 'presenter', label: 'What we did' },
];

export function Timeline({ changes, nowAt, map }: { changes: ChangeEvent[]; nowAt: number | null; map: TimeMap }) {
  const [filter, setFilter] = useState<Filter>('all');
  const [pages, setPages] = useState(1);
  const items = filterTimeline(buildTimeline(changes, nowAt, map), filter);
  const shown = items.slice(0, visibleCount(items.length, pages));
  const clock = map.mode === 'hardware' ? 'the laptop’s clock' : 'the simulated clock';
  return <section className="detail-panel timeline" aria-labelledby="timeline-title">
    <div className="timeline-head"><h3 id="timeline-title">Network changes</h3>
      <div className="segmented" role="group" aria-label="Show">{filters.map(f => <button type="button" key={f.id} aria-pressed={filter === f.id} onClick={() => { setFilter(f.id); setPages(1); }}>{f.label}</button>)}</div></div>
    <p className="timeline-note">Newest first. Times are on {clock}. Rows marked <span className="tag presenter">What we did</span> are presenter actions in the simulation; they are not something the controller knows until the network reports it.</p>
    {shown.length ? <ol className="timeline-list">{shown.map(item => <li key={item.key} className={`timeline-item ${item.kind}${item.presenter ? ' presenter' : ''}`}>
      <div className="when"><time>{item.time}</time>{item.ago && <small>{item.ago}</small>}</div>
      <div className="what"><span className={`tag ${item.kind}`}>{item.tag}</span><strong>{item.title}</strong>{item.detail && <p>{item.detail}</p>}</div>
    </li>)}</ol> : <p className="empty-state">{filter === 'presenter' ? 'Nothing has been done to the simulation yet.' : 'No network change has been seen yet. Failovers, lost contact and route changes appear here.'}</p>}
    {shown.length < items.length && <button type="button" className="plain-button show-more" onClick={() => setPages(p => p + 1)}>Show {Math.min(PAGE_SIZE, items.length - shown.length)} more ({items.length - shown.length} older)</button>}
  </section>;
}

