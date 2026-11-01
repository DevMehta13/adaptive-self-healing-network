export type PageId = 'overview' | 'network' | 'readings' | 'delivery' | 'runs';

export const pages: { id: PageId; path: string; label: string; title: string }[] = [
  { id: 'overview', path: '/', label: 'Overview', title: 'Overview' },
  { id: 'network', path: '/network', label: 'Network', title: 'Network' },
  { id: 'readings', path: '/readings', label: 'Readings', title: 'Readings' },
  { id: 'delivery', path: '/delivery', label: 'Delivery', title: 'Delivery' },
  { id: 'runs', path: '/runs', label: 'Saved runs', title: 'Saved runs' },
];

/** Hash such as "#/network" (or "#/network?x=1", "#/network/") to a page; anything unknown is the Overview. */
export function parseRoute(hash: string): PageId {
  const path = hash.replace(/^#/, '').split(/[?#]/)[0].replace(/\/+$/, '') || '/';
  return pages.find(page => page.path === path)?.id ?? 'overview';
}

export const hrefFor = (id: PageId) => `#${pages.find(page => page.id === id)!.path}`;

export const documentTitle = (id: PageId) => `${pages.find(page => page.id === id)!.title} · Relay`;
