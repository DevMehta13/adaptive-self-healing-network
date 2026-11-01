import { describe, expect, it } from 'vitest';
import { runs, viewAt } from './fixtures';
import { storyMoments, storyProgress, storyRows } from './story';

describe('human-readable recorded story', () => {
  it('offers ordered, named moments instead of every low-level update', () => {
    for (const run of runs) {
      const moments = storyMoments(run);
      expect(moments.length).toBeLessThan(run.events.filter(event => event.event_id !== undefined).length);
      expect(moments[0].cursor).toBe(0);
      expect(moments.at(-1)?.cursor).toBe(Math.max(...run.events.map(event => event.event_id ?? 0)));
      expect(moments.every((moment, index) => index === 0 || moment.cursor > moments[index - 1].cursor)).toBe(true);
      expect(moments.every(moment => moment.title && moment.explanation)).toBe(true);
    }
  });

  it('keeps presenter action distinct from controller evidence', () => {
    const moments = storyMoments(runs[0]);
    const stopped = moments.find(moment => moment.title === 'B is stopped')!;
    const switched = moments.find(moment => moment.title === 'A switches to D')!;
    expect(stopped.source).toBe('injected');
    expect(switched.source).toBe('controller');
    expect(viewAt(runs[0], stopped.cursor).route.path).toEqual(['A', 'B', 'C']);
    expect(viewAt(runs[0], switched.cursor).route.path).toEqual(['A', 'B', 'C']);
  });

  it('shows an established gateway link once a reading has arrived', () => {
    for (const run of runs) {
      const firstReading = storyMoments(run).find(moment => moment.title === 'First reading reaches C')!;
      expect(viewAt(run, firstReading.cursor).gateway.state).toBe('connected');
    }
  });

  it('includes stale knowledge and delayed replay as separate outage moments', () => {
    const run = runs[1];
    const moments = storyMoments(run);
    const stale = moments.find(moment => moment.title === 'Network view is stale')!;
    const replay = moments.find(moment => moment.title === 'First delayed reading arrives')!;
    expect(viewAt(run, stale.cursor).gateway.state).toBe('lost');
    expect(viewAt(run, replay.cursor).readings.recent.at(-1)?.replayed).toBe(true);
    expect(stale.cursor).toBeLessThan(replay.cursor);
  });
});

describe('story navigation and rows', () => {
  it('says where you are in the story', () => {
    expect(storyProgress(0, 10)).toEqual({ position: 1, total: 10, label: 'Moment 1 of 10', percent: 0 });
    expect(storyProgress(4, 10).label).toBe('Moment 5 of 10');
    expect(storyProgress(9, 10).percent).toBe(100);
    expect(storyProgress(99, 10).position).toBe(10);
    expect(storyProgress(-3, 10).position).toBe(1);
    expect(storyProgress(0, 1).percent).toBe(100);
  });
  it('lists the story so far oldest first, with presenter actions marked and recorded times', () => {
    const moments = storyMoments(runs[0]);
    const stopped = moments.findIndex(moment => moment.title === 'B is stopped');
    const rows = storyRows(moments, stopped, moment => viewAt(runs[0], moment.cursor).clock.sim_ms);
    expect(rows).toHaveLength(stopped + 1);
    expect(rows[0].title).toBe('Before reports arrive');
    expect(rows.at(-1)).toMatchObject({ title: 'B is stopped', presenter: true, tag: 'What we did', kind: 'presenter' });
    expect(rows.filter(row => row.presenter)).toHaveLength(1);
    expect(rows.every(row => /^\d\d:\d\d$/.test(row.time) && row.detail)).toBe(true);
  });
});
