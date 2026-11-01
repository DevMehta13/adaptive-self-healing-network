import { describe, expect, it } from 'vitest';
import css from './styles.css?raw';

// The theme tokens are the only colours the live pages use, so checking their pairs checks the pages.
function tokens(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})(?![0-9a-fA-F])/g)) out[m[1]] = m[2].toLowerCase();
  return out;
}
const light = tokens(css.slice(css.indexOf(':root {'), css.indexOf(":root[data-theme='dark']")));
const dark = { ...light, ...tokens(css.slice(css.indexOf(":root[data-theme='dark']"), css.indexOf('* { box-sizing')) ) };

const channel = (v: number) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const luminance = (hex: string) => { const n = parseInt(hex.slice(1), 16); return 0.2126 * channel(n >> 16) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255); };
export const ratio = (a: string, b: string) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };

// [foreground, background, minimum ratio]: 4.5 for text, 3 for graphics and large text (WCAG 2.1 AA).
const text: [string, string][] = [
  ['ink', 'bg'], ['ink', 'surface'], ['ink-2', 'surface'], ['ink-2', 'surface-4'], ['muted', 'bg'], ['muted', 'surface'], ['muted', 'surface-2'], ['muted', 'surface-3'], ['muted', 'surface-4'],
  ['muted-2', 'surface'], ['muted-2', 'bg'], ['muted-2', 'surface-2'],
  ['accent-text', 'surface'], ['accent-text', 'bg'], ['accent-text', 'accent-soft'], ['accent-text', 'surface-3'], ['accent-text', 'surface-4'],
  ['on-accent', 'accent'], ['on-accent', 'accent-ink'],
  ['ok-ink', 'ok-bg'], ['warn-ink', 'warn-bg'], ['warn-ink', 'warn-soft'], ['warn-ink', 'surface'], ['bad-ink', 'bad-bg'], ['bad-ink', 'surface'], ['bad-ink', 'bg'],
  ['series-b', 'surface-3'], ['series-b', 'surface'], ['muted', 'warn-soft'], ['ink', 'warn-soft'],
];
const graphics: [string, string][] = [
  ['series-b', 'surface'], ['series-d', 'surface'], ['series-other', 'surface'], ['link-up', 'surface'], ['node-stroke', 'surface'],
  ['bad-stroke', 'surface'], ['warn-stroke', 'surface'], ['muted-2', 'surface'], ['accent-text', 'surface'], ['focus', 'surface'], ['focus', 'bg'],
  ['line-strong', 'surface'],
];

for (const [name, theme] of [['light', light], ['dark', dark]] as const) {
  describe(`${name} theme contrast`, () => {
    it.each(text)('text %s on %s is at least 4.5:1', (fg, bg) => {
      expect(theme[fg], fg).toBeTruthy(); expect(theme[bg], bg).toBeTruthy();
      expect(ratio(theme[fg], theme[bg])).toBeGreaterThanOrEqual(4.5);
    });
    it.each(graphics.filter(([fg]) => fg !== 'line-strong'))('graphic %s on %s is at least 3:1', (fg, bg) => {
      expect(ratio(theme[fg], theme[bg])).toBeGreaterThanOrEqual(3);
    });
  });
}
