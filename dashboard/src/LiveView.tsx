import { useEffect, useMemo, useState } from 'react';
import type { ConnectionState } from './connection';
import { NetworkScene } from './NetworkScene';
import { eventText, parseLiveSnapshot, policyName, postLiveAction } from './live';
import type { LiveSnapshot } from './live';

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

export function LiveView({ connection }: { connection: ConnectionState }) {
  const [polled, setPolled] = useState<{ snapshot: LiveSnapshot; baseAt: number | null; loadedAt: number } | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  const base = useMemo(() => {
    if (!connection.snapshot) return null;
    try { return parseLiveSnapshot(connection.snapshot); } catch { return null; }
  }, [connection.snapshot]);
  const current = polled?.baseAt === connection.snapshotAt && base
    && polled.snapshot.run_id === base.run_id && polled.snapshot.last_event_id >= base.last_event_id
    ? polled.snapshot : base;
  const loadedAt = current === polled?.snapshot ? polled.loadedAt : connection.snapshotAt;
  const connected = connection.status === 'connected';
  const controlsReady = connected && current !== null && pending === null && pollError === null;
  const simulation = current?.mode === 'simulation';
  const conditions = current?.presenter.conditions;
  const bStopped = conditions?.stopped_nodes.includes('B') ?? false;
  const gatewayAttached = conditions?.gateway_attached ?? true;

  useEffect(() => {
    if (!connected || !base) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const poll = async () => {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 5000);
      try {
        const response = await fetch('/api/snapshot', { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error(`Live refresh failed (${response.status}).`);
        const snapshot = parseLiveSnapshot(await response.json());
        if (active) {
          setPolled({ snapshot, baseAt: connection.snapshotAt, loadedAt: Date.now() });
          setPollError(null);
        }
      } catch (error) {
        if (active) setPollError(error instanceof Error ? error.message : 'Live refresh failed.');
      } finally {
        clearTimeout(timeout);
        if (active) timer = setTimeout(poll, 1000);
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); controller?.abort(); };
  }, [connected, base, connection.snapshotAt, refresh]);

  async function act(label: string, path: string, body: Record<string, unknown>) {
    if (!controlsReady) return;
    setPending(label);
    setFeedback(null);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      await postLiveAction(path, body, controller.signal);
      setFeedback(`${label} applied. Watch the controller view and received readings for the result.`);
      setRefresh(value => value + 1);
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : 'The action failed.');
    } finally {
      clearTimeout(timeout);
      setPending(null);
    }
  }

  const events = current?.recent_events.map(e => ({ event: e, title: eventText(e) }))
    .filter((item): item is { event: typeof item.event; title: string } => item.title !== null).slice(-8).reverse() ?? [];

  return <section className="network-section live-section" aria-labelledby="live-title">
    <div className="section-top"><div><span className="eyebrow">Live backend · D2</span><h2 id="live-title">Watch the network now</h2><p>The backend refreshes this view about once a second. Commands change the simulated environment; the controller learns of changes through C.</p></div><span className={`recorded-tag ${connected && !pollError ? 'live-tag' : ''}`}>{connected && !pollError ? 'Live view' : 'Last known view'}</span></div>
    {!base && <div className="gateway-alert" role="status">Waiting for a complete backend snapshot. Start the backend using the dashboard instructions to see the live network.</div>}
    {base && <>
      {(!connected || pollError) && <div className="gateway-alert" role="alert"><strong>Live view paused.</strong> {pollError ?? 'The backend connection is not healthy.'} Values below are the last received snapshot and may be out of date. Controls will return when the live connection is healthy.</div>}
      <div className="live-meta"><span>Run {current?.run_id} · {current?.scenario ?? 'hardware session'}</span><span>Simulation clock {seconds(current?.clock.sim_ms ?? 0)}{current?.clock.paused ? ' · paused' : ''}</span><span>Snapshot at {loadedAt ? new Date(loadedAt).toLocaleTimeString() : '—'}</span></div>
      <div className="control-panel"><div className="control-heading"><div><span className="eyebrow">Presenter controls</span><h3>Change the demonstration</h3></div><span>{simulation ? 'Simulation only' : 'Hardware mode: simulation controls unavailable'}</span></div>
        {!simulation && <p className="hardware-note" role="note">These controls act on the simulator, so they are turned off while real boards are connected (the backend would answer 409). To test failover on hardware, unplug relay B yourself; to test controller loss, stop the backend. The routing policy below still works.</p>}
        {simulation && <div className="control-groups">
          <div className="control-group"><strong>Relay B</strong><p>Stopping B tests whether A uses D while the controller catches up.</p><button type="button" disabled={!controlsReady} onClick={() => void act(bStopped ? 'Start B' : 'Stop B', '/api/control/node', { node: 'B', action: bStopped ? 'start' : 'stop' })}>{bStopped ? 'Start B' : 'Stop B'}</button></div>
          <div className="control-group"><strong>C’s laptop link</strong><p>Detach C to hold reports, then attach it to observe replay.</p><button type="button" disabled={!controlsReady} onClick={() => void act(gatewayAttached ? 'Detach C' : 'Attach C', '/api/control/gateway', { attached: !gatewayAttached })}>{gatewayAttached ? 'Detach C' : 'Attach C'}</button></div>
          <div className="control-group"><strong>Simulation run</strong><p>Pause to inspect the current state, or reset this scenario to its beginning.</p><div className="control-actions"><button type="button" disabled={!controlsReady} onClick={() => void act(current?.clock.paused ? 'Resume' : 'Pause', current?.clock.paused ? '/api/control/resume' : '/api/control/pause', {})}>{current?.clock.paused ? 'Resume' : 'Pause'}</button><button type="button" disabled={!controlsReady || !current?.scenario} onClick={() => void act('Reset run', '/api/control/reset', { scenario: current?.scenario })}>Reset run</button></div></div>
        </div>}
        <div className="policy-control"><label htmlFor="routing-policy">Routing policy</label><select id="routing-policy" value={current?.policy.current ?? ''} disabled={!controlsReady} onChange={e => void act(`Policy: ${policyName(e.target.value)}`, '/api/policy', { policy: e.target.value })}>{current?.policy.available.map(key => <option key={key} value={key}>{policyName(key)}</option>)}</select><p>Policy changes affect future route choices. An intact route may stay in place until a better choice wins repeated evaluations.</p></div>
        {pending && <p className="control-feedback" role="status">Applying {pending}…</p>}
        {feedback && <p className="control-feedback" role="status">{feedback}</p>}
      </div>
      {current && <NetworkScene view={current} unit={current.mode === 'hardware' ? '°C' : 'units'} timelineTitle="Recent network changes" timeline={events.length ? <ol className="event-list">{events.map(({ event, title }) => <li className={event.type === 'environment' ? 'injected' : ''} key={event.event_id}><span className="story-time">{typeof event.data.sim_ms === 'number' ? seconds(event.data.sim_ms) : '—'}</span><span>{title}</span>{event.type === 'environment' && <small>Presenter action</small>}</li>)}</ol> : <p className="empty-state">No significant change is in the recent event window. Readings and telemetry continue above.</p>} />}
    </>}
  </section>;
}
