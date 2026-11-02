import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { DashboardConnection } from './connection';
import type { ConnectionState } from './connection';
import { PacketsPage } from './PacketsPage';
import { ConnectionFacts, DeliveryPage, NetworkPage, OverviewPage, ReadingsPage, RunsPage, statusLabels } from './pages';
import { documentTitle, hrefFor, pages, parseRoute } from './route';
import { applyTheme, initialTheme, rememberTheme, storedTheme, systemTheme } from './theme';
import type { Theme } from './theme';
import { useLive } from './useLive';
import { LiveStore } from './liveData';
import type { SnapshotLike } from './liveData';

function useRoute() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  useEffect(() => { document.title = documentTitle(route); }, [route]);
  return route;
}

function useTheme() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  // Layout effect: the chart reads the theme's colours in a (child) effect, which must see the new theme already applied.
  useLayoutEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => {
    // Until the person picks a theme, keep following the system setting.
    const query = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!query) return;
    const follow = () => { if (storedTheme() === null) setTheme(systemTheme()); };
    query.addEventListener('change', follow);
    return () => query.removeEventListener('change', follow);
  }, []);
  const toggle = () => setTheme(current => {
    const next: Theme = current === 'dark' ? 'light' : 'dark';
    rememberTheme(next);
    return next;
  });
  return [theme, toggle] as const;
}

export function App() {
  const [connection, setConnection] = useState<ConnectionState>({
    status: 'connecting', snapshot: null, snapshotAt: null, lastMessageAt: null, error: null,
  });
  const [store] = useState(() => new LiveStore());
  const data = useSyncExternalStore(store.subscribe, store.getState);
  const route = useRoute();
  const [theme, toggleTheme] = useTheme();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsRef = useRef<HTMLDivElement>(null);
  const pillRef = useRef<HTMLButtonElement>(null);
  const mainRef = useRef<HTMLElement>(null);

  // One connection for the whole app: pages receive its state and never open their own.
  useEffect(() => {
    const url = new URL('/ws', window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const client = new DashboardConnection({
      socketUrl: url.href, snapshotUrl: '/api/snapshot',
      onSnapshot: snapshot => store.ingestSnapshot(snapshot as unknown as SnapshotLike),
      onEvent: event => store.ingestEvent(event),
      onFrames: records => store.ingestFrames(records),
    }, setConnection);
    client.start();
    return () => client.stop();
  }, [store]);

  const live = useLive(connection, store);

  useEffect(() => { setDetailsOpen(false); }, [route]);
  useEffect(() => {
    if (!detailsOpen) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent) {
        if (event.key === 'Escape') { setDetailsOpen(false); pillRef.current?.focus(); }
      } else if (!detailsRef.current?.contains(event.target as Node)) setDetailsOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', close); };
  }, [detailsOpen]);

  const { snapshot, status } = connection;
  const mode = snapshot ? snapshot.mode : 'unknown';
  return (
    <div className="app-shell">
      <button type="button" className="skip-link" onClick={() => mainRef.current?.focus()}>Skip to the page content</button>
      <header className="topbar">
        <a href={hrefFor('overview')} className="brand" aria-label="Relay dashboard overview">
          <span className="brand-mark" aria-hidden="true">r<span>·</span></span>
          <span className="brand-name">relay</span>
        </a>
        <nav className="nav" aria-label="Pages">
          {pages.map(page => <a key={page.id} href={hrefFor(page.id)} aria-current={page.id === route ? 'page' : undefined}>{page.label}</a>)}
        </nav>
        <div className="bar-status">
          <span className={`mode-tag ${mode}`}>{snapshot ? snapshot.mode === 'simulation' ? 'Simulation' : 'Hardware' : 'Mode unconfirmed'}</span>
          <div className="conn" ref={detailsRef}>
            <button type="button" ref={pillRef} className={`status-pill ${status}`} aria-expanded={detailsOpen} aria-controls="connection-details" onClick={() => setDetailsOpen(open => !open)}>
              <span aria-hidden="true" />{statusLabels[status]}
            </button>
            <span className="visually-hidden" role="status" aria-live="polite">Connection {statusLabels[status]}</span>
            {detailsOpen && <div className="popover" id="connection-details" role="dialog" aria-label="Backend connection details">
              <h2>Backend connection</h2>
              <ConnectionFacts connection={connection} />
            </div>}
          </div>
          <button type="button" className="theme-toggle" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`} title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>
            {theme === 'dark' ? '☀' : '☾'}
          </button>
        </div>
      </header>

      <main id="main" ref={mainRef} tabIndex={-1}>
        {route === 'overview' && <OverviewPage connection={connection} live={live} data={data} />}
        {route === 'network' && <NetworkPage connection={connection} live={live} data={data} />}
        {route === 'readings' && <ReadingsPage live={live} data={data} theme={theme} />}
        {route === 'packets' && <PacketsPage live={live} data={data} />}
        {route === 'delivery' && <DeliveryPage live={live} />}
        {route === 'runs' && <RunsPage />}
      </main>
    </div>
  );
}
