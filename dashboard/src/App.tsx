import { useEffect, useState } from 'react';
import { DashboardConnection } from './connection';
import type { ConnectionState } from './connection';
import { NetworkView } from './NetworkView';

const labels = {
  connecting: 'Connecting', connected: 'Connected', stale: 'Stale', disconnected: 'Disconnected',
};
const descriptions = {
  connecting: 'Connecting to the backend and loading a fresh snapshot.',
  connected: 'Live messages are arriving from the backend.',
  stale: 'The connection is open, but live messages have stopped. Information may be out of date.',
  disconnected: 'The backend connection is unavailable. The dashboard will retry automatically.',
};

export function App() {
  const [connection, setConnection] = useState<ConnectionState>({
    status: 'connecting', snapshot: null, snapshotAt: null, lastMessageAt: null, error: null,
  });

  useEffect(() => {
    const url = new URL('/ws', window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const client = new DashboardConnection({ socketUrl: url.href, snapshotUrl: '/api/snapshot' }, setConnection);
    client.start();
    return () => client.stop();
  }, []);

  const { snapshot, status } = connection;
  const time = (value: number | null) => value === null ? 'Waiting for backend' : new Date(value).toLocaleTimeString();
  return (
    <div className="app-shell">
      <header className="topbar">
        <a href="/" className="brand" aria-label="Relay dashboard home">
          <span className="brand-mark" aria-hidden="true">r<span>·</span></span>
          <span>relay<span className="brand-caption">IoT network lab</span></span>
        </a>
        <span className="project-tag">Adaptive self-healing network</span>
      </header>

      <main>
        <div className="eyebrow"><span /> Network workspace <span className="divider">/</span> Recorded runs</div>
        <section className="intro" aria-labelledby="page-title">
          <div>
            <h1 id="page-title">See the network<br />change course.</h1>
            <p>Follow recorded relay failure and controller outage runs.<br className="desktop-break" /> The live backend connection is shown below.</p>
          </div>
          <span className={`mode-tag ${snapshot?.mode ?? 'unknown'}`}>
            {snapshot ? snapshot.mode === 'simulation' ? 'Simulation' : 'Hardware' : 'Mode unconfirmed'}
          </span>
        </section>

        <section className={`connection-panel ${status}`} aria-labelledby="connection-title">
          <div className="panel-heading">
            <div><span className="eyebrow">Backend link</span><h2 id="connection-title">Connection status</h2></div>
            <div className={`status-pill ${status}`} role="status" aria-live="polite"><span />{labels[status]}</div>
          </div>
          <p className="connection-description">{descriptions[status]}</p>
          {connection.error && <p className="error-detail">{connection.error}</p>}
          <dl className="connection-facts">
            <div><dt>Backend version</dt><dd>{snapshot?.backend.version ?? '—'}</dd></div>
            <div><dt>Current run</dt><dd>{snapshot ? `Run ${snapshot.run_id}` : '—'}</dd></div>
            <div><dt>Snapshot loaded</dt><dd>{time(connection.snapshotAt)}</dd></div>
            <div><dt>Last live message</dt><dd>{time(connection.lastMessageAt)}</dd></div>
          </dl>
          {snapshot && <p className="mode-note">{snapshot.label}</p>}
        </section>

        <NetworkView />

        <section className="learning-row" aria-label="About this phase">
          <div className="phase-number">D1<span>02 / 12</span></div>
          <div><h2>Read the evidence before changing the network.</h2><p>The fixtures let you compare the controller’s plan, what C actually received, and the presenter’s injected actions. Live controls come next.</p></div>
          <div className="next-note"><span>Up next · D2</span>Live controls<br />and policy selection</div>
        </section>
      </main>
      <footer><span>Four nodes. One resilient network.</span><span>Local-first · ESP8266</span></footer>
    </div>
  );
}
