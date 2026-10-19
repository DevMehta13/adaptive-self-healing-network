import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DashboardConnection } from './connection';
import type { ConnectionState } from './connection';

const snapshot = (last = 4, run = 1, mode = 'simulation') => ({
  v: 1, mode, run_id: run, last_event_id: last, server_time: '2026-10-19T00:00:00Z',
  label: mode === 'simulation' ? 'SIMULATION: configured delays.' : 'HARDWARE: test source.',
  backend: { status: 'ok', version: '0.1.0' },
  clock: { sim_ms: 1000, speed: 1, paused: false },
});

class FakeSocket {
  onopen: WebSocket['onopen'] = null;
  onmessage: WebSocket['onmessage'] = null;
  onclose: WebSocket['onclose'] = null;
  onerror: WebSocket['onerror'] = null;
  close = vi.fn();
  seq = 0;
  open() { this.onopen?.call(this as unknown as WebSocket, new Event('open')); }
  send(type: string, data: Record<string, unknown> = {}, event_id?: number) {
    this.raw({ v: 1, seq: ++this.seq, type, data, ...(event_id === undefined ? {} : { event_id }) });
  }
  raw(data: unknown) {
    this.onmessage?.call(this as unknown as WebSocket, new MessageEvent('message', { data: JSON.stringify(data) }));
  }
  disconnect() { this.onclose?.call(this as unknown as WebSocket, {} as CloseEvent); }
}

function setup() {
  const sockets: FakeSocket[] = [];
  const changes: ConnectionState[] = [];
  const fetchSnapshot = vi.fn(async (_url: string, _init: RequestInit) => new Response(JSON.stringify(snapshot())));
  const client = new DashboardConnection({
    socketUrl: 'ws://localhost/ws', snapshotUrl: '/api/snapshot', fetchSnapshot,
    createSocket: () => { const socket = new FakeSocket(); sockets.push(socket); return socket; },
  }, (state) => changes.push(state));
  client.start();
  return { client, sockets, changes, fetchSnapshot, current: () => changes.at(-1)! };
}

const settle = () => vi.advanceTimersByTimeAsync(0);
function status(socket: FakeSocket, run_id = 1) {
  socket.send('status', { run_id, heartbeat_interval_s: 2, snapshot_required: true });
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-19T00:00:00Z')); });
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('dashboard connection contract', () => {
  it('opens the socket before fetching and does not assume a mode', async () => {
    const h = setup();
    expect(h.fetchSnapshot).not.toHaveBeenCalled();
    expect(h.current().snapshot).toBeNull();
    expect(h.current().status).toBe('connecting');
    h.sockets[0].open(); status(h.sockets[0]); await settle();
    expect(h.current().status).toBe('connected');
    expect(h.current().snapshot?.mode).toBe('simulation');
    h.client.stop();
  });

  it('discards buffered events already covered by the snapshot, then tracks continuity', async () => {
    const h = setup();
    let finish!: (response: Response) => void;
    h.fetchSnapshot.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    h.sockets[0].open(); status(h.sockets[0]);
    h.sockets[0].send('reading_received', {}, 4);
    h.sockets[0].send('future_event', { extra: 'allowed' }, 5);
    finish(new Response(JSON.stringify(snapshot(4)))); await settle();
    h.sockets[0].send('future_event', {}, 5); // duplicate
    h.sockets[0].send('future_event', {}, 6);
    expect(h.fetchSnapshot).toHaveBeenCalledTimes(1);
    expect(h.current().status).toBe('connected');
    h.client.stop();
  });

  it('marks a silent socket stale and recovers when a live message arrives', async () => {
    const h = setup(); h.sockets[0].open(); status(h.sockets[0]); await settle();
    await vi.advanceTimersByTimeAsync(6250);
    expect(h.current().status).toBe('stale');
    h.sockets[0].send('heartbeat', {});
    expect(h.current().status).toBe('connected');
    h.client.stop();
  });

  it('uses the advertised heartbeat interval', async () => {
    const h = setup(); h.sockets[0].open();
    h.sockets[0].send('status', { run_id: 1, heartbeat_interval_s: .5 }); await settle();
    await vi.advanceTimersByTimeAsync(1750);
    expect(h.current().status).toBe('stale'); h.client.stop();
  });

  it('re-fetches on reconnect and accepts a restarted backend with lower ids and another mode', async () => {
    const h = setup(); h.sockets[0].open(); status(h.sockets[0]); await settle();
    h.sockets[0].disconnect(); expect(h.current().status).toBe('disconnected');
    await vi.advanceTimersByTimeAsync(1000);
    h.fetchSnapshot.mockResolvedValueOnce(new Response(JSON.stringify(snapshot(0, 1, 'hardware'))));
    h.sockets[1].open(); status(h.sockets[1]); await settle();
    expect(h.current().snapshot?.last_event_id).toBe(0);
    expect(h.current().snapshot?.mode).toBe('hardware');
    expect(h.current().status).toBe('connected'); h.client.stop();
  });

  it('refreshes on event gaps and scenario resets', async () => {
    const h = setup(); h.sockets[0].open(); status(h.sockets[0]); await settle();
    h.fetchSnapshot.mockResolvedValueOnce(new Response(JSON.stringify(snapshot(7))));
    h.sockets[0].send('future_event', {}, 7); await settle();
    expect(h.fetchSnapshot).toHaveBeenCalledTimes(2);
    h.fetchSnapshot.mockResolvedValueOnce(new Response(JSON.stringify(snapshot(8, 2))));
    h.sockets[0].send('reset', { run_id: 2 }, 8); await settle();
    expect(h.current().snapshot?.run_id).toBe(2); h.client.stop();
  });

  it('refreshes on a per-connection sequence gap', async () => {
    const h = setup(); h.sockets[0].open(); status(h.sockets[0]); await settle();
    h.sockets[0].seq++;
    h.sockets[0].send('heartbeat'); await settle();
    expect(h.fetchSnapshot).toHaveBeenCalledTimes(2); h.client.stop();
  });

  it('does not call an HTTP-only success a live WebSocket connection', async () => {
    const h = setup(); h.sockets[0].open(); await settle();
    expect(h.current().status).toBe('stale'); h.client.stop();
  });

  it('reports HTTP failures and retries instead of showing connected', async () => {
    const h = setup(); h.fetchSnapshot.mockResolvedValueOnce(new Response('', { status: 503 }));
    h.sockets[0].open(); status(h.sockets[0]); await settle();
    expect(h.current().status).toBe('disconnected');
    expect(h.current().error).toContain('503');
    await vi.advanceTimersByTimeAsync(1000); expect(h.sockets).toHaveLength(2); h.client.stop();
  });

  it('rejects incompatible protocol versions without assuming simulation', async () => {
    const h = setup(); h.sockets[0].open();
    h.sockets[0].raw({ v: 2, seq: 1, type: 'status', data: {} }); await settle();
    expect(h.current().status).toBe('disconnected');
    expect(h.current().snapshot).toBeNull(); h.client.stop();
  });

  it('aborts an old snapshot request and rejects its response after reconnect', async () => {
    const h = setup(); let finish!: (response: Response) => void;
    h.fetchSnapshot.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    h.sockets[0].open(); status(h.sockets[0]); h.sockets[0].disconnect();
    expect(h.fetchSnapshot.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    h.fetchSnapshot.mockResolvedValueOnce(new Response(JSON.stringify(snapshot(10, 3))));
    h.sockets[1].open(); status(h.sockets[1], 3); await settle();
    finish(new Response(JSON.stringify(snapshot(4, 1)))); await settle();
    expect(h.current().snapshot?.run_id).toBe(3); h.client.stop();
  });

  it('times out a snapshot fetch that stops responding', async () => {
    const h = setup();
    h.fetchSnapshot.mockImplementationOnce((_url, init) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    h.sockets[0].open(); status(h.sockets[0]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(h.current().status).toBe('disconnected');
    expect(h.current().error).toContain('timed out'); h.client.stop();
  });

  it('rejects malformed snapshots instead of presenting an assumed mode', async () => {
    const h = setup(); h.fetchSnapshot.mockResolvedValueOnce(new Response(JSON.stringify({ mode: 'simulation' })));
    h.sockets[0].open(); status(h.sockets[0]); await settle();
    expect(h.current().status).toBe('disconnected');
    expect(h.current().snapshot).toBeNull(); h.client.stop();
  });

  it('bounds updates buffered while loading a snapshot', () => {
    const h = setup(); h.fetchSnapshot.mockImplementationOnce(() => new Promise(() => {}));
    h.sockets[0].open(); status(h.sockets[0]);
    for (let i = 1; i <= 2001; i++) h.sockets[0].send('future_event', {}, i);
    expect(h.current().status).toBe('disconnected');
    expect(h.current().error).toContain('Too many updates'); h.client.stop();
  });

  it('times out unopened sockets and caps retry backoff at 10 seconds', async () => {
    const h = setup(); await vi.advanceTimersByTimeAsync(5000);
    expect(h.current().status).toBe('disconnected');
    await vi.advanceTimersByTimeAsync(1000); expect(h.sockets).toHaveLength(2);
    for (const delay of [2000, 4000, 8000, 10000, 10000]) {
      const count = h.sockets.length; h.sockets.at(-1)!.disconnect();
      await vi.advanceTimersByTimeAsync(delay - 1); expect(h.sockets).toHaveLength(count);
      await vi.advanceTimersByTimeAsync(1); expect(h.sockets).toHaveLength(count + 1);
    }
    h.client.stop();
  });

  it('cleans up pending work when the React view unmounts', async () => {
    const h = setup(); h.sockets[0].disconnect(); h.client.stop();
    const count = h.changes.length; await vi.advanceTimersByTimeAsync(20000);
    expect(h.sockets).toHaveLength(1); expect(h.changes).toHaveLength(count);
    expect(vi.getTimerCount()).toBe(0);
  });
});
