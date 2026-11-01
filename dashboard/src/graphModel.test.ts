import { describe, expect, it } from 'vitest';
import type { LinkView, NodeView } from './fixtures';
import type { ChangeEvent, TimeMap } from './liveData';
import { CENTRE, COMPACT, NODE_RADIUS, WIDE, along, directions, edgeKey, edges, geometryFor, labelPosition, linkText, nodeFacts, positions } from './graphModel';

const sim: TimeMap = { mode: 'simulation', offsetMs: 0 };
const node = (id: NodeView['id'], status: string, extra: Partial<NodeView> = {}) => ({ id, status, session: 111, last_report_age_ms: 2200, telemetry: null, ...extra }) as NodeView;

describe('labelPosition', () => {
  it('puts every label off its line, outside the diamond, and clear of the nodes', () => {
    for (const [a, b] of edges) {
      const pa = positions[a], pb = positions[b];
      const { x, y } = labelPosition(pa, pb, CENTRE, 44);
      const mid = [(pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2];
      const distanceToLine = Math.abs((pb[1] - pa[1]) * x - (pb[0] - pa[0]) * y + pb[0] * pa[1] - pb[1] * pa[0]) / Math.hypot(pb[0] - pa[0], pb[1] - pa[1]);
      expect(distanceToLine).toBeCloseTo(44, 5);
      expect(Math.hypot(x - CENTRE[0], y - CENTRE[1])).toBeGreaterThan(Math.hypot(mid[0] - CENTRE[0], mid[1] - CENTRE[1]));
      for (const p of Object.values(positions)) expect(Math.hypot(x - p[0], y - p[1])).toBeGreaterThan(NODE_RADIUS + 30);
    }
  });
  it('never puts two labels on top of each other', () => {
    const labels = edges.map(([a, b]) => labelPosition(positions[a], positions[b], CENTRE, 44));
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) {
      expect(Math.hypot(labels[i].x - labels[j].x, labels[i].y - labels[j].y)).toBeGreaterThan(110);
    }
  });
});

describe('graph helpers', () => {
  it('names an edge the same way from both ends', () => expect(edgeKey('B', 'A')).toBe(edgeKey('A', 'B')));
  it('finds a point along a line', () => expect(along([0, 0], [10, 20], 0.5)).toEqual({ x: 5, y: 10 }));
  it('lists both directions of the four links', () => {
    expect(directions).toHaveLength(8);
    expect(directions).toContain('A->B');
    expect(directions).toContain('B->A');
  });
});

describe('linkText', () => {
  const link = (state: string, extra: Partial<LinkView> = {}) => ({ a: 'A', b: 'B', state, usable: state === 'up', ...extra }) as LinkView;
  it('keeps the meaning of up, one-way, stale, down and no evidence', () => {
    expect(linkText(link('up', { cost_ms: 30 }))).toEqual({ state: 'up', detail: '30 ms', tone: 'ok' });
    expect(linkText(link('one_way'))).toMatchObject({ state: 'one-way', tone: 'warn' });
    expect(linkText(link('stale'))).toMatchObject({ state: 'stale', tone: 'warn' });
    expect(linkText(link('down'))).toMatchObject({ state: 'down', tone: 'bad' });
    expect(linkText(undefined)).toEqual({ state: 'not reported', detail: '', tone: 'idle' });
  });
  it('shows no cost for a link that is not up', () => expect(linkText(link('stale', { cost_ms: 30 })).detail).toBe(''));
});

describe('nodeFacts', () => {
  it('describes a healthy node with the facts the snapshot gives', () => {
    const facts = nodeFacts(node('B', 'reachable', { since_ms: 5000, telemetry: { counters: { forwarded: 25, dropped: 1, failovers: 0 }, received_ms: 1 } }), [], sim);
    expect(facts).toMatchObject({ role: 'Relay', status: 'reachable', tone: 'ok' });
    expect(facts.facts).toEqual(['Its own report is fresh', 'Last report 2.2 s old', 'In this state since 00:05', 'Session 111', 'Forwarded 25 · dropped 1']);
  });
  it('explains lost contact with who saw it, and never says powered off', () => {
    const facts = nodeFacts(node('B', 'lost_contact', { source: { observed_by: ['A', 'C'], silent_for_ms: 4632 } }), [], sim);
    expect(facts).toMatchObject({ status: 'lost contact', tone: 'bad' });
    expect(facts.facts.join(' ')).toContain('Seen as silent by A, C for 4.6 s');
    expect(facts.facts.join(' ')).not.toMatch(/powered off/i);
  });
  it('names stale and unknown nodes', () => {
    expect(nodeFacts(node('D', 'stale'), [], sim).facts[0]).toBe('Its reports expired and nothing newer exists');
    expect(nodeFacts(node('C', 'unknown', { last_report_age_ms: null, session: null }), [], sim)).toMatchObject({ status: 'unknown (never heard)', tone: 'idle', facts: ['Nobody has reported on it yet'] });
  });
  it('shows a restart seen in the change buffer, for that node only', () => {
    const changes: ChangeEvent[] = [
      { key: '1:1', id: 1, type: 'node_regained_contact', at: 9000, data: { node: 'B', restarted: true } },
      { key: '1:2', id: 2, type: 'node_regained_contact', at: 12_000, data: { node: 'D', restarted: false } },
      { key: '1:3', id: 3, type: 'node_restarted', at: 20_000, data: { node: 'D' } },
    ];
    expect(nodeFacts(node('B', 'reachable'), changes, sim).facts).toContain('Restarted, seen at 00:09');
    expect(nodeFacts(node('D', 'reachable'), changes, sim).facts).toContain('Restarted, seen at 00:20');
    expect(nodeFacts(node('A', 'reachable'), changes, sim).facts.join(' ')).not.toContain('Restarted');
  });
});

describe.each([['wide', WIDE], ['compact', COMPACT]] as const)('%s geometry', (_name, g) => {
  const labels = edges.map(([a, b]) => ({ ...labelPosition(g.positions[a], g.positions[b], g.centre, g.labelOffset) }));
  it('keeps every label box inside the picture', () => {
    for (const { x, y } of labels) {
      expect(x - g.labelW / 2).toBeGreaterThanOrEqual(0);
      expect(x + g.labelW / 2).toBeLessThanOrEqual(g.width);
      expect(y - g.labelH / 2).toBeGreaterThanOrEqual(0);
      expect(y + g.labelH / 2).toBeLessThanOrEqual(g.height);
    }
  });
  it('keeps label boxes off the nodes and off each other', () => {
    for (const { x, y } of labels) for (const p of Object.values(g.positions)) {
      const dx = Math.max(Math.abs(x - p[0]) - g.labelW / 2, 0), dy = Math.max(Math.abs(y - p[1]) - g.labelH / 2, 0);
      expect(Math.hypot(dx, dy)).toBeGreaterThan(g.radius + 4);
    }
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) {
      const apart = Math.abs(labels[i].x - labels[j].x) >= g.labelW || Math.abs(labels[i].y - labels[j].y) >= g.labelH;
      expect(apart).toBe(true);
    }
  });
  it('keeps the nodes inside the picture', () => {
    for (const p of Object.values(g.positions)) {
      expect(p[0] - g.radius).toBeGreaterThan(0); expect(p[0] + g.radius).toBeLessThan(g.width);
      expect(p[1] - g.radius - 30).toBeGreaterThan(0); expect(p[1] + g.radius + 40).toBeLessThan(g.height);
    }
  });
});

describe('geometryFor', () => it('picks the compact diamond for phones', () => { expect(geometryFor(true)).toBe(COMPACT); expect(geometryFor(false)).toBe(WIDE); }));
