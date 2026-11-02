import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DashboardConnection } from './connection';

// The `frames` message rules of docs/DASHBOARD_API.md: no event_id, so the event rule does not apply; after a snapshot,
// records with id <= frames.last_id are dropped; an id that jumps means fetch the snapshot again.

const snapshot = (lastFrame: number | null, last = 4) => ({
  v: 1, mode: 'simulation', run_id: 1, last_event_id: last, server_time: '2026-11-02T00:00:00Z', label: 'SIMULATION: test.',
  backend: { status: 'ok', version: '0.1.0' }, clock: { sim_ms: 1000, speed: 1, paused: false },
  ...(lastFrame === null ? {} : { frames: { last_id: lastFrame, recent: [] } }),
});
const record = (id: number) => ({ id });
const batch = (ids: number[], dropped = 0) => ({ run_id: 1, last_id: ids.at(-1), dropped, records: ids.map(record) });

class FakeSocket {
  onopen: WebSocket['onopen'] = null;
  onmessage: WebSocket['onmessage'] = null;
  onclose: WebSocket['onclose'] = null;
  onerror: WebSocket['onerror'] = null;
  close = vi.fn();
  seq = 0;
  open() { this.onopen?.call(this as unknown as WebSocket, new Event('open')); }
  send(type: string, data: Record<string, unknown> = {}, event_id?: number) {
    const body = { v: 1, seq: ++this.seq, type, data, ...(event_id === undefined ? {} : { event_id }) };
    this.onmessage?.call(this as unknown as WebSocket, new MessageEvent('message', { data: JSON.stringify(body) }));
  }
}

function setup(first = snapshot(10)) {
  const socket = new FakeSocket();
  const got: number[][] = [];
  const fetchSnapshot = vi.fn(async (_url: string, _init: RequestInit) => new Response(JSON.stringify(first)));
  const client = new DashboardConnection({
    socketUrl: 'ws://localhost/ws', snapshotUrl: '/api/snapshot', fetchSnapshot, createSocket: () => socket,
    onFrames: records => got.push((records as { id: number }[]).map(r => r.id)),
  }, () => {});
  client.start();
  return { client, socket, got, fetchSnapshot };
}
const settle = () => vi.advanceTimersByTimeAsync(0);
const connect = async (h: ReturnType<typeof setup>) => { h.socket.open(); h.socket.send('status', { run_id: 1, heartbeat_interval_s: 2 }); await settle(); };

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-11-02T00:00:00Z')); });
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('frames messages', () => {
  it('are delivered in order and are not state-changing events', async () => {
    const h = setup(); await connect(h);
    h.socket.send('frames', batch([11, 12, 13]));
    h.socket.send('frames', batch([14]));
    expect(h.got).toEqual([[11, 12, 13], [14]]);
    expect(h.fetchSnapshot).toHaveBeenCalledTimes(1);
    h.client.stop();
  });

  it('drop the records the snapshot already holds (id <= frames.last_id), also those that arrived while it was loading', async () => {
    const h = setup();
    let finish!: (response: Response) => void;
    h.fetchSnapshot.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    h.socket.open(); h.socket.send('status', { run_id: 1, heartbeat_interval_s: 2 });
    h.socket.send('frames', batch([8, 9, 10, 11]));
    h.socket.send('frames', batch([12]));
    expect(h.got).toEqual([]); // held back until the snapshot says where the frames stand
    finish(new Response(JSON.stringify(snapshot(10)))); await settle();
    expect(h.got).toEqual([[11], [12]]);
    h.socket.send('frames', batch([10, 11, 12, 13])); // a late repeat of the overlap
    expect(h.got).toEqual([[11], [12], [13]]);
    h.client.stop();
  });

  it('a jump in the ids (records dropped or lost) fetches the snapshot again instead of showing a gap', async () => {
    const h = setup(); await connect(h);
    h.socket.send('frames', batch([11, 12]));
    h.fetchSnapshot.mockImplementationOnce(async () => new Response(JSON.stringify(snapshot(19))));
    h.socket.send('frames', batch([20, 21], 7)); // 13..19 never arrived
    await settle();
    expect(h.fetchSnapshot).toHaveBeenCalledTimes(2);
    expect(h.got).toEqual([[11, 12], [20, 21]]); // the new snapshot (last_id 19) holds the rest; the held message is replayed
    h.client.stop();
  });

  it('are still delivered when the snapshot has no frames section', async () => {
    const h = setup(snapshot(null)); await connect(h);
    h.socket.send('frames', batch([1, 2]));
    expect(h.got).toEqual([[1, 2]]);
    h.client.stop();
  });

  it('ignore malformed messages', async () => {
    const h = setup(); await connect(h);
    h.socket.send('frames', { records: 'no' });
    h.socket.send('frames', { last_id: 12 });
    expect(h.got).toEqual([]);
    h.client.stop();
  });
});
