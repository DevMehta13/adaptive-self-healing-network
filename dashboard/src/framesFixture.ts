// Test data only (not imported by the app, so the bundler leaves the recorded run out): the recorded `frames-run`.
import events from '../../tests/dashboard-fixtures/frames-run.events.json?raw';
import initial from '../../tests/dashboard-fixtures/frames-run.snapshot-initial.json?raw';
import mid from '../../tests/dashboard-fixtures/frames-run.snapshot-mid.json?raw';
import type { FrameRecord } from './frames';
import { toFrameRow } from './frames';
import { timeMapFor } from './liveData';
import type { SnapshotLike } from './liveData';

interface Message { seq: number; type: string; event_id?: number; data: Record<string, unknown> }
type FramesSnapshot = SnapshotLike & { last_event_id: number; frames: { last_id: number; recent: FrameRecord[] } };
export interface FramesRun {
  initial: FramesSnapshot;
  mid: FramesSnapshot;
  messages: Message[];
  /** Every record the stream carried, in order. */
  records: FrameRecord[];
}

const parse = <T,>(raw: string) => JSON.parse(raw) as T;
const messages = parse<{ events: Message[] }>(events).events;
export const framesRun: FramesRun = {
  initial: parse(initial), mid: parse(mid), messages,
  records: messages.filter(m => m.type === 'frames').flatMap(m => m.data.records as FrameRecord[]),
};

/** Rows for the whole run on the simulated clock, as the app's buffer would hold them. */
export const framesRows = (instance = 1) => framesRun.records.map(r => toFrameRow(r, timeMapFor(framesRun.mid), instance));
