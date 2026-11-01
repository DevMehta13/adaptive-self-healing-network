export type Theme = 'light' | 'dark';

const KEY = 'relay-theme';

/** The remembered choice, or null if none (or storage is unavailable). */
export function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch { return null; }
}

export function rememberTheme(theme: Theme) {
  try { window.localStorage.setItem(KEY, theme); } catch { /* the page works without storage */ }
}

export const systemTheme = (): Theme =>
  typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

export const initialTheme = (): Theme => storedTheme() ?? systemTheme();

export const applyTheme = (theme: Theme) => { document.documentElement.dataset.theme = theme; };
