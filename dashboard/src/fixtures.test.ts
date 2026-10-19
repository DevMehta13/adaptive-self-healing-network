import { describe, expect, it } from 'vitest';
import { runs, stateEvents, viewAt } from './fixtures';

describe('recorded network evidence', () => {
  it('starts with unknown boards and no invented packet delivery', () => {
    const view = viewAt(runs[0], 0);
    expect(view.nodes.map(n => n.status)).toEqual(['unknown', 'unknown', 'unknown', 'unknown']);
    expect(view.route.state).toBe('waiting');
    expect(view.readings.delivered_unique).toBe(0);
  });

  it('uses the mid-run snapshot without counting its events twice', () => {
    for (const run of runs) {
      const view = viewAt(run, run.mid.last_event_id);
      expect(view.readings).toEqual(run.mid.readings);
      expect(view.route).toEqual(run.mid.route);
      expect(view.last_event_id).toBe(run.mid.last_event_id);
      expect(view.recent_events).toEqual(run.mid.recent_events);
    }
  });

  it('separates the controller plan from C evidence during local failover', () => {
    const run = runs[0];
    const failover = run.events.find(e => e.type === 'local_failover');
    expect(failover?.event_id).toBeDefined();
    const view = viewAt(run, failover!.event_id!);
    expect(view.route.path).toEqual(['A', 'B', 'C']);
    expect(view.route.state).toBe('active');
    expect(stateEvents(run, failover!.event_id!).at(-1)?.type).toBe('local_failover');
  });

  it('shows stale controller knowledge during an outage, then delayed replay', () => {
    const run = runs[1];
    const mid = viewAt(run, run.mid.last_event_id);
    expect(mid.gateway.state).toBe('lost');
    expect(mid.nodes.every(n => n.status === 'stale')).toBe(true);
    expect(mid.counts.created?.age_ms).toBeGreaterThan(20_000);
    const replay = run.events.find(e => e.type === 'reading_received' && e.data.replayed === true)!;
    const after = viewAt(run, replay.event_id!);
    expect(after.readings.replayed).toBeGreaterThan(0);
    expect(after.readings.recent.at(-1)?.delay_ms).toBeGreaterThan(0);
    expect(after.readings.recent.at(-1)?.replayed).toBe(true);
  });

  it('reaches the recorded outcomes with unique delivery counts', () => {
    const expected = [(['A', 'B', 'C']), (['A', 'D', 'C'])];
    runs.forEach((run, index) => {
      const last = Math.max(...run.events.map(e => e.event_id ?? 0));
      const view = viewAt(run, last);
      const unique = run.events.filter(e => e.type === 'reading_received').length;
      expect(view.route.path).toEqual(expected[index]);
      expect(view.readings.delivered_unique).toBe(unique);
      expect(view.counts.delivered?.value).toBe(unique);
      expect(view.last_event_id).toBe(last);
    });
  });
});
