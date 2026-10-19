import { describe, expect, it } from 'vitest';
import { runs, viewAt } from './fixtures';
import { storyMoments } from './story';

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
