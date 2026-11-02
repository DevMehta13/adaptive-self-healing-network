import { describe, expect, it } from 'vitest';
import { documentTitle, hrefFor, pages, parseRoute } from './route';

describe('parseRoute', () => {
  it('maps every page hash to its page', () => {
    for (const page of pages) expect(parseRoute(hrefFor(page.id))).toBe(page.id);
  });
  it('treats an empty hash, "#" and "#/" as the overview', () => {
    for (const hash of ['', '#', '#/']) expect(parseRoute(hash)).toBe('overview');
  });
  it('sends unknown hashes to the overview', () => {
    for (const hash of ['#/nope', '#/network/extra', '#network', '#/NETWORK', '#/readings.html']) expect(parseRoute(hash)).toBe('overview');
  });
  it('ignores a trailing slash, query and second fragment', () => {
    expect(parseRoute('#/delivery/')).toBe('delivery');
    expect(parseRoute('#/runs?run=2')).toBe('runs');
    expect(parseRoute('#/readings#top')).toBe('readings');
  });
});

describe('page metadata', () => {
  it('has unique ids and paths', () => {
    expect(new Set(pages.map(p => p.id)).size).toBe(pages.length);
    expect(new Set(pages.map(p => p.path)).size).toBe(pages.length);
  });
  it('lists the pages in the agreed order, Packets between Readings and Delivery', () => {
    expect(pages.map(p => p.label)).toEqual(['Overview', 'Network', 'Readings', 'Packets', 'Delivery', 'Saved runs']);
    expect(parseRoute('#/packets')).toBe('packets');
    expect(documentTitle('packets')).toBe('Packets · Relay');
  });
  it('builds a distinct document title per page', () => {
    expect(new Set(pages.map(p => documentTitle(p.id))).size).toBe(pages.length);
    expect(documentTitle('runs')).toBe('Saved runs · Relay');
  });
});
