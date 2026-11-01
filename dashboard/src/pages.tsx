import type { ReactNode } from 'react';
import type { ConnectionState } from './connection';
import type { Count } from './fixtures';
import { policyName } from './live';
import type { Live } from './useLive';
import { CountCard, GraphGrid, StaleNotice } from './NetworkScene';
import { ageText, latestReading, nodeLines, readingAgeMs, recentChanges, seconds } from './overview';
import { ReadingsChart } from './ReadingsChart';
import { axisTime } from './chartData';
import type { LiveState } from './liveData';
import type { Theme } from './theme';
import { NetworkView } from './NetworkView';
import { PageHeader } from './PageHeader';
import { hrefFor } from './route';
import type { PageId } from './route';

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
  const { current, connected, pollError, status } = live;
  if (!current) return <div className="gateway-alert" role="status">{status === 'connecting' ? 'Connecting to the backend and loading a snapshot…' : status === 'disconnected' ? 'The backend is not reachable, so there is nothing to show yet. The dashboard keeps retrying; start the backend using the dashboard instructions.' : 'Waiting for a complete backend snapshot.'}</div>;
  return <>
    {(!connected || pollError) && <div className="gateway-alert" role="alert"><strong>Live view paused.</strong> {pollError ?? 'The backend connection is not healthy.'} Values below are the last received snapshot and may be out of date. Controls will return when the live connection is healthy.</div>}
    {children(current)}
  </>;
}

function LiveTag({ live }: { live: Live }) {
  const on = live.connected && !live.pollError;
  return <span className={`recorded-tag ${on ? 'live-tag' : ''}`}>{on ? 'Live view' : 'Last known view'}</span>;
}

const seeAll = (id: PageId, text: string) => <a className="see-all" href={hrefFor(id)}>{text} →</a>;

export function OverviewPage({ connection, live, data }: { connection: ConnectionState; live: Live; data: LiveState }) {
  return <>
    <PageHeader title="Overview" shows="The current state of the network in one view: the latest temperature, the route, each board, the delivery numbers and the last changes."
      reading="Read it top to bottom: is a reading arriving, which way is it going, is every board reachable, is anything being lost. Each block links to its full page."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => {
      const latest = latestReading(view.readings.recent);
      const active = view.route.state === 'active' && view.route.kind === 'route' ? view.route.path : null;
      const unit = unitOf(view.mode);
      const nodes = nodeLines(view.nodes);
      const changes = data.timeMap ? recentChanges(data.changes, data.nowAt, data.timeMap) : [];
      const key = (label: string, count: Count | null | undefined) => <div className="key-number"><span>{label}</span><strong>{count?.value ?? '—'}</strong><small>{count ? `${count.known_by}${count.age_ms != null ? ` · ${seconds(count.age_ms)} old` : ''}` : 'Awaiting telemetry'}</small></div>;
      return <>
        <StaleNotice view={view} />
        <div className="overview-grid">
          <section className="detail-panel tile" aria-labelledby="ov-latest"><div className="tile-head"><h3 id="ov-latest">Latest temperature</h3>{seeAll('readings', 'Readings')}</div>
            {latest ? <>
              <p className="big-value">{latest.value.toFixed(unit === 'units' ? 2 : 1)} <span className="unit">{unit}</span>{latest.replayed && <span className="chip replayed">replayed</span>}</p>
              <p className="muted">{readingAgeMs(view.clock.sim_ms, latest) < 1500 ? 'received just now' : `received ${ageText(readingAgeMs(view.clock.sim_ms, latest))}`} at C · via relay {latest.via ?? '?'}{latest.replayed ? ` · held ${seconds(latest.delay_ms)} while the controller was unreachable` : ''}</p>
            </> : <p className="empty-state">No reading has reached C yet.</p>}
          </section>
          <section className="detail-panel tile" aria-labelledby="ov-route"><div className="tile-head"><h3 id="ov-route">Route</h3>{seeAll('network', 'Network')}</div>
            <div className="route-pair">
              <div><span className="label">Planned by the controller</span><p className="path">{active ? active.map(n => n ?? '?').join(' → ') : view.route.kind === 'no_route' ? 'No available route' : `No active route · ${readable(view.route.state)}`}</p></div>
              <div><span className="label">Last observed at C</span><p className="path">{latest ? latest.observed_path.map(n => n ?? '?').join(' → ') : 'No reading yet'}</p></div>
            </div>
            <p className="muted">Policy {policyName(view.policy.current)} · gateway {readable(view.gateway.state)}. A plan is an intention; the observed path is what a packet took.</p>
          </section>
          <section className="detail-panel tile" aria-labelledby="ov-nodes"><div className="tile-head"><h3 id="ov-nodes">Boards</h3>{seeAll('network', 'Network')}</div>
            <ul className="node-status">{nodes.map(n => <li key={n.id} className={n.tone}><span className="dot" aria-hidden="true" /><div><strong>{n.id} · {n.role}</strong> <span className="state">{n.status}</span>{n.detail && <small>{n.detail}</small>}</div></li>)}</ul>
          </section>
          <section className="detail-panel tile" aria-labelledby="ov-numbers"><div className="tile-head"><h3 id="ov-numbers">Delivery in numbers</h3>{seeAll('delivery', 'Delivery')}</div>
            <div className="key-numbers">{key('Created at A', view.counts.created)}{key('Delivered at C', view.counts.delivered)}{key('Lost in total', view.counts.lost_total)}</div>
          </section>
          <section className="detail-panel tile wide" aria-labelledby="ov-changes"><div className="tile-head"><h3 id="ov-changes">Latest network changes</h3>{seeAll('network', 'Network')}</div>
            {changes.length ? <ol className="event-list">{changes.map(c => <li className={c.injected ? 'injected' : ''} key={c.id}><span className="story-time">{c.time}</span><span>{c.title}{c.ago && <span className="ago"> · {c.ago}</span>}</span>{c.injected && <small>Presenter action</small>}</li>)}</ol> : <p className="empty-state">No significant change yet. Changes such as a relay failover or a lost link appear here.</p>}
          </section>
          <section className="quiet wide" aria-label="Backend facts"><ConnectionFacts connection={connection} /></section>
        </div>
      </>;
    }}</LiveGate>
  </>;
}

export function NetworkPage({ connection, live, data }: { connection: ConnectionState; live: Live; data: LiveState }) {
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
      const events = data.timeMap ? recentChanges(data.changes, data.nowAt, data.timeMap, 8) : [];
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
          {events.length ? <ol className="event-list">{events.map(c => <li className={c.injected ? 'injected' : ''} key={c.id}><span className="story-time">{c.time}</span><span>{c.title}{c.ago && <span className="ago"> · {c.ago}</span>}</span>{c.injected && <small>Presenter action</small>}</li>)}</ol> : <p className="empty-state">No significant change is in the recent event window. Readings and telemetry continue above.</p>}
        </div></div>
      </>;
    }}</LiveGate>
  </>;
}

export function ReadingsPage({ live, data, theme }: { live: Live; data: LiveState; theme: Theme }) {
  return <>
    <PageHeader title="Readings" shows="Temperature readings from node A, as received by the controller (node C), over time."
      reading="Each point is one reading, coloured by the relay that handed it to C. Hollow points were held at C while the controller was unreachable and replayed later. Dashed lines mark events such as a failover. Hover for details."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => {
      const unit = unitOf(view.mode);
      const map = data.timeMap;
      if (!map) return null;
      const rows = [...data.readings].sort((x, y) => y.at - x.at).slice(0, 50);
      return <>
        <StaleNotice view={view} />
        <ReadingsChart readings={data.readings} changes={data.changes} map={map} nowAt={data.nowAt} theme={theme} unit={unit} />
        <section className="detail-panel readings-table" aria-labelledby="table-title"><h3 id="table-title">Recent readings</h3>
          {rows.length ? <div className="table-scroll"><table>
            <thead><tr><th scope="col">Time at C</th><th scope="col">Value</th><th scope="col">Path observed at C</th><th scope="col">Live or replayed</th></tr></thead>
            <tbody>{rows.map(r => <tr key={r.key} className={r.replayed ? 'replayed-row' : ''}>
              <td>{axisTime(map, r.at)}</td><td>{r.value.toFixed(unit === 'units' ? 2 : 1)} {unit}</td>
              <td>{r.path.map(n => n ?? '?').join(' → ')}</td>
              <td>{r.replayed ? `Replayed · held ${seconds(r.delayMs)}, reached the controller ${axisTime(map, r.reachedAt)}` : 'Live'}</td>
            </tr>)}</tbody></table></div> : <p className="empty-state">No delivered reading has been recorded yet.</p>}
        </section>
      </>;
    }}</LiveGate>
  </>;
}

export function DeliveryPage({ live }: { live: Live }) {
  return <>
    <PageHeader title="Delivery" shows="How many readings were created, delivered, acknowledged and lost, as reported by the nodes and the controller."
      reading="Follow the flow left to right. Anything created but not delivered, or not acknowledged, shows up in the loss counters below. Each card says who knows the number and how old it is; counts from A arrive by telemetry every few seconds, so they lag."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => {
      const c = view.counts;
      return <>
        <StaleNotice view={view} />
        <section aria-labelledby="flow-title"><h2 className="section-title" id="flow-title">The path of a reading</h2>
          <div className="flow">
            <CountCard label="Created at A" count={c.created} note="Readings node A produced and sent." />
            <span className="flow-arrow" aria-hidden="true">→</span>
            <CountCard label="Delivered at C" count={c.delivered} note="Unique readings that reached node C." />
            <span className="flow-arrow" aria-hidden="true">→</span>
            <CountCard label="Acknowledged at A" count={c.acknowledged} note="Readings for which A received C’s acknowledgement." />
          </div>
        </section>
        <section aria-labelledby="loss-title"><h2 className="section-title" id="loss-title">Lost and late</h2>
          <div className="counts-grid">
            <CountCard label="Lost at A" count={c.lost} note="Readings A gave up on." />
            <CountCard label="Lost at C" count={c.lost_at_c} note="Readings C gave up on while holding or resending them." />
            <CountCard label="Lost in total" count={c.lost_total} note="A’s lost plus lost at C." />
            <CountCard label="No route at A" count={c.no_route} note="Readings A dropped at creation because no route existed." />
            <CountCard label="Delivered late" count={c.delivered_late} note="Readings that first reached the controller in a replay, held at C meanwhile." />
          </div>
        </section>
      </>;
    }}</LiveGate>
  </>;
}

export function RunsPage() {
  return <>
    <PageHeader title="Saved runs" shows="Recorded relay-failure and controller-outage runs, explained moment by moment."
      reading="Pick a run, then step through its moments. This works offline and is independent of the live connection." />
    <NetworkView />
  </>;
}
