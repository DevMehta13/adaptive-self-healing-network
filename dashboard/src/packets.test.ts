import { describe, expect, it } from 'vitest';
import { framesRows } from './framesFixture';
import {
  HEADER_FIELDS, HEADER_SIZE, anatomy, byteAt, bytesOf, cellOfByte, cellRange, headerMismatches, readHeader, readLittleEndian,
} from './packets';

const rows = framesRows();
const data = rows.find(r => r.header?.type_name === 'DATA')!;

describe('header layout', () => {
  it('is the 14-byte header of docs/PROTOCOL.md, without gaps or overlaps', () => {
    let next = 0;
    for (const field of HEADER_FIELDS) { expect(field.offset).toBe(next); next += field.size; }
    expect(next).toBe(HEADER_SIZE);
  });
  it('reads session and seq little-endian', () => {
    // The reference reading: session 1664252997 is 0x63320345, stored as 45 03 32 63.
    expect(readLittleEndian('45033263', 0, 4)).toBe(0x63320345);
    expect(readLittleEndian('0100000000', 0, 4)).toBe(1);
    expect(readLittleEndian('ffffffff', 0, 4)).toBe(4294967295);
  });
});

describe('byte to field', () => {
  it('maps every byte of a frame to exactly one cell, the payload after byte 14', () => {
    expect([...Array(18).keys()].map(cellOfByte)).toEqual([
      'version', 'type', 'src', 'dst', 'session', 'session', 'session', 'session', 'seq', 'seq', 'seq', 'seq', 'ttl', 'payload_len',
      'payload', 'payload', 'payload', 'payload',
    ]);
  });
  it('gives the byte range a field covers (what a hover highlights)', () => {
    expect(cellRange('session', 18)).toEqual([4, 8]);
    expect(cellRange('seq', 18)).toEqual([8, 12]);
    expect(cellRange('ttl', 18)).toEqual([12, 13]);
    expect(cellRange('payload', 18)).toEqual([14, 18]);
    expect(cellRange('payload', 14)).toEqual([14, 14]); // a frame with no payload highlights nothing
  });
  it('covers each byte once: the ranges of all cells tile the frame', () => {
    const size = data.size;
    const keys = ['version', 'type', 'src', 'dst', 'session', 'seq', 'ttl', 'payload_len', 'payload'] as const;
    const covered = keys.flatMap(key => { const [a, b] = cellRange(key, size); return [...Array(b - a).keys()].map(i => a + i); });
    expect(covered).toEqual([...Array(size).keys()]);
  });
});

describe('the anatomy of a recorded frame matches its hex', () => {
  it('fills the cells from the bytes of a DATA frame', () => {
    const cells = anatomy(data)!;
    expect(cells.map(c => c.key)).toEqual(['version', 'type', 'src', 'dst', 'session', 'seq', 'ttl', 'payload_len', 'payload']);
    expect(cells.map(c => c.hex).join('')).toBe(data.hex); // the cells are the bytes, in order, nothing left over
    const h = data.header!;
    const by = Object.fromEntries(cells.map(c => [c.key, c]));
    expect(by.version.value).toBe('1');
    expect(by.type.value).toBe('0x01 DATA');
    expect(by.src.value).toBe(`${h.src} (${readLittleEndian(data.hex, 2, 1)})`);
    expect(by.session.value).toBe(String(h.session));
    expect(by.session.hex).toBe(data.hex.slice(8, 16));
    expect(by.seq.value).toBe(String(h.seq));
    expect(by.ttl.value).toBe(String(h.ttl));
    expect(by.payload_len.value).toBe('4');
    expect(by.payload.size).toBe(4);
  });
  it('agrees with the backend’s decoded header for every recorded frame', () => {
    for (const row of rows) {
      expect(headerMismatches(row), `frame ${row.id}`).toEqual([]);
      const raw = readHeader(row.hex)!;
      expect(raw.payload_len).toBe(row.size - HEADER_SIZE);
      expect(byteAt(row.hex, 0)).toBe('01');
      expect(bytesOf(row.hex)).toHaveLength(row.size);
    }
  });
  it('notices when the bytes and the decoded header disagree', () => {
    const tampered = { ...data, header: { ...data.header!, seq: data.header!.seq + 1, ttl: 1 } };
    expect(headerMismatches(tampered)).toEqual(['seq', 'ttl']);
  });
  it('has no cells for a frame the codec refused', () => {
    const refused = { ...data, header: null, payload: null, dup: null, rejected: { reason: 'bad_version' } };
    expect(anatomy(refused)).toBeNull();
    expect(headerMismatches(refused)).toEqual([]);
  });
  it('lays out a telemetry frame: 32 payload bytes after the header', () => {
    const telemetry = rows.find(r => r.header?.type_name === 'TELEMETRY')!;
    const cells = anatomy(telemetry)!;
    expect(cells.at(-1)!.size).toBe(32);
    expect(cells.map(c => c.hex).join('')).toBe(telemetry.hex);
  });
});
