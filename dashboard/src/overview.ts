import { oldText, seconds } from './format';
import type { NodeId, NodeView, Reading } from './fixtures';

export const nodeRoles: Record<NodeId, string> = { A: 'Source', B: 'Relay', C: 'USB gateway', D: 'Relay' };

/** The reading that reached C most recently. `recent` is in the order the controller learned of readings, so a replayed one can follow a newer live one: compare received_ms, and let the later entry win a tie. */
export function latestReading(recent: Reading[]): Reading | undefined {
  let best: Reading | undefined;
  for (const reading of recent) if (!best || reading.received_ms >= best.received_ms) best = reading;
  return best;
}

/** How long ago C received the reading, on the controller clock; never negative (replayed received_ms is an estimate). */
export const readingAgeMs = (simMs: number, reading: Reading) => Math.max(0, simMs - reading.received_ms);

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
    if (node.last_report_age_ms != null) parts.push(`last report ${oldText(node.last_report_age_ms)}`);
    return { id: node.id, role: nodeRoles[node.id], status: statusWords[node.status] ?? node.status.replaceAll('_', ' '), tone, detail: parts.join(' · ') };
  });
}
