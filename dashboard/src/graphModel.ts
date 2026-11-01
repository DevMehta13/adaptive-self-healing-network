import type { LinkView, NodeId, NodeView } from './fixtures';
import { axisTime } from './chartData';
import { mapTime } from './liveData';
import type { ChangeEvent, TimeMap } from './liveData';
import { nodeLines } from './overview';
import type { NodeLine } from './overview';

export const WIDTH = 640;
export const HEIGHT = 420;
export const CENTRE: [number, number] = [320, 210];
export const NODE_RADIUS = 34;
export const positions: Record<NodeId, [number, number]> = { A: [96, 210], B: [320, 78], C: [544, 210], D: [320, 342] };
export const edges: [NodeId, NodeId][] = [['A', 'B'], ['B', 'C'], ['A', 'D'], ['D', 'C']];

export interface Geometry {
  width: number; height: number; centre: [number, number]; radius: number; positions: Record<NodeId, [number, number]>;
  labelOffset: number; labelW: number; labelH: number;
}

/** Wide screens use the roomy diamond; phones a taller, narrower one so that the text stays about as large as the page text. */
export const WIDE: Geometry = { width: WIDTH, height: HEIGHT, centre: CENTRE, radius: NODE_RADIUS, positions, labelOffset: 44, labelW: 100, labelH: 42 };
export const COMPACT: Geometry = {
  width: 360, height: 450, centre: [180, 225], radius: 28, labelOffset: 54, labelW: 108, labelH: 42,
  positions: { A: [58, 225], B: [180, 74], C: [302, 225], D: [180, 376] },
};
export const geometryFor = (compact: boolean) => compact ? COMPACT : WIDE;

export const edgeKey = (a: NodeId, b: NodeId) => [a, b].sort().join('-');

/** Where a link's label sits: the middle of the line pushed `offset` pixels away from the diamond's centre, so it never lies on a line. */
export function labelPosition(a: [number, number], b: [number, number], centre: [number, number] = CENTRE, offset = 40): { x: number; y: number } {
  const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
  let nx = -(b[1] - a[1]), ny = b[0] - a[0];
  const length = Math.hypot(nx, ny) || 1;
  nx /= length; ny /= length;
  if ((mx - centre[0]) * nx + (my - centre[1]) * ny < 0) { nx = -nx; ny = -ny; }
  return { x: mx + nx * offset, y: my + ny * offset };
}

/** A point `fraction` of the way from a to b (for the "planned" badge). */
export const along = (a: [number, number], b: [number, number], fraction: number) => ({ x: a[0] + (b[0] - a[0]) * fraction, y: a[1] + (b[1] - a[1]) * fraction });

const LINK_WORDS: Record<string, string> = { up: 'up', one_way: 'one-way', stale: 'stale', down: 'down' };

export interface LinkText { state: string; detail: string; tone: 'ok' | 'warn' | 'bad' | 'idle' }

/** The words on a link: the API's state, and the cost the controller uses when the link is up. A grey link has no evidence. */
export function linkText(link: LinkView | undefined): LinkText {
  if (!link) return { state: 'not reported', detail: '', tone: 'idle' };
  const state = LINK_WORDS[link.state] ?? link.state.replaceAll('_', ' ');
  const tone = link.state === 'up' ? 'ok' : link.state === 'down' ? 'bad' : 'warn';
  return { state, detail: link.state === 'up' && link.cost_ms != null ? `${link.cost_ms} ms` : '', tone };
}

export interface NodeFacts extends NodeLine { facts: string[] }

const STATE_SENTENCE: Record<string, string> = {
  reachable: 'Its own report is fresh',
  stale: 'Its reports expired and nothing newer exists',
  unknown: 'Nobody has reported on it yet',
};

/** What a node card shows: the API's state and the facts the snapshot (and the change buffer) give. */
export function nodeFacts(node: NodeView, changes: ChangeEvent[], map: TimeMap): NodeFacts {
  const line = nodeLines([node])[0];
  const facts: string[] = [];
  if (STATE_SENTENCE[node.status]) facts.push(STATE_SENTENCE[node.status]);
  if (line.detail) facts.push(line.detail[0].toUpperCase() + line.detail.slice(1));
  if (node.since_ms != null) facts.push(`In this state since ${axisTime(map, mapTime(map, node.since_ms))}`);
  if (node.session != null) facts.push(`Session ${node.session}`);
  const restart = [...changes].filter(c => c.data.node === node.id && (c.type === 'node_restarted' || (c.type === 'node_regained_contact' && c.data.restarted === true)))
    .sort((a, b) => b.at - a.at)[0];
  if (restart) facts.push(`Restarted, seen at ${axisTime(map, restart.at)}`);
  const counters = node.telemetry?.counters;
  if ((node.id === 'B' || node.id === 'D') && counters) {
    facts.push(`Forwarded ${counters.forwarded ?? 0} · dropped ${counters.dropped ?? 0}${counters.failovers ? ` · failovers ${counters.failovers}` : ''}`);
  }
  return { ...line, facts };
}

/** The eight link directions the simulator can take down, as "A->B". */
export const directions: string[] = edges.flatMap(([a, b]) => [`${a}->${b}`, `${b}->${a}`]);
