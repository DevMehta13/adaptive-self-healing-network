import { describe, expect, it } from 'vitest';
import type { ChangeEvent, TimeMap } from './liveData';
import { PAGE_SIZE, buildTimeline, eventKind, eventTitle, filterTimeline, isMajor, majorChanges, visibleCount } from './timeline';

const sim: TimeMap = { mode: 'simulation', offsetMs: 0 };
const ev = (id: number, type: string, atSec: number, data: Record<string, unknown> = {}): ChangeEvent => ({ key: `1:${id}`, id, type, at: atSec * 1000, data });

describe('eventKind', () => {
  it('groups route, node, link, controller-link and presenter events', () => {
    expect(eventKind('route_active')).toBe('route');
    expect(eventKind('local_failover')).toBe('route');
    expect(eventKind('no_available_route')).toBe('route');
    expect(eventKind('node_lost_contact')).toBe('node');
    expect(eventKind('link_state_changed')).toBe('link');
    expect(eventKind('gateway_link_lost')).toBe('gateway');
    expect(eventKind('replay_started')).toBe('gateway');
    expect(eventKind('environment')).toBe('presenter');
    expect(eventKind('runtime')).toBe('presenter');
  });
  it('puts an unknown future event under route rather than hiding it', () => expect(eventKind('something_new')).toBe('route'));
});

describe('eventTitle', () => {
  it('uses the API wording for recovery', () => {
    expect(eventTitle(ev(1, 'local_failover', 1, { node: 'A', from: 'B', to: 'D', reason: 'send_failures' }))).toEqual({
      title: 'Local failover: A switched from B to D', detail: 'After repeated send failures',
    });
    expect(eventTitle(ev(1, 'local_failover', 1, { node: 'A', from: 'B', to: null, reason: 'neighbor_lost', replayed: true })).title).toBe('Local failover: A switched from B with no backup left');
    expect(eventTitle(ev(1, 'local_failover', 1, { node: 'A', to: 'D', reason: 'neighbor_lost', replayed: true })).detail).toContain('reported late');
  });
  it('tells a controller confirmation apart from a reroute', () => {
    expect(eventTitle(ev(1, 'route_active', 1, { path: ['A', 'D', 'C'], trigger: 'local_failover_report' })).title).toBe('Controller confirmed the failover: route A → D → C');
    expect(eventTitle(ev(1, 'route_active', 1, { path: ['A', 'D', 'C'], trigger: 'topology_changed' })).title).toBe('Controller rerouted: A → D → C');
    expect(eventTitle(ev(1, 'route_active', 1, { path: ['A', 'B', 'C'], trigger: 'initial' })).title).toBe('Controller confirmed route A → B → C');
  });
  it('says lost contact, never powered off, and keeps the headline text', () => {
    const lost = eventTitle(ev(1, 'node_lost_contact', 1, { node: 'B', observed_by: ['A', 'C'], silent_for_ms: 3100 }));
    expect(lost.title).toBe('B lost contact');
    expect(lost.detail).toBe('Seen as silent by A, C for 3.1 s. A timeout cannot prove it is powered off.');
    expect(eventTitle(ev(1, 'no_available_route', 1)).title).toBe('No available route');
  });
  it('words link, gateway and replay events', () => {
    expect(eventTitle(ev(1, 'link_state_changed', 1, { link: 'B-C', old: 'up', new: 'one_way' }))).toEqual({ title: 'Link B–C is now one way', detail: 'Was up' });
    expect(eventTitle(ev(1, 'gateway_link_lost', 1)).title).toBe('Controller lost its link to C');
    expect(eventTitle(ev(1, 'gateway_link_restored', 1, { lost_for_ms: 29000 })).detail).toBe('Lost for 29.0 s');
    expect(eventTitle(ev(1, 'replay_started', 1, { entries: 24, outage_ms: 29000 })).detail).toBe('24 entries after an outage of about 29.0 s');
    expect(eventTitle(ev(1, 'uplink_loss', 1, { count: 1, reason: 'c_restarted' })).title).toBe('1 message from C will never arrive');
  });
  it('labels presenter actions with the backend’s own label', () => {
    expect(eventTitle(ev(1, 'environment', 1, { label: 'Stop B (injected by presenter, simulation)' })).title).toBe('Stop B (injected by presenter, simulation)');
  });
  it('falls back to a readable type for events it does not know', () => expect(eventTitle(ev(1, 'future_thing', 1)).title).toBe('future thing'));
});

describe('buildTimeline', () => {
  const changes = [ev(1, 'node_lost_contact', 5, { node: 'B' }), ev(2, 'environment', 4, { label: 'Stop B' }), ev(3, 'local_failover', 6, { node: 'A', to: 'D' })];
  it('is newest first with absolute and relative time on the chart’s clock', () => {
    const items = buildTimeline(changes, 10_000, sim);
    expect(items.map(i => i.key)).toEqual(['1:3', '1:1', '1:2']);
    expect(items[0]).toMatchObject({ time: '00:06', ago: '4 s ago', kind: 'route', tag: 'Route', presenter: false });
    expect(items[2]).toMatchObject({ kind: 'presenter', tag: 'What we did', presenter: true });
  });
  it('uses the wall clock in hardware mode', () => {
    const hw: TimeMap = { mode: 'hardware', offsetMs: 0 };
    const at = new Date(2026, 10, 1, 9, 5, 7).getTime();
    const items = buildTimeline([{ ...changes[0], at }], at + 90_000, hw);
    expect(items[0].time).toBe('09:05:07');
    expect(items[0].ago).toBe('1 min 30 s ago');
  });
  it('has no relative time without a clock', () => expect(buildTimeline(changes, null, sim)[0].ago).toBe(''));
});

describe('filterTimeline and paging', () => {
  const items = buildTimeline([ev(1, 'node_lost_contact', 5), ev(2, 'environment', 4), ev(3, 'route_active', 6, { path: ['A'] })], 10_000, sim);
  it('separates what we did from what the controller noticed', () => {
    expect(filterTimeline(items, 'all')).toHaveLength(3);
    expect(filterTimeline(items, 'presenter').map(i => i.key)).toEqual(['1:2']);
    expect(filterTimeline(items, 'controller').map(i => i.key)).toEqual(['1:3', '1:1']);
  });
  it('shows a page at a time and never more than exist', () => {
    expect(visibleCount(100, 1)).toBe(PAGE_SIZE);
    expect(visibleCount(100, 3)).toBe(PAGE_SIZE * 3);
    expect(visibleCount(4, 1)).toBe(4);
    expect(visibleCount(0, 1)).toBe(0);
    expect(visibleCount(10, 0)).toBe(10);
  });
});

describe('isMajor', () => {
  it('keeps what happened and drops the steps in between', () => {
    for (const type of ['environment', 'local_failover', 'route_active', 'no_available_route', 'node_lost_contact', 'node_regained_contact', 'node_restarted', 'gateway_link_lost', 'gateway_link_restored', 'replay_started', 'replay_finished', 'uplink_loss', 'policy_changed', 'route_install_failed', 'runtime', 'reset']) {
      expect(isMajor(type, {})).toBe(true);
    }
    for (const type of ['route_installing', 'route_computed', 'route_candidate', 'route_candidate_dropped', 'epoch_advanced', 'link_held_down', 'node_seen', 'node_stale', 'no_route_confirmed']) {
      expect(isMajor(type, {})).toBe(false);
    }
  });
  it('counts a link as major when it goes up, down or one-way, not when it merely goes stale', () => {
    expect(isMajor('link_state_changed', { new: 'down' })).toBe(true);
    expect(isMajor('link_state_changed', { new: 'up' })).toBe(true);
    expect(isMajor('link_state_changed', { new: 'one_way' })).toBe(true);
    expect(isMajor('link_state_changed', { new: 'stale' })).toBe(false);
  });
  it('never hides an event type it does not know', () => expect(isMajor('something_new', {})).toBe(true));
});

describe('major-only filtering', () => {
  const changes = [
    ev(1, 'environment', 1, { label: 'Stop B' }), ev(2, 'local_failover', 2, { node: 'A', to: 'D' }), ev(3, 'route_installing', 3, { phase: 'relays' }),
    ev(4, 'route_computed', 3, { path: ['A', 'D', 'C'] }), ev(5, 'route_active', 4, { path: ['A', 'D', 'C'] }), ev(6, 'link_state_changed', 5, { link: 'A-B', new: 'stale' }),
  ];
  const items = buildTimeline(changes, 10_000, sim);
  it('hides route-installation steps and keeps the list one click away', () => {
    expect(filterTimeline(items, 'all', true).map(i => i.key)).toEqual(['1:5', '1:2', '1:1']);
    expect(filterTimeline(items, 'all', false)).toHaveLength(6);
  });
  it('combines with the presenter filters', () => {
    expect(filterTimeline(items, 'presenter', true).map(i => i.key)).toEqual(['1:1']);
    expect(filterTimeline(items, 'controller', true).map(i => i.key)).toEqual(['1:5', '1:2']);
  });
  it('gives the Overview the same titles and tags as the Network timeline', () => {
    const overview = majorChanges(changes, 10_000, sim, 5);
    const network = filterTimeline(items, 'all', true);
    expect(overview).toEqual(network);
    expect(majorChanges(changes, 10_000, sim, 2)).toHaveLength(2);
  });
});
