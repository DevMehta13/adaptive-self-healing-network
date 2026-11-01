import type { Count, NetworkSnapshot } from './fixtures';
import { oldText } from './format';

export function CountCard({ label, count, note }: { label: string; count: Count | null | undefined; note?: string }) {
  return <div className="count-card"><span>{label}</span><strong>{count?.value ?? '—'}</strong>{note && <p className="count-note">{note}</p>}<small>{count ? `${count.known_by}${count.age_ms != null ? ` · ${oldText(count.age_ms)}` : ''}` : 'Awaiting telemetry'}</small></div>;
}

export function StaleNotice({ view }: { view: NetworkSnapshot }) {
  return view.gateway.state === 'lost' ? <div className="gateway-alert" role="note"><strong>Network view stale.</strong> C has stopped reporting to the controller. The graph and counts show the last information it received; they do not reveal what the boards are doing now.</div> : null;
}

export function CountsGrid({ view }: { view: NetworkSnapshot }) {
  return <div className="counts-grid"><CountCard label="Created at A" count={view.counts.created} /><CountCard label="Delivered at C" count={view.counts.delivered} /><CountCard label="Acknowledged at A" count={view.counts.acknowledged} /><CountCard label="Lost at A" count={view.counts.lost} /><CountCard label="Lost at C" count={view.counts.lost_at_c} /><CountCard label="Lost in total" count={view.counts.lost_total} /><CountCard label="No route at A" count={view.counts.no_route} /><CountCard label="Delivered late" count={view.counts.delivered_late} /></div>;
}
