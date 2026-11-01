// One place for how the live pages write numbers, ages, paths and the "replayed" wording, so every page agrees.

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

/** "just now", "6 s ago", "2 min 5 s ago". */
export function ageText(ms: number): string {
  if (ms < 1500) return 'just now';
  if (ms < 60_000) return `${Math.round(ms / 1000)} s ago`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes} min ${Math.round((ms % 60_000) / 1000)} s ago`;
}

/** How old a figure was when the snapshot was taken: "4.3 s old". */
export const oldText = (ms: number) => `${seconds(ms)} old`;

export const readable = (value: string) => value.replaceAll('_', ' ');

export const pathText = (path: readonly (string | null)[]) => path.map(n => n ?? '?').join(' → ');

export const unitOf = (mode: string) => mode === 'hardware' ? '°C' : 'units';

/** The number alone: one decimal for °C, two for simulated units. */
export const formatNumber = (value: number, unit: string) => value.toFixed(unit === 'units' ? 2 : 1);

/** °C with one decimal (whole-degree sensor steps stay readable); simulated units with two. */
export const formatValue = (value: number, unit: string) => `${formatNumber(value, unit)} ${unit}`;

/** The API says to label a late reading "delayed" and show delay_ms. */
export const lateLabel = (delayMs: number) => `replayed · delayed ${seconds(delayMs)}`;

const pad2 = (n: number) => String(n).padStart(2, '0');

/** Wall-clock time of day, 24 hours: "11:08:36". */
export function clockText(epochMs: number | null): string {
  if (epochMs === null) return '—';
  const d = new Date(epochMs);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** A length of controller time as mm:ss (minutes may pass 59): "15:35". */
export function elapsedText(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${pad2(Math.floor(total / 60))}:${pad2(total % 60)}`;
}
