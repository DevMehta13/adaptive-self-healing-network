import type { NetworkSnapshot, RecordedEvent } from './fixtures';

export interface LiveSnapshot extends NetworkSnapshot {
  mode: 'simulation' | 'hardware';
  scenario: string | null;
  clock: { sim_ms: number; speed: number; paused: boolean };
  policy: { current: string; available: string[] };
  presenter: { conditions: { stopped_nodes: string[]; gateway_attached: boolean } | null };
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const nonnegative = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

export function parseLiveSnapshot(value: unknown): LiveSnapshot {
  if (!record(value) || value.v !== 1 || !['simulation', 'hardware'].includes(String(value.mode))
    || !nonnegative(value.run_id) || !nonnegative(value.last_event_id)
    || !record(value.clock) || !nonnegative(value.clock.sim_ms)
    || typeof value.clock.paused !== 'boolean' || !Array.isArray(value.nodes)
    || !Array.isArray(value.links) || !record(value.route) || !record(value.gateway)
    || !record(value.readings) || !Array.isArray(value.readings.recent)
    || !record(value.counts) || !Array.isArray(value.recent_events)
    || !record(value.policy) || typeof value.policy.current !== 'string'
    || !Array.isArray(value.policy.available) || !value.policy.available.every(x => typeof x === 'string')
    || !record(value.presenter) || (value.presenter.conditions !== null && !record(value.presenter.conditions))) {
    throw new Error('The backend returned a live view this dashboard cannot display.');
  }
  return value as unknown as LiveSnapshot;
}

const name: Record<string, string> = {
  immediate_rtt: 'Immediate RTT', lowest_delay: 'Lowest delay', highest_reliability: 'Highest reliability',
};
export const policyName = (key: string) => name[key] ?? key.replaceAll('_', ' ');

export function eventText(event: RecordedEvent): string | null {
  const d = event.data;
  const path = Array.isArray(d.path) ? d.path.join(' → ') : '';
  switch (event.type) {
    case 'environment': return typeof d.label === 'string' ? d.label : 'Presenter changed the simulation';
    case 'local_failover': return `A switched locally${d.to ? ` to ${d.to}` : ''}`;
    case 'route_active': return `Controller confirmed route ${path || 'unknown'}`;
    case 'route_computed': return `Controller planned route ${path || 'unknown'}`;
    case 'node_lost_contact': return `${d.node ?? 'A node'} lost contact`;
    case 'node_seen': return `${d.node ?? 'A node'} reported in`;
    case 'gateway_link_lost': return 'C’s laptop link was lost';
    case 'gateway_link_restored': return 'C’s laptop link returned';
    case 'replay_started': return 'C started replaying held reports';
    case 'replay_finished': return 'C finished replaying held reports';
    case 'policy_changed': return `Routing policy changed to ${policyName(String(d.new ?? 'unknown'))}`;
    case 'no_route': return 'Controller found no route';
    default: return null;
  }
}

export async function postLiveAction(path: string, body: Record<string, unknown>, signal?: AbortSignal): Promise<void> {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  });
  if (response.ok) return;
  let message = `The backend rejected this action (${response.status}).`;
  try {
    const result: unknown = await response.json();
    if (record(result) && record(result.error) && typeof result.error.message === 'string') message = result.error.message;
  } catch { /* Preserve the status-based message. */ }
  throw new Error(message);
}
