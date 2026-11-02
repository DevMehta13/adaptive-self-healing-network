import type { FrameRow } from './frames';

/** The 14-byte radio header (docs/PROTOCOL.md): offset, size and what each field means. session and seq are little-endian. */
export type FieldKey = 'version' | 'type' | 'src' | 'dst' | 'session' | 'seq' | 'ttl' | 'payload_len';
export interface FieldSpec { key: FieldKey; label: string; offset: number; size: number; meaning: string }

export const HEADER_SIZE = 14;
export const HEADER_FIELDS: readonly FieldSpec[] = [
  { key: 'version', label: 'version', offset: 0, size: 1, meaning: 'Protocol version. Always 1.' },
  { key: 'type', label: 'type', offset: 1, size: 1, meaning: 'Message type: what the payload is.' },
  { key: 'src', label: 'src', offset: 2, size: 1, meaning: 'The node that created the frame (not the previous hop).' },
  { key: 'dst', label: 'dst', offset: 3, size: 1, meaning: 'The node the frame is for.' },
  { key: 'session', label: 'session', offset: 4, size: 4, meaning: 'Random number src picks at each boot; a new value means src restarted.' },
  { key: 'seq', label: 'seq', offset: 8, size: 4, meaning: 'Counted by src within one session. With src and session it names the packet.' },
  { key: 'ttl', label: 'TTL', offset: 12, size: 1, meaning: 'Hop budget left when C received the frame; each forwarder uses one.' },
  { key: 'payload_len', label: 'length', offset: 13, size: 1, meaning: 'How many payload bytes follow the header.' },
];

/** The payload cell is not a header field but is highlighted the same way. */
export type CellKey = FieldKey | 'payload';
export const NODE_IDS: Record<string, number> = { A: 1, B: 2, C: 3, D: 4 };
export const nodeName = (id: number) => ({ 1: 'A', 2: 'B', 3: 'C', 4: 'D' } as Record<number, string>)[id] ?? String(id);

export const byteCount = (hex: string) => hex.length / 2;
export const byteAt = (hex: string, index: number) => hex.slice(index * 2, index * 2 + 2);
export const bytesOf = (hex: string): string[] => hex.match(/../g) ?? [];

/** Which cell a byte of the frame belongs to. */
export function cellOfByte(index: number): CellKey {
  if (index >= HEADER_SIZE) return 'payload';
  return HEADER_FIELDS.find(field => index >= field.offset && index < field.offset + field.size)!.key;
}

/** First byte (inclusive) and end (exclusive) of a cell in a frame of `size` bytes. */
export function cellRange(key: CellKey, size: number): [number, number] {
  if (key === 'payload') return [HEADER_SIZE, Math.max(HEADER_SIZE, size)];
  const field = HEADER_FIELDS.find(f => f.key === key)!;
  return [field.offset, field.offset + field.size];
}

/** A little-endian unsigned number from `size` bytes at `offset`. */
export function readLittleEndian(hex: string, offset: number, size: number): number {
  let value = 0;
  for (let i = size - 1; i >= 0; i--) value = value * 256 + parseInt(byteAt(hex, offset + i), 16);
  return value;
}

/** The header read straight from the bytes (the dashboard's own check of what the backend decoded). */
export function readHeader(hex: string): Record<FieldKey, number> | null {
  if (byteCount(hex) < HEADER_SIZE) return null;
  const out = {} as Record<FieldKey, number>;
  for (const f of HEADER_FIELDS) out[f.key] = readLittleEndian(hex, f.offset, f.size);
  return out;
}

export const hex32 = (value: number) => `0x${value.toString(16).padStart(8, '0')}`;

/** How a field's value is written under its bytes. */
export function fieldValueText(key: FieldKey, value: number, typeName?: string): string {
  switch (key) {
    case 'type': return `0x${value.toString(16).padStart(2, '0')}${typeName ? ` ${typeName}` : ''}`;
    case 'src': case 'dst': return `${nodeName(value)} (${value})`;
    case 'session': return String(value);
    default: return String(value);
  }
}

export interface Cell { key: CellKey; label: string; offset: number; size: number; hex: string; value: string; meaning: string }

/** The cells of the anatomy strip for a frame: eight header fields, then the payload. Null if the frame has no decoded header. */
export function anatomy(row: FrameRow): Cell[] | null {
  const header = row.header;
  if (!header || byteCount(row.hex) < HEADER_SIZE) return null;
  const cells: Cell[] = HEADER_FIELDS.map(field => ({
    key: field.key, label: field.label, offset: field.offset, size: field.size,
    hex: row.hex.slice(field.offset * 2, (field.offset + field.size) * 2),
    value: fieldValueText(field.key, readLittleEndian(row.hex, field.offset, field.size), header.type_name),
    meaning: field.meaning,
  }));
  cells.push({
    key: 'payload', label: 'payload', offset: HEADER_SIZE, size: byteCount(row.hex) - HEADER_SIZE, hex: row.hex.slice(HEADER_SIZE * 2),
    value: row.summary, meaning: `The ${header.type_name.replaceAll('_', ' ').toLowerCase()} content, ${byteCount(row.hex) - HEADER_SIZE} bytes.`,
  });
  return cells;
}

/**
 * Fields where the bytes and the backend's decoded header disagree (empty when they agree). The backend decodes with its
 * protocol codec; this reads the same bytes with the layout above, so a mismatch would mean one of the two is wrong.
 */
export function headerMismatches(row: FrameRow): FieldKey[] {
  const header = row.header;
  const raw = readHeader(row.hex);
  if (!header || !raw) return [];
  const decoded: Record<FieldKey, number> = {
    version: header.version, type: header.type, src: NODE_IDS[header.src] ?? NaN, dst: NODE_IDS[header.dst] ?? NaN,
    session: header.session, seq: header.seq, ttl: header.ttl, payload_len: header.payload_len,
  };
  return HEADER_FIELDS.map(f => f.key).filter(key => raw[key] !== decoded[key]);
}

/** The DELIVERY_ACK payload (docs/PROTOCOL.md): shown as a format only, because the laptop never receives these frames. */
export const DELIVERY_ACK_PAYLOAD: readonly { label: string; offset: number; size: number; meaning: string }[] = [
  { label: 'origin', offset: 0, size: 1, meaning: 'src of the reading being acknowledged' },
  { label: 'session', offset: 1, size: 4, meaning: 'session of that reading' },
  { label: 'seq', offset: 5, size: 4, meaning: 'seq of that reading' },
];
export const DELIVERY_ACK_PAYLOAD_SIZE = 9;
