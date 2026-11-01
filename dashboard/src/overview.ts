import { eventText } from './live';
import type { NodeId, NodeView, Reading } from './fixtures';
import type { ChangeEvent, TimeMap } from './liveData';
import { axisTime } from './chartData';

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

/** Newest first, only the events the dashboard has a readable title for. Times are on the shared axis (wall clock in hardware, simulated in simulation). */
export function recentChanges(changes: ChangeEvent[], nowAt: number | null, map: TimeMap, limit = 5): Change[] {
  const out: Change[] = [];
  for (let i = changes.length - 1; i >= 0 && out.length < limit; i--) {
    const change = changes[i];
    const title = eventText({ type: change.type, event_id: change.id, data: change.data });
    if (title === null) continue;
    out.push({ id: change.key, title, time: axisTime(map, change.at), ago: nowAt === null ? '' : ageText(Math.max(0, nowAt - change.at)), injected: change.type === 'environment' });
  }
  return out;
}
