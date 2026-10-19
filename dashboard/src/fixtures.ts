import demoInitial from '../../tests/dashboard-fixtures/demo-run.snapshot-initial.json?raw';
import demoMid from '../../tests/dashboard-fixtures/demo-run.snapshot-mid.json?raw';
import demoEvents from '../../tests/dashboard-fixtures/demo-run.events.json?raw';
import outageInitial from '../../tests/dashboard-fixtures/outage-run.snapshot-initial.json?raw';
import outageMid from '../../tests/dashboard-fixtures/outage-run.snapshot-mid.json?raw';
import outageEvents from '../../tests/dashboard-fixtures/outage-run.events.json?raw';

export type NodeId = 'A' | 'B' | 'C' | 'D';
export interface NodeView { id: NodeId; status: string; session: number | null; last_report_age_ms: number | null; source?: { observed_by: string[]; silent_for_ms: number }; telemetry: { counters: Record<string, number>; received_ms: number; age_ms?: number; known_by?: string } | null }
export interface LinkView { a: NodeId; b: NodeId; state: string; usable: boolean; directions?: Record<string, boolean>; cost_ms?: number | null }
export interface RouteView { state: string; kind: string | null; path?: NodeId[]; version?: number | null; policy?: string; backups?: Record<string, string>; cost_ms?: number | null; delivery_probability?: number | null }
export interface Reading { origin: NodeId; session: number; seq: number; value: number; via: NodeId | null; observed_path: (NodeId | null)[]; received_ms: number; replayed: boolean; delay_ms: number }
export interface Count { value: number; known_by: string; as_of_ms?: number | null; age_ms?: number | null }
export interface RecordedEvent { v: number; seq: number; type: string; event_id?: number; data: Record<string, unknown> }
export interface NetworkSnapshot { v: number; mode: string; run_id: number; last_event_id: number; clock: { sim_ms: number }; nodes: NodeView[]; links: LinkView[]; route: RouteView; gateway: { state: string; network_view?: string; last_heard_ms?: number | null }; readings: { delivered_unique: number; duplicates: number; replayed: number; recent: Reading[] }; counts: Record<string, Count | null>; recent_events: RecordedEvent[] }
export interface RecordedRun { name: string; description: string; initial: NetworkSnapshot; mid: NetworkSnapshot; events: RecordedEvent[] }

const load = <T,>(raw: string): T => JSON.parse(raw) as T;
const bundle = (raw: string) => load<{ description: string; events: RecordedEvent[] }>(raw);
const demo = bundle(demoEvents);
const outage = bundle(outageEvents);
export const runs: RecordedRun[] = [
  { name: 'Relay failure', description: 'B relays readings at first. After B stops, A switches locally to D; the controller later confirms that route. B then returns.', initial: load(demoInitial), mid: load(demoMid), events: demo.events },
  { name: 'Controller outage', description: 'C loses its laptop link and holds messages while B stops. Once the link returns, C replays delayed readings and reports the failover.', initial: load(outageInitial), mid: load(outageMid), events: outage.events },
];

const data = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const num = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null;
const path = (value: unknown): NodeId[] => Array.isArray(value) ? value.filter((v): v is NodeId => ['A', 'B', 'C', 'D'].includes(String(v))) : [];

function apply(state: NetworkSnapshot, event: RecordedEvent) {
  const d = event.data;
  state.clock.sim_ms = num(d.sim_ms) ?? state.clock.sim_ms;
  const node = state.nodes.find(n => n.id === d.node);
  switch (event.type) {
    case 'node_seen':
      if (node) { node.status = 'reachable'; node.session = num(d.session); node.source = undefined; node.last_report_age_ms = 0; }
      break;
    case 'node_lost_contact':
      if (node) { node.status = 'lost_contact'; node.source = { observed_by: Array.isArray(d.observed_by) ? d.observed_by.map(String) : [], silent_for_ms: num(d.silent_for_ms) ?? 0 }; }
      break;
    case 'node_stale':
      if (node) { node.status = 'stale'; node.source = undefined; }
      break;
    case 'link_state_changed': {
      const ids = String(d.link).split('-') as NodeId[];
      if (ids.length !== 2) break;
      let link = state.links.find(l => l.a === ids[0] && l.b === ids[1]);
      if (!link) { link = { a: ids[0], b: ids[1], state: 'unknown', usable: false }; state.links.push(link); }
      link.state = String(d.new); link.usable = d.usable === true; link.directions = data(d.directions) as Record<string, boolean>;
      if (!link.usable) link.cost_ms = null;
      break;
    }
    case 'route_computed':
    case 'route_active':
      state.route = { ...state.route, state: event.type === 'route_active' ? 'active' : 'computed', kind: 'route', path: path(d.path), version: num(d.version), cost_ms: num(d.cost_ms), policy: String(d.policy), backups: data(d.backups) as Record<string, string>, delivery_probability: num(d.delivery_probability) };
      break;
    case 'route_installing':
      state.route.state = 'installing'; break;
    case 'gateway_link_lost':
      state.gateway.state = 'lost'; state.gateway.network_view = 'stale: nothing has arrived from C since the link was lost'; break;
    case 'gateway_link_restored':
      state.gateway.state = 'connected'; state.gateway.network_view = 'live'; break;
    case 'replay_started': state.gateway.state = 'replaying'; break;
    case 'replay_finished': state.gateway.state = 'connected'; break;
    case 'telemetry': {
      if (!node) break;
      const counters = data(d.counters) as Record<string, number>;
      node.telemetry = { counters, received_ms: num(d.received_ms) ?? state.clock.sim_ms, known_by: `${node.id} (own counters, delivered by flooding)` };
      if (node.id === 'A') {
        for (const key of ['created', 'acknowledged', 'lost', 'no_route']) state.counts[key] = { value: counters[key] ?? 0, known_by: 'A (telemetry)', as_of_ms: node.telemetry.received_ms };
        state.counts.pending = { value: Math.max(0, (counters.created ?? 0) - (counters.acknowledged ?? 0) - (counters.lost ?? 0) - (counters.no_route ?? 0)), known_by: 'A (telemetry, derived)', as_of_ms: node.telemetry.received_ms };
      }
      break;
    }
    case 'reading_received': {
      const reading = d as unknown as Reading;
      if (!Array.isArray(reading.observed_path)) break;
      state.readings.delivered_unique++;
      if (reading.replayed) state.readings.replayed++;
      state.readings.recent.push(reading);
      state.readings.recent = state.readings.recent.slice(-50);
      state.counts.delivered = { value: state.readings.delivered_unique, known_by: 'C', as_of_ms: state.clock.sim_ms };
      state.counts.delivered_late = { value: state.readings.replayed, known_by: 'C (held while the controller was unreachable)' };
      break;
    }
  }
  if (event.event_id !== undefined) {
    state.last_event_id = event.event_id;
    state.recent_events.push(event);
    state.recent_events = state.recent_events.slice(-100);
  }
}

/** A mid-run snapshot already includes every event through its last_event_id. */
export function viewAt(run: RecordedRun, cursor: number): NetworkSnapshot {
  const start = cursor >= run.mid.last_event_id ? run.mid : run.initial;
  const state = structuredClone(start);
  for (const event of run.events) {
    if (event.event_id !== undefined && event.event_id > start.last_event_id && event.event_id <= cursor) apply(state, event);
  }
  for (const value of Object.values(state.counts)) if (value?.as_of_ms != null) value.age_ms = Math.max(0, state.clock.sim_ms - value.as_of_ms);
  for (const node of state.nodes) if (node.telemetry) node.telemetry.age_ms = Math.max(0, state.clock.sim_ms - node.telemetry.received_ms);
  return state;
}

export const stateEvents = (run: RecordedRun, cursor: number): RecordedEvent[] => run.events.filter(e => e.event_id !== undefined && e.event_id <= cursor);
