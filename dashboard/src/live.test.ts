import { afterEach, describe, expect, it, vi } from 'vitest';
import { runs } from './fixtures';
import { eventText, parseLiveSnapshot, postLiveAction } from './live';

const live = () => ({ ...structuredClone(runs[0].mid), mode: 'simulation', scenario: 'live-demo',
  clock: { sim_ms: 20000, speed: 1, paused: false },
  policy: { current: 'immediate_rtt', available: ['immediate_rtt', 'lowest_delay', 'highest_reliability'] },
  presenter: { conditions: { stopped_nodes: [], gateway_attached: true } },
});

afterEach(() => vi.unstubAllGlobals());

describe('live snapshot and actions', () => {
  it('accepts the complete backend shape and rejects a partial or incompatible view', () => {
    expect(parseLiveSnapshot(live()).policy.current).toBe('immediate_rtt');
    expect(() => parseLiveSnapshot({ ...live(), readings: {} })).toThrow('cannot display');
    expect(() => parseLiveSnapshot({ ...live(), v: 2 })).toThrow('cannot display');
  });

  it('sends JSON actions to the requested endpoint and exposes backend rejection', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{}', { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'not_available', message: 'Simulation controls unavailable' } }), { status: 409 }));
    vi.stubGlobal('fetch', fetcher);
    await postLiveAction('/api/control/node', { node: 'B', action: 'stop' });
    expect(fetcher).toHaveBeenCalledWith('/api/control/node', expect.objectContaining({
      method: 'POST', body: '{"node":"B","action":"stop"}', headers: { 'Content-Type': 'application/json' },
    }));
    await expect(postLiveAction('/api/control/node', { node: 'B', action: 'stop' })).rejects.toThrow('Simulation controls unavailable');
  });

  it('marks presenter action separately and hides routine telemetry', () => {
    expect(eventText({ type: 'environment', data: { label: 'Stop node B', source: 'presenter' } })).toBe('Stop node B');
    expect(eventText({ type: 'route_active', data: { path: ['A', 'D', 'C'] } })).toBe('Controller confirmed route A → D → C');
    expect(eventText({ type: 'reading_received', data: {} })).toBeNull();
  });
});
