import { mapTime } from './liveData';
import type { TimeMap } from './liveData';

/** The five types node C hands up; the backend's `frames.counts.by_type` always has exactly these (docs/DASHBOARD_API.md, "Frame records"). */
export const FRAME_TYPES = ['DATA', 'TOPOLOGY_REPORT', 'ROUTE_INSTALL_ACK', 'LOCAL_FAILOVER_REPORT', 'TELEMETRY'] as const;
export type FrameTypeName = typeof FRAME_TYPES[number];

export const FRAME_CAP = 1000;

export interface FrameHeader {
  version: number; type: number; type_name: string; src: string; dst: string;
  session: number; seq: number; ttl: number; payload_len: number;
}

/** One frame record as the backend sends it (snapshot `frames.recent` and `frames` messages). */
export interface FrameRecord {
  id: number; c_boot: number; n: number; received_ms: number; delay_ms: number; replayed: boolean;
  via: string; dup: boolean | null; hex: string; size: number;
  header: FrameHeader | null; payload: Record<string, unknown> | null; summary: string;
  rejected: { reason: string } | null;
}

/** A record placed on the shared time axis. `at` is when C got it; `reachedAt` is when the controller did (later for a replayed frame). */
export interface FrameRow extends FrameRecord { key: string; instance: number; at: number; reachedAt: number }

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const whole = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value);

/** True when the value has the shape of a record; anything else the backend might add later is ignored, never half-shown. */
export function isFrameRecord(value: unknown): value is FrameRecord {
  if (!object(value)) return false;
  const v = value;
  if (!whole(v.id) || v.id < 1 || !whole(v.c_boot) || !whole(v.n) || !whole(v.received_ms) || !whole(v.delay_ms)
    || typeof v.replayed !== 'boolean' || typeof v.via !== 'string' || (v.dup !== null && typeof v.dup !== 'boolean')
    || typeof v.hex !== 'string' || !/^([0-9a-f]{2})*$/.test(v.hex) || v.size !== v.hex.length / 2
    || typeof v.summary !== 'string') return false;
  if (v.rejected !== null) return object(v.rejected) && typeof v.rejected.reason === 'string' && v.header === null;
  const h = v.header;
  return object(h) && typeof h.type_name === 'string' && typeof h.src === 'string' && typeof h.dst === 'string'
    && whole(h.version) && whole(h.type) && whole(h.session) && whole(h.seq) && whole(h.ttl) && whole(h.payload_len) && object(v.payload);
}

export function toFrameRow(record: FrameRecord, map: TimeMap, instance: number): FrameRow {
  const at = mapTime(map, record.received_ms);
  return { ...record, key: `${instance}:${record.id}`, instance, at, reachedAt: at + record.delay_ms };
}

/**
 * Add records to the buffer: one copy per (instance, id), kept in the order the controller learned of them, the oldest
 * dropped beyond the cap. Returns `existing` itself when nothing is new, so a repeat does not re-render anything.
 */
export function mergeFrames(existing: FrameRow[], incoming: FrameRow[], cap = FRAME_CAP): FrameRow[] {
  const seen = new Set(existing.map(row => row.key));
  const fresh: FrameRow[] = [];
  for (const row of incoming) if (!seen.has(row.key)) { seen.add(row.key); fresh.push(row); }
  if (!fresh.length) return existing;
  const merged = [...existing, ...fresh].sort((a, b) => a.instance - b.instance || a.id - b.id);
  return merged.length > cap ? merged.slice(merged.length - cap) : merged;
}

/** The documented rule: after a snapshot, records with `id <= frames.last_id` are already in it. */
export const afterSnapshot = (records: FrameRecord[], lastId: number) => records.filter(record => record.id > lastId);

/** A short name for the type column: the API name, readable. */
export const typeLabel = (name: string) => name.replaceAll('_', ' ').toLowerCase().replace(/^./, c => c.toUpperCase());
