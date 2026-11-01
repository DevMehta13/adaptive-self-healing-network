import type { ConnectionStatus } from './connection';

/** What a live page can truthfully say about its numbers. */
export type ViewState = 'live' | 'connecting' | 'down' | 'stale' | 'reconnecting' | 'refresh-failed';

export function viewState(opts: { status: ConnectionStatus; hasData: boolean; pollError: string | null }): ViewState {
  const { status, hasData, pollError } = opts;
  if (!hasData) return status === 'disconnected' ? 'down' : 'connecting';
  if (status === 'connected') return pollError ? 'refresh-failed' : 'live';
  if (status === 'stale') return 'stale';
  if (status === 'disconnected') return 'down';
  return 'reconnecting';
}

export const isLive = (state: ViewState) => state === 'live';

export const stateTag: Record<ViewState, string> = {
  live: 'Live view', connecting: 'Connecting', down: 'Backend down', stale: 'Stale view', reconnecting: 'Reconnecting', 'refresh-failed': 'Refresh failed',
};

export interface StateMessage { title: string; text: string; role: 'status' | 'alert' }

/** The banner for a page. `asOf` is the clock time of the last snapshot the numbers come from; `error` the refresh error, if any. */
export function stateMessage(state: ViewState, asOf: string | null, error: string | null = null): StateMessage | null {
  const from = asOf ? `the last snapshot, taken at ${asOf}` : 'the last snapshot';
  switch (state) {
    case 'live': return null;
    case 'connecting': return { title: 'Connecting', text: 'Connecting to the backend and loading a snapshot…', role: 'status' };
    case 'down': return asOf === null
      ? { title: 'Backend not reachable', text: 'There is nothing to show yet. The dashboard keeps retrying; start the backend (see the dashboard README) and this page fills in by itself.', role: 'alert' }
      : { title: 'Backend not reachable', text: `The numbers below are from ${from} and are not live. The dashboard keeps retrying; controls are off until it is back.`, role: 'alert' };
    case 'stale': return { title: 'Stale view', text: `The connection is open but live messages have stopped, so these numbers may be out of date. They are from ${from}.`, role: 'alert' };
    case 'reconnecting': return { title: 'Reconnecting', text: `Reconnecting to the backend. The numbers below are from ${from} until a fresh snapshot arrives.`, role: 'status' };
    case 'refresh-failed': return { title: 'Live refresh failed', text: `${error ?? 'The backend did not answer.'} The numbers below are from ${from}.`, role: 'alert' };
  }
}
