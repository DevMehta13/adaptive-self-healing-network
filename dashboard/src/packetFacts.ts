import { FRAME_TYPES } from './frames';
import type { FrameRow, FrameTypeName } from './frames';

/** Data TTL when a node creates a frame (docs/PROTOCOL.md); a frame that arrives with TTL t has been transmitted 8 - t + 1 times. */
export const DATA_TTL = 8;
export const transmissions = (ttl: number) => DATA_TTL - ttl + 1;

const decoded = (rows: readonly FrameRow[]) => rows.filter(row => row.header !== null);

export interface FrameSummary {
  total: number;
  byType: Record<FrameTypeName, number>;
  duplicates: number;
  replayed: number;
  rejected: number;
  /** TTL on arrival → how many decoded frames, highest TTL first. */
  ttl: { ttl: number; count: number }[];
}

/** Counts over the frames in the buffer (not the backend's cumulative counts, which only arrive with a full snapshot). */
export function summarize(rows: readonly FrameRow[]): FrameSummary {
  const byType = Object.fromEntries(FRAME_TYPES.map(type => [type, 0])) as Record<FrameTypeName, number>;
  const ttls = new Map<number, number>();
  let duplicates = 0, replayed = 0, rejected = 0;
  for (const row of rows) {
    if (row.replayed) replayed++;
    if (row.rejected) { rejected++; continue; }
    const name = row.header!.type_name as FrameTypeName;
    if (name in byType) byType[name]++;
    if (row.dup === true) duplicates++;
    ttls.set(row.header!.ttl, (ttls.get(row.header!.ttl) ?? 0) + 1);
  }
  return {
    total: rows.length, byType, duplicates, replayed, rejected,
    ttl: [...ttls].map(([ttl, count]) => ({ ttl, count })).sort((a, b) => b.ttl - a.ttl),
  };
}

export interface RebootFact { src: string; from: number; to: number; at: number; id: number }
export interface DuplicateFact { src: string; session: number; seq: number; ids: number[]; flagged: number }
export interface Facts {
  /** DATA frames by TTL on arrival, with the transmissions that implies. */
  arrival: { ttl: number; count: number; transmissions: number; via: string[] }[];
  dataFrames: number;
  reboots: RebootFact[];
  duplicates: DuplicateFact[];
  replayed: { count: number; maxDelayMs: number; longest: FrameRow | null; types: Partial<Record<FrameTypeName, number>> };
}

/** What the frames in the buffer show. Every statement comes from header fields and flags C reported, nothing is inferred beyond them. */
export function frameFacts(rows: readonly FrameRow[]): Facts {
  const ok = decoded(rows);
  const data = ok.filter(row => row.header!.type_name === 'DATA');

  const arrival = new Map<number, { count: number; via: Set<string> }>();
  for (const row of data) {
    const entry = arrival.get(row.header!.ttl) ?? { count: 0, via: new Set<string>() };
    entry.count++; entry.via.add(row.via);
    arrival.set(row.header!.ttl, entry);
  }

  // A reboot is a new session from the same src. Order by when C got each frame, so a replayed old-session frame that the
  // controller learned of late does not look like a second reboot.
  const reboots: RebootFact[] = [];
  const bySrc = new Map<string, FrameRow[]>();
  for (const row of ok) bySrc.set(row.header!.src, [...(bySrc.get(row.header!.src) ?? []), row]);
  for (const [src, list] of bySrc) {
    const ordered = [...list].sort((a, b) => a.at - b.at || a.id - b.id);
    for (let i = 1; i < ordered.length; i++) {
      const before = ordered[i - 1].header!.session, now = ordered[i].header!.session;
      if (before !== now) reboots.push({ src, from: before, to: now, at: ordered[i].at, id: ordered[i].id });
    }
  }
  reboots.sort((a, b) => a.at - b.at);

  // The identity of a packet is (src, session, seq). Copies are what C's filter caught (dup) or what the buffer holds twice.
  const identities = new Map<string, DuplicateFact>();
  for (const row of data) {
    const h = row.header!;
    const id = `${h.src}/${h.session}/${h.seq}`;
    const entry = identities.get(id) ?? { src: h.src, session: h.session, seq: h.seq, ids: [], flagged: 0 };
    entry.ids.push(row.id);
    if (row.dup === true) entry.flagged++;
    identities.set(id, entry);
  }
  const duplicates = [...identities.values()].filter(entry => entry.flagged > 0 || entry.ids.length > 1);

  const late = ok.filter(row => row.replayed);
  const types: Partial<Record<FrameTypeName, number>> = {};
  for (const row of late) { const t = row.header!.type_name as FrameTypeName; types[t] = (types[t] ?? 0) + 1; }
  const longest = late.reduce<FrameRow | null>((best, row) => !best || row.delay_ms > best.delay_ms ? row : best, null);

  return {
    arrival: [...arrival].map(([ttl, v]) => ({ ttl, count: v.count, transmissions: transmissions(ttl), via: [...v.via].sort() })).sort((a, b) => b.ttl - a.ttl),
    dataFrames: data.length, reboots, duplicates,
    replayed: { count: late.length, maxDelayMs: longest?.delay_ms ?? 0, longest, types },
  };
}

export interface AckEvidence { row: FrameRow; acknowledged: number; created: number; change: number | null }

/**
 * A's acknowledgement counter as its TELEMETRY frames carried it, newest first. The laptop never sees a DELIVERY_ACK
 * (C creates them and sends them to A), so this counter is the indirect evidence that acknowledgements reached A.
 * `change` is the step since A's previous telemetry in the same session (null for the first or after a reboot).
 */
export function ackEvidence(rows: readonly FrameRow[], limit = 8): AckEvidence[] {
  const mine = decoded(rows)
    .filter(row => row.header!.type_name === 'TELEMETRY' && row.header!.src === 'A')
    .sort((a, b) => a.at - b.at || a.id - b.id);
  const list = mine.map((row, i): AckEvidence => {
    const counters = (row.payload?.counters ?? {}) as Record<string, number>;
    const previous = i > 0 ? mine[i - 1] : null;
    const before = previous && previous.header!.session === row.header!.session
      ? ((previous.payload?.counters ?? {}) as Record<string, number>).acknowledged : undefined;
    return { row, acknowledged: counters.acknowledged ?? 0, created: counters.created ?? 0, change: before === undefined ? null : (counters.acknowledged ?? 0) - before };
  });
  return list.reverse().slice(0, limit);
}

/** The filter choices: every type, plus the rejected frames. */
export type FrameFilter = 'all' | FrameTypeName | 'rejected';
export const matchesFilter = (row: FrameRow, filter: FrameFilter) =>
  filter === 'all' ? true : filter === 'rejected' ? row.rejected !== null : row.header?.type_name === filter;
