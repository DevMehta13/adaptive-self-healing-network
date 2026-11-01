import { useEffect, useMemo, useState } from 'react';
import type { ConnectionState } from './connection';
import { parseLiveSnapshot, postLiveAction } from './live';
import type { LiveStore, SnapshotLike } from './liveData';
import type { LiveSnapshot } from './live';

/** The refreshed live view and the presenter actions. Called once by the app, so changing page never restarts the polling. */
export function useLive(connection: ConnectionState, store: LiveStore) {
  const [polled, setPolled] = useState<{ snapshot: LiveSnapshot; baseAt: number | null; loadedAt: number } | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [feedbackOk, setFeedbackOk] = useState(true);
  const [refresh, setRefresh] = useState(0);

  const base = useMemo(() => {
    if (!connection.snapshot) return null;
    try { return parseLiveSnapshot(connection.snapshot); } catch { return null; }
  }, [connection.snapshot]);
  const current = polled?.baseAt === connection.snapshotAt && base
    && polled.snapshot.run_id === base.run_id && polled.snapshot.last_event_id >= base.last_event_id
    ? polled.snapshot : base;
  const loadedAt = current === polled?.snapshot ? polled.loadedAt : connection.snapshotAt;
  const connected = connection.status === 'connected';
  const controlsReady = connected && current !== null && pending === null && pollError === null;

  useEffect(() => {
    if (!connected || !base) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const poll = async () => {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 5000);
      try {
        const response = await fetch('/api/snapshot', { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error(`Live refresh failed (${response.status}).`);
        const snapshot = parseLiveSnapshot(await response.json());
        if (active) {
          store.ingestSnapshot(snapshot as unknown as SnapshotLike);
          setPolled({ snapshot, baseAt: connection.snapshotAt, loadedAt: Date.now() });
          setPollError(null);
        }
      } catch (error) {
        if (active) setPollError(error instanceof Error ? error.message : 'Live refresh failed.');
      } finally {
        clearTimeout(timeout);
        if (active) timer = setTimeout(poll, 1000);
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); controller?.abort(); };
  }, [connected, base, connection.snapshotAt, refresh, store]);

  async function act(label: string, path: string, body: Record<string, unknown>) {
    if (!controlsReady) return;
    setPending(label);
    setFeedback(null);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      await postLiveAction(path, body, controller.signal);
      setFeedback(`${label} applied. Watch the timeline and the readings for the result.`);
      setFeedbackOk(true);
      setRefresh(value => value + 1);
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : 'The action failed.');
      setFeedbackOk(false);
    } finally {
      clearTimeout(timeout);
      setPending(null);
    }
  }

  return { status: connection.status, base, current, loadedAt, connected, pollError, pending, feedback, feedbackOk, controlsReady, act };
}

export type Live = ReturnType<typeof useLive>;
