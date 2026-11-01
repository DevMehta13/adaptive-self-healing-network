import type { ReactNode } from 'react';
import type { ConnectionState } from './connection';
import { eventText, policyName } from './live';
import type { Live } from './useLive';
import { CountsGrid, GraphGrid, ReadingsPanel, StaleNotice } from './NetworkScene';
import { NetworkView } from './NetworkView';
import { PageHeader } from './PageHeader';
import { hrefFor } from './route';
import type { PageId } from './route';

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;
const readable = (value: string) => value.replaceAll('_', ' ');
const time = (value: number | null) => value === null ? 'Waiting for backend' : new Date(value).toLocaleTimeString();
const unitOf = (mode: string) => mode === 'hardware' ? '°C' : 'units';

export const statusLabels = { connecting: 'Connecting', connected: 'Connected', stale: 'Stale', disconnected: 'Disconnected' };
const statusDescriptions = {
  connecting: 'Connecting to the backend and loading a fresh snapshot.',
  connected: 'Live messages are arriving from the backend.',
  stale: 'The connection is open, but live messages have stopped. Information may be out of date.',
  disconnected: 'The backend connection is unavailable. The dashboard will retry automatically.',
};

/** The facts the old "Connection status" card showed. */
export function ConnectionFacts({ connection }: { connection: ConnectionState }) {
  const { snapshot } = connection;
  return <>
    <p className="connection-description">{statusDescriptions[connection.status]}</p>
    {connection.error && <p className="error-detail">{connection.error}</p>}
    <dl className="connection-facts">
      <div><dt>Backend version</dt><dd>{snapshot?.backend.version ?? '—'}</dd></div>
      <div><dt>Current run</dt><dd>{snapshot ? `Run ${snapshot.run_id}` : '—'}</dd></div>
      <div><dt>Snapshot loaded</dt><dd>{time(connection.snapshotAt)}</dd></div>
      <div><dt>Last live message</dt><dd>{time(connection.lastMessageAt)}</dd></div>
    </dl>
    {snapshot && <p className="mode-note">{snapshot.label}</p>}
  </>;
}

/** Shows the page body only once a complete snapshot exists, and says plainly when the view is not live. */
function LiveGate({ live, children }: { live: Live; children: (current: NonNullable<Live['current']>) => ReactNode }) {
  const { current, connected, pollError } = live;
  if (!current) return <div className="gateway-alert" role="status">Waiting for a complete backend snapshot. Start the backend using the dashboard instructions to see the live network.</div>;
  return <>
    {(!connected || pollError) && <div className="gateway-alert" role="alert"><strong>Live view paused.</strong> {pollError ?? 'The backend connection is not healthy.'} Values below are the last received snapshot and may be out of date. Controls will return when the live connection is healthy.</div>}
    {children(current)}
  </>;
}

function LiveTag({ live }: { live: Live }) {
  const on = live.connected && !live.pollError;
  return <span className={`recorded-tag ${on ? 'live-tag' : ''}`}>{on ? 'Live view' : 'Last known view'}</span>;
}

export function OverviewPage({ connection, live }: { connection: ConnectionState; live: Live }) {
  const links: [PageId, string][] = [['network', 'Network and controls'], ['readings', 'Readings'], ['delivery', 'Delivery counters'], ['runs', 'Saved runs']];
  return <>
    <PageHeader title="Overview" shows="The state of the network and of the connection to the backend, in one place."
      reading="Start here. Each page in the bar above goes into one part in detail." aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => {
      const latest = view.readings.recent.at(-1);
      const active = view.route.state === 'active' && view.route.kind === 'route' ? view.route.path : null;
      const unit = unitOf(view.mode);
      return <div className="overview-grid">
        <div className="detail-panel"><h3>Latest reading</h3>
          <p className="big-value">{latest ? `${latest.value.toFixed(unit === 'units' ? 2 : 1)} ${unit}` : 'None yet'}</p>
          <p className="muted">{latest ? `via ${latest.via ?? '?'} · ${seconds(latest.received_ms)} at C${latest.replayed ? ` · delayed ${seconds(latest.delay_ms)}` : ''}` : 'No delivered reading has been recorded yet.'}</p></div>
        <div className="detail-panel"><h3>Route (controller’s plan)</h3>
          <p className="big-value">{active ? active.map(n => n ?? '?').join(' → ') : view.route.kind === 'no_route' ? 'No available route' : `No active route · ${readable(view.route.state)}`}</p>
          <p className="muted">Policy {policyName(view.policy.current)} · gateway {readable(view.gateway.state)}</p></div>
        <div className="detail-panel wide"><h3>Backend</h3><ConnectionFacts connection={connection} /></div>
        <div className="detail-panel wide"><h3>Pages</h3><ul className="page-links">{links.map(([id, text]) => <li key={id}><a href={hrefFor(id)}>{text}</a></li>)}</ul></div>
      </div>;
    }}</LiveGate>
  </>;
}

export function NetworkPage({ connection, live }: { connection: ConnectionState; live: Live }) {
  const { loadedAt, pending, feedback, controlsReady, act } = live;
  return <>
    <PageHeader title="Network" shows="The four boards and their links as the controller sees them, the route it planned, and the controls."
      reading="Solid lines are links both ends reported up. “Planned” is the controller’s intention; the last reading observed at C shows the path that actually carried data."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => {
      const simulation = view.mode === 'simulation';
      const conditions = view.presenter.conditions;
      const bStopped = conditions?.stopped_nodes.includes('B') ?? false;
      const gatewayAttached = conditions?.gateway_attached ?? true;
      const events = view.recent_events.map(e => ({ event: e, title: eventText(e) }))
        .filter((item): item is { event: typeof item.event; title: string } => item.title !== null).slice(-8).reverse();
      return <>
        <div className="live-meta"><span>Run {view.run_id} · {view.scenario ?? 'hardware session'}</span><span>Simulation clock {seconds(view.clock.sim_ms)}{view.clock.paused ? ' · paused' : ''}</span><span>Snapshot at {loadedAt ? new Date(loadedAt).toLocaleTimeString() : '—'}</span></div>
        {connection.snapshot && <p className="mode-note">{connection.snapshot.label}</p>}
        <div className="control-panel"><div className="control-heading"><div><span className="eyebrow">Presenter controls</span><h3>Change the demonstration</h3></div><span>{simulation ? 'Simulation only' : 'Hardware mode: simulation controls unavailable'}</span></div>
          {!simulation && <p className="hardware-note" role="note">These controls act on the simulator, so they are turned off while real boards are connected (the backend would answer 409). To test failover on hardware, unplug relay B yourself; to test controller loss, stop the backend. The routing policy below still works.</p>}
          {simulation && <div className="control-groups">
            <div className="control-group"><strong>Relay B</strong><p>Stopping B tests whether A uses D while the controller catches up.</p><button type="button" disabled={!controlsReady} onClick={() => void act(bStopped ? 'Start B' : 'Stop B', '/api/control/node', { node: 'B', action: bStopped ? 'start' : 'stop' })}>{bStopped ? 'Start B' : 'Stop B'}</button></div>
            <div className="control-group"><strong>C’s laptop link</strong><p>Detach C to hold reports, then attach it to observe replay.</p><button type="button" disabled={!controlsReady} onClick={() => void act(gatewayAttached ? 'Detach C' : 'Attach C', '/api/control/gateway', { attached: !gatewayAttached })}>{gatewayAttached ? 'Detach C' : 'Attach C'}</button></div>
            <div className="control-group"><strong>Simulation run</strong><p>Pause to inspect the current state, or reset this scenario to its beginning.</p><div className="control-actions"><button type="button" disabled={!controlsReady} onClick={() => void act(view.clock.paused ? 'Resume' : 'Pause', view.clock.paused ? '/api/control/resume' : '/api/control/pause', {})}>{view.clock.paused ? 'Resume' : 'Pause'}</button><button type="button" disabled={!controlsReady || !view.scenario} onClick={() => void act('Reset run', '/api/control/reset', { scenario: view.scenario })}>Reset run</button></div></div>
          </div>}
          <div className="policy-control"><label htmlFor="routing-policy">Routing policy</label><select id="routing-policy" value={view.policy.current} disabled={!controlsReady} onChange={e => void act(`Policy: ${policyName(e.target.value)}`, '/api/policy', { policy: e.target.value })}>{view.policy.available.map(key => <option key={key} value={key}>{policyName(key)}</option>)}</select><p>Policy changes affect future route choices. An intact route may stay in place until a better choice wins repeated evaluations.</p></div>
          {pending && <p className="control-feedback" role="status">Applying {pending}…</p>}
          {feedback && <p className="control-feedback" role="status">{feedback}</p>}
        </div>
        <StaleNotice view={view} />
        <GraphGrid view={view} unit={unitOf(view.mode)} />
        <div className="details-grid single"><div className="detail-panel"><h3>Recent network changes</h3>
          {events.length ? <ol className="event-list">{events.map(({ event, title }) => <li className={event.type === 'environment' ? 'injected' : ''} key={event.event_id}><span className="story-time">{typeof event.data.sim_ms === 'number' ? seconds(event.data.sim_ms) : '—'}</span><span>{title}</span>{event.type === 'environment' && <small>Presenter action</small>}</li>)}</ol> : <p className="empty-state">No significant change is in the recent event window. Readings and telemetry continue above.</p>}
        </div></div>
      </>;
    }}</LiveGate>
  </>;
}

export function ReadingsPage({ live }: { live: Live }) {
  return <>
    <PageHeader title="Readings" shows="Temperature readings from node A, as received by the controller (node C)."
      reading="Newest first. The path is what C observed, not the plan. “Delayed” readings were held at C while the controller was unreachable and replayed later."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => <div className="details-grid single"><ReadingsPanel view={view} unit={unitOf(view.mode)} /></div>}</LiveGate>
  </>;
}

export function DeliveryPage({ live }: { live: Live }) {
  return <>
    <PageHeader title="Delivery" shows="How many readings were created, delivered, acknowledged and lost."
      reading="Each card says which node knows the number and how old it is. Counts from A arrive by telemetry every few seconds, so they lag behind the readings."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => <><StaleNotice view={view} /><CountsGrid view={view} /></>}</LiveGate>
  </>;
}

export function RunsPage() {
  return <>
    <PageHeader title="Saved runs" shows="Recorded relay-failure and controller-outage runs, explained moment by moment."
      reading="Pick a run, then step through its moments. This works offline and is independent of the live connection." />
    <NetworkView />
  </>;
}
