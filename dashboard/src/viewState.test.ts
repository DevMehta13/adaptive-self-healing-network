import { describe, expect, it } from 'vitest';
import { isLive, stateMessage, stateTag, viewState } from './viewState';
import type { ViewState } from './viewState';

describe('viewState', () => {
  it('is connecting, or down, while there is no data yet', () => {
    expect(viewState({ status: 'connecting', hasData: false, pollError: null })).toBe('connecting');
    expect(viewState({ status: 'stale', hasData: false, pollError: null })).toBe('connecting');
    expect(viewState({ status: 'disconnected', hasData: false, pollError: null })).toBe('down');
  });
  it('is live only when connected and refreshing', () => {
    expect(viewState({ status: 'connected', hasData: true, pollError: null })).toBe('live');
    expect(viewState({ status: 'connected', hasData: true, pollError: 'Live refresh failed (500).' })).toBe('refresh-failed');
  });
  it('is never live with old data while stale, down or reconnecting', () => {
    expect(viewState({ status: 'stale', hasData: true, pollError: null })).toBe('stale');
    expect(viewState({ status: 'disconnected', hasData: true, pollError: null })).toBe('down');
    expect(viewState({ status: 'connecting', hasData: true, pollError: null })).toBe('reconnecting');
    for (const s of ['stale', 'disconnected', 'connecting'] as const) expect(isLive(viewState({ status: s, hasData: true, pollError: null }))).toBe(false);
  });
});

describe('stateMessage', () => {
  it('has nothing to say when live', () => expect(stateMessage('live', '11:08:36')).toBeNull());
  it('says plainly that the backend is down and there is nothing to show yet', () => {
    const m = stateMessage('down', null)!;
    expect(m.title).toBe('Backend not reachable');
    expect(m.text).toContain('nothing to show yet');
    expect(m.role).toBe('alert');
  });
  it('says when old numbers are from and that they are not live', () => {
    expect(stateMessage('down', '11:08:36')!.text).toContain('taken at 11:08:36 and are not live');
    expect(stateMessage('stale', '11:08:36')!.text).toContain('may be out of date');
    expect(stateMessage('reconnecting', '11:08:36')!.text).toContain('from the last snapshot, taken at 11:08:36');
    expect(stateMessage('refresh-failed', '11:08:36', 'Live refresh failed (500).')!.text).toBe('Live refresh failed (500). The numbers below are from the last snapshot, taken at 11:08:36.');
  });
  it('has a tag and, except when live, a message for every state', () => {
    const states: ViewState[] = ['live', 'connecting', 'down', 'stale', 'reconnecting', 'refresh-failed'];
    for (const s of states) {
      expect(stateTag[s]).toBeTruthy();
      expect(stateMessage(s, '11:08:36') === null).toBe(s === 'live');
    }
  });
});
