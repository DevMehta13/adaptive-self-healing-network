import type { ReactNode } from 'react';
import type { ConnectionState } from './connection';
import type { Count } from './fixtures';
import { ageText, clockText, elapsedText, formatNumber, lateLabel, oldText, pathText, readable, seconds, unitOf } from './format';
import { policyName } from './live';
import type { Live } from './useLive';
import { CountCard, StaleNotice } from './NetworkScene';
import { ControlsPanel, NetworkGraph, NodeCards, RoutePanel, Timeline, TimelineRows } from './NetworkPanels';
import { latestReading, nodeLines, readingAgeMs } from './overview';
import { ReadingsChart } from './ReadingsChart';
import { axisTime } from './chartData';
import type { LiveState } from './liveData';
import type { Theme } from './theme';
import { majorChanges } from './timeline';
import { NetworkView } from './NetworkView';
import { PageHeader } from './PageHeader';
import { hrefFor } from './route';
import type { PageId } from './route';
import { isLive, stateMessage, stateTag, viewState } from './viewState';

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
      <div><dt>Snapshot loaded</dt><dd>{connection.snapshotAt === null ? 'Waiting for backend' : clockText(connection.snapshotAt)}</dd></div>
      <div><dt>Last live message</dt><dd>{connection.lastMessageAt === null ? 'Waiting for backend' : clockText(connection.lastMessageAt)}</dd></div>
    </dl>
    {snapshot && <p className="mode-note">{snapshot.label}</p>}
  </>;
}

function stateOf(live: Live) {
  const state = viewState({ status: live.status, hasData: live.current !== null, pollError: live.pollError });
  const asOf = live.current && live.loadedAt ? clockText(live.loadedAt) : null;
  return { state, message: stateMessage(state, asOf, live.pollError) };
}

/**
 * The page body only exists once a complete snapshot has arrived. After that, any state other than live says so in a banner
 * and dims the numbers, so a stale figure is never mistaken for a live one.
 */
export function LiveGate({ live, children }: { live: Live; children: (current: NonNullable<Live['current']>, isLiveNow: boolean) => ReactNode }) {
  const { state, message } = stateOf(live);
  const banner = message && <div className={`state-banner ${state}`} role={message.role}><strong>{message.title}.</strong> {message.text}</div>;
  if (!live.current) return <>{banner}</>;
  return <>
    {banner}
    <div className={isLive(state) ? 'live-body' : 'live-body not-live'} data-state={state}>{children(live.current, isLive(state))}</div>
  </>;
}

export function LiveTag({ live }: { live: Live }) {
  const { state } = stateOf(live);
  return <span className={`recorded-tag ${isLive(state) ? 'live-tag' : `not-live-tag ${state}`}`}>{stateTag[state]}</span>;
}

const seeAll = (id: PageId, text: string) => <a className="see-all" href={hrefFor(id)}>{text} →</a>;

export function OverviewPage({ connection, live, data }: { connection: ConnectionState; live: Live; data: LiveState }) {
  return <>
    <PageHeader title="Overview" shows="The current state of the network in one view: the latest temperature, the route, each board, the delivery numbers and the major changes."
      reading="Read it top to bottom: is a reading arriving, which way is it going, is every board reachable, is anything being lost. Each block links to its full page."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{(view, liveNow) => {
      const latest = latestReading(view.readings.recent);
      const active = view.route.state === 'active' && view.route.kind === 'route' ? view.route.path : null;
      const unit = unitOf(view.mode);
      const nodes = nodeLines(view.nodes);
      const changes = data.timeMap ? majorChanges(data.changes, data.nowAt, data.timeMap, 5) : [];
      const key = (label: string, count: Count | null | undefined) => <div className="key-number"><span>{label}</span><strong>{count?.value ?? '—'}</strong><small>{count ? `${count.known_by}${count.age_ms != null ? ` · ${oldText(count.age_ms)}` : ''}` : 'Awaiting telemetry'}</small></div>;
      return <>
        <StaleNotice view={view} />
        <div className="overview-grid">
          <section className="detail-panel tile" aria-labelledby="ov-latest"><div className="tile-head"><h3 id="ov-latest">Latest temperature</h3>{seeAll('readings', 'Readings')}</div>
            {latest ? <>
              <p className="big-value">{formatNumber(latest.value, unit)} <span className="unit">{unit}</span>{latest.replayed && <span className="chip replayed">{lateLabel(latest.delay_ms)}</span>}</p>
              <p className="muted">received {liveNow ? ageText(readingAgeMs(view.clock.sim_ms, latest)) : `${seconds(readingAgeMs(view.clock.sim_ms, latest))} before the last snapshot`} at C · via relay {latest.via ?? '?'}{latest.replayed ? ' · held at C while the controller was unreachable' : ''}</p>
            </> : <p className="empty-state">No reading has reached C yet.</p>}
          </section>
          <section className="detail-panel tile" aria-labelledby="ov-route"><div className="tile-head"><h3 id="ov-route">Route</h3>{seeAll('network', 'Network')}</div>
            <div className="route-pair">
              <div><span className="label">Planned by the controller</span><p className="path">{active ? pathText(active) : view.route.kind === 'no_route' ? 'No available route' : `No active route · ${readable(view.route.state)}`}</p></div>
              <div><span className="label">Last observed at C</span><p className="path">{latest ? pathText(latest.observed_path) : 'No reading yet'}</p></div>
            </div>
            <p className="muted">Policy {policyName(view.policy.current)} · gateway {readable(view.gateway.state)}. A plan is an intention; the observed path is what a packet took.</p>
          </section>
          <section className="detail-panel tile" aria-labelledby="ov-nodes"><div className="tile-head"><h3 id="ov-nodes">Boards</h3>{seeAll('network', 'Network')}</div>
            <ul className="node-status">{nodes.map(n => <li key={n.id} className={n.tone}><span className="dot" aria-hidden="true" /><div><strong>{n.id} · {n.role}</strong> <span className="state">{n.status}</span>{n.detail && <small>{n.detail}</small>}</div></li>)}</ul>
          </section>
          <section className="detail-panel tile" aria-labelledby="ov-numbers"><div className="tile-head"><h3 id="ov-numbers">Delivery in numbers</h3>{seeAll('delivery', 'Delivery')}</div>
            <div className="key-numbers">{key('Created at A', view.counts.created)}{key('Delivered at C', view.counts.delivered)}{key('Lost in total', view.counts.lost_total)}</div>
          </section>
          <section className="detail-panel tile wide" aria-labelledby="ov-changes"><div className="tile-head"><h3 id="ov-changes">Major network changes</h3>{seeAll('network', 'All changes')}</div>
            {changes.length ? <TimelineRows items={changes} compact /> : <p className="empty-state">No major change yet. A relay failover, lost contact or a lost link will appear here.</p>}
          </section>
          <section className="quiet wide" aria-label="Backend facts"><ConnectionFacts connection={connection} /></section>
        </div>
      </>;
    }}</LiveGate>
  </>;
}

export function NetworkPage({ connection, live, data }: { connection: ConnectionState; live: Live; data: LiveState }) {
  return <>
    <PageHeader title="Network" shows="The four boards and their links as the controller sees them, the route it planned, the path a reading actually took, and what changed."
      reading="Solid lines are links both ends reported up; dashed or grey lines are weaker or unknown. “Planned” is the controller’s intention; the observed path is what C saw. The timeline lists changes newest first."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => {
      const map = data.timeMap;
      if (!map) return null;
      return <>
        <div className="live-meta"><span>Run {view.run_id} · {view.scenario ?? 'hardware session'}</span><span>{view.mode === 'hardware' ? 'Laptop clock since the backend started' : 'Simulation clock'} {elapsedText(view.clock.sim_ms)}{view.clock.paused ? ' · paused' : ''}</span><span>Snapshot at {clockText(live.loadedAt)}</span></div>
        {connection.snapshot && <p className="mode-note">{connection.snapshot.label}</p>}
        <StaleNotice view={view} />
        <div className="network-layout">
          <NetworkGraph view={view} />
          <RoutePanel view={view} unit={unitOf(view.mode)} />
        </div>
        <NodeCards view={view} changes={data.changes} map={map} />
        <ControlsPanel view={view} live={live} />
        <Timeline changes={data.changes} nowAt={data.nowAt} map={map} />
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
              <td>{axisTime(map, r.at)}</td><td>{formatNumber(r.value, unit)} {unit}</td>
              <td>{pathText(r.path)}</td>
              <td>{r.replayed ? <>{lateLabel(r.delayMs)}<small>the controller got it at {axisTime(map, r.reachedAt)}</small></> : 'Live'}</td>
            </tr>)}</tbody></table></div> : <p className="empty-state">No reading has reached C yet. They appear here, newest first, as soon as one arrives.</p>}
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
      reading="Pick a recording, then step through its moments with Previous and Next, the dots or the list. The graph, route, counts and readings below show the controller’s knowledge at that moment. This works offline and is independent of the live connection." />
    <NetworkView />
  </>;
}
