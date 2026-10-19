export type ConnectionStatus = 'connecting' | 'connected' | 'stale' | 'disconnected';

// Only the fields used by D0. Network state/rendering belongs to D1.
export interface Snapshot {
  v: 1;
  mode: 'simulation' | 'hardware';
  label: string;
  run_id: number;
  last_event_id: number;
  server_time: string;
  backend: { status: string; version: string };
  clock: { sim_ms: number; speed: number; paused: boolean };
}

export interface ConnectionState {
  status: ConnectionStatus;
  snapshot: Snapshot | null;
  lastMessageAt: number | null;
  snapshotAt: number | null;
  error: string | null;
}

interface Envelope {
  v: 1;
  seq: number;
  type: string;
  data: Record<string, unknown>;
  event_id?: number;
}

type Socket = Pick<WebSocket, 'onopen' | 'onmessage' | 'onclose' | 'onerror' | 'close'>;
interface Options {
  socketUrl: string;
  snapshotUrl: string;
  createSocket?: (url: string) => Socket;
  fetchSnapshot?: (url: string, init: RequestInit) => Promise<Response>;
  now?: () => number;
}

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const integer = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

function parseSnapshot(value: unknown): Snapshot {
  if (!object(value) || value.v !== 1 || !['simulation', 'hardware'].includes(String(value.mode))
    || typeof value.label !== 'string' || !integer(value.run_id)
    || !integer(value.last_event_id) || typeof value.server_time !== 'string'
    || !object(value.backend) || typeof value.backend.status !== 'string'
    || typeof value.backend.version !== 'string' || !object(value.clock)
    || !integer(value.clock.sim_ms) || typeof value.clock.speed !== 'number'
    || !Number.isFinite(value.clock.speed) || typeof value.clock.paused !== 'boolean') {
    throw new Error('The backend snapshot uses an unsupported format.');
  }
  return value as unknown as Snapshot;
}

function parseEnvelope(raw: string): Envelope {
  const value: unknown = JSON.parse(raw);
  if (!object(value) || value.v !== 1 || !integer(value.seq) || value.seq < 1
    || typeof value.type !== 'string' || !object(value.data)
    || (value.event_id !== undefined && !integer(value.event_id))) {
    throw new Error('The backend sent an unsupported message.');
  }
  return value as unknown as Envelope;
}

export class DashboardConnection {
  private state: ConnectionState = {
    status: 'connecting', snapshot: null, lastMessageAt: null, snapshotAt: null, error: null,
  };
  private socket: Socket | null = null;
  private active = false;
  private generation = 0;
  private syncing = false;
  private cursor = 0;
  private sequence = 0;
  private heartbeatMs = 2000;
  private pending: Envelope[] = [];
  private retryMs = 1000;
  private retryTimer?: ReturnType<typeof setTimeout>;
  private openTimer?: ReturnType<typeof setTimeout>;
  private tickTimer?: ReturnType<typeof setInterval>;
  private request?: AbortController;
  private now: () => number;

  constructor(private options: Options, private changed: (state: ConnectionState) => void) {
    this.now = options.now ?? Date.now;
  }

  start() {
    if (this.active) return;
    this.active = true;
    this.tickTimer = setInterval(() => {
      if (this.state.status === 'connected' && this.state.lastMessageAt !== null
        && this.now() - this.state.lastMessageAt > 3 * this.heartbeatMs) {
        this.update({ status: 'stale' });
      }
    }, 250);
    this.connect();
  }

  stop() {
    this.active = false;
    this.generation++;
    clearTimeout(this.retryTimer);
    clearInterval(this.tickTimer);
    this.release();
  }

  private update(patch: Partial<ConnectionState>) {
    this.state = { ...this.state, ...patch };
    if (this.active) this.changed(this.state);
  }

  private release() {
    clearTimeout(this.openTimer);
    this.request?.abort();
    this.request = undefined;
    if (this.socket) {
      this.socket.onopen = this.socket.onmessage = this.socket.onclose = this.socket.onerror = null;
      this.socket.close();
      this.socket = null;
    }
    this.pending = [];
    this.syncing = false;
  }

  private fail(message: string) {
    if (!this.active) return;
    this.generation++;
    this.release();
    clearTimeout(this.retryTimer);
    this.update({ status: 'disconnected', error: message });
    this.retryTimer = setTimeout(() => this.connect(), this.retryMs);
    this.retryMs = Math.min(10000, this.retryMs * 2);
  }

  private connect() {
    if (!this.active) return;
    this.release();
    this.sequence = 0;
    this.heartbeatMs = 2000;
    const generation = ++this.generation;
    this.update({ status: 'connecting', lastMessageAt: null });
    try {
      const socket = (this.options.createSocket ?? ((url) => new WebSocket(url)))(this.options.socketUrl);
      this.socket = socket;
      this.openTimer = setTimeout(() => this.fail('Backend connection timed out. Retrying automatically.'), 5000);
      socket.onopen = () => {
        clearTimeout(this.openTimer);
        if (generation === this.generation) void this.synchronize();
      };
      socket.onmessage = (message) => {
        if (generation !== this.generation) return;
        try {
          const event = parseEnvelope(String(message.data));
          const sequenceGap = event.seq !== this.sequence + 1;
          this.sequence = event.seq;
          this.update({ lastMessageAt: this.now() });
          if (event.type === 'status') {
            const interval = event.data.heartbeat_interval_s;
            if (typeof interval === 'number' && Number.isFinite(interval) && interval > 0) {
              this.heartbeatMs = interval * 1000;
            }
          }
          if (sequenceGap || (event.type === 'status'
            && event.data.run_id !== this.state.snapshot?.run_id)) void this.synchronize();
          if (!this.syncing && this.state.snapshot) this.update({ status: 'connected', error: null });
          this.handle(event);
        } catch (error) {
          this.fail(error instanceof Error ? error.message : 'Unable to read backend message.');
        }
      };
      socket.onclose = () => this.fail('Backend connection closed. Retrying automatically.');
      socket.onerror = () => this.fail('Cannot reach the backend. Check that it is running.');
    } catch {
      this.fail('Unable to open the backend connection.');
    }
  }

  private handle(event: Envelope) {
    if (event.event_id === undefined) return;
    if (this.syncing) {
      if (this.pending.length >= 2000) {
        this.fail('Too many updates arrived during synchronization. Reconnecting.');
      } else this.pending.push(event);
      return;
    }
    if (event.event_id <= this.cursor) return;
    if (event.type === 'reset' || event.event_id !== this.cursor + 1) {
      void this.synchronize();
      this.pending.push(event);
      return;
    }
    // D0 tracks continuity only; it does not turn packet events into a network view.
    this.cursor = event.event_id;
  }

  private async synchronize() {
    if (this.syncing || !this.active) return;
    this.syncing = true;
    this.update({ status: 'connecting' });
    const generation = this.generation;
    const request = new AbortController();
    this.request = request;
    const timeout = setTimeout(() => request.abort(), 5000);
    try {
      const response = await (this.options.fetchSnapshot ?? fetch)(this.options.snapshotUrl, {
        signal: request.signal, cache: 'no-store',
      });
      if (!response.ok) throw new Error(`Snapshot request failed (${response.status}).`);
      const snapshot = parseSnapshot(await response.json());
      if (!this.active || generation !== this.generation) return;
      this.cursor = snapshot.last_event_id;
      this.syncing = false;
      this.retryMs = 1000;
      // HTTP success does not prove that the WebSocket is still sending messages.
      const recent = this.state.lastMessageAt !== null
        && this.now() - this.state.lastMessageAt <= 3 * this.heartbeatMs;
      this.update({ snapshot, snapshotAt: this.now(), status: recent ? 'connected' : 'stale', error: null });
      const buffered = this.pending;
      this.pending = [];
      for (const event of buffered) this.handle(event);
    } catch (error) {
      if (this.active && generation === this.generation) {
        this.fail(error instanceof Error && error.name !== 'AbortError'
          ? error.message : 'Snapshot request timed out. Retrying automatically.');
      }
    } finally {
      clearTimeout(timeout);
    }
  }
}
