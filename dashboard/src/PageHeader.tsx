import type { ReactNode } from 'react';

/** Same heading on every page: what it is, what it shows, how to read it. */
export function PageHeader({ title, shows, reading, aside }: { title: string; shows: string; reading: string; aside?: ReactNode }) {
  return <header className="page-header">
    <div>
      <h1>{title}</h1>
      <p><strong>Shows</strong> {shows}</p>
      <p><strong>How to read it</strong> {reading}</p>
    </div>
    {aside}
  </header>;
}
