import { eventText } from './live';
import type { NodeId, NodeView, Reading, RecordedEvent } from './fixtures';

export const nodeRoles: Record<NodeId, string> = { A: 'Source', B: 'Relay', C: 'USB gateway', D: 'Relay' };

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

/** The reading that reached C most recently. `recent` is in the order the controller learned of readings, so a replayed one can follow a newer live one: compare received_ms, and let the later entry win a tie. */
export function latestReading(recent: Reading[]): Reading | undefined {
  let best: Reading | undefined;
  for (const reading of recent) if (!best || reading.received_ms >= best.received_ms) best = reading;
  return best;
}

/** How long ago C received the reading, on the controller clock; never negative (replayed received_ms is an estimate). */
export const readingAgeMs = (simMs: number, reading: Reading) => Math.max(0, simMs - reading.received_ms);

export function ageText(ms: number): string {
  if (ms < 1500) return 'just now';
  if (ms < 60_000) return `${Math.round(ms / 1000)} s ago`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes} min ${Math.round((ms % 60_000) / 1000)} s ago`;
}

const statusWords: Record<string, string> = {
  reachable: 'reachable', lost_contact: 'lost contact', stale: 'stale', unknown: 'unknown (never heard)',
};

export interface NodeLine { id: NodeId; role: string; status: string; tone: 'ok' | 'bad' | 'warn' | 'idle'; detail: string }

/** One status line per node, using the API's own status words ("lost contact", never "powered off"). */
export function nodeLines(nodes: NodeView[]): NodeLine[] {
  return nodes.map(node => {
    const tone = node.status === 'reachable' ? 'ok' : node.status === 'lost_contact' ? 'bad' : node.status === 'stale' ? 'warn' : 'idle';
    const parts: string[] = [];
    if (node.status === 'lost_contact' && node.source) parts.push(`seen as silent by ${node.source.observed_by.join(', ') || 'neighbours'} for ${seconds(node.source.silent_for_ms)}`);
    if (node.last_report_age_ms != null) parts.push(`last report ${seconds(node.last_report_age_ms)} old`);
    return { id: node.id, role: nodeRoles[node.id], status: statusWords[node.status] ?? node.status.replaceAll('_', ' '), tone, detail: parts.join(' · ') };
  });
}

export interface Change { id: number | string; title: string; time: string; ago: string; injected: boolean }

/** Newest first, only the events the dashboard has a readable title for. */
export function recentChanges(events: RecordedEvent[], simMs: number, limit = 5): Change[] {
  const out: Change[] = [];
  for (let i = events.length - 1; i >= 0 && out.length < limit; i--) {
    const event = events[i];
    const title = eventText(event);
    if (title === null) continue;
    const at = typeof event.data.sim_ms === 'number' ? event.data.sim_ms : null;
    out.push({ id: event.event_id ?? i, title, time: at === null ? '—' : seconds(at), ago: at === null ? '' : ageText(Math.max(0, simMs - at)), injected: event.type === 'environment' });
  }
  return out;
}
