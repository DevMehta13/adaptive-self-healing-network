import { describe, expect, it } from 'vitest';
import { ageText, clockText, elapsedText, formatValue, lateLabel, oldText, pathText, readable, seconds, unitOf } from './format';

describe('numbers and units', () => {
  it('uses °C in hardware mode and units in simulation, with the same decimals everywhere', () => {
    expect(unitOf('hardware')).toBe('°C');
    expect(unitOf('simulation')).toBe('units');
    expect(formatValue(19.66, '°C')).toBe('19.7 °C');
    expect(formatValue(19.666, 'units')).toBe('19.67 units');
  });
  it('writes seconds, ages and old figures the same way', () => {
    expect(seconds(4632)).toBe('4.6 s');
    expect(oldText(4300)).toBe('4.3 s old');
    expect(ageText(300)).toBe('just now');
    expect(ageText(6000)).toBe('6 s ago');
    expect(ageText(125_000)).toBe('2 min 5 s ago');
  });
});

describe('words', () => {
  it('has one wording for a late reading, with its delay', () => {
    expect(lateLabel(28_986)).toBe('replayed · delayed 29.0 s');
    expect(lateLabel(10_800)).toBe('replayed · delayed 10.8 s');
  });
  it('writes paths with ? for a relay C cannot name', () => {
    expect(pathText(['A', 'B', 'C'])).toBe('A → B → C');
    expect(pathText(['A', null, 'C'])).toBe('A → ? → C');
  });
  it('makes API words readable', () => expect(readable('lost_contact')).toBe('lost contact'));
});

describe('time formats', () => {
  it('shows the time of day on 24 hours and a dash without a time', () => {
    expect(clockText(new Date(2026, 10, 1, 9, 5, 7).getTime())).toBe('09:05:07');
    expect(clockText(new Date(2026, 10, 1, 21, 45, 0).getTime())).toBe('21:45:00');
    expect(clockText(null)).toBe('—');
  });
  it('shows controller time as mm:ss, past an hour too', () => {
    expect(elapsedText(125_900)).toBe('02:05');
    expect(elapsedText(3_725_000)).toBe('62:05');
    expect(elapsedText(-5)).toBe('00:00');
  });
});
